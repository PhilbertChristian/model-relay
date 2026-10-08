"""Night shift: burn subscription capacity that would otherwise expire, on the plans you left behind.

    plan (markdown checklist) ─┐
    capacity (what expires) ───┼─> queue ordered by project priority ─> one Relay agent per task
    downtime window ───────────┘        (models ordered by burn urgency, capped by tonight's allowance)
                                    ─> test ─> commit on relay/night-<date> ─> PR ─> MORNING.md

Each project is worked in its own git worktree, `<data_dir>/worktrees/<slug>/night-<date>`, on a fresh branch
`relay/night-<date>` cut from the repo's HEAD (relay/worktree.py). Your checkout keeps its branch, HEAD, index and
files: nothing checks out, switches, stashes, resets, commits or cleans there. Only the night branch is pushed,
from its worktree, and GITHUB_TOKEN reaches git through that one command's environment, never .git/config.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import capacity, schedule, ui, worktree
from .agent import Agent
from .config import build_router
from .contracts import redact
from .plan import Plan, Project, parse
from .telemetry import Telemetry
from .tools import Toolbox
from .worktree import _SCRUB

PROMPT = """You are working an unattended overnight shift on the project "{project}".
{notes}
Task ({n} of {total} tonight): {task}

Your working directory is a git worktree of the repository on tonight's own branch. Make the change, then verify it{test_hint}.
Relay commits your work after each task: do not commit, push, stash, or create, switch or delete branches yourself.
Keep the change focused on this task. Nobody is awake to answer questions: if you need a human, reply BLOCKED: <what you need>."""
NIGHT_IDENTITY = {"user.name": "relay night shift", "user.email": "relay@localhost"}
GITHUB_RE = re.compile(r"(?<![\w.-])github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/*$")


@dataclass
class Workspace:
    repo: Path          # the repo the night branch lives in; its checkout is read-only to the night shift
    worktree: Path      # <data_dir>/worktrees/<slug>/night-<date>: where the work and the commits happen
    cwd: Path           # the agent's directory: the worktree, or the `dir:` subfolder inside it
    branch: str         # relay/night-<date>, with a -2, -3 ... suffix when that name is taken


def _git(cwd: Path, *args: str, env: dict | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    """git in `cwd` with the user's hooks off and no GIT_DIR-style overrides inherited from the environment."""
    clean = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    return subprocess.run(["git", "-C", str(cwd), "-c", f"core.hooksPath={os.devnull}", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                          env={**clean, "GIT_TERMINAL_PROMPT": "0", **(env or {})})


def _github_auth(url: str) -> dict:
    """Environment that hands GITHUB_TOKEN to one git command as an HTTP header for https://github.com/ URLs. The
    token never lands in .git/config or in argv, and the user's credential helpers are left out of it."""
    tok = os.environ.get("GITHUB_TOKEN")
    if not (tok and url.startswith("https://github.com/")):
        return {}
    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": ""}


def source_repo(p: Project, root: Path) -> tuple[Path, str]:
    """(repo, the project's folder inside it) that the night branch is cut from. A `dir:` inside a git work tree is
    used as it is. Otherwise the folder (`dir:`, else <root>/<slug>) must be missing or empty: Relay clones `repo:`
    there, or starts a fresh repo. Anything else is refused: Relay never runs `git init` on your files."""
    path = Path(os.path.expanduser(p.dir)) if p.dir else Path(p.slug)
    path = path if path.is_absolute() else root / path
    if path.is_dir() and (p.dir or (path / ".git").exists()):
        r = _git(path, "rev-parse", "--show-toplevel", "--show-prefix")
        top, _, sub = r.stdout.partition("\n")
        if r.returncode == 0 and top:
            return Path(top), sub.rstrip("\n")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RuntimeError(f"{path} is not a git repository; Relay works in a worktree of one and won't `git init` it")
    path.mkdir(parents=True, exist_ok=True)
    if p.repo:
        r = _git(path.parent, "clone", "-q", "--", p.repo, str(path), env=_github_auth(p.repo), timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {redact(r.stderr.strip())[:200]}")
    else:
        _git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text(f"# {p.name}\n")
        _git(path, "add", "-A")
        _git(path, "-c", "user.name=relay", "-c", "user.email=relay@localhost", "-c", "commit.gpgsign=false",
             "commit", "-qm", "init")
    return path.resolve(), ""


def _start(repo: Path, base: str | None) -> str:
    """The commit the night branch is cut from: the plan's `branch:` (else origin/<branch>), else HEAD."""
    for ref in ([base, f"origin/{base}"] if base else ["HEAD"]):
        sha = _git(repo, "rev-parse", "--verify", "-q", "--end-of-options", ref + "^{commit}").stdout.strip()
        if sha:
            return sha
    raise RuntimeError(f"branch: {base} is not in {repo}" if base else f"{repo} has no commits yet")


def prepare_workspace(p: Project, root: Path, data_dir: Path, night: str) -> Workspace:
    """A worktree of the project's repo on a fresh `relay/<night>` branch, under data_dir. The repo only gains
    that branch and the worktree's entry in .git; its checkout is never touched."""
    repo, sub = source_repo(p, root)
    path, branch = worktree.create(str(repo), str(data_dir), night, p.slug, prefix="relay/",
                                   start=_start(repo, p.base))
    cwd = Path(path) / sub
    cwd.mkdir(parents=True, exist_ok=True)
    return Workspace(repo, Path(path), cwd, branch)


def run_tests(cmd: str | None, cwd: Path) -> tuple[bool, str]:
    if not cmd:
        return True, "no test command"
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "tests timed out after 600s"
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return r.returncode == 0, " / ".join(tail)[:200] or f"exit {r.returncode}"


def commit(ws: Workspace, msg: str, base: str) -> str | None:
    """One commit for the task on the night branch, made in the worktree: commits the agent made since `base` fold
    into it, secret-looking files are never staged, and the user's hooks don't run (worktree.finalize)."""
    if not base:                             # never fall back to the fork point: that would fold the whole night
        raise RuntimeError(f"no HEAD in {ws.worktree}")
    return worktree.finalize(str(ws.worktree), msg, base=base, identity=NIGHT_IDENTITY)["commit"]


def open_pr(ws: Workspace, title: str, body: str) -> str:
    """Push the night branch, and nothing else, from its worktree; then open a draft PR against the default branch."""
    m = GITHUB_RE.search(_git(ws.worktree, "remote", "get-url", "origin").stdout.strip())
    tok = os.environ.get("GITHUB_TOKEN")
    if not (m and tok):
        return f"branch `{ws.branch}` ready locally in {ws.repo} (no GitHub remote or GITHUB_TOKEN for a PR)"
    url, ref = f"https://github.com/{m[1]}/{m[2]}.git", f"refs/heads/{ws.branch}"
    try:
        push = _git(ws.worktree, "push", "-q", url, f"{ref}:{ref}", env=_github_auth(url), timeout=600)
    except subprocess.TimeoutExpired:
        return f"push of `{ws.branch}` timed out"
    if push.returncode != 0:
        return f"push failed: {redact(push.stderr.strip())[:200]}"
    api = f"https://api.github.com/repos/{m[1]}/{m[2]}"
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    try:
        default = json.loads(urllib.request.urlopen(urllib.request.Request(api, headers=headers),
                                                    timeout=30).read())["default_branch"]
        req = urllib.request.Request(f"{api}/pulls", method="POST", headers=headers, data=json.dumps(
            {"title": title, "head": ws.branch, "base": default, "body": body, "draft": True}).encode())
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["html_url"]
    except Exception as e:
        return f"pushed `{ws.branch}`; PR not opened ({redact(str(e))[:160]})"


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
                          "spent": 0.0, "kind": c.kind,
                          "expires": c.kind != "api_budget" or bool(next((s.get("expires") for s in cfg.get("subscriptions", [])
                                                                           if s.get("name") == c.name), False))} for c in caps]
    tel = Telemetry(str(root_p / ".relay"))
    tel.emit("shift_start", plan=plan.title, queue=len(plan.queue),
             capacity=[{"name": c.name, "kind": c.kind, "tonight": c.tonight, "unit": c.unit, "providers": c.providers,
                       "expires": c.kind != "api_budget" or bool(next((s.get("expires") for s in cfg.get("subscriptions", [])
                                                                       if s.get("name") == c.name), False))} for c in caps])

    queue = plan.queue[:max_tasks] if max_tasks else plan.queue
    night, data_dir = f"night-{datetime.now():%Y%m%d}", schedule.data_dir(cfg)
    print(ui.c("1", f"\nqueue: {len(queue)} tasks across {len({p.name for p, _ in queue})} projects · branch relay/{night}")
          + ui.c("2", f" · worktrees in {data_dir / 'worktrees'}"))
    results: list[dict] = []
    workspaces: dict[str, Workspace] = {}
    broken: dict[str, str] = {}          # project -> why its workspace can't be used tonight
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
        if proj.name in broken:
            results.append({"project": proj.name, "task": task.text, "status": "skipped", "note": broken[proj.name]})
            continue
        print(ui.c("1;36", f"\n━━ [{i}/{len(queue)}] {proj.name}: {task.text}"))
        if proj.name not in workspaces:
            try:
                ws = workspaces[proj.name] = prepare_workspace(proj, root_p, data_dir, night)
            except Exception as e:           # one project that can't be set up never takes the shift down
                why = broken[proj.name] = f"workspace: {redact(str(e))[:200]}"
                ui.say(ui.c("1;31", "! blocked") + ui.c("2", f"  {why}"))
                results.append({"project": proj.name, "task": task.text, "status": "blocked", "note": why,
                                "usd": 0.0, "commit": None})
                tel.emit("task", project=proj.name, task=task.text, status="blocked", usd=0.0, note=why)
                continue
            print(ui.c("2", f"worktree {ws.worktree} on {ws.branch}, from {ws.repo}"))
        ws = workspaces[proj.name]
        head = _git(ws.worktree, "rev-parse", "HEAD").stdout.strip()    # this task's one commit goes on top
        before = router.totals()["usd"]
        agent = Agent(router, Toolbox(str(ws.cwd)), tel, max_steps=cfg.get("max_steps", 40), summary=False)
        final = agent.run(PROMPT.format(project=proj.name, notes=f"Context: {proj.notes}" if proj.notes else "",
                                        n=i, total=len(queue), task=task.text,
                                        test_hint=f" and make sure `{proj.test}` passes" if proj.test else ""))
        spent = router.totals()["usd"] - before
        project_spend[proj.name] = project_spend.get(proj.name, 0) + spent
        ok, test_out = run_tests(proj.test, ws.cwd) if agent.outcome == "done" else (False, "")
        if agent.outcome == "done" and ok:
            status, note, msg = "done", "", f"{task.text}\n\nrelay night shift · ${spent:.4f}"
        else:
            note = {"blocked": re.sub(r"^\s*BLOCKED:?\s*", "", final.strip(), flags=re.I)[:160], "refused": "agent refused; needs a human",
                    "no_capacity": "ran out of capacity", "max_steps": "ran out of steps"}.get(
                agent.outcome, f"tests failing: {test_out}")
            status, msg = "blocked", f"WIP (blocked): {task.text}\n\n{note}"
        try:
            sha = commit(ws, msg, head)
        except Exception as e:               # e.g. the agent switched the worktree off the night branch
            status, sha = "blocked", None
            note = broken[proj.name] = f"not committed, work left in {ws.worktree}: {redact(str(e))[:160]}"
        if status == "done":
            plan.mark(task, "x", f"relay {sha or 'no-op'}")
        else:
            plan.mark(task, "!", f"blocked: {note}")
        ui.say(ui.c("1;32" if status == "done" else "1;31", f"{'✓' if status == 'done' else '!'} {status}") +
               ui.c("2", f"  ${spent:,.2f}  {note}"))
        results.append({"project": proj.name, "task": task.text, "status": status, "note": note,
                        "usd": spent, "commit": sha})
        tel.emit("task", project=proj.name, task=task.text, status=status, usd=spent, note=note)
    else:
        stop_reason = "queue empty"

    deploys = {}
    for p in plan.projects:
        if (p.deploy or "").lower() == "instacloud" and p.name in workspaces and any(
                r["project"] == p.name and r["status"] == "done" for r in results):
            from .sponsors import instacloud_deploy
            deploys[p.name] = instacloud_deploy(str(workspaces[p.name].cwd), workspaces[p.name].branch)
            tel.emit("deploy", project=p.name, target="instacloud", result=deploys[p.name])
            print(ui.c("1;36", f"⬆ InstaCloud {p.name}: {deploys[p.name]}"))

    prs = {}
    for name, ws in workspaces.items():
        done = [r for r in results if r["project"] == name and r.get("commit")]
        if not done:
            continue
        body = "Overnight work by relay night shift.\n\n" + "\n".join(
            f"- [{'x' if r['status'] == 'done' else ' '}] {r['task']}{' — ' + r['note'] if r['note'] else ''}"
            for r in results if r["project"] == name)
        prs[name] = open_pr(ws, f"Night shift: {len(done)} task(s)", body) if pr else f"branch `{ws.branch}` in {ws.repo}"

    for name, ws in workspaces.items():
        if cfg.get("keep_worktrees") or name in broken:              # broken: uncommitted work is still in there
            continue
        try:
            worktree.remove(str(ws.repo), str(ws.worktree))         # the branch keeps the work
        except Exception:
            pass                                                    # a leftover worktree is untidy, not unsafe

    t = router.totals()
    report = morning_report(plan, results, prs, caps, router, stop_reason, ledger=str(ledger))
    if deploys:
        report = report.replace("## Capacity burned", "## Previews (InstaCloud)\n" + "".join(
            f"- {k}: {v}\n" for k, v in deploys.items()) + "\n## Capacity burned")
    (root_p / "MORNING.md").write_text(report)
    tel.emit("shift_end", usd=t["usd"], baseline_usd=router.counterfactual_usd(), done=sum(r["status"] == "done" for r in results),
             blocked=sum(r["status"] == "blocked" for r in results), stop=stop_reason)
    print("\n" + report)
    return {"status": "finished", "results": results, "prs": prs, "usd": t["usd"],
            "branches": {name: ws.branch for name, ws in workspaces.items()}}


