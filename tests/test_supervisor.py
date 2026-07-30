"""The resident supervisor, exercised without a browser and without connecting.

Every test here drives the real code over fakes at the two seams that would
otherwise reach this machine: `listener=` stands in for the bound socket so
`serve` runs its actual accept loop against `socketpair` ends, and `sock=`
stands in for the client's connect. Nothing launches Chromium, nothing sleeps
on the real clock, and the two tests that connect for real do so over an
`AF_UNIX` path in a throwaway directory and are marked accordingly.

The permission tests are not hygiene. A debug *port* is reachable by every uid
on the box - which is what the container proved before this design existed - and
a `0600` socket inside a `0700` directory is the only thing that puts the
credential back behind a uid boundary. So the modes are asserted rather than
assumed, and a directory anyone else can enter is a refusal to start.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import threading
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from linkedin_cli import cdp, pipe, state, supervisor
from linkedin_cli.supervisor import PAGE_URL as PAGE

SOCKET = "linkedin.sock"


def dir_mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def root():
    """A temporary directory short enough to bind a socket inside.

    Not `tmp_path`: the kernel caps a unix socket path at 104 bytes on darwin,
    and pytest's own `<basetemp>/<test name>0` layout is longer than that by
    itself, so every bind here would fail on the path length rather than on
    anything the test is about.
    """
    path = Path(tempfile.mkdtemp(prefix="lnk-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------- where it lives


def test_the_default_socket_sits_beside_the_ledger_in_its_0700_directory():
    """The uid gate is the *directory*, and `state` is the one place that
    already creates a 0700 one per invocation."""
    assert supervisor.default_socket_path().parent == state.resolve_path().parent
    assert supervisor.default_socket_path().name == SOCKET


def test_the_socket_path_can_be_relocated_by_the_environment(monkeypatch, tmp_path):
    """Under the credential broker HOME is not the tenant's to write to, so every path the
    package uses has to be movable per invocation - `state.py` already is."""
    monkeypatch.setenv(supervisor.SOCKET_ENV, str(tmp_path / "elsewhere.sock"))
    assert supervisor.default_socket_path() == tmp_path / "elsewhere.sock"


def test_the_environment_override_is_ignored_when_it_is_blank(monkeypatch):
    monkeypatch.setenv(supervisor.SOCKET_ENV, "   ")
    assert supervisor.default_socket_path().name == SOCKET


# --------------------------------------------------------------- which profile


def test_the_default_profile_is_the_one_the_container_is_signed_in_to():
    """Measured in an agent-gateway container, not chosen. The old default named a
    directory nothing had ever signed in to, so an invocation without an
    override opened an *empty* profile and LinkedIn answered 401 - which is
    indistinguishable from a genuinely dead session, and sent the operator at
    the one operation that invalidated this account's session globally."""
    assert supervisor.DEFAULT_PROFILE == "/opt/linkedin-cli/profile"


def test_the_requested_profile_is_the_environment_override(monkeypatch, root):
    monkeypatch.setenv(supervisor.PROFILE_ENV, str(root / "profile"))
    assert supervisor.requested_profile() == str(root / "profile")


def test_a_blank_override_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(supervisor.PROFILE_ENV, "   ")
    assert supervisor.requested_profile() == supervisor.DEFAULT_PROFILE


def test_a_launch_with_no_override_opens_the_default_profile(monkeypatch):
    monkeypatch.delenv(supervisor.PROFILE_ENV, raising=False)
    opened = []

    def open_pipe(binary, profile_dir, **kw):
        opened.append(profile_dir)
        return FakeChromium(), FakeProcess()

    supervisor.Browser.launch(binary="/opt/cloakbrowser/chrome", open_pipe=open_pipe)
    assert opened == [supervisor.DEFAULT_PROFILE]


def test_the_requested_binary_is_the_environment_override(monkeypatch):
    monkeypatch.setenv(supervisor.BINARY_ENV, "/managed/browser/chrome")
    assert supervisor.requested_binary() == "/managed/browser/chrome"


def test_a_blank_binary_override_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(supervisor.BINARY_ENV, "   ")
    assert supervisor.requested_binary() == supervisor.DEFAULT_BINARY


# ------------------------------------------------------- the confined deployment


def test_a_confined_deployment_refuses_the_built_in_binary(monkeypatch):
    """`DEFAULT_BINARY` in the container is owned by the agent gateway's own uid
    at mode 0755 - writable by the untrusted agent uid. Under the credential
    broker the CLI runs as the uid holding *every* tenant's credentials, so a
    missing or misspelled policy key falling back to that path execs whatever the
    untrusted uid last wrote there, as the credential holder, with no error and
    no audit signal. Failing closed is the only way that misconfiguration is
    visible at all."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor.requested_binary()
    assert supervisor.BINARY_ENV in str(caught.value)


def test_a_confined_deployment_refuses_the_built_in_profile(monkeypatch):
    """The other half of the same fail-open. `DEFAULT_PROFILE` is the built-in
    profile from before the broker, which this deployment moved: opening it
    reaches a directory the confined uid has no session in, and the 401 that
    comes back reads exactly like a dead account."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.delenv(supervisor.PROFILE_ENV, raising=False)
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor.requested_profile()
    assert supervisor.PROFILE_ENV in str(caught.value)


def test_a_confined_deployment_still_honours_the_overrides_it_is_given(monkeypatch):
    """Fail *closed*, not fail always: the policy sets both, and the tenant has
    to work when it does."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.setenv(supervisor.BINARY_ENV, "/managed/browser/chrome")
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/managed/profile")
    assert supervisor.requested_binary() == "/managed/browser/chrome"
    assert supervisor.requested_profile() == "/managed/profile"


def test_a_confined_launch_with_neither_override_starts_no_browser(monkeypatch):
    """The refusal has to reach the launch, which is the thing that would have
    run the attacker-writable binary."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    monkeypatch.delenv(supervisor.PROFILE_ENV, raising=False)

    def open_pipe(binary, profile_dir, **kw):
        raise AssertionError(f"a confined deployment launched {binary} on {profile_dir}")

    with pytest.raises(supervisor.SupervisorError):
        supervisor.Browser.launch(open_pipe=open_pipe)


def test_only_the_confined_deployment_fails_closed(monkeypatch):
    """The guard is armed by one exact value, because that is the one deployment
    that exists (the credential broker's `child_env` sets it). An unrecognised
    name is not treated as confined: it would refuse every developer who exported
    the variable for something else, and it would not have caught the failure
    this exists for anyway - a `LINKEDIN_DEPLOYMENT` misspelled in the policy
    arms nothing, whatever this compares against."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, "laptop")
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    monkeypatch.delenv(supervisor.PROFILE_ENV, raising=False)
    assert supervisor.requested_binary() == supervisor.DEFAULT_BINARY
    assert supervisor.requested_profile() == supervisor.DEFAULT_PROFILE


# ------------------------------------------------------------------ permissions


def test_the_socket_is_created_unreadable_to_everyone_else(root):
    path = root / "state" / SOCKET
    with contextlib.closing(supervisor.bind_socket(path)):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_directory_is_created_0700(root):
    """`mkdir(mode=...)` is masked by the umask, so the mode has to be set
    explicitly or a 0022 umask leaves the whole thing world-readable."""
    path = root / "state" / SOCKET
    with contextlib.closing(supervisor.bind_socket(path)):
        assert dir_mode(path.parent) == 0o700


def test_a_group_readable_directory_is_refused(root):
    directory = root / "state"
    directory.mkdir()
    os.chmod(directory, 0o750)
    with pytest.raises(supervisor.SupervisorError, match="0750"):
        supervisor.bind_socket(directory / SOCKET)


def test_a_world_readable_directory_is_refused(root):
    """The whole security argument rests on this bit, so it is asserted at
    startup rather than assumed from whoever created the directory."""
    directory = root / "state"
    directory.mkdir()
    os.chmod(directory, 0o755)
    with pytest.raises(supervisor.SupervisorError, match="0755"):
        supervisor.bind_socket(directory / SOCKET)


def test_a_wide_open_directory_is_refused_rather_than_quietly_narrowed(root):
    """Fixing it would hide the fact that something else opened it up, and the
    socket that was in there was reachable for however long that lasted."""
    directory = root / "state"
    directory.mkdir()
    os.chmod(directory, 0o777)
    with pytest.raises(supervisor.SupervisorError):
        supervisor.bind_socket(directory / SOCKET)
    assert dir_mode(directory) == 0o777


def test_a_directory_owned_by_someone_else_is_refused(root, monkeypatch):
    """0700 is only a boundary when *we* are the owner: `os.stat` follows
    symlinks, so a state path pointed at an attacker's own 0700 directory would
    otherwise pass the mode check and hand them the socket."""
    directory = root / "state"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(directory).st_uid + 1)
    with pytest.raises(supervisor.SupervisorError, match="owned"):
        supervisor.bind_socket(directory / SOCKET)


# ---------------------------------------------------------------- stale sockets


def leftover(path) -> os.stat_result:
    """A socket file with nobody behind it - what a killed supervisor leaves."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    server.bind(str(path))
    server.close()  # the inode stays; nothing answers on it
    return os.stat(path)


def test_a_stale_socket_file_is_replaced(root):
    path = root / "state" / SOCKET
    before = leftover(path)
    with contextlib.closing(supervisor.bind_socket(path, probe=lambda _: False)):
        assert os.stat(path).st_ino != before.st_ino
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_socket_someone_answers_on_is_never_replaced(root):
    """Two supervisors mean two browsers and two copies of the credential, and
    the second one would silently steal every later invocation."""
    path = root / "state" / SOCKET
    before = leftover(path)
    with pytest.raises(supervisor.SupervisorError, match="already"):
        supervisor.bind_socket(path, probe=lambda _: True)
    assert os.stat(path).st_ino == before.st_ino


