"""Reactions and comments: the two social writes, from captured traffic.

Every request body below is a transcription of real traffic recorded on
a live run, reproduced field for field. Nothing here is inferred - which is the
same standard `surfaces/messaging.py` holds its `createMessage` payload to, and
for the same reason: a guessed body comes back as a bare `400` with no
explanation, and an agent cannot tell a guess from a fact.

Three things about this surface drive the code:

* **Like and unlike are two different `queryId`s.** They are one endpoint with
  two content hashes, and only the like carries `entity.reactionType`. So they
  are two entries in `QUERY_IDS`, not one call with a flag - and like every
  other queryId in this package they rotate on LinkedIn's deploys, which is why
  they sit here with a `LINKEDIN_QUERY_ID_<NAME>` override rather than at the
  call site.
* **Commenting is not GraphQL at all.** It is a decorated RestLi create against
  `voyagerSocialDashNormComments`, so none of the queryId machinery applies to
  it and it cannot go stale the same way.
* **These take `urn:li:activity:<id>`, and a post's URL shows
  `urn:li:share:<id>`.** Two different ids for the same post, neither derivable
  from the other, so `activity_urn` refuses a share urn instead of guessing.

`delete_comment` is the one thing here that was **not** learned by interception.
It was verified live against a throwaway post this CLI had
published seconds earlier, which is the only situation in which probing a write
is legitimate - our own object, "gone" as the desired end state either way, an
unconditional cleanup, and an independent read-back as the oracle. The full
argument, and the two false starts it cost, are in docs/write-payloads.md under
"Delete a comment". Two of its findings are load-bearing below:

* **The urn is doubled on the way out and must not be on the way in.** LinkedIn
  answers a create with `urn:li:fsd_normComment:urn:li:fsd_comment:(...)` and
  `comment` reports exactly that, so `comment_urn` unwraps it; the doubled form
  sent as a path key is a 400.
* **The collections are asymmetric.** Create *and* delete go to
  `voyagerSocialDashNormComments`; the entity reads back from
  `voyagerSocialDashComments`. Deletes sent to the read collection are a 400,
  which is what made the second probe round look like a missing route.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

from ..transport import UpstreamError, scrub_secrets
from .feed import WEB_BASE

# Verified live by `tools/capture_payloads.py`. Override one with
# LINKEDIN_QUERY_ID_<KEY.upper()> when LinkedIn rotates it, rather than waiting
# for a release.
QUERY_IDS = {
    "react": "voyagerSocialDashReactions.b731222600772fd42464c0fe19bd722b",
    "unreact": "voyagerSocialDashReactions.f68b48ae5bc0085d7a45c7003b772a39",
}

# The reaction mutation is issued through the generic GraphQL executor, so the
# queryId appears twice per call - once in the query string, once in the body -
# and LinkedIn rejects the request if the two disagree.
GRAPHQL_PATH = "graphql?action=execute&queryId={query_id}"

COMMENTS_PATH = (
    "voyagerSocialDashNormComments"
    "?decorationId=com.linkedin.voyager.dash.deco.social.NormComment-43"
)

# Same collection as the create - not `voyagerSocialDashComments`, which is where
# the entity reads back from and where round two of the probe sent its deletes.
# No decoration: a DELETE returns no entity for one to apply to.
DELETE_COMMENT_PATH = "voyagerSocialDashNormComments/{key}"

TEXT_TYPE = "com.linkedin.voyager.dash.common.text.TextViewModel"

ACTIVITY_PREFIX = "urn:li:activity:"

COMMENT_PREFIX = "urn:li:fsd_comment:"

# The wrapper LinkedIn puts around a comment urn when it answers a create, and
# therefore the wrapper on everything `comment` reports.
NORM_COMMENT_PREFIX = "urn:li:fsd_normComment:"

# The only shape verified against the delete route: the comment id and the
# activity it hangs off, in a RestLi tuple. Anchored and strict on both slots
# because this is the string that decides which comment disappears.
COMMENT_URN = re.compile(rf"{re.escape(COMMENT_PREFIX)}\((\d+),({re.escape(ACTIVITY_PREFIX)}\d+)\)")

# `LIKE` is the value the a live run capture carried. The rest are the enum the
# web client sends and are UNVERIFIED here - but they are enumerated rather than
# passed through, so a typo is refused locally instead of arriving as a bare 400
# that says nothing about which field LinkedIn objected to.
REACTION_TYPES = ("LIKE", "PRAISE", "EMPATHY", "INTEREST", "APPRECIATION", "ENTERTAINMENT")


def query_id(name: str) -> str:
    return os.environ.get(f"LINKEDIN_QUERY_ID_{name.upper()}") or QUERY_IDS[name]


def react(client: Any, urn: str, reaction: str = "LIKE", dry_run: bool = False) -> dict:
    """Add a reaction to a post."""
    thread = activity_urn(urn)
    kind = _reaction(reaction)
    body = {
        "variables": {"entity": {"reactionType": kind}, "threadUrn": thread},
        "queryId": query_id("react"),
        "includeWebMetadata": True,
    }
    result = client.post(GRAPHQL_PATH.format(query_id=query_id("react")), body, dry_run=dry_run)
    if dry_run:
        return result
    _confirm(result, f"the {kind} reaction on {thread}", thread)
    return {
        "activity_urn": thread,
        "reaction": kind,
        "reacted": True,
        "url": f"{WEB_BASE}/feed/update/{thread}",
    }


def unreact(client: Any, urn: str, dry_run: bool = False) -> dict:
    """Remove this account's reaction from a post.

    A separate queryId from `react`, and deliberately no `entity`: the capture
    has none, and sending one is not a harmless extra field - it is the other
    mutation's payload.
    """
    thread = activity_urn(urn)
    body = {
        "variables": {"threadUrn": thread},
        "queryId": query_id("unreact"),
        "includeWebMetadata": True,
    }
    result = client.post(GRAPHQL_PATH.format(query_id=query_id("unreact")), body, dry_run=dry_run)
    if dry_run:
        return result
    _confirm(result, f"removing the reaction on {thread}", thread)
    return {
        "activity_urn": thread,
        "reacted": False,
        "url": f"{WEB_BASE}/feed/update/{thread}",
    }


def comment(client: Any, urn: str, text: str, dry_run: bool = False) -> dict:
    """Post a top-level comment on a post.

    Unlike the reactions above this is a decorated RestLi create, so the answer
    is the created entity - and its urn is the postcondition. Reporting a
    comment with a null `comment_urn` would tell an agent the comment is posted
    and leave it no way to find out otherwise.
    """
    thread = activity_urn(urn)
    if not text or not text.strip():
        raise ValueError("comment text is empty")

    body = {
        "commentary": {"text": text, "attributesV2": [], "$type": TEXT_TYPE},
        "threadUrn": thread,
    }
    result = client.post(COMMENTS_PATH, body, dry_run=dry_run)
    if dry_run:
        return result

    what = f"the comment on {thread}"
    _confirm(result, what, thread)
    created = _created_urn(result)
    if not created:
        raise _unconfirmed(what, "the response named no comment urn", thread)
    return {
        "comment_urn": created,
        "activity_urn": thread,
        "text": text,
        "url": f"{WEB_BASE}/feed/update/{thread}",
    }


def delete_comment(client: Any, urn: Any, dry_run: bool = False) -> dict:
    """Remove a comment, addressed by its own urn. The inverse of `comment`.

    `cli.cmd_comment` books this as a cleanup, so no spent cap, no cooldown and
    no open breaker can withhold it. "Never refused" is narrower than it sounds
    and `cli._write` spells out the whole of it: the urn is validated here first,
    because the exemption is about this CLI's own limits and not about accepting
    whatever it is handed; `state._guard_cleanup` refuses an undo past
    `cleanup_ceiling` (exit 5); and `state._guard_readable` refuses it outright
    on a ledger that will not parse (exit 9), since the ceiling is counted out of
    that file. A deleted comment cannot be put back.

    **The success shape is an empty 2xx**, which is why nothing below demands a
    urn back the way `comment` does: this route answers with no body at all, so a
    postcondition modelled on the create would report every delete that worked as
    unconfirmed. That is not a status code being taken as evidence - the status
    is what the *request* said about itself. The verdict came from
    reading the comment back out of `voyagerSocialDashComments` afterwards and
    getting a 404, and round one's own oracle answering 400 is what made four
    probes look like a missing route (docs/write-payloads.md). What is still
    refused here is the one failure neither `transport.raise_for_status` nor an
    empty-body check can see: LinkedIn saying no inside a 2xx.

    `client` has no `delete`. The transport builds every call from
    `_request(method, path, body, dry_run)` and exposes only `get` and `post` on
    top of it, and this is the CLI's first DELETE; a `delete` method on
    `browser.BrowserClient` is the better home for it and is a change to a file
    this one does not own. So `_request` is used
    directly - and it should keep being used even once that grows a `delete`,
    because `_request` is the seam `cli._WriteWatch` wraps. A write that reaches
    the wire through a method the watch does not know about never sets
    `attempted_write`, so the ledger slot `cli._write` claimed for it is handed
    straight back and the undo goes out uncounted. Measured, not feared: that is
    exactly what this call did before `_WriteWatch._request` existed.
    """
    key = comment_urn(urn)
    path = DELETE_COMMENT_PATH.format(key=urllib.parse.quote(key, safe=""))
    result = client._request("DELETE", path, None, dry_run)
    if dry_run:
        return result

    _confirm_deleted(result, key)
    return {
        "comment_urn": key,
        # Read out of the key rather than asked for: it is already in there, and
        # `render` leads a block with the subject of the write, which for a
        # comment is the post it sits on (README.md's "output shape" note).
        "activity_urn": _thread_of(key),
        "deleted": True,
        # No `url`, for the reason `posts.delete` withholds one: the comment is
        # gone, and a permalink handed back invites an agent to cite a link as
        # evidence the removal worked.
    }


# -------------------------------------------------------------------- the urn

# Named here rather than inlined three times, because all three refusals have to
# say the same thing: which urn is wanted, and where one comes from.
_WANTED = (
    "these writes address a post by its activity urn (`urn:li:activity:<id>`), which "
    "`linkedin feed list` and `linkedin post get` report as `activity_urn`"
)

_NOT_THE_SAME_ID = (
    "A `urn:li:share:` id and a `urn:li:activity:` id are different ids for the same "
    "post and are not interchangeable - neither can be derived from the other, so "
    "this is not converted for you"
)


def activity_urn(value: Any) -> str:
    """Reduce `value` to the activity urn, or refuse it and say why.

    Deliberately **not** `feed.activity_urn`, which also accepts a bare id and a
    permalink. That tolerance is right for a read and wrong here: a post's share
    id and its activity id are both bare numbers, so a bare id names a post only
    by luck, and acting on the wrong one is a write nobody asked for.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.startswith(ACTIVITY_PREFIX) and candidate[len(ACTIVITY_PREFIX) :].isdigit():
        return candidate

    if "urn:li:share:" in candidate or "urn:li:ugcPost:" in candidate:
        raise ValueError(
            f"{value!r} names a post's content, not its activity: {_WANTED}. {_NOT_THE_SAME_ID}."
        )
    if "://" in candidate or "linkedin.com" in candidate:
        raise ValueError(
            f"{value!r} is a post URL, not a urn: {_WANTED}. The id a post URL shows is "
            f"the `urn:li:share:` one. {_NOT_THE_SAME_ID}."
        )
    raise ValueError(
        f"{value!r} is not an activity urn: {_WANTED}. A bare number is refused too - a "
        f"post's share id and its activity id are both bare numbers, and nothing in the "
        f"id itself says which one you are holding. {_NOT_THE_SAME_ID}."
    )


