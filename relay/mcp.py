"""Relay as an MCP server, so any MCP harness (Claude Code, Codex, OpenCode, Hermes, ...) can drive it.

Stdio transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, protocol 2025-06-18. Standard library
only. stdout carries protocol messages and nothing else; logs go to stderr.

    python3 -m relay.mcp [-C WORKSPACE]

WORKSPACE (default: the directory the harness starts the server in) is where relative paths resolve and
where Relay's ledger lives (WORKSPACE/.relay/events.jsonl).

Tools
    relay_capacity   what subscription capacity expires unused, tonight's allowance, next downtime window
    relay_savings    this month's usage at API rates, and how much the night shift rescued
    relay_plan_list  tasks queued in a markdown plan, per project, in run order
    relay_plan_add   queue a task for tonight ("- [ ] task" under "## project")
    relay_burn       start a night shift now (opt-in: RELAY_MCP_ALLOW_BURN=1), or get the schedule commands

Hook it up
    Claude Code    claude mcp add relay -- python3 -m relay.mcp
                   claude mcp add relay -e PYTHONPATH=/path/to/model-relay -- python3 -m relay.mcp -C ~/nightshift
                   (add  -e RELAY_MCP_ALLOW_BURN=1  to let the agent start a shift itself)

    .mcp.json      {"mcpServers": {"relay": {"command": "python3",
                                             "args": ["-m", "relay.mcp", "-C", "/Users/you/nightshift"],
                                             "env": {"PYTHONPATH": "/path/to/model-relay"}}}}

    Codex          ~/.codex/config.toml
                   [mcp_servers.relay]
                   command = "python3"
                   args = ["-m", "relay.mcp"]
                   env = { PYTHONPATH = "/path/to/model-relay" }

    OpenCode       opencode.json
                   {"mcp": {"relay": {"type": "local", "command": ["python3", "-m", "relay.mcp"],
                                      "environment": {"PYTHONPATH": "/path/to/model-relay"}}}}

    Hermes         ~/.hermes/config.yaml
                   mcp_servers:
                     relay: {command: python3, args: ["-m", "relay.mcp"], env: {PYTHONPATH: /path/to/model-relay}}

PYTHONPATH is only needed when the harness does not start the server from the repo root.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shlex
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import __version__

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")   # tools + text content are identical in all three
ALLOW_BURN_ENV = "RELAY_MCP_ALLOW_BURN"
NEW_PLAN_HEADER = "# Night shift plan"
PLAN_SUFFIXES = (".md", ".markdown")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

INSTRUCTIONS = (
    "Relay runs a night shift: while the user is offline it works through a markdown plan of coding tasks "
    "using paid subscription capacity that would otherwise expire unused. Queue work with relay_plan_add "
    "(one self-contained, testable task per call), review the queue with relay_plan_list, check what capacity "
    "is expiring with relay_capacity, report value with relay_savings, and start or schedule a shift with "
    "relay_burn. Relative paths resolve against the server's workspace."
)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
REPO = Path(__file__).resolve().parent.parent      # the relay checkout (holds deploy/agent37.py)


def log(msg: str) -> None:
    print(f"relay.mcp: {msg}", file=sys.stderr, flush=True)


class ToolFailure(Exception):
    """A tool ran but could not do what was asked; reported to the model as isError."""


# --------------------------------------------------------------------------- tool catalogue
_PLAN = {"type": "string", "description": "Path to the markdown plan (.md), e.g. PLAN.md (relative to the workspace)."}
_BURNER = {"type": "string", "description": "Path to burner.json: subscriptions, downtime hours and model ladder. "
                                            "Default: burner.json in the workspace."}

TOOLS: list[dict] = [
    {
        "name": "relay_capacity",
        "title": "Relay: expiring subscription capacity",
        "description": "Show how much paid subscription capacity (Agent37 budget, metered API budgets, Claude/ChatGPT "
                       "plan windows) will expire unused before it resets, tonight's burn allowance per subscription, "
                       "the burn order, and the next downtime window.",
        "inputSchema": {"type": "object", "properties": {"burner": _BURNER}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "relay_savings",
        "title": "Relay: monthly savings",
        "description": "This month's AI usage valued at API list prices, and how much of it Relay's night shifts rescued "
                       "from capacity that was about to expire. Reads local logs only.",
        "inputSchema": {"type": "object", "properties": {
            "month": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$",
                      "description": "YYYY-MM. Default: the current UTC month."},
            "ledger": {"type": "string", "description": "Relay events.jsonl. Default: .relay/events.jsonl in the workspace."},
            "include_claude": {"type": "boolean", "default": True,
                               "description": "Also value Claude Code usage from its local logs (~/.claude/projects)."},
        }, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "relay_plan_list",
        "title": "Relay: list queued tasks",
        "description": "List the tasks queued in a night-shift plan, grouped by project in the order the night shift "
                       "will run them, plus tasks blocked waiting on a human.",
        "inputSchema": {"type": "object", "properties": {"plan": _PLAN}, "required": ["plan"],
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "relay_plan_add",
        "title": "Relay: queue a task for tonight",
        "description": "Queue a task for tonight's night shift: appends '- [ ] <task>' under '## <project>' in the "
                       "markdown plan, creating the project heading or the plan file if missing. A task already "
                       "queued in that project is not added twice. Write one self-contained, testable task per call.",
        "inputSchema": {"type": "object", "properties": {
            "plan": _PLAN,
            "project": {"type": "string", "minLength": 1,
                        "description": "Project name: the plan's '## ' heading (created if missing)."},
            "task": {"type": "string", "minLength": 1,
                     "description": "One line describing the change and how to verify it."},
        }, "required": ["plan", "project", "task"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "relay_burn",
        "title": "Relay: start or schedule a night shift",
        "description": "Work through the plan's queued tasks with Relay agents on capacity that would otherwise expire. "
                       "With now=true it runs synchronously (blocks until the shift ends, commits to a local "
                       "relay/night-<date> branch, never opens PRs) and only if the server was started with "
                       f"{ALLOW_BURN_ENV}=1. Otherwise it returns the commands to schedule the shift.",
        "inputSchema": {"type": "object", "properties": {
            "plan": _PLAN,
            "burner": _BURNER,
            "now": {"type": "boolean", "default": False,
                    "description": "Start immediately instead of returning schedule instructions."},
            "max_tasks": {"type": "integer", "minimum": 1, "description": "Stop after this many tasks."},
            "hours": {"type": "number", "exclusiveMinimum": 0, "description": "Stop after this many hours."},
        }, "required": ["plan"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                        "openWorldHint": True},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}
EMPTY_LISTS = {"resources/list": "resources", "resources/templates/list": "resourceTemplates",
               "prompts/list": "prompts"}


def _check_args(tool: dict, args: dict) -> dict:
    """Minimal JSON Schema check for the flat schemas above. Returns args with defaults filled in."""
    schema = tool["inputSchema"]
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if args.get(k) in (None, "")]
    if missing:
        raise ToolFailure(f"missing required argument(s): {', '.join(missing)}")
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolFailure(f"unknown argument(s): {', '.join(unknown)}; expected {', '.join(props)}")
    out = {}
    for k, spec in props.items():
        if args.get(k) is None or (args[k] == "" and k not in schema.get("required", [])):
            if "default" in spec:
                out[k] = spec["default"]
            continue
        v, t = args[k], spec["type"]
        ok = {"string": isinstance(v, str), "boolean": isinstance(v, bool),
              "integer": isinstance(v, int) and not isinstance(v, bool),
              "number": isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}[t]
        if not ok:
            raise ToolFailure(f"argument '{k}' must be {'an' if t == 'integer' else 'a'} {t}, got {json.dumps(v)}")
        if t == "string" and spec.get("minLength") and not v.strip():
            raise ToolFailure(f"argument '{k}' must not be empty")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], v):     # re.match + '$' accepts a trailing \n
            raise ToolFailure(f"argument '{k}' must match {spec['pattern']}, got {v!r}")
        if "minimum" in spec and v < spec["minimum"]:
            raise ToolFailure(f"argument '{k}' must be >= {spec['minimum']}")
        if "exclusiveMinimum" in spec and v <= spec["exclusiveMinimum"]:
            raise ToolFailure(f"argument '{k}' must be > {spec['exclusiveMinimum']}")
        out[k] = v
    return out


# --------------------------------------------------------------------------- plan editing
def add_task(plan_path: Path, project: str, task: str) -> str:
    """Append '- [ ] task' under '## project'. Returns a one-line description of what happened."""
    from .plan import TASK_RE

    if plan_path.suffix.lower() not in PLAN_SUFFIXES:
        # the path comes from the model; never append checklist lines to a shell rc, a config file or source code
        raise ToolFailure(f"refusing to edit {plan_path}: a plan must be a markdown file "
                          f"({', '.join(PLAN_SUFFIXES)}), e.g. PLAN.md")
    project = " ".join(project.split()).lstrip("#").strip()   # '## api' -> 'api', not a '## ## api' heading
    task = " ".join(task.split())          # one line: a newline would break the checklist
    if task.startswith(("- [", "[ ]", "[x]", "[!]")):
        task = re.sub(r"^(- )?\[[ xX!]\]\s*", "", task)
    if not project or not task:
        raise ToolFailure("project and task must be non-empty")

    if not plan_path.exists():
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        _write(plan_path, [NEW_PLAN_HEADER, "", f"## {project}", "", f"- [ ] {task}"])
        return f"created {plan_path} and queued under '{project}': {task}"

    lines = plan_path.read_text().splitlines()
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    name_of = {i: lines[i][3:].strip() for i in heads}
    h = next((i for i in heads if name_of[i] == project), None)
    if h is None:
        h = next((i for i in heads if name_of[i].lower() == project.lower()), None)

    if h is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ([""] if lines else []) + [f"## {project}", "", f"- [ ] {task}"]
        _write(plan_path, lines)
        return f"added project '{project}' to {plan_path} and queued: {task}"

    end = next((i for i in heads if i > h), len(lines))
    section = range(h + 1, end)
    for i in section:
        m = TASK_RE.match(lines[i])
        if m and m.group(2) == " " and " ".join(m.group(3).split()) == task:
            return f"already queued under '{name_of[h]}' in {plan_path}: {task}"
    task_lines = [i for i in section if TASK_RE.match(lines[i])]
    if task_lines:
        lines.insert(task_lines[-1] + 1, f"- [ ] {task}")
    else:
        last = max([i for i in section if lines[i].strip()], default=h)
        lines[last + 1:last + 1] = ["", f"- [ ] {task}"]
        if last + 3 < len(lines) and lines[last + 3].strip():
            lines.insert(last + 3, "")           # keep a blank line before the next heading
    _write(plan_path, lines)
    return f"queued under '{name_of[h]}' in {plan_path}: {task}"


def _write(path: Path, lines: list[str]) -> None:
    path = Path(os.path.realpath(path))          # a symlinked plan (e.g. into a notes vault) stays a symlink
    tmp = path.with_name(f".{path.name}.relay-tmp")
    tmp.write_text("\n".join(lines) + "\n")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o7777)   # keep the plan's permissions
    os.replace(tmp, path)                        # atomic: a running night shift never sees half a plan


def list_plan(plan_path: Path) -> str:
    from .plan import parse

    if not plan_path.exists():
        raise ToolFailure(f"no plan at {plan_path} yet; queue work with relay_plan_add")
    pl = parse(plan_path)
    tasks = [t for p in pl.projects for t in p.tasks]
    done, blocked = sum(t.state == "x" for t in tasks), [(p, t) for p in pl.projects for t in p.tasks if t.state == "!"]
    out = [f"{pl.title} · {plan_path}",
           f"{len(pl.queue)} queued · {done} done · {len(blocked)} blocked", ""]
    if not pl.projects:
        out.append("no projects yet; add one with relay_plan_add")
    for p in sorted(pl.projects, key=lambda p: (p.priority, p.line)):
        meta = [f"priority {p.priority}"] if p.priority != 100 else []
        meta += [f"test: {p.test}"] if p.test else []
        meta += [f"budget: ${p.budget:g}"] if p.budget is not None else []
        out.append(f"## {p.name}" + (f"  ({', '.join(meta)})" if meta else ""))
        out += [f"- [ ] {t.text}" for t in p.todo] or ["(nothing queued)"]
        out.append("")
    if blocked:
        out.append("needs a human:")
        out += [f"- [!] {p.name}: {t.text}" for p, t in blocked]
    return "\n".join(out).rstrip()


# --------------------------------------------------------------------------- server
class Server:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.getcwd()).expanduser().resolve()
        self.initialized = False
        self.client: dict = {}
        self.handlers: dict[str, Callable[[dict], str]] = {
            "relay_capacity": self.t_capacity, "relay_savings": self.t_savings,
            "relay_plan_list": self.t_plan_list, "relay_plan_add": self.t_plan_add, "relay_burn": self.t_burn,
        }

    # ---- paths and config
    def path(self, p: str) -> Path:
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else self.root / q

    @property
    def ledger(self) -> Path:
        return self.root / ".relay" / "events.jsonl"

    def burner(self, p: str | None) -> tuple[dict, Path]:
        from .config import DEFAULT, load

        path = self.path(p or "burner.json")
        if not path.exists():
            raise ToolFailure(f"burner config not found: {path}. Create one (see examples/burner-demo.json in the "
                              f"relay repo) or pass burner=<path>.")
        try:
            cfg = load(str(path))
        except json.JSONDecodeError as e:
            raise ToolFailure(f"{path} is not valid JSON: {e}") from e
        return ({**DEFAULT, **cfg} if "models" not in cfg else cfg), path

    # ---- tools
    def t_capacity(self, a: dict) -> str:
        from . import capacity

        cfg, path = self.burner(a.get("burner"))
        return f"burner: {path}\n" + capacity.report(capacity.assess(cfg, str(self.ledger)), cfg)

    def t_savings(self, a: dict) -> str:
        from . import value

        ledger = self.path(a["ledger"]) if a.get("ledger") else self.ledger
        m = value.month(a.get("month"), [str(ledger)], include_claude=a.get("include_claude", True))
        text = value.render(m, color=False)
        if not ledger.exists():
            text += f"\n  (no Relay ledger at {ledger} yet)"
        return text

    def t_plan_list(self, a: dict) -> str:
        return list_plan(self.path(a["plan"]))

    def t_plan_add(self, a: dict) -> str:
        return add_task(self.path(a["plan"]), a["project"], a["task"])

    def t_burn(self, a: dict) -> str:
        plan = self.path(a["plan"])
        allowed = os.environ.get(ALLOW_BURN_ENV) == "1"
        if not (a.get("now") and allowed):
            text = self.schedule_help(plan, a.get("burner"))
            if a.get("now"):
                raise ToolFailure(
                    "Refused: starting a night shift from MCP runs unattended code, so it is opt-in. Restart this "
                    f"server with {ALLOW_BURN_ENV}=1 (e.g. claude mcp add relay -e {ALLOW_BURN_ENV}=1 -- python3 -m "
                    "relay.mcp) and call relay_burn again with now=true, or schedule it instead:\n\n" + text)
            return text
        if not plan.exists():
            raise ToolFailure(f"no plan at {plan}; queue work with relay_plan_add first")
        cfg, _ = self.burner(a.get("burner"))
        from .burn import burn

        log(f"night shift starting on {plan}")
        # a git credential prompt would open the harness's terminal (/dev/tty) and hang the shift unattended
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                res = burn(str(plan), cfg, str(self.root), now=True, hours=a.get("hours"),
                           max_tasks=a.get("max_tasks"), wait=False, pr=False)
        finally:
            out = ANSI.sub("", buf.getvalue())
            log_path = self.root / ".relay" / "mcp-burn.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(out)
        results = res.get("results", [])
        n = {s: sum(r["status"] == s for r in results) for s in ("done", "blocked", "skipped")}
        head = (f"night shift {res.get('status')} · {n['done']} done · {n['blocked']} blocked"
                + (f" · {n['skipped']} skipped" if n["skipped"] else "") + f" · ${res.get('usd', 0):.4f} at API rates")
        morning = self.root / "MORNING.md"
        body = morning.read_text() if morning.exists() else "\n".join(out.strip().splitlines()[-40:])
        return f"{head}\nfull log: {log_path}\n\n{body}".rstrip()

    def schedule_help(self, plan: Path, burner_arg: str | None) -> str:
        burner = self.path(burner_arg or "burner.json")
        q = lambda p: shlex.quote(str(p))  # noqa: E731
        tz, at, when, notes = "America/Los_Angeles", "23:00", "", []
        if burner.exists():
            try:
                from . import capacity
                from .config import load

                idle = load(str(burner)).get("idle", {"weekday": "23:00-07:00", "weekend": "all"})
                # the same zone capacity.current_or_next_window reads the window in
                tz = idle.get("timezone") or os.environ.get("TZ") or "UTC"
                if not idle.get("timezone"):
                    notes.append(f"note: {burner.name} has no idle.timezone, so the window is read as {tz} here "
                                 "(and UTC on a server without TZ); add one to pin it")
                # the Agent37 cron starts the shift with --now, so it must fire when the weekday window opens
                m = re.match(r"\s*(\d{1,2}):(\d{2})\s*-", str(idle.get("weekday") or ""))
                at = f"{int(m[1]):02d}:{m[2]}" if m else at
                s, e, active = capacity.current_or_next_window(idle)
                when = f"downtime window: {'now' if active else s.strftime('%a %H:%M')} -> {e.strftime('%a %H:%M')} ({tz})"
            except Exception as e:  # schedule help must not fail on a bad burner file
                notes.append(f"note: could not read downtime from {burner}: {e}")
        else:
            notes.append(f"note: no burner config at {burner} yet (see examples/burner-demo.json)")
        if not plan.exists():
            notes.append(f"note: no plan at {plan} yet; queue work with relay_plan_add")
        rel = lambda p: q(p.relative_to(self.root) if p.is_relative_to(self.root) else p)  # noqa: E731
        pypath = f"PYTHONPATH={q(REPO)} " if (REPO / "relay").is_dir() and REPO != self.root else ""
        relay = f"{pypath}python3 -m relay burn run {rel(plan)} -b {rel(burner)}"
        deploy = REPO / "deploy" / "agent37.py"
        a37 = "python3 " + (rel(deploy) if deploy.exists() else "deploy/agent37.py")
        lines = [f"Not started. To run the night shift on {rel(plan)}:", ""]
        lines += [when, ""] if when else []
        lines += [
            "  from the workspace:",
            f"    cd {q(self.root)}",
            "  tonight, on this machine (sleeps until your downtime window, then starts):",
            f"    {relay} --wait",
            "  right now, on this machine:",
            f"    {relay} --now",
            "  every night on an always-on Agent37 instance (cron fires while your laptop sleeps; needs AGENT37_API_KEY):",
            f"    {a37} create --always-on --budget 5      # once",
            f"    {a37} push --plan {rel(plan)} --burner {rel(burner)}",
            f"    {a37} schedule --at {at} --tz {q(tz)}",
            "",
            f"Start it from here instead: call relay_burn with now=true (this server has {ALLOW_BURN_ENV}=1)."
            if os.environ.get(ALLOW_BURN_ENV) == "1" else
            f"To let MCP clients start a shift directly, restart this server with {ALLOW_BURN_ENV}=1 and call "
            "relay_burn with now=true.",
        ]
        return "\n".join(lines + ([""] + notes if notes else []))

    # ---- JSON-RPC
    def handle(self, msg: Any) -> dict | list | None:
        """One decoded message (or batch) in, the response (or None for notifications) out."""
        if isinstance(msg, list):
            if not msg:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [r for r in (self.handle(m) for m in msg) if r is not None]
            return replies or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST,
                          "expected a JSON-RPC 2.0 object")
        method, is_request = msg.get("method"), "id" in msg
        if method is None:
            if "result" in msg or "error" in msg:
                return None                        # a response to a request we never sent: ignore
            return _error(msg.get("id"), INVALID_REQUEST, "missing method")
        if not isinstance(method, str):
            return _error(msg.get("id"), INVALID_REQUEST, "method must be a string")
        if not is_request:
            if method == "notifications/initialized":
                self.initialized = True
            return None                            # all other notifications (cancelled, progress, ...) are ignored
        rid, params = msg["id"], msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(rid, self.initialize(params))
            if method == "ping":
                return _result(rid, {})
            if method == "tools/list":
                return _result(rid, {"tools": TOOLS})
            if method in EMPTY_LISTS:              # some clients probe these whatever the capabilities say
                return _result(rid, {EMPTY_LISTS[method]: []})
            if method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if not isinstance(name, str) or name not in TOOL_NAMES:
                    return _error(rid, INVALID_PARAMS, f"unknown tool: {name}", {"tools": sorted(TOOL_NAMES)})
                if not isinstance(args, dict):
                    return _error(rid, INVALID_PARAMS, "arguments must be an object")
                return _result(rid, self.call_tool(name, args))
            return _error(rid, METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as e:  # never let one bad request kill the server
            log(f"internal error in {method}: {e}\n{traceback.format_exc()}")
            return _error(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    def initialize(self, params: dict) -> dict:
        self.client = params.get("clientInfo") or {}
        asked = params.get("protocolVersion")
        log(f"initialize from {self.client.get('name', 'unknown client')} (protocol {asked}); workspace {self.root}")
        return {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "relay", "title": "Relay Night Shift", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def call_tool(self, name: str, args: dict) -> dict:
        tool = next(t for t in TOOLS if t["name"] == name)
        stray = io.StringIO()
        try:
            with redirect_stdout(stray):           # a print() deep inside relay must never reach the protocol stream
                text, is_error = self.handlers[name](_check_args(tool, args)), False
        except ToolFailure as e:
            text, is_error = str(e), True
        except FileNotFoundError as e:
            text, is_error = f"file not found: {e.filename or e}", True
        except Exception as e:
            log(f"{name} failed: {e}\n{traceback.format_exc()}")
            text, is_error = f"{name} failed: {type(e).__name__}: {e}", True
        if stray.getvalue():
            sys.stderr.write(ANSI.sub("", stray.getvalue()))
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def serve(instream: BinaryIO, outstream: BinaryIO, server: Server | None = None) -> int:
    """Read newline-delimited JSON-RPC from instream until EOF, write one line per response to outstream."""
    server = server or Server()
    while True:
        raw = instream.readline()
        if not raw:
            return 0
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            reply: Any = _error(None, PARSE_ERROR, f"parse error: {e.msg}")
        else:
            reply = server.handle(msg)
        if reply is not None:
            data = json.dumps(reply, separators=(",", ":")) + "\n"   # ASCII-only, never contains a raw newline
            outstream.write(data.encode() if not isinstance(outstream, io.TextIOBase) else data)
            outstream.flush()


def _claim_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Keep private handles on the real stdin/stdout for the protocol, then point fd 0 at /dev/null and fd 1 at
    stderr, so neither print() nor a child process (git, tests, an agent's shell command) can corrupt or swallow
    protocol messages."""
    sys.stdout.flush()
    try:
        os.fstat(2)
    except OSError:  # started with stderr closed: give fd 2 to /dev/null, or dup(0) below would land on it
        fd = os.open(os.devnull, os.O_WRONLY)
        if fd != 2:
            os.dup2(fd, 2)
            os.close(fd)
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull)
    sys.stdout = sys.stderr
    return proto_in, proto_out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m relay.mcp",
                                 description="Relay MCP server (stdio). Point an MCP client at this command.",
                                 epilog="Example: claude mcp add relay -- python3 -m relay.mcp")
    ap.add_argument("-C", "--cwd", default=None,
                    help="workspace: relative paths and the .relay ledger resolve here (default: current directory)")
    args = ap.parse_args(argv)
    server = Server(args.cwd)
    try:
        proto_in, proto_out = _claim_stdio()
    except OSError as e:  # pragma: no cover - exotic hosts without dup/dup2
        log(f"could not isolate stdio ({e}); using sys streams")
        proto_in, proto_out = sys.stdin.buffer, sys.stdout.buffer
    log(f"relay {__version__} MCP server on stdio (protocol {PROTOCOL_VERSION}); "
        f"night shift via MCP {'ENABLED' if os.environ.get(ALLOW_BURN_ENV) == '1' else 'disabled'}")
    try:
        return serve(proto_in, proto_out, server)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
