"""Sending a connection request: the one write that reaches a named stranger.

The request body is a transcription of traffic captured by driving
the real "Connect" control, pausing the request with CDP `Fetch` at
`requestStage: Request`, recording `request.postData` and then **aborting** it -
the sent-invitations list read 9 before and 9 after, and the target was absent.
So the shape tests below compare the serialized body against the captured bytes
rather than sampling a few keys, the way `test_posts.py` does.

What this file guards harder than the other write surfaces, and why:

* **The response was never captured**, because the capture aborted at the
  request. So a response that proves nothing must never be reported as an
  invitation sent: the operator would find out from the person who never got it,
  and there is no `invite withdraw` in this CLI to take one back with.
* **`verifyQuotaAndCreateV2` means LinkedIn counts invitations itself.** A
  refusal there is a real answer and not a transport hiccup, so it gets its own
  error class rather than arriving as a generic upstream failure that an agent
  reads as "try again".
* **The endpoint's own name contains the word `quota`.** Any refusal-detection
  that scans the error text for it therefore has to strip the action spelling
  first, or every 4xx from this route is misreported as a spent quota. That trap
  is pinned below.
* **There is no note field in the capture.** `--note` is a flag callers reach
  for, and inventing a `message` key for it would be a guessed request body.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from linkedin_cli import cli, transport
from linkedin_cli.surfaces import invitations
from linkedin_cli.transport import UpstreamError
from tools import leakcheck

TARGET = "urn:li:fsd_profile:ACoAAASYNTHETIC0000000000"

DECORATION = "com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-2"

INVITE_PATH = (
    "voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"
    f"&decorationId={DECORATION}"
)

# The captured bytes, compact separators because that is what goes on the wire.
INVITE_BODY = f'{{"invitee":{{"inviteeUnion":{{"memberProfile":"{TARGET}"}}}}}}'

INVITATION = "urn:li:fsd_invitation:7000000000000000000"

# The response shape was *not* captured - only the request was - so this is one
# plausible spelling, and the tests below prove the reader does not depend on it.
CREATED = {"data": {"*value": INVITATION}}

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

    def get(self, path, dry_run=False):  # pragma: no cover - an invite reads nothing
        raise AssertionError(f"sending an invitation read {path!r}")


def wire(client) -> str:
    """The body of the last post, serialized the way the transport sends it."""
    return json.dumps(client.posts[-1][1], separators=(",", ":"))


class CliClient(FakeClient):
    """`FakeClient` with the read side allowed - `cli` builds no other client."""

    def get(self, path, dry_run=False):
        return {}


def run(argv, **kw):
    out, errs = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=errs, **kw)
    return code, out.getvalue(), errs.getvalue()


def envelope(text):
    return json.loads(text)


# ------------------------------------------------------------- the decoration id


def test_the_decoration_id_is_the_one_that_was_captured():
    assert invitations.DECORATION_IDS["invite"] == DECORATION


def test_the_decoration_id_can_be_replaced_from_the_environment(monkeypatch):
    """Decorations are versioned and LinkedIn retires old ones, so the operator
    has to be able to swap one without waiting for a release - the same reason
    every queryId in this package carries an override."""
    monkeypatch.setenv("LINKEDIN_DECORATION_ID_INVITE", "com.linkedin.deco.Rotated-9")
    assert invitations.decoration_id("invite") == "com.linkedin.deco.Rotated-9"


def test_an_unset_override_falls_back_to_the_shipped_id():
    assert invitations.decoration_id("invite") == invitations.DECORATION_IDS["invite"]


def test_the_override_reaches_the_request_path(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DECORATION_ID_INVITE", "com.linkedin.deco.Rotated-9")
    client = FakeClient()
    invitations.invite(client, TARGET)
    assert client.posts[0][0].endswith("decorationId=com.linkedin.deco.Rotated-9")


def test_no_decoration_id_is_written_at_a_call_site():
    """An id inlined at the call site is one no override can replace and nobody
    finds when LinkedIn retires the version."""
    source = Path(invitations.__file__).read_text()
    for name, value in invitations.DECORATION_IDS.items():
        assert source.count(value) == 1, f"the {name} decorationId is written more than once"


# ----------------------------------------------------------------- the payload


def test_invite_posts_to_the_captured_endpoint():
    client = FakeClient()
    invitations.invite(client, TARGET)
    assert client.posts[0][0] == INVITE_PATH


def test_invite_body_reproduces_the_capture_byte_for_byte():
    client = FakeClient()
    invitations.invite(client, TARGET)
    assert wire(client) == INVITE_BODY


def test_the_captured_payload_carries_no_note_and_no_dedupe_token():
    """Two claims the CLI makes about this verb, proved from the payload itself:
    there is nothing for `--note` to map onto, and nothing LinkedIn could collapse
    a duplicate invitation on."""
    client = FakeClient()
    invitations.invite(client, TARGET)
    flat = wire(client).lower()
    for token in ("note", "message", "origintoken", "idempot", "dedupe", "trackingid"):
        assert token not in flat, f"{token} appeared in a payload that was captured without one"


def test_one_invitation_is_exactly_one_request():
    """No retry loop anywhere on this path. A second identical request is a
    second invitation to a real person, and the payload carries no dedupe token
    for LinkedIn to collapse them on."""
    client = FakeClient()
    invitations.invite(client, TARGET)
    assert len(client.posts) == 1


# --------------------------------------------------------------------- the urn


def test_a_profile_urn_is_taken_as_given():
    assert invitations.profile_urn(TARGET) == TARGET


def test_a_profile_urn_is_stripped_before_it_is_checked():
    assert invitations.profile_urn(f"  {TARGET}  ") == TARGET


@pytest.mark.parametrize(
    "value",
    [
        "grace-hopper-1906",
        "https://www.linkedin.com/in/grace-hopper-1906",
        "urn:li:activity:7486948402790400001",
        "urn:li:fs_miniProfile:ACoAAA",
        "urn:li:fsd_profile:",
        "urn:li:fsd_profile:has space",
        "urn:li:msg_conversation:(urn:li:fsd_profile:ABC,2-xyz)",
        "",
        "   ",
        None,
        42,
    ],
)
def test_anything_that_is_not_a_member_urn_is_refused(value):
    """The urn decides *who* gets the invitation, and an invitation cannot be
    taken back by this CLI. A near-miss is refused here rather than sent."""
    with pytest.raises(ValueError):
        invitations.profile_urn(value)


def test_the_refusal_names_the_command_that_produces_the_urn():
    """An agent holding only a public id has to be told where a urn comes from,
    or it guesses one."""
    with pytest.raises(ValueError) as caught:
        invitations.profile_urn("grace-hopper-1906")
    assert "profile get" in str(caught.value)


def test_a_bad_urn_is_refused_before_anything_is_sent():
    client = FakeClient()
    with pytest.raises(ValueError):
        invitations.invite(client, "grace-hopper-1906")
    assert client.posts == []


# ------------------------------------------------------------- postconditions


@pytest.mark.parametrize(
    "answer",
    [
        {},
        None,
        [],
        "",
        {"data": {}},
        {"data": None},
        {"included": []},
        {"data": {"value": {}}},
        {"errors": [{"message": "something went wrong"}]},
        {"data": {"errors": [{"message": "something went wrong"}]}},
        # The invitee is decorated into the response, so the *target's* urn is
        # present in a refusal too. Reading any urn at all as the created
        # invitation would report every refusal as a sent invitation.
        {
            "included": [
                {"$type": "com.linkedin.voyager.dash.identity.profile.Profile", "entityUrn": TARGET}
            ]
        },
    ],
)
def test_a_response_that_proves_nothing_is_never_reported_as_sent(answer):
    """The failure this whole check exists for: an agent does not look again at a
    success, and the operator finds out from the person who never got the
    invitation. There is no withdraw verb here to clean one up with either."""
    client = FakeClient(answer)
    with pytest.raises(UpstreamError) as caught:
        invitations.invite(client, TARGET)
    assert "not confirmed" in str(caught.value)


def test_an_unconfirmed_invitation_is_not_marked_retryable():
    """The request reached LinkedIn, so it may have been delivered, and
    `cli._report` renders `.retryable` straight into the envelope an agent
    branches on. A blind retry sends a second request to a real person."""
    client = FakeClient({})
    with pytest.raises(UpstreamError) as caught:
        invitations.invite(client, TARGET)
    assert getattr(caught.value, "retryable", False) is False


def test_an_unconfirmed_invitation_says_where_to_look():
    client = FakeClient({})
    with pytest.raises(UpstreamError) as caught:
        invitations.invite(client, TARGET)
    message = str(caught.value)
    assert "invitation-manager/sent" in message
    assert "not retry" in message


def test_a_created_invitation_is_reported_with_its_urn():
    client = FakeClient(CREATED)
    result = invitations.invite(client, TARGET)
    assert result["invitation_urn"] == INVITATION
    assert result["profile_urn"] == TARGET
    assert result["invited"] is True


def test_the_created_urn_is_found_in_the_decorated_entity_too():
    """Only the request was captured, so no single path is pinned: a decorated
    RestLi create parks the entity in `included` when `data` holds a reference."""
    client = FakeClient(
        {
            "data": {},
            "included": [
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "entityUrn": TARGET,
                },
                {
                    "$type": "com.linkedin.voyager.dash.relationships.invitation.Invitation",
                    "entityUrn": INVITATION,
                },
            ],
        }
    )
    assert invitations.invite(client, TARGET)["invitation_urn"] == INVITATION


def test_a_public_id_is_carried_into_the_result_when_the_caller_had_one():
    client = FakeClient(CREATED)
    result = invitations.invite(client, TARGET, public_id="grace-hopper-1906")
    assert result["public_id"] == "grace-hopper-1906"
    assert result["url"] == "https://www.linkedin.com/in/grace-hopper-1906"


def test_no_url_is_invented_when_only_the_urn_is_known():
    """A profile permalink is built from the public id, not from the urn. Handing
    back a URL assembled out of a urn would give an agent a dead link to cite as
    evidence the invitation went somewhere."""
    result = invitations.invite(FakeClient(CREATED), TARGET)
    assert result["url"] is None
    assert result["public_id"] is None


# --------------------------------------------------------------- the quota


QUOTA_BODY = {
    "errors": [{"message": "You've reached the weekly invitation quota for this account"}]
}


def test_a_quota_refusal_in_the_body_is_its_own_error():
    """`verifyQuotaAndCreateV2` says LinkedIn counts invitations server-side, so a
    refusal is a real answer about the account rather than a failed request."""
    client = FakeClient(QUOTA_BODY)
    with pytest.raises(invitations.InvitationQuotaExceeded):
        invitations.invite(client, TARGET)


def test_a_quota_refusal_raised_by_the_transport_is_reclassified():
    """A 4xx never reaches the body reader: `transport.raise_for_status` turns it
    into an `UpstreamError` first, which exits 6 - the code an agent retries."""
    client = FakeClient(
        UpstreamError(
            "HTTP 403 from https://www.linkedin.com/voyager/api/x: "
            '{"message":"invitation quota exceeded"}'
        )
    )
    with pytest.raises(invitations.InvitationQuotaExceeded):
        invitations.invite(client, TARGET)


def test_the_endpoint_name_alone_is_not_read_as_a_spent_quota():
    """The trap. The action is spelled `verifyQuotaAndCreateV2`, so it is in the
    URL of every failure from this route - and a naive substring check on the
    error text reports every one of them as a spent invitation quota, which tells
    the operator to wait a week over what is really a rotated decoration."""
    client = FakeClient(
        UpstreamError(
            "HTTP 400 from https://www.linkedin.com/voyager/api/"
            "voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"
            "&decorationId=x: {}"
        )
    )
    with pytest.raises(UpstreamError) as caught:
        invitations.invite(client, TARGET)
    assert not isinstance(caught.value, invitations.InvitationQuotaExceeded)


def test_a_quota_refusal_is_not_marked_retryable():
    """LinkedIn already answered. Retrying a refused invitation is exactly the
    behaviour that turns a spent quota into a restricted account."""
    client = FakeClient(QUOTA_BODY)
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(client, TARGET)
    assert getattr(caught.value, "retryable", False) is False


def test_a_quota_refusal_explains_that_the_limit_is_linkedins_own():
    client = FakeClient(QUOTA_BODY)
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(client, TARGET)
    message = str(caught.value)
    assert "LinkedIn" in message
    assert "not retry" in message


def test_a_quota_refusal_is_still_an_upstream_error():
    """So a caller that only knows the base class still catches it."""
    assert issubclass(invitations.InvitationQuotaExceeded, UpstreamError)


def test_an_already_classified_refusal_is_handed_through_unwrapped():
    """A quota error quoting a quota error is a message nobody can read, and the
    inner one is the one that carries LinkedIn's own words."""
    original = invitations.InvitationQuotaExceeded("weekly invitation quota reached")
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(FakeClient(original), TARGET)
    assert caught.value is original


def test_a_created_invitation_beats_a_quota_shaped_string_in_the_same_answer():
    """The endpoint verifies a quota and then creates, so a *success* can report
    on the quota it just checked. A named invitation urn is a fact about what was
    created; a matched phrase is a guess about what a refusal reads like, and an
    invitation that really went out must not be reported as refused."""
    client = FakeClient(
        {"data": {"*value": INVITATION, "quotaMessage": "3 invitations left in your quota"}}
    )
    assert invitations.invite(client, TARGET)["invitation_urn"] == INVITATION


def test_a_quota_string_still_refuses_when_nothing_was_created():
    """The other half of the same rule: with no created urn there is no fact to
    beat the guess, and a quota-shaped answer is the better explanation than
    `the response named no invitation urn`."""
    client = FakeClient({"data": {"status": "CANNOT_SEND_INVITATION_QUOTA_REACHED"}})
    with pytest.raises(invitations.InvitationQuotaExceeded):
        invitations.invite(client, TARGET)


# ----------------------------------------------------------------- the dry run


def test_a_dry_run_hands_back_the_preview_and_sends_nothing():
    client = FakeClient()
    preview = invitations.invite(client, TARGET, dry_run=True)
    assert preview["method"] == "POST"
    assert client.posts[0][2] is True


def test_a_dry_run_previews_the_body_that_would_really_go_out():
    """The point of a preview is that what the operator approved is what is sent."""
    client = FakeClient()
    invitations.invite(client, TARGET, dry_run=True)
    assert wire(client) == INVITE_BODY
    assert client.posts[0][0] == INVITE_PATH


