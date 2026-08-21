"""LLM provider interface (adapted from vio-ai's ``LLMProvider``).

Pure module — no Anki imports. Concrete providers live alongside and are built by their
``@register_llm`` registration (:mod:`omnia.core.providers.llm.registry`).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from omnia.core.providers.base import ProviderBase
from omnia.core.providers.errors import ProviderError


@dataclass(frozen=True)
class PromptParts:
    """A prompt split into the part that repeats across calls and the part that does not.

    ``prefix + suffix`` is EXACTLY the string the un-split path would have sent — the split is
    lossless by construction, which is what lets a provider that cannot cache behave
    byte-identically without anyone having to remember to make it so.

    Every note of a note type is generated from the same prompt TEMPLATE; only the interpolated
    values differ. Keeping that repeated instruction head in its own part is all a provider
    prefix cache needs, and it is the only thing this type exists to express.
    """

    prefix: str
    suffix: str

    def joined(self) -> str:
        """The exact prompt string an un-split call would have sent."""
        return self.prefix + self.suffix


class LLMProvider(ProviderBase):
    """Generates text (and optionally images) from a prompt.

    Adding a provider means subclassing this and registering it with ``@register_llm`` — no
    feature code changes (ADR-004). ``name`` / ``requires_api`` / ``from_config`` come from
    :class:`~omnia.core.providers.base.ProviderBase`.
    """

    # Whether this provider does anything with a prompt's cacheable prefix. False means
    # ``generate_cached_text`` concatenates and sends today's exact request — correct, just
    # unaccelerated. Declared per class so a caller (the benchmark, a future settings hint) can
    # report which providers actually participate instead of guessing from the provider name.
    supports_prompt_cache: ClassVar[bool] = False

    # Whether this provider can be ASKED for JSON matching a schema. False means
    # ``generate_json`` is an ordinary text call and the caller parses defensively — which it
    # must do regardless, because a schema is a strong hint and never a guarantee.
    supports_json_output: ClassVar[bool] = False

    # The token usage of the most recent call, when the provider's response reports it:
    # ``{"in": prompt_tokens, "out": completion_tokens, "total": total_tokens}``, plus
    # ``"cached"`` when the response reports how much of the input was served from a prompt
    # cache. None when
    # the provider/response carries no usage. The usage recorder reads this to log exact
    # tokens (not just character approximations). Set by each concrete provider per call.
    #
    # It is per-INSTANCE mutable state and the hub hands ONE cached instance to every concurrent
    # note, so it is racy for any external reader. Nothing new may depend on it: return a call's
    # data through the return value, as ``*_with_usage`` and ``generate_cached_text`` do.
    # Stashing a call's result on the provider and reading it back afterwards is exactly the
    # shape that produces "wrong output, no error" once two notes overlap.
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

    def generate_cached_text(
        self,
        parts: PromptParts,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Return ``(text, usage)``, giving the provider a chance to cache ``parts.prefix``.

        A NEW method rather than a keyword on :meth:`generate_text`: dozens of fakes implement
        that signature exactly, and a new keyword from a shared call site would ``TypeError``
        every one of them. This way an implementation opts in by overriding, and everything
        else inherits a default that concatenates and delegates — i.e. today's exact call, with
        today's exact payload on the wire.

        Args:
            parts: The prompt, split into its repeated prefix and its per-call suffix.
            system: Optional system / instruction message.
            temperature: Sampling temperature; ``None`` uses the provider's configured default.
            max_tokens: Optional output token cap.

        Returns:
            The generated text and this call's token usage (``None`` when unreported).

        Raises:
            ProviderError: On bad config or an HTTP/network failure.
        """
        return self.generate_text_with_usage(
            parts.joined(),
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def generate_json(
        self,
        parts: PromptParts,
        *,
        schema: dict[str, Any],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, int]]]:
        """Return ``(text, usage)`` for a prompt whose answer should match ``schema``.

        The default IGNORES the schema and delegates to :meth:`generate_cached_text`, i.e. it is
        an ordinary (prefix-cacheable) text call. That is the right default because the schema
        is an optimisation the caller's contract must never depend on: even a provider with a
        native JSON mode can return a refusal or a near-miss, so whatever asks for JSON has to
        parse defensively anyway. A provider that can enforce the shape overrides this and makes
        the parse succeed more often — it does not make the parse optional.

        Args:
            parts: The prompt, split into its repeated prefix and its per-call suffix.
            schema: A vendor-neutral JSON Schema for the expected answer. Each provider adapts
                it to its own wire format; a provider without a JSON mode ignores it.
            system: Optional system / instruction message.
            temperature: Sampling temperature; ``None`` uses the provider's configured default.
            max_tokens: Optional output token cap.

        Returns:
            The generated text (expected, not guaranteed, to be JSON) and this call's usage.

        Raises:
            ProviderError: On bad config or an HTTP/network failure.
        """
        return self.generate_cached_text(
            parts, system=system, temperature=temperature, max_tokens=max_tokens
        )

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
