"""Output envelope, header redaction and the --text renderer.

The renderer is the surface an agent reads, so the tests pin the exact shape of
the envelope and prove the redactor cannot leak a credential.
"""

import io
import json

from linkedin_cli import render, transport
from linkedin_cli.render import REDACTED, SAFE_HEADERS, emit, err, ok, redact_headers, to_text

# What LinkedIn answered when `post create --visibility=CONNECTIONS`
# went out: HTTP 200, with the refusal in a GraphQL `errors` array. The surface
# correctly refuses to call that a post, but diagnosing *why* needed a
# hand-written script, because `--raw` had nothing to say on the error path -
# which is the path it is most needed on. See docs/incidents.md.
REFUSAL_BODY = {
    "data": {"createPost": None},
    "errors": [
        {
            "message": (
                "Invalid input for enum 'dash_contentcreation_VisibilityType'. "
                "No value found for name 'CONNECTIONS'"
            )
        }
    ],
}

# Assembled rather than written out, so no credential-shaped literal enters a
# tracked file for `tools/leakcheck.py` to find. This is the shape that matters
# most: `csrf-token` *is* the JSESSIONID value, and a challenge body carries it.
CSRF = "ajax:" + "1234567890123456"

# A live-shaped session cookie, assembled for the same reason: `tools/leakcheck.py`
# fails the build on a credential-shaped literal in a tracked file, and a fixture
# that does not look real proves nothing about a redactor.
CREDENTIAL = "li_at=AQED" + "aB3_-" * 12

POST = {
    "activity_urn": "urn:li:activity:7123",
    "content_urn": "urn:li:ugcPost:7122",
    "author": {
        "name": "Jane Doe",
        "headline": "Head of Widgets",
        "urn": "urn:li:fsd_profile:ABC",
        "public_id": "janedoe",
    },
    "text": "Shipping the thing today.",
    "posted_at": "2026-07-20T12:00:00Z",
    "reactions": 12,
    "comments": 3,
    "reshares": 1,
    "url": "https://www.linkedin.com/feed/update/urn:li:activity:7123/",
}

CONVERSATION = {
    "conversation_urn": "urn:li:msg_conversation:(urn:li:fsd_profile:ABC,2-abc==)",
    "last_activity_at": "2026-07-20T12:00:00Z",
    "unread": True,
    "participants": [
        {"name": "Jane Doe", "urn": "urn:li:fsd_profile:ABC", "public_id": "janedoe"},
        {"name": "Bob Smith", "urn": "urn:li:fsd_profile:DEF", "public_id": "bsmith"},
    ],
    "last_message": {"text": "hey there", "sender": "Jane Doe", "sent_at": "2026-07-20T12:00:00Z"},
}

MESSAGE = {
    "message_urn": "urn:li:msg_message:(urn:li:fsd_profile:ABC,2-xyz==)",
    "sender": {"name": "Jane Doe", "urn": "urn:li:fsd_profile:ABC"},
    "text": "hello there",
    "sent_at": "2026-07-20T12:00:00Z",
}

NOTIFICATION = {
    "notification_urn": "urn:li:notification:(urn:li:fsd_profile:ABC,9911)",
    "type": "REACTION",
    "headline": "Jane Doe liked your post",
    "subtext": "Head of Widgets at Acme",
    "published_at": "2026-07-20T12:00:00Z",
    "read": False,
    "url": "https://www.linkedin.com/feed/update/urn:li:activity:7123/",
}

PROFILE = {
    "profile_urn": "urn:li:fsd_profile:ABC",
    "public_id": "janedoe",
    "name": "Jane Doe",
    "headline": "Head of Widgets",
    "location": "Lakeside, Meridian",
    "about": "I make widgets.",
    "connections": 512,
    "experience": [
        {"title": "Head of Widgets", "company": "Acme", "date_range": "2020 - Present"},
    ],
    "education": [
        {"school": "Meridian Institute", "degree": "BSc", "date_range": "2012 - 2016"},
    ],
}


def render_text(data, **kw):
    return to_text(ok(data, **kw))


# --------------------------------------------------------------------------- envelope


def test_ok_envelope_shape():
    assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}, "next_cursor": None, "has_more": False}


def test_ok_carries_pagination():
    env = ok([1, 2], next_cursor="1753000000000", has_more=True)
    assert env["next_cursor"] == "1753000000000"
    assert env["has_more"] is True


def test_err_envelope_shape():
    assert err("session_expired", "run auth sync") == {
        "ok": False,
        "error": {
            "code": "session_expired",
            "message": "run auth sync",
            "retryable": False,
        },
    }


def test_err_can_be_retryable():
    assert err("throttled", "slow down", retryable=True)["error"]["retryable"] is True


