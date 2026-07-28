"""Lazy index over LinkedIn's `{data, included}` normalized envelope.

Payloads here are synthetic but shaped like the real thing, including the three
properties that killed the eager resolver: cycles, dangling `*` references and
duplicate `entityUrn` values.
"""

import time

from linkedin_cli.graph import Graph

PROFILE = "urn:li:fs_miniProfile:ACoAAA"
OTHER = "urn:li:fs_miniProfile:ACoAAB"
UPDATE = "urn:li:activity:7000000000000000001"
MISSING = "urn:li:fs_miniProfile:GONE"


def mini(urn, **kw):
    return {"entityUrn": urn, "$type": "com.linkedin.voyager.identity.shared.MiniProfile", **kw}


def payload(data=None, included=()):
    return {"data": data if data is not None else {}, "included": list(included)}


# --------------------------------------------------------------------------- data


def test_data_returns_the_data_block():
    g = Graph(payload({"*elements": [UPDATE]}))
    assert g.data == {"*elements": [UPDATE]}


def test_data_is_empty_dict_when_absent():
    """Some voyager errors come back without `data`; callers must not crash."""
    assert Graph({}).data == {}
    assert Graph({"data": None}).data == {}


# --------------------------------------------------------------------------- resolve


def test_resolve_finds_included_entity():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    assert g.resolve(PROFILE)["firstName"] == "Ada"


def test_resolve_missing_urn_returns_none():
    g = Graph(payload(included=[mini(PROFILE)]))
    assert g.resolve(MISSING) is None
    assert g.resolve("") is None
    assert g.resolve(None) is None


def test_resolve_matches_trailing_segment_of_type():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    assert g.resolve(PROFILE, "MiniProfile")["firstName"] == "Ada"
    assert g.resolve(PROFILE, "com.linkedin.voyager.identity.shared.MiniProfile") is not None


def test_resolve_type_mismatch_returns_none():
    g = Graph(payload(included=[mini(PROFILE)]))
    assert g.resolve(PROFILE, "MiniCompany") is None


def test_resolve_type_mismatch_when_entity_has_no_type():
    g = Graph(payload(included=[{"entityUrn": PROFILE}]))
    assert g.resolve(PROFILE) is not None
    assert g.resolve(PROFILE, "MiniProfile") is None


# --------------------------------------------------------------------------- index


def test_duplicate_entity_urn_last_one_wins():
    """LinkedIn ships the same urn twice with different decorations; never raise."""
    g = Graph(
        payload(included=[mini(PROFILE, firstName="Stale"), mini(PROFILE, firstName="Fresh")])
    )
    assert g.resolve(PROFILE)["firstName"] == "Fresh"


def test_malformed_included_entries_are_ignored():
    g = Graph({"included": ["not-a-dict", None, {"no": "urn"}, mini(PROFILE)]})
    assert g.resolve(PROFILE) is not None


def test_included_of_wrong_type_does_not_raise():
    assert Graph({"included": "junk"}).resolve(PROFILE) is None
    assert Graph({"included": None}).resolve(PROFILE) is None


# --------------------------------------------------------------------------- deref


def test_deref_single_reference():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    assert g.deref({"*miniProfile": PROFILE}, "miniProfile")["firstName"] == "Ada"


def test_deref_accepts_key_with_or_without_star():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    obj = {"*miniProfile": PROFILE}
    assert g.deref(obj, "*miniProfile") == g.deref(obj, "miniProfile")


def test_deref_list_reference_drops_dangling_entries():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada"), mini(OTHER, firstName="Grace")]))
    got = g.deref({"*elements": [PROFILE, MISSING, OTHER]}, "elements")
    assert [p["firstName"] for p in got] == ["Ada", "Grace"]


def test_deref_empty_list_reference_stays_a_list():
    g = Graph(payload())
    assert g.deref({"*elements": []}, "elements") == []


def test_deref_missing_key_returns_none():
    g = Graph(payload(included=[mini(PROFILE)]))
    assert g.deref({"*miniProfile": PROFILE}, "author") is None


def test_deref_dangling_single_reference_returns_none():
    g = Graph(payload())
    assert g.deref({"*author": MISSING}, "author") is None


def test_deref_on_non_dict_returns_none():
    g = Graph(payload())
    assert g.deref(None, "author") is None
    assert g.deref(["a"], "author") is None


# --------------------------------------------------------------------------- by_type


def test_by_type_returns_every_matching_entity():
    g = Graph(
        payload(
            included=[
                mini(PROFILE, firstName="Ada"),
                mini(OTHER, firstName="Grace"),
                {"entityUrn": UPDATE, "$type": "com.linkedin.voyager.feed.render.UpdateV2"},
            ]
        )
    )
    assert [p["firstName"] for p in g.by_type("MiniProfile")] == ["Ada", "Grace"]
    assert len(g.by_type("UpdateV2")) == 1


def test_by_type_unknown_is_empty():
    g = Graph(payload(included=[mini(PROFILE)]))
    assert g.by_type("Company") == []


