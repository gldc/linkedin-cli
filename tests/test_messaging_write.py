"""Sending and replying.

The payload here is transcribed from a request captured off the live web client
on a live run and then reproduced over plain HTTP. Three details are pinned hard
because getting any of them wrong returns a bare 400 with no explanation, which
is what made this expensive to work out in the first place.
"""

from __future__ import annotations

import json
import uuid

import pytest

from linkedin_cli import restli, transport
from linkedin_cli.surfaces import messaging

MAILBOX = "urn:li:fsd_profile:ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH"
CONV = (
    "urn:li:msg_conversation:(urn:li:fsd_profile:ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH,2-abc==)"
)

# A conversation in somebody else's inbox. The mailbox component is the first
# element of the urn's tuple, so these two differ in exactly the thing the
# membership check reads.
STRANGER = "urn:li:fsd_profile:ACoAASYNTHETICSTRANGER"
FOREIGN = f"urn:li:msg_conversation:({STRANGER},2-def==)"


def thread_page(conversation: str) -> dict:
    """One page of a thread, shaped like the live capture: the messages
    come back under a sync-token root and the entities live in `included`.

    The conversation is named twice because the capture names it twice - once as
    the `*conversation` reference on the message, once as a bare `entityUrn`
    stub of its own - and both spellings are read.
    """
    return {
        "data": {"data": {"messengerMessagesBySyncToken": {"*elements": ["urn:li:msg_message:x"]}}},
        "included": [
            {
                "$type": "com.linkedin.messenger.Message",
                "entityUrn": "urn:li:msg_message:x",
                "*conversation": conversation,
                "body": {"text": "the message a reply would be answering"},
                "deliveredAt": 1783583409545,
            },
            {"$type": "com.linkedin.messenger.Conversation", "entityUrn": conversation},
        ],
    }


THREAD_PAGE = thread_page(CONV)


class FakeClient:
    def __init__(self, result=None, thread=None):
        self.posts = []
        self.gets: list[str] = []
        self.result = result or {"data": {"*value": "urn:li:msg_message:(x,2-y)"}}
        # What the membership read gets back. Defaults to a thread with a message
        # in it, because that is what every send test here is about.
        self.thread = THREAD_PAGE if thread is None else thread

    def get(self, path, dry_run=False):
        self.gets.append(path)
        return self.thread

    def post(self, path, body, dry_run=False):
        self.posts.append((path, body, dry_run))
        if dry_run:
            return {"method": "POST", "url": path, "body": body}
        return self.result


def test_send_hits_the_create_message_action():
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hello")
    path, _, _ = c.posts[0]
    assert path == "voyagerMessagingDashMessengerMessages?action=createMessage"


def test_payload_matches_the_captured_shape():
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hello there")
    _, body, _ = c.posts[0]
    assert body["mailboxUrn"] == MAILBOX
    assert body["dedupeByClientGeneratedToken"] is False
    msg = body["message"]
    assert msg["body"] == {"attributes": [], "text": "hello there"}
    assert msg["renderContentUnions"] == []
    assert msg["conversationUrn"] == CONV


def test_tracking_id_is_sixteen_raw_bytes_not_base64():
    """Base64 was rejected with a bare 400; the wire format is raw bytes."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hi")
    _, body, _ = c.posts[0]
    tracking = body["trackingId"]
    assert isinstance(tracking, str)
    assert len(tracking.encode("latin-1")) == 16


def test_origin_token_is_a_uuid_and_derived_from_the_call():
    """Renamed and rewritten rather than deleted. It used to be
    `..._and_unique_per_call`, and justified the uniqueness by the token being a
    fresh uuid4 on every call. The assertion below still holds - these are two
    *different* texts, so they derive two different tokens - but the reason has
    inverted: the token is now a function of `(mailbox, conversation, text)`, so
    what makes these two differ is the text and nothing else. The uuid4 *shape*
    is still pinned, because that is what the capture carries."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "one")
    messaging.send_message(c, MAILBOX, CONV, "two")
    tokens = [b["message"]["originToken"] for _, b, _ in c.posts]
    for token in tokens:
        uuid.UUID(token)
    assert tokens[0] != tokens[1]


def test_the_origin_token_is_derived_from_the_conversation_and_text():
    """Two identical sends carry one token, because under an agent gateway a
    retry is a fresh process with no memory of the first attempt and the broker
    will not pass `--idempotency-key`. A retry is therefore only recognisable as
    one if its identity is derived from the arguments; a fresh uuid4 per call -
    what this used to be - can never be."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "the same words twice")
    messaging.send_message(c, MAILBOX, CONV, "the same words twice")
    tokens = [b["message"]["originToken"] for _, b, _ in c.posts]
    assert tokens[0] == tokens[1]


def test_the_token_changes_when_the_text_changes():
    """The negative half of the test above, and it is not optional: a hardcoded
    constant satisfies "two identical sends carry one token" perfectly."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "one")
    messaging.send_message(c, MAILBOX, CONV, "one ")
    tokens = [b["message"]["originToken"] for _, b, _ in c.posts]
    assert tokens[0] != tokens[1]


