"""relay CLI.

  python3 -m relay run "fix the failing test"     one task, then exit
  python3 -m relay                                 interactive session
  python3 -m relay models                          list models each provider serves
  python3 -m relay doctor                          check providers and the ladder
  python3 -m relay stats                           usage, switches and savings so far
  python3 -m relay supervise --instance ID "task"  unstick an agent hosted on Agent37
"""
from __future__ import annotations

import argparse
import os
import sys

from . import ui
from .agent import Agent
from .config import build_router, load
from .telemetry import Telemetry, summarize
from .tools import Toolbox


def _confirm_factory(auto_yes: bool):
    if auto_yes or not sys.stdin.isatty():
        return None

    def confirm(cmd: str) -> bool:
        ans = input(ui.c("1;33", f"  run `{cmd}`? [Y/n/a] ")).strip().lower()
        if ans == "a":
            nonlocal_state["yes"] = True
        return nonlocal_state["yes"] or ans in ("", "y", "yes", "a")

    nonlocal_state = {"yes": False}
    return confirm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="relay", description="Coding agent that switches models on limits and blockers.")
    ap.add_argument("-c", "--config", help="path to relay.json (default: ./relay.json, ~/.relay.json, built-in ladder)")
    ap.add_argument("-C", "--cwd", default=".", help="workspace directory for the agent")
    ap.add_argument("-y", "--yes", action="store_true", help="run shell commands without asking")
    ap.add_argument("--tier", type=int, help="tier to start on (overrides config)")
    ap.add_argument("--max-steps", type=int)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run one task")
    r.add_argument("task", nargs="+")
    sub.add_parser("models")
    sub.add_parser("doctor")
    sub.add_parser("stats")
    sv = sub.add_parser("supervise", help="watch a hosted Agent37 agent and switch its model when it is trapped")
    sv.add_argument("task", nargs="+")
    sv.add_argument("--instance", required=True, help="Agent37 instance id")
    sv.add_argument("--models", help="comma-separated ladder of the instance's model ids (default: from GET /v1/models)")
    sv.add_argument("--agent", help="harness on the instance: hermes, openclaw, opencode, claude-code, codex, ...")
    sv.add_argument("--hang", type=float, default=180, help="seconds without any agent event before the turn counts as hung")
    args = ap.parse_args(argv)

    if args.cmd == "supervise":
        from .supervise import supervise
        ladder = [m.strip() for m in args.models.split(",")] if args.models else None
        supervise(args.instance, " ".join(args.task), ladder, args.agent, hang_s=args.hang)
        return 0

    cfg = load(args.config)

    if args.cmd == "stats":
        print(summarize(os.path.join(args.cwd, ".relay/events.jsonl")))
        return 0

    router = build_router(cfg, args.tier)

    if args.cmd in ("models", "doctor"):
        for name, p in router.providers.items():
            ok, why = p.available()
            print(ui.c("1", f"{name}") + f"  {p.resolved_base_url() or '(mock)'}  " + (ui.c("32", "ok") if ok else ui.c("31", why)))
            if ok:
                try:
                    ids = p.list_models()
                    if args.cmd == "models":
                        for i in ids:
                            print("   ", i)
                    else:
                        print(f"   {len(ids)} models served")
                        for m in router.models:
                            if m.provider == name:
                                mark = ui.c("32", "✓") if m.model in ids or p.kind == "mock" or m.model == "default" else ui.c("31", "✗ not listed")
                                print(f"   t{m.tier} {m.id:<16} {m.model:<36} {mark}")
                except Exception as e:
                    print("   ", ui.c("31", f"error: {e}"))
        return 0

    ui.banner()
    tel = Telemetry(os.path.join(args.cwd, ".relay"))
    tools = Toolbox(args.cwd, confirm=_confirm_factory(args.yes))
    agent = Agent(router, tools, tel, max_steps=args.max_steps or cfg.get("max_steps", 40))
    print(ui.c("2", router.status_line()))

    if args.cmd == "run":
        agent.run(" ".join(args.task))
        return 0

    # interactive: one conversation, the ladder state carries across turns
    while True:
        try:
            task = input(ui.c("1;36", "\nrelay> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if task in ("exit", "quit", ":q"):
            return 0
        if task == "/status":
            print(router.status_line())
            continue
        if task.startswith("/tier "):
            router.tier = int(task.split()[1])
            print(f"tier set to {router.tier}")
            continue
        if task:
            agent.run(task)


if __name__ == "__main__":
    sys.exit(main())