@pytest.mark.real_process
def test_a_live_supervisor_is_detected_by_connecting_for_real(root):
    """The seam above proves the branch; this proves the wiring under it.

    A fake probe cannot show that `bind_socket` actually asks the socket, and
    getting that wrong replaces a *live* supervisor's socket - the one failure
    this whole check exists to prevent. An `AF_UNIX` path in a throwaway
    directory reaches nothing but this test.
    """
    path = root / "state" / SOCKET
    live = supervisor.bind_socket(path)
    with contextlib.closing(live):
        with pytest.raises(supervisor.SupervisorError, match="already"):
            supervisor.bind_socket(path)


@pytest.mark.real_process
def test_a_stale_socket_is_replaced_after_a_real_probe(root):
    """The other half: a refused connect is the proof that lets the file go."""
    path = root / "state" / SOCKET
    before = leftover(path)
    with contextlib.closing(supervisor.bind_socket(path)):
        assert os.stat(path).st_ino != before.st_ino


def test_the_listening_socket_is_ready_to_accept(root):
    """`bind` without `listen` refuses every connect, so an unlistened socket
    would look exactly like a stale one to the next invocation."""
    path = root / "state" / SOCKET
    with contextlib.closing(supervisor.bind_socket(path)) as server:
        assert server.family == socket.AF_UNIX
        server.settimeout(0.01)
        with pytest.raises(TimeoutError):
            server.accept()  # listening, just idle


def test_binding_falls_back_to_the_default_path(root, monkeypatch):
    monkeypatch.setenv(supervisor.SOCKET_ENV, str(root / "state" / SOCKET))
    with contextlib.closing(supervisor.bind_socket()):
        assert (root / "state" / SOCKET).exists()


# ------------------------------------------------------------------ the framing


def test_a_request_line_is_read_up_to_the_newline():
    ours, theirs = socket.socketpair()
    with contextlib.closing(ours), contextlib.closing(theirs):
        ours.sendall(b'{"op":"status"}\n')
        assert json.loads(supervisor.read_line(theirs)) == {"op": "status"}


def test_a_request_split_across_two_writes_is_reassembled():
    ours, theirs = socket.socketpair()
    with contextlib.closing(ours), contextlib.closing(theirs):
        ours.sendall(b'{"op":"sta')
        ours.sendall(b'tus"}\n')
        assert json.loads(supervisor.read_line(theirs)) == {"op": "status"}


def test_a_caller_that_hangs_up_mid_line_is_an_error_not_an_empty_request():
    ours, theirs = socket.socketpair()
    with contextlib.closing(theirs):
        ours.sendall(b'{"op":"sta')
        ours.close()
        with pytest.raises(supervisor.SupervisorError):
            supervisor.read_line(theirs)


def test_a_line_that_never_ends_is_refused_rather_than_buffered_forever():
    """One request per connection, so an endless line is a broken client - and
    buffering it is how a resident daemon dies of memory instead of saying no."""
    ours, theirs = socket.socketpair()
    with contextlib.closing(ours), contextlib.closing(theirs):
        ours.sendall(b"x" * 4096)
        with pytest.raises(supervisor.SupervisorError, match="too long"):
            supervisor.read_line(theirs, limit=1024)


def test_a_response_may_be_larger_than_a_request_is_allowed_to_be():
    """`feed list --count=30` died on this on a live run.

    One cap was serving two directions. `MAX_LINE` was sized as a bound on a
    broken *client* - "one request per connection and the largest is a write
    payload, so this is generous by three orders of magnitude" - which is true of
    a request and false of an answer. Thirty feed posts, each with an author, a
    social detail and several image artifacts, is comfortably over a megabyte,
    and the CLI reported `the supervisor stopped answering: the message is too
    long` for a page LinkedIn had returned perfectly well.

    The asymmetry is the point: a request is written by this package and can be
    bounded tightly, while a response is whatever LinkedIn decided to send.
    """
    assert supervisor.MAX_RESPONSE > supervisor.MAX_REQUEST
    ours, theirs = socket.socketpair()
    with contextlib.closing(ours), contextlib.closing(theirs):
        big = b"y" * (supervisor.MAX_REQUEST + 4096)
        sender = threading.Thread(target=lambda: ours.sendall(big + b"\n"), daemon=True)
        sender.start()
        line = supervisor.read_line(theirs, limit=supervisor.MAX_RESPONSE)
        sender.join(timeout=5)
        assert len(line) == len(big)


def test_the_request_cap_still_bounds_a_broken_client():
    """Raising the response cap must not raise the one that bounds an attacker."""
    ours, theirs = socket.socketpair()
    with contextlib.closing(ours), contextlib.closing(theirs):
        ours.sendall(b"x" * 4096)
        with pytest.raises(supervisor.SupervisorError, match="too long"):
            supervisor.read_line(theirs, limit=1024)
    assert supervisor.MAX_REQUEST == 1 << 20


def test_a_socket_path_too_long_to_bind_says_so(root):
    """The kernel's own answer is "AF_UNIX path too long" with no mention of
    which path or how long, and a relocated state directory reaches the cap
    easily - so the errno is translated rather than passed on."""
    path = root / ("d" * 120) / SOCKET
    with pytest.raises(supervisor.SupervisorError, match="bytes"):
        supervisor.bind_socket(path)


# ------------------------------------------------------------------- the daemon


class FakeClock:
    """Monotonic time, advanced only by the fakes that would really have waited."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeListener:
    """A bound socket whose `accept` is scripted.

    Nothing connects to this machine, and - the part that matters - `accept`
    advances the fake clock by exactly the timeout it was handed, which is what
    a real one does when nothing arrives. The idle deadline is therefore driven
    by the code under test rather than asserted against a wall clock, and a
    `serve` that computed the wrong budget moves time by the wrong amount.
    """

    def __init__(self, clock: FakeClock, connections=()):
        self.clock = clock
        self.queue = list(connections)
        self.timeouts: list[float] = []
        self.closed = False
        self._timeout = 0.0

    def settimeout(self, value):
        self.timeouts.append(value)
        self._timeout = value

    def accept(self):
        if self.queue:
            return self.queue.pop(0), ""
        self.clock.advance(self._timeout)
        raise TimeoutError

    def close(self):
        self.closed = True


class FakeBrowser:
    """The one browser, minus the browser. Costs fake time, like a real one."""

    def __init__(
        self,
        results=(),
        *,
        page_url=PAGE,
        profile="/state/profile",
        clock=None,
        cost=0.0,
        reload_error=None,
        seed_error=None,
    ):
        self.results = list(results)
        self.calls: list[tuple] = []
        self.closed = False
        self.profile = profile
        self.reloads = 0
        self._page_url = page_url
        self._clock = clock
        self._cost = cost
        self._reload_error = reload_error
        self._seed_error = seed_error

    def fetch(self, method, path, body):
        self.calls.append((method, path, body))
        if self._clock is not None:
            self._clock.advance(self._cost)
        if not self.results:
            raise AssertionError("the daemon asked for a fetch the test never scripted")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def reload(self):
        self.reloads += 1
        if self._reload_error is not None:
            raise self._reload_error

    def seed(self, cookies):
        if self._seed_error is not None:
            raise self._seed_error
        return {"installed": len(cookies)}

    def page_url(self):
        if isinstance(self._page_url, Exception):
            raise self._page_url
        return self._page_url

    def close(self):
        self.closed = True


@pytest.fixture
def talk():
    """Hand out (client, daemon) socket pairs, pre-loaded with one request.

    `socketpair` rather than a connect: the pair is already joined, so the real
    framing is exercised without anything on this machine being connected to.
    """
    made = []

    def make(payload=None, raw=None):
        ours, theirs = socket.socketpair()
        made.append((ours, theirs))
        ours.settimeout(2.0)
        if payload is not None:
            ours.sendall(json.dumps(payload).encode() + b"\n")
        if raw is not None:
            ours.sendall(raw)
        return ours, theirs

    yield make
    for pair in made:
        for end in pair:
            end.close()


def reply(sock) -> dict:
    """The daemon's one answer, or a failure that names the silence."""
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(f"the daemon closed without answering (got {data!r})")
        data += chunk
    return json.loads(data.split(b"\n")[0])


def silent(sock) -> bool:
    """True when the daemon never answered on this connection.

    `serve` has already returned by the time this is called, so anything the
    daemon was going to write is in the buffer already and the wait is purely
    the cost of proving absence. The fixture's 2 s timeout - right for a read
    that expects an answer - was two seconds of dead wall clock here.
    """
    sock.settimeout(0.05)
    try:
        return sock.recv(65536) == b""
    except TimeoutError:
        return True
    finally:
        sock.settimeout(2.0)


def serve(root, connections, browser=None, *, clock=None, idle_timeout=900.0, launch=None):
    clock = clock or FakeClock()
    listener = FakeListener(clock, connections)
    browser = FakeBrowser() if browser is None else browser
    supervisor.serve(
        root / SOCKET,
        browser=browser,
        idle_timeout=idle_timeout,
        listener=listener,
        now=clock,
        launch=launch,
    )
    return listener, browser


FETCH = {"op": "fetch", "method": "GET", "path": "/voyager/api/me", "body": None}

