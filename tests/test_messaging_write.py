"""Sending and replying.

The payload here is transcribed from a request captured off the live web client
on a live run and then reproduced over plain HTTP. Three details are pinned hard
because getting any of them wrong returns a bare 400 with no explanation, which
is what made this expensive to work out in the first place.
"""

from __future__ import annotations

import json

import pytest

from linkedin_cli.surfaces import messaging

MAILBOX = "urn:li:fsd_profile:ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH"
CONV = (
    "urn:li:msg_conversation:(urn:li:fsd_profile:ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH,2-abc==)"
)


class FakeClient:
    def __init__(self, result=None):
        self.posts = []
        self.result = result or {"data": {"*value": "urn:li:msg_message:(x,2-y)"}}

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


def test_origin_token_is_a_uuid_and_unique_per_call():
    import uuid

    c = FakeClient()
    messaging.send_message(c, MAILBOX, CONV, "one")
    messaging.send_message(c, MAILBOX, CONV, "two")
    tokens = [b["message"]["originToken"] for _, b, _ in c.posts]
    for token in tokens:
        uuid.UUID(token)
    assert tokens[0] != tokens[1]


def test_idempotency_key_overrides_the_origin_token():
    """LinkedIn dedupes on originToken, so that is what --idempotency-key maps to."""
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
