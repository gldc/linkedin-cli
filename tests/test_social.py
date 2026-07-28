"""Reactions and comments - the two writes captured off the live client.

The payloads here are transcriptions of real traffic recorded by
`tools/capture_payloads.py`, not inferences, so the shape tests compare the
serialized body against the captured bytes rather than checking a few keys. Key
order is part of that comparison on purpose: it is what makes a diff against a
future capture legible.

Three properties are pinned harder than the rest, because each one is a way this
surface could look like it worked while doing nothing:

* **Like and unlike are different queryIds.** They are one endpoint with two
  content hashes, and only the like carries `entity.reactionType`. Sending the
  like hash without the type, or the unlike hash with it, is not a variation on
  the same call.
* **A shapeless response is a failure.** A write that answers `{}` and gets
  reported as a success with a null urn is worse than an error, because an agent
  will not retry it and will believe the comment is posted.
* **`urn:li:share:` is not `urn:li:activity:`.** They are different ids for the
  same post and neither can be derived from the other, so a share urn is refused
  rather than converted.

`delete_comment` is the exception to the first line of this docstring: its route
was verified live rather than intercepted, against a throwaway post
this CLI had published seconds earlier. Its own section below pins the three
things that cost two probe rounds - the doubled urn, the asymmetric collections,
and an empty 2xx being the success rather than a shapeless failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin_cli import transport
from linkedin_cli.surfaces import social
from tools import leakcheck

# The post the capture was taken against. Its *URL* carries
# `urn:li:share:7486948402790400000` - a different id for the same post, which is
# the entire reason nothing here guesses a conversion between the two.
ACTIVITY = "urn:li:activity:7486948402790400001"
SHARE = "urn:li:share:7486948402790400000"

# Captured bytes. Compact separators because that is what `json.dumps` writes on
# the wire, and the comparison is against the recorded request body.
REACT_BODY = (
    '{"variables":{"entity":{"reactionType":"LIKE"},'
    '"threadUrn":"urn:li:activity:7486948402790400001"},'
    '"queryId":"voyagerSocialDashReactions.b731222600772fd42464c0fe19bd722b",'
    '"includeWebMetadata":true}'
)

UNREACT_BODY = (
    '{"variables":{"threadUrn":"urn:li:activity:7486948402790400001"},'
    '"queryId":"voyagerSocialDashReactions.f68b48ae5bc0085d7a45c7003b772a39",'
    '"includeWebMetadata":true}'
)

COMMENT_BODY = (
    '{"commentary":{"text":"nicely put","attributesV2":[],'
    '"$type":"com.linkedin.voyager.dash.common.text.TextViewModel"},'
    '"threadUrn":"urn:li:activity:7486948402790400001"}'
)

REACT_PATH = (
    "graphql?action=execute&queryId=voyagerSocialDashReactions.b731222600772fd42464c0fe19bd722b"
)

UNREACT_PATH = (
    "graphql?action=execute&queryId=voyagerSocialDashReactions.f68b48ae5bc0085d7a45c7003b772a39"
)

COMMENT_PATH = (
    "voyagerSocialDashNormComments"
    "?decorationId=com.linkedin.voyager.dash.deco.social.NormComment-43"
)

# The comment urn in its two spellings. `INNER` is the key the delete route
# takes; `DOUBLED` is what LinkedIn answers a create with and therefore what
# `social.comment` reports, so it is the string a caller copy-pastes.
INNER_COMMENT = f"urn:li:fsd_comment:(7487000000000000000,{ACTIVITY})"
DOUBLED_COMMENT = f"urn:li:fsd_normComment:{INNER_COMMENT}"

# Written out rather than built with `urllib.parse.quote`, for the reason the
# request bodies above are: a test that encodes the key the same way the code
# does agrees with the code by construction and would follow it into a wrong
# encoding. This is the path the route was verified against.
DELETE_COMMENT_PATH = (
    "voyagerSocialDashNormComments/"
    "urn%3Ali%3Afsd_comment%3A%287487000000000000000%2C"
    "urn%3Ali%3Aactivity%3A7486948402790400001%29"
)

# The collection the *entity* reads back from. It is not the one the delete goes
# to, and sending deletes here is the false start that cost the second probe
# round (docs/write-payloads.md).
READ_COLLECTION = "voyagerSocialDashComments/"

# What a created comment comes back as. The response shape was not captured, so
# the reader is deliberately liberal - but it must find *something*, and this is
# the RestLi-create spelling it is tried against first.
CREATED_COMMENT = {
    "data": {
        "$type": "com.linkedin.voyager.dash.social.NormComment",
        "entityUrn": "urn:li:fsd_comment:(7486857700000000000,urn:li:activity:7486948402790400001)",
    }
}

REACTED = {"data": {"data": {"doAddReactionV2": {"value": True}}}}


# A sentinel rather than `None`, because `None` is one of the answers under
# test: defaulting on it would quietly turn "the response was null" into "the
# response was fine", which is the exact bug these tests exist to catch.
_DEFAULT = object()


class FakeClient:
    """Records requests and replays one canned answer. No transport, no network.

    `_request` is here because `delete_comment` needs a method neither
    `transport.VoyagerClient` nor `browser.BrowserClient` exposes: both build
    every call from `_request(method, path, body, dry_run)` and put only `get`
    and `post` on top of it. The fake mirrors that shape rather than inventing a
    friendlier one, or the surface would be tested against a client the CLI does
    not have.
    """

    def __init__(self, result=_DEFAULT):
        self.posts: list[tuple] = []
        self.requests: list[tuple] = []
        self.result = REACTED if result is _DEFAULT else result

    def post(self, path, body, dry_run=False):
        self.posts.append((path, body, dry_run))
        return self._request("POST", path, body, dry_run)

    def _request(self, method, path, body, dry_run=False):
        self.requests.append((method, path, body, dry_run))
        if dry_run:
            # What `browser.BrowserClient` hands back for a preview: the request
            # it *would* have made, never an upstream payload.
            return {"method": method, "url": path, "body": body, "headers": {}}
        return self.result


def wire(client) -> str:
    """The body of the last post, serialized the way the transport sends it."""
    return json.dumps(client.posts[-1][1], separators=(",", ":"))


# ------------------------------------------------------------------- query ids


def test_like_and_unlike_are_different_query_ids():
    """One endpoint, two content hashes. Collapsing them into one id would send
    an unlike to the like mutation, which answers 200 and does nothing."""
    assert social.QUERY_IDS["react"] != social.QUERY_IDS["unreact"]
    assert social.QUERY_IDS["react"].startswith("voyagerSocialDashReactions.")
    assert social.QUERY_IDS["unreact"].startswith("voyagerSocialDashReactions.")


def test_a_query_id_can_be_replaced_from_the_environment(monkeypatch):
    """They are content hashes that rotate on LinkedIn's deploys, so the operator
    has to be able to swap one without waiting for a release."""
    monkeypatch.setenv("LINKEDIN_QUERY_ID_REACT", "voyagerSocialDashReactions.deadbeef")
    assert social.query_id("react") == "voyagerSocialDashReactions.deadbeef"
    assert social.query_id("unreact") == social.QUERY_IDS["unreact"]


def test_an_unset_override_falls_back_to_the_shipped_id():
    assert social.query_id("react") == social.QUERY_IDS["react"]


def test_no_query_id_is_written_at_a_call_site():
    """A hash inlined at the call site is one no override can replace and nobody
    finds when it rotates. Checked against the source, because that is the only
    way this regresses - the behaviour above stays green either way."""
    source = Path(social.__file__).read_text()
    for name, value in social.QUERY_IDS.items():
        assert source.count(value) == 1, f"the {name} queryId is written more than once"


# --------------------------------------------------------------------- react


def test_react_posts_to_the_execute_endpoint_with_the_like_query_id():
    client = FakeClient()
    social.react(client, ACTIVITY)
    assert client.posts[0][0] == REACT_PATH


def test_react_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient()
    social.react(client, ACTIVITY)
    assert wire(client) == REACT_BODY


def test_react_carries_the_reaction_type_the_caller_asked_for():
    client = FakeClient()
    social.react(client, ACTIVITY, reaction="PRAISE")
    assert client.posts[0][1]["variables"]["entity"] == {"reactionType": "PRAISE"}


def test_react_returns_what_it_did_rather_than_the_raw_graph():
    client = FakeClient()
    out = social.react(client, ACTIVITY)
    assert out["activity_urn"] == ACTIVITY
    assert out["reaction"] == "LIKE"
    assert out["reacted"] is True
    assert out["url"].endswith(ACTIVITY)


def test_react_honours_the_query_id_override_at_the_url(monkeypatch):
    monkeypatch.setenv("LINKEDIN_QUERY_ID_REACT", "voyagerSocialDashReactions.rotated")
    client = FakeClient()
    social.react(client, ACTIVITY)
    path, body, _ = client.posts[0]
    assert path.endswith("voyagerSocialDashReactions.rotated")
    # The id travels twice - in the query string and in the body - and LinkedIn
    # rejects the call outright if the two disagree.
    assert body["queryId"] == "voyagerSocialDashReactions.rotated"


# -------------------------------------------------------------------- unreact


def test_unreact_uses_its_own_query_id():
    client = FakeClient()
    social.unreact(client, ACTIVITY)
    assert client.posts[0][0] == UNREACT_PATH


def test_unreact_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient()
    social.unreact(client, ACTIVITY)
    assert wire(client) == UNREACT_BODY


def test_unreact_sends_no_reaction_type():
    """The capture has no `entity` at all on the unlike. Sending one is not a
    harmless extra field - it is a different mutation's payload."""
    client = FakeClient()
    social.unreact(client, ACTIVITY)
    assert "entity" not in client.posts[0][1]["variables"]


