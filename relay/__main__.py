"""relay CLI.

  python3 -m relay run "fix the failing test"     one task, then exit
  python3 -m relay                                 interactive session
  python3 -m relay models                          list models each provider serves
  python3 -m relay doctor                          check providers and the ladder
  python3 -m relay stats                           usage, switches and savings so far
  python3 -m relay supervise --instance ID "task"  unstick an agent hosted on Agent37
  python3 -m relay burn capacity -b burner.json    what subscription capacity expires, and when you're idle
  python3 -m relay burn run PLAN.md -b burner.json night shift: burn it on your plan, open PRs, write MORNING.md
  python3 -m relay burn week -b burner-week.json   use it or lose it: a paced agent swarm spends this week's leftovers
  python3 -m relay burn discover|ideas|usage       your repos + TODOs, local chat ideas (opt-in), what's left this week
  python3 -m relay burn demo                       burn week on a sandbox with mock lanes: no keys, no network
  python3 -m relay dash | web                      live burn-week dashboard in the terminal or the browser
  python3 -m relay savings                         this month: used at API rates vs. rescued from expiring
  python3 -m relay serve -c burner.json            OpenAI-compatible endpoint for any harness (failover + savings)
  python3 -m relay mcp                             MCP server: capacity, savings, plan, burn tools for agents
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
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


# burn week|discover|ideas|usage|demo, dash, web (SWARM_SPEC "CLI"). Their modules load lazily, so the CLI keeps
# working while they are built; a missing one prints "not built yet" instead of a traceback.
WEEK_ACTIONS = ("week", "discover", "ideas", "usage", "demo")
_OLD_FLAGS = dict(plan=None, burner=None, now=False, wait=False, hours=None, max_tasks=None, no_pr=False)
_NEW_FLAGS = dict(roots=None, agents=None, ideas=False, lanes=None, dry_run=False, no_dash=False, web=None, out=None,
                  days=None, speed=None, record=None, check=False, detach=False, install=False, uninstall=False,
                  status=False, via=None)
_TAKES = {"week": "plan burner roots agents hours ideas lanes dry_run no_dash web", "discover": "burner roots out",
          "ideas": "burner days", "usage": "burner", "demo": "agents speed web record no_dash",
          "auto": "burner dry_run check detach install uninstall status via"}


def _burn_misuse(args) -> str | None:
    """Flags this burn action doesn't take. capacity|plan|run take exactly the flags they always took."""
    given = {k for k, v in {**_OLD_FLAGS, **_NEW_FLAGS}.items() if getattr(args, k) != v}
    bad = sorted(given - set(_TAKES[args.action].split() if args.action in _TAKES else _OLD_FLAGS))
    if bad:
        return f"burn {args.action} doesn't take " + ", ".join(
            "a planning doc" if k == "plan" else "--" + k.replace("_", "-") for k in bad)
    if (args.speed is not None and args.speed <= 0) or (args.agents is not None and args.agents < 1):
        return "--speed must be > 0 and --agents at least 1"
    return None


def _week_main(args) -> int:
    """Run a burn-week handler; a sibling module (or function) that isn't there yet is reported, not raised."""
    import re
    try:
        return globals()["_burn_" + args.action if args.cmd == "burn" else "_" + args.cmd](args) or 0
    except KeyboardInterrupt:
        print()
        return 130
    except (ImportError, AttributeError) as e:
        name = getattr(e, "name", None) or ""
        m = (re.search(r"cannot import name '(\w+)' from '(relay[\w.]*)'", str(e)) or
             re.match(r"module '(relay[\w.]*)' has no attribute '(\w+)'", str(e)))
        what = ((f"{m[2]}.{m[1]}" if "cannot import" in m[0] else f"{m[1]}.{m[2]}") if m else
                name if isinstance(e, ModuleNotFoundError) and name.startswith("relay") else None)
        if not what:
            raise
        ui.error(f"{what} is not built yet (burn-week modules are still landing)")
        return 2


def _week_cfg(path: str | None) -> dict:
    """burner-week config: -b PATH, else ./burner-week.json, else the documented example (with a note)."""
    from pathlib import Path
    if not path and not os.path.exists("burner-week.json"):
        path = str(Path(__file__).resolve().parent.parent / "examples" / "burner-week.json")
        print(ui.c("2", f"note: no ./burner-week.json, using the example {path} (copy it, then edit roots and lanes)"))
    try:
        return load(path or "burner-week.json")
    except (OSError, ValueError) as e:
        ui.error(f"can't load burner-week config {path}: {'not found' if isinstance(e, FileNotFoundError) else e}")
        raise SystemExit(2)


