"""Voyager HTTP client: header construction, error taxonomy, pacing.

No network. A stub opener stands in for urllib so every failure mode LinkedIn
actually produces can be reproduced deterministically.
"""

import ast
import gzip
import inspect
import json
import socket
import urllib.error
from http.client import HTTPMessage
from pathlib import Path

import pytest

from linkedin_cli import browser, transport
from linkedin_cli.transport import (
    Blocked,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    SessionExpired,
    StaleQueryId,
    UpstreamError,
    VoyagerClient,
)
from tools import leakcheck

COOKIES = {
    "li_at": "AQEDATestToken",
    "JSESSIONID": '"ajax:1111222233334444555"',
    "liap": "true",
    "lidc": "b=OB01:s=O:r=O",
    "bcookie": "v=2&abc",
    "bscookie": "v=1&def",
    # deliberately noisy: values containing ';' must never reach the header
    "li_alerts": "e30=;path=/;domain=.linkedin.com",
    "UserMatchHistory": "AQK;xyz",
}


def msg(**headers) -> HTTPMessage:
    """A real HTTPMessage, so `get_all` behaves as it does against LinkedIn."""
    m = HTTPMessage()
    for name, value in headers.items():
        for one in value if isinstance(value, (list, tuple)) else [value]:
            m[name.replace("_", "-")] = one
    return m


class Resp:
    def __init__(self, status=200, body=b"{}", headers=None, url="https://www.linkedin.com/x"):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.url = url

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StubOpener:
    """Returns queued responses and records the requests it was given."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeState:
    """Stands in for the flock-guarded cross-process pacer."""

    def __init__(self):
        self.waits = []

    def wait_for_slot(self, min_interval):
        self.waits.append(min_interval)
        return 0.0


def client(responses, **kw):
    opener = StubOpener(responses)
    kw.setdefault("rate", 0.0)
    c = VoyagerClient(dict(COOKIES), opener=opener, **kw)
    return c, opener


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retry backoff is real seconds; the suite should not pay for them."""
    slept = []
    monkeypatch.setattr(transport.time, "sleep", slept.append)
    return slept


# --------------------------------------------------------------------------- headers


def test_cookie_header_contains_only_essential_cookies():
    """Regression: extra LinkedIn cookies embed ';' and truncate the header."""
    c, op = client([Resp(body=b'{"ok":1}')])
    c.get("me")
    cookie = op.requests[0].get_header("Cookie")
    names = {p.split("=")[0].strip() for p in cookie.split("; ")}
    assert names == {"li_at", "JSESSIONID", "liap", "lidc", "bcookie", "bscookie"}
    assert "li_alerts" not in cookie
    assert ";path=/" not in cookie


def test_csrf_token_is_jsessionid_without_quotes():
    c, op = client([Resp(body=b"{}")])
    c.get("me")
    assert op.requests[0].get_header("Csrf-token") == "ajax:1111222233334444555"


def test_required_voyager_headers_present():
    c, op = client([Resp(body=b"{}")])
    c.get("me")
    r = op.requests[0]
    assert r.get_header("X-restli-protocol-version") == "2.0.0"
    assert "vnd.linkedin.normalized+json" in r.get_header("Accept")
    assert "Chrome" in r.get_header("User-agent")


# --------------------------------------------------------------------------- errors


def test_self_redirect_302_without_new_cookie_is_session_expired():
    """LinkedIn's signature for a dead session: 302 to the very same URL."""
    url = "https://www.linkedin.com/voyager/api/me"
    c, op = client([Resp(status=302, headers=msg(Location=url), url=url)])
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert "auth seed" in str(exc.value)
    assert exc.value.exit_code == 3
    assert len(op.requests) == 1


