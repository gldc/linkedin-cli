"""The profile surface: `me`, own profile, and any profile by public id or URL.

`resolve_public_id` is the load-bearing piece. Every write in this CLI is
addressed by URN, and a member URN is only reachable from a public id, so a URL
an agent cannot reduce to an identifier is a person it can read about and never
act on. It is pure by design - it never takes a client - so that a malformed
input fails before anything touches the network.

**Documented gap.** The verified `FullProfile-76` decoration carries identity,
headline, location, summary and treasury media, and nothing else: the live
capture contains no `Position`, no `Education` and no connection count anywhere
in `included`. `experience`, `education` and `connections` are therefore part of
the projection's shape but stay empty until the decoration (or extra call) that
carries them is captured live. Filling them from a guessed field name would be
worse than leaving them empty, because an agent cannot tell a guess from a fact.
"""

from __future__ import annotations

import urllib.parse

from ..graph import Graph

FULL_PROFILE_DECORATION = "com.linkedin.voyager.dash.deco.identity.profile.FullProfile-76"

PROFILES_PATH = "identity/dash/profiles"

PROFILE_URN_PREFIX = "urn:li:fsd_profile:"

# The `$type` suffix of the member entity itself, as captured:
# `com.linkedin.voyager.dash.identity.profile.Profile`. Anything else in
# `included` is a decoration about a member, not the member.
_PROFILE_TYPE_SUFFIX = "Profile"

# Only linkedin.com and its country subdomains host member profiles. Matching on
# a suffix without the dot would accept `linkedin.com.evil.example`.
_LINKEDIN_HOSTS = ("linkedin.com",)

_PROFILE_PATH_SEGMENT = "in"


def get_me(client) -> dict:
    """The logged-in member: plain id, dash profile URN, public id and name."""
    graph = Graph(client.get("me"))
    data = graph.data
    mini = graph.deref(data, "miniProfile")
    mini = mini if isinstance(mini, dict) else {}

    urn = mini.get("dashEntityUrn")
    if not isinstance(urn, str) or not urn:
        # `dashEntityUrn` is a decoration LinkedIn can drop; the fs_/fsd_ swap is
        # the documented equivalence and the only fallback available offline.
        raw = data.get("*miniProfile")
        urn = raw.replace("fs_miniProfile", "fsd_profile") if isinstance(raw, str) else None

    return {
        "plain_id": data.get("plainId"),
        "profile_urn": urn or None,
        "public_id": mini.get("publicIdentifier"),
        "name": _full_name(mini),
        "premium": data.get("premiumSubscriber"),
    }


def get_profile(client, who: str | None = None) -> dict:
    """Fetch and project a profile: the operator's own when `who` is omitted."""
    if who is None:
        urn = get_me(client)["profile_urn"]
        if not urn:
            raise ValueError(
                "could not determine the logged-in member's profile urn from `me`; "
                "run `linkedin auth status`"
            )
        return project_profile(fetch_by_urn(client, urn))

    who = who.strip()
    if who.startswith(PROFILE_URN_PREFIX):
        return project_profile(fetch_by_urn(client, who))
    return project_profile(fetch_by_public_id(client, resolve_public_id(who)))


def resolve_public_id(value: str) -> str:
    """Reduce a public id, a profile URL or a `/in/…` path to the bare public id.

    Raises `ValueError` on anything that is not a member profile - a company or
    school URL, another host, or a bare token that cannot be an identifier.
    """
    if not isinstance(value, str):
        raise ValueError(f"expected a public id or profile URL, got {type(value).__name__}")

    candidate = value.strip()
    if not candidate:
        raise ValueError("expected a public id or profile URL, got an empty string")

    if "/" in candidate or "://" in candidate:
        candidate = _from_url(candidate)

    candidate = urllib.parse.unquote(candidate).strip()
    if not candidate or any(ch in candidate for ch in " \t/?#:%"):
        raise ValueError(f"{value!r} is not a usable LinkedIn public id")
    return candidate


def fetch_by_urn(client, urn: str) -> dict:
    """GET the verified dash profile route for a member URN."""
    encoded = urllib.parse.quote(urn, safe="")
    return client.get(f"{PROFILES_PATH}/{encoded}?decorationId={FULL_PROFILE_DECORATION}")


def fetch_by_public_id(client, public_id: str) -> dict:
    """GET a profile by public id.

    UNVERIFIED: unlike `fetch_by_urn`, this route was not exercised during the
    live capture, so the `q=memberIdentity` finder is inferred from the dash
    profile API's collection shape rather than observed. It must be confirmed
    against a live session before anything depends on it; the projection handles
    both the collection and the bare-entity response, so only the path is at risk.
    """
    encoded = urllib.parse.quote(public_id, safe="")
    return client.get(
        f"{PROFILES_PATH}?q=memberIdentity&memberIdentity={encoded}"
        f"&decorationId={FULL_PROFILE_DECORATION}"
    )