def test_unreact_reports_the_reaction_as_removed():
    client = FakeClient()
    out = social.unreact(client, ACTIVITY)
    assert out["activity_urn"] == ACTIVITY
    assert out["reacted"] is False


# -------------------------------------------------------------------- comment


def test_comment_posts_to_the_decorated_norm_comments_collection():
    client = FakeClient(CREATED_COMMENT)
    social.comment(client, ACTIVITY, "nicely put")
    assert client.posts[0][0] == COMMENT_PATH


def test_comment_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient(CREATED_COMMENT)
    social.comment(client, ACTIVITY, "nicely put")
    assert wire(client) == COMMENT_BODY


def test_comment_returns_the_urn_of_the_comment_it_created():
    client = FakeClient(CREATED_COMMENT)
    out = social.comment(client, ACTIVITY, "nicely put")
    assert out["comment_urn"] == CREATED_COMMENT["data"]["entityUrn"]
    assert out["activity_urn"] == ACTIVITY
    assert out["text"] == "nicely put"


def test_comment_is_not_a_graphql_call():
    """It is a plain decorated RestLi create, so nothing about the queryId
    machinery applies to it - including its rotation."""
    client = FakeClient(CREATED_COMMENT)
    social.comment(client, ACTIVITY, "hi")
    assert "queryId" not in client.posts[0][0]
    assert "queryId" not in client.posts[0][1]