def test_a_dry_run_is_not_held_to_a_postcondition():
    """The preview is the request, not an answer, so it has no invitation urn in
    it - and refusing it for that would make previewing this write impossible."""
    assert invitations.invite(FakeClient({}), TARGET, dry_run=True) is not None


# ------------------------------------------------------ listing what was received

# The route verified live (docs/sdui-migration.md): a GET, probed
# alongside eight other spellings of which seven answered 404 or 400. It answered
# 200 with `data.elements` and `data.paging`, and `relationships/invitationsSummary`
# independently reported `numPendingInvitations: 0` while the page rendered
# `All (0)` - three reads agreeing, which is what makes this a verified route
# rather than a URL that happened to return 200.
#
# The limitation that shapes every test below: the account had **zero** received
# invitations, so `elements` was only ever observed as `[]`. No populated element
# has been seen. The projection is therefore written to be liberal about
# spellings and to *fail loudly* rather than quietly - the same rule as "a
# zero-capture run is a failure, not a no-op".

EMPTY_RECEIVED = {"data": {"elements": [], "paging": {"start": 0, "count": 10, "total": 0}}}

INVITER = "urn:li:fs_miniProfile:ACoAAASYNTHETICINVITER00"

INVITATION_URN = "urn:li:invitation:7000000000000000002"
VIEW_URN = "urn:li:fs_relInvitationView:7000000000000000002"
REL_URN = "urn:li:fs_relInvitation:7000000000000000002"