def _week_overrides(cfg: dict, args) -> dict:
    """--ideas opts in to local chat-history mining; --lanes keeps (and enables) only the named lanes."""
    cfg = dict(cfg)
    if args.ideas:
        cfg["ideas"] = {**(cfg.get("ideas") or {}), "enabled": True}
    if args.lanes:
        want = {x.strip() for x in args.lanes.split(",") if x.strip()}
        cfg["lanes"] = [{**ln, "enabled": True} for ln in cfg.get("lanes", []) if {ln.get("name"), ln.get("kind")} & want]
        if not cfg["lanes"]:
            ui.error(f"--lanes {args.lanes} matches no lane in the config")
            raise SystemExit(2)
    return cfg


def _block(url: str | None, what: str = "web dashboard still serving") -> int:
    """Keep the web dashboard (a daemon thread) serving until Ctrl-C."""
    if url:
        import time
        print(ui.c("1;36", f"{what}  {url}") + ui.c("2", "  · Ctrl-C to stop"))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print()
    return 0


def _print_week_plan(res: dict) -> None:
    """--dry-run: burn_week's pace per lane (relay.pace.report), its schedule note, and the task queue."""
    from types import SimpleNamespace
    from . import pace
    print(pace.report([SimpleNamespace(**p) if isinstance(p, dict) else p for p in res.get("pace") or []]))
    if res.get("schedule"):
        print(ui.c("2", res["schedule"]))
    for ln in res.get("lanes") or []:
        if not ln.get("available", True):
            print(ui.c("33", f"  lane {ln.get('name')} unavailable: {ln.get('why')}"))
    queue = res.get("queue") or []
    print(ui.c("1", f"\nqueue · {len(queue)} tasks from {len(res.get('projects') or [])} projects")
          + ui.c("2", " (dry run: nothing started)"))
    for t in queue:
        print(f"  {str(t.get('project')):<18} {str(t.get('source', '')):<8} {t.get('text')}")


def _burn_week(args) -> int:
    from . import demo_week, swarm
    from .bus import Bus
    cfg = _week_overrides(_week_cfg(args.burner), args)
    kw = dict(roots=[os.path.expanduser(r) for r in args.roots] if args.roots else None, plan_path=args.plan,
              max_agents=args.agents, hours=args.hours)
    if args.dry_run:
        _print_week_plan(swarm.burn_week(cfg, dry_run=True, **kw) or {})
        return 0
    bus = Bus(log_dir=os.path.join(args.cwd, ".relay"))
    stop = demo_week.watch(bus, not args.no_dash, args.web)
    try:
        res = swarm.burn_week(cfg, bus=bus, **kw)
    except KeyboardInterrupt:
        res = {"reason": "interrupted"}
    finally:
        stop()
    print(demo_week.summary(bus.rows, res))
    return _block(args.web and f"http://127.0.0.1:{args.web}/")


def _burn_discover(args) -> int:
    from pathlib import Path
    from . import discover
    cfg = _week_cfg(args.burner)
    projects = discover.discover_projects([os.path.expanduser(r) for r in args.roots or cfg.get("roots") or []],
                                          max_depth=cfg.get("max_depth", 3), limit=cfg.get("max_projects", 20),
                                          include=cfg.get("include"), exclude=cfg.get("exclude"), skip=cfg.get("skip"))
    print(discover.report(projects))
    if args.out:
        if Path(args.out).exists():
            ui.error(f"{args.out} already exists; not overwriting it")
            return 1
        found = None
        if (cfg.get("ideas") or {}).get("enabled"):
            from . import ideas
            found = ideas.mine(cfg)
        Path(args.out).write_text(discover.to_plan_md(projects, found, per_project=cfg.get("per_project", 3)))
        print(ui.c("32", f"wrote {args.out}") + ui.c("2", f" · edit it, then: relay burn week {args.out}"))
    return 0


def _burn_ideas(args) -> int:
    from . import ideas
    cfg = _week_cfg(args.burner)
    opts = {**(cfg.get("ideas") or {}), "enabled": True}            # running this command is the opt-in
    if args.days:
        opts["since_days"] = args.days
    print(ui.c("2", "reading local Claude Code / Codex history; this command only prints, nothing is sent"))
    print(ideas.report(ideas.mine({**cfg, "ideas": opts})))
    return 0


def _burn_usage(args) -> int:
    from . import usage
    cfg = _week_cfg(args.burner)
    print(usage.report(usage.weekly_usage(cfg, os.path.join(args.cwd, ".relay/events.jsonl"))))
    return 0


