"""The Voyager error taxonomy: classification, redaction, retryability.

No network and no client. Every function under test derives its answer from its
arguments, so the failure modes LinkedIn actually produces are reproduced by
handing them straight to `raise_for_status`, `parse` or `classify_url` - or, for
the cases where the seam matters, by driving `browser.BrowserClient` with an
injected `request_fn`.
"""

import ast
import inspect
import json
from http.client import HTTPMessage
from pathlib import Path

import pytest

from linkedin_cli import browser, transport
from linkedin_cli.transport import (
    Blocked,
    NotFound,
    RateLimited,
    SessionExpired,
    UpstreamError,
)
from tools import leakcheck

# The two trees a resurrected urllib client could live in. The urllib-user guard
# and the client-construction guard at the bottom of this file must never drift
# apart: a guard that watches one tree and a guard that watches both is how the
# second path came back last time.
_SCANNED_TREES = (
    Path(transport.__file__).resolve().parent,
    Path(__file__).resolve().parent.parent / "tools",
)


def msg(**headers) -> HTTPMessage:
    """A real HTTPMessage, so `get_all` behaves as it does against LinkedIn."""
    m = HTTPMessage()
    for name, value in headers.items():
        for one in value if isinstance(value, (list, tuple)) else [value]:
            m[name.replace("_", "-")] = one
    return m


# ----------------------------------------------------------------------- wire contract


def test_classification_is_callable_without_a_client():
    """The browser transport holds no client object, so the taxonomy cannot live
    on one - otherwise it gets copied, and the copy drifts."""
    assert transport.parse(b'{"a": 1}', msg(), transport.BASE + "me") == {"a": 1}
    with pytest.raises(NotFound):
        transport.raise_for_status(404, b"missing", transport.BASE + "me")


def test_raise_for_status_needs_no_final_url():
    """On a plain 200 `fetch` reports no final URL distinct from the request's."""
    transport.raise_for_status(200, b"{}", transport.BASE + "me")


def test_html_for_a_voyager_path_is_session_expired():
    with pytest.raises(SessionExpired) as exc:
        transport.parse(
            b"<!DOCTYPE html><html><body>Sign in</body></html>",
            msg(Content_Type="text/html; charset=utf-8"),
            transport.BASE + "identity/profiles/me",
        )
    assert exc.value.exit_code == 3
    assert "auth seed" in str(exc.value)


def test_html_carrying_a_checkpoint_marker_is_blocked():
    """The failure the breaker exists for: a challenge served as 200 HTML at the
    URL we asked for, so neither the status nor the final URL says anything."""
    body = b'<html><body><form action="/checkpoint/challenge/verify">Verify</form></body></html>'
    with pytest.raises(Blocked) as exc:
        transport.parse(body, msg(Content_Type="text/html"), transport.BASE + "me")
    assert exc.value.exit_code == 9


def test_the_login_shells_own_checkpoint_links_do_not_read_as_a_challenge():
    """The login page posts to `/checkpoint/lg/login-submit` and links to
    `/checkpoint/rp/request-password-reset`, so scanning a body for `/checkpoint/`
    wholesale reads *every* dead session as a block. That arms the breaker on the
    one failure the operator is expected to hit routinely, tells them to clear a
    challenge that does not exist, and leaves the exit-3 branch below unreachable
    in production - the breaker then has to be cleared by hand before anything
    works again.
    """
    body = (
        b"<!DOCTYPE html><html><body><h1>Sign in</h1>"
        b'<form action="https://www.linkedin.com/checkpoint/lg/login-submit">'
        b'<a href="/checkpoint/rp/request-password-reset">Forgot password?</a>'
        b"</form></body></html>"
    )
    with pytest.raises(SessionExpired) as exc:
        transport.parse(body, msg(Content_Type="text/html"), transport.BASE + "me")
    assert exc.value.exit_code == 3
    assert "auth seed" in str(exc.value)


