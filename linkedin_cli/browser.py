"""Voyager over CDP: `fetch()` evaluated inside an authenticated LinkedIn page.

The only transport there is - `get`, `post`, and the exceptions and exit codes
`transport` defines - so surfaces and `cli.py` never learn which one they hold.

The urllib client this replaced set a Chrome `user-agent`, but its TLS
fingerprint and header ordering were not Chrome's, and HTTP 999 is precisely
that detector firing.
Running the call from inside a page CloakBrowser already loaded means the
fingerprint, the cookie jar and the UA are all the browser's real ones. That is
the entire point, so the injected script sets **no** cookie and **no** user-agent
by hand; it only adds the four headers Voyager needs on top.

The classification lives in `transport` and is called from here rather than
copied: a second taxonomy would drift from the first one deploy after it was
written.

An injected `cdp_session` needs only `evaluate(expression)`, returning the
script's resolved value as plain JSON (`Runtime.evaluate` with `awaitPromise`
and `returnByValue`). `navigate(url)`, `cookies()` and `close()` are used when
present and skipped when not.
"""

from __future__ import annotations

from http.client import HTTPMessage

from . import supervisor, transport
from .transport import OutcomeUnknown, SessionExpired, UpstreamError

# Re-exported, not copied. These were a second set of literals pinned equal to
# the supervisor's by a test, and only one set was ever corrected: the copy here
# named a profile nothing had signed in to, so an operator reading this module
# was sent to the wrong directory and the CLI opened an empty one.
DEFAULT_BINARY = supervisor.DEFAULT_BINARY
DEFAULT_PROFILE = supervisor.DEFAULT_PROFILE

BINARY_ENV = supervisor.BINARY_ENV
PROFILE_ENV = supervisor.PROFILE_ENV

# Any same-origin linkedin.com document will do; the feed is the one page a
# signed-in session is guaranteed to render.
PAGE_URL = supervisor.PAGE_URL

# `%s` slots are filled with json.dumps output, which is valid JS for every value
# we pass. Nothing else is interpolated.
SCRIPT = """(async () => {
  const url = %(url)s;
  const method = %(method)s;
  const body = %(body)s;
  let token = "";
  for (const entry of document.cookie.split("; ")) {
    const eq = entry.indexOf("=");
    if (eq > 0 && entry.slice(0, eq) === "JSESSIONID") {
      token = decodeURIComponent(entry.slice(eq + 1)).replace(/^"|"$/g, "");
    }
  }
  const headers = {
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "csrf-token": token
  };
  if (body !== null) { headers["content-type"] = "application/json; charset=UTF-8"; }
  try {
    const resp = await fetch(url, {
      method: method,
      headers: headers,
      body: body,
      credentials: "include"
    });
    const text = await resp.text();
    const out = {};
    resp.headers.forEach((value, name) => { out[name] = value; });
    return {
      status: resp.status,
      body: text,
      headers: out,
      url: resp.url,
      redirected: resp.redirected
    };
  } catch (err) {
    return {error: String(err)};
  }
})()"""


JSON_TYPE = "application/json; charset=UTF-8"

# Exactly what SCRIPT adds. Kept beside it so the preview and the real request
# cannot drift; a test pins that they match.
SCRIPT_HEADERS = {
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "csrf-token": "",
}


def _safe_status(request_fn):
    """Where a real call would run, for --dry-run. Never starts a browser."""
    try:
        return request_fn({"op": "status"}, autostart=False)
    except Exception:  # noqa: BLE001 - a preview must not fail because nothing is running
        return None


def _redact(headers: dict[str, str]) -> dict[str, str]:
    """Allowlist redaction, sharing transport's set: `csrf-token` *is* the
    JSESSIONID value, so a denylist would print a live credential."""
    return {
        name: (value if name.lower() in transport.SAFE_PREVIEW_HEADERS else transport.REDACTED)
        for name, value in headers.items()
    }


def _http_message(headers) -> HTTPMessage:
    """Response headers as a case-insensitive mapping, the way urllib hands them
    to the shared parser. `fetch` lowercases every name."""
    message = HTTPMessage()
    if isinstance(headers, dict):
        for name, value in headers.items():
            message[str(name)] = str(value)
    return message


