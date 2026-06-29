"""Generic network transports (HTTP + WebSocket), stdlib-only and provider-agnostic.

These are the low-level transports the provider layer is allowed to use. They live here
(rather than under ``core/providers``) because they are not provider-specific: any code
that needs to talk HTTP or WebSocket can depend on them. Like everything in ``core/network``,
they stay pure-Python so the add-on vendors and runs on any OS / Anki-bundled Python.

Dependency direction: ``core/providers/*`` may import ``core/network``, never the reverse.
"""

from __future__ import annotations

from omnia.core.network.http import (
    DEFAULT_HTTP_CLIENT,
    DEFAULT_TIMEOUT,
    HttpClient,
    RetryPolicy,
    UrllibHttpClient,
)
from omnia.core.network.websocket import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_CONTINUATION,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    WebSocketClient,
    WebSocketError,
)

__all__ = [
    "DEFAULT_HTTP_CLIENT",
    "DEFAULT_TIMEOUT",
    "OPCODE_BINARY",
    "OPCODE_CLOSE",
    "OPCODE_CONTINUATION",
    "OPCODE_PING",
    "OPCODE_PONG",
    "OPCODE_TEXT",
    "HttpClient",
    "RetryPolicy",
    "UrllibHttpClient",
    "WebSocketClient",
    "WebSocketError",
]
