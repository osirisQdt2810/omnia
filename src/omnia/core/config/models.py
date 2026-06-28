"""Pydantic v1 models for Omnia configuration.

Each feature has a typed settings model (validated defaults + bounds), and the provider
settings mirror vio-ai's structure. :class:`OmniaConfig` is the validated whole, assembled
by the :class:`~omnia.core.config.loader.ConfigLoader` from the YAML/TOML files.

Pydantic v1 is used because v2 depends on the compiled (Rust) ``pydantic_core`` wheel, which
is not pure-Python and would break the single cross-platform ``.ankiaddon``. v1 has a
pure-Python core, so it vendors cleanly for both macOS and Windows.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field, validator


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


# --- per-feature settings -------------------------------------------------------------
class AutoFlipDeckOverride(_Strict):
    """Per-deck auto-flip override (keyed by deck id in :class:`AutoFlipSettings`).

    Mirrors the reference add-on's two-flag deck gate (``use_general`` / ``use_deck``):

    * ``use_global=True`` → the deck has an override row but defers to the global delays
      (the reference's ``use_general``); the per-deck delays below are ignored.
    * ``use_global=False`` + ``enabled=True`` → use this row's delays (``use_deck``).
    * ``enabled=False`` → auto-flip is OFF for this deck (``use_deck=False``), regardless of
      ``use_global``.
    """

    use_global: bool = False
    enabled: bool = True
    delay_question_seconds: float = Field(3.0, ge=0)
    delay_answer_seconds: float = Field(3.0, ge=0)


class AutoFlipSettings(_Strict):
    """Settings for the auto-flip feature."""

    delay_question_seconds: float = Field(3.0, ge=0)
    delay_answer_seconds: float = Field(3.0, ge=0)
    wait_for_audio: bool = True
    show_timer: bool = True
    # deck id (as a string) -> override; empty means "use the global delays everywhere".
    per_deck: dict[str, AutoFlipDeckOverride] = Field(default_factory=dict)


class TypedAccuracySettings(_Strict):
    """Settings for the typing-accuracy grader."""

    threshold: float = Field(0.7, ge=0.0, le=1.0)
    # Auto-answer on a pass: "good"/"easy" stage that ease; "no" stages nothing (the user's
    # own press stands). A fail always forces Hard regardless of this setting.
    pass_ease: str = Field("good")
    show_stats: bool = (
        True  # show the interactive accuracy panel on the Statistics screen
    )

    @validator("pass_ease")
    def _validate_pass_ease(cls, value: str) -> str:
        if value not in {"good", "easy", "no"}:
            raise ValueError("pass_ease must be 'good', 'easy', or 'no'")
        return value


class OverdueGuardSettings(_Strict):
    """Settings for the overdue guard."""

    ratio: float = Field(0.8, ge=0.0)
    min_days: int = Field(2, ge=0)
    force_again_after_days: int = Field(7, ge=0)


class DisplayIntervalSettings(_Strict):
    """Settings for the next-interval overlay (currently no options)."""


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


_GENERATION_TYPES = {"text", "image", "tts"}


class SmartNotesFieldRule(_Strict):
    """A single, self-contained generation rule (the per-call shape the engine consumes).

    This is the unit :meth:`GenerationService.generate` operates on: read ``source_field``,
    write ``target_field`` via ``kind``. It is NOT the persisted note-type config (see
    :class:`SmartNotesFieldConfig`) — the engine compiles a note type's enabled fields into
    these rules at generation time, and the one-off custom-prompt palette builds one directly.

    Provider selection stays central (``[llm]`` / ``[tts]`` + the ProviderHub); the
    ``provider``/``model``/``voice`` fields here are optional per-rule OVERRIDES that layer
    on top — empty means "inherit the active central provider".

    Every field has a default, so partial dicts still build.
    """

    note_type: str = ""
    source_field: str = ""
    target_field: str = ""
    kind: str = Field("text")  # text | image | tts
    prompt: str = ""
    deck_id: Optional[int] = None  # None = applies to all decks
    enabled: bool = True
    # Per-field provider overrides (empty = inherit the central [llm]/[tts] config).
    provider: str = ""
    model: str = ""
    voice: str = ""
    # Per-rule overwrite (the note-type config carries the real overwrite flag; the engine
    # threads it onto the compiled rule so skip logic can read it per field).
    overwrite: bool = False

    @validator("kind")
    def _validate_kind(cls, value: str) -> str:
        if value not in _GENERATION_TYPES:
            raise ValueError("kind must be 'text', 'image', or 'tts'")
        return value


class SmartNotesFieldConfig(_Strict):
    """The persisted generation config for ONE field on a note type.

    The base field of a note type is never represented here — it is the input. Every other
    field the user wants generated gets a row: its ``type`` (text/tts/image), a ``prompt``
    template that may reference the base field and other generated fields (``{{Word}}``,
    ``{{Meaning}}``), and optional per-field provider overrides (empty = inherit central
    ``[llm]``/``[tts]``). ``prompt_locked`` protects a hand-written prompt/type from being
    overwritten by the auto-smart generator. ``overwrite`` regenerates the field even when it
    already holds content.
    """

    field: str
    enabled: bool = False
    type: str = "text"  # text | image | tts
    prompt: str = ""
    prompt_locked: bool = False
    provider: str = ""
    model: str = ""
    voice: str = ""
    overwrite: bool = False

    @validator("type")
    def _validate_type(cls, value: str) -> str:
        if value not in _GENERATION_TYPES:
            raise ValueError("type must be 'text', 'image', or 'tts'")
        return value


class SmartNotesNoteTypeConfig(_Strict):
    """Per-note-type smart-notes config: one designated base field + per-field generation rows.

    ``base_field`` is the always-present input (e.g. "Word" — a single word OR a phrase) and is
    never generated. ``fields`` holds one :class:`SmartNotesFieldConfig` per other field the
    user configured. A field's prompt may reference the base field and other generated fields,
    forming a DAG resolved at generation time.
    """

    note_type: str
    base_field: str = ""
    fields: list[SmartNotesFieldConfig] = Field(default_factory=list)

    def generatable_fields(self) -> list[SmartNotesFieldConfig]:
        """Return the fields eligible for generation: enabled and not the base field."""
        return [
            field
            for field in self.fields
            if field.enabled and field.field != self.base_field
        ]


class SmartNotesSettings(_Strict):
    """smart_notes feature settings, organised PER NOTE TYPE (provider config is shared).

    Each :class:`SmartNotesNoteTypeConfig` designates one base (input) field and configures
    how every other field is generated. A fresh, empty config (no ``note_types``) validates,
    so smart_notes ships disabled with no rules and never crashes on load.
    """

    note_types: list[SmartNotesNoteTypeConfig] = Field(default_factory=list)
    # Skip a field whose referenced source fields are ALL blank unless this is True.
    allow_empty_fields: bool = False
    # Whether automatic batch generation regenerates fields it already filled.
    regenerate_when_batching: bool = True
    # Pre-generate a card's empty smart fields ahead of the reviewer (best-effort).
    generate_at_review: bool = False

    def note_type_config(self, note_type: str) -> Optional[SmartNotesNoteTypeConfig]:
        """Return the config for ``note_type``, or None when it has no smart-notes config."""
        for config in self.note_types:
            if config.note_type == note_type:
                return config
        return None


# --- top-level --------------------------------------------------------------------------
class PluginToggle(_Strict):
    """Whether a plugin is enabled."""

    enabled: bool = False


class OmniaConfig(BaseModel):
    """The whole, validated configuration (defaults + user overrides merged)."""

    # Tolerate unknown top-level keys so adding a config file can't crash an old build.
    class Config:
        extra = "ignore"

    log_level: str = "INFO"
    plugins: dict[str, PluginToggle] = Field(default_factory=dict)

    auto_flip: AutoFlipSettings = Field(default_factory=AutoFlipSettings)
    typed_accuracy: TypedAccuracySettings = Field(default_factory=TypedAccuracySettings)
    overdue_guard: OverdueGuardSettings = Field(default_factory=OverdueGuardSettings)
    display_interval: DisplayIntervalSettings = Field(
        default_factory=DisplayIntervalSettings
    )
    smart_notes: SmartNotesSettings = Field(default_factory=SmartNotesSettings)

    llm: LLMSettings = Field(default_factory=LLMSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
