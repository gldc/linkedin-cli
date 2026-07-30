"""Messaging read surface, exercised against fixtures captured from the live API.

Fixtures are loaded from `tests/fixtures/` and fall back to `tests/fixtures/raw/`.

**Nothing person-specific is written down in this file.** The raw capture is a
real person's inbox - real names, real private messages, a real member id - and
this repo is public, so a test that pinned any of those values would be a leak
that `.gitignore` cannot catch. The mailbox urn and the thread urn are read out
of `context.json` at runtime, and the exact-projection tests re-derive their
expectations from the fixture by a *second, independent path* (raw dict
navigation rather than the projection under test), which keeps their teeth
without transcribing a single real value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin_cli import transport
from linkedin_cli.surfaces import messaging
from linkedin_cli.transport import UpstreamError
from tools import leakcheck

FIXTURES = Path(__file__).parent / "fixtures"
RAW = FIXTURES / "raw"

# Obviously synthetic, and used only when no capture is on disk at all.
FALLBACK_MEMBER = "urn:li:fsd_profile:SYNTHETIC0000000000000000000000000000"
FALLBACK_THREAD = f"urn:li:msg_conversation:({FALLBACK_MEMBER},2-synthetic==)"


def load(name: str) -> dict:
    for base in (FIXTURES, RAW):
        path = base / name
        if path.exists():
            return json.loads(path.read_text())
    pytest.skip(f"no fixture {name} in {FIXTURES} or {RAW}", allow_module_level=True)
    raise AssertionError("unreachable")


def _context() -> dict:
    for base in (FIXTURES, RAW):
        path = base / "context.json"
        if path.exists():
            return json.loads(path.read_text())
    return {}


_CONTEXT = _context()

MEMBER = _CONTEXT.get("member_urn") or FALLBACK_MEMBER
THREAD = _CONTEXT.get("conversation_urn") or FALLBACK_THREAD

# The exact-projection tests need a capture to compare against.
raw_only = pytest.mark.skipif(
    not (RAW / "conversations.json").exists(),
    reason="raw capture is gitignored; scrubbed fixtures carry different values",
)


def encoded(urn: str) -> str:
    """The urn as `restli.encode` will render it inside a variables tuple."""
    from linkedin_cli import restli

    return restli.encode(urn)


class FakeClient:
    """Records the paths a surface asks for and replays canned payloads."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.paths: list[str] = []

    def get(self, path: str):
        self.paths.append(path)
        return self.payloads.pop(0) if self.payloads else {}


def variables_of(path: str) -> str:
    query = path.split("?", 1)[1]
    for part in query.split("&"):
        if part.startswith("variables="):
            return part[len("variables=") :]
    raise AssertionError(f"no variables in {path}")


def conversations(**kw):
    client = FakeClient(load("conversations.json"))
    return client, messaging.list_conversations(client, MEMBER, **kw)


# ------------------------------------------------------------------- query ids


def test_query_ids_are_pinned():
    assert QUERY_ID_CONVERSATIONS == messaging.QUERY_IDS["conversations"]
    assert QUERY_ID_MESSAGES == messaging.QUERY_IDS["messages"]
    assert QUERY_ID_COUNTS == messaging.QUERY_IDS["mailbox_counts"]


QUERY_ID_CONVERSATIONS = "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"
QUERY_ID_MESSAGES = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"
QUERY_ID_COUNTS = "messengerMailboxCounts.fc528a5a81a76dff212a4a3d2d48e84b"


def test_query_id_is_overridable_from_the_environment(monkeypatch):
    """queryIds rotate on LinkedIn deploys, so an operator must be able to patch one."""
    monkeypatch.setenv("LINKEDIN_QUERY_ID_CONVERSATIONS", "messengerConversations.deadbeef")
    client, _ = conversations()
    assert "queryId=messengerConversations.deadbeef" in client.paths[0]


