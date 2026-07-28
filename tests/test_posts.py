"""Publishing a post, and taking one down again.

Both request bodies here are transcriptions of traffic captured by
driving the real controls, pausing each request with CDP `Fetch` at
`requestStage: Request`, recording `request.postData` and then aborting it - so
nothing was published and nothing was deleted to learn either one. The shape
tests therefore compare the serialized body against the captured bytes rather
than sampling a few keys, and key order is part of the comparison so that a diff
against a future capture stays legible.

The two are one endpoint under two content hashes, the way `react` and `unreact`
are, and the delete is the *undo*: it is booked as a cleanup so that a spent cap
or an open breaker can never strand a post this CLI published with no way to
remove it.

What this file guards harder than the rest, and why:

* **There is no dedupe token in the payload.** `createMessage` carries an
  `originToken`; this carries nothing of the kind, so LinkedIn cannot collapse
  two identical creates and a retry publishes twice. Every path out of `create`
  is therefore pinned to exactly one request, and the failures it raises are
  pinned as *not* retryable.
* **A response nobody captured cannot be allowed to mean success.** The reader
  is deliberately liberal about *where* the new post's urn is, and absolutely
  strict about there being one: a create that answers `{}` and gets reported as
  a published post with a null urn is worse than an error, because an agent does
  not look again at a success.
* **`--visibility` is the difference between a post to Anyone and a post to
  connections.** An unknown value is refused here rather than defaulted, because
  defaulting silently is how a post meant for connections goes public.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from linkedin_cli import browser, cli, state, transport
from linkedin_cli.surfaces import posts, social
from tools import leakcheck


class NoPace:
    """The cross-process pacer with the sleep taken out: the real one waits on a
    wall clock, and a test that waits a second per request gets skipped."""

    def wait_for_slot(self, min_interval):
        return 0.0


TEXT = "hello from the CLI"

CREATE_PATH = (
    "graphql?action=execute"
    "&queryId=voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf"
)

# Captured bytes, compact separators because that is what goes on the wire.
CREATE_BODY = (
    '{"variables":{"post":{"allowedCommentersScope":"ALL",'
    '"intendedShareLifeCycleState":"PUBLISHED",'
    '"origin":"FEED",'
    '"visibilityDataUnion":{"visibilityType":"ANYONE"},'
    '"commentary":{"text":"hello from the CLI","attributesV2":[]}}},'
    '"queryId":"voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf",'
    '"includeWebMetadata":true}'
)

ACTIVITY = "urn:li:activity:7486948402790400001"
SHARE = "urn:li:share:7486948402790400000"

DELETE_PATH = (
    "graphql?action=execute"
    "&queryId=voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496"
)

# The other half of the same capture run. The `updateUrn` tuple is transcribed
# exactly - five fields, in this order, with the punctuation raw rather than
# percent-encoded, because this is a JSON body and not a RestLi query string.
DELETE_BODY = (
    '{"variables":{"updateUrn":'
    f'"urn:li:fsd_update:({ACTIVITY},FEED_DETAIL,EMPTY,DEFAULT,false)"'
    "},"
    '"queryId":"voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496",'
    '"includeWebMetadata":true}'
)

# The response shape was *not* captured - only the request was - so these are one
# plausible spelling each, and the tests below prove the readers do not depend on
# them.
CREATED = {"data": {"data": {"doCreateDashShares": {"value": {"urn": ACTIVITY}}}}}

DELETED = {"data": {"data": {"doDeleteDashShares": {"value": ACTIVITY}}}}


_DEFAULT = object()


class FakeClient:
    """Records posts and replays one canned answer. No transport, no network."""

    def __init__(self, result=_DEFAULT):
        self.posts: list[tuple] = []
        self.result = CREATED if result is _DEFAULT else result

    def post(self, path, body, dry_run=False):
        self.posts.append((path, body, dry_run))
        if dry_run:
            return {"method": "POST", "url": path, "body": body, "headers": {}}
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def get(self, path, dry_run=False):  # pragma: no cover - a create reads nothing
        raise AssertionError(f"publishing a post read {path!r}")


def wire(client) -> str:
    """The body of the last post, serialized the way the transport sends it."""
    return json.dumps(client.posts[-1][1], separators=(",", ":"))


# ------------------------------------------------------------------- query ids


def test_the_create_query_id_is_the_one_that_was_captured():
    assert (
        posts.QUERY_IDS["post_create"]
        == "voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf"
    )


def test_the_query_id_can_be_replaced_from_the_environment(monkeypatch):
    """Content hashes rotate on LinkedIn's deploys, so the operator has to be
    able to swap one without waiting for a release."""
    monkeypatch.setenv("LINKEDIN_QUERY_ID_POST_CREATE", "voyagerContentcreationDashShares.rotated")
    assert posts.query_id("post_create") == "voyagerContentcreationDashShares.rotated"


def test_an_unset_override_falls_back_to_the_shipped_id():
    assert posts.query_id("post_create") == posts.QUERY_IDS["post_create"]


def test_no_query_id_is_written_at_a_call_site():
    """A hash inlined at the call site is one no override can replace and nobody
    finds when it rotates."""
    source = Path(posts.__file__).read_text()
    for name, value in posts.QUERY_IDS.items():
        assert source.count(value) == 1, f"the {name} queryId is written more than once"


def test_the_override_reaches_both_places_the_id_travels(monkeypatch):
    """It goes out twice - in the query string and in the body - and LinkedIn
    rejects the call outright if the two disagree."""
    monkeypatch.setenv("LINKEDIN_QUERY_ID_POST_CREATE", "voyagerContentcreationDashShares.rotated")
    client = FakeClient()
    posts.create(client, TEXT)
    path, body, _ = client.posts[0]
    assert path.endswith("voyagerContentcreationDashShares.rotated")
    assert body["queryId"] == "voyagerContentcreationDashShares.rotated"


# ----------------------------------------------------------------- the payload


def test_create_posts_to_the_execute_endpoint_with_the_create_query_id():
    client = FakeClient()
    posts.create(client, TEXT)
    assert client.posts[0][0] == CREATE_PATH


def test_create_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient()
    posts.create(client, TEXT)
    assert wire(client) == CREATE_BODY


def test_the_captured_payload_carries_no_dedupe_token():
    """The premise of every single-shot rule below. `createMessage` carries an
    `originToken` LinkedIn collapses duplicates on; this carries nothing, so two
    identical creates are two public posts."""
    client = FakeClient()
    posts.create(client, TEXT)
    flat = wire(client).lower()
    for token in ("origintoken", "idempot", "dedupe", "trackingid", "requestid"):
        assert token not in flat, f"{token} appeared in a payload that was captured without one"


# ----------------------------------------------------------------- visibility


def test_visibility_defaults_to_the_value_that_was_captured():
    client = FakeClient()
    posts.create(client, TEXT)
    assert client.posts[0][1]["variables"]["post"]["visibilityDataUnion"] == {
        "visibilityType": "ANYONE"
    }


def test_visibility_changes_the_union_and_nothing_else():
    client = FakeClient()
    posts.create(client, TEXT, visibility="ANYONE")
    body = client.posts[0][1]
    assert body["variables"]["post"]["visibilityDataUnion"] == {"visibilityType": "ANYONE"}
    assert wire(client) == CREATE_BODY


def test_a_lowercase_visibility_is_normalised_rather_than_refused():
    """Agents type `anyone`; the enum is `ANYONE`, and LinkedIn answers
    a lowercase one with a bare 400 that names no field."""
    client = FakeClient()
    posts.create(client, TEXT, visibility="anyone")
    assert client.posts[0][1]["variables"]["post"]["visibilityDataUnion"] == {
        "visibilityType": "ANYONE"
    }


@pytest.mark.parametrize("value", ["PUBLIC", "anyone-plus", "", "GROUP", "1"])
def test_an_unknown_visibility_is_refused_before_anything_is_sent(value):
    """Defaulting silently is how a post meant for connections goes public."""
    client = FakeClient()
    with pytest.raises(ValueError) as caught:
        posts.create(client, TEXT, visibility=value)
    assert "ANYONE" in str(caught.value)
    assert client.posts == []


def test_the_returned_result_names_the_visibility_that_went_out():
    client = FakeClient()
    assert posts.create(client, TEXT, visibility="anyone")["visibility"] == "ANYONE"


# ----------------------------------------------------------------------- text


@pytest.mark.parametrize("value", ["", "   ", "\n", None])
def test_empty_text_is_refused_before_anything_is_sent(value):
    client = FakeClient()
    with pytest.raises(ValueError):
        posts.create(client, value)
    assert client.posts == []


def test_the_text_travels_verbatim():
    """No trimming, no escaping of its own: what the operator approved in a
    preview has to be what is published."""
    client = FakeClient()
    body = " leading and trailing spaces are the author's \n"
    posts.create(client, body)
    assert client.posts[0][1]["variables"]["post"]["commentary"]["text"] == body


# ------------------------------------------------------------- what came back


def test_create_returns_the_urn_that_identifies_the_new_post():
    client = FakeClient()
    out = posts.create(client, TEXT)
    assert out["post_urn"] == ACTIVITY
    assert out["activity_urn"] == ACTIVITY
    assert out["text"] == TEXT
    assert out["url"].endswith(ACTIVITY)


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"entityUrn": ACTIVITY}},
        {"data": {"value": {"*post": ACTIVITY}}},
        {"included": [{"$type": "com.linkedin.voyager.dash.Share", "entityUrn": ACTIVITY}]},
        {"data": {"result": [{"urn": ACTIVITY}]}},
    ],
)
def test_the_urn_is_found_wherever_the_uncaptured_response_parks_it(answer):
    """Only the *request* was captured, so the reader looks for the urn rather
    than pinning one spelling that may not be the one in play."""
    assert posts.create(FakeClient(answer), TEXT)["post_urn"] == ACTIVITY


def test_a_share_urn_is_reported_as_content_rather_than_as_the_activity():
    """They are different ids for the same post and neither is derivable from
    the other, so a share urn must not be handed back as an activity urn - every
    write in this CLI takes the activity one."""
    out = posts.create(FakeClient({"data": {"entityUrn": SHARE}}), TEXT)
    assert out["post_urn"] == SHARE
    assert out["activity_urn"] is None
    assert out["content_urn"] == SHARE


def test_a_response_carrying_both_ids_reports_both():
    answer = {"data": {"entityUrn": SHARE, "value": {"urn": ACTIVITY}}}
    out = posts.create(FakeClient(answer), TEXT)
    assert out["activity_urn"] == ACTIVITY
    assert out["content_urn"] == SHARE


def test_an_activity_urn_wins_over_a_share_urn_in_the_same_response():
    answer = {"data": {"entityUrn": SHARE, "value": {"urn": ACTIVITY}}}
    out = posts.create(FakeClient(answer), TEXT)
    assert out["post_urn"] == ACTIVITY
    assert out["activity_urn"] == ACTIVITY


@pytest.mark.parametrize(
    "answer",
    [
        {},
        None,
        "",
        {"data": None},
        {"data": {"data": {"doCreateDashShares": None}}},
        {"included": []},
        {"data": {"errors": [{"message": "INVALID_ARGUMENT"}]}},
    ],
)
def test_an_unexpected_response_is_never_reported_as_a_published_post(answer):
    """The bug this whole surface is shaped around: a create that answers
    nothing, returned as `{"post_urn": null}`, tells an agent the post is live
    and leaves it no way to find out otherwise."""
    with pytest.raises(transport.VoyagerError):
        posts.create(FakeClient(answer), TEXT)


def test_a_graphql_error_body_is_reported_as_a_failure():
    """The executor answers **200 with the failure in the body**, which
    `transport.raise_for_status` never sees."""
    answer = {"errors": [{"message": "the daily share limit was reached"}]}
    with pytest.raises(transport.VoyagerError) as caught:
        posts.create(FakeClient(answer), TEXT)
    assert "daily share limit" in str(caught.value)


# ------------------------------------------------------------------ single shot


def test_an_unconfirmed_create_issues_exactly_one_request():
    """There is no dedupe token, so a second attempt is a second public post."""
    client = FakeClient({})
    with pytest.raises(transport.VoyagerError):
        posts.create(client, TEXT)
    assert len(client.posts) == 1


def test_a_failed_create_is_not_retried_inside_the_surface():
    client = FakeClient(transport.OutcomeUnknown("the connection died mid-write"))
    with pytest.raises(transport.OutcomeUnknown):
        posts.create(client, TEXT)
    assert len(client.posts) == 1


def test_an_unconfirmed_create_is_never_reported_as_retryable():
    """`cli._report` reads `.retryable` off the exception and renders it into the
    envelope an agent branches on. A retryable create publishes twice."""
    with pytest.raises(transport.VoyagerError) as caught:
        posts.create(FakeClient({}), TEXT)
    assert getattr(caught.value, "retryable", False) is False


def test_the_unconfirmed_message_forbids_the_retry_and_says_how_to_check():
    with pytest.raises(transport.VoyagerError) as caught:
        posts.create(FakeClient({}), TEXT)
    message = str(caught.value)
    assert "retry" in message
    assert "twice" in message or "two" in message


# --------------------------------------------------------------------- dry run


def test_a_dry_run_hands_back_the_request_it_would_have_made():
    client = FakeClient()
    preview = posts.create(client, TEXT, dry_run=True)
    assert client.posts[0][2] is True
    assert preview["method"] == "POST"
    assert json.dumps(preview["body"], separators=(",", ":")) == CREATE_BODY


def test_a_dry_run_still_refuses_a_visibility_that_does_not_exist():
    """The point of a preview is to approve the request that would go out; one
    that silently corrects the flag is previewing a different request."""
    client = FakeClient()
    with pytest.raises(ValueError):
        posts.create(client, TEXT, visibility="PUBLIC", dry_run=True)
    assert client.posts == []


# ============================================================ through the CLI


def run(argv, **kw):
    out, errs = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=errs, **kw)
    return code, out.getvalue(), errs.getvalue()


def envelope(text):
    return json.loads(text)


class CliClient(FakeClient):
    """`FakeClient` with the read side allowed - `cli` builds no other client."""

    def get(self, path, dry_run=False):
        return {}


def test_post_create_publishes_and_reports_the_new_post():
    client = CliClient()
    code, out, err = run(["post", "create", f"--text={TEXT}"], client=client)
    assert code == 0, err
    assert client.posts[0][0] == CREATE_PATH
    assert envelope(out)["data"]["post_urn"] == ACTIVITY


def test_the_visibility_flag_reaches_the_body():
    """Confirmed live once that unknown flags were accepted and dropped: if that
    happened here the post meant for connections would go out to Anyone."""
    client = CliClient()
    code, _, err = run(["post", "create", f"--text={TEXT}", "--visibility=ANYONE"], client=client)
    assert code == 0, err
    assert client.posts[0][1]["variables"]["post"]["visibilityDataUnion"] == {
        "visibilityType": "ANYONE"
    }


def test_an_unknown_visibility_exits_2_and_sends_nothing():
    client = CliClient()
    code, _, err = run(["post", "create", f"--text={TEXT}", "--visibility=PUBLIC"], client=client)
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert client.posts == []


def test_post_create_without_text_exits_2():
    client = CliClient()
    code, _, err = run(["post", "create"], client=client)
    assert code == 2
    assert "--text" in envelope(err)["error"]["message"]
    assert client.posts == []


def test_post_create_refuses_an_idempotency_key_it_cannot_honour():
    """The flag exists for `messages send`, whose payload carries an
    `originToken`. This payload carries nothing of the kind, so accepting the
    key would promise a de-duplication nothing performs - and the caller most
    likely to pass it is one that intends to retry."""
    client = CliClient()
    code, _, err = run(
        ["post", "create", f"--text={TEXT}", "--idempotency-key=abc123"], client=client
    )
    assert code == 2
    message = envelope(err)["error"]["message"]
    assert "idempotency-key" in message
    assert client.posts == []


def test_post_create_is_booked_against_the_post_budget():
    code, _, err = run(["post", "create", f"--text={TEXT}"], client=CliClient())
    assert code == 0, err
    assert state.State().write_count("post", state.DAY) == 1


def test_post_create_stops_at_the_daily_cap_before_it_is_sent(monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    assert run(["post", "create", f"--text={TEXT}"], client=CliClient())[0] == 0

    second = CliClient()
    code, _, err = run(["post", "create", f"--text={TEXT}"], client=second)
    assert code == 5
    assert envelope(err)["error"]["code"] == "write_quota_exceeded"
    assert second.posts == [], "the cap was checked after the post was published"


def test_a_create_refused_before_it_is_sent_costs_no_quota():
    run(["post", "create", f"--text={TEXT}", "--visibility=PUBLIC"], client=CliClient())
    assert state.State().write_count("post", state.DAY) == 0


def test_an_unconfirmed_create_still_costs_quota_and_does_not_report_success():
    """The cap is on what this client *sends*. A response that proves nothing
    does not mean nothing was published, so it cannot be refunded."""
    client = CliClient({})
    code, out, err = run(["post", "create", f"--text={TEXT}"], client=client)
    assert code == 6
    assert envelope(err)["error"]["retryable"] is False
    assert out == ""
    assert state.State().write_count("post", state.DAY) == 1


def test_a_dry_run_previews_the_post_and_spends_nothing():
    client = CliClient()
    code, out, err = run(["post", "create", f"--text={TEXT}", "--dry-run"], client=client)
    assert code == 0, err
    assert client.posts[-1][2] is True
    assert envelope(out)["data"]["body"]["variables"]["post"]["commentary"]["text"] == TEXT
    assert state.State().write_count("post", state.DAY) == 0


def test_a_dry_run_asks_the_browser_for_nothing_but_its_status():
    """End to end through a real `BrowserClient`, because the preview an operator
    approves a public post from is built in `browser.py` and the whole point of
    it is that nothing was published."""
    asked = []

    def request_fn(payload, **kwargs):
        asked.append(payload)
        if payload.get("op") == "status":
            return {"pid": 4242, "profile": "/managed"}
        raise AssertionError("--dry-run published a post")

    client = browser.BrowserClient(rate=1.0, state=NoPace(), request_fn=request_fn)
    code, out, err = run(["post", "create", f"--text={TEXT}", "--dry-run"], client=client)
    assert code == 0, err
    assert [p["op"] for p in asked] == ["status"]
    assert envelope(out)["data"]["body"]["variables"]["post"]["commentary"]["text"] == TEXT


def test_a_dry_run_is_still_previewable_once_the_daily_cap_is_gone(monkeypatch):
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    assert run(["post", "create", f"--text={TEXT}"], client=CliClient())[0] == 0
    assert run(["post", "create", f"--text={TEXT}", "--dry-run"], client=CliClient())[0] == 0


@pytest.mark.parametrize("answer", [CREATED, {"data": {"entityUrn": SHARE}}])
def test_text_output_still_names_the_post_that_was_just_published(answer):
    """`--format=text` renders a post block off `activity_urn`, and a create that
    answered with only a share urn used to leave that key null - so the one
    identifier of a post that is already public was dropped from the output."""
    code, out, err = run(
        ["post", "create", f"--text={TEXT}", "--format=text"], client=CliClient(answer)
    )
    assert code == 0, err
    # `render` puts the identifier on the first line of a block, and a blank one
    # is the shape of a post nobody can point at.
    assert out.splitlines()[0].startswith("urn:li:")


# ------------------------------------------------- flags aimed at the wrong action


@pytest.mark.parametrize("flag", ["--text=hello", "--visibility=ANYONE"])
def test_a_create_only_flag_is_refused_by_post_get(flag):
    """The allowlist is granular to the verb, so `post get --visibility=…` parses
    and is dropped. A flag nothing reads is exactly as silent as one that does
    not exist, and this one is about who can see a post.

    `post delete` is checked the same way further down - it used to need no check
    because it refused every argument it was given.
    """
    client = CliClient()
    code, _, err = run(["post", "get", ACTIVITY, flag], client=client)
    assert code == 2
    assert "post create" in envelope(err)["error"]["message"]
    assert client.posts == []


# ============================================================== post delete
#
# Captured the same way and on the same day as the create above: the real
# control was driven, the request paused at `requestStage: Request`, the body
# recorded and then aborted - so no post was deleted to learn how to delete one.


def test_the_delete_query_id_is_the_one_that_was_captured():
    assert (
        posts.QUERY_IDS["post_delete"]
        == "voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496"
    )


def test_the_delete_query_id_can_be_replaced_from_the_environment(monkeypatch):
    monkeypatch.setenv("LINKEDIN_QUERY_ID_POST_DELETE", "voyagerContentcreationDashShares.rotated")
    assert posts.query_id("post_delete") == "voyagerContentcreationDashShares.rotated"


def test_the_delete_override_reaches_both_places_the_id_travels(monkeypatch):
    """Same trap as the create: the id goes out in the query string *and* in the
    body, and LinkedIn rejects the call outright if the two disagree."""
    monkeypatch.setenv("LINKEDIN_QUERY_ID_POST_DELETE", "voyagerContentcreationDashShares.rotated")
    client = FakeClient(DELETED)
    posts.delete(client, ACTIVITY)
    path, body, _ = client.posts[0]
    assert path.endswith("voyagerContentcreationDashShares.rotated")
    assert body["queryId"] == "voyagerContentcreationDashShares.rotated"


def test_delete_posts_to_the_execute_endpoint_with_the_delete_query_id():
    """A different content hash from the create, not a flag on it - the same
    shape `react`/`unreact` already have."""
    client = FakeClient(DELETED)
    posts.delete(client, ACTIVITY)
    assert client.posts[0][0] == DELETE_PATH
    assert DELETE_PATH != CREATE_PATH


def test_delete_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient(DELETED)
    posts.delete(client, ACTIVITY)
    assert wire(client) == DELETE_BODY


def test_the_update_urn_wraps_the_activity_urn_in_the_captured_tuple():
    """The five fields are fixed, in this order, and the trailing `false` is a
    bare token rather than a string - all of it transcribed, none of it guessed."""
    assert posts.update_urn(ACTIVITY) == (
        f"urn:li:fsd_update:({ACTIVITY},FEED_DETAIL,EMPTY,DEFAULT,false)"
    )


def test_the_update_urns_punctuation_travels_literally_in_the_json_body():
    """`restli.encode` percent-encodes `(`, `)` and `,` because a *query string*
    parses them as structure. This body is JSON, where they are ordinary
    characters, and the capture carries them raw - so encoding here would send a
    body LinkedIn was never observed accepting."""
    client = FakeClient(DELETED)
    posts.delete(client, ACTIVITY)
    sent = client.posts[0][1]["variables"]["updateUrn"]
    assert sent == posts.update_urn(ACTIVITY)
    assert "%28" not in wire(client) and "%2C" not in wire(client)


# ------------------------------------------------------- the urn it will act on


def test_delete_refuses_a_share_urn_rather_than_converting_it():
    """The two ids are not derivable from each other. Guessing one from the other
    on a *delete* takes down a post nobody asked about."""
    client = FakeClient(DELETED)
    with pytest.raises(ValueError) as caught:
        posts.delete(client, SHARE)
    assert "activity" in str(caught.value)
    assert client.posts == []


@pytest.mark.parametrize(
    "value",
    [
        "7486948402790400000",
        "https://www.linkedin.com/feed/update/urn:li:share:7486948402790400000/",
        "urn:li:ugcPost:7486948402790400000",
        "",
        None,
        "urn:li:activity:not-a-number",
    ],
)
def test_delete_refuses_anything_that_is_not_plainly_an_activity_urn(value):
    """Deliberately the strict validator `react`/`comment` use, not the lenient
    one `feed`/`post get` accept a bare number through. A post's share id and its
    activity id are both bare numbers, so a bare number names a post only by
    luck - and a delete run on the wrong post cannot be taken back."""
    client = FakeClient(DELETED)
    with pytest.raises(ValueError):
        posts.delete(client, value)
    assert client.posts == []


def test_the_delete_validator_is_the_one_the_other_writes_share():
    """Reused rather than re-implemented: a second copy is one that drifts, and
    the copy that drifts on a delete is the one that matters most."""
    assert posts.activity_urn is social.activity_urn


# ------------------------------------------------------------- what came back


def test_delete_returns_the_post_it_took_down():
    client = FakeClient(DELETED)
    out = posts.delete(client, ACTIVITY)
    assert out["activity_urn"] == ACTIVITY
    assert out["deleted"] is True
    assert out["update_urn"] == posts.update_urn(ACTIVITY)


def test_a_deleted_post_is_not_reported_with_a_url_that_now_404s():
    """Every other write returns the post's permalink. This one just removed the
    thing that permalink pointed at, so handing it back invites an agent to cite
    a dead link as evidence the write worked."""
    assert "url" not in posts.delete(FakeClient(DELETED), ACTIVITY)


@pytest.mark.parametrize(
    "answer",
    [
        {},
        None,
        "",
        [],
        {"data": None},
        {"data": {}},
        {"included": []},
        {"errors": [{"message": "INVALID_ARGUMENT"}]},
        {"data": {"errors": [{"message": "NOT_AUTHORIZED"}]}},
    ],
)
def test_an_unexpected_response_is_never_reported_as_a_deleted_post(answer):
    """The response shape was never captured - only the request was. A delete
    reported as done on a body that proves nothing is worse than an error: an
    agent does not look again at a success, and the post is still public."""
    with pytest.raises(transport.VoyagerError):
        posts.delete(FakeClient(answer), ACTIVITY)


def test_a_graphql_error_body_is_reported_as_a_failed_delete():
    """The executor answers **200 with the failure in the body**, which
    `transport.raise_for_status` never sees."""
    with pytest.raises(transport.VoyagerError) as caught:
        posts.delete(FakeClient({"errors": [{"message": "ENTITY_NOT_FOUND"}]}), ACTIVITY)
    assert "ENTITY_NOT_FOUND" in str(caught.value)


def test_an_unconfirmed_delete_names_the_post_and_says_how_to_check():
    with pytest.raises(transport.VoyagerError) as caught:
        posts.delete(FakeClient({}), ACTIVITY)
    message = str(caught.value)
    assert ACTIVITY in message
    assert "post get" in message


def test_an_unconfirmed_delete_issues_exactly_one_request():
    client = FakeClient({})
    with pytest.raises(transport.VoyagerError):
        posts.delete(client, ACTIVITY)
    assert len(client.posts) == 1


# --------------------------------------------------------------------- dry run


def test_a_dry_run_delete_hands_back_the_request_it_would_have_made():
    client = FakeClient(DELETED)
    preview = posts.delete(client, ACTIVITY, dry_run=True)
    assert client.posts[0][2] is True
    assert preview["method"] == "POST"
    assert json.dumps(preview["body"], separators=(",", ":")) == DELETE_BODY


def test_a_dry_run_delete_still_refuses_a_urn_it_would_not_send():
    client = FakeClient(DELETED)
    with pytest.raises(ValueError):
        posts.delete(client, SHARE, dry_run=True)
    assert client.posts == []


# ============================================================ through the CLI


def test_post_delete_sends_the_captured_payload():
    client = CliClient(DELETED)
    code, out, err = run(["post", "delete", ACTIVITY, "--yes"], client=client)
    assert code == 0, err
    assert client.posts[0][0] == DELETE_PATH
    assert wire(client) == DELETE_BODY
    assert envelope(out)["data"]["deleted"] is True


def test_post_delete_without_the_opt_in_sends_nothing():
    """An opt-in rather than a prompt: the caller is a program, and this CLI can
    be driven by one that has read an attacker's text. Deleting a post takes its
    reactions and every comment on it with it, and LinkedIn offers no undo."""
    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", ACTIVITY], client=client)
    assert code == 2
    assert "--yes" in envelope(err)["error"]["message"]
    assert client.posts == []


def test_post_delete_needs_a_post_to_act_on():
    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", "--yes"], client=client)
    assert code == 2
    assert client.posts == []


def test_post_delete_refuses_a_share_urn_with_exit_2_and_sends_nothing():
    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", SHARE, "--yes"], client=client)
    assert code == 2
    assert envelope(err)["error"]["code"] == "usage"
    assert client.posts == []


def test_a_dry_run_delete_previews_without_the_opt_in_and_spends_nothing():
    """A preview sends nothing, so there is nothing for `--yes` to confirm - and
    demanding it would take away the one way to inspect the request first."""
    client = CliClient(DELETED)
    code, out, err = run(["post", "delete", ACTIVITY, "--dry-run"], client=client)
    assert code == 0, err
    assert client.posts[-1][2] is True
    assert envelope(out)["data"]["body"]["variables"]["updateUrn"] == posts.update_urn(ACTIVITY)
    assert state.State().write_count(state.cleanup_kind("post"), state.DAY) == 0


# --------------------------------------------------------- the undo exemption


def test_post_delete_is_booked_as_a_cleanup_rather_than_against_the_post_budget():
    """A delete charged to the create budget makes every publish-and-undo pair
    cost two, so cleaning up an over-run pushes the window further out than the
    over-run did."""
    code, _, err = run(["post", "delete", ACTIVITY, "--yes"], client=CliClient(DELETED))
    assert code == 0, err
    assert state.State().write_count("post", state.DAY) == 0
    assert state.State().write_count(state.cleanup_kind("post"), state.DAY) == 1


def test_post_delete_still_works_once_the_post_cap_is_spent(monkeypatch):
    """The concrete incident this exists for: a run publishes, trips the cap, and
    the verb that would take the post back down is the verb the cap just refused.
    A live public post with no CLI way to remove it is a worse outcome than the
    over-run the cap was protecting against."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    assert run(["post", "create", f"--text={TEXT}"], client=CliClient())[0] == 0
    assert run(["post", "create", f"--text={TEXT}"], client=CliClient())[0] == 5

    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", ACTIVITY, "--yes"], client=client)
    assert code == 0, err
    assert client.posts, "the undo never reached the wire"