RESULT = {
    "status": 200,
    "headers": {"content-type": "application/json"},
    "body": '{"data":{"plainId":42}}',
    "url": "https://www.linkedin.com/voyager/api/me",
    "redirected": False,
}


def test_a_fetch_round_trips_the_raw_result(root, talk):
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([RESULT]))
    assert reply(ours) == RESULT


def test_the_method_path_and_body_reach_the_browser_untouched(root, talk):
    """The path is passed through unresolved: the page is already on
    linkedin.com, so the browser resolves it - which is why the daemon needs no
    BASE and no idea what Voyager is."""
    ours, theirs = talk({"op": "fetch", "method": "post", "path": "/x", "body": {"a": 1}})
    _, browser = serve(root, [theirs], FakeBrowser([RESULT]))
    assert browser.calls == [("POST", "/x", {"a": 1})]


def test_the_daemon_never_classifies_what_it_carries(root, talk):
    """A 999 served from a checkpoint URL is `Blocked` - and saying so is
    `transport.py`'s job. A copy of that taxonomy in the resident process would
    drift from the real one within a deploy, and this is the process that is
    awkward to restart."""
    blocked = dict(RESULT, status=999, url="https://www.linkedin.com/checkpoint/challenge")
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([blocked]))
    answer = reply(ours)
    assert answer == blocked
    assert "kind" not in answer


def test_one_connection_carries_exactly_one_request(root, talk):
    ours, theirs = talk(FETCH)
    ours.sendall(json.dumps(FETCH).encode() + b"\n")
    _, browser = serve(root, [theirs], FakeBrowser([RESULT, RESULT]))
    assert len(browser.calls) == 1


def test_a_fetch_that_threw_inside_the_page_is_upstream(root, talk):
    """The injected script catches its own `fetch` throwing; naming the
    transport kind is all the daemon adds to it.

    Deliberately not `TypeError: Failed to fetch`, which this test used to use:
    that string is the signature of a page whose network service has died, and
    it now costs a re-navigation and a retry. The recovery section below is
    where that case belongs.
    """
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([{"error": "TypeError: url is not a string"}]))
    assert reply(ours) == {"error": "TypeError: url is not a string", "kind": "upstream"}


def test_a_dead_browser_surfaces_as_closed(root, talk):
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([pipe.PipeClosed("the browser closed the CDP pipe")]))
    answer = reply(ours)
    assert answer["kind"] == "closed"
    assert "CDP pipe" in answer["error"]


def test_a_dead_browser_stops_the_daemon_so_the_next_call_gets_a_fresh_one(root, talk):
    """A browser reachable only through descriptors this process holds cannot
    come back, and a supervisor still answering with a dead one behind it would
    fail every call until someone noticed. Exiting hands the next invocation a
    clean autostart."""
    ours, theirs = talk(FETCH)
    _, second = talk(FETCH)
    _, browser = serve(root, [theirs, second], FakeBrowser([pipe.PipeClosed("gone"), RESULT]))
    assert reply(ours)["kind"] == "closed"
    assert len(browser.calls) == 1
    assert browser.closed


def test_a_slow_browser_surfaces_as_timeout(root, talk):
    """`cdp.CDPSession.call` catches TimeoutError and re-raises a CDPError that
    is not one, so `__cause__` is the only surviving evidence that the browser
    was slow rather than the script broken - and the two differ in the remedy:
    wait it out, or relaunch."""
    slow = cdp.CDPError("timed out waiting for a reply to Runtime.evaluate")
    slow.__cause__ = pipe.PipeTimeout("timed out waiting for the browser")
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([slow]))
    assert reply(ours)["kind"] == "timeout"


def test_a_javascript_error_is_upstream_not_a_timeout(root, talk):
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([cdp.CDPError("javascript threw: ReferenceError")]))
    assert reply(ours)["kind"] == "upstream"


# -------------------------------------------------------------- dead page recovery

# What a page whose network service has died under it says to every fetch. The
# Mac supervisor had been up 44 hours: `page_url()` still read /feed/, and this
# came back from every single call.
DEAD = {"error": "TypeError: Failed to fetch"}


def test_a_dead_page_is_re_navigated_and_the_read_retried(root, talk):
    """The worst defect in the tree for an always-on gateway: there was no
    health check, no re-navigation and no relaunch, so every surface in `doctor`
    reported the same failure and the CLI stayed dead until it was killed by
    hand."""
    ours, theirs = talk(FETCH)
    browser = FakeBrowser([DEAD, RESULT])
    serve(root, [theirs], browser)
    assert reply(ours) == RESULT
    assert browser.reloads == 1
    assert len(browser.calls) == 2


def test_an_answer_from_linkedin_is_never_mistaken_for_a_dead_page(root, talk):
    """A 500, a 999 or a rotated queryId are pages that *answered*. Recovering
    from each of them would reload the feed on every upstream hiccup."""
    ours, theirs = talk(FETCH)
    browser = FakeBrowser([dict(RESULT, status=500)])
    serve(root, [theirs], browser)
    assert reply(ours)["status"] == 500
    assert browser.reloads == 0


def test_a_write_is_recovered_from_but_never_re_issued(root, talk):
    """The fetch may have reached LinkedIn before it threw, so a resend turns
    one comment into two - which is what `OutcomeUnknown` exists to prevent. The
    page is repaired for whoever asks next; this caller is told it failed."""
    ours, theirs = talk({"op": "fetch", "method": "POST", "path": "/x", "body": {"a": 1}})
    browser = FakeBrowser([DEAD])
    serve(root, [theirs], browser)
    assert reply(ours)["kind"] == "upstream"
    assert browser.reloads == 1
    assert len(browser.calls) == 1


def test_a_page_that_will_not_re_navigate_costs_the_browser_its_life(root, talk):
    ours, theirs = talk(FETCH)
    dead = FakeBrowser([DEAD], reload_error=cdp.CDPError("the page is gone"))
    fresh = FakeBrowser([RESULT])
    serve(root, [theirs], dead, launch=lambda: fresh)
    assert reply(ours) == RESULT
    assert dead.closed, "a replaced browser nothing holds a pipe to is immortal"
    assert len(fresh.calls) == 1


def test_the_replacement_serves_every_later_request(root, talk):
    ours, theirs = talk(FETCH)
    status_ours, status_theirs = talk({"op": "status"})
    dead = FakeBrowser([DEAD], reload_error=cdp.CDPError("gone"), profile="/old")
    fresh = FakeBrowser([RESULT], profile="/new")
    _, _ = serve(root, [theirs, status_theirs], dead, launch=lambda: fresh)
    assert reply(ours) == RESULT
    assert reply(status_ours)["profile"] == "/new"
    assert fresh.closed


def test_a_browser_that_cannot_be_relaunched_stops_the_daemon(root, talk):
    """Exiting hands the next invocation a clean autostart, which is strictly
    better than a resident supervisor that fails every call forever."""
    ours, theirs = talk(FETCH)
    _, second = talk(FETCH)
    dead = FakeBrowser([DEAD], reload_error=cdp.CDPError("gone"))

    def refuse():
        raise supervisor.SupervisorError("chrome will not start")

    listener, _ = serve(root, [theirs, second], dead, launch=refuse)
    answer = reply(ours)
    assert answer["kind"] == "closed"
    assert "Failed to fetch" in answer["error"]
    assert listener.closed


def test_recovery_is_bounded_rather_than_looped(root, talk):
    """Two rungs, one attempt each, and at most one extra read per rung. A
    supervisor that kept trying would hammer LinkedIn from a page that cannot
    talk to it."""
    ours, theirs = talk(FETCH)
    dead = FakeBrowser([DEAD] * 8)
    fresh = FakeBrowser([DEAD] * 8)
    serve(root, [theirs], dead, launch=lambda: fresh)
    assert reply(ours)["kind"] == "closed"
    assert len(dead.calls) + len(fresh.calls) == 3


def test_only_one_relaunch_is_ever_attempted(root, talk):
    """The bound is per daemon rather than per request: a browser that dies
    again after being replaced is the next invocation's clean autostart, not a
    third browser started from this one."""
    ours, theirs = talk(FETCH)
    second_ours, second = talk(FETCH)
    gone = cdp.CDPError("the page is gone")
    dead = FakeBrowser([DEAD], reload_error=gone)
    fresh = FakeBrowser([RESULT, DEAD], reload_error=gone)
    launched = []

    def launch():
        launched.append(fresh)
        return fresh

    serve(root, [theirs, second], dead, launch=launch)
    assert reply(ours) == RESULT
    assert reply(second_ours)["kind"] == "closed"
    assert len(launched) == 1


def test_a_recovery_is_written_down_where_an_operator_can_find_it(root, talk, capsys):
    ours, theirs = talk(FETCH)
    serve(root, [theirs], FakeBrowser([DEAD, RESULT]))
    assert reply(ours) == RESULT
    assert "renavigate" in capsys.readouterr().err


def test_a_broken_request_does_not_take_the_daemon_down(root, talk):
    """The daemon is resident and shared; one malformed line must cost the
    caller its answer, not everyone else their browser."""
    ours, theirs = talk(raw=b"this is not json\n")
    second_ours, second = talk(FETCH)
    _, browser = serve(root, [theirs, second], FakeBrowser([RESULT]))
    assert reply(ours)["kind"] == "upstream"
    assert reply(second_ours) == RESULT


def test_an_unknown_op_is_reported_rather_than_ignored(root, talk):
    ours, theirs = talk({"op": "evaluate", "expression": "document.cookie"})
    serve(root, [theirs])
    answer = reply(ours)
    assert answer["kind"] == "upstream"
    assert "evaluate" in answer["error"]


