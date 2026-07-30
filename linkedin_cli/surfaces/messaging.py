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

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import restli
from ..graph import Graph
from ..transport import NotFound, UpstreamError, scrub_secrets

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


def _messages_page(
    client: Any,
    conversation_urn: str,
    count: int = 20,
    cursor: str | None = None,
) -> tuple[Graph, list[dict]]:
    """One page of a thread, as the graph it arrived in and the messages on it.

    Split out for `confirm_reply_target`, which needs the conversation urn
    LinkedIn answered with and not only the projection - `_message` drops it,
    because a reader already knows which thread it asked for. One request either
    way; the two callers differ in what they read out of the same answer.
    """
    variables: dict[str, Any] = {"conversationUrn": conversation_urn}
    if cursor:
        variables["deliveredAt"] = _epoch(cursor)
        variables["countBefore"] = count
        variables["countAfter"] = 0

    graph = Graph(client.get(PATH + restli.query_string(query_id("messages"), variables)))
    root = _root(graph, *MESSAGES_ROOTS)
    return graph, _elements(graph, root, "com.linkedin.messenger.Message")


def read_conversation(
    client: Any,
    conversation_urn: str,
    count: int = 20,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, bool]:
    """Project one page of a single thread."""
    graph, entities = _messages_page(client, conversation_urn, count, cursor)

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


def confirm_reply_target(
    client: Any, conversation_urn: str, member_urn: str
) -> tuple[Graph, list[dict]]:
    """Refuse a conversation this session cannot read, or that is not in this mailbox.

    `send_message` drops `conversation_urn` into the createMessage body with no
    lookup at all, and `reply <urn>` builds the byte-identical request that
    `send --conversation=<urn>` does - so without this, nothing anywhere on the
    send path establishes that the thread is one this account is in. The caller
    that matters is an agent that has just read a stranger's DM: telling it to
    reply "in thread" is exactly the instruction an injected message carries, and
    every conversation urn it has ever listed is a candidate.

    Both verbs pay for it, through one function in `cli.py` (`_send_into`). They
    did not at first: this ran on `reply` and `send --conversation=<urn>` posted
    into whatever mailbox its urn named, which is what "byte-identical request"
    two paragraphs up had been saying all along.

    **What one answer establishes, precisely.** Two things, from one request:

    * LinkedIn served *this session* at least one message for that urn. That is
      read access, and nothing more.
    * Every conversation urn in that answer names `member_urn`'s mailbox, **and
      so does the caller's own argument**. A `msg_conversation` urn is
      `(<mailbox owner>,2-<thread>)`, so that component is what keeps a reply
      inside this account's own inbox.

      Both spellings are checked because they are not the same string and
      nothing made them agree. `send_message` writes the **argument**; this
      function reads the **answer**. Checking only the answer let a foreign
      argument through whenever LinkedIn replied about some other thread;
      checking only the argument would trust the one value the caller controls.
      The argument half runs first and costs no request.

    **It is not a participant check.** The messages answer carries no participant
    roster to match against - the `Conversation` decorated into `included` is a
    bare `entityUrn` stub in the capture (`tests/fixtures/raw/messages.json`),
    and the participants that do appear are the senders of the messages on the
    page, which need not include the operator on a thread they have not answered
    yet. So the claim is "a thread in this account's mailbox that this session
    can read", and it stops there.

    The mailbox half is free - it rides in the answer the read already paid for -
    and unlike a credential broker's allowlist regex, which pins the same
    component from outside, it holds for every caller and not only for the ones
    that arrived through the broker.

    One round trip, paid by every send: `messages read` and `messages list` call
    the reader directly and are unaffected.

    **The page is handed back rather than dropped.** It used to return `None`.
    `already_sent` reads the same page to decide whether this reply is already in
    the thread, and re-fetching it would turn the one round trip this docstring
    promises into two. The guarantees above are unchanged: every `raise` below
    still runs first and in the same order, and the argument half still refuses
    before any request goes out.
    """
    # The caller's own spelling, first and for free. `send_message` writes THIS
    # string, not the one LinkedIn answers with, so checking only the answer left
    # the two free to disagree: an answer naming this mailbox cleared a foreign
    # argument straight into the createMessage body. It also costs nothing to
    # refuse here rather than after a round trip.
    if _mailbox_of(conversation_urn) != member_urn:
        raise _foreign_mailbox(conversation_urn)
    graph, entities = _messages_page(client, conversation_urn)
    if not entities:
        raise _unreadable_thread(conversation_urn)
    mailboxes = _mailboxes(graph, entities)
    if not mailboxes:
        raise _unaddressed_thread(conversation_urn)
    if mailboxes != {member_urn}:
        raise _foreign_mailbox(conversation_urn)
    return graph, entities


