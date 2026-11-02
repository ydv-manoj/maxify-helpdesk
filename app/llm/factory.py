"""Factory: pick a provider from env, wrap it with retries + mock fallback.

Add a provider = write one adapter file + one line in REGISTRY.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from app.llm.anthropic_adapter import AnthropicAdapter
from app.llm.base import LLMAdapter, ProviderUnavailable
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.mock_adapter import MockAdapter
from app.llm.openai_adapter import OpenAIAdapter
from app.llm.types import LLMResponse, Message, ToolSpec

log = logging.getLogger("llm")

REGISTRY: dict[str, type[LLMAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "gemini": GeminiAdapter,
    "mock": MockAdapter,
}

TRANSIENT_MARKERS = ("RateLimit", "Timeout", "Connection", "Overloaded", "ServiceUnavailable",
                     "InternalServer", "ResourceExhausted", "ServerError")


def _is_transient(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return any(mk in type(e).__name__ for mk in TRANSIENT_MARKERS)


class ResilientLLM(LLMAdapter):
    """Retries transient errors with exponential backoff, then answers with the mock (labelled)."""

    def __init__(self, inner: LLMAdapter, retries: Optional[int] = None, base_delay: Optional[float] = None):
        retries = retries if retries is not None else int(os.getenv("LLM_RETRIES", "3"))
        base_delay = base_delay if base_delay is not None else float(os.getenv("LLM_RETRY_BASE_DELAY", "0.5"))
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.retries = retries
        self.base_delay = base_delay
        self.fallback = MockAdapter()

    def chat(self, messages: list[Message], tools: list[ToolSpec], system: str) -> LLMResponse:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                return self.inner.chat(messages, tools, system)
            except Exception as e:  # noqa: BLE001 - we classify below
                last_err = e
                if not _is_transient(e) or attempt == self.retries - 1:
                    break
                delay = self.base_delay * (2 ** attempt)
                log.warning("LLM %s transient error (%s), retry %d in %.1fs",
                            self.provider, type(e).__name__, attempt + 1, delay)
                time.sleep(delay)
        reason = f"{self.provider} failed: {type(last_err).__name__}: {str(last_err)[:200]}"
        log.error("Falling back to MOCK. %s", reason)
        resp = self.fallback.chat(messages, tools, system)
        resp.fallback_reason = reason
        return resp


def get_llm(provider: Optional[str] = None, model: Optional[str] = None) -> ResilientLLM:
    name = (provider or os.getenv("LLM_PROVIDER") or "mock").strip().lower()
    model = model or os.getenv("LLM_MODEL") or None
    cls = REGISTRY.get(name)
    if cls is None:
        log.warning("Unknown LLM_PROVIDER=%r, using mock. Options: %s", name, list(REGISTRY))
        cls = MockAdapter
    try:
        inner = cls(model=model)
    except ProviderUnavailable as e:
        log.warning("Provider %s unavailable (%s). Using MOCK so the app keeps running.", name, e)
        inner = MockAdapter()
        wrapped = ResilientLLM(inner)
        wrapped.unavailable_reason = f"{name}: {e}"
        return wrapped
    wrapped = ResilientLLM(inner)
    wrapped.unavailable_reason = None
    return wrapped