def test_a_request_that_is_not_an_object_is_refused(root, talk):
    ours, theirs = talk(["fetch", "/voyager/api/me"])
    serve(root, [theirs])
    assert reply(ours)["kind"] == "upstream"


def test_a_caller_that_hangs_up_before_asking_is_not_fatal(root, talk):
    ours, theirs = talk()
    ours.close()
    second_ours, second = talk(FETCH)
    serve(root, [theirs, second], FakeBrowser([RESULT]))
    assert reply(second_ours) == RESULT


# ------------------------------------------------------------------------ status


# A JSESSIONID value in the shape `transport._SECRET_VALUES` matches, and the one
# `tools/leakcheck.py` already knows is synthetic. LinkedIn echoes this exact
# value back as `?ct=` on a checkpoint URL (`transport.py`), so what these two
# pin is the real leak rather than a stand-in for it.
CSRF_TOKEN = "ajax:1111222233334444555"
CHECKPOINT = f"https://www.linkedin.com/checkpoint/challenge/AgHRk?ct={CSRF_TOKEN}"


def test_status_reports_the_path_of_the_live_page(root, talk):
    """Read from the document rather than echoed from a constant: "which page
    is it actually on" is the question status exists to answer, and a redirect
    to a checkpoint is exactly the answer an operator needs to see. The path is
    what carries that diagnosis, which is why the field survives at all."""
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(page_url=CHECKPOINT))
    assert reply(ours)["page_url"] == "/checkpoint/challenge/AgHRk"


def test_status_never_hands_out_the_query_string_of_the_live_page(root, talk):
    """The `?ct=` a checkpoint URL carries back *is* the JSESSIONID cookie value
    (`transport.py`), so `location.href` returned whole is a live credential.

    Cut here, in the daemon, because there are two consumers and only one of
    them was ever projected: `cli.doctor` reduced the field on its way into the
    envelope, while `browser.py` returns this same dict verbatim as `runs_in` on
    every `--dry-run` preview - a path with no LinkedIn round trip at all, and
    therefore a repeatable oracle for anyone who can spell `--dry-run`."""
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(page_url=CHECKPOINT))
    assert CSRF_TOKEN not in json.dumps(reply(ours))


def test_status_still_names_a_page_that_has_no_path(root, talk):
    """`chrome-error://chromewebdata` is the URL a browser lands on when the
    request never made it - the shape a Cookie header grown too large produces,
    which looks nothing like a cookie problem (`Browser.seed`). Its path is
    empty, so cutting to the path alone reports the one page that most needs
    naming as `""`. The scheme and host carry no query and no token."""
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(page_url="chrome-error://chromewebdata"))
    assert reply(ours)["page_url"] == "chrome-error://chromewebdata"


@pytest.mark.parametrize(
    "href, reported",
    [
        # The page every target is created on, and the one `Browser._navigate`
        # names when a load does not take. `urlsplit` calls the whole of an
        # opaque URL its `.path`, so cutting to the path alone renamed the one
        # diagnosis in this module that has its own error message to "blank".
        ("about:blank", "about:blank"),
        # Same shape, and it is why the fix is the scheme rather than a literal.
        ("chrome://newtab", "chrome://newtab"),
        ("chrome-error://chromewebdata", "chrome-error://chromewebdata"),
        (CHECKPOINT, "/checkpoint/challenge/AgHRk"),
        ("https://www.linkedin.com/feed/", "/feed/"),
        ("", ""),
    ],
)
def test_the_reported_page_url_keeps_the_diagnosis_and_drops_the_query(href, reported):
    assert supervisor._reported_page_url(href) == reported


def test_status_still_names_the_page_a_target_is_created_on(root, talk):
    """`about:blank` is a named failure state in this module - the navigation
    error at the bottom of `Browser._navigate` is written about exactly it - and
    a status that answered "blank" sends an operator looking for a page by that
    name instead of reading the browser as never having left its blank tab."""
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(page_url="about:blank"))
    assert reply(ours)["page_url"] == "about:blank"


def test_status_reports_the_pid_socket_and_profile(root, talk):
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(profile="/state/linkedin/profile"))
    answer = reply(ours)
    assert answer["pid"] == os.getpid()
    assert answer["socket"] == str(root / SOCKET)
    assert answer["profile"] == "/state/linkedin/profile"
    assert answer["started_at"] <= time.time()


def test_status_from_a_wedged_browser_is_an_error_not_a_hang(root, talk):
    ours, theirs = talk({"op": "status"})
    serve(root, [theirs], FakeBrowser(page_url=cdp.CDPError("javascript threw")))
    assert reply(ours)["kind"] == "upstream"


# ---------------------------------------------------------------------- lifetime


def test_shutdown_is_acknowledged_and_stops_the_daemon(root, talk):
    ours, theirs = talk({"op": "shutdown"})
    second_ours, second = talk(FETCH)
    listener, browser = serve(root, [theirs, second], FakeBrowser([RESULT]))
    assert reply(ours) == {"ok": True}
    assert browser.calls == []
    assert silent(second_ours)
    assert listener.closed


def test_the_idle_timeout_fires_on_the_fake_clock(root):
    """A forgotten browser must not live forever - it holds the credential and
    keeps rendering LinkedIn at an account nobody is using."""
    clock = FakeClock()
    listener, browser = serve(root, [], clock=clock, idle_timeout=900.0)
    assert listener.timeouts == [900.0]
    assert clock.now == 1900.0
    assert browser.closed


def test_a_request_resets_the_idle_deadline(root, talk):
    """Otherwise the daemon exits mid-session on the strength of how long ago
    it started rather than how long it has been unused."""
    clock = FakeClock()
    _, theirs = talk(FETCH)
    browser = FakeBrowser([RESULT], clock=clock, cost=60.0)
    listener, _ = serve(root, [theirs], browser, clock=clock, idle_timeout=900.0)
    # Without the reset the second budget would be the 840 s left of the first.
    assert listener.timeouts == [900.0, 900.0]


def test_the_browser_is_closed_even_when_it_was_injected(root):
    """A browser is reachable only through the descriptors this process holds,
    so one that outlives its supervisor is unreachable *and* immortal - it has
    to be closed on the way out whoever created it."""
    _, browser = serve(root, [], idle_timeout=1.0)
    assert browser.closed


def test_the_socket_file_is_removed_when_the_daemon_exits(root):
    """A file left behind is a socket every later invocation fails to connect
    to; `bind_socket` can recover from it, but only after proving it is dead."""
    path = root / SOCKET
    server = supervisor.bind_socket(path)
    with contextlib.closing(server):
        clock = FakeClock()
        supervisor.serve(
            path,
            browser=FakeBrowser(),
            idle_timeout=1.0,
            listener=FakeListener(clock),
            now=clock,
        )
    assert not path.exists()


def test_the_daemon_launches_its_own_browser_when_none_is_injected(root, talk):
    launched = FakeBrowser(profile="/launched")
    ours, theirs = talk({"op": "status"})
    clock = FakeClock()
    supervisor.serve(
        root / SOCKET,
        idle_timeout=900.0,
        listener=FakeListener(clock, [theirs]),
        now=clock,
        launch=lambda: launched,
    )
    assert reply(ours)["profile"] == "/launched"
    assert launched.closed


def test_a_browser_that_will_not_launch_leaves_no_socket_behind(root):
    """The client waiting on that socket gets a refused connect and starts a
    fresh supervisor; a file left behind would make it wait for one that is
    never coming."""
    path = root / SOCKET
    server = supervisor.bind_socket(path)
    with contextlib.closing(server):
        clock = FakeClock()

        def explode():
            raise supervisor.SupervisorError("chrome is not installed")

        with pytest.raises(supervisor.SupervisorError):
            supervisor.serve(path, listener=FakeListener(clock), now=clock, launch=explode)
    assert not path.exists()


# ------------------------------------------------------------------ the client


class FakeSocket:
    """The client end of a connection, with `connect` answered by a fake daemon.

    Everything after the connect is a real `socketpair`, so the framing on the
    wire is exercised for real - it is only the reaching-out that is faked, and
    that is the one part that would otherwise touch this machine.
    """

    def __init__(self, daemon):
        self._daemon = daemon
        self._sock = None
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)
        if self._sock is not None:
            self._sock.settimeout(value)

    def connect(self, address):
        self._daemon.events.append(("connect", str(address)))
        if not self._daemon.up:
            raise ConnectionRefusedError(61, "Connection refused")
        self._sock = self._daemon.open()
        self._sock.settimeout(2.0)

    def sendall(self, data):
        self._sock.sendall(data)

    def recv(self, size):
        return self._sock.recv(size)

    def close(self):
        if self._sock is not None:
            self._sock.close()


class FakeDaemon:
    """A supervisor that may or may not be listening, without one running."""

    def __init__(self, *, up=False, response=None, hang_up=False, events=None):
        self.up = up
        self.response = {"ok": True} if response is None else response
        self.hang_up = hang_up
        # Shared with the lock and spawn fakes when a test cares about the order
        # the three of them happen in.
        self.events: list[tuple] = [] if events is None else events
        self.heard: list = []
        self._pairs: list = []

    def socket(self):
        return FakeSocket(self)

    def open(self):
        ours, theirs = socket.socketpair()
        self._pairs.append((ours, theirs))
        if self.hang_up:
            theirs.close()  # accepted the connection, then died before answering
        else:
            theirs.sendall(json.dumps(self.response).encode() + b"\n")
            self.heard.append(theirs)
        return ours

    def close(self):
        for pair in self._pairs:
            for end in pair:
                end.close()


