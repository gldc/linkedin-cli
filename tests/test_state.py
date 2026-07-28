"""Cross-process pacing and the write ledger.

The point of `state.py` is that it survives process exit, so most of these tests
build a *second* `State` over the same path - that is what a second CLI
invocation looks like - and one of them spawns real subprocesses, because flock
semantics differ between "same process, two file descriptors" and "two
processes" and only the latter is the case we ship.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from linkedin_cli import state
from linkedin_cli.state import Blocked, State, Throttled, WriteQuotaExceeded

DAY = 86400
WEEK = 7 * DAY

REPO_ROOT = str(Path(state.__file__).resolve().parent.parent)


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(state.time, "time", c.time)
    monkeypatch.setattr(state.time, "sleep", c.sleep)
    return c


@pytest.fixture
def path(tmp_path):
    return tmp_path / "nested" / "state.json"


@pytest.fixture(autouse=True)
def no_env(monkeypatch, tmp_path):
    """Point the state file at `tmp_path`, so a test that forgets a path writes
    there instead of into the operator's real `~/.local/state`."""
    monkeypatch.setenv("LINKEDIN_STATE_FILE", str(tmp_path / "env" / "state.json"))


# --------------------------------------------------------------------------- layout


def test_default_path_is_under_local_state():
    assert state.DEFAULT_PATH == Path.home() / ".local/state/linkedin-cli/state.json"


def test_resolve_path_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("LINKEDIN_STATE_FILE")
    assert state.resolve_path() == state.DEFAULT_PATH


def test_resolve_path_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_STATE_FILE", str(tmp_path / "broker.json"))
    assert state.resolve_path() == tmp_path / "broker.json"


def test_resolve_path_expands_a_tilde(monkeypatch):
    monkeypatch.setenv("LINKEDIN_STATE_FILE", "~/relocated.json")
    assert state.resolve_path() == Path.home() / "relocated.json"


def test_resolve_path_ignores_a_blank_env_override(monkeypatch):
    monkeypatch.setenv("LINKEDIN_STATE_FILE", "   ")
    assert state.resolve_path() == state.DEFAULT_PATH


def test_an_explicit_path_beats_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_STATE_FILE", str(tmp_path / "broker.json"))
    assert State(tmp_path / "explicit.json").path == tmp_path / "explicit.json"


def test_state_without_a_path_follows_the_env_override(tmp_path, monkeypatch):
    """The broker sets the variable per invocation, so it is read per call."""
    relocated = tmp_path / "the credential broker" / "state.json"
    monkeypatch.setenv("LINKEDIN_STATE_FILE", str(relocated))

    State().record_write("invite")

    assert relocated.exists()
    assert State().write_count("invite", DAY) == 1


def test_parent_directories_are_created(path):
    State(path).record_write("invite")
    assert path.exists()


def test_state_file_is_0600(path):
    State(path).record_write("invite")
    assert path.stat().st_mode & 0o777 == 0o600


def test_state_directory_is_0700(path):
    State(path).record_write("invite")
    assert path.parent.stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------------------------- pacing


def test_first_call_does_not_sleep(path, clock):
    assert State(path).wait_for_slot(60.0) == 0.0


def test_pacing_is_measured_from_the_persisted_timestamp(path, clock):
    """The whole reason this module exists: a fresh process must still wait."""
    State(path).wait_for_slot(10.0)
    clock.now += 4.0

    slept = State(path).wait_for_slot(10.0)

    assert slept == pytest.approx(6.0)


def test_pacing_across_two_real_processes_actually_sleeps(path):
    """Same claim as above, but with the real clock rather than a fake one."""
    State(path).wait_for_slot(0.2)
    started = time.monotonic()
    slept = State(path).wait_for_slot(0.2)
    elapsed = time.monotonic() - started

    assert 0.0 < slept <= 0.2
    assert elapsed >= slept * 0.9


def test_no_wait_once_the_interval_has_already_elapsed(path, clock):
    State(path).wait_for_slot(10.0)
    clock.now += 30.0
    assert State(path).wait_for_slot(10.0) == 0.0


def test_zero_interval_never_sleeps(path, clock):
    s = State(path)
    s.wait_for_slot(0.0)
    assert s.wait_for_slot(0.0) == 0.0


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_a_non_positive_interval_still_records_the_timestamp(path, interval, clock):
    """The `--rate=0` bug, one layer down. Skipping the write as well as the
    sleep leaves the *next* invocation with no `last_request` to pace against,
    so it runs unpaced too - and reaching it needs no flag at all, just a caller
    that hands this a zero. The ledger clamps instead of trusting the caller.
    """
    State(path).wait_for_slot(interval)
    assert json.loads(path.read_text())["last_request"] == clock.now

    clock.now += 1.0
    assert State(path).wait_for_slot(10.0) == pytest.approx(9.0)


def test_backwards_clock_jump_cannot_park_the_cli(path, clock):
    """An NTP step backwards must never produce a wait longer than the interval."""
    State(path).wait_for_slot(5.0)
    clock.now -= 3600.0
    assert State(path).wait_for_slot(5.0) <= 5.0


# --------------------------------------------------------------------------- ledger


def test_record_write_survives_the_process(path, clock):
    State(path).record_write("message")
    assert State(path).write_count("message", DAY) == 1


def test_write_count_is_per_kind(path, clock):
    s = State(path)
    s.record_write("invite")
    s.record_write("invite")
    s.record_write("post")
    assert s.write_count("invite", DAY) == 2
    assert s.write_count("post", DAY) == 1
    assert s.write_count("comment", DAY) == 0


def test_write_count_excludes_entries_outside_the_window(path, clock):
    s = State(path)
    s.record_write("invite")
    clock.now += 3700
    s.record_write("invite")
    assert s.write_count("invite", 3600) == 1
    assert s.write_count("invite", DAY) == 2


# --------------------------------------------------------------------------- quotas


def test_daily_cap_boundary_is_exclusive(path, clock, monkeypatch):
    """At the cap the next write is refused; one below it is allowed."""
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 3)
    s = State(path)
    for _ in range(2):
        s.record_write("invite")
    s.check_write_allowed("invite")  # 2 of 3 used

    s.record_write("invite")
    with pytest.raises(WriteQuotaExceeded) as exc:
        s.check_write_allowed("invite")
    assert exc.value.exit_code == 5
    assert "invite" in str(exc.value)


def test_quota_is_enforced_against_the_persisted_ledger(path, clock, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 1)
    State(path).record_write("invite")
    with pytest.raises(WriteQuotaExceeded):
        State(path).check_write_allowed("invite")


def test_daily_quota_frees_up_once_the_window_rolls(path, clock, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 1)
    s = State(path)
    s.record_write("invite")
    clock.now += DAY + 1
    s.check_write_allowed("invite")


