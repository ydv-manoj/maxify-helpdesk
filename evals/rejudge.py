"""Re-judge a saved run without re-running the agent: python -m evals.rejudge <tag>"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
from evals.judge import Judge  # noqa: E402

HERE = Path(__file__).parent
tag = sys.argv[1]
rows = json.loads((HERE / f"results_{tag}.json").read_text())
cases = {json.loads(l)["id"]: json.loads(l) for f in ("cases.jsonl", "heldout.jsonl") if (HERE / f).exists()
         for l in (HERE / f).read_text().splitlines() if l.strip()}
items = [{"id": r["id"], "category": r["category"], "reference": cases[r["id"]]["reference"],
          "transcript": "\n".join(f"CUSTOMER: {u}\nAGENT: {a}" for u, a in zip(r["turns"], r["replies"]))}
         for r in rows if not r["provider_error"]]
judge = Judge("jev", "jev-latest")
verdicts = judge.judge_all(items)
for r in rows:
    if r["provider_error"]:
        continue
    r["judge"] = verdicts[r["id"]]
    jv = r["judge"]["verdict"]
    r["final"] = "fail" if r["hard_fails"] or jv == "fail" else ("pending" if jv == "pending" else "pass")
(HERE / f"results_{tag}.json").write_text(json.dumps(rows, indent=1))
from collections import Counter  # noqa: E402
print(tag, dict(Counter(r["final"] for r in rows)), "| judge calls:", judge.calls)
for line in judge.log[:5]:
    print("  judge:", line)