def test_self_redirect_with_set_cookie_is_retried_once():
    """A self-redirect can just mean we had not applied a Set-Cookie yet."""
    url = "https://www.linkedin.com/voyager/api/me"
    redirect = Resp(
        status=302,
        headers=msg(Location=url, Set_Cookie='JSESSIONID="ajax:999"; Path=/'),
        url=url,
    )
    c, op = client([redirect, Resp(body=b'{"data":1}')])
    assert c.get("me") == {"data": 1}
    assert len(op.requests) == 2
    # the retry must carry the cookie we just learned about, csrf included
    assert 'JSESSIONID="ajax:999"' in op.requests[1].get_header("Cookie")
    assert op.requests[1].get_header("Csrf-token") == "ajax:999"


def test_second_self_redirect_is_session_expired():
    url = "https://www.linkedin.com/voyager/api/me"

    def redirect(value):
        return Resp(status=302, headers=msg(Location=url, Set_Cookie=value), url=url)

    c, op = client([redirect('JSESSIONID="ajax:1"; Path=/'), redirect('JSESSIONID="ajax:2"')])
    with pytest.raises(SessionExpired):
        c.get("me")
    assert len(op.requests) == 2


def test_401_is_session_expired():
    c, _ = client([Resp(status=401, body=b"nope")])
    with pytest.raises(SessionExpired):
        c.get("me")


def test_404_is_not_found():
    c, _ = client([Resp(status=404, body=b"missing")])
    with pytest.raises(NotFound) as exc:
        c.get("me")
    assert exc.value.exit_code == 4


def test_999_is_blocked_and_actionable():
    """LinkedIn returns a bare 999 when it decides you look automated."""
    c, op = client([Resp(status=999, body=b"")])
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9
    # A hard block, not a transient throttle: retrying digs the hole deeper.
    assert exc.value.retryable is False
    assert len(op.requests) == 1


def test_challenge_redirect_is_blocked():
    c, _ = client(
        [
            Resp(
                status=302,
                headers=msg(Location="https://www.linkedin.com/checkpoint/challenge/AgH?ct=1"),
            )
        ]
    )
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9


def test_checkpoint_redirect_is_blocked():
    c, _ = client(
        [Resp(status=303, headers=msg(Location="https://www.linkedin.com/checkpoint/lg"))]
    )
    with pytest.raises(Blocked):
        c.get("me")


def test_429_is_rate_limited_and_retryable():
    c, _ = client([Resp(status=429, headers=msg(Retry_After="0"))] * 6, max_retries=0)
    with pytest.raises(RateLimited) as exc:
        c.get("me")
    assert exc.value.exit_code == 5
    assert exc.value.retryable is True


def test_400_mentioning_queryid_is_stale_query_id():
    body = b'{"message":"Unrecognized queryId messengerConversations.deadbeef"}'
    c, _ = client([Resp(status=400, body=body)])
    with pytest.raises(StaleQueryId) as exc:
        c.get("voyagerMessagingGraphQL/graphql?queryId=messengerConversations.deadbeef")
    assert exc.value.exit_code == 7
    assert "doctor" in str(exc.value)


def test_gzipped_error_body_is_decoded_before_classification():
    body = gzip.compress(b'{"message":"Unrecognized queryId messengerMessages.dead"}')
    c, _ = client([Resp(status=400, body=body, headers=msg(Content_Encoding="gzip"))])
    with pytest.raises(StaleQueryId):
        c.get("voyagerMessagingGraphQL/graphql?queryId=messengerMessages.dead")


def test_plain_400_is_upstream_error():
    c, _ = client([Resp(status=400, body=b'{"message":"bad param"}')])
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert exc.value.exit_code == 6


def test_html_200_at_a_voyager_path_is_session_expired():
    """A login/interstitial page comes back as 200 text/html, not as an error -
    and a Voyager path never legitimately serves HTML, so this is a dead session
    rather than the exit 6 an agent would retry."""
    page = b"<!DOCTYPE html><html><head><title>LinkedIn</title></head><body>Sign in</body></html>"
    c, _ = client([Resp(body=page, headers=msg(Content_Type="text/html; charset=utf-8"))])
    with pytest.raises(SessionExpired) as exc:
        c.get("me")
    assert exc.value.exit_code == 3
    assert "auth seed" in str(exc.value)


