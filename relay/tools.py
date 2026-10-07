"""The agent's toolbox: files, shell, and a self-report `escalate` tool."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Callable

MAX_OUT = 12_000


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _schema("read_file", "Read a text file.", {"path": {"type": "string"}}, ["path"]),
    _schema("write_file", "Create or overwrite a file.",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _schema("edit_file", "Replace one exact occurrence of old_string with new_string in a file.",
            {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}},
            ["path", "old_string", "new_string"]),
    _schema("list_dir", "List a directory.", {"path": {"type": "string"}}, []),
    _schema("bash", "Run a shell command in the workspace. Returns exit code, stdout, stderr.",
            {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 60"}},
            ["command"]),
    _schema("escalate", "Call this when you are stuck or the task is beyond you. A stronger model takes over.",
            {"reason": {"type": "string"}}, ["reason"]),
]


class ToolError(Exception):
    pass


class Escalate(Exception):
    pass


class Toolbox:
    def __init__(self, root: str, confirm: Callable[[str], bool] | None = None):
        self.root = Path(root).resolve()
        self.confirm = confirm
        self.files_changed: set[str] = set()
        self.hung = False          # set when the last bash call had to be killed
        self.max_timeout = 300

    def _path(self, p: str) -> Path:
        path = (self.root / os.path.expanduser(p)).resolve()
        return path

    def run(self, name: str, raw_args: str) -> tuple[str, bool]:
        """Returns (output, is_error). Raises Escalate for the escalate tool."""
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"error: invalid JSON arguments: {e}", True
        self.hung = False
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"error: unknown tool {name}", True
        try:
            out = fn(**args)
            return out[:MAX_OUT] + ("\n...[truncated]" if len(out) > MAX_OUT else ""), False
        except Escalate:
            raise
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}", True
        except Exception as e:  # tool failures are data for the model, not crashes
            return f"error: {e}", True

    def t_read_file(self, path: str) -> str:
        return self._path(path).read_text(errors="replace")

    def t_write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.files_changed.add(str(p))
        return f"wrote {len(content)} bytes to {path}"

    def t_edit_file(self, path: str, old_string: str, new_string: str) -> str:
        p = self._path(path)
        text = p.read_text()
        n = text.count(old_string)
        if n != 1:
            raise ToolError(f"old_string found {n} times in {path}; must be exactly once")
        p.write_text(text.replace(old_string, new_string))
        self.files_changed.add(str(p))
        return f"edited {path}"

    def t_list_dir(self, path: str = ".") -> str:
        p = self._path(path)
        return "\n".join(sorted((e.name + "/" if e.is_dir() else e.name) for e in p.iterdir()))

    def t_bash(self, command: str, timeout: int = 60) -> str:
        if self.confirm and not self.confirm(command):
            raise ToolError("user declined to run this command")
        # own process group, so a hung command and everything it spawned can be killed together
        p = subprocess.Popen(command, shell=True, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        try:
            stdout, stderr = p.communicate(timeout=min(int(timeout), self.max_timeout))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = p.communicate()
            self.hung = True
            raise ToolError(f"HUNG: killed after {timeout}s with no exit. Partial output:\n{(stdout or '')[-1500:]}"
                            f"{(stderr or '')[-1500:]}\nIf this is a server or watcher, run it in the background "
                            f"(`cmd > log 2>&1 &`) and poll the log instead.")
        out = f"exit_code: {p.returncode}\n"
        if stdout:
            out += f"stdout:\n{stdout}"
        if stderr:
            out += f"stderr:\n{stderr}"
        if p.returncode != 0:
            raise ToolError(out)
        return out

    def t_escalate(self, reason: str) -> str:
        raise Escalate(reason)