# -------------------------------------------------------------------- dry run


@pytest.mark.parametrize(
    "call",
    [
        lambda c: social.react(c, ACTIVITY, dry_run=True),
        lambda c: social.unreact(c, ACTIVITY, dry_run=True),
        lambda c: social.comment(c, ACTIVITY, "nicely put", dry_run=True),
    ],
    ids=["react", "unreact", "comment"],
)
def test_a_dry_run_previews_the_request_and_asserts_nothing_about_a_response(call):
    """The preview is the request, so the postconditions below must not fire on
    it - a preview that raised `UpstreamError` would be unusable for the one
    thing it exists for, which is checking a write before making it."""
    client = FakeClient()
    out = call(client)
    assert client.posts[0][2] is True
    assert out["method"] == "POST"
    assert "li_at" not in json.dumps(out)


# ------------------------------------------------------------- postconditions

# Every way a write can answer without having done anything. `{}` is what
# `transport.parse` returns for an empty body, so it is the shape a 200 with no
# content actually arrives as.
SHAPELESS = [{}, None, [], "", {"data": None}]


@pytest.mark.parametrize("answer", SHAPELESS, ids=lambda a: repr(a)[:12])
@pytest.mark.parametrize(
    "call",
    [
        lambda c: social.react(c, ACTIVITY),
        lambda c: social.unreact(c, ACTIVITY),
        lambda c: social.comment(c, ACTIVITY, "nicely put"),
    ],
    ids=["react", "unreact", "comment"],
)
def test_a_shapeless_response_is_a_failure_not_a_silent_success(call, answer):
    """The failure mode this exists for: a write that reports success with a
    null id is worse than an error, because an agent will not retry it."""
    client = FakeClient(answer)
    with pytest.raises(transport.UpstreamError):
        call(client)


@pytest.mark.parametrize(
    "call",
    [lambda c: social.react(c, ACTIVITY), lambda c: social.unreact(c, ACTIVITY)],
    ids=["react", "unreact"],
)
def test_a_graphql_errors_array_is_a_failure_even_under_a_200(call):
    """Voyager's GraphQL executor answers 200 and puts the failure in the body,
    so `raise_for_status` never sees it and this is the only place left."""
    client = FakeClient({"data": {"data": None}, "errors": [{"message": "INVALID_THREAD_URN"}]})
    with pytest.raises(transport.UpstreamError) as caught:
        call(client)
    assert "INVALID_THREAD_URN" in str(caught.value)


