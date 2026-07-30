"""CLI parsing, dispatch and exit codes.

The exit code and the envelope are the contract an agent actually programs
against, so they are pinned harder than the human-readable text.

Three things this file guards that are new since the browser pivot:

* **`--rate` is validated here or nowhere.** Pacing is the only behavioural
  control left after the transport moved into a browser, and the credential
  broker's allowlist is granular to the verb rather than the flag, so nothing
  outside this process can stop an agent passing `--rate=0`.
* **A `session_expired` that reaches the top is terminal.** There is no re-sync
  any more; the remedy is `auth seed`, which an agent cannot run for itself. So
  the failures are counted and the breaker arms rather than letting a retry loop
  keep calling a session LinkedIn has already ended.
* **Every remedy the CLI prints has to name a command that exists.** `auth sync`
  and `--transport=direct` are deleted, and an error telling an agent to run a
  command `COMMANDS` no longer dispatches is worse than an error with no remedy,
  because the agent retries it.

Nothing here touches the network, a socket or a browser: dispatch tests inject a
`FakeClient`, and the few end-to-end tests inject a real `BrowserClient` whose
one seam onto the supervisor is a function.
"""

import ast
import functools
import importlib
import inspect
import io
import json
import re
import time
from pathlib import Path

import pytest

import linkedin_cli
from linkedin_cli import browser, cli, render, state, transport
from linkedin_cli.surfaces import invitations, messaging, social


def run(argv, **kw):
    out, errs = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=errs, **kw)
    return code, out.getvalue(), errs.getvalue()


def envelope(text):
    return json.loads(text)


class FakeClient:
    """Records the paths a command asks for and replays canned payloads.

    `_request` is here for the same reason it is on the surface fakes: both real
    transports expose only `get` and `post` over a shared
    `_request(method, path, body, dry_run)`, and `comment delete` is the CLI's
    first DELETE. A fake with a friendlier seam would test a client the CLI does
    not have.
    """

    def __init__(self, *payloads, error=None):
        self.payloads = list(payloads)
        self.paths: list[str] = []
        self.posts: list[tuple] = []
        self.requests: list[tuple] = []
        self.error = error

    def get(self, path: str, dry_run: bool = False):
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return self.payloads.pop(0) if self.payloads else {}

    def post(self, path: str, body, dry_run: bool = False):
        self.posts.append((path, body, dry_run))
        return self._request("POST", path, body, dry_run)

    def _request(self, method: str, path: str, body, dry_run: bool = False):
        self.requests.append((method, path, body, dry_run))
        return self.get(path, dry_run)


class NoPace:
    """The cross-process pacer with the sleep taken out.

    Injected wherever a real `BrowserClient` is used, because the real one
    sleeps on a wall clock and a test that waits a second per request is a test
    people start skipping.
    """

    def __init__(self):
        self.waits = []

    def wait_for_slot(self, min_interval):
        self.waits.append(min_interval)
        return 0.0


MEMBER = "urn:li:fsd_profile:SYNTH00000000000000000000000000000000"
MINI_MEMBER = MEMBER.replace("fsd_profile", "fs_miniProfile")

ME_URL = "https://www.linkedin.com/voyager/api/me"

# What LinkedIn serves a signed-out profile: a 200, at the URL we asked for,
# carrying the login shell instead of JSON.
LOGIN_SHELL = "<!DOCTYPE html><html><body>Sign in to LinkedIn</body></html>"


def supervisor_stub(*, status=None, **response):
    """A `request_fn` seam: one canned answer for every fetch."""
    answer = {"status": 200, "body": "{}", "headers": {}, "url": ME_URL} | response

    def request_fn(payload, **kwargs):
        if payload.get("op") == "status":
            return status if status is not None else {"pid": 4242, "profile": "/managed"}
        return answer

    return request_fn


def signed_out_client():
    """A real `BrowserClient` whose browser profile is no longer signed in."""
    return browser.BrowserClient(
        rate=1.0,
        state=NoPace(),
        request_fn=supervisor_stub(body=LOGIN_SHELL, headers={"content-type": "text/html"}),
    )


# --------------------------------------------------------------------------- parsing


def test_both_flag_forms_are_accepted():
    assert cli.parse_flags(["--count=5"])[0]["count"] == "5"
    assert cli.parse_flags(["--count", "5"])[0]["count"] == "5"


def test_boolean_flags_need_no_value():
    flags, rest = cli.parse_flags(["--unread-only", "--count=3"])
    assert flags["unread-only"] is True
    assert flags["count"] == "3"
    assert rest == []


def test_positionals_are_preserved_in_order():
    flags, rest = cli.parse_flags(["alpha", "--text=hi", "beta"])
    assert rest == ["alpha", "beta"]
    assert flags["text"] == "hi"


def test_urn_positional_with_punctuation_survives():
    urn = "urn:li:msg_conversation:(urn:li:fsd_profile:ABC,2-xyz==)"
    _, rest = cli.parse_flags([urn])
    assert rest == [urn]


def test_short_flags_are_rejected():
    """No short flags anywhere - an agent should never guess what -c means."""
    with pytest.raises(cli.UsageError):
        cli.parse_flags(["-c", "5"])


def test_value_flag_missing_its_value_is_a_usage_error():
    with pytest.raises(cli.UsageError):
        cli.parse_flags(["--count"])


def test_value_flag_followed_by_another_flag_is_a_usage_error():
    """`--text --json` must not silently swallow the next flag as a value."""
    with pytest.raises(cli.UsageError):
        cli.parse_flags(["--to", "--json"])


# ------------------------------------------------------------------ unknown flags


def test_an_unknown_flag_is_refused_and_names_the_closest_real_one():
    """`--tyep=PRAISE` used to react LIKE and report success: the flag was
    parsed, stored under a name nothing read, and dropped."""
    client = FakeClient(REACTED_PAYLOAD)
    code, _, err = run(["react", ACTIVITY, "--tyep=PRAISE"], client=client)
    assert code == 2
    body = envelope(err)
    assert body["error"]["code"] == "usage"
    assert "--type" in body["error"]["message"]
    assert client.posts == [], "a write went out under a flag this CLI never read"


def test_no_command_accepts_a_flag_it_never_reads():
    """The refusal has to hold for every command, not just the one that was
    caught. A flag silently dropped by `react` reported a LIKE as a PRAISE; the
    same hole under `post create` sends a post to Anyone under a flag that asked
    for Connections, and the agent is told it succeeded either way.

    Both forms are checked: `--x=v` parses as one token, `--x v` swallows the
    next one, and only the second can eat a real argument.
    """
    for phrase, argv in sorted(INVOCATIONS.items()):
        for extra in (["--not-a-real-flag=1"], ["--not-a-real-flag", "1"]):
            client = FakeClient(REACTED_PAYLOAD)
            code, _, err = run(list(argv) + extra, client=client)
            assert code == 2, f"`{phrase} {extra[0]}` was not refused"
            assert envelope(err)["error"]["code"] == "usage"
            assert client.posts == [], f"`{phrase}` wrote under an unread flag"


def test_a_real_flag_aimed_at_a_command_that_ignores_it_is_refused():
    """`--visibility` reaching `post get` is the same defect wearing a real
    flag's name: the allowlist is granular to the verb, and a verb's actions do
    not all read the same flags."""
    for argv in (
        ["feed", "list", "--text=x"],
        ["post", "get", "urn:li:activity:7000000000000000000", "--visibility=ANYONE"],
        ["me", "--to=someone"],
    ):
        client = FakeClient(FEED_PAYLOAD)
        code, _, err = run(argv, client=client)
        assert code == 2, f"{argv} was accepted"
        assert envelope(err)["error"]["code"] == "usage"


def test_the_page_size_flag_an_agent_guesses_names_the_real_one():
    """Confirmed live: `feed list --limit=2` ran clean and returned a
    default-sized page, because the flag is `--count`."""
    client = FakeClient(FEED_PAYLOAD)
    code, _, err = run(["feed", "list", "--limit=2"], client=client)
    assert code == 2
    assert "--count" in envelope(err)["error"]["message"]
    assert client.paths == []


