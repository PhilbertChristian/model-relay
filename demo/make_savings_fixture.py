"""Generate the EXAMPLE month used in the demo video and landing page. Not real usage.

Persona: a developer on 2x Claude Max 20x + 1x ChatGPT Pro ($600/month of plans) plus $25/month of
Agent37 credit, heavy daytime agentic coding, September 2026.

    daytime Claude Code         $3,767.80 at API list rates (opus-5.5 / fable-5.1 / sonnet-5.5)
  + Relay night shifts          $1,412.60 on plan windows + Agent37 credit that would have expired
  = $5,180.40 used at API rates · $1,412.60 rescued (27.3%) · 8.6x the $600 paid for the plans

Run:  python3 demo/make_savings_fixture.py   ->  examples/savings-demo/
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from relay.value import lookup, CACHE_WRITE_MULT  # noqa: E402

OUT = ROOT / "examples" / "savings-demo"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
DAYS = 30
CLAUDE = [("claude-opus-5-5", 2240.00), ("claude-fable-5-1", 1160.00), ("claude-sonnet-5-5", 367.80)]   # = 3,767.80
NIGHTS = 22
RESCUED = 1412.60


def claude_events():
    rows, n = [], 0
    for model, target in CLAUDE:
        pin, pout, pread = lookup(model)
        per_day = target / DAYS
        for d in range(DAYS):
            # a cache-heavy agentic mix: 10% input, 10% cache writes, 35% cache reads, output absorbs the rest
            inp = int(per_day * 0.10 / pin * 1e6)
            cwrite = int(per_day * 0.10 / (pin * CACHE_WRITE_MULT) * 1e6)
            cread = int(per_day * 0.35 / pread * 1e6)
            rest = per_day - (inp * pin + cwrite * pin * CACHE_WRITE_MULT + cread * pread) / 1e6
            out = round(rest / pout * 1e6)
            n += 1
            ts = START + timedelta(days=d, hours=17, minutes=n % 50)   # daytime, Pacific
            rows.append({"timestamp": ts.isoformat().replace("+00:00", "Z"), "requestId": f"req_demo_{n}",
                         "message": {"id": f"msg_demo_{n}", "model": model, "usage": {
                             "input_tokens": inp, "output_tokens": out,
                             "cache_creation_input_tokens": cwrite, "cache_read_input_tokens": cread}}})
    return rows


def relay_events():
    rows = []
    nightly = [round(RESCUED / NIGHTS + ((i * 7) % 11 - 5) * 1.37, 2) for i in range(NIGHTS)]
    nightly[-1] = round(RESCUED - sum(nightly[:-1]), 2)
    assert abs(sum(nightly) - RESCUED) < 1e-6
    night_days = [d for d in range(DAYS) if d % 4 != 3][:NIGHTS]   # a few nights off
    for k, (d, usd) in enumerate(zip(night_days, nightly)):
        sess = f"night{k + 1}"
        t0 = (START + timedelta(days=d, hours=6)).timestamp()   # 23:00 Pacific
        caps = [{"name": "claude-max-a", "kind": "rolling_window", "unit": "windows", "tonight": 1.0, "providers": ["claude-plan"], "expires": True},
                {"name": "chatgpt-pro", "kind": "rolling_window", "unit": "windows", "tonight": 1.0, "providers": ["codex-plan"], "expires": True},
                {"name": "agent37", "kind": "agent37_budget", "unit": "usd", "tonight": 1.10, "providers": ["agent37"], "expires": True}]
        rows.append({"ts": t0, "session": sess, "host": "demo", "event": "shift_start", "capacity": caps})
        a37 = 1.10
        codex = round(usd * 0.30, 6)
        split = [("agent37", "a37-deepseek", a37), ("codex-plan", "gpt-5.5-codex", codex), ("claude-plan", "claude-opus-5-5", usd - a37 - codex)]
        for i, (prov, model, v) in enumerate(split):
            rows.append({"ts": t0 + 600 * (i + 1), "session": sess, "host": "demo", "event": "call", "model": model,
                         "provider": prov, "input_tokens": int(v * 60_000), "output_tokens": int(v * 9_000), "usd": round(v, 6)})
        done, blocked = 3 + (k % 3 == 0), (1 if k % 5 == 2 else 0)
        for j in range(done):
            rows.append({"ts": t0 + 3600 + j, "session": sess, "host": "demo", "event": "task", "status": "done"})
        for j in range(blocked):
            rows.append({"ts": t0 + 7200 + j, "session": sess, "host": "demo", "event": "task", "status": "blocked"})
        rows.append({"ts": t0 + 8 * 3600, "session": sess, "host": "demo", "event": "shift_end", "usd": usd})
    return rows


if __name__ == "__main__":
    (OUT / "claude" / "demo-project").mkdir(parents=True, exist_ok=True)
    (OUT / "claude" / "demo-project" / "session.jsonl").write_text("\n".join(json.dumps(r) for r in claude_events()) + "\n")
    (OUT / "events.jsonl").write_text("\n".join(json.dumps(r) for r in relay_events()) + "\n")
    (OUT / "README.md").write_text("EXAMPLE month (September 2026) for the demo video and landing page. Not real usage.\n"
                                   "Persona: 2x Claude Max 20x + ChatGPT Pro ($600/mo) + $25 Agent37 credit.\n"
                                   "Regenerate with `python3 demo/make_savings_fixture.py`.\n")
    print("wrote", OUT)