def test_err_envelope_has_no_data_key():
    """An agent branches on `ok`; a null `data` would invite reading it anyway."""
    assert "data" not in err("not_found", "no such post")


# --------------------------------------------------------------------------- emit


def test_emit_writes_indented_json_with_trailing_newline():
    out = io.StringIO()
    emit(ok({"a": 1}), out)
    text = out.getvalue()
    assert text.endswith("}\n")
    assert "\n  " in text
    assert json.loads(text) == ok({"a": 1})


def test_emit_keeps_non_ascii_readable():
    """ensure_ascii would turn every accented name into \\uXXXX noise."""
    out = io.StringIO()
    emit(ok({"name": "Zoë Müller"}), out)
    assert "Zoë Müller" in out.getvalue()


def test_emit_text_mode_uses_to_text():
    out = io.StringIO()
    emit(ok([POST]), out, mode="text")
    assert out.getvalue() == to_text(ok([POST])) + "\n"


def test_emit_raw_unwraps_the_envelope():
    out = io.StringIO()
    emit(ok({"raw": "payload"}), out, raw=True)
    assert json.loads(out.getvalue()) == {"raw": "payload"}


def test_emit_raw_still_reports_errors():
    """--raw carries the upstream body *and* keeps the error that explains it.

    This test used to say "--raw has no data to unwrap on failure", and that
    premise was wrong: there is a response body on the failure path, it is the
    single most useful thing an operator can be shown, and it was the one thing
    `--raw` could not produce. What stays true is the other half - the error may
    not be swallowed to make room for it, because the code and the exit status
    are what an agent branches on.
    """
    out = io.StringIO()
    emit(err("upstream", "LinkedIn refused the post", body=REFUSAL_BODY), out, raw=True)
    reported = json.loads(out.getvalue())
    assert reported["ok"] is False
    assert reported["error"]["code"] == "upstream"
    assert reported["error"]["body"] == REFUSAL_BODY


# ------------------------------------------------------------------ the raw body


def test_err_carries_an_upstream_body_when_it_is_given_one():
    assert err("upstream", "refused", body=REFUSAL_BODY)["error"]["body"] == REFUSAL_BODY


def test_err_has_no_body_key_when_there_is_no_body():
    """Absent rather than null, for the reason `data` is absent from a failure:
    a null key invites reading it, and every error that never reached LinkedIn
    has nothing to put there."""
    assert "body" not in err("usage", "unknown flag")["error"]


def test_a_body_is_scrubbed_on_its_way_into_the_envelope():
    """The whole hazard of surfacing a raw body. A challenge or checkpoint
    response carries the csrf token, which in this system *is* the live
    JSESSIONID value - and `cli._report` renders this envelope onto stderr,
    which under an agent gateway is permanent model context."""
    body = {"csrf_token": CSRF, "status": "CSRF check failed"}
    error = err("session_expired", "rejected", body=body)["error"]
    assert CSRF not in json.dumps(error)
    assert error["body"]["csrf_token"] == REDACTED
    # Per-value, or the operator cannot tell a challenge from a mistyped payload.
    assert error["body"]["status"] == "CSRF check failed"


def test_the_body_scrubber_is_the_transports_own():
    """Reused rather than reimplemented: a second redactor is a second thing to
    forget to update, and `tests/test_transport.py` already checks that one
    against every pattern `tools/leakcheck.py` hunts for."""
    assert render.scrub_secrets is transport.scrub_secrets


def test_a_scrubbed_body_is_still_json_the_caller_can_read():
    body = {"nested": [{"li_at": "AQED" + "A" * 60}]}
    carried = err("blocked", "999", body=body)["error"]["body"]
    assert carried == {"nested": [{"li_at": REDACTED}]}


def test_a_body_that_is_not_a_mapping_is_carried_all_the_same():
    """`transport.parse` hands back whatever LinkedIn sent. A refusal answered as
    a bare list or a string is still the evidence."""
    assert err("upstream", "boom", body=["nope"])["error"]["body"] == ["nope"]


def test_a_text_rendering_carries_the_body_too():
    """--format=text is a rendering choice, not a request for less evidence."""
    text = to_text(err("upstream", "LinkedIn refused the post", body=REFUSAL_BODY))
    assert "upstream" in text
    assert "CONNECTIONS" in text


# ------------------------------------------------- what this file does NOT scrub
#
# `render.py` used to say, unqualified, that "nothing that reaches an envelope is
# unredacted by construction rather than by the caller remembering". Two executed
# counterexamples are below, and the sentence sat in the one file a reader
# consults to decide whether they have to scrub - while `messaging._unconfirmed`
# and `social._unconfirmed` both cite it as their justification for scrubbing
# anyway. The claim was narrowed to what is enforced. These pin the boundary, so
# it cannot widen back by prose.


