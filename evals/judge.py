"""LLM-as-judge: does the agent's reply convey the same OUTCOME as the reference?

Rate-limit safety: batches of N cases per call, min interval between calls, max calls per run,
honour 429 retryDelay once, circuit breaker on quota exhaustion. Verdicts are cached by content hash,
so reruns are deterministic and free. Anything not judged goes to pending_judgments.jsonl, where a
human (or Claude in this session) can judge it into manual_judgments.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from concurrent.futures import ThreadPoolExecutor

from app.llm.base import ProviderUnavailable
from app.llm.decider import DeciderUnavailable, JevDecider, Question
from app.llm.factory import REGISTRY
from app.llm.types import Message

HERE = Path(__file__).parent
CACHE = HERE / "judge_cache.json"
MANUAL = HERE / "manual_judgments.json"
PENDING = HERE / "pending_judgments.jsonl"

RUBRIC = """You are a strict QA grader for a retail customer-support AI agent.
For each item you get the conversation and a REFERENCE OUTCOME written by the test author.
PASS if the agent's final reply communicates the same outcome as the reference: same decision
(approved / submitted for review / declined / asked a clarifying question / escalated), same order,
same amount where one is stated, and the same next step. Wording, tone and extra politeness don't matter.
FAIL if it contradicts the reference, claims an action or approval the reference doesn't, gives a different
amount or order, reveals another customer's details, or misses the key decision.
Lines starting with [MOCK] come from a deterministic test model: judge their content the same way.
Return ONLY a JSON array: [{"id": "<id>", "verdict": "pass" | "fail", "reason": "<max 20 words>"}] with one
entry per item, in any order."""


class Verdict(BaseModel):
    id: str
    verdict: Literal["pass", "fail"]
    reason: str = ""
    confidence: Optional[float] = None


LOW_CONFIDENCE = 0.70  # Jev verdicts below this are flagged for human review
JEV_QUESTION = Question(
    type="choice",
    instructions="You are grading a retail customer-support agent. Compare the AGENT replies in the conversation with the "
                 "REFERENCE OUTCOME. Same decision (approved / submitted for review / declined / asked a clarifying question / "
                 "escalated), same order, same amount where stated, and same next step = pass. Wording and tone don't matter. "
                 "Lines starting with [MOCK] come from a test model; judge their content the same way.",
    criteria={"pass": "the agent's final reply communicates the same outcome as the reference",
              "fail": "it contradicts the reference, claims an action or approval the reference doesn't, gives a different "
                      "amount or order, leaks another customer's details, or misses the key decision"})


def item_hash(item: dict, model: str) -> str:
    raw = json.dumps([item["id"], item["transcript"], item["reference"], model], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Judge:
    def __init__(self, provider: str, model: str, batch_size: int = 10, min_interval: float = 6.0,
                 max_calls: int = 20):
        self.provider, self.model = provider, model
        self.batch_size, self.min_interval, self.max_calls = batch_size, min_interval, max_calls
        self.calls, self.last_call = 0, 0.0
        self.circuit_reason: Optional[str] = None
        self.log: list[str] = []
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.manual = json.loads(MANUAL.read_text()) if MANUAL.exists() else {}
        self.jev = None
        if provider == "jev":
            try:
                self.jev, self.adapter = JevDecider(model=model if model.startswith("jev") else None, timeout=15), None
                self.model = self.jev.model
            except DeciderUnavailable as e:
                self.circuit_reason = f"judge unavailable: {e}"
            self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
            self.manual = json.loads(MANUAL.read_text()) if MANUAL.exists() else {}
            return
        try:
            self.adapter = REGISTRY[provider](model=model, max_tokens=8192) if provider != "none" else None
            if self.adapter is None or self.adapter.provider == "mock":
                self.circuit_reason = "judge disabled"
                self.adapter = None
        except (ProviderUnavailable, KeyError) as e:
            self.adapter, self.circuit_reason = None, f"judge unavailable: {e}"

    def judge_all(self, items: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        todo = []
        for it in items:
            h = item_hash(it, self.model)
            it["hash"] = h
            if (mv := self.manual.get(it["id"])) and mv.get("hash") == h:
                out[it["id"]] = {**mv, "judge": "manual (claude)"}
            elif h in self.cache:
                out[it["id"]] = {**self.cache[h], "judge": f"{self.provider}/{self.model} (cached)"}
            else:
                todo.append(it)
        if self.jev is not None and todo and not self.circuit_reason:
            with ThreadPoolExecutor(max_workers=6) as pool:
                verdicts = list(pool.map(self._call_jev, todo))
            for it, v in zip(todo, verdicts):
                if v is None:
                    continue
                self.cache[it["hash"]] = {"verdict": v.verdict, "reason": v.reason, "confidence": v.confidence}
                out[it["id"]] = {"verdict": v.verdict, "reason": v.reason, "confidence": v.confidence,
                                 "judge": f"jev/{self.model}"}
            CACHE.write_text(json.dumps(self.cache, indent=1))
            todo = []
        for i in range(0, len(todo), self.batch_size):
            if self.circuit_reason:
                break
            batch = todo[i:i + self.batch_size]
            for v in self._call(batch):
                h = next(b["hash"] for b in batch if b["id"] == v.id)
                self.cache[h] = {"verdict": v.verdict, "reason": v.reason}
                out[v.id] = {"verdict": v.verdict, "reason": v.reason, "judge": f"{self.provider}/{self.model}"}
            CACHE.write_text(json.dumps(self.cache, indent=1))
        pending = [it for it in items if it["id"] not in out]
        PENDING.write_text("".join(json.dumps({k: it[k] for k in ("id", "hash", "category", "transcript", "reference")}) + "\n"
                                   for it in pending))
        for it in pending:
            out[it["id"]] = {"verdict": "pending", "reason": self.circuit_reason or "not judged", "judge": None}
        return out

    def _call_jev(self, it: dict) -> Optional[Verdict]:
        state = f"CONVERSATION:\n{it['transcript']}\n\nREFERENCE OUTCOME:\n{it['reference']}"
        self.calls += 1
        try:
            res = self.jev.decide(state, {"verdict": JEV_QUESTION})
        except Exception as e:  # noqa: BLE001
            self.log.append(f"jev {it['id']}: {type(e).__name__}: {str(e)[:120]}")
            return None
        a = res.answers["verdict"]
        p = a.probabilities
        return Verdict(id=it["id"], verdict=a.choice, confidence=a.confidence,
                       reason=f"P(pass)={p.get('pass', 0):.2f} · {res.latency_ms} ms")

    def _call(self, batch: list[dict], attempt: int = 0) -> list[Verdict]:
        if self.calls >= self.max_calls:
            self.circuit_reason = f"max judge calls ({self.max_calls}) reached"
            return []
        wait = self.min_interval - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        payload = [{"id": b["id"], "conversation": b["transcript"], "reference_outcome": b["reference"]} for b in batch]
        self.calls += 1
        self.last_call = time.time()
        try:
            resp = self.adapter.chat([Message(role="user", content=json.dumps(payload, indent=1))], [], RUBRIC)
        except Exception as e:  # noqa: BLE001
            s = str(e)
            self.log.append(f"call {self.calls}: {type(e).__name__}: {s[:160]}")
            if "429" in s or "RESOURCE_EXHAUSTED" in s:
                if "PerDay" in s or "limit: 0" in s or attempt > 0:
                    self.circuit_reason = "Gemini quota/rate limit hit, circuit open"
                    return []
                m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)", s)
                time.sleep(min(int(m.group(1)) + 1 if m else 20, 60))
                return self._call(batch, attempt + 1)
            if attempt == 0:
                time.sleep(3)
                return self._call(batch, 1)
            self.circuit_reason = f"judge error: {type(e).__name__}"
            return []
        self.log.append(f"call {self.calls}: ok {resp.latency_ms}ms tokens {resp.input_tokens}/{resp.output_tokens}")
        text = resp.text
        try:
            arr = json.loads(text[text.index("["): text.rindex("]") + 1])
            ids = {b["id"] for b in batch}
            return [v for v in (Verdict.model_validate(x) for x in arr) if v.id in ids]
        except (ValueError, ValidationError) as e:
            self.log.append(f"call {self.calls}: unparseable judge output ({type(e).__name__})")
            return []
