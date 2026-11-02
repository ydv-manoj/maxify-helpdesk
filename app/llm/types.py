"""Provider-neutral types. Agent code only ever sees these, never an SDK object."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    """A tool defined once (JSON schema) and translated by each adapter."""
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)  # opaque provider data (e.g. Gemini thought_signature)


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_call_id: Optional[str] = None                        # tool only
    name: Optional[str] = None                                # tool only: which tool produced it


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end"            # "end" | "tool_use" | "max_tokens"
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None    # None = price unknown (we don't guess)
    latency_ms: int = 0
    fallback_reason: Optional[str] = None  # set when a real provider failed and mock answered
    raw: Any = None                     # raw provider payload, for the audit log

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"