def test_weekly_cap_is_five_times_the_daily_cap(path, clock, monkeypatch):
    """Five days at the daily cap must not add up to a sixth."""
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 2)
    s = State(path)
    for _ in range(5):
        for _ in range(2):
            s.record_write("invite")
        clock.now += DAY + 1

    # The daily window is empty again, but the 7-day total is 10 = 5 x 2.
    assert s.write_count("invite", DAY) == 0
    with pytest.raises(WriteQuotaExceeded) as exc:
        s.check_write_allowed("invite")
    assert "7" in str(exc.value) or "week" in str(exc.value).lower()


def test_shipped_caps_match_the_design(path):
    assert state.DAILY_CAPS == {
        "invite": 15,
        "message": 40,
        "post": 10,
        "comment": 40,
        "react": 100,
    }


def test_unknown_kind_falls_back_to_the_most_conservative_cap(path, clock, monkeypatch):
    """A typo'd kind must not become an unlimited write channel."""
    monkeypatch.setattr(state, "DEFAULT_DAILY_CAP", 2)
    s = State(path)
    s.record_write("mystery")
    s.check_write_allowed("mystery")
    s.record_write("mystery")
    with pytest.raises(WriteQuotaExceeded):
        s.check_write_allowed("mystery")


# --------------------------------------------------------------------------- pruning


def test_timestamps_older_than_the_longest_window_are_pruned(path, clock):
    s = State(path)
    s.record_write("react")
    clock.now += WEEK + 60
    s.record_write("react")

    stored = json.loads(path.read_text())
    assert len(stored["writes"]["react"]) == 1


def test_pruning_does_not_disturb_live_entries(path, clock):
    s = State(path)
    s.record_write("react")
    clock.now += WEEK - 60
    s.record_write("react")
    assert s.write_count("react", WEEK) == 2


# --------------------------------------------------------------------------- breaker


def test_breaker_is_closed_by_default(path):
    assert State(path).breaker_state() is None


def test_tripped_breaker_survives_the_process(path, clock):
    State(path).trip_breaker("HTTP 999")

    got = State(path).breaker_state()
    assert got["reason"] == "HTTP 999"
    assert got["at"] == pytest.approx(clock.now)


def test_breaker_keeps_the_original_cause(path, clock):
    """The first trip is the diagnosis; later ones are just consequences."""
    s = State(path)
    s.trip_breaker("HTTP 999")
    clock.now += 60
    s.trip_breaker("challenge redirect")
    assert s.breaker_state()["reason"] == "HTTP 999"


def test_clear_breaker_reopens_it(path, clock):
    State(path).trip_breaker("HTTP 999")
    State(path).clear_breaker()
    assert State(path).breaker_state() is None


def test_clear_breaker_on_a_closed_breaker_is_a_no_op(path):
    State(path).clear_breaker()
    assert State(path).breaker_state() is None


def test_writes_are_refused_while_the_breaker_is_open(path, clock):
    s = State(path)
    s.trip_breaker("HTTP 999")
    with pytest.raises(Blocked) as exc:
        s.check_write_allowed("message")
    assert exc.value.exit_code == 9
    assert "HTTP 999" in str(exc.value)


def test_the_ledger_still_works_while_blocked(path, clock):
    """Tripping the breaker must not lose the ledger it shares a file with."""
    s = State(path)
    s.record_write("message")
    s.trip_breaker("HTTP 999")
    assert s.write_count("message", DAY) == 1


# ------------------------------------------------------------- the dead-session run
#
# The browser pivot deleted the re-acquire loop, so a `session_expired` that
# reaches the operator means the managed profile is signed out - and the remedy
# is `auth seed`, which an agent cannot run for itself. Counting them here is
# what stops an agent that treats exit 3 as retryable from hammering a session
# LinkedIn has already ended.


def test_a_single_dead_session_does_not_arm_the_breaker(path, clock):
    """One is ordinary: cookies rotate, and the next call usually works."""
    assert State(path).count_session_failure() == 1
    assert State(path).breaker_state() is None


def test_the_count_survives_the_process(path, clock):
    """Each invocation is a fresh process, so a per-process counter would count
    to one forever and the breaker would never arm at all."""
    assert State(path).count_session_failure() == 1
    assert State(path).count_session_failure() == 2
    assert State(path).breaker_state() is None
    assert State(path).count_session_failure() == 3
    assert State(path).breaker_state() is not None


def test_the_armed_breaker_names_the_signed_out_profile(path, clock):
    """`auth seed` is the only remedy and only the operator can run it, so the
    reason has to say which thing is broken rather than just "blocked"."""
    for _ in range(3):
        State(path).count_session_failure()
    reason = State(path).breaker_state()["reason"]
    assert "signed out" in reason
    assert "3 dead-session responses" in reason
    assert "60 minutes" in reason


def test_failures_older_than_the_window_do_not_count(path, clock):
    """Three failures spread over a fortnight are a rotating cookie, not a
    signed-out profile. Without the window the counter only ever accumulates, so
    an account working normally for a month still trips the breaker eventually -
    and the operator is sent to re-seed a profile that was signed in all along.
    """
    s = State(path)
    assert s.count_session_failure(window=3600) == 1
    clock.now += 3601
    assert s.count_session_failure(window=3600) == 1
    clock.now += 3601
    assert s.count_session_failure(window=3600) == 1
    assert s.breaker_state() is None


def test_failures_just_inside_the_window_still_add_up(path, clock):
    """The other half of the same claim: expiry must not be so eager that a run
    of failures within the hour is forgotten between two of them."""
    s = State(path)
    s.count_session_failure(window=3600)
    clock.now += 1800
    s.count_session_failure(window=3600)
    clock.now += 1799
    assert s.count_session_failure(window=3600) == 3
    assert s.breaker_state() is not None


def test_a_success_clears_the_run(path, clock):
    for _ in range(2):
        State(path).count_session_failure()
    State(path).clear_session_failures()
    assert State(path).count_session_failure() == 1
    assert State(path).breaker_state() is None


def test_clearing_the_run_leaves_the_rest_of_the_ledger_alone(path, clock):
    """It shares one file with the pacing clock and the write counters."""
    s = State(path)
    s.record_write("message")
    s.wait_for_slot(10.0)
    s.count_session_failure()
    s.clear_session_failures()
    assert s.write_count("message", DAY) == 1
    assert json.loads(path.read_text())["last_request"] == clock.now


def test_clearing_a_run_that_never_started_is_a_no_op(path, clock):
    State(path).clear_session_failures()
    assert State(path).count_session_failure() == 1


def test_an_already_open_breaker_keeps_its_original_cause(path, clock):
    """A 999 is a harder fact than a run of dead sessions, and it is the one the
    operator needs to see: over-writing it with the consequence would send them
    to `auth seed` for a block that re-seeding cannot lift."""
    s = State(path)
    s.trip_breaker("HTTP 999")
    for _ in range(4):
        s.count_session_failure()
    assert s.breaker_state()["reason"] == "HTTP 999"


