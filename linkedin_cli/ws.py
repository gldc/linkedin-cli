"""Minimal RFC 6455 WebSocket client, pure stdlib.

CDP is only reachable over a WebSocket and the stdlib ships no client. Roughly
ninety lines of framing is a better trade than a dependency in a tool whose
entire point is credential isolation - every third-party package here would be
one more thing with read access to a live LinkedIn session.

Only what CDP needs is implemented: text frames, continuations, and the two
control frames a Chromium DevTools endpoint actually sends. Notably:

* Client frames are always masked. Servers close the connection on an unmasked
  client frame, and the failure looks like a network fault rather than a bug.
* All three length encodings are handled because CDP responses routinely exceed
  64 KB, which puts the 8-byte path on the common route, not the exotic one.
* Every read is deadline-bounded across the whole frame, not per `recv` call, so
  a server that trickles bytes cannot extend the timeout indefinitely.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CHUNK = 65536


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, sock: socket.socket, timeout: float = 30.0):
        self._sock = sock
        self._timeout = timeout
        self._buf = bytearray()
        self._closed = False

    @classmethod
    def connect(cls, url: str, *, timeout: float = 30.0, sock: socket.socket | None = None):
        """Open a connection to `ws://host:port/path` and complete the handshake."""
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise WebSocketError(f"only ws:// URLs are supported, got {url!r}")

        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        if sock is None:
            sock = socket.create_connection((host, port), timeout=timeout)
        ws = cls(sock, timeout=timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            sock.sendall(request.encode())
        except OSError as exc:
            raise WebSocketError(f"could not send the WebSocket handshake: {exc}") from exc
        ws._verify_handshake(key, time.monotonic() + timeout)
        return ws

    # ---------------------------------------------------------------- handshake

    def _verify_handshake(self, key: str, deadline: float) -> None:
        head = self._read_until(b"\r\n\r\n", deadline)
        lines = head.decode("latin-1").split("\r\n")

        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            self.close()
            raise WebSocketError(f"WebSocket upgrade refused: {lines[0]}")

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()

        expected = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        # Proof the peer actually spoke RFC 6455 rather than echoing a canned
        # 101 - a cached HTTP response cannot produce this value.
        if headers.get("sec-websocket-accept") != expected:
            self.close()
            raise WebSocketError("server returned a bad Sec-WebSocket-Accept key")

    # -------------------------------------------------------------------- reads

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebSocketError("timed out waiting for the WebSocket peer")
        self._sock.settimeout(remaining)
        try:
            chunk = self._sock.recv(CHUNK)
        except TimeoutError as exc:
            raise WebSocketError("timed out waiting for the WebSocket peer") from exc
        except OSError as exc:
            raise WebSocketError(f"WebSocket read failed: {exc}") from exc
        if not chunk:
            self._closed = True
            raise WebSocketError("the WebSocket connection was closed by the peer")
        self._buf += chunk

    def _read_exact(self, n: int, deadline: float) -> bytes:
        while len(self._buf) < n:
            self._fill(deadline)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_until(self, sep: bytes, deadline: float) -> bytes:
        while sep not in self._buf:
            self._fill(deadline)
        head, _, rest = bytes(self._buf).partition(sep)
        # Whatever followed the separator is already framing data; a server may
        # pipeline the first frame into the same write as the 101 response.
        self._buf = bytearray(rest)
        return head

    def _read_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        b0, b1 = self._read_exact(2, deadline)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]

        mask = self._read_exact(4, deadline) if masked else b""
        payload = self._read_exact(length, deadline)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # ------------------------------------------------------------------- writes

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n <= 125:
            head.append(0x80 | n)
        elif n <= 0xFFFF:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        mask = os.urandom(4)
        head += mask
        return bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def _write(self, opcode: int, payload: bytes) -> None:
        # Reads leave their own remaining budget on the socket, so a write that
        # inherited it would fail on whatever was left of the last recv.
        try:
            self._sock.settimeout(self._timeout)
            self._sock.sendall(self._frame(opcode, payload))
        except OSError as exc:
            self._closed = True
            raise WebSocketError(f"WebSocket write failed: {exc}") from exc

    # -------------------------------------------------------------------- public

    def send(self, text: str) -> None:
        if self._closed:
            raise WebSocketError("cannot send on a closed WebSocket")
        self._write(OP_TEXT, text.encode())

    def recv(self, timeout: float | None = None) -> str:
        """Return the next text message, transparently handling control frames."""
        if self._closed:
            raise WebSocketError("the WebSocket is closed")

        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        parts = bytearray()
        assembling = False

        while True:
            fin, opcode, payload = self._read_frame(deadline)

            if opcode == OP_CLOSE:
                self._closed = True
                raise WebSocketError("the peer sent a WebSocket close frame")
            if opcode == OP_PING:
                # Answered inline: a ping may land between two fragments, and
                # `parts` has to survive it untouched.
                self._write(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_TEXT:
                parts = bytearray(payload)
                assembling = True
            elif opcode == OP_CONT:
                if not assembling:
                    raise WebSocketError("continuation frame with no message to continue")
                parts += payload
            else:
                raise WebSocketError(f"unsupported WebSocket opcode 0x{opcode:x}")

            if fin:
                return parts.decode("utf-8", "replace")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._sock.sendall(self._frame(OP_CLOSE, b""))
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed
