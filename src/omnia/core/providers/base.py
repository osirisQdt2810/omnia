"""Kind-agnostic provider interface — what an LLM, a TTS engine (and a future kind) share.

The root of ``core/providers`` holds only the pieces that do NOT belong to one kind: a
provider has a name, may or may not need credentials, and knows how to build itself from a
config dict. Each kind's ``base.py`` subclasses :class:`ProviderBase` and adds what is
specific to it (``generate_text`` for LLM; ``synthesize``/``audio_ext``/voices for TTS).
Pure module — stdlib only, no Anki imports.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient


# B024: no abstract methods here on purpose — ``from_config`` must stay concrete (see below).
# ABC is still required: it supplies the ABCMeta metaclass that makes the ``@abstractmethod``
# declarations in the SUBCLASSES enforceable (``LLMProvider.generate_text``,
# ``TTSProvider.synthesize``). Drop it and an incomplete provider instantiates silently.
class ProviderBase(ABC):  # noqa: B024
    """A configurable provider: it has a name, may need credentials, builds from config."""

    # The provider CLASS name — the identity usage rows and the Account tab join on. NOT the
    # registry key: one class may be registered under several config names (the openai family),
    # and every one of them reports this single name.
    name: str = ""
    # Whether this provider needs an API key / credentials to call. False for keyless /
    # offline / open-source providers that must run without any secret. The registry
    # partitions on it; the real-provider test markers derive from that partition.
    requires_api: bool = True

    @classmethod
    def from_config(
        cls, config: dict[str, Any], http: Optional[HttpClient] = None
    ) -> ProviderBase:
        """Build a configured instance from ``config`` (the registry's entry point).

        Each provider reads the keys it needs out of ``config`` and (where it does HTTP) wires
        in the injected ``http`` client. Deliberately NOT an ``@abstractmethod``: a class must
        be registrable before it implements this, and a provider that forgets it should fail
        loudly at BUILD time rather than at import time (an abstract method would break
        collection of every test that merely imports the package).

        Args:
            config: The provider's config subsection (already includes ``provider``).
            http: Optional HTTP client to inject.

        Returns:
            A ready-to-use provider instance.

        Raises:
            NotImplementedError: If the subclass does not override this.
        """
        raise NotImplementedError(f"{cls.__name__} must implement from_config()")