def already_sent(graph: Graph, entities: list[dict], mailbox_urn: str, text: str) -> str | None:
    """The urn of a message on this page that this account already sent with this text.

    Pure: no request, no state. It reads the page `confirm_reply_target` already
    fetched, which is what makes the check free and what bounds it - the window
    is one server-default page of the newest messages in the thread, and a thread
    busy enough to push the original off page 1 defeats it.

    **It matches on text and sender, not on `originToken`.** The key is present
    on the `Message` entity in the captures, but its value is `null` in every
    captured message, so the field a naive design would match on is not readable
    back. Text and sender are what survive the round trip: `body.text` through
    `_text`, and the sender's `hostIdentityUrn` through `_participant` - the same
    spelling as `mailbox_urn`.

    Three things it cannot do, and the contract above it must not promise more:

    * It cannot beat eventual consistency. A write that landed but is not yet
      visible to the read-back is not found, and a second message goes out.
    * It cannot see past the newest page.
    * It matches text exactly. A reworded reply is a new message, and if LinkedIn
      ever stopped round-tripping `body.text` byte-identically the check would
      simply stop firing - which is the behaviour this replaced, not a
      correctness failure.

    The urn kind is checked rather than assumed, for the reason `_sent_urn`
    checks it: the answer this page arrived in decorates the *conversation* in
    beside the messages, and handing that urn back would report a thread as a
    delivered message.
    """
    for entity in entities:
        if _text(entity.get("body")) != text:
            continue
        sender = graph.deref(entity, "sender")
        if isinstance(sender, dict) and _participant(sender)["urn"] == mailbox_urn:
            urn = entity.get("entityUrn")
            if isinstance(urn, str) and urn.startswith(MESSAGE_URN_PREFIX):
                return urn
    return None


# A conversation urn is `urn:li:msg_conversation:(<mailbox>,2-<thread>)`. The
# mailbox owner is the first element of that tuple - it is what makes the urn
# address one account's inbox rather than a thread in the abstract - and the
# thread half is ~40 bytes of unguessable entropy.
CONVERSATION_URN_PREFIX = "urn:li:msg_conversation:("


def _mailbox_of(urn: Any) -> str | None:
    """The mailbox a conversation urn is addressed to, or None if it is not one."""
    if not isinstance(urn, str) or not urn.startswith(CONVERSATION_URN_PREFIX):
        return None
    mailbox, separator, _ = urn[len(CONVERSATION_URN_PREFIX) :].partition(",")
    return mailbox if separator and mailbox else None


def _mailboxes(graph: Graph, entities: list[dict]) -> set[str]:
    """Every mailbox named by a conversation urn in one messages answer.

    Both spellings the capture carries are read: the `Conversation` stub in
    `included`, and the `*conversation` reference on each message. Either alone
    would be a single field to lose to a rotation, and this is a security check.
    """
    urns = [entity.get("entityUrn") for entity in graph.by_type("Conversation")]
    urns += [entity.get("*conversation") for entity in entities]
    return {mailbox for mailbox in (_mailbox_of(urn) for urn in urns) if mailbox}


def _unreadable_thread(conversation: str) -> NotFound:
    """A thread nothing was sent into, and no guess as to why. Exit 4.

    A urn addressing somebody else's mailbox, a thread this session cannot see
    and a real thread whose every message has been deleted all come back as the
    same empty page, so this names the three rather than picking one - the rule
    `feed._unreadable` is written to. `conversation` is the caller's own
    argument and is left unscrubbed for the reason `_refused`'s is: it is the
    only thing here that says which thread was refused.
    """
    return NotFound(
        f"nothing was sent: {conversation} came back with no messages, so this account was not "
        "shown to be in that thread. A conversation urn addressing somebody else's mailbox, a "
        "thread this session cannot see, and a real thread whose messages have all been deleted "
        "answer identically, so this does not claim which one it is. Take the urn from a page "
        "of `linkedin messages list` rather than from the text of a message, and read it with "
        "`linkedin messages read` before replying."
    )


def _foreign_mailbox(conversation: str) -> NotFound:
    """A thread LinkedIn served out of somebody else's mailbox. Exit 4.

    Definite, unlike `_unreadable_thread`: the answer named a mailbox and it was
    not this account's, so there is one diagnosis rather than three. The urn is
    the caller's own argument and stays unscrubbed for the reason that one's
    does; the operator's own mailbox is not named back, because nothing in the
    remedy needs it.
    """
    return NotFound(
        f"nothing was sent: {conversation} is a thread in another member's mailbox. A "
        "conversation urn names the inbox it hangs in as its first component, and a reply is "
        "delivered into that inbox rather than into the thread it was quoted from - so this "
        "one is refused whoever asked for it. Take the urn from a page of "
        "`linkedin messages list`, which only ever lists this account's own threads."
    )