def test_a_checkpoint_body_still_beats_the_voyager_path():
    """...while the genuine article - a challenge rendered at the URL we asked
    for - must still outrank the path rule, or the breaker never arms."""
    for body in (
        b'<html><form action="/checkpoint/challenge/AgH">Verify</form></html>',
        b'<html><a href="/checkpoint/challengesV2/AgF">Confirm it is you</a></html>',
    ):
        with pytest.raises(Blocked) as exc:
            transport.parse(body, msg(Content_Type="text/html"), transport.BASE + "me")
        assert exc.value.exit_code == 9, body


def test_html_without_a_content_type_is_still_classified():
    """LinkedIn's interstitials do not always label themselves."""
    with pytest.raises(SessionExpired):
        transport.parse(b"  <html>Sign in</html>", msg(), transport.BASE + "me")


def test_html_off_the_voyager_tree_stays_an_upstream_error():
    """Only `/voyager/api/` is known never to serve HTML; guessing about the
    rest of linkedin.com would mis-report a page that is simply broken."""
    with pytest.raises(UpstreamError) as exc:
        transport.parse(
            b"<html>oops</html>", msg(Content_Type="text/html"), "https://www.linkedin.com/feed/"
        )
    assert exc.value.exit_code == 6


def test_a_non_json_non_html_body_is_still_an_upstream_error():
    with pytest.raises(UpstreamError) as exc:
        transport.parse(b"boom", msg(), transport.BASE + "me")
    assert "non-JSON" in str(exc.value)


def test_an_empty_body_is_still_an_empty_object():
    """A 200 with no body is what a successful write answers with."""
    assert transport.parse(b"", msg(), transport.BASE + "me") == {}


def test_classify_url_reads_a_checkpoint_as_blocked():
    for url in (
        "https://www.linkedin.com/checkpoint/challenge/AgH?ct=1",
        "https://www.linkedin.com/checkpoint/lg/login-submit",
        "/checkpoint/challengesV2/AgF",
    ):
        assert transport.classify_url(url) is Blocked, url


def test_classify_url_reads_a_challenge_that_is_not_under_checkpoint():
    """Every other case here also contains `/checkpoint/`, which makes the second
    marker dead weight no test would notice being dropped. A challenge URL that
    fell through classifies as nothing at all, exits 6, and 6 is retried."""
    for url in (
        "https://www.linkedin.com/challenge/verify",
        "https://www.linkedin.com/challengesV2/AgF?ct=1",
    ):
        assert transport.classify_url(url) is Blocked, url


def test_classify_url_reads_a_login_shell_as_session_expired():
    for url in (
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/uas/login?session_redirect=%2Ffeed",
        "https://www.linkedin.com/authwall?trk=bf",
    ):
        assert transport.classify_url(url) is SessionExpired, url


def test_classify_url_leaves_an_api_url_alone():
    assert transport.classify_url(transport.BASE + "me") is None
    assert transport.classify_url("") is None
    assert transport.classify_url(None) is None


def test_classify_url_reads_the_path_not_the_query():
    """`?session_redirect=/login` names where LinkedIn would send a *browser*
    next; the page this body came from is still the API."""
    assert transport.classify_url(transport.BASE + "me?trk=/login&x=/checkpoint/") is None


def test_a_checkpoint_final_url_is_blocked_with_no_redirect_status():
    """`fetch` follows redirects, so a challenge arrives as a plain 200 and the
    only tell is the URL the body actually came from. Classifying it solely when
    a redirect was observed leaves this exiting 6, and 6 is the code an agent
    retries - straight into a session LinkedIn has already flagged."""
    with pytest.raises(Blocked) as exc:
        transport.raise_for_status(
            200,
            b"<html>",
            transport.BASE + "me",
            final_url="https://www.linkedin.com/checkpoint/challenge/AgH",
        )
    assert exc.value.exit_code == 9
    assert "checkpoint" in str(exc.value)


