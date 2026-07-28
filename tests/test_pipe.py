"""CDP over an inherited fd pair: NUL framing, correlation, and the launch dance.

Two real `os.pipe()` pairs play the browser. That is enough to prove the parts
that actually break - short reads, partial writes, EOF, and the fd choreography
itself - while never spawning a browser, never opening a socket, and never
sleeping on a real clock. The fake `spawn` duplicates fds 3 and 4 exactly as
`exec` would leave a child holding them, so the wiring is verified end to end
against something that never runs Chrome.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import threading

import pytest

from linkedin_cli import cdp, pipe


class FakeBrowser:
    """The far end of the two pipes Chrome would inherit as fd 3 and fd 4."""

    def __init__(self):
        # os.pipe() is (read, write). The browser WRITES what we read; getting
        # this pair backwards is trap 3 and shows up as EBADF, not as a hang.
        self.we_read, self._writes = os.pipe()
        self._reads, self.we_write = os.pipe()

    def send(self, *messages) -> None:
        """Write complete NUL-terminated frames, as the browser does."""
        self.send_raw(b"".join(json.dumps(m).encode() + b"\0" for m in messages))

    def send_raw(self, blob: bytes) -> None:
        os.write(self._writes, blob)

    def read_raw(self, timeout: float = 2.0) -> bytes:
        ready, _, _ = select.select([self._reads], [], [], timeout)
        if not ready:
            raise AssertionError("the connection wrote nothing within the timeout")
        return os.read(self._reads, 1 << 20)

    def read_frame(self, timeout: float = 2.0) -> str:
        buf = b""
        while b"\0" not in buf:
            buf += self.read_raw(timeout)
        return buf.split(b"\0")[0].decode()

    def hang_up(self) -> None:
        """EOF on our read end, which is what an exiting browser produces."""
        self._writes = _shut(self._writes)

    def stop_reading(self) -> None:
        """Drop the browser's read end so our next write gets EPIPE."""
        self._reads = _shut(self._reads)

    def close(self) -> None:
        self._writes = _shut(self._writes)
        self._reads = _shut(self._reads)


def _shut(fd: int) -> int:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    return -1


@pytest.fixture
def wired():
    """Hand out (connection, browser) pairs and close them in the right order."""
    made = []

    def make(timeout: float = 2.0, clock=None):
        browser = FakeBrowser()
        # `clock` is a factory rather than a callable because the only clock
        # worth faking here has to write to the browser it is timing.
        extra = {} if clock is None else {"clock": clock(browser)}
        conn = pipe.PipeConnection(browser.we_read, browser.we_write, timeout=timeout, **extra)
        made.append((conn, browser))
        return conn, browser

    yield make
    for conn, browser in made:
        conn.close()  # owns we_read / we_write
        browser.close()


# ------------------------------------------------------------------- framing


def test_one_frame_is_returned_without_its_terminator(wired):
    conn, browser = wired()
    browser.send({"id": 1, "result": {"product": "Chrome/150.0.0.0"}})
    assert json.loads(conn.recv(timeout=1.0)) == {
        "id": 1,
        "result": {"product": "Chrome/150.0.0.0"},
    }


def test_two_frames_in_one_read_are_returned_separately(wired):
    """A single read routinely returns several frames; splitting is on us."""
    conn, browser = wired()
    browser.send({"id": 1, "result": {}}, {"method": "Page.loadEventFired", "params": {}})
    assert json.loads(conn.recv(timeout=1.0))["id"] == 1
    assert json.loads(conn.recv(timeout=1.0))["method"] == "Page.loadEventFired"


def test_a_frame_split_across_two_reads_is_reassembled(wired):
    conn, browser = wired()
    blob = json.dumps({"id": 7, "result": {"value": "split"}}).encode() + b"\0"
    browser.send_raw(blob[:9])
    browser.send_raw(blob[9:])
    assert json.loads(conn.recv(timeout=1.0)) == {"id": 7, "result": {"value": "split"}}