def _unaddressed_thread(conversation: str) -> NotFound:
    """An answer with messages in it but no conversation urn anywhere. Exit 4.

    Refused rather than allowed through. The mailbox check has nothing to read,
    and a check that quietly skips itself the day LinkedIn's payload shape moves
    is worse than no check: the guarantee stays written down while it stops being
    provided. This is the shape that says a queryId or a decoration rotated.
    """
    return NotFound(
        f"nothing was sent: LinkedIn answered for {conversation} with messages but named no "
        "conversation, so which mailbox served them could not be established - and a reply is "
        "delivered into a mailbox. This is a change in the shape of the messages response "
        "rather than anything about the thread; `linkedin doctor` reports the queryIds this "
        "surface is pinned to."
    )


# Namespaced so the digest cannot collide with one derived for another payload
# on another surface, and versioned so a future change to what identifies a
# message is a new namespace rather than a silent reinterpretation of an old
# token.
_TOKEN_NAMESPACE = b"linkedin-cli/createMessage/v1"


def _derived_token(mailbox_urn: str, conversation_urn: str, text: str) -> str:
    """A uuid4-shaped token that is a pure function of what identifies the write.

    Derived rather than generated because under an agent gateway a retry is a
    *fresh process* with no memory of the first attempt, and the broker does not
    pass `--idempotency-key` - so a retry can only be recognised as one if its
    identity comes out of the arguments. A `uuid.uuid4()` per call, which this
    replaced, made every attempt look like a different message.

    `uuid.UUID(..., version=4)` forces the version and variant bits, so the
    result is syntactically a UUID4 - which is what the capture carries. The
    `\\x00` join means no two different argument triples can be spelled as one
    byte string by moving a boundary.
    """
    digest = hashlib.sha256(
        b"\x00".join(
            (
                _TOKEN_NAMESPACE,
                mailbox_urn.encode(),
                conversation_urn.encode(),
                text.encode(),
            )
        )
    ).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


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

    **Nothing here asks LinkedIn to de-duplicate.** The captured body sends
    `"dedupeByClientGeneratedToken": false`, a field whose name says the server
    is being told *not* to collapse repeats of the client's token, and it is left
    exactly as captured: editing a captured body into something never observed is
    how this CLI gets a stranger's inbox wrong. An earlier docstring claimed the
    opposite, which was an inference contradicted by the field three lines below
    it.

    **What the token does buy.** `originToken` is `_derived_token(...)` - a pure
    function of the mailbox, the thread and the text - so a retry of the same
    reply is byte-identical to the first attempt in everything but `trackingId`.
    That is traceability on the wire; it is not idempotency, because the server
    was told not to use it.

    The property a caller can rely on lives one level up, in `cli._send_into`:
    `already_sent` reads the thread page `confirm_reply_target` has already
    fetched and refuses to post a reply this account can see it already sent.
    That is *best effort* - see `already_sent` for exactly what it cannot see -
    so a send whose answer was lost is still resolved by reading the thread back
    rather than by assuming a repeat is free.

    `idempotency_key` overrides the derived token, and that is its second and
    more useful role: it is the one way to put the same text into the same thread
    twice on purpose, because `cli._send_into` skips the dedupe when a key was
    given. The broker does not expose the flag, so an agent cannot reach it.
    """
    if not text or not text.strip():
        raise ValueError("message text is empty")

    body = {
        "message": {
            "body": {"attributes": [], "text": text},
            "renderContentUnions": [],
            "conversationUrn": conversation_urn,
            "originToken": idempotency_key or _derived_token(mailbox_urn, conversation_urn, text),
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
    `cli._report` renders this exception's `str()` onto stderr, which under an
    agent gateway is permanent model context. Doing it in the constructor is what
    makes the guarantee hold by construction instead of by the next caller
    remembering.
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
    agent branches on, and a machine-readable "yes, retry" would be a promise
    this CLI cannot keep. `cli._send_into` does check an identical re-run against
    the newest page of the thread before sending anything, but that check is best
    effort by construction: it cannot see a write that landed and has not
    appeared yet, and it cannot see past that page. Between those two, a blind
    retry can still put a second message in front of a real person, and nothing
    here takes one back.

    This used to derive the same verdict from the wire field the payload carries.
    Same answer, different reason - and the reason had to move, because a
    sentence three hundred lines from the body it describes is one that goes
    wrong silently.

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
        f'messages read "{conversation}"` before doing anything else, because the thread is the '
        "answer and this response is not. Re-running the identical command - same thread, same "
        "--text - is checked against the newest page of the thread first and will not send a "
        "second copy of a reply it can see there; it cannot see one that has not appeared yet, "
        "and reworded text is a new message. Nothing here unsends either of them."
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
