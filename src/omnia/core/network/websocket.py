"""A minimal RFC 6455 WebSocket client over a (TLS) socket — standard library only.

A tiny, dependency-free client for the few cases where a provider must speak WebSocket
(Microsoft Edge TTS today). It sits next to :mod:`omnia.core.network.http` — both are the
network transports the provider layer is allowed to use — and, like the HTTP client, it stays
pure-Python so it vendors and runs on any OS / any Anki-bundled Python (no compiled ``aiohttp``).

Scope is deliberately small: connect, send masked text frames (client->server frames MUST be
masked), reassemble server data frames, answer pings, and stop on close. No compression is
negotiated, so server frames arrive uncompressed. Not a general-purpose library — just enough.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

# Frame opcodes (RFC 6455 §5.2) callers match against the first element of recv_message().
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 handshake magic
_MAX_HANDSHAKE_BYTES = 1 << 16


class WebSocketError(Exception):
    """A handshake, transport, or framing failure."""


class WebSocketClient:
    """A blocking WebSocket client speaking ``ws://`` or ``wss://`` over a socket."""

    def __init__(
        self, url: str, headers: dict[str, str], timeout: float = 30.0
    ) -> None:
        parts = urlsplit(url)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                ctx = ssl.create_default_context()
                self._sock: socket.socket = ctx.wrap_socket(raw, server_hostname=host)
            else:
                self._sock = raw
            self._sock.settimeout(timeout)
            self._handshake(host, path, headers)
        except Exception:
            raw.close()
            raise

    # --- handshake ----------------------------------------------------------------------
    def _handshake(self, host: str, path: str, headers: dict[str, str]) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

        response = self._read_until(b"\r\n\r\n")
        status_line = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "101" not in status_line:
            raise WebSocketError(f"WebSocket upgrade failed: {status_line!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected.encode("ascii") not in response:
            raise WebSocketError("WebSocket handshake accept-key mismatch")

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            buf += chunk
            if len(buf) > _MAX_HANDSHAKE_BYTES:
                raise WebSocketError("handshake response too large")
        return buf

    # --- frame I/O ----------------------------------------------------------------------
    def _recv_exactly(self, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = self._sock.recv(count - len(buf))
            if not chunk:
                raise WebSocketError("connection closed mid-frame")
            buf += chunk
        return buf

    def send_text(self, text: str) -> None:
        """Send a complete text message as one masked frame (FIN set)."""
        self._send_frame(OPCODE_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x80 | opcode])  # FIN + opcode
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)  # MASK bit + 7-bit length
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        self._sock.sendall(bytes(header) + mask + masked)

    def recv_message(self) -> tuple[int, bytes]:
        """Return ``(opcode, payload)`` for the next complete message.

        Reassembles continuation frames, transparently answers pings, and reports a close as
        ``(OPCODE_CLOSE, payload)``. ``opcode`` is the data type of the first frame.
        """
        message = bytearray()
        first_opcode = 0
        while True:
            b1, b2 = self._recv_exactly(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exactly(8))[0]
            payload = self._recv_exactly(length) if length else b""
            if b2 & 0x80:  # servers never mask, but unmask defensively
                mask, body = payload[:4], payload[4:]
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(body))
            if opcode == OPCODE_PING:
                self._send_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_CLOSE:
                return OPCODE_CLOSE, payload
            if opcode != OPCODE_CONTINUATION:
                first_opcode = opcode
            message += payload
            if fin:
                return first_opcode, bytes(message)

    def close(self) -> None:
        """Best-effort close handshake, then close the socket."""
        with contextlib.suppress(OSError):
            self._send_frame(OPCODE_CLOSE, b"")
        with contextlib.suppress(OSError):
            self._sock.close()
