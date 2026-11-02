from __future__ import annotations

import os
import time

from app.llm.base import LLMAdapter, ProviderUnavailable
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec


class AnthropicAdapter(LLMAdapter):
    provider = "anthropic"
    default_model = "claude-sonnet-5-5"

    def __init__(self, model=None, max_tokens=1024):
        super().__init__(model, max_tokens)
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY not set")
        try:
            import anthropic
        except ImportError as e:
            raise ProviderUnavailable(f"anthropic SDK not installed: {e}")
        self.client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=0)

    @staticmethod
    def _to_sdk(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                blocks = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                           for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks})
            else:  # tool result -> user turn with tool_result block; merge consecutive results
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def chat(self, messages, tools, system) -> LLMResponse:
        t0 = time.time()
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=self._to_sdk(messages),
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                   for t in tools],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=b.input or {})
                 for b in resp.content if b.type == "tool_use"]
        return LLMResponse(
            text=text, tool_calls=calls,
            stop_reason="tool_use" if calls else ("max_tokens" if resp.stop_reason == "max_tokens" else "end"),
            provider=self.provider, model=self.model,
            input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
            cost_usd=self.estimate_cost(resp.usage.input_tokens, resp.usage.output_tokens),
            latency_ms=int((time.time() - t0) * 1000), raw=resp.model_dump(),
        )
