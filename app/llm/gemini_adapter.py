from __future__ import annotations

import base64
import json
import os
import time
import uuid

from app.llm.base import LLMAdapter, ProviderUnavailable
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec


# For tool calls Gemini didn't issue (system-injected support updates, mock-fallback steps): accepted bypass value.
SKIP_SIGNATURE = b"skip_thought_signature_validator"


class GeminiAdapter(LLMAdapter):
    provider = "gemini"
    default_model = "gemini-3.5-flash-lite"

    def __init__(self, model=None, max_tokens=1024):
        super().__init__(model, max_tokens)
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise ProviderUnavailable("GEMINI_API_KEY not set")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise ProviderUnavailable(f"google-genai SDK not installed: {e}")
        self.types = types
        self.client = genai.Client(api_key=key)

    def _to_sdk(self, messages: list[Message]) -> list:
        T = self.types
        out = []
        for m in messages:
            if m.role == "user":
                out.append(T.Content(role="user", parts=[T.Part(text=m.content)]))
            elif m.role == "assistant":
                parts = [T.Part(text=m.content)] if m.content else []
                # Gemini 3 requires the thought_signature it issued to be echoed back with each function call
                parts += [T.Part(function_call=T.FunctionCall(name=c.name, args=c.arguments),
                                 thought_signature=base64.b64decode(c.meta["thought_signature"])
                                 if c.meta.get("thought_signature") else SKIP_SIGNATURE)
                          for c in m.tool_calls]
                out.append(T.Content(role="model", parts=parts))
            else:
                try:
                    payload = json.loads(m.content)
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                except json.JSONDecodeError:
                    payload = {"result": m.content}
                part = T.Part(function_response=T.FunctionResponse(name=m.name or "tool", response=payload))
                if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[0].function_response:
                    out[-1].parts.append(part)
                else:
                    out.append(T.Content(role="user", parts=[part]))
        return out

    def chat(self, messages, tools, system) -> LLMResponse:
        T = self.types
        t0 = time.time()
        config = T.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            tools=[T.Tool(function_declarations=[
                T.FunctionDeclaration(name=t.name, description=t.description,
                                      parameters_json_schema=t.input_schema)
                for t in tools])] if tools else None,
            automatic_function_calling=T.AutomaticFunctionCallingConfig(disable=True),
            temperature=0,
            # newer Gemini models think by default (we measured ~56s/call); "low" keeps latency sane
            thinking_config=T.ThinkingConfig(thinking_level=os.getenv("GEMINI_THINKING_LEVEL"))
            if os.getenv("GEMINI_THINKING_LEVEL") else None,
        )
        resp = self.client.models.generate_content(
            model=self.model, contents=self._to_sdk(messages), config=config)
        calls, text = [], ""
        for cand in (resp.candidates or [])[:1]:
            for p in (cand.content.parts if cand.content else []) or []:
                if p.function_call:
                    fc = p.function_call
                    meta = {"thought_signature": base64.b64encode(p.thought_signature).decode()} \
                        if p.thought_signature else {}
                    calls.append(ToolCall(id=fc.id or f"call_{uuid.uuid4().hex[:8]}", name=fc.name,
                                          arguments=dict(fc.args or {}), meta=meta))
                elif p.text and not p.thought:
                    text += p.text
        um = resp.usage_metadata
        in_tok = (um.prompt_token_count or 0) if um else 0
        out_tok = (um.candidates_token_count or 0) if um else 0
        return LLMResponse(
            text=text, tool_calls=calls, stop_reason="tool_use" if calls else "end",
            provider=self.provider, model=self.model, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=self.estimate_cost(in_tok, out_tok),
            latency_ms=int((time.time() - t0) * 1000), raw=resp.model_dump(mode="json"),
        )
