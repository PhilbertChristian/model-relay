"""A lane turns a task into work inside a git worktree, burning one subscription."""
from __future__ import annotations

import time

from ..contracts import Emit, LaneResult, SwarmTask, redact

TASK_PROMPT = """You are one of several agents spending this week's leftover AI capacity on the user's project "{project}".
Nobody is watching live. Work only in the current directory: a fresh git worktree on its own branch.

Task: {task}
{brief}
Make the change, keep it small and focused, and verify it{test_hint}.
Do not commit, push or touch git remotes, do not install global packages, do not read secrets (.env, keys).
When done, reply with a 1-3 sentence summary. If the task needs a human (credentials, payment, a product
decision), reply starting with BLOCKED: and what you need."""


def task_prompt(task: SwarmTask) -> str:
    p = task.project
    brief = f"\nResearch notes:\n{task.brief.strip()}\n" if task.brief.strip() else ""
    test_hint = f" (`{p.test_cmd}` must pass)" if p.test_cmd else ""
    return redact(TASK_PROMPT.format(project=p.name, task=task.text, brief=brief, test_hint=test_hint))


class Lane:
    kind = "base"

    def __init__(self, name: str | None = None, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.name = name or self.cfg.get("name") or self.kind
        self.subscription = self.cfg.get("subscription", self.name)
        self.flavor = self.cfg.get("as", self.kind)      # display flavor (mock lanes pose as claude/codex/...)
        self.max_agents = int(self.cfg.get("max_agents", 4))
        self.limited_until = 0.0

    def available(self) -> tuple[bool, str]:
        """(ok, why). Cheap: no network calls that cost money."""
        return True, "ok"

    def is_limited(self, now: float | None = None) -> bool:
        return self.limited_until > (now or time.time())

    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        """Do the task inside `workdir` (a git worktree). Don't commit: the swarm commits.
        Call emit("agent_progress", tokens=..., usd=..., note=...) as work happens (tokens cumulative).
        Return status "limited" with limited_until when the subscription reports a usage limit."""
        raise NotImplementedError