def project_profile(payload: dict) -> dict:
    """Reduce a dash profile response - bare entity or collection - to the projection."""
    graph = Graph(payload)
    node = _profile_node(graph)

    return {
        "profile_urn": node.get("entityUrn"),
        "public_id": node.get("publicIdentifier"),
        "name": _full_name(node),
        "headline": node.get("headline") or None,
        "location": _location(graph, node),
        "about": node.get("summary") or None,
        # See the module docstring: absent from FullProfile-76, not guessed.
        "connections": None,
        "experience": [],
        "education": [],
    }


# --------------------------------------------------------------------- internals


def _from_url(value: str) -> str:
    # A scheme-less `www.linkedin.com/in/x` parses as a path, not a host, so one
    # is added before parsing rather than special-cased afterwards.
    text = value if "://" in value else ("https://" + value.lstrip("/") if "." in value else value)
    parsed = urllib.parse.urlparse(text)

    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    if host and not any(host == h or host.endswith("." + h) for h in _LINKEDIN_HOSTS):
        raise ValueError(f"{value!r} is not a linkedin.com profile URL")

    segments = [s for s in (parsed.path or "").split("/") if s]
    if len(segments) < 2 or segments[0].lower() != _PROFILE_PATH_SEGMENT:
        raise ValueError(f"{value!r} is not a member profile URL - expected a /in/<public-id> path")
    return segments[1]


def _full_name(node: dict) -> str | None:
    parts = [node.get("firstName"), node.get("lastName")]
    name = " ".join(str(p) for p in parts if isinstance(p, str) and p).strip()
    return name or None


def _profile_node(graph: Graph) -> dict:
    """The Profile entity, whether the response is one entity or a collection."""
    data = graph.data
    if "*elements" in data:
        elements = graph.deref(data, "elements")
        if isinstance(elements, list) and elements:
            return elements[0]
        return {}
    return data


def _location(graph: Graph, node: dict) -> str | None:
    geo_location = node.get("geoLocation")
    if isinstance(geo_location, dict):
        geo = graph.deref(geo_location, "geo")
        if isinstance(geo, dict):
            for key in ("defaultLocalizedName", "defaultLocalizedNameWithoutCountryName"):
                name = geo.get(key)
                if isinstance(name, str) and name:
                    return name
    # `address` is the member's own free-text location; `locationName` is the
    # legacy field. Either beats returning nothing.
    for key in ("address", "locationName"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_urn(client, who: str) -> str:
    """Turn a public id, profile URL or bare URN into an fsd_profile URN.

    Writes need a URN, and an agent usually only has a name or a link, so this
    is the bridge between what it can see and what it can act on - and it is the
    function that decides which human `linkedin invite` writes to.

    It matches on identity, never on position. A profile response's `included`
    carries the subject *and* everyone the decoration dragged in - "people also
    viewed", suggested connections - all of them `Profile` entities with
    `urn:li:fsd_profile:` urns, in LinkedIn's order. The previous revision
    returned the first one it saw and never read `publicIdentifier`, which is
    incident 1 of this project living in shipped code: a capture script matched
    a control by prefix and clicked "Invite … to connect" on somebody else's
    card. An invitation cannot be withdrawn by this CLI.

    So it raises rather than returns a merely plausible urn. "Not found" costs a
    refused command the operator can fix; a stranger's urn costs a real
    connection request, and nobody looks twice at a call that succeeded.
    """
    if who.startswith(PROFILE_URN_PREFIX):
        return who

    public_id = resolve_public_id(who)
    graph = Graph(fetch_by_public_id(client, public_id))
    # `data` is included as a candidate so the bare-entity spelling of the answer
    # resolves like the collection one, not because it is trusted more: every
    # candidate passes the same identity check, and the check is what decides.
    candidates = [graph.data, *graph.by_type(_PROFILE_TYPE_SUFFIX)]
    found = {urn for node in candidates if (urn := _subject_urn(node, public_id))}

    if len(found) == 1:
        return found.pop()
    if found:
        raise ValueError(
            f"{public_id!r} was claimed by {len(found)} different member urns in one answer, "
            "and two members cannot share a public id - so this answer was not understood. "
            "Nothing was written. Run `linkedin profile get` on the person and pass the "
            "`profile_urn` it reports."
        )
    raise ValueError(
        f"{who!r} did not resolve to a member urn: the profile lookup answered, and no "
        f"profile in the answer carries publicIdentifier {public_id!r}. Check the public id "
        "with `linkedin profile get`, which reports the urn a write takes. Nothing was "
        "written, and no urn was guessed from the answer's ordering."
    )


def _subject_urn(node: dict, public_id: str) -> str | None:
    """The node's own urn if it *is* the requested member, else None.

    `publicIdentifier` decides, not the `$type` and not the position - but the
    type is checked too, because decorations copy identity fields around freely
    and the urn this returns gets spent on a human.
    """
    if not isinstance(node, dict):
        return None
    urn = node.get("entityUrn")
    if not isinstance(urn, str) or not urn.startswith(PROFILE_URN_PREFIX):
        return None
    found = node.get("publicIdentifier")
    if not isinstance(found, str):
        return None
    # Case-folded because linkedin.com/in/ URLs are case-insensitive and an agent
    # copies them as it finds them, while the payload always spells the id lower.
    return urn if found.casefold() == public_id.casefold() else None