def test_a_flag_that_belongs_to_another_verb_is_refused():
    """Knowing the name is not enough - a flag the *handler* never reads is as
    silent as one that does not exist."""
    client = FakeClient(FEED_PAYLOAD)
    code, _, err = run(["feed", "list", "--text=hello"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "--text" in message
    assert "feed" in message
    assert client.paths == []


@pytest.mark.parametrize("args", [["invite", "someone", "--note=hi"]])
def test_a_flag_whose_payload_field_was_never_captured_says_which_half_is_missing(args):
    """`--note` is advertised in the usage text and is not a misspelling, so
    "unknown flag" would tell an agent the wrong thing about which half is
    missing. `invite` itself works; what has no capture behind it is the note
    field, and a note accepted and dropped sends the bare invitation the caller
    was using the flag to avoid.

    `post create --text=…` used to be the other case here and is now implemented,
    so it answers about the post rather than about the payload; `tests/test_posts.py`
    owns it.
    """
    code, _, err = run(args, client=FakeClient())
    assert code == 2
    assert "has not been captured" in envelope(err)["error"]["message"]


def test_a_flag_is_still_accepted_by_the_verb_that_owns_it():
    client = FakeClient(REACTED_PAYLOAD)
    code, out, err = run(["react", ACTIVITY, "--type=PRAISE"], client=client)
    assert code == 0, err
    assert envelope(out)["data"]["reaction"] == "PRAISE"


def test_the_deleted_transport_selector_is_now_refused_rather_than_swallowed():
    """It chose the urllib client, which no longer exists. Accepted and dropped,
    an agent that passes it believes it routed the request somewhere it did not."""
    client = FakeClient(ME_PAYLOAD)
    code, _, err = run(["me", "--transport=direct"], client=client)
    assert code == 2
    assert client.paths == []


def test_every_boolean_flag_is_a_flag_this_cli_knows():
    """A boolean flag missing from the allowlist would parse and then be refused
    by the checker - two tables that have to agree, so pin that they do."""
    assert cli.BOOLEAN_FLAGS <= cli.KNOWN_FLAGS


def test_every_flag_the_usage_text_advertises_is_accepted():
    """A documented flag the parser rejects is the same defect as an undocumented
    one it swallows, pointing the other way."""
    advertised = set(re.findall(r"--([a-z][a-z0-9-]*)", cli.USAGE))
    assert advertised, "the usage text stopped naming flags"
    assert advertised <= cli.KNOWN_FLAGS


# Every spelling by which a flag's value is actually consumed. `ctx.count`,
# `ctx.cursor` and `ctx.rate` all go through `flags.get`, so the properties need
# no entry of their own.
FLAG_READ = re.compile(
    r"(?:int_flag|require|flag|flags\.get|flags\.pop)\(\s*[\"']([a-z][a-z0-9-]*)[\"']"
)


def flags_the_package_reads() -> set[str]:
    package = Path(linkedin_cli.__file__).parent
    return {
        name
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts
        for name in FLAG_READ.findall(path.read_text())
    }


def test_every_flag_the_usage_text_advertises_is_read_or_says_it_is_not():
    """Accepting a flag and dropping it is the same lie as a verb that refuses.

    `--tyep=PRAISE` reacted LIKE and reported success, and the allowlist ended
    that for *misspellings* - but `--verbose` is spelled correctly, is on the
    allowlist, and is read by nothing. An agent passing it gets exit 0 and no
    extra output, which is indistinguishable from working. So a flag this text
    names must be consumed somewhere in the package, or the line naming it must
    say it is ignored.
    """
    read = flags_the_package_reads()
    silent = {}
    for line in cli.USAGE.splitlines():
        for name in re.findall(r"--([a-z][a-z0-9-]*)", line):
            if name not in read and "ignored" not in line and "not implemented" not in line:
                silent[name] = line.strip()
    assert silent == {}
    # The reader has to still find things, or every flag passes for free.
    assert {"count", "text", "visibility", "idempotency-key"} <= read
    assert "verbose" not in read, "if --verbose now does something, stop calling it ignored"


def usage_block(flag: str) -> str:
    """The `--flag` line from the usage text, with its wrapped continuations."""
    lines = cli.USAGE.splitlines()
    start = next(i for i, line in enumerate(lines) if f"--{flag}" in line)
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.strip() or re.match(r"\s+--[a-z]", line):
            break
        block.append(line)
    return "\n".join(block)


def test_the_usage_text_does_not_promise_the_dedupe_the_payload_switches_off():
    """`--idempotency-key` is named after a guarantee LinkedIn does not provide.

    The assertions are unchanged and still right; the reasoning behind them has
    moved. The usage text used to explain `post create`'s refusal by contrast
    with a de-duplication `messages send` was supposed to have, which was false.
    It is now false in the other direction too: the flag *overrides* the token
    `messages send`/`reply` derive from the thread and the text, and switches off
    the repeat check those verbs do before sending - so it is precisely the way
    to put a second message carrying the same text into a real person's thread.

    "second message" therefore has to stay in the block, truthfully and for a
    new reason, and none of the three banned promises is needed to say it. The
    block must not name a field in LinkedIn's request body either; that is
    `tests/test_wire_field_stays_in_the_capture.py`'s rule, and this file is not
    on its allowlist.
    """
    block = usage_block("idempotency-key")
    assert "second message" in block, "the flag's one real hazard is not stated"
    for promise in ("de-duplicates", "dedupes on", "retry is safe"):
        assert promise not in block, f"the usage text still promises {promise!r}"


# --------------------------------------------------------------------------- dispatch


def test_no_command_prints_help_and_exits_2():
    code, out, err = run([])
    assert code == 2
    assert "linkedin" in (out + err)


def test_unknown_command_exits_2_with_envelope():
    code, out, err = run(["frobnicate"])
    assert code == 2
    body = envelope(err)
    assert body["ok"] is False
    assert body["error"]["code"] == "usage"


def test_help_exits_zero():
    code, out, _ = run(["--help"])
    assert code == 0
    assert "messages" in out


def test_every_spec_command_is_registered():
    """A missing verb is a silent regression an agent only finds at runtime."""
    assert {"auth", "me", "profile", "feed", "post", "messages", "notifications", "doctor"} <= set(
        cli.COMMANDS
    )


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "exc,code,slug",
    [
        (transport.SessionExpired("stale"), 3, "session_expired"),
        (transport.NotFound("gone"), 4, "not_found"),
        (transport.RateLimited("slow"), 5, "rate_limited"),
        (transport.UpstreamError("boom"), 6, "upstream"),
        (transport.OutcomeUnknown("maybe"), 6, "outcome_unknown"),
        (transport.StaleQueryId("rotated"), 7, "stale_query_id"),
        (transport.Blocked("999"), 9, "blocked"),
        (state.Blocked("breaker open"), 9, "blocked"),
        (state.WriteQuotaExceeded("daily cap"), 5, "write_quota_exceeded"),
        (cli.UsageError("bad flag"), 2, "usage"),
        (ValueError("not a profile URL"), 2, "usage"),
    ],
)
def test_errors_map_to_exit_codes_and_slugs(exc, code, slug, monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "boom", lambda ctx: (_ for _ in ()).throw(exc))
    got, out, err = run(["boom"])
    assert got == code
    body = envelope(err)
    assert body["ok"] is False
    assert body["error"]["code"] == slug


def test_an_outcome_unknown_write_is_never_marked_retryable(monkeypatch):
    """A write whose outcome is unknown may already have landed, and a retried
    write duplicates. The envelope says so; nothing about the exit code does."""
    monkeypatch.setitem(
        cli.COMMANDS,
        "boom",
        lambda ctx: (_ for _ in ()).throw(transport.OutcomeUnknown("mid-flight")),
    )
    _, _, err = run(["boom"])
    assert envelope(err)["error"]["retryable"] is False


def test_no_exit_code_is_the_retry_signal_and_exit_six_least_of_all():
    """The position, pinned: `retryable` in the envelope is the only retry
    signal this CLI emits, and the exit code is not a second one.

    `surfaces/messaging.py:_unconfirmed` is what made the ambiguity expensive. A
    message that was sent and not confirmed exits 6, reports `retryable: false`
    and sends the caller to read the thread back before anything else. Alongside
    it, `cli.py` described exit 6 as "the code an agent retries". An agent
    branching on the code rather than the envelope would have done exactly the
    thing the message tells it not to.

    Asserted against the source because the defect was documentation: the code
    already behaved, and only the prose an agent reads was wrong.

    **Changed with the idempotent-reply work.** This used to assert the literal
    "Do not retry it blind", which was the whole of the old advice and was
    justified by a wire field. `messages reply` now checks an identical re-run
    against the newest page of the thread, so the advice is "read it back first,
    then re-run once" rather than "never" - the verdict `retryable: False` is
    unchanged, because that check is best effort. The assertions below are the
    same claim without the deleted sentence: the prose must order the read-back
    first, and must still promise nothing about a retry being safe.
    """
    unconfirmed = messaging._unconfirmed(CONVERSATION, "the response named no message urn")
    assert cli._report(unconfirmed) == ("upstream", 6, False)
    message = str(unconfirmed)
    assert "read it back with `linkedin messages read" in message
    assert "before doing anything else" in message
    for promise in ("retry is safe", "safe to retry", "just retry", "retry it"):
        assert promise not in message.lower(), f"the unconfirmed message promises {promise!r}"

    # Nothing that exits 6 ever reports itself retryable, and the one thing that
    # does exits 5 - so the code carries no retry answer in either direction.
    assert cli._report(transport.RateLimited("HTTP 429")) == ("rate_limited", 5, True)
    for kind, _slug, code in cli.ERROR_SLUGS:
        if code == 6:
            assert getattr(kind, "retryable", False) is False, kind

    assert "the code an agent retries" not in Path(cli.__file__).read_text()


def test_errors_go_to_stderr_and_stdout_stays_parseable(monkeypatch):
    monkeypatch.setitem(
        cli.COMMANDS, "boom", lambda ctx: (_ for _ in ()).throw(transport.NotFound("x"))
    )
    _, out, err = run(["boom"])
    assert out == ""
    assert envelope(err)["ok"] is False


def test_retryable_flag_is_surfaced(monkeypatch):
    monkeypatch.setitem(
        cli.COMMANDS,
        "boom",
        lambda ctx: (_ for _ in ()).throw(transport.RateLimited("slow", retryable=True)),
    )
    _, _, err = run(["boom"])
    assert envelope(err)["error"]["retryable"] is True


# --------------------------------------------------------------------------- output


def test_success_is_wrapped_in_the_envelope(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "ping", lambda ctx: {"pong": True})
    code, out, _ = run(["ping"])
    assert code == 0
    body = envelope(out)
    assert body["ok"] is True
    assert body["data"] == {"pong": True}


def test_paged_result_exposes_cursor(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "ping", lambda ctx: cli.Page([1, 2], "CURSOR", True))
    _, out, _ = run(["ping"])
    body = envelope(out)
    assert body["data"] == [1, 2]
    assert body["next_cursor"] == "CURSOR"
    assert body["has_more"] is True


def test_raw_flag_unwraps_the_envelope(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "ping", lambda ctx: {"pong": True})
    _, out, _ = run(["ping", "--raw"])
    assert envelope(out) == {"pong": True}


# What LinkedIn answered `post create --visibility=CONNECTIONS` with: HTTP 200,
# and the refusal inside the body. `--raw` had nothing to say about it, so
# diagnosing it took a hand-written script - the one thing the flag exists for.
# See docs/incidents.md.
GRAPHQL_REFUSAL = {
    "data": {"doCreateDashShare": None},
    "errors": [{"message": "Invalid input for enum 'dash_contentcreation_VisibilityType'"}],
}


def test_raw_carries_the_upstream_body_when_the_write_is_refused():
    """The path `--raw` is most needed on, and the one it did nothing on: on a
    failure it fell straight through to the ordinary envelope."""
    client = FakeClient(GRAPHQL_REFUSAL)
    code, _, err = run(["post", "create", "--text=hi", "--raw"], client=client)
    assert code == 6, "surfacing the body must not change the outcome"
    error = envelope(err)["error"]
    assert error["code"] == "upstream"
    assert error["body"] == GRAPHQL_REFUSAL
    assert "VisibilityType" in error["message"] or "VisibilityType" in json.dumps(error["body"])


def test_the_error_envelope_is_unchanged_without_the_flag():
    """The body is opt-in. An agent that never passes `--raw` sees exactly the
    envelope it saw before, which is why this went inside `error` rather than
    beside it."""
    code, _, err = run(["post", "create", "--text=hi"], client=FakeClient(GRAPHQL_REFUSAL))
    assert code == 6
    assert set(envelope(err)["error"]) == {"code", "message", "retryable"}


def test_a_raw_body_is_scrubbed_before_it_reaches_stderr():
    """A body is where a live credential would ride out, and stderr under an
    agent gateway is permanent model context. Assembled by concatenation so no
    live-shaped literal enters a tracked file for `tools/leakcheck.py` to find."""
    secret = "AQED" + "B" * 60
    client = FakeClient({"errors": [{"message": "denied"}], "cookie": f"li_at={secret}"})
    _, _, err = run(["post", "create", "--text=hi", "--raw"], client=client)
    assert secret not in err
    assert envelope(err)["error"]["body"]["cookie"] == f"li_at={render.REDACTED}"


def test_a_failure_that_never_reached_linkedin_carries_no_body():
    """There is nothing upstream about a flag this CLI refused by itself, and an
    empty `body` key would invite reading one."""
    code, _, err = run(["post", "create", "--text=hi", "--visibility=NOBODY", "--raw"])
    assert code == 2
    assert "body" not in envelope(err)["error"]


def test_a_dry_run_preview_is_never_reported_as_an_upstream_body():
    """A preview is this CLI's own description of a request nobody sent, and
    labelling it as LinkedIn's answer is the confusion `--dry-run` exists to
    prevent."""
    monkeypatch_free_client = FakeClient()
    code, _, err = run(
        ["post", "create", "--text=hi", "--dry-run", "--raw"], client=monkeypatch_free_client
    )
    assert code == 0, err


# A body attached to the wrong failure is worse than no body at all. `--raw`
# labels it "the response body" and `render.to_text` prints it under "upstream
# response:", so an operator handed the previous request's payload diagnoses
# against evidence this CLI does not have - which is this project's own failure
# mode wearing the costume of a fix.

LOOKUP_PAYLOAD = {"data": {"conversation": "urn:li:msg_conversation:LOOKUP"}}


class LookupThenReject:
    """A GET LinkedIn answers and a POST it rejects before any body comes back."""

    def __init__(self, payload, error):
        self.payload = payload
        self.error = error
        self.posts: list[tuple] = []

    def get(self, path: str, dry_run: bool = False):
        return self.payload

    def post(self, path: str, body, dry_run: bool = False):
        self.posts.append((path, body, dry_run))
        raise self.error


def test_a_local_refusal_after_a_lookup_does_not_borrow_the_lookup_body(monkeypatch):
    """The recorded response belongs to a request that *succeeded*. Attaching it
    to a refusal this CLI made by itself reports a payload LinkedIn never sent
    about this failure - and the operator reads the conversation lookup while
    looking for why the send was refused."""

    def ping(ctx):
        ctx.client.get("/conversations")
        raise cli.UsageError("--to did not resolve to a member")

    monkeypatch.setitem(cli.COMMANDS, "ping", ping)
    code, _, err = run(["ping", "--raw"], client=FakeClient(LOOKUP_PAYLOAD))

    assert code == 2
    assert "body" not in envelope(err)["error"]


def test_a_failure_the_transport_raised_carries_no_earlier_body(monkeypatch):
    """A 4xx never reaches the recorder - the transport raises first - so the
    last body in hand is the lookup's. The request that failed produced no body
    this process ever saw, and saying otherwise is the whole defect."""

    def ping(ctx):
        ctx.client.get("/conversations")
        ctx.client.post("/send", {"text": "hi"})

    monkeypatch.setitem(cli.COMMANDS, "ping", ping)
    client = LookupThenReject(LOOKUP_PAYLOAD, transport.UpstreamError("HTTP 422"))
    code, _, err = run(["ping", "--raw"], client=client)

    assert code == 6
    assert client.posts, "the test did not reach the POST it is about"
    assert "body" not in envelope(err)["error"]


def test_a_second_call_to_one_path_does_not_inherit_the_first_answer(monkeypatch):
    """Requests are counted, not keyed by `(method, path)`. A read-back that
    answers once and is rejected the second time round hits the same endpoint
    both times, so matching on the path alone would hand the failure the body
    its own predecessor returned - the identical defect, one polling loop later.
    """

    class AnswersOnce:
        def __init__(self):
            self.calls = 0

        def get(self, path: str, dry_run: bool = False):
            self.calls += 1
            if self.calls == 1:
                return LOOKUP_PAYLOAD
            raise transport.NotFound("the message is not in the thread")

    def ping(ctx):
        ctx.client.get("/messages")
        ctx.client.get("/messages")

    monkeypatch.setitem(cli.COMMANDS, "ping", ping)
    code, _, err = run(["ping", "--raw"], client=AnswersOnce())

    assert code == 4
    assert "body" not in envelope(err)["error"]


def test_the_body_survives_when_it_is_the_failing_request_own_answer(monkeypatch):
    """The other half, and the reason the key exists at all: a refusal that
    arrives *inside* a 200 is recorded against the request that failed, so it is
    real evidence and must still be attached - see the GraphQL refusal above."""

    def ping(ctx):
        ctx.client.get("/conversations")
        ctx.client.post("/send", {"text": "hi"})
        raise transport.UpstreamError("LinkedIn refused the message")

    monkeypatch.setitem(cli.COMMANDS, "ping", ping)
    _, _, err = run(["ping", "--raw"], client=FakeClient(LOOKUP_PAYLOAD, GRAPHQL_REFUSAL))

    assert envelope(err)["error"]["body"] == GRAPHQL_REFUSAL


def test_format_text_drops_the_envelope(monkeypatch):
    """Human mode shows the content, not the machine wrapper."""
    monkeypatch.setitem(
        cli.COMMANDS,
        "ping",
        lambda ctx: [
            {"profile_urn": "urn:li:fsd_profile:X", "name": "Ada", "headline": "Engineer"}
        ],
    )
    _, out, _ = run(["ping", "--format=text"])
    assert "Ada" in out
    assert '"ok"' not in out
    assert "has_more" not in out


def test_bad_format_value_is_a_usage_error():
    code, _, err = run(["me", "--format=yaml"], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


# ------------------------------------------------------------------ real commands

ME_PAYLOAD = {
    "data": {
        "plainId": 1234,
        "*miniProfile": MINI_MEMBER,
        "premiumSubscriber": False,
    },
    "included": [
        {
            "$type": "com.linkedin.voyager.identity.shared.MiniProfile",
            "entityUrn": MINI_MEMBER,
            "dashEntityUrn": MEMBER,
            "publicIdentifier": "synthetic-operator",
            "firstName": "Syn",
            "lastName": "Thetic",
        }
    ],
}

PROFILE_PAYLOAD = {
    "data": {
        "entityUrn": MEMBER,
        "publicIdentifier": "synthetic-operator",
        "firstName": "Syn",
        "lastName": "Thetic",
        "headline": "Builder of small tools",
    }
}

FEED_PAYLOAD = {
    "data": {
        "paging": {"start": 0, "count": 1, "total": 9},
        "*elements": ["urn:li:fs_updateV2:(urn:li:activity:7486933303296000001,MAIN_FEED)"],
    },
    "included": [
        {
            "$type": "com.linkedin.voyager.feed.render.UpdateV2",
            "entityUrn": "urn:li:fs_updateV2:(urn:li:activity:7486933303296000001,MAIN_FEED)",
            "updateMetadata": {"urn": "urn:li:activity:7486933303296000001"},
            "commentary": {"text": {"text": "a synthetic post"}},
        }
    ],
}

CONVERSATION = f"urn:li:msg_conversation:({MEMBER},2-synthetic==)"

# A thread hanging in somebody else's inbox. A conversation urn is
# `(<mailbox owner>,2-<thread>)`, so this differs from the one above in exactly
# the component that decides whose mailbox a message is delivered into.
STRANGER = "urn:li:fsd_profile:ACoAASYNTHETICSTRANGER"
FOREIGN_CONVERSATION = f"urn:li:msg_conversation:({STRANGER},2-synthetic==)"

CONVERSATIONS_PAYLOAD = {
    "data": {"data": {"messengerConversationsByCategoryQuery": {"*elements": [CONVERSATION]}}},
    "included": [
        {
            "$type": "com.linkedin.messenger.Conversation",
            "entityUrn": CONVERSATION,
            "lastActivityAt": 1783583409545,
            "read": False,
        }
    ],
}

# What `createMessage` answers with. Required by every `messages send`/`reply`
# test since the surface grew a postcondition: an empty body used to be reported
# as a message delivered, and `surfaces/messaging.py` now refuses one. A canned
# `{}` in a post slot is exactly that empty body, so the tests below name the
# urn the write really answers with rather than proving the refusal by accident.
SENT_MESSAGE = {"data": {"*value": f"urn:li:msg_message:({MEMBER},2-synthetic==)"}}


def messages_payload(conversation: str = CONVERSATION) -> dict:
    """One page of `conversation`, as the messages query answers it.

    The thread is named twice, the way the capture names it: once as the
    message's `*conversation` reference and once as a bare stub of its own. The
    mailbox component of that urn is what `messages reply` checks against the
    operator's own member urn, so a page that omitted it would let every reply
    test pass against a surface that had stopped checking.
    """
    return {
        "data": {"data": {"messengerMessagesBySyncToken": {"*elements": ["urn:li:msg_message:x"]}}},
        "included": [
            {
                "$type": "com.linkedin.messenger.Message",
                "entityUrn": "urn:li:msg_message:x",
                "*conversation": conversation,
                "body": {"text": "hello"},
                "deliveredAt": 1783583409545,
            },
            {"$type": "com.linkedin.messenger.Conversation", "entityUrn": conversation},
        ],
    }


MESSAGES_PAYLOAD = messages_payload()

NOTIFICATIONS_PAYLOAD = {
    "data": {
        "metadata": {"nextStart": 11},
        "*elements": ["urn:li:fsd_notificationCard:(REACTED_TO_YOUR_POST,x)"],
    },
    "included": [
        {
            "$type": "com.linkedin.voyager.dash.identity.notifications.Card",
            "entityUrn": "urn:li:fsd_notificationCard:(REACTED_TO_YOUR_POST,x)",
            "headline": {"text": "Someone reacted"},
            "publishedAt": 1783583409545,
            "read": False,
        }
    ],
}


def test_me_projects_the_member():
    code, out, _ = run(["me"], client=FakeClient(ME_PAYLOAD))
    assert code == 0
    data = envelope(out)["data"]
    assert data["profile_urn"] == MEMBER
    assert data["public_id"] == "synthetic-operator"
    assert data["name"] == "Syn Thetic"


def test_profile_get_without_an_argument_reads_the_operator():
    client = FakeClient(ME_PAYLOAD, PROFILE_PAYLOAD)
    code, out, _ = run(["profile", "get"], client=client)
    assert code == 0
    assert envelope(out)["data"]["headline"] == "Builder of small tools"


def test_profile_get_resolves_a_url_to_a_public_id():
    client = FakeClient(PROFILE_PAYLOAD)
    code, _, _ = run(
        ["profile", "get", "https://www.linkedin.com/in/synthetic-operator"], client=client
    )
    assert code == 0
    assert "memberIdentity=synthetic-operator" in client.paths[0]


def test_profile_get_rejects_a_company_url():
    code, _, err = run(
        ["profile", "get", "https://www.linkedin.com/company/acme"], client=FakeClient()
    )
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


def test_profile_requires_a_subcommand():
    code, _, err = run(["profile"], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


def test_feed_list_pages_and_returns_an_envelope():
    client = FakeClient(FEED_PAYLOAD)
    code, out, _ = run(["feed", "list", "--count=1"], client=client)
    assert code == 0
    body = envelope(out)
    assert body["data"][0]["activity_urn"] == "urn:li:activity:7486933303296000001"
    assert body["next_cursor"] == "1"
    assert body["has_more"] is True
    assert "count=1" in client.paths[0]


def test_feed_list_threads_the_cursor_through():
    client = FakeClient(FEED_PAYLOAD)
    run(["feed", "list", "--cursor", "20"], client=client)
    assert client.paths[0].endswith("&start=20")


def test_feed_defaults_to_list_when_no_action_is_given():
    """`feed` alone is unambiguous - there is only one thing it can mean."""
    code, out, _ = run(["feed"], client=FakeClient(FEED_PAYLOAD))
    assert code == 0
    assert envelope(out)["ok"] is True


def test_post_get_addresses_the_activity_urn():
    client = FakeClient(FEED_PAYLOAD)
    code, out, _ = run(["post", "get", "7486933303296000001"], client=client)
    assert code == 0
    assert "urn%3Ali%3Aactivity%3A7486933303296000001" in client.paths[0]
    assert envelope(out)["data"]["text"] == "a synthetic post"


def test_post_get_needs_an_argument():
    code, _, err = run(["post", "get"], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


def test_messages_read_projects_a_thread():
    client = FakeClient(MESSAGES_PAYLOAD)
    code, out, _ = run(["messages", "read", CONVERSATION], client=client)
    assert code == 0
    assert envelope(out)["data"][0]["text"] == "hello"
    assert "conversationUrn" in client.paths[0]


def test_messages_read_needs_a_conversation_urn():
    code, _, err = run(["messages", "read"], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


def test_mark_all_read_drains_the_mailbox_once_it_is_confirmed():
    """The verb is named for what the captured payload does - `markAllMessagesAsSeen`
    takes no conversation - and it only runs when the caller says so."""
    client = FakeClient()
    code, out, err = run(["messages", "mark-all-read", "--yes"], client=client)
    assert code == 0, err
    assert envelope(out)["data"]["marked_read_until"] > 0
    path, _, _ = client.posts[-1]
    assert path == "voyagerMessagingDashMessagingBadge?action=markAllMessagesAsSeen"


def test_mark_all_read_without_the_opt_in_sends_nothing():
    """SKILL.md tells the agent to run this on every triage pass. Draining every
    unread thread on a triage pass is how messages stop being answered, so the
    breadth has to be stated by the caller rather than discovered afterwards."""
    client = FakeClient()
    code, _, err = run(["messages", "mark-all-read"], client=client)
    assert code == 2
    body = envelope(err)
    assert body["error"]["code"] == "usage"
    assert "--yes" in body["error"]["message"]
    assert client.posts == []


def test_mark_all_read_refuses_a_conversation_urn_instead_of_ignoring_it():
    """It used to accept one and drop it, so `mark-read <thread>` marked every
    *other* unread thread seen too - irreversibly, and silently."""
    client = FakeClient()
    code, _, err = run(["messages", "mark-all-read", CONVERSATION, "--yes"], client=client)
    assert code == 2
    assert "not been captured" in envelope(err)["error"]["message"]
    assert client.posts == []


def test_the_old_mark_read_spelling_is_refused_and_names_the_new_one():
    """An agent holding the old spelling must not have its argument silently
    widened into the whole mailbox."""
    client = FakeClient()
    code, _, err = run(["messages", "mark-read", CONVERSATION], client=client)
    assert code == 2
    assert "mark-all-read" in envelope(err)["error"]["message"]
    assert client.posts == []


def test_messages_reply_sends_into_the_thread():
    # `me` first, then the thread page: the mailbox the answer is checked against
    # is resolved before the read that produces the answer (`cmd_messages`), and
    # it is cached in the ledger on any run but the first.
    client = FakeClient(ME_PAYLOAD, MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, out, _ = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0
    assert envelope(out)["data"]["conversation_urn"] == CONVERSATION
    path, body, _ = client.posts[-1]
    assert path == "voyagerMessagingDashMessengerMessages?action=createMessage"
    assert body["message"]["body"]["text"] == "ack"


def test_messages_reply_reads_the_thread_before_it_writes_into_it():
    """`messaging.send_message` drops the urn into the createMessage body with no
    lookup, and `reply <urn>` builds the identical request that `send
    --conversation=<urn>` does - so before this, nothing on the reply path
    established that the thread was one this account is in. The caller that
    matters is an agent that has just read a stranger's DM: "reply in the thread"
    is what an injected message says, and every urn the agent has listed is a
    candidate."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, _, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    assert "conversationUrn" in client.paths[0], "the reply went out before the thread was read"


def test_messages_reply_refuses_a_thread_that_reads_back_empty():
    """Exit 4, and nothing sent. What the read establishes is that LinkedIn
    served this session a message from that thread; an empty page is the answer
    for a urn naming somebody else's mailbox."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient({})
    code, _, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 4
    assert envelope(err)["error"]["code"] == "not_found"
    assert client.posts == []


def test_messages_reply_refuses_a_thread_in_another_members_mailbox():
    """Exit 4, nothing sent, and the read is the only request it costs.

    A conversation urn is `(<mailbox owner>,2-<thread>)` and `send_message` drops
    it into the createMessage body untouched, so the mailbox component is what
    decides whose inbox a reply lands in. The broker's value regex pins the same
    component from outside; this holds for any caller, including one that never
    went through the broker."""
    state.State().remember("member_urn", MEMBER)
    foreign = "urn:li:msg_conversation:(urn:li:fsd_profile:ACoAASYNTHETICSTRANGER,2-def==)"
    client = FakeClient(messages_payload(foreign))
    code, _, err = run(["messages", "reply", foreign, "--text=ack"], client=client)
    assert code == 4
    assert envelope(err)["error"]["code"] == "not_found"
    assert client.posts == []
    # No request at all: the caller's own urn names a foreign mailbox, and that
    # is a string comparison. The answer-side check still exists for the case
    # this cannot see - LinkedIn resolving to a thread other than the one asked
    # about - and that one does cost the read.
    assert client.paths == []


def test_a_reply_refused_for_its_thread_costs_no_message_quota():
    """The claim is taken before the read and handed back when nothing was sent,
    the way every other locally-refused write is - `DAILY_CAPS["message"]` is
    40/day and it is the bound R2 leans on."""
    state.State().remember("member_urn", MEMBER)
    code, _, _ = run(["messages", "reply", CONVERSATION, "--text=ack"], client=FakeClient({}))
    assert code == 4
    assert state.State().write_count("message", state.DAY) == 0


def test_a_spent_cap_refuses_a_reply_before_it_spends_a_request(monkeypatch):
    """Ordering, and it is the reason the check sits inside `_write` rather than
    in front of it: the cap is a local file and the membership read is a live
    round trip, so an agent that has spent its budget is told so without paying
    for the answer."""
    monkeypatch.setitem(state.DAILY_CAPS, "message", 0)
    state.State().remember("member_urn", MEMBER)
    client = FakeClient({})
    code, _, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 5, err
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert client.paths == [], "the cap was checked after a request went out"


def test_messages_send_pays_for_the_same_round_trip_reply_does():
    """It used to be exempted, and the exemption was the hole.

    The argument for it was that `send`'s caller names `--conversation`
    deliberately rather than being handed one by a message it just read - which
    describes the operator and not the urn, and the urn is the whole input. Both
    verbs reach `messaging.send_message` through one function now, so the read
    is not a property of the verb any more.
    """
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, _, err = run(
        ["messages", "send", f"--conversation={CONVERSATION}", "--text=hi"], client=client
    )
    assert code == 0, err
    assert client.paths[-1] == "voyagerMessagingDashMessengerMessages?action=createMessage"
    assert len(client.paths) == 2, "the thread read and the send, and nothing else"


# ------------------------------------ every way a message reaches LinkedIn

# The two spellings that end in `messaging.send_message`, with the arguments
# that reach the send without a lookup. They are parametrized together because
# the guard landed on `reply` alone the first time: `send --conversation=<urn>`
# builds the byte-identical request one branch down in the same function, and it
# took a stranger's urn to exit 0 with the message in their thread.
SEND_PATHS = {
    "reply": [CONVERSATION, "--text=hi"],
    "send": [f"--conversation={CONVERSATION}", "--text=hi"],
}


@pytest.mark.parametrize("action", sorted(SEND_PATHS))
def test_every_way_to_post_a_message_reads_the_thread_first(action):
    """`confirm_reply_target`'s docstring claims the guard "holds for every
    caller and not only for the ones that arrived through the broker". Both
    verbs hand the same urn to the same function, so either both pay for the
    read or the claim is false for whichever one does not."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, _, err = run(["messages", action] + SEND_PATHS[action], client=client)
    assert code == 0, err
    assert "conversationUrn" in client.paths[0], "the message went out before the thread was read"


@pytest.mark.parametrize("action", sorted(SEND_PATHS))
def test_every_way_to_post_a_message_refuses_another_members_mailbox(action):
    """Exit 4, nothing sent, whichever spelling was used. Reproduced against the
    unguarded branch: `send --to=<mine> --conversation=<stranger-thread>` exited
    0 with the message posted into the stranger's thread, because `--to` is read
    only when `--conversation` is absent."""
    state.State().remember("member_urn", MEMBER)
    argv = [arg.replace(CONVERSATION, FOREIGN_CONVERSATION) for arg in SEND_PATHS[action]]
    client = FakeClient(messages_payload(FOREIGN_CONVERSATION))
    code, _, err = run(["messages", action] + argv, client=client)
    assert code == 4
    assert envelope(err)["error"]["code"] == "not_found"
    assert client.posts == []


# A member the operator has a thread with, and that thread as `messages list`
# projects it. `--to` is the third way a conversation urn reaches the writer:
# looked up rather than supplied, which is why it is easy to assume it needs no
# check - the urn came from LinkedIn. It goes through the same one function.
COUNTERPART = "urn:li:fsd_profile:ACoAASYNTHETICFRIEND00000000000000"


def conversations_with_counterpart(conversation: str = CONVERSATION) -> dict:
    participant = f"urn:li:msg_messagingParticipant:{COUNTERPART}"
    return {
        "data": {"data": {"messengerConversationsByCategoryQuery": {"*elements": [conversation]}}},
        "included": [
            {
                "$type": "com.linkedin.messenger.Conversation",
                "entityUrn": conversation,
                "lastActivityAt": 1783583409545,
                "read": True,
                "*conversationParticipants": [participant],
            },
            {
                "$type": "com.linkedin.messenger.MessagingParticipant",
                "entityUrn": participant,
                "hostIdentityUrn": COUNTERPART,
                "participantType": {"member": {"firstName": {"text": "Synthetic"}}},
            },
        ],
    }


def test_a_looked_up_thread_is_confirmed_like_any_other():
    """`send --to=<member>` finds the thread rather than being handed one, and
    the urn arrives from LinkedIn's own listing - which is the argument for
    exempting it and the reason not to. The exemption is what `--conversation`
    had, and one call site means there is no per-branch exemption left to grant."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(conversations_with_counterpart(), MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, _, err = run(["messages", "send", f"--to={COUNTERPART}", "--text=hi"], client=client)
    assert code == 0, err
    assert "conversationUrn" in client.paths[1], "the thread was never read back"
    assert client.posts, "nothing was sent, so this proves nothing about the guard"


def test_a_looked_up_thread_in_another_mailbox_is_refused_too():
    """The guard reads LinkedIn's answer rather than the caller's argument, so
    it is not vacuous on a urn LinkedIn supplied: an answer naming another
    member's mailbox is refused whoever produced the urn."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(
        conversations_with_counterpart(FOREIGN_CONVERSATION),
        messages_payload(FOREIGN_CONVERSATION),
    )
    code, _, err = run(["messages", "send", f"--to={COUNTERPART}", "--text=hi"], client=client)
    assert code == 4
    assert envelope(err)["error"]["code"] == "not_found"
    assert client.posts == []


# ------------------------------------- a reply this account already sent

# The urn of the reply already on the page. Distinct from `SENT_MESSAGE`'s, so
# "the urn it found" and "the urn a send would have answered with" cannot be
# confused for each other.
ALREADY_SENT_URN = f"urn:li:msg_message:({MEMBER},2-already-sent==)"


def thread_with_reply(text: str, sender: str = MEMBER, conversation: str = CONVERSATION) -> dict:
    """`messages_payload`, plus one more message on the page: `text`, from `sender`.

    One page, not two answers: the dedupe window *is* the page
    `confirm_reply_target` already fetched, so a fixture that needed a second
    request would be testing a mechanism this design does not have.

    `sender` is a parameter because the sender check is the whole difference
    between "this account already said that" and "somebody quoted it back". The
    participant is decorated the way the capture decorates one - a
    `MessagingParticipant` carrying `hostIdentityUrn`, which is the same
    spelling as the mailbox urn.
    """
    participant = f"urn:li:msg_messagingParticipant:{sender}"
    page = messages_payload(conversation)
    page["data"]["data"]["messengerMessagesBySyncToken"]["*elements"].append(ALREADY_SENT_URN)
    page["included"] += [
        {
            "$type": "com.linkedin.messenger.Message",
            "entityUrn": ALREADY_SENT_URN,
            "*conversation": conversation,
            "*sender": participant,
            "body": {"text": text},
            "deliveredAt": 1783583409546,
        },
        {
            "$type": "com.linkedin.messenger.MessagingParticipant",
            "entityUrn": participant,
            "hostIdentityUrn": sender,
            "participantType": {"member": {"firstName": {"text": "Synthetic"}}},
        },
    ]
    return page


def reads(client: FakeClient) -> list[str]:
    """The GETs only. `FakeClient.post` routes through `get`, so `paths` holds
    the create as well and counting it would hide a second round trip."""
    return [path for path in client.paths if "voyagerMessagingGraphQL" in path]


def test_a_reply_this_account_already_sent_with_this_text_is_not_sent_again():
    """The mechanism, end to end. The reply is on the page the membership check
    already paid for, so nothing is posted and the envelope says why - `deduped`
    is a field an agent can branch on rather than a string it has to parse."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("ack"))
    code, out, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    assert client.posts == []
    data = envelope(out)["data"]
    assert data["deduped"] is True
    assert data["message_urn"] == ALREADY_SENT_URN
    assert data["conversation_urn"] == CONVERSATION


def test_the_same_text_from_the_other_party_does_not_suppress_a_reply():
    """The negative that gives the test above its teeth: without the sender
    check, "ok" quoted back by the counterpart would silently swallow the
    operator's own "ok" - and the agent would be told it had already replied to
    a thread it has never answered."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("ack", sender=COUNTERPART), SENT_MESSAGE)
    code, out, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    assert client.posts, "a reply was suppressed by a message this account did not send"
    assert envelope(out)["data"]["deduped"] is False


def test_different_text_in_the_thread_does_not_suppress_a_reply():
    """The other half: matching on the thread rather than on the text would make
    every second reply into any thread a no-op."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("something else entirely"), SENT_MESSAGE)
    code, out, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    assert client.posts
    assert envelope(out)["data"]["deduped"] is False


def test_the_dedupe_check_costs_no_second_round_trip():
    """The check reads the page `confirm_reply_target` already fetched - the same
    argument the mailbox half is worth having on. A dedupe that cost its own read
    would double the traffic of the verb it exists to make cheaper."""
    state.State().remember("member_urn", MEMBER)
    deduped = FakeClient(thread_with_reply("ack"))
    assert run(["messages", "reply", CONVERSATION, "--text=ack"], client=deduped)[0] == 0
    assert len(reads(deduped)) == 1

    sent = FakeClient(thread_with_reply("something else"), SENT_MESSAGE)
    assert run(["messages", "reply", CONVERSATION, "--text=ack"], client=sent)[0] == 0
    assert len(reads(sent)) == 1


def test_a_deduped_reply_still_spends_a_write_slot(monkeypatch):
    """A deduped reply is not free: it issues the live, browser-driven GET that
    `confirm_reply_target` has always issued, against the operator's account.

    Refunding the slot would remove a control rather than save one. `claim_write`
    runs *before* that round trip so an agent that has spent its budget is
    refused without paying for the answer - and if every identical attempt were
    refunded, the 40/day cap could never be reached by an identical-text loop, so
    that pre-round-trip refusal would never fire on this verb again.

    `ctx.attempted_write` is asserted directly because it is the flag that
    decides commit-or-release, and `_WriteWatch.post` is deliberately no longer
    its only setter."""
    state.State().remember("member_urn", MEMBER)
    seen = {}
    real_write = cli._write

    def spy(ctx, kind, write, *, cleanup=False):
        try:
            return real_write(ctx, kind, write, cleanup=cleanup)
        finally:
            seen["attempted_write"] = ctx.attempted_write

    monkeypatch.setattr(cli, "_write", spy)
    client = FakeClient(thread_with_reply("ack"))
    code, _, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    assert client.posts == [], "this has to be the deduped path or it proves nothing"
    assert seen["attempted_write"] is True
    assert state.State().write_count("message", state.DAY) == 1


def test_a_dry_run_reply_spends_nothing():
    """The twin the test above needs: on a ledger that counted every invocation,
    "+1 on a dedupe" would pass for free."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("ack"))
    code, _, err = run(
        ["messages", "reply", CONVERSATION, "--text=ack", "--dry-run"], client=client
    )
    assert code == 0, err
    assert state.State().write_count("message", state.DAY) == 0
    assert reads(client) == [], "a dry run does not read the thread either"
    assert [dry for _, _, dry in client.posts] == [True], "a dry run previews and sends nothing"


def test_a_deduped_reply_reports_the_urn_it_found_not_a_fabricated_one():
    """The urn has to come off the page. Answering with the conversation urn, or
    with a null, would put an agent back where `_confirm_sent` was written to
    stop it: a success it does not look at again."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("ack"))
    code, out, err = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0, err
    found = envelope(out)["data"]["message_urn"]
    assert found == ALREADY_SENT_URN
    assert found.startswith(messaging.MESSAGE_URN_PREFIX)
    assert found != SENT_MESSAGE["data"]["*value"]


def test_the_mailbox_guard_still_runs_before_the_dedupe_check(monkeypatch):
    """The refactor that gave `confirm_reply_target` a return value is exactly
    where the security gate could silently move behind the convenience check.
    The page here *would* match, so only ordering can produce this refusal."""
    state.State().remember("member_urn", MEMBER)
    calls = []
    monkeypatch.setattr(messaging, "already_sent", lambda *args, **kw: calls.append(args) or None)
    client = FakeClient(thread_with_reply("ack", conversation=FOREIGN_CONVERSATION))
    code, _, err = run(["messages", "reply", FOREIGN_CONVERSATION, "--text=ack"], client=client)
    assert code == 4
    assert envelope(err)["error"]["code"] == "not_found"
    assert calls == [], "the dedupe check ran on a thread the guard refuses"
    assert client.posts == []


def test_an_explicit_idempotency_key_is_the_only_way_past_the_dedupe():
    """Sending the same text into the same thread twice on purpose has to stay
    reachable for the operator, and `--idempotency-key` is what makes it so: it
    overrides the derived token and skips the check. It is not in the broker's
    flag allowlist, so an agent cannot reach it - which is the point, and why
    this is a design property rather than a gap."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(thread_with_reply("ack"), SENT_MESSAGE)
    code, out, err = run(
        ["messages", "reply", CONVERSATION, "--text=ack", "--idempotency-key=on-purpose"],
        client=client,
    )
    assert code == 0, err
    assert client.posts, "the escape hatch was closed"
    assert client.posts[0][1]["message"]["originToken"] == "on-purpose"
    assert envelope(out)["data"]["deduped"] is False


def test_the_cli_reaches_the_message_writer_from_exactly_one_place():
    """Structural, and it is the point of the fix rather than a nicety.

    Two call sites is how the first guard came to cover one of them: `reply` and
    `send` built the same createMessage body from two branches of one function,
    and only the branch somebody was looking at got the check. One call site
    cannot drift from itself.
    """
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(cli)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
    ]
    assert len(calls) == 1, "a second path into `messaging.send_message` is a second guard to keep"


def test_the_reported_send_into_a_strangers_thread_is_refused():
    """The exact invocation the review reproduced: a recipient this account may
    write to, and a conversation urn hanging in somebody else's mailbox. It
    exited 0 with the message posted into the stranger's thread - `--to` is read
    only when `--conversation` is absent, so the recipient argument made the
    call look addressed while contributing nothing to where it went."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(messages_payload(FOREIGN_CONVERSATION), SENT_MESSAGE)
    code, out, _ = run(
        [
            "messages",
            "send",
            f"--to={MEMBER}",
            f"--conversation={FOREIGN_CONVERSATION}",
            "--text=hi",
        ],
        client=client,
    )
    assert code != 0, out
    assert client.posts == [], "a message was delivered into another member's mailbox"


def test_messages_send_refuses_naming_its_destination_twice():
    """`--to` was read only inside `if not conversation:`, so passing both made
    the recipient look like validation while contributing nothing: the message
    went wherever the urn said and the flag naming a human was dropped. Refused
    rather than resolved, for the reason `mark-all-read <urn>` is refused - a
    flag this CLI would drop is a flag it says no to."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient()
    code, _, err = run(
        ["messages", "send", f"--to={MEMBER}", f"--conversation={CONVERSATION}", "--text=hi"],
        client=client,
    )
    assert code == 2
    body = envelope(err)
    assert body["error"]["code"] == "usage"
    assert "--to" in body["error"]["message"] and "--conversation" in body["error"]["message"]
    assert client.posts == []
    assert client.paths == []


def test_messages_send_still_needs_one_of_the_two_ways_to_name_a_destination():
    """Mutually exclusive, not optional. Neither flag is a send with no address."""
    code, _, err = run(["messages", "send", "--text=hi"], client=FakeClient())
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "--to" in message and "--conversation" in message


def test_reading_a_thread_still_costs_exactly_one_request():
    """The round trip is on the write path only; a read that paid it twice would
    double every page an agent turns."""
    client = FakeClient(MESSAGES_PAYLOAD)
    code, _, err = run(["messages", "read", CONVERSATION], client=client)
    assert code == 0, err
    assert len(client.paths) == 1


def test_messages_reply_dry_run_sends_nothing_real():
    # The member urn is cached rather than fetched: looking it up is a live call,
    # and a dry run refuses to make one.
    state.State().remember("member_urn", MEMBER)
    client = FakeClient({})
    code, out, err = run(
        ["messages", "reply", CONVERSATION, "--text=ack", "--dry-run"], client=client
    )
    assert code == 0, err
    assert client.posts[-1][2] is True
    # The membership read is skipped under a dry run, and both halves of that
    # matter: `_WriteWatch.get` turns any read under `--dry-run` into a preview
    # of *that read*, which would replace the createMessage body the operator is
    # here to approve - and a dry run sends nothing, so there is no reply to
    # confine.
    assert client.paths == ["voyagerMessagingDashMessengerMessages?action=createMessage"]


def test_text_content_flag_does_not_change_output_format():
    """Regression: `--text` is a message body, not a request for prose output.

    Overloading one name meant the first real `messages reply` printed its result
    as human text instead of the JSON envelope an agent parses.
    """
    client = FakeClient(ME_PAYLOAD, MESSAGES_PAYLOAD, SENT_MESSAGE)
    code, out, _ = run(["messages", "reply", CONVERSATION, "--text=ack"], client=client)
    assert code == 0
    body = envelope(out)
    assert body["ok"] is True
    assert body["data"]["conversation_urn"] == CONVERSATION


# --------------------------------------------------------- reactions and comments

# The activity the feed fixture above returns, so the two agree on what a post
# is. Its share urn is a *different id for the same post*, which is why the
# verbs below refuse it instead of converting it.
ACTIVITY = "urn:li:activity:7486933303296000001"
SHARE = "urn:li:share:7486933303296000000"

REACTED_PAYLOAD = {"data": {"data": {"doAddReactionV2": {"value": True}}}}

COMMENTED_PAYLOAD = {
    "data": {
        "$type": "com.linkedin.voyager.dash.social.NormComment",
        "entityUrn": "urn:li:fsd_comment:(7486933303296000002,urn:li:activity:7486933303296000001)",
    }
}

# The comment urn in its two spellings. `COMMENT_URN` is the key the delete
# route takes; `DOUBLED_COMMENT_URN` is what LinkedIn answers a create with, and
# therefore what `comment` prints and what a caller copy-pastes.
COMMENT_URN = COMMENTED_PAYLOAD["data"]["entityUrn"]
DOUBLED_COMMENT_URN = f"urn:li:fsd_normComment:{COMMENT_URN}"

SOCIAL_WRITES = {
    "react": [ACTIVITY],
    "unreact": [ACTIVITY],
    "comment": [ACTIVITY, "--text=nicely put"],
}


def test_the_social_writes_are_registered_and_no_longer_unimplemented():
    """They were `UNIMPLEMENTED` while their payloads were uncaptured. The
    payloads exist now, so an agent must stop being told to wait for them - and
    the table itself is gone, which the sweep in `unrunnable` now enforces from
    the other direction for every verb at once."""
    assert {"react", "unreact", "comment"} <= set(cli.COMMANDS)
    assert unrunnable({"react", "unreact", "comment"}) == set()


def test_react_sends_the_captured_reaction_payload():
    client = FakeClient(REACTED_PAYLOAD)
    code, out, err = run(["react", ACTIVITY], client=client)
    assert code == 0, err
    path, body, dry = client.posts[-1]
    assert path.startswith("graphql?action=execute&queryId=voyagerSocialDashReactions.")
    assert body["variables"] == {"entity": {"reactionType": "LIKE"}, "threadUrn": ACTIVITY}
    assert dry is False
    assert envelope(out)["data"]["reacted"] is True


def test_react_takes_its_reaction_type_from_the_flag():
    client = FakeClient(REACTED_PAYLOAD)
    code, out, err = run(["react", ACTIVITY, "--type=PRAISE"], client=client)
    assert code == 0, err
    assert client.posts[-1][1]["variables"]["entity"] == {"reactionType": "PRAISE"}
    assert envelope(out)["data"]["reaction"] == "PRAISE"


def test_unreact_is_a_different_call_from_react_not_a_flag_on_it():
    """Two content hashes, one endpoint. If these ever addressed the same
    queryId, `unreact` would answer 200 and leave the reaction in place."""
    reacting, unreacting = FakeClient(REACTED_PAYLOAD), FakeClient(REACTED_PAYLOAD)
    assert run(["react", ACTIVITY], client=reacting)[0] == 0
    code, out, err = run(["unreact", ACTIVITY], client=unreacting)
    assert code == 0, err
    assert unreacting.posts[-1][0] != reacting.posts[-1][0]
    assert "entity" not in unreacting.posts[-1][1]["variables"]
    assert envelope(out)["data"]["reacted"] is False


def test_comment_sends_the_text_and_returns_the_comment_it_created():
    client = FakeClient(COMMENTED_PAYLOAD)
    code, out, err = run(["comment", ACTIVITY, "--text=nicely put"], client=client)
    assert code == 0, err
    path, body, _ = client.posts[-1]
    assert path.startswith("voyagerSocialDashNormComments?decorationId=")
    assert body["commentary"]["text"] == "nicely put"
    assert body["threadUrn"] == ACTIVITY
    assert envelope(out)["data"]["comment_urn"] == COMMENTED_PAYLOAD["data"]["entityUrn"]


def test_comment_without_text_is_a_usage_error():
    client = FakeClient(COMMENTED_PAYLOAD)
    code, _, err = run(["comment", ACTIVITY], client=client)
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert client.posts == []


@pytest.mark.parametrize("verb", sorted(SOCIAL_WRITES))
def test_a_social_write_needs_a_post_to_act_on(verb):
    code, _, err = run([verb] + SOCIAL_WRITES[verb][1:], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


@pytest.mark.parametrize("verb", sorted(SOCIAL_WRITES))
def test_a_share_urn_is_refused_rather_than_converted(verb):
    """A share urn and an activity urn are different ids for the same post.
    Guessing a conversion would react to whatever that id turned out to be."""
    args = [verb, SHARE] + SOCIAL_WRITES[verb][1:]
    client = FakeClient()
    code, _, err = run(args, client=client)
    assert code == 2
    body = envelope(err)
    assert body["error"]["code"] == "usage"
    assert "urn:li:activity:" in body["error"]["message"]
    assert client.posts == [], "a request went out for an urn this CLI cannot use"


def test_comment_delete_removes_the_comment_and_reports_what_it_removed():
    """The undo `comment` never had. Verified live; the route, the doubled urn
    and the asymmetric collections are in docs/write-payloads.md."""
    client = FakeClient({})
    code, out, err = run(["comment", "delete", COMMENT_URN], client=client)
    assert code == 0, err
    method, path, body, _ = client.requests[-1]
    assert method == "DELETE"
    assert path.startswith("voyagerSocialDashNormComments/")
    assert body is None
    data = envelope(out)["data"]
    assert data["comment_urn"] == COMMENT_URN
    assert data["deleted"] is True


def test_comment_delete_accepts_the_urn_comment_itself_reports():
    """`comment` hands back the doubled urn because that is what LinkedIn
    answers with, so the copy-paste an agent will actually make has to work."""
    client = FakeClient({})
    code, out, err = run(["comment", "delete", DOUBLED_COMMENT_URN], client=client)
    assert code == 0, err
    assert envelope(out)["data"]["comment_urn"] == COMMENT_URN


def test_comment_delete_refuses_an_activity_urn_before_anything_is_sent():
    """The likeliest mistake: it is the urn every other social write takes, and
    a deleted comment cannot be put back."""
    client = FakeClient({})
    code, _, err = run(["comment", "delete", ACTIVITY], client=client)
    assert code == 2
    assert client.requests == []
    assert "comment_urn" in envelope(err)["error"]["message"]


def test_comment_delete_without_a_urn_is_a_usage_error():
    client = FakeClient({})
    code, _, err = run(["comment", "delete"], client=client)
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert client.requests == []


def test_comment_delete_refuses_the_create_flag_rather_than_dropping_it():
    """`--text` parses under `comment` and this action never reads it, which is
    the silence the flag allowlist exists to end - and here it reads like an
    edit that is really a removal."""
    client = FakeClient({})
    code, _, err = run(["comment", "delete", COMMENT_URN, "--text=oops"], client=client)
    assert code == 2
    assert client.requests == []
    assert "--text" in envelope(err)["error"]["message"]


def test_comment_delete_books_a_cleanup_and_leaves_the_comment_budget_alone():
    """The inverse of `comment`, so it books under `comment.cleanup`: recorded
    and paced like any write, but never refused by the cap that the run holding
    a live comment it wants back has very often already spent."""
    run(["comment", ACTIVITY, "--text=oops"], client=FakeClient(COMMENTED_PAYLOAD))
    run(["comment", "delete", COMMENT_URN], client=FakeClient({}))
    assert state.State().write_count("comment", state.DAY) == 1
    assert state.State().write_count(state.cleanup_kind("comment"), state.DAY) == 1


def test_comment_delete_is_not_refused_by_a_spent_comment_cap(monkeypatch):
    """The concrete reason the exemption exists: a run that comments and then
    trips its own cap would otherwise strand a live comment with no CLI way to
    remove it."""
    monkeypatch.setitem(state.DAILY_CAPS, "comment", 1)
    assert run(["comment", ACTIVITY, "--text=oops"], client=FakeClient(COMMENTED_PAYLOAD))[0] == 0
    assert run(["comment", ACTIVITY, "--text=again"], client=FakeClient(COMMENTED_PAYLOAD))[0] == 5
    assert run(["comment", "delete", COMMENT_URN], client=FakeClient({}))[0] == 0


def test_comment_delete_is_still_refused_once_its_cleanup_ceiling_is_reached(monkeypatch):
    """Exempt from the caps is not exempt from arithmetic. `state._guard_cleanup`
    is what stops a wedged undo loop, and it reaches every cleanup verb - the
    newest one included."""
    monkeypatch.setitem(state.DAILY_CAPS, "comment", 1)
    ceiling = state.cleanup_ceiling("comment")
    for _ in range(ceiling):
        assert run(["comment", "delete", COMMENT_URN], client=FakeClient({}))[0] == 0
    assert run(["comment", "delete", COMMENT_URN], client=FakeClient({}))[0] == 5


# ------------------------------------------------------------- the write ledger


@pytest.mark.parametrize(
    "verb,kind",
    [
        ("react", "react"),
        ("unreact", state.cleanup_kind("react")),
        ("comment", "comment"),
    ],
)
def test_a_social_write_is_recorded_against_its_daily_budget(verb, kind):
    """`unreact` books under `react.cleanup`, not `react`: it is still recorded,
    because undoing a reaction is still traffic, but a cleanup row is one the
    caps and the breaker cannot refuse. Booking it as an ordinary react made the
    undo refusable by the limit that made it necessary - see
    `test_unreact_still_works_once_the_reaction_cap_is_spent`."""
    client = FakeClient(REACTED_PAYLOAD if verb != "comment" else COMMENTED_PAYLOAD)
    code, _, err = run([verb] + SOCIAL_WRITES[verb], client=client)
    assert code == 0, err
    assert state.State().write_count(kind, state.DAY) == 1


@pytest.mark.parametrize("verb,kind", [("react", "react"), ("comment", "comment")])
def test_a_social_write_stops_at_the_cap_before_it_is_sent(verb, kind, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, kind, 1)
    first = FakeClient(REACTED_PAYLOAD if kind == "react" else COMMENTED_PAYLOAD)
    assert run([verb] + SOCIAL_WRITES[verb], client=first)[0] == 0

    second = FakeClient(REACTED_PAYLOAD if kind == "react" else COMMENTED_PAYLOAD)
    code, _, err = run([verb] + SOCIAL_WRITES[verb], client=second)
    assert code == 5
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert second.posts == [], "the cap was checked after the write went out"


def test_a_write_whose_outcome_is_unconfirmed_still_costs_quota():
    """The cap is on what this client *sends*. A response that proves nothing
    does not mean nothing was applied, so it cannot be refunded."""
    client = FakeClient({})
    code, _, err = run(["react", ACTIVITY], client=client)
    assert code == 6
    assert envelope(err)["error"]["code"] == "upstream"
    assert state.State().write_count("react", state.DAY) == 1


def test_a_ledger_failure_cannot_replace_the_writes_own_outcome(monkeypatch):
    """Settling the claim happens in a `finally`. If it raised, the write's own
    outcome would be *replaced* by a disk error and the caller would lose the one
    message telling it what actually happened to the request."""

    def explode(self, kind, at):
        raise OSError("read-only file system")

    monkeypatch.setattr(state.State, "_release_claim", explode)
    code, _, err = run(["react", SHARE], client=FakeClient())
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "urn:li:activity:" in message
    assert "read-only file system" not in message


def test_a_write_refused_before_it_is_sent_costs_no_quota():
    code, _, _ = run(["react", SHARE], client=FakeClient())
    assert code == 2
    assert state.State().write_count("react", state.DAY) == 0


# ------------------------------------------------------------- undoing a write


def test_unreact_still_works_once_the_reaction_cap_is_spent(monkeypatch):
    """`unreact` is the only undo this CLI has, and the run that most needs it is
    the one that hit the cap: every reaction it placed is live and the verb that
    takes them back is the verb the cap just refused. An undo that a limit can
    withhold leaves the account dirtier than no limit would have."""
    monkeypatch.setitem(state.DAILY_CAPS, "react", 1)
    assert run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))[0] == 0
    assert run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))[0] == 5

    client = FakeClient(REACTED_PAYLOAD)
    code, _, err = run(["unreact", ACTIVITY], client=client)
    assert code == 0, err
    assert client.posts, "the undo never reached the wire"


def test_unreact_still_works_while_the_breaker_is_open():
    """Same argument one layer up: the breaker refuses a whole verb before its
    handler is dispatched, so exempting the ledger alone still strands the
    reaction. Nothing else is exempt - `react` must stay refused."""
    state.State().trip_breaker("HTTP 999")
    client = FakeClient(REACTED_PAYLOAD)
    code, _, err = run(["unreact", ACTIVITY], client=client)
    assert code == 0, err
    assert client.posts, "the undo never reached the wire"
    blocked = FakeClient(REACTED_PAYLOAD)
    assert run(["react", ACTIVITY], client=blocked)[0] == 9
    assert blocked.posts == [], "the breaker let a fresh write through"


def test_undoing_a_reaction_does_not_spend_the_reaction_budget():
    """A react-and-undo pair that cost two would let a run that cleans up after
    itself exhaust the day faster than one that leaves its mess live."""
    run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))
    run(["unreact", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))
    assert state.State().write_count("react", state.DAY) == 1
    assert state.State().write_count(state.cleanup_kind("react"), state.DAY) == 1


def test_the_undo_exemption_reaches_every_cleanup_action_that_exists():
    """A guard keyed on the verb alone cannot see that `post delete` is an undo
    while `post create` is not. This pins the mapping so a cleanup action added
    later to an existing verb has to be added here too, rather than silently
    inheriting the create's limits."""
    assert cli.CLEANUP_ACTIONS == {
        "unreact": None,
        "post": frozenset({"delete"}),
        "comment": frozenset({"delete"}),
    }


@pytest.mark.parametrize("verb", sorted(SOCIAL_WRITES))
def test_a_dry_run_previews_a_social_write_and_spends_nothing(verb):
    client = FakeClient()
    code, _, err = run([verb] + SOCIAL_WRITES[verb] + ["--dry-run"], client=client)
    assert code == 0, err
    assert client.posts[-1][2] is True
    assert state.State().write_count("react", state.DAY) == 0
    assert state.State().write_count("comment", state.DAY) == 0


def test_a_dry_run_is_still_previewable_once_the_daily_cap_is_gone(monkeypatch):
    """The cap is on writes that go out, and a preview is not one of them.

    Checking the cap on a `--dry-run` would take the day's *last* useful command
    away at exactly the moment an operator most wants it: budget exhausted, and
    the one thing left that costs nothing is being told what the request would
    have been. `--dry-run` therefore skips the ledger entirely rather than
    reading it and passing.
    """
    monkeypatch.setitem(state.DAILY_CAPS, "react", 1)
    assert run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))[0] == 0
    assert run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))[0] == 5

    client = FakeClient(REACTED_PAYLOAD)
    code, _, err = run(["react", ACTIVITY, "--dry-run"], client=client)
    assert code == 0, err
    assert client.posts[-1][2] is True
    # Still one, not two: the preview neither spent against the cap nor was
    # refused by it.
    assert state.State().write_count("react", state.DAY) == 1


def test_a_dry_run_react_asks_the_supervisor_for_nothing_but_status():
    """End to end against a real `BrowserClient`: the preview an operator
    approves a write from is built there, and the whole point is that the
    request was never issued."""
    asked = []

    def request_fn(payload, **kwargs):
        asked.append(payload)
        if payload.get("op") == "status":
            return {"pid": 4242, "profile": "/managed"}
        raise AssertionError("--dry-run made a real request")

    client = browser.BrowserClient(rate=1.0, state=NoPace(), request_fn=request_fn)
    code, out, err = run(["react", ACTIVITY, "--dry-run"], client=client)
    assert code == 0, err
    preview = envelope(out)["data"]
    assert preview["method"] == "POST"
    # The absolute URL from the capture, assembled from `transport.BASE` and the
    # surface's path - the one thing neither the surface tests nor the browser
    # tests can check on their own.
    assert preview["url"] == (
        "https://www.linkedin.com/voyager/api/graphql?action=execute"
        f"&queryId={social.QUERY_IDS['react']}"
    )
    assert preview["body"]["variables"]["threadUrn"] == ACTIVITY
    assert [p["op"] for p in asked] == ["status"]
    assert preview["headers"]["csrf-token"] == transport.REDACTED


# ------------------------------------------------- the ledger and message writes

# Every message write, with the arguments that reach the send without a lookup.
# `--conversation` is passed so these test the ledger rather than the search -
# and without `--to`, which names the same destination a second way and is
# refused alongside it.
MESSAGE_WRITES = {
    "send": [f"--conversation={CONVERSATION}", "--text=hi"],
    "reply": [CONVERSATION, "--text=hi"],
    "mark-all-read": ["--yes"],
}


def message_write_client(action: str) -> FakeClient:
    """A client queued for one message write, whatever it costs to get there.

    `send` and `reply` both read the thread back before they send into it - they
    are one call site - so their queues start with that page; `mark-all-read`
    addresses the whole mailbox and goes straight to the wire. Written as a
    function rather than a longer table because the difference is one request
    and naming it here keeps the ledger tests about the ledger.
    """
    payloads = [] if action == "mark-all-read" else [MESSAGES_PAYLOAD]
    return FakeClient(*payloads, SENT_MESSAGE)


@pytest.mark.parametrize("action", sorted(MESSAGE_WRITES))
def test_a_message_write_is_recorded_against_the_message_budget(action):
    """`DAILY_CAPS["message"]` was enforced nowhere: `cmd_messages` called the
    surface directly, and `_write` - the only caller of the ledger - was reached
    from react/unreact/comment alone. The one bound on a prompt-injected agent
    sending DMs was the 1 req/s pacer."""
    state.State().remember("member_urn", MEMBER)
    client = message_write_client(action)
    code, _, err = run(["messages", action] + MESSAGE_WRITES[action], client=client)
    assert code == 0, err
    assert client.posts, "nothing was sent, so this proves nothing about the ledger"
    assert state.State().write_count("message", state.DAY) == 1


@pytest.mark.parametrize("action", sorted(MESSAGE_WRITES))
def test_a_message_write_stops_at_the_cap_before_it_is_sent(action, monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "message", 1)
    state.State().remember("member_urn", MEMBER)
    assert (
        run(["messages", action] + MESSAGE_WRITES[action], client=message_write_client(action))[0]
        == 0
    )

    second = FakeClient()
    code, _, err = run(["messages", action] + MESSAGE_WRITES[action], client=second)
    assert code == 5
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert second.posts == [], "the cap was checked after the message went out"


def test_a_message_refused_before_it_is_sent_costs_no_quota():
    state.State().remember("member_urn", MEMBER)
    code, _, _ = run(["messages", "reply", CONVERSATION], client=FakeClient())
    assert code == 2
    assert state.State().write_count("message", state.DAY) == 0


def test_a_write_takes_one_atomic_claim_rather_than_check_then_record(monkeypatch):
    """Checking the cap under one lock and appending under another lets two
    invocations both read "one below the cap" and both write. Both halves of the
    old pair are stubbed out here, so a `_write` still using either fails."""

    def gone(self, *a, **kw):
        raise AssertionError("the non-atomic cap path is still in use")

    monkeypatch.setattr(state.State, "check_write_allowed", gone)
    monkeypatch.setattr(state.State, "record_write", gone)
    code, _, err = run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))
    assert code == 0, err
    assert state.State().write_count("react", state.DAY) == 1


