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

import contextlib
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from omnia.core.config.loader import BaseConfigLoader
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
        self, loader: BaseConfigLoader, secrets: Optional[SecretsStore] = None
    ) -> None:
        self._loader = loader
        # Secrets live under the live config dir (``<config_dir>/.secrets``) unless one is
        # injected (bootstrap injects it explicitly; tests pass their own). The reference scheme
        # keeps keys/JSON out of providers.toml.
        self._secrets = secrets or SecretsStore(loader.config_dir / ".secrets")
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

    def section_rejects(self, plugin_id: str, values: dict[str, Any]) -> str:
        """Why merging ``values`` into ``plugin_id``'s section would not parse, or ``""``.

        The write side of :meth:`feature_settings`, and the reason it exists: a section is
        validated LAZILY, so :meth:`update_section` will happily persist a value the plugin's
        own model rejects. Nothing raises at write time; what breaks is everything after it —
        the plugin cannot be activated (``PluginManager._activate`` catches the
        ``ValidationError`` and reports a failed enable), and ``feature_settings`` raises for
        the whole section from then on, so the panel that wrote the value cannot be reopened
        to correct it. The user is left hand-editing TOML.

        The check is the real merge, not a field-by-field one: a model may constrain two
        fields against each other, and a value only valid beside the one already stored would
        pass a per-field check and fail the save.

        Args:
            plugin_id: The plugin whose section is being written.
            values: The keys the caller intends to merge in.

        Returns:
            A message naming the offending fields, or ``""`` when the merge would parse (which
            includes a plugin that declares no ``config_model`` — there is nothing to reject).
        """
        plugin_cls = get_registered().get(plugin_id)
        model_cls = getattr(plugin_cls, "config_model", None) if plugin_cls else None
        if model_cls is None:
            return ""
        merged = {**self.raw_section(plugin_id), **values}
        try:
            model_cls.parse_obj(merged)
        except Exception as exc:
            return _rejection(exc)
        return ""

    def raw_section(self, section: str) -> dict[str, Any]:
        """Return the merged RAW ``section``, unvalidated — the read side of a shallow write.

        :meth:`update_section` merges SHALLOWLY, so a caller that rewrites a whole sub-map has
        to start from what is actually STORED — including the keys and shapes its own model
        cannot parse — or its write deletes them (the ADR-010 hazard, one layer above the
        models). :meth:`feature_settings` cannot serve that: it is all-or-nothing, and a single
        unreadable value makes it raise for the entire section, leaving the caller with nothing
        to merge onto.

        Args:
            section: A top-level key like ``"note_maintenance"`` or ``"llm"``.

        Returns:
            A deep copy of the section as stored (so editing it cannot mutate the repository's
            merged config), or ``{}`` when it is absent or is not a table.
        """
        value = self._merged.get(section)
        return deepcopy(value) if isinstance(value, dict) else {}

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

        Writes ``[llm].provider`` plus the chosen model field on that provider's subsection in
        providers.toml, then reloads. Other fields (api keys, base urls) are left untouched.
        Used by the Account default-model picker (text → ``text_model``, image →
        ``image_model``).

        Goes through ``_provider_table`` for the same reason ``set_provider_fields`` does: a
        custom endpoint lives at ``[llm.custom.<label>]``, and writing it flat produced a
        SECOND table, ``[llm."custom:mine"]``, that nothing reads. The picker accepted the
        choice, the file changed, and the setting had no effect.
        """
        data = self._loader.read_file("providers.toml")
        llm = data.setdefault("llm", {})
        llm["provider"] = provider
        if text_model is not None or image_model is not None:
            sub = self._provider_table(data, "llm", provider)
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
        provider like google_translate, which has no voice field for the value to land in.
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
        sub = self._provider_table(data, domain, provider)
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
    def _provider_table(data: dict, domain: str, provider: str) -> dict:
        """The TOML table a provider's fields live in.

        A user's own endpoint lives one level deeper, under ``[llm.custom.<label>]``, so that
        any number of them can exist without each needing a key of its own at the top of the
        domain — and so a label can never collide with a shipped provider's section.

        A shipped provider's table is created on demand: the name is compiled in, so an absent
        section only means nothing has been written to it yet. A custom one is NOT, because
        there the section IS the endpoint — creating it would bring an endpoint into existence
        as a side effect of writing a field to it.

        That is not hypothetical. Delete the active endpoint and the Account panel keeps the
        id it was holding in a picker built when the dialog opened; changing the model there
        writes ``text_model``, which recreated the section, and a card reappeared in Keys for
        an endpoint with no URL, no key, and a credential already shredded. Removal has to
        stay removed, so every writer but :meth:`add_custom_provider` must find it or fail.

        Raises:
            ValueError: for a custom endpoint that does not exist.
        """
        from omnia.core.config.models import custom_provider_label

        label = custom_provider_label(provider)
        if label:
            table = data.setdefault(domain, {}).setdefault("custom", {})
            if label not in table:
                raise ValueError(f"There is no endpoint called “{label}”.")
            return table[label]
        return data.setdefault(domain, {}).setdefault(provider, {})

    def add_custom_provider(self, domain: str, label: str) -> str:
        """Create an empty endpoint called ``label`` and return its provider id.

        Empty rather than pre-filled: the card that appears is then the thing the user fills
        in, and there is one place the values come from instead of two that could disagree.

        Raises:
            ValueError: on a blank label, a label already in use, or a bad domain. Refusing a
                duplicate rather than merging into it: two endpoints sharing a name would be
                one endpoint, and the second Save would silently overwrite the first.
        """
        if domain not in ("llm", "tts"):
            raise ValueError(f"unknown provider domain: {domain}")
        label = label.strip()
        if not label:
            raise ValueError("Give the endpoint a name.")
        data = self._loader.read_file("providers.toml")
        existing = data.setdefault(domain, {}).setdefault("custom", {})
        if label in existing:
            raise ValueError(f"There is already an endpoint called “{label}”.")
        existing[label] = {"base_url": "", "api_key": "", "text_model": ""}
        self._loader.write_file("providers.toml", data)
        self._reload()
        from omnia.core.config.models import custom_provider_name

        return custom_provider_name(label)

    def remove_custom_provider(self, domain: str, provider: str) -> None:
        """Delete one of the user's endpoints, and the secrets it stored.

        The secrets go too: leaving an orphaned key behind means a credential outliving every
        reference to it, which is the kind of thing nobody ever goes back and cleans up.

        Raises:
            ValueError: when ``provider`` is not a custom endpoint. A shipped provider has no
                delete — removing ``gemini`` would mean removing support for it.
        """
        from omnia.core.config.models import custom_provider_label

        label = custom_provider_label(provider)
        if not label or domain not in ("llm", "tts"):
            raise ValueError(f"not a removable endpoint: {provider}")
        data = self._loader.read_file("providers.toml")
        table = data.get(domain, {}).get("custom", {})
        for field in list(table.get(label, {})):
            with contextlib.suppress(Exception):
                self._secrets.forget(self._secret_name(domain, provider, field))
        table.pop(label, None)
        # Otherwise the domain goes on naming an endpoint that is gone. `active()` returns None,
        # so the hub falls back to a bare `openai_compatible` with no base URL and no key, and
        # every generation — plus language-detect, Auto-prompt and Improve — fails with
        # "requires an api_key", pointing the user at a credential rather than at the endpoint
        # they deleted. A shipped provider at least still reaches "Unknown provider"; the
        # `custom:` branch rewrites the name first and loses even that.
        section = data.setdefault(domain, {})
        if section.get("provider") == provider:
            section["provider"] = self._default_provider(domain)
        self._loader.write_file("providers.toml", data)
        self._reload()

    @staticmethod
    def _default_provider(domain: str) -> str:
        """The provider a domain falls back to, read from the settings model's own default."""
        from omnia.core.config.models import LLMSettings, TTSSettings

        model = LLMSettings if domain == "llm" else TTSSettings
        return str(model.__fields__["provider"].default)

    #: Characters Windows forbids in a filename. A custom endpoint's provider id contains a
    #: colon (``custom:mine``), which is legal on Linux and macOS and not on Windows — where
    #: the write simply fails and the credential is never stored. Caught by the CI matrix's
    #: Windows leg, which is exactly what it is for.
    _UNSAFE_IN_FILENAMES = ':*?"<>|/\\'

    @classmethod
    def _secret_name(cls, domain: str, provider: str, field: str) -> str:
        """The secrets filename for a credential field: ``<domain>.<provider>.<field>``.

        Dotted + domain-prefixed: the domain keeps ``llm.openai.api_key`` and
        ``tts.openai.api_key`` (same provider name across domains) from colliding on one file.

        Characters no filesystem in the matrix accepts are percent-encoded. Encoding rather
        than substituting keeps the mapping injective: collapsing them all to ``-`` made the
        labels ``a:b`` and ``a-b`` share one file, so adding the second endpoint overwrote the
        first one's key. A shipped provider's name contains none of these characters, so its
        filename is unchanged and existing secrets keep resolving.
        """
        # `%` first, or an encoded name and a literal one could still collide.
        safe = provider.replace("%", "%25")
        for char in cls._UNSAFE_IN_FILENAMES:
            safe = safe.replace(char, f"%{ord(char):02X}")
        return f"{domain}.{safe}.{field}"

    def _write_provider_field(
        self, domain: str, provider: str, field: str, value: str
    ) -> None:
        """Write one raw value to ``[<domain>.<provider>].<field>`` and reload."""
        if domain not in ("llm", "tts"):
            raise ValueError(f"unknown provider domain: {domain}")
        data = self._loader.read_file("providers.toml")
        # Through `_provider_table` like every other writer: a custom endpoint nests, and the
        # flat write this used to do produced a second table nothing reads — the setting was
        # accepted, the file changed, and nothing happened. Not reachable today (custom cards
        # declare no file-typed field) which is why it is worth closing now rather than after
        # one does.
        self._provider_table(data, domain, provider)[field] = value
        self._loader.write_file("providers.toml", data)
        self._reload()

    def _resolve_secrets(self) -> None:
        """Replace any ``secret:`` / ``secret-file:`` reference in [llm]/[tts] with its value.

        Runs after every load so providers receive plain credentials; the reference scheme is
        an on-disk concern only.

        Walks the DICT-valued subsections too, which is where the user's own endpoints live.
        Only direct ``BaseModel`` attributes were visited before, so a custom endpoint's
        ``secret:`` reference was handed to the provider verbatim — it authenticated with the
        literal string ``secret:llm.custom-mine.api_key`` and every request was rejected.
        """
        for section in (self._config.llm, self._config.tts):
            for name in type(section).__fields__:
                self._resolve_in(getattr(section, name, None))

    def _resolve_in(self, value: Any) -> None:
        """Replace every credential reference inside ``value``, whatever shape it arrives in.

        One function for both shapes rather than two loops: the next settings model to hold a
        map of subsections gets this for free, and cannot be forgotten the way ``custom`` was.
        """
        if isinstance(value, dict):
            for item in value.values():
                self._resolve_in(item)
            return
        if not isinstance(value, BaseModel):
            return
        for field in type(value).__fields__:
            field_value = getattr(value, field, None)
            if self._secrets.is_ref(field_value):
                setattr(value, field, self._secrets.resolve(field_value))

    @staticmethod
    def _tts_voice_field(provider: str) -> Optional[str]:
        """The settings field a TTS provider stores its selectable voice in (or None).

        Most providers use ``voice``; piper has no named voice — its selectable value is the
        ``.onnx`` model, so it stores into ``model``. A voice-less provider (google_translate)
        returns None: its settings model has neither field, so the value has nowhere to land.
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
        if section == "sync":
            # Per-machine, and it has to stay that way: features.toml lives in the collection,
            # so a synced sharing switch would turn sharing on over there too, and a synced
            # machine key would give two machines one identity.
            return "machine.toml"
        return "features.toml"

    def _reload(self) -> None:
        self._config = self._loader.load()
        self._resolve_secrets()
        self._merged = self._loader.load_merged()


def _rejection(exc: Exception) -> str:
    """Render a validation failure as one line a person can act on.

    Names the FIELD and what it wanted. Pydantic's own ``str(exc)`` is several lines of model
    name, location tuples and a repeated "(type=value_error.number.not_le; limit_value=…)" —
    accurate, and not something to put in a settings panel.
    """
    errors = getattr(exc, "errors", None)
    details: list[str] = []
    if callable(errors):
        try:
            for error in errors():
                where = ".".join(
                    str(part) for part in error.get("loc", ()) if part != ""
                )
                why = str(error.get("msg", "is not valid"))
                details.append(f"{where}: {why}" if where else why)
        except (
            Exception
        ):  # a model that renders its own errors oddly is not worth raising over
            details = []
    if not details:
        details = [" ".join(str(exc).split())[:200]]
    return "; ".join(details[:3])
