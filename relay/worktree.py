"""Burn-week worktrees: each agent works in `<data_dir>/worktrees/<slug>/<task-id>` on its own local branch
`relay/burn/<task-id>`, made from the repo's HEAD with `git worktree add`.

Safety (SWARM_SPEC invariants 1, 2 and 4): the user's checkout is read-only here. Nothing checks out, switches,
stashes, resets, commits or cleans in it, and nothing talks to a remote. Commits (and the index reset before
them) happen only inside a linked worktree on a relay/burn/* branch. They run with the user's hooks off, since a
post-commit hook could push, and with signing off, so no key store is read. A file matching
contracts.SECRET_FILE_RE is never staged.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from .contracts import is_secret_file, redact, slugify

PREFIX = "relay/burn/"
IDENTITY = {"user.name": "relay burn", "user.email": "relay-burn@localhost"}   # only when the repo has none
NEVER = {"push", "pull", "fetch", "clone", "remote", "ls-remote", "submodule", "checkout", "switch", "stash", "clean",
         "restore", "merge", "rebase", "gc", "prune"}
WORKTREE_ONLY = {"commit", "reset"}              # finalize() only, after _burn_branch() proved it is our worktree
_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
          "GIT_NAMESPACE")
_TAIL_LINES, _TAIL_CHARS = 20, 2000
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _subcommand(args) -> str:
    it = iter(args)
    for a in it:
        if a in ("-c", "-C"):
            next(it, None)
        elif not a.startswith("-"):
            return a
    return ""


def _git(cwd, *args: str, wt: bool = False, input: str | None = None,
         timeout: float = 300) -> subprocess.CompletedProcess:
    """git in `cwd`, hooks off. Remote and checkout-moving commands are refused; commit/reset only with wt=True."""
    sub = _subcommand(args)
    if sub in NEVER or (sub in WORKTREE_ONLY and not wt):
        raise RuntimeError(f"refusing `git {sub}` in {cwd}")
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C")
    io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["git", "-C", str(cwd), "-c", f"core.hooksPath={os.devnull}", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout, **io)


def _lock(repo: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(repo), threading.Lock())


def _safe_id(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(task_id)).strip("-")[:80] or "task"


def create(repo: str, data_dir: str, task_id: str, slug: str) -> tuple[str, str]:
    """`git -C repo worktree add -b relay/burn/<task-id> <data_dir>/worktrees/<slug>/<task-id> HEAD` -> (path, branch).
    If the branch or the path already exists, both get a -2, -3 ... suffix. Nothing is deleted or reused."""
    repo_p = Path(repo).expanduser().resolve()
    root = Path(data_dir).expanduser().resolve() / "worktrees" / slugify(slug)
    if repo_p == root or repo_p in root.parents:
        raise ValueError(f"refusing to nest burn worktrees inside the repo itself ({root})")
    tid = _safe_id(task_id)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(repo_p):                          # concurrent `worktree add` on one repo races on .git/worktrees
        for n in range(1, 100):
            name = tid if n == 1 else f"{tid}-{n}"
            path, branch = root / name, PREFIX + name
            if path.exists() or _git(repo_p, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
                continue
            r = _git(repo_p, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
            if r.returncode == 0:
                return str(path), branch
            if not (path.exists() or "already" in r.stderr):
                raise RuntimeError(f"git worktree add failed: {redact(r.stderr.strip())[:300]}")
    raise RuntimeError(f"no free worktree name for {tid} under {root}")


def _burn_branch(wt: Path) -> str:
    """The relay/burn/* branch checked out in `wt`. Raises unless wt is a linked worktree on such a branch."""
    ref = _git(wt, "symbolic-ref", "-q", "HEAD").stdout.strip()
    dirs = [d.strip() for d in _git(wt, "rev-parse", "--git-dir", "--git-common-dir").stdout.splitlines()]
    linked = len(dirs) == 2 and all(dirs) and (wt / dirs[0]).resolve() != (wt / dirs[1]).resolve()
    if not (linked and ref.startswith("refs/heads/" + PREFIX)):
        raise RuntimeError(f"refusing to commit in {wt}: not a linked {PREFIX}* worktree")
    return ref[len("refs/heads/"):]


def _base(wt: Path, branch: str) -> str:
    """The commit the branch was created from (its oldest reflog entry), else HEAD."""
    shas = _git(wt, "reflog", "show", "--format=%H", f"refs/heads/{branch}").stdout.split()
    return shas[-1] if shas else _git(wt, "rev-parse", "HEAD").stdout.strip()


def _changed(wt: Path) -> list[str]:
    """Paths that differ from the index: edits, deletions, untracked files. Embedded repos are skipped."""
    out = _git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    return [e[3:] for e in out.split("\0") if len(e) > 3 and not e.endswith("/")]


def _kill_group(p: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL, the test run's whole process group (it was started in its own session)."""
    if not hasattr(os, "killpg"):
        p.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            return                               # the group is gone
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _run_tests(cmd: str, cwd: Path, timeout: float) -> tuple[bool, str]:
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         start_new_session=True, env={**os.environ, "CI": "true", "GIT_TERMINAL_PROMPT": "0"})
    try:
        out, _ = p.communicate(timeout=timeout)
        ok, note = p.returncode == 0, "" if p.returncode == 0 else f"[exit {p.returncode}]"
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired as e:   # a grandchild left the group and still holds the pipe
            out = e.output.decode("utf-8", "replace") if isinstance(e.output, bytes) else (e.output or "")
        ok, note = False, f"[timed out after {timeout:g}s; process group killed]"
    lines = ((out or "").rstrip() + ("\n" + note if note else "")).strip().splitlines()
    return ok, redact("\n".join(lines[-_TAIL_LINES:]))[-_TAIL_CHARS:]


def _shortstat(s: str) -> dict:
    def num(pat: str) -> int:
        m = re.search(pat, s)
        return int(m.group(1)) if m else 0
    return {"files": num(r"(\d+) files? changed"), "insertions": num(r"(\d+) insertions?\(\+\)"),
            "deletions": num(r"(\d+) deletions?\(-\)")}


def finalize(path: str, message: str, test_cmd: str | None = None, timeout: float = 600) -> dict:
    """Stage the worktree's changes (never secret files, never .relay/), run `test_cmd` with a timeout, and commit
    on the worktree's relay/burn branch: one commit per task, folding any commits the agent made despite the
    prompt. Returns {"tests_ok", "tests_tail", "commit", "diffstat", "files", "insertions", "deletions"}."""
    wt = Path(path).expanduser().resolve()
    wt = Path(_git(wt, "rev-parse", "--show-toplevel").stdout.strip() or wt).resolve()
    branch = _burn_branch(wt)
    base = _base(wt, branch)
    r = _git(wt, "reset", "-q", base, wt=True)  # index (and branch) back to the fork point; work tree untouched
    if r.returncode != 0:
        raise RuntimeError(f"could not reset the worktree index: {redact(r.stderr.strip())[:300]}")
    keep =[p for p in _changed(wt) if not is_secret_file(p) and not p.startswith(".relay/")]
    if keep:
        r = _git(wt, "--literal-pathspecs", "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                 input="\0".join(keep))
        if r.returncode != 0:
            raise RuntimeError(f"git add failed: {redact(r.stderr.strip())[:300]}")
    staged = _git(wt, "diff", "--cached", "--name-only", "-z").stdout.split("\0")
    leaked = [p for p in staged if p and is_secret_file(p)]
    if leaked:                                   # belt and braces: unstage anything secret-shaped
        _git(wt, "reset", "-q", "--", *leaked, wt=True)
    tests_ok, tail = (None, "") if not test_cmd else _run_tests(test_cmd, wt, timeout)
    commit = None
    if _git(wt, "diff", "--cached", "--quiet").returncode == 1:
        ident = [a for k, v in IDENTITY.items() if not _git(wt, "config", k).stdout.strip() for a in ("-c", f"{k}={v}")]
        msg = redact(message.strip() or "relay burn")
        if test_cmd:
            msg += f"\n\nRelay-Tests: {'pass' if tests_ok else 'fail'} ({redact(test_cmd)})"
        r = _git(wt, *ident, "-c", "commit.gpgsign=false", "-c", "gc.auto=0", "commit", "-q", "--no-verify",
                 "-F", "-", wt=True, input=msg + "\n")
        if r.returncode != 0:
            raise RuntimeError(f"git commit failed: {redact(r.stderr.strip())[:300]}")
        commit = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    stat = _git(wt, "diff", "--shortstat", base, "HEAD").stdout.strip()
    return {"tests_ok": tests_ok, "tests_tail": tail, "commit": commit, "diffstat": stat, **_shortstat(stat)}


def _worktrees(repo: Path) -> list[dict]:
    rows = []
    for block in _git(repo, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        kv = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if "worktree" in kv:
            rows.append({"path": Path(kv["worktree"]).resolve(), "branch": kv.get("branch", "")[len("refs/heads/"):],
                         "main": not rows})
    return rows


def remove(repo: str, path: str) -> None:
    """`git worktree remove --force <path>`: deletes a relay/burn worktree dir, untracked files too, and keeps
    its branch."""
    repo_p, wt = Path(repo).expanduser().resolve(), Path(path).expanduser().resolve()
    entry = next((e for e in _worktrees(repo_p) if e["path"] == wt), None)
    if entry is None:
        return                                   # not (or no longer) a worktree of this repo: nothing to remove
    if entry["main"] or not entry["branch"].startswith(PREFIX):
        raise RuntimeError(f"refusing to remove {wt}: not a {PREFIX}* worktree")
    with _lock(repo_p):
        r = _git(repo_p, "worktree", "remove", "--force", str(wt))
    if r.returncode != 0:
        raise RuntimeError(f"git worktree remove failed: {redact(r.stderr.strip())[:300]}")


def branches(repo: str) -> list[dict]:
    """[{"branch", "sha", "subject"}] for every relay/burn/* branch, newest commit first."""
    out = _git(Path(repo).expanduser(), "for-each-ref", "--sort=-committerdate",
               "--format=%(refname)%00%(objectname:short)%00%(subject)", "refs/heads/" + PREFIX.rstrip("/")).stdout
    rows = []
    for line in out.splitlines():
        ref, sha, subject = (line.split("\0") + ["", ""])[:3]
        rows.append({"branch": ref[len("refs/heads/"):], "sha": sha, "subject": redact(subject)})
    return rows