def test_post_delete_still_works_while_the_breaker_is_open():
    """Same argument one layer up: the breaker refuses a whole verb before its
    handler is dispatched, so exempting the ledger alone still strands the post."""
    state.State().trip_breaker("HTTP 999")
    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", ACTIVITY, "--yes"], client=client)
    assert code == 0, err
    assert client.posts, "the undo never reached the wire"


def test_post_delete_still_works_while_a_throttle_cooldown_is_running():
    """The third refusal a cleanup has to survive, and the one the other two undo
    tests do not reach: `_guard_write` checks the breaker, the cooldown and the
    caps, and `cleanup=True` skips the whole function rather than three of its
    branches. `post create` under the same cooldown is the control - if it were
    let through too, this test would be measuring nothing."""
    state.State().record_throttle("HTTP 429")

    refused = CliClient()
    assert run(["post", "create", f"--text={TEXT}"], client=refused)[0] == 5
    assert refused.posts == []

    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", ACTIVITY, "--yes"], client=client)
    assert code == 0, err
    assert client.posts, "the undo never reached the wire"


def test_post_create_does_not_inherit_the_exemption_from_its_own_verb(monkeypatch):
    """`CLEANUP_ACTIONS` is keyed verb -> actions precisely so that one action of
    a verb can be an undo while its sibling is not. Keyed on the verb alone,
    publishing would become unstoppable the moment its inverse shipped."""
    monkeypatch.setitem(state.DAILY_CAPS, "post", 1)
    assert run(["post", "create", f"--text={TEXT}"], client=CliClient())[0] == 0
    spent = CliClient()
    assert run(["post", "create", f"--text={TEXT}"], client=spent)[0] == 5
    assert spent.posts == []

    state.State().trip_breaker("HTTP 999")
    blocked = CliClient()
    assert run(["post", "create", f"--text={TEXT}"], client=blocked)[0] == 9
    assert blocked.posts == [], "the breaker let a fresh post through"


