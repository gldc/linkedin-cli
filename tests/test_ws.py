"""RFC 6455 client framing, driven against a real loopback socket.

A background thread plays the server on the far end of a `socketpair`, so the
handshake is answered live (the accept key depends on the client's random key)
and every frame the client writes is inspected as raw bytes. No network.
"""

import base64
import hashlib
import socket
import struct
import threading
import time

import pytest

from linkedin_cli.ws import WebSocket, WebSocketError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(sock, n: int) -> bytes:
    """Server-side read that tolerates short reads, same as the client must."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"peer closed after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build an unmasked frame, as a server sends them."""
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n <= 125:
        head += bytes([n])
    elif n <= 0xFFFF:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


def read_client_frame(sock) -> tuple[bool, int, bytes, bytes]:
    """Parse one frame from the client. Returns (fin, opcode, mask, payload)."""
    b0, b1 = recv_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    raw = recv_exact(sock, length)
    if not masked:
        return fin, opcode, b"", raw
    return fin, opcode, mask, bytes(b ^ mask[i % 4] for i, b in enumerate(raw))


class Peer:
    """The server end of a socketpair, handshaken and scripted on a thread."""

    def __init__(self, script=None, *, accept=None, status="HTTP/1.1 101 Switching Protocols"):
        self.sock, self.client_sock = socket.socketpair()
        self.request = b""
        self.error = None
        # Set by `close`. A script that needs to stay quiet waits on this rather
        # than sleeping, so teardown does not pay for the silence a second time.
        self.stopping = threading.Event()
        self._accept_override = accept
        self._status = status
        self._thread = threading.Thread(target=self._run, args=(script,), daemon=True)
        self._thread.start()

    def _run(self, script):
        try:
            while b"\r\n\r\n" not in self.request:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            key = ""
            for line in self.request.decode().split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = self._accept_override
            if accept is None:
                accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
            self.sock.sendall(
                (
                    f"{self._status}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            if script:
                script(self)
        except Exception as exc:  # surfaced by close() so a bug is not silent
            self.error = exc

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        # Order matters, and it is worth two seconds a test. A script parked in
        # `recv` - which is every peer whose client never sent anything - only
        # returns when the other end goes away, so joining first waited out the
        # full timeout on each of them. Closing the client end first gives that
        # `recv` an EOF, and the join is then immediate.
        self.stopping.set()
        self.client_sock.close()
        self._thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def peer():
    made = []

    def make(script=None, **kw):
        p = Peer(script, **kw)
        made.append(p)
        return p

    yield make
    for p in made:
        p.close()


def connect(p, **kw):
    kw.setdefault("timeout", 5.0)
    return WebSocket.connect("ws://127.0.0.1:9222/devtools/page/ABC", sock=p.client_sock, **kw)


# ---------------------------------------------------------------- handshake


def test_handshake_sends_a_valid_upgrade_request(peer):
    p = peer(lambda p: None)
    ws = connect(p)
    lines = p.request.decode().split("\r\n")

    assert lines[0] == "GET /devtools/page/ABC HTTP/1.1"
    headers = {
        line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
        for line in lines[1:]
        if ":" in line
    }
    assert headers["upgrade"].lower() == "websocket"
    assert "upgrade" in headers["connection"].lower()
    assert headers["sec-websocket-version"] == "13"
    assert len(base64.b64decode(headers["sec-websocket-key"])) == 16
    assert not ws.closed


def test_handshake_key_is_random_per_connection(peer):
    keys = set()
    for _ in range(2):
        p = peer(lambda p: None)
        connect(p)
        for line in p.request.decode().split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                keys.add(line.split(":", 1)[1].strip())
    assert len(keys) == 2


def test_handshake_rejects_a_wrong_accept_key(peer):
    p = peer(lambda p: None, accept="aGVsbG8gd29ybGQgaGVsbG8gd28=")
    with pytest.raises(WebSocketError, match="[Aa]ccept"):
        connect(p)


def test_handshake_rejects_a_non_101_status(peer):
    p = peer(lambda p: None, status="HTTP/1.1 403 Forbidden")
    with pytest.raises(WebSocketError, match="403"):
        connect(p)


def test_handshake_rejects_a_non_ws_url(peer):
    p = peer(lambda p: None)
    with pytest.raises(WebSocketError, match="ws://"):
        WebSocket.connect("https://127.0.0.1:9222/x", sock=p.client_sock, timeout=5.0)


def test_frame_pipelined_with_the_handshake_response_is_not_lost(peer):
    """A server may pack a frame into the same write as the 101 response."""

    def script(p):
        p.send(server_frame(0x1, b'{"id":1}'))

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == '{"id":1}'


# ------------------------------------------------------------------- send


def test_client_frames_are_masked(peer):
    seen = {}

    def script(p):
        seen["frame"] = read_client_frame(p.sock)

    p = peer(script)
    ws = connect(p)
    ws.send("hello")
    time.sleep(0.05)
    p.close()

    fin, opcode, mask, payload = seen["frame"]
    assert fin is True
    assert opcode == 0x1
    assert len(mask) == 4
    assert payload == b"hello"


def test_masking_actually_obscures_the_payload(peer):
    """A zero mask would satisfy the unmask check but not the requirement."""
    raw = {}

    def script(p):
        raw["bytes"] = recv_exact(p.sock, 2 + 4 + 5)

    p = peer(script)
    ws = connect(p)
    ws.send("hello")
    time.sleep(0.05)
    p.close()
    assert raw["bytes"][6:] != b"hello"


def test_send_uses_two_byte_length_over_125(peer):
    lengths = {}

    def script(p):
        b0, b1 = recv_exact(p.sock, 2)
        lengths["marker"] = b1 & 0x7F
        lengths["extended"] = struct.unpack("!H", recv_exact(p.sock, 2))[0]

    p = peer(script)
    ws = connect(p)
    ws.send("x" * 200)
    time.sleep(0.05)
    p.close()
    assert lengths == {"marker": 126, "extended": 200}


def test_send_uses_eight_byte_length_over_65535(peer):
    lengths = {}

    def script(p):
        b0, b1 = recv_exact(p.sock, 2)
        lengths["marker"] = b1 & 0x7F
        n = struct.unpack("!Q", recv_exact(p.sock, 8))[0]
        lengths["extended"] = n
        recv_exact(p.sock, 4 + n)  # drain, or sendall blocks on a full pipe

    p = peer(script)
    ws = connect(p)
    ws.send("x" * 70000)
    p.close()
    assert lengths == {"marker": 127, "extended": 70000}


def test_a_timed_out_read_does_not_shrink_the_write_timeout(peer):
    """Reads leave their remaining budget on the socket; writes must not use it."""

    def script(p):
        time.sleep(0.4)
        read_client_frame(p.sock)

    p = peer(script)
    ws = connect(p)
    with pytest.raises(WebSocketError, match="[Tt]imed out"):
        ws.recv(timeout=0.05)
    ws.send("y" * 70000)
    p.close()


def test_send_after_close_raises(peer):
    p = peer(lambda p: None)
    ws = connect(p)
    ws.close()
    with pytest.raises(WebSocketError):
        ws.send("hello")


# ------------------------------------------------------------------- recv


def test_recv_inline_length(peer):
    p = peer(lambda p: p.send(server_frame(0x1, b"short")))
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "short"


def test_recv_two_byte_length(peer):
    body = ("m" * 1000).encode()
    p = peer(lambda p: p.send(server_frame(0x1, body)))
    ws = connect(p)
    assert ws.recv(timeout=2.0) == body.decode()


def test_recv_eight_byte_length(peer):
    """CDP responses routinely exceed 64 KB, so this path is not theoretical."""
    body = ("L" * 100_000).encode()
    p = peer(lambda p: p.send(server_frame(0x1, body)))
    ws = connect(p)
    assert ws.recv(timeout=5.0) == body.decode()


def test_large_payload_round_trip(peer):
    """Echo a >64 KB message: 8-byte length in both directions."""
    result = {}

    def script(p):
        fin, opcode, mask, payload = read_client_frame(p.sock)
        result["opcode"] = opcode
        result["len"] = len(payload)
        p.send(server_frame(0x1, payload))

    p = peer(script)
    ws = connect(p)
    text = "z" * 80_000
    ws.send(text)
    assert ws.recv(timeout=5.0) == text
    assert result == {"opcode": 0x1, "len": 80_000}


def test_recv_decodes_utf8_beyond_ascii(peer):
    body = "café — 日本語".encode()
    p = peer(lambda p: p.send(server_frame(0x1, body)))
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "café — 日本語"


def test_recv_reassembles_continuation_frames(peer):
    def script(p):
        p.send(server_frame(0x1, b"one-", fin=False))
        p.send(server_frame(0x0, b"two-", fin=False))
        p.send(server_frame(0x0, b"three", fin=True))

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "one-two-three"


def test_recv_tolerates_a_short_read(peer):
    """socket.recv may return fewer bytes than the frame header promised."""
    body = ("s" * 4000).encode()

    def script(p):
        blob = server_frame(0x1, body)
        for i in range(0, len(blob), 137):
            p.send(blob[i : i + 137])
            time.sleep(0.002)

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=5.0) == body.decode()


def test_two_messages_arrive_in_order(peer):
    def script(p):
        p.send(server_frame(0x1, b"first") + server_frame(0x1, b"second"))

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "first"
    assert ws.recv(timeout=2.0) == "second"


# ------------------------------------------------------------- control frames


def test_ping_is_answered_with_a_pong(peer):
    got = {}

    def script(p):
        p.send(server_frame(0x9, b"ping-payload"))
        p.send(server_frame(0x1, b"after"))
        got["frame"] = read_client_frame(p.sock)

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "after"
    time.sleep(0.05)
    p.close()

    fin, opcode, mask, payload = got["frame"]
    assert opcode == 0xA
    assert payload == b"ping-payload"
    assert len(mask) == 4


def test_control_frame_between_fragments_does_not_corrupt_reassembly(peer):
    def script(p):
        p.send(server_frame(0x1, b"head-", fin=False))
        p.send(server_frame(0x9, b"interleaved"))
        p.send(server_frame(0xA, b"unsolicited"))
        p.send(server_frame(0x0, b"tail", fin=True))
        read_client_frame(p.sock)

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "head-tail"


def test_pong_is_skipped_while_waiting_for_text(peer):
    def script(p):
        p.send(server_frame(0xA, b"heartbeat"))
        p.send(server_frame(0x1, b"payload"))

    p = peer(script)
    ws = connect(p)
    assert ws.recv(timeout=2.0) == "payload"


# ------------------------------------------------------------------- closing


def test_close_frame_marks_the_socket_closed_and_raises(peer):
    p = peer(lambda p: p.send(server_frame(0x8, b"\x03\xe8")))
    ws = connect(p)
    with pytest.raises(WebSocketError, match="close"):
        ws.recv(timeout=2.0)
    assert ws.closed


def test_recv_after_a_close_frame_raises(peer):
    p = peer(lambda p: p.send(server_frame(0x8, b"")))
    ws = connect(p)
    with pytest.raises(WebSocketError):
        ws.recv(timeout=2.0)
    with pytest.raises(WebSocketError):
        ws.recv(timeout=2.0)


def test_close_sends_a_masked_close_frame(peer):
    got = {}

    def script(p):
        got["frame"] = read_client_frame(p.sock)

    p = peer(script)
    ws = connect(p)
    ws.close()
    time.sleep(0.05)
    p.close()

    fin, opcode, mask, payload = got["frame"]
    assert opcode == 0x8
    assert len(mask) == 4
    assert ws.closed


def test_close_is_idempotent(peer):
    p = peer(lambda p: None)
    ws = connect(p)
    ws.close()
    ws.close()
    assert ws.closed


def test_eof_mid_frame_raises(peer):
    def script(p):
        p.send(server_frame(0x1, b"truncated")[:5])
        p.sock.shutdown(socket.SHUT_WR)

    p = peer(script)
    ws = connect(p)
    with pytest.raises(WebSocketError):
        ws.recv(timeout=2.0)


def test_eof_during_handshake_raises(peer):
    p = Peer(None)
    p.client_sock.sendall(b"")
    p.sock.close()
    with pytest.raises(WebSocketError):
        WebSocket.connect("ws://127.0.0.1:9222/x", sock=p.client_sock, timeout=2.0)
    p.client_sock.close()


# ------------------------------------------------------------------- timeout


def test_recv_honours_its_timeout(peer):
    p = peer(lambda p: p.stopping.wait(1.0))
    ws = connect(p)
    started = time.monotonic()
    with pytest.raises(WebSocketError, match="[Tt]imed out"):
        ws.recv(timeout=0.15)
    assert time.monotonic() - started < 1.0


def test_recv_falls_back_to_the_connect_timeout(peer):
    p = peer(lambda p: p.stopping.wait(1.0))
    ws = WebSocket.connect("ws://127.0.0.1:9222/x", sock=p.client_sock, timeout=0.15)
    with pytest.raises(WebSocketError, match="[Tt]imed out"):
        ws.recv()


def test_timeout_spans_a_whole_frame_not_each_read(peer):
    """A trickling server must not extend the deadline read by read."""

    def script(p):
        p.send(server_frame(0x1, b"x" * 500)[:4])
        p.stopping.wait(1.0)

    p = peer(script)
    ws = connect(p)
    started = time.monotonic()
    with pytest.raises(WebSocketError, match="[Tt]imed out"):
        ws.recv(timeout=0.2)
    assert time.monotonic() - started < 0.9
