"""The one interface every provider adapter implements."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

from app.llm.types import LLMResponse, Message, ToolSpec


class ProviderUnavailable(Exception):
    """Raised at construction when a provider can't be used (no key, SDK missing)."""


class LLMAdapter(ABC):
    provider: str = "base"
    default_model: str = ""

    def __init__(self, model: Optional[str] = None, max_tokens: int = 1024):
        self.model = model or self.default_model
        self.max_tokens = max_tokens

    @abstractmethod
    def chat(self, messages: list[Message], tools: list[ToolSpec], system: str) -> LLMResponse:
        ...

    @staticmethod
    def estimate_cost(input_tokens: int, output_tokens: int) -> Optional[float]:
        """Cost only if prices are configured; we never invent a price."""
        p_in = os.getenv("LLM_PRICE_INPUT_PER_MTOK")
        p_out = os.getenv("LLM_PRICE_OUTPUT_PER_MTOK")
        if not p_in or not p_out:
            return None
        return round(input_tokens / 1e6 * float(p_in) + output_tokens / 1e6 * float(p_out), 6)