# The two places Voyager's GraphQL executor parks an error list, both under a
# 200. The nested one is not hypothetical: it is the placement LinkedIn actually
# used when `surfaces/posts.py` measured a rejected create against a bad
# `visibilityType`, and reading only the top level missed it.
REFUSALS = [
    {"errors": [{"message": "INVALID_THREAD_URN"}]},
    {"data": {"errors": [{"message": "INVALID_THREAD_URN"}]}},
]


@pytest.mark.parametrize("answer", REFUSALS, ids=["top-level", "under-data"])
@pytest.mark.parametrize(
    "call",
    [
        lambda c: social.react(c, ACTIVITY),
        lambda c: social.unreact(c, ACTIVITY),
        lambda c: social.comment(c, ACTIVITY, "nicely put"),
    ],
    ids=["react", "unreact", "comment"],
)
def test_a_graphql_error_under_a_200_is_a_refusal_wherever_it_is_parked(call, answer):
    """A rejected mutation is a refusal, not an unknown outcome - and not a
    success, which is what the nested shape used to be reported as.

    `{"data": {"errors": [...]}}` is a non-empty `data`, so the old check read
    the top level, found nothing, saw a result body and called react and unreact
    done; `comment` fell through to the urn reader and said "may have landed" for
    a comment that was rejected outright. Both send an agent somewhere the write
    never reached. `surfaces/posts.py` measured this placement live and already
    reads both.

    The message has to carry LinkedIn's own words, because the reason a mutation
    was refused is never derivable from the request.
    """
    client = FakeClient(answer)
    with pytest.raises(transport.UpstreamError) as caught:
        call(client)
    message = str(caught.value)
    assert "INVALID_THREAD_URN" in message
    assert "refused" in message.lower()


@pytest.mark.parametrize("answer", REFUSALS, ids=["top-level", "under-data"])
@pytest.mark.parametrize(
    "call",
    [
        lambda c: social.react(c, ACTIVITY),
        lambda c: social.unreact(c, ACTIVITY),
        lambda c: social.comment(c, ACTIVITY, "nicely put"),
    ],
    ids=["react", "unreact", "comment"],
)
def test_a_refusal_does_not_send_the_caller_looking_for_a_write_that_never_landed(call, answer):
    """The distinction `posts.py` draws, held here too. An `errors` array means
    nothing was applied; an unparseable body means the write may have landed and
    the only safe move is to read the post back. Telling an agent "unknown" when
    the answer is "no" costs it a read of something that does not exist, and a
    retry loop that will be refused identically every time - so neither the
    unconfirmed wording nor `retryable` may appear on this path.
    """
    client = FakeClient(answer)
    with pytest.raises(transport.UpstreamError) as caught:
        call(client)
    message = str(caught.value)
    assert "not confirmed" not in message
    assert not isinstance(caught.value, transport.OutcomeUnknown)
    assert caught.value.retryable is False


def test_a_comment_with_no_urn_in_the_response_is_a_failure():
    client = FakeClient({"data": {"$type": "com.linkedin.voyager.dash.social.NormComment"}})
    with pytest.raises(transport.UpstreamError):
        social.comment(client, ACTIVITY, "nicely put")


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"entityUrn": None}},
        {"data": {"urn": ""}},
        {"data": {"*value": 7486857700000000000}},
        # A bare id where a urn belongs. `urn:li:fsd_comment:(<id>,<activity>)`
        # is what every reader of a comment urn expects, and half of one is not
        # something a caller can go look the comment up with.
        {"data": {"*value": "7486857700000000000"}},
        {
            "included": [
                {"$type": "com.linkedin.voyager.dash.social.NormComment", "entityUrn": None}
            ]
        },
    ],
    ids=["null", "empty", "not-a-string", "bare-id", "null-in-included"],
)
def test_a_comment_is_never_reported_posted_with_a_urn_nobody_can_use(answer):
    """The postcondition already holds - this pins it against every shape that
    could slip past it, because it is the one that decides whether `comment`
    can hand back `{"comment_urn": null}` and call that posted. A create is
    reported by its created entity or not at all.
    """
    client = FakeClient(answer)
    with pytest.raises(transport.UpstreamError):
        social.comment(client, ACTIVITY, "nicely put")


