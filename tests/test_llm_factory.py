from app.llm.factory import ResilientLLM, get_llm
from app.llm.mock_adapter import MockAdapter
from app.llm.openai_adapter import OpenAIAdapter
from app.llm.types import Message, ToolSpec

TOOL = ToolSpec(name="get_order_status", description="x",
                input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}})


def test_mock_selected_by_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert get_llm().provider == "mock"


def test_missing_key_falls_back_to_mock(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = get_llm("openai")
    assert llm.provider == "mock" and "OPENAI_API_KEY" in llm.unavailable_reason


def test_real_adapter_selected_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    assert isinstance(get_llm("openai").inner, OpenAIAdapter)


def test_transient_error_retries_then_mock(monkeypatch):
    class RateLimitError(Exception):
        status_code = 429

    class Flaky(MockAdapter):
        provider = "flaky"
        calls = 0

        def chat(self, *a):
            Flaky.calls += 1
            raise RateLimitError("slow down")

    llm = ResilientLLM(Flaky(), retries=3, base_delay=0)
    r = llm.chat([Message(role="user", content="where is ORD-10021")], [TOOL], "sys")
    assert Flaky.calls == 3
    assert r.is_mock and "RateLimitError" in r.fallback_reason
    assert r.tool_calls[0].arguments == {"order_id": "ORD-10021"}


def test_non_transient_error_no_retry():
    class Broken(MockAdapter):
        calls = 0

        def chat(self, *a):
            Broken.calls += 1
            raise ValueError("bad request")

    r = ResilientLLM(Broken(), retries=3, base_delay=0).chat([Message(role="user", content="hi")], [TOOL], "s")
    assert Broken.calls == 1 and r.fallback_reason


def test_anthropic_translation_merges_tool_results():
    from app.llm.anthropic_adapter import AnthropicAdapter
    from app.llm.types import ToolCall
    msgs = [Message(role="user", content="hi"),
            Message(role="assistant", tool_calls=[ToolCall(id="a", name="t", arguments={}),
                                                  ToolCall(id="b", name="t", arguments={})]),
            Message(role="tool", tool_call_id="a", name="t", content="1"),
            Message(role="tool", tool_call_id="b", name="t", content="2")]
    out = AnthropicAdapter._to_sdk(msgs)
    assert len(out) == 3 and len(out[2]["content"]) == 2