def test_a_frame_split_inside_a_multibyte_character_is_reassembled(wired):
    """Decoding per read rather than per frame mangles any non-ASCII body.

    Chromium writes raw UTF-8, not the \\u-escaped form `json.dumps` defaults to,
    so multibyte sequences really do straddle reads.
    """
    conn, browser = wired()
    body = {"id": 1, "result": {"name": "café 日本語"}}
    blob = json.dumps(body, ensure_ascii=False).encode() + b"\0"
    cut = blob.index("é".encode()) + 1  # mid-character
    browser.send_raw(blob[:cut])
    browser.send_raw(blob[cut:])
    assert json.loads(conn.recv(timeout=1.0))["result"]["name"] == "café 日本語"


def test_trailing_bytes_after_a_frame_survive_until_the_next_recv(wired):
    conn, browser = wired()
    first = json.dumps({"id": 1}).encode() + b"\0"
    second = json.dumps({"id": 2}).encode() + b"\0"
    browser.send_raw(first + second[:4])
    assert json.loads(conn.recv(timeout=1.0))["id"] == 1
    browser.send_raw(second[4:])
    assert json.loads(conn.recv(timeout=1.0))["id"] == 2


def test_an_empty_frame_is_skipped_rather_than_returned(wired):
    """`json.loads("")` is the only thing an empty message could produce."""
    conn, browser = wired()
    browser.send_raw(b"\0" + json.dumps({"id": 1}).encode() + b"\0")
    assert json.loads(conn.recv(timeout=1.0))["id"] == 1


def test_send_terminates_the_message_with_a_nul_and_nothing_else(wired):
    """Pipe mode is NUL-framed, not WebSocket-framed; a header would desync it."""
    conn, browser = wired()
    conn.send('{"id":1,"method":"Browser.getVersion"}')
    assert browser.read_raw() == b'{"id":1,"method":"Browser.getVersion"}\0'


def test_send_moves_a_payload_larger_than_the_pipe_buffer(wired):
    """A `Runtime.evaluate` carrying a page script dwarfs the ~64 KB pipe
    buffer, so the write has to survive several fill-and-drain rounds."""
    conn, browser = wired()
    text = json.dumps({"id": 1, "params": {"expression": "x" * 200_000}})
    got = []
    reader = threading.Thread(target=lambda: got.append(browser.read_frame(timeout=5.0)))
    reader.start()
    conn.send(text)
    reader.join(timeout=5.0)
    assert got == [text]


# ------------------------------------------------------ correlation via CDPSession


def test_cdp_session_skips_an_event_before_the_matching_reply(wired):
    """CDPSession must sit on this transport unchanged; events carry no id."""
    conn, browser = wired()
    browser.send(
        {"method": "Network.requestWillBeSent", "params": {}},
        {"method": "Page.frameNavigated", "params": {}},
        {"id": 1, "result": {"targetInfos": []}},
    )
    assert cdp.CDPSession(conn).call("Target.getTargets") == {"targetInfos": []}


def test_cdp_session_skips_a_reply_to_a_different_id(wired):
    conn, browser = wired()
    browser.send({"id": 999, "result": {"stale": True}}, {"id": 1, "result": {"fresh": True}})
    assert cdp.CDPSession(conn).call("Foo.bar") == {"fresh": True}


def test_cdp_session_sends_a_framed_request(wired):
    conn, browser = wired()
    browser.send({"id": 1, "result": {}})
    cdp.CDPSession(conn).call("Browser.getVersion")
    assert json.loads(browser.read_frame()) == {
        "id": 1,
        "method": "Browser.getVersion",
        "params": {},
    }


def test_cdp_session_reports_a_pipe_timeout_as_a_cdp_error(wired):
    """`CDPSession.call` catches TimeoutError, so the timeout must be one."""
    conn, _ = wired()
    with pytest.raises(cdp.CDPError, match="timed out"):
        cdp.CDPSession(conn).call("Browser.getVersion", timeout=0.05)


# ------------------------------------------------------------------- timeout


def test_recv_honours_its_timeout(wired):
    conn, _ = wired()
    with pytest.raises(TimeoutError):
        conn.recv(timeout=0.05)


def test_the_timeout_covers_the_whole_frame_not_each_read(wired):
    """A browser trickling bytes must not extend the deadline read by read."""
    conn, browser = wired()
    browser.send_raw(b'{"id":1,"resu')
    with pytest.raises(TimeoutError):
        conn.recv(timeout=0.1)


