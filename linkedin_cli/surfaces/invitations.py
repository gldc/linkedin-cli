"""Connection requests: sending one, and reading the ones you were sent.

Two halves with almost nothing in common, kept together because they are one
LinkedIn surface and because the *missing* third half explains both. The
invitation manager was observed live (docs/sdui-migration.md) and
it has left Voyager: the screen is server-driven UI, its data is rendered into
the document, and pressing Withdraw posts a protobuf-shaped
`proto.sdui.actions.core.NavigateToScreen` to `/flagship-web/` which answers with
a React Server Components stream. `transport.parse` cannot read that and `BASE`
does not point at it. So:

* **`invite` sends**, from a payload transcribed by interception (below).
* **`list_received` reads**, from the one finder that survived the migration -
  `relationships/invitationViews?q=receivedInvitation`, verified against three
  agreeing reads.
* **`invite withdraw` and the sent list do not exist here and will not.** Not
  "not captured yet": `NO_WITHDRAW` and `NO_SENT_LIST` say so and point at the
  browser. Seven spellings of a sent-invitations finder were probed; three
  answered 404 and four 400. Another capture run cannot close either gap, and
  the last two capture runs cost a stranger's invitation each.

The request body is a transcription of traffic captured by driving
the real "Connect" control, pausing the request with CDP `Fetch` at
`requestStage: Request`, recording `request.postData` and then **aborting** it.
Nothing was sent to learn it: the sent-invitations list read 9 before and 9
after, with the target absent. It is reproduced field for field, to the standard
`surfaces/social.py` and `surfaces/posts.py` hold their payloads to - a guessed
body comes back as a bare `400` that names no field, and an agent cannot tell a
guess from a fact.

Four properties of this write drive everything below.

* **It is addressed by a member urn, and it cannot be taken back.** There is no
  withdraw verb in this CLI and there is not going to be one, so the only
  protection against inviting the wrong person is refusing a urn that is not
  unambiguously one. `profile_urn` therefore accepts `urn:li:fsd_profile:<id>`
  and nothing else: not a public id, not a profile URL, not a neighbouring urn
  type. `linkedin profile get` is where a caller gets the real one.
* **`verifyQuotaAndCreateV2` means LinkedIn counts invitations server-side.** A
  refusal there is a real answer about the account, not a transport hiccup, so
  it is raised as `InvitationQuotaExceeded` rather than as a generic upstream
  failure that an agent reads as "try again". The action's own name contains the
  word `quota`, and it is in the URL of *every* failure from this route - so the
  detector strips the action spelling before it looks, or a rotated decoration
  gets reported as a week-long quota wait.
* **The response was never captured**, because the capture aborted at the
  request. So the reader is liberal about *where* the new invitation's urn sits
  and strict about there being one. An invitation reported as sent with a null
  urn is worse than an error: an agent does not look again at a success, and the
  operator finds out from the person who never received it.
* **There is no dedupe token and no note field.** Two identical requests are two
  invitations to the same human, so this surface is single-shot by construction:
  one `client.post` on the only path through it, and no retry loop anywhere. And
  `--note` has nothing in the capture to map onto, which is why `cli` refuses the
  flag instead of inventing a key for it.
"""

from __future__ import annotations

import os
from typing import Any

from ..graph import Graph
from ..transport import UpstreamError, scrub_secrets
from .feed import WEB_BASE

# Borrowed rather than reimplemented: LinkedIn stamps every surface in epoch
# milliseconds, and a second converter is a second place for a timezone to be
# got wrong.
from .notifications import iso_utc

# Captured live by `tools/capture_payloads.py` through CDP `Fetch`; the
# request was aborted, so no invitation was sent to learn it. Override it with
# LINKEDIN_DECORATION_ID_<KEY.upper()> when LinkedIn retires the version, rather
# than waiting for a release - decorations are versioned (`-2` here) and a
# retired one fails the same way a rotated queryId does.
DECORATION_IDS = {
    "invite": "com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-2",
}

INVITE_PATH = (
    "voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2"
    "&decorationId={decoration_id}"
)