def test_by_type_deduplicates_by_urn():
    g = Graph(
        payload(included=[mini(PROFILE, firstName="Stale"), mini(PROFILE, firstName="Fresh")])
    )
    assert [p["firstName"] for p in g.by_type("MiniProfile")] == ["Fresh"]


# --------------------------------------------------------------------------- expand


def test_expand_inlines_a_single_ref_under_the_unstarred_key():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    got = g.expand({"*author": PROFILE, "text": "hi"})
    assert got["author"]["firstName"] == "Ada"
    assert got["text"] == "hi"
    assert "*author" not in got


def test_expand_inlines_list_refs_and_drops_dangling():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada"), mini(OTHER, firstName="Grace")]))
    got = g.expand({"*elements": [PROFILE, MISSING, OTHER]})
    assert [p["firstName"] for p in got["elements"]] == ["Ada", "Grace"]


def test_expand_unresolvable_single_ref_becomes_none():
    g = Graph(payload())
    assert g.expand({"*author": MISSING}) == {"author": None}


def test_expand_resolved_ref_wins_over_plain_key_of_the_same_name():
    """`author` alongside `*author` is a bare urn string; the entity is what callers want."""
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    got = g.expand({"author": PROFILE, "*author": PROFILE})
    assert got["author"]["firstName"] == "Ada"


def test_expand_walks_nested_plain_containers():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    got = g.expand({"actor": {"image": {"*profile": PROFILE}}, "list": [{"*profile": PROFILE}]})
    assert got["actor"]["image"]["profile"]["firstName"] == "Ada"
    assert got["list"][0]["profile"]["firstName"] == "Ada"


def test_expand_stops_at_depth_and_leaves_the_urn():
    a = {"entityUrn": "urn:a", "$type": "x.A", "*b": "urn:b"}
    b = {"entityUrn": "urn:b", "$type": "x.B", "*c": "urn:c"}
    c = {"entityUrn": "urn:c", "$type": "x.C", "name": "leaf"}
    g = Graph(payload(included=[a, b, c]))

    one = g.expand({"*a": "urn:a"}, depth=1)
    assert one["a"]["b"] == "urn:b"

    three = g.expand({"*a": "urn:a"}, depth=3)
    assert three["a"]["b"]["c"]["name"] == "leaf"


def test_expand_at_depth_zero_expands_nothing():
    g = Graph(payload(included=[mini(PROFILE)]))
    assert g.expand({"*author": PROFILE}, depth=0) == {"author": PROFILE}


def test_expand_does_not_mutate_its_input():
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    obj = {"*author": PROFILE}
    g.expand(obj)
    assert obj == {"*author": PROFILE}
    assert g.resolve(PROFILE) == mini(PROFILE, firstName="Ada")


def test_expand_terminates_on_a_self_referential_node():
    node = {"entityUrn": "urn:self", "$type": "x.A", "*me": "urn:self", "name": "loop"}
    g = Graph(payload(included=[node]))
    got = g.expand({"*root": "urn:self"}, depth=50)
    assert got["root"]["name"] == "loop"
    assert got["root"]["me"] == "urn:self"


def test_expand_terminates_on_a_two_node_cycle_with_fanout():
    """Without a visited set this is 2**depth work and the test never returns."""
    a = {"entityUrn": "urn:a", "$type": "x.A", "*x": "urn:b", "*y": "urn:b"}
    b = {"entityUrn": "urn:b", "$type": "x.B", "*x": "urn:a", "*y": "urn:a"}
    g = Graph(payload(included=[a, b]))

    started = time.monotonic()
    got = g.expand(a, depth=64)
    assert time.monotonic() - started < 5.0

    assert got["x"]["x"] == "urn:a"
    assert got["y"]["y"] == "urn:a"


def test_expand_allows_the_same_entity_on_sibling_branches():
    """Cycle cutting is per-path: one author on two posts must resolve on both."""
    g = Graph(payload(included=[mini(PROFILE, firstName="Ada")]))
    got = g.expand({"one": {"*author": PROFILE}, "two": {"*author": PROFILE}})
    assert got["one"]["author"]["firstName"] == "Ada"
    assert got["two"]["author"]["firstName"] == "Ada"


def test_expand_on_non_dict_returns_empty_dict():
    assert Graph(payload()).expand(None) == {}


# --------------------------------------------------------------------------- graphql


def test_graphql_root_digs_out_the_query_payload():
    g = Graph(
        {
            "data": {"data": {"messengerConversationsBySyncToken": {"elements": [1, 2]}}},
            "included": [],
        }
    )
    assert g.graphql_root("messengerConversationsBySyncToken") == {"elements": [1, 2]}


def test_graphql_root_accepts_the_unnested_form():
    g = Graph({"data": {"messengerMailboxCounts": {"unread": 3}}})
    assert g.graphql_root("messengerMailboxCounts") == {"unread": 3}


def test_graphql_root_missing_query_returns_none():
    g = Graph({"data": {"data": {"somethingElse": {}}}})
    assert g.graphql_root("messengerConversationsBySyncToken") is None
    assert Graph({}).graphql_root("anything") is None


def test_graphql_root_non_dict_value_returns_none():
    g = Graph({"data": {"data": {"q": [1, 2, 3]}}})
    assert g.graphql_root("q") is None