def test_notifications_list_projects_cards():
    client = FakeClient(NOTIFICATIONS_PAYLOAD)
    code, out, _ = run(["notifications", "list", "--count=1"], client=client)
    assert code == 0
    body = envelope(out)
    assert body["data"][0]["type"] == "REACTED_TO_YOUR_POST"
    assert body["next_cursor"] == "11"
    assert "count=1" in client.paths[0]


def test_notifications_cursor_is_threaded_through():
    client = FakeClient(NOTIFICATIONS_PAYLOAD)
    run(["notifications", "list", "--cursor=11"], client=client)
    assert "start=11" in client.paths[0]


def test_count_flag_must_be_a_number():
    code, _, err = run(["feed", "list", "--count=lots"], client=FakeClient(FEED_PAYLOAD))
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


@pytest.mark.parametrize("argv", [["notifications", "mark-read", "x"]])
def test_an_uncaptured_verb_says_so_instead_of_guessing_a_body(argv):
    """Shipping a guessed request is how an account gets restricted; an agent
    cannot tell a guess from a fact, so these exit 2 and say why.

    This is a *write* whose payload nobody has recorded, which is a gap a capture
    run can still close - and the message names the method. That is not true of
    the invitation stubs, which are refused for a reason no capture can change;
    they are checked separately, and the two must not drift back into one
    message. `comment delete` left this list when its route was verified live -
    by read-back against our own throwaway post rather than by interception,
    which docs/write-payloads.md argues for at length.
    """
    code, _, err = run(argv, client=FakeClient())
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "not implemented" in message
    assert "has not been captured" in message
    assert "interception" in message