class TricklingClock:
    """A clock that releases one more byte to the connection every time it ticks.

    A browser that simply goes quiet times out under either spelling of the
    deadline - per frame or per read - so the tests around it prove nothing
    about which one is implemented. The difference only surfaces against a
    browser that keeps dribbling, and dribbling on the real clock means putting
    a sleep in the suite, which is the mistake that once took it from 7 s to 50 s.

    So the trickle is driven by the code under test instead: every read of the
    clock costs `step` seconds *and* makes one more byte available, so `select`
    always finds data and is never once made to wait. A per-read deadline can
    then never expire and the frame completes; a per-frame deadline expires with
    the browser still writing.
    """

    def __init__(self, browser, blob: bytes, *, step: float):
        self._browser = browser
        self._rest = blob
        self._step = step
        self._now = 1000.0

    @property
    def left(self) -> int:
        return len(self._rest)

    def monotonic(self) -> float:
        self._now += self._step
        if self._rest:
            self._browser.send_raw(self._rest[:1])
            self._rest = self._rest[1:]
        return self._now


def test_a_trickling_browser_cannot_extend_the_deadline(wired):
    """One deadline covers the whole frame, not each read.

    The supervisor is resident and single-threaded: a browser that dribbles a
    reply out - or wedges half way through one - would otherwise hold `recv`
    open indefinitely, which is a permanent wedge rather than a slow call.
    """
    blob = json.dumps({"id": 1, "result": {"trickled": True}}).encode() + b"\0"
    built = []

    def trickle(browser):
        built.append(TricklingClock(browser, blob, step=0.05))
        return built[-1].monotonic

    conn, _ = wired(clock=trickle)
    with pytest.raises(pipe.PipeTimeout):
        conn.recv(timeout=0.5)
    # Without this the test would also pass for a browser that fell silent,
    # which is the case the neighbouring timeout tests already cover.
    assert built[0].left, "the browser had stopped writing; nothing was trickling"


def test_recv_falls_back_to_the_connection_timeout(wired):
    conn, _ = wired(timeout=0.05)
    with pytest.raises(TimeoutError):
        conn.recv()


def test_a_timeout_leaves_the_connection_usable(wired):
    """The reply landing late is the normal case, not a fatal one."""
    conn, browser = wired()
    with pytest.raises(TimeoutError):
        conn.recv(timeout=0.05)
    browser.send({"id": 1, "result": {}})
    assert json.loads(conn.recv(timeout=1.0))["id"] == 1


# -------------------------------------------------------------- closed pipe


def test_a_browser_that_exits_before_replying_raises_pipe_closed(wired):
    conn, browser = wired()
    browser.hang_up()
    with pytest.raises(pipe.PipeClosed):
        conn.recv(timeout=1.0)


def test_a_browser_that_dies_mid_frame_raises_pipe_closed(wired):
    """Half a frame then EOF: a crash, not a timeout, and typed as one."""
    conn, browser = wired()
    browser.send_raw(b'{"id":1,"result')
    browser.hang_up()
    with pytest.raises(pipe.PipeClosed):
        conn.recv(timeout=1.0)


def test_a_closed_pipe_is_not_reported_as_a_timeout(wired):
    """Exit-code-wise these are different failures: retry vs. relaunch."""
    conn, browser = wired()
    browser.hang_up()
    with pytest.raises(pipe.PipeClosed) as exc:
        conn.recv(timeout=1.0)
    assert not isinstance(exc.value, TimeoutError)


def test_eof_marks_the_connection_closed(wired):
    conn, browser = wired()
    browser.hang_up()
    with pytest.raises(pipe.PipeClosed):
        conn.recv(timeout=1.0)
    assert conn.closed


def test_send_to_a_browser_that_is_gone_raises_pipe_closed(wired):
    conn, browser = wired()
    browser.stop_reading()
    with pytest.raises(pipe.PipeClosed):
        conn.send('{"id":1}')


def test_recv_after_close_raises_pipe_closed(wired):
    conn, _ = wired()
    conn.close()
    with pytest.raises(pipe.PipeClosed):
        conn.recv(timeout=1.0)


def test_send_after_close_raises_pipe_closed(wired):
    conn, _ = wired()
    conn.close()
    with pytest.raises(pipe.PipeClosed):
        conn.send('{"id":1}')


def test_close_is_idempotent(wired):
    conn, _ = wired()
    conn.close()
    conn.close()
    assert conn.closed