def test_a_login_final_url_is_session_expired_not_upstream():
    with pytest.raises(SessionExpired) as exc:
        transport.raise_for_status(
            200, b"<html>", transport.BASE + "me", final_url="https://www.linkedin.com/uas/login"
        )
    assert exc.value.exit_code == 3
    assert "auth seed" in str(exc.value)


def test_the_final_url_outranks_the_status_code():
    """A 404 served by the authwall is a dead session, not a missing profile:
    exit 4 would send the caller looking for the wrong bug."""
    with pytest.raises(SessionExpired):
        transport.raise_for_status(
            404, b"", transport.BASE + "x", final_url="https://www.linkedin.com/authwall"
        )


def test_no_remediation_string_names_the_verb_the_pivot_deletes():
    """`auth sync` decrypted cookies out of the operator's own Chrome profile.
    The browser pivot deletes it for `auth seed`, and an error that tells an
    agent to run a command `cli.COMMANDS` no longer dispatches is worse than an
    error with no remedy at all - the agent retries it."""
    source = inspect.getsource(transport)
    assert "auth sync" not in source
    assert "auth seed" in source


def test_the_names_browser_py_imports_stay_exported():
    """browser.py builds its own previews and URLs off these, and this is now the
    only thing standing between the constant pruning and one constant too many.

    `ESSENTIAL_COOKIES`, `USER_AGENT`, `NEVER_SENT` and `REDIRECT_CODES` went
    with the urllib client, cleared by grep - but a `getattr(transport, name)`
    would have evaded that grep, so the surviving set is pinned by name here.
    `REDIRECT_CODES` is deliberately absent: it is deleted on purpose, because
    the one transport follows redirects and nothing can reach a 3xx arm.
    """
    assert transport.BASE.endswith("/voyager/api/")
    assert transport.API_PATH == "/voyager/api/"
    assert transport.REDACTED == "<redacted>"
    assert "/checkpoint/" in transport.CHALLENGE_MARKERS
    assert "user-agent" in transport.SAFE_PREVIEW_HEADERS
    assert "csrf-token" not in transport.SAFE_PREVIEW_HEADERS
    assert "GET" in transport.IDEMPOTENT_METHODS


# ------------------------------------------------------------------- body redaction
#
# The dry-run preview has always been redacted (`tests/test_browser.py`). The
# *error* path was not:
# `raise_for_status` spliced the response body into `UpstreamError`, `cli._report`
# renders that into `{"ok": false, "error": {"message": ...}}` on stderr, and under
# an agent gateway this CLI's stderr becomes permanent model context. A login, checkpoint or
# challenge body carries the csrf token - which in this system *is* the JSESSIONID
# cookie value - so the leak was a live credential, not a hint of one.


def credential_body() -> bytes:
    """A body carrying one of every credential shape LinkedIn hands back.

    Assembled from pieces rather than written out: a literal live-shaped `li_at`
    or member id in a tracked file is precisely what `tools/leakcheck.py` fails
    the build for, and the whole point of this fixture is that it looks real.
    """
    return json.dumps(
        {
            "csrf_token": "ajax:1111222233334444555",
            "li_at": "AQED" + "aB3_-" * 12,
            "miniProfile": "urn:li:fsd_profile:" + "ACoAA" + "cD4_-" * 8,
            "bcookie": "v=2&" + "a1b2c3d4e5" * 4,
            "email": "operator" + "@" + "example.com",
            "status": "CHALLENGE_REQUIRED",
        }
    ).encode()


def test_the_scrubber_removes_every_credential_shape_leakcheck_hunts_for():
    scrubbed = transport.scrub_secrets(credential_body().decode())
    assert "ajax:1111222233334444555" not in scrubbed
    assert "AQED" + "aB3_-" not in scrubbed
    assert "ACoAA" + "cD4_-" not in scrubbed
    assert "v=2&" + "a1b2c3d4e5" not in scrubbed
    assert "operator" + "@" + "example.com" not in scrubbed
    assert transport.REDACTED in scrubbed


