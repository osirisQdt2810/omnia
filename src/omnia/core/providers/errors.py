"""Provider error type."""

from __future__ import annotations

from typing import Optional


class ProviderError(RuntimeError):
    """Raised when an LLM/TTS provider fails (bad config, HTTP error, unsupported op).

    ``status_code`` carries the upstream HTTP status when the failure came from a response
    (e.g. 429 quota, 5xx transient), so callers/tests can distinguish a transient/quota limit
    from a wiring bug without parsing the message.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