def test_a_second_close_does_not_close_a_recycled_descriptor(wired, tmp_path):
    """Descriptor numbers are reused immediately; a blind re-close shuts down
    whatever the supervisor opened next."""
    conn, _ = wired()
    conn.close()
    victim = open(tmp_path / "recycled", "w")  # takes one of the freed numbers
    try:
        conn.close()
        os.fstat(victim.fileno())  # raises EBADF if the second close took it
    finally:
        victim.close()


def test_pipe_errors_share_one_family(wired):
    """So the supervisor can catch the transport failing without listing types."""
    assert issubclass(pipe.PipeClosed, pipe.PipeError)
    assert issubclass(pipe.PipeTimeout, (pipe.PipeError, TimeoutError))


# -------------------------------------------------------------------- launch


class FakeProcess:
    pid = 4321

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class RecordingSpawn:
    """Stands in for `subprocess.Popen`, capturing argv, kwargs and the fds.

    Duplicating 3 and 4 is precisely what `exec` leaves a child holding, so the
    test can play browser on the far end of the real inherited pipes.
    """

    def __init__(self, error: Exception | None = None):
        self.argv: list[str] = []
        self.kwargs: dict = {}
        self.inheritable: tuple[bool, bool] = ()
        self.child_reads = -1
        self.child_writes = -1
        self._error = error

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.inheritable = (os.get_inheritable(3), os.get_inheritable(4))
        self.child_reads = os.dup(3)
        self.child_writes = os.dup(4)
        if self._error is not None:
            raise self._error
        return FakeProcess()

    def close(self):
        self.child_reads = _shut(self.child_reads)
        self.child_writes = _shut(self.child_writes)


@pytest.fixture
def spawn():
    made = []

    def make(error=None):
        recorder = RecordingSpawn(error)
        made.append(recorder)
        return recorder

    yield make
    for recorder in made:
        recorder.close()


@contextlib.contextmanager
def fds_3_and_4_free():
    """Force the condition that makes traps 2 and 4 fire at all.

    `os.pipe()` returns the lowest free descriptors, so on a fresh process it
    hands back 3 and 4 themselves - and only then can a dup2 land on the very
    end it is copying, or degrade into a no-op that keeps FD_CLOEXEC. Under
    pytest those two numbers are already taken, which would silently make both
    traps untestable and both fixes look like dead code.
    """
    saved = []
    for fd in (3, 4):
        try:
            inheritable = os.get_inheritable(fd)
            saved.append((fd, fcntl.fcntl(fd, fcntl.F_DUPFD, 60), inheritable))
        except OSError:
            continue  # already free
        os.close(fd)
    try:
        yield
    finally:
        for fd, backup, inheritable in saved:
            os.dup2(backup, fd, inheritable=inheritable)
            os.close(backup)


def open_fds() -> set[int]:
    """Every descriptor this process currently holds."""
    listing = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    live = set()
    for name in os.listdir(listing):
        fd = int(name)
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)  # drops the listdir handle itself
        except OSError:
            continue
        live.add(fd)
    return live


def launch(spawner, tmp_path, **kw):
    return pipe.launch("/opt/cloakbrowser/chrome", str(tmp_path / "profile"), spawn=spawner, **kw)


def test_launch_asks_for_pipe_mode(spawn, tmp_path):
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert "--remote-debugging-pipe" in recorder.argv


def test_launch_never_opens_a_debug_port(spawn, tmp_path):
    """A port is reachable by every local uid; the whole pivot rests on this."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert not any("remote-debugging-port" in arg for arg in recorder.argv)


def test_launch_detaches_the_browser_from_our_session(spawn, tmp_path):
    """Ctrl-C or a dropped ssh session must not kill a browser other
    invocations are sharing."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert recorder.kwargs["start_new_session"] is True


def test_launch_passes_exactly_the_two_debug_descriptors(spawn, tmp_path):
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert tuple(recorder.kwargs["pass_fds"]) == (3, 4)
    assert recorder.kwargs["close_fds"] is True


def test_launch_keeps_browser_output_off_our_streams(spawn, tmp_path):
    """stdout carries the CLI's JSON; Chromium's chatter would corrupt it."""
    import subprocess

    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert recorder.kwargs["stdout"] == subprocess.DEVNULL
    assert recorder.kwargs["stderr"] == subprocess.DEVNULL


