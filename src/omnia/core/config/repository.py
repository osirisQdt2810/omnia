"""The read/write facade over the typed config.

Plugins and the settings GUI talk to a :class:`ConfigRepository`, never to the raw files.
The core sections (``log_level``/``plugins``/``llm``/``tts``) are validated into an
:class:`OmniaConfig`; the per-feature sections are validated lazily by each plugin's OWN
``config_model``, resolved through the registry. The repository keeps the raw merged dict so
it can hand a plugin's namespace to that model — which is how ``core`` stays decoupled from
``plugins`` (the registry holds plugin classes but lives in ``core``; plugins import IT).
Writes update the owning domain live file (``plugins``→``omnia.toml``, feature sections→
``features.toml``, ``llm``/``tts``→``providers.toml``) and re-validate; there is no override
layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from omnia.core.config.loader import ConfigLoader
from omnia.core.config.models import (
    LLMSettings,
    OmniaConfig,
    TTSSettings,
)
from omnia.core.config.secrets import SecretsStore
from omnia.core.registry import get_registered


class ConfigRepository:
    """Typed config access + persistence to the owning domain live files.

    Provider credentials are kept OUT of the plaintext TOML: the config stores only a
    ``secret:`` / ``secret-file:`` reference (see :class:`SecretsStore`), which this repository
    resolves to the real value/path after every load so the rest of the app — providers, the
    hub — sees plain credentials transparently. Writes go the other way (value → secret file +
    reference), so a secret never lands back in the TOML.
    """

    def __init__(
        self, loader: ConfigLoader, secrets: Optional[SecretsStore] = None
    ) -> None:
        self._loader = loader
        # Secrets live next to the config dir (``<addon>/.secrets``) unless one is injected
        # (tests pass their own). The reference scheme keeps keys/JSON out of providers.toml.
        self._secrets = secrets or SecretsStore(loader.config_dir.parent / ".secrets")
        self._config: OmniaConfig = loader.load()
        self._resolve_secrets()
        # Retained so a plugin's namespace can be validated by its own config_model.
        self._merged: dict[str, Any] = loader.load_merged()

    @property
    def config(self) -> OmniaConfig:
        """The current validated CORE configuration."""
        return self._config

    # --- enabled state --------------------------------------------------------------
    def is_enabled(self, plugin_id: str) -> bool:
        """Return whether ``plugin_id`` is enabled (default False)."""
        toggle = self._config.plugins.get(plugin_id)
        return bool(toggle and toggle.enabled)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        """Persist the enabled flag for ``plugin_id`` (in ``omnia.toml``) and reload."""
        data = self._loader.read_file("omnia.toml")
        data.setdefault("plugins", {}).setdefault(plugin_id, {})["enabled"] = bool(
            enabled
        )
        self._loader.write_file("omnia.toml", data)
        self._reload()

    # --- typed settings access ------------------------------------------------------
    def feature_settings(self, plugin_id: str) -> Optional[BaseModel]:
        """Return the plugin's typed settings, parsed from its raw config namespace.

        Resolves the plugin's ``config_model`` via the registry and validates the merged
        ``[<plugin_id>]`` section against it. Returns None for an unregistered plugin or one
        that declares no ``config_model``. Keeping the lookup in the registry (which lives in
        ``core``) is what lets this stay coupling-clean: ``core/config`` never imports
        ``plugins/*``.
        """
        plugin_cls = get_registered().get(plugin_id)
        model_cls = getattr(plugin_cls, "config_model", None) if plugin_cls else None
        if model_cls is None:
            return None
        return model_cls.parse_obj(self._merged.get(plugin_id, {}))

    def llm_settings(self) -> LLMSettings:
        """Return the LLM provider settings."""
        return self._config.llm

    def tts_settings(self) -> TTSSettings:
        """Return the TTS provider settings."""
        return self._config.tts

    # --- writes (used by the settings GUI) ------------------------------------------
    def update_section(self, section: str, values: dict[str, Any]) -> None:
        """Merge ``values`` into ``section`` in its owning live file and reload.

        ``section`` is a top-level key like ``"auto_flip"`` or ``"llm"``; the owning file is
        resolved by :meth:`_file_for`.
        """
        fname = self._file_for(section)
        data = self._loader.read_file(fname)
        data.setdefault(section, {}).update(values)
        self._loader.write_file(fname, data)
        self._reload()

    def set_active_llm(
        self,
        provider: str,
        *,
        text_model: Optional[str] = None,
        image_model: Optional[str] = None,
    ) -> None:
        """Set the active LLM provider (and optionally its text/image model), preserving creds.

        Writes ``[llm].provider`` plus the chosen model field on the ``[llm.<provider>]``
        subsection in providers.toml, then reloads. Other fields (api keys, base urls) are
        left untouched. Used by the Account default-model picker (text → ``text_model``,
        image → ``image_model``).
        """
        data = self._loader.read_file("providers.toml")
        llm = data.setdefault("llm", {})
        llm["provider"] = provider
        if text_model is not None or image_model is not None:
            sub = llm.setdefault(provider, {})
            if text_model is not None:
                sub["text_model"] = text_model
            if image_model is not None:
                sub["image_model"] = image_model
        self._loader.write_file("providers.toml", data)
        self._reload()

    def set_active_tts(self, provider: str, *, voice: Optional[str] = None) -> None:
        """Set the active TTS provider (and optionally its voice, where the provider has one).

        A blank/None ``voice`` only switches the provider. A non-empty value is written to the
        provider's voice field — ``voice`` for most, but ``model`` for piper (whose selectable
        "voice" is its ``.onnx`` model, not a named voice) — and skipped for a voice-less
        provider like google_translate, whose strict model would reject the unknown key.
        """
        data = self._loader.read_file("providers.toml")
        tts = data.setdefault("tts", {})
        tts["provider"] = provider
        field = self._tts_voice_field(provider)
        if voice and field:
            tts.setdefault(provider, {})[field] = voice
        self._loader.write_file("providers.toml", data)
        self._reload()

    def set_auto_voice(self, lang: str, value: str) -> None:
        """Set (or clear) the global Auto-detect voice for ``lang`` in ``[tts.auto_voices]``.

        ``value`` is a ``"provider:voice"`` string; an empty value removes the language's
        mapping. Persists to providers.toml (the same file ``[tts]`` lives in) and reloads.

        Args:
            lang: The ISO-639-1 language code (e.g. ``"ja"``).
            value: The ``"provider:voice"`` mapping, or ``""`` to delete the language's entry.
        """
        data = self._loader.read_file("providers.toml")
        auto = data.setdefault("tts", {}).setdefault("auto_voices", {})
        if value:
            auto[lang] = value
        else:
            auto.pop(lang, None)
        self._loader.write_file("providers.toml", data)
        self._reload()

    def set_provider_secret(
        self, domain: str, provider: str, field: str, value: str
    ) -> None:
        """Set one NON-secret provider field inline (e.g. ``project`` / ``location``).

        ``domain`` is ``"llm"`` or ``"tts"``. For actual secrets use
        :meth:`set_provider_fields` / :meth:`set_provider_credential_file`, which route the
        value into the secrets store instead of writing it to the TOML.
        """
        self._write_provider_field(domain, provider, field, value)

    def set_provider_fields(
        self,
        domain: str,
        provider: str,
        updates: list[tuple[str, str, str]],
    ) -> None:
        """Persist a batch of provider fields in one write (one Save button per card).

        Each update is ``(field, kind, value)`` where ``kind`` is ``"secret"`` (routed into
        the secrets store, only a ``secret:`` ref written to the TOML), ``"file"`` (skipped —
        files are imported via :meth:`set_provider_credential_file` on Browse), or anything
        else (written inline). One file write + reload for the whole card.
        """
        if domain not in ("llm", "tts"):
            raise ValueError(f"unknown provider domain: {domain}")
        data = self._loader.read_file("providers.toml")
        sub = data.setdefault(domain, {}).setdefault(provider, {})
        for field, kind, value in updates:
            if kind == "file":
                continue
            if kind == "secret":
                sub[field] = (
                    self._secrets.store_value(
                        self._secret_name(domain, provider, field), value
                    )
                    if value
                    else ""
                )
            else:
                sub[field] = value
        self._loader.write_file("providers.toml", data)
        self._reload()

    def set_provider_credential_file(
        self, domain: str, provider: str, field: str, src_path: str
    ) -> str:
        """Import a credential file into the secrets store; return the resolved absolute path.

        Copies ``src_path`` into ``.secrets/<provider>__<field><ext>`` and writes a
        ``secret-file:`` reference to the TOML, so the JSON itself never lives in the config
        dir and the stored value is portable (it follows the add-on, not the source path).
        """
        name = self._secret_name(domain, provider, field) + Path(src_path).suffix
        ref = self._secrets.import_file(name, src_path)
        self._write_provider_field(domain, provider, field, ref)
        return str(self._secrets.resolve(ref))

    @staticmethod
    def _secret_name(domain: str, provider: str, field: str) -> str:
        """The secrets filename for a credential field: ``<domain>.<provider>.<field>``.

        Dotted + domain-prefixed: the domain keeps ``llm.openai.api_key`` and
        ``tts.openai.api_key`` (same provider name across domains) from colliding on one file.
        """
        return f"{domain}.{provider}.{field}"

    def _write_provider_field(
        self, domain: str, provider: str, field: str, value: str
    ) -> None:
        """Write one raw value to ``[<domain>.<provider>].<field>`` and reload."""
        if domain not in ("llm", "tts"):
            raise ValueError(f"unknown provider domain: {domain}")
        data = self._loader.read_file("providers.toml")
        data.setdefault(domain, {}).setdefault(provider, {})[field] = value
        self._loader.write_file("providers.toml", data)
        self._reload()

    def _resolve_secrets(self) -> None:
        """Replace any ``secret:`` / ``secret-file:`` reference in [llm]/[tts] with its value.

        Runs after every load so providers receive plain credentials; the reference scheme is
        an on-disk concern only.
        """
        for section in (self._config.llm, self._config.tts):
            for name in type(section).__fields__:
                sub = getattr(section, name, None)
                if not isinstance(sub, BaseModel):
                    continue
                for field in type(sub).__fields__:
                    value = getattr(sub, field, None)
                    if self._secrets.is_ref(value):
                        setattr(sub, field, self._secrets.resolve(value))

    @staticmethod
    def _tts_voice_field(provider: str) -> Optional[str]:
        """The settings field a TTS provider stores its selectable voice in (or None).

        Most providers use ``voice``; piper has no named voice — its selectable value is the
        ``.onnx`` model, so it stores into ``model``. A voice-less provider (google_translate)
        returns None: its strict settings model would reject either key.
        """
        sub = getattr(TTSSettings(), provider, None)
        if sub is None:
            return None
        fields = type(sub).__fields__
        if "voice" in fields:
            return "voice"
        if "model" in fields:
            return "model"
        return None

    @staticmethod
    def _file_for(section: str) -> str:
        """Return the live file that owns ``section``."""
        if section in ("llm", "tts"):
            return "providers.toml"
        if section in ("log_level", "plugins"):
            return "omnia.toml"
        return "features.toml"

    def _reload(self) -> None:
        self._config = self._loader.load()
        self._resolve_secrets()
        self._merged = self._loader.load_merged()