def test_an_error_message_is_carried_exactly_as_the_raiser_built_it():
    """`error.body` is scrubbed by construction; `error.message` is not.

    It is whatever `str(exc)` the raiser handed over, written out as given -
    which is why a surface splicing LinkedIn-supplied text into one has to scrub
    it *there*. `surfaces/social.py`'s `_refused` and `_unconfirmed` are what
    that looks like, and they are the reason this gap has no live path through
    it today rather than evidence that this function closes it.
    """
    message = f"LinkedIn rejected {CREDENTIAL}"
    assert err("upstream", message)["error"]["message"] == message
    assert CREDENTIAL in to_text(err("upstream", message))


def test_raw_on_a_success_writes_data_out_without_scrubbing_it():
    """`--raw` asks for the upstream payload, and `emit` unwraps `data` verbatim.

    So `profile get x --raw` against a payload carrying a live `li_at` prints it,
    and that is the flag doing its job: redacting the payload asked for
    explicitly reproduces the reason a hand-written script was needed in the
    first place (docs/incidents.md). It is opt-in, it is
    stdout rather than the stderr an agent gateway keeps, and it is recorded here rather
    than denied in the module docstring.
    """
    stream = io.StringIO()
    emit(ok({"session": CREDENTIAL}), stream, raw=True)
    assert CREDENTIAL in stream.getvalue()


def test_the_failure_path_of_raw_still_scrubs_the_body_it_carries():
    """The other half, and the reason the narrowed claim is worth keeping: the
    body `--raw` attaches to a *failure* goes through `scrub_secrets` in `err`,
    with no caller involved."""
    stream = io.StringIO()
    emit(err("blocked", "999", body={"li_at": CREDENTIAL}), stream, raw=True)
    assert CREDENTIAL not in stream.getvalue()


def test_the_module_docstring_claims_only_what_it_enforces():
    """The defect being hunted, in the file most likely to carry it: a docstring
    asserting a safety property the shipped path does not have.

    Read out of `__doc__` because nothing else fails when that sentence goes
    stale - and a reader deciding whether to scrub reads it rather than `emit`.
    """
    # Whitespace-normalized, or the assertion passes on a sentence that merely
    # wrapped differently - which is exactly how a stale claim survives.
    doc = " ".join(render.__doc__.split())
    assert "nothing that reaches an envelope is unredacted" not in doc
    # Both gaps named, and the one guarantee that does hold still stated.
    assert "error.message" in doc
    assert "--raw" in doc
    assert "error.body" in doc


# --------------------------------------------------------------------------- redaction


