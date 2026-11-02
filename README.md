# Threadline support agent (Maxify task)

A customer-support agent for a fictional Singapore apparel store. It answers order questions and handles
returns, refunds, exchanges and cancellations. It clarifies when information is missing and escalates to a
human when an action is unsafe. Policy is enforced in code (the tools), so it holds whatever the model is told.

- **Agent:** an LLM with tool use, behind a provider-agnostic adapter (Anthropic / OpenAI / Gemini / mock).
- **Decisions:** TypeSafe Jev classifies every message (intent, defect claim, manipulation) with calibrated confidence.
- **Human in the loop:** a separate support desk receives escalations live (Server-Sent Events). Staff approve
  or reject refunds and resolve tickets, and the agent relays the review to the customer.
- **Accountability:** every model call, tool call, decision and human action is written to an audit log.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # all keys are optional, see below
```

**No API keys are needed to run it.** Without keys, the agent uses a deterministic, clearly labelled mock
model and keyword rules instead of Jev, so the app, tests and evals all run offline.

Optional keys in `.env` (never commit this file):

| Variable | Used for |
|---|---|
| `GEMINI_API_KEY` | Live chat agent and the Gemini eval runs (`LIVE_LLM_MODEL`, default `gemini-3.5-flash-lite`) |
| `JEV_API_KEY` | Jev decision model (live triage) and the eval judge |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Alternative agents: set `LIVE_LLM_PROVIDER=anthropic` or `openai` |

## Run the app

```bash
python -m app.db.seed                       # create support.db with seed data
uvicorn app.main:app --port 8000
```

- http://localhost:8000: customer console.
  - **Scenario lab** uses the deterministic mock with the seeded customers.
  - **Live chat** uses the real model plus Jev, as test user `CUST-008`.
  - The agent trace shows every decision, model call and tool call.
- http://localhost:8000/desk: support desk. Escalations arrive live. Add internal notes, approve or reject,
  and resolve with a note; the agent then replies to the customer in their chat.

## Tests, evals and the test card

```bash
pytest -q                                   # unit + human-in-the-loop tests

python -m evals.build_cases                 # regenerate evals/cases.jsonl (118 cases)
python -m evals.run --judge none            # deterministic checks only, offline, ~3 s
python -m evals.run --judge jev --tag python                                   # + Jev judge
python -m evals.run --agent gemini --decider none --judge jev --tag gemini     # Gemini alone
python -m evals.run --agent gemini --decider jev  --judge jev --tag gemini_jev # Gemini + Jev
python -m evals.run --cases heldout.jsonl --agent mock --judge jev --tag heldout_python   # held-out phrasing
python -m evals.compare python gemini gemini_jev                               # -> evals/comparison.md

python scripts/run_card.py                  # the 10 test-card prompts -> card_results.txt
```

On free-tier keys, set `LLM_RETRIES=6 LLM_RETRY_BASE_DELAY=4` for the Gemini runs. If the provider fails,
the case is marked `provider_error` and excluded from the score. After 5 provider errors in a row the run
stops early. `python -m evals.rejudge <tag>` re-grades a saved run without calling the agent again.

## Layout

```
app/
  llm/        adapters (anthropic, openai, gemini, mock), factory with retries + labelled fallback,
              decider.py (Jev / rules / none)
  agent/      tools.py (policy, auth, idempotency), agent.py (loop + audit), triage.py (Jev decisions),
              followup.py (relays human review to the customer)
  db/         SQLAlchemy models, seed data
  api/        FastAPI routes: chat, support desk, SSE stream
  events.py   in-process pub/sub for SSE
  static/     index.html (customer console), desk.html (support desk)
evals/        build_cases.py, build_heldout.py, run.py, judge.py, compare.py, rejudge.py,
              cases.jsonl, heldout.jsonl, results_*.md/json, comparison.md, judge cache + manual verdicts
scripts/      run_card.py (test card), smoke_llm.py, try_agent.py
tests/        factory, tools, human-in-the-loop
ASSUMPTIONS.md   30 decisions the brief left open, and where each is enforced
card_results.txt test-card replies (agent, decision scores, tools called, resulting state)
```

## Results at the end of the session

| | Main set (118) | Held-out phrasing (24) |
|---|---|---|
| Rule-based mock agent | 100% (cases developed alongside it) | 42% |
| Gemini 3.5 Flash-Lite agent | 91% | not run (quota) |
| Gemini + Jev | not run (quota) | not run (quota) |

Safety (no unauthorised refunds, no cross-customer leaks, no crashes) held in 100% of cases in every
configuration run. Details are in `evals/comparison.md` and `evals/results_*.md`.

**Known gaps:**
- The Gemini + Jev runs were blocked by the free-tier quota.
- In `card_results.txt`, cards 1–6 were answered by Gemini and cards 7–10 by the labelled mock fallback,
  after the quota ran out.
- In the Gemini-alone run, Jev judged all cases. An earlier manual review agreed on 92% of them.
