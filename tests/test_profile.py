"""Profile projection and public-id resolution.

`resolve_public_id` gets the heaviest coverage here on purpose: every write in
this CLI needs a URN, and a URL an agent cannot turn into an identifier is a
name it can read but never act on.

As in test_notifications, person-specific expectations are derived from the
fixture rather than typed out, so the committed synthetic fixture and a raw
capture of a real account both satisfy the same test.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from linkedin_cli.surfaces import profile

FIXTURES = Path(__file__).parent / "fixtures"

CONTRACT_KEYS = {
    "profile_urn",
    "public_id",
    "name",
    "headline",
    "location",
    "about",
    "connections",
    "experience",
    "education",
}


def load(name: str) -> dict:
    for candidate in (FIXTURES / f"{name}.json", FIXTURES / "raw" / f"{name}.json"):
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        f"no fixture {name}.json in {FIXTURES} or {FIXTURES / 'raw'}. "
        f"{FIXTURES / f'{name}.json'} is a tracked file - a checkout missing it is broken."
    )


ME = load("me")
PROFILE_SELF = load("profile_self")
MINI = next(e for e in ME["included"] if e["$type"].endswith("MiniProfile"))


@pytest.mark.parametrize("name", ["me", "profile_self"])
def test_the_fixtures_this_module_runs_on_are_committed_not_gitignored(name):
    """`tests/fixtures/raw/` is gitignored, so while the only captures lived
    there a fresh clone could not import this file at all and the whole suite
    stopped at collection. The committed fixtures are what make TDD possible in
    a clean worktree, and this fails the moment one stops being there."""
    assert (FIXTURES / f"{name}.json").is_file()


class StubClient:
    """Replays payloads in order, repeating the last one, recording paths."""

    def __init__(self, *payloads: dict):
        self.queue = list(payloads)
        self.paths: list[str] = []

    def get(self, path: str, dry_run: bool = False):
        self.paths.append(path)
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


# --------------------------------------------------------------- resolve_public_id


@pytest.mark.parametrize(
    "value",
    [
        "grace-hopper-1906",
        "https://www.linkedin.com/in/grace-hopper-1906",
        "https://www.linkedin.com/in/grace-hopper-1906/",
        "https://www.linkedin.com/in/grace-hopper-1906?originalSubdomain=ca",
        "https://www.linkedin.com/in/grace-hopper-1906/?trk=public_profile",
        "https://www.linkedin.com/in/grace-hopper-1906#experience",
        "http://www.linkedin.com/in/grace-hopper-1906",
        "https://linkedin.com/in/grace-hopper-1906",
        "https://ca.linkedin.com/in/grace-hopper-1906",
        "www.linkedin.com/in/grace-hopper-1906",
        "linkedin.com/in/grace-hopper-1906/",
        "/in/grace-hopper-1906",
        "  https://www.linkedin.com/in/grace-hopper-1906/  ",
        "https://WWW.LINKEDIN.COM/IN/grace-hopper-1906",
    ],
)
def test_resolve_public_id_accepts_every_shape_of_the_same_person(value):
    assert profile.resolve_public_id(value) == "grace-hopper-1906"


def test_resolve_public_id_keeps_trailing_path_segments_out():
    url = "https://www.linkedin.com/in/the-operator/recent-activity/all/"
    assert profile.resolve_public_id(url) == "the-operator"


def test_resolve_public_id_decodes_percent_escapes():
    assert profile.resolve_public_id("https://www.linkedin.com/in/jos%C3%A9-p") == "josé-p"


def test_resolve_public_id_is_pure():
    """No client argument, so it can never be the thing that fires a request."""
    assert profile.resolve_public_id("the-operator") == "the-operator"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/in/grace-hopper-1906",
        "https://linkedin.com.evil.example/in/the-operator",
        "https://notlinkedin.com/in/the-operator",
    ],
)
def test_resolve_public_id_rejects_urls_that_are_not_linkedin(value):
    with pytest.raises(ValueError, match="linkedin"):
        profile.resolve_public_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://www.linkedin.com/company/synthetic-holdings",
        "https://www.linkedin.com/school/synthetic-polytechnic/",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/",
    ],
)
def test_resolve_public_id_rejects_linkedin_urls_that_are_not_profiles(value):
    with pytest.raises(ValueError):
        profile.resolve_public_id(value)


@pytest.mark.parametrize("value", ["", "   ", None, 42, "urn:li:fsd_profile:ACoAAA", "a b"])
def test_resolve_public_id_rejects_junk(value):
    with pytest.raises(ValueError):
        profile.resolve_public_id(value)


# ------------------------------------------------------------------------- get_me


def test_get_me_returns_the_dash_profile_urn():
    result = profile.get_me(StubClient(ME))
    assert result["profile_urn"] == MINI["dashEntityUrn"]
    assert result["profile_urn"].startswith("urn:li:fsd_profile:")


def test_get_me_falls_back_to_rewriting_the_mini_profile_urn():
    """`dashEntityUrn` is a decoration; without it the fs_ -> fsd_ swap is all we have."""
    stripped = {k: v for k, v in MINI.items() if k != "dashEntityUrn"}
    payload = {"data": ME["data"], "included": [stripped]}
    result = profile.get_me(StubClient(payload))
    assert result["profile_urn"] == ME["data"]["*miniProfile"].replace(
        "fs_miniProfile", "fsd_profile"
    )


def test_get_me_carries_the_plain_id_and_public_id():
    result = profile.get_me(StubClient(ME))
    assert result["plain_id"] == ME["data"]["plainId"]
    assert result["public_id"] == MINI["publicIdentifier"]
    assert result["name"] == f"{MINI['firstName']} {MINI['lastName']}"


def test_get_me_hits_the_me_endpoint():
    client = StubClient(ME)
    profile.get_me(client)
    assert client.paths == ["me"]


def test_get_me_tolerates_a_payload_without_a_mini_profile():
    result = profile.get_me(StubClient({"data": {"plainId": 1}, "included": []}))
    assert result["profile_urn"] is None
    assert result["name"] is None


# -------------------------------------------------------------------- get_profile


def test_get_profile_without_an_argument_looks_up_me_first():
    client = StubClient(ME, PROFILE_SELF)
    profile.get_profile(client)
    assert client.paths[0] == "me"
    assert client.paths[1].startswith("identity/dash/profiles/")


def test_get_profile_url_encodes_the_urn_and_pins_the_decoration():
    client = StubClient(ME, PROFILE_SELF)
    profile.get_profile(client)
    path = client.paths[1]
    assert urllib.parse.quote(MINI["dashEntityUrn"], safe="") in path
    assert f"decorationId={profile.FULL_PROFILE_DECORATION}" in path


def test_get_profile_accepts_a_urn_directly_without_calling_me():
    urn = PROFILE_SELF["data"]["entityUrn"]
    client = StubClient(PROFILE_SELF)
    result = profile.get_profile(client, urn)
    assert client.paths[0].startswith("identity/dash/profiles/")
    assert result["profile_urn"] == urn


def test_get_profile_resolves_a_url_to_the_public_id_route():
    client = StubClient(PROFILE_SELF)
    profile.get_profile(client, "https://www.linkedin.com/in/the-operator/")
    assert "memberIdentity=the-operator" in client.paths[0]


def test_get_profile_rejects_a_non_linkedin_url_before_any_request():
    client = StubClient(PROFILE_SELF)
    with pytest.raises(ValueError):
        profile.get_profile(client, "https://example.com/in/the-operator")
    assert client.paths == []


# --------------------------------------------------------------------- projection


def test_projection_holds_exactly_the_contract_keys():
    assert set(profile.project_profile(PROFILE_SELF)) == CONTRACT_KEYS


def test_projection_pulls_identity_from_the_dash_profile():
    data = PROFILE_SELF["data"]
    result = profile.project_profile(PROFILE_SELF)
    assert result["profile_urn"] == data["entityUrn"]
    assert result["public_id"] == data["publicIdentifier"]
    assert result["name"] == f"{data['firstName']} {data['lastName']}"
    assert result["headline"] == data["headline"]
    assert result["about"] == data["summary"]


def test_location_is_dereferenced_from_the_geo_entity():
    """`geoLocation` holds only a urn; the readable name lives in `included`."""
    geo_urn = PROFILE_SELF["data"]["geoLocation"]["*geo"]
    geo = next(e for e in PROFILE_SELF["included"] if e.get("entityUrn") == geo_urn)
    assert profile.project_profile(PROFILE_SELF)["location"] == geo["defaultLocalizedName"]


def test_location_falls_back_to_the_free_text_address():
    payload = {"data": dict(PROFILE_SELF["data"], geoLocation=None), "included": []}
    assert profile.project_profile(payload)["location"] == PROFILE_SELF["data"]["address"]


def test_projection_survives_an_empty_payload():
    result = profile.project_profile({})
    assert set(result) == CONTRACT_KEYS
    assert result["profile_urn"] is None
    assert result["name"] is None


def test_collection_shaped_payloads_project_the_first_element():
    """The public-id lookup returns a collection, not a bare entity."""
    node = PROFILE_SELF["data"]
    payload = {
        "data": {"*elements": [node["entityUrn"]]},
        "included": [node, *PROFILE_SELF["included"]],
    }
    assert profile.project_profile(payload)["public_id"] == node["publicIdentifier"]


def test_full_profile_decoration_carries_no_positions_or_schools():
    """Documented gap: FullProfile-76 has no positions, schools or connection count.

    These stay empty rather than guessed - see the module docstring. If a later
    decoration starts carrying them this test is the thing that should fail.
    """
    result = profile.project_profile(PROFILE_SELF)
    assert result["experience"] == []
    assert result["education"] == []
    assert result["connections"] is None


# -------------------------------------------------------------------- resolve_urn

# This is the function that decides which human `linkedin invite` writes to, so
# the payloads below are built to be hostile in the one way a real one is: a
# profile response's `included` carries the subject *and* every other member the
# decoration dragged in - "people also viewed", suggested connections - all of
# them `Profile` entities with `urn:li:fsd_profile:` urns, in LinkedIn's order,
# not ours. Incident 1 of this project was a capture script that matched a
# control by prefix and clicked "Invite … to connect" on somebody else's card.
SUBJECT_URN = "urn:li:fsd_profile:ACoAAASYNTHETICSUBJECT00"
SUBJECT_ID = "synthetic-subject"
DECOY_URN = "urn:li:fsd_profile:ACoAAASYNTHETICDECOY0000"

PROFILE_TYPE = "com.linkedin.voyager.dash.identity.profile.Profile"


def profile_entity(urn: str, public_id: str, **extra) -> dict:
    return {
        "$type": PROFILE_TYPE,
        "entityUrn": urn,
        "publicIdentifier": public_id,
        "firstName": "Syn",
        "lastName": "Thetic",
        **extra,
    }


def finder_payload(*included: dict) -> dict:
    """The `q=memberIdentity` collection shape, with `included` in a given order."""
    return {
        "data": {"*elements": [SUBJECT_URN], "paging": {"count": 10, "start": 0, "total": 1}},
        "included": list(included),
    }


DECOY_FIRST = finder_payload(
    profile_entity(DECOY_URN, "someone-else-entirely"),
    profile_entity(SUBJECT_URN, SUBJECT_ID),
)


def test_resolve_urn_matches_the_public_identifier_not_the_first_profile_in_included():
    """The old resolver returned the first `urn:li:fsd_profile:` in `included` by
    position and never read `publicIdentifier`, so a decorated stranger sitting
    ahead of the subject was the urn an irreversible invitation went to."""
    assert profile.resolve_urn(StubClient(DECOY_FIRST), SUBJECT_ID) == SUBJECT_URN


def test_resolve_urn_matches_the_subject_from_a_profile_url_too():
    url = f"https://www.linkedin.com/in/{SUBJECT_ID}/"
    assert profile.resolve_urn(StubClient(DECOY_FIRST), url) == SUBJECT_URN


def test_resolve_urn_matches_a_public_id_whose_case_the_url_changed():
    """LinkedIn's own profile URLs are case-insensitive and an agent copies them
    as it finds them; the payload always spells the id in lower case."""
    url = "https://www.linkedin.com/in/Synthetic-Subject"
    assert profile.resolve_urn(StubClient(DECOY_FIRST), url) == SUBJECT_URN


def test_resolve_urn_raises_rather_than_hand_back_a_urn_nobody_asked_for():
    """ "Not found" has to beat "the first thing that looked right". A urn that is
    merely plausible costs a real connection request to a stranger, and no agent
    looks twice at a call that returned successfully."""
    payload = finder_payload(profile_entity(DECOY_URN, "someone-else-entirely"))
    with pytest.raises(ValueError, match=SUBJECT_ID):
        profile.resolve_urn(StubClient(payload), SUBJECT_ID)


def test_resolve_urn_raises_on_an_answer_that_carried_no_profile_at_all():
    with pytest.raises(ValueError, match=SUBJECT_ID):
        profile.resolve_urn(StubClient({"data": {}, "included": []}), SUBJECT_ID)


def test_resolve_urn_ignores_an_entity_that_is_not_a_profile():
    """Constructed, not captured: no live entity is known to mirror another
    member's `publicIdentifier`. It is here because decorations copy identity
    fields around freely, and the urn this returns is spent on a human - so the
    entity that answers has to be the member itself, by `$type`, not something
    that merely quotes them."""
    mirror = {
        "$type": "com.linkedin.voyager.dash.identity.profile.ProfileCardWrapper",
        "entityUrn": DECOY_URN,
        "publicIdentifier": SUBJECT_ID,
    }
    payload = finder_payload(mirror, profile_entity(SUBJECT_URN, SUBJECT_ID))
    assert profile.resolve_urn(StubClient(payload), SUBJECT_ID) == SUBJECT_URN


def test_resolve_urn_refuses_two_members_claiming_the_requested_public_id():
    """One public id is one member, so two urns answering to it means the answer
    was not understood. Picking either is the positional guess this exists to
    stop; the operator can look the person up and pass the urn instead."""
    payload = finder_payload(
        profile_entity(SUBJECT_URN, SUBJECT_ID),
        profile_entity(DECOY_URN, SUBJECT_ID),
    )
    with pytest.raises(ValueError, match="two"):
        profile.resolve_urn(StubClient(payload), SUBJECT_ID)


def test_resolve_urn_reads_the_subject_out_of_data_when_included_holds_nothing():
    """The bare-entity spelling of the same answer - identity-checked exactly as
    the collection one is, so the shape it arrives in changes nothing."""
    payload = {"data": profile_entity(SUBJECT_URN, SUBJECT_ID), "included": []}
    assert profile.resolve_urn(StubClient(payload), SUBJECT_ID) == SUBJECT_URN


def test_resolve_urn_hands_back_a_member_urn_without_asking_linkedin():
    client = StubClient(DECOY_FIRST)
    assert profile.resolve_urn(client, SUBJECT_URN) == SUBJECT_URN
    assert client.paths == []


def test_resolve_urn_refuses_a_company_url_before_any_request():
    client = StubClient(DECOY_FIRST)
    with pytest.raises(ValueError):
        profile.resolve_urn(client, "https://www.linkedin.com/company/some-co")
    assert client.paths == []
