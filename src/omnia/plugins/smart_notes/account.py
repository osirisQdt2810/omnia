"""Pure helpers for the Smart Notes Account tab: models-in-use + usage merge.

No Anki imports. :func:`models_in_use` gathers the distinct (provider, model/voice) pairs a
collection's Smart Notes config will actually call, per kind (text / image / sound),
substituting the central ``[llm]`` / ``[tts]`` defaults where a field leaves its override
blank. :func:`merge_usage` left-joins those models with the self-tracked usage rows
(:meth:`~omnia.core.providers.usage.JsonUsageRecorder.snapshot`) so the dialog can show
calls + rough char counts beside each model — including any ad-hoc usage whose model isn't
configured on a field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnia.core.config.models import LLMSettings, TTSSettings
    from omnia.plugins.smart_notes.config import SmartNotesSettings

# A field's stored ``type`` (text / image / tts) → the usage "kind" the recorder uses.
_TYPE_TO_KIND = {"text": "text", "image": "image", "tts": "sound"}


def _llm_default_model(llm: LLMSettings, kind: str) -> str:
    """The active LLM subsection's model id for ``kind`` (text/image), or "" when none."""
    active = llm.active()
    if active is None:
        return ""
    field = "text_model" if kind == "text" else "image_model"
    return str(getattr(active, field, "") or "")


def _canonical_llm_provider(provider: str) -> str:
    """Map a config LLM provider id to the provider CLASS name the usage recorder stores.

    The recorder writes each row under ``provider.name`` (a class attribute), so all three
    OpenAI-family config ids (openai / openrouter / openai_compatible) collapse to
    ``"openai_compatible"``. Normalizing the models-in-use join key to that same class name is
    what lets :func:`merge_usage`'s left-join actually attach the recorded counts. An unknown
    id passes through unchanged.
    """
    from omnia.core.providers.llm.factory import _PROVIDER_CLASSES

    cls = _PROVIDER_CLASSES.get(provider)
    return cls.name if cls is not None else provider


def models_in_use(
    settings: SmartNotesSettings, llm: LLMSettings, tts: TTSSettings
) -> dict[str, list[dict]]:
    """Return the distinct (provider, model) pairs in use, grouped by kind.

    Walks every per-note-type field, mapping its ``type`` to a kind (text/image/sound) and
    its (provider, model/voice) — substituting the central default provider/model when a
    field's override is blank. The central default for each kind is always included, so an
    unconfigured collection still lists something.

    Args:
        settings: The Smart Notes per-note-type config.
        llm: The central LLM provider settings.
        tts: The central TTS provider settings.

    Returns:
        ``{"text": [...], "image": [...], "sound": [...]}`` where each entry is
        ``{"provider": p, "model": m}`` (for sound, ``model`` is the voice or "(default)").
    """
    out: dict[str, list[dict]] = {"text": [], "image": [], "sound": []}
    seen: dict[str, set[tuple[str, str]]] = {
        "text": set(),
        "image": set(),
        "sound": set(),
    }

    def add(kind: str, provider: str, model: str) -> None:
        key = (provider, model)
        if key not in seen[kind]:
            seen[kind].add(key)
            out[kind].append({"provider": provider, "model": model})

    # Always list the central defaults so an unconfigured collection shows something. LLM
    # providers are normalized to the class name the recorder stores (see _canonical_llm_provider)
    # so the usage left-join matches; TTS provider ids already match their class name.
    add(
        "text",
        _canonical_llm_provider(llm.provider),
        _llm_default_model(llm, "text") or "(default)",
    )
    add(
        "image",
        _canonical_llm_provider(llm.provider),
        _llm_default_model(llm, "image") or "(default)",
    )
    add("sound", tts.provider, "(default)")

    for note_type in settings.note_types:
        for field in note_type.fields:
            kind = _TYPE_TO_KIND.get(field.type)
            if kind is None:
                continue
            if kind == "sound":
                provider = field.provider or tts.provider
                add("sound", provider, field.voice or "(default)")
            else:
                provider = field.provider or llm.provider
                model = field.model or _llm_default_model(llm, kind) or "(default)"
                add(kind, _canonical_llm_provider(provider), model)
    return out


def merge_usage(models: list[dict], usage_rows: list[dict], kind: str) -> list[dict]:
    """Left-join ``models`` (a kind's models-in-use) with the recorded ``usage_rows``.

    Each model gets ``calls`` / ``in_chars`` / ``out_chars`` / ``last_used_ts`` attached
    from the matching usage row (provider + model), defaulting to 0 / None when unrecorded.
    Any usage row for ``kind`` whose (provider, model) isn't in ``models`` is appended too,
    so ad-hoc usage (e.g. a one-off playground test) still shows.

    Args:
        models: The kind's models-in-use (``{provider, model}`` entries).
        usage_rows: All recorded usage rows (the recorder snapshot).
        kind: The usage kind to join on ("text" / "image" / "sound").

    Returns:
        One row per configured model plus any extra recorded rows, each with usage counts.
    """
    by_key = {
        (row.get("provider", ""), row.get("model", "")): row
        for row in usage_rows
        if row.get("kind") == kind
    }
    merged: list[dict] = []
    matched: set[tuple[str, str]] = set()
    for model in models:
        key = (model["provider"], model["model"])
        matched.add(key)
        merged.append(_with_usage(model["provider"], model["model"], by_key.get(key)))
    for (provider, model), row in by_key.items():
        if (provider, model) not in matched:
            merged.append(_with_usage(provider, model, row))
    return merged


