"""Detects when the agent is stuck, so the router can escalate to a stronger model."""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field


def _h(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:12]


REFUSAL_RE = re.compile(
    r"\b(i\s*(?:can(?:no|')t|cannot|won'?t|will not|am not able to|'m not able to|am unable to|'m unable to)\s+"
    r"(?:help|assist|comply|do that|complete|provide|fulfil|perform|carry out|create|write|run)"
    r"|against (?:my|the) (?:guidelines|policies|policy)|not (?:something|a request) i can help with)", re.I)


def looks_like_refusal(text: str) -> bool:
    """A short final reply that declines the task (not a long answer that merely mentions a limitation)."""
    t = (text or "").strip()
    return bool(t) and len(t) < 900 and bool(REFUSAL_RE.search(t[:400]))


@dataclass
class BlockerDetector:
    repeat_limit: int = 3          # same tool call + same result this many times in the window
    error_streak_limit: int = 3    # consecutive tool errors
    malformed_limit: int = 2       # unparseable tool calls / empty replies in a row
    no_progress_limit: int = 10    # tool steps without changing any file
    window: int = 8
    _recent: deque = field(default_factory=lambda: deque(maxlen=8))
    _error_streak: int = 0
    _malformed: int = 0
    _since_progress: int = 0

    def reset(self) -> None:
        """After a model switch, give the new model a clean slate."""
        self._recent.clear()
        self._error_streak = 0
        self._malformed = 0
        self._since_progress = 0

    def on_tool_result(self, name: str, args: str, output: str, is_error: bool,
                       hung: bool = False, progressed: bool = False) -> str | None:
        if hung:
            return f"hung process: `{name}` never exited and was killed"
        self._since_progress = 0 if progressed else self._since_progress + 1
        try:
            norm_args = json.dumps(json.loads(args or "{}"), sort_keys=True)
        except json.JSONDecodeError:
            norm_args = args
        key = _h(name, norm_args, output[-500:])
        self._recent.append(key)
        self._malformed = 0
        self._error_streak = self._error_streak + 1 if is_error else 0
        if list(self._recent).count(key) >= self.repeat_limit:
            return f"loop: `{name}` repeated {self.repeat_limit}x with identical result"
        if self._error_streak >= self.error_streak_limit:
            return f"{self._error_streak} tool errors in a row (last: {name})"
        if self._since_progress >= self.no_progress_limit:
            self._since_progress = 0
            return f"wrong approach: {self.no_progress_limit} steps without changing any file"
        return None

    def on_malformed(self, what: str) -> str | None:
        self._malformed += 1
        if self._malformed >= self.malformed_limit:
            return f"{self._malformed} malformed responses in a row ({what})"
        return None
