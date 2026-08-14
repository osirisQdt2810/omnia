"""One generic provider registry, bound once per provider kind (ADR-014).

Both kinds used to answer the same questions — what is registered, which names need a key,
build the one named by config — and only TTS had a registry for it; LLM carried a
hand-maintained builder table that had to be kept in sync with a second name->class map. This
module is that mechanism, written once: each kind creates one :class:`ProviderRegistry` in its
own ``registry.py`` and exposes thin ``register_*`` / ``create_*`` / ``available_*`` wrappers
over it. Pure module — imports only ``.base`` + ``.errors``, so concrete providers depend on
it without a cycle and a future kind gets the whole surface for one line.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Optional, TypeVar, cast

from omnia.core.providers.base import ProviderBase
from omnia.core.providers.errors import ProviderError

if TYPE_CHECKING:
    from omnia.core.network.http import HttpClient

# The provider kind this registry holds. PEP 695 syntax (``class ProviderRegistry[P]``) is a
# SyntaxError on Python 3.10 — Anki's supported minimum — so use an explicit TypeVar.
P = TypeVar("P", bound=ProviderBase)


class ProviderRegistry(Mapping[str, "type[P]"]):
    """Name -> provider class for ONE provider kind, plus the queries every kind needs.

    A read-only :class:`~collections.abc.Mapping` (so callers keep doing ``registry[name]``,
    ``dict(registry)``, ``set(registry)``, ``.items()``) that also owns registration and
    construction. One instance per kind (``LLM_REGISTRY``, ``TTS_REGISTRY``).
    """

    def __init__(self, kind: str, *, default: str) -> None:
        """Create an empty registry for one provider kind.

        Args:
            kind: Human label used in error messages (e.g. ``"LLM"``, ``"TTS"``).
            default: Provider name :meth:`create` falls back to when a config carries no
                ``provider`` key. Per-kind *data*, which is why it is passed in rather than
                branched on inside :meth:`create`.
        """
        self._kind = kind
        self._default = default
        self._classes: dict[str, type[P]] = {}

    # --- Mapping ---------------------------------------------------------------------
    def __getitem__(self, name: str) -> type[P]:
        return self._classes[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._classes)

    def __len__(self) -> int:
        return len(self._classes)

    # --- registration ----------------------------------------------------------------
    def register(self, *names: str) -> Callable[[type[P]], type[P]]:
        """Register a provider class under one or more config names.

        Accepts multiple names so a single class can serve several config keys (the openai
        family binds ``"openai"``/``"openrouter"``/``"openai_compatible"`` to one class). Does
        NOT stamp a ``name`` attribute onto the class — a multi-name class keeps its own
        declared ``name`` (``OpenAICompatibleProvider.name`` stays ``"openai_compatible"`` under
        all three keys). This is the opposite of :mod:`omnia.core.registry`, which stamps
        ``cls.id``: stamping here would break the usage rows, the Account tab's join on the
        class name, and multi-name registration itself (the last name would win).

        Args:
            *names: One or more unique, stable config keys for the provider.

        Returns:
            A class decorator that records the class under each name.

        Raises:
            ValueError: If ``names`` is empty, any name is empty, or a name is already bound to
                a DIFFERENT class. Re-registering the SAME class under a name is a no-op.
        """
        if not names:
            raise ValueError(f"{self._kind} provider registration requires a name")
        if any(not name for name in names):
            raise ValueError(f"{self._kind} provider name must be a non-empty string")

        def decorator(cls: type[P]) -> type[P]:
            for name in names:
                existing = self._classes.get(name)
                if existing is not None and existing is not cls:
                    raise ValueError(
                        f"{self._kind} provider name {name!r} already registered to "
                        f"{existing.__name__}"
                    )
                self._classes[name] = cls
            return cls

        return decorator

    # --- queries ---------------------------------------------------------------------
    def names(self) -> list[str]:
        """Return the registered provider names, sorted.

        Sorted, not registration order: the test conftest turns this list into pytest param
        IDs, so an import-order-dependent order would make test IDs (and ``--lf``) unstable.
        """
        return sorted(self._classes)

    def classes(self) -> list[type[P]]:
        """Return the distinct provider classes in registration order (deduped).

        Several names can share one class (the openai family), so this is what a caller that
        wants each implementation exactly once — e.g. aggregating TTS voices — reads.
        """
        seen: set[type[P]] = set()
        distinct: list[type[P]] = []
        for cls in self._classes.values():
            if cls not in seen:
                seen.add(cls)
                distinct.append(cls)
        return distinct

    def requiring_api(self) -> list[str]:
        """Names whose provider needs an API key / credentials to call, sorted."""
        return sorted(n for n, c in self._classes.items() if c.requires_api)

    def keyless(self) -> list[str]:
        """Names callable WITHOUT a key (free / offline / open-source), sorted."""
        return sorted(n for n, c in self._classes.items() if not c.requires_api)

    # --- construction ----------------------------------------------------------------
    def create(self, config: dict[str, Any], http: Optional[HttpClient] = None) -> P:
        """Instantiate the provider named by ``config['provider']`` (or this kind's default).

        The WHOLE ``config`` dict is handed to ``from_config``, ``provider`` key included: a
        multi-name class reads that key to pick its own defaults (which base URL the openai
        family points at), so resolving the name here instead would make an ``openrouter``
        config talk to ``api.openai.com``.

        Args:
            config: Provider config; ``config['provider']`` selects the class.
            http: Optional HTTP client injected into the built provider (DIP).

        Returns:
            The configured provider.

        Raises:
            ProviderError: If the provider name is unknown. Deliberately no fallback to the
                default class — a config synced from a newer device naming a provider this
                build does not have must fail loudly, not silently run on another one.
        """
        name = config.get("provider", self._default)
        cls = self.get(name)
        if cls is None:
            raise ProviderError(
                f"Unknown {self._kind} provider {name!r}; known: {self.names()}"
            )
        # ``from_config`` is declared on ProviderBase returning ProviderBase; each concrete
        # class narrows it to itself. ``typing.Self`` would express this but needs 3.11+ and
        # the add-on targets 3.10+.
        return cast("P", cls.from_config(config, http))