def test_an_unconfirmed_delete_still_costs_a_cleanup_slot_and_reports_the_failure():
    """The cap is on what this client *sends*. A response that proves nothing
    does not mean the post is still up, so it cannot be refunded."""
    client = CliClient({})
    code, out, err = run(["post", "delete", ACTIVITY, "--yes"], client=client)
    assert code == 6
    assert out == ""
    assert envelope(err)["error"]["retryable"] is False
    assert state.State().write_count(state.cleanup_kind("post"), state.DAY) == 1


def test_the_undo_exemption_names_post_delete_and_not_post_create():
    assert cli.CLEANUP_ACTIONS["post"] == frozenset({"delete"})
    assert cli.is_cleanup("post", ["delete", ACTIVITY]) is True
    assert cli.is_cleanup("post", ["create"]) is False


# ------------------------------------------------ flags aimed at the wrong action


@pytest.mark.parametrize("flag", ["--text=hello", "--visibility=ANYONE"])
def test_a_create_only_flag_is_refused_by_post_delete(flag):
    """Same hole as `post get`: the allowlist is granular to the verb, so these
    parse on `post delete` and are dropped. Silently accepting `--text` on a
    delete reads like an edit, which is not what this does."""
    client = CliClient(DELETED)
    code, _, err = run(["post", "delete", ACTIVITY, "--yes", flag], client=client)
    assert code == 2
    assert "post create" in envelope(err)["error"]["message"]
    assert client.posts == []


