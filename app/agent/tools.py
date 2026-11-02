"""Agent tools. ALL safety lives here, not in the prompt:
- identity comes from the session (ToolContext), never from model arguments
- policy (30-day window, sale = exchange-only unless defective, S$200 approval threshold)
- idempotency (unique key per order+action+items)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import models as m
from app.llm.types import ToolSpec

RETURN_WINDOW_DAYS = 30
APPROVAL_THRESHOLD_SGD = 200.0


@dataclass
class ToolContext:
    db: Session
    conversation_id: Optional[int]
    customer_id: Optional[str] = None
    guest_email: Optional[str] = None
    failures: set[str] = field(default_factory=set)   # simulated outages, e.g. {"order_lookup"}
    outbox: list[tuple[str, dict]] = field(default_factory=list)  # events published after commit


class ToolUnavailable(Exception):
    pass


class AccessDenied(Exception):
    pass


def _audit_guardrail(ctx: ToolContext, name: str, detail: dict) -> None:
    ctx.db.add(m.AuditEvent(conversation_id=ctx.conversation_id, kind="guardrail", name=name, input=detail, ok=False))


def _check_outage(ctx: ToolContext) -> None:
    env = {s.strip() for s in os.getenv("FAIL_TOOLS", "").split(",") if s.strip()}
    if "order_lookup" in ctx.failures | env:
        raise ToolUnavailable("order system timeout (simulated)")


def normalize_order_id(raw: str) -> str:
    """'10095', '#10095', 'ord 10095', 'ORD10095' -> 'ORD-10095'. Models (and people) drop the prefix."""
    digits = re.sub(r"\D", "", raw or "")
    return f"ORD-{digits}" if digits else (raw or "").strip().upper()


def _load_order(ctx: ToolContext, order_id: str) -> m.Order:
    _check_outage(ctx)
    order_id = normalize_order_id(order_id)
    order = ctx.db.get(m.Order, order_id)
    if order is not None:
        if ctx.customer_id and order.customer_id == ctx.customer_id:
            return order
        if not ctx.customer_id and ctx.guest_email and order.email.lower() == ctx.guest_email.strip().lower():
            return order
        _audit_guardrail(ctx, "order_access_denied",
                         {"order_id": order_id, "customer_id": ctx.customer_id, "guest_email": ctx.guest_email})
    # Same answer whether the order doesn't exist or isn't theirs: no information leak.
    raise AccessDenied(order_id)


def _not_found(order_id: str) -> dict:
    return {"ok": False, "error_type": "not_found_or_not_authorised",
            "message": f"I couldn't find an order {order_id} linked to your account or email. "
                       "Please double-check the order ID (and the email used at checkout)."}


def _order_summary(o: m.Order) -> dict:
    return {"order_id": o.id, "status": o.status, "total_sgd": o.total,
            "placed_at": o.placed_at.date().isoformat(),
            "delivered_at": o.delivered_at.date().isoformat() if o.delivered_at else None,
            "items": [{"sku": i.sku, "name": i.name, "qty": i.qty, "unit_price": i.unit_price,
                       "sale_item": i.was_on_sale} for i in o.items]}


def _item_eligibility(o: m.Order, item: m.OrderItem, defective: bool) -> tuple[list[str], str]:
    """Returns (allowed options, explanation)."""
    if o.status != "delivered" or not o.delivered_at:
        return [], f"order is {o.status}, returns open once it is delivered"
    days = (datetime.now() - o.delivered_at).days
    if days > RETURN_WINDOW_DAYS:
        return [], f"delivered {days} days ago, outside the {RETURN_WINDOW_DAYS}-day window"
    if item.was_on_sale and not defective:
        return ["exchange"], "sale item: exchange only (refund only if defective)"
    return ["refund", "exchange"], f"within window ({days} days since delivery)"


# ---------------- argument schemas (validated before execution) ----------------
class NoArgs(BaseModel):
    pass


class OrderIdArgs(BaseModel):
    order_id: str = Field(description="Order ID, e.g. ORD-10021")


class RefundArgs(BaseModel):
    order_id: str
    item_skus: list[str] = Field(default_factory=list, description="SKUs to refund; empty = all items")
    reason: str = Field(description="Customer's reason in their words")
    defective: bool = Field(default=False, description="True only if the customer says the item is faulty/broken")
    requested_amount: Optional[float] = Field(default=None, description="Amount the customer asked for, SGD, if stated")


class ExchangeArgs(BaseModel):
    order_id: str
    item_sku: str
    details: str = Field(description="What they want instead, e.g. 'size M'")


class CancelArgs(BaseModel):
    order_id: str
    reason: str = ""


class PolicyArgs(BaseModel):
    topic: Literal["returns", "sale_items", "defective", "cancellation", "refund_approval", "shipping"]


class SupportUpdateArgs(BaseModel):
    escalation_id: int = Field(description="Ticket number, e.g. 2 for ESC-2")


class EscalateArgs(BaseModel):
    reason: str
    category: Literal["approval_needed", "tool_failure", "out_of_policy", "customer_request", "other"] = "other"
    order_id: Optional[str] = None


# ---------------- tools ----------------
def list_my_orders(ctx: ToolContext, _: NoArgs) -> dict:
    _check_outage(ctx)
    if not ctx.customer_id:
        return {"ok": False, "error_type": "needs_verification",
                "message": "As a guest, please give me your order ID so I can verify it against your email."}
    orders = ctx.db.query(m.Order).filter_by(customer_id=ctx.customer_id).order_by(m.Order.placed_at.desc()).all()
    return {"ok": True, "orders": [_order_summary(o) for o in orders],
            "message": f"You have {len(orders)} order(s): " +
                       "; ".join(f"{o.id} ({o.status}, S${o.total:.2f})" for o in orders)}


def get_order_status(ctx: ToolContext, a: OrderIdArgs) -> dict:
    o = _load_order(ctx, a.order_id)
    # build only the message for the current status: unshipped orders have no shipped_at to format
    if o.status == "processing":
        msg = f"Order {o.id} is being prepared and hasn't shipped yet. It can still be cancelled."
    elif o.status == "shipped" and o.shipped_at:
        msg = (f"Order {o.id} shipped on {o.shipped_at:%d %b} with {o.carrier} (tracking {o.tracking_no}). "
               "Expected within 2-4 working days of shipping.")
    elif o.status == "delivered" and o.delivered_at:
        msg = f"Order {o.id} was delivered on {o.delivered_at:%d %b}."
    elif o.status == "cancelled":
        msg = f"Order {o.id} was cancelled."
    else:
        msg = f"Order {o.id} status: {o.status}."
    return {"ok": True, **_order_summary(o), "carrier": o.carrier, "tracking_no": o.tracking_no, "message": msg}


def check_return_eligibility(ctx: ToolContext, a: OrderIdArgs) -> dict:
    o = _load_order(ctx, a.order_id)
    rows = []
    for i in o.items:
        opts, why = _item_eligibility(o, i, defective=False)
        rows.append({"sku": i.sku, "name": i.name, "options": opts, "why": why})
    return {"ok": True, "order_id": o.id, "items": rows,
            "message": "; ".join(f"{r['name']}: {', '.join(r['options']) or 'not returnable'} ({r['why']})" for r in rows)}


def _existing_action(ctx: ToolContext, key: str) -> Optional[m.Action]:
    return ctx.db.query(m.Action).filter_by(idempotency_key=key).first()


def _duplicate(act: m.Action) -> dict:
    return {"ok": True, "duplicate": True, "action_id": act.id, "status": act.status, "amount_sgd": act.amount,
            "message": f"You already have a {act.kind} request (#{act.id}) for order {act.order_id}, "
                       f"status: {act.status.replace('_', ' ')}. I haven't created a second one."}


def _escalate(ctx, category, reason, order_id=None, action_id=None) -> m.Escalation:
    esc = (ctx.db.query(m.Escalation)
           .filter_by(conversation_id=ctx.conversation_id, category=category, order_id=order_id, status="open")
           .first())
    if esc:
        return esc
    esc = m.Escalation(conversation_id=ctx.conversation_id, order_id=order_id, action_id=action_id,
                       category=category, reason=reason)
    ctx.db.add(esc)
    ctx.db.flush()
    ctx.outbox.append(("desk", {"type": "ticket_created", "escalation_id": esc.id, "category": category,
                                "order_id": order_id, "conversation_id": ctx.conversation_id}))
    return esc


def request_refund(ctx: ToolContext, a: RefundArgs) -> dict:
    o = _load_order(ctx, a.order_id)
    by_sku = {i.sku: i for i in o.items}
    skus = [s.strip().upper() for s in a.item_skus] or list(by_sku)
    unknown = [s for s in skus if s not in by_sku]
    if unknown:
        return {"ok": False, "error_type": "invalid_item", "message": f"Items {unknown} are not in order {o.id}."}

    key = f"refund:{o.id}:{','.join(sorted(skus))}"
    if (act := _existing_action(ctx, key)):
        return _duplicate(act)

    eligible, denied = [], []
    for s in skus:
        opts, why = _item_eligibility(o, by_sku[s], a.defective)
        (eligible if "refund" in opts else denied).append({"sku": s, "name": by_sku[s].name, "why": why,
                                                           "options": opts})
    if not eligible:
        alt = " You can exchange it for a different size or colour instead." if any(d["options"] for d in denied) else ""
        return {"ok": False, "error_type": "policy_denied", "denied": denied,
                "message": "I can't refund this: " + "; ".join(f"{d['name']} - {d['why']}" for d in denied) + "." + alt}

    eligible_total = round(sum(by_sku[e["sku"]].unit_price * by_sku[e["sku"]].qty for e in eligible), 2)
    amount = eligible_total
    note = ""
    if a.requested_amount is not None and a.requested_amount < eligible_total:
        amount = round(a.requested_amount, 2)
    elif a.requested_amount is not None and a.requested_amount > eligible_total:
        note = f" You asked for S${a.requested_amount:.2f}, but the most I can refund is S${eligible_total:.2f}."

    sale_defect = a.defective and any(by_sku[e["sku"]].was_on_sale for e in eligible)
    needs_approval = amount > APPROVAL_THRESHOLD_SGD or sale_defect
    act = m.Action(conversation_id=ctx.conversation_id, order_id=o.id, kind="refund",
                   skus=[e["sku"] for e in eligible], amount=amount, reason=a.reason[:500],
                   status="awaiting_approval" if needs_approval else "done", idempotency_key=key)
    ctx.db.add(act)
    try:
        ctx.db.flush()
    except IntegrityError:  # concurrent duplicate hit the unique constraint
        ctx.db.rollback()
        return _duplicate(_existing_action(ctx, key))

    denied_txt = (" Not included: " + "; ".join(f"{d['name']} ({d['why']})" for d in denied) + ".") if denied else ""
    if needs_approval:
        why = "defective sale items are reviewed" if sale_defect else f"refunds over S${APPROVAL_THRESHOLD_SGD:.0f} need approval"
        esc = _escalate(ctx, "approval_needed", f"Refund S${amount:.2f} on {o.id}: {why}", o.id, act.id)
        return {"ok": True, "action_id": act.id, "status": act.status, "amount_sgd": amount, "escalation_id": esc.id,
                "message": f"I've submitted a refund request of S${amount:.2f} for order {o.id} (request #{act.id}). "
                           f"Because {why}, a team member will review it, usually within one working day. "
                           "If approved, it goes back to your original payment method."
                           + note + denied_txt}
    return {"ok": True, "action_id": act.id, "status": "done", "amount_sgd": amount,
            "message": f"Your refund of S${amount:.2f} for order {o.id} is approved (request #{act.id}). "
                       "It will reach your original payment method in 5-7 working days." + note + denied_txt}


def request_exchange(ctx: ToolContext, a: ExchangeArgs) -> dict:
    o = _load_order(ctx, a.order_id)
    item = next((i for i in o.items if i.sku == a.item_sku.strip().upper()), None)
    if not item:
        return {"ok": False, "error_type": "invalid_item", "message": f"{a.item_sku} is not in order {o.id}."}
    key = f"exchange:{o.id}:{item.sku}"
    if (act := _existing_action(ctx, key)):
        return _duplicate(act)
    opts, why = _item_eligibility(o, item, defective=False)
    if "exchange" not in opts:
        return {"ok": False, "error_type": "policy_denied", "message": f"I can't exchange {item.name}: {why}."}
    act = m.Action(conversation_id=ctx.conversation_id, order_id=o.id, kind="exchange", skus=[item.sku],
                   amount=0.0, reason=a.details[:500], status="done", idempotency_key=key)
    ctx.db.add(act)
    ctx.db.flush()
    return {"ok": True, "action_id": act.id, "status": "done",
            "message": f"Exchange #{act.id} created for {item.name} ({a.details}). "
                       "A prepaid return label has been emailed to you."}


def cancel_order(ctx: ToolContext, a: CancelArgs) -> dict:
    o = _load_order(ctx, a.order_id)
    key = f"cancel:{o.id}"
    if (act := _existing_action(ctx, key)):
        return _duplicate(act)
    if o.status != "processing":
        return {"ok": False, "error_type": "policy_denied",
                "message": f"Order {o.id} is already {o.status}, so it can't be cancelled."
                           + (" Once it's delivered you can return it within 30 days." if o.status == "shipped" else "")}
    o.status = "cancelled"
    act = m.Action(conversation_id=ctx.conversation_id, order_id=o.id, kind="cancel", skus=[i.sku for i in o.items],
                   amount=o.total, reason=a.reason[:500], status="done", idempotency_key=key)
    ctx.db.add(act)
    ctx.db.flush()
    return {"ok": True, "action_id": act.id, "status": "done",
            "message": f"Order {o.id} is cancelled and S${o.total:.2f} will be refunded in 5-7 working days."}


def get_policy(ctx: ToolContext, a: PolicyArgs) -> dict:
    p = ctx.db.get(m.Policy, a.topic)
    return {"ok": True, "topic": a.topic, "message": f"{p.title}: {p.text}"}


def escalate_to_human(ctx: ToolContext, a: EscalateArgs) -> dict:
    esc = _escalate(ctx, a.category, a.reason[:500], a.order_id)
    return {"ok": True, "escalation_id": esc.id,
            "message": f"I've passed this to our support team (ticket ESC-{esc.id}). "
                       "Someone will get back to you by email within one working day."}


def get_support_update(ctx: ToolContext, a: SupportUpdateArgs) -> dict:
    esc = ctx.db.get(m.Escalation, a.escalation_id)
    conv = ctx.db.get(m.Conversation, esc.conversation_id) if esc and esc.conversation_id else None
    owns = conv is not None and (
        (ctx.customer_id and conv.customer_id == ctx.customer_id)
        or (not ctx.customer_id and ctx.guest_email and (conv.guest_email or "").lower() == ctx.guest_email.lower()))
    if not owns:
        return {"ok": False, "error_type": "not_found_or_not_authorised",
                "message": f"I couldn't find ticket ESC-{a.escalation_id} for you."}
    if esc.status != "resolved":
        return {"ok": True, "status": "open",
                "message": f"Ticket ESC-{esc.id} is still with our support team. You'll hear back within one working day."}
    act = ctx.db.get(m.Action, esc.action_id) if esc.action_id else None
    outcome = ""
    if act and act.status == "approved":
        outcome = (f" Your {act.kind} of S${act.amount:.2f} for order {act.order_id} has been approved and will reach "
                   "your original payment method in 5-7 working days.")
    elif act and act.status == "rejected":
        outcome = f" Your {act.kind} request for order {act.order_id} was not approved."
    note = (esc.resolution_note or "").strip()
    return {"ok": True, "status": "resolved", "resolved_by": esc.resolved_by, "note": note,
            "action_status": act.status if act else None,
            "message": f"I got a review from our support team on ticket ESC-{esc.id}"
                       + (f': "{note}"' if note else ".") + outcome}


@dataclass
class Tool:
    fn: Callable[[ToolContext, Any], dict]
    args: type[BaseModel]
    description: str

    @property
    def spec(self) -> ToolSpec:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.fn.__name__, description=self.description, input_schema=schema)


TOOLS: dict[str, Tool] = {t.fn.__name__: t for t in [
    Tool(list_my_orders, NoArgs, "List the signed-in customer's orders (newest first). Not available to guests."),
    Tool(get_order_status, OrderIdArgs, "Get status, items and tracking for one order the customer owns."),
    Tool(check_return_eligibility, OrderIdArgs, "Check per item whether refund and/or exchange is allowed."),
    Tool(request_refund, RefundArgs, "Request a refund. Policy, approval and duplicate checks are enforced by the system."),
    Tool(request_exchange, ExchangeArgs, "Request an exchange for one item (e.g. different size)."),
    Tool(cancel_order, CancelArgs, "Cancel an order that has not shipped yet."),
    Tool(get_policy, PolicyArgs, "Fetch the official policy text for a topic."),
    Tool(escalate_to_human, EscalateArgs, "Hand the case to a human agent with a clear reason."),
    Tool(get_support_update, SupportUpdateArgs, "Get the support team's review of one of this customer's tickets (ESC-n)."),
]}

TOOL_SPECS: list[ToolSpec] = [t.spec for t in TOOLS.values()]