# Anything starting with this is judged as a urn rather than handed to
# `profile.resolve_public_id`, which would call it "not a usable public id" -
# true, and no use to a caller who is plainly holding a urn and only has the
# wrong kind of one.
URN_PREFIX = "urn:li:"

PROFILE_URN_PREFIX = f"{URN_PREFIX}fsd_profile:"

# Where a pending invitation is visible. Named here because both failure paths
# send the operator to it, and a write that may or may not have landed is only
# resolvable by looking.
SENT_INVITATIONS_URL = f"{WEB_BASE}/mynetwork/invitation-manager/sent/"

# The received-invitation finder, verified live and reproduced in
# the parameter order the probe used (docs/sdui-migration.md). Thirteen spellings
# were tried and this is one of the two that answered: the sent-side collection
# 404s and every Dash spelling 400s. Probing a *read* is legitimate where probing
# a write never is - a wrong guess on a GET costs a status code - which is the
# only reason a verified route exists here at all.
RECEIVED_PATH = "relationships/invitationViews?count={count}&q=receivedInvitation&start={start}"

RECEIVED_DEFAULT_COUNT = 10

# The evidence for both refusals below, gathered live and read-only.
# Cited by name in the messages themselves so that the next reader
# gets what was observed rather than somebody's conclusion about it - the two
# stubs previously said "the payload was never captured", which reads as one
# more careful capture run away, and that is exactly what the last session was
# left pointed at.
INVITATION_MANAGER_DOC = "docs/sdui-migration.md"

NO_WITHDRAW = (
    "`invite withdraw` is not implemented, and it is not a gap another capture run can "
    "close. LinkedIn's invitation manager has migrated to its server-driven UI: pressing "
    "Withdraw posts a `proto.sdui.actions.core.NavigateToScreen` to /flagship-web/ and gets "
    "back a React Server Components stream - and it does not even perform the withdrawal, it "
    "opens a confirmation screen that has to be fetched from the server first. This CLI's "
    "transport speaks Voyager JSON at /voyager/api/ and cannot speak that protocol at all, "
    f"so there is no request body for anyone to record. The evidence is in "
    f"{INVITATION_MANAGER_DOC}, observed live. Withdraw it in the browser at "
    f"{SENT_INVITATIONS_URL} - and note before you do that LinkedIn blocks re-inviting a "
    "withdrawn contact for up to three weeks."
)

NO_SENT_LIST = (
    "`invitations sent` is not implemented: no Voyager route for the invitations you have "
    "*sent* survives. Seven spellings were probed live - three answered 404 and "
    "four answered 400 - because that screen is server-driven UI now and its data is rendered "
    "into the document rather than fetched; a cold load of the invitation manager issues zero "
    "invitation requests. The invitations you have *received* are a different route and do "
    "work: `linkedin invitations list`. The evidence is in "
    f"{INVITATION_MANAGER_DOC}; the sent list itself is at {SENT_INVITATIONS_URL}, in a "
    "browser."
)

# The action spelling, lowercased. Stripped from any text before it is searched
# for quota signs: `verifyQuotaAndCreateV2` contains `quota`, it appears in the
# URL of every failure from this route, and a detector that did not remove it
# first would classify all of them - a rotated decoration included - as a spent
# invitation quota, which tells the operator to wait a week over a config change.
_ACTION_WORDS = ("verifyquotaandcreatev2", "verifyquotaandcreate")

# What a server-side invitation refusal says. UNVERIFIED - no refusal was ever
# captured - so this is a classifier over LinkedIn's own error text, and it fails
# safe: an unrecognised refusal falls through to the generic "sent but not
# confirmed" error rather than being reported as a success.
_QUOTA_SIGNS = ("quota", "invitation limit", "too many invitation", "weekly limit")


class InvitationQuotaExceeded(UpstreamError):
    """LinkedIn refused the invitation against its own invitation quota.

    Its own class, and not `retryable`, because the three answers an agent can
    take from a failure are "retry", "look" and "stop", and this one is "stop".
    A generic upstream error is the code agents retry; a refused invitation
    retried in a loop is how a spent quota becomes a restricted account.
    """


