"""python3 -m unittest tests.test_burn_isolation -v   (temp git repos, a scripted stand-in for the agent; offline)

The night shift (relay/burn.py) on a plan whose `dir:` is the user's real checkout, the way `relay burn discover -o`
writes them. The work must land on a relay/night-* branch, made in a worktree under the data dir, and the checkout
must keep its branch, HEAD, index, working tree and untracked files, however dirty it is.
"""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from relay import burn

CFG = {"idle": {"weekday": "all", "weekend": "all"}, "subscriptions": [], "providers": {"mock": {"kind": "mock"}},
       "models": [{"id": "m", "provider": "mock", "model": "m", "tier": 0}]}
TOKEN = "night-shift-test-token-0123456789abcdef"        # not a real credential
NETWORK_ENV = ("GITHUB_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "AGENT37_API_KEY")


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check).stdout.strip()


def make_checkout(root: Path) -> Path:
    """The user's own checkout, mid-work: on a feature branch, with a staged edit, an unstaged edit and untracked
    files, one of them a secret."""
    repo = root / "app"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Repo Owner")
    git(repo, "config", "user.email", "owner@example.com")
    (repo / "app.py").write_text("print('v1')\n")
    (repo / "lib.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "release")
    git(repo, "switch", "-q", "-c", "feature/wip")
    (repo / "wip.py").write_text("wip = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "wip")
    (repo / "lib.py").write_text("x = 2  # staged, not committed\n")
    git(repo, "add", "lib.py")
    (repo / "app.py").write_text("print('v2, not committed')\n")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-proj-0123456789abcdefghijkl\n")
    (repo / "notes").mkdir()
    (repo / "notes" / "scratch.txt").write_text("my notes\n")
    return repo


def snapshot(repo: Path) -> dict:
    """Everything a night must leave alone in the user's checkout."""
    files = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(repo).parts}
    refs = [r for r in git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads").splitlines()
            if not r.startswith("refs/heads/relay/night-")]
    return {"branch": git(repo, "symbolic-ref", "HEAD"), "head": git(repo, "rev-parse", "HEAD"),
            "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
            "untracked": git(repo, "ls-files", "--others"), "index": git(repo, "ls-files", "--stage"),
            "files": files, "refs": refs, "stash": git(repo, "stash", "list"),
            "config": (repo / ".git" / "config").read_text()}


class FakeAgent:
    """Stands in for relay.agent.Agent: writes the `<name>.py` its task names, plus a .env that must never be
    committed. "itself" also commits with git, and "blocked" ends BLOCKED. Records where and how it ran."""
    runs: list = []

    def __init__(self, router, tools, tel, max_steps=40, summary=True):
        self.cwd, self.outcome = Path(tools.root), "max_steps"

    def run(self, prompt: str) -> str:
        task = re.search(r"Task \(\d+ of \d+ tonight\): (.+)", prompt)[1]
        FakeAgent.runs.append({"task": task, "cwd": self.cwd, "branch": git(self.cwd, "symbolic-ref", "--short", "HEAD"),
                               "dirs": git(self.cwd, "rev-parse", "--git-dir", "--git-common-dir").splitlines(),
                               "files": sorted(str(p.relative_to(self.cwd)) for p in self.cwd.rglob("*")
                                               if ".git" not in p.relative_to(self.cwd).parts)})
        name = re.search(r"\b(\w+\.py)\b", task)
        if name:
            (self.cwd / name[1]).write_text(f"# {task}\nok = True\n")
        (self.cwd / ".env").write_text("OPENAI_API_KEY=sk-proj-agentwrotethis0123456789\n")
        if "itself" in task:
            git(self.cwd, "add", "-A")
            git(self.cwd, "commit", "-qm", "the agent's own commit")
        if "blocked" in task:
            self.outcome = "blocked"
            return "BLOCKED: needs a PyPI token"
        self.outcome = "done"
        return f"Done: {task}"


class NightCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-night-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gitconfig = self.tmp / "gitconfig"
        self.gitconfig.write_text("")
        env = {k: v for k, v in os.environ.items() if k not in NETWORK_ENV}
        env.update(GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1", RELAY_CLAUDE_LOGS="0")
        for patch in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(burn, "Agent", FakeAgent)):
            patch.start()
            self.addCleanup(patch.stop)
        FakeAgent.runs = []
        self.data, self.work, self.plan = self.tmp / "data", self.tmp / "night", self.tmp / "PLAN.md"
        self.repo = make_checkout(self.tmp)
        self.before = snapshot(self.repo)

    def night(self, body: str, **kw) -> dict:
        self.plan.write_text(f"# Tonight\n\n## app\ndir: {self.repo}\n{body}")
        with redirect_stdout(io.StringIO()):
            return burn.burn(str(self.plan), {**CFG, "data_dir": str(self.data)}, str(self.work), now=True,
                             **{"pr": False, **kw})

    def assert_checkout_untouched(self):
        self.assertEqual(snapshot(self.repo), self.before)

    def tree(self, ref: str) -> set:
        return set(git(self.repo, "ls-tree", "-r", "--name-only", ref).split())


class Isolation(NightCase):
    def test_dirty_checkout_untouched_and_the_work_lands_on_the_night_branch(self):
        res = self.night("test: python3 -c 'import feature'\n\n- [ ] add feature.py\n- [ ] add more.py\n")
        self.assertEqual([r["status"] for r in res["results"]], ["done", "done"])
        self.assert_checkout_untouched()

        branch = res["branches"]["app"]
        self.assertRegex(branch, r"^relay/night-\d{8}$")
        head = self.before["head"]
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{head}..{branch}"), "2")   # one commit per task
        self.assertEqual(git(self.repo, "rev-parse", f"{branch}~2"), head)                  # cut from the user's HEAD
        self.assertEqual(self.tree(branch), {"app.py", "lib.py", "wip.py", "feature.py", "more.py"})
        self.assertEqual(git(self.repo, "show", f"{branch}:app.py"), "print('v1')")         # not the unstaged edit
        self.assertEqual(git(self.repo, "show", f"{branch}:lib.py"), "x = 1")               # nor the staged one
        self.assertEqual(git(self.repo, "log", "--format=%an: %s", f"{head}..{branch}").splitlines(),
                         ["relay night shift: add more.py", "relay night shift: add feature.py"])
        self.assertIn("- [x] add feature.py (relay ", self.plan.read_text())

        wt = self.data / "worktrees" / "app" / branch.split("/")[-1]
        self.assertEqual(len(FakeAgent.runs), 2)
        self.assertNotIn(".env", FakeAgent.runs[0]["files"])      # the user's untracked files never reach the agent
        for run in FakeAgent.runs:                     # the agent only ever ran in the linked worktree
            self.assertEqual(run["cwd"], wt)
            self.assertEqual(run["branch"], branch)
            git_dir, common = (Path(run["cwd"], d).resolve() for d in run["dirs"])
            self.assertNotEqual(git_dir, common)
            self.assertEqual(common, (self.repo / ".git").resolve())
            self.assertIn("wip.py", run["files"])
            self.assertNotIn("notes/scratch.txt", run["files"])
        self.assertFalse(wt.exists())                  # removed after the shift; the branch keeps the work
        self.assertEqual(len(git(self.repo, "worktree", "list", "--porcelain").split("\n\n")), 1)
        self.assertIn(f"branch `{branch}` in {self.repo}", (self.work / "MORNING.md").read_text())

    def test_agent_commits_fold_blocked_tasks_keep_their_wip_and_no_secret_is_committed(self):
        res = self.night("\n- [ ] add one.py and commit it itself\n- [ ] add two.py then get blocked\n- [ ] add three.py\n")
        self.assertEqual([r["status"] for r in res["results"]], ["done", "blocked", "done"])
        self.assert_checkout_untouched()
        branch = res["branches"]["app"]
        self.assertEqual(git(self.repo, "log", "--format=%s", f"{self.before['head']}..{branch}").splitlines(),
                         ["add three.py", "WIP (blocked): add two.py then get blocked", "add one.py and commit it itself"])
        self.assertEqual(self.tree(branch), {"app.py", "lib.py", "wip.py", "one.py", "two.py", "three.py"})
        for sha in git(self.repo, "rev-list", f"{self.before['head']}..{branch}").split():
            self.assertNotIn(".env", git(self.repo, "show", "--name-only", "--format=", sha).split())
        self.assertIn("- [!] add two.py then get blocked (blocked: needs a PyPI token)", self.plan.read_text())

    def test_each_night_gets_a_fresh_branch_from_head_and_the_branch_key_is_honoured(self):
        first = self.night("\n- [ ] add one.py\n")["branches"]["app"]
        second = self.night("\n- [ ] add two.py\n")["branches"]["app"]
        self.assertEqual(second, first + "-2")                                    # nothing reused or overwritten
        self.assertEqual(git(self.repo, "rev-parse", f"{second}~1"), self.before["head"])   # not stacked on night 1
        self.assertEqual(self.tree(first) - self.tree(second), {"one.py"})
        release = git(self.repo, "rev-parse", "release")
        third = self.night("branch: release\n\n- [ ] add three.py\n")["branches"]["app"]
        self.assertEqual(git(self.repo, "rev-parse", f"{third}~1"), release)      # cut from `branch:`, never checked out
        self.assert_checkout_untouched()

    def test_a_dir_inside_a_repo_is_worked_on_in_that_folder_of_the_worktree(self):
        mono = self.tmp / "mono"
        (mono / "pkg").mkdir(parents=True)
        git(mono, "init", "-q", "-b", "main")
        (mono / "pkg" / "lib.py").write_text("x = 1\n")
        git(mono, "add", "-A")
        git(mono, "-c", "user.name=Owner", "-c", "user.email=owner@example.com", "commit", "-qm", "init")
        before = snapshot(mono)
        self.plan.write_text(f"# Tonight\n\n## pkg\ndir: {mono / 'pkg'}\ntest: python3 -c 'import lib, one'\n\n"
                             "- [ ] add one.py\n")
        with redirect_stdout(io.StringIO()):
            res = burn.burn(str(self.plan), {**CFG, "data_dir": str(self.data)}, str(self.work), now=True, pr=False)
        self.assertEqual(res["results"][0]["status"], "done")                   # the test ran in pkg/ too
        branch = res["branches"]["pkg"]
        self.assertEqual(FakeAgent.runs[0]["cwd"], self.data / "worktrees" / "pkg" / branch.split("/")[-1] / "pkg")
        self.assertEqual(set(git(mono, "ls-tree", "-r", "--name-only", branch).split()), {"pkg/lib.py", "pkg/one.py"})
        self.assertEqual(snapshot(mono), before)

    def test_a_folder_that_is_not_a_repo_is_refused_never_initialised(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "keep.txt").write_text("mine\n")
        self.plan.write_text(f"# Tonight\n\n## plain\ndir: {plain}\n\n- [ ] add one.py\n- [ ] add two.py\n\n"
                             f"## app\ndir: {self.repo}\n\n- [ ] add three.py\n")
        with redirect_stdout(io.StringIO()):
            res = burn.burn(str(self.plan), {**CFG, "data_dir": str(self.data)}, str(self.work), now=True, pr=False)
        status = {r["task"]: (r["status"], r["note"]) for r in res["results"]}
        self.assertEqual(status["add one.py"][0], "blocked")
        self.assertIn("not a git repository", status["add one.py"][1])
        self.assertEqual(status["add two.py"][0], "skipped")                     # one refusal per project, not per task
        self.assertEqual(status["add three.py"], ("done", ""))                   # the rest of the shift goes on
        self.assertEqual(sorted(p.name for p in plain.iterdir()), ["keep.txt"])  # no .git, nothing written
        self.assertIn("- [ ] add one.py", self.plan.read_text())                 # queued again once you fix it
        self.assertIn("not a git repository", (self.work / "MORNING.md").read_text())
        self.assert_checkout_untouched()


class Push(NightCase):
    """--pr against a GitHub remote, offline: git sends pushes for https://github.com/ to a local bare repo
    (pushInsteadOf) and the GitHub API is mocked, so what is pushed, with which credentials, and what is written
    to disk can all be checked."""

    def test_only_the_night_branch_is_pushed_and_the_token_never_touches_disk(self):
        remotes = self.tmp / "github"
        bare = remotes / "kai" / "kai.github.io.git"                           # a dot in the name (REVIEW #6)
        bare.mkdir(parents=True)
        git(bare, "init", "-q", "--bare")
        self.gitconfig.write_text(f'[url "{remotes.as_uri()}/"]\n\tpushInsteadOf = https://github.com/\n')
        git(self.repo, "remote", "add", "origin", "https://github.com/kai/kai.github.io.git")
        self.before = snapshot(self.repo)
        api, real_run, runs = [], subprocess.run, []

        def urlopen(req, timeout=None):
            api.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None, timeout))
            out = {"default_branch": "main"} if req.get_method() == "GET" else {"html_url": "https://github.com/pr/1"}
            return io.BytesIO(json.dumps(out).encode())

        def spy(cmd, *a, **kw):
            runs.append((cmd, kw.get("env") or {}))
            return real_run(cmd, *a, **kw)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}), mock.patch.object(burn.urllib.request, "urlopen", urlopen), \
                mock.patch.object(subprocess, "run", spy):
            res = self.night("\n- [ ] add one.py\n", pr=True)
        branch = res["branches"]["app"]
        self.assertEqual(res["prs"], {"app": "https://github.com/pr/1"})
        self.assert_checkout_untouched()                                          # .git/config included

        pushes = [(cmd, env) for cmd, env in runs if isinstance(cmd, list) and "push" in cmd]
        self.assertEqual(len(pushes), 1)
        cmd, env = pushes[0]
        self.assertEqual(cmd[cmd.index("push"):], ["push", "-q", "https://github.com/kai/kai.github.io.git",
                                                   f"refs/heads/{branch}:refs/heads/{branch}"])
        self.assertEqual(cmd[cmd.index("-C") + 1], str(self.data / "worktrees" / "app" / branch.split("/")[-1]))
        self.assertFalse([a for a in cmd if TOKEN in a])                          # not in argv
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]),
                         ("http.https://github.com/.extraheader", f"Authorization: Basic {basic}"))
        self.assertEqual((env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]), ("credential.helper", ""))

        self.assertEqual(git(bare, "for-each-ref", "--format=%(refname) %(objectname)").splitlines(),
                         [f"refs/heads/{branch} {git(self.repo, 'rev-parse', branch)}"])   # nothing else pushed
        for p in self.tmp.rglob("*"):                  # the checkout's .git, the data dir, the remote, the reports
            if p.is_file():
                data = p.read_bytes()
                self.assertFalse(TOKEN.encode() in data or basic.encode() in data, p)

        self.assertEqual([(m, u, t) for m, u, _, t in api],
                         [("GET", "https://api.github.com/repos/kai/kai.github.io", 30),
                          ("POST", "https://api.github.com/repos/kai/kai.github.io/pulls", 30)])
        self.assertEqual({k: api[1][2][k] for k in ("head", "base", "draft")}, {"head": branch, "base": "main", "draft": True})


if __name__ == "__main__":
    unittest.main()
