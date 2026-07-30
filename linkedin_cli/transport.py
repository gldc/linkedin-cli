"""The Voyager error taxonomy: what a LinkedIn response means, in one place.

Nothing here talks to the network. `browser.py` issues the request from inside
an authenticated page and hands the result to these functions, which keeps the
exit codes, the classification and the redaction rules in exactly one place
instead of one copy per surface.

The rules that matter most, all learned the hard way against the live API:

* Classification is module-level (`classify_url`, `raise_for_status`, `parse`)
  and derives everything from its arguments, because the transport that matters
  issues its requests from inside a page and has no client object to hang it
  on. That transport's `fetch` **follows** redirects, so `Location` is never
  seen here: a dead session or a challenge arrives as an ordinary `200` whose
  only tells are the URL the body came from and the HTML in it. Both are
  therefore checked unconditionally - a missed one exits 6, and 6 is the code an
  agent retries, against a client LinkedIn has already flagged.
* That premise is also why there is no `3xx` arm at all. `resp.status` is the
  *final* response's status, never a redirect's, and no caller can supply a
  `Location` - so a redirect arm here would be code no production path could
  reach, and the exact seam a re-added redirect-declining client would plug
  into. `tests/test_browser.py::test_the_injected_fetch_follows_redirects`
  fails the moment the injected script stops following them.
* A failed write is classified, never silently retried. What survives of that
  rule in this module is `_throttle_retryable`: a throttled *write* is never
  reported `retryable`, because `post create` carries no dedupe token and a
  second post to a real audience cannot be recalled.
* Nothing LinkedIn sends back is printable as it arrived. A response body is
  scrubbed before it is spliced into an error, and `retryable` is answered from
  the request method rather than the status alone - see `scrub_secrets` and
  `_throttle_retryable`.
"""

from __future__ import annotations

import json
import re
import urllib.parse

BASE = "https://www.linkedin.com/voyager/api/"

# The one path on linkedin.com that is known never to serve HTML, which is what
# lets an HTML body under it be classified instead of merely reported.
API_PATH = urllib.parse.urlsplit(BASE).path

# LinkedIn parks a flagged session behind one of these before it starts
# answering with 999.
CHALLENGE_MARKERS = ("/checkpoint/", "/challenge")

# ...and serves a dead one one of these instead. Distinguishing the two is the
# whole point: only `Blocked` arms the circuit breaker, and only `SessionExpired`
# tells the operator to re-seed.
SESSION_MARKERS = ("/login", "/uas/", "/authwall")

# Trap: the markers above are for URLs, and a *body* has to be read more
# narrowly. LinkedIn's ordinary login page posts to `/checkpoint/lg/login-submit`
# and links to `/checkpoint/rp/request-password-reset`, so scanning HTML for
# `/checkpoint/` matches every dead session as well as every challenge. That is
# not a conservative default: it arms the breaker on the one failure the operator
# is expected to hit routinely, sends them looking for a challenge that does not
# exist, and leaves the next command refused until they clear the breaker by
# hand. Only the challenge flow itself is a block.
CHALLENGE_BODY_MARKERS = ("/checkpoint/challenge",)

REDACTED = "<redacted>"

# Redaction is an allowlist because `csrf-token` *is* the JSESSIONID value: a
# denylist that dropped `cookie` alone would still print a live credential.
# render.py holds a mirror of this set for its own previews - importing it here
# would make transport and render circular.
SAFE_PREVIEW_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-type",
        "referer",
        "user-agent",
        "x-li-lang",
        "x-restli-protocol-version",
    }
)

# Methods whose repetition is not a second event on the account. Everything else
# is treated as a write; see `_throttle_retryable`.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# `name = value` in any of the spellings a body actually uses: `"csrf_token":"x"`,
# `li_at=x`, `JSESSIONID: x`. Name-driven rather than shape-driven because the
# values these hold have no fixed shape - `lidc` is `b=OB01:s=O:r=O` and would
# never be recognised as a credential on sight.
_SECRET_ASSIGNMENTS = re.compile(
    r"(?i)(\b(?:li_at|jsessionid|csrf[-_]?token|bcookie|bscookie|liap|lidc"
    r"|session[-_]?key|session[-_]?password|access[-_]?token)\"?\s*[:=]\s*\"?)"
    r"([^\"'\s,;&})\]]+)"
)