# ---------------------------------------------------------------------- doctor


@pytest.mark.parametrize("name", ["post_create", "post_delete"])
def test_doctor_reports_every_post_query_id_and_its_override(name):
    """Exit 7 tells the operator to run doctor, and a doctor that cannot name the
    id that rotated is a dead end. Both hashes rotate independently - they are
    two content hashes on one endpoint, not one id with a flag."""
    report = envelope(run(["doctor"], client=CliClient())[1])["data"]
    entry = report["query_ids"][name]
    assert entry["value"] == posts.QUERY_IDS[name]
    assert entry["override_env"] == f"LINKEDIN_QUERY_ID_{name.upper()}"


def test_no_two_surfaces_claim_the_same_query_id_name():
    """`doctor` collects them into one dict keyed by name, so a collision would
    silently drop one surface's id from the only report that names it."""
    seen: list[str] = []
    for surface in cli.QUERY_ID_SURFACES:
        seen.extend(surface.QUERY_IDS)
    assert len(seen) == len(set(seen)), sorted(seen)


def test_connections_is_refused_because_linkedin_has_no_such_enum_value():
    """Measured, not assumed. LinkedIn answers 200 with

        Invalid input for enum 'dash_contentcreation_VisibilityType'.
        No value found for name 'CONNECTIONS'

    README advertised the flag, the CLI accepted it, and the post was silently
    never created. Only the value the capture carried is known good.
    """
    client = FakeClient()
    with pytest.raises(ValueError) as caught:
        posts.create(client, TEXT, visibility="CONNECTIONS")
    assert not client.posts
    assert "ANYONE" in str(caught.value)


