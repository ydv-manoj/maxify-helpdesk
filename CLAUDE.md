# Maxify AI: live prototyping interview playbook

This folder is used during a live interview. An interviewer is watching the screen and is judging
how the problem is structured, how tools are chosen, technical decisions and trade-offs, building,
iterating, debugging, and how the reasoning is explained. Polish and completeness matter less
than a clear, working, well-reasoned slice.

Maxify's product: AI agents and workflows that **own real tasks**, connect to business systems,
and deliver measurable results. Reliable and accountable, not a chat demo. Every design choice
should show that: state that is tracked, actions that are logged, failures that are visible,
and a human able to approve or correct.

## Ground rules for Claude

1. **Start in plan mode.** Do not create or edit files until Manoj approves the plan.
2. **Work in small steps and narrate every step.** Before each step, say in 1 to 3 lines what you
   are about to do and why. After it, say what changed and what's next. Stop at every
   checkpoint marked ⏸ below and wait for Manoj to say go.
3. **Never write the whole app in one go.** One slice at a time: one table, one endpoint,
   one screen. Each slice must run before the next one starts.
4. **Explain trade-offs out loud.** For each real decision give the chosen option, one or two
   alternatives, and why this one fits a prototype today and what would change in production.
   Keep it short: a small table or 3 bullets.
5. **Prefer boring, fast-to-run tech.** No setup that can fail live (no Docker, no cloud deploys)
   unless the task needs it.
6. **Secrets.** Read API keys only from environment variables or `.env`. Never print, echo,
   log or commit a key. Add `.env` to `.gitignore` first.
7. **Always have a fallback.** If the LLM call fails (no key, no network, rate limit), the app
   must still run with a clearly labelled mock response so the demo never dies.

## The process (follow in order)

### Phase 0. Understand the task (plan mode) ⏸
- Restate the scenario in 3 to 5 lines: who the user is, what job the agent owns, what
  "done" looks like, and how success would be measured.
- List assumptions and open questions. Suggest which questions to ask the interviewer.
- Define the **smallest end-to-end slice** that proves the idea, plus 2 or 3 stretch goals.
- Propose a rough time budget (example for ~60 min: 10 plan, 10 schema/API, 20 agent,
  10 UI, 10 demo and polish).

### Phase 1. Research (plan mode) ⏸
- Quickly check anything unfamiliar: the business system involved, its data shape, the right
  API or library. Use web search if needed. Keep it under 5 minutes and cite what you used.
- Summarise findings in a few bullets that affect the design.

### Phase 2. Backend first: schema design ⏸
- Identify the entities (for example: task, run, step, tool call, approval, audit event).
- Draw the schema as a short table or ERD in text: tables, key columns, types, relations,
  status fields and their allowed transitions.
- Always include, because they show "accountable AI":
  - a **run/status** model (queued → running → awaiting_approval → done / failed),
  - an **audit/event log** of every agent action and tool call (input, output, timestamp),
  - fields for **model, tokens and cost** per call,
  - an **idempotency key** or unique constraint where duplicate work is possible.

### Phase 3. Schema trade-offs ⏸
- Normalised tables vs a JSON column for flexible agent output (usually: core fields as
  columns, raw model output as JSON).
- Event log (append-only) vs updating rows in place.
- What we skip for the prototype (multi-tenancy, auth, migrations tooling) and say so out loud.

### Phase 4. Database choice ⏸
- Default: **SQLite** for the prototype (zero setup, one file, easy to inspect live).
- Say clearly: in production it would be **Postgres** (concurrency, JSONB, row locks, managed
  backups), plus Redis or a queue only if the workload needs it.
- Only pick something else if the task demands it, and explain why.

### Phase 5. API and agent design ⏸
- List the endpoints (method, path, request, response) before writing them.
- Design the agent: its goal, its tools (each with a typed input schema), the loop
  (model → tool → result → model), step and token limits, where a human approval pause
  sits, and what gets written to the audit log.
- Validate LLM output against a schema (Pydantic). Bad output goes back to the model as an
  error or is marked for review. Never write unvalidated output into business data.
- Default stack unless the task suggests otherwise: **Python + FastAPI + SQLModel/SQLAlchemy +
  SQLite**, Anthropic API with tool use (model id from env, default `claude-sonnet-5-5`).

### Phase 6. Build in slices (leave plan mode only now)
For each slice: say the plan, write the code, run it, show the result, then ⏸.
1. Project skeleton, `.gitignore`, `.env.example`, `requirements.txt`, health endpoint.
2. Models and tables, plus seed data with realistic content (real-looking names and values,
   no lorem ipsum).
3. Core endpoints, tested with `curl` or a tiny pytest.
4. The agent loop with one tool, then more tools.
5. Human approval step and the audit log endpoint.
6. Error handling: retries with backoff on rate limits, timeouts, mock fallback.

### Phase 7. Frontend (use the `product-design` skill) ⏸
- Load the `product-design` skill before building any UI and follow its process, scaled down
  for a prototype: pick tokens (colour, type, spacing) first, one clear primary action, real
  data on screen, no generated-looking mock-ups.
- Keep it simple to run: a single page (Vite + React + Tailwind, or plain HTML served by
  FastAPI if time is short).
- The screens that matter for Maxify: the task/run list with status, the run detail with the
  step timeline (what the agent did, which tools it called, cost), and the approve/reject action.

### Phase 8. Verify and debug
- Run the full flow end to end. Read server logs and the browser console. Fix errors one at a
  time and explain each root cause in one line.
- Take a screenshot if useful to confirm the UI.

### Phase 9. Wrap-up summary ⏸
Prepare a short spoken summary for Manoj:
- What was built and the demo path (3 to 5 clicks).
- Key decisions and their trade-offs.
- What would come next for production: Postgres, auth and multi-tenancy, a durable workflow
  engine (Temporal or a queue) for long runs, an eval set to measure accuracy before changes,
  monitoring of cost and failures.

## Update format (use every step)

```
STEP: <phase / slice name>
DOING: <what and why, 1 to 3 lines>
DECISION: <choice> vs <alternative>, because <reason>   (only when there is one)
RESULT: <what changed / what ran>
NEXT: <next step>, waiting for go
```