# --------------------------------------------------------------------------- cookies


def test_set_cookie_updates_are_captured():
    """LinkedIn rotates `lidc` for datacenter routing; keep the fresh value."""
    c, _ = client([Resp(body=b"{}", headers=msg(Set_Cookie='lidc="b=NEW:s=N"; Path=/'))])
    c.get("me")
    assert "NEW" in c.cookies["lidc"]


def test_every_set_cookie_header_is_captured():
    """`headers.get()` returns one header and silently drops the rest."""
    headers = msg(Set_Cookie=['lidc="b=NEW"; Path=/', 'JSESSIONID="ajax:777"; Path=/'])
    c, _ = client([Resp(body=b"{}", headers=headers)])
    c.get("me")
    assert c.cookies["lidc"] == '"b=NEW"'
    assert c.cookies["JSESSIONID"] == '"ajax:777"'


def test_set_cookie_with_comma_in_expires_is_parsed():
    """Joining headers on ',' mis-parses Expires dates - parse each separately."""
    headers = msg(
        Set_Cookie=[
            "lidc=b=NEW:s=N; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/",
            'JSESSIONID="ajax:888"; Expires=Thu, 22 Oct 2026 07:28:00 GMT',
        ]
    )
    c, _ = client([Resp(body=b"{}", headers=headers)])
    c.get("me")
    assert c.cookies["lidc"] == "b=NEW:s=N"
    assert c.cookies["JSESSIONID"] == '"ajax:888"'


def test_rotated_jsessionid_keeps_its_quotes():
    """LinkedIn wants JSESSIONID quoted in Cookie and unquoted in csrf-token."""
    c, op = client([Resp(body=b"{}", headers=msg(Set_Cookie='JSESSIONID="ajax:222"')), Resp()])
    c.get("me")
    c.get("me")
    assert c.cookies["JSESSIONID"] == '"ajax:222"'
    assert 'JSESSIONID="ajax:222"' in op.requests[1].get_header("Cookie")
    assert op.requests[1].get_header("Csrf-token") == "ajax:222"


def test_rotated_cookies_are_reported_for_write_back():
    seen = []
    c, _ = client(
        [Resp(body=b"{}", headers=msg(Set_Cookie='JSESSIONID="ajax:new"; Path=/'))],
        on_cookies_changed=seen.append,
    )
    c.get("me")
    assert len(seen) == 1
    assert seen[0]["JSESSIONID"] == '"ajax:new"'
    assert seen[0]["li_at"] == COOKIES["li_at"]


def test_unchanged_cookies_do_not_trigger_write_back():
    seen = []
    headers = msg(Set_Cookie=f"lidc={COOKIES['lidc']}; Path=/")
    c, _ = client([Resp(body=b"{}", headers=headers)], on_cookies_changed=seen.append)
    c.get("me")
    assert seen == []


# --------------------------------------------------------------------------- pacing


def test_pacing_delegates_to_the_cross_process_state():
    """Per-process pacing is inert in a CLI; the ledger has to be shared."""
    state = FakeState()
    c, _ = client([Resp(body=b"{}"), Resp(body=b"{}")], rate=2.0, state=state)
    c.get("me")
    c.get("me")
    assert state.waits == [0.5, 0.5]


def test_rate_zero_never_consults_state():
    state = FakeState()
    c, _ = client([Resp(body=b"{}")], rate=0.0, state=state)
    c.get("me")
    assert state.waits == []


def test_each_retry_takes_its_own_slot():
    state = FakeState()
    c, _ = client(
        [Resp(status=429, headers=msg(Retry_After="0")), Resp(body=b'{"data":1}')],
        rate=1.0,
        state=state,
    )
    assert c.get("me") == {"data": 1}
    assert len(state.waits) == 2


# --------------------------------------------------------------------------- behaviour