def decoration_id(name: str) -> str:
    return os.environ.get(f"LINKEDIN_DECORATION_ID_{name.upper()}") or DECORATION_IDS[name]


def invite(client: Any, urn: Any, public_id: str | None = None, dry_run: bool = False) -> dict:
    """Send a connection request to a member, addressed by their profile urn.

    Single-shot by construction - see the module docstring. Everything that can
    be refused without a request is refused before the request, so a malformed
    urn costs neither a write slot nor an invitation to somebody unintended.
    """
    target = profile_urn(urn)
    body = {"invitee": {"inviteeUnion": {"memberProfile": target}}}

    path = INVITE_PATH.format(decoration_id=decoration_id("invite"))
    # The only request in this function, on purpose. Anything that wrapped this
    # line in a loop, or called it again after a failure, would put a second
    # invitation in front of the same person.
    try:
        result = client.post(path, body, dry_run)
    except InvitationQuotaExceeded:
        raise
    except UpstreamError as exc:
        # A 4xx never reaches the body reader below: `transport.raise_for_status`
        # turns it into an `UpstreamError` carrying the response text, which
        # exits 6 - the code an agent retries. A quota refusal arriving that way
        # has to be reclassified here or it is indistinguishable from a fault.
        if _is_quota_refusal(str(exc)):
            raise _quota(target, str(exc)) from exc
        raise
    if dry_run:
        return result

    _confirm_sent(result, target)
    return {
        # `render` keys a text block on the first identifier it recognises, and
        # this is the one the caller passed in - the *subject* of the write. An
        # invitation urn would name a record only LinkedIn can resolve.
        "profile_urn": target,
        "public_id": public_id,
        "invitation_urn": _created_urn(result),
        "invited": True,
        # Built from the public id or not at all. A profile permalink cannot be
        # assembled out of an fsd_profile urn, and handing back one that was
        # would give an agent a dead link to cite as evidence the invitation
        # reached somebody.
        "url": f"{WEB_BASE}/in/{public_id}" if public_id else None,
    }


# ------------------------------------------------ the invitations you received


def list_received(
    client: Any, count: int = RECEIVED_DEFAULT_COUNT, cursor: str | None = None
) -> tuple[list[dict], str | None, bool]:
    """One page of pending *received* invitations: (items, next_cursor, has_more).

    The route rests on three agreeing reads; the contents rest on **one** row
    from **one** inbox, and that asymmetry is what this function is built around.

    `relationships/invitationViews?q=receivedInvitation` was probed live and
    answered 200 with `data.elements` and `data.paging`
    (docs/sdui-migration.md). Three independent reads agreed - the finder,
    `relationships/invitationsSummary` reporting `numPendingInvitations: 0`, and
    the page itself rendering `All (0)` - which is what makes it a verified route
    rather than a URL that happened to return 200.

    That inbox was empty, so for a while `elements` had only ever been seen as
    `[]` and everything below the wrapper was a guess. It has since been read
    against a real invitation and pinned by
    `tests/fixtures/invitations_received.json`, and the guess turned out to be
    wrong about the collection *and* about every field on the row - see
    `_elements` and `_project_invitation` for what it actually is.

    So the reading below stays liberal - several spellings tried per field, all
    optional except the urn - and the whole thing still **fails loudly**: a
    non-empty `elements` that projects to nothing raises rather than returning
    `[]`. The fail-loud rule survives the fixture on purpose. It was never there
    only because the shape was unknown; it is there because the failure mode does
    not announce itself, and one observed row cannot rule out a second kind of
    row. A wrong parser that answers `[]` is indistinguishable from an operator
    who has no pending invitations. One of those is a bug report and the other is
    a fact about the account, and the agent reading the output will conclude the
    second. It is the same rule as "a zero-capture run is a failure, not a no-op",
    and it is why one unreadable element among readable ones is an error too: a
    page reporting three invitations out of ten is the same defect, quieter.
    """
    payload = client.get(_received_path(count, cursor))
    graph = Graph(payload if isinstance(payload, dict) else {})
    elements = _elements(graph, payload)
    if elements is None:
        raise _unreadable_page()

    projected = [_project_invitation(graph, element) for element in elements]
    unreadable = projected.count(None)
    if unreadable:
        # Before the cursor is computed, and before anything is returned. There
        # is no partial answer here: a caller handed nine of ten rows has no way
        # to know the tenth existed.
        raise _unprojectable(unreadable, len(elements))

    next_cursor = _received_cursor(payload, elements, count, cursor)
    return [item for item in projected if item], next_cursor, next_cursor is not None


