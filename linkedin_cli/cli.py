"""Argument parsing, dispatch and exit codes.

Deliberately hand-rolled rather than argparse: the surface is a verb/noun tree
with URN positionals full of `:`, `(` and `,`, and argparse's prefix matching and
short-flag habits both work against a caller that is a program rather than a
person.

Two responsibilities here are not obvious from the command table:

* **The circuit breaker is armed and checked here.** `transport` raises `Blocked`
  on a 999 or a challenge redirect, but a raise only ends *this* process - and
  the failure mode that matters is an agent in a loop, where the next invocation
  starts clean and hits LinkedIn again. So a `Blocked` trips the on-disk breaker
  before it is reported, and every subsequent command refuses until the operator
  clears it. `auth` and `doctor` are exempt, because refusing the two commands
  that explain and clear the block would leave no way out.
* **Exit codes come from three unrelated exception families** - `transport`,
  `state` and this module's own `UsageError` - which is why the mapping is a
  table rather than an `exit_code` attribute lookup. See `_report`.
"""

from __future__ import annotations

import difflib
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from . import render, state, supervisor, transport
from .surfaces import feed, invitations, messaging, notifications, posts, profile, social

# One command per line, and no sub-verb hidden behind a `|`: the sweep in
# tests/test_cli.py reads this text the way an agent does, and a phrase written
# `post get <urn> | post delete <urn>` hid a stub behind a verb that works.
# ASCII throughout for the same reason `render.py` is - this goes to stdout,
# which may be a pipe under `LC_ALL=C`.
USAGE = """linkedin - drive LinkedIn from the shell

  linkedin me
  linkedin auth seed [--from-profile=PATH]
  linkedin auth status
  linkedin profile get [<public-id-or-url>]

  linkedin feed list [--count=N] [--cursor=C]
  linkedin post get <urn>
  linkedin post create --text=... [--visibility=ANYONE]
  linkedin post delete <urn> --yes             (irreversible; exempt from caps)
  linkedin react <urn> [--type=LIKE]
  linkedin unreact <urn>
  linkedin comment <urn> --text=...
  linkedin comment delete <comment-urn>        (irreversible; exempt from caps)

  linkedin messages list [--unread-only] [--count=N] [--cursor=C]
  linkedin messages read <conversation-urn> [--count=N] [--cursor=C]
  linkedin messages counts
  linkedin messages send --to=<urn|url> --text=... [--conversation=<urn>]
  linkedin messages reply <conversation-urn> --text=...
  linkedin messages mark-all-read --yes        (marks the WHOLE mailbox seen)

  linkedin invite <public-id-or-url>           (a connection request; no undo)
  linkedin invitations list [--count=N] [--cursor=C]   (ones you RECEIVED)

  linkedin notifications list [--unread-only] [--count=N] [--cursor=C]
  linkedin doctor [--clear-breaker]           (browser, breaker, write budget)

Not written yet. Each dispatches and then refuses, whatever you pass it - named
here rather than omitted, because a silent gap reads as a typo:
  linkedin notifications mark-read <urn>  not implemented - body never captured
  linkedin messages mark-read <urn>       not implemented - renamed mark-all-read

Not written, and not coming. The invitation manager left Voyager for LinkedIn's
server-driven UI, so there is nothing to capture; see docs/sdui-migration.md:
  linkedin invite withdraw <urn>         not implemented - withdraw in a browser
  linkedin invitations sent              not implemented - no route survives

Global: --format=json|text (default json) --raw --dry-run --rate=N --help
  --count=N --cursor=C   read by feed list, messages list, messages read,
                         notifications list and invitations list; ignored by
                         every other command
  --raw                  prints the upstream payload instead of the envelope,
                         and on a failure carries the response body inside
                         error.body - redacted, without changing the exit code
  --idempotency-key=K    read by messages send and messages reply, where it sets
                         the originToken. It buys traceability, NOT safety: the
                         captured body sends dedupeByClientGeneratedToken=false,
                         so the same key twice is a second message in the thread.
                         post create refuses the flag outright
  --note=...             refused by invite - the captured payload has no field
                         for a note, and one is not invented for you
  --verbose              accepted and ignored - nothing in this CLI reads it

Exit: 0 ok | 2 usage | 3 auth | 4 not found | 5 throttled | 6 upstream
      | 7 stale queryId | 9 blocked
      No exit code means "retry": error.retryable in the envelope is the only
      thing that says so. Exit 6 covers both a request that never landed and a
      write that may already have - a send reported as unconfirmed is the second
      kind, and repeating it puts a second message in the thread with no unsend.
"""

BOOLEAN_FLAGS = {
    "raw",
    "dry-run",
    "verbose",
    "help",
    "unread-only",
    "clear-breaker",
    "yes",
}

# Flags every command accepts, whatever it does with them.
GLOBAL_FLAGS = {
    "format",
    "raw",
    "dry-run",
    "verbose",
    "help",
    "rate",
    "count",
    "cursor",
    "idempotency-key",
}

# Flags scoped to one verb. Two tables rather than one set, because knowing the
# *name* of a flag is not enough: a flag the handler never reads is exactly as
# silent as one that does not exist, and `feed list --text=…` used to be
# accepted. A new flag has to be registered here or the parser refuses it.
COMMAND_FLAGS: dict[str, set[str]] = {
    "auth": {"from-profile"},
    "me": set(),
    "profile": set(),
    "feed": set(),
    # `--visibility` is registered here or the allowlist refuses it, and a post
    # meant for connections cannot be written at all. It used to be worse than
    # that: unknown flags were accepted and dropped, so it would have gone out to
    # everyone under a flag that said otherwise. `--yes` is `post delete`'s
    # opt-in, and unregistered it would make the undo unreachable entirely.
    "post": {"text", "visibility", "yes"},
    "messages": {"unread-only", "text", "to", "conversation", "yes"},
    "react": {"type"},
    "unreact": set(),
    "comment": {"text"},
    "notifications": {"unread-only"},
    "doctor": {"clear-breaker"},
    "invite": {"note"},
    "invitations": set(),
}

KNOWN_FLAGS = GLOBAL_FLAGS.union(*COMMAND_FLAGS.values())

# Names that are not near-misses of a real flag but are what a caller reaching
# for the concept guesses first. `--limit=2` was confirmed live: it ran clean and
# returned a default-sized page, because the flag is `--count`.
FLAG_ALIASES = {
    "limit": "count",
    "max": "count",
    "size": "count",
    "page-size": "count",
    "offset": "cursor",
    "start": "cursor",
    "after": "cursor",
    "page": "cursor",
    "body": "text",
    "message": "text",
    "content": "text",
    "msg": "text",
    "recipient": "to",
    "reaction": "type",
    "json": "format",
    "output": "format",
    "force": "yes",
    "confirm": "yes",
}

# `auth` acquires the session the breaker is complaining about, and `doctor` is
# what reports and clears it. Gating either would be a dead end.
BREAKER_EXEMPT = frozenset({"auth", "doctor"})

# Undo verbs, keyed the way the limits have to see them: a value of `None` means
# the whole verb is an undo, otherwise it names the sub-actions that are. The
# distinction is not cosmetic - `post create` and `post delete` are one verb and
# only one of them may be refused - so anything gating a command has to consult
# this and not the verb alone.
CLEANUP_ACTIONS: dict[str, frozenset[str] | None] = {
    "unreact": None,
    # Not `{"post": None}`. `post create` and `post delete` are one verb, and a
    # verb-keyed exemption would make publishing unstoppable - no cap, no
    # breaker - the moment its inverse shipped.
    "post": frozenset({"delete"}),
    # And the same shape again, for the same reason: `comment` and
    # `comment delete` are one verb, and only the undo is exempt.
    "comment": frozenset({"delete"}),
}


