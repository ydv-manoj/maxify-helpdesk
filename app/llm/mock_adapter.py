"""Deterministic, offline adapter. Keeps the demo alive and makes evals repeatable.

It is deliberately simple keyword routing that emits the same tool calls a real model would.
All policy decisions still happen in the tools, so the mock exercises the same safety code paths.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.llm.base import LLMAdapter
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec

ORDER_RE = re.compile(r"\bORD-\d{4,}\b", re.I)
AMOUNT_RE = re.compile(r"s?\$\s?(\d+(?:\.\d{1,2})?)", re.I)
DEFECT_RE = re.compile(r"\b(broke|broken|defect\w*|faulty|torn|ripped|damaged|fell apart|stopped working|hole)\b", re.I)

INTENTS = [  # first match wins
    ("human", re.compile(r"\b(human|real person|someone real)\b"
                         r"|\b(speak|talk|get me|put me through)\b.{0,20}\b(agent|manager|supervisor|person)\b", re.I)),
    ("cancel", re.compile(r"\bcancel", re.I)),
    ("exchange", re.compile(r"\b(exchange|swap|different size|another size)\b", re.I)),
    ("refund", re.compile(r"\b(refund|return|money back)\b", re.I)),
    ("status", re.compile(r"\b(where|status|track\w*|arriv\w*|shipped|deliver\w*)\b", re.I)),
    ("policy", re.compile(r"\b(policy|how long|how many days|shipping)\b", re.I)),
]
POLICY_TOPICS = [("sale", "sale_items"), ("defect", "defective"), ("cancel", "cancellation"),
                 ("ship", "shipping"), ("return", "returns"), ("refund", "returns")]


def _intent(text: str) -> Optional[str]:
    return next((name for name, rx in INTENTS if rx.search(text)), None)


class MockAdapter(LLMAdapter):
    provider = "mock"
    default_model = "mock-rules-v1"

    def chat(self, messages: list[Message], tools: list[ToolSpec], system: str) -> LLMResponse:
        user_idxs = [i for i, msg in enumerate(messages) if msg.role == "user"]
        if not user_idxs:
            return self._reply("How can I help you today?")
        idx = user_idxs[-1]
        text = messages[idx].content
        results = [(msg.name, self._load(msg.content)) for msg in messages[idx + 1:] if msg.role == "tool"]
        called = [n for n, _ in results]
        n = len(messages)

        # Resolve intent; a bare "ORD-10031" reply inherits intent from the earlier question.
        intent = _intent(text)
        if intent is None:
            for j in reversed(user_idxs[:-1]):
                if (intent := _intent(messages[j].content)):
                    text = messages[j].content + " " + text
                    break

        if results:
            name, res = results[-1]
            if res.get("error_type") == "tool_unavailable" and "escalate_to_human" not in called:
                return self._call("escalate_to_human", {"reason": f"Tool failure: {res.get('detail', 'unavailable')}",
                                                        "category": "tool_failure"}, n)
            if name == "escalate_to_human":
                prev = results[-2][1]["message"] + " " if len(results) > 1 else ""
                return self._reply(prev + res["message"])
            intermediate = name == "list_my_orders" or (name == "get_order_status" and intent == "exchange")
            if not intermediate or not res.get("ok"):
                return self._reply(res.get("message", "Done."))

        if intent is None:
            return self._reply("I can help with order status, returns, refunds, exchanges and cancellations. "
                               "What would you like to do?")
        if intent == "human":
            return self._call("escalate_to_human", {"reason": f"Customer asked for a human: {text[:200]}",
                                                    "category": "customer_request"}, n)
        if intent == "policy":
            topic = next((t for kw, t in POLICY_TOPICS if kw in text.lower()), "returns")
            return self._call("get_policy", {"topic": topic}, n)

        m = ORDER_RE.search(text)
        order_id = m.group(0).upper() if m else None
        if order_id is None:
            if "list_my_orders" not in called:
                return self._call("list_my_orders", {}, n)
            orders = dict(results)["list_my_orders"].get("orders", [])
            wanted = {"status": ("processing", "shipped"), "refund": ("delivered",),
                      "exchange": ("delivered",), "cancel": ("processing",)}[intent]
            if re.search(r"\b(last|latest|most recent)\b", text, re.I) and orders:
                cands = orders[:1]
            else:
                cands = [o for o in orders if o["status"] in wanted] or \
                        ([o for o in orders if o["status"] != "cancelled"] if intent == "status" else [])
            if not cands:
                if not orders:
                    return self._reply("I don't see any orders on your account. Which order ID do you mean?")
                verb = {"refund": "returned or refunded", "exchange": "exchanged", "cancel": "cancelled",
                        "status": "tracked"}[intent]
                why = "; ".join(f"{o['order_id']} is {o['status']}" for o in orders)
                return self._reply(f"None of your orders can be {verb} right now ({why}). "
                                   "Which order do you mean, or can I help with something else?")
            if len(cands) > 1:
                listing = "; ".join(f"{o['order_id']} ({o['status']}, "
                                    f"{', '.join(i['name'] for i in o['items'])})" for o in cands)
                return self._reply(f"You have more than one order: {listing}. Which one do you mean?")
            order_id = cands[0]["order_id"]

        if intent == "status":
            return self._call("get_order_status", {"order_id": order_id}, n)
        if intent == "cancel":
            return self._call("cancel_order", {"order_id": order_id, "reason": text[:200]}, n)
        if intent == "refund":
            args = {"order_id": order_id, "reason": text[:300], "defective": bool(DEFECT_RE.search(text))}
            if (amt := AMOUNT_RE.search(text)):
                args["requested_amount"] = float(amt.group(1))
            return self._call("request_refund", args, n)
        # exchange: need an item -> look the order up first
        if "get_order_status" not in called:
            return self._call("get_order_status", {"order_id": order_id}, n)
        items = dict(results)["get_order_status"].get("items", [])
        return self._call("request_exchange", {"order_id": order_id, "item_sku": items[0]["sku"],
                                               "details": text[:200]}, n)

    @staticmethod
    def _load(content: str) -> dict:
        try:
            v = json.loads(content)
            return v if isinstance(v, dict) else {"message": str(v)}
        except json.JSONDecodeError:
            return {"message": content}

    def _reply(self, text: str) -> LLMResponse:
        return LLMResponse(text=f"[MOCK] {text}", stop_reason="end", provider=self.provider,
                           model=self.model, cost_usd=0.0)

    def _call(self, name: str, args: dict, n: int) -> LLMResponse:
        return LLMResponse(tool_calls=[ToolCall(id=f"mock_{n}", name=name, arguments=args)],
                           stop_reason="tool_use", provider=self.provider, model=self.model, cost_usd=0.0)