# The REAL normalized shape, observed live against the first
# populated received inbox this route was ever pointed at. It is not flat: the
# collection holds *references* in `*elements` and the entities live in
# `included`, three hops deep -
#
#     InvitationView --*invitation--> Invitation --*fromMember--> MiniProfile
#
# The previous version of this file invented a flat `{"invitation": {...,
# "fromMember": {...}}}` element and asserted the projection read it correctly.
# It did. LinkedIn does not send that, so the suite was green while `invitations
# list` failed against the live account on the first invitation it ever saw.
# Everything here is the observed structure with invented identities.


def view(urn=VIEW_URN, rel=REL_URN):
    return {
        "$type": "com.linkedin.voyager.relationships.invitation.InvitationView",
        "entityUrn": urn,
        "*invitation": rel,
        "insights": [],
    }


def invitation(rel=REL_URN, sender=INVITER, **over):
    body = {
        "$type": "com.linkedin.voyager.relationships.invitation.Invitation",
        "entityUrn": rel,
        "mailboxItemId": INVITATION_URN,
        "invitationType": "PENDING",
        "message": "we met at the conference",
        "sentTime": 1783583409545,
        "unseen": True,
        "sharedSecret": "aBcDeFgH",
        "*fromMember": sender,
    }
    body.update(over)
    return body