def test_the_scrubber_keeps_the_body_diagnostic():
    """Redaction that ate the body would trade one unusable error for another:
    the operator still has to be able to tell a challenge from a bad payload."""
    scrubbed = transport.scrub_secrets(credential_body().decode())
    assert "CHALLENGE_REQUIRED" in scrubbed
    assert "csrf_token" in scrubbed
    assert "miniProfile" in scrubbed


def test_the_scrubber_leaves_an_ordinary_error_body_alone():
    """The 400 an agent debugs a payload from names fields, not credentials."""
    body = '{"message":"Unrecognized queryId messengerConversations.deadbeef"}'
    assert transport.scrub_secrets(body) == body


def test_an_error_body_carrying_the_csrf_token_never_reaches_the_message():
    with pytest.raises(UpstreamError) as exc:
        transport.raise_for_status(500, credential_body(), transport.BASE + "me")
    text = str(exc.value)
    assert "ajax:1111222233334444555" not in text
    assert "AQED" + "aB3_-" not in text
    # ...and it is still an error someone can act on.
    assert "HTTP 500" in text
    assert "CHALLENGE_REQUIRED" in text


def test_no_leakcheck_pattern_survives_into_the_error_text():
    """Pinned to the scanner itself, not to a copy of its patterns: a shape added
    to `tools/leakcheck.py` that the scrubber does not know about has to fail
    here rather than the first time a real body carries one."""
    with pytest.raises(UpstreamError) as exc:
        transport.raise_for_status(500, credential_body(), transport.BASE + "me")
    for label, pattern in leakcheck.PATTERNS:
        assert not pattern.findall(str(exc.value)), label


def test_a_non_json_body_is_scrubbed_before_it_is_reported():
    """`parse` splices the body into its own error too, and a challenge that
    answers 200 with something that is neither JSON nor HTML lands right here."""
    body = b"csrf-token=ajax:1111222233334444555&state=CHALLENGE"
    with pytest.raises(UpstreamError) as exc:
        transport.parse(body, msg(), transport.BASE + "me")
    assert "ajax:1111222233334444555" not in str(exc.value)
    assert "non-JSON" in str(exc.value)


def test_the_scrubbed_body_is_still_capped():
    """Redaction must not become a licence to print an unbounded page to stderr."""
    with pytest.raises(UpstreamError) as exc:
        transport.raise_for_status(500, b"x" * 5000, transport.BASE + "me")
    assert len(str(exc.value)) < 700


# --------------------------------------------------- redaction of the URL, not just the body
#
# The body was scrubbed and the *URL it came from* was not. `_classified` spliced
# `where` verbatim into `Blocked` and `SessionExpired`, and a checkpoint fills its
# own query string with the csrf token it wants echoed back - so the one branch
# `scrub_secrets`' docstring names by hand ("a login, checkpoint or challenge
# response carries the csrf token") was the branch that printed one.
#
# Driven through `BrowserClient` rather than `raise_for_status`, because that is
# the transport `cli` holds: `browser.py` passes the URL `fetch` landed on as
# `final_url`, which makes a classified URL the common case here rather than a
# corner of it - and, now that the redirect arm has gone with the urllib client,
# the only case there is.


class Pacer:
    """The cross-process pacer, without the clock or the file."""

    def wait_for_slot(self, min_interval: float) -> float:
        return 0.0


def landed_on(final_url: str, status: int = 200, body: str = "{}"):
    """One `BrowserClient` read whose in-page `fetch` ended at `final_url`."""
    answer = {"status": status, "body": body, "headers": {}, "url": final_url}
    client = browser.BrowserClient(state=Pacer(), request_fn=lambda payload, **kw: answer)
    return client.get("me")