def test_the_token_changes_when_the_conversation_changes():
    """Same argument, on the other component of the identity. The mailbox is in
    the digest too, but nothing in this CLI sends from two mailboxes in one
    process, so the thread is the half a test can move."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "one")
    messaging.send_message(c, MAILBOX, FOREIGN, "one")
    tokens = [b["message"]["originToken"] for _, b, _ in c.posts]
    assert tokens[0] != tokens[1]


def test_the_derived_token_is_still_uuid4_shaped():
    """A digest is not a uuid4, and the capture carries one. `uuid.UUID(bytes=…,
    version=4)` forces the version and variant bits, and dropping that argument
    is the way this quietly stops being true."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hi")
    token = c.posts[0][1]["message"]["originToken"]
    assert uuid.UUID(token).version == 4


def test_a_retry_of_the_same_reply_carries_a_byte_identical_message_body():
    """`message`, not the whole body: `trackingId` is sixteen random bytes and
    sits *outside* the message object, so it does not perturb the retried
    message's identity - and a whole-body comparison would fail forever. The
    second assertion is what pins that distinction."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hello there")
    messaging.send_message(c, MAILBOX, CONV, "hello there")
    assert c.posts[0][1]["message"] == c.posts[1][1]["message"]
    assert c.posts[0][1]["trackingId"] != c.posts[1][1]["trackingId"]


def test_idempotency_key_overrides_the_origin_token():
    """The key sets `originToken` in place of the derived one. That is what it
    is for, and the docstring here used to say "LinkedIn dedupes on originToken"
    - which was false when it was written and is still false: the captured body
    switches the server-side dedupe off. What the key buys is an override of the
    derivation, which is the only way to put the same text into the same thread
    twice on purpose."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hi", idempotency_key="fixed-key-123")
    _, body, _ = c.posts[0]
    assert body["message"]["originToken"] == "fixed-key-123"


def test_send_returns_the_created_message_urn():
    c = FakeClient({"data": {"*value": "urn:li:msg_message:(a,2-b)"}})
    out = messaging.send_message(c, MAILBOX, CONV, "hi")
    assert out["message_urn"] == "urn:li:msg_message:(a,2-b)"
    assert out["conversation_urn"] == CONV


def test_dry_run_does_not_send():
    c = FakeClient()
    out = messaging.send_message(c, MAILBOX, CONV, "hi", dry_run=True)
    assert c.posts[0][2] is True
    assert "li_at" not in json.dumps(out)


def test_empty_text_is_rejected_before_any_request():
    c = FakeClient()
    with pytest.raises(ValueError):
        messaging.send_message(c, MAILBOX, CONV, "   ")
    assert c.posts == []


# ------------------------------------------------ the thread a reply is aimed at


def test_confirming_a_reply_target_reads_the_thread_it_names():
    c = FakeClient()
    messaging.confirm_reply_target(c, CONV, MAILBOX)
    assert len(c.gets) == 1, "the membership check is one round trip, not several"
    assert restli.encode(CONV) in c.gets[0]
    assert c.posts == [], "confirming a target must not send anything"


def test_a_thread_that_comes_back_empty_is_refused_as_not_found():
    """Exit 4. The urn is the caller's argument, and an argument this account
    cannot read a message out of is the closest thing to "not yours" that the
    messages response supports."""
    c = FakeClient(thread={})
    with pytest.raises(transport.NotFound) as caught:
        messaging.confirm_reply_target(c, CONV, MAILBOX)
    assert transport.NotFound.exit_code == 4
    assert CONV in str(caught.value)


def test_the_refusal_names_what_it_cannot_tell_apart_instead_of_guessing():
    """Somebody else's mailbox, a thread this session cannot see and a real
    thread whose messages were all deleted arrive spelled identically. Naming one
    of the three would be a diagnosis the response does not support - the same
    rule `feed._unreadable` is written to."""
    c = FakeClient(thread={})
    with pytest.raises(transport.NotFound) as caught:
        messaging.confirm_reply_target(c, CONV, MAILBOX)
    message = str(caught.value)
    assert "mailbox" in message and "deleted" in message
    # It must not claim to have established membership, because it has not.
    assert "participant" not in message


# --------------------------------------------------- whose mailbox that thread is in


