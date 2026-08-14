"""Tool self-registration registry — the smart-notes mirror of ``@register_tts``.

Same shape as :mod:`omnia.core.providers.tts.registry`: each tool class registers itself with
the :func:`register_tool` decorator at import time, and everything else (the pipeline, the
settings catalog, the dependency derivation) reads :data:`TOOL_REGISTRY` instead of a
hand-maintained table. Adding a tool is therefore one subclass + one decorator + one import in
``tools/__init__.py`` — no edit to the service, the pipeline, or the UI.

Pure module: imports only :mod:`.base` plus stdlib, so concrete tools depend on it without a
cycle (``registry`` ← ``base``; tools ← ``registry``; ``__init__`` ← tools).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine.tools.base import Tool

logger = get_logger("smart_notes")

if TYPE_CHECKING:
    from collections.abc import Callable

    from omnia.plugins.smart_notes.config import CompiledToolSpec
    from omnia.plugins.smart_notes.engine.tools.base import ToolContext

# name -> tool class. Builtin names are bare slugs ("ai", "cloze"); user-authored tools loaded
# from ``user_files/tools/`` register namespaced names ("user:<slug>"), so the two can never
# collide.
TOOL_REGISTRY: dict[str, type[Tool]] = {}


def register_tool(name: str) -> Callable[[type[Tool]], type[Tool]]:
    """Register a :class:`~omnia.plugins.smart_notes.engine.tools.base.Tool` under ``name``.

    Args:
        name: The unique, stable config key the field's tool chain stores.

    Returns:
        A class decorator that records the class under ``name``.

    Raises:
        ValueError: If ``name`` is empty, or is already bound to a DIFFERENT class.
            Re-registering the SAME class under the same name is a no-op (so a module
            re-imported under a second path does not explode).
    """
    if not name:
        raise ValueError("register_tool requires a non-empty name")

    def decorator(cls: type[Tool]) -> type[Tool]:
        existing = TOOL_REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"Tool name {name!r} already registered to {existing.__name__}"
            )
        TOOL_REGISTRY[name] = cls
        return cls

    return decorator


def unregister_tool(name: str) -> bool:
    """Remove ``name`` from the registry, returning whether it was registered.

    The counterpart :func:`register_tool` deliberately lacks: rebinding a name to a DIFFERENT
    class raises, which is right for builtins (a duplicate name is a bug) and wrong for the
    ``user:`` namespace, where re-loading an edited file is the normal case. The user-tool
    loader owns that namespace on disk — it drops a name before rebinding it, and drops one for
    good when the user deletes the file, so the picker stops offering a tool that no longer
    exists without restarting Anki.

    Args:
        name: The registered tool name.

    Returns:
        True when a registration was removed.
    """
    return TOOL_REGISTRY.pop(name, None) is not None


def get_tool(name: str) -> type[Tool] | None:
    """Return the tool CLASS registered under ``name`` (or None when unknown)."""
    return TOOL_REGISTRY.get(name)


def registered_tools() -> list[str]:
    """Return the registered tool names, sorted."""
    return sorted(TOOL_REGISTRY)


def resolve_tool(name: str) -> Tool | None:
    """Instantiate the tool registered under ``name`` (or None when unknown).

    Tools are stateless and take no constructor arguments, so instantiating per resolve is
    free and keeps the registry free of shared instances. An unknown name is NOT an error: a
    field may reference a user tool this device does not have, and the pipeline degrades that
    to an ``unknown_tool`` attempt and tries the next tool.
    """
    cls = get_tool(name)
    return None if cls is None else cls()


def tool_referenced_fields(specs: Iterable[CompiledToolSpec]) -> list[str]:
    """Return the note fields a compiled tool chain reads through its params.

    Feeds :func:`~omnia.plugins.smart_notes.engine.rules.rule_prerequisites`, so a tool param
    that names a field (``sentence_field``, ``source_field``) becomes a real dependency edge
    for ordering, blocking and the graph UI. Unknown tools contribute nothing — a chain
    referencing a tool this device lacks must still order and generate.

    Args:
        specs: The rule's compiled tool chain, in run order.

    Returns:
        The referenced field names in chain order, duplicates included (the caller de-dupes
        case-insensitively alongside the prompt's refs).
    """
    referenced: list[str] = []
    for spec in specs:
        cls = get_tool(spec.name)
        if cls is None:
            continue
        # Guarded for the same reason the pipeline guards `run`: this is called while compiling
        # a note's rules, so a tool that raises here would abort the WHOLE note. From Phase 4 the
        # registry can hold user-authored classes loaded off disk, where that is a real risk.
        try:
            referenced.extend(cls.referenced_fields(spec.params))
        except Exception:
            logger.exception(
                "smart_notes: tool %r failed to report its referenced fields", spec.name
            )
    return referenced


@dataclass(frozen=True)
class ChainConflict:
    """An exclusive tool sharing a field's chain with a tool that can produce the same kind.

    The chain is not merely sub-optimal, it is unsafe to RUN: whichever of the two goes first,
    the other may fill the field in its place, and an exclusive tool declares exactly that
    outcome to be harmful (see :attr:`~omnia.plugins.smart_notes.engine.tools.base.Tool.exclusive`).
    A value object rather than a bare message so the pipeline can attribute its refusal to the
    exclusive tool while the message stays in one place.
    """

    exclusive: str
    rivals: tuple[str, ...]

    @property
    def message(self) -> str:
        """The refusal, in words the user can act on."""
        rivals = ", ".join(repr(name) for name in self.rivals)
        return (
            f"{self.exclusive!r} must be the only tool that generates this field, but "
            f"{rivals} can generate it too and would produce in its place — which "
            f"{self.exclusive!r} declares unsafe. Nothing was generated: remove the other "
            "tool(s) from this field's chain."
        )


def tool_kinds(cls: type[Tool]) -> frozenset[str]:
    """Return ``cls.kinds`` defensively, as an empty set when the class is malformed.

    Reading a class attribute is not safe once the registry can hold classes this repo did not
    write (Phase 4 loads user-authored tools from ``user_files/tools/``). ``getattr`` with a
    default only swallows ``AttributeError`` — the likelier slip is ``kinds`` written as a
    ``@property``, where the attribute EXISTS and is a property object, so ``kind in cls.kinds``
    raises ``TypeError`` from whatever is walking the registry. A bare string is the other trap:
    ``"tts" in "text"`` is a substring test, not membership.
    """
    try:
        kinds = cls.kinds
    except (
        Exception
    ):  # a descriptor that raises is still a malformed class, not our crash
        logger.exception("smart_notes: tool %r could not report its kinds", cls)
        return frozenset()
    if isinstance(kinds, (str, bytes)) or not isinstance(kinds, Iterable):
        logger.error(
            "smart_notes: tool %r declares kinds=%r, which is not a set of kinds",
            cls,
            kinds,
        )
        return frozenset()
    try:
        return frozenset(str(kind) for kind in kinds)
    except Exception:
        logger.exception("smart_notes: tool %r has an unreadable kinds", cls)
        return frozenset()


def chain_conflict(
    specs: Iterable[CompiledToolSpec], kind: str
) -> Optional[ChainConflict]:
    """Return why ``specs`` must not run for a field of ``kind``, or None when it may.

    A chain that pairs an exclusive tool with a rival is refused in BOTH orders, because both
    leak: the rival first simply wins, and the rival second wins whenever the exclusive tool
    fails non-terminally or is missing from this build. Ordering the check by "who runs first"
    is what let one of them through.

    Only tools that can serve ``kind`` count as rivals — a text tool sitting in a tts field's
    chain never produces there — and a tool this build does not have is skipped, since nothing
    here can know what it would produce.

    Args:
        specs: The rule's compiled tool chain, in run order.
        kind: The field's generation kind.

    Returns:
        The first conflict found, or None.
    """
    serving: list[tuple[str, bool]] = []
    for spec in specs:
        cls = get_tool(spec.name)
        if cls is None:
            continue
        if kind not in tool_kinds(cls):
            continue
        serving.append((spec.name, bool(getattr(cls, "exclusive", False))))
    for name, exclusive in serving:
        if not exclusive:
            continue
        rivals = tuple(other for other, _ in serving if other != name)
        if rivals:
            return ChainConflict(name, rivals)
    return None


def tools_catalog(ctx: ToolContext) -> list[dict[str, Any]]:
    """Build the settings-UI payload describing every registered tool.

    Baked into the smart-notes dialog (like the provider catalog) so the tools picker paints
    without a ``pycmd`` round-trip.

    Args:
        ctx: The live tool context, so each tool can report what this machine is missing.

    Returns:
        One dict per tool, sorted by name: ``name``, ``label``, ``description``, ``kinds``
        (sorted), ``deterministic``, ``exclusive`` (so the picker can warn about a chain the
        pipeline would refuse, without knowing which tool it is), ``params_schema`` (the
        pydantic JSON schema, or None) and ``unavailable_reason`` — the tool's ADVICE, which
        the picker shows without disabling the tool (see
        :meth:`~omnia.plugins.smart_notes.engine.tools.base.Tool.availability`).
    """
    catalog: list[dict[str, Any]] = []
    for name in registered_tools():
        cls = TOOL_REGISTRY[name]
        # One broken tool must cost only its own row, not the whole picker (and not the dialog,
        # which bakes this payload at open time). Phase 4 loads user-authored classes off disk.
        try:
            catalog.append(
                {
                    "name": name,
                    "label": cls.label,
                    "description": cls.description,
                    "kinds": sorted(cls.kinds),
                    "deterministic": cls.deterministic,
                    "exclusive": cls.exclusive,
                    "params_schema": (
                        None if cls.params_model is None else cls.params_model.schema()
                    ),
                    "unavailable_reason": cls.availability(ctx),
                }
            )
        except Exception:
            logger.exception("smart_notes: tool %r could not describe itself", name)
    return catalog