def _received_path(count: Any, cursor: Any) -> str:
    """The finder URL, with both parameters reduced to integers.

    `start` is an offset rather than an opaque token, so a cursor that is not a
    number is a caller error and is refused here - both because the alternative
    is pasting an arbitrary string into a query string, and because a finder
    given a nonsense offset answers with an empty page, which reads as an empty
    inbox. A `count` of zero would do the same thing, so it is floored rather
    than passed through.
    """
    start = 0 if cursor in (None, "", True) else _offset(cursor)
    return RECEIVED_PATH.format(count=max(1, int(count or RECEIVED_DEFAULT_COUNT)), start=start)


def _offset(cursor: Any) -> int:
    try:
        value = int(str(cursor))
    except (TypeError, ValueError):
        raise ValueError(
            f"{cursor!r} is not a usable cursor for `invitations list`: this finder pages by "
            "a numeric offset, and the value to pass is the `next_cursor` the previous page "
            "reported."
        ) from None
    if value < 0:
        raise ValueError(f"{cursor!r} is not a usable cursor: an offset cannot be negative.")
    return value


def _elements(graph: Graph, payload: Any) -> list | None:
    """The invitation views, or `None` if the answer carried no list at all.

    `None` and `[]` are kept apart deliberately. An empty list means "no pending
    invitations"; a missing list means the route stopped answering the way it was
    seen to answer, and reporting *that* as an empty inbox is the failure this
    whole surface is written to avoid.

    **The list is `*elements`, not `elements`**, and that cost a live run. This
    is a normalized Voyager collection: `data["*elements"]` holds *references* -
    `urn:li:fs_relInvitationView:<id>` - and the entities themselves are in
    `included`. Reading only the plain `elements` key found nothing on the first
    populated inbox this route was ever pointed at, and the surface correctly
    refused rather than reporting the operator had no invitations. `feed.py` had
    always dereferenced its own `*elements`; this one was written before a
    populated answer had been seen, against a shape that guessed.

    Both spellings are still accepted: a plain list is read as entities, a ref
    list is resolved through `included`.
    """
    if not isinstance(payload, dict):
        return None
    for node in (payload.get("data"), payload):
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("elements"), list):
            return node["elements"]
        if isinstance(node.get("*elements"), list):
            # `deref` drops danglers, so an element whose entity is missing from
            # `included` would vanish silently. Count first and refuse below, on
            # the same rule that makes a partially-projected page an error.
            refs = [ref for ref in node["*elements"] if isinstance(ref, str)]
            resolved = graph.deref(node, "elements") or []
            if len(resolved) != len(refs):
                raise _dangling(len(refs) - len(resolved), len(refs))
            return resolved
    return None


# Where the invitation itself sits inside an element, and where the member who
# sent it sits inside that. The real answer reaches both by *reference*, which
# `Graph.deref` resolves; the flat spellings below are what was guessed before
# that answer was read, kept because one populated inbox is not enough evidence
# to delete a fallback. A single wrong key here is a projection that silently
# drops the page, and a bare invitation with no wrapper is read as itself for the
# same reason.
_INVITATION_KEYS = ("invitation", "genericInvitationView", "invitationView")
_SENDER_KEYS = ("fromMember", "fromMemberProfile", "inviter", "fromMemberResolutionResult")
_MESSAGE_KEYS = ("message", "customMessage", "invitationMessage")
_SENT_AT_KEYS = ("sentTime", "sentAt", "createdAt")
_TYPE_KEYS = ("invitationType", "type")


