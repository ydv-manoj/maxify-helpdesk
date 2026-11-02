"""Decision models: answer named questions about some text with calibrated confidence.

Separate from LLMAdapter on purpose: a decider classifies (choice / yes-no) and returns probabilities;
it never writes replies or calls tools. Jev (TypeSafe System One) is the real one; RulesDecider is the
deterministic fallback so the app and evals never depend on the network.
"""
from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("decider")


class Question(BaseModel):
    type: Literal["choice", "noul"]
    instructions: str
    criteria: Optional[dict[str, Optional[str]]] = None   # choice: option -> description


class Answer(BaseModel):
    type: Literal["choice", "noul"]
    choice: Optional[str] = None
    confidence: Optional[float] = None        # choice: 0-1; noul: probability of "yes"
    probabilities: dict[str, float] = Field(default_factory=dict)


class DecisionResult(BaseModel):
    answers: dict[str, Answer]
    provider: str
    model: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_reason: Optional[str] = None


class Decider(ABC):
    provider = "base"
    model = ""

    @abstractmethod
    def decide(self, state: str, questions: dict[str, Question]) -> DecisionResult:
        ...


class DeciderUnavailable(Exception):
    pass


class JevDecider(Decider):
    """TypeSafe Jev via POST /v1/systemone. All questions are evaluated in parallel in one call."""
    provider = "jev"

    def __init__(self, model: Optional[str] = None, timeout: float = 4.0):
        import httpx
        key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY")
        if not key:
            raise DeciderUnavailable("JEV_API_KEY not set")
        self.model = model or os.getenv("JEV_MODEL", "jev-latest")
        self.client = httpx.Client(base_url="https://api.typesafe.ai", timeout=timeout,
                                   headers={"Authorization": f"Bearer {key}"})

    def decide(self, state, questions) -> DecisionResult:
        payload = {"model": self.model, "state": state,
                   "questions": {k: q.model_dump(exclude_none=True) for k, q in questions.items()}}
        t0 = time.time()
        for attempt in range(2):
            r = self.client.post("/v1/systemone", json=payload)
            if r.status_code in (429, 529) or r.status_code >= 500:
                if attempt == 0:
                    time.sleep(min(float(r.headers.get("retry-after", 1)), 5))
                    continue
            r.raise_for_status()
            break
        data = r.json()
        answers = {}
        for name, a in data["answers"].items():
            if a["type"] == "noul":
                answers[name] = Answer(type="noul", confidence=a["noul"],
                                       probabilities={"true": a["noul"], "false": 1 - a["noul"]})
            else:
                answers[name] = Answer(type="choice", choice=a["choice"], confidence=a.get("confidence"),
                                       probabilities=a.get("probabilities", {}))
        usage = data.get("usage", {})
        return DecisionResult(answers=answers, provider=self.provider, model=data.get("model", self.model),
                              latency_ms=int((time.time() - t0) * 1000),
                              input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0))


# Keyword rules mirroring the mock chat model. Confidence is 1.0/0.0: rules don't know how sure they are.
_RULES: dict[str, re.Pattern] = {
    "human_request": re.compile(r"\b(human|real person|someone real)\b|\b(speak|talk|get me|put me through)\b.{0,20}"
                                r"\b(agent|manager|supervisor|person)\b", re.I),
    "cancel": re.compile(r"\bcancel", re.I),
    "exchange": re.compile(r"\b(exchange|swap|different size|another size)\b", re.I),
    "refund_or_return": re.compile(r"\b(refund|return|money back)\b", re.I),
    "order_status": re.compile(r"\b(where|status|track\w*|arriv\w*|shipped|deliver\w*)\b", re.I),
    "policy_question": re.compile(r"\b(policy|how long|how many days|shipping)\b", re.I),
}
_NOUL_RULES: dict[str, re.Pattern] = {
    "defect_claim": re.compile(r"\b(broke|broken|defect\w*|faulty|torn|ripped|damaged|fell apart|stopped working|hole)\b", re.I),
    "manipulation": re.compile(r"ignore (all |your |previous |the )*(instructions|rules)|system prompt|you are now"
                               r"|act as (an? )?(admin|developer)|developer mode|admin mode|override|jailbreak"
                               r"|disregard (the )?(policy|rules)|^\s*\[?(system|assistant|developer)\]?\s*:", re.I | re.M),
    "order_specified": re.compile(r"\b(ORD-?\s?)?\d{5}\b", re.I),
}


class RulesDecider(Decider):
    provider = "rules"
    model = "keyword-rules-v1"

    def decide(self, state, questions) -> DecisionResult:
        # Only judge the newest customer line, like the mock chat model does.
        last = [ln for ln in state.splitlines() if ln.startswith("CUSTOMER:")]
        text = last[-1] if last else state
        answers = {}
        for name, q in questions.items():
            if q.type == "choice":
                opts = list(q.criteria or {})
                pick = next((o for o, rx in _RULES.items() if o in opts and rx.search(text)), "unclear" if "unclear" in opts else opts[-1])
                answers[name] = Answer(type="choice", choice=pick, confidence=1.0,
                                       probabilities={o: float(o == pick) for o in opts})
            else:
                rx = _NOUL_RULES.get(name)
                p = 1.0 if rx and rx.search(text) else 0.0
                answers[name] = Answer(type="noul", confidence=p, probabilities={"true": p, "false": 1 - p})
        return DecisionResult(answers=answers, provider=self.provider, model=self.model)


class NullDecider(Decider):
    """No decision model at all (baseline for 'chat model alone')."""
    provider = "none"
    model = "none"

    def decide(self, state, questions) -> DecisionResult:
        return DecisionResult(answers={k: Answer(type=q.type, choice="unclear" if q.type == "choice" else None,
                                                 confidence=0.0) for k, q in questions.items()},
                              provider=self.provider, model=self.model)


class ResilientDecider(Decider):
    """Jev with rules fallback: decisions never block a conversation."""

    def __init__(self, inner: Decider):
        self.inner, self.provider, self.model = inner, inner.provider, inner.model
        self.fallback = RulesDecider()

    def decide(self, state, questions) -> DecisionResult:
        try:
            return self.inner.decide(state, questions)
        except Exception as e:  # noqa: BLE001
            reason = f"{self.provider} failed: {type(e).__name__}: {str(e)[:160]}"
            log.warning("Decider fallback to rules. %s", reason)
            res = self.fallback.decide(state, questions)
            res.fallback_reason = reason
            return res


def get_decider(provider: Optional[str] = None) -> ResilientDecider:
    name = (provider or os.getenv("DECIDER_PROVIDER") or "rules").lower()
    if name == "none":
        return ResilientDecider(NullDecider())
    if name == "jev":
        try:
            return ResilientDecider(JevDecider())
        except DeciderUnavailable as e:
            log.warning("Jev unavailable (%s), using rules", e)
    return ResilientDecider(RulesDecider())


def jev_answer_summary(res: DecisionResult) -> dict[str, Any]:
    return {k: (f"{a.choice} ({a.confidence:.2f})" if a.type == "choice" else round(a.confidence or 0, 2))
            for k, a in res.answers.items()}