def _with_usage(provider: str, model: str, row: dict | None) -> dict:
    """A merged row: the (provider, model) plus usage counts from ``row`` (0/None if absent)."""
    return {
        "provider": provider,
        "model": model,
        "calls": int(row.get("calls", 0)) if row else 0,
        "in_chars": int(row.get("in_chars", 0)) if row else 0,
        "out_chars": int(row.get("out_chars", 0)) if row else 0,
        "in_tokens": int(row.get("in_tokens", 0)) if row else 0,
        "out_tokens": int(row.get("out_tokens", 0)) if row else 0,
        "last_used_ts": row.get("last_used_ts") if row else None,
    }


# --- default-model picker --------------------------------------------------------------
def default_models(llm: LLMSettings, tts: TTSSettings) -> dict[str, dict]:
    """Return the central default (provider, model) per kind, for the Account default picker.

    These central defaults drive the meta-tasks (language-detect, Auto-prompt, Improve) and
    any field left on "(inherit)". ``text``/``image`` read the active LLM provider + its
    text/image model; ``sound`` reads the active TTS provider + its voice (blank for a
    provider that has no voice, e.g. google_translate).

    Args:
        llm: The central LLM provider settings.
        tts: The central TTS provider settings.

    Returns:
        ``{"text": {provider, model}, "image": {...}, "sound": {provider, model}}`` where the
        sound ``model`` is the configured voice ("" = the provider's default).
    """
    return {
        "text": {"provider": llm.provider, "model": _llm_default_model(llm, "text")},
        "image": {"provider": llm.provider, "model": _llm_default_model(llm, "image")},
        "sound": {"provider": tts.provider, "model": _tts_default_voice(tts.active())},
    }


def _tts_default_voice(tts_active: object | None) -> str:
    """The active TTS provider's stored selectable "voice", mirroring ``_tts_voice_field``.

    Reads the ``voice`` field for providers that have one (a blank voice stays blank — the
    provider's own built-in default), the ``.onnx`` ``model`` for piper (which has no named
    voice), and "" for a voice-less provider (google_translate). Preferring ``voice`` keeps a
    blank voice blank instead of leaking an unrelated ``model`` — e.g. viettts also carries an
    OpenAI-compat ``model="tts-1"`` that is not a voice.
    """
    if tts_active is None:
        return ""
    if hasattr(tts_active, "voice"):
        return str(tts_active.voice or "")
    if hasattr(tts_active, "model"):
        return str(tts_active.model or "")
    return ""


# --- key / secret management (Keys subtab) ---------------------------------------------
# The credentialed LLM providers the Keys subtab manages, in display order. Each spec lists
# the editable credential fields (key, label, kind) and the honest credit story: only
# OpenRouter exposes a live balance to an API key (``credit="live"`` → a real quota bar);
# everyone else keeps quota in their own console (``credit="note"`` → an honest note, never a
# fabricated bar). ``console`` is a [label, url] pair the page opens via the ``open_url`` op.
_KEY_CARD_SPECS: list[dict] = [
    {
        "id": "gemini",
        "label": "Gemini · AI Studio",
        "console": ["Google AI Studio", "https://aistudio.google.com/app/apikey"],
        "credit": "note",
        "note": "No key-accessible quota — usage and limits live in Google AI Studio.",
        "fields": [("api_key", "API key", "secret", "")],
    },
    {
        "id": "gemini_vertex",
        "label": "Gemini · Vertex AI",
        "console": ["Google Cloud Console", "https://console.cloud.google.com/billing"],
        "credit": "note",
        "note": (
            "Pay-as-you-go. The $300 free credit and quotas live in the GCP Console — "
            "they are not fetchable from a service-account key."
        ),
        "fields": [
            ("project", "Project ID", "text", "Read from the JSON if left blank"),
            ("location", "Location", "text", ""),
            ("credentials_path", "Service-account JSON", "file", ""),
            ("access_token", "Access token (optional)", "secret", ""),
        ],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "console": ["OpenRouter credits", "https://openrouter.ai/settings/credits"],
        "credit": "live",
        "note": "",
        "fields": [("api_key", "API key", "secret", "")],
    },
]


def key_cards(llm: LLMSettings) -> list[dict]:
    """Build the Keys subtab cards: each managed LLM provider's credential fields + state.

    Pure: reads the current values from ``llm`` so the page can show (masked) and edit them.
    ``credit`` is ``"live"`` only for OpenRouter (a real balance is fetchable from its key);
    ``"note"`` flags a provider whose quota lives in its console (with an honest ``note``).

    Args:
        llm: The central LLM provider settings.

    Returns:
        One card dict per managed provider:
        ``{id, label, console: [label, url], credit, note, active, fields: [{key, label,
        type, value}]}`` where ``type`` is ``secret`` / ``text`` / ``file``.
    """
    cards: list[dict] = []
    for spec in _KEY_CARD_SPECS:
        sub = getattr(llm, spec["id"], None)
        fields = [
            {
                "key": key,
                "label": label,
                "type": ftype,
                "value": str(getattr(sub, key, "") or ""),
                "placeholder": placeholder,
            }
            for key, label, ftype, placeholder in spec["fields"]
        ]
        cards.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "console": list(spec["console"]),
                "credit": spec["credit"],
                "note": spec["note"],
                "active": llm.provider == spec["id"],
                "fields": fields,
            }
        )
    return cards