def test_a_graphql_error_nested_under_data_is_a_refusal_not_an_unknown_outcome():
    """LinkedIn refuses a bad create with 200 and the errors under `data`.

    The check used to read only the top level, so it fell through, found no urn,
    and reported "it may already be public" for a post that was never created.
    Measured live: two attempts with a bad visibilityType created nothing, and
    the profile activity page still showed the previous post as the newest.

    The distinction is what an agent acts on. `OutcomeUnknown` sends it to go
    looking for a post; a refusal tells it the request was rejected.
    """
    client = FakeClient(
        {
            "data": {
                "errors": [
                    {
                        "message": (
                            "Variable 'post' has an invalid value: Invalid input for enum "
                            "'dash_contentcreation_VisibilityType'. No value found for name 'X'"
                        )
                    }
                ]
            }
        }
    )
    with pytest.raises(transport.UpstreamError) as caught:
        posts.create(client, TEXT)
    message = str(caught.value)
    assert "refused" in message.lower()
    assert "Nothing was created" in message
    assert not isinstance(caught.value, transport.OutcomeUnknown)


# ------------------------------------------------ credentials in LinkedIn's prose

# `transport.raise_for_status` scrubs the body it splices into an error, but a
# refusal that arrives inside a **200** never reaches it: `create` reads the
# `errors` array itself and hands `_error_text` straight to `_refused`, and
# `_confirm_removed` does the same into `_not_removed`. That string is whatever
# LinkedIn wrote, `cli._report` renders this exception's `str()` onto stderr, and
# under an agent gateway stderr is permanent model context - so a csrf token quoted back
# inside a refusal is a live session nobody can retract. render.py's docstring
# claims nothing reaching an envelope is unredacted "by construction rather than
# by the caller remembering"; these two write paths are where that stopped being
# true.