def test_garbage_in_the_failure_list_is_ignored(path, clock):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_failures": ["ages ago", None, True, {}]}))
    assert State(path).count_session_failure() == 1
    assert State(path).breaker_state() is None


# --------------------------------------------------------------------------- corruption


@pytest.mark.parametrize("junk", [b"", b"   ", b"{not json", b"[]", b'"scalar"', b"\x00\xff"])
def test_corrupt_state_degrades_to_empty_rather_than_crashing(path, clock, junk):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(junk)
    s = State(path)

    assert s.write_count("invite", DAY) == 0
    assert s.breaker_state() is None
    assert s.wait_for_slot(10.0) == 0.0


def test_corrupt_state_is_rewritten_by_the_unguarded_recorder(path, clock):
    """`record_write` books a row and checks nothing - no caller in the CLI uses
    it - so it still repairs the file. What it must not do is repair away the
    *fact* that a ledger was lost: the claim path reads the replacement and is
    still refused by it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")
    s = State(path)
    s.record_write("invite")

    assert json.loads(path.read_text())["writes"]["invite"]
    with pytest.raises(Blocked):
        s.claim_write("invite")


def test_garbage_timestamps_are_ignored(path, clock):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_request": "soon", "writes": {"invite": ["yesterday", None]}}))
    s = State(path)
    assert s.write_count("invite", DAY) == 0
    assert s.wait_for_slot(10.0) == 0.0


# --------------------------------------------------------------------------- claims
#
# The cap check and the ledger append used to be two separate flocks, so two
# invocations could both read "one below the cap" and both then append. A claim
# is the same two steps under one lock, and the row lands *before* the write is
# attempted: a process killed mid-write leaves the attempt counted, which is the
# only direction that is safe when LinkedIn may already have applied it.


def test_a_claim_records_its_row_before_the_write_is_attempted(path, clock):
    State(path).claim_write("invite")
    assert State(path).write_count("invite", DAY) == 1


def test_a_released_claim_gives_the_slot_back(path, clock, monkeypatch):
    """Nothing was sent, so nothing should be charged - the local-rejection case."""
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 1)
    s = State(path)
    s.claim_write("invite").release()

    assert s.write_count("invite", DAY) == 0
    s.claim_write("invite")


def test_a_committed_claim_cannot_be_released(path, clock):
    """Once the request is on the wire the row is a fact, whatever comes back."""
    s = State(path)
    claim = s.claim_write("invite")
    claim.commit()
    claim.release()
    assert s.write_count("invite", DAY) == 1


def test_a_release_removes_only_its_own_row(path, clock):
    s = State(path)
    first = s.claim_write("invite")
    s.claim_write("invite")
    first.release()
    assert s.write_count("invite", DAY) == 1


def test_releasing_twice_does_not_refund_a_second_row(path, clock):
    """Two concurrent claims can share a timestamp, so a double release that
    removed by value again would refund the *other* process's write."""
    s = State(path)
    first = s.claim_write("invite")
    s.claim_write("invite")
    first.release()
    first.release()
    assert s.write_count("invite", DAY) == 1


def test_releasing_a_row_that_is_already_gone_is_a_no_op(path, clock):
    s = State(path)
    claim = s.claim_write("invite")
    path.write_text(json.dumps({}))
    claim.release()
    assert s.write_count("invite", DAY) == 0


def test_a_claim_at_the_daily_cap_is_refused_and_records_nothing(path, clock, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 2)
    s = State(path)
    s.claim_write("invite")
    s.claim_write("invite")

    with pytest.raises(WriteQuotaExceeded) as exc:
        s.claim_write("invite")
    assert exc.value.exit_code == 5
    assert "invite" in str(exc.value)
    assert s.write_count("invite", DAY) == 2


def test_a_claim_at_the_weekly_cap_is_refused(path, clock, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 2)
    s = State(path)
    for _ in range(5):
        for _ in range(2):
            s.claim_write("invite")
        clock.now += DAY + 1

    with pytest.raises(WriteQuotaExceeded) as exc:
        s.claim_write("invite")
    assert "7" in str(exc.value) or "week" in str(exc.value).lower()


def test_a_claim_is_refused_while_the_breaker_is_open(path, clock):
    s = State(path)
    s.trip_breaker("HTTP 999")

    with pytest.raises(Blocked) as exc:
        s.claim_write("invite")
    assert exc.value.exit_code == 9
    assert "HTTP 999" in str(exc.value)
    assert s.write_count("invite", DAY) == 0


def test_an_unknown_kind_claims_against_the_conservative_cap(path, clock, monkeypatch):
    monkeypatch.setattr(state, "DEFAULT_DAILY_CAP", 1)
    s = State(path)
    s.claim_write("mystery")
    with pytest.raises(WriteQuotaExceeded):
        s.claim_write("mystery")


# ------------------------------------------------------------------------- cleanup
#
# `post delete`, `comment delete` and `invite withdraw` undo a write that is
# already visible to real people. Putting them through the caps means a run that
# trips one mid-way cannot clean up after itself, and strands a live public post
# or a live connection request - so a cleanup claim is recorded and paced like
# any other write, but is never refused.


