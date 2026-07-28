"""The browser transport, exercised without a browser, a socket or a credential.

After the pivot `BrowserClient` is a thin client over the resident supervisor:
it holds no cookie jar, launches nothing, and reaches the outside world through
exactly one seam, `request_fn`. Every test below injects that seam, so what is
under test is all the class has left - URL shaping, pacing, and the
classification of whatever the resident browser hands back.

The old CDP-session tests are gone along with what they described. There is no
`cdp_session=`, no seeded jar, no `open_session` and no `cookies` property here
any more, and several tests exist specifically to keep it that way: the security
argument for the whole pivot is that this process *cannot* read `li_at`, and an
argument that nothing checks is a comment.
"""

import ast
import inspect
import json
import re

import pytest

from linkedin_cli import browser, supervisor, transport
from linkedin_cli.browser import BrowserClient
from linkedin_cli.transport import (
    Blocked,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    SessionExpired,
    StaleQueryId,
    UpstreamError,
    VoyagerError,
)

# What a leaked credential would look like. Nothing in this file ever hands one
# to the client - that is the point - so the sentinels only ever arrive from the
# supervisor side, where a real one could.
SENTINEL_LI_AT = "AQEDATestSentinelLiAtValue"
SENTINEL_JSESSIONID = '"ajax:1111222233334444555"'

ME_URL = "https://www.linkedin.com/voyager/api/me"


def page(status=200, body="{}", headers=None, url=None):
    """One in-page fetch result, shaped the way the supervisor returns it."""
    return {"status": status, "body": body, "headers": headers or {}, "url": url or ME_URL}


class FakeState:
    """The cross-process pacer, without the clock or the file."""

    def __init__(self):
        self.waits = []

    def wait_for_slot(self, min_interval):
        self.waits.append(min_interval)
        return 0.0


class Daemon:
    """Stands in for the resident supervisor: records asks, replays answers.

    Kept as a callable rather than an object with methods because that is the
    entire protocol `BrowserClient` is allowed to speak - one function, one JSON
    payload in, one JSON payload out.
    """

    def __init__(self, results=(), status=None):
        self.results = list(results)
        self.asks: list[tuple[dict, dict]] = []
        self.status = {"pid": 4242, "profile": "/managed/profile"} if status is None else status

    def __call__(self, payload, **kwargs):
        self.asks.append((payload, kwargs))
        if payload.get("op") == "status":
            if isinstance(self.status, BaseException):
                raise self.status
            return self.status
        if not self.results:
            raise AssertionError(f"the client made an unscripted request: {payload!r}")
        answer = self.results.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def ops(self):
        return [payload.get("op") for payload, _ in self.asks]

    @property
    def fetches(self):
        return [payload for payload, _ in self.asks if payload.get("op") == "fetch"]


def make(*results, rate=1.0, state=None, status=None):
    daemon = Daemon(results, status=status)
    return BrowserClient(rate=rate, state=state or FakeState(), request_fn=daemon), daemon