# Every verb that reaches `_write`, keyed the same way, and it has to be kept
# that way by hand for the same reason `COMMAND_FLAGS` is: what a command does
# is not derivable from its name. `_guard_breaker` refuses these and lets
# everything else through, so a write missing from this table is a write that
# goes out with the breaker unverified - which is the defect this table exists
# to close. `tests/test_cli.py` derives the set of commands that really POST and
# checks it against this one, so a new write cannot be added without landing
# here.
WRITE_ACTIONS: dict[str, frozenset[str] | None] = {
    "post": frozenset({"create", "delete"}),
    "messages": frozenset({"send", "reply", "mark-all-read"}),
    "react": None,
    "unreact": None,
    "comment": None,
    "invite": None,
}


def _dispatches_to(table: dict[str, frozenset[str] | None], verb: str, args: list[str]) -> bool:
    """Whether `verb` plus its sub-verb is in one of the verb/action tables."""
    if verb not in table:
        return False
    actions = table[verb]
    return actions is None or bool(args) and args[0] in actions


def is_cleanup(verb: str, args: list[str]) -> bool:
    return _dispatches_to(CLEANUP_ACTIONS, verb, args)


def is_write(verb: str, args: list[str]) -> bool:
    return _dispatches_to(WRITE_ACTIONS, verb, args)


# There is no `UNIMPLEMENTED` table any more, and its absence is the point: it
# held `invitations`, whose received side ships now that the route is verified
# (docs/sdui-migration.md) and whose sent side is refused inside `cmd_invitations`
# because it is not a gap that dispatching differently would close. A table with
# nothing in it is a mechanism nobody maintains and a parametrized test that
# quietly stops running, so it was deleted rather than emptied.

# Two distinct reasons a write is missing, and an agent deciding whether to wait
# or to give up needs to be told which one it hit.
# The method matters as much as the gap. The earlier plan said to capture a
# write by performing it - publish and delete a throwaway post - and on
# a live run that sent a real connection invitation to the wrong person. Anyone
# reading this string is about to go and capture the payload, so it names the
# method that replaced it rather than the phase that was withdrawn.
_NOT_CAPTURED = (
    "`{what}` is not implemented: its request payload has not been captured from "
    "a live session, and this CLI does not ship guessed request bodies. Capture it "
    "by interception with tools/capture_payloads.py - drive the real control, pause "
    "the request with CDP `Fetch` at `requestStage: Request`, record "
    "`request.postData` and abort it - so the action never reaches LinkedIn."
)

# The opt-in `post delete` demands. An opt-in rather than a confirmation prompt,
# for the reason `messages mark-all-read` uses one: the caller is a program, and
# this CLI can be driven by an agent that has read an attacker's text. It is a
# flag the *caller* supplies, so it withholds nothing from the run that needs the
# undo - unlike a cap or a breaker, which is why those exempt this verb and this
# does not.
_CONFIRM_DELETE = (
    "`post delete` removes the post for good, and takes its reactions and every comment "
    "on it along with it - LinkedIn offers no undo and this CLI cannot put any of it back. "
    "Pass --yes to confirm it, or --dry-run to see the exact request first."
)

# `--note` is the flag a caller reaches for the moment `invite` exists, and the
# captured payload has nothing to put one in. Refused rather than dropped: a
# dropped note sends the bare invitation the caller was trying to avoid, and
# reports success.
_NO_INVITE_NOTE = (
    "`invite` cannot carry a note: a request body with a note field in it has not been "
    "captured, and this CLI does not ship guessed request bodies. An invented `message` key "
    "either comes back as a "
    "bare 400 that names no field, or is accepted and ignored - in which case the invitation "
    "goes out bare while you believe your note went with it. Send it without --note, or add "
    "the note by hand in the browser."
)


class UsageError(Exception):
    exit_code = 2


# Refusals this CLI makes on its own account, which by construction have no
# upstream response behind them: bad input, an open breaker, a spent cap, a
# ledger that will not parse. Listed so `Context.upstream_body` can refuse to
# hand one of them a payload some *earlier* request happened to return - the
# subclasses come with their parents, so `Throttled` and `LedgerUnreadable` are
# covered by `WriteQuotaExceeded` and `Blocked`. `transport.Blocked` is
# deliberately absent: LinkedIn is the one saying 999, and that is an answer.
LOCAL_REFUSALS: tuple[type[BaseException], ...] = (
    UsageError,
    state.Blocked,
    state.WriteQuotaExceeded,
)


@dataclass
class Page:
    """A list result plus the cursor needed to continue it."""

    items: list
    next_cursor: str | None = None
    has_more: bool = False


