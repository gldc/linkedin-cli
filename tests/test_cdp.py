"""CDP driver: message correlation, target discovery, launch flags.

No real browser and no real socket. A scripted fake stands in for the WebSocket
so the correlation logic - which is the part that actually goes wrong - is
exercised deterministically.
"""

from __future__ import annotations

import json

import pytest

from linkedin_cli import cdp


class FakeWS:
    """Replays a scripted server side, keyed by the method that was sent."""

    def __init__(self, script: dict, events: list | None = None):
        self.script = script
        self.pending: list[str] = [json.dumps(e) for e in (events or [])]
        self.sent: list[dict] = []
        self.closed = False

    def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        reply = self.script.get(msg["method"], {})
        if isinstance(reply, Exception):
            raise reply
        body = {"id": msg["id"]}
        body.update(reply)
        self.pending.append(json.dumps(body))

    def recv(self, timeout=None) -> str:
        if not self.pending:
            raise TimeoutError("no more scripted messages")
        return self.pending.pop(0)

    def close(self) -> None:
        self.closed = True


def session(script, events=None):
    ws = FakeWS(script, events)
    return cdp.CDPSession(ws), ws


def test_call_returns_the_matching_result():
    s, _ = session({"Runtime.evaluate": {"result": {"result": {"value": 42}}}})
    assert s.call("Runtime.evaluate")["result"]["value"] == 42


def test_call_skips_interleaved_events():
    """CDP emits events constantly; they must not be mistaken for a reply."""
    events = [
        {"method": "Network.requestWillBeSent", "params": {}},
        {"method": "Page.frameNavigated", "params": {}},
    ]
    s, _ = session({"Target.getTargets": {"result": {"targetInfos": []}}}, events)
    assert s.call("Target.getTargets") == {"targetInfos": []}


def test_call_skips_a_reply_to_a_different_id():
    s, ws = session({})
    ws.pending.append(json.dumps({"id": 999, "result": {"stale": True}}))
    ws.script["Foo.bar"] = {"result": {"fresh": True}}
    assert s.call("Foo.bar") == {"fresh": True}


def test_ids_are_monotonic_and_never_reused():
    s, ws = session({"A.b": {"result": {}}, "C.d": {"result": {}}})
    s.call("A.b")
    s.call("C.d")
    ids = [m["id"] for m in ws.sent]
    assert ids == sorted(set(ids)) and len(ids) == 2


def test_error_replies_raise():
    s, _ = session({"Bad.method": {"error": {"code": -32000, "message": "nope"}}})
    with pytest.raises(cdp.CDPError) as exc:
        s.call("Bad.method")
    assert "nope" in str(exc.value)


def test_evaluate_returns_the_value():
    s, _ = session({"Runtime.evaluate": {"result": {"result": {"value": {"a": 1}}}}})
    assert s.evaluate("({a:1})") == {"a": 1}


def test_evaluate_raises_on_a_javascript_exception():
    s, _ = session(
        {
            "Runtime.evaluate": {
                "result": {"exceptionDetails": {"text": "Uncaught", "lineNumber": 3}}
            }
        }
    )
    with pytest.raises(cdp.CDPError):
        s.evaluate("boom()")


def test_get_cookies_shapes_into_a_plain_mapping():
    s, _ = session(
        {
            "Network.getCookies": {
                "result": {
                    "cookies": [
                        {"name": "li_at", "value": "AQEDsynthetic"},
                        {"name": "JSESSIONID", "value": '"ajax:1111222233334444555"'},
                    ]
                }
            }
        }
    )
    got = s.get_cookies(["https://www.linkedin.com"])
    assert got["li_at"] == "AQEDsynthetic"
    assert got["JSESSIONID"] == '"ajax:1111222233334444555"'


# ------------------------------------------------------------------ port file


def test_devtools_port_file_is_parsed(tmp_path):
    f = tmp_path / "DevToolsActivePort"
    f.write_text("9222\n/devtools/browser/abc-123\n")
    port, path = cdp.read_port_file(tmp_path)
    assert (port, path) == (9222, "/devtools/browser/abc-123")


def test_truncated_port_file_raises(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9222\n")
    with pytest.raises(cdp.CDPError):
        cdp.read_port_file(tmp_path)


def test_missing_port_file_raises(tmp_path):
    with pytest.raises(cdp.CDPError):
        cdp.read_port_file(tmp_path)
