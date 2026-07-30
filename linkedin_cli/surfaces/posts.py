"""Publishing a post, and taking one down again.

Both request bodies are transcriptions of traffic captured by
driving the real controls, pausing each request with CDP `Fetch` at
`requestStage: Request`, recording `request.postData` and then aborting it - so
nothing was published and nothing was deleted to learn them. They are reproduced
field for field, in the captured order, to the same standard `surfaces/social.py`
holds its reaction payloads to: a guessed body comes back as a bare `400` that
names no field, and an agent cannot tell a guess from a fact.

Create and delete are one endpoint under two content hashes - the same shape
`react` and `unreact` have - so they are two entries in `QUERY_IDS` rather than
one call with a flag.

Three properties of the create drive everything below.

* **There is no dedupe token, and nothing to read back.** The captured share
  payload carries nothing of the kind, and nothing in it is derived from the text
  either. `messages reply` *is* protected against an identical re-run - it checks
  the newest page of the thread it is replying into before it sends - and that
  contrast is now real, where an earlier docstring here had it backwards. It does
  not transfer: a share has no thread to read back and no token to match, so two
  identical requests are two public posts. This surface is therefore deliberately
  *single-shot*: one `client.post` on every path, no retry loop, and every
  failure it raises is non-retryable and says so in words. That constraint has to
  survive people editing this file, which is why `create` has no attempt counter
  to increment and no `retries` parameter to pass.
* **The response shape was never captured** - only the request was, because the
  request is where the capture aborted. So the reader is liberal about *where*
  the new post's urn sits and strict about there being one. A create reported as
  a success with a null urn is worse than an error: an agent does not look again
  at a success, and the operator finds out when someone asks why the post never
  appeared.
* **`visibilityDataUnion.visibilityType` is who sees it.** An unrecognised value
  is refused here rather than defaulted, because defaulting silently is how a
  post meant for connections goes out to everyone.

`delete` is the inverse, and three things about it are different in kind.

* **It is the undo, so nothing this CLI enforces may withhold it.** `cli` books
  it with `cleanup=True`; a run that publishes and then trips its own daily cap
  would otherwise leave a live public post with no CLI way to remove it. The
  create must *not* inherit that exemption, which is why `CLEANUP_ACTIONS` is
  keyed verb -> actions rather than by verb.
* **It addresses the post by the strict activity urn, `social.activity_urn`.**
  Not `feed.activity_urn`, which also accepts a bare number: a post's share id
  and its activity id are both bare numbers, so a bare number names a post only
  by luck. Acting on the wrong post by luck is survivable for a reaction and is
  not survivable here.
* **The `updateUrn` tuple goes out as a literal JSON string.** `restli.encode`
  percent-encodes `(`, `)` and `,` because a *query string* would otherwise
  parse them as nested structure; this is a JSON body, where they are ordinary
  characters, and the capture carries them raw. Encoding it would send a body
  LinkedIn was never observed accepting.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..transport import OutcomeUnknown, UpstreamError, scrub_secrets
from .feed import WEB_BASE

# Deliberately re-exported rather than re-implemented. `delete` needs exactly the
# refusals `react` and `comment` already make, and a second copy of a urn
# validator is a copy that drifts - on the one verb where drifting means taking
# down a post nobody named.
from .social import activity_urn

# Captured live by `tools/capture_payloads.py` through CDP `Fetch`; both
# requests were aborted, so nothing was published and nothing was deleted.
# Override one with LINKEDIN_QUERY_ID_<KEY.upper()> when LinkedIn rotates the
# hash, rather than waiting for a release. The keys are `post_create` and
# `post_delete` rather than `create`/`delete` because `doctor` collects every
# surface's ids into one dict keyed by name.
QUERY_IDS = {
    "post_create": "voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf",
    "post_delete": "voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496",
}

# The queryId travels twice per call - once in the query string, once in the
# body - and LinkedIn rejects the request outright if the two disagree.
GRAPHQL_PATH = "graphql?action=execute&queryId={query_id}"

# `ANYONE` is the value the capture carried. `CONNECTIONS` is the other value
# the web client sends and is UNVERIFIED here - but the pair is enumerated
# rather than passed through, so a typo is refused locally instead of arriving
# as a bare 400 that says nothing about which field LinkedIn objected to.
# Live measurement, not inference: `CONNECTIONS` is rejected by LinkedIn with
# "Invalid input for enum 'dash_contentcreation_VisibilityType'. No value found
# for name 'CONNECTIONS'". Only the value the capture carried is known good, and
# a guessed second one is exactly what this project does not ship.
VISIBILITY_TYPES = ("ANYONE",)

DEFAULT_VISIBILITY = "ANYONE"

# Two knobs the capture pinned that this CLI does not expose. `ALL` is the only
# `allowedCommentersScope` that was observed, and `PUBLISHED` the only lifecycle
# state - a `DRAFT` spelling would be a guess, and a guessed lifecycle is the one
# field whose failure mode is publishing something meant to stay unpublished.
COMMENTERS_SCOPE = "ALL"
LIFECYCLE_STATE = "PUBLISHED"
ORIGIN = "FEED"

# What `delete` addresses a post by: an activity urn wrapped in a fixed
# five-field tuple, transcribed from the capture. The four trailing fields are
# constants there - `FEED_DETAIL` is the surface the web client deleted from,
# and `false` is a bare token rather than a quoted string. None of them is
# derived from the post, so the tuple is buildable from what `feed list` and
# `post get` already hand back.
UPDATE_URN = "urn:li:fsd_update:({activity},FEED_DETAIL,EMPTY,DEFAULT,false)"

# The urns a created post can be identified by, in the order they are preferred.
# An activity urn is the one every write in this CLI accepts; a share urn names
# the same post and is *not* convertible to it, so it is reported under its own
# key rather than passed off as one.
_POST_URN = re.compile(r"urn:li:(activity|share|ugcPost):\d+")

_URN_PRIORITY = ("activity", "share", "ugcPost")


def query_id(name: str) -> str:
    return os.environ.get(f"LINKEDIN_QUERY_ID_{name.upper()}") or QUERY_IDS[name]


def audience(value: Any = None) -> str:
    """Normalize `--visibility` to the wire spelling, or refuse it here.

    `None` means the caller did not ask, which is the captured default. An empty
    or unknown string is refused rather than falling back to it: `--visibility=`
    and `--visibility=PUBLIC` are attempts to restrict the audience, and turning
    either into `ANYONE` publishes to everyone under a flag that said otherwise.
    """
    if value is None or value is True:
        return DEFAULT_VISIBILITY
    kind = str(value).strip().upper()
    if kind not in VISIBILITY_TYPES:
        raise ValueError(
            f"{value!r} is not a LinkedIn post audience; expected one of "
            f"{', '.join(VISIBILITY_TYPES)}. It is not defaulted, because a post meant "
            "for connections that goes out to anyone cannot be un-seen."
        )
    return kind


def create(client: Any, text: Any, visibility: str | None = None, dry_run: bool = False) -> dict:
    """Publish a post. Single-shot by construction - see the module docstring.

    Everything that can be refused without a request is refused before the
    request, so a bad audience or an empty body costs neither a write slot nor a
    post nobody wanted.
    """
    seen_by = audience(visibility)
    body_text = text if isinstance(text, str) else ""
    if not body_text.strip():
        raise ValueError(
            "a post needs text: pass --text=… . An empty share is not refused by "
            "LinkedIn with anything this CLI can explain."
        )

    body = {
        "variables": {
            "post": {
                "allowedCommentersScope": COMMENTERS_SCOPE,
                "intendedShareLifeCycleState": LIFECYCLE_STATE,
                "origin": ORIGIN,
                "visibilityDataUnion": {"visibilityType": seen_by},
                # The text goes out exactly as it was given: what an operator
                # approved in a `--dry-run` preview has to be what is published.
                "commentary": {"text": body_text, "attributesV2": []},
            }
        },
        "queryId": query_id("post_create"),
        "includeWebMetadata": True,
    }

    # The only request in this function, on purpose. Anything that wrapped this
    # line in a loop, or called it again after a failure, would publish twice.
    result = client.post(GRAPHQL_PATH.format(query_id=query_id("post_create")), body, dry_run)
    if dry_run:
        return result

    # `_errors` looks in both places the executor parks a list. Reading only the
    # top level missed the one LinkedIn actually used: a rejected create answers
    # 200 with the errors nested under `data`, so the check fell through, found
    # no urn, and reported "it may already be public" for a post that was never
    # created. Measured live against a bad `visibilityType`.
    errors = _errors(result) if isinstance(result, dict) else None
    if errors:
        raise _refused(_error_text(errors))

    found = _created_urns(result)
    best = next((found[kind] for kind in _URN_PRIORITY if kind in found), None)
    if not best:
        raise _unconfirmed("the response named no post urn")

    return {
        "post_urn": best,
        # The two ids are named separately rather than collapsed. `post delete`,
        # `react` and `comment` all take the activity urn, a share urn cannot be
        # turned into one, and a single `urn` key would hide from a caller
        # holding only the share urn that it has to read the post back before it
        # can act on it. `content_urn` is also the key `feed.project_update`
        # uses, and `render` falls back to it - without that, a create that
        # answered with only a share urn printed a blank first line under
        # `--format=text`, which is the shape of a post nobody can point at.
        "activity_urn": found.get("activity"),
        "content_urn": found.get("share") or found.get("ugcPost"),
        "text": body_text,
        "visibility": seen_by,
        "url": f"{WEB_BASE}/feed/update/{best}",
    }


def update_urn(activity: str) -> str:
    """Wrap an activity urn in the five-field tuple the delete addresses.

    Built here rather than at the call site so the one string that decides
    *which post is removed* is written down once, next to the capture it came
    from.
    """
    return UPDATE_URN.format(activity=activity)


def delete(client: Any, urn: Any, dry_run: bool = False) -> dict:
    """Take a post down, addressed by its activity urn.

    The undo. `cli.cmd_post` books it as a cleanup so no cap, no cooldown and no
    open breaker can withhold it - but "never refused" is narrower than it
    sounds, and `cli._write` spells out the whole of it. Three things still
    refuse this: the urn is validated here before anything is sent, because the
    exemption is about this CLI's own limits and not about accepting whatever it
    is handed; `state._guard_cleanup` refuses an undo past `cleanup_ceiling`
    (exit 5), which is what stops a runaway loop; and `state._guard_readable`
    refuses it outright when the ledger will not parse (exit 9), because the
    ceiling is counted out of that same file.
    """
    activity = activity_urn(urn)
    target = update_urn(activity)
    body = {
        "variables": {"updateUrn": target},
        "queryId": query_id("post_delete"),
        "includeWebMetadata": True,
    }
    result = client.post(GRAPHQL_PATH.format(query_id=query_id("post_delete")), body, dry_run)
    if dry_run:
        return result

    _confirm_removed(result, activity)
    return {
        # `render` puts the identifier on the first line of a block, and this is
        # the id the caller passed in - a delete that answered with a bare
        # `{"deleted": true}` would name no post at all in `--format=text`.
        "activity_urn": activity,
        "update_urn": target,
        "deleted": True,
        # No `url` on purpose, unlike every other write here. The permalink now
        # points at nothing, and handing it back invites an agent to cite a dead
        # link as evidence that the delete worked.
    }


def _confirm_removed(result: Any, activity: str) -> None:
    """Refuse to call a delete done on a response that proves nothing.

    Only the *request* was captured, so no success shape is pinned - but three
    answers that a naive reader reports as a removed post are refused. An empty
    body is what `transport.parse` hands back for a 200 with nothing in it; the
    GraphQL executor answers **200 with the failure in the body**, which
    `transport.raise_for_status` never sees; and it parks that failure under
    either the top level or `data`, so both are read.
    """
    if not isinstance(result, dict) or not result:
        raise _not_removed(activity, "the response was empty")

    errors = _errors(result)
    if errors:
        raise _not_removed(activity, f"LinkedIn reported {_error_text(errors)}")

    data = result.get("data")
    if not (isinstance(data, dict) and data) and not result.get("included"):
        raise _not_removed(activity, "the response carried no result body")


def _errors(result: dict) -> list | None:
    """The executor's error list, from either place it parks one."""
    for node in (result, result.get("data")):
        if isinstance(node, dict):
            found = node.get("errors")
            if isinstance(found, list) and found:
                return found
    return None


