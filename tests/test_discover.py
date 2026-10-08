"""python3 -m unittest tests.test_discover -v

Offline: the fixture tree in tests/fixtures/discover plus throwaway git repos in temp dirs, never a real repo."""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import discover, plan
from relay.contracts import Idea, ProjectInfo, is_secret_file

ACME = Path(__file__).resolve().parent / "fixtures" / "discover" / "acme"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HAS_GIT = shutil.which("git") is not None
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"     # built at runtime: no token-shaped literal in the repo
UNIT = "python3 -m unittest discover -s tests"
TEST_APP = "import unittest\n\nfrom src import app\n\n\nclass AppTest(unittest.TestCase):\n    def test_fetch(self):\n" \
           "        self.assertEqual(app.fetch('x'), 'x')\n"
ACME_TODOS = [
    "Add a --json flag to the report command (TODO.md)",
    "Windows support (TODO.md)",
    "Add retries to fetch() for 502 and 503 responses (src/app.py:5)",
    "Split this function once the pricing rules settle (src/app.py:10)",
    "Fix src/app.py:17: crashes when the cart is empty",
    "Debounce the search box (src/util.js:1)",
    "Handle empty input (src/util.js:3)",
    "Resolve the XXX in src/legacy.py:2: temporary workaround for the v1 pricing API",
]


