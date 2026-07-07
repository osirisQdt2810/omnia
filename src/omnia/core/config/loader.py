"""Load + merge the config into a validated :class:`OmniaConfig`, via swappable backends.

The config is split across three domains — ``omnia`` (``log_level`` + ``[plugins.*]``),
``features`` (per-feature sections) and ``providers`` (``[llm]``/``[tts]`` + secret refs).
:class:`ConfigRepository` reads/writes them through a filename-keyed contract
(``read_file``/``write_file``/``load``/``load_merged``/``ensure_live_files``), which
:class:`BaseConfigLoader` formalises so the STORAGE can be swapped without touching the
repository or any caller.

Two independent backends implement that ABC (neither wraps the other; they share only the
stateless module helpers below):

* :class:`TomlConfigLoader` (alias :data:`ConfigLoader`) keeps all three domains in live TOML
  files under ``config/`` (seeded from the tracked ``*.example.toml`` templates).
* :class:`CollectionConfigLoader` keeps ``omnia``/``features`` in the Anki COLLECTION
  (``col.get_config``/``set_config``, synced across devices) and reads/writes ``providers.toml``
  from disk directly (credentials must never enter a synced collection). It NEVER reads the
  ``omnia``/``features`` files — the two worlds are separate, created fresh from defaults.

:func:`build_config_loader` maps a backend name (``"database"`` | ``"toml"``) to a loader; at
runtime the ``PersistenceDispatcher`` (``core/config/dispatch.py``) owns the selection from
``OMNIA_CONFIG_STORAGE``. TOML is read with the stdlib ``tomllib`` (Python 3.11+) and written
with ``tomli_w``.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

try:  # stdlib on the add-on's Python 3.13; tomli is the fallback for <3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from omnia.core.config.models import OmniaConfig


# --- stateless helpers (shared by BOTH backends; no state, no cross-backend coupling) ------
def read_toml(path: Path) -> dict[str, Any]:
    """Return the parsed contents of the TOML file at ``path`` (``{}`` if absent)."""
    if not path.exists():
        return {}
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def write_toml(path: Path, data: dict[str, Any]) -> None:
    """Serialise ``data`` to the TOML file at ``path`` (creating parents as needed)."""
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        tomli_w.dump(data, handle)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (override wins; ``base`` untouched)."""
    result = dict(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


class BaseConfigLoader(ABC):
    """The filename-keyed config contract :class:`ConfigRepository` depends on."""

    @property
    @abstractmethod
    def config_dir(self) -> Path:
        """The directory holding the on-disk config files (and secrets sibling)."""

    @abstractmethod
    def ensure_live_files(self) -> None:
        """Create any missing on-disk live file this backend owns."""

    @abstractmethod
    def load(self) -> OmniaConfig:
        """Read + merge the config and validate into an :class:`OmniaConfig`."""

    @abstractmethod
    def load_merged(self) -> dict[str, Any]:
        """Return the fully merged raw config dict (omnia → features → providers)."""

    @abstractmethod
    def read_file(self, name: str) -> dict[str, Any]:
        """Return the parsed contents of the ``name`` domain (or ``{}``)."""

    @abstractmethod
    def write_file(self, name: str, data: dict[str, Any]) -> None:
        """Persist ``data`` for the ``name`` domain."""


class TomlConfigLoader(BaseConfigLoader):
    """File backend: all three domains live in live TOML files under ``config_dir``."""

    LIVE_FILES = ("omnia.toml", "features.toml", "providers.toml")

    def __init__(self, config_dir: Path, *, template_dir: Path | None = None) -> None:
        """Initialise the loader.

        Args:
            config_dir: The LIVE directory holding the domain config files (all reads and
                writes happen here).
            template_dir: Directory holding the tracked ``*.example.toml`` templates used to
                seed any missing live file. Defaults to ``config_dir`` (single-dir back-compat).
        """
        self._config_dir = config_dir
        self._template_dir = template_dir or config_dir

    @property
    def config_dir(self) -> Path:
        """The LIVE directory holding the domain config files."""
        return self._config_dir

    def ensure_live_files(self) -> None:
        """Seed any missing live file by copying its template from ``template_dir``."""
        for name in self.LIVE_FILES:
            live = self._config_dir / name
            template = self._template_dir / name.replace(".toml", ".example.toml")
            if not live.exists() and template.exists():
                self._config_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(template, live)

    def load(self) -> OmniaConfig:
        """Read + merge the live files and validate into an :class:`OmniaConfig`."""
        return OmniaConfig.parse_obj(self.load_merged())

    def load_merged(self) -> dict[str, Any]:
        """Return the fully merged raw config dict (omnia → features → providers).

        The repository keeps this so per-plugin sections (``[auto_flip]``, …) — which
        :class:`OmniaConfig` ignores — can be validated by each plugin's own ``config_model``.
        """
        self.ensure_live_files()
        merged: dict[str, Any] = {}
        for name in self.LIVE_FILES:
            merged = deep_merge(merged, read_toml(self._config_dir / name))
        return merged

    def read_file(self, name: str) -> dict[str, Any]:
        """Return the parsed contents of one live file, or ``{}`` if it is absent."""
        return read_toml(self._config_dir / name)

    def write_file(self, name: str, data: dict[str, Any]) -> None:
        """Persist ``data`` to the live file ``name`` (the owning domain file)."""
        write_toml(self._config_dir / name, data)


class CollectionConfigLoader(BaseConfigLoader):
    """Collection backend: ``omnia``/``features`` in ``col`` config, ``providers`` on disk.

    ``omnia``/``features`` are stored as JSON blobs in the Anki collection config
    (``col.get_config``/``set_config``), so plugin settings + enable flags SYNC across devices;
    a fresh device gets them from collection sync alone, with no config files present. Provider
    credentials must never enter a synced collection, so ``providers.toml`` stays on disk (read
    with the shared :func:`read_toml`/:func:`write_toml` helpers).

    This backend is fully independent of :class:`TomlConfigLoader`: it never reads or writes the
    ``omnia``/``features`` files, and never falls back to them. ``col`` is resolved LAZILY
    (``mw.col`` is not ready at add-on init); an optional ``col_provider`` lets tests inject a
    fake collection. Without a collection the DB domains return ``{}`` (defaults) and writes are
    skipped — never a file read/write.
    """

    _DB_FILES = ("omnia.toml", "features.toml")
    _MERGE_ORDER = ("omnia.toml", "features.toml", "providers.toml")

    def __init__(
        self,
        config_dir: Path,
        *,
        template_dir: Path | None = None,
        col_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Initialise the loader.

        Args:
            config_dir: The LIVE directory holding ``providers.toml`` and the secrets sibling;
                the ``omnia``/``features`` domains live in the collection, not here.
            template_dir: Directory holding the ``providers.example.toml`` template used to seed
                ``providers.toml``. Defaults to ``config_dir`` (single-dir back-compat).
            col_provider: Optional collection accessor (tests inject a fake); defaults to the
                lazily-resolved ``mw.col``.
        """
        self._config_dir = config_dir
        self._template_dir = template_dir or config_dir
        self._col_provider = col_provider

    @property
    def config_dir(self) -> Path:
        """The LIVE directory holding ``providers.toml`` and the secrets sibling."""
        return self._config_dir

    def ensure_live_files(self) -> None:
        """Seed ONLY ``providers.toml`` from its template; never the DB-backed domains."""
        live = self._config_dir / "providers.toml"
        template = self._template_dir / "providers.example.toml"
        if not live.exists() and template.exists():
            self._config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(template, live)

    def load(self) -> OmniaConfig:
        """Read + merge the config and validate into an :class:`OmniaConfig`."""
        return OmniaConfig.parse_obj(self.load_merged())

    def load_merged(self) -> dict[str, Any]:
        """Return the merged raw config dict (collection omnia/features → disk providers)."""
        self.ensure_live_files()
        merged: dict[str, Any] = {}
        for name in self._MERGE_ORDER:
            merged = deep_merge(merged, self.read_file(name))
        return merged

    def read_file(self, name: str) -> dict[str, Any]:
        """Return the ``name`` domain: from ``col`` for DB domains, from disk otherwise."""
        if name in self._DB_FILES:
            col = self._col()
            if col is None:
                return {}
            return col.get_config(self._col_key(name)) or {}
        return read_toml(self._config_dir / name)

    def write_file(self, name: str, data: dict[str, Any]) -> None:
        """Persist the ``name`` domain: to ``col`` for DB domains, to disk otherwise."""
        if name in self._DB_FILES:
            col = self._col()
            if col is None:
                from omnia.core.logging import get_logger

                get_logger("config").warning(
                    "no collection loaded; skipping write of %s", name
                )
                return
            col.set_config(self._col_key(name), data)
            return
        write_toml(self._config_dir / name, data)

    @staticmethod
    def _col_key(name: str) -> str:
        """The collection-config key for a DB domain (``omnia.toml`` → ``omnia:config:omnia``)."""
        return f"omnia:config:{name[:-5]}"

    def _col(self) -> Any:
        if self._col_provider is not None:
            try:
                return self._col_provider()
            except Exception:
                return None
        from omnia.core import anki_compat

        try:
            return anki_compat.main_window().col
        except Exception:
            return None


# Back-compat alias: tests + the repository construct ``ConfigLoader(dir)`` (the file backend).
ConfigLoader = TomlConfigLoader


def build_config_loader(
    config_dir: Path, *, backend: str = "database", template_dir: Path | None = None
) -> BaseConfigLoader:
    """Return the config loader for ``backend`` (``"database"`` default, or ``"toml"``).

    ``"database"`` → the collection-backed :class:`CollectionConfigLoader`; ``"toml"`` → the
    file-backed :class:`TomlConfigLoader`. The vocabulary matches the ``OMNIA_CONFIG_STORAGE``
    knob + the dispatch marker, so there is ONE name per backend across the stack.
    ``config_dir`` is the LIVE dir; ``template_dir`` (default ``config_dir``) holds the
    ``*.example.toml`` templates the backend seeds missing live files from.
    """
    if backend == "database":
        return CollectionConfigLoader(config_dir, template_dir=template_dir)
    if backend == "toml":
        return TomlConfigLoader(config_dir, template_dir=template_dir)
    raise ValueError(f"unknown config backend: {backend!r}")