class BrowserClient:
    """A thin client over the supervisor. Holds no credential and no browser.

    Everything that used to live here - profile paths, launching, a seeded cookie
    jar to derive a csrf token from - moved behind the unix socket. That is the
    security boundary the pivot rests on: this process cannot read `li_at` even
    if it wanted to, because the only thing it can do is ask for a fetch by name.
    """

    def __init__(self, *, rate: float = 1.0, state=None, request_fn=None):
        self._min_interval = 1.0 / rate
        self._state = state
        # Injected so tests never open a socket; conftest blocks that anyway.
        self._request_fn = request_fn or supervisor.request

    def close(self) -> None:
        """Nothing to close: the browser deliberately outlives this process."""

    # ------------------------------------------------------------------ helpers

    def _pace(self) -> None:
        if self._state is None:
            from .state import State

            self._state = State()
        self._state.wait_for_slot(self._min_interval)

    def _undeliverable(self, method: str, detail: str) -> transport.VoyagerError:
        """A failure with no status: only a read can be safely called a failure."""
        if method != "GET":
            return OutcomeUnknown(
                f"the in-page request failed after it was issued: {detail}. LinkedIn may "
                "or may not have applied this write - check on LinkedIn before retrying, "
                "because a blind retry can duplicate it."
            )
        return UpstreamError(f"the in-page request failed: {detail}")

    def _rejected(self, status: int, url: str) -> str:
        """What a bare 401 here usually means, which is the wrong profile.

        The profile that ships as the default and the profile the container is
        signed in to disagreed, so an invocation without an override opened an
        empty one and LinkedIn answered 401 - reported as a dead session, whose
        named remedy was the operation that invalidated this account's session
        globally. A 401 is not evidence of a dead session; it is
        evidence that whoever answered was not signed in, and naming which
        profile answered is the difference between the two diagnoses.

        Deliberately only for a status. A login shell or a checkpoint URL is
        proof the browser reached LinkedIn and is signed out *there*, which is a
        different conversation and keeps its own message.

        Nothing in here may raise. This runs while a `SessionExpired` is being
        built, and `requested_profile()` refuses rather than defaults under a
        confined deployment (P4) - so a policy missing that key used to replace
        exit 3 with exit 6 on the way out. Exit 3 is what the breaker counts, and
        three in an hour is what makes a rotted session degrade loudly instead of
        an agent looping on it, so the refusal is folded into the text and the
        exception it interrupted is the one that leaves.
        """
        running = str((_safe_status(self._request_fn) or {}).get("profile") or "")
        try:
            wanted = supervisor.requested_profile()
        except supervisor.SupervisorError as exc:
            return (
                f"LinkedIn rejected the session ({status}) for {url}. The resident supervisor "
                f"is serving browser profile {running or '(none: nothing is running)'}, and "
                f"which profile this invocation asked for cannot be established: {exc}"
            )
        if running and running != wanted:
            where = (
                f"the resident supervisor is serving browser profile {running}, not the "
                f"{wanted} this invocation asked for - the profile binds at launch, so "
                f"{supervisor.PROFILE_ENV} was ignored. Stop that supervisor so the next "
                "call opens the right one."
            )
        else:
            where = (
                f"browser profile {running or wanted} is not signed in. Confirm that it is "
                "the profile holding the session before treating this as a dead account: "
                "re-seeding one that is still alive is what invalidated this account "
                "everywhere once already."
            )
        return (
            f"LinkedIn rejected the session ({status}) for {url}. {where} "
            "`linkedin doctor` reports which browser is running."
        )

    # ------------------------------------------------------------------ request

    def _request(self, method: str, path: str, body: dict | None, dry_run: bool):
        url = path if path.startswith("http") else transport.BASE + path

        if dry_run:
            # The preview reports where the call would run rather than inventing
            # a csrf token: the real one is read from `document.cookie` inside
            # the page and this process never sees it. Claiming to redact a value
            # we do not hold would tell an operator approving a write strictly
            # less than saying who is holding it.
            status = _safe_status(self._request_fn)
            return {
                "method": method,
                "url": url,
                "headers": _redact(SCRIPT_HEADERS | ({"content-type": JSON_TYPE} if body else {})),
                "body": body,
                "runs_in": status,
            }

        self._pace()
        try:
            result = self._request_fn({"op": "fetch", "method": method, "path": url, "body": body})
        except transport.VoyagerError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified as undeliverable below
            raise self._undeliverable(method, f"the browser supervisor failed: {exc}") from exc

        if not isinstance(result, dict):
            raise UpstreamError(
                f"the supervisor returned {type(result).__name__}, expected a result"
            )
        if result.get("error"):
            raise self._undeliverable(method, str(result["error"]))

        try:
            status = int(result["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamError(f"the supervisor returned no HTTP status for {url}") from exc

        raw = result.get("body")
        if raw is None:
            # Distinct from an empty body: a body-less non-204 means the read
            # was truncated, and reporting it as success would hand back a write
            # result with a null urn that reads exactly like a real one.
            if status != 204:
                raise self._undeliverable(method, f"the response to {url} carried no body")
            raw = ""

        payload = str(raw).encode()
        headers = _http_message(result.get("headers"))
        final = str(result.get("url") or url)

        # `final` is passed rather than dropped: fetch follows redirects, so the
        # status alone cannot tell a checkpoint or a login shell from a real
        # answer, and the URL the response actually came from is the only signal
        # left. There is no second challenge check here - one taxonomy, in
        # transport.
        #
        # `method` is passed for the same reason it exists: it is the only input
        # that keeps a throttled write from being reported retryable. Omitting it
        # left `raise_for_status` answering from the status alone, which renders
        # `retryable: true` on a 429 - and this is the transport `cli` holds, so
        # every `post create`, `invite`, `comment` and `messages send` shipped
        # with the hazard the parameter was added to close.
        try:
            transport.raise_for_status(status, payload, url, final_url=final, method=method)
        except SessionExpired as exc:
            if status not in (401, 403):
                raise
            raise SessionExpired(self._rejected(status, url)) from exc
        return transport.parse(payload, headers, url)

    def get(self, path: str, dry_run: bool = False):
        return self._request("GET", path, None, dry_run)

    def post(self, path: str, body: dict, dry_run: bool = False):
        return self._request("POST", path, body, dry_run)
