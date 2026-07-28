"""The single output envelope, plus the human-readable renderer.

Every command returns the same two shapes, so an agent branches on `ok` once and
never has to pattern-match per command:

    {"ok": true,  "data": ..., "next_cursor": ...|null, "has_more": false}
    {"ok": false, "error": {"code": ..., "message": ..., "retryable": false}}

...plus one optional key: `error.body`, the upstream response, present only when
the caller passed `--raw`. See `err`.

`redact_headers` is an **allowlist**. The `csrf-token` header is the `JSESSIONID`
cookie value with the quotes stripped, so a denylist that dropped only `cookie`
would print a live session credential straight into an agent's transcript, where
it lives forever. The same rule covers `error.body`: it goes through
`transport.scrub_secrets` inside `err`, so *that* key is redacted by construction
rather than by the caller remembering.

Two things this file does **not** redact, stated here because this paragraph is
what a reader consults to decide whether they have to scrub - and because it
claimed the opposite for both until a live run:

* **`error.message` carries no such guarantee.** It is whatever `str(exc)` the
  raiser built, spliced into the envelope as given. A surface that puts
  LinkedIn-supplied text in one - a refusal read out of a 200 body, an upstream
  message - has to scrub it *there*; `surfaces/social.py`'s `_refused` and
  `_unconfirmed` are what that looks like. `transport.raise_for_status` scrubs
  the body *it* splices, which covers everything raised from a non-2xx and
  nothing else.
* **One live path through `error.message` is deliberate**, and calling the two
  gaps above exhaustive would be the same false comfort this paragraph exists to
  correct. A surface may pass an identifier through unscrubbed when it is the
  only thing naming the *subject* of the write - `invitations._quota`'s and
  `_unconfirmed`'s `target`, `posts._not_removed`'s `activity`,
  `social._refused`'s `what`. `scrub_secrets` would redact a member urn, and on
  the public-id branch that urn came out of LinkedIn's answer rather than the
  operator's argument - so the scrubbed message would be one nobody could read
  the account back with, which is its own way of leaving the operator unable to
  find out what happened.
* **`--raw` on a success unwraps `data` unscrubbed.** `emit` writes
  `payload["data"]` straight out, so a payload carrying a live `li_at` prints it
  verbatim. That is the flag doing what it exists for - redacting a payload asked
  for explicitly reproduces the reason a hand-written script was needed
  (docs/incidents.md) - and it is opt-in, and it goes to
  stdout rather than the stderr an agent gateway keeps forever. The failure path is
  different: `--raw` there attaches `error.body`, which *is* scrubbed.

Separators here are deliberately ASCII: `--text` goes to stdout, which may be a
pipe under `LC_ALL=C`, and a decorative em dash is not worth a UnicodeEncodeError.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from .transport import scrub_secrets

REDACTED = "<redacted>"

SAFE_HEADERS: frozenset[str] = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-type",
        "user-agent",
        "x-restli-protocol-version",
        "x-li-lang",
        "referer",
    }
)


def ok(data: Any, next_cursor: str | None = None, has_more: bool = False) -> dict:
    """Wrap a successful result in the envelope."""
    return {"ok": True, "data": data, "next_cursor": next_cursor, "has_more": has_more}


def err(code: str, message: str, retryable: bool = False, body: Any = None) -> dict:
    """Wrap a failure. No `data` key at all - a null one invites reading it.

    `body` is the upstream response, and it is present only when `--raw` asked
    for it (`cli.main`). Three decisions are worth stating, because the obvious
    alternatives were all considered:

    * **Inside `error`, not beside it.** A new *top-level* key is something
      agents start depending on and something an existing parser has to be told
      about; nested under a key that only appears on failure, and only under an
      opt-in flag, it cannot change the shape any current caller reads. The
      alternative floated was an environment variable, which is worse for the
      one case this exists for: the operator is already holding the failing
      command, and adding a flag to it is one edit where adding a variable is a
      different mechanism to remember under a different name.
    * **The error survives it.** `--raw` on a *success* replaces the envelope,
      and doing the same here would trade the code an agent branches on for a
      body it cannot classify. The exit code does not move either - see
      `cli._report`, which is unchanged.
    * **Not truncated.** `transport.raise_for_status` cuts the body it splices
      into a *message* at 400 characters, because that is prose an agent has to
      read past. This is the payload, asked for explicitly; cutting it would
      reproduce the reason a hand-written script was needed in the first place
      (docs/incidents.md).
    """
    error = {"code": code, "message": message, "retryable": retryable}
    if body is not None:
        error["body"] = scrub_body(body)
    return {"ok": False, "error": error}


def scrub_body(body: Any) -> Any:
    """`body` with every credential-shaped value redacted, still readable.

    The scrubber is the transport's, deliberately: a body is exactly where a
    live `li_at` or csrf token rides out - `cli._report` puts this on stderr,
    which under an agent gateway is permanent model context - and a second redactor here
    would be a second thing to forget when a pattern is added to the first.
    `tests/test_transport.py` already checks that one against every pattern
    `tools/leakcheck.py` hunts for, and this inherits it.

    It works on text, so the body is serialized, scrubbed and read back. If the
    redaction leaves something `json` cannot parse, the scrubbed *text* is
    carried instead: a diagnostic that has to be read as a string is a nuisance,
    and an unredacted one is a live session in a transcript that cannot be taken
    back.
    """
    try:
        text = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return scrub_secrets(str(body))
    scrubbed = scrub_secrets(text)
    if scrubbed == text:
        return body
    try:
        return json.loads(scrubbed)
    except ValueError:
        return scrubbed


def redact_headers(headers: dict) -> dict:
    """Replace every header not on `SAFE_HEADERS` with a placeholder."""
    return {k: (v if k.lower() in SAFE_HEADERS else REDACTED) for k, v in headers.items()}


def emit(payload: dict, stream: TextIO, *, mode: str = "json", raw: bool = False) -> None:
    """Write `payload` to `stream` as JSON, as text, or unwrapped."""
    # --raw asks for the upstream payload, so it outranks --text; on failure there
    # is no data to unwrap and swallowing the error would be worse than verbosity.
    if raw and payload.get("ok", True):
        stream.write(json.dumps(payload.get("data"), indent=2, ensure_ascii=False) + "\n")
        return
    if mode == "text":
        stream.write(to_text(payload) + "\n")
        return
    stream.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def to_text(payload: dict) -> str:
    """Render an envelope as one indented block per item, identifier first."""
    if not payload.get("ok", True):
        e = payload.get("error") or {}
        line = f"error {e.get('code', 'unknown')}: {e.get('message', '')}".rstrip()
        if e.get("retryable"):
            line += " (retryable)"
        if "body" in e:
            # `--format=text` is a rendering choice, not a request for less
            # evidence, and the body is why `--raw` was passed. Appended rather
            # than folded into the line: it is a payload, and one flattened into
            # prose is the thing the operator went to a hand-written script for.
            line += "\n\nupstream response:\n" + json.dumps(e["body"], indent=2, ensure_ascii=False)
        return line

    body = _render_data(payload.get("data"))
    cursor = payload.get("next_cursor")
    if payload.get("has_more") and cursor:
        body += f"\n\nmore results: pass --cursor={cursor}"
    return body


# --------------------------------------------------------------------- internals


def _render_data(data: Any) -> str:
    if isinstance(data, list):
        return "\n\n".join(_block(item) for item in data) if data else "(no results)"
    if isinstance(data, dict):
        return _block(data)
    if data is None:
        return "(no data)"
    return str(data)


def _block(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    for key, renderer in _RENDERERS:
        if key in item:
            return "\n".join(renderer(item))
    return json.dumps(item, indent=2, ensure_ascii=False)


def _join(parts, sep: str) -> str:
    return sep.join(str(p) for p in parts if p not in (None, "", []))


def _name(who: Any) -> str:
    """Sender/participant fields arrive as either a projection dict or a name."""
    if isinstance(who, dict):
        return str(who.get("name") or who.get("urn") or "")
    return str(who or "")


def _count(value: Any, noun: str) -> str | None:
    if not isinstance(value, int):
        return None
    return f"{value} {noun}" if value == 1 else f"{value} {noun}s"


def _indent(text: Any, prefix: str = "  ") -> list[str]:
    return [prefix + line for line in str(text).splitlines() if line.strip()]


def _flag(item: dict) -> str:
    unread = (
        item.get("unread") if "unread" in item else (not item["read"] if "read" in item else None)
    )
    return "  [unread]" if unread else ""


def _post(item: dict) -> list[str]:
    author = item.get("author") or {}
    meta = _join(
        [
            _count(item.get("reactions"), "reaction"),
            _count(item.get("comments"), "comment"),
            _count(item.get("reshares"), "reshare"),
            item.get("posted_at"),
        ],
        " | ",
    )
    lines = [str(item.get("activity_urn") or item.get("content_urn") or "")]
    byline = _join([author.get("name"), author.get("headline")], " - ")
    if byline:
        lines.append("  " + byline)
    lines += _indent(item.get("text") or "")
    if meta:
        lines.append("  " + meta)
    if item.get("url"):
        lines.append("  " + str(item["url"]))
    return lines


def _conversation(item: dict) -> list[str]:
    last = item.get("last_message") or {}
    lines = [str(item.get("conversation_urn", "")) + _flag(item)]
    people = _join([_name(p) for p in item.get("participants") or []], ", ")
    if people:
        lines.append("  with " + people)
    tail = _join(
        [item.get("last_activity_at"), _join([_name(last.get("sender")), last.get("text")], ": ")],
        " ",
    )
    if tail:
        lines.append("  " + tail)
    return lines


def _message(item: dict) -> list[str]:
    lines = [str(item.get("message_urn", ""))]
    head = _join([_name(item.get("sender")), item.get("sent_at")], " - ")
    if head:
        lines.append("  " + head)
    lines += _indent(item.get("text") or "")
    return lines


def _notification(item: dict) -> list[str]:
    lines = [str(item.get("notification_urn", "")) + _flag(item)]
    head = _join([item.get("type"), item.get("headline")], " - ")
    if head:
        lines.append("  " + head)
    for key in ("subtext", "published_at", "url"):
        if item.get(key):
            lines.append("  " + str(item[key]))
    return lines


def _invitation(item: dict) -> list[str]:
    """A received invitation: who sent it, what they wrote, and their permalink.

    The sender leads the second line because that is the only thing a reader
    decides on - there is no accept and no ignore verb here, so the actionable
    step is looking the person up. Every field but the urn is optional on
    purpose: the populated element shape has never been observed
    (docs/sdui-migration.md), and a renderer that assumed one would fall over on
    the first real page.
    """
    sender = item.get("from") or {}
    lines = [str(item.get("invitation_urn", "")) + ("  [new]" if item.get("unseen") else "")]
    byline = _join([_name(sender), sender.get("headline")], " - ")
    if byline:
        lines.append("  " + byline)
    lines += _indent(item.get("message") or "")
    meta = _join([item.get("type"), item.get("sent_at")], " | ")
    if meta:
        lines.append("  " + meta)
    if item.get("url"):
        lines.append("  " + str(item["url"]))
    return lines


def _profile(item: dict) -> list[str]:
    lines = [str(item.get("profile_urn", ""))]
    byline = _join([item.get("name"), item.get("headline")], " - ")
    if byline:
        lines.append("  " + byline)
    facts = _join(
        [
            item.get("location"),
            _count(item.get("connections"), "connection"),
            item.get("public_id"),
        ],
        " | ",
    )
    if facts:
        lines.append("  " + facts)
    lines += _indent(item.get("about") or "")

    for label, key, fields in (
        ("experience", "experience", ("title", "company")),
        ("education", "education", ("school", "degree")),
    ):
        entries = item.get(key) or []
        if not entries:
            continue
        lines.append(f"  {label}:")
        for entry in entries:
            head = _join([entry.get(f) for f in fields], " - ")
            span = entry.get("date_range")
            lines.append("    " + _join([head, f"({span})" if span else None], " "))
    return lines


_RENDERERS = (
    ("activity_urn", _post),
    ("conversation_urn", _conversation),
    ("message_urn", _message),
    ("notification_urn", _notification),
    ("invitation_urn", _invitation),
    # Last, and it has to stay last: an invitation projects the *sender* under
    # `from`, but a renderer keyed on a nested urn would be the wrong shape, and
    # a future projection that carried `profile_urn` at the top level alongside
    # a more specific key must match the specific one.
    ("profile_urn", _profile),
)