def _project_invitation(graph: Graph, element: Any) -> dict | None:
    """One invitation reduced to what an agent can act on, or `None` if unreadable.

    Written against a real populated answer, observed live. The shape is
    three hops, each a reference resolved through `included`:

        InvitationView --*invitation--> Invitation --*fromMember--> MiniProfile

    The `Invitation` carries `mailboxItemId` (`urn:li:invitation:<id>`), which is
    what names the invitation itself, plus `sentTime`, `message`, `invitationType`
    and `sharedSecret`. The earlier version of this function guessed at flat keys
    on the element - `invitation`, `fromMemberProfile`, `sentAt` - and none of
    them exist. It was written before any populated row had been seen, and it
    would have projected every one of them to `None`.

    `None` is a refusal, not an omission - see `list_received`. The invitation
    urn is the one required field: without it the row names nothing, and a list
    of rows naming nothing is worse than an error because it looks like an answer.
    """
    if not isinstance(element, dict):
        return None
    # The view wraps the invitation; a bare Invitation is read as itself, since
    # the wrapper carries nothing else this projection wants.
    invitation = graph.deref(element, "invitation")
    if not isinstance(invitation, dict):
        invitation = element

    # `mailboxItemId` first: `urn:li:invitation:<id>` is the urn LinkedIn's own
    # accept and ignore actions take, while `entityUrn` is the read-side
    # `fs_relInvitation` spelling. Neither is usable by this CLI today - there is
    # no accept verb - so the more actionable one is carried for whoever adds it.
    urn = invitation.get("mailboxItemId") or invitation.get("entityUrn")
    if not isinstance(urn, str) or not urn.startswith(URN_PREFIX):
        return None

    sender = graph.deref(invitation, "fromMember")
    return {
        "invitation_urn": urn,
        "type": _first_str(invitation, _TYPE_KEYS),
        "from": _sender(sender),
        # Null on an invitation sent without a note, which is the common case.
        "message": _first_str(invitation, _MESSAGE_KEYS),
        "sent_at": iso_utc(_first(invitation, _SENT_AT_KEYS)),
        # Absent stays `None` rather than collapsing to `False`: "they have not
        # seen it" and "this response did not say" are different things.
        "unseen": invitation.get("unseen") if isinstance(invitation.get("unseen"), bool) else None,
        # Carried even though nothing here consumes it. It is the per-invitation
        # token LinkedIn's accept and ignore actions require, it is only in this
        # response, and a missing one would cost another live probe against the
        # operator's invitation surface to recover.
        "shared_secret": (
            invitation.get("sharedSecret")
            if isinstance(invitation.get("sharedSecret"), str)
            else None
        ),
        # No accept and no ignore verb exists here, so the actionable thing an
        # agent can do with one of these is look the sender up - which is why the
        # sender is projected at all and why the permalink is theirs.
        "url": _sender_url(sender),
    }


def _sender(node: dict | None) -> dict:
    """The member who sent the invitation, with the empty fields dropped.

    Dropped rather than nulled, so that a sender the response did not describe
    reads as `{}` - nothing known - instead of a record full of nulls that looks
    like a member with no name.
    """
    if not isinstance(node, dict):
        return {}
    name = " ".join(
        str(node[key]) for key in ("firstName", "lastName") if isinstance(node.get(key), str)
    ).strip()
    public_id = node.get("publicIdentifier")
    found = {
        "name": name or _first_str(node, ("name", "fullName")),
        "headline": _first_str(node, ("occupation", "headline")),
        "public_id": public_id if isinstance(public_id, str) else None,
        # `dashEntityUrn`, never `entityUrn`. A MiniProfile's `entityUrn` is the
        # `urn:li:fs_miniProfile:` spelling, which addresses nothing: `invite`
        # refuses it by name and `profile get` cannot take it. Only the dash form
        # is usable, which is the same rule `feed.py` follows for post authors.
        "profile_urn": _first_str(node, ("dashEntityUrn",)),
    }
    return {key: value for key, value in found.items() if value}


def _sender_url(node: dict | None) -> str | None:
    public_id = (node or {}).get("publicIdentifier")
    return f"{WEB_BASE}/in/{public_id}" if isinstance(public_id, str) and public_id else None


