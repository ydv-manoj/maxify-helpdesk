import asyncio
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.agent import event_dict, run_turn
from app.db import models as m
from app.db.seed import seed
from app.db.session import get_db
from app.events import bus, sse

router = APIRouter(prefix="/api")
SIMULATED_FAILURES: set[str] = set()   # in-memory toggle for demo/evals ("order_lookup")


class ChatIn(BaseModel):
    message: str
    mode: Literal["lab", "live"] = "lab"  # lab = deterministic mock, live = real LLM (LIVE_LLM_PROVIDER)
    conversation_id: Optional[int] = None
    customer_id: Optional[str] = None   # simulates a signed-in session
    guest_email: Optional[str] = None   # guest: verified against the order's email


class FailuresIn(BaseModel):
    order_lookup: bool


def get_llms():
    from app.main import lab_decider, lab_llm, live_decider, live_llm
    return {"lab": (lab_llm, lab_decider), "live": (live_llm, live_decider)}


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), llms=Depends(get_llms)):
    llm, decider = llms[body.mode]
    if body.conversation_id:
        conv = db.get(m.Conversation, body.conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
    else:  # identity is fixed when the conversation starts; it can't be changed mid-chat
        if body.customer_id and not db.get(m.Customer, body.customer_id):
            raise HTTPException(400, "unknown customer")
        conv = m.Conversation(customer_id=body.customer_id, guest_email=None if body.customer_id else body.guest_email,
                              mode=body.mode)
        db.add(conv)
        db.commit()
    return run_turn(db, conv, body.message, llm, failures=set(SIMULATED_FAILURES), decider=decider)


@router.get("/customers")
def customers(db: Session = Depends(get_db)):
    out = []
    for c in db.query(m.Customer).order_by(m.Customer.id):
        out.append({"id": c.id, "name": c.name, "email": c.email,
                    "orders": [{"id": o.id, "status": o.status, "total": o.total,
                                "items": [{"name": i.name, "sale": i.was_on_sale} for i in o.items]}
                               for o in sorted(c.orders, key=lambda o: o.id)]})
    guests = db.query(m.Order).filter(m.Order.customer_id.is_(None)).all()
    return {"customers": out, "guest_orders": [{"id": o.id, "email": o.email, "status": o.status} for o in guests]}


@router.get("/conversations/{conv_id}/events")
def conversation_events(conv_id: int, db: Session = Depends(get_db)):
    evs = db.query(m.AuditEvent).filter_by(conversation_id=conv_id).order_by(m.AuditEvent.id).all()
    return [event_dict(e) for e in evs]


@router.get("/queue")
def queue(db: Session = Depends(get_db)):
    """Human-in-the-loop view: actions awaiting approval + open escalations."""
    acts = db.query(m.Action).order_by(m.Action.id.desc()).limit(50).all()
    escs = db.query(m.Escalation).order_by(m.Escalation.id.desc()).limit(50).all()
    return {
        "actions": [{"id": a.id, "order_id": a.order_id, "kind": a.kind, "amount": a.amount, "status": a.status,
                     "reason": a.reason, "skus": a.skus, "decided_by": a.decided_by} for a in acts],
        "escalations": [{"id": e.id, "order_id": e.order_id, "category": e.category, "reason": e.reason,
                         "status": e.status, "action_id": e.action_id} for e in escs],
    }


class CommentIn(BaseModel):
    author: str = Field(min_length=1, max_length=60)
    body: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=60)
    note: str = Field(min_length=1, max_length=2000, description="Shared with the customer")
    decision: Optional[Literal["approve", "reject"]] = None   # required when the ticket has a pending action


def _ticket(db: Session, e: m.Escalation, full: bool = False) -> dict:
    conv = db.get(m.Conversation, e.conversation_id) if e.conversation_id else None
    cust = db.get(m.Customer, conv.customer_id) if conv and conv.customer_id else None
    act = db.get(m.Action, e.action_id) if e.action_id else None
    t = {"id": e.id, "category": e.category, "reason": e.reason, "status": e.status, "order_id": e.order_id,
         "created_at": e.created_at, "resolved_at": e.resolved_at, "resolved_by": e.resolved_by,
         "resolution_note": e.resolution_note, "followup_sent": e.followup_sent,
         "conversation_id": e.conversation_id, "mode": conv.mode if conv else None,
         "customer": {"id": cust.id, "name": cust.name, "email": cust.email} if cust
         else {"id": None, "name": "Guest", "email": conv.guest_email if conv else None},
         "action": {"id": act.id, "kind": act.kind, "amount": act.amount, "status": act.status, "skus": act.skus,
                    "order_id": act.order_id, "reason": act.reason} if act else None}
    if full and conv:
        msgs = db.query(m.ChatMessage).filter_by(conversation_id=conv.id).order_by(m.ChatMessage.id).all()
        t["transcript"] = [{"role": x.role, "text": x.payload.get("content", ""), "at": x.created_at}
                           for x in msgs if x.role in ("user", "assistant") and x.payload.get("content")]
        t["decisions"] = [{"answers": (ev.output or {}).get("answers"), "provider": ev.provider, "latency_ms": ev.latency_ms}
                          for ev in db.query(m.AuditEvent).filter_by(conversation_id=conv.id, kind="decision", name="triage")
                          .order_by(m.AuditEvent.id)]
        t["notes"] = [{"author": n.author, "body": n.body, "at": n.created_at}
                      for n in db.query(m.EscalationNote).filter_by(escalation_id=e.id).order_by(m.EscalationNote.id)]
    return t


