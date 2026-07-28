"""HTTP client for LinkedIn's Voyager API.

Everything that talks to the network goes through here, which keeps the error
taxonomy, the pacing policy and the outcome classification in exactly one place.

The rules that matter most, all learned the hard way against the live API:

* Send **only** the six essential cookies. Several other LinkedIn cookies carry
  `;` or raw JSON in their values, which truncates the `Cookie` header and
  silently drops `li_at` - producing an auth failure that looks like anything
  but a cookie bug.
* A `3xx` whose `Location` is the request URL usually means the session is dead,
  but it is also what a client that ignored a `Set-Cookie` gets. So we absorb the
  cookies and retry exactly once; only a second self-redirect is `SessionExpired`.
  Redirects are never followed implicitly - urllib would loop until it gives up
  with "infinite loop", and a checkpoint redirect would be swallowed.
* Classification is module-level (`classify_url`, `raise_for_status`, `parse`)
  and derives everything from its arguments, because the transport that matters
  now issues its requests from inside a page and has no client object to hang it
  on. That transport's `fetch` **follows** redirects, so `Location` is always
  `None` there: a dead session or a challenge arrives as an ordinary `200` whose
  only tells are the URL the body came from and the HTML in it. Both are
  therefore checked unconditionally - a missed one exits 6, and 6 is the code an
  agent retries, against a client LinkedIn has already flagged.
* `JSESSIONID` rotates mid-session and the csrf token derives from it, so every
  `Set-Cookie` header must be read (`get_all`, not `get`) and parsed on its own,
  and rotations handed to `on_cookies_changed` for write-back.
* Pacing is cross-process. A per-process bucket resets on every CLI invocation
  and enforces nothing, so the interval is claimed from `state.State`.
* A failed write is classified, never silently retried: only a failure that
  provably predates the first byte on the wire may be sent again.
* Nothing LinkedIn sends back is printable as it arrived. A response body is
  scrubbed before it is spliced into an error, and `retryable` is answered from
  the request method rather than the status alone - see `scrub_secrets` and
  `_throttle_retryable`.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from http.cookies import SimpleCookie

BASE = "https://www.linkedin.com/voyager/api/"

# The one path on linkedin.com that is known never to serve HTML, which is what
# lets an HTML body under it be classified instead of merely reported.
API_PATH = urllib.parse.urlsplit(BASE).path

# The only cookies that may enter the Cookie header. See module docstring.
ESSENTIAL_COOKIES = ("li_at", "JSESSIONID", "liap", "lidc", "bcookie", "bscookie")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

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

# Failures that happen before the request is written: DNS never resolved, the
# TCP handshake was refused, or the certificate was rejected during the
# handshake. Nothing reached LinkedIn, so even a POST may be sent again.
NEVER_SENT = (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects to us instead of following them blindly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _never_sent(exc: BaseException) -> bool:
    """True when the failure provably predates the first byte of the request."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, NEVER_SENT)


