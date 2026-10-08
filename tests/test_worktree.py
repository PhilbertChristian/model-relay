"""python3 -m unittest tests.test_worktree -v   (throwaway git repos in temp dirs; offline)"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import worktree

SECRETS = [".env", ".env.local", "config/credentials.json", "keys/id_ed25519", "deploy/server.pem", "nested/dir/.npmrc"]


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check).stdout.strip()


def make_repo(root: Path, name: str = "app", identity: bool = True) -> Path:
    """A repo with one commit and an untracked .env, like a real checkout."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    if identity:
        git(repo, "config", "user.name", "Repo Owner")
        git(repo, "config", "user.email", "owner@example.com")
    (repo / "README.md").write_text(f"# {name}\n")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Setup", "-c", "user.email=setup@example.com", "commit", "-qm", "init")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-proj-0123456789abcdefghijkl\n")
    return repo


def checkout_state(repo: Path) -> tuple:
    """Everything burn-week must never change in the user's checkout."""
    return (git(repo, "rev-parse", "HEAD"), git(repo, "symbolic-ref", "HEAD"),
            git(repo, "status", "--porcelain", "--untracked-files=all"), git(repo, "stash", "list"), git(repo, "remote"))


class GitSandbox(unittest.TestCase):
    """Fresh temp dirs, and a git config that ignores the developer's ~/.gitconfig (signing, hooks, identity)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-wt-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "gitconfig").write_text("")
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.data = self.tmp / "data"


class Create(GitSandbox):
    def test_new_branch_from_head_in_data_dir_checkout_untouched(self):
        repo = make_repo(self.tmp)
        before = checkout_state(repo)
        path, branch = worktree.create(str(repo), str(self.data), "app-abc123", "app")
        self.assertEqual(branch, "relay/burn/app-abc123")
        self.assertEqual(Path(path), self.data / "worktrees" / "app" / "app-abc123")
        self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), branch)
        self.assertEqual(git(path, "rev-parse", "HEAD"), before[0])
        self.assertTrue((Path(path) / "app.py").exists())
        self.assertFalse((Path(path) / ".env").exists())            # untracked secrets stay in the user's checkout
        self.assertEqual(checkout_state(repo), before)

    def test_existing_branch_or_path_gets_a_suffix_nothing_deleted(self):
        repo = make_repo(self.tmp)
        p1, _ = worktree.create(str(repo), str(self.data), "app-abc123", "app")
        p2, b2 = worktree.create(str(repo), str(self.data), "app-abc123", "app")
        stray = self.data / "worktrees" / "app" / "app-abc123-3"
        stray.mkdir()
        (stray / "keep.txt").write_text("mine\n")
        p3, b3 = worktree.create(str(repo), str(self.data), "app-abc123", "app")
        self.assertEqual((b2, b3), ("relay/burn/app-abc123-2", "relay/burn/app-abc123-4"))
        self.assertTrue(Path(p1).is_dir() and Path(p2).is_dir() and Path(p3).is_dir())
        self.assertEqual((stray / "keep.txt").read_text(), "mine\n")

    def test_refuses_worktrees_nested_in_the_repo(self):
        repo = make_repo(self.tmp)
        with self.assertRaises(ValueError):
            worktree.create(str(repo), str(repo / ".burn"), "app-1", "app")

    def test_git_guard_refuses_remote_and_checkout_moving_commands(self):
        repo = make_repo(self.tmp)
        for args in (("push", "origin", "main"), ("remote", "add", "x", "y"), ("-c", "a.b=c", "fetch"),
                     ("checkout", "-b", "x"), ("switch", "x"), ("stash",), ("clean", "-fd"), ("commit", "-m", "x"),
                     ("reset", "--hard")):
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                worktree._git(repo, *args)


class Finalize(GitSandbox):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.before = checkout_state(self.repo)
        self.path, self.branch = worktree.create(str(self.repo), str(self.data), "app-abc123", "app")
        self.wt = Path(self.path)

    def tearDown(self):
        self.assertEqual(checkout_state(self.repo), self.before)    # the user's checkout never moves

    def write(self, rel: str, text: str) -> None:
        (self.wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.wt / rel).write_text(text)

    def test_commits_the_change_but_never_secret_files(self):
        self.write("feature.py", "def f():\n    return 1\n")
        self.write("README.md", "# app\nmore\n")
        (self.wt / "app.py").unlink()
        for s in SECRETS:
            self.write(s, "API_KEY=supersecretvalue123\n")
        git(self.wt, "add", "-f", ".env")                              # an agent staged one anyway
        res = worktree.finalize(self.path, "add feature")
        self.assertTrue(res["commit"])
        self.assertEqual(set(git(self.wt, "show", "--name-only", "--format=", "HEAD").split()),
                         {"feature.py", "README.md", "app.py"})
        self.assertFalse(set(git(self.repo, "ls-tree", "-r", "--name-only", self.branch).split()) & set(SECRETS))
        self.assertEqual((res["files"], res["insertions"], res["deletions"]), (3, 3, 1))
        self.assertIn("3 files changed", res["diffstat"])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"main..{self.branch}"), "1")
        self.assertEqual(git(self.wt, "log", "-1", "--format=%an %s"), "Repo Owner add feature")
        self.assertTrue((self.wt / ".env").exists())                  # left alone, just never committed

    def test_no_changes_or_only_secrets_means_no_commit(self):
        res = worktree.finalize(self.path, "nothing")
        self.assertEqual((res["commit"], res["files"], res["diffstat"], res["tests_ok"]), (None, 0, "", None))
        self.write(".env", "TOKEN=abcdefghijkl\n")
        self.assertIsNone(worktree.finalize(self.path, "secrets only")["commit"])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"main..{self.branch}"), "0")

    def test_test_results_and_redacted_tail(self):
        self.write("a.txt", "a\n")
        ok = worktree.finalize(self.path, "a", "python3 -c \"print('token sk-ant-api03-abcdefghijklmnopqrstuv')\"")
        self.assertTrue(ok["tests_ok"])
        self.assertIn("[redacted]", ok["tests_tail"])
        self.assertNotIn("sk-ant", ok["tests_tail"])
        self.assertIn("Relay-Tests: pass", git(self.wt, "log", "-1", "--format=%B"))
        self.write("b.txt", "b\n")
        bad = worktree.finalize(self.path, "b", "echo nope; exit 3")
        self.assertFalse(bad["tests_ok"])
        self.assertIn("nope", bad["tests_tail"])
        self.assertIn("exit 3", bad["tests_tail"])
        self.assertTrue(bad["commit"])                                 # failing work is still kept for review

    def test_timeout_kills_the_whole_process_group(self):
        cmd = ("python3 -c \"import subprocess,time; p=subprocess.Popen(['sleep','30']); "
               "open('child.pid','w').write(str(p.pid)); time.sleep(30)\"")
        t = time.time()
        res = worktree.finalize(self.path, "slow", cmd, timeout=1)
        self.assertLess(time.time() - t, 10)
        self.assertFalse(res["tests_ok"])
        self.assertIn("timed out", res["tests_tail"])
        self.assertIsNone(res["commit"])                               # test leftovers are never staged
        pid = int((self.wt / "child.pid").read_text())
        for _ in range(60):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)
        else:
            self.fail("the test command's grandchild survived the timeout")

    def test_agent_commits_fold_into_one_commit_without_secrets(self):
        self.write("one.py", "1\n")
        self.write(".env", "SECRET=abcdefghijkl\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "agent commit 1")
        self.write("two.py", "2\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "agent commit 2")
        res = worktree.finalize(self.path, "relay: the task")
        self.assertEqual(git(self.repo, "rev-list", "--count", f"main..{self.branch}"), "1")
        self.assertEqual(set(git(self.wt, "show", "--name-only", "--format=", "HEAD").split()), {"one.py", "two.py"})
        self.assertEqual(git(self.wt, "log", "-1", "--format=%s"), "relay: the task")
        self.assertEqual(res["files"], 2)

    def test_fallback_identity_only_when_the_repo_has_none(self):
        repo = make_repo(self.tmp, "anon", identity=False)
        path, _ = worktree.create(str(repo), str(self.data), "anon-1", "anon")
        (Path(path) / "x.txt").write_text("x\n")
        self.assertTrue(worktree.finalize(path, "x")["commit"])
        self.assertEqual(git(path, "log", "-1", "--format=%an <%ae>"), "relay burn <relay-burn@localhost>")
        self.assertEqual(git(repo, "config", "--get", "user.name", check=False), "")    # passed with -c only

    def test_user_hooks_never_run(self):
        marker = self.tmp / "hook-ran"
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "commit-msg", "post-commit", "post-checkout", "reference-transaction"):
            (hooks / name).write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
            (hooks / name).chmod(0o755)
        path, _ = worktree.create(str(self.repo), str(self.data), "app-hooks", "app")
        (Path(path) / "x.txt").write_text("x\n")
        self.assertTrue(worktree.finalize(path, "x")["commit"])
        self.assertFalse(marker.exists())                              # a post-commit hook could push

    def test_refuses_anything_but_a_relay_burn_worktree(self):
        with self.assertRaises(RuntimeError):
            worktree.finalize(str(self.repo), "never in the user's checkout")
        mine = self.tmp / "mine"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(mine), "HEAD")
        (mine / "x.txt").write_text("x\n")
        with self.assertRaises(RuntimeError):
            worktree.finalize(str(mine), "nor in the user's own worktrees")
        with self.assertRaises(RuntimeError):
            worktree.remove(str(self.repo), str(mine))
        self.assertTrue((mine / "x.txt").exists())


class RemoveAndBranches(GitSandbox):
    def test_remove_keeps_the_branch_and_branches_lists_only_burn(self):
        repo = make_repo(self.tmp)
        git(repo, "branch", "feature")
        path, branch = worktree.create(str(repo), str(self.data), "app-abc123", "app")
        (Path(path) / "x.txt").write_text("x\n")
        (Path(path) / ".env").write_text("TOKEN=abcdefghijkl\n")
        sha = worktree.finalize(path, "add x")["commit"]
        worktree.remove(str(repo), path)
        self.assertFalse(Path(path).exists())
        self.assertNotIn(path, git(repo, "worktree", "list"))
        self.assertEqual(worktree.branches(str(repo)), [{"branch": branch, "sha": sha, "subject": "add x"}])
        worktree.remove(str(repo), path)                               # already gone: a no-op
        with self.assertRaises(RuntimeError):
            worktree.remove(str(repo), str(repo))
        self.assertTrue((repo / "app.py").exists())


if __name__ == "__main__":
    unittest.main()