def mini(urn=INVITER):
    return {
        "$type": "com.linkedin.voyager.identity.shared.MiniProfile",
        "entityUrn": urn,
        "dashEntityUrn": urn.replace("fs_miniProfile", "fsd_profile"),
        "firstName": "Syn",
        "lastName": "Inviter",
        "occupation": "Head of Synthetic Widgets",
        "publicIdentifier": "synthetic-inviter",
    }


def received_page(*, paging=None, refs=(VIEW_URN,), included=None, outside_data=False):
    """One normalized page, in the shape LinkedIn actually answers with."""
    body = {"*elements": list(refs)}
    if paging is not None:
        body["paging"] = paging
    entries = [view(), invitation(), mini()] if included is None else included
    return (
        {"*elements": list(refs), "included": entries}
        if outside_data
        else {
            "data": body,
            "included": entries,
        }
    )


class ReadClient:
    """Replays one canned answer to a GET. Refuses to be written through."""

    def __init__(self, payload=None):
        self.payload = EMPTY_RECEIVED if payload is None else payload
        self.paths: list[str] = []

    def get(self, path, dry_run=False):
        self.paths.append(path)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload

    def post(self, path, body, dry_run=False):  # pragma: no cover - a read writes nothing
        raise AssertionError(f"listing invitations posted to {path!r}")


def test_the_received_route_is_the_one_that_was_verified_live():
    """Byte for byte, including the parameter order the probe used. A finder is
    not guessable: seven of the sent-side spellings tried answered 404
    or 400, and the two that answered are the only two known to exist."""
    client = ReadClient()
    invitations.list_received(client, count=10)
    assert client.paths == ["relationships/invitationViews?count=10&q=receivedInvitation&start=0"]


def test_the_cursor_is_the_offset_this_finder_pages_by():
    client = ReadClient()
    invitations.list_received(client, count=5, cursor="20")
    assert client.paths == ["relationships/invitationViews?count=5&q=receivedInvitation&start=20"]


def test_an_empty_inbox_is_the_one_answer_that_was_actually_observed():
    """`elements: []` is the *only* shape this route has ever been seen to
    return, so it has to be the one answer that is certainly right."""
    items, cursor, more = invitations.list_received(ReadClient())
    assert items == []
    assert cursor is None
    assert more is False


def test_a_received_invitation_is_projected_with_who_sent_it():
    """Against the observed shape, resolving all three reference hops."""
    payload = received_page(paging={"start": 0, "count": 10, "total": 1})
    (item,), _, _ = invitations.list_received(ReadClient(payload))
    assert item["invitation_urn"] == INVITATION_URN
    assert item["from"]["name"] == "Syn Inviter"
    assert item["from"]["headline"] == "Head of Synthetic Widgets"
    assert item["from"]["public_id"] == "synthetic-inviter"
    assert item["message"] == "we met at the conference"
    assert item["type"] == "PENDING"
    assert item["sent_at"] == "2026-07-09T07:50:09Z"
    assert item["unseen"] is True
    assert item["shared_secret"] == "aBcDeFgH"


def test_the_sender_urn_is_the_dash_form_that_addresses_something():
    """`fs_miniProfile` addresses nothing. `invite` refuses it by name and
    `profile get` cannot take it, so handing it back would give an agent an
    identifier that looks actionable and is not."""
    (item,), _, _ = invitations.list_received(ReadClient(received_page()))
    assert item["from"]["profile_urn"].startswith("urn:li:fsd_profile:")


def test_an_invitation_whose_entity_is_missing_is_refused_not_dropped():
    """`Graph.deref` drops danglers so a feed can skip an unreadable post. Here
    the count IS the answer, so a silently shorter list is a wrong answer that
    looks right."""
    payload = received_page(refs=(VIEW_URN, "urn:li:fs_relInvitationView:7000000000000000009"))
    with pytest.raises(transport.UpstreamError) as caught:
        invitations.list_received(ReadClient(payload))
    assert "undercounting" in str(caught.value)


def test_a_bare_element_is_read_as_the_invitation_itself():
    """Liberal about the wrapper for the reason the whole projection is liberal:
    the populated shape has never been seen, and a finder that answered with the
    invitation un-nested would otherwise be reported as an inbox with nothing in
    it."""
    payload = {"data": {"elements": [invitation()]}, "included": [mini()]}
    (item,), _, _ = invitations.list_received(ReadClient(payload))
    assert item["invitation_urn"] == INVITATION_URN