def test_gzip_response_is_decoded():
    payload = json.dumps({"data": {"hello": "world"}}).encode()
    gz = gzip.compress(payload)
    c, _ = client([Resp(body=gz, headers=msg(Content_Encoding="gzip"))])
    assert c.get("me")["data"]["hello"] == "world"


def test_429_is_retried_then_succeeds():
    c, op = client([Resp(status=429, headers=msg(Retry_After="0")), Resp(body=b'{"data":1}')])
    assert c.get("me") == {"data": 1}
    assert len(op.requests) == 2


def test_429_gives_up_after_max_retries():
    c, _ = client([Resp(status=429, headers=msg(Retry_After="0"))] * 6, max_retries=2)
    with pytest.raises(RateLimited):
        c.get("me")


def test_writes_are_never_auto_retried():
    """A retried POST can double-send a message; refuse it."""
    c, op = client([Resp(status=429, headers=msg(Retry_After="0")), Resp(body=b"{}")])
    with pytest.raises(RateLimited):
        c.post("some/action", {"a": 1})
    assert len(op.requests) == 1


def test_post_sends_json_body_and_csrf():
    c, op = client([Resp(body=b"{}")])
    c.post("voyagerMessagingDashMessengerMessages?action=createMessage", {"x": 1})
    req = op.requests[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"x": 1}
    assert req.get_header("Content-type") == "application/json; charset=UTF-8"


# --------------------------------------------------------------------- outcome classification


def test_connection_refused_is_retried_even_for_a_write():
    """Refused means nothing was written to the socket, so a POST is safe."""
    c, op = client([urllib.error.URLError(ConnectionRefusedError(61, "refused")), Resp(body=b"{}")])
    assert c.post("some/action", {"a": 1}) == {}
    assert len(op.requests) == 2


def test_dns_failure_exhausted_is_reported_as_not_delivered():
    c, _ = client([urllib.error.URLError(socket.gaierror(8, "nodename"))] * 5, max_retries=1)
    with pytest.raises(UpstreamError) as exc:
        c.post("some/action", {"a": 1})
    assert not isinstance(exc.value, OutcomeUnknown)
    assert "never reached LinkedIn" in str(exc.value)


def test_timeout_on_a_write_is_outcome_unknown():
    """The request was already on the wire; only LinkedIn knows if it landed."""
    c, op = client([TimeoutError("timed out")])
    with pytest.raises(OutcomeUnknown) as exc:
        c.post("some/action", {"a": 1})
    assert exc.value.exit_code == 6
    assert exc.value.retryable is False
    assert "check" in str(exc.value).lower()
    assert len(op.requests) == 1


def test_connection_reset_on_a_write_is_outcome_unknown():
    c, op = client([urllib.error.URLError(ConnectionResetError(54, "reset by peer"))])
    with pytest.raises(OutcomeUnknown):
        c.post("some/action", {"a": 1})
    assert len(op.requests) == 1


def test_timeout_on_a_read_is_retried():
    """A GET has no outcome to be unknown about."""
    c, op = client([TimeoutError("timed out"), Resp(body=b'{"data":1}')])
    assert c.get("me") == {"data": 1}
    assert len(op.requests) == 2


def test_timeout_on_a_read_gives_up_as_upstream_error():
    c, _ = client([TimeoutError("timed out")] * 5, max_retries=1)
    with pytest.raises(UpstreamError):
        c.get("me")


# --------------------------------------------------------------------------- dry run


def test_dry_run_returns_request_without_sending():
    c, op = client([])
    preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    assert preview["method"] == "POST"
    assert preview["body"] == {"b": 2}
    assert op.requests == []
    # cookies must never appear in a preview an agent might print
    assert "li_at" not in json.dumps(preview)