# ------------------------------------------------------------------ the member urn


def test_the_member_urn_is_recalled_from_the_ledger_rather_than_refetched():
    """It used to live in the session file the pivot deleted. Every messaging
    call addresses a mailbox by it, so re-fetching it per invocation would
    double the request rate of an account whose one remaining safety control is
    how rarely it calls."""
    state.State().remember("member_urn", MINI_MEMBER)
    client = FakeClient(CONVERSATIONS_PAYLOAD)
    code, out, _ = run(["messages", "list", "--count=1"], client=client)
    assert code == 0
    assert envelope(out)["data"][0]["conversation_urn"] == CONVERSATION
    assert "me" not in client.paths


def test_the_recalled_urn_is_rewritten_into_the_form_messaging_accepts():
    """`me` hands back the `fs_miniProfile` spelling and messaging wants
    `fsd_profile`; they are the same id under different decorations, so it is
    rewritten rather than costing a request to rediscover."""
    state.State().remember("member_urn", MINI_MEMBER)
    client = FakeClient(CONVERSATIONS_PAYLOAD)
    run(["messages", "list"], client=client)
    assert "fsd_profile" in client.paths[0]
    assert "fs_miniProfile" not in client.paths[0]


def test_the_member_urn_is_remembered_the_first_time_it_is_fetched():
    client = FakeClient(ME_PAYLOAD, CONVERSATIONS_PAYLOAD)
    code, _, _ = run(["messages", "list"], client=client)
    assert code == 0
    assert client.paths[0] == "me"
    assert state.State().recall("member_urn") == MEMBER

    # ...and the next invocation, which is a fresh process, spends no request on it
    second = FakeClient(CONVERSATIONS_PAYLOAD)
    run(["messages", "list"], client=second)
    assert "me" not in second.paths


def test_a_profile_that_cannot_name_the_member_says_how_to_fix_it():
    client = FakeClient({"data": {}})
    code, _, err = run(["messages", "list"], client=client)
    assert code == 2
    assert "auth seed" in envelope(err)["error"]["message"]


def test_messages_list_honours_unread_only():
    state.State().remember("member_urn", MEMBER)
    code, out, _ = run(
        ["messages", "list", "--unread-only"], client=FakeClient(CONVERSATIONS_PAYLOAD)
    )
    assert len(envelope(out)["data"]) == 1


# ------------------------------------------------------------------------- --rate


@pytest.mark.parametrize("raw", ["0", "0.0", "-1", "-0.5", "abc", "999", "1.5", "nan", "inf"])
def test_a_rate_outside_the_accepted_range_is_a_usage_error(raw):
    """`--rate=0` used to disable pacing *and* skip writing the timestamp the
    next invocation paces against, so an agent in a loop was unpaced from then
    on. Pacing is the only behavioural control left after the pivot, and the
    credential broker's allowlist is granular to the verb, so it cannot block a
    flag."""
    client = FakeClient(ME_PAYLOAD)
    code, _, err = run(["me", f"--rate={raw}"], client=client)
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert client.paths == [], "the rate was validated after a request went out"


@pytest.mark.parametrize("raw", ["1", "1.0", "0.5", "0.001"])
def test_a_rate_inside_the_accepted_range_is_honoured(raw):
    code, _, _ = run(["me", f"--rate={raw}"], client=FakeClient(ME_PAYLOAD))
    assert code == 0
    assert cli.Context(args=[], flags={"rate": raw}, stdout=None, stderr=None).rate == float(raw)


def test_an_absent_rate_paces_at_one_request_a_second():
    assert cli.Context(args=[], flags={}, stdout=None, stderr=None).rate == 1.0


