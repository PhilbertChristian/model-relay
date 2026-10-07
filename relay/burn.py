"""Night shift: burn subscription capacity that would otherwise expire, on the plans you left behind.

    plan (markdown checklist) ─┐
    capacity (what expires) ───┼─> queue ordered by project priority ─> one Relay agent per task
    downtime window ───────────┘        (models ordered by burn urgency, capped by tonight's allowance)
                                    ─> test ─> commit on relay/night-<date> ─> PR ─> MORNING.md
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import capacity, ui
from .agent import Agent
from .config import build_router
from .plan import Plan, Project, parse
from .telemetry import Telemetry
from .tools import Toolbox

PROMPT = """You are working an unattended overnight shift on the project "{project}".
{notes}
Task ({n} of {total} tonight): {task}

The repository is your working directory. Make the change, then verify it{test_hint}.
Keep the change focused on this task. Nobody is awake to answer questions: if you need a human, reply BLOCKED: <what you need>."""


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _authed(url: str) -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and url.startswith("https://github.com/"):
        return url.replace("https://", f"https://x-access-token:{tok}@")
    return url


def prepare_workspace(p: Project, root: Path, branch: str) -> Path:
    if p.dir:
        path = Path(os.path.expanduser(p.dir))
        path = path if path.is_absolute() else root / path
    else:
        path = root / p.slug
    if p.repo and not (path / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", _authed(p.repo), str(path)], check=True)
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _git(path, "init", "-q", "-b", "main")
        (path / "README.md").exists() or (path / "README.md").write_text(f"# {p.name}\n")
        _git(path, "add", "-A")
        _git(path, "-c", "user.name=relay", "-c", "user.email=relay@localhost", "commit", "-qm", "init")
    if p.base:
        _git(path, "checkout", "-q", p.base)
    if _git(path, "rev-parse", "--verify", "-q", branch).returncode == 0:
        _git(path, "checkout", "-q", branch)
    else:
        _git(path, "checkout", "-q", "-b", branch)
    return path


def run_tests(cmd: str | None, cwd: Path) -> tuple[bool, str]:
    if not cmd:
        return True, "no test command"
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "tests timed out after 600s"
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return r.returncode == 0, " / ".join(tail)[:200] or f"exit {r.returncode}"


def commit(cwd: Path, msg: str) -> str | None:
    _git(cwd, "add", "-A")
    if _git(cwd, "diff", "--cached", "--quiet").returncode == 0:
        return None
    _git(cwd, "-c", "user.name=relay night shift", "-c", "user.email=relay@localhost", "commit", "-qm", msg)
    return _git(cwd, "rev-parse", "--short", "HEAD").stdout.strip()


def open_pr(cwd: Path, branch: str, title: str, body: str) -> str:
    remote = _git(cwd, "remote", "get-url", "origin").stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote)
    tok = os.environ.get("GITHUB_TOKEN")
    if not (m and tok):
        return f"branch `{branch}` ready locally at {cwd} (no GitHub remote or GITHUB_TOKEN for a PR)"
    push = _git(cwd, "push", "-q", "-u", _authed(f"https://github.com/{m[1]}/{m[2]}.git"), branch)
    if push.returncode != 0:
        return f"push failed: {push.stderr.strip()[:200]}"
    default = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"https://api.github.com/repos/{m[1]}/{m[2]}", headers={"Authorization": f"Bearer {tok}"})).read())["default_branch"]
    req = urllib.request.Request(f"https://api.github.com/repos/{m[1]}/{m[2]}/pulls", method="POST",
                                 data=json.dumps({"title": title, "head": branch, "base": default, "body": body,
                                                  "draft": True}).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        return json.loads(urllib.request.urlopen(req).read())["html_url"]
    except Exception as e:
        return f"pushed `{branch}`; PR not opened ({e})"


def burn(plan_path: str, cfg: dict, root: str = ".", now: bool = False, hours: float | None = None,
         max_tasks: int | None = None, wait: bool = False, pr: bool = True) -> dict:
    root_p = Path(root).expanduser().resolve()
    root_p.mkdir(parents=True, exist_ok=True)
    ledger = root_p / ".relay" / "events.jsonl"
    plan: Plan = parse(plan_path)
    idle = cfg.get("idle", {"weekday": "23:00-07:00", "weekend": "all"})
    caps = capacity.assess(cfg, str(ledger))

    ui.banner()
    print(ui.c("1", "night shift · ") + plan.title)
    print(capacity.report(caps, cfg))

    start, end, active = capacity.current_or_next_window(idle)
    if not active and not now:
        if not wait:
            print(ui.c("33", f"\nnot in your downtime; next window {start:%a %H:%M}. Use --now to start anyway, "
                             f"or --wait to sleep until then."))
            return {"status": "waiting", "next": start.isoformat()}
        delay = (start - datetime.now(start.tzinfo)).total_seconds()
        print(ui.c("2", f"\nsleeping {delay / 3600:.1f}h until downtime starts at {start:%a %H:%M}"))
        time.sleep(max(0, delay))
    deadline = time.time() + hours * 3600 if hours else (end.timestamp() if active or wait else time.time() + 8 * 3600)

    router = build_router(cfg)
    router.provider_priority = {p: i for i, c in enumerate(caps) for p in c.providers}
    router.allowances = [{"name": c.name, "providers": set(c.providers), "usd": c.tonight if c.unit == "usd" else None,
                          "spent": 0.0} for c in caps]
    tel = Telemetry(str(root_p / ".relay"))
    tel.emit("shift_start", plan=plan.title, queue=len(plan.queue),
             capacity=[{"name": c.name, "tonight": c.tonight, "unit": c.unit} for c in caps])

    queue = plan.queue[:max_tasks] if max_tasks else plan.queue
    branch = f"relay/night-{datetime.now():%Y%m%d}"
    print(ui.c("1", f"\nqueue: {len(queue)} tasks across {len({p.name for p, _ in queue})} projects · branch {branch}"))
    results: list[dict] = []
    workspaces: dict[str, Path] = {}
    project_spend: dict[str, float] = {}
    stop_reason = "queue empty"

    for i, (proj, task) in enumerate(queue, 1):
        if time.time() > deadline:
            stop_reason = "downtime window ended"
            break
        if not router.has_capacity():
            stop_reason = "all tonight's capacity burned"
            break
        if proj.budget is not None and project_spend.get(proj.name, 0) >= proj.budget:
            results.append({"project": proj.name, "task": task.text, "status": "skipped", "note": "project budget reached"})
            continue
        print(ui.c("1;36", f"\n━━ [{i}/{len(queue)}] {proj.name}: {task.text}"))
        if proj.name not in workspaces:
            workspaces[proj.name] = prepare_workspace(proj, root_p, branch)
        ws = workspaces[proj.name]
        before = router.totals()["usd"]
        agent = Agent(router, Toolbox(str(ws)), tel, max_steps=cfg.get("max_steps", 40), summary=False)
        final = agent.run(PROMPT.format(project=proj.name, notes=f"Context: {proj.notes}" if proj.notes else "",
                                        n=i, total=len(queue), task=task.text,
                                        test_hint=f" and make sure `{proj.test}` passes" if proj.test else ""))
        spent = router.totals()["usd"] - before
        project_spend[proj.name] = project_spend.get(proj.name, 0) + spent
        ok, test_out = run_tests(proj.test, ws) if agent.outcome == "done" else (False, "")
        if agent.outcome == "done" and ok:
            status, note = "done", ""
            sha = commit(ws, f"{task.text}\n\nrelay night shift · ${spent:.4f}")
            plan.mark(task, "x", f"relay {sha or 'no-op'}")
        else:
            why = {"blocked": re.sub(r"^\s*BLOCKED:?\s*", "", final.strip(), flags=re.I)[:160], "refused": "agent refused; needs a human",
                   "no_capacity": "ran out of capacity", "max_steps": "ran out of steps"}.get(
                agent.outcome, f"tests failing: {test_out}")
            status, note = "blocked", why
            sha = commit(ws, f"WIP (blocked): {task.text}\n\n{why}")
            plan.mark(task, "!", f"blocked: {why}")
        ui.say(ui.c("1;32" if status == "done" else "1;31", f"{'✓' if status == 'done' else '!'} {status}") +
               ui.c("2", f"  ${spent:.4f}  {note}"))
        results.append({"project": proj.name, "task": task.text, "status": status, "note": note,
                        "usd": spent, "commit": sha})
        tel.emit("task", project=proj.name, task=task.text, status=status, usd=spent, note=note)
    else:
        stop_reason = "queue empty"

    prs = {}
    for name, ws in workspaces.items():
        done = [r for r in results if r["project"] == name and r.get("commit")]
        if not done:
            continue
        body = "Overnight work by relay night shift.\n\n" + "\n".join(
            f"- [{'x' if r['status'] == 'done' else ' '}] {r['task']}{' — ' + r['note'] if r['note'] else ''}"
            for r in results if r["project"] == name)
        prs[name] = open_pr(ws, branch, f"Night shift: {len(done)} task(s)", body) if pr else f"branch `{branch}` at {ws}"

    t = router.totals()
    report = morning_report(plan, results, prs, caps, router, stop_reason)
    (root_p / "MORNING.md").write_text(report)
    tel.emit("shift_end", usd=t["usd"], baseline_usd=router.counterfactual_usd(), done=sum(r["status"] == "done" for r in results),
             blocked=sum(r["status"] == "blocked" for r in results), stop=stop_reason)
    print("\n" + report)
    return {"status": "finished", "results": results, "prs": prs, "usd": t["usd"]}


def morning_report(plan: Plan, results: list[dict], prs: dict, caps, router, stop: str) -> str:
    done = [r for r in results if r["status"] == "done"]
    blocked = [r for r in results if r["status"] == "blocked"]
    lines = [f"# Good morning ☕  {plan.title}", "",
             f"**{len(done)} done · {len(blocked)} need you · {len(plan.queue)} still queued** · stopped: {stop}", ""]
    if done:
        lines += ["## Done"] + [f"- {r['project']}: {r['task']}  (`{r['commit']}`, ${r['usd']:.4f})" for r in done] + [""]
    if blocked:
        lines += ["## Needs you"] + [f"- {r['project']}: {r['task']} — {r['note']}" for r in blocked] + [""]
    if prs:
        lines += ["## Review"] + [f"- {k}: {v}" for k, v in prs.items()] + [""]
    lines += ["## Capacity burned"]
    for a in router.allowances:
        cap = next((c for c in caps if c.name == a["name"]), None)
        if cap and cap.unit == "usd":
            lines.append(f"- {a['name']}: ${a['spent']:.4f} of tonight's ${cap.tonight:.2f} allowance "
                         f"(${cap.expiring:.2f} would have expired unused by {cap.resets_at:%b %d})")
        elif cap:
            lines.append(f"- {a['name']}: up to {cap.tonight:g} usage window(s) available tonight "
                         f"({cap.note})")
    lines += ["", "## Models", ""] + [f"- {m.id}: {router.state[m.id].calls} calls, ${router.state[m.id].usd:.4f}"
                                      for m in router.models if router.state[m.id].calls]
    return "\n".join(lines) + "\n"
