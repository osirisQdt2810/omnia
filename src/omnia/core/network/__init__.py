"""Generic network transports (HTTP + WebSocket) and the provider bound, stdlib-only.

These are the low-level transports the provider layer is allowed to use. They live here
(rather than under ``core/providers``) because they are not provider-specific: any code
that needs to talk HTTP or WebSocket can depend on them. Like everything in ``core/network``,
they stay pure-Python so the add-on vendors and runs on any OS / Anki-bundled Python.

Dependency direction: ``core/providers/*`` may import ``core/network``, never the reverse — with
one deliberate exception, ``http`` raising a :class:`~omnia.core.providers.errors.ProviderError`.
That exception is why the names below are resolved **lazily** (PEP 562, the same mechanism
:mod:`omnia.envs` uses) instead of being imported when this package is. Importing them eagerly
made ``import omnia.core.network.limiter`` — a module with no dependencies at all — run
``http``, and therefore the whole provider package, and therefore ``http`` again while it was
still half-built: a plain ``ImportError`` for whichever module happened to be imported first.
Nothing about the public surface changes; ``from omnia.core.network import HttpClient`` still
works, and now costs only the submodule it actually needs.
"""

from __future__ import annotations

from typing import Any

# Public name -> the submodule that defines it. One dict rather than one import per name, so a
# new export is one line and cannot drift from __all__.
_EXPORTS: dict[str, str] = {
    "DEFAULT_HTTP_CLIENT": "http",
    "DEFAULT_TIMEOUT": "http",
    "HttpClient": "http",
    "RetryPolicy": "http",
    "ThrottledHttpClient": "http",
    "UrllibHttpClient": "http",
    "PROVIDER_LIMITER": "limiter",
    "LimiterStats": "limiter",
    "ProviderLimiter": "limiter",
    "OPCODE_BINARY": "websocket",
    "OPCODE_CLOSE": "websocket",
    "OPCODE_CONTINUATION": "websocket",
    "OPCODE_PING": "websocket",
    "OPCODE_PONG": "websocket",
    "OPCODE_TEXT": "websocket",
    "WebSocketClient": "websocket",
    "WebSocketError": "websocket",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    # Resolved at access time, not at import time — see the module docstring.
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return list(__all__)
