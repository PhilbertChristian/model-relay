"""Terminal output."""
from __future__ import annotations

import json
import os
import sys

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def banner() -> None:
    print(c("1;36", "relay") + c("2", " · one agent, many models · switches on limits and blockers"))


def thinking(step: int, model: str, tier: int) -> None:
    print(c("2", f"\n[{step}] ") + c("1;34", model) + c("2", f" (tier {tier})"))


def switch(old: str | None, new: str, reason: str) -> None:
    if old is None:
        print(c("1;32", f"▶ starting on {new}") + c("2", f"  ({reason})"))
    else:
        print(c("1;35", f"⇄ switch {old} → {new}") + c("35", f"  {reason}"))


def hint(coach: str, text: str) -> None:
    print(c("1;36", f"💡 second opinion from {coach}:"))
    for line in text.strip().splitlines()[:8]:
        print(c("36", "   " + line))


def review(judge: str, verdict: str, reason: str) -> None:
    color = "1;32" if verdict == "benign" else "1;31"
    print(c("1;36", f"⚖ refusal review by {judge}: ") + c(color, verdict) + c("36", f"  {reason}"))


def say(text: str) -> None:
    print(text.strip())


def tool(name: str, args: str) -> None:
    try:
        a = json.loads(args)
        short = a.get("command") or a.get("path") or a.get("reason") or args
    except Exception:
        short = args
    short = str(short).replace("\n", " ")
    print(c("33", f"  ⚙ {name}") + c("2", f" {short[:120]}"))


def tool_result(out: str, is_err: bool) -> None:
    lines = out.strip().splitlines()
    preview = "\n".join("    " + l for l in lines[:6]) + ("\n    …" if len(lines) > 6 else "")
    print(c("31" if is_err else "2", preview))


def warn(msg: str) -> None:
    print(c("1;33", f"! {msg}"))


def error(msg: str) -> None:
    print(c("1;31", f"✗ {msg}"))


def summary(router, baseline: float) -> None:
    t = router.totals()
    print(c("1", "\n── usage ──"))
    for m in router.models:
        s = router.state[m.id]
        if s.calls:
            print(f"  {m.id:<18} {s.calls:>3} calls  {s.input_tokens:>7} in  {s.output_tokens:>6} out  ${s.usd:.4f}")
    saved = max(0.0, baseline - t["usd"])
    print(f"  total ${t['usd']:.4f}  ·  top-tier-only would be ${baseline:.4f}  ·  " + c("1;32", f"saved ${saved:.4f}"))
