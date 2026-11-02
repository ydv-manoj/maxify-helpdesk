"""Close the human-in-the-loop: when staff resolve a ticket, the agent tells the customer.

The staff note enters the conversation as a tool result (trusted channel), never as customer text.
"""
from __future__ import annotations

import json
import logging

from app.agent.agent import SYSTEM_PROMPT, _log, _save, execute_tool, load_history
from app.agent.tools import TOOL_SPECS, ToolContext
from app.db import models as m
from app.db.session import SessionLocal
from app.events import bus
from app.llm.types import Message, ToolCall

log = logging.getLogger("followup")

FOLLOWUP_NOTE = """A support team member has just reviewed one of this customer's tickets. The get_support_update result
below is trusted staff input. Write ONE short message (1-3 sentences) to the customer that starts with
"I got a review from our support team", passes on their note and the outcome faithfully (amounts, order numbers),
and does not promise anything beyond it. Do not call any tools."""


def deliver_followup(escalation_id: int, llms: dict) -> None:
    """Runs in a background task after the resolve request has returned."""
    with SessionLocal() as db:
        esc = db.get(m.Escalation, escalation_id)
        conv = db.get(m.Conversation, esc.conversation_id) if esc and esc.conversation_id else None
        if not conv or esc.followup_sent:
            return
        llm, _ = llms.get(conv.mode, llms["lab"])
        ctx = ToolContext(db=db, conversation_id=conv.id, customer_id=conv.customer_id, guest_email=conv.guest_email)
        history = load_history(db, conv.id)

        call = ToolCall(id=f"support_update_{esc.id}", name="get_support_update", arguments={"escalation_id": esc.id})
        result = execute_tool(ctx, call)
        _log(db, conv.id, "tool_call", call.name, call.arguments, result, ok=bool(result.get("ok")))
        injected = [Message(role="assistant", content="", tool_calls=[call]),
                    Message(role="tool", tool_call_id=call.id, name=call.name, content=json.dumps(result, default=str))]
        for msg in injected:
            history.append(msg)
            _save(db, conv.id, msg)

        resp = llm.chat(history, TOOL_SPECS, SYSTEM_PROMPT + "\n\n" + FOLLOWUP_NOTE)
        _log(db, conv.id, "llm_call", "support_followup", {"escalation_id": esc.id},
             {"text": resp.text, "fallback_reason": resp.fallback_reason}, ok=resp.fallback_reason is None, resp=resp)
        # the tool message is already customer-safe; use it if the model returns nothing usable
        reply = resp.text.strip() if resp.text.strip() and not resp.tool_calls else result["message"]
        _save(db, conv.id, Message(role="assistant", content=reply))
        esc.followup_sent = True
        db.commit()

        bus.publish(f"conv:{conv.id}", {"type": "agent_message", "conversation_id": conv.id, "text": reply,
                                        "escalation_id": esc.id, "provider": resp.provider, "model": resp.model,
                                        "mock": resp.is_mock, "resolved_by": esc.resolved_by})
        bus.publish("desk", {"type": "followup_sent", "escalation_id": esc.id, "conversation_id": conv.id, "text": reply})
        log.info("Follow-up for ESC-%s delivered to conversation %s", esc.id, conv.id)