def morning_report(plan: Plan, results: list[dict], prs: dict, caps, router, stop: str,
                   ledger: str | None = None) -> str:
    from . import value
    done = [r for r in results if r["status"] == "done"]
    blocked = [r for r in results if r["status"] == "blocked"]
    lines = [f"# Good morning ☕  {plan.title}", "",
             f"**{len(done)} done · {len(blocked)} need you · {len(plan.queue)} still queued** · stopped: {stop}", ""]
    tonight = paygo = 0.0
    for a in router.allowances:
        if not a.get("expires", True):
            paygo += a["spent"]          # pay-as-you-go is new money, never rescued
            continue
        tonight += min(a["spent"], a["usd"]) if a["usd"] is not None else a["spent"]
    lines += ["## Credits", f"- Tonight: **{value.money(tonight)} rescued** at API rates from capacity that was about to expire"]
    if paygo >= 0.01:
        lines.append(f"- Pay-as-you-go spend: {value.money(paygo)} (new money, not counted as rescued)")
    if ledger:
        m = value.month(ledgers=[ledger], include_claude=os.environ.get("RELAY_CLAUDE_LOGS", "1") != "0")
        if m.used_usd >= 0.01 and m.nights > 1:
            lines.append(f"- This month: **{value.money(m.used_usd)} used at API rates · {value.money(m.rescued_usd)} rescued** "
                         f"({m.rescue_rate:.1%}) over {m.nights} night shift(s)")
    lines.append("")
    if done:
        lines += ["## Done"] + [f"- {r['project']}: {r['task']}  (`{r['commit']}`, ${r['usd']:,.2f})" for r in done] + [""]
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