def test_the_validated_rate_is_what_the_client_is_built_with(monkeypatch):
    """Validating a value the client never receives would prove nothing."""
    seen = {}

    class Recorder:
        def __init__(self, *, rate, state=None, request_fn=None):
            seen["rate"] = rate

    monkeypatch.setattr(browser, "BrowserClient", Recorder)
    cli.Context(args=[], flags={"rate": "0.25"}, stdout=None, stderr=None).client
    assert seen == {"rate": 0.25}


# ------------------------------------------------------------------ the transport


def test_the_cookie_extraction_tower_is_gone():
    """The pivot's security argument is that no process outside the supervisor
    holds `li_at`. A module that reads one, left importable, is the argument
    quietly coming undone."""
    for gone in ("session", "chrome", "_aes"):
        with pytest.raises(ImportError):
            importlib.import_module(f"linkedin_cli.{gone}")


def test_the_only_transport_is_the_browser():
    built = cli.Context(args=[], flags={}, stdout=None, stderr=None).client
    assert isinstance(built.inner, browser.BrowserClient)


def test_a_stale_transport_flag_cannot_reach_a_deleted_transport():
    """`--transport=direct` was the urllib client. An agent still passing it
    must get the browser, not a resurrection and not a crash."""
    built = cli.Context(args=[], flags={"transport": "direct"}, stdout=None, stderr=None).client
    assert isinstance(built.inner, browser.BrowserClient)


def test_building_a_client_starts_nothing():
    """conftest turns any socket or spawn into a failure; this asserts the
    lazy build is reached at all, so that guard is actually exercised."""
    ctx = cli.Context(args=[], flags={}, stdout=None, stderr=None)
    assert ctx.client is ctx.client


# ------------------------------------------------------------------------- auth


def test_auth_seed_dispatches_to_the_bootstrap_module(monkeypatch):
    from linkedin_cli import bootstrap

    monkeypatch.setattr(bootstrap, "seed", lambda *a, **kw: {"seeded": True, "cookies": 3})
    code, out, _ = run(["auth", "seed"])
    assert code == 0
    assert envelope(out)["data"]["seeded"] is True


def _recording_seed(seen):
    """A stub with `bootstrap.seed`'s real signature.

    Deliberately not `**kwargs`: `cmd_auth` spent a while calling this with
    `source_profile=` for a parameter named `source_profile_path`, so every
    `linkedin auth seed` died with a `TypeError` at exit 6 - the one command
    every remediation string in the package tells an operator to run. A
    permissive stub would have passed the whole time it was broken.
    """

    def seed(source_profile_path=None, *, request=None, reader=None):
        seen["path"] = source_profile_path
        return {"seeded": True}

    return seed


def test_auth_seed_forwards_the_operators_source_profile(monkeypatch):
    from linkedin_cli import bootstrap

    seen: dict = {}
    monkeypatch.setattr(bootstrap, "seed", _recording_seed(seen))
    code, _, err = run(["auth", "seed", "--from-profile=/tmp/chrome"])
    assert code == 0, err
    assert seen == {"path": "/tmp/chrome"}


def test_auth_seed_without_a_source_profile_lets_bootstrap_choose(monkeypatch):
    from linkedin_cli import bootstrap

    seen: dict = {}
    monkeypatch.setattr(bootstrap, "seed", _recording_seed(seen))
    code, _, err = run(["auth", "seed"])
    assert code == 0, err
    assert seen == {"path": None}


def test_auth_status_reports_the_managed_profile():
    code, out, _ = run(["auth", "status"], client=FakeClient(ME_PAYLOAD))
    assert code == 0
    data = envelope(out)["data"]
    assert data["signed_in"] is True
    assert data["member_urn"] == MEMBER


def test_auth_export_is_not_a_verb():
    """Its only possible output is a live `li_at` on stdout, which under an agent
    gateway is permanent model context - and dispatch is granular to the verb, so
    an allowlist permitting `seed` could not have denied `export`."""
    code, _, err = run(["auth", "export"], client=FakeClient(ME_PAYLOAD))
    assert code == 2
    assert "export" in envelope(err)["error"]["message"]


def test_auth_needs_an_action():
    code, _, err = run(["auth"], client=FakeClient())
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"


def test_the_usage_text_names_only_verbs_that_exist():
    assert "auth seed" in cli.USAGE
    assert "auth sync" not in cli.USAGE
    assert "auth export" not in cli.USAGE
    assert "--transport" not in cli.USAGE


# ------------------------------------------------------------------ the breaker


def test_a_blocked_response_trips_the_breaker():
    """A 999 must stop the *next* invocation, or an agent in a loop keeps digging."""
    code, _, err = run(["feed", "list"], client=FakeClient(error=transport.Blocked("999")))
    assert code == 9
    assert envelope(err)["error"]["code"] == "blocked"
    assert state.State().breaker_state() is not None