def code_of(module) -> str:
    """A module's source with its prose emptied out.

    The source-level checks below ask what the code *can do*, and this module's
    docstrings are all about what it deliberately does not - "cannot read
    `li_at`", "no cookie", "never starts a browser". Grepping the raw source
    would match the promise instead of the behaviour and pass forever.
    `ast.unparse` drops comments on its own.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body[0].value = ast.Constant("")
    return ast.unparse(ast.fix_missing_locations(tree))


# ------------------------------------------------------------------- round trip


def test_get_returns_the_parsed_body():
    c, daemon = make(page(body='{"data":{"plainId":42}}'))
    assert c.get("me") == {"data": {"plainId": 42}}
    assert daemon.fetches == [{"op": "fetch", "method": "GET", "path": ME_URL, "body": None}]


def test_relative_paths_become_absolute_voyager_urls():
    c, daemon = make(page())
    c.get("me")
    assert daemon.fetches[0]["path"] == transport.BASE + "me"


def test_absolute_urls_are_passed_through_untouched():
    c, daemon = make(page())
    c.get(ME_URL)
    assert daemon.fetches[0]["path"] == ME_URL


def test_post_hands_the_body_over_as_an_object():
    """Serialising it here as well as in the page would send JSON inside JSON."""
    c, daemon = make(page(body="{}"))
    c.post("voyagerMessagingDashMessengerMessages?action=createMessage", {"x": 1})
    assert daemon.fetches[0]["method"] == "POST"
    assert daemon.fetches[0]["body"] == {"x": 1}


def test_a_204_with_no_body_parses_as_an_empty_object():
    c, _ = make({"status": 204, "body": None, "headers": {}, "url": ME_URL})
    assert c.post("some/action", {"a": 1}) == {}


def test_an_empty_body_parses_as_an_empty_object():
    c, _ = make(page(body=""))
    assert c.post("some/action", {"a": 1}) == {}


def test_close_is_a_no_op_that_leaves_the_browser_running():
    """The browser deliberately outlives this process; a `close` that shut it
    down would make every invocation pay a cold start and re-arm the seeding."""
    c, daemon = make(page())
    c.get("me")
    c.close()
    c.close()
    assert daemon.ops == ["fetch"]


# ----------------------------------------------------------------- no credential


def test_the_constructor_takes_no_credential():
    """`BrowserClient(cookies, ...)` was the old shape. Nothing may hand this
    class a jar again: the credential lives in the supervisor's browser, and a
    parameter for it is all it takes for a caller to start reading one."""
    parameters = inspect.signature(BrowserClient).parameters
    assert set(parameters) == {"rate", "state", "request_fn"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())


def test_the_client_exposes_no_cookies():
    c, _ = make(page())
    assert not hasattr(c, "cookies")
    assert not any("cookie" in name.lower() for name in vars(c))


def test_the_only_ops_the_client_can_send_are_fetch_and_status():
    """The security boundary in one assertion. This process can ask for a fetch
    by name and ask where it would run; there is no op that returns a cookie, so
    no argument it can pass reaches `li_at`."""
    c, daemon = make(page(), page())
    c.get("me")
    c.post("x/y?action=create", {"a": 1})
    c.get("me", dry_run=True)
    c.post("x/y?action=create", {"a": 1}, dry_run=True)
    assert set(daemon.ops) == {"fetch", "status"}


def test_no_op_name_other_than_fetch_and_status_appears_in_the_source():
    """The runtime check above only covers the paths a test happens to walk."""
    assert set(re.findall(r"'op': '(\w+)'", code_of(browser))) == {"fetch", "status"}


def test_the_module_holds_no_credential_and_no_launcher():
    code = code_of(browser)
    assert "li_at" not in code
    assert "getCookies" not in code
    assert "Popen" not in code
    assert "subprocess" not in code
    # Pinned by name rather than counted: `request` is the one seam that leaves
    # this process, and everything else here has to be inert - a name, or an
    # environment read - so a new one appearing is a decision, not an accident.
    assert set(re.findall(r"supervisor\.(\w+)", code)) == {
        "request",
        "requested_profile",
        "PROFILE_ENV",
        "DEFAULT_BINARY",
        "DEFAULT_PROFILE",
        "BINARY_ENV",
        "PAGE_URL",
    }


def test_response_cookies_from_the_page_never_reach_the_caller():
    """A `set-cookie` on a Voyager response carries a rotated `JSESSIONID`, and
    the csrf token *is* that value. The page keeps its own jar; anything this
    process kept a copy of would be a credential it was not supposed to have."""
    c, _ = make(
        page(
            body='{"data":{"plainId":42}}',
            headers={"set-cookie": f"li_at={SENTINEL_LI_AT}; Path=/", "content-type": "text/json"},
        )
    )
    dumped = json.dumps(c.get("me"))
    assert SENTINEL_LI_AT not in dumped


def test_the_injected_script_sets_no_cookie_and_no_user_agent():
    """Both are the browser's real ones, which is the entire reason the calls
    were moved into a page; setting either by hand throws that away."""
    script = browser.SCRIPT
    assert SENTINEL_LI_AT not in script
    assert "li_at" not in script
    assert "user-agent" not in script.lower()
    assert '"cookie"' not in script
    # The token is read out of the page's own jar, never passed in.
    assert "document.cookie" in script
    assert "JSESSIONID" in script


# ------------------------------------------------------------------- the script

HEADER_BLOCK = re.search(r"const headers = \{(.*?)\};", browser.SCRIPT, re.S)


def script_header_names() -> set[str]:
    """The header names the injected fetch always sets, read off the script."""
    assert HEADER_BLOCK is not None, "the headers literal in SCRIPT was renamed"
    return set(re.findall(r'"([a-z][a-z0-9-]*)"\s*:', HEADER_BLOCK.group(1)))


def script_conditional_header_names() -> set[str]:
    """...and the ones it adds later, which is how the body's type gets in."""
    return set(re.findall(r'headers\["([a-z][a-z0-9-]*)"\]', browser.SCRIPT))