def test_an_invitation_carrying_only_a_urn_still_projects():
    """The one field that has to be there. Everything else is decoration that a
    card without a note, or from a member with no headline, will simply lack."""
    payload = {"data": {"elements": [{"entityUrn": REL_URN}]}}
    (item,), _, _ = invitations.list_received(ReadClient(payload))
    # `entityUrn` is the fallback when there is no `mailboxItemId` to prefer.
    assert item["invitation_urn"] == REL_URN
    assert item["from"] == {}
    assert item["message"] is None
    assert item["shared_secret"] is None


def test_elements_that_project_to_nothing_are_an_error_and_not_an_empty_inbox():
    """The rule this whole surface is written around.

    The populated element shape was never observed, so the realistic failure is
    that the parser is wrong. A wrong parser that returns `[]` is indistinguishable
    from an operator with no pending invitations - and the second one is what an
    agent will conclude. So a non-empty `elements` that projects to nothing
    raises, which is the same rule as `a zero-capture run is a failure, not a
    no-op`: the report an agent gets has to be able to say the tool is broken.
    """
    payload = {"data": {"elements": [{"somethingElse": {"id": 1}}, {"nope": 2}]}}
    with pytest.raises(UpstreamError) as caught:
        invitations.list_received(ReadClient(payload))
    assert "2" in str(caught.value), "the refusal does not say how many were dropped"
    assert "sdui-migration" in str(caught.value), "the refusal does not carry its evidence"


def test_one_unreadable_element_among_readable_ones_is_still_an_error():
    """Not "most of them parsed". A page reporting three invitations out of ten
    is the same failure in miniature, and it is the version nobody notices."""
    payload = {"data": {"elements": [invitation(), {"nope": 1}]}, "included": [mini()]}
    with pytest.raises(UpstreamError):
        invitations.list_received(ReadClient(payload))


def test_the_failure_to_parse_is_not_reported_as_retryable():
    """The answer will be identical next time; what has to change is this parser."""
    payload = {"data": {"elements": [{"nope": 1}]}}
    with pytest.raises(UpstreamError) as caught:
        invitations.list_received(ReadClient(payload))
    assert caught.value.retryable is False


def test_elements_arriving_outside_the_data_wrapper_are_still_read():
    payload = received_page(outside_data=True)
    (item,), _, _ = invitations.list_received(ReadClient(payload))
    assert item["invitation_urn"] == INVITATION_URN


def test_a_response_carrying_no_element_list_at_all_is_an_error():
    """Distinct from an empty list, and the distinction is the whole point: a
    route that stopped answering the way it was observed to answer must not
    report itself as an empty inbox."""
    with pytest.raises(UpstreamError):
        invitations.list_received(ReadClient({"data": {"paging": {"total": 0}}}))


def test_the_next_page_is_offered_when_paging_says_there_is_one():
    payload = {
        **received_page(paging={"start": 0, "count": 1, "total": 4}),
    }
    _, cursor, more = invitations.list_received(ReadClient(payload), count=1)
    assert (cursor, more) == ("1", True)


def test_the_last_page_offers_no_cursor():
    payload = received_page(paging={"start": 3, "count": 1, "total": 4})
    _, cursor, more = invitations.list_received(ReadClient(payload), count=1)
    assert (cursor, more) == (None, False)


def test_a_full_page_with_no_total_is_assumed_to_continue():
    """`paging.total` was observed as 0 against an empty inbox and never against
    a populated one. Without it, a page that came back full is the only evidence
    left that there is more - and stopping early loses invitations silently,
    while one extra request returns an empty page and costs one call."""
    payload = received_page(paging={"start": 0, "count": 1})
    _, cursor, more = invitations.list_received(ReadClient(payload), count=1)
    assert (cursor, more) == ("1", True)


def test_listing_invitations_never_writes():
    invitations.list_received(ReadClient())


# ------------------------------------------------ the two verbs that cannot ship


