"""Convert the retired ``[llm.openai_compatible]`` slot into a named endpoint (ADR-022).

Keys used to show one built-in "Self-hosted / OpenAI-compatible" card beside the endpoints a
user adds by name, which made two ways to do one thing. The card is gone, and a slot somebody
configured becomes the endpoint ``Self-hosted`` — so it keeps working with nothing for them to
do. That is the requirement, and it is why this converts where ADR-006 and ADR-008 start fresh.

Only local files are touched: ``providers.toml`` and the ``.secrets/`` store never sync, so each
machine converts its own slot. The synced collection is never written — a Smart Notes field
pinned to ``openai_compatible`` keeps that value and is resolved where it is consumed
(:meth:`~omnia.core.config.models.LLMSettings.canonical_provider`).

Works on the RAW providers.toml dict, edited in place: rebuilding ``[llm]`` from this build's
models would drop whatever it cannot parse (ADR-010). The move itself is pure — it touches no
file, and hands back the secret to store and the one to forget — so the repository does every
write in one place and one order. No ``aqt``/``anki`` imports, so it tests headless.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omnia.core.config.models import (
    LEGACY_ENDPOINT_LABEL,
    LEGACY_ENDPOINT_PROVIDER,
    custom_provider_name,
    label_in_use,
)
from omnia.core.config.secrets import SecretsStore
from omnia.core.logging import get_logger

_logger = get_logger("config")


@dataclass(frozen=True)
class SecretToStore:
    """A key the move carries over, and the file it is to be stored under."""

    name: str
    #: The key itself: kept out of the repr, so a result that reaches a log never carries it.
    value: str = field(repr=False)


@dataclass(frozen=True)
class LegacyEndpointResult:
    """What one :meth:`LegacyEndpointMigration.run` did, and the secret I/O it leaves over.

    The caller does that I/O in one order: store ``new_secret``, write the TOML, forget
    ``stale_secret``. Any other order has a moment in which a reference on disk names a key
    file that is not there, and a failed write at that moment loses the only copy of the key.

    Attributes:
        changed: The data was modified and has to be written back.
        moved_to: The label THIS run moved the slot to; ``""`` when it had moved before (the
            run only pointed ``[llm].provider`` at it again) or nothing was done.
        new_secret: The key to store before the TOML naming it is written, if one moved.
        stale_secret: A secret file nothing names any more, to forget once the TOML is on disk.
    """

    changed: bool = False
    moved_to: str = ""
    new_secret: SecretToStore | None = None
    stale_secret: str = ""


class LegacyEndpointMigration:
    """Moves a configured slot into a named endpoint, once; re-points the active provider.

    Args:
        resolve: Maps a ``secret:`` reference to the key behind it, ``""`` when there is none —
            :meth:`SecretsStore.resolve`. A reader only: the move writes nothing itself.
        secret_name: Maps ``(domain, provider, field)`` to a secret file name — the
            repository's own, so the endpoint's key lands exactly where removing the endpoint
            later looks for it.
    """

    def __init__(
        self,
        resolve: Callable[[str], object],
        secret_name: Callable[[str, str, str], str],
    ) -> None:
        self._resolve = resolve
        self._secret_name = secret_name

    def run(self, data: dict[str, Any]) -> LegacyEndpointResult:
        """Convert the slot in ``data`` (a parsed providers.toml), editing it in place.

        A slot with no base URL is left alone, and so is an active provider naming it: the slot
        never worked, so there is nothing to carry over, and switching the user to some other
        provider would be a choice they did not make. A slot already moved is only re-pointed,
        which also corrects an older build on this machine writing the old id back.
        """
        llm = _table(data, "llm", "[llm]")
        legacy = _table(llm or {}, LEGACY_ENDPOINT_PROVIDER, "[llm.openai_compatible]")
        if llm is None or legacy is None:
            return LegacyEndpointResult()
        if not isinstance(llm.get("custom", {}), dict):
            _logger.warning("providers.toml: [llm.custom] is not a table; not moving")
            return LegacyEndpointResult()
        label, secret, stale = "", None, ""
        if not str(legacy.get("moved_to") or ""):
            if not str(legacy.get("base_url") or "").strip():
                return LegacyEndpointResult()
            label, secret, stale = self._move(llm, legacy)
        repointed = self._repoint(llm, legacy)
        if stale and _names(data, SecretsStore.value_ref(stale)):
            stale = ""  # another table still reads that file
        return LegacyEndpointResult(
            changed=bool(label) or repointed,
            moved_to=label,
            new_secret=secret,
            stale_secret=stale,
        )

    def _move(
        self, llm: dict[str, Any], legacy: dict[str, Any]
    ) -> tuple[str, SecretToStore | None, str]:
        """Copy the slot into a new endpoint and mark it moved.

        Returns ``(label, the key to store, the file it came from)``.

        Every key is carried verbatim, unknown ones included, except the bookkeeping
        ``moved_to``, which belongs to the slot. Nothing is added: a model id the slot left to
        its default resolves the same on the endpoint — the text model has the same default,
        and with no image model the provider sends the same fallback — so writing one out
        would only put OpenAI's ``gpt-image-1`` in the endpoint's Image model box.
        """
        custom = llm.setdefault("custom", {})
        label = self._free_label(custom)
        endpoint = {
            key: copy.deepcopy(value)
            for key, value in legacy.items()
            if key != "moved_to"
        }
        secret, stale = self._move_key(
            legacy.get("api_key"), custom_provider_name(label)
        )
        if secret is not None:
            # One file, named for its owner, and both tables name it: removing the endpoint
            # later shreds the key, while a downgraded build reading the slot still finds one.
            endpoint["api_key"] = legacy["api_key"] = SecretsStore.value_ref(
                secret.name
            )
        custom[label] = endpoint
        legacy["moved_to"] = label
        return label, secret, stale

    def _move_key(self, raw: object, provider: str) -> tuple[SecretToStore | None, str]:
        """The slot's key, bound for a file named for ``provider``; and the file it came from.

        A ``secret:`` reference is read and its old file reported stale; an inline key moves
        into the secrets store, so the plaintext leaves the TOML. Anything else — no key, a
        ``secret-file:`` path, a reference with nothing behind it — has no key to move, and the
        caller copies it verbatim: ``(None, "")``.
        """
        if (
            not isinstance(raw, str)
            or not raw
            or raw.startswith(SecretsStore.FILE_SCHEME)
        ):
            return None, ""
        stale = ""
        value = raw
        if raw.startswith(SecretsStore.VALUE_SCHEME):
            stale = raw[len(SecretsStore.VALUE_SCHEME) :]
            value = str(self._resolve(raw) or "")
        if not value:
            return None, ""
        name = self._secret_name("llm", provider, "api_key")
        return SecretToStore(name, value), stale

    @staticmethod
    def _free_label(existing: dict[str, Any]) -> str:
        """``Self-hosted``, else the first free ``Self-hosted N``.

        Never an existing endpoint, whatever it holds: the user made that one, and merging the
        slot into it would overwrite their address and key with the slot's.
        """
        label, number = LEGACY_ENDPOINT_LABEL, 2
        while label_in_use(existing, label):
            label, number = f"{LEGACY_ENDPOINT_LABEL} {number}", number + 1
        return label

    @staticmethod
    def _repoint(llm: dict[str, Any], legacy: dict[str, Any]) -> bool:
        """Point ``[llm].provider`` from the slot at the endpoint it moved to, if it exists.

        Runs on every start rather than only with the move, so an older build that writes the
        old id back is corrected at the next one. A moved-to endpoint the user has since
        removed is not re-pointed at: that would name a provider that does not exist.
        """
        label = str(legacy.get("moved_to") or "")
        if not label or label not in llm.get("custom", {}):
            return False
        if llm.get("provider") != LEGACY_ENDPOINT_PROVIDER:
            return False
        llm["provider"] = custom_provider_name(label)
        return True


def _table(parent: dict[str, Any], key: str, where: str) -> dict[str, Any] | None:
    """``parent[key]`` when it is a table, else None — with a warning when it is not one."""
    value = parent.get(key)
    if value is not None and not isinstance(value, dict):
        _logger.warning("providers.toml: %s is not a table; not moving", where)
    return value if isinstance(value, dict) else None


def _names(value: object, ref: str) -> bool:
    """Whether ``ref`` is a value anywhere in ``value``, however deeply nested."""
    if isinstance(value, dict):
        return any(_names(item, ref) for item in value.values())
    if isinstance(value, list):
        return any(_names(item, ref) for item in value)
    return value == ref
