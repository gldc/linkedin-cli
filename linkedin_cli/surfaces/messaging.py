"""Messaging reads: conversations, threads and mailbox counts.

Messaging lives behind `voyagerMessagingGraphQL`, whose `queryId`s rotate on
LinkedIn's deploys - which is why they sit in `QUERY_IDS` rather than in the call
sites, and why each one can be replaced from the environment without a release.

Two things about this payload shape drive the code below:

* A `msg_conversation` urn contains parentheses, commas and `==` padding. Those
  are RestLi's own structural characters, so the urn must go through
  `restli.encode`; interpolating it raw makes the server parse half of it as
  nested tuples.
* Everything interesting is in `included`, referenced by `*` keys, and the
  GraphQL result itself is buried at `data.data.<queryName>`. `graph.Graph` does
  both of those lookups and returns `None` rather than raising when a reference
  dangles, which happens routinely for participants of deleted accounts.

Projections stay deliberately small: an agent reading its inbox should not have
to page through 90 KB of profile-picture artifacts to find out who wrote to it.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import restli
from ..graph import Graph
from ..transport import UpstreamError, scrub_secrets

PATH = "voyagerMessagingGraphQL/graphql?"

# Verified live. Override one with LINKEDIN_QUERY_ID_<KEY.upper()>
# when LinkedIn rotates it, rather than waiting for a release.
QUERY_IDS = {
    "conversations": "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf",
    "messages": "messengerMessages.5846eeb71c981f11e0134cb6626cc314",
    "mailbox_counts": "messengerMailboxCounts.fc528a5a81a76dff212a4a3d2d48e84b",
}

CONVERSATIONS_ROOT = "messengerConversationsByCategoryQuery"
COUNTS_ROOT = "messengerMailboxCountsByMailbox"

# The messages query answers under whichever root matches the pagination mode it
# was called in; the capture used the sync-token form. Trying each in turn beats
# guessing, and `_root` falls back to structural detection if they all miss.
MESSAGES_ROOTS = (
    "messengerMessagesBySyncToken",
    "messengerMessagesByConversation",
    "messengerMessagesByAnchorTimestamp",
)


def query_id(name: str) -> str:
    return os.environ.get(f"LINKEDIN_QUERY_ID_{name.upper()}") or QUERY_IDS[name]


def list_conversations(
    client: Any,
    mailbox_urn: str,
    count: int = 20,
    cursor: str | None = None,
    unread_only: bool = False,
    category: str = "PRIMARY_INBOX",
) -> tuple[list[dict], str | None, bool]:
    """Project one page of the mailbox, newest activity first."""
    variables: dict[str, Any] = {
        "query": {"predicateUnions": [{"conversationCategoryPredicate": {"category": category}}]},
        "count": count,
        "mailboxUrn": mailbox_urn,
    }
    if cursor:
        # LinkedIn's own `nextCursor` is a base64 blob whose middle field is this
        # same epoch, so paginating on `lastActivityAt` is equivalent and leaves
        # the caller holding a cursor it can reason about.
        variables["lastUpdatedBefore"] = _epoch(cursor)

    graph = Graph(client.get(PATH + restli.query_string(query_id("conversations"), variables)))
    root = _root(graph, CONVERSATIONS_ROOT)
    entities = _elements(graph, root, "com.linkedin.messenger.Conversation")

    items = [_conversation(graph, entity, mailbox_urn) for entity in entities]
    next_cursor, has_more = _page(
        [entity.get("lastActivityAt") for entity in entities], len(entities), count
    )
    if unread_only:
        # Filtered here, not upstream: no unread predicate was observed on the
        # live query, and inventing one risks a 400 on every list call.
        items = [item for item in items if item["unread"]]
    return items, next_cursor, has_more


def read_conversation(
    client: Any,
    conversation_urn: str,
    count: int = 20,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, bool]:
    """Project one page of a single thread."""
    variables: dict[str, Any] = {"conversationUrn": conversation_urn}
    if cursor:
        variables["deliveredAt"] = _epoch(cursor)
        variables["countBefore"] = count
        variables["countAfter"] = 0

    graph = Graph(client.get(PATH + restli.query_string(query_id("messages"), variables)))
    root = _root(graph, *MESSAGES_ROOTS)
    entities = _elements(graph, root, "com.linkedin.messenger.Message")

    items = [_message(graph, entity) for entity in entities]
    next_cursor, has_more = _page(
        [entity.get("deliveredAt") for entity in entities], len(entities), count
    )
    return items, next_cursor, has_more


def mailbox_counts(client: Any, mailbox_urn: str) -> dict:
    """Unread conversation count per mailbox category."""
    variables = {"mailboxUrn": mailbox_urn}
    graph = Graph(client.get(PATH + restli.query_string(query_id("mailbox_counts"), variables)))
    root = _root(graph, COUNTS_ROOT) or {}

    counts: dict[str, int] = {}
    for entry in root.get("elements") or []:
        category = entry.get("category") if isinstance(entry, dict) else None
        unread = entry.get("unreadConversationCount") if isinstance(entry, dict) else None
        if isinstance(category, str) and isinstance(unread, int):
            counts[category] = unread
    return counts


# --------------------------------------------------------------------- projection


def _conversation(graph: Graph, entity: dict, mailbox_urn: str) -> dict:
    participants = [_participant(p) for p in graph.deref(entity, "conversationParticipants") or []]
    return {
        "conversation_urn": entity.get("entityUrn"),
        "last_activity_at": _iso(entity.get("lastActivityAt")),
        "unread": _unread(entity),
        "participants": participants,
        # An agent replying to a thread needs to know who it is talking to, and
        # the operator is always in `participants` too. Group chats keep every
        # participant in the list and surface the first other party here.
        "counterpart": next((p for p in participants if p["urn"] != mailbox_urn), None),
        "last_message": _last_message(graph, entity),
    }


def _participant(entity: dict) -> dict:
    member = ((entity.get("participantType") or {}).get("member")) or {}
    name = " ".join(
        part for part in (_text(member.get("firstName")), _text(member.get("lastName"))) if part
    )
    profile_url = member.get("profileUrl") or ""
    return {
        "name": name or None,
        # The profile urn, not the messagingParticipant one: this is the handle
        # `profile get` and `messages send --to` accept.
        "urn": entity.get("hostIdentityUrn"),
        "public_id": profile_url.rstrip("/").rsplit("/", 1)[-1] or None,
    }


def _last_message(graph: Graph, entity: dict) -> dict | None:
    block = entity.get("messages")
    refs = block.get("*elements") if isinstance(block, dict) else None
    for ref in refs or []:
        message = graph.resolve(ref) if isinstance(ref, str) else None
        if message:
            sender = graph.deref(message, "sender")
            return {
                "text": _text(message.get("body")),
                # A bare name, per the output contract: the full sender lives on
                # each message once the agent opens the thread.
                "sender": _participant(sender)["name"] if isinstance(sender, dict) else None,
                "sent_at": _iso(message.get("deliveredAt")),
            }
    return None


def _message(graph: Graph, entity: dict) -> dict:
    sender = graph.deref(entity, "sender")
    who = _participant(sender) if isinstance(sender, dict) else {}
    return {
        "message_urn": entity.get("entityUrn"),
        "sender": {"name": who.get("name"), "urn": who.get("urn")},
        "text": _text(entity.get("body")),
        "sent_at": _iso(entity.get("deliveredAt")),
    }


# ---------------------------------------------------------------------- helpers


def _root(graph: Graph, *names: str) -> dict | None:
    for name in names:
        root = graph.graphql_root(name)
        if root is not None:
            return root
    # The query name is part of the rotating GraphQL surface, so fall back to the
    # only collection under `data.data` rather than returning nothing.
    inner = graph.data.get("data")
    if isinstance(inner, dict):
        for value in inner.values():
            if isinstance(value, dict) and ("*elements" in value or "elements" in value):
                return value
    return None


def _elements(graph: Graph, root: dict | None, want_type: str) -> list[dict]:
    if not isinstance(root, dict):
        return []
    resolved = (graph.resolve(ref, want_type) for ref in root.get("*elements") or [])
    return [entity for entity in resolved if entity]


def _page(stamps: list, fetched: int, count: int) -> tuple[str | None, bool]:
    """A short page means the collection is exhausted; only then is there no cursor."""
    if not stamps or fetched < count:
        return None, False
    oldest = stamps[-1]
    return (str(oldest), True) if isinstance(oldest, int) else (None, False)


def _unread(entity: dict) -> bool:
    if isinstance(entity.get("read"), bool):
        return not entity["read"]
    return bool(entity.get("unreadCount"))


def _text(node: Any) -> str | None:
    """AttributedText carries the plain string in `text`; attributes are styling."""
    if isinstance(node, dict):
        value = node.get("text")
        return value if isinstance(value, str) else None
    return node if isinstance(node, str) else None


def _epoch(value: Any) -> int | str:
    """Cursors travel as strings through the CLI but must go out as bare ints."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _iso(millis: Any) -> str | None:
    if not isinstance(millis, int):
        return None
    return datetime.fromtimestamp(millis / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- writes

CREATE_MESSAGE = "voyagerMessagingDashMessengerMessages?action=createMessage"
MARK_SEEN = "voyagerMessagingDashMessagingBadge?action=markAllMessagesAsSeen"


def send_message(
    client: Any,
    mailbox_urn: str,
    conversation_urn: str,
    text: str,
    *,
    idempotency_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Post a message into an existing conversation.

    The shape below is transcribed from a captured request, not inferred. Every
    field matters and LinkedIn rejects a wrong one with a bare 400:

    * `trackingId` is sixteen *raw bytes* carried as a string. Base64 fails.
    * `originToken` is required, and is a uuid4 in the capture.
    * `conversationUrn` is required even when the conversation is brand new -
      the web client resolves or creates the conversation before sending.

    **`idempotency_key` buys nothing from LinkedIn.** It sets `originToken` - and
    the same captured body sends `"dedupeByClientGeneratedToken": false`, a field
    whose name says the server is being told *not* to collapse repeats of the
    client's token. This docstring used to assert the opposite, which was an
    inference contradicted by the field three lines below it; the capture is the
    fact, so the claim was corrected rather than the body, because editing a
    captured body into something never observed is how this CLI gets a stranger's
    inbox wrong. What the key actually delivers is a stable, caller-chosen token
    on the wire - traceability, and nothing more. There is no client-side dedupe
    here either. Two calls with the same key are two messages in the thread, and
    nothing in this CLI unsends one, so a send whose answer was lost is resolved
    by reading the thread back and not by repeating it.
    """
    if not text or not text.strip():
        raise ValueError("message text is empty")

    body = {
        "message": {
            "body": {"attributes": [], "text": text},
            "renderContentUnions": [],
            "conversationUrn": conversation_urn,
            "originToken": idempotency_key or str(uuid.uuid4()),
        },
        "mailboxUrn": mailbox_urn,
        "trackingId": os.urandom(16).decode("latin-1"),
        "dedupeByClientGeneratedToken": False,
    }
    result = client.post(CREATE_MESSAGE, body, dry_run=dry_run)
    if dry_run:
        return result
    return {
        # Read out of the response rather than off the request, and never null on
        # an `ok`: `_confirm_sent` raises instead of handing back a None urn.
        "message_urn": _confirm_sent(result, conversation_urn),
        "conversation_urn": conversation_urn,
    }


# ------------------------------------------------------------- postconditions

# The kind of urn a delivered message is named by. Checked rather than assumed,
# because the create answers with the *conversation* decorated in beside the
# message and the caller already holds that one - see `_sent_urn`.
MESSAGE_URN_PREFIX = "urn:li:msg_message:"


def _confirm_sent(result: Any, conversation: str) -> str:
    """Refuse to call a message sent on a response that proves nothing.

    This function is the whole reason `send_message` cannot report a null urn.
    It used to return `data["*value"]` unread, so a refusal and a 200 with an
    empty body both came back as `ok: true` with `message_urn: null` - the exact
    failure `invitations._confirm_sent` was written against: an agent does not
    look again at a success, and the operator finds out from the person who
    never received the message.

    Three answers are refused. An empty body is what `transport.parse` hands back
    for a 200 with nothing in it; a create can answer 200 with the failure in the
    body, which `transport.raise_for_status` never sees, and it parks that
    failure under either the top level or `data`, so both are read; and a body
    that names no message urn establishes nothing in either direction.
    """
    if not isinstance(result, dict) or not result:
        raise _unconfirmed(conversation, "the response was empty")

    errors = _errors(result)
    if errors:
        # A named refusal is a definite answer and outranks any urn in the same
        # body: this endpoint is a create, so the only urn it can carry beside an
        # error is one that was not created.
        raise _refused(conversation, _error_text(errors))

    urn = _sent_urn(result)
    if urn:
        return urn
    raise _unconfirmed(conversation, "the response named no message urn")


def _sent_urn(result: dict) -> str | None:
    """The urn of the message the create answered with.

    `data["*value"]` is the spelling the capture pins - it was reproduced over
    plain HTTP, see docs/write-payloads.md - so it is read first. The inline
    spellings are tried after it rather than instead of it: a rotation that
    inlined the entity would otherwise turn every successful send into "sent but
    not confirmed", which sends the operator to look at a thread that is fine.

    `included` is filtered on the urn kind rather than taken first-found. The
    thread this message went into is decorated into the same answer, and
    accepting that urn would report a refusal as a delivered message - using the
    conversation urn the caller passed in as the evidence.
    """
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("*value", "entityUrn", "urn"):
            found = data.get(key)
            if isinstance(found, str) and found.startswith(MESSAGE_URN_PREFIX):
                return found

    included = result.get("included")
    for entry in included if isinstance(included, list) else []:
        found = entry.get("entityUrn") if isinstance(entry, dict) else None
        if isinstance(found, str) and found.startswith(MESSAGE_URN_PREFIX):
            return found
    return None


def _errors(result: dict) -> list | None:
    """The error list, from either place a Voyager answer parks one."""
    for node in (result, result.get("data")):
        if isinstance(node, dict):
            found = node.get("errors")
            if isinstance(found, list) and found:
                return found
    return None


def _error_text(errors: list) -> str:
    for entry in errors:
        if isinstance(entry, dict) and entry.get("message"):
            return str(entry["message"])
    return str(errors[0])


def _refused(conversation: str, detail: str) -> UpstreamError:
    """A message LinkedIn answered 200 to and then declined in the body.

    Kept distinct from `_unconfirmed` because the two ask for different next
    steps: this one is "stop" - a named refusal is an answer about the write, and
    the same request gets the same answer until something about it changes.

    `detail` is scrubbed here rather than at the call site. It is LinkedIn's own
    prose out of a **200** body, so `transport.raise_for_status` - which scrubs
    the body it splices, transport.py `raise_for_status` - never saw it; and
    `cli._report` renders this exception's `str()` onto stderr, which under
    an agent gateway is permanent model context. Doing it in the constructor is what makes
    the guarantee hold by construction instead of by the next caller remembering.
    `conversation` is deliberately left alone: it is the caller's own argument
    and the only thing here that says which thread to go and look at.
    """
    return UpstreamError(
        f"LinkedIn refused the message to {conversation}: {scrub_secrets(detail)}. That is an "
        f"answer about the write and not a transport failure, so sending it again unchanged will be "
        f"refused the same way. Read the thread back with `linkedin messages read "
        f'"{conversation}"` if you need to be sure nothing landed.'
    )


def _unconfirmed(conversation: str, detail: str) -> UpstreamError:
    """A message whose outcome the response does not establish.

    Reported rather than swallowed, and never as a success. Not marked retryable
    either - `cli._report` renders `.retryable` straight into the envelope an
    agent branches on, and the payload switches LinkedIn's client-token dedupe
    off (see `send_message`), so a retry that lands is a second message in front
    of a real person that nothing here can take back.

    `detail` goes through the scrubber for the same reason `_refused`'s does.
    Every detail that reaches here today is a literal written in this file, so
    there is no live path leaking through it - it is scrubbed anyway, because
    what `render.py` claims is that nothing unredacted reaches an envelope *by
    construction*, and a constructor that trusts its argument is one caller away
    from being the counterexample.
    """
    return UpstreamError(
        f"the message to {conversation} was sent but not confirmed: {scrub_secrets(detail)}. The "
        f"request reached LinkedIn, so it may already be in the thread - read it back with `linkedin "
        f'messages read "{conversation}"` before doing anything else. Do not retry it blind: '
        "the captured payload sends `dedupeByClientGeneratedToken: false`, so a second attempt "
        "is a second message, and this CLI has no verb that unsends either of them."
    )


def mark_all_read(client: Any, until_ms: int | None = None, *, dry_run: bool = False) -> dict:
    """Mark every conversation up to `until_ms` as seen. Mailbox-wide, and final.

    Named for what the capture does rather than for what a caller wants. The
    payload is `{"until": <epoch ms>}` against a *badge* endpoint: it carries no
    conversation and there is no argument that could narrow it, so the verb that
    used to accept a conversation urn was accepting something it then dropped.

    Marking one thread read needs a payload nobody has captured from a live
    session yet - see `docs/write-payloads.md`. Until it exists, a triage loop
    that runs this per thread marks every *other* unread thread seen too, and
    nothing on LinkedIn undoes that.
    """
    stamp = int(until_ms if until_ms is not None else time.time() * 1000)
    result = client.post(MARK_SEEN, {"until": stamp}, dry_run=dry_run)
    return result if dry_run else {"marked_read_until": stamp}


def find_conversation_with(
    client: Any, mailbox_urn: str, participant_urn: str | None, scan: int = 40
) -> str | None:
    """Locate an existing conversation with `participant_urn`.

    LinkedIn requires a conversationUrn even to start a thread, so sending to
    someone means finding the thread the web client would have created.
    """
    if not participant_urn:
        return None
    items, _, _ = list_conversations(client, mailbox_urn, count=scan)
    for conversation in items:
        for person in conversation.get("participants") or []:
            if person.get("urn") == participant_urn:
                return conversation.get("conversation_urn")
    return None