def _received_cursor(payload: Any, elements: list, count: Any, cursor: Any) -> str | None:
    """The offset of the next page, or `None` when this one is the last.

    `paging.total` is used when it is there, and a full page is the fallback when
    it is not. That fallback leans deliberately the wrong way: stopping early
    loses invitations silently, while carrying on one page too far costs one
    request that answers `[]` - and this is a read.

    The open question is `total` itself. The one populated answer read so far
    reported `total: 0` beside a non-empty `*elements`, so `total` is not the
    authority its name suggests, and this treats that page as the last one. That
    was right for the inbox it came from - it held a single invitation - but if a
    multi-page inbox also answers `total: 0`, the second page is never asked for.
    Settling it needs a populated inbox bigger than one page, which is a read and
    therefore costs nothing but the opportunity.
    """
    if not elements:
        return None
    paging = (
        _first_dict(payload, ("paging",)) or _first_dict(payload.get("data"), ("paging",)) or {}
    )
    start = paging.get("start")
    if not isinstance(start, int) or isinstance(start, bool):
        start = _offset(cursor) if cursor not in (None, "", True) else 0
    following = start + len(elements)
    total = paging.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        return str(following) if following < total else None
    return str(following) if len(elements) >= max(1, int(count or RECEIVED_DEFAULT_COUNT)) else None


