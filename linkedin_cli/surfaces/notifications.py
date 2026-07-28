"""The notifications surface.

A notification card is mostly presentation: LinkedIn ships rendered text models
(`{"text": …, "attributesV2": […]}`), images, setting menus and swipe actions,
and an agent needs none of it. The projection keeps the seven fields an agent
can act on and throws the rest away - a 10-card page is ~95 KB raw.

Two things about the real payload shape the code:

* Card text lives under different keys per card *type*. `subHeadline` is null on
  every card in the capture; what actually carries the second line is
  `contentPrimaryText`/`contentSecondaryText` (a *list* of text models) on social
  cards and `actionCaption` on upsell cards. A card lacking all of them is
  normal, not an error.
* The notification type is not a field. It is the first component of the card
  urn - `urn:li:fsd_notificationCard:(REACTED_TO_YOUR_COMMENT,…)` - so it is
  parsed out rather than read.

Pagination is by `metadata.nextStart`, which is an offset rather than a token,
and it is passed straight back as `start`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from ..graph import Graph

DECORATION = (
    "com.linkedin.voyager.dash.deco.identity.notifications.CardsCollectionWithInjectionsNoPills-24"
)

PATH = "voyagerIdentityDashNotificationCards"

WEB_BASE = "https://www.linkedin.com"

# Checked in order; the first one holding text wins.
SUBTEXT_KEYS = ("subHeadline", "contentPrimaryText", "contentSecondaryText", "actionCaption")


def list_notifications(
    client,
    count: int = 10,
    cursor: str | None = None,
    unread_only: bool = False,
) -> tuple[list[dict], str | None, bool]:
    """Fetch one page of notifications: (items, next_cursor, has_more)."""
    payload = client.get(_path(count, cursor))
    graph = Graph(payload)

    cards = graph.deref(graph.data, "elements")
    if not isinstance(cards, list):
        # Order matters (LinkedIn ranks these), so `included` is only a fallback
        # for a payload that lost its element list entirely.
        cards = graph.by_type("Card")

    items = [project_card(card) for card in cards if isinstance(card, dict)]
    if unread_only:
        items = [item for item in items if not item["read"]]

    # The cursor is deliberately computed before filtering: a page whose cards
    # were all read still has to advance, or `--unread-only` would report the
    # end of the list at the first fully-read page.
    next_cursor = _next_cursor(graph)
    return items, next_cursor, next_cursor is not None


def project_card(card: dict) -> dict:
    """Reduce one Card to the notification projection."""
    return {
        "notification_urn": card.get("entityUrn"),
        "type": _card_type(card),
        "headline": text_of(card.get("headline")),
        "subtext": _subtext(card),
        "published_at": iso_utc(card.get("publishedAt")),
        "read": bool(card.get("read")),
        "url": _url(card),
    }


def text_of(value: object) -> str | None:
    """Flatten a TextViewModel - or a list of them - into a plain string."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("text", "accessibilityText"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        return None
    if isinstance(value, list):
        parts = [t for t in (text_of(entry) for entry in value) if t]
        return "\n".join(parts) or None
    return None


def iso_utc(epoch_ms: object) -> str | None:
    """Convert LinkedIn's epoch milliseconds to a UTC ISO-8601 second."""
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- internals


def _path(count: int, cursor: str | None) -> str:
    query = f"decorationId={DECORATION}&count={int(count)}&q=filterVanityName"
    if cursor not in (None, ""):
        query += f"&start={quote(str(cursor), safe='')}"
    return f"{PATH}?{query}"


def _subtext(card: dict) -> str | None:
    for key in SUBTEXT_KEYS:
        found = text_of(card.get(key))
        if found:
            return found
    return None


def _card_type(card: dict) -> str | None:
    # `urn:li:fsd_notificationCard:(TYPE,<suffix urn>)`; the objectUrn is
    # `urn:li:notificationV2:(<member>,TYPE,<suffix urn>)`. Neither the member
    # urn nor the type token ever contains a comma, so a split is enough.
    for urn, position in ((card.get("entityUrn"), 0), (card.get("objectUrn"), 1)):
        if not isinstance(urn, str) or "(" not in urn:
            continue
        fields = urn.split("(", 1)[1].split(",")
        if len(fields) > position and fields[position]:
            return fields[position]
    return None


def _url(card: dict) -> str | None:
    action = card.get("cardAction")
    target = action.get("actionTarget") if isinstance(action, dict) else None
    if not isinstance(target, str) or not target:
        return None
    if target.startswith("http"):
        return target
    return WEB_BASE + target if target.startswith("/") else None


def _next_cursor(graph: Graph) -> str | None:
    metadata = graph.data.get("metadata")
    start = metadata.get("nextStart") if isinstance(metadata, dict) else None
    if isinstance(start, bool) or not isinstance(start, (int, str)):
        return None
    return str(start) or None