@router.get("/desk/tickets")
def desk_tickets(db: Session = Depends(get_db)):
    escs = db.query(m.Escalation).order_by(m.Escalation.status.desc(), m.Escalation.id.desc()).limit(200).all()
    return [_ticket(db, e) for e in escs]   # "open" sorts before "resolved" with desc()


@router.get("/desk/tickets/{esc_id}")
def desk_ticket(esc_id: int, db: Session = Depends(get_db)):
    e = db.get(m.Escalation, esc_id)
    if not e:
        raise HTTPException(404, "ticket not found")
    return _ticket(db, e, full=True)


@router.post("/desk/tickets/{esc_id}/comments")
def desk_comment(esc_id: int, body: CommentIn, db: Session = Depends(get_db)):
    if not db.get(m.Escalation, esc_id):
        raise HTTPException(404, "ticket not found")
    db.add(m.EscalationNote(escalation_id=esc_id, author=body.author, body=body.body))
    db.commit()
    bus.publish("desk", {"type": "ticket_updated", "escalation_id": esc_id})
    return {"ok": True}


@router.post("/desk/tickets/{esc_id}/resolve")
def desk_resolve(esc_id: int, body: ResolveIn, background: BackgroundTasks, db: Session = Depends(get_db),
                 llms=Depends(get_llms)):
    e = db.get(m.Escalation, esc_id)
    if not e:
        raise HTTPException(404, "ticket not found")
    if e.status != "open":
        raise HTTPException(409, f"ticket is already {e.status}")
    act = db.get(m.Action, e.action_id) if e.action_id else None
    if act and act.status == "awaiting_approval":
        if not body.decision:
            raise HTTPException(422, f"ticket has a pending {act.kind}: choose approve or reject")
        act.status = "approved" if body.decision == "approve" else "rejected"
        act.decided_by = body.reviewer
    e.status, e.resolution_note, e.resolved_by, e.resolved_at = "resolved", body.note, body.reviewer, datetime.now()
    db.add(m.EscalationNote(escalation_id=e.id, author=body.reviewer, body=f"[resolved{' + ' + body.decision if body.decision else ''}] {body.note}"))
    db.add(m.AuditEvent(conversation_id=e.conversation_id, kind="action_decision", name="ticket_resolved",
                        input={"escalation_id": e.id, "reviewer": body.reviewer, "decision": body.decision, "note": body.note},
                        output={"ticket": "resolved", "action_status": act.status if act else None}))
    db.commit()
    bus.publish("desk", {"type": "ticket_updated", "escalation_id": e.id})
    from app.agent.followup import deliver_followup
    background.add_task(deliver_followup, e.id, llms)   # agent replies to the customer after the response is sent
    return _ticket(db, e)


@router.get("/stream")
async def stream(topic: str, request: Request):
    """Server-Sent Events. topic=desk for staff, topic=conv:<id> for a customer chat."""
    if topic != "desk" and not topic.startswith("conv:"):
        raise HTTPException(400, "topic must be 'desk' or 'conv:<id>'")

    async def gen():
        async with bus.subscribe(topic) as q:
            yield sse({"type": "ready", "topic": topic})
            while not await request.is_disconnected():
                try:
                    yield sse(await asyncio.wait_for(q.get(), timeout=15))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/failures")
def get_failures():
    return {"order_lookup": "order_lookup" in SIMULATED_FAILURES}


@router.post("/failures")
def set_failures(body: FailuresIn):
    (SIMULATED_FAILURES.add if body.order_lookup else SIMULATED_FAILURES.discard)("order_lookup")
    return get_failures()


@router.post("/reset")
def reset():
    seed()
    SIMULATED_FAILURES.clear()
    return {"ok": True}