def test_pinned_query_id_is_used_when_no_override(monkeypatch):
    monkeypatch.delenv("LINKEDIN_QUERY_ID_CONVERSATIONS", raising=False)
    client, _ = conversations()
    assert client.paths[0].startswith("voyagerMessagingGraphQL/graphql?")
    assert f"queryId={QUERY_ID_CONVERSATIONS}" in client.paths[0]


# ------------------------------------------------------------------- variables


def test_conversations_variables_match_the_verified_live_form():
    """Character for character: a stray unescaped ',' turns the urn into structure."""
    client, _ = conversations()
    assert variables_of(client.paths[0]) == (
        "(query:(predicateUnions:List((conversationCategoryPredicate:"
        "(category:PRIMARY_INBOX)))),count:20,"
        f"mailboxUrn:{encoded(MEMBER)})"
    )


@pytest.mark.parametrize("urn", [MEMBER, THREAD])
def test_no_urn_punctuation_survives_into_the_variables(urn):
    """`:` `(` `)` `,` and `=` are RestLi's own structure characters.

    This is the bug the pinned form above exists to catch, asserted directly so
    it holds for any urn rather than only for the captured one.
    """
    assert not set(encoded(urn)) & set(":(),=")


def test_conversations_variables_honour_count_and_category():
    client, _ = conversations(count=5, category="MESSAGE_REQUEST_PENDING")
    variables = variables_of(client.paths[0])
    assert "(category:MESSAGE_REQUEST_PENDING)" in variables
    assert ",count:5," in variables


def test_conversations_cursor_becomes_last_updated_before():
    client, _ = conversations(cursor="1782564422409")
    assert variables_of(client.paths[0]).endswith(",lastUpdatedBefore:1782564422409)")


def test_messages_variables_are_just_the_conversation_urn():
    client = FakeClient(load("messages.json"))
    messaging.read_conversation(client, THREAD)
    assert variables_of(client.paths[0]) == f"(conversationUrn:{encoded(THREAD)})"
    assert f"queryId={QUERY_ID_MESSAGES}" in client.paths[0]


def test_messages_cursor_anchors_on_delivered_at():
    client = FakeClient(load("messages.json"))
    messaging.read_conversation(client, THREAD, count=20, cursor="1783583409545")
    assert variables_of(client.paths[0]).endswith(
        ",deliveredAt:1783583409545,countBefore:20,countAfter:0)"
    )


def test_mailbox_counts_variables_and_query_id():
    client = FakeClient(load("mailbox_counts.json"))
    messaging.mailbox_counts(client, MEMBER)
    assert variables_of(client.paths[0]) == f"(mailboxUrn:{encoded(MEMBER)})"
    assert f"queryId={QUERY_ID_COUNTS}" in client.paths[0]


# -------------------------------------------------------------- conversations


def test_projects_every_conversation_in_the_capture():
    _, (items, _, _) = conversations()
    assert len(items) == 10


def test_conversation_projection_has_exactly_the_contract_keys():
    _, (items, _, _) = conversations()
    for item in items:
        assert set(item) == {
            "conversation_urn",
            "last_activity_at",
            "unread",
            "participants",
            "counterpart",
            "last_message",
        }
        for who in item["participants"]:
            assert set(who) == {"name", "urn", "public_id"}
        assert item["last_message"] is not None
        assert set(item["last_message"]) == {"text", "sender", "sent_at"}


def test_conversation_urns_are_addressable():
    """The urn must be the msg_conversation one - it is what `messages read` takes."""
    _, (items, _, _) = conversations()
    for item in items:
        assert item["conversation_urn"].startswith("urn:li:msg_conversation:")
        for who in item["participants"]:
            assert who["urn"].startswith("urn:li:fsd_profile:")


def test_timestamps_are_iso_utc():
    _, (items, _, _) = conversations()
    for item in items:
        assert item["last_activity_at"].endswith("Z")
        assert len(item["last_activity_at"]) == 20


def test_counterpart_is_the_other_party_not_the_operator():
    _, (items, _, _) = conversations()
    for item in items:
        assert item["counterpart"] is not None
        assert item["counterpart"]["urn"] != MEMBER
        assert item["counterpart"] in item["participants"]