def test_the_missing_withdraw_no_longer_blames_an_uncaptured_payload():
    """It was described as a payload nobody had captured yet, which reads as one
    more careful capture run away - and that is what the last session ended
    pointed at. It is not: the invitation surface has migrated to LinkedIn's
    server-driven UI, pressing Withdraw posts a `NavigateToScreen` action to
    /flagship-web/ and receives an RSC stream, and this CLI's transport speaks
    Voyager JSON. See docs/sdui-migration.md.
    """
    message = invitations.NO_WITHDRAW
    assert "never captured" not in message
    assert "docs/sdui-migration.md" in message
    assert "server-driven" in message.lower() or "sdui" in message.lower()
    assert invitations.SENT_INVITATIONS_URL in message, "the browser is the remedy; name it"


def test_the_missing_sent_list_says_no_route_survives_rather_than_none_was_seen():
    """Seven spellings were probed: three 404, four 400. There is
    nothing left to capture, and a message implying otherwise sends the reader
    on a capture run that cannot succeed - which is how the last two of those
    cost a stranger's invitation each."""
    message = invitations.NO_SENT_LIST
    assert "docs/sdui-migration.md" in message
    assert "never captured" not in message
    assert invitations.SENT_INVITATIONS_URL in message


def test_the_urn_refusals_no_longer_promise_a_capture_would_bring_the_undo_back():
    """`_NO_UNDO` is spliced into every refusal `profile_urn` raises, so it is
    the most-read sentence on this surface."""
    with pytest.raises(ValueError) as caught:
        invitations.profile_urn("urn:li:activity:1")
    assert "never been captured" not in str(caught.value)
    assert "docs/sdui-migration.md" in str(caught.value)


# ------------------------------------------------ credentials in LinkedIn's prose

# `transport.raise_for_status` scrubs the body it splices into an error, but a
# refusal that arrives inside a **200** never reaches it: `_confirm_sent` reads
# the body itself and hands what it finds to `_quota` and `_unconfirmed`.
# `cli._report` renders those exceptions' `str()` into `error.message`, and under
# an agent gateway stderr is permanent model context - so a csrf token quoted back inside a
# refusal is a live session nobody can retract. render.py's docstring claims
# nothing reaching an envelope is unredacted "by construction rather than by the
# caller remembering"; this surface is where that stopped being true.
#
# `_quota_sign_in` makes it worse than the `errors`-array case. It walks up to
# six levels of an arbitrary 200 body and returns the *first string it likes the
# look of*, with no `errors` wrapper required - so the text spliced into the
# refusal is not even a field this CLI chose to read.


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