def _first(node: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        found = node.get(key) if isinstance(node, dict) else None
        if found not in (None, "", [], {}):
            return found
    return None


def _first_str(node: Any, keys: tuple[str, ...]) -> str | None:
    found = _first(node, keys)
    return found if isinstance(found, str) else None


def _first_dict(node: Any, keys: tuple[str, ...]) -> dict | None:
    found = _first(node, keys)
    return found if isinstance(found, dict) else None


def _unreadable_page() -> UpstreamError:
    return UpstreamError(
        "the received-invitations route answered without an `elements` list, so this is not "
        "an empty inbox - it is an answer this CLI does not recognise, and reporting it as "
        "`no pending invitations` would be a claim about the account that nothing supports. "
        f"The shape that was verified live is recorded in {INVITATION_MANAGER_DOC}; check the "
        "raw response with --raw. Retrying will not change it."
    )


def _dangling(missing: int, total: int) -> UpstreamError:
    """Refs in `*elements` that `included` did not carry.

    `Graph.deref` drops a dangling reference so a caller can iterate without a
    per-item guard, which is right for a feed - one unreadable post among ten is
    a gap in a page nobody promised was complete. It is wrong here: the count of
    pending invitations is the answer, so a silently shorter list is a wrong
    answer that looks like a right one, and an inbox reported as two when it
    holds three is the same defect as reporting it empty.
    """
    return UpstreamError(
        f"the received-invitations route listed {total} invitation(s) but {missing} of them "
        "were not in the response body, so this page cannot be reported without undercounting "
        "the inbox. Nothing is wrong with the account - this is a response shape this CLI does "
        "not fully understand. Check it with --raw."
    )


def _unprojectable(unread: int, total: int) -> UpstreamError:
    return UpstreamError(
        f"{unread} of {total} received invitations could not be read. This is a defect in "
        "this parser, not an answer about your account: the projection is pinned against a "
        "single real invitation, which was enough to correct it and is not enough to cover "
        f"every kind of row this page can carry ({INVITATION_MANAGER_DOC}), so a spelling "
        "here can still be wrong. It is reported rather than dropped because a "
        "list with the unreadable rows removed is indistinguishable from having fewer "
        "invitations, and that is the failure an agent cannot detect. Read the real shape "
        "with --raw and fix the projection. Retrying will produce the same answer."
    )


# -------------------------------------------------------------------- the urn

_WANTED = (
    "an invitation is addressed by the target's member urn (`urn:li:fsd_profile:<id>`), "
    "which `linkedin profile get <public-id-or-url>` reports as `profile_urn`"
)

_NO_UNDO = (
    "This CLI has no verb that takes an invitation back, and cannot have one - withdrawing "
    "has moved to LinkedIn's server-driven UI, outside anything this transport speaks "
    "(docs/sdui-migration.md) - so the wrong urn here is not something it can clean up"
)


def profile_urn(value: Any) -> str:
    """Reduce `value` to a member urn, or refuse it and say why.

    Deliberately strict. `profile.resolve_public_id` is lenient because it feeds
    a *read*, where naming the wrong person costs a wasted request; this feeds a
    write that appears in a stranger's notifications and that nothing here can
    withdraw.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.startswith(PROFILE_URN_PREFIX) and _is_member_id(
        candidate[len(PROFILE_URN_PREFIX) :]
    ):
        return candidate

    if candidate.startswith(URN_PREFIX):
        raise ValueError(
            f"{value!r} is not a member urn: {_WANTED}. LinkedIn's urn types are not "
            f"interchangeable and this one is not converted for you. {_NO_UNDO}."
        )
    raise ValueError(
        f"{value!r} is a name, not a urn: {_WANTED}. A public id or a profile URL has to be "
        f"resolved first, because the id in a profile URL is not the id this write takes. "
        f"{_NO_UNDO}."
    )


def _is_member_id(text: str) -> bool:
    """Whether what follows the prefix can be a member id.

    The ids are base64url of the member's internal id, so the alphabet is fixed.
    Checking it is what keeps a tuple urn - `urn:li:fsd_profile:` followed by
    punctuation - from being accepted and pasted into a request body.
    """
    return bool(text) and all(ch.isalnum() or ch in "-_" for ch in text)


# ------------------------------------------------------------- postconditions


def _confirm_sent(result: Any, target: str) -> None:
    """Refuse to call an invitation sent on a response that proves nothing.

    Only the *request* was captured, so no success shape is pinned - but four
    answers a naive reader reports as a delivered invitation are refused here.
    An empty body is what `transport.parse` hands back for a 200 with nothing in
    it; a decorated create can answer 200 with the failure in the body, which
    `transport.raise_for_status` never sees; the refusal may be parked under
    either the top level or `data`; and a result carrying no created urn at all
    establishes nothing.
    """
    if not isinstance(result, dict) or not result:
        raise _unconfirmed(target, "the response was empty")

    errors = _errors(result)
    if errors:
        detail = _error_text(errors)
        if _is_quota_refusal(detail):
            raise _quota(target, detail)
        raise _unconfirmed(target, f"LinkedIn reported {detail}")

    # Positive evidence first, and it wins. The endpoint is
    # `verifyQuotaAndCreateV2`, so a *successful* result may well report on the
    # quota it just checked - "3 invitations remaining" is a string the refusal
    # detector below would match. A named invitation urn is a fact about what was
    # created; a matched phrase is a guess about what a refusal reads like, and
    # the fact has to beat the guess or every success gets reported as a refusal.
    if _created_urn(result):
        return

    # Scanned over `data` only, never `included`. The decoration is
    # `...WithInvitee`, so `included` carries the *target's own profile* - and a
    # headline is free text that could contain any of these words.
    refusal = _quota_sign_in(result.get("data"))
    if refusal:
        raise _quota(target, refusal)

    raise _unconfirmed(target, "the response named no invitation urn")


def _created_urn(result: Any) -> str | None:
    """The urn of the invitation the create answered with.

    Only the request was captured, so every spelling a decorated RestLi create
    uses is tried rather than pinning one that may not be in play. `included` is
    filtered by `$type` because the invitee's profile is decorated into the same
    response - taking the first urn found there would report every refusal as a
    sent invitation, using the target's own urn as the evidence.
    """
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        for key in ("*value", "entityUrn", "urn"):
            found = data.get(key)
            if isinstance(found, str) and found.startswith("urn:li:"):
                return found

    included = result.get("included") if isinstance(result, dict) else None
    for entry in included if isinstance(included, list) else []:
        if not isinstance(entry, dict) or "Invitation" not in str(entry.get("$type") or ""):
            continue
        found = entry.get("entityUrn")
        if isinstance(found, str) and found.startswith("urn:li:"):
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


# --------------------------------------------------------------- the quota


def _is_quota_refusal(text: Any) -> bool:
    """Whether this text is LinkedIn saying the account is out of invitations.

    The action spelling goes first, for the reason `_ACTION_WORDS` exists. What
    is left is searched for phrases a quota refusal uses; anything unrecognised
    is *not* claimed to be a quota, because the fallback - "sent but not
    confirmed" - is an honest answer and a wrong quota verdict is not.
    """
    haystack = str(text or "").lower()
    for word in _ACTION_WORDS:
        haystack = haystack.replace(word, " ")
    return any(sign in haystack for sign in _QUOTA_SIGNS)


def _quota_sign_in(node: Any, depth: int = 0) -> str | None:
    """The first string under `node` that reads as a quota refusal."""
    if depth > 6:
        return None
    if isinstance(node, str):
        return node if _is_quota_refusal(node) else None
    if isinstance(node, dict):
        values = node.values()
    elif isinstance(node, list):
        values = node
    else:
        return None
    for value in values:
        found = _quota_sign_in(value, depth + 1)
        if found:
            return found
    return None


def _quota(target: str, detail: str) -> InvitationQuotaExceeded:
    """LinkedIn refused this against its own count, in words this file classified.

    `detail` is scrubbed here rather than at the call site, the same way
    `surfaces/messaging.py::_refused` and `surfaces/social.py::_refused` do it,
    and for a sharper reason than either: `_quota_sign_in` reaches this
    constructor with an *arbitrary* string harvested from up to six levels of a
    200 body, with no `errors` wrapper required. Nothing upstream scrubbed it -
    `transport.raise_for_status` only ever saw a 4xx - and `cli._report` renders
    this exception's `str()` into `error.message`, which under an agent gateway is
    permanent model context. In the constructor is what makes render.py's
    "unredacted by construction rather than by the caller remembering" true here.

    `target` is deliberately not scrubbed. It is an `urn:li:fsd_profile:ACoAA…`
    member urn, which the scrubber redacts on sight, and it is the only thing in
    this message naming the human the invitation was aimed at - the one fact an
    operator needs to read the account back with. Scrubbing the whole string
    would stop the leak by making the tool unable to say who it just failed to
    invite, which is its own way of lying about what happened.

    `invite`'s 4xx branch passes text `transport.raise_for_status` already
    scrubbed; a second pass over `<redacted>` is a no-op, so that path is left
    alone rather than special-cased.
    """
    return InvitationQuotaExceeded(
        f"LinkedIn refused the invitation to {target} against its own invitation quota: "
        f"{scrub_secrets(detail)}. That count is LinkedIn's, not this CLI's, so it is not "
        "something the local write ledger can be reset to get around. Do not retry it - the "
        "answer will be the same until the quota rolls, and repeating a refused write is what "
        "turns a spent quota into a restricted account. Pending invitations can be withdrawn at "
        f"{SENT_INVITATIONS_URL} to free some of it up, which is a browser action: this CLI "
        "cannot withdraw one."
    )


def _unconfirmed(target: str, detail: str) -> UpstreamError:
    """An invitation whose outcome the response does not establish.

    Reported rather than swallowed, and never as a success. Not marked retryable
    either - `cli._report` renders `.retryable` straight into the envelope an
    agent branches on, and the right next step is to look, because a retry that
    lands is a second invitation in front of the same person.

    `detail` goes through the scrubber for the same reason `_quota`'s does: it is
    LinkedIn's own prose out of a **200** body that nothing upstream saw, and
    this is the branch an *unrecognised* refusal deliberately falls through to -
    so it is where every future error text LinkedIn invents will land, whatever
    that text turns out to quote back. `target` is left alone for the same reason
    it is in `_quota`: it is the only thing here naming who the invitation was
    for, and this CLI has no verb that can take one back.
    """
    return UpstreamError(
        f"the invitation to {target} was sent but not confirmed: {scrub_secrets(detail)}. The "
        f"request reached LinkedIn, so it may already be in front of them - open "
        f"{SENT_INVITATIONS_URL} and look before doing anything else. Do not retry it: the "
        "captured payload carries no dedupe token, so a second attempt is a second invitation "
        "to a real person, and nothing here can take either of them back."
    )
