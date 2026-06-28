"""Load + merge the TOML config files into a validated :class:`OmniaConfig`.

Layering: bundled defaults (``omnia.toml`` + ``features.toml`` + ``providers.toml``, one
domain per file) are deep-merged with the user's overrides (``user_files/omnia.toml``), then
validated by Pydantic. TOML is read with the stdlib ``tomllib`` (Python 3.11+) and written
with ``tomli_w``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # stdlib on the add-on's Python 3.13; tomli is the fallback for <3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from omnia.core.config.models import OmniaConfig


class ConfigLoader:
    """Reads and writes Omnia's layered config files."""

    def __init__(self, config_dir: Path, user_file: Path) -> None:
        """Initialise the loader.

        Args:
            config_dir: Directory holding the bundled default config files.
            user_file: Path to the user's TOML overrides (created on first save).
        """
        self._config_dir = config_dir
        self._user_file = user_file

    def load(self) -> OmniaConfig:
        """Read defaults + user overrides, merge, and validate into an :class:`OmniaConfig`."""
        return OmniaConfig.parse_obj(self.load_merged())

    def load_merged(self) -> dict[str, Any]:
        """Return the fully merged raw config dict (defaults + user overrides).

        The repository keeps this so per-plugin sections (``[auto_flip]``, …) — which
        :class:`OmniaConfig` ignores — can be validated by each plugin's own ``config_model``.
        """
        return self._deep_merge(self._load_defaults(), self.read_overrides())

    def read_overrides(self) -> dict[str, Any]:
        """Return the user's override layer (``user_files/omnia.toml``), or ``{}``."""
        return self._read_toml(self._user_file)

    def save_overrides(self, overrides: dict[str, Any]) -> None:
        """Persist ``overrides`` to the user TOML file (the override layer only)."""
        import tomli_w

        self._user_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._user_file, "wb") as handle:
            tomli_w.dump(overrides, handle)

    # --- internals ------------------------------------------------------------------
    def _load_defaults(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        data.update(self._read_toml(self._config_dir / "omnia.toml"))
        data.update(self._read_toml(self._config_dir / "features.toml"))
        # providers.toml carries [llm] (with one [llm.<provider>] subsection each) + [tts].
        data.update(self._read_toml(self._config_dir / "providers.toml"))
        return data

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