def test_an_unconfirmable_comment_says_it_may_still_have_landed():
    """The request was delivered, so a blind retry can post the comment twice.
    The remedy is to read the post back, and the message has to say so."""
    client = FakeClient({"data": {"$type": "com.linkedin.voyager.dash.social.NormComment"}})
    with pytest.raises(transport.UpstreamError) as caught:
        social.comment(client, ACTIVITY, "nicely put")
    message = str(caught.value)
    assert "may" in message
    assert "retry" in message


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"*value": "urn:li:fsd_comment:(1,urn:li:activity:2)"}},
        {"data": {"urn": "urn:li:fsd_comment:(1,urn:li:activity:2)"}},
        {
            "data": {"$type": "com.linkedin.voyager.dash.social.NormComment"},
            "included": [
                {
                    "$type": "com.linkedin.voyager.dash.social.NormComment",
                    "entityUrn": "urn:li:fsd_comment:(1,urn:li:activity:2)",
                }
            ],
        },
    ],
    ids=["star-value", "urn", "included"],
)
def test_the_created_comment_urn_is_found_wherever_the_create_puts_it(answer):
    """Only the *request* was captured, so the reader is liberal about where the
    urn sits - and strict about there being one at all."""
    client = FakeClient(answer)
    out = social.comment(client, ACTIVITY, "nicely put")
    assert out["comment_urn"] == "urn:li:fsd_comment:(1,urn:li:activity:2)"


# -------------------------------------------------------------- comment delete
#
# The route was verified live against this account's own throwaway
# post, and the write-up in docs/write-payloads.md records two false starts that
# cost a probe round each: the doubled urn sent as a path key, and the right key
# sent to the collection the entity only *reads back* from. Both are pinned
# below, because both looked like evidence the route did not exist.


def test_delete_comment_issues_a_delete_rather_than_a_post():
    """The only non-GET in this CLI that is not a POST. A `client.post` here
    reaches a collection endpoint that creates comments, which is the opposite
    of the verb asked for."""
    client = FakeClient({})
    social.delete_comment(client, INNER_COMMENT)
    assert [r[0] for r in client.requests] == ["DELETE"]
    assert client.posts == []


def test_delete_comment_addresses_the_write_collection_not_the_read_one():
    """The asymmetry that cost the second probe round. Create and delete both
    live on `voyagerSocialDashNormComments`; the entity reads back from
    `voyagerSocialDashComments`, and a delete sent there answers 400."""
    client = FakeClient({})
    social.delete_comment(client, INNER_COMMENT)
    path = client.requests[0][1]
    assert path == DELETE_COMMENT_PATH
    assert READ_COLLECTION not in path


def test_delete_comment_percent_encodes_the_key_whole():
    """The key is a RestLi tuple. Left raw, its `(`, `,` and `:` are read as
    path and query structure rather than as the id they belong to."""
    client = FakeClient({})
    social.delete_comment(client, INNER_COMMENT)
    key = client.requests[0][1].split("/", 1)[1]
    assert not set("(),:") & set(key)


def test_delete_comment_strips_the_wrapper_linkedin_answers_a_create_with():
    """`social.comment` reports the doubled urn because that is what LinkedIn
    hands back, so the obvious copy-paste of its own output has to work. Round
    one of the probe sent the doubled urn as the key and got a 400."""
    doubled = FakeClient({})
    social.delete_comment(doubled, DOUBLED_COMMENT)
    inner = FakeClient({})
    social.delete_comment(inner, INNER_COMMENT)
    assert doubled.requests[0][1] == inner.requests[0][1] == DELETE_COMMENT_PATH
    assert "normComment" not in doubled.requests[0][1]


def test_delete_comment_sends_no_body():
    """A DELETE addressed by a path key. A body here is a field LinkedIn was
    never observed being sent and cannot be told apart from a guess."""
    client = FakeClient({})
    social.delete_comment(client, INNER_COMMENT)
    assert client.requests[0][2] is None


def test_an_empty_2xx_is_the_documented_success_and_is_not_refused():
    """`{}` is what `transport.parse` hands back for a 2xx with no body, and that
    is the shape this route answers with. Demanding a urn back - which is right
    for the create, where the entity *is* the postcondition - would report every
    successful delete as unconfirmed."""
    out = social.delete_comment(FakeClient({}), INNER_COMMENT)
    assert out["deleted"] is True


