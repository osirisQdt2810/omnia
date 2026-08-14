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

#: Config name -> API base URL. ``openai_compatible`` points at OpenAI's own endpoint because it
#: is the "some other server speaking this protocol" name: without an explicit ``base_url`` the
#: only sane guess is the reference implementation.
OPENAI_FAMILY_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "https://api.openai.com/v1",
}


def openai_family_base_url(config: Mapping[str, Any]) -> str:
    """Return the API base URL an openai-family config should call.

    Args:
        config: A provider config. An explicit non-empty ``base_url`` always wins; otherwise the
            ``provider`` name selects the default. The whole config is passed rather than just
            the name because one class serves three names, so it cannot be told which it is any
            other way.

    Returns:
        The base URL, falling back to OpenAI's own endpoint for an unrecognised name.
    """
    explicit = config.get("base_url")
    if explicit:
        return str(explicit)
    provider = config.get("provider", "openai_compatible")
    return OPENAI_FAMILY_BASE_URLS.get(provider, OPENAI_FAMILY_BASE_URLS["openai"])
