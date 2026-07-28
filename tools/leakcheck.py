#!/usr/bin/env python3
"""Scan the files that can reach a public remote for anything that looks like
real account data.

`.gitignore` is not a security boundary: it does nothing about `git add -f`,
about files that were already tracked, or about a capture saved under a name
nobody thought to exclude.

Two sets are scanned, and the second one is the lesson. Scanning `git ls-files`
alone checks what is *already* tracked, which is one commit too late: a file that
is untracked and unignored is invisible to this script right up to the moment
`git add` makes it a tracked file with its contents intact. A fixture modelled on
a real capture passed this gate that way - it was clean every time it was run,
because it had never been added yet. So `--others --exclude-standard` is scanned
too, and listed even when clean, because that list is exactly what the next
`git add -A` will sweep in.

    python3 tools/leakcheck.py          # exits 1 on a hit, in either set

Patterns target *real* values, not the obviously synthetic ones the suite uses:
a live `li_at` is ~150 characters, a real member id ~36, so length is what
separates a leak from a fixture.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("live li_at token", re.compile(r"AQED[A-Za-z0-9_\-]{40,}")),
    ("real member id", re.compile(r"ACoAA[A-Za-z0-9_\-]{30,}")),
    ("JSESSIONID value", re.compile(r"ajax:\d{15,}")),
    ("email address", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("bcookie/bscookie value", re.compile(r"v=2&[A-Fa-f0-9\-]{30,}")),
    ("linkedin auth cookie assignment", re.compile(r"li_at\s*=\s*[\"']AQED[A-Za-z0-9_\-]{20,}")),
]

# Synthetic values the suite deliberately contains. Anything added here must be
# obviously fake to a reader, and must be something the tree actually contains -
# an entry matching nothing is an assertion nobody can check, and the next reader
# has to take on trust that the value it exempts was ever synthetic.
ALLOW = {
    "ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH",
    "ajax:1111222233334444555",
}

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".lock"}


def _git(*args: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    )
    return [REPO / line for line in out.stdout.splitlines() if line]


def tracked_files() -> list[Path]:
    return _git("ls-files")


def untracked_files() -> list[Path]:
    """Files git would add next, minus everything `.gitignore` covers.

    `--exclude-standard` is what keeps the raw captures out of this list: they
    live under a gitignored directory, cannot be committed by accident, and
    reporting them every run would train the reader to skim the output.
    """
    return _git("ls-files", "--others", "--exclude-standard")


def scan(paths: list[Path]) -> list[tuple[Path, str, str]]:
    hits: list[tuple[Path, str, str]] = []
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS:
            for match in pattern.findall(text):
                if match in ALLOW:
                    continue
                hits.append((path.relative_to(REPO), label, match))
    return hits


def _report(header: str, hits: list[tuple[Path, str, str]]) -> None:
    print(f"leakcheck: {header}\n", file=sys.stderr)
    for path, label, match in hits:
        shown = match if len(match) <= 24 else match[:18] + "…"
        print(f"  {path}: {label}: {shown}", file=sys.stderr)


def main() -> int:
    tracked = tracked_files()
    untracked = untracked_files()
    # Scanned together and judged the same, because a hit in an untracked file is
    # a hit that is one `git add` from being permanent, and the point of running
    # this before a commit is to be told now rather than after.
    tracked_hits = scan(tracked)
    untracked_hits = scan(untracked)

    if tracked_hits:
        _report("POSSIBLE ACCOUNT DATA IN TRACKED FILES", tracked_hits)
    if untracked_hits:
        _report("POSSIBLE ACCOUNT DATA IN UNTRACKED, UNIGNORED FILES", untracked_hits)
    if tracked_hits or untracked_hits:
        print(
            "\nScrub the value, or add it to ALLOW if it is genuinely synthetic.",
            file=sys.stderr,
        )
        return 1

    # Flushed because the note below goes to stderr, which is unbuffered: without
    # this the two streams interleave and the note reads as if it preceded the
    # result it qualifies.
    print(f"leakcheck: clean ({len(tracked)} tracked files)", flush=True)
    if untracked:
        # Clean, and still worth naming. These files were scanned against the
        # patterns above, which only cover shapes somebody thought of; the reason
        # to list them is that they are about to become tracked and nobody has
        # reviewed them for anything else.
        print(
            f"leakcheck: note - {len(untracked)} untracked, unignored file(s) scanned; "
            "`git add -A` would commit these:",
            file=sys.stderr,
        )
        for path in untracked:
            print(f"  {path.relative_to(REPO)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
