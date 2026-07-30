"""One fact, one place. The wire field may be named where it is the *capture*,
and nowhere else.

This project has now drifted the claim "LinkedIn does not de-duplicate, so a
retry is a second message" between prose and payload five times, and the last
correction had to touch sixteen sites across twelve files. A checklist is what
produced that number; this is the mechanism that replaces it.

The rule it enforces is narrow and deliberately not about the payload:
`surfaces/messaging.py` still sends the field below exactly as captured, with the
value the capture carries, because the capture wins over any prose written about
it. What may not spread is the *justification* - every agent- and
operator-facing surface has to
state the retry rule in terms of what the CLI does, not by naming a field in
LinkedIn's request body, because the day that field's meaning changes those
sentences all become wrong at once and nothing goes red.

Stdlib only, and it walks the working tree rather than asking git. A guard that
asks git what exists can only see what git has been told about; this one is
about prose in files that may not be committed yet, so it reads the tree.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from linkedin_cli.surfaces import messaging

# Split so this file is not its own hit and needs no exemption for itself.
# `+` rather than implicit concatenation, which `ruff format` joins back up.
FIELD = "dedupeByClient" + "GeneratedToken"

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "raw",  # tests/fixtures/raw - real captures, gitignored, not ours to edit
}

# Where the field is allowed to be named, and why each one is on the list.
ALLOWED = {
    # The payload itself, plus `send_message`'s docstring explaining it.
    "linkedin_cli/surfaces/messaging.py",
    # The capture, and the paragraph that reasons from it.
    "docs/write-payloads.md",
    # The tests that go red if somebody flips the field.
    "tests/test_messaging_write.py",
    "tests/test_messaging.py",
    # A changelog records what was true at a release. Editing it is falsifying
    # history; the newer entry supersedes the older one.
    "CHANGELOG.md",
}

# Design documents are records of a decision, exempt for the same reason
# CHANGELOG.md is: they argue *about* the field, they are dated, and nothing
# reads them at runtime. They are not an agent-facing surface.
ALLOWED_PREFIXES = ("docs/specs/",)


def _files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def test_the_wire_field_appears_only_in_the_capture_and_its_tests():
    """Everything else states the retry rule without naming a wire field."""
    found = set()
    for path in _files():
        try:
            if FIELD in path.read_text(encoding="utf-8"):
                found.add(path.relative_to(ROOT).as_posix())
        except (UnicodeDecodeError, OSError):
            continue
    stray = {
        name for name in found if name not in ALLOWED and not name.startswith(ALLOWED_PREFIXES)
    }
    assert stray == set(), (
        f"{FIELD} names a field in LinkedIn's request body, and these files are not the "
        "capture: state the retry rule in terms of what this CLI does instead"
    )


def test_the_field_is_confined_to_the_function_that_sends_it():
    """Scope within the module, not only across files.

    `_unconfirmed` used to justify `retryable: False` by naming this field, three
    hundred lines away from the body that carries it - so the payload and the
    prose could drift without either one looking wrong on its own. The count has
    to match `send_message`'s own source: anything else means it has escaped
    again.
    """
    source = inspect.getsource(messaging)
    assert source.count(FIELD) == inspect.getsource(messaging.send_message).count(FIELD)
    assert FIELD in source, "the guard is vacuous if the field is gone from the payload"