# ----------------------------------------------------------------- classification
#
# Module-level, not methods: the browser transport issues its requests from
# inside a page and holds no client object, so a taxonomy that lived on
# `VoyagerClient` would be copied over there and the copy would answer a
# different exit code within a release of being written.


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

    `where` is scrubbed for exactly the reason the unclassified redirect below
    is - its query string is LinkedIn's to fill - and this is the branch that
    actually carries a credential: a checkpoint URL echoes the csrf token back
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
    location: str | None = None,
    final_url: str | None = None,
    method: str | None = None,
) -> None:
    """Turn a response into the one exception that names what to do about it.

    `location` is only ever set by a transport that declines redirects;
    `final_url` by one that follows them and can say where the body came from.
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

    if status in REDIRECT_CODES:
        verdict = classify_url(location)
        if verdict is not None:
            raise _classified(verdict, location or "")
        # Scrubbed like a body: an unclassified redirect is LinkedIn sending us
        # somewhere we do not recognise, and its query string is theirs to fill.
        raise UpstreamError(f"unexpected redirect to {scrub_secrets(str(location))}")

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


class VoyagerClient:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        opener=None,
        rate: float = 1.0,
        state=None,
        timeout: int = 30,
        max_retries: int = 3,
        user_agent: str = USER_AGENT,
        on_cookies_changed: Callable[[dict[str, str]], None] | None = None,
        verbose: bool = False,
    ):
        self.cookies = dict(cookies)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.verbose = verbose
        self._min_interval = (1.0 / rate) if rate else 0.0
        self._state = state
        self._on_cookies_changed = on_cookies_changed
        self._opener = opener or urllib.request.build_opener(_NoRedirect)

    # ------------------------------------------------------------------ helpers

    def _cookie_header(self) -> str:
        return "; ".join(
            f"{name}={self.cookies[name]}" for name in ESSENTIAL_COOKIES if name in self.cookies
        )

    def _csrf(self) -> str:
        return self.cookies.get("JSESSIONID", "").strip('"')

    def _headers(self, extra: dict | None = None) -> dict[str, str]:
        h = {
            "Cookie": self._cookie_header(),
            "csrf-token": self._csrf(),
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "user-agent": self.user_agent,
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate",
            "x-li-lang": "en_US",
            "referer": "https://www.linkedin.com/feed/",
        }
        if extra:
            h.update(extra)
        return h

    def _pace(self) -> None:
        if not self._min_interval:
            return
        if self._state is None:
            # Imported lazily so a client with pacing off never opens the state
            # file, and so a caller can inject its own pacer instead.
            from .state import State

            self._state = State()
        self._state.wait_for_slot(self._min_interval)

    def _absorb_cookies(self, headers) -> bool:
        """Ingest `Set-Cookie`; report whether a cookie we send actually changed.

        Each header gets its own jar: `.get()` returns one header and drops the
        rest, and joining the values on ',' mis-parses any `Expires` date.
        """
        if hasattr(headers, "get_all"):
            raw = headers.get_all("Set-Cookie") or []
        else:
            one = headers.get("Set-Cookie")
            raw = [one] if one else []

        rotated = False
        for header in raw:
            jar = SimpleCookie()
            try:
                jar.load(header)
            except Exception:
                continue
            for name, morsel in jar.items():
                # `coded_value`, not `value`: SimpleCookie unquotes, and
                # JSESSIONID's surrounding quotes are part of what LinkedIn
                # expects back in the Cookie header.
                fresh = morsel.coded_value
                if name in ESSENTIAL_COOKIES and self.cookies.get(name) != fresh:
                    rotated = True
                self.cookies[name] = fresh

        if rotated and self._on_cookies_changed:
            self._on_cookies_changed(dict(self.cookies))
        return rotated

    @staticmethod
    def _decode(body: bytes, headers) -> bytes:
        """Decompress, tolerating a truncated body so the status still reports."""
        enc = (headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in enc:
                return gzip.decompress(body)
            if "deflate" in enc:
                return zlib.decompress(body)
        except (OSError, EOFError, zlib.error):
            return body
        return body

    @staticmethod
    def _is_self_redirect(status: int, url: str, location: str | None) -> bool:
        if status not in REDIRECT_CODES or not location:
            return False
        return location.rstrip("/") == url.rstrip("/")

    # ------------------------------------------------------------------ errors
    #
    # Both shims exist only so `browser.py` can keep calling them unbound while
    # it is repointed at the module-level functions; neither adds behaviour.

    def _raise_for_status(
        self,
        status: int,
        body: bytes,
        url: str,
        location: str | None = None,
        final_url: str | None = None,
        method: str | None = None,
    ) -> None:
        raise_for_status(status, body, url, location, final_url, method)

    @staticmethod
    def _parse(payload: bytes, headers, url: str):
        return parse(payload, headers, url)

    # ------------------------------------------------------------------ request

    def _request(self, method: str, path: str, body: dict | None, dry_run: bool):
        url = path if path.startswith("http") else BASE + path
        extra = {"content-type": "application/json; charset=UTF-8"} if body is not None else None

        if dry_run:
            safe = {
                name: (value if name.lower() in SAFE_PREVIEW_HEADERS else REDACTED)
                for name, value in self._headers(extra).items()
            }
            return {"method": method, "url": url, "headers": safe, "body": body}

        data = json.dumps(body).encode() if body is not None else None
        attempt = 0
        redirect_retried = False

        while True:
            self._pace()
            # Headers are rebuilt per attempt: a Set-Cookie we just absorbed
            # changes both the Cookie header and the csrf token derived from it.
            req = urllib.request.Request(
                url, data=data, headers=self._headers(extra), method=method
            )
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    raw = resp.read()
                    resp_headers = resp.headers
            except urllib.error.HTTPError as exc:
                # With redirects disabled urllib surfaces even a 302 this way.
                status, raw, resp_headers = exc.code, exc.read(), exc.headers
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
                if _never_sent(exc):
                    if attempt >= self.max_retries:
                        raise UpstreamError(
                            f"could not connect to LinkedIn: {exc}. The request never "
                            "reached LinkedIn, so nothing was applied."
                        ) from exc
                elif method != "GET":
                    raise OutcomeUnknown(
                        f"the connection failed after the request was sent: {exc}. LinkedIn "
                        "may or may not have applied this write - check on LinkedIn before "
                        "retrying, because a blind retry can duplicate it."
                    ) from exc
                elif attempt >= self.max_retries:
                    raise UpstreamError(f"connection to LinkedIn failed: {exc}") from exc
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue

            rotated = self._absorb_cookies(resp_headers)
            payload = self._decode(raw, resp_headers)
            location = resp_headers.get("Location")

            if self._is_self_redirect(status, url, location):
                # Cookies arrived with the redirect, so the first attempt simply
                # went out without them. One retry separates that from a session
                # that is genuinely dead.
                if rotated and not redirect_retried:
                    redirect_retried = True
                    continue
                raise SessionExpired(
                    "LinkedIn redirected the request to itself"
                    + (" twice" if redirect_retried else "")
                    + ", which means the session cookies are stale. Run "
                    "`linkedin auth seed` to refresh them."
                )

            try:
                raise_for_status(status, payload, url, location, method=method)
            except RateLimited as rl:
                # One decision, not two. This used to keep its own `method ==
                # "GET"` copy of the same rule alongside `rl.retryable`; now
                # that the exception answers from the method, a second copy here
                # is just somewhere for the two to disagree later.
                if not rl.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue

            return parse(payload, resp_headers, url)

    def get(self, path: str, dry_run: bool = False):
        return self._request("GET", path, None, dry_run)

    def post(self, path: str, body: dict, dry_run: bool = False):
        return self._request("POST", path, body, dry_run)