def test_a_cleanup_write_is_allowed_at_the_cap(path, clock, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    s = State(path)
    s.claim_write("post")
    with pytest.raises(WriteQuotaExceeded):
        s.claim_write("post")

    s.claim_write("post", cleanup=True)


def test_a_cleanup_write_is_allowed_while_the_breaker_is_open(path, clock):
    """The breaker exists to stop this client digging in deeper; withdrawing an
    invitation it already sent is the opposite of digging in deeper."""
    s = State(path)
    s.trip_breaker("HTTP 999")
    s.claim_write("invite", cleanup=True)


def test_a_cleanup_write_does_not_spend_the_create_budget(path, clock, monkeypatch):
    """Otherwise every create-then-undo pair costs two, and cleaning up an
    over-run pushes the daily window further out than the run itself did."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    s = State(path)
    s.claim_write("post", cleanup=True)
    s.claim_write("post", cleanup=True)

    s.claim_write("post")
    s.claim_write("post")
    with pytest.raises(WriteQuotaExceeded):
        s.claim_write("post")


def test_cleanup_writes_are_recorded_under_their_own_kind(path, clock):
    """Exempt from the caps is not exempt from the ledger: `doctor` has to be
    able to show every request this client sent."""
    s = State(path)
    s.claim_write("post", cleanup=True)

    assert s.write_count(state.cleanup_kind("post"), DAY) == 1
    assert s.write_count("post", DAY) == 0
    assert json.loads(path.read_text())["writes"][state.cleanup_kind("post")]


def test_a_cleanup_claim_can_be_released(path, clock):
    s = State(path)
    s.claim_write("post", cleanup=True).release()
    assert s.write_count(state.cleanup_kind("post"), DAY) == 0


def test_a_cleanup_claim_knows_what_it_is(path, clock):
    assert State(path).claim_write("post", cleanup=True).cleanup is True
    assert State(path).claim_write("post").cleanup is False


# ------------------------------------------------------------------ the ceiling
#
# The exemption above used to be the whole story: `cleanup=True` skipped
# `_guard_write` outright, and that function is the only place the caps, the
# cooldown *and* the breaker are enforced - so `unreact` in a retry loop was an
# unmetered write channel with no bound of any kind, forever. The exemption is
# still right and stays: an undo refused by a spent cap strands a live write in
# front of real people. Being unbounded was never part of it.


def test_an_undo_loop_is_refused_once_it_is_plainly_a_loop(path, clock, monkeypatch):
    """The bound is arithmetic, not judgement: nothing here can tell a tidy-up
    from a wedged agent, so the only honest line is one no cleanup a person
    would perform can reach."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    s = State(path)
    for _ in range(5):  # 1 x CLEANUP_CEILING_MULTIPLIER
        s.claim_write("post", cleanup=True)

    with pytest.raises(WriteQuotaExceeded) as exc:
        s.claim_write("post", cleanup=True)
    assert exc.value.exit_code == 5
    assert "post" in str(exc.value)
    assert s.write_count(state.cleanup_kind("post"), DAY) == 5, "the refusal booked a row"


def test_the_ceiling_is_a_multiple_of_the_cap_it_bypasses(path):
    """A runaway-loop stop, not a second budget. Undoing everything a full week
    at the cap could have created has to fit under it, or the ceiling quietly
    becomes the cap the exemption exists to get past."""
    for kind, cap in state.DAILY_CAPS.items():
        assert state.cleanup_ceiling(kind) == cap * state.CLEANUP_CEILING_MULTIPLIER
    assert state.CLEANUP_CEILING_MULTIPLIER >= state.WEEKLY_MULTIPLIER
    assert (
        state.cleanup_ceiling("mystery")
        == state.DEFAULT_DAILY_CAP * state.CLEANUP_CEILING_MULTIPLIER
    )


def test_an_undo_of_something_created_long_ago_is_still_allowed(path, clock):
    """The bound cannot be "as many undos as there were writes today". An
    invitation sent on Monday is withdrawn on Thursday and `unreact` on a
    reaction left last month is an ordinary thing to want; a ceiling derived
    from the day's own writes refuses both. This one passes before the ceiling
    exists and has to go on passing after it - that is what it is for.
    """
    s = State(path)
    s.claim_write("invite")
    clock.now += WEEK + DAY  # every trace of the create has aged out

    s.claim_write("invite", cleanup=True)
    assert s.write_count(state.cleanup_kind("invite"), DAY) == 1


def test_the_ceiling_frees_up_as_its_window_rolls(path, clock, monkeypatch):
    """Rolling, like the caps beside it: a loop that ran yesterday must not be
    holding the undo of something published today."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    s = State(path)
    for _ in range(5):
        s.claim_write("post", cleanup=True)
    with pytest.raises(WriteQuotaExceeded):
        s.claim_write("post", cleanup=True)

    clock.now += DAY + 1
    s.claim_write("post", cleanup=True)


def test_the_ceiling_is_per_kind(path, clock, monkeypatch):
    """An undo loop on one verb must not strand the undo of another."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    s = State(path)
    for _ in range(5):
        s.claim_write("post", cleanup=True)

    s.claim_write("react", cleanup=True)


def test_an_open_breaker_tightens_the_ceiling_without_closing_the_channel(path, clock, monkeypatch):
    """Hammering an undo through an open breaker is how a soft block becomes a
    restriction. Refusing it outright is how a run blocked mid-way leaves its
    post live, so the channel stays open - but only up to what this client could
    itself have created in a day, which is the most a blocked run can be
    holding. More than that is not a cleanup, it is a loop against a client
    LinkedIn is currently refusing.
    """
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    s = State(path)
    s.trip_breaker("HTTP 999")

    s.claim_write("post", cleanup=True)
    s.claim_write("post", cleanup=True)
    with pytest.raises(WriteQuotaExceeded) as exc:
        s.claim_write("post", cleanup=True)
    assert "clear-breaker" in str(exc.value)

    # The control: the same third undo goes through once the breaker is closed,
    # so what refused it was the breaker and not the ordinary ceiling.
    s.clear_breaker()
    s.claim_write("post", cleanup=True)


def test_an_open_breaker_does_not_strand_the_undo_of_something_created_today(
    path, clock, monkeypatch
):
    """The tightened ceiling reintroduced the failure it argues against.

    Its case is about *creates* - this client cannot have made more than a day's
    cap, so it cannot need more undos than that to take back what it made before
    the block - but it counted *undos in the last 24h*, which the design beside
    it says may every one of them be undoing something published days ago. So an
    operator who cleared out old posts this morning, well inside the relaxed
    ceiling, could not delete the post this run had just published. A live public
    post the CLI refuses to take back is the exact outcome the cleanup exemption
    exists to prevent.
    """
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    s = State(path)
    for _ in range(2):  # old posts, deleted while nothing was wrong
        s.claim_write("post", cleanup=True)
    s.claim_write("post")  # what this run published
    clock.now += 60
    s.trip_breaker("HTTP 999")

    s.claim_write("post", cleanup=True)  # and now takes back
    assert s.write_count(state.cleanup_kind("post"), DAY) == 3


def test_the_tightened_ceiling_still_stops_a_loop_that_started_before_the_block(
    path, clock, monkeypatch
):
    """The other side of the same line. Undos from before the block are not
    counted, but that must not buy a wedged loop a fresh unmetered budget: the
    tightened ceiling is a full day's worth of creates *pushed through the block*
    and stops there."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    s = State(path)
    for _ in range(2):
        s.claim_write("post", cleanup=True)
    clock.now += 60
    s.trip_breaker("HTTP 999")

    s.claim_write("post", cleanup=True)
    s.claim_write("post", cleanup=True)
    with pytest.raises(WriteQuotaExceeded) as exc:
        s.claim_write("post", cleanup=True)
    assert "clear-breaker" in str(exc.value)


def test_a_breaker_with_no_timestamp_falls_back_to_the_whole_window(path, clock, monkeypatch):
    """A hand-edited or clock-stepped `at` cannot be trusted to say when the
    block started, and the safe reading of "unknown" is the widest window - the
    behaviour before this counted from the block at all."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    s = State(path)
    for _ in range(2):
        s.claim_write("post", cleanup=True)
    seed_breaker(path, {"reason": "HTTP 999"})

    with pytest.raises(WriteQuotaExceeded):
        s.claim_write("post", cleanup=True)


def seed_breaker(path: Path, breaker: dict) -> None:
    """Write a breaker entry straight into the file, `at` and all."""
    data = json.loads(path.read_text())
    data["breaker"] = breaker
    path.write_text(json.dumps(data))


# ------------------------------------------------------------------------ throttle
#
# A 429 or 503 is the loudest warning LinkedIn gives before it starts
# restricting accounts, and it used to leave nothing behind: the next
# invocation - a fresh process - had no idea it had just been told to slow down.


def test_there_is_no_throttle_by_default(path):
    assert State(path).throttle_state() is None


def test_a_throttle_survives_the_process(path, clock):
    State(path).record_throttle("HTTP 429")

    got = State(path).throttle_state()
    assert got["reason"] == "HTTP 429"
    assert got["at"] == pytest.approx(clock.now)
    assert got["until"] == pytest.approx(clock.now + state.THROTTLE_COOLDOWN)


def test_a_throttle_refuses_the_next_write(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")

    with pytest.raises(Throttled) as exc:
        s.claim_write("comment")
    assert isinstance(exc.value, WriteQuotaExceeded)  # exit 5, without touching cli.py
    assert exc.value.exit_code == 5
    assert "429" in str(exc.value)
    assert s.write_count("comment", DAY) == 0


def test_check_write_allowed_honours_the_throttle_too(path, clock):
    """The advisory check and the atomic claim must not disagree about it."""
    s = State(path)
    s.record_throttle("HTTP 503")
    with pytest.raises(Throttled):
        s.check_write_allowed("comment")


def test_the_throttle_lifts_once_its_cooldown_passes(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    clock.now += state.THROTTLE_COOLDOWN + 1

    assert s.throttle_state() is None
    s.claim_write("comment")


def test_a_longer_retry_after_wins(path, clock):
    s = State(path)
    s.record_throttle("HTTP 503", retry_after=state.THROTTLE_COOLDOWN * 3)
    clock.now += state.THROTTLE_COOLDOWN + 1
    assert s.throttle_state() is not None


def test_a_shorter_retry_after_cannot_shorten_the_cooldown(path, clock):
    """`Retry-After` is what LinkedIn will accept, not what is safe here: the
    transport already waited it out and got throttled anyway."""
    s = State(path)
    s.record_throttle("HTTP 429", retry_after=1)
    clock.now += 2
    assert s.throttle_state() is not None


def test_a_repeat_throttle_backs_off_further(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    clock.now += state.THROTTLE_COOLDOWN + 1
    s.record_throttle("HTTP 429")

    assert s.throttle_state()["count"] == 2
    clock.now += state.THROTTLE_COOLDOWN + 1
    assert s.throttle_state() is not None


def test_the_backoff_stops_growing_at_the_ceiling(path, clock):
    """An unbounded doubling would park the CLI for days on a bad afternoon."""
    s = State(path)
    for _ in range(12):
        s.record_throttle("HTTP 429")
    assert s.throttle_state()["until"] - clock.now <= state.THROTTLE_MAX_COOLDOWN


def test_an_old_throttle_starts_the_backoff_over(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    clock.now += state.THROTTLE_MEMORY + 1
    s.record_throttle("HTTP 429")
    assert s.throttle_state()["count"] == 1


def test_a_cleanup_write_is_allowed_while_throttled(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    s.claim_write("post", cleanup=True)


def test_a_throttle_slows_the_pacer(path, clock):
    """Damping the writes it refuses is not enough: the reads that are still
    allowed are part of the velocity LinkedIn just complained about."""
    s = State(path)
    s.wait_for_slot(10.0)
    s.record_throttle("HTTP 429")
    clock.now += 10.0

    slept = s.wait_for_slot(10.0)
    assert slept == pytest.approx(10.0 * (state.THROTTLE_PACE_MULTIPLIER - 1))


def test_clearing_the_throttle_releases_the_writes(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    s.clear_throttle()

    assert s.throttle_state() is None
    s.claim_write("comment")


def test_clearing_a_throttle_that_never_happened_is_a_no_op(path, clock):
    State(path).clear_throttle()
    assert State(path).throttle_state() is None


def test_a_throttle_leaves_the_rest_of_the_ledger_alone(path, clock):
    s = State(path)
    s.record_write("message")
    s.wait_for_slot(10.0)
    s.record_throttle("HTTP 429")

    assert s.write_count("message", DAY) == 1
    assert json.loads(path.read_text())["last_request"] == clock.now


@pytest.mark.parametrize("junk", ["soon", None, [], {}, {"until": "later"}])
def test_a_malformed_throttle_row_is_ignored(path, clock, junk):
    """Same policy as the rest of the file: unreadable state means no history,
    because a ledger that cannot be parsed must not become a permanent block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"throttle": junk}))
    s = State(path)

    assert s.throttle_state() is None
    s.claim_write("comment")


def test_a_forgotten_throttle_is_pruned_from_the_file(path, clock):
    s = State(path)
    s.record_throttle("HTTP 429")
    clock.now += state.THROTTLE_MEMORY + 1
    s.record_write("react")

    assert "throttle" not in json.loads(path.read_text())


# --------------------------------------------------------------------------- concurrency

_CLAIM_WORKER = """
import sys
import time
from pathlib import Path
from linkedin_cli import state

path, attempts, cap, go, ready = sys.argv[1:6]
state.DAILY_CAPS["invite"] = int(cap)
s = state.State(Path(path))

# Staggered starts would let each process finish before the next one opened the
# file, and the defect only shows up when they are inside the claim together.
Path(ready).write_text("here")
while not Path(go).exists():
    time.sleep(0.001)

granted = 0
for _ in range(int(attempts)):
    try:
        s.claim_write("invite")
    except state.WriteQuotaExceeded:
        continue
    granted += 1
sys.stdout.write(str(granted))
"""


def _race(tmp_path, source: str, *, workers: int, attempts: int, cap: int):
    """Run `workers` copies of `source` and release them all at one instant."""
    script = tmp_path / "worker.py"
    script.write_text(source)
    path = tmp_path / "state.json"
    ready = tmp_path / "ready"
    ready.mkdir()
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(path),
                str(attempts),
                str(cap),
                str(go),
                str(ready / str(i)),
            ],
            env=env,
            stdout=subprocess.PIPE,
        )
        for i in range(workers)
    ]
    deadline = time.monotonic() + 60
    while len(list(ready.iterdir())) < workers:
        assert time.monotonic() < deadline, "the workers never reached the barrier"
        time.sleep(0.005)
    go.write_text("go")

    granted = 0
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        granted += int(out)
    return path, granted


@pytest.mark.real_process
def test_concurrent_claims_cannot_overshoot_the_cap(tmp_path):
    """Eight processes released onto five slots at the same instant.

    Mocking the lock would prove nothing here: what this pins is that the check
    and the append happen under *one* flock, and a fake that serialises whole
    calls hides exactly that. Under two locks all eight pass the check on the
    first round and all eight then write.
    """
    path, granted = _race(tmp_path, _CLAIM_WORKER, workers=8, attempts=10, cap=5)

    assert granted == 5
    assert State(path).write_count("invite", DAY) == 5


@pytest.mark.real_process
def test_concurrent_cleanup_claims_are_not_refused_by_the_cap(tmp_path):
    """The same race, for the writes that undo something already published.

    The cap is 5 and 20 undos get through it, which is the claim. It used to be
    1: raised only so that the runaway ceiling - 5 x the cap, and the subject of
    the next test - is not what the four processes collide with here.
    """
    source = _CLAIM_WORKER.replace('claim_write("invite")', 'claim_write("invite", cleanup=True)')
    path, granted = _race(tmp_path, source, workers=4, attempts=5, cap=5)

    assert granted == 20
    assert State(path).write_count(state.cleanup_kind("invite"), DAY) == 20
    assert State(path).write_count("invite", DAY) == 0


@pytest.mark.real_process
def test_concurrent_cleanup_claims_cannot_overshoot_the_ceiling(tmp_path):
    """And the ceiling is checked and booked under the same single flock the cap
    is. Counted under one lock and appended under another, four looping
    processes overshoot it exactly the way they used to overshoot the cap - and
    the ceiling exists precisely for the case where something is looping.
    """
    source = _CLAIM_WORKER.replace('claim_write("invite")', 'claim_write("invite", cleanup=True)')
    path, granted = _race(tmp_path, source, workers=4, attempts=5, cap=1)

    assert granted == 5  # 1 x CLEANUP_CEILING_MULTIPLIER
    assert State(path).write_count(state.cleanup_kind("invite"), DAY) == 5


_WORKER = """
import sys
from pathlib import Path
from linkedin_cli.state import State

s = State(Path(sys.argv[1]))
for _ in range(int(sys.argv[2])):
    s.record_write("invite")
"""


@pytest.mark.real_process
def test_concurrent_processes_do_not_lose_or_corrupt_writes(tmp_path):
    """Read-modify-write without flock silently drops the losing process's rows."""
    script = tmp_path / "worker.py"
    script.write_text(_WORKER)
    path = tmp_path / "state.json"
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}

    procs = [
        subprocess.Popen([sys.executable, str(script), str(path), "5"], env=env) for _ in range(6)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    assert State(path).write_count("invite", DAY) == 30


@pytest.mark.real_process
def test_concurrent_pacing_serializes(tmp_path):
    """Two processes must not both pace off the same stale timestamp."""
    script = tmp_path / "pace.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from linkedin_cli.state import State\n"
        "print(State(Path(sys.argv[1])).wait_for_slot(0.3))\n"
    )
    path = tmp_path / "state.json"
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}

    State(path).wait_for_slot(0.3)
    started = time.monotonic()
    procs = [subprocess.Popen([sys.executable, str(script), str(path)], env=env) for _ in range(2)]
    for p in procs:
        assert p.wait(timeout=60) == 0
    elapsed = time.monotonic() - started

    # Both had to wait, and the second behind the first: ~0.6s of pacing total.
    assert elapsed >= 0.55


# ------------------------------------------------------------------ the report

# The real ledger read off the container's state.json after the a live run live
# verification run. Used as the fixture so the report is checked against a shape
# that actually occurred rather than one invented to suit the reader.
LIVE_LEDGER = {
    "react": 1,
    "react.cleanup": 1,
    "post": 3,
    "post.cleanup": 1,
    "message": 1,
    "invite": 1,
}


def seed(path: Path, counts: dict, at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"writes": {k: [at] * n for k, n in counts.items()}}))


def test_the_ledger_report_covers_every_capped_kind_even_at_zero(path, clock):
    """A kind missing from the report reads as a kind with no limit. Every cap
    this CLI enforces has to appear whether or not it has been spent."""
    report = State(path).ledger_state()
    assert set(report["kinds"]) == set(state.DAILY_CAPS)
    for kind, cap in state.DAILY_CAPS.items():
        assert report["kinds"][kind]["used"] == 0
        assert report["kinds"][kind]["cap"] == cap
        assert report["kinds"][kind]["remaining"] == cap


def test_the_ledger_report_counts_what_the_day_actually_holds(path, clock):
    seed(path, LIVE_LEDGER, clock.now)
    kinds = State(path).ledger_state()["kinds"]
    assert kinds["post"]["used"] == 3
    assert kinds["react"]["used"] == 1
    assert kinds["message"]["used"] == 1
    assert kinds["invite"]["used"] == 1
    assert kinds["comment"]["used"] == 0


def test_an_undo_is_reported_apart_from_the_writes_the_cap_bounds(path, clock):
    """The whole point of `post.cleanup`: it is recorded and never refusable, so
    a reader must be able to tell it from cap consumption. Folded into `used` it
    would read as a spent budget that no cap will actually enforce."""
    seed(path, LIVE_LEDGER, clock.now)
    post = State(path).ledger_state()["kinds"]["post"]
    assert post["used"] == 3, "an undo was counted as a capped write"
    assert post["cleanup_used"] == 1
    assert post["cleanup_bucket"] == "post.cleanup"
    assert post["cleanup_counts_against_cap"] is False


def test_an_undo_does_not_reduce_what_is_left_of_the_cap(path, clock):
    seed(path, LIVE_LEDGER, clock.now)
    kinds = State(path).ledger_state()["kinds"]
    assert kinds["post"]["remaining"] == state.DAILY_CAPS["post"] - 3
    assert kinds["react"]["remaining"] == state.DAILY_CAPS["react"] - 1


def test_the_report_carries_the_seven_day_window_too(path, clock):
    """A write can be refused by the 7-day cap while the daily one still has
    room, so a report showing only the day would say `remaining: 7` about a
    command that is about to exit 5."""
    kinds = State(path).ledger_state()["kinds"]
    assert kinds["post"]["cap_7d"] == state.DAILY_CAPS["post"] * state.WEEKLY_MULTIPLIER
    assert kinds["post"]["remaining_7d"] == kinds["post"]["cap_7d"]


def test_yesterdays_writes_leave_the_day_but_stay_in_the_week(path, clock):
    seed(path, {"post": 2}, clock.now - DAY - 60)
    post = State(path).ledger_state()["kinds"]["post"]
    assert post["used"] == 0
    assert post["remaining"] == state.DAILY_CAPS["post"]
    assert post["used_7d"] == 2


def test_a_kind_nothing_caps_by_name_is_reported_under_the_default(path, clock):
    """`_guard_write` falls back to `DEFAULT_DAILY_CAP` for an unknown kind, so
    the report has to agree with it - a report that omitted the kind entirely
    would hide a limit that is really being enforced."""
    seed(path, {"newverb": 1}, clock.now)
    newverb = State(path).ledger_state()["kinds"]["newverb"]
    assert newverb["cap"] == state.DEFAULT_DAILY_CAP
    assert newverb["used"] == 1


def test_a_cleanup_bucket_is_never_reported_as_a_kind_of_its_own(path, clock):
    """`post.cleanup` is a bucket of `post`, not a sixth verb with its own cap."""
    seed(path, LIVE_LEDGER, clock.now)
    assert not [k for k in State(path).ledger_state()["kinds"] if k.endswith(".cleanup")]


def test_an_over_count_never_reports_a_negative_remainder(path, clock, monkeypatch):
    """A claim released after the process died, or a hand-edited file, can put
    `used` past the cap. `remaining: -3` reads as a number to act on."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 2)
    seed(path, {"post": 5}, clock.now)
    post = State(path).ledger_state()["kinds"]["post"]
    assert post["used"] == 5
    assert post["remaining"] == 0


def test_reading_the_report_does_not_change_the_ledger(path, clock):
    """`doctor` reports; it must not be a way to clear a budget. An agent can
    run it too, and a diagnostic that reset the counters would be the loop the
    counters exist to stop."""
    seed(path, LIVE_LEDGER, clock.now)
    before = path.read_text()
    State(path).ledger_state()
    assert path.read_text() == before
    assert State(path).write_count("post", DAY) == 3


def test_a_ledger_that_was_never_written_reports_honest_zeros(path, clock):
    """A fresh host has no state.json and genuinely has no writes, so the zeros
    are the truth and the report says the counts are good."""
    assert not path.exists()
    report = State(path).ledger_state()
    assert report["readable"] is True
    assert report["kinds"]["post"]["used"] == 0


def test_a_ledger_that_cannot_be_parsed_is_not_reported_as_a_quiet_day(path, clock):
    """`_read` fails open - a corrupt file must never take the CLI down - so an
    unreadable ledger and an empty one both arrive as `{}`. Every *writer* is
    right to treat those the same. A *report* is not: zeros presented as fact
    are how an unreadable ledger reads as "no writes today", which is the one
    reading that would have an operator carry on writing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")

    report = State(path).ledger_state()
    assert report["readable"] is False
    assert report["kinds"] == {}, "unknown counts were reported as zeros"
    assert "unreadable" in report["problem"].lower() or "parse" in report["problem"].lower()


def test_a_ledger_holding_something_other_than_an_object_is_also_unreadable(path, clock):
    """`[]` parses as JSON and is not a ledger. `_read` discards it the same way
    it discards a syntax error, so the report has to as well."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    assert State(path).ledger_state()["readable"] is False


def test_reading_an_unreadable_ledger_still_does_not_raise(path, clock):
    """`doctor` wraps this in `_safe`, but a report that raises loses the
    distinction it was added to draw - `_safe` would turn it into a bare null."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00\x01 not text")
    assert State(path).ledger_state()["readable"] is False


def test_the_report_names_the_ceiling_that_bounds_the_undo_bucket(path, clock):
    """`cleanup_counts_against_cap: false` says "not bounded by the cap" and was
    read as "not bounded". The number that does bound it belongs in the same
    row, or the only way to discover it is to be refused by it."""
    seed(path, LIVE_LEDGER, clock.now)
    post = State(path).ledger_state()["kinds"]["post"]
    assert post["cleanup_used"] == 1
    assert post["cleanup_ceiling"] == state.cleanup_ceiling("post")
    assert post["cleanup_remaining"] == state.cleanup_ceiling("post") - 1
    assert post["cleanup_counts_against_cap"] is False


def test_the_report_shows_the_tightened_ceiling_while_the_breaker_is_open(path, clock):
    """Reporting the relaxed ceiling under an open breaker would describe a
    budget the next claim is not going to honour."""
    seed(path, LIVE_LEDGER, clock.now)
    State(path).trip_breaker("HTTP 999")

    post = State(path).ledger_state()["kinds"]["post"]
    assert post["cleanup_ceiling"] == state.cleanup_ceiling("post", blocked=True)
    assert post["cleanup_ceiling"] < state.cleanup_ceiling("post")


def test_the_report_measures_the_tightened_ceiling_from_the_block(path, clock):
    """The window has to be the claim's window too. Counted over the flat day,
    `cleanup_remaining` reports a channel as spent over undos performed before
    anything was wrong - and an operator told the CLI cannot take a post back
    goes to the browser for a delete the next command would have made."""
    seed(path, LIVE_LEDGER, clock.now)  # one post.cleanup, while all was well
    clock.now += 60
    State(path).trip_breaker("HTTP 999")

    post = State(path).ledger_state()["kinds"]["post"]
    assert post["cleanup_used"] == 1, "the day's undo traffic is still reported"
    assert post["cleanup_remaining"] == state.cleanup_ceiling("post", blocked=True)


def test_the_blocked_row_adds_up_without_reading_the_note(path, clock):
    """`ceiling - used` is what an agent computes before it reads prose, and
    while the breaker is open the two numbers are measured over different
    windows: `cleanup_used` counts the rolling day, `cleanup_remaining` counts
    from the block. Ten legitimate deletions, a block, an eleventh undo the
    ledger allowed, and the row reads used 11 / ceiling 10 / remaining 9 - which
    computes to "spent" for a channel the next `post delete` will find open, the
    reading `_cleanup_cutoff` exists to prevent. The window the numbers are
    measured over has to be in the row."""
    seed(path, {"post.cleanup": 10}, clock.now)
    clock.now += 60
    State(path).trip_breaker("HTTP 999")
    clock.now += 60
    State(path).claim_write("post", cleanup=True)

    post = State(path).ledger_state()["kinds"]["post"]
    assert post["cleanup_used"] == 11, "the day's undo traffic is still reported"
    assert post["cleanup_used_in_window"] == 1
    assert post["cleanup_window_from"] == State(path).breaker_state()["at"]
    assert post["cleanup_ceiling"] - post["cleanup_used_in_window"] == post["cleanup_remaining"]


def test_the_healthy_row_measures_both_counts_over_the_same_day(path, clock):
    """With the breaker closed there is one window and the two counts agree, so
    the added field cannot become a second number to reconcile."""
    seed(path, LIVE_LEDGER, clock.now)
    post = State(path).ledger_state()["kinds"]["post"]
    assert post["cleanup_used_in_window"] == post["cleanup_used"] == 1
    assert post["cleanup_window_from"] == pytest.approx(clock.now - DAY)
    assert post["cleanup_ceiling"] - post["cleanup_used_in_window"] == post["cleanup_remaining"]


def test_the_note_does_not_promise_an_unbounded_undo(path, clock):
    """It used to read "recorded and paced but never refused", full stop, which
    is what an unbounded cleanup channel looks like when it is described."""
    note = State(path).ledger_state()["note"]
    assert "cleanup_ceiling" in note


# ------------------------------------------------ a ledger that cannot be read
#
# `_read` collapsed "no file" and "a file I cannot parse" into `{}`, and every
# enforcement path reads through it - so a corrupt state.json presented itself
# as a pristine one: breaker closed, no cooldown, zero writes used, every limit
# disarmed at once. The next write then rewrote the file and whatever history it
# held was gone. Reads still have to work, because an agent that cannot run
# `doctor` cannot find out why it cannot write; writes cannot, because there is
# nothing left to enforce them against.


def test_a_write_is_refused_while_the_ledger_cannot_be_parsed(path, clock):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this was truncated mid-write")
    s = State(path)

    with pytest.raises(Blocked) as exc:
        s.claim_write("post")
    assert exc.value.exit_code == 9
    assert s.write_count("post", DAY) == 0


def test_the_refusal_names_the_file_and_the_command_that_clears_it(path, clock):
    """The agent that hits this cannot read the source, cannot see the host's
    filesystem and does not know where the ledger lives - `LINKEDIN_STATE_FILE`
    moves it per invocation. "Corrupt ledger" and nothing else leaves it with
    the one failure it cannot act on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")

    with pytest.raises(Blocked) as exc:
        State(path).claim_write("post")
    message = str(exc.value)
    assert str(path) in message
    assert f"mv {path}" in message


def test_the_refusal_is_its_own_kind_of_blocked(path, clock):
    """Tellable from a tripped breaker without parsing English: the remedy is a
    file on this host, not `auth seed` and not waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")

    with pytest.raises(state.LedgerUnreadable):
        State(path).claim_write("post")


def test_an_undo_is_refused_too_because_its_ceiling_cannot_be_counted(path, clock):
    """The one exemption that survives a spent cap does not survive an
    unreadable ledger: the ceiling that bounds an undo is counted out of the
    file that cannot be read, so letting one through restores exactly the
    unmetered channel the ceiling replaced. Unlike a cap this costs no waiting -
    the remedy is one `mv` away, and the message carries it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")

    with pytest.raises(Blocked):
        State(path).claim_write("post", cleanup=True)


def test_the_advisory_check_agrees_with_the_claim_about_it(path, clock):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")

    with pytest.raises(Blocked):
        State(path).check_write_allowed("post")


def test_a_ledger_that_was_never_written_is_not_corruption(path, clock):
    """A first run has no state.json at all and has to work. Collapsing absent
    into corrupt would refuse every write on a fresh host - and on every host
    under the broker, where the ledger starts empty per tenant."""
    assert not path.exists()

    State(path).claim_write("post")
    assert State(path).write_count("post", DAY) == 1


def test_reads_still_work_while_the_ledger_cannot_be_parsed(path, clock):
    """Fail closed on writes, open on reads. A CLI that refused to run at all
    would be the outage the fail-open rule was written to prevent, and `doctor`
    is how the operator is told which file to move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)

    assert s.write_count("post", DAY) == 0
    assert s.breaker_state() is None
    assert s.throttle_state() is None
    assert s.recall("member_urn") is None
    assert s.wait_for_slot(10.0) == 0.0
    assert s.ledger_state()["readable"] is False


def test_the_pacer_rewriting_the_file_does_not_disarm_the_guard(path, clock):
    """The hole this would otherwise leave wide open. Every command paces before
    it writes, and pacing is a read-modify-write: it replaces the unparseable
    file with a parseable one holding a timestamp and nothing else. The claim
    that follows would then sail through with every counter at zero - the exact
    silent disarm this section exists to stop, one layer further along. What was
    lost is recorded in the replacement rather than papered over by it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)
    s.wait_for_slot(10.0)

    json.loads(path.read_text())  # the file parses again
    with pytest.raises(Blocked):
        s.claim_write("post")
    assert s.ledger_state()["readable"] is False


def test_a_throttle_landing_on_a_corrupt_ledger_does_not_re_arm_the_writes(path, clock):
    """The same claim for the other paths that read-modify-write. A 429 arriving
    while the file is corrupt must not be the thing that hands the caps back -
    least of all a 429, which is LinkedIn saying this client is writing too
    much."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)
    s.record_throttle("HTTP 429")

    # A cleanup, because a throttle does not refuse those: the only thing that
    # can raise here is the unreadable ledger.
    with pytest.raises(Blocked):
        s.claim_write("post", cleanup=True)


def test_moving_the_ledger_aside_is_what_the_message_says_it_is(path, clock):
    """The documented remedy has to work, and it has to be the operator's
    deliberate choice rather than something the next write does silently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)
    s.wait_for_slot(10.0)
    path.rename(path.with_name(path.name + ".corrupt"))

    s.claim_write("post")
    assert s.write_count("post", DAY) == 1


def test_a_file_that_is_still_unreadable_never_ages_into_a_trustworthy_one(path, clock):
    """Waiting is not a remedy while the bytes on disk are still unparseable:
    every read of them re-discovers the same unknown history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)
    clock.now += WEEK * 4

    with pytest.raises(Blocked):
        s.claim_write("post")


