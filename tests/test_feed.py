"""Feed projection, exercised against the live 11-post capture.

Nothing person-specific is hard-coded here. The raw capture is a real feed - real
names, real post text - and it is gitignored, so every expectation about a
*value* is derived from the fixture at runtime. What is pinned literally is what
a scrubber may never change: the request path, the projection's key set, the
activity-urn epoch decoding, and the pagination arithmetic.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from linkedin_cli.surfaces import feed

FIXTURES = Path(__file__).parent / "fixtures"
RAW = FIXTURES / "raw"

CONTRACT_KEYS = {
    "activity_urn",
    "content_urn",
    "author",
    "text",
    "posted_at",
    "reactions",
    "comments",
    "reshares",
    "url",
}

AUTHOR_KEYS = {"name", "headline", "urn", "public_id"}


def load(name: str) -> dict:
    for base in (FIXTURES, RAW):
        path = base / name
        if path.exists():
            return json.loads(path.read_text())
    pytest.skip(f"no fixture {name} in {FIXTURES} or {RAW}", allow_module_level=True)
    raise AssertionError("unreachable")


class FakeClient:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.paths: list[str] = []

    def get(self, path: str):
        self.paths.append(path)
        return self.payloads.pop(0) if self.payloads else {}


@pytest.fixture
def capture():
    return load("feed.json")


# ------------------------------------------------------------------------ path


def test_path_matches_the_verified_capture():
    client = FakeClient({})
    feed.list_feed(client, count=10)
    assert client.paths[0] == (
        "feed/updatesV2?commentsCount=0&count=10&likesCount=0"
        "&moduleKey=home-feed%3Adesktop&q=chronFeed"
    )


def test_count_is_threaded_into_the_path():
    client = FakeClient({})
    feed.list_feed(client, count=3)
    assert "count=3" in client.paths[0]


def test_cursor_becomes_a_start_offset():
    client = FakeClient({})
    feed.list_feed(client, count=10, cursor="10")
    assert client.paths[0].endswith("&start=10")


def test_non_numeric_cursor_is_rejected_rather_than_sent():
    """A garbage cursor must fail locally, not 400 against LinkedIn."""
    with pytest.raises(ValueError):
        feed.list_feed(FakeClient({}), cursor="not-a-number")


# ------------------------------------------------------------------ projection


def test_projects_every_update_in_the_capture(capture):
    """10 posts, not the 11 UpdateV2 entities in `included`.

    The extra one is the update a reshare wraps; it is reachable through
    `*resharedUpdate` and is not itself a feed item.
    """
    items, _, _ = feed.list_feed(FakeClient(capture))
    assert len(items) == 10
    assert len([e for e in capture["included"] if e["$type"].endswith("UpdateV2")]) == 11


def test_items_follow_server_order(capture):
    """Feed order is the ranking; `included` order is an implementation detail."""
    items, _, _ = feed.list_feed(FakeClient(capture))
    expected = [
        ref.split("urn:li:activity:")[1].split(",")[0] for ref in capture["data"]["*elements"]
    ]
    assert [item["activity_urn"].rsplit(":", 1)[1] for item in items] == expected


def test_projection_has_exactly_the_contract_keys(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    for item in items:
        assert set(item) == CONTRACT_KEYS
        assert set(item["author"]) == AUTHOR_KEYS


def test_activity_urn_is_the_addressable_one(capture):
    """`react`, `comment` and `post delete` all take the activity urn."""
    items, _, _ = feed.list_feed(FakeClient(capture))
    for item in items:
        assert item["activity_urn"].startswith("urn:li:activity:")


def test_author_urn_is_a_dash_urn_not_the_actor_urn(capture):
    """`urn:li:member:100000010` and `urn:li:company:9200001` are both unusable.

    Only the dash forms address anything, and the capture holds both a member
    actor and a page actor, so both branches are covered here.
    """
    items, _, _ = feed.list_feed(FakeClient(capture))
    urns = [item["author"]["urn"] for item in items]
    assert all(urn for urn in urns)
    prefixes = {urn.rsplit(":", 1)[0] for urn in urns}
    assert prefixes == {"urn:li:fsd_profile", "urn:li:fsd_company"}


def test_every_author_has_a_public_id(capture):
    """A public id is what `profile get` takes; a page's is its universalName."""
    items, _, _ = feed.list_feed(FakeClient(capture))
    assert all(item["author"]["public_id"] for item in items)


