"""Shared contracts for burn-week: the swarm that spends this week's leftover subscription capacity.

Every burn-week module builds to these types. They belong to the integrator: import them, don't edit them.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

# Never read into a prompt, a log line, a report, or a remote request.
SECRET_FILE_RE = re.compile(
    r"(^|/)(\.env(\.[\w.-]+)?|\.envrc|\.netrc|\.npmrc|\.pypirc|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|[^/]*\.(pem|key|p12|pfx|keystore|jks)|[^/]*(credentials?|secrets?)[^/]*)$", re.I)

_SECRET_VALUE_RE = re.compile(
    r"(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}"
    r"|ctxt_secret_[A-Za-z0-9]{8,}|monid_(?:live|test)_[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._~+/=\-]{8,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    r"|(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{8,})",
    re.I)


def redact(text: str) -> str:
    """Mask API-key/token-looking strings."""
    return _SECRET_VALUE_RE.sub("[redacted]", text or "")


def is_secret_file(path: str) -> bool:
    return bool(SECRET_FILE_RE.search(str(path).replace("\\", "/")))


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "project"


@dataclass
class ProjectInfo:
    path: str                                        # absolute repo root: the user's checkout, read-only to burn-week
    name: str
    last_commit_ts: float = 0.0
    dirty: bool = False
    branch: str = ""
    languages: list[str] = field(default_factory=list)
    test_cmd: str | None = None
    remote: str | None = None
    todos: list[str] = field(default_factory=list)   # harvested task candidates: imperative, <= 160 chars, redacted
    score: float = 0.0

    @property
    def slug(self) -> str:
        return slugify(self.name)


@dataclass
class Idea:
    text: str                                        # one actionable sentence, redacted, <= 200 chars
    project_dir: str | None = None                   # cwd the conversation happened in, when known
    source: str = "claude-code"                      # "claude-code" | "codex" | "chatgpt"
    ts: float = 0.0
    score: float = 0.0


@dataclass
class SwarmTask:
    id: str                                          # f"{project.slug}-{sha1(text)[:6]}"
    project: ProjectInfo
    text: str
    source: str = "todo"                             # "plan" | "todo" | "idea" | "generic"
    priority: float = 0.0                            # higher runs first
    brief: str = ""                                  # optional research brief (Monid) added to the prompt
    attempts: int = 0


@dataclass
class LaneResult:
    status: str                                      # "done" | "blocked" | "failed" | "limited"
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    model: str = ""
    limited_until: float | None = None               # epoch seconds, when status == "limited"

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Usage:
    name: str                                        # subscription name from config, e.g. "claude-max"
    lane: str                                        # lane kind that burns it: claude|codex|relay|agent37|monid|mock
    unit: str                                        # "tokens" | "usd" | "credits"
    used: float
    limit: float | None
    resets_at: float                                 # epoch seconds
    source: str = "config"                           # "live" | "transcripts" | "ledger" | "config" | "demo"
    note: str = ""

    @property
    def remaining(self) -> float | None:
        return None if self.limit is None else max(0.0, self.limit - self.used)

    @property
    def pct(self) -> float | None:
        return None if not self.limit else min(1.0, self.used / self.limit)

    @property
    def hours_left(self) -> float:
        return max(0.0, (self.resets_at - time.time()) / 3600)


@dataclass
class PacePlan:
    lane: str
    subscription: str
    unit: str
    remaining: float
    hours_left: float
    target_rate: float                               # units/hour needed to land at target_pct by reset
    agents: int                                      # recommended concurrent agents on this lane


Emit = Callable[..., None]                           # emit(event: str, **data)


def to_dict(obj) -> dict:
    d = asdict(obj)
    for k in ("remaining", "pct", "hours_left", "tokens", "slug"):
        if hasattr(type(obj), k):
            d[k] = getattr(obj, k)
    return d
