"""Out-of-config secret storage.

API keys / access tokens / credential files must NOT sit as plaintext in the TOML config —
that file is easy to leak (open it in an editor, paste it in a bug report, sync it by
accident). Instead the config holds only a *reference*; the real secret lives in a file under
the add-on's gitignored ``secrets/`` directory.

A credential field's value uses one of two reference schemes:

* ``secret:<name>``      — the field's VALUE is the (stripped) content of ``secrets/<name>``.
  Used for api keys / access tokens — the secret is the string itself.
* ``secret-file:<name>`` — the field is a PATH to a file; it resolves to the absolute path of
  ``secrets/<name>``. Used for a service-account JSON, which is consumed as a file path.

A bare value with no scheme is treated as a literal — so non-secret fields (``project``,
``location``, model ids, base urls) keep living inline in the config, and any pre-existing
inline secret still works until it is re-saved through the UI (which rewrites it as a ref).

This module only touches the filesystem (no ``aqt``/``anki`` imports), so it unit-tests
headless.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class SecretsStore:
    """Reads/writes secret values + credential files under a gitignored ``secrets/`` dir."""

    VALUE_SCHEME = "secret:"
    FILE_SCHEME = "secret-file:"

    def __init__(self, secrets_dir: Path) -> None:
        """Initialise the store.

        Args:
            secrets_dir: Directory holding the secret files (created lazily on first write).
        """
        self._dir = Path(secrets_dir)

    def is_ref(self, value: object) -> bool:
        """Whether ``value`` is a secret reference (``secret:`` / ``secret-file:``)."""
        return isinstance(value, str) and value.startswith(
            (self.VALUE_SCHEME, self.FILE_SCHEME)
        )

    def resolve(self, value: object) -> object:
        """Resolve a config value: a ref → its real value/path; anything else unchanged.

        ``secret:<name>`` → the file's stripped content (``""`` if the file is missing, so a
        moved-but-not-copied secret surfaces as the provider's clear "missing api_key" error
        rather than a crash). ``secret-file:<name>`` → the absolute path of ``secrets/<name>``.
        """
        if not isinstance(value, str):
            return value
        if value.startswith(self.VALUE_SCHEME):
            return self._read(value[len(self.VALUE_SCHEME) :])
        if value.startswith(self.FILE_SCHEME):
            return str(self._dir / value[len(self.FILE_SCHEME) :])
        return value

    def store_value(self, name: str, value: str) -> str:
        """Write ``value`` to ``secrets/<name>`` and return its ``secret:<name>`` reference."""
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / name).write_text(value, encoding="utf-8")
        return f"{self.VALUE_SCHEME}{name}"

    def import_file(self, name: str, src_path: str) -> str:
        """Copy the file at ``src_path`` to ``secrets/<name>``; return its ``secret-file:`` ref.

        Raises:
            OSError: If ``src_path`` cannot be read/copied (surfaced to the caller, which
                turns it into a friendly dialog message).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_path, self._dir / name)
        return f"{self.FILE_SCHEME}{name}"

    def _read(self, name: str) -> str:
        path = self._dir / name
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