def test_author_names_are_populated(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    assert all(item["author"]["name"] for item in items)


def test_counts_are_integers(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    for item in items:
        for key in ("reactions", "comments", "reshares"):
            assert isinstance(item[key], int)


def test_at_least_one_post_carries_text(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    assert any(item["text"] for item in items)


def test_urls_are_linkedin_permalinks(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    for item in items:
        assert item["url"].startswith("https://www.linkedin.com/")
        # Tracking parameters identify the *reader*, so they never ship.
        assert "?" not in item["url"]


# -------------------------------------------------------------------- posted_at


def test_posted_at_is_decoded_from_the_activity_id():
    """The first 41 bits of a LinkedIn activity id are its creation epoch-ms.

    Checked against the capture: every id in it decodes to within minutes of the
    response's own `paginationToken` timestamp.
    """
    assert feed.posted_at("urn:li:activity:7486933303296000001") == "2026-07-26T00:00:00Z"


def test_posted_at_tolerates_a_urn_it_cannot_decode():
    assert feed.posted_at("urn:li:activity:not-a-number") is None
    assert feed.posted_at(None) is None


def test_every_post_in_the_capture_has_a_timestamp(capture):
    items, _, _ = feed.list_feed(FakeClient(capture))
    assert all(item["posted_at"] and item["posted_at"].endswith("Z") for item in items)


# ------------------------------------------------------------------ pagination


def test_short_page_reports_no_more(capture):
    """The capture holds 11 updates against a total of 528, so there is more."""
    _, cursor, has_more = feed.list_feed(FakeClient(capture), count=10)
    assert has_more is True
    assert cursor == "10"


def test_exhausted_collection_has_no_cursor():
    payload = {"data": {"paging": {"start": 20, "count": 10, "total": 25}, "*elements": []}}
    _, cursor, has_more = feed.list_feed(FakeClient(payload))
    assert (cursor, has_more) == (None, False)


def test_missing_paging_block_reports_no_more():
    payload = {"data": {"*elements": []}}
    assert feed.list_feed(FakeClient(payload)) == ([], None, False)


# ----------------------------------------------------------------- resilience


def test_empty_payload_projects_to_nothing():
    assert feed.list_feed(FakeClient({})) == ([], None, False)


def test_dangling_update_reference_is_dropped():
    payload = {"data": {"*elements": ["urn:li:fs_updateV2:gone"]}, "included": []}
    assert feed.list_feed(FakeClient(payload))[0] == []


def test_update_without_an_actor_still_projects():
    payload = {
        "data": {"*elements": ["urn:li:fs_updateV2:x"]},
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": "urn:li:fs_updateV2:x",
                "updateMetadata": {"urn": "urn:li:activity:7486933303296000001"},
            }
        ],
    }
    items, _, _ = feed.list_feed(FakeClient(payload))
    assert items[0]["author"] == {"name": None, "headline": None, "urn": None, "public_id": None}
    assert items[0]["reactions"] == 0


# -------------------------------------------------------------------- post get


@pytest.mark.parametrize(
    "given,expected",
    [
        ("7486933303296000001", "urn:li:activity:7486933303296000001"),
        ("urn:li:activity:7486933303296000001", "urn:li:activity:7486933303296000001"),
        (
            "https://www.linkedin.com/feed/update/urn:li:activity:748693330329600000/",
            "urn:li:activity:748693330329600000",
        ),
        (
            "urn:li:fs_updateV2:(urn:li:activity:7486933303296000001,MAIN_FEED,-,-,false)",
            "urn:li:activity:7486933303296000001",
        ),
    ],
)
def test_get_post_addresses_the_activity_urn(capture, given, expected):
    client = FakeClient(capture)
    feed.get_post(client, given)
    assert client.paths[0] == (
        "feed/updatesV2?q=backendUrnOrNss&urnOrNss=" + urllib.parse.quote(expected, safe="")
    )


def test_get_post_rejects_something_that_is_not_a_post():
    with pytest.raises(ValueError):
        feed.get_post(FakeClient({}), "urn:li:fsd_profile:ABC")


def test_get_post_projects_the_first_update_of_the_response(capture):
    """The single-post route answers with the same collection envelope."""
    post = feed.get_post(FakeClient(capture), "7486933303296000001")
    assert set(post) == CONTRACT_KEYS


def test_get_post_raises_not_found_when_the_response_is_empty():
    from linkedin_cli import transport

    with pytest.raises(transport.NotFound):
        feed.get_post(FakeClient({}), "7486933303296000001")


# ------------------------------------------------------- post get, gone or hidden


def shell(entity_urn: str, in_elements: bool = True) -> dict:
    """The response shape for a post whose entity is present and content gone.

    Derived from the capture rather than invented: every live UpdateV2 in it
    carries `actor`, `commentary`, `updateMetadata.shareUrn` and a
    `*socialDetail` ref, and the entity urn is the five-field tuple. Live
    verification recorded `post get` on a post this CLI had just
    deleted answering `ok: true` with the content fields null - so what LinkedIn
    hands back is one of these updates with everything but its identity gone.

    `in_elements=False` is the same shell arriving without the collection
    wrapper, which is the branch `get_post` falls through to `by_type` for.
    """
    update = {
        "$type": feed.UPDATE_TYPE,
        "entityUrn": entity_urn,
        "dashEntityUrn": entity_urn.replace("fs_updateV2", "fsd_update"),
    }
    data = {"*elements": [entity_urn]} if in_elements else {}
    return {"data": data, "included": [update]}


def tombstone(entity_urn: str) -> dict:
    """What a deleted post ACTUALLY answers with, recorded live.

    `shell` above was derived from the capture by reasoning about which fields a
    readable post carries and blanking them. That reasoning was sound and the
    fixture was still wrong, in the direction that matters: the real answer is
    not empty. LinkedIn fills `content` with a tombstone - a title reading "This
    post cannot be displayed" and a warning icon - and fills `updateMetadata`
    with the urn that was asked for, while leaving `actor`, `commentary` and
    `socialDetail` null and omitting `shareUrn`.

    So the entity says, in a field the projection treated as evidence of a
    readable post, that the post cannot be displayed. `post get` answered
    `ok: true` on exactly this while the whole suite was green, because both were
    checked against a fixture nobody had compared with a live response.

    Verbatim except for the ids and the tracking blob.
    """
    activity = feed.activity_urn(entity_urn)
    return {
        "data": {"*elements": [entity_urn]},
        "included": [
            {
                "$type": feed.UPDATE_TYPE,
                "entityUrn": entity_urn,
                "actor": None,
                "commentary": None,
                "socialDetail": None,
                "resharedUpdate": None,
                "content": {
                    "$type": "com.linkedin.voyager.feed.render.EntityComponent",
                    "title": {
                        "$type": "com.linkedin.voyager.common.TextViewModel",
                        "text": "This post cannot be displayed",
                        "textDirection": "USER_LOCALE",
                    },
                    "image": {
                        "$type": "com.linkedin.voyager.common.ImageViewModel",
                        "attributes": [
                            {
                                "$type": "com.linkedin.voyager.common.ImageAttribute",
                                "sourceType": "ART_DECO_ICON",
                                "artDecoIcon": "IMG_CIRCLE_WARNING_48DP",
                            }
                        ],
                    },
                },
                "updateMetadata": {
                    "$type": "com.linkedin.voyager.feed.render.UpdateMetadata",
                    "urn": activity,
                    "shareMediaUrn": activity,
                    "excludedFromSeen": True,
                },
            }
        ],
    }


def test_a_deleted_post_is_refused_even_though_linkedin_fills_in_content(capture):
    """The live failure: `content` present, and it says the post is not there.

    Measured live against a post this CLI had just deleted. `post get`
    returned exit 0 with a live-looking permalink, because `content` was one of
    the things counted as evidence and LinkedIn puts its "cannot be displayed"
    placeholder there. A placeholder saying the post is unreadable is the single
    worst thing to read as proof that it is readable.
    """
    from linkedin_cli import transport

    entity = entity_urn_from(capture)
    with pytest.raises(transport.NotFound):
        feed.get_post(FakeClient(tombstone(entity)), feed.activity_urn(entity))


def test_updatemetadata_without_a_shareurn_is_not_evidence(capture):
    """`updateMetadata` is present on the tombstone; only `shareUrn` is evidence.

    The real answer carries `urn` and `shareMediaUrn`, both echoes of what was
    asked for, and no `shareUrn`. Keying on the dict being present rather than on
    that one field would pass every tombstone.
    """
    from linkedin_cli import transport

    entity = entity_urn_from(capture)
    payload = tombstone(entity)
    payload["included"][0]["content"] = None
    with pytest.raises(transport.NotFound):
        feed.get_post(FakeClient(payload), feed.activity_urn(entity))


def test_a_real_post_with_content_and_an_actor_still_reads(capture):
    """The guard against overcorrecting: a readable post must stay readable."""
    post = feed.get_post(FakeClient(capture), feed.activity_urn(entity_urn_from(capture)))
    assert post["author"]["name"]
    assert post["activity_urn"]


def entity_urn_from(capture: dict) -> str:
    return capture["data"]["*elements"][0]


def test_get_post_refuses_a_shell_rather_than_projecting_it(capture):
    """A deleted post is an UpdateV2 with no content, and it must not answer ok.

    Measured live after `post delete` succeeded, `post get` on the
    same urn returned `ok: true` with null fields. Every field the projection
    could still fill - `activity_urn`, `posted_at`, `url` - is derived from the
    urn that was *sent*, so a projection of a shell is the request echoed back
    dressed as an answer. `post get` is the oracle `post delete` tells callers to
    read the post back with; an oracle that says "still there" about a post that
    is gone confirms nothing.
    """
    from linkedin_cli import transport

    entity = entity_urn_from(capture)
    with pytest.raises(transport.NotFound):
        feed.get_post(FakeClient(shell(entity)), feed.activity_urn(entity))


def test_get_post_refuses_a_shell_that_arrives_without_the_collection_wrapper(capture):
    """The `by_type` fallback must not be the way a shell gets projected anyway."""
    from linkedin_cli import transport

    entity = entity_urn_from(capture)
    with pytest.raises(transport.NotFound):
        feed.get_post(FakeClient(shell(entity, in_elements=False)), feed.activity_urn(entity))


def test_shell_error_names_both_causes_and_asserts_neither(capture):
    """Deleted and not-visible are indistinguishable in this response.

    Both answer with the same empty shell, and the agent's next step is the same
    either way, so naming one cause would be a confident-wrong answer bought for
    nothing.
    """
    from linkedin_cli import transport

    entity = entity_urn_from(capture)
    with pytest.raises(transport.NotFound) as caught:
        feed.get_post(FakeClient(shell(entity)), feed.activity_urn(entity))

    message = str(caught.value).lower()
    assert "deleted" in message
    assert "visible" in message
    # The urn the caller passed is what makes the message actionable.
    assert feed.activity_urn(entity) in str(caught.value)
    # A permalink here would be the exact artifact this raise exists to suppress:
    # a live-looking link to a post that cannot be read.
    assert f"{feed.WEB_BASE}/feed/update/" not in str(caught.value)


def test_get_post_still_answers_for_a_post_that_merely_has_no_text(capture):
    """A post with no commentary - a bare image, an article share - is not gone.

    Every one of the 10 updates in the capture carries commentary text, so this
    shape is one the fixture cannot supply and the refusal must still not fire
    on it. The shell check keys on there being nothing from LinkedIn at all, not
    on any single field, precisely so a normal post that is shaped unusually
    still reads.
    """
    entity = entity_urn_from(capture)
    payload = shell(entity)
    payload["included"][0]["actor"] = {"name": {"text": "Someone"}}
    payload["included"][0]["updateMetadata"] = {
        "urn": feed.activity_urn(entity),
        "shareUrn": "urn:li:share:7486790696960000000",
    }

    post = feed.get_post(FakeClient(payload), feed.activity_urn(entity))
    assert post["text"] is None
    assert post["author"]["name"] == "Someone"


def test_get_post_no_longer_calls_its_own_route_unverified():
    """`post get` was exercised live; the warning outlived the fact.

    Pinned because a stale UNVERIFIED tells an agent to distrust a route that was
    observed answering 200, which is the same class of wrong-report this codebase
    refuses everywhere else.
    """
    assert "UNVERIFIED" not in (feed.get_post.__doc__ or "")