def credential_detail() -> str:
    """A refusal message that carries live credentials in LinkedIn's own prose.

    Assembled from pieces rather than written out, for the reason
    `tests/test_transport.py::credential_body` gives: a literal live-shaped
    `li_at` in a tracked file is what `tools/leakcheck.py` fails the build for,
    and this fixture is only worth anything if it looks real.
    """
    return (
        'CHALLENGE_REQUIRED: csrf_token "ajax:1111222233334444555" was rejected for '
        'li_at="AQED' + "aB3_-" * 12 + '" - re-authenticate and retry'
    )


def assert_scrubbed_but_still_diagnostic(message: str) -> None:
    """No credential shape survives, and LinkedIn's own wording still does.

    Both halves matter. Dropping the detail wholesale would trade a leak for an
    error nobody can act on, which is the trade `transport.scrub_secrets` was
    written to avoid; the leakcheck sweep is pinned to the scanner itself so a
    shape added there fails here rather than the first time a live body carries
    one.
    """
    assert "ajax:1111222233334444555" not in message
    assert "AQED" + "aB3_-" not in message
    for label, pattern in leakcheck.PATTERNS:
        assert not pattern.findall(message), label
    assert transport.REDACTED in message
    assert "CHALLENGE_REQUIRED" in message
    assert "csrf_token" in message