@pytest.fixture
def daemon():
    made = []

    def make(**kw):
        fake = FakeDaemon(**kw)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


class RecordingLock:
    """The launch lock, single-process.

    `flock` is granted per open file description, so two acquisitions from one
    process do **not** block each other and a naive same-process test of mutual
    exclusion hangs forever instead - `state.py:88-90` documents exactly that.
    So the lock is a seam, and the race is played out by letting `on_acquire`
    change the world the way the invocation that won the lock would have.
    """

    def __init__(self, events, on_acquire=None):
        self.events = events
        self.paths: list = []
        self._on_acquire = on_acquire

    @contextlib.contextmanager
    def __call__(self, path):
        self.paths.append(path)
        self.events.append(("acquire", str(path)))
        if self._on_acquire is not None:
            self._on_acquire()
        try:
            yield
        finally:
            self.events.append(("release", str(path)))


class RecordingSpawn:
    def __init__(self, events, *, daemon=None, error=None):
        self.events = events
        self.calls: list = []
        self._daemon = daemon
        self._error = error

    def __call__(self, path):
        self.calls.append(path)
        self.events.append(("spawn", str(path)))
        if self._error is not None:
            raise self._error
        if self._daemon is not None:
            self._daemon.up = True


class FakeSleep:
    """Time passing on the fake clock, with the world allowed to change.

    A real sleep in this suite is the mistake that once took it from 7 s to
    50 s, so the wait loop's own sleep is what moves the clock - and, when the
    test asks for it, what finally brings the daemon up.
    """

    def __init__(self, clock, *, daemon=None, after=None):
        self.clock = clock
        self.slept: list[float] = []
        self._daemon = daemon
        self._after = after

    def __call__(self, seconds):
        self.slept.append(seconds)
        self.clock.advance(seconds)
        if self._after is not None and len(self.slept) >= self._after:
            self._daemon.up = True


def ask(fake, path, payload=None, **kw):
    kw.setdefault("sock", fake.socket)
    return supervisor.request(payload or {"op": "status"}, socket_path=path, **kw)


def test_a_request_round_trips_over_the_socket(root, daemon):
    fake = daemon(up=True, response=RESULT)
    assert ask(fake, root / SOCKET, FETCH) == RESULT


def test_the_request_is_sent_as_one_newline_terminated_line(root, daemon):
    fake = daemon(up=True)
    ask(fake, root / SOCKET, FETCH)
    assert fake.heard[0].recv(65536) == json.dumps(FETCH).encode() + b"\n"


def test_the_request_goes_to_the_socket_it_was_given(root, daemon):
    fake = daemon(up=True)
    ask(fake, root / SOCKET)
    assert fake.events == [("connect", str(root / SOCKET))]


def test_a_missing_supervisor_is_reported_when_autostart_is_off(root, daemon):
    """`browser.py` checks for `error` on every result, so a failure to reach
    the daemon arrives in the same shape as a failure inside it."""
    fake = daemon(up=False)
    events: list = []
    spawn = RecordingSpawn(events)
    answer = ask(fake, root / SOCKET, autostart=False, spawn=spawn)
    assert answer["kind"] == "closed"
    assert spawn.calls == []


def test_a_missing_supervisor_is_started_and_then_asked(root, daemon):
    fake = daemon(up=False, response=RESULT)
    events: list = []
    spawn = RecordingSpawn(events, daemon=fake)
    answer = ask(fake, root / SOCKET, FETCH, spawn=spawn, lock=RecordingLock(events))
    assert answer == RESULT
    assert len(spawn.calls) == 1


def test_a_supervisor_another_invocation_started_is_not_started_again(root, daemon):
    """Two CLI processes racing must produce exactly one browser. The loser
    blocks on the lock, and by the time it gets in the winner is already
    listening - so the re-check under the lock is what prevents the second
    launch, not the lock itself."""
    fake = daemon(up=False, response=RESULT)
    events: list = []
    spawn = RecordingSpawn(events, daemon=fake)

    def the_winner_finishes():
        fake.up = True

    lock = RecordingLock(events, on_acquire=the_winner_finishes)
    assert ask(fake, root / SOCKET, FETCH, spawn=spawn, lock=lock) == RESULT
    assert spawn.calls == []


def test_the_recheck_and_the_spawn_both_happen_under_the_lock(root, daemon):
    """A spawn outside the lock, or a re-check before it, is the same bug: two
    supervisors, two browsers, two copies of the credential."""
    events: list = []
    fake = daemon(up=False, response=RESULT, events=events)
    spawn = RecordingSpawn(events, daemon=fake)
    ask(fake, root / SOCKET, FETCH, spawn=spawn, lock=RecordingLock(events))
    assert [name for name, _ in events] == [
        "connect",  # the first look, unlocked and cheap
        "acquire",
        "connect",  # ...and again, now that nobody else can be starting one
        "spawn",
        "connect",  # the wait for it to answer
        "release",
    ]


def test_the_lock_is_a_sibling_of_the_socket(root, daemon):
    """Not the socket itself: that file is unlinked and recreated on every
    start, and a lock on a replaced inode guards nothing."""
    fake = daemon(up=False, response=RESULT)
    events: list = []
    lock = RecordingLock(events)
    ask(fake, root / SOCKET, spawn=RecordingSpawn(events, daemon=fake), lock=lock)
    assert lock.paths == [root / (SOCKET + supervisor.LOCK_SUFFIX)]


def test_the_lock_is_released_when_the_spawn_fails(root, daemon):
    """A lock leaked here wedges every later invocation on a supervisor that
    was never started."""
    fake = daemon(up=False)
    events: list = []
    spawn = RecordingSpawn(events, error=OSError("no such file: chrome"))
    answer = ask(fake, root / SOCKET, spawn=spawn, lock=RecordingLock(events))
    assert answer["kind"] == "closed"
    assert "chrome" in answer["error"]
    assert events[-1][0] == "release"


def test_the_wait_polls_until_the_supervisor_answers(root, daemon):
    """Chromium takes seconds to start; connecting once and giving up would
    make every cold start a failure."""
    clock = FakeClock()
    fake = daemon(up=False, response=RESULT)
    events: list = []
    sleep = FakeSleep(clock, daemon=fake, after=3)
    answer = ask(
        fake,
        root / SOCKET,
        FETCH,
        spawn=RecordingSpawn(events),  # the spawn itself brings nothing up
        lock=RecordingLock(events),
        sleep=sleep,
        now=clock,
    )
    assert answer == RESULT
    assert sleep.slept == [supervisor.POLL_INTERVAL] * 3


def test_a_supervisor_that_never_answers_is_reported_rather_than_waited_on(root, daemon):
    clock = FakeClock()
    fake = daemon(up=False)
    events: list = []
    answer = ask(
        fake,
        root / SOCKET,
        spawn=RecordingSpawn(events),
        lock=RecordingLock(events),
        sleep=FakeSleep(clock),
        now=clock,
    )
    assert answer["kind"] == "closed"
    assert "did not" in answer["error"]
    assert clock.now >= 1000.0 + supervisor.STARTUP_TIMEOUT


def test_a_supervisor_that_dies_mid_request_is_never_retried(root, daemon):
    """The request may already have reached the page, and a blind resend of a
    write duplicates it - which is `OutcomeUnknown`'s whole reason for
    existing. Only a connect that never happened may be started over."""
    fake = daemon(up=True, hang_up=True)
    events: list = []
    spawn = RecordingSpawn(events)
    answer = ask(
        fake,
        root / SOCKET,
        {"op": "fetch", "method": "POST", "path": "/x", "body": {}},
        spawn=spawn,
        lock=RecordingLock(events),
    )
    assert answer["kind"] == "closed"
    assert spawn.calls == []


def test_an_answer_that_is_not_an_object_is_refused(root, daemon):
    fake = daemon(up=True, response=[1, 2, 3])
    assert ask(fake, root / SOCKET)["kind"] == "closed"


def test_the_socket_path_defaults_to_the_environment(root, daemon, monkeypatch):
    monkeypatch.setenv(supervisor.SOCKET_ENV, str(root / SOCKET))
    fake = daemon(up=True, response=RESULT)
    assert supervisor.request(FETCH, sock=fake.socket) == RESULT
    assert fake.events == [("connect", str(root / SOCKET))]


# ------------------------------------------------------- the caller's profile


def test_a_status_reply_names_the_profile_this_invocation_asked_for(root, daemon, monkeypatch):
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/opt/linkedin-cli/profile")
    fake = daemon(up=True, response={"pid": 7, "profile": "/opt/linkedin-cli/profile"})
    answer = ask(fake, root / SOCKET, {"op": "status"})
    assert answer["requested_profile"] == "/opt/linkedin-cli/profile"
    assert answer["profile_mismatch"] is False
    assert "warning" not in answer


def test_a_supervisor_serving_another_profile_is_reported_loudly(root, daemon, monkeypatch):
    """The profile binds at launch, so once one supervisor is resident every
    later invocation is served by *its* profile whatever the environment says -
    and `doctor` reported the running one as though it were the intended one.
    Only this side knows both halves, which is why the comparison lives here."""
    monkeypatch.setenv(supervisor.PROFILE_ENV, "/opt/linkedin-cli/profile")
    fake = daemon(up=True, response={"pid": 7, "profile": "/opt/linkedin-cli/other-profile"})
    answer = ask(fake, root / SOCKET, {"op": "status"})
    assert answer["profile_mismatch"] is True
    warning = answer["warning"]
    assert "/opt/linkedin-cli/other-profile" in warning
    assert "/opt/linkedin-cli/profile" in warning
    assert "7" in warning, "an operator who has to stop it needs the pid"