def test_an_open_breaker_refuses_the_next_command():
    state.State().trip_breaker("HTTP 999")
    client = FakeClient(FEED_PAYLOAD)
    code, _, err = run(["feed", "list"], client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "blocked"
    assert client.paths == [], "the command was sent despite an open breaker"


def test_an_open_breaker_still_allows_doctor_and_auth():
    """Refusing the diagnostic that explains the refusal would be a dead end."""
    state.State().trip_breaker("HTTP 999")
    code, out, _ = run(["doctor"], client=FakeClient(error=transport.Blocked("999")))
    assert code == 0
    assert envelope(out)["data"]["breaker"]["reason"] == "HTTP 999"


def test_doctor_can_clear_the_breaker():
    state.State().trip_breaker("HTTP 999")
    code, out, _ = run(
        ["doctor", "--clear-breaker"], client=FakeClient(error=transport.Blocked("x"))
    )
    assert code == 0
    assert envelope(out)["data"]["breaker"] is None
    assert state.State().breaker_state() is None


def test_doctor_does_not_trip_the_breaker_on_a_999():
    """The probe is deliberate, and the breaker it would set is the one thing
    stopping the operator from running doctor again."""
    run(["doctor"], client=FakeClient(error=transport.Blocked("999")))
    assert state.State().breaker_state() is None


# ------------------------------------------- the breaker that cannot be consulted

# Measured: with `LINKEDIN_STATE_FILE` pointed at a path under a
# directory that cannot exist, `_guard_breaker` read the breaker through `_safe`,
# got `None` for the exception and let every write through - `None` being exactly
# what a *closed* breaker also looks like. A client LinkedIn has already blocked
# then goes on calling LinkedIn, which is the documented road from a soft block
# to a restriction.
#
# Two ways the same question goes unanswered, and both are exercised below: the
# file cannot be reached at all (raises), and the file is reachable but will not
# parse (`state._read` fails open, so the breaker reads as closed whether or not
# one was recorded in it).


def unreachable_ledger(monkeypatch, tmp_path) -> str:
    """Point the ledger at a path under a *file*, so it can neither be read nor
    created. Portable stand-in for the unwritable directory this was measured on.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory")
    path = blocker / "state.json"
    monkeypatch.setenv("LINKEDIN_STATE_FILE", str(path))
    return str(path)


def corrupt_ledger() -> str:
    """A ledger that is present and unparseable - see `state._read_checked`."""
    path = state.resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")
    return str(path)


@pytest.mark.parametrize("argv", [["react", ACTIVITY], ["post", "create", "--text=hi"]])
def test_a_write_is_refused_when_the_breaker_cannot_be_read(argv, monkeypatch, tmp_path):
    """Fail closed. The breaker exists to stop the next invocation, and an
    invocation that cannot tell whether one is open has to assume it is."""
    unreachable_ledger(monkeypatch, tmp_path)
    client = FakeClient()
    code, _, err = run(argv, client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "ledger_unreadable"
    assert client.posts == [], "a write went out with the breaker unverifiable"


def test_an_undo_is_refused_too_when_the_breaker_cannot_be_read(monkeypatch, tmp_path):
    """The one exemption that does *not* survive this.

    An open breaker cannot refuse a cleanup - `test_unreact_still_works_while_the
    _breaker_is_open` - because the run holding something it wants to take back
    is often the run that just got blocked. That argument needs a ledger: the
    undo is bounded by `state.cleanup_ceiling`, counted out of the same file, so
    letting it through here restores exactly the unmetered write channel the
    ceiling closes. `state._guard_readable` refuses a cleanup for that reason;
    this refuses it one layer earlier, before the lookups a write does first.
    """
    unreachable_ledger(monkeypatch, tmp_path)
    client = FakeClient()
    code, _, err = run(["unreact", ACTIVITY], client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "ledger_unreadable"
    assert client.posts == []


@pytest.mark.parametrize(
    "argv,payload",
    [
        (["me"], "ME_PAYLOAD"),
        (["feed", "list"], "FEED_PAYLOAD"),
        (["post", "get", ACTIVITY], "FEED_PAYLOAD"),
    ],
)
def test_a_read_still_works_when_the_breaker_cannot_be_read(argv, payload, monkeypatch, tmp_path):
    """Fail open for reads, and this half is not a concession.

    A read is how an operator finds out what happened - `post get` on the urn a
    half-finished write returned is the read-back this whole project turns on -
    and a CLI that refuses to look at the account because of a local file is a
    CLI that sends its operator to the browser at the worst moment. Reads also
    cannot make a soft block worse in the way a write can: nothing they do is
    visible to another human.
    """
    unreachable_ledger(monkeypatch, tmp_path)
    code, out, err = run(argv, client=FakeClient(globals()[payload]))
    assert code == 0, err
    assert envelope(out)["ok"] is True


def test_the_unverifiable_breaker_refusal_names_the_file_and_how_to_clear_it(monkeypatch, tmp_path):
    """The agent reading this cannot see the host, the environment or which file
    `LINKEDIN_STATE_FILE` moved the ledger to, so the message carries all three -
    the same standard `state._guard_readable` holds its own refusal to."""
    path = unreachable_ledger(monkeypatch, tmp_path)
    _, _, err = run(["react", ACTIVITY], client=FakeClient())
    message = envelope(err)["error"]["message"]
    assert path in message
    assert f"mv {path}" in message
    assert unrunnable(named_commands(message)) == set()


def test_a_corrupt_ledger_refuses_a_write_before_it_reaches_linkedin(monkeypatch, tmp_path):
    """The quieter half: the file is readable, so nothing raises - `state._read`
    fails open and the breaker reads as closed whether or not one was written in
    it. `state.claim_write` refuses this, but only once the write is ready to
    send, and `invite` issues a profile lookup on the way there. A request from a
    client whose breaker is unverifiable is the thing being prevented.
    """
    corrupt_ledger()
    client = FakeClient(INVITEE_PAYLOAD, INVITED_PAYLOAD)
    code, _, err = run(["invite", "synthetic-invitee"], client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "ledger_unreadable"
    assert client.paths == [], "the lookup went out before the ledger was checked"


def test_the_two_unreadable_ledger_refusals_are_one_kind_and_one_exit_code():
    """`cli` and `state` answer the same question in two places - before dispatch
    and at the claim - and an agent that saw two different codes for one cause
    would have to parse the message to tell them apart."""
    assert issubclass(state.LedgerUnreadable, state.Blocked)
    assert cli._report(state.LedgerUnreadable("x")) == ("ledger_unreadable", 9, False)
    assert cli._report(state.Blocked("x")) == ("blocked", 9, False)


# Two answers to one question is one answer too many. `state._prune` expires the
# lost-history marker after a week, on the argument that a host whose operator
# never read the message must not refuse writes forever over history that stopped
# existing days ago - and `_guard_breaker` decided readability off a view that had
# never been pruned. So a marker older than WEEK made this refuse before dispatch
# while `claim_write`, on the same file at the same instant, allowed the write.
# On a write-only workflow that is permanent: exit 9 before anything runs, and
# nothing ever gets far enough to prune the marker away.


def lost_history_ledger(age: float) -> Path:
    """A ledger whose only content is a lost-history marker of the given age."""
    path = state.resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({state.HISTORY_LOST_KEY: time.time() - age}))
    return path


@pytest.mark.parametrize(
    "age,write_allowed",
    [
        # Past the longest enforcement window: every write the lost ledger could
        # have held has aged out of both caps, so the marker has stopped meaning
        # anything and the host is capped normally again.
        (state.WEEK + 10, True),
        # Inside it. Nothing is known about the day, so nothing can be enforced.
        (10, False),
    ],
)
def test_the_cli_and_the_claim_agree_about_a_lost_history_marker(age, write_allowed):
    """One file, one instant, one verdict. Which of the two paths is asked must
    not change the answer, because they are the same fail-closed policy."""

    def cli_allows():
        lost_history_ledger(age)
        try:
            cli._guard_breaker("invite", ["synthetic-invitee"])
        except state.LedgerUnreadable:
            return False
        return True

    def claim_allows():
        lost_history_ledger(age)
        try:
            state.State().claim_write("invite")
        except state.LedgerUnreadable:
            return False
        return True

    # Evaluated over a freshly seeded file each time: `claim_write` prunes and
    # rewrites, so asking it first would hand the other path a different file.
    assert cli_allows() == claim_allows() == write_allowed


def test_a_write_only_workflow_is_not_bricked_by_an_expired_marker():
    """What the disagreement cost in practice. `_guard_breaker` runs before
    dispatch, so an agent that only ever writes was refused at exit 9 before
    reaching the claim that would have pruned the marker - forever, on a host
    whose ledger stopped being a mystery a week ago."""
    lost_history_ledger(state.WEEK + 10)
    code, _, err = run(["react", ACTIVITY], client=FakeClient(REACTED_PAYLOAD))
    assert code == 0, err


# ------------------------------------------------------------------ the throttle


def test_a_throttled_response_is_persisted_for_the_next_invocation(monkeypatch):
    """Every invocation is a fresh process. A 429 that lived only in this one
    would leave the next agent call starting as if it had never been told to
    slow down - and a 429 is the loudest warning before a restriction."""
    monkeypatch.setitem(
        cli.COMMANDS, "boom", lambda ctx: (_ for _ in ()).throw(transport.RateLimited("HTTP 429"))
    )
    code, _, err = run(["boom"])
    assert code == 5
    assert envelope(err)["error"]["code"] == "rate_limited"

    throttle = state.State().throttle_state()
    assert throttle is not None
    assert "429" in throttle["reason"]


def test_a_throttle_in_force_refuses_the_next_write_before_it_is_sent():
    state.State().record_throttle("HTTP 429")
    client = FakeClient(REACTED_PAYLOAD)
    code, _, err = run(["react", ACTIVITY], client=client)
    assert code == 5
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert client.posts == []


def test_doctor_reports_a_throttle_but_does_not_clear_it():
    """An agent can run `doctor --clear-breaker` too, and clearing the cooldown
    it just earned is the loop the cooldown exists to stop. It expires on its
    own; what an operator needs is to be able to see it."""
    state.State().record_throttle("HTTP 429")
    code, out, _ = run(["doctor", "--clear-breaker"], client=FakeClient())
    assert code == 0
    assert envelope(out)["data"]["throttle"]["reason"] == "HTTP 429"
    assert state.State().throttle_state() is not None


# ------------------------------------------------------- the dead-session breaker


def test_two_dead_session_responses_do_not_arm_the_breaker():
    for _ in range(2):
        code, _, _ = run(["feed", "list"], client=signed_out_client())
        assert code == 3
    assert state.State().breaker_state() is None


def test_three_dead_session_responses_arm_the_breaker():
    """There is no re-sync any more, so an exit 3 that reaches an agent means the
    profile is signed out and the remedy is one only the operator can run. An
    agent treating exit 3 as retryable would otherwise keep issuing authenticated
    requests against a session LinkedIn just ended - the documented road from a
    soft block to a restriction."""
    for _ in range(3):
        code, _, _ = run(["feed", "list"], client=signed_out_client())
        assert code == 3

    breaker = state.State().breaker_state()
    assert breaker is not None
    assert "signed out" in breaker["reason"]

    # ...and the next invocation is refused before it reaches LinkedIn at all
    client = FakeClient(FEED_PAYLOAD)
    code, _, err = run(["feed", "list"], client=client)
    assert code == 9
    assert client.paths == []


def test_a_success_between_failures_resets_the_count():
    """The counter is for a *run* of dead sessions. Two failures a week apart
    with working calls in between are not a signed-out profile."""
    for _ in range(2):
        assert run(["feed", "list"], client=signed_out_client())[0] == 3
    assert run(["feed", "list"], client=FakeClient(FEED_PAYLOAD))[0] == 0
    for _ in range(2):
        assert run(["feed", "list"], client=signed_out_client())[0] == 3
    assert state.State().breaker_state() is None

    # the third consecutive one after the reset still arms it
    assert run(["feed", "list"], client=signed_out_client())[0] == 3
    assert state.State().breaker_state() is not None


def test_the_exempt_commands_neither_arm_the_breaker_nor_clear_it():
    """`auth` and `doctor` are what explain and repair a block, so they stay
    outside the count in both directions - a diagnostic that reset the counter
    could mask the very run of failures it was called to investigate."""
    for _ in range(5):
        assert run(["auth", "status"], client=signed_out_client())[0] == 3
    assert state.State().breaker_state() is None


def test_doctor_never_counts_a_dead_session_against_the_breaker():
    for _ in range(5):
        assert run(["doctor"], client=signed_out_client())[0] == 0
    assert state.State().breaker_state() is None


# ------------------------------------------------------------------- invitations

# What `profiles?q=memberIdentity` hands back, reduced to the one field
# `profile.resolve_urn` reads: the fsd_profile urn in `included`. This is the
# bridge between the public id an agent has and the urn the write takes.
INVITEE = "urn:li:fsd_profile:ACoAAASYNTHETICINVITEE00"

INVITEE_PAYLOAD = {
    "data": {"entityUrn": INVITEE, "publicIdentifier": "synthetic-invitee"},
    "included": [
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "entityUrn": INVITEE,
            "publicIdentifier": "synthetic-invitee",
            "firstName": "Syn",
            "lastName": "Invitee",
        }
    ],
}

# The response the surface needs to call an invitation confirmed. Only the
# *request* was captured, so this is one plausible spelling of the answer - the
# surface's own tests are where the reader's tolerance is pinned.
INVITED_PAYLOAD = {"data": {"*value": "urn:li:fsd_invitation:7000000000000000001"}}

INVITE_PATH_PREFIX = "voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"


def test_invite_resolves_a_public_id_to_a_urn_and_sends_the_captured_body():
    """The whole point of taking a public id: writes are addressed by urn, and an
    agent that has read a feed or a notification is holding a name or a link."""
    client = FakeClient(INVITEE_PAYLOAD, INVITED_PAYLOAD)
    code, out, err = run(["invite", "synthetic-invitee"], client=client)
    assert code == 0, err
    assert client.posts[-1][0].startswith(INVITE_PATH_PREFIX)
    assert client.posts[-1][1] == {"invitee": {"inviteeUnion": {"memberProfile": INVITEE}}}
    assert envelope(out)["data"]["profile_urn"] == INVITEE
    assert envelope(out)["data"]["public_id"] == "synthetic-invitee"


def test_invite_takes_a_member_urn_directly_without_a_lookup():
    """An agent that already read the profile should not pay for a second call -
    and this is the only form a `--dry-run` can preview."""
    client = FakeClient(INVITED_PAYLOAD)
    code, _, err = run(["invite", INVITEE], client=client)
    assert code == 0, err
    assert [p for p in client.paths if not p.startswith(INVITE_PATH_PREFIX)] == []


def test_invite_accepts_a_full_profile_url():
    client = FakeClient(INVITEE_PAYLOAD, INVITED_PAYLOAD)
    code, out, err = run(["invite", "https://www.linkedin.com/in/synthetic-invitee"], client=client)
    assert code == 0, err
    assert envelope(out)["data"]["public_id"] == "synthetic-invitee"


def test_an_invitation_is_recorded_against_the_invite_budget():
    """`DAILY_CAPS["invite"]` was wired to nothing at all: the key existed and no
    command booked against it, so the only bound on an agent sending connection
    requests was the 1 req/s pacer."""
    client = FakeClient(INVITED_PAYLOAD)
    assert run(["invite", INVITEE], client=client)[0] == 0
    assert client.posts, "nothing was sent, so this proves nothing about the ledger"
    assert state.State().write_count("invite", state.DAY) == 1


def test_an_invitation_stops_at_the_daily_cap_before_it_is_sent(monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "invite", 1)
    assert run(["invite", INVITEE], client=FakeClient(INVITED_PAYLOAD))[0] == 0

    second = FakeClient(INVITED_PAYLOAD)
    code, _, err = run(["invite", INVITEE], client=second)
    assert code == 5
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert second.posts == [], "the cap was checked after the invitation had gone out"


def test_an_open_breaker_refuses_an_invitation_before_it_is_sent():
    """An invite is not a cleanup, so nothing exempts it: the breaker is open
    because LinkedIn already objected to this client's traffic."""
    state.State().trip_breaker("999 from linkedin")
    client = FakeClient(INVITED_PAYLOAD)
    code, _, err = run(["invite", INVITEE], client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "blocked"
    assert client.posts == []


def test_a_public_id_that_resolves_to_nothing_costs_no_ledger_slot():
    """The lookup answered, and answered with no urn. Charging a write slot for a
    person this CLI never wrote to spends a cap on nothing."""
    client = FakeClient({"data": {}, "included": []})
    code, _, err = run(["invite", "synthetic-invitee"], client=client)
    assert code == 2
    assert client.posts == []
    assert state.State().write_count("invite", state.DAY) == 0


def test_a_urn_of_the_wrong_kind_is_refused_with_the_kind_this_write_takes():
    """A caller holding `urn:li:activity:…` used to be told it was "not a usable
    public id" - true, and no help at all. Anything shaped like a urn is judged
    as a urn, so the refusal names the one kind this write is addressed by."""
    client = FakeClient()
    code, _, err = run(["invite", "urn:li:activity:7486948402790400001"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "fsd_profile" in message
    assert "profile get" in message
    assert client.paths == []


def test_a_company_url_is_refused_before_any_request():
    client = FakeClient()
    code, _, _ = run(["invite", "https://www.linkedin.com/company/some-co"], client=client)
    assert code == 2
    assert client.paths == []


def test_a_dry_run_invite_by_public_id_refuses_rather_than_resolving_it():
    """Resolving a public id is a real request, so a preview cannot make it - and
    inventing a urn for the preview would have the operator approving a body that
    names somebody other than whoever the invitation would really reach."""
    client = FakeClient(INVITEE_PAYLOAD, INVITED_PAYLOAD)
    code, _, err = run(["invite", "synthetic-invitee", "--dry-run"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "profile get" in message
    assert client.paths == []


def test_a_dry_run_invite_by_urn_previews_and_spends_nothing():
    """The body handed to the client is the assertion: a preview an operator
    approves has to carry the request that would really go out."""
    client = FakeClient()
    code, _, err = run(["invite", INVITEE, "--dry-run"], client=client)
    assert code == 0, err
    path, body, dry_run = client.posts[-1]
    assert dry_run is True
    assert path.startswith(INVITE_PATH_PREFIX)
    assert body == {"invitee": {"inviteeUnion": {"memberProfile": INVITEE}}}
    assert state.State().write_count("invite", state.DAY) == 0


def test_a_note_is_refused_rather_than_silently_dropped():
    """`--note` is what a caller reaches for first, and the captured payload has
    no field for one. Accepting and dropping it sends a bare invitation while the
    caller believes their note went with it."""
    client = FakeClient(INVITED_PAYLOAD)
    code, _, err = run(["invite", INVITEE, "--note=hello there"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "not" in message and "captured" in message
    assert client.posts == []


def test_a_quota_refusal_exits_5_under_its_own_code():
    """LinkedIn checks its own invitation quota on this endpoint, so a refusal is
    a real answer about the account. Exit 6 would tell an agent to retry it."""
    client = FakeClient({"errors": [{"message": "You have reached the weekly invitation quota"}]})
    code, _, err = run(["invite", INVITEE], client=client)
    assert code == 5
    body = envelope(err)
    assert body["error"]["code"] == "invite_quota_exceeded"
    assert body["error"]["retryable"] is False


def test_a_quota_refusal_still_counts_the_request_that_was_sent():
    """The cap is on what this client *sends*, not on what it can confirm - the
    request reached LinkedIn either way."""
    client = FakeClient({"errors": [{"message": "invitation quota exceeded"}]})
    run(["invite", INVITEE], client=client)
    assert state.State().write_count("invite", state.DAY) == 1


def test_an_empty_response_is_not_reported_as_an_invitation_sent():
    client = FakeClient({})
    code, _, err = run(["invite", INVITEE], client=client)
    assert code == 6
    assert envelope(err)["error"]["retryable"] is False


def test_invite_withdraw_refuses_and_sends_the_operator_to_the_browser():
    """The one verb an operator reaches for right after sending the wrong
    invitation - and the one this CLI cannot have.

    It used to say the payload had never been captured, which reads as one more
    capture run away and is what the last session was left pointed at. The
    invitation surface has migrated to LinkedIn's server-driven UI: Withdraw
    posts an SDUI action to /flagship-web/ and gets an RSC stream back, which
    this transport cannot speak or read. So the remedy is the browser, and the
    message carries the evidence rather than the conclusion.
    """
    client = FakeClient()
    code, _, err = run(["invite", "withdraw", "urn:li:invitation:1"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "not implemented" in message
    assert "docs/sdui-migration.md" in message
    assert "never captured" not in message
    assert "interception" not in message, "this is not a capture that anyone can make"
    assert client.posts == []


# The one answer `relationships/invitationViews?q=receivedInvitation` has ever
# been observed to give: the account had zero received invitations when the
# route was verified. See docs/sdui-migration.md.
RECEIVED_PAYLOAD = {"data": {"elements": [], "paging": {"start": 0, "count": 10, "total": 0}}}

RECEIVED_ROUTE = "relationships/invitationViews?count=10&q=receivedInvitation&start=0"


@pytest.mark.parametrize("argv", [["invitations"], ["invitations", "list"]])
def test_invitations_list_reads_the_route_that_was_verified_live(argv):
    """The received side ships. Its finder is one of two spellings out of nine
    that answered at all, and `list` is the default action for the same reason
    `feed` and `notifications` have one."""
    client = FakeClient(RECEIVED_PAYLOAD)
    code, out, err = run(argv, client=client)
    assert code == 0, err
    assert client.paths == [RECEIVED_ROUTE]
    assert envelope(out)["data"] == []


def test_invitations_list_pages_the_way_the_other_reads_do():
    client = FakeClient(RECEIVED_PAYLOAD)
    run(["invitations", "list", "--count=5", "--cursor=20"], client=client)
    assert client.paths == ["relationships/invitationViews?count=5&q=receivedInvitation&start=20"]


def test_invitations_list_reports_its_cursor_in_the_envelope():
    """A `Page`, like every other paged read here, so an agent continues it the
    same way it continues a feed."""
    # A bare Invitation entity - the shape observed live, minus
    # the wrapper and the sender, both of which this test does not need.
    element = {"entityUrn": "urn:li:fs_relInvitation:7000000000000000003"}
    client = FakeClient({"data": {"elements": [element], "paging": {"start": 0, "count": 1}}})
    code, out, err = run(["invitations", "list", "--count=1"], client=client)
    assert code == 0, err
    body = envelope(out)
    assert body["data"][0]["invitation_urn"].startswith("urn:li:fs_relInvitation:")
    assert body["next_cursor"] == "1"
    assert body["has_more"] is True


def test_invitations_that_cannot_be_parsed_are_not_reported_as_an_empty_inbox():
    """The failure this surface is written around, seen from the CLI: exit 6 and
    a message, never exit 0 with an empty list."""
    client = FakeClient({"data": {"elements": [{"unrecognised": 1}]}})
    code, _, err = run(["invitations", "list"], client=client)
    assert code == 6
    assert envelope(err)["error"]["retryable"] is False


def test_invitations_sent_refuses_and_says_no_route_survives():
    """The other half of the split. `list` ships and `sent` cannot: the sent
    screen is server-driven UI, and seven probed spellings answered 404 or 400."""
    client = FakeClient()
    code, _, err = run(["invitations", "sent"], client=client)
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "not implemented" in message
    assert "docs/sdui-migration.md" in message
    assert "never captured" not in message
    assert client.paths == [], "a route nobody has seen was requested anyway"


def test_the_invitation_verbs_are_routed_to_a_real_handler():
    """`invite` shipped when its payload was transcribed; `invitations list`
    ships now that its route is verified. Nothing is left dispatching to a
    handler whose only answer is a raise, so that mechanism is gone rather than
    left empty - an empty table is a test that quietly stops running."""
    assert cli.COMMANDS["invitations"] is cli.cmd_invitations
    assert not hasattr(cli, "cmd_unimplemented")
    assert not hasattr(cli, "UNIMPLEMENTED")


# ----------------------------------------------------------- what a dead session says


# Verb plus at most one following word, and the separator is a literal space so
# the match cannot run across a line break and glue two unrelated examples into
# one phrase. A word starting with `<`, `"`, `[` or `-` is an argument or a flag,
# not a sub-verb, and simply does not match.
COMMAND_PHRASE = re.compile(r"linkedin ([a-z][a-z0-9-]*)(?:[ ]([a-z][a-z0-9-]*))?")


@functools.cache
def verb_actions() -> dict[str, set[str]]:
    """Every sub-verb each command will actually dispatch, read off `cli.py`.

    Hand-maintaining this list is what the sweep below exists to avoid, so it is
    derived from the two spellings `cli.py` uses to gate an action - the
    `_action(...)` allowlist and, in `cmd_auth`, comparisons against `action`.
    A verb with no entry (`me`) or an empty one (the unimplemented writes) takes
    no sub-verb, so its second word is an argument and is not checked.
    """
    tree = ast.parse(inspect.getsource(cli))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")):
            continue
        actions: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_action":
                actions |= {a.value for a in inner.args if isinstance(a, ast.Constant) and a.value}
                actions |= {
                    k.value.value
                    for k in inner.keywords
                    if k.arg == "default" and isinstance(k.value, ast.Constant) and k.value.value
                }
            if isinstance(inner, ast.Compare) and getattr(inner.left, "id", "") == "action":
                actions |= {
                    c.value for c in inner.comparators if isinstance(c, ast.Constant) and c.value
                }
        found[node.name[len("cmd_") :]] = actions - {"ctx"}
    return found


def named_commands(text: str) -> set[str]:
    """The `linkedin …` command phrases a piece of prose tells someone to run."""
    return {" ".join(filter(None, match)) for match in COMMAND_PHRASE.findall(text)}


# The same phrase written the way prose writes it - in backticks, without the
# program name. `[^`]*` swallows the arguments so that `post delete <urn>` and
# `post delete` are one phrase.
QUOTED_PHRASE = re.compile(r"`([a-z][a-z0-9-]*)(?:[ ]([a-z][a-z0-9-]*))?[^`]*`")

# A line naming a command this CLI cannot carry out is a lie unless it is the
# line saying so. Two spellings, because the two cases read differently: a verb
# that was never written is "not implemented", and one the pivot removed - the
# README explains why `auth export` is gone - is "there is no".
SAYS_SO = re.compile(r"not implemented|there is no", re.IGNORECASE)


def quoted_commands(text: str) -> set[str]:
    """Backticked command phrases, restricted to verbs this CLI has.

    Unrestricted, every backticked word in a README is a candidate and
    `python3` reads as a broken verb. The unknown-verb half of the sweep is what
    `named_commands` is for; this half exists for the sub-verb of a verb that
    does exist, which is where the stubs are.
    """
    found = set()
    for verb, action in QUOTED_PHRASE.findall(text):
        if verb in cli.COMMANDS:
            found.add(" ".join(filter(None, (verb, action))))
    return found


def _excused(line: str) -> set[str]:
    """Phrases on this line that it is allowed to name, because it says so."""
    if not SAYS_SO.search(line):
        return set()
    return named_commands(line) | quoted_commands(line)


def _raises_usage(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and getattr(node.exc.func, "id", "") == "UsageError"
    )


def _is_the_sub_verb(node: ast.expr) -> bool:
    """Whether this expression is the sub-verb a handler branches on.

    Two spellings, because `cmd_comment` never binds one: every other handler
    compares the local `action`, and it compares `ctx.args[0]` in place.
    """
    if isinstance(node, ast.Name):
        return node.id == "action"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "args"
    )


def _docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


@functools.cache
def refusals() -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """The handlers, and the sub-verbs of handlers, whose only answer is a raise.

    Derived for the same reason `verb_actions` is derived: a hand-kept list of
    what is broken is maintained by whoever last broke something. Two shapes
    carry it - a handler whose entire body is a raise (`cmd_unimplemented`, and
    so every verb in `UNIMPLEMENTED`), and an `if` on the sub-verb whose body is
    nothing but a raise. `!=` inverts the sense: `cmd_notifications` refuses
    every action that is not `list`.

    Narrow on purpose. `if ctx.flag("idempotency-key"): raise UsageError(...)`
    also has a raise for a body, and reading its string constants would make
    `idempotency-key` a refused sub-verb - so only a comparison against the
    sub-verb itself counts.
    """
    tree = ast.parse(inspect.getsource(cli))
    whole: set[str] = set()
    partial: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")):
            continue
        stem = node.name[len("cmd_") :]
        body = [s for s in node.body if not _docstring(s)]
        if len(body) == 1 and _raises_usage(body[0]):
            whole.add(stem)
            continue
        equal: set[str] = set()
        unequal: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If) or not inner.body:
                continue
            if not all(_raises_usage(s) for s in inner.body):
                continue
            for test in ast.walk(inner.test):
                if not (isinstance(test, ast.Compare) and _is_the_sub_verb(test.left)):
                    continue
                for op, other in zip(test.ops, test.comparators):
                    if not (isinstance(other, ast.Constant) and isinstance(other.value, str)):
                        continue
                    if isinstance(op, ast.Eq):
                        equal.add(other.value)
                    elif isinstance(op, ast.NotEq):
                        unequal.add(other.value)
        if unequal:
            equal |= verb_actions().get(stem, set()) - unequal
        if equal:
            partial[stem] = frozenset(equal)
    return frozenset(whole), partial


def _stem(verb: str) -> str:
    """The `cmd_` function a verb dispatches to, minus the prefix."""
    return cli.COMMANDS[verb].__name__[len("cmd_") :]


def unrunnable(phrases: set[str]) -> set[str]:
    """The subset of those phrases this CLI could not carry out.

    Three ways to fail, and the third is the one that went unnoticed longest:

    * the verb is not in `COMMANDS` at all;
    * the verb exists but the sub-verb does not, which is where every *deleted*
      command lives - checking only the first word, as the first version of this
      did, passes `auth sync` and `auth export` forever;
    * the verb *and* the sub-verb dispatch, and the handler's only possible
      answer is `UsageError`. Dispatching is not working. `post delete`,
      `comment delete`, `notifications mark-read` and the whole invitation
      family are stubs that reach a handler and then refuse every argument, so
      the first two checks let them through - and SKILL.md advertised four of
      them to the agent as writes it could make.
    """
    actions = verb_actions()
    whole, partial = refusals()
    bad = set()
    for phrase in phrases:
        verb, _, action = phrase.partition(" ")
        if verb not in cli.COMMANDS:
            bad.add(phrase)
            continue
        stem = _stem(verb)
        if stem in whole:
            bad.add(phrase)
        elif action and actions.get(verb) and action not in actions[verb]:
            bad.add(phrase)
        elif action and action in partial.get(stem, frozenset()):
            bad.add(phrase)
    return bad


def operator_facing_files() -> list[Path]:
    """Everything shipped that tells a human or an agent what to run."""
    package = Path(linkedin_cli.__file__).parent
    root = package.parent
    return [p for p in sorted(package.rglob("*.py")) if "__pycache__" not in p.parts] + [
        root / "SKILL.md",
        root / "README.md",
    ]


def test_a_signed_out_profile_exits_3_and_names_a_command_that_exists():
    """The whole point of the sweep: `auth sync` and `--transport=direct` are
    deleted, and an error telling an agent to run a command `COMMANDS` does not
    dispatch is worse than an error with no remedy - the agent retries it."""
    code, _, err = run(["me"], client=signed_out_client())
    assert code == 3
    body = envelope(err)
    assert body["error"]["code"] == "session_expired"

    message = body["error"]["message"]
    assert "auth seed" in message
    assert "auth sync" not in message
    assert "--transport" not in message
    assert unrunnable(named_commands(message)) == set()


def test_a_challenge_page_exits_9_and_arms_the_breaker_in_one_invocation():
    """A checkpoint served as a 200 used to exit 6 - the code an agent retries -
    and the breaker never armed at all."""
    client = browser.BrowserClient(
        rate=1.0,
        state=NoPace(),
        request_fn=supervisor_stub(
            body="<html>verify</html>",
            url="https://www.linkedin.com/checkpoint/challenge/AgH",
        ),
    )
    code, _, err = run(["me"], client=client)
    assert code == 9
    assert envelope(err)["error"]["code"] == "blocked"
    assert state.State().breaker_state() is not None
    assert unrunnable(named_commands(envelope(err)["error"]["message"])) == set()


def test_the_sub_verbs_the_sweep_checks_against_are_the_ones_cli_dispatches():
    """The sweep is only as good as this table, and the table is derived from
    `cli.py` rather than written down - so pin what the derivation found.

    `sync` and `export` are the two spellings the pivot deleted, and both start
    with a verb that still exists. A sweep that stopped resolving `auth`'s
    actions would silently start passing every string naming them again.
    """
    actions = verb_actions()
    assert actions["auth"] == {"seed", "status"}
    assert {"sync", "export", "login"} & actions["auth"] == set()
    assert actions["messages"] == {
        "list",
        "read",
        "counts",
        "send",
        "reply",
        # Still dispatched, and it now answers "renamed" rather than draining the
        # mailbox - an agent holding the old spelling has to be told, not obeyed.
        "mark-read",
        "mark-all-read",
    }
    assert actions["me"] == set(), "a verb taking no sub-verb must not gate its argument"


@pytest.mark.parametrize(
    "phrase",
    ["auth sync", "auth export", "auth login", "messages purge", "frobnicate", "post publish"],
)
def test_the_sweep_rejects_a_command_this_cli_would_not_answer(phrase):
    """The sweep's own test. Checking only the first word - which is what this
    did before - passed `auth sync` and `auth export`, the exact two strings it
    was written to catch, because both start with a verb that does exist."""
    assert unrunnable({phrase}) == {phrase}


@pytest.mark.parametrize(
    "phrase",
    [
        "auth seed",
        "auth status",
        "me",
        "doctor",
        "messages mark-all-read",
        "post create",
        # Shipped once its payload was transcribed from a live capture run. It
        # sat in the list below for as long as `post create` had no inverse.
        "post delete",
        # Same capture run, same reason. Its *undo* is still a stub, which is why
        # the sweep has to resolve a sub-verb rather than trusting the verb.
        "invite",
        # Shipped once its route was verified live. `invitations sent` is the stub
        # beside it, so this verb is the second one whose two halves the sweep has
        # to tell apart.
        "invitations",
        "invitations list",
        # And the third. `comment` writes, `comment delete` undoes it, and both
        # halves work - so a sweep that resolved the verb alone would now be
        # right here by luck rather than by looking.
        "comment",
        "comment delete",
    ],
)
def test_the_sweep_accepts_what_the_cli_really_dispatches(phrase):
    assert unrunnable({phrase}) == set()


@pytest.mark.parametrize(
    "phrase",
    [
        "notifications mark-read",
        "messages mark-read",
        # `invite` itself shipped once its captured payload was transcribed;
        # its undo did not, and cannot - the surface it lives on is no longer
        # Voyager. `invitations sent` is refused for the same reason.
        "invite withdraw",
        "invitations sent",
    ],
)
def test_the_sweep_rejects_a_verb_that_dispatches_and_then_only_refuses(phrase):
    """Dispatching is not working, and this is the hole the sweep had.

    Every one of these reaches a handler, so a check that asks `COMMANDS` alone
    passes it. Each one's handler then raises `UsageError` for every argument it
    could ever be given. SKILL.md advertised four of them to the agent as though
    they were writes it could make.
    """
    assert unrunnable({phrase}) == {phrase}


# The shortest argument list that gets each command as far as its first request.
# Deliberately data rather than assertion: the *claim* under test is that the
# commands missing from the reaching half have no such argument list at all, and
# `test_the_sweep_agrees_with_what_reaches_linkedin` derives that by running
# every one of them rather than by trusting this table.
INVOCATIONS = {
    "auth seed": ["auth", "seed"],
    "auth status": ["auth", "status"],
    "me": ["me"],
    "profile get": ["profile", "get", "grace-hopper-1906"],
    "feed list": ["feed", "list"],
    "post get": ["post", "get", "urn:li:activity:7000000000000000000"],
    "post create": ["post", "create", "--text=hello"],
    # `--yes` for the same reason `messages mark-all-read` carries it here: the
    # opt-in is part of the shortest invocation that reaches the wire.
    "post delete": ["post", "delete", "urn:li:activity:7000000000000000000", "--yes"],
    "react": ["react", "urn:li:activity:7000000000000000000"],
    "unreact": ["unreact", "urn:li:activity:7000000000000000000"],
    "comment": ["comment", "urn:li:activity:7000000000000000000", "--text=hi"],
    # The real key shape, not a placeholder: the urn is validated before
    # anything is sent, so a made-up one never gets this as far as a request and
    # the closure test below would read a shipped write as a stub.
    "comment delete": ["comment", "delete", COMMENT_URN],
    "messages list": ["messages", "list"],
    "messages read": ["messages", "read", "urn:li:msg_conversation:(x,y)"],
    "messages counts": ["messages", "counts"],
    "messages send": ["messages", "send", "--to=some-person", "--text=hi"],
    # A real conversation urn, not a `(x,y)` placeholder: `confirm_reply_target`
    # now checks the caller's own argument names this mailbox, and a placeholder
    # names `x`. That it used to reach the wire is the defect, not the fixture -
    # the argument said one mailbox, the answer said another, and the write went
    # out addressed to the argument.
    "messages reply": ["messages", "reply", CONVERSATION, "--text=hi"],
    "messages mark-read": ["messages", "mark-read", "urn:li:msg_conversation:(x,y)"],
    "messages mark-all-read": ["messages", "mark-all-read", "--yes"],
    "notifications list": ["notifications", "list"],
    "notifications mark-read": ["notifications", "mark-read", "urn:li:notification:1"],
    "invite": ["invite", "grace-hopper-1906"],
    "invite withdraw": ["invite", "withdraw", "urn:li:invitation:1"],
    "invitations": ["invitations"],
    "invitations list": ["invitations", "list"],
    "invitations sent": ["invitations", "sent"],
    "doctor": ["doctor"],
}


def _reaches_linkedin(argv: list[str]) -> bool:
    """Run one command against a client that records and then fails.

    "Refused outright" is exit 2 with the client never touched: no argument got
    this command as far as a request, so there is nothing LinkedIn could have
    been asked. `auth seed` is the reason the client alone is not the test - it
    talks to the supervisor rather than to Voyager, reaches neither here (the
    conftest guard blocks the socket) and is still a real command, which the
    exit code shows: 6, not 2.
    """
    touched: list[str] = []

    class Recorder:
        def get(self, path, dry_run=False):
            touched.append(path)
            return {}

        def post(self, path, body, dry_run=False):
            touched.append(path)
            return {}

        def _request(self, method, path, body=None, dry_run=False):
            # `comment delete` is a DELETE, and a recorder that spoke only GET
            # and POST answered it with an `AttributeError` - which is not exit
            # 2, so this read "reaches LinkedIn" for the wrong reason and would
            # have gone on doing so if the route had been removed.
            touched.append(path)
            return {}

    code, _, _ = run(list(argv), client=Recorder())
    return bool(touched) or code != 2


def test_the_invocation_table_covers_every_sub_verb_cli_dispatches():
    """A new sub-verb has to be exercised, not just added to a doc. Without this
    the closure test below silently stops covering whatever was added last."""
    dispatchable = {
        f"{verb} {action}".strip()
        for verb, handler in cli.COMMANDS.items()
        for action in (verb_actions().get(handler.__name__[len("cmd_") :]) or {""})
    }
    assert dispatchable <= set(INVOCATIONS)


def test_the_sweep_agrees_with_what_reaches_linkedin():
    """The sweep's answer, checked against the CLI actually running.

    This is the guard against the derivation quietly going blind: it is static
    analysis of `cli.py`, and static analysis that stops matching reports
    everything as fine. So every command is run, and the two halves have to be
    the same set - a command the sweep calls unrunnable must refuse every
    argument it is given, and a command that gets as far as a request must be
    one the sweep lets the docs name.
    """
    refused = {phrase for phrase, argv in INVOCATIONS.items() if not _reaches_linkedin(argv)}
    assert refused == {phrase for phrase in INVOCATIONS if unrunnable({phrase})}


def test_the_refusals_the_sweep_derives_are_the_ones_cli_hard_codes():
    """Pin the derivation, for the reason the sub-verb table above is pinned:
    a walker that matched nothing would report a clean tree forever."""
    whole, partial = refusals()
    # Empty since `invitations list` shipped: no verb now dispatches to a handler
    # whose entire body is a raise, and `cmd_unimplemented` went with the last of
    # them rather than being left behind as a mechanism with nothing in it.
    assert whole == set()
    assert partial == {
        "messages": frozenset({"mark-read"}),
        "notifications": frozenset({"mark-read"}),
        # `invite` sends; `invite withdraw` has no captured payload and no
        # capturable one. One verb, one working action and one stub - the shape
        # the sweep exists to see.
        "invite": frozenset({"withdraw"}),
        # And the same shape again, for the verb that had been wholly refused:
        # the received side is a verified route, the sent side is not a route at
        # all any more.
        "invitations": frozenset({"sent"}),
    }
    # `post delete` was here until its payload was transcribed into a writer. It
    # is now a real write, so the sweep has to stop calling it a stub - and the
    # docs have to stop calling it unimplemented, which is what
    # `test_no_shipped_message_names_a_command_that_does_not_exist` and its
    # backticked twin check from the other direction.
    assert "post" not in partial
    # `comment delete` went the same way when its route was verified live.
    # `cmd_comment` still branches on the sub-verb, so the derivation has to be
    # seeing that the branch *writes* rather than raises.
    assert "comment" not in partial


def _ledgered(argv: list[str]) -> tuple[bool, bool]:
    """Run one command and report (it wrote, it left a ledger row).

    Driven off `INVOCATIONS` rather than a hand-kept list of writes, because the
    bug this guards against is a *new* write reaching LinkedIn without anyone
    remembering to add it to the list of writes.

    "Wrote" is any request that is not a GET, not "POSTed". `comment delete` is
    a DELETE - the CLI's first - and a recorder that watched only POSTs reported
    it as issuing nothing at all, which turns this test and the two below into a
    skip for exactly the command they were needed for.
    """
    posted: list[str] = []

    class Recorder:
        def get(self, path, dry_run=False):
            # `messages reply` reads the thread back before it sends into it
            # (`cmd_messages`), and an empty page there is a refusal - which
            # would turn the one write this test exists to watch into a skip,
            # silently, and take the ledger claim it is checking with it.
            return MESSAGES_PAYLOAD if "conversationUrn" in path else {}

        def post(self, path, body, dry_run=False):
            posted.append(path)
            return {}

        def _request(self, method, path, body=None, dry_run=False):
            posted.append(path)
            return {}

    state.State().remember("member_urn", MEMBER)
    before = _all_write_rows()
    run(list(argv), client=Recorder())
    return bool(posted), _all_write_rows() > before


def _all_write_rows() -> int:
    path = state.resolve_path()
    data = json.loads(path.read_text()) if path.exists() else {}
    return sum(len(stamps) for stamps in (data.get("writes") or {}).values())


# `INVOCATIONS` reaches `messages send` through a thread lookup that a recording
# client cannot answer, so it never gets as far as a POST there. The write is
# real, so it is given the arguments that do reach the wire.
WRITE_INVOCATIONS = {
    **INVOCATIONS,
    "messages send": ["messages", "send"] + MESSAGE_WRITES["send"],
    # `INVOCATIONS` invites by public id, which a recording client answers with
    # no urn in it, so it never reaches the POST there. The member urn is the
    # form that goes straight to the wire.
    "invite": ["invite", INVITEE],
}


@pytest.mark.parametrize("phrase", sorted(WRITE_INVOCATIONS))
def test_every_command_that_writes_is_counted_by_the_ledger(phrase):
    """The caps are the only bound on an agent that can be talked into a loop,
    and they bind on nothing a command does behind the ledger's back. `messages
    send` and `reply` bypassed `_write` entirely once, so `DAILY_CAPS["message"]`
    was enforced nowhere while the docs said it was.

    A non-GET is the criterion because it is what LinkedIn sees. Reads are
    exempt by being reads, not by being listed here.
    """
    posted, counted = _ledgered(WRITE_INVOCATIONS[phrase])
    if not posted:
        pytest.skip(f"`{phrase}` issued no write, so there is nothing to charge")
    assert counted, f"`{phrase}` wrote to LinkedIn without claiming a ledger slot"


def test_the_write_ledger_test_above_is_actually_exercising_writes():
    """A skip-if-no-POST test degrades into vacuity the moment the invocations
    stop reaching the wire. This names the writes that must stay covered."""
    wrote = {p for p in WRITE_INVOCATIONS if _ledgered(WRITE_INVOCATIONS[p])[0]}
    assert wrote == set(_WRITING_PHRASES)


# The commands `test_the_write_ledger_test_above_is_actually_exercising_writes`
# proves reach the wire. Kept as a literal so a write that stops writing is a
# failure in that test rather than a silent gap in the two below.
_WRITING_PHRASES = frozenset(
    {
        "react",
        "unreact",
        "comment",
        # The CLI's only non-POST write. It is named here for the reason the
        # whole set is: a write that stops writing has to fail the test above
        # rather than quietly skip, and this one skipped until `_ledgered`
        # learned to watch a DELETE.
        "comment delete",
        "post create",
        "post delete",
        "invite",
        "messages send",
        "messages reply",
        "messages mark-all-read",
    }
)


@pytest.mark.parametrize("phrase", sorted(_WRITING_PHRASES))
def test_every_command_that_writes_is_known_to_the_breaker_guard(phrase):
    """`cli.WRITE_ACTIONS` decides who an unverifiable breaker refuses, and it is
    hand-kept - so it is checked against the set of commands that provably reach
    the wire rather than against itself. A write missing from that table is a
    write that goes out with the breaker unread, which is the whole defect."""
    verb, *args = WRITE_INVOCATIONS[phrase]
    assert cli.is_write(verb, args), (
        f"`{phrase}` POSTs to LinkedIn and `cli.WRITE_ACTIONS` does not call it a write"
    )


@pytest.mark.parametrize(
    "phrase", ["me", "profile get", "feed list", "post get", "messages list", "doctor"]
)
def test_a_read_is_not_mistaken_for_a_write_by_the_breaker_guard(phrase):
    """The other direction, or the table degenerates into refusing everything and
    the half of the policy that keeps the CLI usable is gone."""
    verb, *args = INVOCATIONS[phrase]
    assert not cli.is_write(verb, args)


@pytest.mark.parametrize("phrase", sorted(WRITE_INVOCATIONS))
def test_no_command_issues_a_real_request_under_dry_run(phrase):
    """`--dry-run` is what an operator approves a write from, so a preview that
    quietly performs a lookup of its own is worse than no preview: it teaches the
    caller that `--dry-run` is free while it is spending pace, quota and - for
    `auth seed` - reading a live cookie jar.

    `dry_run=True` is the seam. A client call carrying it is answered from the
    request rather than issued; a call without it went to LinkedIn.
    """
    issued: list[str] = []

    class Recorder:
        def get(self, path, dry_run=False):
            if not dry_run:
                issued.append(f"GET {path}")
            return {}

        def post(self, path, body, dry_run=False):
            if not dry_run:
                issued.append(f"POST {path}")
            return {}

        def _request(self, method, path, body=None, dry_run=False):
            # A preview that silently performed the CLI's one DELETE would be
            # the worst version of this failure: `--dry-run` is what an operator
            # approves an irreversible removal from.
            if not dry_run:
                issued.append(f"{method} {path}")
            return {}

    state.State().remember("member_urn", MEMBER)
    run(list(WRITE_INVOCATIONS[phrase]) + ["--dry-run"], client=Recorder())
    assert issued == []


@pytest.mark.parametrize("phrase", sorted(_WRITING_PHRASES))
def test_a_spent_cap_never_refuses_a_preview(phrase, monkeypatch):
    """Every cap at zero, which is the state an agent that has been looping all
    day is in. A preview issues nothing, so there is nothing for a cap to
    protect - and refusing it takes away the one command that still costs
    nothing at exactly the moment it is most worth running."""
    state.State().remember("member_urn", MEMBER)
    for kind in state.DAILY_CAPS:
        monkeypatch.setitem(state.DAILY_CAPS, kind, 0)
    monkeypatch.setattr(state, "DEFAULT_DAILY_CAP", 0)
    code, _, err = run(list(WRITE_INVOCATIONS[phrase]) + ["--dry-run"], client=FakeClient())
    assert code == 0, err


def test_no_stub_sends_its_reader_to_the_capture_method_that_was_withdrawn():
    """The superseded capture phase said to capture a write by *performing* it -
    "publish and delete a throwaway post". That method sent a real connection
    invitation to the wrong person and was replaced by capture-by-interception,
    which learns the payload without the action ever reaching LinkedIn. A stub
    that cites the withdrawn phase is an instruction to repeat the incident, and
    it is on the one path a reader follows when they set out to fill the gap.

    `_POST_DELETE` was checked here too until `post delete` shipped. Its removal
    is asserted rather than assumed: a stub message that outlives its stub goes
    on telling a reader to capture a payload that is already transcribed.
    """
    assert not hasattr(cli, "_POST_DELETE"), "a stub message outlived the stub"
    # `_NO_INVITATION_READ` went the same way when `invitations list` shipped -
    # and its removal matters more than most, because it was the message telling
    # its reader to go and capture a route that turned out not to exist.
    assert not hasattr(cli, "_NO_INVITATION_READ"), "a stub message outlived the stub"
    # `_NOT_WIRED` said the messaging surface had no writer for a captured
    # payload. It has had one for some time, nothing referenced the string, and
    # an unused refusal is a refusal nobody notices has gone stale.
    assert not hasattr(cli, "_NOT_WIRED"), "a stub message outlived the stub"
    for name in ("_NOT_CAPTURED",):
        message = getattr(cli, name)
        assert "design.md" not in message, f"{name} points at the superseded capture-by-doing phase"
        assert "interception" in message, f"{name} does not name the method that replaced it"


def test_the_invitation_stubs_do_not_send_their_reader_on_a_capture_run():
    """The stronger version of the rule above, for the two verbs where even
    capture-by-interception is the wrong instruction.

    `invite withdraw` and `invitations sent` are not uncaptured payloads. The
    screen they live on left Voyager (docs/sdui-migration.md, observed live), so
    a capture run cannot succeed - and the last two capture runs against this
    surface cost a stranger's invitation each. A stub that still
    said "capture it" would be an instruction to repeat that, aimed at exactly
    the reader who has set out to close the gap.
    """
    for name in ("NO_WITHDRAW", "NO_SENT_LIST"):
        message = getattr(invitations, name)
        assert "docs/sdui-migration.md" in message, f"{name} states a conclusion with no evidence"
        # Saying a capture run cannot close this is the point; naming the tool or
        # the method is the instruction that must not survive.
        assert "tools/capture_payloads.py" not in message, f"{name} still points at the tool"
        assert "interception" not in message, f"{name} still names the method"
        assert "not implemented" in message, f"{name} must be excused by the doc sweep"


def test_no_shipped_message_names_a_command_that_does_not_exist():
    """A source-level sweep in the spirit of `tools/leakcheck.py`. The runtime
    tests above only cover the error paths a test happens to walk, and the
    strings that matter most are on the paths nobody reproduces - `state.py`'s
    quota messages, `supervisor.py`'s socket errors, and SKILL.md, which is the
    only one of these an agent reads before it has hit anything at all.

    Line by line, and a line naming an unrunnable command is forgiven only if
    that same line says so. Deleting every trace of `post delete` would be its
    own lie - an agent that just published needs to be told the undo is a
    browser action, not left to guess - so the docs are allowed to name it while
    calling it unimplemented, and nowhere else.
    """
    referenced: set[str] = set()
    offenders: dict[str, set[str]] = {}
    for path in operator_facing_files():
        for line in path.read_text().splitlines():
            phrases = named_commands(line)
            referenced |= phrases
            if unknown := unrunnable(phrases) - _excused(line):
                offenders.setdefault(path.name, set()).update(unknown)
    # The check is only worth anything if it is still finding the strings. Named
    # phrases the docs are not about to lose, so that rewriting SKILL.md does not
    # have to come back here.
    assert {"auth seed", "doctor"} <= referenced
    assert offenders == {}


def test_the_agent_facing_docs_name_no_unrunnable_command_without_saying_so():
    """The same rule for commands written without the `linkedin ` prefix.

    SKILL.md's own writes section listed the inverses as "`post delete`,
    `unreact`, `comment delete`, `invite withdraw`" - three of the four are
    stubs, none of them carried the prefix, and the sweep above never looked at
    that sentence. Scoped to the two markdown files: the CLI's error strings
    name stubs on purpose and are covered by the prefixed form.
    """
    offenders: dict[str, set[str]] = {}
    for path in (
        Path(linkedin_cli.__file__).parent.parent / name for name in ("SKILL.md", "README.md")
    ):
        for line in path.read_text().splitlines():
            if unknown := unrunnable(quoted_commands(line)) - _excused(line):
                offenders.setdefault(path.name, set()).update(unknown)
    assert offenders == {}


def test_the_backtick_reader_finds_a_command_written_without_the_prefix():
    """Its own test, because a reader that matched nothing would make the check
    above pass on any document at all."""
    assert quoted_commands("each has an inverse: `post delete`, `unreact`.") == {
        "post delete",
        "unreact",
    }
    # `invite withdraw` rather than `post delete`: the example has to be a phrase
    # that is *still* a stub, and `post delete` stopped being one when its
    # payload was transcribed - as did `comment delete`, which stood here until
    # its route was verified live. Reading a shipped command as unrunnable would
    # make the two sweeps above fail on honest documentation.
    assert unrunnable(quoted_commands("use `invite withdraw` to undo it")) == {"invite withdraw"}
    assert unrunnable(quoted_commands("use `post delete` to undo it")) == set()
    assert unrunnable(quoted_commands("use `comment delete` to undo it")) == set()
    # Not a command, and a reader that guessed otherwise would report the
    # install instructions as a broken verb.
    assert quoted_commands("run `python3 -m pytest`") == set()


def test_no_shipped_file_names_the_deleted_transport_selector():
    """`--transport=direct` chose the urllib client, which no longer exists.
    It never reaches the sweep above - a flag is not a verb - and an agent that
    passes it gets its request silently routed nowhere near where it thinks."""
    offenders = {
        path.name: dead
        for path in operator_facing_files()
        for dead in ["--transport", "LINKEDIN_TRANSPORT"]
        if dead in path.read_text()
    }
    assert offenders == {}


# ---------------------------------------------------------------------- dry run


def offline_client(asked, status=None):
    """A real `BrowserClient` for which issuing any request is a test failure."""

    def request_fn(payload, **kwargs):
        asked.append(payload)
        if payload.get("op") == "status":
            return status if status is not None else {"pid": 4242, "profile": "/managed"}
        raise AssertionError("--dry-run made a real request")

    return browser.BrowserClient(rate=1.0, state=NoPace(), request_fn=request_fn)


# A JSESSIONID value in the shape `transport._SECRET_VALUES` matches, and the one
# `tools/leakcheck.py` already knows is synthetic. LinkedIn echoes this exact
# value back as `?ct=` on a checkpoint URL, so the leak these pin is the real one
# rather than a stand-in for it.
CSRF_TOKEN = "ajax:1111222233334444555"
CHECKPOINT_URL = f"https://www.linkedin.com/checkpoint/challenge/AgHRk?ct={CSRF_TOKEN}"


class ParkedBrowser:
    """Only the three things the daemon's `status` reads off a resident browser."""

    profile = "/managed/profile"
    relaunched = 0

    def __init__(self, href):
        self._href = href

    def page_url(self):
        return self._href


def daemon_status(href):
    """The status dict a real supervisor answers with, built by the daemon itself.

    Not typed out here, deliberately. `--dry-run` returns that dict *verbatim* as
    `runs_in` and `doctor` projects it, so a hand-written stand-in would pin
    whatever this file invented and would have gone on passing while the daemon
    handed both of them a live credential.
    """
    answer, _ = cli.supervisor._dispatch(
        {"op": "status"}, ParkedBrowser(href), Path("/state/linkedin.sock"), 0.0
    )
    return answer


def test_dry_run_previews_the_request_without_asking_for_a_fetch():
    """End to end, because the preview an operator approves a write from is
    built in `browser.py` and rendered through `cli.py`, and the point of it is
    that nothing was sent."""
    asked = []
    code, out, err = run(["messages", "mark-all-read", "--dry-run"], client=offline_client(asked))
    assert code == 0, err
    preview = envelope(out)["data"]
    assert preview["method"] == "POST"
    assert [p["op"] for p in asked] == ["status"]
    assert preview["headers"]["csrf-token"] == transport.REDACTED
    assert "ajax:" not in out


def test_a_dry_run_reply_never_prints_the_query_string_of_the_live_page():
    """A preview reports where the call *would* run, which is the supervisor's
    `status` dict handed back verbatim as `runs_in` - and a session parked on a
    checkpoint carries the csrf token back in `?ct=`, which *is* the JSESSIONID
    cookie value (`transport.py`).

    The same leak `doctor` had, on the path that is worse: `messages reply` is an
    allowlisted verb under the credential broker, and `--dry-run` skips the
    membership read, the ledger claim, the cap and the breaker and makes no
    LinkedIn round trip at all - so it is a credential oracle that re-answers as
    fast as the caller is paced. `doctor` was projected and this was not, which is
    why the reduction now lives in the daemon where both consumers inherit it.
    """
    state.State().remember("member_urn", MEMBER)
    asked = []
    client = offline_client(asked, status=daemon_status(CHECKPOINT_URL))
    code, out, err = run(
        ["messages", "reply", CONVERSATION, "--text=hi", "--dry-run"], client=client
    )
    assert code == 0, err
    assert CSRF_TOKEN not in out
    assert "ct=" not in out
    assert [p["op"] for p in asked] == ["status"]
    # The path survives for the reason it survives in `doctor`: it is the half
    # that says the browser is sitting on a checkpoint.
    assert envelope(out)["data"]["runs_in"]["page_url"] == "/checkpoint/challenge/AgHRk"


def test_a_dry_run_read_previews_its_request_instead_of_issuing_it():
    """`--dry-run` means no traffic, and a read is traffic. It used to be honoured
    only by the writes, so `feed list --dry-run` fetched the feed for real."""
    asked = []
    code, out, err = run(["feed", "list", "--dry-run"], client=offline_client(asked))
    assert code == 0, err
    preview = envelope(out)["data"]
    assert preview["method"] == "GET"
    assert "/voyager/api/" in preview["url"]
    assert [p["op"] for p in asked] == ["status"]


def test_a_dry_run_send_does_not_look_the_conversation_up_over_the_network():
    """Finding the thread to send into is a real request, so a preview cannot do
    it - and inventing a conversation urn for the preview would have an operator
    approving a body that is not the one that would be sent."""
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(CONVERSATIONS_PAYLOAD)
    code, _, err = run(
        ["messages", "send", "--to=synthetic-operator", "--text=hi", "--dry-run"], client=client
    )
    assert code == 2
    assert "--conversation" in envelope(err)["error"]["message"]
    assert client.paths == []


def test_a_dry_run_send_with_a_conversation_previews_and_spends_nothing():
    state.State().remember("member_urn", MEMBER)
    client = FakeClient()
    code, _, err = run(
        ["messages", "send", f"--conversation={CONVERSATION}", "--text=hi", "--dry-run"],
        client=client,
    )
    assert code == 0, err
    assert client.posts[-1][2] is True
    # `FakeClient` records a post's path too, so the send itself is the only
    # thing here: no lookup went out ahead of it.
    assert client.paths == ["voyagerMessagingDashMessengerMessages?action=createMessage"]
    assert state.State().write_count("message", state.DAY) == 0


def test_a_dry_run_does_not_fetch_the_member_urn_it_has_not_cached():
    """The mailbox is addressed by it, and fetching it is a live call. Refusing
    with the command that caches it beats making the request a preview promised
    not to make."""
    asked = []
    code, _, err = run(
        ["messages", "reply", CONVERSATION, "--text=hi", "--dry-run"], client=offline_client(asked)
    )
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert [p["op"] for p in asked] == []


def test_a_dry_run_auth_seed_copies_nothing_and_names_what_it_would_copy(monkeypatch):
    """`auth seed` is the heaviest side effect this CLI has: it reads a live jar
    out of the operator's Chrome and then proves it with a real page load. Under
    `--dry-run` it used to do both."""
    from linkedin_cli import bootstrap

    def boom(*args, **kwargs):
        raise AssertionError("--dry-run seeded the managed browser for real")

    monkeypatch.setattr(bootstrap, "seed", boom)
    code, out, err = run(["auth", "seed", "--from-profile=/tmp/chrome", "--dry-run"])
    assert code == 0, err
    data = envelope(out)["data"]
    assert data["seeded"] is False
    assert data["would_copy_from"] == "/tmp/chrome"


def test_a_dry_run_doctor_probes_no_surface():
    """Five live Voyager calls, and `--dry-run` said not to make any."""
    asked = []
    code, out, err = run(["doctor", "--dry-run"], client=offline_client(asked))
    assert code == 0, err
    assert [p["op"] for p in asked] == ["status"]


def test_a_dry_run_message_is_still_previewable_once_the_daily_cap_is_gone(monkeypatch):
    """The one command that costs nothing is most useful exactly when the budget
    is spent."""
    monkeypatch.setitem(state.DAILY_CAPS, "message", 1)
    state.State().remember("member_urn", MEMBER)
    args = ["messages", "reply", CONVERSATION, "--text=hi"]
    assert run(args, client=message_write_client("reply"))[0] == 0
    assert run(args, client=FakeClient())[0] == 5

    client = FakeClient()
    code, _, err = run(args + ["--dry-run"], client=client)
    assert code == 0, err
    assert client.posts[-1][2] is True
    assert state.State().write_count("message", state.DAY) == 1


# ---------------------------------------------------------------------- doctor


def test_doctor_reports_every_surface():
    state.State().remember("member_urn", MEMBER)
    client = FakeClient(
        ME_PAYLOAD, PROFILE_PAYLOAD, FEED_PAYLOAD, CONVERSATIONS_PAYLOAD, NOTIFICATIONS_PAYLOAD
    )
    code, out, _ = run(["doctor"], client=client)
    assert code == 0
    data = envelope(out)["data"]
    assert {s["surface"] for s in data["surfaces"]} == {
        "me",
        "profile",
        "feed",
        "messages",
        "notifications",
    }
    assert all(s["ok"] for s in data["surfaces"])


def test_doctor_reports_the_browser_instead_of_a_session_source(monkeypatch):
    """There is no session file and no transport choice left to report; what an
    operator needs to know is which browser process is holding the credential."""
    asked = []

    def fake_request(payload, **kwargs):
        asked.append((payload, kwargs))
        return {"pid": 4242, "profile": "/managed/profile", "page_url": "https://x/feed/"}

    monkeypatch.setattr(cli.supervisor, "request", fake_request)
    _, out, _ = run(["doctor"], client=FakeClient())
    data = envelope(out)["data"]
    assert data["browser"]["pid"] == 4242
    assert "session" not in data
    assert "transport" not in data
    # A diagnostic that started a browser would change what it was diagnosing.
    assert asked == [({"op": "status"}, {"autostart": False})]


def _doctor_with_page_url(monkeypatch, href: str):
    """`doctor` against a supervisor whose browser is parked on `href`.

    The answer is assembled by the daemon (`daemon_status`) rather than typed
    out, because the reduction being checked happens *there* now - one source,
    two consumers, and the other one is `--dry-run`'s `runs_in`.
    """
    monkeypatch.setattr(cli.supervisor, "request", lambda payload, **kw: daemon_status(href))
    return run(["doctor"], client=FakeClient())


def test_doctor_never_prints_the_query_string_of_the_page_the_browser_is_on(monkeypatch):
    """`page_url` is `location.href` read out of the live page, and a session
    parked on a checkpoint carries the csrf token back in the query string as
    `?ct=` - which *is* the JSESSIONID cookie value (`transport.py`). `doctor`
    returned the supervisor's status dict verbatim, so its success path could
    print a live session credential onto stdout, where under an agent gateway it
    is permanent model context.

    Nothing downstream catches it: `render.ok` never scrubs and cannot, because
    `profile get` legitimately returns an `ACoAA…` urn the patterns would eat
    (render.py's own docstring says so), and the tenant that runs under the
    credential broker ships no scrub literals by design.
    """
    code, out, err = _doctor_with_page_url(monkeypatch, CHECKPOINT_URL)
    assert code == 0, err
    assert CSRF_TOKEN not in out
    assert CSRF_TOKEN not in err
    assert "ct=" not in out


def test_doctor_still_reports_which_page_the_browser_is_parked_on(monkeypatch):
    """The path survives, and it is the half that carries the diagnosis: "which
    page is it on" is the question `status` exists to answer, and a session
    parked on a checkpoint is exactly the answer an operator needs
    (`supervisor.Browser.page_url`). Redacting the whole field would take the
    diagnosis away to remove the credential."""
    _, out, _ = _doctor_with_page_url(monkeypatch, CHECKPOINT_URL)
    assert envelope(out)["data"]["browser"]["page_url"] == "/checkpoint/challenge/AgHRk"


def test_doctor_leaves_an_ordinary_page_url_readable(monkeypatch):
    _, out, _ = _doctor_with_page_url(monkeypatch, "https://www.linkedin.com/feed/")
    assert envelope(out)["data"]["browser"]["page_url"] == "/feed/"


def test_doctor_answers_when_no_supervisor_is_running(monkeypatch):
    monkeypatch.setattr(
        cli.supervisor,
        "request",
        lambda payload, **kw: {"error": "no supervisor is listening", "kind": "closed"},
    )
    code, out, _ = run(["doctor"], client=FakeClient())
    assert code == 0
    assert envelope(out)["data"]["browser"]["kind"] == "closed"


def test_doctor_survives_a_supervisor_that_raises(monkeypatch):
    monkeypatch.setattr(
        cli.supervisor,
        "request",
        lambda payload, **kw: (_ for _ in ()).throw(OSError("socket vanished")),
    )
    code, out, _ = run(["doctor"], client=FakeClient())
    assert code == 0
    assert envelope(out)["data"]["browser"] is None


def test_doctor_does_not_raise_when_a_surface_is_broken():
    """Reporting breakage is doctor's entire job; failing at it is the one bug."""
    code, out, _ = run(["doctor"], client=FakeClient(error=transport.StaleQueryId("rotated")))
    assert code == 0
    data = envelope(out)["data"]
    assert all(s["ok"] is False for s in data["surfaces"])
    assert all(s["error"] == "stale_query_id" for s in data["surfaces"])


def test_doctor_reports_a_signed_out_profile_on_every_surface():
    code, out, _ = run(["doctor"], client=signed_out_client())
    assert code == 0
    data = envelope(out)["data"]
    assert all(s["ok"] is False for s in data["surfaces"])
    assert all(s["error"] == "session_expired" for s in data["surfaces"])


def test_doctor_lists_the_query_ids_and_how_to_override_them():
    """A rotated queryId is exit 7; the fix is an env var, so doctor prints it."""
    _, out, _ = run(["doctor"], client=FakeClient())
    ids = envelope(out)["data"]["query_ids"]
    assert ids["conversations"]["override_env"] == "LINKEDIN_QUERY_ID_CONVERSATIONS"
    assert ids["conversations"]["value"].startswith("messengerConversations.")


def test_doctor_lists_every_surfaces_query_ids_not_just_messagings():
    """Reactions ship two more rotating hashes, and exit 7 tells the operator to
    run doctor. A doctor that could not name the id that rotated - or the
    variable that overrides it - would be a dead end for the two newest writes."""
    _, out, _ = run(["doctor"], client=FakeClient())
    ids = envelope(out)["data"]["query_ids"]
    assert set(social.QUERY_IDS) <= set(ids)
    assert ids["react"]["value"] == social.QUERY_IDS["react"]
    assert ids["react"]["override_env"] == "LINKEDIN_QUERY_ID_REACT"
    assert ids["unreact"]["value"] == social.QUERY_IDS["unreact"]


def test_doctor_lists_the_decoration_ids_it_lets_you_override():
    """`invite` is addressed by a versioned decoration rather than a content
    hash, so it never appears under `query_ids` - and a doctor that could not
    name it would leave the newest write with no diagnosable failure at all."""
    _, out, _ = run(["doctor"], client=FakeClient())
    ids = envelope(out)["data"]["decoration_ids"]
    assert ids["invite"]["value"] == invitations.DECORATION_IDS["invite"]
    assert ids["invite"]["override_env"] == "LINKEDIN_DECORATION_ID_INVITE"
    assert "decoration" in envelope(out)["data"]["decoration_id_recipe"].lower()


def test_doctor_reports_the_decoration_override_already_in_force(monkeypatch):
    """The shipped value would be a lie the moment an operator sets the variable,
    and the whole point of the report is telling them what is really being sent."""
    monkeypatch.setenv("LINKEDIN_DECORATION_ID_INVITE", "com.linkedin.deco.Rotated-9")
    _, out, _ = run(["doctor"], client=FakeClient())
    assert envelope(out)["data"]["decoration_ids"]["invite"]["value"] == (
        "com.linkedin.deco.Rotated-9"
    )


def test_doctor_reports_what_the_write_ledger_holds():
    """The counts were readable only by opening `state.json` on the host, which
    an agent cannot do and an operator should not have to. Exit 5 was the only
    way to discover a budget was spent - by spending it."""
    ledger = state.State()
    for kind, count in {"react": 1, "post": 3, "message": 1, "invite": 1}.items():
        for _ in range(count):
            ledger.record_write(kind)

    _, out, _ = run(["doctor"], client=FakeClient())
    kinds = envelope(out)["data"]["ledger"]["kinds"]
    assert kinds["post"]["used"] == 3
    assert kinds["post"]["remaining"] == state.DAILY_CAPS["post"] - 3
    assert kinds["invite"]["used"] == 1
    # A cap that has been spent on nothing still has to be visible, or it reads
    # as a write with no limit at all.
    assert kinds["comment"]["used"] == 0
    assert kinds["comment"]["remaining"] == state.DAILY_CAPS["comment"]


def test_doctor_shows_an_undo_as_something_other_than_a_spent_cap():
    """`post.cleanup` is recorded and never refusable. Reported inside `used` it
    would look like budget gone, on the one kind of write no budget withholds."""
    ledger = state.State()
    ledger.record_write("post")
    ledger.record_write(state.cleanup_kind("post"))

    _, out, _ = run(["doctor"], client=FakeClient())
    post = envelope(out)["data"]["ledger"]["kinds"]["post"]
    assert post["used"] == 1
    assert post["cleanup_used"] == 1
    assert post["cleanup_counts_against_cap"] is False
    assert post["remaining"] == state.DAILY_CAPS["post"] - 1


def _doctor_traffic(**kw) -> list[str]:
    client = FakeClient()
    run(["doctor"], client=client, **kw)
    return client.paths


def test_reading_the_ledger_costs_doctor_no_traffic(monkeypatch):
    """`doctor` already spends live Voyager calls on its surface probes. The
    ledger is a local file, so reporting it must add none - a diagnostic that
    charged the account for its own report is one an operator learns not to run,
    on the one client whose session has to survive.

    Compared against doctor with the ledger stubbed out rather than against a
    fixed number. Counting requests looked right and was not: doctor happens to
    issue exactly `len(PROBES)` of them for unrelated reasons - two probes re-ask
    for `me` when the member urn will not resolve - so that assertion passed by
    coincidence and would have gone on passing with a request added here.
    """
    with_ledger = _doctor_traffic()

    monkeypatch.setattr(state.State, "ledger_state", lambda self: {})
    without_ledger = _doctor_traffic()

    assert with_ledger == without_ledger


def test_what_the_ledger_holds_does_not_change_what_doctor_sends():
    """The other direction: a busy ledger must not cost more than an empty one,
    or the report gets more expensive the more the account has been used."""
    empty = _doctor_traffic()

    ledger = state.State()
    for kind, count in {"react": 1, "post": 3, "message": 1, "invite": 1}.items():
        for _ in range(count):
            ledger.record_write(kind)
    ledger.record_write(state.cleanup_kind("post"))

    assert _doctor_traffic() == empty


def test_doctor_reports_an_unreadable_ledger_as_unknown_not_as_a_quiet_day():
    """A fresh host has no state.json, which is fine and reports honest zeros. A
    *corrupt* one is not fine, and zeros presented as fact are how it reads as
    "nothing written today" - the one reading that would have an agent carry on
    writing against caps nobody can account for."""
    path = state.resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this was truncated mid-write")

    code, out, _ = run(["doctor"], client=FakeClient())
    assert code == 0
    ledger = envelope(out)["data"]["ledger"]
    assert ledger["readable"] is False
    assert ledger["kinds"] == {}
    assert "not zero" in ledger["problem"]


def test_doctor_reports_a_fresh_host_as_a_readable_empty_ledger():
    """No state file at all is the normal first run, and its zeros are true -
    reporting it as broken would send an operator looking for a fault."""
    assert not state.resolve_path().exists()
    _, out, _ = run(["doctor"], client=FakeClient())
    ledger = envelope(out)["data"]["ledger"]
    assert ledger["readable"] is True
    assert ledger["kinds"]["invite"]["used"] == 0
    assert ledger["kinds"]["invite"]["remaining"] == state.DAILY_CAPS["invite"]


def test_doctor_explains_the_cleanup_bucket_rather_than_leaving_it_to_be_inferred():
    _, out, _ = run(["doctor"], client=FakeClient())
    note = envelope(out)["data"]["ledger"]["note"]
    assert "cleanup_used" in note
    assert "never refused" in note


def test_doctor_still_answers_when_the_ledger_cannot_be_read(monkeypatch):
    """Reporting breakage is doctor's whole job. A ledger that raises must cost
    the ledger line, not the browser status and the breaker state beside it."""
    monkeypatch.setattr(
        state.State, "ledger_state", lambda self: (_ for _ in ()).throw(OSError("disk gone"))
    )
    code, out, _ = run(["doctor"], client=FakeClient())
    assert code == 0
    data = envelope(out)["data"]
    assert data["ledger"] is None
    assert data["breaker"] is None


def test_doctor_does_not_spend_or_clear_the_budget_it_reports():
    """An agent can run doctor too. A diagnostic that reset the counters would
    be exactly the loop the counters exist to stop."""
    state.State().record_write("post")
    for _ in range(3):
        assert run(["doctor"], client=FakeClient())[0] == 0
    assert state.State().write_count("post", state.DAY) == 1


def test_a_decoration_id_is_not_reported_as_a_query_id():
    """Two different failure modes with two different recipes: a queryId is a
    content hash copied out of a request, a decorationId is a schema version. One
    list holding both would send the operator to the wrong instructions."""
    _, out, _ = run(["doctor"], client=FakeClient())
    data = envelope(out)["data"]
    assert set(data["query_ids"]) & set(data["decoration_ids"]) == set()
