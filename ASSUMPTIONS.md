# Assumptions

Everything the brief didn't specify, what I decided, and where it is enforced. Policy lives in code
(`app/agent/tools.py`), not in the prompt, so it holds whatever the model does.

## Policy

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | When does the 30-day window start? | From `delivered_at`, counted in whole days. Day 30 is still inside the window. | `RETURN_WINDOW_DAYS`, `_item_eligibility` |
| 2 | Orders not yet delivered? | Not returnable. `processing` → can be cancelled. `shipped` → return after delivery. | `_item_eligibility`, `cancel_order` |
| 3 | Is "sale item" decided at purchase or now? | At purchase. Each order line stores `was_on_sale`, so a later price change doesn't change the policy. | `OrderItem.was_on_sale` |
| 4 | Non-defective sale item | Exchange only. A refund request is declined and an exchange is offered. | `request_refund` |
| 5 | Defective sale item | Refundable, but the refund **always goes to human approval**: a defect claim can't be verified in chat. | `sale_defect → awaiting_approval` |
| 6 | Who decides "defective"? | The Jev decision model scores the customer's words (P ≥ 0.5). The chat model's guess is overridden and the override is logged. No photo check. | `DEFECT_TRUE_MIN`, `defective_override` |
| 7 | Defective but outside 30 days | Declined. The window applies to defects too. | `_item_eligibility` |
| 8 | Large refunds | Above **S$200** a refund needs human approval (`awaiting_approval` + escalation). | `APPROVAL_THRESHOLD_SGD` |
| 9 | Customer asks for more than they paid | Capped at the value of the eligible items, and the customer is told the cap. | `request_refund` |
| 10 | Customer asks for less than eligible | That amount is refunded (partial refund). | `request_refund` |
| 11 | Mixed orders (sale + full price) | Eligibility is per item. Eligible items are refunded and the rest are explained. | `request_refund` |
| 12 | Where does refund money go? | Always the original payment method. "Refund to my other card" is ignored. | tool messages |
| 13 | Cancellation | Only while `processing`, for a full refund. | `cancel_order` |
| 14 | Exchanges | Any delivered item within 30 days, including sale items. A return label is "emailed". Stock is not checked. | `request_exchange` |

## Identity and access

| # | Question | Decision |
|---|---|---|
| 15 | Who is the customer? | A signed-in session (`customer_id`), or a guest verified by **order ID + checkout email**. Identity comes from the session and is never taken from model arguments or chat text. |
| 16 | Someone else's order | The same "couldn't find that order for your account/email" reply as for an order that doesn't exist, so nothing leaks. Logged as `order_access_denied`. |
| 17 | "I'm Marcus" / "it's my husband's order" | Ignored for access. Flagged as manipulation when Jev scores ≥ 0.6. |
| 18 | Order ID formats | `10095`, `#10095` and `ord 10095` are normalised to `ORD-10095`. |
| 19 | Support-ticket updates | A customer can only read tickets from their own conversations. |

## Escalation and human review

| # | Question | Decision |
|---|---|---|
| 20 | When to escalate | (a) a tool is down or crashed; (b) the customer explicitly asks for a human (Jev confidence ≥ 0.85 → instant handoff, no LLM call); (c) the customer insists after a denial, or raises something no tool covers. Not escalated: status, policy questions, accepted denials. |
| 21 | Approval tickets | Opened automatically by the refund tool (over S$200, or a defective sale item). The model must not open a second ticket for the same issue. |
| 22 | Duplicate escalations | One open ticket per conversation + category + order. Repeats reuse it. |
| 23 | Resolving | Staff must approve or reject a pending refund before closing the ticket. The resolution note **is shared with the customer**. Internal notes never are. |
| 24 | Telling the customer | On resolve, the agent posts "I got a review from our support team: …" in the customer's chat. The note reaches the model as a tool result (trusted), never as chat text. |

## Duplicates and failures

| # | Question | Decision |
|---|---|---|
| 25 | What counts as a duplicate? | Same order + action + item set (`refund:ORD-x:SKUs`, `cancel:ORD-x`, `exchange:ORD-x:SKU`), across conversations, enforced by a unique DB constraint. |
| 26 | Duplicate after rejection | Still a duplicate: a rejected refund isn't automatically re-submitted. A human reopens it. |
| 27 | Order system down | No retries of business tools. An honest "system isn't responding" reply, escalated as `tool_failure`. Order details are never guessed. |
| 28 | LLM provider fails | Retried with backoff on 429/5xx/timeouts, then a clearly **labelled mock** answers so the chat never dies. |
| 29 | Decision model fails | Falls back to keyword rules (logged as a fallback). |
| 30 | Prompt injection | Detected (Jev ≥ 0.6 or regex) and logged, but not blocked. Safety comes from the tools enforcing policy whatever the model is told. |

## Data and environment

- Currency SGD, Singapore shipping copy, fictional store "Threadline". Seed dates are relative to *now*, so the
  30-day cases stay stable whenever the evals run.
- Refunds marked "done" stand in for a payment-provider call. No real payments, emails, labels or stock checks.
- Out of scope for the prototype: authentication, multi-tenancy, migrations (tables are recreated on reset),
  multiple languages, carrier tracking APIs.