def test_delete_comment_reports_the_comment_it_removed_and_the_post_it_was_on():
    """The comment urn in the form the route takes, whichever form came in, plus
    the activity read out of the urn itself - `render` names the subject of a
    write, and for a comment that is the post."""
    for given in (INNER_COMMENT, DOUBLED_COMMENT):
        out = social.delete_comment(FakeClient({}), given)
        assert out["comment_urn"] == INNER_COMMENT
        assert out["activity_urn"] == ACTIVITY


def test_delete_comment_hands_back_no_permalink():
    """`posts.delete` withholds one for the same reason: a live URL invites an
    agent to cite it as evidence the removal worked, and the comment is gone."""
    assert "url" not in social.delete_comment(FakeClient({}), INNER_COMMENT)


@pytest.mark.parametrize("answer", REFUSALS, ids=["top-level", "under-data"])
def test_a_refusal_parked_in_a_2xx_is_not_a_successful_delete(answer):
    """A 4xx never reaches the surface - `transport.raise_for_status` raises
    first - so the one failure left for this postcondition is the one neither
    layer sees: LinkedIn saying no inside a 200."""
    with pytest.raises(transport.UpstreamError) as caught:
        social.delete_comment(FakeClient(answer), INNER_COMMENT)
    message = str(caught.value)
    assert "INVALID_THREAD_URN" in message
    assert "refused" in message.lower()
    assert caught.value.retryable is False


@pytest.mark.parametrize("answer_key", ["top-level", "under-data"])
def test_a_refused_delete_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """Read out of a 2xx body, so nothing upstream scrubbed it, and `cli._report`
    renders it onto stderr - which under an agent gateway is permanent model context."""
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(transport.UpstreamError) as caught:
        social.delete_comment(FakeClient(answer), INNER_COMMENT)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


def test_delete_comment_previews_the_request_without_issuing_it():
    client = FakeClient({})
    out = social.delete_comment(client, DOUBLED_COMMENT, dry_run=True)
    assert client.requests[0][3] is True
    assert out["method"] == "DELETE"
    assert out["url"] == DELETE_COMMENT_PATH
    assert "li_at" not in json.dumps(out)


def test_delete_comment_goes_through_request_even_past_a_delete_shortcut():
    """`_request` is not merely what the transports happen to expose - it is the
    seam `cli._WriteWatch` wraps.

    A write that reaches the wire by a method the watch does not know about never
    sets `attempted_write`, so `cli._write` hands the ledger slot it claimed
    straight back and the undo goes out uncounted. That is not hypothetical: it
    is what this call did until `_WriteWatch._request` existed, and a convenience
    `delete()` on the transports would put it back the moment one shipped.
    """

    class WithDelete(FakeClient):
        def __init__(self):
            super().__init__({})
            self.deletes: list[tuple] = []

        def delete(self, path, dry_run=False):  # pragma: no cover - must not be called
            self.deletes.append((path, dry_run))
            return {}

    client = WithDelete()
    social.delete_comment(client, INNER_COMMENT)
    assert client.deletes == []
    assert client.requests == [("DELETE", DELETE_COMMENT_PATH, None, False)]


# ------------------------------------------------------------ the comment urn


@pytest.mark.parametrize(
    "bad",
    [
        # The post, not the comment on it. The likeliest mistake by far: it is
        # the urn every other write in this file takes.
        ACTIVITY,
        SHARE,
        "urn:li:ugcPost:7486948402790400000",
        # Half a urn. The activity the comment hangs off is part of the key and
        # is not derivable from the comment id.
        "7487000000000000000",
        "urn:li:fsd_comment:7487000000000000000",
        # The tuple, with the wrong thing in the second slot.
        f"urn:li:fsd_comment:(7487000000000000000,{SHARE})",
        "urn:li:fsd_comment:(,urn:li:activity:7486948402790400001)",
        "urn:li:fsd_comment:(abc,urn:li:activity:7486948402790400001)",
        "urn:li:fsd_comment:(7487000000000000000)",
        # A neighbouring urn type, and the wrapper on its own.
        "urn:li:comment:(7487000000000000000,urn:li:activity:7486948402790400001)",
        "urn:li:fsd_profile:AbC123",
        "urn:li:fsd_normComment:",
        f"urn:li:fsd_normComment:{DOUBLED_COMMENT}",
        "https://www.linkedin.com/feed/update/urn:li:activity:7486948402790400001/",
        "",
        "   ",
        None,
        7487000000000000000,
    ],
    ids=lambda b: str(b)[:40],
)
def test_only_a_comment_urn_is_accepted(bad):
    """Deleting the wrong comment is not recoverable, so every near miss is
    refused rather than coerced into something addressable."""
    with pytest.raises(ValueError):
        social.comment_urn(bad)