@dataclass
class Context:
    args: list[str]
    flags: dict[str, Any]
    stdout: Any
    stderr: Any
    _client: Any = None
    _injected_client: Any = None
    _cookies: dict = field(default_factory=dict)
    _member_urn: str | None = None
    attempted_write: bool = False
    # The last body LinkedIn handed back, kept for `--raw` on the error path.
    # Only useful for the failures that arrive *inside* a 200 - a GraphQL
    # `errors` array, a create that names no urn - which is exactly the class
    # that has no other evidence: a 4xx already carries its body in the message
    # `transport.raise_for_status` builds, while a refusal under a 200 is
    # reduced by the surface to one extracted sentence.
    last_response: Any = None
    # How many requests this process has started, and which of them the body
    # above came out of. Counted rather than keyed by `(method, path)`: a
    # read-back that answers once and fails the second time asks the same
    # endpoint both times, and a path-keyed match would hand the failure its own
    # predecessor's payload. Two fields because they disagree exactly when the
    # body must not be shown - see `upstream_body`.
    requested: int = 0
    answered: int | None = None

    def flag(self, name: str, default=None):
        return self.flags.get(name, default)

    def int_flag(self, name: str, default: int) -> int:
        raw = self.flags.get(name)
        if raw in (None, True):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise UsageError(f"--{name} expects a number, got {raw!r}") from exc

    def arg(self, index: int, name: str) -> str:
        try:
            return self.args[index]
        except IndexError as exc:
            raise UsageError(f"missing required argument: <{name}>") from exc

    def require(self, name: str) -> str:
        value = self.flags.get(name)
        if not value or value is True:
            raise UsageError(f"--{name} is required")
        return value

    @property
    def count(self) -> int:
        return self.int_flag("count", 0)

    @property
    def cursor(self) -> str | None:
        value = self.flags.get("cursor")
        return None if value in (None, True, "") else str(value)

    def upstream_body(self, exc: BaseException):
        """LinkedIn's answer to the request that raised `exc`, or None.

        Two conditions, and each one closes a measured way of attaching an
        unrelated payload to a failure and calling it the upstream response.
        That is this project's own failure mode - evidence the tool does not
        have - and it is worse than showing nothing, because the operator then
        diagnoses against the wrong body.

        * **The recording has to belong to the failing request.** `_WriteWatch`
          records every answer, so after a lookup succeeds and the POST after it
          is rejected with a 4xx, the last body in hand is the lookup's: the
          transport raised before the recorder ever ran. `requested` advances on
          every call and `answered` only when one comes back, so the two differ
          precisely when nothing was recorded for the request that failed.
        * **The exception has to have come from the wire.** A refusal this CLI
          made by itself - a bad flag, an open breaker, a spent cap, an
          unreadable ledger - has no upstream response at all, however many
          successful lookups preceded it.

        What survives both is the case the key exists for: a refusal that
        arrives inside a 200, recorded against the very request it refused.
        """
        if isinstance(exc, LOCAL_REFUSALS):
            return None
        if self.answered is None or self.answered != self.requested:
            return None
        return self.last_response

    def reset_client(self) -> None:
        """Drop the cached client so the next call picks up refreshed cookies."""
        if self._injected_client is None:
            self._client = None

    @property
    def client(self):
        """Built lazily so `auth seed` and `--help` never start a browser."""
        if self._client is None:
            self._client = _WriteWatch(self, self._injected_client or self._build_client())
        return self._client

    def _build_client(self):
        from . import browser

        return browser.BrowserClient(rate=self.rate, state=None)

    @property
    def rate(self) -> float:
        """Requests per second, clamped to a range that cannot disable pacing.

        `--rate=0` used to parse to a falsy interval, which skipped the pacer
        *and* skipped writing the timestamp it paces against - so the next
        invocation was unpaced too. Since the pivot removed the fingerprint
        problem, pacing is the only control left on behaviour, and the credential broker
        allowlist is granular to the verb rather than the flag, so it cannot
        block this from the outside.
        """
        raw = self.flags.get("rate")
        if raw in (None, True, ""):
            return 1.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise UsageError(f"--rate must be a number, not {raw!r}") from None
        if not 0 < value <= 1.0:
            raise UsageError(f"--rate must be greater than 0 and at most 1.0, not {value}")
        return value

    @property
    def member_urn(self) -> str:
        """The operator's `fsd_profile` urn, which messaging addresses mailboxes by.

        `me` hands back the `fs_miniProfile` form and messaging wants `fsd_profile`;
        two are the same id under different decorations - so the stored one is
        rewritten rather than spending a request to re-fetch what we already have.

        Discovering it costs a live call, which is one a `--dry-run` promised not
        to make - so an uncached one is refused rather than fetched, and named
        with the command that caches it.
        """
        if self._member_urn is None:
            stored = state.State().recall("member_urn")
            if isinstance(stored, str) and stored:
                self._member_urn = stored.replace("fs_miniProfile", "fsd_profile")
            elif self.flags.get("dry-run"):
                raise UsageError(
                    "a dry run issues no requests, and your member urn is not cached yet - "
                    "finding it would be a real call. Run `linkedin messages list` once "
                    "without --dry-run, which caches it, then preview the write."
                )
            else:
                found = profile.get_me(self.client).get("profile_urn")
                if not found:
                    raise UsageError(
                        "could not determine your member urn; run `linkedin auth seed`"
                    )
                state.State().remember("member_urn", found)
                self._member_urn = found
        return self._member_urn