def test_dry_run_never_leaks_the_csrf_token():
    """csrf-token *is* the JSESSIONID value: a denylist would print a credential."""
    c, _ = client([])
    preview = c.get("me", dry_run=True)
    dumped = json.dumps(preview)
    assert "ajax:1111222233334444555" not in dumped
    assert preview["headers"]["csrf-token"] == "<redacted>"
    assert preview["headers"]["Cookie"] == "<redacted>"


def test_dry_run_redaction_is_an_allowlist():
    c, _ = client([])
    preview = c.post("x/y?action=create", {"b": 2}, dry_run=True)
    for name, value in preview["headers"].items():
        if name.lower() in transport.SAFE_PREVIEW_HEADERS:
            assert value != "<redacted>"
        else:
            assert value == "<redacted>", f"{name} survived redaction"
    assert preview["headers"]["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert "Chrome" in preview["headers"]["user-agent"]


# ----------------------------------------------------------------------- wire contract


def test_classification_is_callable_without_a_client():
    """The browser transport owns no VoyagerClient, so the taxonomy cannot live
    on one - otherwise it gets copied, and the copy drifts."""
    assert transport.parse(b'{"a": 1}', msg(), transport.BASE + "me") == {"a": 1}
    with pytest.raises(NotFound):
        transport.raise_for_status(404, b"missing", transport.BASE + "me")


def test_raise_for_status_needs_neither_location_nor_final_url():
    """`fetch` reports no Location and, on a plain 200, no distinct final URL."""
    transport.raise_for_status(200, b"{}", transport.BASE + "me")


def test_the_client_classifies_through_the_module_level_functions(monkeypatch):
    """Pin the delegation: a second taxonomy inside the client would answer a
    different exit code than the browser transport within one release."""
    seen = []
    monkeypatch.setattr(transport, "raise_for_status", lambda *a, **k: seen.append(a))
    monkeypatch.setattr(transport, "parse", lambda *a, **k: {"sentinel": True})
    c, _ = client([Resp(status=418, body=b"whatever")])
    assert c.get("me") == {"sentinel": True}
    assert seen and seen[0][0] == 418


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


def test_a_redirect_to_a_login_shell_is_session_expired():
    """The transport that declines redirects only ever sees the Location."""
    with pytest.raises(SessionExpired):
        transport.raise_for_status(
            302, b"", transport.BASE + "me", location="https://www.linkedin.com/uas/login"
        )


def test_the_shim_forwards_final_url_into_the_final_url_slot():
    """`browser.py` reaches the taxonomy only through this shim, and `location`
    and `final_url` are adjacent optionals that mean opposite things. Swap them
    and a checkpoint arriving as a 200 goes back to exiting 6 with no test
    failing anywhere, because nothing else calls the shim."""
    with pytest.raises(Blocked):
        VoyagerClient._raise_for_status(
            None, 200, b"", transport.BASE + "me", None, "https://www.linkedin.com/checkpoint/lg"
        )


def test_a_checkpoint_page_served_as_200_reaches_the_caller_as_blocked():
    """End to end through the client, because this is the case the breaker
    exists for: `_request` catches `RateLimited` around `raise_for_status`, and
    a `parse` result is returned rather than raised, so a taxonomy that is right
    in isolation can still be swallowed on the way out."""
    body = b'<html><form action="/checkpoint/challenge/AgH">Verify</form></html>'
    c, _ = client([Resp(body=body, headers=msg(Content_Type="text/html"))])
    with pytest.raises(Blocked) as exc:
        c.get("me")
    assert exc.value.exit_code == 9


def test_no_remediation_string_names_the_verb_the_pivot_deletes():
    """`auth sync` decrypted cookies out of the operator's own Chrome profile.
    The browser pivot deletes it for `auth seed`, and an error that tells an
    agent to run a command `cli.COMMANDS` no longer dispatches is worse than an
    error with no remedy at all - the agent retries it."""
    source = inspect.getsource(transport)
    assert "auth sync" not in source
    assert "auth seed" in source


def test_the_names_browser_py_imports_stay_exported():
    """browser.py builds its own previews and URLs off these four."""
    assert transport.BASE.endswith("/voyager/api/")
    assert transport.REDACTED == "<redacted>"
    assert "/checkpoint/" in transport.CHALLENGE_MARKERS
    assert "user-agent" in transport.SAFE_PREVIEW_HEADERS
    assert "csrf-token" not in transport.SAFE_PREVIEW_HEADERS


# ------------------------------------------------------------------- body redaction
#
# The dry-run preview above has always been redacted. The *error* path was not:
# `raise_for_status` spliced the response body into `UpstreamError`, `cli._report`
# renders that into `{"ok": false, "error": {"message": ...}}` on stderr, and under
# an agent gateway this CLI's stderr becomes permanent model context. A login, checkpoint or
# challenge body carries the csrf token - which in this system *is* the JSESSIONID
# cookie value - so the leak was a live credential, not a hint of one.


def credential_body() -> bytes:
    """A body carrying one of every credential shape this client holds.

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


def test_an_error_body_is_scrubbed_on_the_way_out_of_the_client():
    """End to end, because the message is built in one place and rendered in
    another: the value that matters is the one `cli._report` gets handed."""
    c, _ = client([Resp(status=500, body=credential_body())])
    with pytest.raises(UpstreamError) as exc:
        c.get("me")
    assert "ajax:1111222233334444555" not in str(exc.value)


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
# corner of it. The unclassified redirect three lines further down has been
# scrubbed all along - "its query string is theirs to fill" - and this is the
# same argument about the same string.


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


def test_a_classified_redirect_location_is_scrubbed_too():
    """The transport that declines redirects reaches `_classified` by the other
    door, with `location` instead of `final_url`, and LinkedIn fills that query
    string with exactly the same thing."""
    with pytest.raises(SessionExpired) as exc:
        transport.raise_for_status(302, b"", transport.BASE + "me", location=LOGIN_URL)
    for label, pattern in leakcheck.PATTERNS:
        assert not pattern.findall(str(exc.value)), label


# ------------------------------------------------------- retryability of a throttle
#
# `retryable` is not advice, it is an instruction an agent branches on, and the
# `post create` payload carries no dedupe token (docs/write-payloads.md). A 503
# reported retryable on that request publishes the post twice. The transport
# already refuses to auto-retry a write on this exact status - telling the caller
# to do what the transport itself will not do is the contradiction being fixed.


def test_a_503_on_a_read_is_retryable():
    c, _ = client([Resp(status=503)] * 6, max_retries=0)
    with pytest.raises(RateLimited) as exc:
        c.get("me")
    assert exc.value.exit_code == 5
    assert exc.value.retryable is True


def test_a_503_on_a_write_is_not_reported_retryable():
    """The gateway may have given up *after* LinkedIn processed the request."""
    c, op = client([Resp(status=503)])
    with pytest.raises(RateLimited) as exc:
        c.post("some/action", {"a": 1})
    assert exc.value.retryable is False
    assert len(op.requests) == 1


def test_a_429_on_a_write_is_not_reported_retryable():
    c, op = client([Resp(status=429, headers=msg(Retry_After="0"))])
    with pytest.raises(RateLimited) as exc:
        c.post("some/action", {"a": 1})
    assert exc.value.retryable is False
    assert len(op.requests) == 1


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
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("raise_for_status", "_raise_for_status"):
                continue
            # `method` is the sixth parameter, so six positionals reach it too.
            named = any(kw.arg == "method" for kw in node.keywords) or len(node.args) >= 6
            assert named, f"{source.name}:{node.lineno} reaches the taxonomy with no method"


def test_the_shim_forwards_the_method_into_the_method_slot():
    """Same trap as `final_url` above: `browser.py` reaches the taxonomy through
    this shim, and a method that never arrives silently restores the default."""
    with pytest.raises(RateLimited) as exc:
        VoyagerClient._raise_for_status(None, 503, b"", transport.BASE + "me", None, None, "POST")
    assert exc.value.retryable is False