def test_the_unknown_history_stops_mattering_once_no_window_could_hold_it(path, clock):
    """Self-healing on purpose, once the file itself has been replaced. What the
    lost ledger might have held is only dangerous while it could still be inside
    an enforcement window; after the longest one has passed, every write it
    could have recorded would have aged out of every cap anyway. A host whose
    operator never saw the message is then paced and capped normally again
    rather than refusing writes for the rest of its life.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated")
    s = State(path)
    s.wait_for_slot(10.0)  # the replacement carries the fact that history was lost
    with pytest.raises(Blocked):
        s.claim_write("post")

    clock.now += WEEK + 1
    s.claim_write("post")
    assert s.ledger_state()["readable"] is True


def test_a_writer_no_longer_treats_a_corrupt_ledger_as_an_empty_one(path, clock):
    """This test asserted the opposite until the incident above: it read "the
    distinction is for the report only - a *writer* that refused to run against
    a corrupt ledger would be the outage the fail-open rule prevents", and let
    the claim through. The fail-open rule is right about reads and was wrong
    about writes. A claim allowed here is a write issued with the caps, the
    cooldown and the breaker all disarmed at once, against a file that the same
    write then overwrites - so the evidence of what was already spent is gone
    and the report that would have said so has nothing left to read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")
    s = State(path)

    with pytest.raises(Blocked):
        s.claim_write("post")
    assert s.write_count("post", DAY) == 0
    assert s.ledger_state()["readable"] is False