@pytest.mark.parametrize("given", [INNER_COMMENT, DOUBLED_COMMENT, f"  {DOUBLED_COMMENT}\n"])
def test_both_spellings_reduce_to_the_key_the_route_takes(given):
    """Padding included: a urn arrives with a trailing newline every time one is
    piped in from another command, and sending the padding is a 400 with the
    whitespace invisible in the error."""
    assert social.comment_urn(given) == INNER_COMMENT


def test_an_activity_urn_is_refused_by_name_and_says_where_a_comment_urn_comes_from():
    """The refusal has to name what is wanted and where one is found, or the
    caller is told what is wrong and not how to fix it."""
    with pytest.raises(ValueError) as caught:
        social.comment_urn(ACTIVITY)
    message = str(caught.value)
    assert "urn:li:fsd_comment:" in message
    assert "comment_urn" in message
    assert "post" in message


def test_a_comment_urn_refusal_says_the_delete_cannot_be_taken_back():
    """The reason the refusals here are stricter than the reads': there is no
    verb in this CLI that puts a deleted comment back."""
    with pytest.raises(ValueError) as caught:
        social.comment_urn("7487000000000000000")
    assert "cannot" in str(caught.value)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_bad_comment_urn_is_refused_before_anything_is_sent(dry_run):
    """Including on a dry run: previewing a request that could never be valid
    tells the operator nothing."""
    client = FakeClient({})
    with pytest.raises(ValueError):
        social.delete_comment(client, ACTIVITY, dry_run=dry_run)
    assert client.requests == []


# ------------------------------------------------------------------ the urn trap


@pytest.mark.parametrize(
    "bad",
    [
        SHARE,
        "https://www.linkedin.com/feed/update/urn:li:share:7486948402790400000/",
        "https://www.linkedin.com/posts/someone_a-post-activity-7486948402790400001-abcd",
        "7486948402790400001",
        "urn:li:ugcPost:7486948402790400000",
        "urn:li:activity:",
        "urn:li:activity:not-a-number",
        "",
        "   ",
        None,
        7486948402790400001,
    ],
    ids=lambda b: str(b)[:34],
)
def test_only_an_activity_urn_is_accepted(bad):
    with pytest.raises(ValueError):
        social.activity_urn(bad)


def test_a_share_urn_is_refused_with_the_reason_spelled_out():
    """A share urn and an activity urn are different ids for the same post and
    neither can be derived from the other, so converting one is guessing."""
    with pytest.raises(ValueError) as caught:
        social.activity_urn(SHARE)
    message = str(caught.value)
    assert "urn:li:activity:" in message
    assert "urn:li:share:" in message
    assert "interchangeable" in message


def test_a_post_url_is_refused_and_says_which_urn_to_pass():
    with pytest.raises(ValueError) as caught:
        social.activity_urn("https://www.linkedin.com/feed/update/urn:li:share:748685762165")
    message = str(caught.value)
    assert "urn:li:activity:" in message
    # The remedy has to name where an activity urn actually comes from, or the
    # caller is told what is wrong and not how to fix it.
    assert "activity_urn" in message


def test_a_bare_id_is_refused_because_it_could_be_either_id():
    """`7486948402790400000` is the share id of the very post whose activity id
    is `7486948402790400001`. A bare number carries no way to tell them apart, so
    accepting one would react to whatever it happened to be."""
    with pytest.raises(ValueError):
        social.activity_urn("7486948402790400000")


def test_surrounding_whitespace_is_not_a_different_urn():
    assert social.activity_urn(f"  {ACTIVITY}\n") == ACTIVITY


@pytest.mark.parametrize(
    "call,key",
    [
        (lambda c, urn: social.react(c, urn), "variables"),
        (lambda c, urn: social.unreact(c, urn), "variables"),
        (lambda c, urn: social.comment(c, urn, "nicely put"), None),
    ],
    ids=["react", "unreact", "comment"],
)
def test_the_normalized_urn_is_what_reaches_the_wire(call, key):
    """Accepting a padded urn and then sending the padding is not tolerance, it
    is a bare 400 with the whitespace invisible in the error. A urn arrives with
    a trailing newline every time one is piped in from another command, so this
    is the common case rather than the odd one.
    """
    client = FakeClient(CREATED_COMMENT)
    call(client, f"  {ACTIVITY}\n")
    body = client.posts[0][1]
    assert (body[key] if key else body)["threadUrn"] == ACTIVITY