def test_counterpart_is_none_when_the_operator_is_alone():
    payload = {
        "data": {
            "data": {
                "messengerConversationsByCategoryQuery": {
                    "*elements": ["urn:li:msg_conversation:x"]
                }
            }
        },
        "included": [
            {
                "$type": "com.linkedin.messenger.Conversation",
                "entityUrn": "urn:li:msg_conversation:x",
                "lastActivityAt": 1783583409545,
                "read": True,
                "*conversationParticipants": [f"urn:li:msg_messagingParticipant:{MEMBER}"],
            },
            {
                "$type": "com.linkedin.messenger.MessagingParticipant",
                "entityUrn": f"urn:li:msg_messagingParticipant:{MEMBER}",
                "hostIdentityUrn": MEMBER,
                "participantType": {"member": {"firstName": {"text": "Operator"}}},
            },
        ],
    }
    items, _, _ = messaging.list_conversations(FakeClient(payload), MEMBER)
    assert items[0]["counterpart"] is None


def test_unread_reflects_the_read_flag():
    _, (items, _, _) = conversations()
    assert [item["unread"] for item in items] == [False] + [True] * 9


def test_unread_only_drops_the_read_conversations():
    _, (items, _, _) = conversations(unread_only=True)
    assert len(items) == 9
    assert all(item["unread"] for item in items)


def test_short_page_reports_no_more():
    """10 conversations for a count of 20 means the mailbox is exhausted."""
    _, (_, cursor, has_more) = conversations(count=20)
    assert (cursor, has_more) == (None, False)


def test_full_page_hands_back_the_epoch_of_the_oldest_conversation():
    payload = load("conversations.json")
    refs = payload["data"]["data"]["messengerConversationsByCategoryQuery"]["*elements"]
    _, (_, cursor, has_more) = conversations(count=len(refs))
    assert has_more is True
    assert cursor == str(_included(payload, refs[-1])["lastActivityAt"])


def _included(payload: dict, urn: str) -> dict:
    """Find an entity in `included` without going through `graph.Graph`.

    A second, independent path to the same data: if the projection and this
    helper agree, the projection is reading the fields it claims to read.
    """
    return next(e for e in payload["included"] if e.get("entityUrn") == urn)


def _expected_participant(payload: dict, participant_urn: str) -> dict:
    member = _included(payload, participant_urn)["participantType"]["member"]
    first = (member.get("firstName") or {}).get("text") or ""
    last = (member.get("lastName") or {}).get("text") or ""
    return {
        "name": f"{first} {last}".strip() or None,
        "urn": _included(payload, participant_urn)["hostIdentityUrn"],
        "public_id": member["profileUrl"].rstrip("/").rsplit("/", 1)[-1],
    }


@raw_only
def test_one_conversation_pinned_against_the_raw_entity():
    """Every projected field re-derived straight from `included`, no transcription."""
    payload = load("conversations.json")
    _, (items, _, _) = conversations()
    pinned = next(item for item in items if item["conversation_urn"] == THREAD)

    entity = _included(payload, THREAD)
    participants = [
        _expected_participant(payload, ref) for ref in entity["*conversationParticipants"]
    ]
    message = _included(payload, entity["messages"]["*elements"][0])

    assert pinned == {
        "conversation_urn": THREAD,
        "last_activity_at": messaging._iso(entity["lastActivityAt"]),
        "unread": not entity["read"],
        "participants": participants,
        "counterpart": next(p for p in participants if p["urn"] != MEMBER),
        "last_message": {
            "text": message["body"]["text"],
            "sender": _expected_participant(payload, message["*sender"])["name"],
            "sent_at": messaging._iso(message["deliveredAt"]),
        },
    }
    # The capture really does hold a two-party unread thread, so the assertion
    # above is not vacuously true against an empty conversation.
    assert len(participants) == 2
    assert pinned["unread"] is True
    assert pinned["last_message"]["text"]


# ------------------------------------------------------------------- messages