def quota_credential_detail() -> str:
    """The same, worded so `_is_quota_refusal` classifies it as a spent quota.

    Both branches of `_confirm_sent` have to be reachable with a credential in
    them, and they take different constructors: the quota one is the branch an
    operator sees most, because a quota refusal is the refusal this endpoint
    actually produces.
    """
    return f"weekly invitation quota reached. {credential_detail()}"


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
def test_a_quota_refusal_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """Checked at both placements a Voyager answer parks an `errors` list at,
    because a scrubber applied to only one of them is the same bug one level
    down - and `_errors` reading both is why they are both reachable."""
    errors = [{"message": quota_credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(FakeClient(answer), TARGET)
    assert_scrubbed_but_still_diagnostic(str(caught.value))
    assert "quota" in str(caught.value)


@pytest.mark.parametrize("answer_key", ["top-level", "under-data"])
def test_an_unrecognised_refusal_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """The other branch of the same `if`. An unrecognised refusal falls through to
    `_unconfirmed` by design - fail-safe on the classification - so it is the
    branch every *unknown* future error text lands in, credentials included."""
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(UpstreamError) as caught:
        invitations.invite(FakeClient(answer), TARGET)
    assert not isinstance(caught.value, invitations.InvitationQuotaExceeded)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


def test_a_harvested_quota_string_is_scrubbed_before_it_is_reported():
    """The worst of the three, because nothing about this string was chosen.

    With no created urn and no `errors` wrapper, `_quota_sign_in` walks up to six
    levels of the 200 body and returns the first string that reads like a quota
    refusal - so whatever LinkedIn happens to have parked in a field this CLI
    never named goes into the message verbatim.
    """
    answer = {"data": {"result": {"detail": {"status": quota_credential_detail()}}}}
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(FakeClient(answer), TARGET)
    assert_scrubbed_but_still_diagnostic(str(caught.value))


def test_a_scrubbed_refusal_still_names_who_the_invitation_was_for():
    """The trap in the fix. `target` is an `urn:li:fsd_profile:ACoAA…` member urn,
    which `scrub_secrets` redacts and `tools/leakcheck.py` flags - and it is the
    only thing in either message naming the person the write was aimed at. A
    blanket scrub of the whole message would stop the leak by making the tool
    unable to say who it just failed to invite, which is a different way of
    lying about what it did. So the scrubber goes on `detail` alone.
    """
    for message in (
        str(invitations._quota(TARGET, quota_credential_detail())),
        str(invitations._unconfirmed(TARGET, credential_detail())),
    ):
        assert TARGET in message
        assert transport.scrub_secrets(TARGET) != TARGET, "the trap is real, not hypothetical"


def test_an_unconfirmed_invitation_scrubs_its_detail_by_construction():
    """Today the literals reaching `_unconfirmed` from `_confirm_sent` directly are
    written in this repo. It is scrubbed anyway, because the property render.py
    advertises is "by construction rather than by the caller remembering", and
    the caller who forgets is the one added later."""
    assert_scrubbed_but_still_diagnostic(str(invitations._unconfirmed(TARGET, credential_detail())))


def test_a_transport_scrubbed_quota_refusal_is_not_mangled_by_a_second_pass():
    """`invite`'s 4xx branch hands `str(exc)` to `_quota`, and that text was
    already scrubbed by `transport.raise_for_status` before it was raised. Adding
    the scrubber inside the constructor must leave that path readable rather than
    redacting the redaction - so the quota wording and the HTTP status both have
    to survive the round trip."""
    client = FakeClient(
        UpstreamError(
            "HTTP 403 from https://www.linkedin.com/voyager/api/x: "
            '{"message":"invitation quota exceeded","csrf_token":"<redacted>"}'
        )
    )
    with pytest.raises(invitations.InvitationQuotaExceeded) as caught:
        invitations.invite(client, TARGET)
    message = str(caught.value)
    assert "HTTP 403" in message
    assert "invitation quota exceeded" in message
    assert TARGET in message
    assert message.count(transport.REDACTED) == 1


def test_a_refused_invitation_reaches_stderr_scrubbed_through_the_cli():
    """End to end, because the leak is only a leak once `cli._report` has written
    it: `str(exc)` becomes `error.message`, and under an agent gateway that stream is
    permanent model context. `--raw` is off on purpose - `render.scrub_body`
    already covers `error.body`, and the message is the field that did not."""
    errors = [{"message": quota_credential_detail()}]
    code, out, err = run(["invite", TARGET], client=CliClient({"data": {"errors": errors}}))
    assert code == 5, err
    assert envelope(err)["error"]["code"] == "invite_quota_exceeded"
    assert out == ""
    reported = envelope(err)["error"]["message"]
    assert_scrubbed_but_still_diagnostic(reported)
    assert_scrubbed_but_still_diagnostic(err)
    assert TARGET in reported, "the person the invitation was for is still named"


# ---------------------------------------------- the shape, as it was recorded


def test_the_recorded_shape_from_a_real_inbox_still_projects():
    """`tests/fixtures/invitations_received.json` is the answer LinkedIn gave.

    Structure verbatim from a live read, identities replaced. The
    inline builders above are convenient and are also written by the same person
    who wrote the parser, which is how this surface shipped a projection that
    could not read a single real invitation: the route had only ever been seen
    against an empty inbox, and every test agreed with the guess.

    So this one is pinned to a file that was not invented - three reference hops,
    `*elements` rather than `elements`, `mailboxItemId` rather than `entityUrn` -
    and it fails if the parser drifts back toward the shape it assumed.
    """
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "invitations_received.json").read_text()
    )
    (item,), cursor, more = invitations.list_received(ReadClient(payload), count=10)

    assert item["invitation_urn"].startswith("urn:li:invitation:")
    assert item["type"] == "PENDING"
    assert item["from"]["name"] == "Marguerite Okonkwo-Bell"
    assert item["from"]["headline"] == "Hydrologist, river restoration"
    assert item["from"]["profile_urn"].startswith("urn:li:fsd_profile:")
    assert item["sent_at"].endswith("Z")
    assert item["shared_secret"] == "aBcDeFgH"
    # `paging.total` is 0 while `*elements` holds one row - LinkedIn really does
    # answer that - so the cursor must come from what was returned, not the total.
    assert (cursor, more) == (None, False)
