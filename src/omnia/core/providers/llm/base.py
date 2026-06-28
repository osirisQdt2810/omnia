"""LLM provider interface (adapted from vio-ai's ``LLMProvider``).

Pure module — no Anki imports. Concrete providers live alongside and are built by
:mod:`omnia.core.providers.llm.factory`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from omnia.core.providers.errors import ProviderError


class LLMProvider(ABC):
    """Generates text (and optionally images) from a prompt.

    Adding a provider means subclassing this and registering it in the factory — no feature
    code changes (ADR-004).
    """

    name: str = ""
    # Whether this provider needs an API key / credentials to call. False for keyless /
    # offline / open-source providers that must run without any secret. Used to classify
    # providers (factory: requiring-api vs keyless) and to derive test markers.
    requires_api: bool = True

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Return the model's text completion for ``prompt``.

        The text model is fixed at construction (``__init__``); to use a different model,
        build a provider configured with it (see ``ProviderHub.llm``).

        Args:
            prompt: The user prompt.
            system: Optional system / instruction message.
            temperature: Sampling temperature.
            max_tokens: Optional output token cap.

        Raises:
            ProviderError: On bad config or an HTTP/network failure.
        """

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        """Return PNG/JPEG bytes for ``prompt``. Optional; not all providers support it.

        The image model is fixed at construction (``__init__``).

        Args:
            prompt: The image prompt.
            size: Requested image size (provider-specific; ignored where unsupported).
        """
        raise ProviderError(
            f"{self.name or type(self).__name__} does not support images"
        )
