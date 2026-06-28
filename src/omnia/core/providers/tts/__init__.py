"""TTS provider package."""

from __future__ import annotations

from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.factory import (
    available_keyless_tts_providers,
    available_tts_providers,
    available_tts_providers_requiring_api,
    create_tts_provider,
)

__all__ = [
    "TTSProvider",
    "available_keyless_tts_providers",
    "available_tts_providers",
    "available_tts_providers_requiring_api",
    "create_tts_provider",
]
