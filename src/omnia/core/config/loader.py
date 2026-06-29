"""Load + merge the live TOML config files into a validated :class:`OmniaConfig`.

Model: three live domain files ARE the configuration, edited directly by the user and
written back to by the add-on — there is NO separate override layer. ``omnia.toml`` owns
``log_level`` + ``[plugins.*]``; ``features.toml`` owns the per-feature sections; and
``providers.toml`` owns ``[llm]`` (one ``[llm.<provider>]`` subsection each) + ``[tts]``. On
a fresh install a missing live file is created by copying its tracked ``*.example.toml``
template. The files are deep-merged in order (omnia → features → providers) and validated by
Pydantic. TOML is read with the stdlib ``tomllib`` (Python 3.11+) and written with ``tomli_w``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:  # stdlib on the add-on's Python 3.13; tomli is the fallback for <3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from omnia.core.config.models import OmniaConfig


class ConfigLoader:
    """Reads and writes Omnia's live domain config files."""

    LIVE_FILES = ("omnia.toml", "features.toml", "providers.toml")

    def __init__(self, config_dir: Path) -> None:
        """Initialise the loader.

        Args:
            config_dir: Directory holding the live domain config files (and the tracked
                ``*.example.toml`` templates used to seed any missing live file).
        """
        self._config_dir = config_dir

    @property
    def config_dir(self) -> Path:
        """The directory holding the live domain config files."""
        return self._config_dir

    def ensure_live_files(self) -> None:
        """Create any missing live file by copying its ``*.example.toml`` template."""
        for name in self.LIVE_FILES:
            live = self._config_dir / name
            template = self._config_dir / name.replace(".toml", ".example.toml")
            if not live.exists() and template.exists():
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
            merged = self._deep_merge(merged, self._read_toml(self._config_dir / name))
        return merged

    def read_file(self, name: str) -> dict[str, Any]:
        """Return the parsed contents of one live file, or ``{}`` if it is absent."""
        return self._read_toml(self._config_dir / name)

    def write_file(self, name: str, data: dict[str, Any]) -> None:
        """Persist ``data`` to the live file ``name`` (the owning domain file)."""
        import tomli_w

        path = self._config_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            tomli_w.dump(data, handle)

    # --- internals ------------------------------------------------------------------
    @staticmethod
    def _read_toml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with open(path, "rb") as handle:
            return tomllib.load(handle)

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge ``override`` onto ``base`` (override wins; base untouched)."""
        result = dict(base)
        for key, value in override.items():
            existing = result.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = ConfigLoader._deep_merge(existing, value)
            else:
                result[key] = value
        return result