# Assembled from pieces for the reason `credential_body` is: a literal live-shaped
# token in a tracked file is what `tools/leakcheck.py` fails the build for, and
# these have to look real enough for that same scanner to catch them below.
CHALLENGE_URL = "https://www.linkedin.com/checkpoint/challenge/?ct=" + "AQED" + "aB3_-" * 12
LOGIN_URL = "https://www.linkedin.com/uas/login?trk=" + "ACoAA" + "cD4_-" * 8


def test_a_challenge_url_is_scrubbed_before_it_is_named_in_the_block():
    """A `Blocked` is the message an operator is most likely to paste somewhere,
    and under an agent gateway stderr is permanent model context either way."""
    with pytest.raises(Blocked) as exc:
        landed_on(CHALLENGE_URL)
    for label, pattern in leakcheck.PATTERNS:
        assert not pattern.findall(str(exc.value)), label


def test_a_login_shell_url_is_scrubbed_before_it_is_named_in_the_expiry():
    """The other half of `_classified`, and the half an operator hits routinely:
    a login shell carries the member id in its own tracking parameters."""
    with pytest.raises(SessionExpired) as exc:
        landed_on(LOGIN_URL)
    for label, pattern in leakcheck.PATTERNS:
        assert not pattern.findall(str(exc.value)), label


def test_the_scrubbed_url_still_says_where_the_answer_came_from():
    """Per-value redaction, like the body: the path is the whole diagnostic, and
    an error that named no URL would trade one unreadable failure for another."""
    with pytest.raises(Blocked) as exc:
        landed_on(CHALLENGE_URL)
    text = str(exc.value)
    assert "/checkpoint/challenge/" in text
    assert transport.REDACTED in text
    assert "auth seed" in text


# ------------------------------------------------------- retryability of a throttle
#
# `retryable` is not advice, it is an instruction an agent branches on, and the
# `post create` payload carries no dedupe token (docs/write-payloads.md). A 503
# reported retryable on that request publishes the post twice. The transport
# already refuses to auto-retry a write on this exact status - telling the caller
# to do what the transport itself will not do is the contradiction being fixed.


def test_raise_for_status_reads_the_method_for_retryability():
    for method, expected in (("GET", True), ("HEAD", True), ("POST", False), ("DELETE", False)):
        for status in (429, 503):
            with pytest.raises(RateLimited) as exc:
                transport.raise_for_status(status, b"", transport.BASE + "me", method=method)
            assert exc.value.retryable is expected, (method, status)


def test_an_unknown_method_gets_the_conservative_answer_on_a_503():
    """No in-repo caller reaches this branch any more - the test below pins that
    - but the default it answers with is what an out-of-tree caller, or a call
    site that stops passing a method, gets. Assuming a read there would report a
    duplicate-publishing 503 as retryable."""
    with pytest.raises(RateLimited) as exc:
        transport.raise_for_status(503, b"", transport.BASE + "me")
    assert exc.value.retryable is False


def test_an_unknown_method_still_reports_a_429_as_retryable():
    """A 429 is the throttle refusing to route the request, so nothing was
    applied; and `cli` turns it into a persisted cooldown that refuses the next
    write anyway. Calling it non-retryable would strand every read."""
    with pytest.raises(RateLimited) as exc:
        transport.raise_for_status(429, b"", transport.BASE + "me")
    assert exc.value.retryable is True


def test_every_taxonomy_call_site_in_the_package_names_its_method():
    """What keeps the conservative default a *default*. A call site that stopped
    passing a method would fall back to the status alone, which renders
    `retryable: true` on a 429 for `post create` - and that omission shipped
    once already, from `browser.py`, with nothing failing to say so."""
    package = Path(transport.__file__).resolve().parent
    seen = 0
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "raise_for_status":
                continue
            seen += 1
            # `method` is the fifth parameter now that `location` has gone, so
            # five positionals reach it too. Left at six this would have accepted
            # every call site by matching none of them.
            named = any(kw.arg == "method" for kw in node.keywords) or len(node.args) >= 5
            assert named, f"{source.name}:{node.lineno} reaches the taxonomy with no method"
    # The floor. With one call site left in the package, a loop that walked,
    # filtered and found nothing would pass green - so rewriting `browser.py`'s
    # call as `raise_for_status(*args)`, or hiding it behind a differently-named
    # helper, would silently disarm this guard rather than trip it.
    assert seen == 1, f"the taxonomy guard matched {seen} call sites; browser.py should be the one"


