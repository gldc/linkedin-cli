"""The capture driver: interception, exact matching, and refusing a no-op run.

No browser and no pipe. A scripted fake transport stands in, and it is faithful
in the two ways that have actually cost this project a capture run:

* `recv` returns ONE already-de-framed message, exactly as
  `pipe.PipeConnection.recv` does. Nothing here is NUL-framed, so an
  implementation that re-splits what it gets back loses the message and the
  tests below see zero captures.
* An event queued by a click is delivered *before* the click's own reply, which
  is the interleaving `cdp.CDPSession.call` discards and this driver must not.
"""

from __future__ import annotations

import json
import time

import pytest

from tools import capture_payloads as capture

VOYAGER_WRITE = "https://www.linkedin.com/voyager/api/graphql?action=execute"
VOYAGER_READ = "https://www.linkedin.com/voyager/api/feed/updatesV2"
BODY = '{"variables":{"invitee":"urn:li:fsd_profile:ACoAAASYNTHETICTARGET000"}}'

# The two controls a prefix match had to choose between on one profile page:
# the profile's own Connect button, and a sidebar recommendation card for
# somebody the run was never aiming at. `startswith("Invite ")` picked the
# sidebar, and the connection request that went out was real. Every finder test
# below is that pair.
INVITE_TARGET = "Invite Ada Lovelace to connect"
INVITE_SIDEBAR = "Invite someone else entirely to connect"


class FakeCDP:
    """A scripted CDP transport with the `send`/`recv` surface of the pipe."""

    def __init__(self, results=None, on_send=None, silent=(), clock=None):
        self.sent: list[dict] = []
        self.outbox: list[dict] = []
        self.results = dict(results or {})
        self.on_send = dict(on_send or {})
        # Methods this transport never answers. Chrome does answer them; the
        # point is that nothing here may *depend* on the answer.
        self.silent = set(silent)
        # Optional, and only a test polling on a `FakeClock` needs it - see
        # `recv`.
        self._clock = clock

    def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        hook = self.on_send.get(message["method"])
        if hook is not None:
            hook(self, message)
        if message["method"] in self.silent:
            return
        result = self.results.get(message["method"], {})
        if callable(result):
            result = result(message.get("params") or {})
        self.outbox.append({"id": message["id"], "result": result})

    def recv(self, timeout=None) -> str:
        if not self.outbox:
            # A real pipe *blocks* for `timeout` before giving up, and that is
            # the only thing that makes time pass inside an armed poll: `_wait`
            # pumps rather than sleeps once interception is on, so the driver's
            # injected `sleep` is never reached. Without this a finder driven by
            # a FakeClock spins forever, and one driven by the real clock spins
            # hot for the whole 20s timeout instead of waiting.
            if self._clock is not None and timeout:
                self._clock.advance(timeout)
            raise TimeoutError("the fake transport has nothing more")
        return json.dumps(self.outbox.pop(0))

    # ------------------------------------------------------------------ helpers

    def emit(self, method: str, params: dict) -> None:
        self.outbox.append({"method": method, "params": params})

    def calls(self, method: str) -> list[dict]:
        return [m for m in self.sent if m["method"] == method]

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent]


def paused(request_id="req-1", method="POST", url=VOYAGER_WRITE, post_data=BODY):
    return {
        "requestId": request_id,
        "request": {
            "method": method,
            "url": url,
            "postData": post_data,
            "headers": {
                "cookie": "li_at=AQEDreal; JSESSIONID=ajax:1",
                "csrf-token": "ajax:1111222233334444555",
                "content-type": "application/json",
            },
        },
    }


def ax_node(name, backend, role="button"):
    return {
        # Chrome names this field backendDOMNodeId on AX nodes, not backendNodeId.
        "backendDOMNodeId": backend,
        "ignored": False,
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
    }


def centre(backend):
    return {"x": backend * 100 + 50.0, "y": backend * 10 + 20.0}


def page(nodes, on_send=None, clock=None):
    """A fake page whose `queryAXTree` ignores the `accessibleName` filter.

    Chrome does filter by exact name, but the driver must not rely on it: the
    run that invited the wrong person had the whole page's nodes in hand and
    chose one of them by prefix.
    """

    def query(params):
        role = params.get("role")
        return {"nodes": [n for n in nodes if not role or n["role"]["value"] == role]}

    def box(params):
        backend = params.get("backendNodeId")
        if not any(n["backendDOMNodeId"] == backend for n in nodes):
            return {}
        x, y = backend * 100, backend * 10
        return {
            "model": {
                "width": 100,
                "height": 40,
                "content": [x, y, x + 100, y, x + 100, y + 40, x, y + 40],
            }
        }

    return FakeCDP(
        results={
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "Accessibility.queryAXTree": query,
            "DOM.getBoxModel": box,
        },
        on_send=on_send,
        clock=clock,
    )


