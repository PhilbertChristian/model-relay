"""Planning docs -> task queue, and back.

A plan is plain markdown. Each `## ` heading is a project; `key: value` lines under it configure
the project; `- [ ]` items are tasks, done top to bottom. Relay writes results back in place:

    ## todo-cli
    repo: https://github.com/you/todo-cli     # or  dir: ~/code/todo-cli   (neither: a fresh local repo)
    test: python3 -m pytest -q                 # must pass for a task to count as done
    budget: 1.50                               # max USD this project may burn per night
    priority: 1                                # lower runs first
    notes: Python 3.11, click, no other deps
    deploy: instacloud                         # optional: preview-deploy the night branch on InstaCloud

    - [ ] add `add`, `list`, `done` commands
    - [x] scaffold the package                 <- done
    - [!] publish to PyPI (blocked: needs a token)  <- relay could not finish it
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TASK_RE = re.compile(r"^(\s*)- \[( |x|X|!)\] (.+)$")
KEY_RE = re.compile(r"^(repo|dir|test|budget|priority|notes|branch|base|deploy):\s*(.+?)\s*$", re.I)


@dataclass
class Task:
    text: str
    state: str          # " " todo, "x" done, "!" blocked
    line: int           # 0-based line in the plan file

    @property
    def todo(self) -> bool:
        return self.state == " "


@dataclass
class Project:
    name: str
    line: int
    repo: str | None = None
    dir: str | None = None
    test: str | None = None
    budget: float | None = None
    priority: int = 100
    notes: str = ""
    base: str | None = None
    deploy: str | None = None          # "instacloud": preview-deploy the night branch after its tasks pass
    tasks: list[Task] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "project"

    @property
    def todo(self) -> list[Task]:
        return [t for t in self.tasks if t.todo]


@dataclass
class Plan:
    path: Path
    title: str
    projects: list[Project]

    @property
    def queue(self) -> list[tuple[Project, Task]]:
        return [(p, t) for p in sorted(self.projects, key=lambda p: (p.priority, p.line)) for t in p.todo]

    def mark(self, task: Task, state: str, note: str = "") -> None:
        """Rewrite one task's checkbox in the plan file (re-reads the file so human edits survive)."""
        lines = self.path.read_text().splitlines()
        m = TASK_RE.match(lines[task.line])
        if not m or m.group(3).split(" (")[0].strip() != task.text.split(" (")[0].strip():
            return  # the human edited the plan underneath us; leave it alone
        suffix = f" ({note})" if note else ""
        lines[task.line] = f"{m.group(1)}- [{state}] {task.text}{suffix}"
        self.path.write_text("\n".join(lines) + "\n")
        task.state = state


def parse(path: str | Path) -> Plan:
    path = Path(path).expanduser()
    lines = path.read_text().splitlines()
    title, projects, cur = path.stem, [], None
    for i, line in enumerate(lines):
        if line.startswith("# ") and not projects:
            title = line[2:].strip()
        elif line.startswith("## "):
            cur = Project(name=line[3:].strip(), line=i)
            projects.append(cur)
        elif cur is None:
            continue
        elif m := KEY_RE.match(line.strip()):
            k, v = m.group(1).lower(), m.group(2)
            if k == "budget":
                cur.budget = float(v.lstrip("$"))
            elif k == "priority":
                cur.priority = int(v)
            elif k == "branch":
                cur.base = v
            else:
                setattr(cur, k, v)
        elif m := TASK_RE.match(line):
            cur.tasks.append(Task(text=m.group(3).strip(), state=m.group(2).lower(), line=i))
    return Plan(path=path, title=title, projects=projects)