def test_redact_headers_hides_cookie_and_csrf_token():
    """Regression: `csrf-token` IS the JSESSIONID value - a live credential."""
    headers = {
        "Cookie": 'li_at=AQEDATestToken; JSESSIONID="ajax:1111222233334444555"',
        "csrf-token": "ajax:1111222233334444555",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
    }
    safe = redact_headers(headers)
    assert safe["Cookie"] == "<redacted>"
    assert safe["csrf-token"] == "<redacted>"
    assert safe["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert "AQEDATestToken" not in json.dumps(safe)
    assert "1111222233334444555" not in json.dumps(safe)


def test_redact_headers_is_an_allowlist_not_a_denylist():
    """A header nobody thought to forbid must still be redacted by default."""
    safe = redact_headers({"x-li-track": '{"clientVersion":"1.2.3"}', "x-some-future": "secret"})
    assert safe == {"x-li-track": "<redacted>", "x-some-future": "<redacted>"}


def test_redact_headers_matches_case_insensitively():
    safe = redact_headers({"CSRF-Token": "ajax:1", "User-Agent": "Mozilla/5.0"})
    assert safe["CSRF-Token"] == "<redacted>"
    assert safe["User-Agent"] == "Mozilla/5.0"


def test_safe_headers_contains_no_credential_bearing_names():
    assert isinstance(SAFE_HEADERS, frozenset)
    assert "cookie" not in SAFE_HEADERS
    assert "csrf-token" not in SAFE_HEADERS
    assert {"accept", "user-agent", "x-restli-protocol-version", "referer"} <= SAFE_HEADERS


def test_redact_headers_does_not_mutate_the_input():
    headers = {"csrf-token": "ajax:1"}
    redact_headers(headers)
    assert headers["csrf-token"] == "ajax:1"


# --------------------------------------------------------------------------- to_text


def test_to_text_post_leads_with_the_actionable_urn():
    text = render_text([POST])
    assert text.splitlines()[0] == "urn:li:activity:7123"
    assert "Jane Doe" in text
    assert "Head of Widgets" in text
    assert "Shipping the thing today." in text
    assert "12 reactions" in text
    assert "3 comments" in text
    assert POST["url"] in text


def test_to_text_separates_items_with_a_blank_line():
    second = dict(POST, activity_urn="urn:li:activity:7999")
    text = render_text([POST, second])
    assert "\n\n" in text
    assert "urn:li:activity:7999" in text


def test_to_text_conversation_shows_participants_and_unread():
    text = render_text([CONVERSATION])
    assert CONVERSATION["conversation_urn"] in text
    assert "Jane Doe" in text and "Bob Smith" in text
    assert "unread" in text
    assert "hey there" in text


def test_to_text_message_shows_sender_and_body():
    text = render_text([MESSAGE])
    assert MESSAGE["message_urn"] in text
    assert "Jane Doe" in text
    assert "hello there" in text


def test_to_text_notification_shows_headline_and_read_state():
    text = render_text([NOTIFICATION])
    assert NOTIFICATION["notification_urn"] in text
    assert "Jane Doe liked your post" in text
    assert "REACTION" in text
    assert "unread" in text


def test_to_text_profile_renders_experience_and_education():
    text = render_text(PROFILE)
    assert "urn:li:fsd_profile:ABC" in text
    assert "Jane Doe" in text
    assert "Lakeside, Meridian" in text
    assert "512 connections" in text
    assert "Head of Widgets" in text and "Acme" in text
    assert "Meridian Institute" in text


def test_to_text_survives_missing_keys():
    """Projections come off a graph with dangling refs; half-empty is normal."""
    text = render_text([{"activity_urn": "urn:li:activity:1"}, {"message_urn": "urn:li:msg:2"}])
    assert "urn:li:activity:1" in text
    assert "urn:li:msg:2" in text


def test_to_text_handles_empty_list():
    assert render_text([]).strip() != ""


def test_to_text_mentions_the_cursor_when_more_results_exist():
    text = render_text([POST], next_cursor="1753000000000", has_more=True)
    assert "1753000000000" in text
    assert "--cursor" in text


def test_to_text_renders_errors():
    text = to_text(err("stale_query_id", "queryId rotated", retryable=False))
    assert "stale_query_id" in text
    assert "queryId rotated" in text


def test_to_text_marks_retryable_errors():
    assert "retryable" in to_text(err("throttled", "slow down", retryable=True))


def test_to_text_falls_back_to_json_for_unknown_shapes():
    text = render_text({"some": "unrecognised", "shape": [1, 2]})
    assert json.loads(text) == {"some": "unrecognised", "shape": [1, 2]}


def test_to_text_renders_scalar_data():
    assert render_text("urn:li:activity:7123").strip() == "urn:li:activity:7123"


def test_to_text_never_leaks_module_level_state_between_calls():
    first = render_text([POST])
    assert render_text([POST]) == first


def test_module_imports_no_third_party():
    assert render.__file__.endswith("render.py")


# ------------------------------------------------------------- invitation blocks

INVITATION = {
    "invitation_urn": "urn:li:fs_invitation:(7000000000000000004,SYNTHETIC)",
    "type": "CONNECTION",
    "from": {
        "name": "Syn Inviter",
        "headline": "Head of Synthetic Widgets",
        "public_id": "synthetic-inviter",
        "profile_urn": "urn:li:fs_miniProfile:ACoAAASYNTHETICINVITER00",
    },
    "message": "we met at the conference",
    "sent_at": "2026-07-09T07:50:09Z",
    "unseen": True,
    "url": "https://www.linkedin.com/in/synthetic-inviter",
}


def test_to_text_invitation_leads_with_the_urn_and_names_the_sender():
    """Every other listed surface has a block renderer; without one, `invitations
    list --format=text` printed raw JSON per row - which is the one output mode
    that exists so a human does not have to read JSON."""
    text = render_text([INVITATION])
    assert text.splitlines()[0].startswith(INVITATION["invitation_urn"])
    assert "Syn Inviter" in text
    assert "Head of Synthetic Widgets" in text
    assert "we met at the conference" in text
    assert INVITATION["url"] in text


def test_to_text_invitation_marks_one_that_has_not_been_seen():
    text = render_text([INVITATION])
    assert "[new]" in text
    assert "[new]" not in render_text([dict(INVITATION, unseen=False)])


def test_to_text_invitation_survives_a_row_with_nothing_but_a_urn():
    """Which is the shape a received invitation may well arrive in: the populated
    element has never been observed, so every field but the urn is optional."""
    text = render_text([{"invitation_urn": "urn:li:fs_invitation:(1,X)"}])
    assert "urn:li:fs_invitation:(1,X)" in text