def test_read_conversation_projects_the_thread():
    client = FakeClient(load("messages.json"))
    items, _, _ = messaging.read_conversation(client, THREAD)
    assert len(items) == 1
    for item in items:
        assert set(item) == {"message_urn", "sender", "text", "sent_at"}
        assert set(item["sender"]) == {"name", "urn"}
        assert item["message_urn"].startswith("urn:li:msg_message:")
        assert item["sender"]["urn"].startswith("urn:li:fsd_profile:")


@raw_only
def test_one_message_pinned_against_the_raw_entity():
    payload = load("messages.json")
    items, _, _ = messaging.read_conversation(FakeClient(payload), THREAD)

    entity = next(e for e in payload["included"] if e["$type"].endswith("Message"))
    who = _expected_participant(payload, entity["*sender"])
    assert items[0] == {
        "message_urn": entity["entityUrn"],
        "sender": {"name": who["name"], "urn": who["urn"]},
        "text": entity["body"]["text"],
        "sent_at": messaging._iso(entity["deliveredAt"]),
    }
    assert items[0]["message_urn"].startswith("urn:li:msg_message:")
    assert items[0]["text"]


def test_short_message_page_reports_no_more():
    client = FakeClient(load("messages.json"))
    _, cursor, has_more = messaging.read_conversation(client, THREAD, count=20)
    assert (cursor, has_more) == (None, False)


def test_full_message_page_hands_back_the_oldest_delivered_at():
    payload = load("messages.json")
    _, cursor, has_more = messaging.read_conversation(FakeClient(payload), THREAD, count=1)
    oldest = [e for e in payload["included"] if e["$type"].endswith("Message")][-1]
    assert has_more is True
    assert cursor == str(oldest["deliveredAt"])


# -------------------------------------------------------------- mailbox counts


def test_mailbox_counts_projects_category_to_unread_count():
    counts = messaging.mailbox_counts(FakeClient(load("mailbox_counts.json")), MEMBER)
    assert set(counts) >= {"INBOX", "PRIMARY_INBOX", "ARCHIVE", "SPAM"}
    assert all(isinstance(v, int) for v in counts.values())


@raw_only
def test_mailbox_counts_pinned_against_the_raw_entity():
    payload = load("mailbox_counts.json")
    counts = messaging.mailbox_counts(FakeClient(payload), MEMBER)
    root = payload["data"]["data"][messaging.COUNTS_ROOT]
    assert counts == {e["category"]: e["unreadConversationCount"] for e in root["elements"]}
    assert len(counts) == 6


# ------------------------------------------------------------------- resilience


def test_empty_payload_projects_to_nothing():
    """A voyager error body has no `data`; a read surface must not raise on it."""
    assert messaging.list_conversations(FakeClient({}), MEMBER) == ([], None, False)
    assert messaging.read_conversation(FakeClient({}), THREAD) == ([], None, False)
    assert messaging.mailbox_counts(FakeClient({}), MEMBER) == {}


def test_dangling_participant_reference_is_tolerated():
    payload = {
        "data": {
            "data": {
                "messengerConversationsByCategoryQuery": {
                    "*elements": ["urn:li:msg_conversation:x"]
                }
            }
        },
        "included": [
            {
                "$type": "com.linkedin.messenger.Conversation",
                "entityUrn": "urn:li:msg_conversation:x",
                "lastActivityAt": 1783583409545,
                "read": False,
                "*conversationParticipants": ["urn:li:msg_messagingParticipant:gone"],
                "messages": {"*elements": ["urn:li:msg_message:gone"]},
            }
        ],
    }
    items, _, _ = messaging.list_conversations(FakeClient(payload), MEMBER)
    assert items == [
        {
            "conversation_urn": "urn:li:msg_conversation:x",
            "last_activity_at": "2026-07-09T07:50:09Z",
            "unread": True,
            "participants": [],
            "counterpart": None,
            "last_message": None,
        }
    ]


# ------------------------------------------------------- the send postcondition

