"""The feed surface: the chronological feed, and one post by URN.

A `UpdateV2` is a rendering tree - actor components, tracking tokens, save
actions, lego tokens - and almost none of it is actionable. The projection keeps
the ten fields the output contract names and drops ~10 KB per post.

Three things about the real payload shape the code:

* **`actor.urn` is a `urn:li:member:` id, which no write in this CLI accepts.**
  The usable handle is the `fsd_profile` urn, and it is only reachable by
  resolving the `*miniProfile` reference hanging off the actor's name or image
  attributes. An agent handed `urn:li:member:100000010` can read a post and never
  act on its author, so the projection resolves rather than copies.
* **No update carries a creation timestamp.** `actor.subDescription` is the
  rendered string "1d •", which is a locale artifact, not data. The timestamp is
  instead recovered from the activity id itself: LinkedIn's ids are Snowflake-
  shaped and the top 41 bits are epoch milliseconds. That is verified against
  this capture - every one of the 11 ids decodes to within three minutes of the
  response's own `paginationToken` epoch - not assumed.
* **`socialContent.shareUrl` carries `rcm=<the reader's member id>`** plus
  `utm_*`. Emitting it would leak the operator's own identifier into every
  projection, so the permalink is rebuilt from the activity urn instead.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any

from ..graph import Graph
from ..transport import NotFound

# Pinned to the request that was captured live; `commentsCount`/`likesCount` at 0
# ask LinkedIn not to inline comment and reaction bodies we would only discard.
PATH = "feed/updatesV2"

BASE_QUERY = "commentsCount=0&count={count}&likesCount=0&moduleKey=home-feed%3Adesktop&q=chronFeed"

WEB_BASE = "https://www.linkedin.com"

ACTIVITY_PREFIX = "urn:li:activity:"

UPDATE_TYPE = "com.linkedin.voyager.feed.render.UpdateV2"

# LinkedIn ids are Snowflake-shaped: the top 41 bits are epoch milliseconds and
# the low 22 are worker/sequence bits.
_TIMESTAMP_SHIFT = 22


def list_feed(
    client: Any,
    count: int = 10,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, bool]:
    """Project one page of the chronological feed: (items, next_cursor, has_more)."""
    graph = Graph(client.get(_path(count, cursor)))
    updates = graph.deref(graph.data, "elements")
    updates = updates if isinstance(updates, list) else []

    items = [project_update(graph, update) for update in updates if isinstance(update, dict)]
    next_cursor, has_more = _page(graph)
    return items, next_cursor, has_more


def get_post(client: Any, who: str) -> dict:
    """Fetch and project a single post, by activity urn, bare id or permalink.

    Verified live against the real account: the
    `q=backendUrnOrNss` finder answered 200 with a real post
    (docs/incidents.md). `activity_urn` is parsed and
    validated locally, so a malformed argument fails here rather than at
    LinkedIn, and the projection is shared with the feed listing.

    That same run found the failure mode this function now refuses. An entity
    that is no longer readable does not 404 - it answers 200 with an UpdateV2
    *shell*, the entity present and its content gone - and every field the
    projection can still fill for a shell (`activity_urn`, `posted_at`, `url`)
    is computed from the urn that was sent, never read back from LinkedIn. So a
    projected shell is the request echoed back wearing the shape of an answer,
    and `post get` is exactly the oracle `post delete` sends a caller to when its
    own outcome is unclear. An oracle that reports "still there" for a post that
    is gone cannot confirm anything, so this raises instead.
    """
    urn = activity_urn(who)
    encoded = urllib.parse.quote(urn, safe="")
    graph = Graph(client.get(f"{PATH}?q=backendUrnOrNss&urnOrNss={encoded}"))

    updates = graph.deref(graph.data, "elements")
    if not isinstance(updates, list) or not updates:
        # `included` is the fallback for a single-entity response that answered
        # without the collection wrapper.
        updates = graph.by_type("UpdateV2")
    if not updates:
        raise NotFound(f"no post at {urn}")

    update = updates[0]
    if not _carries_content(graph, update):
        raise _unreadable(urn)
    return project_update(graph, update)


def activity_urn(value: str) -> str:
    """Reduce an activity urn, a bare numeric id or a permalink to the urn."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected an activity urn, id or post URL")

    candidate = urllib.parse.unquote(value.strip())
    if ACTIVITY_PREFIX in candidate:
        tail = candidate.split(ACTIVITY_PREFIX, 1)[1]
        # A permalink continues past the urn (`/`, `?`, `,` inside an updateV2
        # tuple); the id itself is digits only.
        digits = ""
        for ch in tail:
            if not ch.isdigit():
                break
            digits += ch
        if digits:
            return ACTIVITY_PREFIX + digits
    elif candidate.isdigit():
        return ACTIVITY_PREFIX + candidate

    raise ValueError(f"{value!r} is not a LinkedIn post - expected an activity urn, id or URL")


