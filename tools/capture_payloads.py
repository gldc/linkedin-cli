"""Capture real Voyager write payloads without performing the write.

This project does not ship guessed request bodies. Four guessed `createMessage`
variants were each rejected with a bare 400 before the real one was captured,
and the verbs still missing are missing for want of the same evidence. The old
way of obtaining it was to perform the action once and read what went out -
which for an invitation or a post means an irreversible, publicly visible act
against a real person's feed.

Capture by interception removes that. CDP's `Fetch` domain pauses the request
inside the browser before it leaves, so the body can be read and the request
then failed: LinkedIn never sees it.

Three rules are binding, and each was paid for. While capturing the invite
payload an earlier script matched the Connect control by prefix, clicked
"Invite someone else entirely to connect" on a sidebar recommendation card, and - having
armed interception only *after* that click, on the assumption that a
confirmation dialog stood in between - let a real connection request leave
unobserved. The run reported "0 captured", which read as a harmless no-op. It
was found by reading the sent-invitations list, not by the script.

1. Arm before the first click of a flow, never before the last. `capture()`
   arms, and `click` refuses until it has, so a flow's clicks cannot precede it.
2. Match the full accessible name exactly and fail loudly when it is absent.
   `find_exact` is the only finder here for that reason.
3. A run that captures nothing is a failure. `capture()` raises and `main`
   exits non-zero, so nobody reads silence as "nothing happened".

One flow is registered and refuses. `invite_withdraw` used to drive the
sent-invitations page, and each of the two runs it ever made retracted a real
pending invitation: the write left through an endpoint the pattern set did not
cover, so nothing paused it and the run reported itself as intercepting anyway.
It cannot succeed even in principle now - that surface speaks server-driven UI
rather than Voyager JSON, so there is no request body here to record - and every
document in this repo says so. A rule that only the documents enforce is a rule
remembered, so the flow refuses in code. It stays in `FLOWS` rather than being
deleted because "unknown flow" is what a reader retries with a different
spelling.

Four measured constraints this file exists to respect:

* `cdp.CDPSession.call` discards every message whose id does not match the call
  it is awaiting, events included, so an interception loop cannot be built on
  it: the paused request rides in on the click's own reply and would be dropped,
  leaving the page hung with the request stalled. `Interceptor` is one reader
  that dispatches both events and replies.
* `pipe.PipeConnection.recv` returns ONE already-de-framed message. Re-splitting
  what it returns on the NUL delimiter leaves a single element that is then
  popped as a partial frame - which is what made an earlier capture report zero
  writes on a run that had in fact sent one.
* Controls inside closed shadow roots are reachable only through
  `Accessibility.queryAXTree` plus `DOM.getBoxModel({backendNodeId})`.
  `DOM.getDocument(pierce=True)` and `DOM.querySelectorAll` do not reach them,
  and `DOM.performSearch` given a bare word like "button" falls back to a
  plain-text search over hundreds of irrelevant nodes.
* `Page.captureScreenshot` is unreliable while `Fetch` is intercepting - it
  timed out and killed a capture run - so nothing here depends on a screenshot.

A fifth constraint is about cost, and it killed a run just as dead. The invite
capture ran for over eight minutes against a live profile page and never reached
its click. A profile is far heavier than the feed, and while armed *every*
voyager request it issues is paused waiting on this process to resume it, so the
page cannot finish rendering until we answer. Four things follow, and each is
pinned by a test:

* **Waiting means reading.** `_wait` pumps rather than sleeps whenever
  interception is armed. A poll loop that slept for a second drained nothing for
  that second, so the page stalled, so the control never rendered, so it slept
  again. That is the whole eight minutes.
* **Resuming a request must not await its reply.** `_on_paused` uses `send`, so
  a page issuing hundreds of reads costs one message each rather than a
  round-trip each.
* **Ask for a box only for the node that already matched by name**, and cache the
  document root. `DOM.getBoxModel` is a round-trip per node and the tree runs to
  hundreds; `DOM.getDocument` answers the same thing all document long.
  `find_optional` additionally skips the near-miss diagnostic, whose second
  unfiltered sweep it was only going to throw away.
* **A run is bounded.** `CAPTURE_BUDGET` turns a hang into a message naming the
  control it was waiting for and what it had seen.

Narrowing what is paused is available (`FLOW_PATTERNS`, `Interceptor.arm`) and is
validated by `check_patterns`, because the obvious optimisation - pause fewer
URLs - is also the obvious way to reintroduce the incident above. No flow
declares a narrowing today: it requires knowing the endpoint the write goes to,
and `invite`, the flow that most needs the speed, is the one whose endpoint has
never been observed.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linkedin_cli import supervisor  # noqa: E402

OUT = Path("/tmp/payloads")

VOYAGER = "/voyager/api/"
PAUSED = "Fetch.requestPaused"

# The default, and the safe one: every voyager request, paused before it leaves.
# `Request` is not a detail - `Response` pauses a request that has already been
# answered, which means it already reached LinkedIn.
REQUEST_STAGE = "Request"
# Every request, not just Voyager's. `*/voyager/api/*` looked broad and was not:
# withdrawing an invitation posts somewhere else entirely, so the request was
# never paused, went to LinkedIn for real, and the run still reported itself as
# intercepting - it retracted a real pending invitation while printing that it
# had captured one presence poll. A pattern that does not match cannot abort.
PATTERNS = [{"urlPattern": "*", "requestStage": REQUEST_STAGE}]

# A ready-made narrowing for a flow whose write is known to go through the
# GraphQL executor. Narrowing is a *performance* lever: on a profile page the
# broad pattern pauses hundreds of reads, and each one is a message this process
# has to answer before the page can carry on rendering.
#
# `FLOW_PATTERNS` is deliberately empty. Narrowing requires knowing which
# endpoint the write goes to, and the flow that most needs the speed - `invite` -
# is the one whose endpoint has never been observed. Guessing it would mean the
# real invitation URL is not in the pattern set, so the request is never paused,
# and it goes to LinkedIn for real while the run reports itself as intercepting.
# That is the incident this file was written after, reintroduced through an
# optimisation. A flow declares a narrowing only once its endpoint is known.
GRAPHQL_PATTERNS = [{"urlPattern": "*/voyager/api/graphql*", "requestStage": REQUEST_STAGE}]

FLOW_PATTERNS: dict[str, list[dict]] = {}

# Redacted in the record itself rather than on the way to disk, so the captured
# body can be printed or inspected without the live credential riding along.
SECRET_HEADERS = ("cookie", "csrf-token")

FEED_URL = "https://www.linkedin.com/feed/"
PROFILE_URL = "https://www.linkedin.com/in/%s/"
SENT_INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"

# Where the measurement behind the refusal below is written down. Named once, so
# the tool and `surfaces/invitations.py` send a reader to the same page.
SDUI_MIGRATION_DOC = "docs/sdui-migration.md"

# Why `invite_withdraw` refuses instead of running. Two claims, and the second is
# the one that closes the subject: it is not a payload nobody has got round to
# capturing, it is a payload that no longer exists to capture.
NO_WITHDRAW = (
    "the invite_withdraw flow is refused, and it is not a gap another capture run can "
    "close. LinkedIn's invitation manager has migrated to its server-driven UI: pressing "
    "Withdraw posts a `proto.sdui.actions.core.NavigateToScreen` to /flagship-web/ and gets "
    "back a React Server Components stream - and that request does not even perform the "
    "withdrawal, it opens a confirmation screen that has to be fetched from the server "
    "first. There is no Voyager request body left here for anyone to record. The evidence "
    f"is in {SDUI_MIGRATION_DOC}. Running it anyway is not free: both of the runs this flow "
    "ever made cost a real person their pending invitation, because the write left through "
    "an endpoint the pattern set did not cover and so was never paused, while the run "
    f"reported itself as intercepting. Withdraw an invitation in the browser at "
    f"{SENT_INVITATIONS_URL} - and note before you do that LinkedIn blocks re-inviting a "
    "withdrawn contact for up to three weeks."
)

# Flows that exist so that typing their name fails by name. Read by `main`
# before a browser is launched, and by the flow itself, so the refusal holds
# whichever way it is reached.
REFUSED = {"invite_withdraw": NO_WITHDRAW}

# The labels the note dialog confirms with, in preference order - the one that
# skips a step first. Some profiles interpose this dialog and some fire straight
# off Connect, so absent is a legitimate answer and this goes through
# `find_optional`. Alternatives, each still matched in full and never by prefix;
# `Driver.find_any` carries the reasoning for why a list of exact spellings is
# safe where a prefix never is.
INVITE_CONFIRM = ("Send without a note", "Send", "Send invitation", "Send now")

FIND_TIMEOUT = 20.0
# A profile's action bar hydrates well after first paint.
SLOW_HYDRATE = 60.0
POLL_INTERVAL = 1.0

# The whole of one flow, from arming to disarming. A capture that has not got
# anywhere in this long is not about to: the invite run was killed by hand after
# eight minutes, having never reached its click, and a run that hangs teaches
# nothing about why. This turns that into a message naming what it was waiting
# for and what it had seen.
CAPTURE_BUDGET = 180.0
# How long to keep reading after the last click. The request is paused inside
# the browser, so this is waiting on the page's own event loop, not the network.
SETTLE = 8.0

# The request never leaves the browser, so the text is only ever seen by this
# process - but it is what an operator would see on screen if the run were
# watched, and it should explain itself there too.
PLACEHOLDER = "(payload capture - this request is aborted before it leaves the browser)"


class CaptureError(Exception):
    pass


def is_write(method: str, url: str) -> bool:
    """Anything to Voyager that is not a plain read.

    Deliberately wider than the POST and DELETE actually needed: letting one
    write through is irreversible and holding one back is not, so an unfamiliar
    method is aborted rather than continued.
    """
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return False
    # Not restricted to `/voyager/api/`: the endpoint that retracts an invitation
    # is not under it, and treating "not Voyager" as "not a write" is what let a
    # real withdrawal through. Anything non-idempotent aimed at LinkedIn counts.
    return VOYAGER in url or "linkedin.com" in url


def check_patterns(patterns) -> list[dict]:
    """Refuse a pattern set that would leave a voyager write unpaused.

    Narrowing exists for speed, and speed must not be able to buy itself a hole.
    Two ways it could, and both are refused here rather than at review time:

    * **A pattern that is not a voyager pattern**, or an empty set. Whatever is
      not matched is not paused, so it is sent - while `capture()` still reports
      the run as intercepting, which is precisely the "0 captured" that read as a
      harmless no-op the day a real invitation went out.
    * **`requestStage: Response`.** That pauses the request *after* LinkedIn has
      answered it. The body would still be recorded and the run would still look
      like a success, but the write really happened.
    """
    if not isinstance(patterns, (list, tuple)) or not patterns:
        raise CaptureError(
            "an empty Fetch pattern set pauses nothing, so every write would reach "
            f"LinkedIn while the run reported itself as intercepting. Pass {PATTERNS!r} "
            "or a narrower set that still covers /voyager/api/."
        )
    for pattern in patterns:
        url = (pattern or {}).get("urlPattern") or ""
        # `*` matches everything, so nothing can escape it. The check used to
        # demand the literal string /voyager/api/ and so rejected the only
        # pattern that is strictly safe - while accepting */voyager/api/* , which
        # looks broad and is not: retracting an invitation posts outside it, and
        # that request was never paused. A pattern that does not match cannot
        # abort.
        if url != "*" and VOYAGER not in url:
            raise CaptureError(
                f"the Fetch pattern {url!r} does not cover {VOYAGER}, so a voyager write "
                "would not be paused and would reach LinkedIn unobserved. Use '*', or a "
                "narrower set that still covers /voyager/api/ - and remember that not every "
                "write LinkedIn makes is under /voyager/api/."
            )
        stage = (pattern or {}).get("requestStage")
        if stage != REQUEST_STAGE:
            raise CaptureError(
                f"the Fetch pattern {url!r} asks for requestStage {stage!r}; it must be "
                f"{REQUEST_STAGE!r}. Pausing at the response stage means the request has "
                "already been answered by LinkedIn - the body would be captured from a "
                "write that really happened."
            )
    return [dict(pattern) for pattern in patterns]


class Interceptor:
    """One reader over the raw CDP connection, dispatching events and replies.

    Every call the driver makes goes through here, including the clicks. That is
    what makes awaiting a click's reply safe: through `cdp.CDPSession.call` the
    `Fetch.requestPaused` that arrives milliseconds after the click would be
    discarded while it waited, and the page would hang on a request nothing ever
    resumed.
    """

    def __init__(self, connection, session_id: str | None = None, *, clock=time.monotonic):
        self._connection = connection
        self._session_id = session_id
        self._clock = clock
        self._id = 0
        # Only ids somebody is waiting on are kept. `Fetch.failRequest` and
        # `Fetch.continueRequest` are answered too, and nothing reads those.
        self._awaiting: set[int] = set()
        self._replies: dict[int, dict] = {}
        self.captured: list[dict] = []
        # Writes whose body the browser paused but did not hand over. Recording
        # the URL alone teaches nothing, so these are failures, not captures.
        self.withheld: list[str] = []
        self.seen = 0
        self.paused = 0
        self.armed = False
        self.patterns: list[dict] = []

    # ------------------------------------------------------------------- wire

    def send(self, method: str, params: dict | None = None) -> int:
        self._id += 1
        message: dict = {"id": self._id, "method": method, "params": params or {}}
        if self._session_id:
            message["sessionId"] = self._session_id
        self._connection.send(json.dumps(message))
        return self._id

    def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0) -> dict:
        message_id = self.send(method, params)
        self._awaiting.add(message_id)
        deadline = self._clock() + timeout
        try:
            while message_id not in self._replies:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise CaptureError(f"timed out waiting for a reply to {method}")
                try:
                    raw = self._connection.recv(timeout=remaining)
                except Exception as exc:  # noqa: BLE001 - the pipe's own error type varies
                    raise CaptureError(f"the CDP connection failed during {method}: {exc}") from exc
                self._dispatch(raw)
        finally:
            self._awaiting.discard(message_id)
        reply = self._replies.pop(message_id)
        if "error" in reply:
            raise CaptureError(f"{method} failed: {reply['error']}")
        return reply.get("result", {})

    def pump(self, seconds: float) -> None:
        """Read and dispatch for a while, so a request that lands after the
        click is still seen."""
        deadline = self._clock() + seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            try:
                raw = self._connection.recv(timeout=remaining)
            except Exception:  # noqa: BLE001 - a silent or closed pipe ends the window
                return
            self._dispatch(raw)

    def _dispatch(self, raw) -> None:
        # One message, already de-framed. Splitting it again on the NUL
        # delimiter is the bug that silently dropped every event.
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except ValueError:
            return
        self.seen += 1
        if message.get("method") == PAUSED:
            self._on_paused(message.get("params") or {})
        elif "method" not in message and message.get("id") in self._awaiting:
            self._replies[message["id"]] = message

    # ------------------------------------------------------------- intercepting

    def arm(self, patterns=None) -> None:
        """Pause matching requests. `None` means everything voyager serves.

        Validated before `Fetch.enable` is sent, so a bad narrowing leaves the
        run disarmed and loud rather than running unprotected.
        """
        self.patterns = check_patterns(PATTERNS if patterns is None else patterns)
        self.call("Fetch.enable", {"patterns": self.patterns})
        self.armed = True

    def disarm(self) -> None:
        # Cleared first, so a failure here still leaves `click` refusing rather
        # than clicking into a window nothing is watching. Suppressed wholesale
        # because this runs from a `finally`: a dead pipe here must not replace
        # the exception that killed the flow.
        self.armed = False
        with contextlib.suppress(Exception):
            self.call("Fetch.disable")

    def _on_paused(self, params: dict) -> None:
        self.paused += 1
        request_id = params.get("requestId")
        request = params.get("request") or {}
        method = request.get("method") or ""
        url = request.get("url") or ""
        if not is_write(method, url):
            # Everything not being captured has to be resumed, or the page
            # stalls on it and the flow never reaches its own write. `send`
            # rather than `call` on purpose: a profile page pauses hundreds of
            # reads, and awaiting a reply for each one turns the page load into
            # a round-trip per request. Nothing reads these replies, so nothing
            # has to wait for them.
            self.send("Fetch.continueRequest", {"requestId": request_id})
            return
        self.captured.append(
            {
                "method": method,
                "url": url,
                "headers": {
                    key: ("<redacted>" if key.lower() in SECRET_HEADERS else value)
                    for key, value in (request.get("headers") or {}).items()
                },
                "body": request.get("postData"),
            }
        )
        if request.get("postData") is None and request.get("hasPostData"):
            self.withheld.append(url)
        self.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})


class Driver:
    """Finding, clicking and typing, with the three rules built into the API."""

    def __init__(
        self,
        interceptor: Interceptor,
        *,
        browser=None,
        out_dir: Path | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        budget: float = CAPTURE_BUDGET,
    ):
        self.interceptor = interceptor
        self.browser = browser
        self.out_dir = Path(out_dir) if out_dir else OUT
        self.payloads: dict[str, list[dict]] = {}
        self._domains = False
        self._root = None
        self._clock = clock
        self._sleep = sleep
        self.budget = budget
        self._flow: str | None = None
        self._deadline = clock() + budget

    @classmethod
    def over(cls, browser, **kwargs) -> Driver:
        session_id = getattr(browser._session, "_session_id", None)
        clock = kwargs.get("clock", time.monotonic)
        return cls(
            Interceptor(browser._connection, session_id, clock=clock), browser=browser, **kwargs
        )

    # ---------------------------------------------------------------- looking

    def find_exact(
        self, role, name: str, *, timeout: float = FIND_TIMEOUT, diagnose: bool = True
    ) -> dict:
        """Locate one control by its FULL accessible name.

        Never by prefix or substring. The accessibility tree covers the whole
        page, sidebar recommendation cards included, and a `startswith("invite ")`
        match once picked a stranger's card and sent them a connection request.
        Absent is an error, not a reason to settle for a looser match.

        `role` may be several roles: the Connect control is a button, the same
        page's cards expose theirs as links, and asking for the wrong one finds
        nothing while the control is plainly on screen. `None` among them means
        "any role", which is the only query some dialog confirms resolve on at
        all.

        One candidate of `find_any`, and implemented as one so that the exactness
        rule has a single implementation and cannot drift between them.
        """
        return self.find_any(role, name, timeout=timeout, diagnose=diagnose)

    def find_any(
        self, role, *names: str, timeout: float = FIND_TIMEOUT, diagnose: bool = True
    ) -> dict:
        """The first of several candidate names to resolve - each still EXACT.

        This is **not** a relaxation of `find_exact`, and must not be turned into
        one. Every candidate is still compared in full; what varies is only which
        complete strings are acceptable. The distinction that makes it safe: a
        *dialog confirm* is not person-specific. The measured case was a dialog
        rendering its confirm as `Withdraw` on some loads and `Withdraw
        invitation` on others - two labels for one button, in a dialog already
        scoped to a single item by the click that opened it, so neither could
        resolve to somebody else's card. `INVITE_CONFIRM` is the same shape.

        A prefix match has no such property, and that is the difference that
        matters: `Withdraw` is a prefix of `Withdraw invitation sent to <Name>`,
        one of which is on screen for every pending invitation the account has,
        so a prefix would have matched whichever rendered first - a stranger's.
        Do not "simplify" this into a substring test.

        Candidates are polled *together*, one sweep per attempt rather than one
        timeout per candidate: a dialog animates in, so a name-at-a-time search
        would spend the whole budget on the first spelling before ever asking
        about the second.
        """
        if not names:
            raise CaptureError("find_any was given no name to look for")
        deadline = self._clock() + timeout
        doing = f"looking for the {self._role_name(role)} named {self._either(names)}"
        while True:
            self._check_budget(doing)
            found = self._sweep(role, names)
            if found is not None:
                return found
            self._check_budget(doing)
            if self._clock() >= deadline:
                raise CaptureError(
                    self._absent(role, names)
                    if diagnose
                    else f"no {role} named {self._either(names)}"
                )
            self._wait()

    def _sweep(self, role, names) -> dict | None:
        """One pass over every candidate, or `None` if none of them resolved.

        A *clickable* match anywhere in the list beats a merely resolvable one,
        which is why the fallback is held until every name has been tried rather
        than returned as soon as one name runs out of nodes.

        The name is checked *before* the box is resolved, and the sweep returns
        on the first node that has one. `DOM.getBoxModel` is a CDP round-trip per
        node, and a profile page's accessibility tree runs to hundreds of them.
        """
        fallback = None
        for name in names:
            for node in self._ax_nodes(role, name):
                if (node.get("name") or {}).get("value") != name:
                    continue
                if node.get("ignored"):
                    continue
                # Chrome names this backendDOMNodeId on accessibility nodes; the
                # DOM domain calls the same thing backendNodeId. Reading the DOM
                # spelling here silently yielded None for every node, so no
                # control was ever locatable against a real browser.
                point = self._centre(node.get("backendDOMNodeId") or node.get("backendNodeId"))
                if point is None:
                    continue
                if self._clickable(point, name):
                    return {**point, "name": name}
                if fallback is None:
                    fallback = {**point, "name": name}
        return fallback

    def find_optional(self, role, *names: str, timeout: float = 0.0) -> dict | None:
        """For a step that may not be there - a confirmation dialog that some
        profiles show and others do not.

        Takes candidate names like `find_any`, and for the same reason: the
        invite flow's note dialog confirms as `Send without a note`, `Send`,
        `Send invitation` or `Send now`. Each is still matched in full; earlier
        candidates win, which is why the one that skips a step is listed first.

        Asked one name at a time these cost a full timeout *each* - four labels
        at six seconds is twenty-four seconds of an armed capture's budget spent
        before the last is even asked about, and this flow has already died of a
        budget timeout once. One sweep per attempt covers them all.

        Safe to click blind only because interception is already armed by the
        time a flow reaches one: a wrong guess cannot reach LinkedIn.

        `diagnose=False` because the message is thrown away here, and building it
        costs a second unfiltered sweep of the whole accessibility tree. Absent is
        the *expected* answer for this call - the invite flow asks on every run
        and most profiles have no such step - so that sweep was pure cost on the
        one flow that could least afford it.
        """
        try:
            return self.find_any(role, *names, timeout=timeout, diagnose=False)
        except CaptureError:
            return None

    def _wait(self) -> None:
        """The pause between attempts, which while armed has to mean *reading*.

        Every voyager request the page issues is paused waiting on this process
        to resume it. Sleeping through that window drains nothing, so the page
        stalls on requests nobody is answering, so the control never renders, so
        the loop waits again. That is the shape of the run that went eight
        minutes without reaching its click.
        """
        if self.interceptor.armed:
            self.interceptor.pump(POLL_INTERVAL)
        else:
            self._sleep(POLL_INTERVAL)

    def _check_budget(self, doing: str) -> None:
        if self._clock() < self._deadline:
            return
        flow = f"[{self._flow}] " if self._flow else ""
        raise CaptureError(
            f"{flow}gave up after {self.budget:g}s while {doing}. "
            f"{self.interceptor.paused} request(s) paused, "
            f"{len(self.interceptor.captured)} captured. A capture that has got this far "
            "and no further is usually looking for a control that never rendered - check "
            "the accessible name against the live page, and check by hand what the account "
            "did before re-running."
        )

    @staticmethod
    def _role_name(role) -> str:
        return role if isinstance(role, str) else "/".join(r or "any" for r in role)

    def _ax_nodes(self, role, name: str | None) -> list[dict]:
        self._enable_domains()
        root = self._document_root()
        roles = (role,) if isinstance(role, str) else tuple(role)
        nodes: list[dict] = []
        for one in roles:
            # `None` means "any role". A dialog's confirm button is not always
            # exposed as one: the confirm measured on the invitation manager
            # resolved only on an unrestricted query, and asking for a role a
            # control does not carry finds nothing while it is plainly on screen.
            params: dict = {"nodeId": root}
            if one is not None:
                params["role"] = one
            if name is not None:
                params["accessibleName"] = name
            nodes += self.interceptor.call("Accessibility.queryAXTree", params).get("nodes", [])
        return nodes

    def _document_root(self):
        """The root nodeId, cached for as long as the document lasts.

        It was re-fetched for every role of every poll - one round-trip each
        time, answering the same thing. Invalidated on navigation, because a
        stale root queries the tree of a page that is no longer there.
        """
        if self._root is None:
            self._root = self.interceptor.call("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        return self._root

    def _centre(self, backend_node_id) -> dict | None:
        if backend_node_id is None:
            return None
        try:
            model = self.interceptor.call(
                "DOM.getBoxModel", {"backendNodeId": backend_node_id}
            ).get("model")
        except CaptureError:
            return None
        if not model or not model.get("width") or not model.get("height"):
            return None
        quad = model["content"]
        return {"x": sum(quad[0::2]) / 4, "y": sum(quad[1::2]) / 4}

    def _clickable(self, point: dict, name: str) -> bool:
        """Would a click at `point` actually reach this control?

        A profile renders the same control twice: once in the page and once in a
        sticky header pinned to the top of the viewport. Both carry the same
        accessible name and both resolve a box, so taking the first match clicked
        the header copy at y=27 - on the navigation bar - and no invitation was
        ever built. Hit-testing asks the browser what is on top at that point and
        compares its accessible name, which is what a real click will hit.

        Returns True when it cannot tell: a hit test that fails must not be able
        to veto every candidate and turn a findable control into "absent".
        """
        try:
            hit = self.interceptor.call(
                "DOM.getNodeForLocation",
                {"x": int(point["x"]), "y": int(point["y"]), "includeUserAgentShadowDOM": True},
            )
        except CaptureError:
            return True
        backend = hit.get("backendNodeId")
        if backend is None:
            return True
        try:
            nodes = self.interceptor.call(
                "Accessibility.queryAXTree", {"backendNodeId": backend}
            ).get("nodes", [])
        except CaptureError:
            return True
        if not nodes:
            return True
        return any((n.get("name") or {}).get("value") == name for n in nodes)

    @staticmethod
    def _either(names) -> str:
        """The candidates as prose. Every one of them is named when a search
        fails: a reader who has to go and check the live page needs to know all
        of what was asked for, not just whichever was tried last."""
        return " or ".join(repr(n) for n in names)

    def _absent(self, role, names) -> str:
        """Name the near misses: one of them is what an earlier run clicked."""
        heads = {name.split(" ")[0].lower() for name in names}
        wanted = set(names)
        others = []
        nodes: list[dict] = []
        # Diagnostics only: it must not be what raises instead of the message.
        with contextlib.suppress(Exception):
            nodes = self._ax_nodes(role, None)
        for node in nodes:
            value = (node.get("name") or {}).get("value") or ""
            if value and value not in wanted and any(value.lower().startswith(h) for h in heads):
                others.append(value)
        nearby = ", ".join(repr(v) for v in sorted(set(others))[:6])
        return (
            f"no {role} is named exactly {self._either(names)}; refusing to fall through to a "
            f"looser match. Similarly named: {nearby or '(none)'}"
        )

    def _enable_domains(self) -> None:
        if self._domains:
            return
        self.interceptor.call("DOM.enable")
        self.interceptor.call("Accessibility.enable")
        self._domains = True

    # ----------------------------------------------------------------- acting

    def click(self, point: dict) -> None:
        if not self.interceptor.armed:
            raise CaptureError(
                "interception must be armed before the first click of a flow. "
                "Assuming a confirmation dialog stands between a click and its "
                "request is what sent a real connection request to the wrong person."
            )
        # Checked here as well as in the finder: a budget enforced only while
        # looking would still let the last click of an expired run go out, and
        # that is a click whose request nobody is still waiting to see.
        self._check_budget(f"about to click {point.get('name') or 'a control'!r}")
        self._dispatch_click(point)

    def setup_click(self, point: dict) -> None:
        """For a control that cannot start a write - the cookie banner, a tab.

        The only clicking allowed outside an armed window, and the reason it is
        named differently from `click` rather than being a flag on it.
        """
        self._dispatch_click(point)

    def _dispatch_click(self, point: dict) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.interceptor.call(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": point["x"],
                    "y": point["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )

    def type_text(self, text: str) -> None:
        """No selector: opening the composer autofocuses its editor, which is
        the only way to reach one that lives in a closed shadow root."""
        self.interceptor.call("Input.insertText", {"text": text})

    def navigate(self, url: str) -> None:
        if self.interceptor.armed:
            raise CaptureError(
                f"cannot navigate to {url} while interception is armed: the "
                "load wait reads the pipe through CDPSession, which would "
                "discard the paused requests and hang the page"
            )
        if self.browser is None:
            raise CaptureError("this driver has no browser to navigate")
        self.browser._navigate(url)
        self._root = None

    # ---------------------------------------------------------------- capturing

    @contextlib.contextmanager
    def capture(self, name: str, *, settle: float = SETTLE, patterns=None):
        """Arm interception, run the flow, and refuse to call an empty run a no-op.

        `patterns` narrows what is paused, for a flow that knows which endpoint
        its write goes to; `FLOW_PATTERNS` is where a flow declares one so it
        applies without the call site being edited. `None` in both keeps the
        broad default - see `check_patterns` for why a narrowing cannot be taken
        on trust.
        """
        before = len(self.interceptor.captured)
        held = len(self.interceptor.withheld)
        self._flow = name
        self._deadline = self._clock() + self.budget
        self.interceptor.arm(FLOW_PATTERNS.get(name) if patterns is None else patterns)
        try:
            yield self
            self.interceptor.pump(settle)
        finally:
            self.interceptor.disarm()
            self._flow = None

        found = self.interceptor.captured[before:]
        if not found:
            raise CaptureError(
                f"[{name}] captured nothing from {self.interceptor.paused} paused "
                "request(s). Either the control fired no request, or one left "
                "unobserved - check by hand what the account did before re-running."
            )
        missing = self.interceptor.withheld[held:]
        if missing:
            raise CaptureError(
                f"[{name}] the browser paused {len(missing)} write(s) but kept the "
                f"body back, so nothing was learned: {missing[0]}"
            )
        self.payloads[name] = found
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{name}.json"
        path.write_text(json.dumps(found, indent=2))
        print(f"[{name}] {len(found)} write(s) captured and aborted -> {path}")
        for item in found:
            print(f"    {item['method']} {item['url'][:92]}")


# ---------------------------------------------------------------------- flows


def flow_post_create(driver: Driver, args: list[str]) -> None:
    """Known-good: this payload was captured this way once already, so it is
    also the flow that proves the tool still works."""
    text = args[0] if args else PLACEHOLDER
    driver.navigate(FEED_URL)
    with driver.capture("post_create"):
        driver.click(driver.find_exact(("button", "link"), "Start a post"))
        driver.type_text(text)
        driver.click(driver.find_exact("button", "Post"))


def flow_invite(driver: Driver, args: list[str]) -> None:
    if len(args) < 2:
        raise CaptureError(
            "invite needs the public id and the target's full name exactly as "
            'LinkedIn renders it, e.g. invite ada-lovelace-1815 "Ada Lovelace"'
        )
    public_id, full_name = args[0], " ".join(args[1:])
    connect = f"Invite {full_name} to connect"
    driver.navigate(PROFILE_URL % public_id)
    # Wait for the control BEFORE arming. A profile's own action bar hydrates
    # after the sidebar recommendation cards do, and while interception is armed
    # every request it needs goes through this loop, so the bar can miss a
    # 20-second window and leave only strangers' cards to match against. Finding
    # is not clicking, so the rule that interception is armed before the first
    # click of a flow still holds.
    driver.find_exact(("button", "link"), connect, timeout=SLOW_HYDRATE)
    with driver.capture("invite"):
        driver.click(driver.find_exact(("button", "link"), connect))
        # Some profiles interpose a note dialog and some fire straight off
        # Connect. A zero timeout finds neither: the dialog animates in, so the
        # first look lands before it exists and the flow ends having sent
        # nothing. Safe to wait and safe to click blind - interception is armed,
        # so a wrong guess cannot reach LinkedIn.
        #
        # All four labels in one search rather than a loop of four: asked one at
        # a time they cost six seconds each before the last is even tried, and
        # this is the flow whose budget has actually run out mid-capture.
        note = driver.find_optional(("button", "generic"), *INVITE_CONFIRM, timeout=6.0)
        if note:
            driver.click(note)


def flow_invite_withdraw(driver: Driver, args: list[str]) -> None:
    """Registered, and refusing is the whole of what it does. See `NO_WITHDRAW`.

    It keeps a flow's signature and its place in `FLOWS` so that the failure is
    this refusal rather than "unknown flow" - the second reads as a typo and
    gets retried with another spelling. Nothing is navigated to and nothing is
    armed: the driver is accepted and ignored.
    """
    raise CaptureError(NO_WITHDRAW)


FLOWS = {
    "post_create": (flow_post_create, "[text]"),
    "invite": (flow_invite, '<public-id> "<Full Name>"'),
    # No argument hint: there is no invocation of this that works, and a worked
    # example in the usage text is an instruction to try one.
    "invite_withdraw": (flow_invite_withdraw, f"(refused - see {SDUI_MIGRATION_DOC})"),
}


def usage() -> str:
    lines = ["usage: capture_payloads.py <flow> [args]", "", "flows:"]
    lines += [f"  {name} {hint}" for name, (_, hint) in FLOWS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, launch=None, out_dir: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in FLOWS:
        print(f"unknown flow {argv[0]!r}" if argv else "no flow given", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2

    name, args = argv[0], argv[1:]
    # Before the launch, not inside the flow. `flow_invite_withdraw` refuses on
    # its own too, but by then a browser is attached to the logged-in profile
    # and loading a page for a flow that cannot succeed; a refusal that has
    # already touched the account is not much of a refusal.
    if name in REFUSED:
        print(f"!! {REFUSED[name]}", file=sys.stderr)
        return 1

    flow, _ = FLOWS[name]
    browser = (launch or supervisor.Browser.launch)(headless=True)
    driver = Driver.over(browser, out_dir=out_dir)
    try:
        flow(driver, args)
    except CaptureError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
