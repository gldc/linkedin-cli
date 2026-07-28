"""CDP over the fd pair Chromium inherits under `--remote-debugging-pipe`.

A debug **port** is not uid-gated on Linux. Any local uid can read
`/proc/net/tcp`, connect to it, and ask `Network.getCookies` for `li_at` or
`Runtime.evaluate` for an authenticated write - which would put the prompt-
injectable agent this design excludes on the wrong side of the boundary. Pipe
mode leaves zero TCP listeners and no `DevToolsActivePort`, so the only route to
the browser is a descriptor the supervisor holds and never hands out.

The wire format is *not* WebSocket. Messages are NUL-terminated JSON in both
directions, so a single read can return several messages or half of one; the
framing here buffers and splits, and everything above it - `cdp.CDPSession`
especially - stays identical to the WebSocket path in `ws.py`. That symmetry is
the point: `CDPSession` is constructed on either transport without knowing which.
"""

from __future__ import annotations

import fcntl
import os
import select
import subprocess
import time
from typing import Any

CHUNK = 65536

# Chromium hardcodes these: it reads its input on 3 and writes its output on 4.
CHILD_READ_FD = 3
CHILD_WRITE_FD = 4

# Our own ends are moved above the pair before anything is parked on 3 and 4,
# so that parking cannot land on top of a descriptor we still need.
HIGH_FD = 10

# Chromium terminates every message with a NUL byte and never emits one inside
# a message: JSON escapes it as a six-character sequence long before it
# reaches the pipe, so splitting on the raw byte cannot cut a message in half.
FRAME_END = b"\0"


class PipeError(Exception):
    exit_code = 6


class PipeClosed(PipeError):
    """The browser is gone. Distinct from a timeout because the remedy differs:
    a timeout is worth waiting out, a dead browser has to be relaunched."""


class PipeTimeout(PipeError, TimeoutError):
    """Also a `TimeoutError`, which is the only thing `cdp.CDPSession.call`
    catches, so a slow browser arrives at the caller as a `CDPError` naming the
    method rather than as a transport type it has never heard of.

    Note this is *better* than the WebSocket path, not the same: `ws.py` turns
    its own timeout into a `WebSocketError`, which is not a `TimeoutError`, so
    `call` does not catch it and a bare `WebSocketError` - carrying no
    `exit_code` - escapes. Do not "simplify" this base away by pointing at
    `ws.py` as the precedent.
    """