@pytest.mark.parametrize("answer_key", ["top-level", "under-data"])
def test_a_refused_create_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """The refusal is read out of a 200 body, so nothing upstream of here scrubbed
    it - `transport.raise_for_status` never saw this response at all.

    Checked at both placements the GraphQL executor parks an `errors` list at,
    because a scrubber applied to only one of them is the same bug one level
    down - and `_errors` reading both is exactly why they are both reachable.
    """
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(transport.UpstreamError) as caught:
        posts.create(FakeClient(answer), TEXT)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


@pytest.mark.parametrize("answer_key", ["top-level", "under-data"])
def test_a_refused_delete_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """The other half of the same 200-body gap. `delete` is booked as a cleanup,
    so it is the one write no cap and no open breaker can withhold - which makes
    it the path most likely to be running when a session goes bad and LinkedIn
    starts quoting the credentials back."""
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(transport.UpstreamError) as caught:
        posts.delete(FakeClient(answer), ACTIVITY)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


def test_an_unconfirmed_create_scrubs_its_detail_by_construction():
    """Today every `_unconfirmed` detail is a literal written in this file, so no
    live path leaks through it. It is scrubbed anyway, because the guarantee
    render.py claims is "by construction rather than by the caller remembering" -
    and the caller that forgets is the one added later."""
    assert_scrubbed_but_still_diagnostic(str(posts._unconfirmed(credential_detail())))