# The delete's own refusals, and they say the same two things `_WANTED` does:
# which urn is wanted, and where one comes from.
_WANTED_COMMENT = (
    "a comment is addressed by its own urn, `urn:li:fsd_comment:(<comment-id>,"
    "urn:li:activity:<activity-id>)`, which `linkedin comment` reports as `comment_urn` - "
    "wrapped in `urn:li:fsd_normComment:`, which is the form LinkedIn answers with and is "
    "accepted and unwrapped here"
)

_NO_UNDELETE = (
    "A deleted comment cannot be put back by this CLI or by LinkedIn, so the wrong urn here "
    "is not a mistake anything can clean up"
)


def comment_urn(value: Any) -> str:
    """Reduce `value` to the key the delete route takes, or refuse it and say why.

    Two spellings in, one out. LinkedIn answers a create with the inner urn
    wrapped in a second urn type, and `comment` reports exactly that, so the
    obvious copy-paste of this CLI's own output has to work - while the path key
    is the inner urn alone, and the doubled one is a 400 (docs/write-payloads.md).

    Strict on both slots of the tuple, and stricter than `activity_urn` is about
    its neighbours, because the failure modes are not comparable: reacting to
    the wrong post is undone by `unreact`, and deleting the wrong comment is
    undone by nothing. A shape that was never verified against this route is
    refused rather than sent to find out.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.startswith(NORM_COMMENT_PREFIX):
        candidate = candidate[len(NORM_COMMENT_PREFIX) :]
    if COMMENT_URN.fullmatch(candidate):
        return candidate

    if candidate.startswith(COMMENT_PREFIX):
        raise ValueError(
            f"{value!r} is not a comment urn this route can address: {_WANTED_COMMENT}. Both "
            f"halves of the tuple are required - the comment id alone does not say which post "
            f"the comment is on, and that is part of the key. {_NO_UNDELETE}."
        )
    if candidate.startswith(ACTIVITY_PREFIX) or "urn:li:share:" in candidate:
        raise ValueError(
            f"{value!r} names a post, not a comment on one: {_WANTED_COMMENT}. Deleting the "
            f"post is `linkedin post delete`, and it takes every comment on it down too. "
            f"{_NO_UNDELETE}."
        )
    if "://" in candidate or "linkedin.com" in candidate:
        raise ValueError(f"{value!r} is a URL, not a urn: {_WANTED_COMMENT}. {_NO_UNDELETE}.")
    if candidate.startswith("urn:li:"):
        raise ValueError(
            f"{value!r} is not a comment urn: {_WANTED_COMMENT}. LinkedIn's urn types are not "
            f"interchangeable and this one is not converted for you. {_NO_UNDELETE}."
        )
    raise ValueError(
        f"{value!r} is not a comment urn: {_WANTED_COMMENT}. A bare comment id is refused "
        f"too - the key carries the activity the comment hangs off as well, and nothing in a "
        f"comment id says which post that is. {_NO_UNDELETE}."
    )


def _thread_of(key: str) -> str:
    """The activity a comment urn already names. Only ever called on a validated key."""
    return COMMENT_URN.fullmatch(key).group(2)


# ------------------------------------------------------------- postconditions


def _reaction(value: Any) -> str:
    """Normalize a reaction to the wire spelling, or refuse it here.

    Agents type `like`; the enum is `LIKE`, and LinkedIn answers a lowercase one
    with a bare 400 that names no field.
    """
    kind = str(value or "").strip().upper()
    if kind not in REACTION_TYPES:
        raise ValueError(
            f"{value!r} is not a LinkedIn reaction; expected one of {', '.join(REACTION_TYPES)}"
        )
    return kind


def _confirm(result: Any, what: str, thread: str) -> None:
    """Refuse to call a write a success on a response that proves nothing.

    Three shapes reach here that a naive writer reports as success. An empty body
    is what `transport.parse` hands back for a 200 with nothing in it, and the
    GraphQL executor answers **200 with the failure in the body**, which
    `transport.raise_for_status` never sees - parked under either the top level
    or `data`. Either one returned as `{"reacted": true}` is worse than an error:
    an agent does not retry a success, and the operator finds out when someone
    asks why the comment never appeared.

    Reading only the top level is what this used to do, and it missed the
    placement LinkedIn actually uses: `{"data": {"errors": [...]}}` is a
    non-empty `data`, so a refused react and unreact fell straight through to
    `{"reacted": true}` and a refused comment was reported as an unknown outcome.
    Measured live against a bad `visibilityType`; `surfaces/posts.py`
    carries the same reader for the same reason.
    """
    if not isinstance(result, dict) or not result:
        raise _unconfirmed(what, "the response was empty", thread)

    errors = _errors(result)
    if errors:
        raise _refused(what, _error_text(errors))

    data = result.get("data")
    if not (isinstance(data, dict) and data) and not result.get("included"):
        raise _unconfirmed(what, "the response carried no result body", thread)


def _confirm_deleted(result: Any, key: str) -> None:
    """Refuse to call a delete done on a response that says it was not.

    Deliberately the opposite shape from `_confirm`, and the difference is the
    measurement rather than a preference: an **empty 2xx is the documented
    success** for this route, so the emptiness check that guards every other
    write in this file would refuse every delete that worked. A 4xx never
    arrives - `transport.raise_for_status` turns it into an exception before
    this is reached - so what is left to catch is the one thing neither layer
    sees, an error LinkedIn parked inside a 2xx body, read from both places the
    executor parks one.
    """
    errors = _errors(result) if isinstance(result, dict) else None
    if errors:
        raise _refused(f"deleting the comment {key}", _error_text(errors))


def _errors(result: dict) -> list | None:
    """The executor's error list, from either place it parks one.

    A duplicate of `posts._errors` on purpose, not an oversight. Both surfaces
    drive the same `graphql?action=execute` endpoint and so have the same
    exposure, but `posts` already imports `activity_urn` from here - the shared
    home is this module, and moving `posts` onto it means editing `posts`, which
    is a separate change. Keep the two in step until then.
    """
    for node in (result, result.get("data")):
        if isinstance(node, dict):
            found = node.get("errors")
            if isinstance(found, list) and found:
                return found
    return None


def _unconfirmed(what: str, detail: str, thread: str) -> UpstreamError:
    """A delivered write whose outcome the response does not establish.

    Not silently swallowed and not reported as done: the request *was* sent, so
    it may have landed, and the only safe next step is to look rather than to
    retry.

    `detail` goes through the scrubber for the same reason `_refused`'s does.
    Every detail that reaches here today is a literal written in this file, so
    there is no live path leaking through it - it is scrubbed anyway, because
    `render.py` is explicit that `error.message` is the one key it does **not**
    redact and that a surface splicing upstream text has to scrub it here. A
    constructor that trusts its argument is one caller away from being the
    counterexample, and there is no second layer behind this one.
    """
    return UpstreamError(
        f"{what} was sent but not confirmed: {scrub_secrets(detail)}. The request reached "
        f'LinkedIn, so it may have been applied - read the post back with `linkedin post get "{thread}"` '
        "before retrying, because a blind retry can apply it twice."
    )


def _refused(what: str, detail: str) -> UpstreamError:
    """LinkedIn said no, in a 200.

    Distinct from `_unconfirmed` on purpose, and the same distinction
    `posts._refused` draws. A GraphQL `errors` array is a decision: the mutation
    was rejected and nothing was applied, so there is nothing to read back and
    nothing to undo. `_unconfirmed` says the opposite - that the write may have
    landed - and an agent acts on the two completely differently; answering
    "unknown" to a plain "no" spends a read on a comment that does not exist.
    Not retryable: `cli._report` renders `.retryable` into the envelope an agent
    branches on, and this request is refused the same way until it changes.

    `detail` is scrubbed here rather than at the call site. It is LinkedIn's own
    prose out of a **200** body, so `transport.raise_for_status` - which scrubs
    the body *it* splices - never saw it; and `cli._report` renders this
    exception's `str()` onto stderr, which under an agent gateway is permanent model
    context. In the constructor is what makes the guarantee hold by construction
    instead of by the next caller remembering. `what` is left alone: it is built
    here out of the caller's own arguments and it is the only thing that says
    which post and which write were turned down.
    """
    return UpstreamError(f"LinkedIn refused {what}: {scrub_secrets(detail)}. Nothing was applied.")


def _error_text(errors: list) -> str:
    for entry in errors:
        if isinstance(entry, dict) and entry.get("message"):
            return str(entry["message"])
    return str(errors[0])


def _created_urn(result: Any) -> str | None:
    """The urn of the entity a create answered with.

    Only the *request* was captured, so every spelling LinkedIn uses for a
    created entity is tried rather than pinning one that may not be the one in
    play - `*value` is the normalized reference form, `entityUrn`/`urn` the
    inline ones, and `included` is where a decorated create parks the entity
    when `data` holds only the reference.
    """
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        for key in ("*value", "entityUrn", "urn"):
            found = data.get(key)
            if isinstance(found, str) and found.startswith("urn:li:"):
                return found

    included = result.get("included") if isinstance(result, dict) else None
    for entry in included if isinstance(included, list) else []:
        if not isinstance(entry, dict) or "Comment" not in str(entry.get("$type") or ""):
            continue
        found = entry.get("entityUrn")
        if isinstance(found, str) and found.startswith("urn:li:"):
            return found
    return None
