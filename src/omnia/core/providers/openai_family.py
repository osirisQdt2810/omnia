"""Base URLs for the OpenAI-compatible family — one table, shared by every provider kind.

``openai``, ``openrouter`` and ``openai_compatible`` are three CONFIG NAMES served by a single
class on each side of the provider layer (``llm.OpenAICompatibleProvider`` and
``tts.OpenAICompatibleTTS``), so each side has to turn the configured name into a base URL. That
mapping describes the vendors' HTTP endpoints — not text, not audio — which makes it the one
genuinely kind-agnostic piece of provider data here, and therefore something that belongs at the
``providers/`` root rather than inside a kind.

It lived twice, byte for byte, in ``llm/openai_compatible.py`` and ``tts/openai_compatible.py``.
Two copies of one vendor fact is a fact that goes stale on one side: change OpenRouter's base URL
where you happen to be reading, ship, and the other kind keeps posting to the old host — with a
valid key, so it fails as a confusing HTTP error rather than as a configuration problem.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnia.core.providers.errors import ProviderError

#: Config name -> API base URL. Only for the names that HAVE one: a vendor's endpoint is a fact
#: about that vendor, so guessing it is safe.
#:
#: ``openai_compatible`` is deliberately absent. It used to default to OpenAI's own endpoint on
#: the reasoning that the reference implementation is the only sane guess — but the name means
#: "some other server", and a user who picks it and leaves Base URL blank has not asked for
#: OpenAI. Guessing there sends their prompts, and the key they typed, to a third party they
#: never named. No default is the only honest answer.
OPENAI_FAMILY_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def openai_family_base_url(config: Mapping[str, Any]) -> str:
    """Return the API base URL an openai-family config should call.

    Args:
        config: A provider config. An explicit non-empty ``base_url`` always wins; otherwise the
            ``provider`` name selects the default. The whole config is passed rather than just
            the name because one class serves three names, so it cannot be told which it is any
            other way.

    Returns:
        The base URL, or ``""`` when there is nothing to guess — a name meaning "some other
        server" with no address configured. Empty rather than raising, because building a
        provider must stay total: the metadata sweep constructs every registered one from an
        empty config. The complaint belongs at the call. See :func:`require_base_url`.
    """
    explicit = config.get("base_url")
    if explicit:
        return str(explicit)
    provider = str(config.get("provider", "") or "")
    return OPENAI_FAMILY_BASE_URLS.get(provider, "")


def require_base_url(base_url: str) -> str:
    """Return ``base_url``, or raise the one message that says how to fix it.

    On the request path rather than at construction. An empty base URL used to become OpenAI's
    own endpoint, so a user who picked "self-hosted" and left the field blank shipped their
    prompts and their key to a third party they never named — silently, and successfully, which
    is worse than a failure.

    Raises:
        ProviderError: when there is no address to call.
    """
    if base_url:
        return base_url
    raise ProviderError(
        "This provider has no Base URL set. Fill it in under Usage & keys — it is the "
        "address of your own server, so there is nothing sensible to guess."
    )
