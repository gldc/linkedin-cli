"""Cross-process pacing, the write ledger, and the circuit breaker.

A token bucket held in process memory enforces nothing here: every `linkedin`
invocation is a fresh process that would start with a full bucket, so an agent
firing commands in a loop would be paced at exactly zero. The pacing clock and
the write counters therefore live on disk, guarded by `fcntl.flock`.

Who each of these binds, because the caps below read as though they were all the
agent's boundary and four of the five are not. Where a credential broker fronts
this CLI, the `linkedin` tool is allowlisted only a subset of its subcommands -
twelve under the deployment this was written for, of which exactly one is a write
(`messages reply`). Of the five capped kinds, that allowlist therefore leaves the
agent only `message`. The `invite`, `post`, `comment` and `react` caps, and the
whole cleanup-exempt path with its ceiling, are reachable only by the operator
running the CLI by hand. The throttle cooldown and the circuit breaker still bind
both, and so does the pacing above - every one of the eleven reads goes through
them. The allowlist lives outside this repo, so this paragraph is only as true as
the broker policy in force. Nothing here is scoped to the operator or skipped for
the agent; this says who can reach what, not what to enforce.

Two consequences worth knowing before editing:

* The lock is taken on a **sibling** file, not on the state file itself. We
  publish updates with `os.replace`, which swaps the inode - a lock held on the
  old inode would guard nothing once a second process opened the new one.
* `wait_for_slot` sleeps **while holding the lock**. That is deliberate: it is
  what makes a second process queue behind the first instead of pacing off the
  same stale timestamp and firing simultaneously.

Timestamps are wall clock (`time.time`), because `time.monotonic` is only
comparable within one process, and this file is read by many.

Four rules govern the ledger itself:

* A write is **claimed**, not checked-then-recorded. Checking the cap under one
  lock and appending under another lets two invocations both read "one below the
  cap" and both append, which is how a 15-invite cap becomes 20. `claim_write`
  is those two steps under a single lock, and hands back something that can give
  the slot back if the request never left.
* A **cleanup** write - `post delete`, `unreact` and `comment delete` today - is
  never refused by a cap. It is paced and recorded like any other, but a run that trips a cap
  mid-way has to be able to undo what it already did; refusing the undo strands a
  live public post in front of real people, which is worse than any cap it
  protects. It is not unbounded either, which it was until the cleanup ceiling
  shipped: exempt from the caps and exempt from arithmetic are different claims,
  and only the first of them was ever argued for. `cleanup_ceiling` is that
  arithmetic, and an open breaker tightens it rather than closing the channel.
  `invite` is the one write with no inverse anywhere, and not for want of a
  capture: withdrawing an invitation left Voyager for LinkedIn's server-driven
  UI, so there is no payload for anyone to record (`docs/sdui-migration.md`). It
  is therefore bounded by its cap, by LinkedIn's own server-side invitation
  quota, and by nothing else - which is the argument for keeping the invite cap
  low.
* A ledger this process cannot **parse** stops writes. Reads keep working, since
  a corrupt file taking the CLI down is a worse outcome than one that paces
  badly for an invocation - but a write claimed against `{}` is a write made with
  the caps, the cooldown and the breaker disarmed simultaneously, and it then
  overwrites the only record of what had already been spent. Absent is not
  corrupt: a first run has no file and its zeros are true.
* A 429 or 503 is **persisted**. It is the loudest warning LinkedIn gives before
  it restricts an account, and every invocation is a fresh process: discarded,
  the next one starts as if it had never been told to slow down.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local/state/linkedin-cli/state.json"

# Under the credential broker the ledger lives in a broker-owned tree and HOME is
# not the tenant's to write to, so the location has to be relocatable per
# invocation.
STATE_FILE_ENV = "LINKEDIN_STATE_FILE"

DAY = 86400
WEEK = 7 * DAY

# Conservative enough that a human could plausibly have done it by hand; the
# risk of account restriction scales with write velocity specifically.
DAILY_CAPS: dict[str, int] = {
    "invite": 15,
    "message": 40,
    "post": 10,
    "comment": 40,
    "react": 100,
}

# An unrecognised kind is a bug, not a licence to write without limit.
DEFAULT_DAILY_CAP = 15

# A day at the cap every day for a week is not human behaviour, so the rolling
# 7-day allowance is deliberately less than 7x the daily one.
WEEKLY_MULTIPLIER = 5

# Cleanup writes are booked under their own kind rather than the one they undo.
# Charging a delete to the create budget makes every create-and-undo pair cost
# two, so cleaning up an over-run pushes the daily window further out than the
# over-run itself did.
CLEANUP_SUFFIX = ".cleanup"

# How many times a kind's daily cap may be *undone* in 24h. The exemption that
# lets an undo past a spent cap is right and is not what this bounds; what it
# bounds is that the exemption used to have no arithmetic behind it at all, so
# `unreact` in a retry loop was a write channel with no limit of any kind.
#
# Deliberately not "as many undos as there were writes today", which was the
# first proposal and is wrong: an invitation sent on Monday is withdrawn on
# Thursday, and `unreact` on a reaction left last month is an ordinary thing to
# want. A bound derived from the day's own creates refuses both, which is the
# stranded-undo failure the exemption exists to prevent, reintroduced as a
# subtler bug. A multiple of the cap cannot strand one: everything a *full week*
# at the cap could have produced still fits under a single day's ceiling
# (WEEKLY_MULTIPLIER is 5), so reaching it means something is looping rather
# than cleaning up. The ceiling rolls with the same 24h window as the caps -
# except while the breaker is open, where `_cleanup_cutoff` counts from the block
# instead, because the tightened ceiling is an argument about what happened after
# it and counting the whole day stranded exactly the undos it means to protect.
CLEANUP_CEILING_MULTIPLIER = 5

# Where the fact that a ledger was lost is kept, in the file that replaces it.
# Every read of a corrupt file is followed sooner or later by a *write* of a
# clean one - the pacer alone rewrites the file on every invocation - and
# without this the replacement is indistinguishable from a pristine ledger, so
# the refusal below would last exactly until the next command paced itself.
HISTORY_LOST_KEY = "history_lost_at"

# Printed with the counts by `doctor`. The distinction it draws is the one a
# reader gets wrong: a `.cleanup` row looks like consumption and is not, and
# `used` reaching `cap` is the difference between a command that runs and one
# that exits 5.
LEDGER_NOTE = (
    "`used` is what the daily cap bounds; `cleanup_used` is undo traffic, which is recorded "
    "and paced but never refused by a cap, so it is not subtracted from `remaining`. It is "
    "not unlimited either: `cleanup_ceiling` is what stops a runaway undo loop over the same "
    "24h, and an open breaker tightens that ceiling to one day's worth of creates, counted "
    "from the moment the block started rather than from the start of the day - which is why "
    "`cleanup_remaining` is `cleanup_ceiling` minus `cleanup_used_in_window`, the count over "
    "`cleanup_window_from`, and not minus `cleanup_used`, the whole day's. "
    "`unreact`, `post delete` and `comment delete` book a cleanup; `invite` has no inverse "
    "anywhere - withdrawing left Voyager for server-driven UI - so it is bounded by its cap, "
    "by LinkedIn's own server-side invitation quota, and by nothing else. Both windows are "
    "rolling rather than calendar days, and a write is "
    "refused when either one is spent."
)

# How long a throttle damps this client for. LinkedIn's own `Retry-After` is a
# floor, not the answer: the transport already waited it out and was throttled
# anyway, so reaching here means their number was too small. The ceiling holds
# in the other direction - a header this client cannot verify must not be able
# to park an unattended gateway for a day.
THROTTLE_COOLDOWN = 900
THROTTLE_MAX_COOLDOWN = 4 * 3600

# Consecutive throttles double the cooldown, and this is how long one is
# remembered for that purpose - necessarily longer than the cooldown itself, or
# the count would reset the moment the previous damping expired.
THROTTLE_MEMORY = 6 * 3600

# Refusing writes is not enough on its own: the reads that are still allowed are
# part of the request velocity LinkedIn just complained about.
THROTTLE_PACE_MULTIPLIER = 4


class WriteQuotaExceeded(Exception):
    exit_code = 5


class Throttled(WriteQuotaExceeded):
    """LinkedIn said slow down and the cooldown from it has not run out.

    A subclass so that `cli.ERROR_SLUGS` maps it to exit 5 without being taught
    about it, and so a caller that wants to tell a self-imposed cap apart from
    LinkedIn's own signal can.
    """


class Blocked(Exception):
    exit_code = 9


class LedgerUnreadable(Blocked):
    """The ledger file is present and cannot be parsed, so nothing is enforceable.

    A `Blocked` subclass for the same two reasons `Throttled` subclasses
    `WriteQuotaExceeded`: `cli.ERROR_SLUGS` gives it exit 9 without being taught
    about it, and a caller that wants to tell this apart from a tripped breaker
    can - the remedy here is one file on this host, not `auth seed` and not
    waiting. Exit 9 rather than 5 because nothing about waiting fixes it.
    """


def cleanup_kind(kind: str) -> str:
    """The ledger kind an undo of `kind` is booked under."""
    return f"{kind}{CLEANUP_SUFFIX}"


def cleanup_ceiling(kind: str, *, blocked: bool = False) -> int:
    """How many undos of `kind` may be booked in 24h.

    `blocked` is the breaker being open, and it tightens the ceiling rather than
    closing the channel. Both halves of that are load-bearing:

    * Tightened, because an open breaker means LinkedIn is refusing this client -
      a 999, a challenge redirect, or a profile that is signed out - and pushing
      undo after undo through that refusal is the documented road from a soft
      block to a restriction. The loop does not know it is failing; the ledger
      does.
    * Not closed, because the run that is holding something it wants to take back
      is very often the run that just got blocked, and a `post delete` refused
      here leaves a live public post with no CLI way to remove it. One day's cap
      is the honest bound on that: this client cannot have created more than the
      daily cap today, so it cannot legitimately need more undos than that to
      take back what it created before the block. Anything beyond it needs the
      operator, who has `linkedin doctor --clear-breaker`.

    That second argument is about undos performed *since* the block, and it only
    holds if that is what gets counted - which is `_cleanup_cutoff`'s job, and
    was the defect it fixes. Measured over a flat 24h instead, ten legitimate
    `post delete`s this morning left nothing to take back a post published
    minutes before the block: the stranded undo this whole exemption exists to
    prevent, reintroduced by the thing that bounds it.
    """
    daily = DAILY_CAPS.get(kind, DEFAULT_DAILY_CAP)
    return daily if blocked else daily * CLEANUP_CEILING_MULTIPLIER


def resolve_path(path: Path | None = None) -> Path:
    """Where the ledger lives: explicit argument, then env, then the default.

    Read at construction rather than at import, so a broker that sets the
    variable per invocation is honoured and so the tests can relocate it.

    Under a confined deployment (`LINKEDIN_DEPLOYMENT`, see `supervisor.confined`)
    the default is refused rather than used, on the same terms as the browser
    binary and profile. `DEFAULT_PATH` is under `$HOME`, and that HOME is an image
    layer there - destroyed by every container rebuild. So a policy that lost this
    key does not fail; it relocates the ledger onto a surface the next redeploy
    wipes, where the "absent is not corrupt" rule above then reads an honest zero:
    the 40/day message cap, any live throttle cooldown and an *open* circuit
    breaker, all reset in one step with nothing to notice. The supervisor socket
    goes with it, since that is derived from this file's parent.
    """
    if path is not None:
        return Path(path)
    override = os.environ.get(STATE_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    # Imported here rather than at module scope: `supervisor` imports this
    # module, so the two can only meet at call time. Which deployment this is is
    # its question - one answer, three resolvers - and nothing on this path runs
    # while that module is still being imported.
    from . import supervisor

    if supervisor.confined():
        raise supervisor.no_fallback(
            "ledger",
            STATE_FILE_ENV,
            f"{DEFAULT_PATH} is under a HOME that this deployment rebuilds on every redeploy, "
            "so falling back to it silently hands every later invocation a ledger with no "
            "history - the caps, the cooldown and the breaker all back to zero.",
        )
    return DEFAULT_PATH


class State:
    def __init__(self, path: Path | None = None):
        self.path = resolve_path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    # ------------------------------------------------------------------- storage

    @contextmanager
    def _locked(self):
        """Serialize a read-modify-write across processes.

        Never call this from inside another `_locked` block: flock is granted per
        open file description, so a second acquisition from this same process
        would deadlock against the first rather than being re-entrant.
        """
        self._ensure_dir()
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # releases the lock

    def _ensure_dir(self) -> None:
        parent = self.path.parent
        if not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)

    def _read(self) -> dict:
        """Load the state, treating anything unreadable as "no history I have".

        Still fails open, and still must: a corrupt state file taking the CLI
        down is a worse outcome than one invocation pacing badly, and `doctor` -
        the command that says which file to move aside - reads through here too.

        What it no longer does is *forget*. The empty dict it hands back carries
        `HISTORY_LOST_KEY`, so the caller can tell "nothing has been written yet"
        from "something was, and I cannot read it", and so does the file, because
        every read-modify-write path from here persists whatever it was given.
        That is what makes the write refusal survive the pacer: `wait_for_slot`
        replaces the unparseable file on the very next invocation, and without
        the marker the replacement would be a clean slate with every limit at
        zero. Its value is when the loss was first noticed, which is what lets
        `_prune` expire it once no window could still be holding the lost rows.
        """
        data, readable = self._read_checked()
        if not readable:
            # `setdefault`, so the stamp is when the loss was *first* noticed. Set
            # afresh on every read it would never age out, and the week `_prune`
            # measures from it would restart on every command.
            data.setdefault(HISTORY_LOST_KEY, time.time())
        return data

    def _read_checked(self) -> tuple[dict, bool]:
        """`_read`, plus whether the ledger can be trusted to be complete.

        False covers both a file that will not parse and one that parses but is
        standing in for one that would not - see `HISTORY_LOST_KEY`. The second
        case is the one that lasts: the pacer repairs the *file* within a
        command or two, while the counts stay unknown until the operator moves it
        aside or a week passes.

        An absent file is not in that category. Nothing has been written yet, so
        its zeros are true and a first run has to work.

        Not the flag to enforce anything on, and `_read` is the only caller for
        that reason: it answers about the bytes on disk, before `_prune` has had
        the chance to expire a marker that has stopped meaning anything.
        `_enforcement_view` is where that question is settled.
        """
        try:
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {}, True
        except (OSError, ValueError):
            return {}, False
        if not isinstance(data, dict):
            return {}, False
        return data, HISTORY_LOST_KEY not in data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _prune(data: dict, now: float) -> dict:
        """Drop everything past the longest window so the file stays bounded."""
        if HISTORY_LOST_KEY in data:
            # Expired on the same argument the rows are: what a lost ledger might
            # have held can only matter while it could still be inside an
            # enforcement window, and after WEEK every write it could have
            # recorded would have aged out of both caps anyway. Without this, a
            # host whose operator never read the message refuses writes forever
            # over history that stopped existing days ago. A value that is
            # missing, garbage or in the future (a hand edit, a clock step)
            # restarts the week rather than skipping it.
            at = _number(data.get(HISTORY_LOST_KEY))
            if at is None or at > now:
                data[HISTORY_LOST_KEY] = now
            elif now - at >= WEEK:
                data.pop(HISTORY_LOST_KEY)

        throttle = data.get("throttle")
        if "throttle" in data:
            at = _number(throttle.get("at")) if isinstance(throttle, dict) else None
            if at is None or now - at >= THROTTLE_MEMORY:
                data.pop("throttle")

        writes = data.get("writes")
        if not isinstance(writes, dict):
            data["writes"] = {}
            return data
        cutoff = now - WEEK
        data["writes"] = {
            kind: kept
            for kind, stamps in writes.items()
            if (kept := [t for t in _numbers(stamps) if t >= cutoff])
        }
        return data

    def _enforcement_view(self) -> tuple[dict, float, bool]:
        """The ledger as every path that decides something must see it.

        One function because two of them drifted, and the drift bricked writes.
        `claim_write` judged an unreadable ledger off the *pruned* dict while
        `ledger_state` judged it off the raw file, so a `HISTORY_LOST_KEY` older
        than `WEEK` - which `_prune` expires on purpose, see the reason there -
        went on being reported as unreadable. `cli._guard_breaker` reads that
        report and runs before dispatch, so on a write-only workflow the command
        exited 9 before reaching the claim that would have pruned the marker
        away. The same file, at the same instant, refused by one path and allowed
        by the other, permanently.

        Returns the pruned dict, the instant it was pruned at, and whether
        anything can be enforced from it. Takes no lock: `claim_write` holds one
        around this *and* the write that follows, and acquiring a second from
        inside would deadlock against the first - see `_locked`.
        """
        data = self._read()
        now = time.time()
        data = self._prune(data, now)
        return data, now, HISTORY_LOST_KEY not in data

    # -------------------------------------------------------------------- pacing

    def wait_for_slot(self, min_interval: float) -> float:
        """Block until `min_interval` has passed since the last request, then
        record now. Returns the seconds actually slept.

        Clamped rather than short-circuited. Returning early on a non-positive
        interval also skips *writing the timestamp*, so the next invocation
        would find no `last_request` and run unpaced too - which is exactly the
        `--rate=0` hole the CLI now rejects, reintroduced one layer down where
        no flag has to be passed to reach it. This file is the last word on
        pacing, not its caller.

        A throttle still in force multiplies the interval, so the reads a
        cooldown leaves allowed are slowed rather than merely unrefused.
        """
        min_interval = max(min_interval, 0.0)

        slept = 0.0
        with self._locked():
            data = self._read()
            last = data.get("last_request")
            now = time.time()
            if _active_throttle(data, now) is not None:
                min_interval *= THROTTLE_PACE_MULTIPLIER
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                # Clamped, so a backwards clock step (NTP, suspend) parks the
                # CLI for one interval at worst instead of for hours.
                wait = min(min_interval - (now - last), min_interval)
                if wait > 0:
                    time.sleep(wait)
                    slept = wait
                    now = time.time()
            data["last_request"] = now
            self._write(self._prune(data, now))
        return slept

    # -------------------------------------------------------------------- ledger

    def claim_write(self, kind: str, *, cleanup: bool = False) -> "WriteClaim":
        """Take a cap slot and book it in one locked operation.

        The two halves cannot be separated. A caller that checks under one lock
        and appends under another leaves a window in which every concurrent
        invocation sees the same "one below the cap", and they all write - which
        is the whole point of a cap on an agent that can fire in a loop.

        The row lands *before* the request is issued, and `release` is how a
        caller gives it back when nothing was sent. The asymmetry is deliberate:
        an unreleased claim over-counts by one, while recording only on success
        loses every write whose process died with the request already on the
        wire, and those are exactly the ones LinkedIn may have applied.

        `cleanup=True` is an undo of a write that is already visible to someone.
        It is booked, the caller still paces it, and no cap, cooldown or open
        breaker can refuse it outright - see the module docstring. It answers to
        `cleanup_ceiling` instead, which is what stops the exemption from being
        an unmetered write channel.
        """
        with self._locked():
            data, now, readable = self._enforcement_view()
            self._guard_readable(readable)
            if cleanup:
                _guard_cleanup(data, kind, now)
            else:
                _guard_write(data, kind, now)
            booked = cleanup_kind(kind) if cleanup else kind
            data["writes"].setdefault(booked, []).append(now)
            self._write(data)
        return WriteClaim(self, booked, now, cleanup)

    def _guard_readable(self, readable: bool) -> None:
        """Refuse a write claimed against a ledger this process could not read.

        Takes the verdict rather than the data, so that `_enforcement_view` is
        the only place in this file that decides what "readable" means. It used
        to be decided twice and the two spellings drifted - see that function.

        The incident this closes: `_read` collapsed "absent" and "unparseable"
        into `{}`, and every enforcement path reads through it - so a corrupt
        state.json presented itself as a pristine one, with the breaker closed,
        no cooldown and zero writes used. Not one limit was in force, the run
        reported success, and the next write rewrote the file over the evidence.

        A cleanup is refused here too, though nothing else refuses one. Its
        ceiling is counted out of the same unreadable file, so letting it through
        restores precisely the unmetered channel the ceiling exists to close.
        The trade is defensible only because this refusal costs no waiting: it is
        one `mv` away, on this host, and the message carries the command - which
        is why it names the *resolved* path. `LINKEDIN_STATE_FILE` moves the
        ledger per invocation under the broker, and the agent that reads this
        cannot see the source, the host or the environment it ran under.
        """
        if readable:
            return
        target = shlex.quote(str(self.path))
        raise LedgerUnreadable(
            f"the write ledger at {self.path} exists but could not be parsed, so how much of "
            "the day's budget is already spent is unknown - it is not zero. No cap, cooldown "
            "or breaker can be enforced from a ledger this client cannot read, so writes are "
            "refused until it is replaced. Move it aside and let the next run recreate it, "
            f"deliberately discarding whatever history it held: mv {target} {target}.corrupt"
        )

    def _release_claim(self, kind: str, at: float) -> None:
        with self._locked():
            data = self._read()
            writes = data.get("writes")
            stamps = writes.get(kind) if isinstance(writes, dict) else None
            if isinstance(stamps, list) and at in stamps:
                stamps.remove(at)
                self._write(data)

    def record_write(self, kind: str) -> None:
        with self._locked():
            data = self._read()
            now = time.time()
            data = self._prune(data, now)
            data["writes"].setdefault(kind, []).append(now)
            self._write(data)

    def write_count(self, kind: str, window_seconds: int) -> int:
        with self._locked():
            data = self._read()
        return _count(data, kind, time.time() - window_seconds)

    def ledger_state(self) -> dict:
        """What each write kind has spent against its caps, for `doctor`.

        One locked read for the whole picture rather than a `write_count` call
        per kind: the counts would otherwise be sampled at different instants
        under different locks, and a report whose lines disagree with each other
        is worse than no report.

        Read-only, and that is a property to keep. An agent can run `doctor`
        too, and a diagnostic that pruned or reset the counters would be the
        loop the counters exist to stop. `_enforcement_view` prunes in memory
        and nothing here writes the result back, which is what lets this answer
        the question `claim_write` will answer without becoming a writer itself.

        `readable` is that same verdict and not a second opinion on it.
        `cli._breaker_verdict` refuses writes on this key before dispatch, so a
        report that said "unreadable" where the claim would have said "fine"
        refused writes nothing could ever unrefuse - see `_enforcement_view`.

        Every kind in `DAILY_CAPS` appears whether or not it has been spent - an
        absent kind reads as a kind with no limit - plus anything on disk that
        is not in the table, because `_guard_write` still enforces
        `DEFAULT_DAILY_CAP` against it.

        Costs no traffic. The ledger is a local file, and `doctor` already
        spends five live calls on its surface probes; a diagnostic that added a
        sixth to read a number off the disk beside it would be charging the
        account for its own report.

        A ledger that will not parse reports **no kinds at all**, not a set of
        zeros - see `_read_checked`.
        """
        with self._locked():
            data, now, readable = self._enforcement_view()
        if not readable:
            return {
                "window_hours": DAY // 3600,
                "readable": False,
                # Deliberately empty rather than zeroed. Nothing about the day is
                # known, and an empty mapping cannot be mistaken for a day in
                # which nothing was written.
                "kinds": {},
                "problem": (
                    f"the write ledger at {self.path} could not be parsed, so how much of "
                    "today's budget is spent is unknown - it is not zero. Writes are refused "
                    "until it is replaced, because no cap, cooldown or breaker can be "
                    "enforced from a file this client cannot read. Move it aside and let the "
                    "next run recreate it, deliberately discarding whatever history it held: "
                    f"mv {shlex.quote(str(self.path))} {shlex.quote(str(self.path))}.corrupt"
                ),
                "note": LEDGER_NOTE,
            }
        writes = data.get("writes")
        on_disk = writes.keys() if isinstance(writes, dict) else ()
        kinds = set(DAILY_CAPS) | {
            k[: -len(CLEANUP_SUFFIX)] if k.endswith(CLEANUP_SUFFIX) else k for k in on_disk
        }

        # Reported against the breaker as it stands, not against the relaxed
        # number, because an open breaker tightens every ceiling below and a
        # report that printed the relaxed one would be describing a budget the
        # next claim is not going to honour.
        blocked = isinstance(data.get("breaker"), dict)
        # And counted from where the claim counts it. While the breaker is open
        # that is the moment of the block, not the start of the day - a
        # `cleanup_remaining` measured over the wrong window would report a
        # channel as spent that the next undo is going to find open, which is the
        # reading that sends an operator to the browser for a post this CLI would
        # in fact have deleted.
        cutoff = _cleanup_cutoff(data, now)

        report = {}
        for kind in sorted(kinds):
            daily = DAILY_CAPS.get(kind, DEFAULT_DAILY_CAP)
            weekly = daily * WEEKLY_MULTIPLIER
            used, used_week = _count(data, kind, now - DAY), _count(data, kind, now - WEEK)
            ceiling = cleanup_ceiling(kind, blocked=blocked)
            cleanup_used = _count(data, cleanup_kind(kind), now - DAY)
            cleanup_in_window = _count(data, cleanup_kind(kind), cutoff)
            report[kind] = {
                "used": used,
                "cap": daily,
                # Clamped. A claim whose process died with the row already
                # written, or a hand-edited file, can put `used` past the cap,
                # and `remaining: -3` reads as a number to act on.
                "remaining": max(0, daily - used),
                "used_7d": used_week,
                "cap_7d": weekly,
                "remaining_7d": max(0, weekly - used_week),
                # Reported *beside* the capped count and never folded into it.
                # An undo is recorded and paced like any other write and is never
                # refused, so adding it to `used` would show a budget being spent
                # that no cap will ever enforce - which is the opposite of what
                # the separate bucket exists to say. The bucket name is carried
                # so a number here can be traced to a row in the file.
                "cleanup_bucket": cleanup_kind(kind),
                "cleanup_used": cleanup_used,
                "cleanup_counts_against_cap": False,
                # `cleanup_counts_against_cap: false` says "not bounded by the
                # cap" and gets read as "not bounded". The number that does
                # bound it belongs in the same row, or the only way to learn it
                # is to be refused by it - and the refusal reaches an agent
                # holding something it has already published.
                "cleanup_ceiling": ceiling,
                # The window the two numbers below are measured over, emitted
                # because without it the row does not add up. `cleanup_used`
                # counts the rolling day and `cleanup_remaining` counts from the
                # block, so an open breaker could print `used: 11, ceiling: 10,
                # remaining: 9` - and an agent that does `ceiling - used` before
                # it reads `LEDGER_NOTE` gets -1 and concludes the undo channel
                # is spent, which is the reading that sends an operator to the
                # browser for a post this CLI would in fact have deleted. With
                # the count over the same window beside it, `ceiling -
                # cleanup_used_in_window` is `cleanup_remaining` - clamped at
                # zero, exactly as `used` and `remaining` are above.
                "cleanup_window_from": cutoff,
                "cleanup_used_in_window": cleanup_in_window,
                # Against `cutoff` rather than `cleanup_used`, which is always
                # the day's traffic. The two agree unless the breaker is open,
                # and there the difference is the point: `cleanup_used` says what
                # this client has undone today, `cleanup_remaining` says how much
                # of the tightened ceiling the block has left.
                "cleanup_remaining": max(0, ceiling - _count(data, cleanup_kind(kind), cutoff)),
            }
        return {
            "window_hours": DAY // 3600,
            "readable": True,
            "kinds": report,
            "problem": None,
            "note": LEDGER_NOTE,
        }

    def check_write_allowed(self, kind: str) -> None:
        """Raise if the breaker, a throttle or the rolling caps forbid a write.

        Advisory only, and kept for callers that want to refuse before they have
        anything to send. Nothing enforces a cap here - two processes can both
        pass this - so any write that actually goes out must go through
        `claim_write` instead.

        Pruned in memory before the guard runs, and not written back: this must
        answer the same question `claim_write` would, and the claim prunes first.
        An advisory check that refused an unreadable ledger the claim would let
        through - or the reverse - sends a caller looking for a fault in the
        wrong place.
        """
        with self._locked():
            data, now, readable = self._enforcement_view()
        self._guard_readable(readable)
        _guard_write(data, kind, now)

    # ------------------------------------------------------------------ throttle

    def record_throttle(self, reason: str = "", retry_after: float | None = None) -> dict:
        """Persist a 429/503 and start (or extend) the cooldown it earns.

        Consecutive throttles double the cooldown, because a second one means
        the first cooldown was not long enough - and the failure this guards
        against is an agent that reads exit 5 as "try again in a minute" until
        the account is restricted.
        """
        with self._locked():
            now = time.time()
            data = self._read()

            count = 1
            previous = data.get("throttle")
            if isinstance(previous, dict):
                at = _number(previous.get("at"))
                seen = _number(previous.get("count"))
                if at is not None and 0 <= now - at < THROTTLE_MEMORY:
                    count = int(seen or 0) + 1

            cooldown = THROTTLE_COOLDOWN * 2 ** min(count - 1, 16)
            cooldown = max(cooldown, _number(retry_after) or 0.0)
            entry = {
                "reason": reason or "LinkedIn throttled this client",
                "at": now,
                "until": now + min(cooldown, THROTTLE_MAX_COOLDOWN),
                "count": count,
            }
            data["throttle"] = entry
            self._write(self._prune(data, now))
            return entry

    def throttle_state(self) -> dict | None:
        """The throttle still in force, or None once its cooldown has run out."""
        with self._locked():
            data = self._read()
        return _active_throttle(data, time.time())

    def clear_throttle(self) -> None:
        with self._locked():
            data = self._read()
            if data.pop("throttle", None) is not None:
                self._write(data)

    # ------------------------------------------------------------------- breaker

    def trip_breaker(self, reason: str) -> None:
        with self._locked():
            data = self._read()
            # Keep the first cause: later trips are usually consequences of it,
            # and the operator needs to see what actually started the block.
            if not isinstance(data.get("breaker"), dict):
                data["breaker"] = {"reason": reason, "at": time.time()}
                self._write(data)

    def breaker_state(self) -> dict | None:
        with self._locked():
            data = self._read()
        breaker = data.get("breaker")
        return breaker if isinstance(breaker, dict) else None

    def clear_breaker(self) -> None:
        with self._locked():
            data = self._read()
            if data.pop("breaker", None) is not None:
                self._write(data)

    def count_session_failure(self, *, window: int = 3600, limit: int = 3) -> int:
        """Record a dead-session outcome and arm the breaker once they repeat.

        Nothing else counts these. The browser pivot removed the re-acquire loop,
        so a `session_expired` that reaches the operator means the profile is
        signed out - and the remedy is `auth seed`, which an agent cannot run for
        itself. An agent that treats exit 3 as retryable would otherwise keep
        issuing authenticated requests against a session LinkedIn just ended,
        which is how a soft block becomes a restriction.
        """
        with self._locked():
            data = self._read()
            now = time.time()
            stamps = [t for t in _numbers(data.get("session_failures", [])) if now - t < window]
            stamps.append(now)
            data["session_failures"] = stamps
            if len(stamps) >= limit and not isinstance(data.get("breaker"), dict):
                data["breaker"] = {
                    "reason": (
                        f"{len(stamps)} dead-session responses within "
                        f"{window // 60} minutes; the browser profile is signed out"
                    ),
                    "at": now,
                }
            self._write(data)
            return len(stamps)

    def clear_session_failures(self) -> None:
        """Any successful call proves the session is alive, so the run resets."""
        with self._locked():
            data = self._read()
            if data.pop("session_failures", None):
                self._write(data)

    # ------------------------------------------------------------------- notes

    def remember(self, key: str, value) -> None:
        """Cache a derived fact that costs a request to rediscover.

        The member urn is the only one today. It used to live in the session
        file, which the browser pivot deleted along with the rest of the
        cookie-handling tower - but the reason for caching it survived: every
        messaging call addresses a mailbox by it, so re-fetching it per
        invocation would double this CLI's request rate against an account whose
        one remaining safety control is how rarely it calls.
        """
        with self._locked():
            data = self._read()
            notes = data.setdefault("notes", {})
            if not isinstance(notes, dict):
                notes = data["notes"] = {}
            if notes.get(key) != value:
                notes[key] = value
                self._write(data)

    def recall(self, key: str):
        with self._locked():
            data = self._read()
        notes = data.get("notes")
        return notes.get(key) if isinstance(notes, dict) else None


class WriteClaim:
    """One booked ledger row, held by the caller until the write resolves.

    Two methods, and neither of them writes the row: `claim_write` already did.
    `commit` says the request left this process, which makes the row permanent;
    `release` says it never did, and gives the slot back. Settling twice is a
    no-op rather than a second refund - concurrent claims can share a timestamp,
    so a release that ran twice would remove another process's row.
    """

    def __init__(self, ledger: "State", kind: str, at: float, cleanup: bool):
        self.kind = kind
        self.at = at
        self.cleanup = cleanup
        self._ledger = ledger
        self._settled = False

    def commit(self) -> None:
        self._settled = True

    def release(self) -> None:
        if self._settled:
            return
        self._settled = True
        self._ledger._release_claim(self.kind, self.at)


def _guard_write(data: dict, kind: str, now: float) -> None:
    """Raise if `data` says an ordinary write of `kind` must not go out."""
    breaker = data.get("breaker")
    if isinstance(breaker, dict):
        raise Blocked(
            f"LinkedIn blocked this client ({breaker.get('reason', 'reason unrecorded')}) "
            "and the circuit breaker is open. Re-authenticate from the browser, then run "
            "`linkedin doctor --clear-breaker`."
        )

    throttle = _active_throttle(data, now)
    if throttle is not None:
        minutes = max(1, int((throttle["until"] - now) // 60))
        raise Throttled(
            f"LinkedIn throttled this client ({throttle.get('reason', 'HTTP 429/503')}) "
            f"and the cooldown has ~{minutes} minutes left. Cleanup verbs still run; "
            "ordinary writes have to wait it out."
        )

    daily = DAILY_CAPS.get(kind, DEFAULT_DAILY_CAP)
    if _count(data, kind, now - DAY) >= daily:
        raise WriteQuotaExceeded(
            f"daily cap reached for '{kind}' ({daily} per 24h). Wait for the "
            "window to roll rather than raising the cap."
        )

    weekly = daily * WEEKLY_MULTIPLIER
    if _count(data, kind, now - WEEK) >= weekly:
        raise WriteQuotaExceeded(
            f"7-day cap reached for '{kind}' ({weekly} per 7 days). Sustained "
            "write volume is what gets accounts restricted."
        )


def _guard_cleanup(data: dict, kind: str, now: float) -> None:
    """Raise if an undo of `kind` has stopped looking like an undo.

    Everything `_guard_write` checks is deliberately absent. The caps are, for
    the reason in the module docstring. The cooldown is, because a 429 is
    LinkedIn saying "slow down" rather than "stop", and the pacer already
    quadruples the interval for every request a cooldown leaves allowed, so the
    undo goes out slowly instead of not at all. The breaker is not absent so much
    as folded into the ceiling - `cleanup_ceiling` explains that trade - because
    an open breaker is the one signal that means the next request will be refused
    outright rather than merely disapproved of.

    What is left is arithmetic that no cleanup a person would perform can reach,
    and that a wedged retry loop reaches in seconds.
    """
    blocked = isinstance(data.get("breaker"), dict)
    ceiling = cleanup_ceiling(kind, blocked=blocked)
    if _count(data, cleanup_kind(kind), _cleanup_cutoff(data, now)) < ceiling:
        return

    if blocked:
        breaker = data.get("breaker", {})
        raise WriteQuotaExceeded(
            f"{ceiling} undo writes of '{kind}' since LinkedIn started refusing this client "
            f"({breaker.get('reason', 'reason unrecorded')}) is already as many as it could "
            "have created in a day, so what is left is a loop rather than a cleanup. Pushing "
            "more undos through an open breaker is how a soft block becomes a restriction. "
            "Re-authenticate from the browser, then run `linkedin doctor --clear-breaker`."
        )
    raise WriteQuotaExceeded(
        f"cleanup ceiling reached for '{kind}' ({ceiling} undo writes per 24h). An undo is "
        "never refused by a cap, but this many in one day is a loop rather than a cleanup - "
        f"more than a full week at the '{kind}' cap could have created. Wait for the window "
        "to roll."
    )


def _cleanup_cutoff(data: dict, now: float) -> float:
    """The instant the cleanup ceiling counts undos from.

    The rolling 24h, except while the breaker is open, where it is whenever the
    block started. The tightened ceiling's whole argument is about what this
    client could have created and now needs to take back *since* LinkedIn began
    refusing it; undos performed hours earlier against a healthy account are not
    that, and counting them is what let ten legitimate deletions this morning
    strand the eleventh - a post published minutes before the block, live and
    with no CLI verb left to remove it.

    Never wider than the 24h window the caps roll on, and never narrower than
    "unknown": a breaker with a missing, garbage or future `at` - a hand edit, a
    clock step - falls back to the full day rather than to nothing.
    """
    day = now - DAY
    breaker = data.get("breaker")
    if not isinstance(breaker, dict):
        return day
    at = _number(breaker.get("at"))
    return day if at is None or at > now else max(at, day)


def _active_throttle(data: dict, now: float) -> dict | None:
    throttle = data.get("throttle")
    if not isinstance(throttle, dict):
        return None
    until, at = _number(throttle.get("until")), _number(throttle.get("at"))
    if until is None:
        return None
    if at is not None:
        # A file that says the cooldown ends in a year - a hand edit, or a clock
        # that stepped - must not park every write until then.
        until = min(until, at + THROTTLE_MAX_COOLDOWN)
    return {**throttle, "until": until} if now < until else None


def _count(data: dict, kind: str, cutoff: float) -> int:
    writes = data.get("writes")
    stamps = writes.get(kind) if isinstance(writes, dict) else None
    return sum(1 for t in _numbers(stamps) if t >= cutoff)


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _numbers(values) -> list[float]:
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