def click_emits(*events):
    """Deliver events on the release half, so one click produces one request."""

    def hook(conn, message):
        if (message.get("params") or {}).get("type") != "mouseReleased":
            return
        for method, params in events:
            conn.emit(method, params)

    return {"Input.dispatchMouseEvent": hook}


def driver_over(conn, *, browser=None, out_dir=None, clock=None, budget=None, sleep=None):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if budget is not None:
        kwargs["budget"] = budget
    if sleep is not None:
        kwargs["sleep"] = sleep
    return capture.Driver(
        capture.Interceptor(conn, "S1", clock=clock or time.monotonic),
        browser=browser,
        out_dir=out_dir,
        **kwargs,
    )


class FakeClock:
    """A monotonic clock the test advances by hand.

    The deadline tests are about a run that hung for eight minutes; waiting for
    that in a test would reproduce the defect rather than check the fix.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBrowser:
    def __init__(self, connection):
        self._connection = connection
        self._session = type("S", (), {"_session_id": "S1"})()
        self.visited: list[str] = []
        self.closed = False

    def _navigate(self, url):
        self.visited.append(url)

    def close(self):
        self.closed = True


# ------------------------------------------------------------------ interception


def test_arming_pauses_voyager_requests_at_the_request_stage():
    conn = FakeCDP()
    capture.Interceptor(conn, "S1").arm()
    enable = conn.calls("Fetch.enable")[0]
    assert enable["params"] == {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
    assert enable["sessionId"] == "S1"


def test_a_voyager_write_is_recorded_and_aborted():
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(capture.PAUSED, paused())
    interceptor.pump(5)

    assert [c["params"] for c in conn.calls("Fetch.failRequest")] == [
        {"requestId": "req-1", "errorReason": "Aborted"}
    ]
    assert not conn.calls("Fetch.continueRequest")
    assert [(c["method"], c["url"], c["body"]) for c in interceptor.captured] == [
        ("POST", VOYAGER_WRITE, BODY)
    ]


def test_a_voyager_delete_is_recorded_and_aborted():
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(capture.PAUSED, paused(method="DELETE", post_data=None))
    interceptor.pump(5)

    assert conn.calls("Fetch.failRequest")
    assert len(interceptor.captured) == 1


def test_a_read_is_continued_and_never_recorded():
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(capture.PAUSED, paused(method="GET", url=VOYAGER_READ, post_data=None))
    interceptor.pump(5)

    assert [c["params"] for c in conn.calls("Fetch.continueRequest")] == [{"requestId": "req-1"}]
    assert not conn.calls("Fetch.failRequest")
    assert interceptor.captured == []


def test_a_non_voyager_linkedin_post_is_aborted_not_continued():
    """Retracting an invitation posts OUTSIDE /voyager/api/.

    The old rule treated "not Voyager" as "not a write" and continued it. That
    is how a capture run retracted a real pending invitation while printing that
    it had intercepted one presence poll: the request was never paused, because
    the pattern that looked broad only covered /voyager/api/.
    """
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(
        capture.PAUSED,
        paused(method="POST", url="https://www.linkedin.com/mynetwork/invite/withdraw"),
    )
    interceptor.pump(5)

    assert conn.calls("Fetch.failRequest")
    assert not conn.calls("Fetch.continueRequest")
    assert len(interceptor.captured) == 1


def test_the_captured_headers_are_redacted():
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(capture.PAUSED, paused())
    interceptor.pump(5)

    headers = interceptor.captured[0]["headers"]
    assert headers["cookie"] == "<redacted>"
    assert headers["csrf-token"] == "<redacted>"
    assert headers["content-type"] == "application/json"


def test_a_request_paused_during_a_call_is_handled_not_discarded():
    """The reason this driver cannot be built on `cdp.CDPSession.call`."""
    conn = FakeCDP(on_send=click_emits((capture.PAUSED, paused())))
    interceptor = capture.Interceptor(conn, "S1")
    interceptor.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 1, "y": 2})

    assert len(interceptor.captured) == 1
    assert conn.calls("Fetch.failRequest")


def test_a_call_still_returns_its_own_reply():
    conn = FakeCDP(results={"DOM.getDocument": {"root": {"nodeId": 1}}})
    assert capture.Interceptor(conn, "S1").call("DOM.getDocument") == {"root": {"nodeId": 1}}


def test_an_error_reply_raises():
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    conn.outbox.append({"id": 1, "error": {"code": -32000, "message": "nope"}})
    with pytest.raises(capture.CaptureError) as exc:
        interceptor.call("Whatever.method")
    assert "nope" in str(exc.value)


# ------------------------------------------------------------------ exact naming


def test_a_control_is_never_matched_by_prefix():
    conn = page([ax_node(INVITE_SIDEBAR, 3)])
    with pytest.raises(capture.CaptureError) as exc:
        driver_over(conn).find_exact("button", INVITE_TARGET, timeout=0)

    assert INVITE_TARGET in str(exc.value)
    # The near miss is named, because that is the node an earlier run clicked.
    assert INVITE_SIDEBAR in str(exc.value)
    assert not conn.calls("DOM.getBoxModel")


def test_the_full_name_picks_the_right_control_among_several():
    conn = page([ax_node(INVITE_SIDEBAR, 3), ax_node(INVITE_TARGET, 4)])
    point = driver_over(conn).find_exact("button", INVITE_TARGET)

    assert (point["x"], point["y"]) == (centre(4)["x"], centre(4)["y"])
    assert [c["params"]["backendNodeId"] for c in conn.calls("DOM.getBoxModel")] == [4]


def test_a_role_that_does_not_match_is_not_clicked():
    conn = page([ax_node("Withdraw invitation sent to Ada Lovelace", 5, role="link")])
    with pytest.raises(capture.CaptureError):
        driver_over(conn).find_exact(
            "button", "Withdraw invitation sent to Ada Lovelace", timeout=0
        )


def test_an_optional_control_returns_none_rather_than_raising():
    conn = page([ax_node("Send", 2)])
    assert driver_over(conn).find_optional("button", "Send without a note") is None


# ------------------------------------------------------- alternative exact names

# The two spellings LinkedIn rendered for one dialog's confirm - the measurement
# that produced `find_any`. The flow that measured it is gone (see the refusal
# tests at the end of this file), but the rule it paid for governs
# `INVITE_CONFIRM`, so the pair stays here as the case that exercises it: a set
# of exact alternatives is safe in a way a prefix never is, because a dialog
# confirm is not person-specific and so cannot resolve to a stranger's card.
WITHDRAW_CONFIRMS = ("Withdraw", "Withdraw invitation")


def test_a_confirm_may_be_named_any_of_several_exact_alternatives():
    """The dialog renders `Withdraw` on some loads and `Withdraw invitation` on
    others, and refusing either was blocking the capture entirely."""
    conn = page([ax_node("Withdraw invitation", 5)])
    point = driver_over(conn).find_any("button", *WITHDRAW_CONFIRMS)
    assert point["name"] == "Withdraw invitation"
    assert (point["x"], point["y"]) == (centre(5)["x"], centre(5)["y"])


def test_the_alternatives_are_tried_in_the_order_they_are_given():
    """Two candidates on screen at once has to resolve the same way every run,
    or a capture is reproducible only by luck."""
    conn = page([ax_node("Withdraw invitation", 5), ax_node("Withdraw", 3)])
    assert driver_over(conn).find_any("button", *WITHDRAW_CONFIRMS)["name"] == "Withdraw"


def test_an_alternative_is_still_never_matched_by_prefix():
    """The whole point. `Withdraw` is a *prefix* of every card on the sent-
    invitations page, so a candidate list that relaxed into a substring match
    would withdraw whichever invitation happened to render first."""
    conn = page([ax_node("Withdraw invitation sent to Ada Lovelace", 5)])
    with pytest.raises(capture.CaptureError):
        driver_over(conn).find_any("button", *WITHDRAW_CONFIRMS, timeout=0)


def test_every_alternative_is_named_when_none_of_them_resolves():
    """A reader who has to check the live page needs to know all of what was
    asked for, not just the last thing tried."""
    conn = page([ax_node("Message Ada Lovelace", 5)])
    with pytest.raises(capture.CaptureError) as exc:
        driver_over(conn).find_any("button", *WITHDRAW_CONFIRMS, timeout=0)
    message = str(exc.value)
    for name in WITHDRAW_CONFIRMS:
        assert repr(name) in message


def test_every_alternative_is_tried_before_the_finder_waits():
    """Candidates polled one after another would spend the entire timeout on the
    first name before ever asking about the second - and a dialog that animates
    in is exactly the case this exists for."""
    conn = page([ax_node("Withdraw invitation", 5)])
    clock = FakeClock()
    waited: list[int] = []

    def sleep(seconds):
        waited.append(len(conn.calls("Accessibility.queryAXTree")))
        clock.advance(seconds)

    driver = driver_over(conn, clock=clock, sleep=sleep)
    assert driver.find_any("button", *WITHDRAW_CONFIRMS)["name"] == "Withdraw invitation"
    assert waited == [], "the finder waited before it had tried every alternative"


def test_an_optional_control_may_also_be_named_any_of_several_alternatives():
    """`flow_invite`'s note dialog is the other place candidates are tried, and
    absent is a legitimate answer there - some profiles fire straight off
    Connect - so it must still hand back `None` rather than raise."""
    conn = page([ax_node("Send now", 2)])
    driver = driver_over(conn)
    assert driver.find_optional("button", "Send", "Send now")["name"] == "Send now"
    assert driver.find_optional("button", "Send", "Send later") is None


def test_an_optional_search_prefers_the_earlier_candidate():
    """`Send without a note` is first because it is the one that skips a step,
    and that preference has to survive both spellings being on screen."""
    conn = page([ax_node("Send", 2), ax_node("Send without a note", 3)])
    found = driver_over(conn).find_optional("button", "Send without a note", "Send")
    assert found["name"] == "Send without a note"


def test_the_invite_note_dialog_is_polled_in_one_pass_not_one_timeout_each():
    """Four labels at six seconds each is twenty-four seconds of an armed
    capture's budget spent before the last one is even asked about - and the
    invite capture has already died of a budget timeout once."""
    clock = FakeClock()
    conn = page([ax_node("Send now", 2)], clock=clock)
    driver = driver_over(conn, clock=clock, sleep=clock.advance)

    assert (
        driver.find_optional("button", *capture.INVITE_CONFIRM, timeout=6.0)["name"] == "Send now"
    )
    assert clock.now == 0.0, "a later candidate waited behind an earlier one's timeout"


def test_a_single_name_is_the_same_search_as_one_alternative():
    """`find_exact` is the one-candidate case of the same loop, so the exactness
    rule cannot drift between them - there is only one implementation of it."""
    conn = page([ax_node(INVITE_SIDEBAR, 3)])
    with pytest.raises(capture.CaptureError) as exc:
        driver_over(conn).find_exact("button", INVITE_TARGET, timeout=0)
    assert INVITE_SIDEBAR in str(exc.value)


# ------------------------------------------------------------------ arming order


def test_a_click_before_arming_is_refused():
    conn = page([ax_node(INVITE_TARGET, 4)])
    driver = driver_over(conn)
    with pytest.raises(capture.CaptureError) as exc:
        driver.click(centre(4))

    assert "arm" in str(exc.value).lower()
    assert not conn.calls("Input.dispatchMouseEvent")


def test_interception_is_armed_before_the_first_click_of_a_flow(tmp_path):
    conn = page([ax_node(INVITE_TARGET, 4)], on_send=click_emits((capture.PAUSED, paused())))
    driver = driver_over(conn, out_dir=tmp_path)
    with driver.capture("invite"):
        driver.click(driver.find_exact("button", INVITE_TARGET))

    methods = conn.methods()
    assert methods.index("Fetch.enable") < methods.index("Input.dispatchMouseEvent")


def test_navigation_while_armed_is_refused():
    """`Browser._navigate` reads the pipe through `CDPSession`, which would
    discard the paused requests and hang the page."""
    conn = page([])
    browser = FakeBrowser(conn)
    driver = driver_over(conn, browser=browser)
    driver.interceptor.arm()
    with pytest.raises(capture.CaptureError):
        driver.navigate("https://www.linkedin.com/feed/")
    assert browser.visited == []


# ------------------------------------------------------------------ a run's verdict


def test_a_flow_that_captures_nothing_is_a_failure(tmp_path):
    conn = page([ax_node(INVITE_TARGET, 4)])
    driver = driver_over(conn, out_dir=tmp_path)
    with pytest.raises(capture.CaptureError) as exc:
        with driver.capture("invite"):
            driver.click(driver.find_exact("button", INVITE_TARGET))

    assert "nothing" in str(exc.value).lower()
    assert list(tmp_path.iterdir()) == []


def test_a_body_the_browser_withheld_is_not_reported_as_captured(tmp_path):
    """`postData` is absent on a paused request whose body Chrome did not
    deliver, and a record of the URL alone teaches nothing."""
    withheld = paused(post_data=None)
    withheld["request"]["hasPostData"] = True
    conn = page([ax_node(INVITE_TARGET, 4)], on_send=click_emits((capture.PAUSED, withheld)))
    driver = driver_over(conn, out_dir=tmp_path)

    with pytest.raises(capture.CaptureError) as exc:
        with driver.capture("invite"):
            driver.click(driver.find_exact("button", INVITE_TARGET))

    assert "body" in str(exc.value).lower()
    assert conn.calls("Fetch.failRequest")
    assert list(tmp_path.iterdir()) == []


def test_a_write_that_carries_no_body_at_all_is_still_a_capture(tmp_path):
    """A DELETE has none, which is not the same as one being withheld.

    A rule of the interceptor rather than of any one flow, so it is driven under
    a bare capture name: the flow that first produced a bodyless write no longer
    runs.
    """
    conn = page(
        [ax_node("Confirm", 4)],
        on_send=click_emits((capture.PAUSED, paused(method="DELETE", post_data=None))),
    )
    driver = driver_over(conn, out_dir=tmp_path)
    with driver.capture("probe"):
        driver.click(driver.find_exact("button", "Confirm"))

    assert driver.payloads["probe"][0]["method"] == "DELETE"


def test_a_failed_flow_still_disables_interception(tmp_path):
    conn = page([], on_send=None)
    driver = driver_over(conn, out_dir=tmp_path)
    with pytest.raises(capture.CaptureError):
        with driver.capture("invite"):
            driver.find_exact("button", INVITE_TARGET, timeout=0)

    assert conn.calls("Fetch.disable")
    assert driver.interceptor.armed is False


def test_a_capture_writes_the_payload_and_reports_it(tmp_path):
    conn = page([ax_node(INVITE_TARGET, 4)], on_send=click_emits((capture.PAUSED, paused())))
    driver = driver_over(conn, out_dir=tmp_path)
    with driver.capture("invite"):
        driver.click(driver.find_exact("button", INVITE_TARGET))

    written = json.loads((tmp_path / "invite.json").read_text())
    assert [(w["method"], w["body"]) for w in written] == [("POST", BODY)]
    assert written[0]["headers"]["cookie"] == "<redacted>"
    assert driver.payloads["invite"] == written


# ------------------------------------------------------- what gets paused, and cost
#
# A profile page issues a great many voyager reads, and the invite capture timed
# out after eight minutes without reaching its click. Everything in this section
# is about making that page affordable *without* letting a write past.


def test_arming_still_defaults_to_the_broad_voyager_pattern():
    """The default is the safe one. A narrower set is something a flow opts into
    knowing which endpoints it expects; a flow that says nothing gets everything
    paused, because a write on an endpoint nobody predicted is exactly the one
    that must not reach LinkedIn."""
    conn = FakeCDP()
    capture.Interceptor(conn, "S1").arm()
    assert conn.calls("Fetch.enable")[0]["params"] == {"patterns": capture.PATTERNS}


def test_a_flow_can_narrow_what_is_paused_to_the_endpoints_it_expects():
    conn = FakeCDP()
    narrow = [{"urlPattern": "*/voyager/api/relationships/*", "requestStage": "Request"}]
    capture.Interceptor(conn, "S1").arm(narrow)
    assert conn.calls("Fetch.enable")[0]["params"] == {"patterns": narrow}


@pytest.mark.parametrize(
    "patterns",
    [
        [],
        [{"urlPattern": "*/li/track*", "requestStage": "Request"}],
        [
            {"urlPattern": "*/voyager/api/relationships/*", "requestStage": "Request"},
            {"urlPattern": "*://example.com/*", "requestStage": "Request"},
        ],
    ],
)
def test_a_narrowing_that_would_let_a_voyager_write_through_is_refused(patterns):
    """Narrowing is a performance lever and must not become a safety hole. An
    empty list pauses nothing at all; a pattern off `/voyager/api/` leaves every
    real write unpaused while the run still reports itself as intercepting."""
    conn = FakeCDP()
    with pytest.raises(capture.CaptureError) as exc:
        capture.Interceptor(conn, "S1").arm(patterns)
    assert "voyager" in str(exc.value).lower()
    assert not conn.calls("Fetch.enable")


def test_a_pattern_at_the_response_stage_is_refused():
    """`requestStage: Response` pauses the request *after* it has been answered,
    which means it already reached LinkedIn. A capture armed that way would print
    a body and a captured payload for a write that really happened - the precise
    failure this whole tool exists to prevent."""
    conn = FakeCDP()
    with pytest.raises(capture.CaptureError) as exc:
        capture.Interceptor(conn, "S1").arm(
            [{"urlPattern": "*/voyager/api/*", "requestStage": "Response"}]
        )
    assert "Request" in str(exc.value)
    assert not conn.calls("Fetch.enable")


def test_the_shipped_example_narrowing_passes_its_own_validation():
    """`GRAPHQL_PATTERNS` is the worked example a flow copies. An example that
    would be refused by `arm` teaches the wrong shape."""
    assert capture.check_patterns(capture.GRAPHQL_PATTERNS) == capture.GRAPHQL_PATTERNS


def test_a_flow_declares_its_narrowing_in_one_place(tmp_path):
    """`FLOW_PATTERNS` is what `capture()` consults, so declaring one there is
    enough - the flow body does not have to be edited to opt in."""
    conn = page([ax_node(INVITE_TARGET, 4)], on_send=click_emits((capture.PAUSED, paused())))
    driver = driver_over(conn, out_dir=tmp_path)
    narrow = [{"urlPattern": "*/voyager/api/graphql*", "requestStage": "Request"}]
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(capture.FLOW_PATTERNS, "invite", narrow)
        with driver.capture("invite"):
            driver.click(driver.find_exact("button", INVITE_TARGET))

    assert conn.calls("Fetch.enable")[0]["params"] == {"patterns": narrow}


def test_narrowing_does_not_change_that_a_voyager_write_is_aborted():
    """Rule 4 is a property of the handler, not of the pattern set."""
    conn = FakeCDP()
    interceptor = capture.Interceptor(conn, "S1")
    interceptor.arm([{"urlPattern": "*/voyager/api/relationships/*", "requestStage": "Request"}])
    conn.emit(capture.PAUSED, paused())
    interceptor.pump(5)

    assert conn.calls("Fetch.failRequest")
    assert not conn.calls("Fetch.continueRequest")


def test_resuming_a_paused_read_never_waits_for_its_reply():
    """One paused read must cost one message out and nothing else.

    The transport is scripted to never answer `Fetch.continueRequest`, which is
    the only way to tell "sent it" apart from "sent it and waited": both look
    identical against a fake that replies. Awaiting a reply per paused request
    turns a page issuing hundreds of reads into a round-trip each, and that is
    the cost that has to stay off the page-load path.
    """
    conn = FakeCDP(silent=["Fetch.continueRequest"])
    interceptor = capture.Interceptor(conn, "S1")
    for i in range(50):
        conn.emit(capture.PAUSED, paused(request_id=f"r{i}", method="GET", url=VOYAGER_READ))
    interceptor.pump(5)

    assert len(conn.calls("Fetch.continueRequest")) == 50
    # Nothing is left waiting: an entry per resumed request would grow without
    # bound over a page load and is the shape of a leak.
    assert interceptor._awaiting == set()


def test_aborting_a_paused_write_never_waits_for_its_reply_either():
    """Same property on the write half, and it matters more: this is the message
    that stops the request, so it must go out whatever the reply does."""
    conn = FakeCDP(silent=["Fetch.failRequest"])
    interceptor = capture.Interceptor(conn, "S1")
    conn.emit(capture.PAUSED, paused())
    interceptor.pump(5)

    assert [c["params"] for c in conn.calls("Fetch.failRequest")] == [
        {"requestId": "req-1", "errorReason": "Aborted"}
    ]
    assert len(interceptor.captured) == 1
    assert interceptor._awaiting == set()


def test_finding_a_control_resolves_the_box_of_only_the_node_that_matched():
    """The single biggest cost on a heavy page. `DOM.getBoxModel` is one CDP
    round-trip per node, so it runs only after the accessible name has already
    picked the node out - never once per candidate the page returned."""
    nodes = [ax_node(f"Follow {i}", i) for i in range(2, 60)] + [ax_node(INVITE_TARGET, 99)]
    conn = page(nodes)
    point = driver_over(conn).find_exact("button", INVITE_TARGET)

    assert (point["x"], point["y"]) == (centre(99)["x"], centre(99)["y"])
    assert [c["params"]["backendNodeId"] for c in conn.calls("DOM.getBoxModel")] == [99]


def test_the_document_root_is_fetched_once_across_repeated_lookups():
    """`DOM.getDocument` was re-issued for every role of every poll. It is one
    round-trip that answers the same thing each time within a document."""
    conn = page([ax_node("Start a post", 2), ax_node("Post", 6)])
    driver = driver_over(conn)
    driver.find_exact("button", "Start a post")
    driver.find_exact("button", "Post")

    assert len(conn.calls("DOM.getDocument")) == 1


def test_navigating_re_reads_the_document_root():
    """The cache is per document. Keeping a stale root across a navigation would
    query the accessibility tree of a page that is no longer there."""
    conn = page([ax_node("Start a post", 2)])
    driver = driver_over(conn, browser=FakeBrowser(conn))
    driver.find_exact("button", "Start a post")
    driver.navigate("https://www.linkedin.com/feed/")
    driver.find_exact("button", "Start a post")

    assert len(conn.calls("DOM.getDocument")) == 2


def test_an_optional_control_that_is_absent_does_not_enumerate_the_page_twice():
    """`find_optional` throws its diagnostic away, so building one costs a second
    full unfiltered sweep of the accessibility tree for nothing. The invite flow
    hits this on every run - most profiles show no "Send without a note" step."""
    conn = page([ax_node("Send", 2)])
    assert driver_over(conn).find_optional("button", "Send without a note") is None
    assert len(conn.calls("Accessibility.queryAXTree")) == 1


def test_a_missing_required_control_still_pays_for_its_near_miss_diagnostic():
    """The counterpart, and it is worth the extra sweep: this message is what
    named the sidebar card an earlier run clicked by prefix. `find_exact`'s
    query is filtered by accessible name, so the near misses genuinely are not
    in hand and have to be asked for."""
    conn = page([ax_node(INVITE_SIDEBAR, 3)])
    with pytest.raises(capture.CaptureError) as exc:
        driver_over(conn).find_exact("button", INVITE_TARGET, timeout=0)

    assert INVITE_SIDEBAR in str(exc.value)
    assert len(conn.calls("Accessibility.queryAXTree")) == 2


def test_waiting_for_a_control_reads_the_pipe_instead_of_sleeping_on_it():
    """The defect behind the eight-minute run. While armed, every voyager request
    the page issues is paused and waits on *this* process to resume it. A poll
    loop that sleeps for a second is a second in which nothing is drained: the
    page stalls on requests nobody is answering, so the control never renders, so
    the loop sleeps again. Waiting has to mean reading.

    The seam is which primitive the wait uses, because that is the whole
    difference - `pump` dispatches what has arrived, `sleep` cannot.
    """
    clock, slept = FakeClock(), []
    conn = page([])
    conn.on_send["Accessibility.queryAXTree"] = lambda c, m: clock.advance(1.0)
    driver = driver_over(conn, clock=clock, sleep=slept.append)
    driver.interceptor.arm()

    with pytest.raises(capture.CaptureError):
        driver.find_exact("button", INVITE_TARGET, timeout=3.0)

    assert slept == [], "the poll loop slept while paused requests were waiting on it"


def test_waiting_off_an_armed_window_may_still_sleep():
    """The counterpart, so the check above is about *arming* rather than about
    having quietly deleted the wait. Nothing is paused here, so there is nothing
    to drain and a sleep costs the page nothing."""
    clock, slept = FakeClock(), []
    conn = page([])
    conn.on_send["Accessibility.queryAXTree"] = lambda c, m: clock.advance(1.0)
    driver = driver_over(conn, clock=clock, sleep=slept.append)

    with pytest.raises(capture.CaptureError):
        driver.find_exact("button", INVITE_TARGET, timeout=3.0)

    assert slept, "the poll loop stopped waiting between attempts altogether"


# ------------------------------------------------------------------ a bounded run


def test_a_capture_fails_loudly_once_its_budget_is_gone():
    """Eight minutes of silence is not a diagnosis. A bounded run says what it
    was waiting for and what it had seen when it gave up."""
    clock = FakeClock()
    conn = page([])
    conn.on_send["Accessibility.queryAXTree"] = lambda c, m: clock.advance(40.0)
    driver = driver_over(conn, clock=clock, budget=60.0)

    with pytest.raises(capture.CaptureError) as exc:
        with driver.capture("invite"):
            driver.find_exact("button", INVITE_TARGET, timeout=600.0)

    message = str(exc.value)
    assert "invite" in message
    assert INVITE_TARGET in message, "the message does not say what it was waiting for"
    assert "60" in message, "the message does not say what budget it exceeded"


def test_an_expired_budget_still_disarms_interception():
    clock = FakeClock()
    conn = page([])
    conn.on_send["Accessibility.queryAXTree"] = lambda c, m: clock.advance(40.0)
    driver = driver_over(conn, clock=clock, budget=60.0)

    with pytest.raises(capture.CaptureError):
        with driver.capture("invite"):
            driver.find_exact("button", INVITE_TARGET, timeout=600.0)

    assert conn.calls("Fetch.disable")
    assert driver.interceptor.armed is False


def test_the_budget_is_refused_before_a_click_rather_than_after_it():
    """A budget checked only in the finder would still let the last click of a
    flow go out on an expired run, which is a click nobody is watching for."""
    clock = FakeClock()
    conn = page([ax_node(INVITE_TARGET, 4)])
    driver = driver_over(conn, clock=clock, budget=60.0)
    driver.interceptor.arm()
    clock.advance(90.0)

    with pytest.raises(capture.CaptureError):
        driver.click(centre(4))
    assert not conn.calls("Input.dispatchMouseEvent")


def test_a_run_inside_its_budget_is_untouched(tmp_path):
    clock = FakeClock()
    conn = page([ax_node(INVITE_TARGET, 4)], on_send=click_emits((capture.PAUSED, paused())))
    driver = driver_over(conn, out_dir=tmp_path, clock=clock, budget=60.0)
    with driver.capture("invite"):
        driver.click(driver.find_exact("button", INVITE_TARGET))

    assert driver.payloads["invite"][0]["body"] == BODY


# ------------------------------------------------------------------ the entry point


def test_no_flow_runs_unless_it_was_named_on_the_command_line():
    """An earlier version defaulted to running {"react", "comment", "post"} and
    those flows *published for real*. Nothing may be implicit here: with no
    argument this must refuse, not pick a set."""

    def refuse(*args, **kwargs):
        raise AssertionError("main launched a browser with no flow named")

    assert capture.main([], launch=refuse) == 2


def test_every_flow_is_capture_only_and_named_explicitly():
    """The flows that exist are the three that were captured by interception.
    A `react`/`comment`/`post` trio - the spelling of the set that published -
    must not reappear, and no flow may be reachable without being typed."""
    assert set(capture.FLOWS) == {"post_create", "invite", "invite_withdraw"}


@pytest.mark.parametrize("name", sorted(capture.FLOWS))
def test_no_flow_arms_a_pattern_set_that_would_let_a_write_escape(name):
    """A flow narrows what is paused for speed. This runs each flow's declared
    patterns through the same validation `arm` applies, so a flow cannot ship a
    narrowing that quietly stops covering the write it is trying to capture."""
    patterns = capture.FLOW_PATTERNS.get(name)
    if patterns is None:
        return
    capture.check_patterns(patterns)


def test_an_unknown_flow_exits_two_without_launching(capsys):
    def refuse(*args, **kwargs):
        raise AssertionError("main must not launch a browser for an unknown flow")

    assert capture.main(["nonesuch"], launch=refuse) == 2
    assert "nonesuch" in capsys.readouterr().err


def test_a_run_that_captures_nothing_exits_non_zero(capsys):
    conn = page([ax_node("Start a post", 2), ax_node("Post", 6)])
    browser = FakeBrowser(conn)

    assert capture.main(["post_create"], launch=lambda **kw: browser) == 1
    assert "nothing" in capsys.readouterr().err.lower()
    assert browser.closed


# --------------------------------------------------------- the flow that refuses
#
# `invite_withdraw` used to be implemented here, and running it twice cost two
# real people their pending invitation. It cannot succeed even in principle now:
# the invitation manager is server-driven UI, so there is no Voyager body left to
# capture. Every document in the repo says never run it - which, while the code
# still ran it, was a rule remembered rather than enforced. These pin the
# enforcement.


def test_the_withdraw_flow_is_refused_before_a_browser_is_even_launched(tmp_path, capsys):
    """Refusing inside the flow would be a refusal that has already attached to
    the logged-in profile and loaded the page. Nothing about this run may
    reach LinkedIn, and the cheapest way to guarantee that is not to start."""

    def refuse(*args, **kwargs):
        raise AssertionError("main launched a browser for a flow that cannot succeed")

    code = capture.main(["invite_withdraw", "Ada", "Lovelace"], launch=refuse, out_dir=tmp_path)

    assert code == 1, "a refused flow must not report success"
    assert list(tmp_path.iterdir()) == [], "a refused flow wrote a payload file"
    err = capsys.readouterr().err
    assert "docs/sdui-migration.md" in err, "the message asserts rather than citing"
    assert "server-driven" in err


def test_the_withdraw_flow_refuses_when_it_is_driven_directly_too(tmp_path):
    """The check in `main` is the cheap path, not the rule. Anyone importing the
    module reaches the flow function itself, and it must refuse there as well -
    without navigating, without arming, and without clicking."""
    conn = page([ax_node("Withdraw invitation sent to Ada Lovelace", 4, role="link")])
    browser = FakeBrowser(conn)
    driver = driver_over(conn, browser=browser, out_dir=tmp_path)

    with pytest.raises(capture.CaptureError) as exc:
        capture.flow_invite_withdraw(driver, ["Ada", "Lovelace"])

    assert "docs/sdui-migration.md" in str(exc.value)
    assert browser.visited == []
    assert not conn.calls("Fetch.enable")
    assert not conn.calls("Input.dispatchMouseEvent")
    assert list(tmp_path.iterdir()) == []


def test_the_withdraw_flow_stays_registered_so_it_fails_by_name():
    """Deleted, it would answer "unknown flow" - which reads as a typo and gets
    retried with another spelling. Registered, the answer is the reason."""

    def refuse(*args, **kwargs):
        raise AssertionError("main launched a browser for a flow that cannot succeed")

    assert "invite_withdraw" in capture.FLOWS
    # Bare, with no argument at all: `main` answers 2 for a name it does not
    # know, and this name must never be one of those.
    assert capture.main(["invite_withdraw"], launch=refuse) == 1


def test_the_usage_text_offers_no_way_to_run_it():
    """It used to advertise a worked example, one copy-paste from the act every
    document in this repo says must never be performed."""
    line = next(ln for ln in capture.usage().splitlines() if "invite_withdraw" in ln)
    assert "refused" in line
    assert "docs/sdui-migration.md" in line
    assert "Full Name" not in line, "the usage text still shows an argument to pass"


def test_a_successful_run_exits_zero(tmp_path, capsys):
    conn = page(
        [ax_node("Start a post", 2), ax_node("Post", 6)],
        on_send=click_emits((capture.PAUSED, paused())),
    )
    browser = FakeBrowser(conn)

    assert capture.main(["post_create"], launch=lambda **kw: browser, out_dir=tmp_path) == 0
    assert json.loads((tmp_path / "post_create.json").read_text())[0]["body"] == BODY
    assert browser.visited == ["https://www.linkedin.com/feed/"]
    assert browser.closed
