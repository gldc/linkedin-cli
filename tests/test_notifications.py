"""Notification projection, run against a 10-card page.

Assertions here deliberately derive person-specific expectations *from* the
fixture rather than hard-coding them, so the committed synthetic fixture and a
raw capture of a real account both satisfy the same test. What is hard-coded is
what neither may legitimately differ on - the request path, the projection's key
set, the URL base and the epoch->ISO conversion.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from linkedin_cli.surfaces import notifications

FIXTURES = Path(__file__).parent / "fixtures"

CONTRACT_KEYS = {
    "notification_urn",
    "type",
    "headline",
    "subtext",
    "published_at",
    "read",
    "url",
}

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load(name: str) -> dict:
    """Prefer the committed fixture; fall back to the gitignored raw capture."""
    for candidate in (FIXTURES / f"{name}.json", FIXTURES / "raw" / f"{name}.json"):
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        f"no fixture {name}.json in {FIXTURES} or {FIXTURES / 'raw'}. "
        f"{FIXTURES / f'{name}.json'} is a tracked file - a checkout missing it is broken."
    )


NOTIFICATIONS = load("notifications")


def test_the_fixture_this_module_runs_on_is_committed_not_gitignored():
    """`tests/fixtures/raw/` is gitignored, so while the only capture lived
    there a fresh clone could not import this file at all and the whole suite
    stopped at collection. The committed fixture is what makes TDD possible in
    a clean worktree, and this fails the moment it stops being there."""
    assert (FIXTURES / "notifications.json").is_file()


class StubClient:
    """Records the paths asked for and replays a canned payload."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.paths: list[str] = []

    def get(self, path: str, dry_run: bool = False):
        self.paths.append(path)
        return self.payload


def cards(payload: dict) -> list[dict]:
    index = {e["entityUrn"]: e for e in payload["included"] if "entityUrn" in e}
    return [index[urn] for urn in payload["data"]["*elements"]]


def card_with(payload: dict, key: str) -> dict:
    return next(c for c in cards(payload) if c.get(key))


def listed(payload: dict = NOTIFICATIONS, **kwargs):
    return notifications.list_notifications(StubClient(payload), **kwargs)


# ------------------------------------------------------------------------ request


def test_path_carries_the_verified_decoration_and_query():
    client = StubClient(NOTIFICATIONS)
    notifications.list_notifications(client, count=10)
    path = client.paths[0]
    assert path.startswith("voyagerIdentityDashNotificationCards?")
    assert f"decorationId={notifications.DECORATION}" in path
    assert "count=10" in path
    assert "q=filterVanityName" in path


def test_count_is_threaded_into_the_path():
    client = StubClient(NOTIFICATIONS)
    notifications.list_notifications(client, count=25)
    assert "count=25" in client.paths[0]


def test_cursor_becomes_the_start_parameter():
    client = StubClient(NOTIFICATIONS)
    notifications.list_notifications(client, cursor="11")
    assert "start=11" in client.paths[0]


def test_no_cursor_means_no_start_parameter():
    client = StubClient(NOTIFICATIONS)
    notifications.list_notifications(client)
    assert "start=" not in client.paths[0]


# --------------------------------------------------------------------- projection


def test_projects_every_card_in_server_order():
    items, _, _ = listed()
    assert len(items) == 10
    assert [i["notification_urn"] for i in items] == NOTIFICATIONS["data"]["*elements"]


def test_projection_holds_exactly_the_contract_keys():
    items, _, _ = listed()
    assert all(set(item) == CONTRACT_KEYS for item in items)


def test_type_is_the_token_embedded_in_the_card_urn():
    items, _, _ = listed()
    for item in items:
        token = item["notification_urn"].split("(", 1)[1].split(",", 1)[0]
        assert item["type"] == token


def test_headline_is_a_plain_string_not_a_text_view_model():
    items, _, _ = listed()
    expected = [card["headline"]["text"] for card in cards(NOTIFICATIONS)]
    assert [i["headline"] for i in items] == expected


