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
    # The token usage of the most recent call, when the provider's response reports it:
    # ``{"in": prompt_tokens, "out": completion_tokens, "total": total_tokens}``. None when
    # the provider/response carries no usage. The usage recorder reads this to log exact
    # tokens (not just character approximations). Set by each concrete provider per call.
    last_usage: Optional[dict[str, int]] = None

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Return the model's text completion for ``prompt``.

        The text model and the default ``temperature`` are fixed at construction (``__init__``);
        to use a different model, build a provider configured with it (see ``ProviderHub.llm``).

        Args:
            prompt: The user prompt.
            system: Optional system / instruction message.
            temperature: Sampling temperature; ``None`` uses the provider's configured default.
            max_tokens: Optional output token cap.

        Raises:
            ProviderError: On bad config or an HTTP/network failure.
        """

    def generate_text_with_usage(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Return ``(text, usage)`` for one text generation.

        The usage is returned per call, so a caller (the recording wrapper) attributes token
        counts to THIS call rather than reading shared ``last_usage`` state — which a concurrent
        generation on the same cached instance could clobber. The default delegates to
        :meth:`generate_text` and reports ``last_usage``; a provider that makes one HTTP call
        overrides this to return the usage parsed from that call's response.
        """
        text = self.generate_text(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        return text, self.last_usage

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

    def generate_image_with_usage(
        self, prompt: str, *, size: str = "1024x1024"
    ) -> tuple[bytes, Optional[dict[str, int]]]:
        """Return ``(image_bytes, usage)`` for one image generation.

        The image-generation counterpart of :meth:`generate_text_with_usage`: usage flows back
        via the return value so recording never depends on shared ``last_usage`` state.
        """
        data = self.generate_image(prompt, size=size)
        return data, self.last_usage
