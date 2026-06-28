"""Pydantic v1 models for Omnia's CORE configuration (non-plugin).

Holds only the cross-cutting config: the log level, the plugin enable-map, and the LLM/TTS
provider settings — these are core seams, not features. Each FEATURE owns its own settings
model in ``plugins/<plugin>/config.py`` (resolved via the registry by
:meth:`~omnia.core.config.repository.ConfigRepository.feature_settings`), so this module never
imports ``omnia.plugins`` and the coupling rule (``core/* never imports plugins/*``) holds.
:class:`OmniaConfig` validates only the core sections and tolerates the per-plugin sections
(``extra = "ignore"``); the repository keeps the raw merged dict for plugin sections.

Pydantic v1 is used because v2 depends on the compiled (Rust) ``pydantic_core`` wheel, which
is not pure-Python and would break the single cross-platform ``.ankiaddon``. v1 has a
pure-Python core, so it vendors cleanly for both macOS and Windows.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


# --- LLM provider settings ------------------------------------------------------------
# One subsection per provider (mirrors vio-ai's split config): the top-level ``provider``
# selects which subsection is active, so only the active provider's credentials need filling
# in. The model ids (text/image/embedding) are common to every provider, so they live on a
# shared base; each provider subclass adds only its own API auth and tweaks model defaults.
class LLMModelSettings(_Strict):
    """Model ids shared by every LLM provider subsection.

    ``embedding_model`` is reserved for a future embedding feature (no consumer yet).
    """

    text_model: str = ""
    image_model: str = ""
    embedding_model: str = ""


class GeminiVertexLLMSettings(LLMModelSettings):
    """``gemini_vertex``: Google Cloud auth (project + one credential strategy) + model ids.

    Auth — set exactly one strategy: ``credentials_path`` (a service-account JSON key) or a
    pasted short-lived ``access_token``. The add-on never shells out to the ``gcloud`` CLI.
    This is also the Google auth reused by the ``google_cloud`` TTS provider.
    """

    # --- Google auth (also reused by the google_cloud TTS provider) ---
    project: str = ""
    location: str = "global"
    credentials_path: str = ""
    access_token: str = ""
    text_model: str = "gemini-2.5-flash"

    # The auth field names live here, next to the fields, so the google_cloud TTS bridge
    # can derive them instead of duplicating the list elsewhere (avoids drift).
    _AUTH_FIELDS: ClassVar[tuple[str, ...]] = (
        "project",
        "location",
        "credentials_path",
        "access_token",
    )

    def google_auth(self) -> dict[str, Any]:
        """Return just the Google service-account auth fields (for the google_cloud TTS bridge)."""
        return self.dict(include=set(self._AUTH_FIELDS))


class GeminiLLMSettings(LLMModelSettings):
    """``gemini`` (AI Studio): a ``GOOGLE_API_KEY`` + model ids."""

    api_key: str = ""
    text_model: str = "gemini-2.0-flash"


class OpenAICompatibleLLMSettings(LLMModelSettings):
    """``openai`` / ``openrouter`` / ``openai_compatible``: key + base URL + model ids."""

    api_key: str = ""
    base_url: str = ""
    text_model: str = "gpt-4o-mini"
    image_model: str = "gpt-image-1"
    embedding_model: str = "text-embedding-3-small"


class LLMSettings(_Strict):
    """LLM provider selection + per-provider config (one subsection per provider).

    ``provider`` selects the active subsection; :meth:`active` returns it (or None for an
    unknown name, so the factory raises the clear "unknown provider" error lazily rather
    than bricking config load).
    """

    provider: str = "gemini_vertex"
    gemini_vertex: GeminiVertexLLMSettings = Field(
        default_factory=GeminiVertexLLMSettings
    )
    gemini: GeminiLLMSettings = Field(default_factory=GeminiLLMSettings)
    openai: OpenAICompatibleLLMSettings = Field(
        default_factory=lambda: OpenAICompatibleLLMSettings(
            base_url="https://api.openai.com/v1"
        )
    )
    openrouter: OpenAICompatibleLLMSettings = Field(
        default_factory=lambda: OpenAICompatibleLLMSettings(
            base_url="https://openrouter.ai/api/v1", text_model="openai/gpt-4o-mini"
        )
    )
    openai_compatible: OpenAICompatibleLLMSettings = Field(
        default_factory=OpenAICompatibleLLMSettings
    )

    def active(self) -> Optional[BaseModel]:
        """Return the settings subsection for the selected ``provider`` (None if unknown)."""
        sub = getattr(self, self.provider, None)
        return sub if isinstance(sub, BaseModel) else None


# --- TTS provider settings ------------------------------------------------------------
# Same per-provider shape as [llm]: ``provider`` selects the active [tts.<provider>]
# subsection. Unlike the LLM models, TTS providers don't share a common field set (gTTS uses
# lang/tld; piper a model path; openai a key/voice), so each has its own subsection — except
# the openai family, which reuses one model exactly as the LLM side does.
class GoogleTranslateTTSSettings(_Strict):
    """``google_translate`` (free, no key, gTTS-style)."""

    lang: str = "en"
    tld: str = "com"  # domain ("com.vn" nudges a Vietnamese accent)


class OpenAICompatibleTTSSettings(_Strict):
    """``openai`` / ``openrouter`` / ``openai_compatible`` — POSTs ``/audio/speech``."""

    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"


class GoogleCloudTTSSettings(_Strict):
    """``google_cloud`` — reuses the Google service-account auth from ``[llm.gemini_vertex]``."""

    lang: str = "en"
    voice: str = ""
    language_code: str = ""  # BCP-47 override (e.g. "vi-VN")
    speaking_rate: float = 1.0


class EdgeTTSSettings(_Strict):
    """``edge_tts`` — Microsoft Edge neural voices (needs the ``edge-tts`` package)."""

    lang: str = "en"
    voice: str = ""  # e.g. "vi-VN-HoaiMyNeural"


class PiperTTSSettings(_Strict):
    """``piper`` — offline; needs an injected vendored native runner + a ``model`` (.onnx) path.

    The add-on never shells out to a ``piper`` CLI; out of the box the provider raises a clear
    error, so prefer ``google_translate``/``edge_tts`` unless a native runner is injected.
    """

    model: str = ""  # .onnx voice path


class TTSSettings(_Strict):
    """TTS provider selection + per-provider config (one subsection per provider).

    ``provider`` selects the active subsection; :meth:`active` returns it (or None if unknown,
    so the factory raises the clear "unknown provider" error lazily). google_cloud reuses the
    Google auth from ``[llm.gemini_vertex]`` (bridged by the ProviderHub).
    """

    provider: str = "google_translate"
    google_translate: GoogleTranslateTTSSettings = Field(
        default_factory=GoogleTranslateTTSSettings
    )
    openai: OpenAICompatibleTTSSettings = Field(
        default_factory=lambda: OpenAICompatibleTTSSettings(
            base_url="https://api.openai.com/v1"
        )
    )
    openrouter: OpenAICompatibleTTSSettings = Field(
        default_factory=lambda: OpenAICompatibleTTSSettings(
            base_url="https://openrouter.ai/api/v1"
        )
    )
    openai_compatible: OpenAICompatibleTTSSettings = Field(
        default_factory=OpenAICompatibleTTSSettings
    )
    google_cloud: GoogleCloudTTSSettings = Field(default_factory=GoogleCloudTTSSettings)
    edge_tts: EdgeTTSSettings = Field(default_factory=EdgeTTSSettings)
    piper: PiperTTSSettings = Field(default_factory=PiperTTSSettings)

    def active(self) -> Optional[BaseModel]:
        """Return the settings subsection for the selected ``provider`` (None if unknown)."""
        sub = getattr(self, self.provider, None)
        return sub if isinstance(sub, BaseModel) else None


# --- top-level --------------------------------------------------------------------------
class PluginToggle(_Strict):
    """Whether a plugin is enabled."""

    enabled: bool = False


class OmniaConfig(BaseModel):
    """The validated CORE configuration (the cross-cutting, non-plugin sections).

    Only the core seams are typed here: ``log_level``, the plugin enable-map, and the
    ``llm``/``tts`` provider settings. The per-feature sections (``[auto_flip]``,
    ``[typed_accuracy]``, …) are validated separately by each plugin's own ``config_model``
    via :meth:`~omnia.core.config.repository.ConfigRepository.feature_settings`, so this core
    model never imports ``omnia.plugins``. ``extra = "ignore"`` lets those plugin sections
    (and any future top-level key) ride along on the merged dict without tripping validation.
    """

    class Config:
        extra = "ignore"

    log_level: str = "INFO"
    plugins: dict[str, PluginToggle] = Field(default_factory=dict)

    llm: LLMSettings = Field(default_factory=LLMSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
