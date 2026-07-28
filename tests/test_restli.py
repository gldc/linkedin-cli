"""RestLi 2.0 `variables` encoding.

Expected outputs here are transcribed from real requests captured off the
LinkedIn web client, so these tests pin the wire format against
observed traffic rather than against our own assumptions.
"""

from linkedin_cli import restli

MAILBOX = "urn:li:fsd_profile:ACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH"


def test_scalar_string_is_percent_encoded():
    assert restli.encode("PRIMARY_INBOX") == "PRIMARY_INBOX"
    assert restli.encode(MAILBOX) == (
        "urn%3Ali%3Afsd_profile%3AACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH"
    )


def test_integers_and_bools_are_bare():
    assert restli.encode(20) == "20"
    assert restli.encode(True) == "true"
    assert restli.encode(False) == "false"
    assert restli.encode(1784038334786) == "1784038334786"


def test_dict_becomes_parenthesised_tuple():
    assert restli.encode({"count": 20, "start": 0}) == "(count:20,start:0)"


def test_list_uses_List_wrapper():
    assert restli.encode([1, 2, 3]) == "List(1,2,3)"
    assert restli.encode([]) == "List()"


def test_conversations_query_matches_captured_request():
    """The exact shape the web client sends for the primary inbox."""
    got = restli.encode(
        {
            "query": {
                "predicateUnions": [
                    {"conversationCategoryPredicate": {"category": "PRIMARY_INBOX"}}
                ]
            },
            "count": 20,
            "mailboxUrn": MAILBOX,
        }
    )
    assert got == (
        "(query:(predicateUnions:List((conversationCategoryPredicate:(category:PRIMARY_INBOX)))),"
        "count:20,mailboxUrn:urn%3Ali%3Afsd_profile%3AACoAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHH)"
    )


def test_messages_pagination_shape():
    got = restli.encode({"deliveredAt": 1783583526337, "countBefore": 20, "countAfter": 0})
    assert got == "(deliveredAt:1783583526337,countBefore:20,countAfter:0)"


def test_nested_conversation_urn_is_fully_encoded():
    """Conversation URNs embed parens and commas; they must not leak as structure."""
    curn = "urn:li:msg_conversation:(urn:li:fsd_profile:ABC,2-xyz==)"
    got = restli.encode({"conversationUrn": curn})
    assert "(" not in got[len("(conversationUrn:") :]
    assert got.startswith("(conversationUrn:urn%3Ali%3Amsg_conversation%3A%28")
    assert got.endswith("%29)")


def test_key_order_is_preserved():
    assert restli.encode({"b": 1, "a": 2}) == "(b:1,a:2)"


def test_none_values_are_omitted():
    assert restli.encode({"a": 1, "b": None, "c": 2}) == "(a:1,c:2)"