def _burn_demo(args) -> int:
    from . import demo_week
    demo_week.run_demo(agents=args.agents or 8, speed=args.speed or 1.0, dash=not args.no_dash, web_port=args.web,
                       record=args.record)
    return _block(args.web and f"http://127.0.0.1:{args.web}/")


def _dash(args) -> int:
    from . import dash
    dash.tail(args.events or os.path.join(args.cwd, ".relay/events.jsonl"))
    return 0


def _web(args) -> int:
    from . import web
    path, feed = args.events or os.path.join(args.cwd, ".relay/events.jsonl"), None
    if args.supabase:
        from . import supa
        if not supa.available():
            ui.error("--supabase needs SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY)")
            return 2
        feed = web.Feed(lambda since: supa.recent_events(since_ts=since))    # polls Supabase, acts as the bus
    try:
        srv = web.serve(port=args.port, events_path=path, bus=feed)
    except OSError as e:
        ui.error(f"can't listen on 127.0.0.1:{args.port}: {e}")
        return 1
    _block(getattr(srv, "url", f"http://127.0.0.1:{args.port}/"), f"burn-week dashboard ({'Supabase' if feed else path})")
    getattr(srv, "shutdown", lambda: None)()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "serve" in argv:   # everything after `serve` belongs to relay.serve's own parser
        i = argv.index("serve")
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("-c", "--config"); pre.add_argument("-C", "--cwd")
        top, _ = pre.parse_known_args(argv[:i])
        from .serve import main as serve_main
        fwd = (["-c", top.config] if top.config else []) + (["-C", top.cwd] if top.cwd else []) + argv[i + 1:]
        return serve_main(fwd) or 0
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
    sub.add_parser("mcp", help="serve Relay's tools over MCP on stdio (Claude Code, Codex, OpenCode, Hermes...)")
    sub.add_parser("serve", help="OpenAI-compatible endpoint: point any harness's base_url here (see relay serve -h)")
    sv2 = sub.add_parser("savings", help="this month's usage at API rates, and how much Relay rescued from expiring")
    sv2.add_argument("--month", help="YYYY-MM (default: this month, UTC)")
    sv2.add_argument("--ledger", action="append", help="relay events.jsonl (repeatable; default: <cwd>/.relay/events.jsonl)")
    sv2.add_argument("--claude-logs", help="Claude Code projects dir(s), comma-separated (default: ~/.claude/projects)")
    sv2.add_argument("--no-claude", action="store_true", help="skip Claude Code logs")
    sv2.add_argument("--json", action="store_true")
    bn = sub.add_parser("burn", help="night shift (capacity|plan|run), burn week (week|discover|ideas|usage|demo) "
                                     "and end-of-week autoburn (auto)")
    bn.add_argument("action", choices=["capacity", "plan", "run", *WEEK_ACTIONS, "auto"])
    bn.add_argument("plan", nargs="?", help="planning doc (markdown checklist)")
    bn.add_argument("-b", "--burner", help="subscriptions + downtime + model ladder "
                                           "(default: burner.json; burner-week.json for burn week|discover|ideas|usage)")
    bn.add_argument("--now", action="store_true", help="start even if you're not in a downtime window")
    bn.add_argument("--wait", action="store_true", help="sleep until the next downtime window, then start")
    bn.add_argument("--hours", type=float, help="stop after this many hours")
    bn.add_argument("--max-tasks", type=int)
    bn.add_argument("--no-pr", action="store_true", help="leave the branch local")
    wk = bn.add_argument_group("burn week|discover|ideas|usage|demo")
    wk.add_argument("--roots", nargs="+", metavar="DIR", help="where to look for git repos (default: config roots)")
    wk.add_argument("--agents", type=int, help="max concurrent agents (default: config max_agents; demo 8)")
    wk.add_argument("--ideas", action="store_true", help="opt in: mine local Claude Code / Codex chats for task ideas")
    wk.add_argument("--lanes", help="only these lanes, comma-separated names or kinds (e.g. claude,codex)")
    wk.add_argument("--dry-run", action="store_true", help="print the pace and queue plan, start nothing")
    wk.add_argument("--no-dash", action="store_true", help="plain log lines instead of the terminal dashboard")
    wk.add_argument("--web", type=int, metavar="PORT", help="also serve the web dashboard on 127.0.0.1:PORT")
    wk.add_argument("-o", "--out", metavar="PLAN.md", help="discover: write a planning doc here")
    wk.add_argument("--days", type=float, help="ideas: look back this many days (default: config, 21)")
    wk.add_argument("--speed", type=float, help="demo: playback speed (default 1.0)")
    wk.add_argument("--record", metavar="JSONL", help="demo: save the redacted event rows (docs/sample-events.jsonl)")
    au = bn.add_argument_group("burn auto (per plan, before each plan's own reset)")
    au.add_argument("--check", action="store_true", help="auto: decide and fire if due (what the hourly job runs)")
    au.add_argument("--detach", action="store_true", help="auto: fire in the background")
    act = au.add_mutually_exclusive_group()
    act.add_argument("--install", action="store_true", help="auto: run --check every hour")
    act.add_argument("--uninstall", action="store_true", help="auto: remove the hourly job")
    act.add_argument("--status", action="store_true", help="auto: is the hourly job installed?")
    from .schedule import VIAS
    au.add_argument("--via", choices=VIAS, help="auto: scheduler (default: launchd on macOS, else cron)")
    dh = sub.add_parser("dash", help="live terminal dashboard of burn-week events")
    dh.add_argument("--events", help="events.jsonl to follow (default: <cwd>/.relay/events.jsonl)")
    wb = sub.add_parser("web", help="burn-week web dashboard (SSE) on 127.0.0.1")
    wb.add_argument("--port", type=int, default=3737)
    wb.add_argument("--events", help="events.jsonl to serve (default: <cwd>/.relay/events.jsonl)")
    wb.add_argument("--supabase", action="store_true", help="serve the rows in Supabase instead (SUPABASE_URL + SUPABASE_KEY)")
    from . import schedule, windows
    windows.add_cli(sub)      # relay windows plan|prime|due|install|uninstall|status
    schedule.add_cli(sub)     # relay schedule
    args, extra = ap.parse_known_args(argv)

    # stdout belongs to the protocol: dispatch before any banner or print
    if args.cmd == "mcp":
        from .mcp import main as mcp_main
        return mcp_main(["-C", args.cwd])
    if extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    if args.cmd == "savings":
        from . import value
        m = value.month(args.month, args.ledger or [os.path.join(args.cwd, ".relay/events.jsonl")],
                        args.claude_logs, include_claude=not args.no_claude)
        if args.json:
            import json as _json
            print(_json.dumps({"period": m.period, "used_usd": round(m.used_usd, 2), "rescued_usd": round(m.rescued_usd, 2),
                               "rescue_rate": round(m.rescue_rate, 4), "nights": m.nights, "tasks_done": m.tasks_done,
                               "tasks_blocked": m.tasks_blocked, "by_source": m.by_source, "by_model": m.by_model,
                               "unpriced": sorted(m.unpriced)}, indent=2))
        else:
            print(value.render(m, color=sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
        return 0

    if args.cmd == "burn":
        bad = _burn_misuse(args)
        if bad:
            ap.error(bad)
    if (args.cmd == "burn" and args.action == "auto") or args.cmd in ("windows", "schedule"):
        if not args.burner and not os.path.exists("burner-week.json"):   # same fallback as burn week: the documented example
            args.burner = str(Path(__file__).resolve().parent.parent / "examples" / "burner-week.json")
            print(ui.c("2", f"note: no ./burner-week.json, using the example {args.burner} (copy it, then edit plans and hours)"))
        if args.cmd == "burn":
            from . import autoburn
            return autoburn.run_cli(args)
        return args.func(args)
    if args.cmd in ("dash", "web") or (args.cmd == "burn" and args.action in WEEK_ACTIONS):
        return _week_main(args)

    if args.cmd == "burn":
        from . import capacity
        from .config import DEFAULT
        from .plan import parse
        bcfg = load(args.burner or "burner.json")
        bcfg = {**DEFAULT, **bcfg} if "models" not in bcfg else bcfg
        if args.action == "capacity":
            print(capacity.report(capacity.assess(bcfg, os.path.join(args.cwd, ".relay/events.jsonl")), bcfg))
            return 0
        if not args.plan:
            ap.error("burn plan/run needs a planning doc")
        if args.action == "plan":
            pl = parse(args.plan)
            print(capacity.report(capacity.assess(bcfg, os.path.join(args.cwd, ".relay/events.jsonl")), bcfg))
            print(ui.c("1", f"\nqueue · {pl.title}"))
            for proj, t in pl.queue:
                print(f"  {proj.name:<18} {t.text}")
            done = sum(t.state == "x" for p in pl.projects for t in p.tasks)
            print(ui.c("2", f"  ({len(pl.queue)} queued, {done} done)"))
            return 0
        from .burn import burn
        res = burn(args.plan, bcfg, args.cwd, now=args.now, hours=args.hours, max_tasks=args.max_tasks,
                   wait=args.wait, pr=not args.no_pr)
        return 0 if res.get("status") in ("finished", "waiting") else 1

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
