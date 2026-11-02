from __future__ import annotations

import json
import os
import time

from app.llm.base import LLMAdapter, ProviderUnavailable
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec


class OpenAIAdapter(LLMAdapter):
    provider = "openai"
    default_model = "gpt-4o-mini"

    def __init__(self, model=None, max_tokens=1024):
        super().__init__(model, max_tokens)
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ProviderUnavailable("OPENAI_API_KEY not set")
        try:
            import openai
        except ImportError as e:
            raise ProviderUnavailable(f"openai SDK not installed: {e}")
        self.client = openai.OpenAI(api_key=key, timeout=30.0, max_retries=0)

    @staticmethod
    def _to_sdk(messages: list[Message], system: str) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                msg: dict = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                         for c in m.tool_calls]
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out

    def chat(self, messages, tools, system) -> LLMResponse:
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self._to_sdk(messages, system),
            tools=[{"type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                   for t in tools] or None,
        )
        choice = resp.choices[0]
        calls = []
        for c in choice.message.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": c.function.arguments}  # agent will reject via validation
            calls.append(ToolCall(id=c.id, name=c.function.name, arguments=args))
        usage = resp.usage
        return LLMResponse(
            text=choice.message.content or "", tool_calls=calls,
            stop_reason="tool_use" if calls else ("max_tokens" if choice.finish_reason == "length" else "end"),
            provider=self.provider, model=self.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cost_usd=self.estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else None,
            latency_ms=int((time.time() - t0) * 1000), raw=resp.model_dump(),
        )