def posted_at(urn: str | None) -> str | None:
    """Recover a post's creation time from its activity id. See the docstring."""
    if not isinstance(urn, str):
        return None
    tail = urn.rsplit(":", 1)[-1]
    if not tail.isdigit():
        return None
    try:
        moment = datetime.fromtimestamp((int(tail) >> _TIMESTAMP_SHIFT) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def project_update(graph: Graph, update: dict) -> dict:
    """Reduce one UpdateV2 to the post projection."""
    metadata = update.get("updateMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    urn = metadata.get("urn") or _urn_from_entity(update.get("entityUrn"))
    counts = _counts(graph, update)

    return {
        "activity_urn": urn,
        # The share is the content the activity points at; `post delete` takes
        # the activity, so both are named rather than collapsed into one `urn`.
        "content_urn": metadata.get("shareUrn"),
        "author": _author(graph, update.get("actor")),
        "text": _commentary(update),
        "posted_at": posted_at(urn),
        "reactions": _int(counts.get("numLikes")),
        "comments": _int(counts.get("numComments")),
        "reshares": _int(counts.get("numShares")),
        "url": f"{WEB_BASE}/feed/update/{urn}" if urn else None,
    }


# --------------------------------------------------------------------- internals


def _path(count: int, cursor: str | None) -> str:
    query = BASE_QUERY.format(count=int(count))
    if cursor not in (None, ""):
        start = str(cursor)
        if not start.isdigit():
            raise ValueError(
                f"--cursor must be the numeric offset this command handed back, got {cursor!r}"
            )
        query += f"&start={start}"
    return f"{PATH}?{query}"


def _page(graph: Graph) -> tuple[str | None, bool]:
    """Feed paging is an offset, not a token, so it is arithmetic on `paging`."""
    paging = graph.data.get("paging")
    if not isinstance(paging, dict):
        return None, False
    start, count, total = (_int(paging.get(k)) for k in ("start", "count", "total"))
    nxt = start + count
    return (str(nxt), True) if count and nxt < total else (None, False)


def _carries_content(graph: Graph, update: Any) -> bool:
    """Did LinkedIn return anything *about* this post, or only its identity?

    Measured against the capture: all 10 feed updates in it carry an actor, an
    `updateMetadata.shareUrn` and a `*socialDetail` ref, so any one of those is
    present on a readable post. Any *one* of them counts here rather than all of
    them, because absence has to mean "the entity is hollow", not "this post is
    shaped unusually" - a post with no commentary at all (a bare image or an
    article share) is an ordinary post that this capture happens not to contain,
    and refusing it would be worse than the bug being fixed.

    `entityUrn` and `dashEntityUrn` are deliberately not evidence: both are the
    tuple built around the urn the caller asked for, so they are present on a
    shell too.

    **`content` is not evidence either, and that one was learned the hard way.**
    A deleted post does not answer with `content: null`. LinkedIn fills it with a
    tombstone - an `EntityComponent` whose title reads "This post cannot be
    displayed" beside a warning icon - and fills `updateMetadata` with the urn
    that was asked for, while leaving `actor`, `commentary` and `socialDetail`
    null and omitting `shareUrn`. Measured live against a post this
    CLI had just deleted, after the suite had been green on a hand-built fixture
    that guessed the shell was empty. So `content` is not merely weak evidence:
    on the one case this function exists to catch, it is present *because* the
    post is gone, and reading it as proof the post is readable inverts the check.

    Nothing is lost by dropping it. The measurement above is that a live update
    carries an actor, and every readable post has an author.
    """
    if not isinstance(update, dict):
        return False
    metadata = update.get("updateMetadata")
    # `shareUrn` specifically, not the presence of `updateMetadata`: the tombstone
    # carries the block, holding `urn` and `shareMediaUrn` - both echoes of the
    # request - and no `shareUrn`.
    if isinstance(metadata, dict) and metadata.get("shareUrn"):
        return True
    if isinstance(update.get("actor"), dict) and update["actor"]:
        return True
    if _commentary(update):
        return True
    return bool(_counts(graph, update))


def _unreadable(urn: str) -> NotFound:
    """A post that answered 200 with nothing in it. Exit 4, and no guess as to why.

    Deleted by its author, hidden because the author restricted or blocked this
    account, or on a page this session cannot see: all three come back as the
    same empty shell. The difference lives in LinkedIn's authorization layer and
    is nowhere in the body, so this names the possibilities instead of picking
    one. It costs nothing to be honest here - the next step is identical in every
    case, since there is no post to read from this account either way.
    """
    return NotFound(
        f"{urn} came back empty: LinkedIn answered with the update entity but no author, "
        "no text and no counts. That is what a deleted post looks like, and equally what a "
        "post that is not visible to this account looks like - the response does not "
        "distinguish the two, so this does not claim one. Nothing about the post was read "
        "back, so do not treat its permalink as evidence that it exists. If this is the "
        "read-back after a `post delete`, this is the confirmation: the same call returned "
        "the post before the delete and returns nothing now."
    )


def _urn_from_entity(entity_urn: Any) -> str | None:
    """`urn:li:fs_updateV2:(urn:li:activity:123,MAIN_FEED,…)` -> the activity urn."""
    if not isinstance(entity_urn, str) or ACTIVITY_PREFIX not in entity_urn:
        return None
    try:
        return activity_urn(entity_urn)
    except ValueError:
        return None


def _author(graph: Graph, actor: Any) -> dict:
    empty = {"name": None, "headline": None, "urn": None, "public_id": None}
    if not isinstance(actor, dict):
        return empty

    mini = _mini_entity(graph, actor)
    return {
        "name": _text(actor.get("name")),
        # The actor's rendered description is what the feed shows; `occupation`
        # on the MiniProfile is the same string when the render omits it.
        "headline": _text(actor.get("description")) or mini.get("occupation"),
        # `fsd_profile:…` for a member, `fsd_company:…` for a page. Both are
        # prefix-identifiable, which is the point: a null urn would tell an agent
        # nothing at all, and 3 of the 10 posts in the capture are page posts.
        "urn": mini.get("dashEntityUrn") or mini.get("dashCompanyUrn"),
        "public_id": mini.get("publicIdentifier") or mini.get("universalName"),
    }


def _mini_entity(graph: Graph, actor: dict) -> dict:
    """Find the actor's MiniProfile or MiniCompany, wherever the ref hangs.

    The reference sits on a text attribute of the name, or on an image attribute,
    depending on how the actor is rendered, so both are tried rather than one
    being assumed.
    """
    for key in ("name", "image", "supplementaryActorInfo"):
        block = actor.get(key)
        if not isinstance(block, dict):
            continue
        for attribute in block.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            for ref in ("miniProfile", "miniCompany"):
                found = graph.deref(attribute, ref)
                if isinstance(found, dict):
                    return found
    return {}


def _commentary(update: dict) -> str | None:
    commentary = update.get("commentary")
    if not isinstance(commentary, dict):
        return None
    return _text(commentary.get("text"))


def _counts(graph: Graph, update: dict) -> dict:
    detail = graph.deref(update, "socialDetail")
    if not isinstance(detail, dict):
        return {}
    counts = graph.deref(detail, "totalSocialActivityCounts")
    return counts if isinstance(counts, dict) else {}


def _text(node: Any) -> str | None:
    """A TextViewModel keeps the plain string in `text`; the rest is styling."""
    if isinstance(node, dict):
        value = node.get("text")
        return value or None if isinstance(value, str) else None
    return node or None if isinstance(node, str) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