def test_the_preview_table_lists_exactly_the_headers_the_script_sets():
    """`SCRIPT_HEADERS` is what `--dry-run` shows an operator approving a write.
    It is a hand-maintained copy of a JS object literal, so nothing but this
    test stops the preview and the request from drifting apart."""
    assert script_header_names() == set(browser.SCRIPT_HEADERS)
    assert script_conditional_header_names() == {"content-type"}


def test_the_preview_table_carries_the_same_values_the_script_does():
    for name in ("accept", "x-restli-protocol-version", "x-li-lang"):
        assert f'"{name}": "{browser.SCRIPT_HEADERS[name]}"' in browser.SCRIPT
    assert browser.JSON_TYPE in browser.SCRIPT


def test_the_script_sends_the_page_session_and_nothing_else():
    assert 'credentials: "include"' in browser.SCRIPT
    assert browser.SCRIPT_HEADERS["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert browser.SCRIPT_HEADERS["x-restli-protocol-version"] == "2.0.0"


# ----------------------------------------------------------------------- dry run


def test_dry_run_previews_the_call_without_making_it():
    c, daemon = make()
    preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    assert preview["method"] == "POST"
    assert preview["url"] == transport.BASE + "x/y?action=create"
    assert preview["body"] == {"b": 2}
    assert daemon.fetches == []


def test_dry_run_previews_exactly_the_headers_the_script_would_send():
    c, _ = make()
    get_preview = c.get("me", dry_run=True)
    post_preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    assert set(get_preview["headers"]) == script_header_names()
    assert set(post_preview["headers"]) == script_header_names() | {"content-type"}


@pytest.mark.xfail(
    strict=True,
    reason="browser.py:178 branches on `if body` where the script branches on "
    "`body !== null`, so an empty-dict POST previews without the content-type "
    "the page would actually send. Not this file's to fix.",
)
def test_dry_run_previews_the_content_type_for_an_empty_body_too():
    c, _ = make()
    preview = c.post("x/y?action=create", {}, dry_run=True)
    assert "content-type" in preview["headers"]


def test_dry_run_never_invents_a_csrf_token():
    """The real token is read from `document.cookie` inside the page and this
    process never sees it. Printing a plausible-looking one would tell an
    operator approving a write that we hold a credential we do not."""
    assert browser.SCRIPT_HEADERS["csrf-token"] == ""
    c, _ = make()
    preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    assert preview["headers"]["csrf-token"] == transport.REDACTED
    dumped = json.dumps(preview)
    assert "ajax:" not in dumped
    assert SENTINEL_JSESSIONID not in dumped
    assert SENTINEL_LI_AT not in dumped


def test_dry_run_redaction_is_an_allowlist():
    """`csrf-token` *is* the JSESSIONID value, so a denylist that dropped
    `cookie` alone would still print a live credential the day one appears."""
    c, _ = make()
    preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    for name, value in preview["headers"].items():
        if name.lower() in transport.SAFE_PREVIEW_HEADERS:
            assert value != transport.REDACTED, f"{name} was redacted for no reason"
        else:
            assert value == transport.REDACTED, f"{name} survived redaction"
    assert preview["headers"]["accept"] == browser.SCRIPT_HEADERS["accept"]


def test_dry_run_reports_which_browser_would_have_run_it():
    """Instead of a redacted token: naming the process that holds the credential
    is strictly more than claiming to hide one we never had."""
    c, daemon = make(status={"pid": 4242, "profile": "/managed/profile"})
    preview = c.get("me", dry_run=True)
    assert preview["runs_in"] == {"pid": 4242, "profile": "/managed/profile"}
    assert daemon.asks == [({"op": "status"}, {"autostart": False})]


def test_dry_run_never_starts_a_supervisor():
    """A preview that launched a browser would be the one thing `--dry-run`
    promises it will not do."""
    c, daemon = make()
    c.get("me", dry_run=True)
    assert all(kwargs == {"autostart": False} for _, kwargs in daemon.asks)


def test_dry_run_still_answers_when_no_supervisor_is_running():
    c, _ = make(status=OSError("no supervisor on that socket"))
    preview = c.get("me", dry_run=True)
    assert preview["runs_in"] is None
    assert preview["method"] == "GET"


# ------------------------------------------------------------------------ pacing


def test_the_interval_is_the_reciprocal_of_the_rate():
    pacer = FakeState()
    c, _ = make(page(), page(), rate=0.5, state=pacer)
    c.get("me")
    c.get("me")
    assert pacer.waits == [2.0, 2.0]


@pytest.mark.parametrize("rate", [1.0, 0.5, 0.1, 0.001])
def test_no_accepted_rate_can_produce_an_unpaced_client(rate):
    """Pacing is the only behavioural control left after the pivot, so there
    must be no arithmetic that lands on a zero interval."""
    pacer = FakeState()
    c, _ = make(page(), rate=rate, state=pacer)
    c.get("me")
    assert pacer.waits == [1.0 / rate]
    assert all(wait > 0 for wait in pacer.waits)


def test_a_zero_rate_fails_loudly_instead_of_disabling_pacing():
    """`(1.0 / rate) if rate else 0.0` used to turn `--rate=0` into "no pacing,
    and no timestamp written either", so the *next* invocation was unpaced too.
    `cli` rejects it now; the arithmetic here refuses it as well rather than
    quietly reintroducing the hole if it ever gets past."""
    with pytest.raises(ZeroDivisionError):
        BrowserClient(rate=0, state=FakeState(), request_fn=Daemon())


def test_a_failing_request_is_paced_like_any_other():
    """An agent retrying a failure is exactly the loop pacing exists to slow."""
    pacer = FakeState()
    c, _ = make(page(status=500, body="boom"), rate=1.0, state=pacer)
    with pytest.raises(UpstreamError):
        c.get("me")
    assert pacer.waits == [1.0]


def test_dry_run_is_not_paced():
    pacer = FakeState()
    c, _ = make(rate=1.0, state=pacer)
    c.get("me", dry_run=True)
    assert pacer.waits == []


def test_the_ledger_is_only_opened_when_a_request_is_actually_made(monkeypatch):
    """Imported inside `_pace` so a preview never touches the state file."""
    built = []

    class Recorder:
        def __init__(self, *a, **kw):
            built.append(True)

        def wait_for_slot(self, min_interval):
            return 0.0

    monkeypatch.setattr("linkedin_cli.state.State", Recorder)
    c = BrowserClient(rate=1.0, request_fn=Daemon([page()]))
    assert built == []
    c.get("me")
    assert built == [True]


# ---------------------------------------------------------------- classification


def test_999_is_blocked():
    c, _ = make(page(status=999, body=""))
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9
    assert exc.value.retryable is False


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_are_a_dead_session(status):
    c, _ = make(page(status=status, body="nope"))
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert exc.value.exit_code == 3


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_session_names_the_profile_rather_than_re_seeding(status, monkeypatch):
    """A bare 401 is what an *empty* profile gets, and the shipped default named
    one nothing had ever signed in to - so the message was
    `session_expired ... Run linkedin auth seed`, indistinguishable from a real
    dead session, pointing at the operation that invalidated this account's
    session globally. Which profile answered is the whole
    diagnosis, so it is what gets said."""
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/opt/linkedin-cli/profile")
    c, _ = make(page(status=status, body="nope"), status={"pid": 4242, "profile": "/empty"})
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    message = str(exc.value)
    assert "auth seed" not in message
    assert "/empty" in message
    assert "/opt/linkedin-cli/profile" in message


def test_a_rejected_session_on_the_right_profile_says_only_that(monkeypatch):
    """No mismatch to report, so it must not invent one - the profile really is
    signed out, and that is a different conversation from the wrong profile."""
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/managed/profile")
    c, _ = make(page(status=401, body="nope"))
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    message = str(exc.value)
    assert "auth seed" not in message
    assert "/managed/profile" in message


def test_a_rejected_session_answers_even_with_no_supervisor_to_ask(monkeypatch):
    """The status probe is a diagnostic on a failure path; it must not turn the
    failure into a different one when nobody is listening."""
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/opt/linkedin-cli/profile")
    c, _ = make(page(status=401, body="nope"), status=OSError("no supervisor"))
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert "/opt/linkedin-cli/profile" in str(exc.value)


def test_the_status_probe_on_a_rejected_session_starts_no_browser():
    c, daemon = make(page(status=401, body="nope"))
    with pytest.raises(SessionExpired):
        c.get("me")
    assert daemon.asks[-1] == ({"op": "status"}, {"autostart": False})


def test_404_is_not_found():
    c, _ = make(page(status=404, body="missing"))
    with pytest.raises(NotFound) as exc:
        c.get("me")
    assert exc.value.exit_code == 4


def test_a_400_naming_the_queryid_is_a_rotation():
    body = '{"message":"Unrecognized queryId messengerConversations.deadbeef"}'
    c, _ = make(page(status=400, body=body))
    with pytest.raises(StaleQueryId) as exc:
        c.get("voyagerMessagingGraphQL/graphql?queryId=messengerConversations.deadbeef")
    assert exc.value.exit_code == 7


def test_429_is_throttling_and_is_retryable():
    c, _ = make(page(status=429))
    with pytest.raises(RateLimited) as exc:
        c.get("me")
    assert exc.value.exit_code == 5
    assert exc.value.retryable is True


def test_a_read_may_be_sent_again_after_a_503():
    """A gateway giving up on a read is the case `retryable` exists for, and
    answering it from the status alone stranded every read behind one."""
    c, _ = make(page(status=503))
    with pytest.raises(RateLimited) as exc:
        c.get("me")
    assert exc.value.retryable is True


@pytest.mark.parametrize("status", [429, 503])
def test_a_throttled_write_is_never_reported_retryable(status):
    """`retryable` is not advice - `cli._report` renders it into the envelope an
    agent branches on, and `post create` carries no dedupe token, so a retry is
    a second public post that cannot be recalled. This is the transport `cli`
    actually holds, so the method has to reach `raise_for_status` from here;
    classifying without naming one answered `429` retryable on every write."""
    c, _ = make(page(status=status))
    with pytest.raises(RateLimited) as exc:
        c.post("contentcreation/normShares", {"text": "hello"})
    assert exc.value.exit_code == 5
    assert exc.value.retryable is False


def test_a_plain_400_is_an_upstream_error():
    c, _ = make(page(status=400, body='{"message":"bad param"}'))
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert exc.value.exit_code == 6


def test_a_checkpoint_url_is_blocked_even_though_the_status_is_200():
    """`fetch` follows redirects, so a challenge arrives as an ordinary 200 and
    the URL the body came from is the only signal left. Exiting 6 here is what
    put an agent in a retry loop against a client LinkedIn had already flagged."""
    c, _ = make(
        page(
            body="<html>challenge</html>",
            url="https://www.linkedin.com/checkpoint/challenge/AgH?ct=1",
        )
    )
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9
    assert "checkpoint" in str(exc.value)


def test_a_login_url_is_a_dead_session_rather_than_a_block():
    """Only `Blocked` arms the breaker, and only `SessionExpired` tells the
    operator to re-seed. Getting these the wrong way round costs one of the two."""
    c, _ = make(page(body="<html>sign in</html>", url="https://www.linkedin.com/uas/login"))
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert exc.value.exit_code == 3
    assert "auth seed" in str(exc.value)


def test_html_on_a_voyager_path_is_a_dead_session_not_an_upstream_fault():
    """A login shell answering 200 with HTML at the requested URL is invisible to
    the URL check. A Voyager path never legitimately serves HTML, which is the
    last place left to tell a signed-out profile from a broken endpoint."""
    c, _ = make(
        page(
            body="<!DOCTYPE html><html><body>Sign in</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert exc.value.exit_code == 3


def test_a_challenge_marker_in_an_html_body_is_blocked():
    c, _ = make(
        page(
            body='<html><form action="/checkpoint/challenge/AgH">Verify</form></html>',
            headers={"content-type": "text/html"},
        )
    )
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9


def test_the_url_the_response_came_from_is_forwarded_to_the_taxonomy():
    """`raise_for_status` takes `location` and `final_url` as adjacent optionals
    that mean opposite things, and only this transport fills the second one."""
    c, _ = make(page(url="https://www.linkedin.com/checkpoint/lg/login-submit"))
    with pytest.raises(Blocked):
        c.get("me")


# --------------------------------------------------------- undeliverable requests


def test_an_in_page_fetch_failure_on_a_read_is_an_upstream_error():
    c, _ = make({"error": "TypeError: Failed to fetch"})
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert not isinstance(exc.value, OutcomeUnknown)


def test_an_in_page_fetch_failure_on_a_write_leaves_the_outcome_unknown():
    """The fetch may have reached LinkedIn before it threw, so a blind retry
    duplicates the write."""
    c, _ = make({"error": "TypeError: Failed to fetch"})
    with pytest.raises(OutcomeUnknown) as exc:
        c.post("some/action", {"a": 1})
    assert exc.value.exit_code == 6
    assert exc.value.retryable is False
    assert "check on LinkedIn before retrying" in str(exc.value)


def test_a_supervisor_that_stops_answering_a_write_leaves_the_outcome_unknown():
    c, _ = make(ConnectionResetError("the supervisor socket closed"))
    with pytest.raises(OutcomeUnknown):
        c.post("some/action", {"a": 1})


def test_a_supervisor_that_stops_answering_a_read_is_an_upstream_error():
    c, _ = make(ConnectionResetError("the supervisor socket closed"))
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert not isinstance(exc.value, OutcomeUnknown)


def test_a_body_less_response_is_a_failure_rather_than_an_empty_success():
    """A truncated read reported as success hands back a write result with a
    null urn that reads exactly like a real one."""
    c, _ = make({"status": 200, "body": None, "headers": {}, "url": ME_URL})
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert "carried no body" in str(exc.value)


def test_a_body_less_response_to_a_write_leaves_the_outcome_unknown():
    c, _ = make({"status": 200, "body": None, "headers": {}, "url": ME_URL})
    with pytest.raises(OutcomeUnknown):
        c.post("some/action", {"a": 1})


def test_a_result_that_is_not_an_object_is_an_upstream_error():
    c, _ = make("not a dict")
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert "expected a result" in str(exc.value)


def test_a_result_with_no_status_is_an_upstream_error():
    c, _ = make({"body": "{}", "headers": {}, "url": ME_URL})
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert "no HTTP status" in str(exc.value)


def test_a_taxonomy_error_from_the_seam_is_passed_through_unchanged():
    """Anything already classified must not be re-wrapped as an upstream fault:
    that would turn a 9 into a 6, which is the code an agent retries."""
    c, _ = make(Blocked("LinkedIn returned 999"))
    with pytest.raises(Blocked) as exc:
        c.post("some/action", {"a": 1})
    assert exc.value.exit_code == 9
    assert isinstance(exc.value, VoyagerError)


# ------------------------------------------------------------------- wiring


def test_the_default_seam_is_the_supervisor_socket():
    """Constructing a client must not connect - conftest would fail it if it
    did - but the seam it would use when asked has to be the real one."""
    assert BrowserClient(rate=1.0)._request_fn is supervisor.request


def test_the_launch_constants_are_the_supervisor_s_own(monkeypatch):
    """They used to be a second copy, pinned equal by a test. Equal copies are
    still two things to edit, and only one of them got edited: the shipped
    default named a profile nothing had signed in to for as long as it took
    somebody to run the CLI in the container. These are the same objects now."""
    assert browser.DEFAULT_BINARY is supervisor.DEFAULT_BINARY
    assert browser.DEFAULT_PROFILE is supervisor.DEFAULT_PROFILE
    assert browser.BINARY_ENV is supervisor.BINARY_ENV
    assert browser.PROFILE_ENV is supervisor.PROFILE_ENV
    assert browser.PAGE_URL is supervisor.PAGE_URL


def test_no_launch_path_is_written_down_twice():
    """The `is` above only covers the names that exist today; a fresh literal
    under a new name would be the same bug wearing a different hat."""
    assert "/opt/" not in code_of(browser)