def test_launch_carries_the_profile_and_extra_flags(spawn, tmp_path):
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path, extra_args=["--lang=en-US"])
    conn.close()
    assert recorder.argv[0] == "/opt/cloakbrowser/chrome"
    assert f"--user-data-dir={tmp_path / 'profile'}" in recorder.argv
    assert "--headless=new" in recorder.argv
    assert recorder.argv[-1] == "--lang=en-US"


def test_launch_survives_a_container_sized_dev_shm(spawn, tmp_path):
    """/dev/shm is 64 MB by default in a container and Chromium dies on it -
    and dies as a startup crash with nothing to show for it, because stderr is
    DEVNULL, so it surfaces here as a browser that never answers."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert "--disable-dev-shm-usage" in recorder.argv


def test_launch_suppresses_the_first_run_interaction(spawn, tmp_path):
    """The managed profile is created empty on the first launch, and a headless
    browser sitting on the first-run flow is indistinguishable from one that
    hung."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert "--no-first-run" in recorder.argv
    assert "--no-default-browser-check" in recorder.argv


def test_launch_does_not_disable_the_browser_sandbox(spawn, tmp_path):
    """The port-mode launcher passed `--no-sandbox`, justified as "cannot create
    a sandbox as root". That justification died with the pivot: the tenant runs
    as uid 10001, and this browser is permanent and continuously renders
    attacker-influenceable content - any feed post, any DM body - in the one
    process that holds the credential. Re-adding the flag to fix a container
    startup failure would be a silent downgrade of exactly that boundary, so its
    absence is pinned here; per spec §9 the sandbox is fixed at the container
    level, and an operator who must override does it through `extra_args`.
    """
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert not any(arg.startswith("--no-sandbox") for arg in recorder.argv)


def test_launch_can_leave_headless_off(spawn, tmp_path):
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path, headless=False)
    conn.close()
    # Startswith, not `in`: tmp_path carries the test's own name.
    assert not any(arg.startswith("--headless") for arg in recorder.argv)


def test_launch_hands_its_timeout_to_the_connection(spawn, tmp_path):
    """The supervisor sets one budget for the browser at launch; a connection
    that quietly kept the 30 s default would wedge it for half a minute per
    call instead."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path, timeout=0.05)
    with pytest.raises(pipe.PipeTimeout):
        conn.recv()  # no argument: the launch budget is the only one in play
    conn.close()


def test_launch_returns_the_process_handle(spawn, tmp_path):
    recorder = spawn()
    conn, proc = launch(recorder, tmp_path)
    conn.close()
    assert proc.pid == FakeProcess.pid


def test_the_browser_end_of_fd_4_reaches_the_connection(spawn, tmp_path):
    """fd 4 is what the child WRITES to. Inverting the pair is trap 3."""
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    os.write(recorder.child_writes, b'{"id":1,"result":{"protocolVersion":"1.3"}}\0')
    assert json.loads(conn.recv(timeout=1.0))["result"]["protocolVersion"] == "1.3"
    conn.close()


def test_the_connection_reaches_the_browser_end_of_fd_3(spawn, tmp_path):
    recorder = spawn()
    conn, _ = launch(recorder, tmp_path)
    conn.send('{"id":1,"method":"Browser.getVersion"}')
    ready, _, _ = select.select([recorder.child_reads], [], [], 2.0)
    assert ready, "nothing arrived on the child's read end"
    assert os.read(recorder.child_reads, 4096) == b'{"id":1,"method":"Browser.getVersion"}\0'
    conn.close()


def test_the_inherited_descriptors_survive_exec(spawn, tmp_path):
    """`os.pipe()` fds are close-on-exec and a dup2 onto itself is a no-op that
    keeps the flag, so fd 3 arrives here still doomed whenever the pipe came
    back as fd 3 - which `fds_3_and_4_free` guarantees it did.

    `subprocess` would clear the flag for `pass_fds` anyway; `spawn` is a seam
    and the assertion is on what `launch` hands the spawner, not on Popen.
    """
    recorder = spawn()
    with fds_3_and_4_free():
        conn, _ = launch(recorder, tmp_path)
    conn.close()
    assert recorder.inheritable == (True, True)


def test_the_wiring_holds_when_os_pipe_hands_back_fds_3_and_4(spawn, tmp_path):
    """Trap 2: our own ends have to be lifted clear first, or parking the
    child's ends closes one of them and the second dup2 clobbers the first."""
    recorder = spawn()
    with fds_3_and_4_free():
        conn, _ = launch(recorder, tmp_path)
    os.write(recorder.child_writes, b'{"id":1,"result":{"ok":true}}\0')
    assert json.loads(conn.recv(timeout=1.0))["result"] == {"ok": True}

    conn.send('{"id":2}')
    ready, _, _ = select.select([recorder.child_reads], [], [], 2.0)
    assert ready, "nothing arrived on the child's read end"
    assert os.read(recorder.child_reads, 4096) == b'{"id":2}\0'
    conn.close()