def git(cwd, *args, date=None):
    """git for building test repos only: no user config, hooks or signing, never the caller's repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@localhost",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@localhost")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def tree(path, files=None, copy=None) -> Path:
    path = Path(path)
    if copy:
        shutil.copytree(copy, path)
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    return path


def repo(path, files=None, copy=None, date=None) -> Path:
    """A throwaway git repo with one (optionally backdated) commit."""
    path = tree(path, files, copy)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "--allow-empty", "-m", "init", date=date)
    return path


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class HarvestTodos(TempDir):
    def test_fixture_tasks_best_first(self):
        # unchecked plan items first; never [x]/[!] items, fenced examples, string literals or fixture dirs
        self.assertEqual(discover.harvest_todos(str(ACME)), ACME_TODOS)

    def test_limit(self):
        self.assertEqual(discover.harvest_todos(str(ACME), limit=3), ACME_TODOS[:3])

    def test_never_opens_secrets_binaries_vendored_or_big_files(self):
        root = tree(self.tmp / "acme", {
            ".env": "# TODO: leak the env file\n", "config/secrets.yml": "# TODO: leak the secrets file\n",
            "deploy/id_rsa": "# TODO: leak the ssh key\n", "certs/server.pem": "# TODO: leak the cert\n",
            "node_modules/pkg/index.js": "// TODO: leak a dependency\n", ".cache/x.py": "# TODO: leak a hidden dir\n",
            "build/out.py": "# TODO: leak a build dir\n", "vendor/lib.py": "# TODO: leak vendored code\n",
            "src/big.py": "# TODO: leak a big file\n" + "x = 1\n" * 400,
            "src/dup.py": "# TODO: add retries to fetch() for 502 and 503 responses\n"}, copy=ACME)
        (root / "logo.png").write_bytes(b"\x89PNG\r\n# TODO: leak a binary\n")
        (root / "src" / "blob.py").write_bytes(b"\x00\x01# TODO: leak a binary file\n")
        opened, real = [], discover._read_text
        with mock.patch.object(discover, "_read_text", side_effect=lambda p, *a: (opened.append(p), real(p, *a))[1]), \
                mock.patch.object(discover, "MAX_FILE_BYTES", 1000):
            todos = discover.harvest_todos(str(root), limit=50)
        self.assertFalse([t for t in todos if "leak" in t])
        self.assertFalse([p for p in opened if is_secret_file(p)])
        self.assertFalse([p for p in opened if re.search(r"node_modules|\.cache|/build/|/vendor/|logo\.png", p)])
        self.assertEqual(sum(t.startswith("Add retries to fetch()") for t in todos), 1)     # deduped across files

    def test_redacts_and_caps_length(self):
        tree(self.tmp, {"app.py": f"# TODO: remove the hardcoded token {TOKEN} from the client\n"
                                  f"# TODO: {'refactor the pricing pipeline ' * 10}\n"})   # ~300 chars
        todos = discover.harvest_todos(str(self.tmp))
        self.assertEqual(len(todos), 2)
        self.assertTrue(all(len(t) <= 160 for t in todos))
        self.assertNotIn(TOKEN, " ".join(todos))
        self.assertIn("[redacted]", todos[0])
        self.assertTrue(todos[1].endswith("… (app.py:2)"))

    def test_test_command_detection(self):
        cases = [
            ({"package.json": '{"scripts": {"test": "vitest run"}}', "src/index.ts": "export const x = 1\n"}, "npm test"),
            ({"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": "", "a.ts": "x\n"}, "pnpm test"),
            ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}', "a.js": "x\n"}, None),
            ({"pytest.ini": "[pytest]\n", "tests/test_x.py": "def test_x(): pass\n", "x.py": "x = 1\n"},
             "python3 -m pytest -q"),
            ({"tests/__init__.py": "", "tests/test_x.py": "import unittest\n", "x.py": "x = 1\n"},
             "python3 -m unittest discover"),
            ({"tests/test_x.py": "import unittest\n", "x.py": "x = 1\n"}, UNIT),
            ({"Cargo.toml": '[package]\nname = "x"\n', "src/main.rs": "fn main() {}\n"}, "cargo test"),
            ({"go.mod": "module x\n", "main.go": "package main\n"}, "go test ./..."),
            ({"Makefile": "test:\n\techo ok\n", "x.c": "int main(void) { return 0; }\n"}, "make test"),
            ({"README.md": "hello\n"}, None),
        ]
        for i, (files, want) in enumerate(cases):
            with self.subTest(want=want, case=i):
                self.assertEqual(discover.inspect_project(str(tree(self.tmp / f"p{i}", files))).test_cmd, want)

    def test_clean_remote_url(self):
        self.assertEqual(discover._clean_url(f"https://kai:{TOKEN}@github.com/kai/x.git?token=abc"),
                         "https://github.com/kai/x.git")
        self.assertEqual(discover._clean_url(f"https://{TOKEN}@github.com/kai/x.git"), "https://github.com/kai/x.git")
        self.assertEqual(discover._clean_url("ssh://git@example.com:2222/kai/x.git"), "ssh://git@example.com:2222/kai/x.git")
        self.assertEqual(discover._clean_url("git@github.com:kai/x.git"), "git@github.com:kai/x.git")


@unittest.skipUnless(HAS_GIT, "needs git")
class InspectProject(TempDir):
    def setUp(self):
        super().setUp()
        self.repo = repo(self.tmp / "acme", {"tests/test_app.py": TEST_APP}, copy=ACME)
        with open(self.repo / ".git" / "config", "a") as f:     # a remote with credentials, written without `git remote`
            f.write(f'[remote "origin"]\n\turl = https://kai:{TOKEN}@github.com/kai/acme.git\n')

    def test_git_facts_languages_tests_todos_and_score(self):
        p = discover.inspect_project(str(self.repo))
        self.assertEqual((p.path, p.name, p.branch, p.dirty), (os.path.realpath(self.repo), "acme", "main", False))
        self.assertAlmostEqual(p.last_commit_ts, time.time(), delta=600)
        self.assertEqual(p.remote, "https://github.com/kai/acme.git")
        self.assertEqual(p.languages, ["python", "javascript"])
        self.assertEqual(p.test_cmd, UNIT)
        self.assertEqual(p.todos, ACME_TODOS)
        self.assertEqual(p.score, 100 + 3 * len(ACME_TODOS) + 15)
        (self.repo / "scratch.txt").write_text("work in progress\n")
        self.assertTrue(discover.inspect_project(str(self.repo)).dirty)

    def test_runs_read_only_git_only(self):
        calls, real = [], subprocess.run

        def spy(cmd, *a, **k):
            calls.append(cmd)
            return real(cmd, *a, **k)
        with mock.patch.object(discover.subprocess, "run", side_effect=spy):
            discover.inspect_project(str(self.repo))
        self.assertTrue(calls and all(c[:2] == ["git", "-C"] for c in calls))
        self.assertLessEqual({c[c.index("core.fsmonitor=false") + 1] for c in calls}, {"log", "status", "branch", "config"})
        for args in (("checkout", "-b", "x"), ("branch", "x"), ("config", "user.name", "x"), ("stash",), ("push",)):
            with self.assertRaises(ValueError):
                discover._git(str(self.repo), *args)


@unittest.skipUnless(HAS_GIT, "needs git")
class DiscoverProjects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        root = cls.root = cls.tmp / "code"
        repo(root / "alpha", {"tests/test_app.py": TEST_APP}, copy=ACME)
        repo(root / "beta-old", {"main.py": "x = 1\n"}, date="2020-01-01T00:00:00Z")
        repo(root / "group" / "gamma", {"Cargo.toml": '[package]\nname = "gamma"\n', "src/lib.rs": "pub fn f() {}\n"})
        repo(root / "a" / "b" / "c" / "deep", {"x.py": "x = 1\n"})
        for skipped in ("node_modules/pkg", ".hidden/repo", "vendor/lib", "build/thing", "burn-data/t1", "skipme/repo"):
            repo(root / skipped, {"x.py": "x = 1\n"})
        tree(root / "linked", {".git": "gitdir: /nonexistent/main/.git/worktrees/linked\n", "x.py": "x = 1\n"})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def names(self, **kw):
        kw.setdefault("data_dir", str(self.root / "burn-data"))
        return [p.name for p in discover.discover_projects([str(self.root), "/nonexistent/relay-root"], **kw)]

    def test_finds_repos_best_first_and_skips_the_rest(self):
        ps = discover.discover_projects([str(self.root), "/nonexistent/relay-root"], skip=["skipme"],
                                        data_dir=str(self.root / "burn-data"))
        self.assertEqual([p.name for p in ps], ["alpha", "gamma", "beta-old"])
        self.assertEqual([p.score for p in ps], sorted((p.score for p in ps), reverse=True))
        self.assertEqual(ps[1].test_cmd, "cargo test")

    def test_skip_and_data_dir(self):
        self.assertIn("repo", self.names())                                   # skipme/repo is only pruned on request
        self.assertNotIn("repo", self.names(skip=[str(self.root / "skipme")]))
        self.assertIn("t1", self.names(data_dir="~/.relay-burn"))            # not the data dir: an ordinary repo
        self.assertNotIn("t1", self.names())

    def test_depth_limit_include_exclude(self):
        self.assertNotIn("deep", self.names())
        self.assertIn("deep", self.names(max_depth=4))
        self.assertEqual(self.names(limit=1), ["alpha"])
        self.assertEqual(self.names(include=["gamma"]), ["gamma"])
        self.assertEqual(self.names(include=["*-old", "ALPHA"]), ["alpha", "beta-old"])
        self.assertNotIn("beta-old", self.names(exclude=["beta*"]))
        self.assertNotIn("gamma", self.names(exclude=[str(self.root / "group")]))

    def test_a_root_that_is_a_repo(self):
        self.assertEqual([p.name for p in discover.discover_projects([str(self.root / "alpha")])], ["alpha"])


class GenericTasks(TempDir):
    def test_tests_for_untested_modules_first(self):
        p = discover.inspect_project(str(tree(self.tmp / "acme", {"tests/test_app.py": TEST_APP}, copy=ACME)))
        tasks = discover.generic_tasks(p)
        self.assertEqual(tasks[0], f"Add unit tests for src/billing.py covering its main paths and edge cases; "
                                   f"keep `{UNIT}` passing")
        self.assertIn(f"Run `{UNIT}` and fix any failing or flaky tests without weakening assertions", tasks)
        self.assertIn("Add a .gitignore for python build output, caches and local env files", tasks)
        self.assertFalse([t for t in tasks if "README" in t])                # acme has one
        self.assertTrue(all(len(t) <= 160 for t in tasks) and len(set(tasks)) == len(tasks))

    def test_repo_without_tests_or_readme(self):
        p = discover.inspect_project(str(tree(self.tmp / "tiny", {"main.py": "print('hi')\n"})))
        tasks = discover.generic_tasks(p)
        self.assertEqual(tasks[0], discover.SETUP_TESTS["python"])
        self.assertTrue([t for t in tasks if t.startswith("Write a README.md for tiny")])

    def test_go_gets_go_vet(self):
        p = discover.inspect_project(str(tree(self.tmp / "svc", {"go.mod": "module svc\n", "main.go": "package main\n"})))
        self.assertIn("Fix the warnings reported by `go vet ./...` without changing behavior", discover.generic_tasks(p))


class PlanAndReport(TempDir):
    def projects(self):
        shop = ProjectInfo(path="/nonexistent/code/shop", name="shop", last_commit_ts=time.time() - 7200, dirty=True,
                           branch="main", languages=["python"], test_cmd="python3 -m pytest -q", score=150,
                           todos=["Add retries (app.py:3)", "Fix the cart (cart.py:9)", "Add a --json flag (TODO.md)"])
        blog = ProjectInfo(path="/nonexistent/code/blog", name="blog", languages=["go"], score=40)
        return [shop, blog]

    def test_plan_round_trips_through_relay_plan(self):
        ideas = {"/nonexistent/code/shop": [Idea("Add a dark mode toggle", "/nonexistent/code/shop", score=70)],
                 "/nonexistent/code/shop/web": [Idea("Cache the tax lookup", "/nonexistent/code/shop/web", score=60)],
                 "": [Idea("Write a post about relay", None, "chatgpt", score=50)]}
        md = discover.to_plan_md(self.projects(), ideas, per_project=3, title="Burn week")
        (self.tmp / "PLAN.md").write_text(md)
        parsed = plan.parse(self.tmp / "PLAN.md")
        self.assertEqual(parsed.title, "Burn week")
        shop, blog = parsed.projects
        self.assertEqual((shop.name, shop.dir, shop.test, shop.priority), ("shop", "/nonexistent/code/shop",
                                                                            "python3 -m pytest -q", 1))
        self.assertEqual([t.text for t in shop.tasks], ["Add retries (app.py:3)", "Add a dark mode toggle",
                                                        "Fix the cart (cart.py:9)"])   # TODOs and ideas alternate
        self.assertIn("uncommitted changes", shop.notes)
        self.assertEqual((blog.name, blog.dir, blog.test, blog.priority), ("blog", "/nonexistent/code/blog", None, 2))
        self.assertEqual([t.text for t in blog.tasks][0], discover.SETUP_TESTS["go"])     # generic tasks fill in
        self.assertEqual(len(parsed.queue), 6)
        self.assertTrue(all(t.todo for p in parsed.projects for t in p.tasks))
        self.assertIn("> - Write a post about relay", md)                               # quoted, not queued
        self.assertNotIn("Write a post about relay", [t.text for p in parsed.projects for t in p.tasks])

    def test_same_name_repos_get_their_parent(self):
        a = ProjectInfo(path="/nonexistent/work/app", name="app")
        b = ProjectInfo(path="/nonexistent/play/app", name="app")
        (self.tmp / "PLAN.md").write_text(discover.to_plan_md([a, b], per_project=1))
        self.assertEqual([p.name for p in plan.parse(self.tmp / "PLAN.md").projects], ["app (work)", "app (play)"])

    def test_report(self):
        out = ANSI.sub("", discover.report(self.projects()))
        for want in ("shop", "blog", "main*", "python3 -m pytest -q", "2h ago", "2 repos · 3 TODOs harvested"):
            self.assertIn(want, out)
        self.assertIn("no git repos found", ANSI.sub("", discover.report([])))


if __name__ == "__main__":
    unittest.main()