# `messages send` and `messages reply` are one function, and it is the only write
# on this surface that puts text in front of another human. LinkedIn has no verb
# that unsends a message and this CLI has captured none, so the send is held to
# the standard `posts._confirm_removed` and `invitations._confirm_sent` hold:
# a response that establishes nothing is an error, never an `ok: true` carrying a
# null urn. An agent does not look again at a success.

SENT_URN = f"urn:li:msg_message:({FALLBACK_MEMBER},2-synthetic==)"


class WriteClient:
    """Replays one canned answer to a write and records what went out."""

    def __init__(self, result=None):
        self.result = result
        self.posts: list[tuple] = []

    def post(self, path, body, dry_run=False):
        self.posts.append((path, body, dry_run))
        if dry_run:
            return {"method": "POST", "url": path, "body": body}
        return self.result


def send(result, **kw):
    return messaging.send_message(WriteClient(result), MEMBER, THREAD, "hello", **kw)


def test_send_reports_the_urn_from_the_captured_success_shape():
    """`data["*value"]` is the shape docs/write-payloads.md pins from the capture."""
    assert send({"data": {"*value": SENT_URN}}) == {
        "message_urn": SENT_URN,
        "conversation_urn": THREAD,
    }


def test_send_refuses_to_report_a_send_on_an_empty_body():
    """What `transport.parse` hands back for a 200 with nothing in it."""
    for empty in ({}, None, ""):
        with pytest.raises(UpstreamError):
            send(empty)


def test_send_refuses_a_refusal_parked_at_the_top_level():
    with pytest.raises(UpstreamError) as exc:
        send({"errors": [{"message": "PARTICIPANT_NOT_REACHABLE"}]})
    assert "PARTICIPANT_NOT_REACHABLE" in str(exc.value)


def test_send_refuses_a_refusal_parked_under_data():
    """Voyager parks an executor refusal under either key, so both are read."""
    with pytest.raises(UpstreamError) as exc:
        send({"data": {"errors": [{"message": "CANNOT_MESSAGE_MEMBER"}], "*value": None}})
    assert "CANNOT_MESSAGE_MEMBER" in str(exc.value)


def test_send_refuses_a_null_value_reported_as_a_delivered_message():
    """The exact shape the old reader returned as `ok: true, message_urn: null`."""
    with pytest.raises(UpstreamError):
        send({"data": {"*value": None}})


def test_send_refuses_a_body_that_names_no_message_urn_at_all():
    with pytest.raises(UpstreamError):
        send({"data": {}, "included": []})


def test_send_reads_the_urn_from_the_other_spellings_a_create_answers_with():
    """A rotated create may inline the entity instead of referencing it."""
    assert send({"data": {"entityUrn": SENT_URN}})["message_urn"] == SENT_URN
    assert send({"data": {"urn": SENT_URN}})["message_urn"] == SENT_URN
    inlined = {"included": [{"$type": "com.linkedin.messenger.Message", "entityUrn": SENT_URN}]}
    assert send(inlined)["message_urn"] == SENT_URN


def test_a_decorated_conversation_is_not_mistaken_for_the_new_message():
    """`included` carries the thread the message went into; that urn proves nothing."""
    with pytest.raises(UpstreamError):
        send({"included": [{"$type": "com.linkedin.messenger.Conversation", "entityUrn": THREAD}]})


def test_an_unconfirmed_send_sends_the_operator_to_read_the_thread():
    with pytest.raises(UpstreamError) as exc:
        send({"data": {"*value": None}})
    message = str(exc.value)
    assert THREAD in message
    assert "messages read" in message


def test_an_unconfirmed_send_is_not_reported_as_retryable():
    """`cli._report` renders `.retryable` into the envelope an agent branches on,
    and a retry that lands is a second message to a real person.

    Unchanged and still green after `messages reply` grew a repeat check, and
    that is the point: the check is best effort - one page, exact text, blind to
    a write that has not appeared yet - so a machine-readable "yes, retry" would
    be a promise it cannot keep. The verdict used to be derived from the wire
    field the payload carries; same verdict, different reason."""
    for result in ({}, {"errors": [{"message": "nope"}]}, {"data": {"*value": None}}):
        with pytest.raises(UpstreamError) as exc:
            send(result)
        assert getattr(exc.value, "retryable", False) is False


