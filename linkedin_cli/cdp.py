"""Chrome DevTools Protocol driver over the stdlib WebSocket in `ws.py`.

One job now: read the live cookie jar out of the operator's own running Chrome,
which is the source of the one-time seed. Launching moved to `pipe.py`, because
a browser started with `--remote-debugging-port` listens on loopback and
loopback is not uid-gated - an unprivileged uid in an agent-gateway container
connected to exactly that port with no credentials at all.

Reading the jar here matters more than it sounds: Chrome rewrites its on-disk
cookie database while running, deleting and reinserting `li_at` rather than
updating it in place, so a disk read intermittently returns no token at all or
one LinkedIn has already rotated past. The browser's own memory is the only
authoritative copy.

Correlation is the fiddly part. CDP multiplexes replies and a constant stream of
events onto one socket, so a reply has to be matched by id and everything else
buffered - not simply read as "the next message".
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from .ws import WebSocket

PORT_FILE = "DevToolsActivePort"


class CDPError(Exception):
    exit_code = 6


def read_port_file(profile_dir: str | Path) -> tuple[int, str]:
    """Read the port and browser websocket path Chromium writes on startup."""
    path = Path(profile_dir) / PORT_FILE
    try:
        lines = path.read_text().strip().splitlines()
    except OSError as exc:
        raise CDPError(f"no {PORT_FILE} under {profile_dir}; is the browser running?") from exc
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise CDPError(f"{path} is truncated; the browser may still be starting")
    try:
        return int(lines[0].strip()), lines[1].strip()
    except ValueError as exc:
        raise CDPError(f"{path} does not begin with a port number") from exc


def _http_json(url: str, opener=None) -> Any:
    opener = opener or urllib.request.build_opener()
    request = urllib.request.Request(url, headers={"Host": "localhost"})
    with opener.open(request, timeout=10) as response:
        return json.loads(response.read())


def discover_targets(port: int, host: str = "127.0.0.1", opener=None) -> list[dict]:
    """List targets over HTTP.

    Only works when Chromium was started with an explicit
    `--remote-debugging-port`; the `chrome://inspect` toggle serves no HTTP
    endpoints, and there the browser websocket is the only way in.
    """
    return _http_json(f"http://{host}:{port}/json/list", opener)


def new_target(port: int, url: str, host: str = "127.0.0.1", opener=None) -> dict:
    return _http_json(f"http://{host}:{port}/json/new?{url}", opener)


class CDPSession:
    def __init__(self, ws, session_id: str | None = None):
        self._ws = ws
        self._id = 0
        self._session_id = session_id

    @classmethod
    def attach(cls, ws_url: str, ws_factory=None, session_id: str | None = None) -> CDPSession:
        factory = ws_factory or WebSocket.connect
        return cls(factory(ws_url), session_id)

    def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0) -> dict:
        self._id += 1
        message_id = self._id
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if self._session_id:
            payload["sessionId"] = self._session_id
        self._ws.send(json.dumps(payload))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"timed out waiting for a reply to {method}")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise CDPError(f"timed out waiting for a reply to {method}") from exc
            message = json.loads(raw)
            # Events carry no id, and replies to earlier calls carry a different
            # one. Both must be discarded rather than returned.
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise CDPError(f"{method} failed: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str, *, await_promise: bool = True, timeout: float = 30.0):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise CDPError(f"javascript threw: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def get_cookies(self, urls: list[str]) -> dict[str, str]:
        result = self.call("Network.getCookies", {"urls": urls})
        return {c["name"]: c["value"] for c in result.get("cookies", [])}

    def navigate(self, url: str, *, wait_ms: int = 5000) -> None:
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        time.sleep(wait_ms / 1000)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing must never be the thing that fails
            pass


def cookies_from_running_browser(profile_dir: str | Path, ws_factory=None) -> dict[str, str]:
    """Read LinkedIn's cookies out of an already-running browser.

    Uses the browser-level websocket and attaches to the LinkedIn tab, which is
    the path that works even under Chrome's restricted `chrome://inspect` mode
    where the HTTP endpoints are not served.
    """
    port, path = read_port_file(profile_dir)
    session = CDPSession.attach(f"ws://127.0.0.1:{port}{path}", ws_factory)
    try:
        targets = session.call("Target.getTargets").get("targetInfos", [])
        page = next(
            (t for t in targets if t.get("type") == "page" and "linkedin.com" in t.get("url", "")),
            None,
        )
        if page is None:
            raise CDPError("no linkedin.com tab is open in the running browser")
        attached = session.call(
            "Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}
        )
        session._session_id = attached["sessionId"]
        return session.get_cookies(["https://www.linkedin.com"])
    finally:
        session.close()
