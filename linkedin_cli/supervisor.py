"""The resident browser owner, and the uid-gated socket in front of it.

CDP rides descriptors the browser inherits (`--remote-debugging-pipe`, see
`pipe.py`), so the browser dies with whatever launched it - and every `linkedin`
invocation is a short-lived process. Something has to outlive the CLI and hold
the credential, which is what this module is.

It deliberately is **not** a debug port. That was tested in the real agent gateway
container: an unprivileged uid enumerated the port out of `/proc/net/tcp`,
connected with zero credentials, and could have read `li_at` straight out of
`Network.getCookies` - or issued authenticated writes past the broker allowlist,
the write ledger, the pacer and the circuit breaker. A unix socket is uid-gated
by the permissions on the directory holding it, so the boundary the port
destroyed is restored by a `0600` socket inside a `0700` directory. Those two
modes are the entire security argument, which is why `bind_socket` asserts them
instead of assuming whoever made the directory got it right.

The daemon is a dumb credential-holding proxy. It performs the in-page fetch and
hands back the raw `{status, headers, body, url}`; it classifies nothing.
`transport.py` owns the taxonomy and the CLI applies it, so the resident process
- the one that is awkward to restart and holds the credential - never needs
updating when LinkedIn changes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import shutil
import stat
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import cdp, pipe, state, transport

SOCKET_ENV = "LINKEDIN_SOCKET"
SOCKET_NAME = "linkedin.sock"

# The launch lock is a sibling of the socket rather than the socket itself: the
# socket file is unlinked and recreated on every start, and a lock held on an
# inode that has been replaced guards nothing (`state.py` learned the same thing
# about `os.replace`).
LOCK_SUFFIX = ".lock"

# The daemon is spawned detached, so its own two descriptors are the only place
# it can write down why it would not start. They used to be DEVNULL, which is
# how a browser that never launched produced nothing at all for the operator.
LOG_SUFFIX = ".log"

# A diagnostic, not a journal: truncated past this, so a browser failing every
# ten minutes for a month cannot fill the state directory.
LOG_MAX = 1 << 20

# The one page the daemon parks on, and its only piece of LinkedIn knowledge:
# an in-page `fetch` only carries the session when the document it runs in is
# same-origin, so the origin - not the API - is what has to be right here.
ORIGIN = "https://www.linkedin.com"
PAGE_URL = ORIGIN + "/feed/"

# The container's CloakBrowser build and the profile that is actually signed in.
# `browser.py` re-exports these rather than keeping copies: while there were two
# copies only one of them was ever corrected, and the stale one named
# /opt/linkedin-cli/other-profile - a directory nothing had logged into. An
# invocation without an override opened it empty, and the 401 that came back
# reads exactly like a session LinkedIn had killed.
# A deployment default, not a discovery mechanism: whoever runs this in a
# container knows where they put the browser. LINKEDIN_BROWSER_BINARY beats it,
# and `resolve_binary` additionally searches PATH for something to fall back on.
DEFAULT_BINARY = "/opt/linkedin-cli/browser/chrome"
DEFAULT_PROFILE = "/opt/linkedin-cli/profile"

# Tried in order when neither the env var nor DEFAULT_BINARY names a real file.
# Chromium first: this package passes `--remote-debugging-pipe`, which Chrome
# and Chromium both support and which several Chromium *forks* do not.
BINARY_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)
BINARY_ENV = "LINKEDIN_BROWSER_BINARY"
PROFILE_ENV = "LINKEDIN_BROWSER_PROFILE"

# Which deployment this invocation is running under, and the one value that
# changes anything. Both defaults above **fail open**, and under a credential
# broker that is a code-substitution path rather than an inconvenience: in such a
# container the path `DEFAULT_BINARY` names is owned and writable by the untrusted
# agent uid, while the CLI itself runs as the uid holding every tenant's
# credentials. A missing or misspelled key in the broker policy would
# therefore exec whatever that uid last wrote there, as the credential holder,
# with no error and no audit line to notice afterwards. So under this deployment
# the built-in defaults are refused rather than used, and a policy that forgot a
# key fails loudly on the first call instead of quietly on every one.
#
# The comparison is exact, against the single deployment that exists. Treating
# any unrecognised value as confined would refuse anyone who exported the
# variable for something else, and it would still not catch the case worth
# worrying about: a `LINKEDIN_DEPLOYMENT` misspelled in the policy arms nothing
# here whatever it is compared against. The broker's own load-time assertions are
# what cover that half.
DEPLOYMENT_ENV = "LINKEDIN_DEPLOYMENT"
CONFINED_DEPLOYMENT = "credexec"

# `--window-size` is deliberately absent: passing it still yields 800x600 under
# `--headless=new`, because the flag is ignored there.
# `Emulation.setDeviceMetricsOverride` is what actually moves those numbers, so
# the flag would only be reassuring. See docs/voyager-headers.md.
# Opt-in, never inferred. Chromium refuses to run as root without it, so the
# tempting fix is to always pass it - but under the pivot the browser is
# permanent and continuously renders attacker-authored content (any feed post,
# any DM body) in the same process that holds the only credential. Disabling the
# renderer sandbox there is a standing risk, not a startup detail, so it has to
# be something an operator chose out loud.
NO_SANDBOX_ENV = "LINKEDIN_BROWSER_NO_SANDBOX"

LAUNCH_ARGS = (
    "--lang=en-US",
    # navigator.webdriver is true otherwise, which marks every page as automated
    # - measured on this machine, and the seed's own environment check refuses to
    # install a session into a browser that reports it. CloakBrowser patches this
    # out at the source; stock Chrome needs the flag.
    "--disable-blink-features=AutomationControlled",
)

CHUNK = 65536

# One request per connection and the largest is a write payload, so this is
# generous by three orders of magnitude and still bounds a broken client.
MAX_REQUEST = 1 << 20

# The answer, which is a different problem in the other direction. A request is
# written by this package and can be bounded tightly; a response is whatever
# LinkedIn decided to send, and `feed list --count=30` is routinely over a
# megabyte - thirty posts, each with an author, a social detail and several image
# artifacts. One constant served both directions until a live run reported `the
# supervisor stopped answering: the message is too long` for a page LinkedIn had
# returned perfectly well. The reasoning above was sound about requests and was
# never true of answers.
#
# Still bounded, because the supervisor is not obliged to buffer an unbounded
# stream just because the peer is trusted - but bounded at a size no real page
# reaches rather than one an ordinary read crosses.
MAX_RESPONSE = 64 << 20

# The historical spelling, kept so an external caller of `read_line` still works.
MAX_LINE = MAX_REQUEST

IDLE_TIMEOUT = 900.0

# Reading the request off an accepted connection: the client sends immediately,
# so anything slower than this is a client that died mid-write.
REQUEST_TIMEOUT = 10.0

# Waiting for the answer, which covers a page load and a Voyager round trip, so
# it has to exceed the daemon's own CDP budget rather than merely look generous.
RESPONSE_TIMEOUT = 120.0

CONNECT_TIMEOUT = 5.0

# How long a cold start may take: Chromium's own startup plus a feed load.
STARTUP_TIMEOUT = 30.0
POLL_INTERVAL = 0.1

CDP_TIMEOUT = 30.0
LOAD_TIMEOUT = 30.0
TERMINATE_TIMEOUT = 10.0

BACKLOG = 16


class SupervisorError(Exception):
    """Refusing to serve, or refusing to reach a server.

    Exit code 6 to sit with the rest of the transport failures: from the CLI's
    point of view a supervisor that will not start is the same class of problem
    as a browser that will not answer.
    """

    exit_code = 6


class NoFallback(SupervisorError):
    """A key the confined deployment was supposed to supply is not set.

    A type rather than a message, because the same refusal is raised on both
    sides of the socket and the two sides classified it differently: the client's
    annotation called it `config` while the daemon's dispatcher fell through
    `_kind` to `upstream`, so what an operator was told depended on which op ran
    first. `upstream` sends them to restart a browser over a line missing from
    the tenant's policy. One classifier now reads this type - see `_kind`.
    """


# --------------------------------------------------------------------- paths


def default_socket_path() -> Path:
    """Where the socket lives: the env override, then beside the ledger.

    Read per call rather than at import, so a broker that relocates the tenant's
    state per invocation is honoured - and so the test suite's own relocation of
    `LINKEDIN_STATE_FILE` carries the socket with it.
    """
    override = os.environ.get(SOCKET_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return state.resolve_path().parent / SOCKET_NAME


def _resolve(socket_path: str | Path | None) -> Path:
    return Path(socket_path) if socket_path is not None else default_socket_path()


def confined() -> bool:
    """Whether the built-in defaults are refused rather than fallen back to."""
    return os.environ.get(DEPLOYMENT_ENV, "").strip() == CONFINED_DEPLOYMENT


def no_fallback(what: str, env: str, why: str) -> NoFallback:
    """The refusal every confined resolver raises, in one place so they cannot drift.

    Three of them now, and the third is `state.resolve_path` in the module this
    one imports - so this is spelled without the underscore rather than copied
    across the seam, which is how the two halves of the browser resolver drifted
    the first time.
    """
    return NoFallback(
        f"{env} is not set and this invocation is running under "
        f"{DEPLOYMENT_ENV}={CONFINED_DEPLOYMENT}, where the built-in {what} is refused rather "
        f"than used. {why} Set {env}: the confined deployment supplies it in the tenant's "
        "policy, so an unset one is a key that policy lost rather than a machine that needs "
        "the default."
    )


def requested_profile() -> str:
    """The profile *this* invocation would open a browser on.

    Read per call and never cached: it is the half of the profile question the
    client knows, and the other half - what a resident supervisor already
    opened - can only be asked over the socket.
    """
    override = os.environ.get(PROFILE_ENV, "").strip()
    if override:
        return override
    if confined():
        raise no_fallback(
            "profile",
            PROFILE_ENV,
            f"{DEFAULT_PROFILE} is the profile the confined deployment moved *out* of, so "
            "opening it reaches a directory this uid has no session in - and the 401 that "
            "comes back reads exactly like a dead account, which is how a re-seed that "
            "invalidated a live session got started once already.",
        )
    return DEFAULT_PROFILE


def requested_binary() -> str:
    """The browser *this* invocation would execute. Refused, not defaulted, when confined.

    Split out of `Browser.launch` so it fails closed on the same terms the
    profile does: the two used to be one `or` chain each, and only one of them
    was ever the documented risk.
    """
    override = os.environ.get(BINARY_ENV, "").strip()
    if override:
        return override
    if confined():
        raise no_fallback(
            "binary",
            BINARY_ENV,
            f"{DEFAULT_BINARY} is writable by the untrusted uid this deployment exists to keep "
            "away from the credential, so falling back to it would execute whatever that uid "
            "last wrote there as the uid holding every tenant's credentials.",
        )
    # Unconfined, no override: the ordinary resolution chain, PATH discovery
    # included. Only a confined deployment refuses to guess.
    return resolve_binary()


def resolve_binary() -> str:
    """Which browser to launch: the env var, the deployment default, then PATH.

    The precedence never changes with what happens to exist - an explicit
    `LINKEDIN_BROWSER_BINARY` is honoured even when it is wrong, because a
    silently-substituted browser is worse than a launch failure that names the
    path it was given. PATH discovery is the last resort and exists so a fresh
    checkout runs without configuration.
    """
    chosen = os.environ.get(BINARY_ENV, "").strip()
    if chosen:
        return chosen
    if Path(DEFAULT_BINARY).exists():
        return DEFAULT_BINARY
    for name in BINARY_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    # Nothing found. Returning the default rather than raising keeps the failure
    # in one place - the launch itself, which already reports the path it could
    # not execute and is the message an operator can act on.
    return DEFAULT_BINARY


def log_path(socket_path: str | Path | None = None) -> Path:
    """Where a spawned supervisor's output goes: beside its own socket.

    Inside the same 0700 directory and created 0600, like the socket. Nothing
    written there is ever a credential - see `_log` - but a file a detached
    process appends to unattended must not be the one place that stops being
    true without anybody noticing.
    """
    path = _resolve(socket_path)
    return path.with_name(path.name + LOG_SUFFIX)


def _log(event: str, detail: str) -> None:
    """The daemon's own account of itself, one line at a time on stderr.

    Never a request body, never a cookie and never a `seed` payload: the spawn
    points stderr at a file that outlives every invocation, so anything written
    here stays on disk until somebody deletes it - which is the point for a
    failure and a liability for a credential.
    """
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event}: {detail}", file=sys.stderr, flush=True)


def _prepare_dir(path: Path) -> None:
    """Create the containing directory 0700, and refuse if it is not.

    Not repaired when it is wrong. A directory anyone else can enter means the
    socket inside it was reachable for as long as that lasted, and quietly
    chmod-ing it back would hide the fact that something opened it up.
    """
    directory = path.parent
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        # `mkdir(mode=...)` is masked by the umask, so the mode is set after the
        # fact - a 0022 umask would otherwise leave this 0755.
        os.chmod(directory, 0o700)

    info = directory.stat()
    if info.st_uid != os.getuid():
        raise SupervisorError(
            f"{directory} is owned by uid {info.st_uid}, not by uid {os.getuid()}. The "
            "socket in there would be under someone else's control; point "
            f"{state.STATE_FILE_ENV} somewhere this uid owns."
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise SupervisorError(
            f"{directory} is mode {mode:04o}, so another uid on this machine can reach the "
            "socket inside it - and that socket is the only thing keeping li_at away from "
            f"them. Run `chmod 700 {directory}` and start again."
        )


# --------------------------------------------------------------- the listener


def _unix_socket() -> socket.socket:
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


def answers(path: Path, sock=None) -> bool:
    """Is anybody listening on `path`? The proof required before unlinking it.

    A supervisor killed with SIGKILL leaves its socket file behind, and a stale
    file makes every later invocation fail to connect forever. Unlinking on
    sight instead would let a second supervisor steal a live one's socket, and
    two supervisors mean two browsers and two copies of the credential.
    """
    probe = (sock or _unix_socket)()
    try:
        probe.settimeout(CONNECT_TIMEOUT)
        probe.connect(str(path))
    except TimeoutError:
        # Connecting to an AF_UNIX socket only blocks when the backlog is full,
        # which takes a listener - so a timeout is evidence *for* one, and this
        # branch must come first because TimeoutError is an OSError.
        return True
    except OSError:
        # Refused, missing, or not a socket at all: whatever is at that path,
        # nothing is answering on it.
        return False
    finally:
        probe.close()
    return True


def bind_socket(socket_path: str | Path | None = None, *, probe=None) -> socket.socket:
    """Bind and listen, with the permissions this design rests on enforced."""
    path = _resolve(socket_path)
    _prepare_dir(path)

    if path.exists():
        if (probe or answers)(path):
            raise SupervisorError(
                f"another supervisor is already listening on {path}, so this one would be a "
                "second browser holding a second copy of the credential. Talk to that one - "
                "`linkedin doctor` reports it - or stop it before starting another."
            )
        path.unlink(missing_ok=True)

    server = _unix_socket()
    # The umask is what makes the socket 0600 *at creation*. A bind followed by
    # a chmod leaves a window in which the socket exists group- or
    # world-accessible, and that window is all an attacker on the same box needs.
    old = os.umask(0o177)
    try:
        server.bind(str(path))
    except OSError as exc:
        server.close()
        # Almost always the length: the kernel caps a socket path at 104 bytes
        # on darwin and 108 on Linux - short enough that a relocated state
        # directory reaches it - and the bare errno is "AF_UNIX path too long"
        # with no mention of which path or how long.
        raise SupervisorError(
            f"could not bind {path}: {exc}. The path is {len(str(path).encode())} bytes and "
            f"a unix socket path is capped near 104; point {SOCKET_ENV} somewhere shorter."
        ) from exc
    finally:
        os.umask(old)
    # Belt and braces: not every platform applies the umask to a socket inode,
    # and this file is the boundary.
    os.chmod(path, 0o600)
    server.listen(BACKLOG)
    return server


# ---------------------------------------------------------------- the framing


def read_line(sock, limit: int = MAX_REQUEST) -> bytes:
    """One newline-delimited message, reassembled across reads.

    A short read splitting a request is normal on a stream socket; treating a
    single `recv` as a whole message works right up until the first write body
    large enough to be split, which is exactly when it must not.
    """
    buffer = bytearray()
    while True:
        end = buffer.find(b"\n")
        if end >= 0:
            return bytes(buffer[:end])
        if len(buffer) > limit:
            raise SupervisorError(f"the message is too long: over {limit} bytes with no newline")
        chunk = sock.recv(CHUNK)
        if not chunk:
            raise SupervisorError("the connection closed before the message was complete")
        buffer += chunk


def _failure(message: str, kind: str) -> dict:
    """The one error shape both ends speak.

    `kind` never carries a LinkedIn verdict: naming a session expired or a client
    blocked is `transport.py`'s job, and a copy of that taxonomy in here would
    drift from it one deploy after it was written. Three of the four are
    transport - closed, timeout, upstream.

    The fourth, `config`, is this side refusing before the wire rather than
    anything that happened on it, and it exists because it was being spelled
    `closed`: a confined deployment missing a policy key came back as "the
    supervisor stopped answering" from a supervisor that had answered fine, which
    sends an operator to restart a healthy daemon and never mentions the key.
    Which of the four a raised exception is belongs to `_kind` and to nothing
    else - a caller that decides for itself is how `config` and `upstream` came
    to name the same refusal.
    """
    return {"error": message, "kind": kind}


def _kind(exc: BaseException) -> str:
    """Which of the four failures this is. The only place that decides.

    The `__cause__` hop is not defensive programming. `cdp.CDPSession.call`
    catches `TimeoutError` and re-raises a `CDPError` that is *not* one,
    chaining the original - so after that conversion the chained cause is the
    only surviving evidence that the browser was slow rather than the script
    broken, and the two differ in what the caller should do next.

    `NoFallback` is here rather than at each raise site for the reason
    `no_fallback` itself is one function: the same lost policy key is refused on
    both sides of the socket, and it used to answer `config` from `_annotate` and
    `upstream` from `_dispatch` - one cause, two diagnoses, chosen by which op
    the caller happened to run.
    """
    if isinstance(exc, NoFallback):
        return "config"
    if isinstance(exc, pipe.PipeClosed):
        return "closed"
    if isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError):
        return "timeout"
    return "upstream"


# ------------------------------------------------------------------- serving


def serve(
    socket_path: str | Path | None = None,
    *,
    browser=None,
    idle_timeout: float = IDLE_TIMEOUT,
    listener=None,
    now=None,
    launch=None,
) -> None:
    """Own one browser, answer on the socket, and exit when nobody is asking.

    Single-threaded on purpose. `pipe.launch` is explicitly not reentrant - fds
    3 and 4 are process-global and are borrowed for the length of a launch - and
    one browser answering one request at a time is also the only shape in which
    the pacing in `state.py` means anything.

    No signal handling: a supervisor killed outright leaves its socket file
    behind, and `bind_socket` already treats a file nobody answers on as stale.
    Recovering from the wreckage is strictly more reliable than trying to
    guarantee a clean exit.
    """
    path = _resolve(socket_path)
    clock = now or time.monotonic
    started_at = time.time()

    # Bound before the browser is launched, so that the client whose autostart
    # this is queues in the backlog and waits out the launch instead of finding
    # a refused connect and starting a *second* supervisor.
    server = listener if listener is not None else bind_socket(path)
    resident = Resident(browser, launch=launch)
    try:
        if browser is None:
            resident.start()
        _accept_loop(server, path, resident, idle_timeout, clock, started_at)
    finally:
        # The listener goes first: while it is open the kernel still accepts,
        # and a client landing in the teardown window would wait out the
        # browser's exit only to be handed an EOF.
        server.close()
        path.unlink(missing_ok=True)
        # Closed even when it was injected, and whichever browser is current
        # after a relaunch. A browser is reachable only through the descriptors
        # this process holds, so one that outlives its supervisor is both
        # unreachable and immortal.
        resident.close()


class Resident:
    """The browser the daemon owns, and the two ways it may be brought back.

    Recovery lives out here rather than on `Browser` for two reasons. Replacing
    a browser is something no method on the old one can do, and the bound on how
    often it may happen belongs to the daemon rather than to any single browser:
    one relaunch, after which exiting is strictly better, because the next
    invocation's autostart is a cleaner fresh start than anything this process
    can arrange.
    """

    def __init__(self, browser=None, *, launch=None):
        self._browser = browser
        self._launch = launch or Browser.launch
        self.relaunched = False

    def start(self) -> None:
        self._browser = self._launch()

    @property
    def profile(self) -> str:
        return getattr(self._browser, "profile", "")

    def fetch(self, method: str, path: str, body):
        return self._browser.fetch(method, path, body)

    def seed(self, cookies: list) -> dict:
        return self._browser.seed(cookies)

    def page_url(self) -> str:
        return self._browser.page_url()

    def renavigate(self) -> bool:
        """Put the page back on the parked URL. False when it would not go."""
        if self._browser is None:
            return False
        try:
            self._browser.reload()
        except Exception as exc:  # noqa: BLE001 - a repair that fails is the next rung's cue
            _log("renavigate-failed", str(exc))
            return False
        return True

    def relaunch(self) -> bool:
        """A whole new browser, once per daemon and never twice."""
        if self.relaunched:
            return False
        self.relaunched = True
        old, self._browser = self._browser, None
        if old is not None:
            old.close()
        try:
            self._browser = self._launch()
        except Exception as exc:  # noqa: BLE001 - reported here, then the daemon exits
            _log("relaunch-failed", str(exc))
            return False
        return True

    def close(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            browser.close()


def _accept_loop(
    server, path: Path, resident, idle_timeout: float, clock, started_at: float
) -> None:
    deadline = clock() + idle_timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return
        # Without this the accept blocks forever and the idle timeout is dead
        # code: a supervisor with nobody asking would hold its browser - and the
        # credential in it - until the machine went down.
        server.settimeout(remaining)
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue  # the loop re-reads the clock rather than trusting this one
        with contextlib.closing(connection):
            if not _serve_connection(connection, resident, path, started_at):
                return
        deadline = clock() + idle_timeout


def _serve_connection(connection, resident, path: Path, started_at: float) -> bool:
    """Answer one request. Returns False when the daemon should stop."""
    connection.settimeout(REQUEST_TIMEOUT)
    try:
        # A request, so the tight cap: this is the untrusted direction.
        line = read_line(connection, MAX_REQUEST)
    except (OSError, SupervisorError):
        # A caller that died mid-request costs itself an answer and nothing else.
        return True

    try:
        payload = json.loads(line)
    except ValueError as exc:
        response, keep = _failure(f"the request is not JSON: {exc}", "upstream"), True
    else:
        response, keep = _dispatch(payload, resident, path, started_at)

    try:
        connection.sendall(json.dumps(response).encode() + b"\n")
    except OSError:
        pass  # the caller hung up; there is nobody left to tell
    return keep


def _reported_page_url(href: str) -> str:
    """Where the page is, without what the query string carries.

    A checkpoint URL echoes the csrf token back as `?ct=` (`transport.py:284`),
    and the csrf token **is** the JSESSIONID cookie value - so `location.href`
    handed out whole is a live session credential, on a success path where
    nothing downstream scrubs. `render.ok` cannot: `profile get` legitimately
    returns an `ACoAA…` member urn that the same patterns would eat.

    Cut here, at the source, rather than in either caller: this dict reaches
    output down two paths, and a reduction in one of them leaves the other open.
    `cli.doctor` renders it, and `browser.py` returns it verbatim as a
    `--dry-run` preview's `runs_in` - the second being an allowlisted verb that
    skips the ledger, the cap, the breaker and every LinkedIn round trip, i.e. a
    credential oracle repeatable at whatever rate the caller is paced to. The
    first attempt at this fix reduced it in `cmd_doctor` alone and left that one
    standing; the test passed, because it only ever exercised `doctor`.

    The path survives, and that is the point of keeping the field at all: "which
    page is it on" is the question `status` exists to answer (`Browser.page_url`)
    and `/checkpoint/challenge/…` is that answer. Only the query goes.

    A URL with no path of its own falls back to everything but the query, which
    is `_reported_location` below. Two cases arrive that way and both are pages
    that most need naming: `chrome-error://chromewebdata`, where the request
    never left the browser - the shape an over-grown Cookie header produces
    (`Browser.seed`) - and `about:blank`, which is where every target starts and
    what `Browser._navigate` refuses over when a navigation does not commit.

    The `netloc` half of the first branch is what makes the second case work.
    `about:blank` has no authority, so `urlsplit` calls the whole of it a
    `.path` - and cutting to the path alone renamed a diagnosis this module
    writes its own error message about to `blank`, a page by no name at all.
    """
    split = urlsplit(str(href))
    if split.path and split.netloc:
        return split.path
    return _reported_location(href)


def _reported_location(href: str) -> str:
    """The whole of `href` except its query string and fragment.

    The same cut as `_reported_page_url` and a different amount kept, because
    the two callers need different halves: `status` answers "which page", where
    the host is always linkedin.com and the path carries the diagnosis, while
    `Browser._navigate` refuses precisely *because* the host may no longer be
    linkedin.com - so naming the origin it landed on is the whole message.

    `hostname` rather than `netloc`: an authority can carry `user:password@`,
    and a credential in the URL is the one thing this reduction exists to keep
    out of an answer. Anything that parses as no part of a URL comes back empty
    rather than guessed at - an unparsed href is exactly where a query string
    would still be hiding.
    """
    split = urlsplit(str(href))
    return urlunsplit((split.scheme, split.hostname or "", split.path, "", ""))


def _dispatch(payload, resident, path: Path, started_at: float) -> tuple[dict, bool]:
    if not isinstance(payload, dict):
        return _failure("the request must be a JSON object", "upstream"), True

    op = payload.get("op")
    if op == "shutdown":
        return {"ok": True}, False

    try:
        if op == "fetch":
            return _fetch(resident, payload)
        if op == "seed":
            return resident.seed(payload.get("cookies") or []), True
        if op == "status":
            return {
                "pid": os.getpid(),
                "socket": str(path),
                # Named so an operator has somewhere to look when this process
                # is the thing that is broken.
                "log": str(log_path(path)),
                "profile": resident.profile,
                "page_url": _reported_page_url(resident.page_url()),
                "relaunched": resident.relaunched,
                "started_at": started_at,
            }, True
    except pipe.PipeClosed as exc:
        # The one failure worth dying of: the browser is gone and cannot be
        # reached again from here, so answering the caller and exiting hands the
        # next invocation a clean autostart instead of a supervisor that fails
        # every call until somebody notices.
        return _failure(str(exc), "closed"), False
    except Exception as exc:  # noqa: BLE001 - one bad request must not end the daemon
        return _failure(str(exc), _kind(exc)), True

    return _failure(f"unknown op {op!r}", "upstream"), True


# What a page whose network service has died under it says to every fetch, and
# what a page that has slipped off the origin says when the script reads
# `document.cookie`. Both are the *page* being broken rather than LinkedIn
# answering badly, and both are repairable from here.
DEAD_PAGE_MARKERS = ("failed to fetch", "networkerror", "securityerror", "load failed")


def _looks_dead(response: dict) -> bool:
    """Did the page stop working, or did LinkedIn merely answer something bad?

    Only the first is this daemon's problem. A 500, a 999 and a rotated queryId
    all arrive as ordinary results and are `transport.py`'s business; an in-page
    `fetch` that *threw* never got far enough to have a status at all.
    """
    error = str(response.get("error") or "").lower()
    return any(marker in error for marker in DEAD_PAGE_MARKERS)


def _fetch_once(resident, method: str, path: str, body) -> dict:
    result = resident.fetch(method, path, body)
    if not isinstance(result, dict):
        return _failure(
            f"the page returned {type(result).__name__}, not a fetch result", "upstream"
        )
    if "error" in result:
        # The injected script catches its own `fetch` throwing and reports it
        # this way; naming the transport kind is all the daemon adds.
        return _failure(str(result["error"]), "upstream")
    # Returned verbatim, extra keys and all: the CLI is what knows which of them
    # mean anything, and this way the daemon survives the script growing new ones.
    return result


def _fetch(resident, payload: dict) -> tuple[dict, bool]:
    """One fetch, and the bounded repair of a page that has stopped answering.

    A resident browser dies quietly. After 44 hours `page_url()` still read
    /feed/ while every in-page fetch failed, because Chrome's network service
    had gone out from under the renderer - so the failing fetch is the only
    health signal there is, and nothing weaker than trying one detects it.

    Two rungs, each attempted at most once, and running out of them is not
    another retry: it is the daemon exiting, so the next invocation autostarts a
    browser this process could not have repaired.
    """
    method = str(payload.get("method") or "GET").upper()
    path = str(payload.get("path") or "")
    body = payload.get("body")

    response = _fetch_once(resident, method, path, body)
    if not _looks_dead(response):
        return response, True

    for repair in (resident.renavigate, resident.relaunch):
        if not repair():
            continue
        _log("recovered", f"{repair.__name__} after {response.get('error')}")
        # Only a read may be issued twice. A write that failed after leaving the
        # page may already have reached LinkedIn, and `browser.py` calls that
        # `OutcomeUnknown` precisely so that nothing re-sends it from in here.
        if method != "GET":
            return response, True
        response = _fetch_once(resident, method, path, body)
        if not _looks_dead(response):
            return response, True

    _log("unrecoverable", str(response.get("error")))
    return _failure(
        f"{response.get('error')}, and the page could not be brought back: re-navigating and "
        "relaunching the browser both failed. This supervisor is stopping so that the next "
        "call starts a fresh one.",
        "closed",
    ), False


# ------------------------------------------------------------------- asking


def request(
    payload: dict,
    *,
    socket_path: str | Path | None = None,
    autostart: bool = True,
    sock=None,
    spawn=None,
    lock=None,
    sleep=None,
    now=None,
) -> dict:
    """Send one request to the supervisor, starting one if there is none.

    Never raises. Every failure comes back in the `{"error", "kind"}` shape the
    daemon itself uses, so `browser.py` has exactly one thing to check and the
    taxonomy stays in `transport.py`.
    """
    # Inside the promise, not in front of it. The socket path is derived from
    # the ledger's parent, and `state.resolve_path` *refuses* under a confined
    # deployment rather than defaulting - so the one lost policy key that
    # reaches this function before anything is connected used to escape it as an
    # exception. `browser.py` catches that as "the browser supervisor failed" and
    # calls a POST which never left this process `outcome_unknown`, telling an
    # agent a message may have landed when nothing was ever sent.
    try:
        path = _resolve(socket_path)
    except SupervisorError as exc:
        return _failure(str(exc), _kind(exc))
    factory = sock or _unix_socket

    # Encoded before anything is connected or started: a payload that cannot be
    # serialised is the caller's bug, and it must not cost a browser launch - or
    # escape as a bare TypeError, which is the one way this function could break
    # its promise never to raise.
    try:
        message = json.dumps(payload).encode() + b"\n"
    except (TypeError, ValueError) as exc:
        return _failure(f"the request cannot be encoded as JSON: {exc}", "upstream")

    client = _connect(path, factory)
    if client is None:
        if not autostart:
            return _failure(
                f"no supervisor is listening on {path} and autostart is off, so nothing was "
                "started. Any ordinary command starts one; `linkedin doctor` reports whether "
                "it is up.",
                "closed",
            )
        try:
            client = _autostart(path, factory, spawn=spawn, lock=lock, sleep=sleep, now=now)
        except SupervisorError as exc:
            return _failure(str(exc), "closed")

    with contextlib.closing(client):
        try:
            return _annotate(payload, _exchange(client, message))
        except (OSError, ValueError, SupervisorError) as exc:
            # Never retried, and never autostarted from here. The request has
            # already been sent, so a resend of a write can duplicate it - which
            # is the whole reason `transport.OutcomeUnknown` exists.
            return _failure(f"the supervisor stopped answering: {exc}", "closed")


def _annotate(payload, answer: dict) -> dict:
    """Say so when the resident supervisor is not on the profile that was asked for.

    The profile binds at launch, so once one supervisor is up every later
    invocation is served by whatever profile *it* opened and `PROFILE_ENV` is
    ignored - silently, with `doctor` reporting the running profile as though it
    were the intended one. The comparison is here rather than in the daemon
    because only this side knows both halves: the daemon's environment was
    frozen whenever it started, which may have been days ago.

    The resolver it asks can *refuse* - see `requested_profile` - and that
    refusal is caught here rather than left to `request`'s handler, which reports
    "the supervisor stopped answering". It answered; this side does not know what
    to compare its answer against. Reported as the config failure it is, and the
    daemon's status is dropped with it deliberately: an unannotated status is the
    running profile read as though it were the intended one, which is the exact
    failure this function exists to prevent.
    """
    if not isinstance(payload, dict) or payload.get("op") != "status":
        return answer
    if "profile" not in answer:
        return answer

    try:
        wanted = requested_profile()
    except SupervisorError as exc:
        return _failure(str(exc), _kind(exc))
    running = str(answer.get("profile") or "")
    answer["requested_profile"] = wanted
    answer["profile_mismatch"] = bool(running) and running != wanted
    if answer["profile_mismatch"]:
        answer["warning"] = (
            f"this supervisor (pid {answer.get('pid')}) is serving the browser profile "
            f"{running}, not the {wanted} this invocation asked for. The profile binds at "
            f"launch, so {PROFILE_ENV} was ignored; stop that supervisor before expecting "
            "anything to be served from the requested profile."
        )
    return answer


def _connect(path: Path, factory):
    """A connected socket, or None when nobody is answering there.

    Every `OSError` is "nobody is answering": a refused connect, a missing file
    and a leftover regular file all mean the same thing to the caller, which is
    that it has to start one. If the real problem is the directory, the
    autostart path says so precisely - it takes its lock in there.
    """
    client = factory()
    try:
        client.settimeout(CONNECT_TIMEOUT)
        client.connect(str(path))
    except OSError:
        client.close()
        return None
    except BaseException:
        client.close()
        raise
    return client


def _exchange(client, message: bytes) -> dict:
    client.settimeout(RESPONSE_TIMEOUT)
    client.sendall(message)
    # An answer, so the generous cap. A page of thirty feed posts is over a
    # megabyte and the tight cap refused it live.
    response = json.loads(read_line(client, MAX_RESPONSE))
    if not isinstance(response, dict):
        raise SupervisorError(
            f"the supervisor answered with {type(response).__name__}, not an object"
        )
    return response


def _autostart(path: Path, factory, *, spawn=None, lock=None, sleep=None, now=None):
    """Start exactly one supervisor, however many invocations arrive at once."""
    guard = lock or _flock
    with guard(path.with_name(path.name + LOCK_SUFFIX)):
        # Re-checked *inside* the lock. Between the unlocked look and this line
        # another invocation may have started one, and starting a second means a
        # second browser and a second copy of the credential. The lock alone
        # does not prevent that - the re-check under it does.
        client = _connect(path, factory)
        if client is not None:
            return client
        try:
            (spawn or spawn_supervisor)(path)
        except OSError as exc:
            raise SupervisorError(f"could not start a supervisor: {exc}") from exc
        # Waited out while still holding the lock, so a second invocation queues
        # behind a supervisor that is starting rather than starting its own.
        return _await(path, factory, sleep=sleep, now=now)


def _await(path: Path, factory, *, sleep=None, now=None, timeout: float = STARTUP_TIMEOUT):
    nap = sleep or time.sleep
    clock = now or time.monotonic
    deadline = clock() + timeout
    while True:
        client = _connect(path, factory)
        if client is not None:
            return client
        if clock() >= deadline:
            raise SupervisorError(
                f"the supervisor did not start listening on {path} within {timeout:.0f}s. It "
                f"writes why it stopped to {log_path(path)}; failing that, check that "
                f"{BINARY_ENV} points at a browser that runs on this machine."
            )
        nap(POLL_INTERVAL)


@contextlib.contextmanager
def _flock(path: Path):
    """Cross-process mutual exclusion for the launch, as `state.py` does it.

    Never nest this: flock is granted per open file description, so a second
    acquisition from this same process deadlocks against the first rather than
    being re-entrant.
    """
    _prepare_dir(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def supervisor_argv(path: Path) -> list[str]:
    """How the daemon is started: this module, on that socket.

    Deliberately not a CLI verb. The supervisor has to be startable without
    depending on how `cli.py` happens to dispatch, and `sys.executable` keeps it
    inside whatever interpreter the CLI itself is running under.
    """
    return [sys.executable, "-m", __spec__.name, str(path)]


def _page_script(method: str, path: str, body) -> str:
    """Fill the injected fetch template `browser.py` owns.

    Imported here rather than at module scope because `browser.py` imports this
    module to reach `request`, and a top-level `from .browser import SCRIPT`
    would make that cycle resolve differently depending on which of the two the
    CLI happened to import first. Importing it at all - rather than keeping a
    copy - is the point: two copies of the script drift, and the one living in
    the resident process is the one nobody remembers to update.

    `path` goes in unresolved. The page is already on linkedin.com, so the
    browser resolves it against that origin, which is why the daemon needs no
    `BASE` and no idea what Voyager is called.
    """
    from .browser import SCRIPT

    # A caller that serialised its own payload must not have it encoded twice
    # into a JSON string that contains JSON.
    text = body if body is None or isinstance(body, str) else json.dumps(body)
    return SCRIPT % {
        "url": json.dumps(path),
        "method": json.dumps(method),
        "body": json.dumps(text),
    }


# The claimed identity is operator-specific, so every field is overridable and
# the built-in defaults are deliberately neutral rather than any real machine's.
IDENTITY_WIDTH_ENV = "LINKEDIN_IDENTITY_WIDTH"
IDENTITY_HEIGHT_ENV = "LINKEDIN_IDENTITY_HEIGHT"
IDENTITY_SCALE_ENV = "LINKEDIN_IDENTITY_SCALE"
IDENTITY_TIMEZONE_ENV = "LINKEDIN_IDENTITY_TIMEZONE"
IDENTITY_LOCALE_ENV = "LINKEDIN_IDENTITY_LOCALE"


def _env_number(name: str, default, cast):
    """An identity dimension from the environment, or the neutral default.

    A malformed override is ignored rather than fatal: the whole point of these
    values is that the browser presents a coherent identity, and refusing to
    start over a stray character would be a worse outcome than the default.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