@contextlib.contextmanager
def occupying(fd: int, path):
    """Sit an ordinary open file on `fd`, then hand the number back untouched.

    Not inheritable, which is what an ordinary `open()` gives and what the
    supervisor's own socket and lock file are.
    """
    try:
        backup = fcntl.fcntl(fd, fcntl.F_DUPFD, 50)
    except OSError:
        backup = None
    with open(path) as fh:
        os.dup2(fh.fileno(), fd, inheritable=False)
        try:
            yield
        finally:
            if backup is None:
                _shut(fd)
            else:
                os.dup2(backup, fd)
                os.close(backup)


def test_launch_does_not_destroy_a_descriptor_already_parked_on_fd_3(spawn, tmp_path):
    """The supervisor holds its listening socket and its flock for its whole
    life; clobbering one would release a lock with nothing to show for it."""
    marker = tmp_path / "occupant"
    marker.write_text("x")
    with occupying(3, marker):
        before = os.fstat(3)
        recorder = spawn()
        conn, _ = launch(recorder, tmp_path)
        conn.close()
        after = os.fstat(3)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_launch_gives_back_a_parked_descriptor_still_close_on_exec(spawn, tmp_path):
    """Handing the number back but not its flag is worse than losing it.

    `os.dup2` defaults to *inheritable*, so a restore that forgets the flag
    turns the supervisor's own socket - or its lock file - into something every
    later child inherits. A leaked copy of a flocked descriptor holds the lock
    for the life of that child, so the lock outlives the supervisor and the next
    one blocks on an owner that no longer exists. Nothing about that failure
    points back here.
    """
    marker = tmp_path / "occupant"
    marker.write_text("x")
    with occupying(3, marker):
        recorder = spawn()
        conn, _ = launch(recorder, tmp_path)
        conn.close()
        restored = os.get_inheritable(3)
    assert restored is False


def test_launch_leaves_no_descriptors_behind(spawn, tmp_path):
    """A resident supervisor relaunches; a pair leaked per launch is a slow
    death by EMFILE."""
    recorder = spawn()
    before = open_fds()
    conn, _ = launch(recorder, tmp_path)
    conn.close()
    recorder.close()
    assert open_fds() == before


def test_launch_leaves_nothing_behind_when_3_and_4_started_free(spawn, tmp_path):
    """The supervisor's *first* launch, and the case the other leak test cannot
    see: with nothing parked on 3 and 4 there is no restore to close the child's
    ends as a side effect, so they have to be dropped deliberately."""
    recorder = spawn()
    with fds_3_and_4_free():
        before = open_fds()
        conn, _ = launch(recorder, tmp_path)
        conn.close()
        recorder.close()
        after = open_fds()
    assert after == before


def test_a_failed_spawn_leaks_nothing(spawn, tmp_path):
    recorder = spawn(error=OSError("no such binary"))
    before = open_fds()
    with pytest.raises(OSError, match="no such binary"):
        launch(recorder, tmp_path)
    recorder.close()
    assert open_fds() == before


def test_launch_spawns_for_real_when_no_seam_is_injected(tmp_path):
    """Every other launch test drives a fake, so nothing otherwise proves the
    default is a real `Popen` rather than a stub that never starts a browser.
    The conftest guard is what answers here: it replaces `Popen` with a refusal,
    so its message *is* the assertion that the real one would have been called.

    Doubles as the other half of `test_a_failed_spawn_leaks_nothing` - the
    spawner raising something that is not an `OSError` must clean up too.
    """
    before = open_fds()
    with pytest.raises(AssertionError, match=r"Popen is blocked in tests"):
        pipe.launch("/opt/cloakbrowser/chrome", str(tmp_path / "profile"))
    assert open_fds() == before
