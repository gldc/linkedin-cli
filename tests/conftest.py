"""One autouse guard that makes the suite incapable of reaching this machine.

Two incidents shaped this file. A test once resolved the operator's real Chrome
profile and went through the macOS Keychain, which cost thirty seconds on every
run; another left a retry sleep unmocked and took the suite from seven seconds
to fifty. The browser pivot raises the stakes rather than lowering them: the
code now launches Chromium and opens sockets, and the cookie reader in `cdp.py`
resolves the profile from whatever path it is handed, so a test that forgets to
inject one attaches to the operator's *live* browser and pulls their real
`li_at` into the test process. That is not hypothetical - while this guard was
being written, an unguarded probe of `127.0.0.1:9222` from a test got a real
HTTP response back from the operator's own Chrome.

So the rule is inverted here: reaching outside the process is a test *failure*,
not a slow test. Every seam the package deliberately exposes - `spawn=`,
`sock=`, `opener=`, `cdp_session=` - exists because of this file, and the
AssertionError names the one that was skipped.

Two halves, and the second is the one people forget:

* Relocation. Every `LINKEDIN_*` variable is repointed under pytest's own
  temporary root or removed, so a code path that falls back to the environment
  lands in a throwaway directory rather than on `~/.local/state` or the
  operator's Chrome. A blocked socket does not help if the profile path was
  already the real one.
* Interception. `Popen`, `connect`, `connect_ex`, `create_connection` and the
  urllib opener raise instead of running.

`@pytest.mark.real_process` opts out of the *interception* only - never the
relocation - and exists for the handful of tests that must prove cross-process
behaviour, where a fake would prove nothing. `state.State._locked` is the
reason: flock is granted per open file description, so mutual exclusion is only
real between two actual processes.

Two limits worth knowing before trusting this file:

* The guard is function-scoped, so it is installed *after* every module- and
  session-scoped fixture has already run. Work done in a higher-scoped fixture
  is outside the guard entirely - keep spawning and connecting inside the test
  or a function-scoped fixture.
* Relocation only covers what the package reads from the environment, and one
  path is not read from the environment at all. `bootstrap.SOURCE_PROFILES` is
  the operator's *own* Chrome - `~/Library/Application Support/Google/Chrome`,
  `~/.config/google-chrome` - and `bootstrap.source_profile` falls back to it
  whenever `LINKEDIN_SOURCE_PROFILE` is unset, which after the scrub below it
  always is. `LINKEDIN_BROWSER_PROFILE` does not redirect it: that names the
  *managed* profile, which is a different directory on purpose. Interception is
  what contains this one - reading a live jar out of that profile means
  `cdp.read_port_file` and then a websocket, so it dies on `connect`.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import urllib.request

import pytest

# Where each relocatable variable points during a test. The values are paths
# that do not exist yet: a test that reads one gets "no history", and a test
# that writes one leaves the mess under pytest's temporary root.
RELOCATED = {
    "LINKEDIN_STATE_FILE": "state/state.json",  # state.resolve_path
    "LINKEDIN_BROWSER_PROFILE": "profile",  # supervisor.requested_profile
    "LINKEDIN_BROWSER_BINARY": "bin/chrome",  # supervisor.resolve_binary
}

# Everything else in the namespace is scrubbed rather than replaced, because
# there is no safe stand-in for it: `LINKEDIN_SOURCE_PROFILE` names the
# operator's own signed-in Chrome, `LINKEDIN_SOCKET` names a resident supervisor
# that is already holding a live session, and `LINKEDIN_BROWSER_NO_SANDBOX`
# changes how a real browser gets launched. Scrubbing by prefix rather than by
# name is deliberate - `surfaces/messaging.py` reads
# `LINKEDIN_QUERY_ID_<NAME>` built at call time, so no fixed list can stay
# complete, and a variable added after this file was written must not be able
# to leak the operator's shell into a test.
PREFIX = "LINKEDIN_"

# (owner, attribute, the seam the caller should have injected). `connect_ex` is
# here because it is the natural spelling of "is the supervisor's socket up
# yet?" - it opens the same connection as `connect` but reports the errno
# instead of raising, so a liveness probe written with it would sail past a
# guard that only covered `connect`.
SEAMS = (
    (subprocess, "Popen", "spawn="),
    (socket.socket, "connect", "sock="),
    (socket.socket, "connect_ex", "sock="),
    (socket, "create_connection", "sock="),
    (urllib.request.OpenerDirector, "open", "opener="),
)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "real_process: needs a real subprocess or socket - exempt from the "
        "conftest seam guard, but still environment-relocated.",
    )


def _relocation_root(basetemp, node_id: str):
    """A unique directory per test that is deliberately never created.

    The obvious spelling is `tmp_path`, and it was measurably wrong: requesting
    it from an autouse fixture forces a mkdir for *every* test, including the
    hundreds that never touch a filesystem, which cost ~5% of the whole run.
    Nothing here needs the directory to exist - the package creates what it
    writes, and a reader of a missing file correctly sees "no history".

    Readable stem so a stray file can be traced back to its test, plus a digest
    because pytest truncates long names and two truncated node ids that collided
    would quietly share one ledger.
    """
    stem = re.sub(r"[^0-9A-Za-z]+", "-", node_id).strip("-")[-48:]
    return basetemp / f"{stem}-{hashlib.blake2s(node_id.encode(), digest_size=4).hexdigest()}"


def _blocked(owner, attribute: str, seam: str, node_id: str):
    name = getattr(owner, "__name__", owner.__class__.__name__)

    def refuse(*args, **kwargs):
        raise AssertionError(
            f"{name}.{attribute} is blocked in tests, and {node_id} called it. "
            f"Inject the `{seam}` seam instead of touching the operator's "
            "machine. If this test genuinely needs a real one, mark it "
            "`@pytest.mark.real_process`."
        )

    return refuse


@pytest.fixture(autouse=True)
def hermetic(request, monkeypatch, tmp_path_factory):
    """Relocate the package's environment and block every off-process call."""
    root = _relocation_root(tmp_path_factory.getbasetemp(), request.node.nodeid)
    # Materialised first: deleting from `os.environ` while iterating it would
    # skip entries.
    for name in [n for n in os.environ if n.startswith(PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    for name, relative in RELOCATED.items():
        monkeypatch.setenv(name, str(root / relative))

    if request.node.get_closest_marker("real_process") is not None:
        return

    for owner, attribute, seam in SEAMS:
        monkeypatch.setattr(owner, attribute, _blocked(owner, attribute, seam, request.node.nodeid))
