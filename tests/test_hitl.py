"""Human-in-the-loop: escalate -> staff resolve -> agent follow-up, plus isolation of support updates."""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/hitl.db"
os.environ["LIVE_LLM_PROVIDER"] = "mock"
os.environ["DECIDER_PROVIDER"] = "rules"

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.agent import load_history  # noqa: E402
from app.agent.tools import SupportUpdateArgs, ToolContext, get_support_update  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_resolve_loop_and_isolation():
    client.post("/api/reset")
    r = client.post("/api/chat", json={"mode": "lab", "customer_id": "CUST-002", "message": "Please refund ORD-10022."}).json()
    conv_id = r["conversation_id"]
    tickets = client.get("/api/desk/tickets").json()
    assert len(tickets) == 1 and tickets[0]["category"] == "approval_needed"
    esc_id = tickets[0]["id"]

    # a pending refund can't be closed without a decision
    assert client.post(f"/api/desk/tickets/{esc_id}/resolve", json={"reviewer": "Sam", "note": "ok"}).status_code == 422
    assert client.post(f"/api/desk/tickets/{esc_id}/comments", json={"author": "Sam", "body": "checked"}).status_code == 200
    res = client.post(f"/api/desk/tickets/{esc_id}/resolve",
                      json={"reviewer": "Sam", "note": "Approved, thanks for returning it.", "decision": "approve"}).json()
    assert res["status"] == "resolved" and res["action"]["status"] == "approved"
    assert client.post(f"/api/desk/tickets/{esc_id}/resolve",
                       json={"reviewer": "Sam", "note": "again", "decision": "approve"}).status_code == 409

    # background follow-up ran: the customer's chat now ends with the staff note relayed by the agent
    with SessionLocal() as db:
        last = load_history(db, conv_id)[-1]
        assert last.role == "assistant" and "I got a review from our support team" in last.content
        assert "Approved, thanks for returning it." in last.content
        detail = client.get(f"/api/desk/tickets/{esc_id}").json()
        assert detail["followup_sent"] and len(detail["notes"]) == 2

        # another customer can't read this ticket's resolution
        other = ToolContext(db=db, conversation_id=None, customer_id="CUST-001")
        assert get_support_update(other, SupportUpdateArgs(escalation_id=esc_id))["ok"] is False