def test_a_reply_to_anything_but_status_is_left_alone(root, daemon):
    """The daemon's answers are carried verbatim - the CLI is what knows which
    keys mean anything - and a fetch result with extra keys in it would be a
    second taxonomy growing in the client."""
    fake = daemon(up=True, response=RESULT)
    assert ask(fake, root / SOCKET, FETCH) == RESULT


def test_a_status_that_failed_is_not_annotated(root, daemon):
    fake = daemon(up=True, response={"error": "the browser is gone", "kind": "closed"})
    answer = ask(fake, root / SOCKET, {"op": "status"})
    assert "profile_mismatch" not in answer


def test_a_confined_invocation_with_no_profile_key_blames_the_policy_not_the_daemon(
    root, daemon, monkeypatch
):
    """The annotation asks `requested_profile()`, which under the credential
    broker *refuses* rather than defaulting - and that refusal was raised inside
    the same `except` that means "the connection died mid-request". So a
    supervisor that answered perfectly was reported as one that had stopped
    answering, sending an operator to restart a daemon that is fine while the key
    the policy dropped goes unmentioned. A misconfiguration has to read as a
    misconfiguration."""
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.delenv(supervisor.PROFILE_ENV, raising=False)
    fake = daemon(up=True, response={"pid": 7, "profile": "/managed/profile"})
    answer = ask(fake, root / SOCKET, {"op": "status"})
    assert answer["kind"] == "config"
    assert supervisor.PROFILE_ENV in answer["error"]
    assert "stopped answering" not in answer["error"]


# ------------------------------------ one cause, one classification for it


def test_a_lost_policy_key_is_classified_by_kind_rather_than_by_where_it_landed():
    """`no_fallback` is the one refusal every confined resolver raises, and it
    was reported as two different things depending on which side of the socket
    it was raised on: `config` from the client's annotation, `upstream` from the
    daemon, because `_kind` had no branch for it and fell through to its
    transport default. An operator reading `upstream` restarts a browser over a
    key the policy dropped."""
    lost = supervisor.no_fallback("profile", supervisor.PROFILE_ENV, "because.")
    assert supervisor._kind(lost) == "config"
    assert isinstance(lost, supervisor.SupervisorError)


def test_a_lost_policy_key_reaching_the_daemon_is_reported_as_config(root, talk):
    """The daemon's half of the same cause. `Browser.launch` resolves both the
    binary and the profile, and under the credential broker either can refuse -
    which arrives in `_dispatch`'s broad except like any other failure."""
    ours, theirs = talk({"op": "status"})
    lost = supervisor.no_fallback("binary", supervisor.BINARY_ENV, "because.")
    serve(root, [theirs], FakeBrowser(page_url=lost))
    answer = reply(ours)
    assert answer["kind"] == "config"
    assert supervisor.BINARY_ENV in answer["error"]


def test_a_lost_ledger_key_is_answered_rather_than_raised(monkeypatch):
    """`request` promises never to raise, and this is the one input that made it.

    The socket path is derived from the ledger's parent, and `state.resolve_path`
    refuses under the credential broker rather than defaulting - so the same lost key that
    `_annotate` reports as `config` escaped this function as an exception before
    anything was connected. `browser.py` catches it as "the browser supervisor
    failed" and calls a POST that never left this process `outcome_unknown`,
    which tells an agent a message may have landed when nothing was sent.
    """
    monkeypatch.setenv(supervisor.DEPLOYMENT_ENV, supervisor.CONFINED_DEPLOYMENT)
    monkeypatch.delenv(state.STATE_FILE_ENV, raising=False)
    monkeypatch.delenv(supervisor.SOCKET_ENV, raising=False)
    answer = supervisor.request({"op": "status"}, autostart=False)
    assert answer["kind"] == "config"
    assert state.STATE_FILE_ENV in answer["error"]


# ------------------------------------------------------------------ the log


def test_the_log_sits_beside_the_socket(root):
    assert supervisor.log_path(root / SOCKET) == root / (SOCKET + supervisor.LOG_SUFFIX)


def test_the_spawned_supervisor_writes_its_output_to_that_log(root):
    """DEVNULL is how a browser that would not start produced *nothing* for the
    operator: the daemon is detached, so its own two descriptors are the only
    place its failure can be written down, and diagnosing one took a hand-driven
    `Browser.launch` instead."""
    seen = []

    def popen(argv, **kwargs):
        os.write(kwargs["stdout"], b"chrome is not installed\n")
        seen.append(kwargs)

    supervisor.spawn_supervisor(root / "state" / SOCKET, popen=popen)
    assert seen[0]["stderr"] == subprocess.STDOUT
    log = root / "state" / (SOCKET + supervisor.LOG_SUFFIX)
    assert log.read_text() == "chrome is not installed\n"


def test_the_log_is_0600_inside_the_0700_directory(root):
    """It sits beside the socket, so it inherits the boundary the socket rests
    on. The daemon is careful never to print a credential; a file it writes
    unattended must not be readable by another uid if that ever stops being
    true."""
    supervisor.spawn_supervisor(root / "state" / SOCKET, popen=lambda argv, **kw: None)
    log = root / "state" / (SOCKET + supervisor.LOG_SUFFIX)
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    assert dir_mode(log.parent) == 0o700


def test_the_entry_point_records_why_it_refused_to_start(monkeypatch, root, capsys):
    def refuse(path):
        raise supervisor.SupervisorError("the state directory is mode 0755")

    monkeypatch.setattr(supervisor, "serve", refuse)
    assert supervisor.main([str(root / SOCKET)]) == supervisor.SupervisorError.exit_code
    assert "mode 0755" in capsys.readouterr().err


def test_an_unexpected_failure_is_recorded_rather_than_lost(monkeypatch, root, capsys):
    """A bare traceback from a detached process goes nowhere at all."""

    def explode(path):
        raise RuntimeError("the pipe helper vanished")

    monkeypatch.setattr(supervisor, "serve", explode)
    assert supervisor.main([str(root / SOCKET)]) == supervisor.SupervisorError.exit_code
    err = capsys.readouterr().err
    assert "the pipe helper vanished" in err
    assert "Traceback" in err


def test_a_failing_seed_is_never_written_to_the_log(root, talk, capsys):
    """The log outlives every invocation, so anything in it is on disk until
    somebody deletes it - which is the point for a failure and a liability for
    a cookie. `seed` is the one op that is handed a credential at all."""
    secret = "AQEDATestSentinelLiAtValue"
    ours, theirs = talk({"op": "seed", "cookies": [{"name": "li_at", "value": secret}]})
    browser = FakeBrowser(seed_error=cdp.CDPError(f"Network.setCookie refused {secret}"))
    serve(root, [theirs], browser)
    assert reply(ours)["kind"] == "upstream"
    assert secret not in capsys.readouterr().err


def test_the_spawned_command_re_runs_this_module_on_that_socket(root):
    """`python -m linkedin_cli.supervisor <socket>` rather than a CLI verb: the
    daemon has to be startable without depending on how the CLI dispatches."""
    assert supervisor.supervisor_argv(root / SOCKET) == [
        sys.executable,
        "-m",
        "linkedin_cli.supervisor",
        str(root / SOCKET),
    ]


def test_the_spawn_detaches_the_supervisor_from_the_caller(root):
    """It has to outlive the invocation that started it, and a Ctrl-C or a
    dropped ssh session must not take the browser down with the CLI.

    stdin stays DEVNULL - the daemon is never asked anything on it - but stdout
    and stderr no longer are; see the log tests above for where they go now.
    """
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))

    supervisor.spawn_supervisor(root / SOCKET, popen=popen)
    _, kwargs = calls[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] != subprocess.DEVNULL
    assert kwargs["stderr"] != subprocess.DEVNULL


def test_the_default_spawn_really_starts_a_process(root):
    """Every other spawn test drives a fake, so nothing else proves the default
    is a real `Popen`. The conftest guard answers here: its refusal *is* the
    assertion that the real one would have been called."""
    with pytest.raises(AssertionError, match=r"Popen is blocked in tests"):
        supervisor.spawn_supervisor(root / SOCKET)


def test_the_module_entry_point_serves_the_socket_it_is_given(monkeypatch, root):
    served = []
    monkeypatch.setattr(supervisor, "serve", lambda path: served.append(path))
    assert supervisor.main([str(root / SOCKET)]) == 0
    assert served == [root / SOCKET]


def test_the_entry_point_exits_with_a_code_rather_than_a_traceback(monkeypatch):
    """Its stderr is DEVNULL - it was started detached - so a traceback goes
    nowhere and only the exit status is ever visible."""

    def refuse(path):
        raise supervisor.SupervisorError("the state directory is mode 0755")

    monkeypatch.setattr(supervisor, "serve", refuse)
    assert supervisor.main([]) == supervisor.SupervisorError.exit_code


# ------------------------------------------------------------------ the browser

HEADLESS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/150.0.0.0 Safari/537.36"
)