@pytest.mark.parametrize(
    "call",
    [
        lambda c, urn: social.react(c, urn),
        lambda c, urn: social.unreact(c, urn),
        lambda c, urn: social.comment(c, urn, "nicely put"),
        lambda c, urn: social.react(c, urn, dry_run=True),
        lambda c, urn: social.comment(c, urn, "nicely put", dry_run=True),
    ],
    ids=["react", "unreact", "comment", "react-dry-run", "comment-dry-run"],
)
def test_a_bad_urn_is_rejected_before_anything_is_sent(call):
    """Including on a dry run: previewing a request that could never be valid
    tells the operator nothing."""
    client = FakeClient()
    with pytest.raises(ValueError):
        call(client, SHARE)
    assert client.posts == []


# ------------------------------------------------------------ the other inputs


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_an_empty_comment_is_rejected_before_any_request(text):
    client = FakeClient(CREATED_COMMENT)
    with pytest.raises(ValueError):
        social.comment(client, ACTIVITY, text)
    assert client.posts == []


def test_a_reaction_type_is_normalized_to_the_wire_spelling():
    """Agents type `like`; LinkedIn's enum is `LIKE`, and a lowercase one is a
    bare 400."""
    client = FakeClient()
    out = social.react(client, ACTIVITY, reaction="like")
    assert client.posts[0][1]["variables"]["entity"] == {"reactionType": "LIKE"}
    assert out["reaction"] == "LIKE"


def test_an_unknown_reaction_type_is_rejected_here_rather_than_by_linkedin():
    client = FakeClient()
    with pytest.raises(ValueError) as caught:
        social.react(client, ACTIVITY, reaction="THUMBS_UP")
    assert "LIKE" in str(caught.value)
    assert client.posts == []


def test_every_accepted_reaction_type_reaches_the_wire():
    for reaction in social.REACTION_TYPES:
        client = FakeClient()
        social.react(client, ACTIVITY, reaction=reaction)
        assert client.posts[0][1]["variables"]["entity"] == {"reactionType": reaction}


# ------------------------------------------------ credentials in LinkedIn's prose

# `transport.raise_for_status` scrubs the body it splices into an error, but a
# refusal that arrives inside a **200** never reaches it: `_confirm` reads the
# `errors` array itself and hands `_error_text` straight to `_refused`. That
# string is whatever LinkedIn wrote, `cli._report` renders this exception's
# `str()` onto stderr, and under an agent gateway stderr is permanent model context - so a
# csrf token quoted back inside a refusal is a live session nobody can retract.


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
@pytest.mark.parametrize(
    "call",
    [
        lambda c: social.react(c, ACTIVITY),
        lambda c: social.unreact(c, ACTIVITY),
        lambda c: social.comment(c, ACTIVITY, "nicely put"),
    ],
    ids=["react", "unreact", "comment"],
)
def test_a_refusal_does_not_put_linkedins_credentials_on_stderr(call, answer_key):
    """The refusal is read out of a 200 body, so nothing upstream of here scrubbed
    it - `render.py` names `error.message` as the key it does not redact, and
    this surface is the layer it says has to.

    Checked at both placements Voyager parks an `errors` list at, because a
    scrubber applied to only one of them is the same bug one level down.
    """
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    client = FakeClient(answer)
    with pytest.raises(transport.UpstreamError) as caught:
        call(client)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


def test_an_unconfirmed_write_scrubs_its_detail_by_construction():
    """Today every `_unconfirmed` detail is a literal written in this repo, so no
    live path leaks through it. It is scrubbed anyway, because there is no layer
    behind this one - `render.py` writes `error.message` out as given - and the
    caller that forgets is the one added later.
    """
    unconfirmed = social._unconfirmed("the comment", credential_detail(), ACTIVITY)
    assert_scrubbed_but_still_diagnostic(str(unconfirmed))


def test_a_scrubbed_refusal_still_names_the_write_it_refused():
    """The scrubber is applied to LinkedIn's detail, not to the whole message.
    `what` is built here out of the caller's own arguments, and it is the only
    thing that says which post and which write were turned down."""
    message = str(social._refused(f"the LIKE reaction on {ACTIVITY}", credential_detail()))
    assert ACTIVITY in message
    assert "LIKE" in message
