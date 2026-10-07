#!/usr/bin/env python3
"""Flag near-duplicate prose across docs.

One file owns a thing; every other file links to it (docs/Relay-Documentation-Standards.md).
Reports doc pairs sharing many runs of >=MIN_WORDS words, ignoring blockquotes, fenced
blocks and table rows — repeating a command or a key name is fine, copying an explanation is not.
"""
import collections
import pathlib
import re
import sys

MIN_WORDS = 12
NOISE_FLOOR = 25  # pairs sharing fewer n-grams than this are almost always boilerplate
ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".relay", ".worktrees", "node_modules", "examples", "plans", "notes", "burner"}

SKIP_LINE = re.compile(r"^\s*(>|\|)")
FENCE = re.compile(r"^\s*```")


def strip_quoted(text: str) -> str:
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or SKIP_LINE.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def norm(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", strip_quoted(text).lower()).split()


def main() -> int:
    files = [p for p in ROOT.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(ROOT).parts)]
    seen = collections.defaultdict(set)
    for path in files:
        words = norm(path.read_text(encoding="utf-8", errors="replace"))
        for i in range(len(words) - MIN_WORDS):
            seen[" ".join(words[i:i + MIN_WORDS])].add(path)

    pairs = collections.Counter()
    for paths in seen.values():
        if len(paths) > 1:
            pairs[tuple(sorted(str(p.relative_to(ROOT)) for p in paths))] += 1

    hits = [(n, group) for group, n in pairs.items() if n >= NOISE_FLOOR]
    if not hits:
        return 0
    print(f"Duplicated prose across docs (shared runs of {MIN_WORDS}+ words):\n")
    for n, group in sorted(hits, reverse=True):
        print(f"  {n:5d} shared passages   {' <-> '.join(group)}")
    print("\nPick an owner for each; replace the copy with a link.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