def _host_timezone() -> str:
    """The host's own timezone where it states one, else UTC.

    The claimed timezone has to be consistent with how the account is actually
    used, and the closest thing to that this process can read without being told
    is the host it runs on, which states it in `TZ`. A container that sets
    nothing gets a definite UTC rather than a guess at somewhere else.
    """
    return os.environ.get("TZ", "").strip() or "UTC"


@dataclass(frozen=True)
class Identity:
    """The coherent identity the headless browser has to claim.

    A headless browser does not present like an ordinary one: left uncorrected
    it reports `HeadlessChrome` in the UA on **every** call, `displayDensity` 1
    and an 800x600 display, and - in a container - UTC regardless of the
    operator. `x-li-track` carries the display metrics and timezone on every
    request, so an identity that is internally inconsistent, or inconsistent
    with how the account is actually used, is itself a signal. See
    docs/voyager-headers.md.

    The requirement is therefore consistency, not any specific hardware, which
    is why the defaults are neutral and every field is overridable by an
    environment variable so an operator can match their own environment.
    """

    width: int = field(default_factory=lambda: _env_number(IDENTITY_WIDTH_ENV, 1920, int))
    height: int = field(default_factory=lambda: _env_number(IDENTITY_HEIGHT_ENV, 1080, int))
    scale: float = field(default_factory=lambda: _env_number(IDENTITY_SCALE_ENV, 1.0, float))
    timezone: str = field(
        default_factory=lambda: os.environ.get(IDENTITY_TIMEZONE_ENV, "").strip()
        or _host_timezone()
    )
    # Bare, never a full Accept-Language list: passing `en-US,en;q=0.9` produced
    # `en-US,en;q=0.9;q=0.9` back, a doubled q-value that is a malformed header
    # in its own right. The bare locale round-trips cleanly.
    locale: str = field(
        default_factory=lambda: os.environ.get(IDENTITY_LOCALE_ENV, "").strip() or "en-US"
    )