class FakeChromium:
    """The far end of the CDP pipe, answering the way Chromium does.

    A real `cdp.CDPSession` sits on top of this, so the correlation, the target
    handshake and the framing are all exercised rather than mocked past. The
    load event is queued behind the navigate reply because that is the order the
    browser sends them in - the reply lands when the navigation commits, the
    event when the document is done.
    """

    def __init__(self, *, user_agent=HEADLESS_UA, page_url=PAGE, results=()):
        self.user_agent = user_agent
        self.page_url = page_url
        self.results = list(results)
        self.sent: list[dict] = []
        self.scripts: list[str] = []
        self.pending: list[str] = []
        self.closed = False

    # ---------------------------------------------------------- the ws interface

    def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        self.pending.append(json.dumps({"id": message["id"], "result": self._result(message)}))
        if message.get("method") == "Page.navigate":
            self.pending.append(json.dumps({"method": "Page.loadEventFired", "params": {}}))

    def recv(self, timeout=None) -> str:
        if not self.pending:
            raise pipe.PipeTimeout("the browser said nothing else")
        return self.pending.pop(0)

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ replies

    def _result(self, message: dict) -> dict:
        method = message.get("method")
        if method == "Target.createTarget":
            return {"targetId": "TARGET-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "SESSION-1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": self._value(message["params"]["expression"])}}
        return {}

    def _value(self, expression: str):
        if expression == "navigator.userAgent":
            return self.user_agent
        if expression == "location.href":
            return self.page_url
        self.scripts.append(expression)
        if not self.results:
            raise AssertionError("the page was asked to run a script the test never scripted")
        return self.results.pop(0)

    # ------------------------------------------------------------------ reading

    def calls(self, method: str) -> list[dict]:
        return [m["params"] for m in self.sent if m.get("method") == method]

    def order(self) -> list[str]:
        return [m["method"] for m in self.sent]


class FakeProcess:
    def __init__(self, *, error=None, exits=True):
        self.terminated = False
        self.waited = False
        self._error = error
        # Whether it honours the polite CDP shutdown. A browser that ignores it
        # must still be signalled, or the daemon leaks one per restart.
        self._exits = exits

    def poll(self):
        return 0 if (self._exits or self.terminated) else None

    def terminate(self):
        self.terminated = True
        if self._error is not None:
            raise self._error

    def wait(self, timeout=None):
        self.waited = True
        return 0


def browser_on(chromium, **kw):
    """A `Browser` over the fake, using a real session with a real session id."""
    browser = supervisor.Browser(
        cdp.CDPSession(chromium, "SESSION-1"), chromium, profile="/state/profile", **kw
    )
    # The browser-level session, which is the only one Browser.close may travel
    # on: it must not carry a page sessionId.
    browser._control = cdp.CDPSession(chromium)
    return browser


def test_the_identity_is_corrected_before_the_first_navigation():
    """Not cosmetic and not ordering pedantry: every in-page call inherits the
    UA and the device metrics, and an override applied after the document has
    loaded is an override the page's own tracking headers already got past."""
    chromium = FakeChromium()
    browser_on(chromium).start()
    order = chromium.order()
    for method in (
        "Emulation.setUserAgentOverride",
        "Emulation.setDeviceMetricsOverride",
        "Emulation.setTimezoneOverride",
    ):
        assert order.index(method) < order.index("Page.navigate"), method


def test_the_headless_tell_is_stripped_out_of_the_user_agent():
    """Measured, not guessed: the capture found `HeadlessChrome/150.0.0.0` on
    100% of in-page calls, so this is on all traffic rather than on a header
    template we could choose not to send."""
    chromium = FakeChromium()
    browser_on(chromium).start()
    sent = chromium.calls("Emulation.setUserAgentOverride")[0]["userAgent"]
    assert "Headless" not in sent
    assert "Chrome/150.0.0.0" in sent


def test_the_user_agent_override_is_built_from_the_browser_s_own():
    """Pinning a UA string in source dates the moment Chromium updates, and a
    version that disagrees with the sec-ch-ua headers the browser sends itself
    is a worse tell than the one being fixed."""
    chromium = FakeChromium(user_agent="Mozilla/5.0 HeadlessChrome/999.1.2.3 Safari/537.36")
    browser_on(chromium).start()
    assert (
        chromium.calls("Emulation.setUserAgentOverride")[0]["userAgent"]
        == "Mozilla/5.0 Chrome/999.1.2.3 Safari/537.36"
    )


def test_a_page_that_reports_no_user_agent_is_a_loud_failure():
    """Falling back to a guessed string would put a UA on the wire that matches
    nothing else the browser sends."""
    chromium = FakeChromium(user_agent=None)
    with pytest.raises(supervisor.SupervisorError, match="user agent"):
        browser_on(chromium).start()


def test_the_accept_language_is_the_bare_locale():
    """Measured trap: `en-US,en;q=0.9` came back as `en-US,en;q=0.9;q=0.9` - a
    doubled q-value, which is itself malformed and a tell of its own."""
    chromium = FakeChromium()
    browser_on(chromium).start()
    assert chromium.calls("Emulation.setUserAgentOverride")[0]["acceptLanguage"] == "en-US"


def test_the_display_size_is_set_through_screen_width_and_height():
    """The other measured trap: `x-li-track`'s displayWidth/displayHeight follow
    `screenWidth`/`screenHeight`, not `width`/`height`. An override that sets
    only the viewport still reports 800x600 times the scale factor - the
    headless default, doubled, which is a stranger number than the default."""
    chromium = FakeChromium()
    identity = supervisor.Identity(width=1512, height=982, scale=2.0)
    browser_on(chromium, identity=identity).start()
    metrics = chromium.calls("Emulation.setDeviceMetricsOverride")[0]
    assert (metrics["screenWidth"], metrics["screenHeight"]) == (1512, 982)
    assert metrics["deviceScaleFactor"] == 2.0
    assert metrics["mobile"] is False


def test_the_timezone_is_overridden_rather_than_inherited():
    """The container has no TZ and reports UTC, against an account whose whole
    history is a desktop in a single timezone."""
    chromium = FakeChromium()
    browser_on(chromium, identity=supervisor.Identity(timezone="Europe/Lisbon")).start()
    assert chromium.calls("Emulation.setTimezoneOverride")[0] == {"timezoneId": "Europe/Lisbon"}


def test_the_page_is_parked_on_linkedin():
    """An in-page `fetch` only carries the session on a same-origin document."""
    chromium = FakeChromium()
    browser_on(chromium).start()
    assert chromium.calls("Page.navigate")[0]["url"] == PAGE
    assert chromium.order().index("Page.enable") < chromium.order().index("Page.navigate")


def test_navigation_waits_for_the_load_event_rather_than_sleeping():
    """`cdp.CDPSession.navigate` sleeps a flat 5 s, which is both a real sleep
    in the suite and a guess in production. The event is the fact."""
    chromium = FakeChromium()
    browser_on(chromium).start()
    assert chromium.pending == []  # the load event was consumed, not left behind


def test_a_page_that_never_finishes_loading_is_an_error_not_a_wedge():
    chromium = FakeChromium()
    original = chromium.send

    def swallow_the_load_event(text):
        original(text)
        chromium.pending = [f for f in chromium.pending if "loadEventFired" not in f]

    chromium.send = swallow_the_load_event
    with pytest.raises(pipe.PipeTimeout):
        browser_on(chromium).start()


# ------------------------------------------------------------------------ fetch


def test_a_fetch_runs_the_script_browser_py_owns():
    """Imported, never copied. Two copies of the injected script drift, and the
    one in the resident process is the one nobody remembers to update."""
    from linkedin_cli.browser import SCRIPT

    chromium = FakeChromium(results=[RESULT])
    browser_on(chromium).fetch("GET", "/voyager/api/me", None)
    assert chromium.scripts == [
        SCRIPT
        % {
            "url": json.dumps("/voyager/api/me"),
            "method": json.dumps("GET"),
            "body": json.dumps(None),
        }
    ]


def test_a_fetch_returns_the_page_s_result_untouched():
    chromium = FakeChromium(results=[RESULT])
    assert browser_on(chromium).fetch("GET", "/voyager/api/me", None) == RESULT


def test_a_dict_body_is_encoded_for_the_page():
    chromium = FakeChromium(results=[RESULT])
    browser_on(chromium).fetch("POST", "/x", {"text": "hi"})
    assert json.dumps(json.dumps({"text": "hi"})) in chromium.scripts[0]


def test_a_body_that_is_already_text_is_passed_through():
    """A caller that has serialised its own payload must not have it
    double-encoded into a JSON string containing JSON."""
    chromium = FakeChromium(results=[RESULT])
    browser_on(chromium).fetch("POST", "/x", '{"text":"hi"}')
    assert json.dumps('{"text":"hi"}') in chromium.scripts[0]


def test_the_browser_is_started_once_however_many_fetches_arrive():
    chromium = FakeChromium(results=[RESULT, RESULT])
    browser = browser_on(chromium)
    browser.fetch("GET", "/a", None)
    browser.fetch("GET", "/b", None)
    assert chromium.order().count("Page.navigate") == 1


def test_the_page_url_is_read_from_the_live_document():
    chromium = FakeChromium(page_url="https://www.linkedin.com/checkpoint/challenge")
    assert browser_on(chromium).page_url() == "https://www.linkedin.com/checkpoint/challenge"


def test_a_navigation_that_did_not_take_names_the_page_without_its_query(monkeypatch):
    """The last member of the class the `status` reduction closed.

    `location.href` was interpolated raw into this refusal, and the refusal
    reaches the client as `_failure(str(exc), ...)` - so the one path where the
    page has slipped somewhere unexpected was also the one path that reported
    the full URL of wherever it slipped to. Which origin it landed on is the
    whole diagnosis and survives; the query string does not.
    """
    monkeypatch.setattr(supervisor, "LOAD_TIMEOUT", -1)
    chromium = FakeChromium(page_url=f"https://sso.example/authorize?ct={CSRF_TOKEN}")
    with pytest.raises(supervisor.SupervisorError) as caught:
        browser_on(chromium).start()
    message = str(caught.value)
    assert CSRF_TOKEN not in message
    assert "https://sso.example/authorize" in message


def test_a_navigation_that_reported_no_page_at_all_still_names_about_blank(monkeypatch):
    """The fallback survives the reduction: an empty `location.href` is the
    blank document Chromium swaps in while a navigation commits, and naming it
    is what tells an operator the page never went anywhere."""
    monkeypatch.setattr(supervisor, "LOAD_TIMEOUT", -1)
    with pytest.raises(supervisor.SupervisorError, match="about:blank"):
        browser_on(FakeChromium(page_url="")).start()


# ---------------------------------------------------------------------- closing


def test_closing_asks_the_browser_to_exit_before_signalling_it():
    """Chromium writes its cookie store lazily, so a SIGTERM'd browser can die
    with the session still only in memory. That is not hypothetical: a real
    login in the container left the profile's Cookies table empty and every
    later invocation started signed out. A clean shutdown is what flushes it."""
    chromium = FakeChromium()
    process = FakeProcess()
    browser_on(chromium, process=process).close()
    assert [m["method"] for m in chromium.sent if m.get("method") == "Browser.close"], (
        "the browser was signalled without being asked to exit, so its cookies never flushed"
    )
    assert chromium.closed
    assert process.waited
    assert not process.terminated, "a browser that exited cleanly must not also be killed"


def test_a_browser_that_ignores_the_polite_request_is_still_killed():
    chromium = FakeChromium()
    process = FakeProcess(exits=False)
    browser_on(chromium, process=process).close()
    assert process.terminated, "otherwise the daemon leaks a browser per restart"


def test_closing_never_raises():
    """This runs from the daemon's exit path, where the alternative to a failed
    close is a browser nothing can ever reach again."""
    chromium = FakeChromium()
    browser_on(chromium, process=FakeProcess(error=OSError("already gone"))).close()
    assert chromium.closed


# --------------------------------------------------------------------- launching


def test_launching_attaches_to_a_page_and_starts_it(root):
    chromium = FakeChromium()
    opened = []

    def open_pipe(binary, profile_dir, **kw):
        opened.append((binary, profile_dir, kw))
        return chromium, FakeProcess()

    browser = supervisor.Browser.launch(
        binary="/opt/cloakbrowser/chrome", profile_dir=str(root), open_pipe=open_pipe
    )
    assert browser.profile == str(root)
    assert chromium.order()[:2] == ["Target.createTarget", "Target.attachToTarget"]
    assert chromium.calls("Target.attachToTarget")[0]["flatten"] is True
    assert "Page.navigate" in chromium.order()


def test_launching_asks_for_the_language_the_identity_claims(root):
    """`--window-size` is deliberately not here: the capture passed it and
    measured 800x600 coming back anyway, because it is ignored under
    `--headless=new`."""
    opened = []

    def open_pipe(binary, profile_dir, **kw):
        opened.append(kw)
        return FakeChromium(), FakeProcess()

    supervisor.Browser.launch(profile_dir=str(root), open_pipe=open_pipe)
    extra = opened[0]["extra_args"]
    assert "--lang=en-US" in extra
    assert not any(arg.startswith("--window-size") for arg in extra)


def test_launching_takes_the_binary_and_profile_from_the_environment(monkeypatch, root):
    monkeypatch.setenv(supervisor.BINARY_ENV, "/opt/cloakbrowser/chrome")
    monkeypatch.setenv(supervisor.PROFILE_ENV, str(root / "profile"))
    opened = []

    def open_pipe(binary, profile_dir, **kw):
        opened.append((binary, profile_dir))
        return FakeChromium(), FakeProcess()

    supervisor.Browser.launch(open_pipe=open_pipe)
    assert opened == [("/opt/cloakbrowser/chrome", str(root / "profile"))]


def test_the_default_launch_really_starts_a_browser(root):
    """As with the spawn: the guard's refusal is the assertion that the real
    `pipe.launch` - and through it a real `Popen` - would have been called."""
    with pytest.raises(AssertionError, match=r"Popen is blocked in tests"):
        supervisor.Browser.launch(binary="/opt/cloakbrowser/chrome", profile_dir=str(root))


# ---------------------------------------------------------------- loose corners


@pytest.mark.real_process
def test_a_refused_start_leaves_the_live_supervisor_s_socket_alone(root):
    """`serve` unlinks its socket on the way out, so the refusal has to happen
    *before* the try block that arms that - otherwise a second supervisor
    politely declines to start and then deletes the first one's socket on its
    way past, and every later invocation talks to nothing.

    Real, because a fake probe cannot show which side of the try block the
    refusal landed on: only a genuinely live socket makes `serve` take it.
    """
    path = root / SOCKET
    with contextlib.closing(supervisor.bind_socket(path)):
        before = os.stat(path)
        with pytest.raises(supervisor.SupervisorError, match="already"):
            # No listener seam - the point is that `bind_socket` is reached for
            # real - so the idle budget is zeroed: if the refusal ever regresses
            # this has to fail, not sit in a real `accept` for fifteen minutes.
            supervisor.serve(path, browser=FakeBrowser(), idle_timeout=0.0)
        assert os.stat(path).st_ino == before.st_ino


def test_a_full_backlog_counts_as_someone_listening(root):
    """Connecting to an AF_UNIX socket only blocks when the backlog is full,
    and a full backlog takes a listener - so a timeout is evidence *for* one.
    Reading it as "stale" would unlink the socket of a supervisor that is
    merely busy."""

    class Wedged:
        def settimeout(self, value):
            pass

        def connect(self, address):
            raise TimeoutError

        def close(self):
            pass

    assert supervisor.answers(root / SOCKET, sock=Wedged) is True


def test_the_launch_lock_is_a_0600_file_in_the_guarded_directory(root):
    """Acquired twice in sequence, never nested: flock is granted per open file
    description, so a nested acquisition from this one process would deadlock
    against itself instead of being re-entrant. Sequential acquisition is what
    proves the first one was released."""
    lock = root / "state" / (SOCKET + supervisor.LOCK_SUFFIX)
    for _ in range(2):
        with supervisor._flock(lock):
            assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
    assert dir_mode(lock.parent) == 0o700


def test_the_socket_is_0600_at_creation_not_narrowed_afterwards(root, monkeypatch):
    """The mode assertion alone cannot see this: a bind followed by a chmod
    ends up at 0600 too, having existed group- and world-accessible in between,
    and that window is all another uid on the box needs. The umask is what
    closes it - the chmod after it only covers platforms that ignore the umask
    on a socket inode."""
    seen = []
    real = os.umask
    original = real(0o022)  # the only way to read a umask is to set one...
    real(original)  # ...so put it straight back

    def record(mask):
        seen.append(mask)
        return real(mask)

    monkeypatch.setattr(os, "umask", record)
    with contextlib.closing(supervisor.bind_socket(root / "state" / SOCKET)):
        pass
    # Restored too: the umask is process-global, and one left at 0177 would
    # quietly follow every unrelated file this process creates afterwards.
    assert seen == [0o177, original]


def test_a_payload_that_cannot_be_encoded_is_reported_not_raised(root, daemon):
    """`request` promising never to raise is what lets `browser.py` check one
    thing - `error` - on every result. An exception escaping from here instead
    would reach the CLI as an unclassified crash with no exit code."""
    fake = daemon(up=True)
    answer = ask(fake, root / SOCKET, {"op": "fetch", "path": "/x", "body": {1, 2}})
    assert answer["kind"] == "upstream"
    assert fake.events == []  # not even connected: nothing could have been sent


# ------------------------------------------------------- which browser to launch


def test_an_explicit_binary_is_honoured_even_when_it_does_not_exist(monkeypatch):
    """Precedence must not depend on what happens to be installed.

    A silently-substituted browser is worse than a launch failure naming the
    path it was given: the operator asked for a specific build, and the whole
    point of the env var is to override discovery.
    """
    monkeypatch.setenv(supervisor.BINARY_ENV, "/nowhere/at/all/chrome")
    assert supervisor.resolve_binary() == "/nowhere/at/all/chrome"


def test_the_deployment_default_wins_over_path_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    placed = tmp_path / "chrome"
    placed.write_text("#!/bin/sh\n")
    monkeypatch.setattr(supervisor, "DEFAULT_BINARY", str(placed))
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/" + name)
    assert supervisor.resolve_binary() == str(placed)


def test_a_fresh_checkout_finds_a_browser_on_path(monkeypatch):
    """Without this a clone launches nothing until the operator reads the source."""
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    monkeypatch.setattr(supervisor, "DEFAULT_BINARY", "/does/not/exist/chrome")
    monkeypatch.setattr(
        supervisor.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None
    )
    assert supervisor.resolve_binary() == "/usr/bin/chromium"


def test_finding_nothing_defers_to_the_launch_error(monkeypatch):
    """One failure message, from the place that knows what it tried to execute."""
    monkeypatch.delenv(supervisor.BINARY_ENV, raising=False)
    monkeypatch.setattr(supervisor, "DEFAULT_BINARY", "/does/not/exist/chrome")
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    assert supervisor.resolve_binary() == "/does/not/exist/chrome"