def test_a_scrubbed_delete_failure_still_names_the_post_to_read_back():
    """The scrubber is applied to LinkedIn's detail, not to the whole message.
    `activity` is the caller's own argument and the only thing here that says
    which post to go and look at - a blanket scrub would trade the leak for a
    tool that cannot say what it just failed to delete."""
    message = str(posts._not_removed(ACTIVITY, credential_detail()))
    assert ACTIVITY in message
    assert "post get" in message


def test_a_refused_create_reaches_stderr_scrubbed_through_the_cli():
    """End to end, because the leak is only a leak once `cli._report` has written
    it: `str(exc)` becomes `error.message`, and under an agent gateway that stream is
    permanent model context. `--raw` is off here on purpose - `render.scrub_body`
    already covers `error.body`, and the message is the field that did not."""
    errors = [{"message": credential_detail()}]
    code, out, err = run(
        ["post", "create", f"--text={TEXT}"], client=CliClient({"data": {"errors": errors}})
    )
    assert code == 6
    assert out == ""
    assert_scrubbed_but_still_diagnostic(envelope(err)["error"]["message"])
    assert_scrubbed_but_still_diagnostic(err)


def test_a_refused_delete_reaches_stderr_scrubbed_through_the_cli():
    errors = [{"message": credential_detail()}]
    code, out, err = run(
        ["post", "delete", ACTIVITY, "--yes"], client=CliClient({"errors": errors})
    )
    assert code == 6
    assert out == ""
    assert_scrubbed_but_still_diagnostic(envelope(err)["error"]["message"])
    assert ACTIVITY in envelope(err)["error"]["message"], "the post to read back is still named"