class PipeConnection:
    """The `ws.WebSocket` interface - `send`/`recv`/`close`/`closed` - over fds."""

    def __init__(self, read_fd: int, write_fd: int, *, timeout: float = 30.0, clock=None):
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._timeout = timeout
        # A per-frame deadline and a per-read one behave identically against a
        # browser that falls silent, so only a browser that keeps dribbling
        # tells them apart - and dribbling on the real clock means a sleep in
        # the suite, which is the mistake that once took it from 7 s to 50 s.
        self._clock = clock or time.monotonic
        self._buf = bytearray()
        self._closed = False

    # -------------------------------------------------------------------- reads

    def _fill(self, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise PipeTimeout("timed out waiting for the browser")
        # select rather than a blocking read: the deadline spans the whole
        # message, so a browser trickling bytes cannot extend it read by read.
        ready, _, _ = select.select([self._read_fd], [], [], remaining)
        if not ready:
            raise PipeTimeout("timed out waiting for the browser")
        try:
            chunk = os.read(self._read_fd, CHUNK)
        except OSError as exc:
            self._closed = True
            raise PipeClosed(f"CDP pipe read failed: {exc}") from exc
        if not chunk:
            self._closed = True
            raise PipeClosed("the browser closed the CDP pipe")
        self._buf += chunk

    def _take(self) -> str | None:
        """Pop the next complete message out of the buffer, if there is one."""
        while True:
            end = self._buf.find(FRAME_END)
            if end < 0:
                return None
            raw = bytes(self._buf[:end])
            del self._buf[: end + 1]
            if raw:
                return raw.decode("utf-8", "replace")
            # A bare terminator is not a message; returning "" would only hand
            # `json.loads` an empty document one frame later.

    # -------------------------------------------------------------------- public

    def send(self, text: str) -> None:
        if self._closed:
            raise PipeClosed("cannot send on a closed CDP pipe")
        # A blocking write moves the whole buffer before returning, except when
        # a signal lands mid-transfer and it returns having written only part.
        # The supervisor takes SIGCHLD from the browser it owns, so that case is
        # reachable here, and a short write desyncs the framing for good rather
        # than failing loudly.
        view = memoryview(text.encode() + FRAME_END)
        while view:
            try:
                written = os.write(self._write_fd, view)
            except OSError as exc:
                self._closed = True
                raise PipeClosed(f"CDP pipe write failed: {exc}") from exc
            view = view[written:]

    def recv(self, timeout: float | None = None) -> str:
        """Return the next message, buffering whatever else the read brought."""
        if self._closed:
            raise PipeClosed("the CDP pipe is closed")

        deadline = self._clock() + (self._timeout if timeout is None else timeout)
        while True:
            message = self._take()
            if message is not None:
                return message
            self._fill(deadline)

    def close(self) -> None:
        self._closed = True
        # Cleared before closing: descriptor numbers are recycled immediately,
        # so a second close would shut down whatever the supervisor opened next.
        read_fd, write_fd = self._read_fd, self._write_fd
        self._read_fd = self._write_fd = -1
        for fd in (read_fd, write_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    @property
    def closed(self) -> bool:
        return self._closed


# ------------------------------------------------------------------- launching


def _drop(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _lift(fd: int) -> int:
    """Move one of our own ends clear of the 3/4 window.

    `os.pipe()` hands back 3 and 4 themselves whenever they are free, which is
    the usual case - so the write end of the first pipe is routinely fd 4, the
    very descriptor the child's write end is about to be parked on. A naive
    dup2-then-close then closes the end just parked and the second dup2 clobbers
    the first pair.

    Both of our ends are lifted, though only the first pipe's is in danger while
    both pipes are opened before either lift. Which one is exposed depends on
    that ordering, so lifting both keeps the invariant true if it ever changes.
    """
    high = fcntl.fcntl(fd, fcntl.F_DUPFD, HIGH_FD)
    os.close(fd)
    return high


def _park(fds) -> list[tuple[int, int, bool]]:
    """Save whatever already occupies the target fds so it can be put back.

    The supervisor holds its unix socket and its lock file open for its entire
    life, and either can be sitting on fd 3. Overwriting one loses a listener or
    silently releases a flock, and neither failure points back here.
    """
    parked = []
    for fd in fds:
        try:
            inheritable = os.get_inheritable(fd)
            backup = fcntl.fcntl(fd, fcntl.F_DUPFD, HIGH_FD)
        except OSError:
            continue  # nothing was open there, which is the common case
        parked.append((fd, backup, inheritable))
    return parked


def _restore(parked) -> None:
    for fd, backup, inheritable in parked:
        try:
            os.dup2(backup, fd, inheritable=inheritable)
        finally:
            _drop(backup)


def launch(
    binary: str,
    profile_dir: str,
    *,
    headless: bool = True,
    extra_args: list[str] | None = None,
    spawn=None,
    timeout: float = 30.0,
) -> tuple[PipeConnection, Any]:
    """Start a browser in pipe mode. Returns `(connection, process)`.

    The fd choreography is exact and every step of it cost a debugging round:

    * The dup2 into 3 and 4 happens **here**, not in a `preexec_fn`. `subprocess`
      closes non-passed descriptors *after* `preexec_fn` runs, so anything set up
      there is gone before exec and the browser starts on two dead fds.
    * `os.pipe()` returns `(read, write)`, so the child's fd 4 - the one it
      writes to - is the write end of the *second* pipe. Inverting that pair
      fails with EBADF on the first read rather than at setup.
    * A `dup2` onto the same descriptor is a no-op that leaves FD_CLOEXEC set,
      and `os.pipe()` fds are close-on-exec, so 3 or 4 can otherwise reach exec
      already doomed. Hence the explicit `set_inheritable`.

    Not reentrant, and the supervisor has to keep it that way: fds 3 and 4 are
    process-global, so for the length of this call whatever the supervisor had
    on either number is closed and standing in for a pipe. A second thread
    touching its own socket or lock fd in that window gets the browser's pipe
    instead, and nothing about the resulting failure points back here.
    """
    argv = [
        binary,
        "--remote-debugging-pipe",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # /dev/shm is 64 MB by default in a container and Chromium dies on it.
        "--disable-dev-shm-usage",
    ]
    if headless:
        argv.append("--headless=new")
    argv.extend(extra_args or [])

    runner = spawn or subprocess.Popen

    # Strictly before the pipes are opened. `os.pipe()` takes fd 3 the moment it
    # is free, and parking afterwards would "save" the child's own read end and
    # then faithfully restore it - leaving us holding a reader on the browser's
    # pipe forever, so a dead browser never produces EPIPE and `send` succeeds
    # into nothing instead of raising.
    parked = _park((CHILD_READ_FD, CHILD_WRITE_FD))
    child_reads = child_writes = we_read = we_write = -1

    try:
        child_reads, we_write = os.pipe()
        we_read, child_writes = os.pipe()
        we_write = _lift(we_write)
        we_read = _lift(we_read)

        os.dup2(child_reads, CHILD_READ_FD, inheritable=True)
        if child_reads != CHILD_READ_FD:
            _drop(child_reads)
        child_reads = -1

        os.dup2(child_writes, CHILD_WRITE_FD, inheritable=True)
        if child_writes != CHILD_WRITE_FD:
            _drop(child_writes)
        child_writes = -1

        # `os.pipe()` fds are close-on-exec, and a dup2 onto the same descriptor
        # is a no-op that keeps the flag - so when the pipe already came back as
        # fd 3, fd 3 is still doomed at this point. `subprocess` happens to clear
        # the flag for everything in `pass_fds`, but `spawn` is a seam and the
        # next thing plugged into it need not; hand the pair over exec-ready
        # rather than assume.
        os.set_inheritable(CHILD_READ_FD, True)
        os.set_inheritable(CHILD_WRITE_FD, True)

        process = runner(
            argv,
            pass_fds=(CHILD_READ_FD, CHILD_WRITE_FD),
            close_fds=True,
            # The browser outlives the invocation that started it, so a Ctrl-C
            # or a dropped ssh session must not take it down with the CLI -
            # other invocations are sharing this one.
            start_new_session=True,
            # Chromium is noisy and stdout carries the CLI's JSON.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except BaseException:
        for fd in (child_reads, child_writes, we_read, we_write):
            if fd >= 0:
                _drop(fd)
        raise
    finally:
        # Ours only until exec; the child has its own copies from here on.
        _drop(CHILD_READ_FD)
        _drop(CHILD_WRITE_FD)
        _restore(parked)

    return PipeConnection(we_read, we_write, timeout=timeout), process