def _not_removed(activity: str, detail: str) -> UpstreamError:
    """A delete whose outcome the response does not establish.

    Reported rather than swallowed, and never as a success: a post believed
    deleted and still public is the failure this whole check exists for. Not
    marked retryable either - `cli._report` renders `.retryable` straight into
    the envelope an agent branches on, and the right next step is to look, not
    to fire the same request again on the chance it lands.

    `detail` is scrubbed here rather than at the call site. It is LinkedIn's own
    prose out of a **200** body, so `transport.raise_for_status` - which scrubs
    the body *it* splices - never saw it; and `cli._report` renders this
    exception's `str()` into `error.message`, which under an agent gateway is permanent
    model context. In the constructor is what makes render.py's "unredacted by
    construction rather than by the caller remembering" true of this path too.
    `activity` is deliberately left alone: the scrubber is aggressive about urn
    shapes on purpose, and this is the caller's own argument and the only thing
    in the message naming the post to read back. Scrubbing the whole string
    would trade the leak for a delete that cannot say what it failed on.
    """
    return UpstreamError(
        f"the delete of {activity} was sent but not confirmed: {scrub_secrets(detail)}. "
        f"The request reached LinkedIn, so the post may already be gone - read it back with "
        f"`linkedin post get "
        f'"{activity}"` before doing anything else. If it is still there the delete did not '
        "land and the same command can be run again; unlike a create, deleting a post that is "
        "already deleted publishes nothing."
    )