def test_a_dry_run_is_still_previewed_without_a_postcondition():
    """Nothing was sent, so there is nothing to confirm."""
    out = messaging.send_message(WriteClient(), MEMBER, THREAD, "hi", dry_run=True)
    assert out["method"] == "POST"


# --------------------------------------------------------- the idempotency key


def test_the_captured_body_still_switches_the_server_side_dedupe_off():
    """The capture is the fact. `dedupeByClientGeneratedToken: false` is what the
    web client sends, and it is not edited here to make a docstring true."""
    client = WriteClient({"data": {"*value": SENT_URN}})
    messaging.send_message(client, MEMBER, THREAD, "hi", idempotency_key="fixed-key-123")
    _, body, _ = client.posts[0]
    assert body["message"]["originToken"] == "fixed-key-123"
    assert body["dedupeByClientGeneratedToken"] is False


def test_send_does_not_promise_the_dedupe_its_own_payload_disables():
    """The docstring is what an agent reads before deciding a retry is safe, and
    "LinkedIn dedupes on originToken" was an inference contradicted by the field
    sitting three lines under it. It has to name the field and its value."""
    doc = messaging.send_message.__doc__ or ""
    assert "dedupeByClientGeneratedToken" in doc
    assert "dedupes on" not in doc.lower()
    assert "idempot" in doc.lower()


def test_the_same_idempotency_key_twice_sends_twice():
    """The honest guarantee: none. There is no client-side dedupe either, and the
    key only makes the two bodies identical - which is not the same as one send."""
    client = WriteClient({"data": {"*value": SENT_URN}})
    messaging.send_message(client, MEMBER, THREAD, "hi", idempotency_key="k")
    messaging.send_message(client, MEMBER, THREAD, "hi", idempotency_key="k")
    assert len(client.posts) == 2


# ------------------------------------------------ credentials in LinkedIn's prose

# `transport.raise_for_status` scrubs the body it splices into an error, but a
# refusal that arrives inside a **200** never goes through it: `_confirm_sent`
# reads the `errors` array itself and hands `_error_text` straight to `_refused`.
# That string is whatever LinkedIn wrote, `cli._report` renders this exception's
# `str()` onto stderr, and under an agent gateway stderr is permanent model context - so a
# csrf token in a refusal is a live session an agent cannot be told to forget.


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
def test_a_refused_send_does_not_put_linkedins_credentials_on_stderr(answer_key):
    """`messages send` and `messages reply` are this one function - `cli.cmd_messages`
    routes both into `send_message` - so both verbs leak or neither does.

    The refusal is read out of a 200 body, so nothing upstream of here scrubbed
    it. Checked at both placements Voyager parks an `errors` list at, because a
    scrubber applied to only one of them is the same bug one level down.
    """
    errors = [{"message": credential_detail()}]
    answer = {"errors": errors} if answer_key == "top-level" else {"data": {"errors": errors}}
    with pytest.raises(UpstreamError) as exc:
        send(answer)
    assert_scrubbed_but_still_diagnostic(str(exc.value))


def test_an_unconfirmed_send_scrubs_its_detail_by_construction():
    """Today every `_unconfirmed` detail is a literal written in this repo, so
    there is no live path that leaks through it. It is scrubbed anyway, because
    the guarantee `render.py` claims is "by construction rather than by the
    caller remembering" - and the caller that forgets is the one added later.
    """
    assert_scrubbed_but_still_diagnostic(str(messaging._unconfirmed(THREAD, credential_detail())))


def test_a_scrubbed_refusal_still_names_the_thread_to_read_back():
    """The scrubber is applied to LinkedIn's detail, not to the whole message.
    The conversation urn is the caller's own argument and it is the only thing in
    here that tells an operator which thread to go and look at."""
    message = str(messaging._refused(THREAD, credential_detail()))
    assert THREAD in message
    assert "messages read" in message
