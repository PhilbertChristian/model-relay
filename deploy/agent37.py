#!/usr/bin/env python3
"""Deploy relay onto an Agent37 instance and drive it from your laptop.

Inside an Agent37 instance relay gets Agent37's managed LLM router (dozens of
OpenRouter models, billed to your wallet within the instance budget) for free,
plus OpenAI directly if you pass OPENAI_API_KEY.

    export AGENT37_API_KEY=sk_live_...
    python3 deploy/agent37.py create --budget 2      # new instance, $2 of managed-LLM headroom
    python3 deploy/agent37.py push                   # upload relay + relay.json
    python3 deploy/agent37.py doctor                 # which models are reachable from the instance
    python3 deploy/agent37.py run "build a todo CLI in python with tests"
    python3 deploy/agent37.py run --tier 2 "..."     # start a deployment on the strong tier
    python3 deploy/agent37.py stats                  # usage, switches, savings on the instance
    python3 deploy/agent37.py supervise "task"       # unstick the instance's own hosted agent (Hermes etc.)
    python3 deploy/agent37.py budget [--top-up 1]    # read / raise the instance budget
    python3 deploy/agent37.py destroy
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shlex
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.agent37.com/v1"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".relay" / "instance.json"
FORWARD_ENV = ["OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_TABLE"]


def key() -> str:
    k = os.environ.get("AGENT37_API_KEY")
    if not k:
        sys.exit("set AGENT37_API_KEY (mint one at agent37.com/dashboard/cloud/api-keys)")
    return k


def call(method: str, path: str, body: dict | None = None, timeout: float = 900) -> dict:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {key()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode().strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")


def instance_id(args) -> str:
    if getattr(args, "instance", None):
        return args.instance
    if STATE.exists():
        return json.loads(STATE.read_text())["id"]
    sys.exit("no instance yet: run `create` first or pass --instance")


def exec_(iid: str, command: str, quiet: bool = False) -> dict:
    res = call("POST", f"/instances/{iid}/exec", {"command": command})
    if not quiet:
        sys.stdout.write(res.get("stdout", ""))
        sys.stderr.write(res.get("stderr", ""))
    return res


def cmd_create(args) -> None:
    env = {k: os.environ[k] for k in FORWARD_ENV if os.environ.get(k)}
    body = {"name": args.name, "auto_sleep": True,
            "budget": {"credit_micros": int(args.budget * 1_000_000)}}
    if env:
        body["env"] = env
    print(f"creating instance '{args.name}' (budget ${args.budget}, forwarding {', '.join(env) or 'no env'})…")
    inst = call("POST", "/instances", body)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"id": inst["id"], "name": args.name}))
    print(f"instance {inst['id']} {inst.get('status')}  https://{inst['id']}.agent37.app")


def cmd_push(args) -> None:
    iid = instance_id(args)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(ROOT / "relay", arcname="relay", filter=lambda t: None if "__pycache__" in t.name else t)
        cfg = Path(args.config) if args.config else ROOT / "relay.json"
        if cfg.exists():
            tar.add(cfg, arcname="relay.json")
    b64 = base64.b64encode(buf.getvalue()).decode()
    exec_(iid, "rm -rf ~/relay-app && mkdir -p ~/relay-app ~/work && rm -f /tmp/relay.b64", quiet=True)
    chunk = 60_000
    for i in range(0, len(b64), chunk):
        exec_(iid, f"printf %s {shlex.quote(b64[i:i + chunk])} >> /tmp/relay.b64", quiet=True)
    res = exec_(iid, "cd ~/relay-app && base64 -d /tmp/relay.b64 | tar xz && ls && python3 --version", quiet=True)
    print(res.get("stdout", "") + res.get("stderr", ""))
    print(f"pushed {len(buf.getvalue())} bytes to {iid}:~/relay-app")


def _relay(iid: str, rest: str) -> dict:
    # PYTHONPATH so `python3 -m relay` works from the work dir; relay.json lives next to the package
    return exec_(iid, f"cd ~/work && PYTHONPATH=~/relay-app NO_COLOR=1 RELAY_CONFIG=~/relay-app/relay.json "
                      f"python3 -m relay -y {rest} 2>&1")


def cmd_run(args) -> None:
    iid = instance_id(args)
    flags = f"--tier {args.tier} " if args.tier is not None else ""
    flags += f"--max-steps {args.max_steps} " if args.max_steps else ""
    _relay(iid, f"{flags}run {shlex.quote(' '.join(args.task))}")


def cmd_doctor(args) -> None:
    _relay(instance_id(args), "doctor")


def cmd_models(args) -> None:
    _relay(instance_id(args), "models")


def cmd_stats(args) -> None:
    _relay(instance_id(args), "stats")


def cmd_supervise(args) -> None:
    sys.path.insert(0, str(ROOT))
    from relay.supervise import supervise
    ladder = [m.strip() for m in args.models.split(",")] if args.models else None
    supervise(instance_id(args), " ".join(args.task), ladder, args.agent, hang_s=args.hang)


def cmd_shell(args) -> None:
    exec_(instance_id(args), " ".join(args.command))


def cmd_budget(args) -> None:
    iid = instance_id(args)
    if args.top_up:
        import uuid
        call("POST", f"/instances/{iid}/budget/top-up",
             {"amount_micros": int(args.top_up * 1_000_000), "idempotency_key": uuid.uuid4().hex[:32]})
    print(json.dumps(call("GET", f"/instances/{iid}/budget"), indent=2))


def cmd_destroy(args) -> None:
    iid = instance_id(args)
    call("DELETE", f"/instances/{iid}")
    STATE.unlink(missing_ok=True)
    print(f"deleted {iid}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", help="instance id (default: the one from `create`)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--name", default="relay"); c.add_argument("--budget", type=float, default=2.0)
    p = sub.add_parser("push"); p.add_argument("--config")
    r = sub.add_parser("run"); r.add_argument("task", nargs="+"); r.add_argument("--tier", type=int); r.add_argument("--max-steps", type=int)
    sub.add_parser("doctor"); sub.add_parser("models"); sub.add_parser("stats")
    v = sub.add_parser("supervise"); v.add_argument("task", nargs="+"); v.add_argument("--models")
    v.add_argument("--agent"); v.add_argument("--hang", type=float, default=180)
    s = sub.add_parser("shell"); s.add_argument("command", nargs="+")
    b = sub.add_parser("budget"); b.add_argument("--top-up", type=float)
    sub.add_parser("destroy")
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