def _created_urns(result: Any) -> dict[str, str]:
    """Every id the response offers for the new post, keyed by kind.

    Only the request was captured, so no single path is pinned. Every post-shaped
    urn anywhere in the answer is collected - including one wrapped inside an
    `urn:li:fsd_update:(urn:li:activity:…,…)` tuple - and the first of each kind
    is kept.
    """
    found: dict[str, str] = {}
    for kind, urn in _urns(result):
        found.setdefault(kind, urn)
    return found


def _urns(node: Any):
    if isinstance(node, str):
        for match in _POST_URN.finditer(node):
            yield match.group(1), match.group(0)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _urns(value)
    elif isinstance(node, list):
        for value in node:
            yield from _urns(value)


def _error_text(errors: list) -> str:
    for entry in errors:
        if isinstance(entry, dict) and entry.get("message"):
            return str(entry["message"])
    return str(errors[0])


def _refused(detail: str) -> UpstreamError:
    """LinkedIn said no, in a 200.

    Distinct from `_unconfirmed` on purpose. A GraphQL `errors` array is a
    validation failure: nothing was created, and the caller must not go looking
    for a post that does not exist. `OutcomeUnknown` says the opposite - that the
    write may have landed - and an agent reads the two completely differently.
    Not retryable: the request will be refused the same way until it changes.

    `detail` is scrubbed here rather than at the call site, the same way
    `surfaces/messaging.py::_refused` and `surfaces/social.py::_refused` do it.
    It is LinkedIn's own prose out of a **200** body, so
    `transport.raise_for_status` - which scrubs the body *it* splices - never saw
    it; and `cli._report` renders this exception's `str()` into `error.message`,
    which under an agent gateway is permanent model context. In the constructor is what
    makes the guarantee hold by construction instead of by the next caller
    remembering.
    """
    return UpstreamError(
        f"LinkedIn refused the post: {scrub_secrets(detail)}. Nothing was created."
    )


def _unconfirmed(detail: str) -> OutcomeUnknown:
    """A create whose outcome the response does not establish.

    `OutcomeUnknown` rather than a plain upstream error, and never `retryable`:
    the request reached LinkedIn, the payload carries no dedupe token, and
    `cli._report` renders `.retryable` straight into the envelope an agent
    branches on. The only safe next step is to look.

    `detail` goes through the scrubber for the same reason `_refused`'s does.
    Every detail that reaches here today is a literal written in this file, so
    nothing live leaks through it as it stands - it is scrubbed anyway, because
    the property render.py advertises is "unredacted by construction rather than
    by the caller remembering", and the caller who forgets is the one added
    later.
    """
    return OutcomeUnknown(
        f"the post was sent but not confirmed: {scrub_secrets(detail)}. The request reached "
        "LinkedIn, so it may already be public - check your recent activity in the browser, or run "
        "`linkedin feed list`, before doing anything else. Do not retry this create: the "
        "captured payload carries no dedupe token, so a second attempt publishes the post "
        "twice rather than replacing the first."
    )