def parse_flags(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    flags: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            rest.extend(argv[i + 1 :])
            break
        if token.startswith("--"):
            name, sep, value = token[2:].partition("=")
            if not name:
                raise UsageError("empty flag name")
            if name not in KNOWN_FLAGS:
                # Checked here rather than after dispatch: whether the *next*
                # token is this flag's value depends on knowing the flag, so an
                # unknown one cannot be parsed correctly to be reported later.
                raise UsageError(_unknown_flag(name))
            if sep:
                flags[name] = value
            elif name in BOOLEAN_FLAGS:
                flags[name] = True
            else:
                if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                    raise UsageError(f"--{name} expects a value")
                i += 1
                flags[name] = argv[i]
        elif token.startswith("-") and len(token) > 1 and not token[1].isdigit():
            # A bare `-c` is never valid here; saying so beats silently treating
            # it as a positional and failing somewhere less obvious.
            raise UsageError(f"unknown option {token!r} - this CLI has no short flags")
        else:
            rest.append(token)
        i += 1
    return flags, rest


def _unknown_flag(name: str) -> str:
    """Why this is not silence: an unknown flag used to be stored under a name
    nothing read, so `--tyep=PRAISE` reacted LIKE and reported success."""
    closest = FLAG_ALIASES.get(name) or next(
        iter(difflib.get_close_matches(name, sorted(KNOWN_FLAGS), 1, 0.6)), None
    )
    if closest:
        return f"unknown flag --{name}; did you mean --{closest}?"
    return f"unknown flag --{name}; run `linkedin --help` for the flags this CLI takes."


def _check_flags(verb: str, flags: dict[str, Any]) -> None:
    """Refuse a real flag aimed at a command that does not read it."""
    allowed = GLOBAL_FLAGS | COMMAND_FLAGS.get(verb, set())
    for name in flags:
        if name.startswith("_") or name in allowed:
            continue
        owners = sorted(other for other, own in COMMAND_FLAGS.items() if name in own)
        raise UsageError(
            f"--{name} is not a flag of `{verb}`"
            + (f"; it belongs to: {', '.join(owners)}." if owners else ".")
        )


def _action(ctx: Context, *allowed: str, default: str | None = None) -> str:
    """The subcommand of a verb/noun command, validated against what exists."""
    if not ctx.args:
        if default is not None:
            return default
        raise UsageError(f"missing action: expected {' | '.join(allowed)}")
    action = ctx.args[0]
    if action not in allowed:
        raise UsageError(f"unknown action {action!r}; expected {' | '.join(allowed)}")
    return action


# --------------------------------------------------------------------------- commands


def cmd_auth(ctx: Context):
    """Seed the managed browser profile, or report whether it is still signed in.

    There is deliberately no `export`. Its only possible output is a live `li_at`
    on stdout, which under an agent gateway is permanent model context - and dispatch here
    is granular to the verb, so a broker allowlist that permits `seed` could not
    have denied `export`.
    """
    from . import bootstrap

    action = ctx.arg(0, "seed|status")
    if action == "seed":
        source = ctx.flags.get("from-profile") or None
        if ctx.flag("dry-run"):
            # Nothing about seeding can be previewed for real: it reads a live
            # jar out of the operator's Chrome and then proves it with a signed-in
            # page load. Resolving which profile it would copy costs nothing and
            # is the part an operator gets wrong.
            return {
                "seeded": False,
                "would_copy_from": str(bootstrap.source_profile(source)),
                "note": (
                    "a dry run makes no request and copies no cookie; re-run without "
                    "--dry-run to seed the managed browser from that profile."
                ),
            }
        return bootstrap.seed(source)
    if action == "status":
        return bootstrap.status(ctx.client)
    raise UsageError(f"unknown auth action {action!r}")


def cmd_me(ctx: Context):
    return profile.get_me(ctx.client)


def cmd_profile(ctx: Context):
    _action(ctx, "get")
    who = ctx.args[1] if len(ctx.args) > 1 else None
    return profile.get_profile(ctx.client, who)


def cmd_feed(ctx: Context):
    _action(ctx, "list", default="list")
    items, cursor, more = feed.list_feed(
        ctx.client, count=ctx.int_flag("count", 10), cursor=ctx.cursor
    )
    return Page(items, cursor, more)


def cmd_post(ctx: Context):
    """Read a post, publish one, or take one back down.

    `create` is the only irreversible write in this CLI whose result is public
    the instant it lands, and its payload carries no dedupe token, so everything
    that can be refused without a request is refused before the ledger slot is
    claimed - see `surfaces/posts.py`.

    `delete` is its inverse and is booked as a *cleanup*, so no spent cap and no
    open breaker can withhold it. The concrete reason: a run that publishes and
    then trips its own daily cap would otherwise strand a live public post with
    no CLI way to remove it. `create` must not inherit that, which is what
    `CLEANUP_ACTIONS` being keyed verb -> actions buys.
    """
    action = _action(ctx, "get", "create", "delete")
    if action in ("get", "delete"):
        # `_check_flags` is granular to the verb, so `post get --visibility=…`
        # parses and is dropped. That is the same silence the allowlist exists to
        # end, and this is the flag it matters most for. `post delete --text=…`
        # is the same hole pointing at a verb that reads like an edit and is not.
        stray = sorted(name for name in ("text", "visibility") if ctx.flags.get(name))
        if stray:
            names = " and ".join(f"--{name}" for name in stray)
            does = "only reads a post back" if action == "get" else "only takes one down"
            raise UsageError(
                f"{names} {'are flags' if len(stray) > 1 else 'is a flag'} of `post create`, "
                f"not of `post {action}`, which {does}."
            )
    if action == "get":
        return feed.get_post(ctx.client, ctx.arg(1, "urn"))
    if action == "delete":
        urn = ctx.arg(1, "urn")
        dry_run = bool(ctx.flag("dry-run"))
        # A preview sends nothing, so there is nothing for `--yes` to confirm -
        # and demanding it there would remove the one way to inspect the request
        # before approving it.
        if not dry_run and not ctx.flag("yes"):
            raise UsageError(_CONFIRM_DELETE)
        return _write(
            ctx,
            "post",
            lambda: posts.delete(ctx.client, urn, dry_run=dry_run),
            cleanup=True,
        )

    text = ctx.require("text")
    # Validated before the claim: a refused audience must cost neither a write
    # slot nor a post, and `posts.audience` is where the two spellings the enum
    # accepts are enumerated.
    seen_by = posts.audience(ctx.flag("visibility"))
    if ctx.flag("idempotency-key"):
        # Refused because the share payload has no field to put a key in at all.
        # This used to be justified by contrast - `messages send` "honours this
        # because `createMessage` carries an `originToken` LinkedIn de-duplicates
        # on" - and that contrast was false: the captured messaging body sends
        # `"dedupeByClientGeneratedToken": false` (docs/write-payloads.md), so
        # LinkedIn is told not to dedupe on the token. The refusal here is right
        # either way, and arguably more so: there is no idempotent write in this
        # CLI, and the caller passing this flag is the one that intends to retry.
        raise UsageError(
            "`post create` cannot honour --idempotency-key: the captured payload carries "
            "no dedupe token, so LinkedIn has no way to recognise a repeat and a second "
            "attempt publishes a second post. Send it once, then read your recent "
            "activity to find out whether it landed."
        )
    return _write(
        ctx,
        "post",
        lambda: posts.create(
            ctx.client, text, visibility=seen_by, dry_run=bool(ctx.flag("dry-run"))
        ),
    )


def cmd_messages(ctx: Context):
    """Read the mailbox, and the three writes that go through it.

    All three - `send`, `reply` and `mark-all-read` - are booked against
    `DAILY_CAPS["message"]`. They used to call the surface directly, so the cap
    was enforced nowhere and the only bound on an agent sending DMs was the
    1 req/s pacer.
    """
    action = _action(
        ctx,
        "list",
        "read",
        "counts",
        "send",
        "reply",
        "mark-read",
        "mark-all-read",
        default="list",
    )

    if action == "list":
        items, cursor, more = messaging.list_conversations(
            ctx.client,
            ctx.member_urn,
            count=ctx.int_flag("count", 20),
            cursor=ctx.cursor,
            unread_only=bool(ctx.flag("unread-only")),
        )
        return Page(items, cursor, more)

    if action == "read":
        items, cursor, more = messaging.read_conversation(
            ctx.client,
            ctx.arg(1, "conversation-urn"),
            count=ctx.int_flag("count", 20),
            cursor=ctx.cursor,
        )
        return Page(items, cursor, more)

    if action == "counts":
        return messaging.mailbox_counts(ctx.client, ctx.member_urn)

    if action == "mark-read":
        raise UsageError(
            "`messages mark-read` never marked one conversation read: the captured "
            "payload is `markAllMessagesAsSeen`, which takes a timestamp and no "
            "conversation, so the urn was accepted and dropped and every other unread "
            "thread was marked seen too. It is now `messages mark-all-read --yes`, which "
            "says so. A per-conversation payload has not been captured from a live "
            "session yet; see docs/write-payloads.md."
        )

    if action == "mark-all-read":
        if len(ctx.args) > 1:
            raise UsageError(
                f"`messages mark-all-read` takes no argument, and {ctx.args[1]!r} looks like "
                "one: this marks the whole mailbox seen, not a single thread. Marking one "
                "conversation read needs a payload that has not been captured from a live "
                "session yet (docs/write-payloads.md)."
            )
        dry_run = bool(ctx.flag("dry-run"))
        if not dry_run and not ctx.flag("yes"):
            # An opt-in rather than a confirmation prompt: the caller is a
            # program, and one that cannot spell `--yes` should not be able to
            # reach an irreversible mailbox-wide write by accident.
            raise UsageError(
                "`messages mark-all-read` marks every unread conversation seen, including "
                "threads nobody has answered, and LinkedIn offers no undo. Pass --yes to "
                "confirm it, or --dry-run to see the request first."
            )
        return _write(ctx, "message", lambda: messaging.mark_all_read(ctx.client, dry_run=dry_run))

    if action == "reply":
        conversation = ctx.arg(1, "conversation-urn")
        text = ctx.require("text")
        return _write(
            ctx,
            "message",
            lambda: messaging.send_message(
                ctx.client,
                ctx.member_urn,
                conversation,
                text,
                idempotency_key=ctx.flag("idempotency-key"),
                dry_run=bool(ctx.flag("dry-run")),
            ),
        )

    # `send` needs a conversation to exist first: LinkedIn requires a
    # conversationUrn even for a brand-new thread, so an existing thread with the
    # recipient is looked up before falling back to an explicit --conversation.
    recipient = ctx.require("to")
    text = ctx.require("text")
    conversation = ctx.flag("conversation")
    if not conversation:
        if ctx.flag("dry-run"):
            # Two requests stand between here and the send, and a preview that
            # issued them would not be a preview. Inventing a conversation urn
            # instead would be worse: the operator would approve a body that is
            # not the one that would go out.
            raise UsageError(
                f"a dry run issues no requests, so it cannot look up the thread with "
                f"{recipient!r}. Pass --conversation=<urn> to preview the exact body, or "
                "drop --dry-run."
            )
        # `resolve_urn` raises rather than answering `None` for a recipient it
        # could not identify, so an unresolvable `--to` now exits 2 from the
        # resolver instead of falling through to the "no existing conversation"
        # refusal below. That is the message to keep: "I could not tell which
        # member you meant" and "I found them and you have never spoken" are
        # different problems with different remedies, and the second one used to
        # be printed for both.
        conversation = messaging.find_conversation_with(
            ctx.client, ctx.member_urn, profile.resolve_urn(ctx.client, recipient)
        )
    if not conversation:
        raise UsageError(
            f"no existing conversation with {recipient!r}. LinkedIn requires a "
            "conversationUrn even for a new thread; open the thread once in the "
            "browser, or pass --conversation=<urn>."
        )
    return _write(
        ctx,
        "message",
        lambda: messaging.send_message(
            ctx.client,
            ctx.member_urn,
            conversation,
            text,
            idempotency_key=ctx.flag("idempotency-key"),
            dry_run=bool(ctx.flag("dry-run")),
        ),
    )


def cmd_react(ctx: Context):
    return _write(
        ctx,
        "react",
        lambda: social.react(
            ctx.client,
            ctx.arg(0, "activity-urn"),
            reaction=str(ctx.flag("type") or "LIKE"),
            dry_run=bool(ctx.flag("dry-run")),
        ),
    )


def cmd_unreact(ctx: Context):
    # Booked against `react` so the counter an operator set is the counter this
    # is measured against - but as a cleanup, so it is recorded without being
    # refusable. Undoing a reaction is still traffic and still counts; what it
    # must never be is *blocked*, because the run holding a live reaction it
    # wants back is exactly the run that has already spent its budget.
    return _write(
        ctx,
        "react",
        lambda: social.unreact(
            ctx.client, ctx.arg(0, "activity-urn"), dry_run=bool(ctx.flag("dry-run"))
        ),
        cleanup=True,
    )


def cmd_comment(ctx: Context):
    """Comment on a post, or take one of this account's comments back down.

    `delete` is the inverse and is booked as a *cleanup*, so no spent cap and no
    open breaker can withhold it - the same argument `post delete` makes, and the
    same concrete failure: the run that has just commented and then tripped its
    own daily cap is exactly the run holding a live comment it wants back.
    `comment` itself must not inherit the exemption, which is what
    `CLEANUP_ACTIONS` being keyed verb -> actions buys.

    No `--yes` here, and the asymmetry with `post delete` is deliberate rather
    than an omission. That opt-in guards a write that takes a post's whole
    comment thread and every reaction on it down with it; this removes one
    comment this account wrote. Gating the cheaper half of that trade behind a
    flag pushes a caller who cannot spell it toward leaving the mess live, which
    is the outcome the exemption above exists to prevent.
    """
    if ctx.args and ctx.args[0] == "delete":
        # `_check_flags` is granular to the verb, so `comment delete --text=...`
        # parses and is dropped - the same silence the allowlist exists to end,
        # pointing at a verb that reads like an edit and is really a removal.
        if ctx.flags.get("text"):
            raise UsageError(
                "--text is a flag of `comment`, not of `comment delete`, which only takes "
                "a comment down. There is no verb here that edits one."
            )
        return _write(
            ctx,
            "comment",
            lambda: social.delete_comment(
                ctx.client, ctx.arg(1, "comment-urn"), dry_run=bool(ctx.flag("dry-run"))
            ),
            cleanup=True,
        )
    return _write(
        ctx,
        "comment",
        lambda: social.comment(
            ctx.client,
            ctx.arg(0, "activity-urn"),
            ctx.require("text"),
            dry_run=bool(ctx.flag("dry-run")),
        ),
    )


def cmd_invite(ctx: Context):
    """Send a connection request. Taking one back is a payload nobody captured.

    Three things about this handler are load-bearing.

    * **It is booked against `DAILY_CAPS["invite"]`.** That key existed with
      nothing writing to it, so the only bound on an agent firing connection
      requests was the 1 req/s pacer - and unlike a reaction or a comment, each
      one of these arrives in a stranger's notifications.
    * **A public id is resolved here, not in the surface.** Writes are addressed
      by urn and an agent that has read a feed or a notification is holding a
      name or a link, so refusing to bridge the two would make the verb
      unreachable in practice. The lookup is a real request, which is why a
      `--dry-run` refuses it rather than making it: inventing a urn for the
      preview would have an operator approving a body naming somebody other than
      whoever the invitation would really reach.
    * **`invite withdraw` is a stub, and it is the verb wanted most.** Nobody
      goes looking for it until an invitation has already gone to the wrong
      person, so it says what is missing and how to capture it instead of
      answering "unknown action".
    """
    if ctx.args and ctx.args[0] == "withdraw":
        raise UsageError(invitations.NO_WITHDRAW)
    if ctx.flag("note"):
        # Before the argument is even read: a refusal that costs no request also
        # costs no ledger slot, and this one is certain without asking LinkedIn.
        raise UsageError(_NO_INVITE_NOTE)

    who = ctx.arg(0, "public-id-or-url")
    dry_run = bool(ctx.flag("dry-run"))
    public_id = None
    if who.strip().startswith(invitations.URN_PREFIX):
        # Any urn, not just the right one. A caller holding `urn:li:activity:…`
        # is not making a public-id mistake, and `resolve_public_id` would tell
        # them it is "not a usable public id" - true, and no help.
        target = invitations.profile_urn(who)
    elif dry_run:
        raise UsageError(
            f"a dry run issues no requests, so it cannot resolve {who!r} to the member urn "
            "this write is addressed by. Run `linkedin profile get` on them once without "
            "--dry-run and pass the `profile_urn` it reports, or drop --dry-run."
        )
    else:
        # Validated first, so a company or school URL is refused before a slot is
        # claimed and before anything is asked of LinkedIn.
        public_id = profile.resolve_public_id(who)
        # No `if not target` behind this. `resolve_urn` used to answer `None` for
        # a lookup it could not read and this handler turned that into a refusal;
        # it now raises a `ValueError` naming the public id, which
        # `ERROR_SLUGS` maps to the same exit 2 - so the branch survived only as
        # code that could never run. The refusal has to stay in the resolver
        # rather than here: it is the only place that knows *why* an answer was
        # not understood, and "two members claim this public id" is not the same
        # refusal as "nothing in the answer was a profile".
        target = profile.resolve_urn(ctx.client, public_id)
    return _write(
        ctx,
        "invite",
        lambda: invitations.invite(ctx.client, target, public_id=public_id, dry_run=dry_run),
    )


def _write(ctx: Context, kind: str, write: Callable[[], Any], *, cleanup: bool = False):
    """Put a write through the on-disk ledger: claim a slot, then settle it.

    The caps are cross-process (`state.DAILY_CAPS`) because every invocation is
    a fresh process, so an in-memory counter would enforce nothing against the
    agent loop the caps exist for. `claim_write` takes the slot and books it
    under one lock; checking and recording separately let two invocations both
    read "one below the cap" and both write.

    Three details carry the weight:

    * The slot is *claimed before* the request and given back only if nothing
      was sent. `ctx.attempted_write` is what `_WriteWatch` sets when a POST is
      really issued, so an argument this CLI rejects locally - a share urn where
      an activity urn was needed - costs nothing, while a write whose response
      proved nothing still counts. LinkedIn may well have applied that one, and
      the cap is on what this client *sends*, not on what it can confirm.
    * `--dry-run` neither claims a slot nor spends one. Nothing is issued, so
      there is nothing to charge for - and refusing to *preview* a write because
      the day's budget is gone would take away the one command that costs
      nothing to run at exactly the moment it is most useful.
    * `cleanup=True` is an undo of something already visible to real people, and
      "never refused" is not what it buys. It is exempt from the daily caps, from
      the throttle cooldown and from an open breaker, because a run that trips
      one of those mid-way has to be able to take back what it already did. It is
      *not* exempt from arithmetic: `state._guard_cleanup` refuses a cleanup past
      `state.cleanup_ceiling` - five days' worth of creates, tightened to one
      day's while the breaker is open and counted from the block rather than from
      the start of the day - because that many undos in 24h is a wedged loop
      rather than a cleanup, and it exits 5 like any other spent budget. A
      cleanup is refused outright by `state._guard_readable` too, which is exit
      9: the ceiling is counted out of the ledger, so a ledger this process
      cannot parse would turn the exemption into an unmetered write channel.
    """
    if ctx.flag("dry-run"):
        return write()

    claim = state.State().claim_write(kind, cleanup=cleanup)
    try:
        return write()
    finally:
        # `_safe` on the release, because this runs in a `finally` and a raising
        # ledger would *replace* the write's own outcome: the caller would get a
        # disk error instead of the one message telling it what happened to the
        # request. Over-counting one write is the smaller failure.
        if ctx.attempted_write:
            claim.commit()
        else:
            _safe(claim.release)


def cmd_notifications(ctx: Context):
    action = _action(ctx, "list", "mark-read", default="list")
    if action != "list":
        raise UsageError(_NOT_CAPTURED.format(what="notifications mark-read"))

    items, cursor, more = notifications.list_notifications(
        ctx.client,
        count=ctx.int_flag("count", 10),
        cursor=ctx.cursor,
        unread_only=bool(ctx.flag("unread-only")),
    )
    return Page(items, cursor, more)


def cmd_invitations(ctx: Context):
    """List the invitations you were sent. The ones *you* sent are not readable.

    One verb, two halves, and the split is LinkedIn's rather than this CLI's.
    `relationships/invitationViews?q=receivedInvitation` was verified live on
    a live run against three agreeing reads and it ships. No route for the sent
    side survived: seven spellings were probed, three answered 404 and four 400,
    because that screen is server-driven UI now and its data is rendered into the
    document instead of fetched. Both facts are recorded in
    docs/sdui-migration.md, and `invitations.NO_SENT_LIST` carries the evidence
    to whoever asks for it.

    `sent` is dispatched and then refused, rather than answered with "unknown
    action". The caller asking for it is not making a typo, and being told the
    action does not exist would send them looking for the right spelling of
    something that has none.
    """
    action = _action(ctx, "list", "sent", default="list")
    if action == "sent":
        raise UsageError(invitations.NO_SENT_LIST)
    items, cursor, more = invitations.list_received(
        ctx.client,
        count=ctx.int_flag("count", invitations.RECEIVED_DEFAULT_COUNT),
        cursor=ctx.cursor,
    )
    return Page(items, cursor, more)


# ----------------------------------------------------------------------- doctor


def _probe_me(ctx: Context, client) -> None:
    profile.get_me(client)


def _probe_profile(ctx: Context, client) -> None:
    profile.project_profile(profile.fetch_by_urn(client, ctx.member_urn))


def _probe_feed(ctx: Context, client) -> None:
    feed.list_feed(client, count=1)


def _probe_messages(ctx: Context, client) -> None:
    messaging.list_conversations(client, ctx.member_urn, count=1)


def _probe_notifications(ctx: Context, client) -> None:
    notifications.list_notifications(client, count=1)


PROBES: tuple[tuple[str, Callable[[Context, Any], None]], ...] = (
    ("me", _probe_me),
    ("profile", _probe_profile),
    ("feed", _probe_feed),
    ("messages", _probe_messages),
    ("notifications", _probe_notifications),
)

# The surfaces whose calls are addressed by a rotating content hash. A surface
# added here needs `QUERY_IDS` and `query_id`, which is the pair every one of
# them already exposes.
QUERY_ID_SURFACES = (messaging, posts, social)

QUERY_ID_RECIPE = (
    "queryIds are content hashes that rotate on LinkedIn's deploys. To refresh "
    "one: open LinkedIn in Chrome, navigate *within* the app (a cold page load "
    "issues no Voyager calls), copy the `queryId` parameter out of the request in "
    "the Network tab, and export it as the `override_env` variable below."
)

# The surfaces addressed by a versioned decoration instead of a content hash.
# Kept apart from `QUERY_ID_SURFACES` rather than merged into it: the two fail
# for different reasons and are refreshed by different steps, and one list
# holding both would send an operator to the wrong instructions.
DECORATION_ID_SURFACES = (invitations,)

DECORATION_ID_RECIPE = (
    "decorationIds name a versioned response schema rather than a content hash, "
    "so they survive an ordinary deploy - but LinkedIn does retire old versions, "
    "and a retired one fails the way a stale queryId does. To refresh one: open "
    "LinkedIn in Chrome, perform the action, copy the `decorationId` parameter out "
    "of the request in the Network tab, and export it as the `override_env` "
    "variable below. Only the ids listed here are overridable - the others are "
    "pinned in the surface that owns them."
)


def cmd_doctor(ctx: Context):
    """Exercise one call per surface and report what is broken.

    This must never raise. Reporting breakage is the whole job, and a doctor that
    dies on the first broken surface tells the operator strictly less than the
    command they already ran. It also deliberately does *not* trip the breaker:
    its own probes are the diagnostic, and blocking the next `doctor` run on
    their result would take away the only tool left.
    """
    ledger = state.State()
    if ctx.flag("clear-breaker"):
        ledger.clear_breaker()

    client, failure = None, None
    try:
        client = ctx.client
    except Exception as exc:  # noqa: BLE001 - every failure is a reportable result
        failure = exc

    surfaces = []
    for name, probe in PROBES:
        error = failure
        if error is None:
            try:
                probe(ctx, client)
            except Exception as exc:  # noqa: BLE001 - see the docstring
                error = exc
        slug, _, _ = _report(error) if error is not None else (None, 0, False)
        surfaces.append(
            {
                "surface": name,
                "ok": error is None,
                "error": slug,
                "message": str(error) if error is not None else None,
            }
        )

    return {
        "browser": _safe(lambda: supervisor.request({"op": "status"}, autostart=False)),
        "breaker": ledger.breaker_state(),
        # Reported, never spent and never cleared. Until this existed the counts
        # were readable only by opening `state.json` on the host, so the only way
        # an agent could discover a budget was gone was to spend it and read exit
        # 5 - which is a diagnosis that costs the thing being diagnosed.
        "ledger": _safe(ledger.ledger_state),
        # Reported, never cleared - not even by `--clear-breaker`. An agent can
        # run doctor too, and clearing the cooldown it just earned from LinkedIn
        # is the loop the cooldown exists to stop. It expires on its own.
        "throttle": _safe(ledger.throttle_state),
        "surfaces": surfaces,
        "query_ids": {
            name: {
                "value": surface.query_id(name),
                "override_env": f"LINKEDIN_QUERY_ID_{name.upper()}",
            }
            # Every surface holding rotating hashes, not just messaging: exit 7
            # tells the operator to run doctor, and a doctor that cannot name the
            # id that rotated is a dead end. `query_id` is resolved per surface
            # so the report shows the override already in effect, not the shipped
            # value it replaced.
            for surface in QUERY_ID_SURFACES
            for name in sorted(surface.QUERY_IDS)
        },
        "query_id_recipe": QUERY_ID_RECIPE,
        # `invite` is addressed by a decoration and carries no queryId at all, so
        # it would appear nowhere above - and a doctor that cannot name the id
        # that went stale is a dead end for the write that depends on it.
        "decoration_ids": {
            name: {
                "value": surface.decoration_id(name),
                "override_env": f"LINKEDIN_DECORATION_ID_{name.upper()}",
            }
            for surface in DECORATION_ID_SURFACES
            for name in sorted(surface.DECORATION_IDS)
        },
        "decoration_id_recipe": DECORATION_ID_RECIPE,
    }


def _safe(thunk):
    try:
        return thunk()
    except Exception:  # noqa: BLE001 - doctor reports, it does not fail
        return None


COMMANDS: dict[str, Callable[[Context], Any]] = {
    "auth": cmd_auth,
    "me": cmd_me,
    "profile": cmd_profile,
    "feed": cmd_feed,
    "post": cmd_post,
    "messages": cmd_messages,
    "react": cmd_react,
    "unreact": cmd_unreact,
    "comment": cmd_comment,
    "invite": cmd_invite,
    "invitations": cmd_invitations,
    "notifications": cmd_notifications,
    "doctor": cmd_doctor,
}


# ------------------------------------------------------------------ error mapping

# Three unrelated families end up here. `state.Blocked` and `transport.Blocked`
# are distinct classes that mean the same thing - one is raised by the ledger
# when the breaker is already open, the other by the transport when LinkedIn
# says 999 - and collapsing them into one class would make `state` import
# `transport`, which `transport` cannot afford. Both are exit 9; the duplication
# is deliberate and lives only in this table.
#
# No code in it means "retry", and exit 6 least of all. It is the general
# upstream failure, so it carries both a request that never landed and a write
# whose outcome the response did not establish - `surfaces/messaging.py`
# `_unconfirmed`, where a repeat is a second message in a real person's thread
# that nothing here can unsend. `retryable` in the envelope is the only retry
# answer this CLI gives, which is why that one is set per exception rather than
# derived from the code beside it.
ERROR_SLUGS: tuple[tuple[type[BaseException], str, int], ...] = (
    (transport.SessionExpired, "session_expired", 3),
    (transport.NotFound, "not_found", 4),
    (transport.RateLimited, "rate_limited", 5),
    (transport.StaleQueryId, "stale_query_id", 7),
    (transport.Blocked, "blocked", 9),
    (transport.OutcomeUnknown, "outcome_unknown", 6),
    # Ahead of `UpstreamError`, which it subclasses. LinkedIn checks its own
    # invitation quota on `verifyQuotaAndCreateV2`, so a refusal is a real answer
    # about the account rather than a failed request - exit 5 says "wait", which
    # is what a spent quota means, where exit 6 says only that something upstream
    # went wrong and leaves what to do next to `retryable`.
    (invitations.InvitationQuotaExceeded, "invite_quota_exceeded", 5),
    (transport.UpstreamError, "upstream", 6),
    # Ahead of `state.Blocked`, which it subclasses. Same exit code, different
    # remedy: a tripped breaker is cleared with `auth seed` and `doctor
    # --clear-breaker`, while a ledger this client cannot read is one file on
    # this host and nothing about waiting or re-authenticating touches it. An
    # agent that could only tell them apart by parsing the message would end up
    # doing the wrong one of the two.
    (state.LedgerUnreadable, "ledger_unreadable", 9),
    (state.Blocked, "blocked", 9),
    (state.WriteQuotaExceeded, "write_quota_exceeded", 5),
    (UsageError, "usage", 2),
    # A surface raises ValueError for input it can reject without a request -
    # a company URL where a profile was wanted. That is the caller's mistake,
    # so it is exit 2, not an upstream failure.
    (ValueError, "usage", 2),
)


def _report(exc: BaseException) -> tuple[str, int, bool]:
    for kind, slug, code in ERROR_SLUGS:
        if isinstance(exc, kind):
            return slug, code, bool(getattr(exc, "retryable", False))
    return "upstream", 6, False


def main(argv: list[str] | None = None, stdout=None, stderr=None, client=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    try:
        flags, rest = parse_flags(argv)
    except UsageError as exc:
        render.emit(render.err("usage", str(exc)), stderr)
        return 2

    if flags.get("help") or (not rest and not flags):
        stdout.write(USAGE)
        return 0 if flags.get("help") else 2

    if not rest:
        render.emit(render.err("usage", "no command given"), stderr)
        return 2

    name, args = rest[0], rest[1:]
    handler = COMMANDS.get(name)
    if handler is None:
        render.emit(render.err("usage", f"unknown command {name!r}; try `linkedin --help`"), stderr)
        return 2

    flags["_verb"] = name
    ctx = Context(args=args, flags=flags, stdout=stdout, stderr=stderr, _injected_client=client)
    # `--text` is a *content* flag (reply/comment/post create), so the output
    # format lives on `--format` instead. Overloading one name silently rendered
    # write results as prose the first time a message was sent.
    fmt = str(flags.get("format") or "json").lower()
    if fmt not in ("json", "text"):
        render.emit(render.err("usage", f"--format must be json or text, got {fmt!r}"), stderr)
        return 2
    mode = fmt

    try:
        ctx.rate  # validated before anything is dispatched
        _check_flags(name, flags)
        _guard_breaker(name, args)
        result = handler(ctx)
    except _DryRunStop as stop:
        # The command needed a read to get any further, and a dry run does not
        # read. What it would have asked for is the answer.
        render.emit(render.ok(stop.preview), stdout, mode=mode, raw=bool(flags.get("raw")))
        return 0
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        slug, code, retryable = _report(exc)
        if isinstance(exc, transport.SessionExpired) and name not in BREAKER_EXEMPT:
            # A dead session used to be re-acquired and retried. The browser owns
            # rotation now, so a SessionExpired that survives to here means the
            # profile is genuinely signed out - and the remedy, `auth seed`, is
            # something an agent cannot perform for itself. Left uncounted, an
            # agent that retries on exit 3 keeps calling a session LinkedIn has
            # already ended, which is the documented road from a soft block to a
            # restriction.
            _safe(lambda: state.State().count_session_failure())
        if isinstance(exc, transport.RateLimited):
            # Persisted for the same reason the breaker is: a raise ends this
            # process, and the next invocation would otherwise start as if it
            # had never been told to slow down. The ledger turns it into a
            # cooldown that refuses ordinary writes and slows the reads.
            reason = str(exc)
            _safe(lambda: state.State().record_throttle(reason))
        if isinstance(exc, transport.Blocked):
            # Arm the breaker before reporting: this process is ending either
            # way, and the point is to stop the *next* one. The reason is bound
            # out of `exc` first, which Python unbinds at the end of the block.
            reason = str(exc)
            _safe(lambda: state.State().trip_breaker(reason))
        # `--raw` used to change nothing on the path it is most needed on: the
        # `--visibility=CONNECTIONS` refusal of a live run came back as an HTTP
        # 200 with a GraphQL `errors` array, the surface reduced it to one
        # sentence, and diagnosing it took a hand-written script
        # (docs/incidents.md). The body is attached only when
        # it was asked for, is redacted by `render.err`, and neither replaces the
        # error nor moves the exit code.
        #
        # Which body, though, is `Context.upstream_body`'s question and not this
        # one's: attaching whatever came back last put an unrelated payload under
        # "upstream response:" on every failure that never reached the wire.
        #
        # No `mode=` here, deliberately: every error in this function is emitted
        # as JSON whatever `--format` says, and quietly making one of them
        # respect `--format=text` would change the shape of the failure output an
        # agent already parses. `render.to_text` renders the body for a caller
        # that does ask for it.
        body = ctx.upstream_body(exc) if flags.get("raw") else None
        render.emit(render.err(slug, str(exc), retryable, body), stderr)
        return code

    if name not in BREAKER_EXEMPT:
        _safe(lambda: state.State().clear_session_failures())

    if isinstance(result, Page):
        envelope = render.ok(result.items, result.next_cursor, result.has_more)
    else:
        envelope = render.ok(result)
    render.emit(envelope, stdout, mode=mode, raw=bool(flags.get("raw")))
    return 0


class _DryRunStop(BaseException):
    """A request a `--dry-run` would have had to issue, carrying its preview.

    A `BaseException` on purpose. `cmd_doctor` and `_safe` both swallow
    `Exception` by design, and every surface is free to catch one; a dry run
    caught there would carry on and issue the request it exists to refuse.
    """

    def __init__(self, preview):
        super().__init__("a dry run issues no requests")
        self.preview = preview


class _WriteWatch:
    """Passes calls through, recording whether a write was attempted.

    A rotation raised on a POST leaves the outcome unknown, so the retry path
    needs to know a write was in flight and refuse to replay it.

    It is also where `--dry-run` becomes a property of the *process* rather than
    of each call site. The writes threaded the flag through themselves, so a
    lookup a write needed first - resolving a recipient, finding a thread - went
    out for real under a flag that says nothing does.
    """

    def __init__(self, ctx: "Context", inner):
        self._ctx = ctx
        self._inner = inner

    @property
    def inner(self):
        """The real client underneath, for callers that care about its type."""
        return self._inner

    def get(self, path, dry_run: bool = False):
        if not dry_run and self._ctx.flag("dry-run"):
            raise _DryRunStop(self._inner.get(path, True))
        self._begun(dry_run)
        return self._seen(self._inner.get(path, dry_run), dry_run)

    def post(self, path, body, dry_run: bool = False):
        if not dry_run:
            self._ctx.attempted_write = True
        self._begun(dry_run)
        return self._seen(self._inner.post(path, body, dry_run), dry_run)

    def _request(self, method, path, body=None, dry_run: bool = False):
        """The seam both transports build every call on, DELETE included.

        Wrapped rather than left to `__getattr__`, which is what used to happen:
        the forwarded call reached the real client unwatched, so `comment delete`
        never set `attempted_write` - `_write` handed back the ledger slot it had
        claimed and the undo went out uncounted - and `--raw` had no body to show
        for a failure that arrived in a 2xx. `get` and `post` are delegated
        rather than duplicated so a caller reaching this way gets exactly the
        `--dry-run` interception and write accounting it would have got by name.
        """
        if method == "GET":
            return self.get(path, dry_run)
        if method == "POST":
            return self.post(path, body, dry_run)
        if not dry_run:
            self._ctx.attempted_write = True
        self._begun(dry_run)
        return self._seen(self._inner._request(method, path, body, dry_run), dry_run)

    def _begun(self, dry_run: bool) -> None:
        """Count a request as started, before it has had a chance to fail.

        The half that has to happen first. A 4xx raises inside the transport, so
        the recorder below never runs and the newest body in hand is the
        *previous* request's - which `--raw` then attached to the failure and
        labelled the upstream response. Counted here, the two numbers disagree
        and `Context.upstream_body` says there is nothing to show.
        """
        if not dry_run:
            self._ctx.requested += 1

    def _seen(self, result, dry_run: bool):
        """Remember the real answer, so `--raw` has one to show if this fails.

        Stamped with the request it answers, for the same reason `_begun` counts:
        a body is evidence only about the request that produced it.

        A dry run is skipped: what comes back from one is this CLI's own preview
        of a request, and reporting it as the upstream response would be the
        exact confusion the whole preview mechanism exists to avoid.
        """
        if not dry_run:
            self._ctx.last_response = result
            self._ctx.answered = self._ctx.requested
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _breaker_verdict() -> tuple[dict | None, bool]:
    """The open breaker if there is one, and whether the ledger answered at all.

    Two questions and one answer, because the second decides whether the first
    means anything. `state.State.breaker_state` reads through `state._read`,
    which *fails open* by design - a ledger that will not parse hands back an
    empty dict so that the CLI keeps working - and an empty dict has no breaker
    in it whether or not the file did. So "no breaker" and "no idea" arrive here
    spelled identically, and `ledger_state()["readable"]` is the only public way
    to tell them apart.
    """
    ledger = state.State()
    return ledger.breaker_state(), bool(ledger.ledger_state().get("readable"))


def _guard_breaker(name: str, args: list[str]) -> None:
    """Refuse a command the on-disk breaker forbids - or cannot be asked about.

    This used to read the breaker through `_safe`, which swallows every
    exception and answers `None` - the same answer a *closed* breaker gives. So
    a ledger the process could not reach (measured with `LINKEDIN_STATE_FILE`
    pointed under a directory that cannot exist) disarmed the breaker entirely,
    and a client LinkedIn had already blocked went on calling LinkedIn. That is
    the documented road from a soft block to a restriction, and the failure is
    silent by construction: nothing about it is visible in the run's own output.

    So the answer is now three-valued, and the split is write/read rather than
    verb/verb. A write is refused when the breaker cannot be verified, because a
    write is what turns a soft block into a restriction and what reaches another
    human. A read is let through, because a read is how the operator finds out
    what happened - `post get` on the urn a half-finished write returned is the
    read-back this project's incident record turns on - and a CLI that will not
    look at the account over a local file sends its operator to the browser at
    the worst possible moment.

    The policy is `state._guard_readable`'s, one layer earlier and by design:
    that guard refuses a write, and a cleanup, claimed against a ledger it
    could not read. Refusing here as well is not redundancy - `claim_write` runs
    after the lookups a write does first (`invite` resolves a public id,
    `messages send` finds the thread), and those are requests from exactly the
    client that must stop calling.
    """
    if name in BREAKER_EXEMPT:
        return
    try:
        breaker, readable = _breaker_verdict()
    except Exception as exc:  # noqa: BLE001 - any failure is the same verdict
        if is_write(name, args):
            raise _unverifiable_breaker() from exc
        return
    if not readable:
        if is_write(name, args):
            raise _unverifiable_breaker()
        return
    # An undo is let through for the same reason the ledger lets it through: the
    # breaker opens *after* writes have already gone out, and the command that
    # takes them back is the one thing that still needs to reach LinkedIn. Note
    # where this sits - *after* the readability check, not before it. A cleanup
    # is exempt from an open breaker, never from an unreadable ledger: its own
    # bound is `state.cleanup_ceiling`, counted out of that same file, so an
    # exemption there would be an unmetered write channel rather than an undo.
    if is_cleanup(name, args):
        return
    if breaker:
        raise state.Blocked(
            f"LinkedIn blocked this client ({breaker.get('reason')}) and the circuit breaker "
            "is open, so this command was not sent. Clear the challenge in the browser, run "
            "`linkedin auth seed`, then `linkedin doctor --clear-breaker`."
        )


def _unverifiable_breaker() -> state.LedgerUnreadable:
    """The refusal, naming the resolved path and the command that ends it.

    `state.LedgerUnreadable` rather than a new class, so that a caller sees one
    code for one cause however it was discovered - here, or at the claim. The
    resolved path is in the text because `LINKEDIN_STATE_FILE` relocates the
    ledger per invocation under the credential broker, and the agent reading this
    can see neither the host nor the environment it ran under.
    """
    path = shlex.quote(str(state.resolve_path()))
    return state.LedgerUnreadable(
        f"the write ledger at {path} could not be read, so whether LinkedIn has already "
        "blocked this client is unknown - and an unreadable ledger is indistinguishable from "
        "a closed breaker, which is why this refuses rather than assuming. Reads still work; "
        "writes are refused until the file answers again. Check the path and its permissions "
        "first, and if the file itself is the problem, move it aside and let the next run "
        f"recreate it, deliberately discarding whatever history it held: mv {path} {path}.corrupt"
    )


if __name__ == "__main__":
    raise SystemExit(main())
