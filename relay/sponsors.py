"""Sponsor integrations: one small, real job each, plus `relay sponsors` to ping them all.

  Agent37      hosts the night shift, LLM router, budget API, platform crons   AGENT37_API_KEY
  OpenAI       a provider in the model ladder                                   OPENAI_API_KEY
  Supabase     telemetry: every call, switch and shift in relay_events          SUPABASE_URL + SUPABASE_KEY
  Monid        `find_tool`: agents discover paid tools at runtime (monid CLI)   `monid keys add -k ...`
  InstaCloud   preview deploy of each night branch (`deploy: instacloud`)       INSTA_TOKEN (+ insta CLI)
  Context.dev  `web_fetch`: agents read docs pages as markdown                  CONTEXT_DEV_API_KEY

Every call here is read-only or tiny, and only runs when you have provided that sponsor's key.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

TIMEOUT = 20


def _http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    for k, v in headers.items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode(errors="replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]
    except Exception as e:  # network errors are reported, never raised
        return 0, str(e)


# ------------------------------------------------------------------ Context.dev
def context_fetch(url: str, max_chars: int = 12_000) -> str:
    """Scrape one URL to markdown with Context.dev. Used by the agent's `web_fetch` tool."""
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    if not key:
        raise RuntimeError("CONTEXT_DEV_API_KEY is not set")
    status, data = _http("POST", "https://api.context.dev/v1/web/scrape", {"Authorization": f"Bearer {key}"},
                         {"url": url, "formats": {"markdown": {}}})
    if status != 200:
        raise RuntimeError(f"context.dev {status}: {str(data)[:200]}")
    md = ""
    if isinstance(data, dict):
        md = (data.get("markdown") or (data.get("data") or {}).get("markdown")
              or ((data.get("formats") or {}).get("markdown") if isinstance(data.get("formats"), dict) else "") or "")
        if isinstance(md, dict):
            md = md.get("content") or json.dumps(md)
    return (md or json.dumps(data))[:max_chars]


# ------------------------------------------------------------------ Monid
def monid_discover(query: str, limit: int = 5) -> str:
    """Find tools for a job via the Monid CLI. Used by the agent's `find_tool` tool."""
    if not shutil.which("monid"):
        raise RuntimeError("monid CLI not installed (see https://monid.ai/skill.md)")
    r = subprocess.run(["monid", "discover", "-q", query, "-l", str(limit), "-j"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or f"monid exit {r.returncode}")
    return r.stdout[:12_000]


# ------------------------------------------------------------------ InstaCloud
def instacloud_deploy(workdir: str, branch: str) -> str:
    """Preview-deploy a night branch with the insta CLI. Returns a one-line result for the morning report."""
    if not (shutil.which("insta") and os.environ.get("INSTA_TOKEN")):
        return "skipped (needs the insta CLI and INSTA_TOKEN)"
    safe = branch.replace("/", "-")
    subprocess.run(["insta", "login", "--api-key", os.environ["INSTA_TOKEN"]], capture_output=True, text=True, timeout=60)
    r = subprocess.run(["insta", "deploy", ".", "--branch", safe], cwd=workdir, capture_output=True, text=True, timeout=900)
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
    url = next((w for w in (r.stdout + r.stderr).split() if w.startswith("https://")), "")
    return f"deployed {url}".strip() if r.returncode == 0 else f"deploy failed: {tail[0][:160]}"


# ------------------------------------------------------------------ relay sponsors
def check() -> list[dict]:
    rows = []

    def add(name, job, status, detail):
        rows.append({"sponsor": name, "job": job, "status": status, "detail": detail})

    k = os.environ.get("AGENT37_API_KEY")
    if k:
        s, d = _http("GET", "https://api.agent37.com/v1/usage", {"Authorization": f"Bearer {k}"})
        add("Agent37", "hosting, router, budgets, crons", "ok" if s == 200 else "error",
            f"usage {d.get('total_micros', 0) / 1e6:.2f} USD (30d)" if s == 200 and isinstance(d, dict) else f"{s}: {str(d)[:120]}")
    else:
        add("Agent37", "hosting, router, budgets, crons", "skipped", "set AGENT37_API_KEY")

    k = os.environ.get("OPENAI_API_KEY")
    if k:
        s, d = _http("GET", "https://api.openai.com/v1/models", {"Authorization": f"Bearer {k}"})
        add("OpenAI", "model provider", "ok" if s == 200 else "error",
            f"{len(d.get('data', []))} models" if s == 200 and isinstance(d, dict) else f"{s}: {str(d)[:120]}")
    else:
        add("OpenAI", "model provider", "skipped", "set OPENAI_API_KEY")

    url, k = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and k:
        table = os.environ.get("SUPABASE_TABLE", "relay_events")
        s, d = _http("GET", f"{url}/rest/v1/{table}?select=id&limit=1", {"apikey": k, "Authorization": f"Bearer {k}"})
        add("Supabase", "telemetry (relay_events)", "ok" if s in (200, 206) else "error",
            "relay_events reachable" if s in (200, 206) else f"{s}: {str(d)[:120]} (run supabase/schema.sql)")
    else:
        add("Supabase", "telemetry (relay_events)", "skipped", "set SUPABASE_URL and SUPABASE_KEY")

    if shutil.which("monid"):
        r = subprocess.run(["monid", "balance", "-j"], capture_output=True, text=True, timeout=30)
        add("Monid", "find_tool: runtime tool discovery", "ok" if r.returncode == 0 else "error",
            (r.stdout or r.stderr).strip().replace("\n", " ")[:120])
    else:
        add("Monid", "find_tool: runtime tool discovery", "skipped", "install the monid CLI, then monid keys add -k ...")

    k = os.environ.get("INSTA_TOKEN")
    if k:
        s, d = _http("GET", "https://api.instacloud.com/me", {"Authorization": f"Bearer {k}"})
        who = (d.get("email") or d.get("name") or d.get("id")) if isinstance(d, dict) else None
        add("InstaCloud", "preview deploys of night branches", "ok" if s == 200 else "error",
            f"signed in as {who}" if s == 200 else f"{s}: {str(d)[:120]}")
    else:
        add("InstaCloud", "preview deploys of night branches", "skipped", "set INSTA_TOKEN")

    if os.environ.get("CONTEXT_DEV_API_KEY"):
        try:
            md = context_fetch("https://example.com", max_chars=200)
            add("Context.dev", "web_fetch: agents read docs", "ok", f"scraped example.com ({len(md)} chars)")
        except Exception as e:
            add("Context.dev", "web_fetch: agents read docs", "error", str(e)[:120])
    else:
        add("Context.dev", "web_fetch: agents read docs", "skipped", "set CONTEXT_DEV_API_KEY")
    return rows


def render(rows: list[dict]) -> str:
    mark = {"ok": "✓", "skipped": "·", "error": "✗"}
    lines = ["  sponsor       job                                   status"]
    for r in rows:
        lines.append(f"  {mark[r['status']]} {r['sponsor']:<12}{r['job']:<38}{r['status']:<8} {r['detail']}")
    return "\n".join(lines)