# ...and the same secrets carried loose, with no name in front of them: a
# challenge body embeds the member urn and the account's email in prose.
#
# The length floors are deliberately shorter than the ones in
# `tools/leakcheck.py`. That tool separates a live value from an obviously
# synthetic fixture inside this repo, where a false positive costs a reader ten
# seconds. Here the input is an unknown body from LinkedIn and a false negative
# is a live credential on stderr - which under an agent gateway becomes permanent model
# context - so anything credential-shaped goes.
_SECRET_VALUES = (
    # csrf-token *is* the JSESSIONID value; this one pattern covers both.
    re.compile(r"ajax:\d{4,}"),
    re.compile(r"AQED[A-Za-z0-9_\-]{10,}"),
    re.compile(r"ACoAA[A-Za-z0-9_\-]{10,}"),
    re.compile(r"v=2&[A-Fa-f0-9\-]{10,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
)


def scrub_secrets(text: str) -> str:
    """Redact every credential shape in `text`, leaving the rest of it readable.

    A login, checkpoint or challenge response carries the csrf token, and in
    this system the csrf token is the `JSESSIONID` cookie value - so a body
    spliced verbatim into an error is a live session handed to whoever reads
    stderr. `cli._report` renders that error straight into
    `{"ok": false, "error": {"message": ...}}`, and under an agent gateway stderr is
    permanent model context: there is no later point at which it can be taken
    back.

    Redaction is per-value, not wholesale, because the operator still has to be
    able to tell a challenge from a mistyped payload. The keys, the status words
    and the prose survive; only the values do not.
    """
    text = _SECRET_ASSIGNMENTS.sub(rf"\g<1>{REDACTED}", text)
    for pattern in _SECRET_VALUES:
        text = pattern.sub(REDACTED, text)
    return text


def _throttle_retryable(status: int, method: str | None) -> bool:
    """Whether an agent may send a throttled request again.

    `retryable` is not advice - `cli._report` renders it into the envelope an
    agent branches on. `post create` carries no dedupe token
    (docs/write-payloads.md), so a throttle reported retryable on that request
    is a second post to a real audience that cannot be recalled; the same
    hazard `surfaces/invitations.py` documents for an invitation.

    Three answers, because there are three states:

    * A known idempotent method may always be sent again - that is what
      idempotent means, and refusing it would strand every read behind a
      transient throttle.
    * A known write never may. The transport already declines to auto-retry one
      on this exact status (`_request`); telling the caller to do what the
      transport itself refuses to do is the contradiction being fixed here.
    * An unknown method is answered from the status alone. Every call site in
      this package names one - a test pins that - so this is the defensive
      default for a caller that cannot: it is what an omission falls back to,
      not what the shipped path relies on. Reading it as a description of this
      repo is what let the omission ship from `browser.py` in the first place.
      A `429` is the throttle refusing to route the request, so nothing was
      applied, and `cli` turns it into a persisted cooldown that refuses the
      next write anyway. A `503` is a gateway giving up, which it can do after
      LinkedIn has already processed the request, so it is not retryable
      without knowing the method. The asymmetry is the point: under-reporting
      costs a read one extra decision, over-reporting publishes twice.
    """
    if method is None:
        return status == 429
    return method.upper() in IDEMPOTENT_METHODS


class VoyagerError(Exception):
    exit_code = 6
    retryable = False


class SessionExpired(VoyagerError):
    exit_code = 3


class NotFound(VoyagerError):
    exit_code = 4


class RateLimited(VoyagerError):
    exit_code = 5

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class UpstreamError(VoyagerError):
    exit_code = 6


class OutcomeUnknown(VoyagerError):
    """The request was already on the wire when it failed; it may have landed."""

    exit_code = 6


class StaleQueryId(VoyagerError):
    exit_code = 7


class Blocked(VoyagerError):
    """LinkedIn flagged the client: HTTP 999 or a challenge redirect."""

    exit_code = 9


# ----------------------------------------------------------------- classification
#
# Module-level, not methods: the browser transport issues its requests from
# inside a page and holds no client object, so a taxonomy that lived on a client
# would have been copied over there, and the copy would answer a different exit
# code within a release of being written.


def classify_url(url: str | None) -> type[VoyagerError] | None:
    """What the URL a response came from says it is, or `None` for an API URL.

    Matched against the *path*: a query string routinely carries
    `session_redirect=/feed`-style targets naming a page we never landed on.

    `Blocked` outranks `SessionExpired` because a challenge page links to
    `/login` in its own chrome, and calling a block a stale session sends an
    agent into re-seed-and-retry against a client LinkedIn has already flagged.
    """
    path = urllib.parse.urlsplit(url or "").path
    if any(marker in path for marker in CHALLENGE_MARKERS):
        return Blocked
    if any(marker in path for marker in SESSION_MARKERS):
        return SessionExpired
    return None


def _classified(verdict: type[VoyagerError], where: str) -> VoyagerError:
    """The operator-facing form of a `classify_url` verdict.

    `where` is scrubbed for exactly the reason a response body is - its query
    string is LinkedIn's to fill - and this is the branch that actually
    carries a credential: a checkpoint URL echoes the csrf token back
    as `?ct=`, and a login shell carries the member id in its own tracking
    parameters. Both verdicts are reached from `final_url` on the transport
    `cli` holds, so this is the common path rather than a corner of it, and
    `cli._report` renders the message onto stderr - permanent model context
    under an agent gateway.
    """
    where = scrub_secrets(where)
    if verdict is Blocked:
        return Blocked(
            f"LinkedIn answered from a security checkpoint ({where}) rather than the API. "
            "Clear the challenge in the browser profile, then run `linkedin auth seed`. "
            "Do not keep calling the API in the meantime."
        )
    return SessionExpired(
        f"LinkedIn answered from its login shell ({where}) rather than the API, which "
        "means the session is dead. Run `linkedin auth seed`."
    )


def raise_for_status(
    status: int,
    payload: bytes,
    url: str,
    final_url: str | None = None,
    method: str | None = None,
) -> None:
    """Turn a response into the one exception that names what to do about it.

    `final_url` is where the body actually came from, and it is the only URL
    signal there is: the one transport in this package follows redirects, so
    `status` is the final response's and no caller can hand over a `Location`.
    `method` decides only whether a throttle is reported retryable, and a caller
    that omits it is answered conservatively - see `_throttle_retryable`.
    """
    text = payload.decode("utf-8", "replace")

    # Unconditional, and before the status: `fetch` follows redirects, so a
    # checkpoint or a login shell arrives as a perfectly ordinary 200 - or as a
    # 404 from a page that has no idea what a Voyager path is - and the URL the
    # body came from is the only signal left.
    landed = final_url or url
    verdict = classify_url(landed)
    if verdict is not None:
        raise _classified(verdict, landed)

    if status in (401, 403):
        raise SessionExpired(f"LinkedIn rejected the session ({status}). Run `linkedin auth seed`.")
    if status == 404:
        raise NotFound(f"not found: {url}")
    if status == 999:
        # Not a throttle: this is LinkedIn deciding the client looks
        # automated, and retrying it digs the hole deeper.
        raise Blocked(
            "LinkedIn returned 999, which means it has flagged this client as "
            "automated. Stop for a while and re-authenticate from the browser; "
            "retrying immediately makes it worse."
        )
    if status in (429, 503):
        raise RateLimited(
            f"LinkedIn throttled the request ({status}). Slow down and retry later.",
            retryable=_throttle_retryable(status, method),
        )
    # The window stays the one this check has always used. `text` is no longer
    # pre-truncated - it is scrubbed whole below - and widening the classifier
    # by accident would be a taxonomy change riding along with a redaction fix.
    if status == 400 and "queryid" in text[:400].lower():
        raise StaleQueryId(
            "LinkedIn rejected the GraphQL queryId - it rotates on their deploys. "
            "Run `linkedin doctor --refresh-query-ids` to re-discover it."
        )
    if status >= 400:
        # Scrubbed *before* the cut, not after: a credential straddling the
        # 400th character would otherwise be truncated to a prefix short enough
        # to slip under the length floors in `_SECRET_VALUES` and survive.
        raise UpstreamError(f"HTTP {status} from {url}: {scrub_secrets(text)[:400]}")


def parse(payload: bytes, headers, url: str):
    """Decode a Voyager body, or say which failure the HTML we got instead is.

    An HTML body is never an upstream fault here: a checkpoint or a login shell
    answering `200` at the requested URL is invisible to `raise_for_status`, so
    this is the last place left to tell them apart. `UpstreamError` would exit 6,
    which is the code an agent retries.
    """
    if not payload.strip():
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        ctype = (headers.get("Content-Type") or "").lower()
        if "html" not in ctype and payload.lstrip()[:1] != b"<":
            # Scrubbed for the same reason `raise_for_status` scrubs its body:
            # a challenge that answers 200 with a form-encoded blob is neither
            # JSON nor HTML and lands right here, csrf token and all.
            snippet = scrub_secrets(payload.decode("utf-8", "replace"))[:200]
            raise UpstreamError(f"non-JSON response from {url}: {snippet!r}") from exc

        # Checked before the path rule, and against CHALLENGE_BODY_MARKERS
        # rather than CHALLENGE_MARKERS - see the note on that tuple. A real
        # challenge is worth over-reporting, because a false `SessionExpired`
        # counts against the dead-session breaker and sends the operator
        # re-seeding a profile that is signed in; the login shell's own chrome
        # is not evidence of a challenge.
        text = payload.decode("utf-8", "replace").lower()
        if any(marker in text for marker in CHALLENGE_BODY_MARKERS):
            raise Blocked(
                f"LinkedIn served a security challenge page for {url}. Clear the challenge "
                "in the browser profile, then run `linkedin auth seed`. Do not keep calling "
                "the API in the meantime."
            ) from exc

        if urllib.parse.urlsplit(url).path.startswith(API_PATH):
            raise SessionExpired(
                f"LinkedIn served HTML instead of JSON for {url}, and a Voyager path never "
                "legitimately does: that is the login shell, so the session is dead. Run "
                "`linkedin auth seed`."
            ) from exc

        raise UpstreamError(
            f"LinkedIn served an HTML page instead of JSON for {url}. That is a "
            "login or interstitial page rather than an API response - run "
            "`linkedin auth seed`."
        ) from exc