# ------------------------------------------------------------- no second transport
#
# The browser pivot deleted `tools/acquire.py` and left its consumer behind, so
# a ~30-line file is all that stands between this package and a second,
# supervisor-free path to LinkedIn - one that bypasses `browser.py`, carries a
# raw cookie header and has the TLS fingerprint HTTP 999 detects. The three
# guards below are what turn restoring it from an easy accident into a red test.

_URLLIB_CLIENT_NAMES = ("build_opener", "OpenerDirector", "HTTPRedirectHandler")


def _scanned_modules():
    """Every `*.py` under `_SCANNED_TREES`, parsed once, with its file name."""
    for tree in _SCANNED_TREES:
        for source in sorted(tree.rglob("*.py")):
            yield source.name, ast.parse(source.read_text())


def _urllib_request_users(modules) -> set:
    """File names that reach urllib's **request** machinery, by any spelling.

    Split out of the guard below so the detector can be exercised against
    spellings that are not in the tree. The whole-package equality can only ever
    test the spellings somebody already wrote; a resurrection will be written by
    whoever is resurrecting it, so the shapes it does *not* catch are exactly the
    ones no test could reach.
    """
    users = set()
    for name, module in modules:
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                # `import urllib.request` and `import urllib.request as u` both
                # carry the dotted name. Deliberately NOT a prefix match on
                # "urllib": `transport.py` imports `urllib.parse` for `quote`.
                if any(alias.name == "urllib.request" for alias in node.names):
                    users.add(name)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "urllib.request" or (
                    node.module == "urllib" and any(a.name == "request" for a in node.names)
                ):
                    users.add(name)
            elif isinstance(node, ast.Attribute):
                if node.attr in _URLLIB_CLIENT_NAMES:
                    users.add(name)
                # `import urllib` + `urllib.request.urlopen(...)`. The import is
                # of the *package*, so no dotted name appears and every check
                # above misses it. Standalone that spelling would AttributeError
                # - but `cdp.py` does `import urllib.request`, which binds the
                # submodule onto the package object for the whole process, and
                # `bootstrap.py`/`supervisor.py` import `cdp`. So inside THIS
                # package it runs, and the exemption is what arms it.
                elif (
                    node.attr == "request"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "urllib"
                ):
                    users.add(name)
            elif isinstance(node, ast.Name) and node.id in _URLLIB_CLIENT_NAMES:
                users.add(name)
    return users


# (source, why) - every spelling that reaches a urllib opener. The last entry is
# the one that shipped undetected: the CHANGELOG and the deletion commit both
# claimed a resurrected client "fails three tests", and for this spelling it
# failed none.
_URLLIB_SPELLINGS = (
    ("import urllib.request\nurllib.request.urlopen(x)", "dotted submodule import"),
    ("import urllib.request as u\nu.urlopen(x)", "aliased submodule import"),
    ("from urllib.request import urlopen\nurlopen(x)", "from-import of the opener"),
    ("from urllib import request\nrequest.urlopen(x)", "from-import of the submodule"),
    ("from urllib.request import build_opener\nbuild_opener()", "opener factory"),
    ("import urllib\nurllib.request.urlopen(x)", "bare package import, attribute chain"),
)