def test_subtext_falls_back_to_content_primary_text():
    """`subHeadline` is null on every real card; the comment body lives here."""
    card = card_with(NOTIFICATIONS, "contentPrimaryText")
    items, _, _ = listed()
    item = next(i for i in items if i["notification_urn"] == card["entityUrn"])
    assert item["subtext"] == card["contentPrimaryText"][0]["text"]


def test_subtext_falls_back_to_action_caption():
    card = card_with(NOTIFICATIONS, "actionCaption")
    items, _, _ = listed()
    item = next(i for i in items if i["notification_urn"] == card["entityUrn"])
    assert item["subtext"] == card["actionCaption"]["text"]


def test_cards_without_any_text_project_to_none_rather_than_crashing():
    bare = {
        "entityUrn": "urn:li:fsd_notificationCard:(BREAKING_NEWS,urn:li:string:x)",
        "$type": "com.linkedin.voyager.dash.identity.notifications.Card",
        "read": False,
        "publishedAt": 1700000000000,
    }
    payload = {"data": {"*elements": [bare["entityUrn"]]}, "included": [bare]}
    items, _, _ = listed(payload)
    assert items[0]["headline"] is None
    assert items[0]["subtext"] is None
    assert items[0]["url"] is None
    assert items[0]["type"] == "BREAKING_NEWS"


def test_read_flags_survive_as_booleans():
    items, _, _ = listed()
    assert [i["read"] for i in items] == [bool(c.get("read")) for c in cards(NOTIFICATIONS)]
    assert any(i["read"] is False for i in items)


def test_published_at_is_iso8601_utc():
    items, _, _ = listed()
    assert all(ISO.match(i["published_at"]) for i in items)


def test_epoch_milliseconds_convert_to_utc_iso():
    assert notifications.iso_utc(1700000000000) == "2023-11-14T22:13:20Z"
    assert notifications.iso_utc(None) is None
    assert notifications.iso_utc("not-a-timestamp") is None


# ---------------------------------------------------------------------------- url


def test_relative_action_targets_become_absolute_linkedin_urls():
    items, _, _ = listed()
    relative = [
        c
        for c in cards(NOTIFICATIONS)
        if str((c.get("cardAction") or {}).get("actionTarget", "")).startswith("/")
    ]
    assert relative, "fixture should contain at least one relative actionTarget"
    for card in relative:
        item = next(i for i in items if i["notification_urn"] == card["entityUrn"])
        assert item["url"] == "https://www.linkedin.com" + card["cardAction"]["actionTarget"]


def test_absolute_action_targets_are_left_alone():
    absolute = [
        c
        for c in cards(NOTIFICATIONS)
        if str((c.get("cardAction") or {}).get("actionTarget", "")).startswith("http")
    ]
    assert absolute, "fixture should contain at least one absolute actionTarget"
    items, _, _ = listed()
    for card in absolute:
        item = next(i for i in items if i["notification_urn"] == card["entityUrn"])
        assert item["url"] == card["cardAction"]["actionTarget"]


# --------------------------------------------------------------------- pagination


def test_next_cursor_comes_from_metadata_next_start():
    _, cursor, has_more = listed()
    assert cursor == str(NOTIFICATIONS["data"]["metadata"]["nextStart"])
    assert has_more is True


def test_absent_next_start_ends_the_pagination():
    payload = {"data": {"metadata": {}, "*elements": []}, "included": []}
    items, cursor, has_more = listed(payload)
    assert (items, cursor, has_more) == ([], None, False)


def test_empty_payload_yields_an_empty_page():
    assert listed({}) == ([], None, False)


# ------------------------------------------------------------------ unread filter


def test_unread_only_drops_read_cards():
    items, cursor, has_more = listed(unread_only=True)
    assert items, "fixture should contain at least one unread card"
    assert all(item["read"] is False for item in items)
    # Filtering is client-side, so the page cursor must still advance - otherwise
    # a caller whose whole first page was read would think it had reached the end.
    assert cursor == str(NOTIFICATIONS["data"]["metadata"]["nextStart"])
    assert has_more is True
