#!/usr/bin/env python3
"""Fail if any doc violates docs/Relay-Documentation-Standards.md (naming, format, links)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MAX_LINES = 500
NAME_RE = re.compile(r"^Relay(-[A-Z0-9][A-Za-z0-9]*)+\.md$")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# examples/ and docs/plans/ hold input fixtures (plan.py reads the H1 of a plan as its title), not docs
SKIP_DIRS = {".git", ".relay", "node_modules", "examples", "plans"}


def doc_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def check(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    bad = []
    if len(lines) > MAX_LINES:
        bad.append(f"{rel}: {len(lines)} lines > {MAX_LINES}; split into sub-docs")
    if path.parent == DOCS:
        if not NAME_RE.match(path.name):
            bad.append(f"{rel}: name must be Relay-Title-Kebab-Case.md")
        if not text.startswith("---\n") or "\nupdated:" not in text.split("\n---", 1)[0]:
            bad.append(f"{rel}: needs YAML frontmatter with tags, area, updated")
    if re.search(r"^title\s*:", text, re.MULTILINE):
        bad.append(f"{rel}: frontmatter must not contain title:")
    in_fence = False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if path.name != "README.md" and (re.match(r"^# [^#\s]", line) or line == "#"):
            bad.append(f"{rel}:{i}: remove leading H1 — use ## or prose")
        for target in LINK_RE.findall(line):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            if not (path.parent / target.split("#")[0]).exists():
                bad.append(f"{rel}:{i}: broken link {target}")
    return bad


def main() -> int:
    bad = [msg for p in doc_files() for msg in check(p)]
    if bad:
        print("Documentation standard violations:\n", file=sys.stderr)
        for msg in bad:
            print(f"  {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