class Browser:
    """The one browser the supervisor owns, and the one page it keeps.

    Constructed over a `cdp.CDPSession` *and* the connection under it: the
    session drops every message that is not the reply it is waiting for, events
    included, so waiting for `Page.loadEventFired` means reading the pipe
    directly. That is also why `cdp.CDPSession.navigate` is not used - it sleeps
    a flat five seconds instead, which is both a guess in production and a real
    sleep in the suite.
    """

    def __init__(self, session, connection, *, process=None, profile: str = "", identity=None):
        self._session = session
        self._connection = connection
        self._process = process
        self.profile = profile
        self.page = PAGE_URL
        self.identity = identity or Identity()
        self._control = None
        self._started = False

    @classmethod
    def launch(
        cls,
        binary: str | None = None,
        profile_dir: str | None = None,
        *,
        headless: bool = True,
        spawn=None,
        identity=None,
        open_pipe=None,
    ) -> Browser:
        binary = binary or requested_binary()
        profile_dir = profile_dir or requested_profile()
        extra = list(LAUNCH_ARGS)
        if os.environ.get(NO_SANDBOX_ENV, "").strip() not in ("", "0", "false"):
            extra.append("--no-sandbox")
        elif os.getuid() == 0:
            raise SupervisorError(
                "Chromium will not run as root with its sandbox on, and this browser "
                "renders whatever LinkedIn sends it beside the only credential. Either "
                f"run as a non-root uid, or set {NO_SANDBOX_ENV}=1 to accept an "
                "unsandboxed renderer deliberately."
            )
        connection, process = (open_pipe or pipe.launch)(
            binary,
            profile_dir,
            headless=headless,
            extra_args=extra,
            spawn=spawn,
            timeout=CDP_TIMEOUT,
        )
        control = cdp.CDPSession(connection)
        target = control.call("Target.createTarget", {"url": "about:blank"})
        attached = control.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        # A second session over the same connection, not a second connection.
        # Its ids restart at 1, which is only safe because the handshake above is
        # finished and drained - nothing of the control session's is still in
        # flight to be mistaken for a reply to this one.
        page = cdp.CDPSession(connection, attached["sessionId"])
        browser = cls(page, connection, process=process, profile=profile_dir, identity=identity)
        browser._control = control
        browser.start()
        return browser

    # ------------------------------------------------------------------ startup

    def start(self) -> None:
        """Correct the identity, then park the page on linkedin.com."""
        if self._started:
            return
        self._apply_identity()
        self._navigate(self.page)
        # Last, so a start that failed half way is retried in full rather than
        # leaving a page running with an uncorrected identity.
        self._started = True

    def reload(self) -> None:
        """Drive the page back onto the origin after it stopped answering.

        A full re-start rather than a bare navigate. The identity overrides are
        per-target, and a renderer that has lost its network service is exactly
        the kind of wreckage that may have lost those as well - a browser that
        came back with an uncorrected user agent would present `HeadlessChrome`,
        inconsistent with the rest of its identity, on every call from then on,
        which is worse than staying dead.
        """
        self._started = False
        self.start()

    def _apply_identity(self) -> None:
        real = self._session.evaluate("navigator.userAgent")
        if not isinstance(real, str) or not real:
            raise SupervisorError(
                "the page reported no user agent, so its headless marker could not be "
                "corrected. Refusing to run: every in-page call would carry an identity "
                "inconsistent with an ordinary browser."
            )
        self._session.call(
            "Emulation.setUserAgentOverride",
            {
                # Taken from the browser rather than pinned in source: a UA
                # string that disagrees with the `sec-ch-ua` headers the browser
                # sends by itself would be internally inconsistent, so it is
                # derived from the real one rather than invented.
                "userAgent": real.replace("Headless", ""),
                "acceptLanguage": self.identity.locale,
            },
        )
        self._session.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.identity.width,
                "height": self.identity.height,
                "deviceScaleFactor": self.identity.scale,
                "mobile": False,
                # Measured: `x-li-track`'s displayWidth/displayHeight follow
                # these two, *not* width/height. Setting only the viewport still
                # reports 800x600 times the scale factor.
                "screenWidth": self.identity.width,
                "screenHeight": self.identity.height,
            },
        )
        self._session.call("Emulation.setTimezoneOverride", {"timezoneId": self.identity.timezone})

    def _navigate(self, url: str) -> None:
        # Before the navigate, or the load event is never delivered.
        self._session.call("Page.enable")
        self._session.call("Page.navigate", {"url": url})
        self._wait_for_load()

        # The load event alone is not proof of arrival. The target is created on
        # about:blank, so its own load can fire after Page.enable and be taken
        # for this one - and an in-page fetch on about:blank does not merely
        # return the wrong thing, it throws SecurityError reading document.cookie
        # because an opaque origin has none. Arrival is therefore confirmed
        # against the document itself.
        deadline = time.monotonic() + LOAD_TIMEOUT
        while True:
            here = str(self._session.evaluate("location.href") or "")
            if here.startswith(ORIGIN):
                return
            if time.monotonic() > deadline:
                # Reduced like every other href this daemon reports, and this
                # one was the last that was not. It is only reachable when the
                # page is *off* linkedin.com - a checkpoint returns above - so
                # the `?ct=` leak that motivated the rule cannot arrive here;
                # but "the query string is where the credential is" is the rule,
                # not "linkedin.com's query string is", and this message is
                # interpolated into `_failure(str(exc), …)` and handed to the
                # client. Where it landed is what the message is for, so the
                # origin and the path both survive.
                raise SupervisorError(
                    f"the page is on {_reported_location(here) or 'about:blank'} rather than the "
                    f"{ORIGIN} origin; the navigation did not take"
                )
            # Polled rather than waiting on another load event: the load has
            # already fired and been consumed above. What is seen here is the
            # blank document Chromium swaps in while the navigation commits, and
            # no further event announces it going away.
            time.sleep(POLL_INTERVAL)

    def _wait_for_load(self, timeout: float = LOAD_TIMEOUT) -> None:
        """Drain the pipe until the document is done.

        Reading the connection under the session is deliberate: `CDPSession`
        discards everything that is not the reply it is waiting for, so an event
        can only be seen from down here. Chromium answers `Page.navigate` when
        the navigation commits and fires `load` afterwards, so the reply has
        already been taken by the call above.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SupervisorError(f"{self.page} did not finish loading within {timeout:.0f}s")
            try:
                message = json.loads(self._connection.recv(timeout=remaining))
            except ValueError:
                continue
            if message.get("method") == "Page.loadEventFired":
                return

    # ------------------------------------------------------------------ requests

    def fetch(self, method: str, path: str, body=None) -> dict:
        self.start()
        return self._session.evaluate(_page_script(method, path, body))

    def seed(self, cookies: list) -> dict:
        """Install a jar copied from the operator's Chrome, then prove it works.

        The only op that accepts a credential, and it runs once. What comes back
        is deliberately not a bare 200: a session LinkedIn has flagged but not
        yet blocked answers 200 too, which is what this account did shortly
        before it was invalidated. The caller gets the environment this browser
        would present and whether the page is genuinely signed in, and decides.
        """
        self.start()
        # Replace rather than accumulate. Seeding is re-run whenever a session
        # dies, and every run adds another generation of the same names: the
        # Cookie header grows until the request is rejected outright and the
        # browser lands on chrome-error://chromewebdata, which looks nothing
        # like a cookie problem.
        self._session.call("Network.clearBrowserCookies")
        installed = 0
        for cookie in cookies:
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            params = {
                "name": cookie["name"],
                "value": cookie.get("value", ""),
                "domain": cookie.get("domain") or ".linkedin.com",
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure")),
                "httpOnly": bool(cookie.get("httpOnly")),
            }
            if cookie.get("sameSite") in ("Strict", "Lax", "None"):
                params["sameSite"] = cookie["sameSite"]
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                params["expires"] = expires
            if self._session.call("Network.setCookie", params).get("success"):
                installed += 1

        self._navigate(PAGE_URL)
        probe = self._session.evaluate(
            "({userAgent: navigator.userAgent, webdriver: navigator.webdriver,"
            " screenWidth: screen.width, screenHeight: screen.height,"
            " timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,"
            " signedIn: !/(login|uas|authwall|checkpoint)/.test(location.pathname)})"
        )
        member_urn = None
        signed_in = False
        response = self.fetch("GET", transport.BASE + "me")
        if isinstance(response, dict) and int(response.get("status") or 0) == 200:
            with contextlib.suppress(Exception):
                data = json.loads(response.get("body") or "{}").get("data") or {}
                member_urn = data.get("*miniProfile") or data.get("miniProfile")
            signed_in = bool(probe.get("signedIn")) and member_urn is not None

        return {
            "installed": installed,
            "profile": self.profile,
            "environment": probe,
            "verified": {"signed_in": signed_in, "member_urn": member_urn},
        }

    def page_url(self) -> str:
        """Where the page actually is - read, not remembered.

        "Which page is it on" is the question `status` exists to answer, and a
        session parked on a checkpoint is exactly the answer an operator needs.
        """
        self.start()
        return str(self._session.evaluate("location.href"))

    def close(self) -> None:
        """Never raises. This runs from the daemon's exit path, where the
        alternative to a failed close is a browser nothing can ever reach
        again: its CDP pipe is a descriptor only this process holds."""
        # Ask the browser to exit before signalling it. Chromium writes its
        # cookie store lazily, and a SIGTERM'd browser can die with the session
        # still only in memory - which is exactly how a successful login left
        # the profile's Cookies table empty and every later invocation started
        # signed out. A clean shutdown is what flushes it.
        if self._control is not None:
            with contextlib.suppress(Exception):
                self._control.call("Browser.close", timeout=TERMINATE_TIMEOUT)
        process, self._process = self._process, None
        if process is not None:
            with contextlib.suppress(Exception):
                process.wait(timeout=TERMINATE_TIMEOUT)

        with contextlib.suppress(Exception):
            self._connection.close()
        if process is None:
            return
        # Only if it ignored the polite request.
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=TERMINATE_TIMEOUT)


def _open_log(path: Path) -> int:
    """The log, opened for append at 0600 inside its 0700 directory."""
    _prepare_dir(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with contextlib.suppress(OSError):
        if os.fstat(fd).st_size > LOG_MAX:
            os.ftruncate(fd, 0)
    return fd


def spawn_supervisor(path: Path, *, popen=None, open_log=None) -> None:
    # stdout and stderr were DEVNULL, which is why a browser that would not
    # start produced nothing at all: the daemon is detached from whoever spawned
    # it, so these two descriptors are the only account of itself it can leave.
    log = (open_log or _open_log)(log_path(path))
    try:
        (popen or subprocess.Popen)(
            supervisor_argv(path),
            # It has to outlive the invocation that starts it: a Ctrl-C or a
            # dropped ssh session must not take down a browser other invocations
            # are sharing.
            start_new_session=True,
            close_fds=True,
            # Nothing is ever asked of it on stdin, and the CLI's own stdout
            # carries JSON that a daemon writing to it would corrupt.
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        # Ours only until Popen has duplicated it into the child.
        os.close(log)


def main(argv: list[str] | None = None) -> int:
    """`python -m linkedin_cli.supervisor [socket]`, which is how it is started.

    It is spawned detached, with these two descriptors pointed at the log beside
    the socket - so what is written here is the only account anybody gets of a
    supervisor that would not start. The exit status alone was not one.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else None
    try:
        serve(path)
    except SupervisorError as exc:
        _log("refused", str(exc))
        return SupervisorError.exit_code
    except Exception:  # noqa: BLE001 - an unread traceback is the failure being lost again
        _log("crashed", traceback.format_exc())
        return SupervisorError.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