def test_a_thread_in_another_members_mailbox_is_refused():
    """A `msg_conversation` urn is `(<mailbox owner>,2-<thread>)`, and
    `send_message` drops it into the createMessage body untouched - so the
    mailbox component decides whose inbox a reply lands in. Until this, the only
    thing pinning it was the broker's value regex: a control in another repo that
    reaches calls which came through the broker and nothing else."""
    c = FakeClient(thread=thread_page(FOREIGN))
    with pytest.raises(transport.NotFound) as caught:
        messaging.confirm_reply_target(c, FOREIGN, MAILBOX)
    assert transport.NotFound.exit_code == 4
    assert "mailbox" in str(caught.value)
    assert c.posts == [], "nothing may be sent on the way to refusing"


def test_the_mailbox_check_costs_no_second_round_trip():
    """Neither half adds a request. The argument half is a string comparison and
    refuses before anything is sent; the answer half is read out of the page the
    thread read already paid for, which is why it is worth having on a verb that
    was already paying for one.

    `CONV` here, not `FOREIGN`: a foreign argument never reaches the read, so it
    would measure the wrong half. This drives the answer-side check, which is the
    one that could have cost a second trip."""
    c = FakeClient(thread=thread_page(FOREIGN))
    with pytest.raises(transport.NotFound):
        messaging.confirm_reply_target(c, CONV, MAILBOX)
    assert len(c.gets) == 1


def test_the_answer_is_checked_even_when_the_argument_looks_right():
    """LinkedIn resolves the urn, and it need not resolve to what was asked for -
    an unrecognised thread could fall back to a default page. Checking only the
    caller's spelling would check the one string the caller controls."""
    c = FakeClient(thread=thread_page(FOREIGN))
    with pytest.raises(transport.NotFound):
        messaging.confirm_reply_target(c, CONV, MAILBOX)


def test_the_argument_is_checked_even_when_the_answer_looks_right():
    """The divergence the answer-side check alone cannot see, and the one that
    matters: `confirm_reply_target` validates the urn LinkedIn *answered* with,
    while `send_message` writes the urn the caller *passed*. Nothing compared
    them, so an answer naming this mailbox cleared a foreign argument straight
    into the createMessage body.

    Both halves are needed and they fail differently. This one is free - it is a
    string comparison, it runs before the round trip, and it cannot be talked out
    of by anything upstream says."""
    c = FakeClient(thread=thread_page(CONV))
    with pytest.raises(transport.NotFound) as caught:
        messaging.confirm_reply_target(c, FOREIGN, MAILBOX)
    assert transport.NotFound.exit_code == 4
    assert "mailbox" in str(caught.value)
    assert c.posts == [], "nothing may be sent on the way to refusing"
    assert c.gets == [], "and the refusal must not cost a request it does not need"


def test_an_answer_naming_no_conversation_at_all_is_refused():
    """A page of messages with no conversation urn anywhere establishes read
    access and nothing whatever about whose mailbox served it. Refused rather
    than waved through: a check that silently skips itself the day the payload
    shape drifts is a check that reports a guarantee it stopped providing."""
    c = FakeClient(
        thread={
            "data": {
                "data": {"messengerMessagesBySyncToken": {"*elements": ["urn:li:msg_message:x"]}}
            },
            "included": [
                {
                    "$type": "com.linkedin.messenger.Message",
                    "entityUrn": "urn:li:msg_message:x",
                    "body": {"text": "hello"},
                    "deliveredAt": 1783583409545,
                }
            ],
        }
    )
    with pytest.raises(transport.NotFound) as caught:
        messaging.confirm_reply_target(c, CONV, MAILBOX)
    assert "mailbox" in str(caught.value)


def test_send_message_still_performs_no_membership_check_of_its_own():
    """Pinned so nobody reads the guarantee as living here. The createMessage
    body carries the urn with no lookup, whoever supplied it; the check is one
    call up, in the single `cli._send_into` that both `reply` and `send` reach
    it through. This documents that it is absent here rather than untested -
    which is exactly what the surface tests above would otherwise imply."""
    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "hi")
    assert c.gets == []


def test_mark_all_read_uses_the_badge_action():
    c = FakeClient({})
    messaging.mark_all_read(c, until_ms=1784992734986)
    path, body, _ = c.posts[0]
    assert path == "voyagerMessagingDashMessagingBadge?action=markAllMessagesAsSeen"
    assert body == {"until": 1784992734986}


def test_mark_all_read_defaults_to_now(monkeypatch):
    monkeypatch.setattr(messaging.time, "time", lambda: 1700.5)
    c = FakeClient({})
    messaging.mark_all_read(c)
    assert c.posts[0][1]["until"] == 1700500


def test_there_is_no_per_conversation_mark_read_to_call_by_mistake():
    """The captured payload takes a timestamp and no conversation. A function
    named for a single thread would be a promise this surface cannot keep, and
    the CLI verb above it was named after that promise for a while."""
    assert not hasattr(messaging, "mark_read")
