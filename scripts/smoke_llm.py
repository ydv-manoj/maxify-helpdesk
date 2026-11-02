"""Send one tool-calling prompt through whichever provider LLM_PROVIDER selects."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.llm.factory import get_llm
from app.llm.types import Message, ToolSpec

TOOL = ToolSpec(
    name="get_order_status",
    description="Look up the shipping status of an order by its ID.",
    input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
)

llm = get_llm()
msgs = [Message(role="user", content="Hi, where is my order ORD-10021?")]
r = llm.chat(msgs, [TOOL], system="You are a support agent. Use tools to look up orders.")
print(f"provider={r.provider} model={r.model} mock={r.is_mock} fallback={r.fallback_reason}")
print(f"tool_calls={[c.model_dump() for c in r.tool_calls]}  tokens={r.input_tokens}/{r.output_tokens} cost={r.cost_usd}")

# Round-trip: feed a fake tool result back, get the final answer
msgs += [Message(role="assistant", content=r.text, tool_calls=r.tool_calls)]
for c in r.tool_calls:
    msgs.append(Message(role="tool", tool_call_id=c.id, name=c.name,
                        content='{"order_id": "ORD-10021", "status": "shipped", "carrier": "Ninja Van"}'))
r2 = llm.chat(msgs, [TOOL], system="You are a support agent. Use tools to look up orders.")
print(f"final: {r2.text}")