@pytest.mark.parametrize("source,why", _URLLIB_SPELLINGS, ids=[w for _, w in _URLLIB_SPELLINGS])
def test_every_spelling_of_a_urllib_client_is_detected(source, why):
    """The detector is tested against source it will never otherwise see.

    Without this, `_urllib_request_users` is only ever run over files that are
    already clean, so a spelling it cannot see is indistinguishable from a
    spelling that is absent - which is how the bare-`import urllib` form passed
    the entire suite while the CHANGELOG and the deletion commit said it could
    not.
    """
    assert _urllib_request_users([("planted.py", ast.parse(source))]) == {"planted.py"}, why


def test_urllib_parse_is_not_mistaken_for_a_client():
    """`transport.py` imports `urllib.parse` for `quote` and must stay clean, so
    the detector cannot simply match the `urllib` prefix. This is the assertion
    that stops the fix above from being 'flag everything named urllib'."""
    source = "import urllib.parse\nurllib.parse.quote(s, safe='')"
    assert _urllib_request_users([("planted.py", ast.parse(source))]) == set()


def test_the_only_urllib_user_is_the_loopback_cdp_helper():
    """`cdp.py` is the exemption, and the exemption is checked rather than trusted.

    An equality, not an absence: `cdp.py` genuinely speaks urllib, to the
    loopback CDP debug port, and `bootstrap.py`/`supervisor.py` import it - so
    "no urllib in the package" is a guard that could never go green. Stated as
    a set equality it stays red both when a urllib LinkedIn client comes back
    *and* when somebody quietly widens the exemption to a second file.
    """
    assert _urllib_request_users(_scanned_modules()) == {"cdp.py"}

    # ...and why that one is safe: every `urllib.request` reference in `cdp.py`
    # is inside `_http_json`, and every call to `_http_json` targets a literal
    # `http://` prefix - i.e. plaintext loopback, which cannot be a LinkedIn
    # client. Deliberately *not* "cdp.py never mentions linkedin.com": it does,
    # over the CDP websocket, and asserting otherwise would ship red.
    cdp_source = Path(transport.__file__).resolve().parent / "cdp.py"
    cdp_module = ast.parse(cdp_source.read_text())
    helper = next(
        node
        for node in ast.walk(cdp_module)
        if isinstance(node, ast.FunctionDef) and node.name == "_http_json"
    )
    for node in ast.walk(cdp_module):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "request"
            and isinstance(node.value, ast.Name)
            and node.value.id == "urllib"
        ):
            assert helper.lineno <= node.lineno <= helper.end_lineno, node.lineno
    calls = [
        node
        for node in ast.walk(cdp_module)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_http_json"
    ]
    assert calls, "nothing calls _http_json; the exemption is no longer scoped by it"
    for call in calls:
        target = call.args[0]
        assert isinstance(target, ast.JoinedStr), ast.dump(target)
        leading = target.values[0]
        assert isinstance(leading, ast.Constant), ast.dump(target)
        assert leading.value.startswith("http://"), leading.value


def test_transport_exports_no_client():
    """The taxonomy module holds no client, and not one under another name either.

    The second clause is what makes this non-vacuous against a
    rename-instead-of-delete: `class VoyagerClient` reappearing as
    `class LegacyClient` would satisfy the first assertion alone.
    """
    assert not hasattr(transport, "VoyagerClient")
    assert [name for name in dir(transport) if name.endswith("Client")] == []


def test_the_only_voyager_client_is_the_browser_one():
    """One transport, constructed in one place.

    Scoped to `tools/` as well as the package, which is the half that makes it
    bite: restricted to `linkedin_cli/` it passes at HEAD and proves nothing,
    because `cli.py`'s `browser.BrowserClient(...)` is already the only
    construction there. `tools/resilient.py` was the live counter-example.
    Being a set equality it also has a floor: an empty set fails.
    """
    constructed = set()
    for _source, module in _scanned_modules():
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.endswith("Client"):
                constructed.add(name)
    assert constructed == {"BrowserClient"}, constructed
