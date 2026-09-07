"""The regeneration capability Smart Notes publishes for other plugins to call.

Smart Notes' generation machinery is built for callers that live inside it: the batch runner,
the editor button, the review-time evaluator. Each of them already holds a
:class:`~omnia.plugins.smart_notes.engine.service.GenerationService`, already knows which of
``(results, blocked, failed)`` it cares about, and already runs on a thread where touching the
collection is legal. A *stranger* — a clipper reaching Omnia over loopback HTTP — has none of
that, and must not acquire it by importing Smart Notes (see :mod:`omnia.core.services` for why
an import is exactly the wrong handle to hold on a feature that can be switched off).

So this module is the translation layer, and the whole of it:

* **one status per field, always.** Not an exception, not a silent omission. A field the user
  clicked always comes back saying what happened to it, in a vocabulary
  (:data:`STATUS_GENERATED` … :data:`STATUS_NOT_GENERATABLE`) small enough to render as a
  button state plus a tooltip. Those strings cross an HTTP boundary, so they are constants here
  and a test pins them.
* **one field's problem is that field's problem.** "Generate all" runs to the end and reports
  what failed; nothing aborts the rest. The engine already isolates a failing tool chain
  (:class:`~omnia.plugins.smart_notes.engine.note_run.FailedField`); this adds the same promise
  for the gates *around* it, and for the fields the engine drops without a word.
* **the collection is main-thread-only.** Every read and every write is marshalled onto the Qt
  main thread and awaited (:meth:`RegenerationService._on_main`), because
  :meth:`RegenerationService.regenerate` is called from an HTTP worker. Provider calls stay on
  the calling thread — they are the slow part, and the main thread must never wait for them.

The one place this deliberately DIFFERS from an existing entry point is the per-field
``enabled`` checkbox. :func:`~omnia.plugins.smart_notes.integration.field_menu.single_field_config`
forces ``enabled=True``, because the editor's right-click menu means "generate this, now,
whatever the batch settings say" and the user is looking straight at the field. A clipper is not
that: it renders a button per field from a config it cannot see, so an unticked Generate box
must come back as :data:`STATUS_RULE_OFF` with an instruction, never as a generation the user
never asked for.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, TypeVar

from omnia.core import anki_compat
from omnia.core.concurrency.pool import pooled_dispatch
from omnia.core.logging import get_logger
from omnia.plugins.smart_notes.engine import applies_to_deck, compile_note_type_rules
from omnia.plugins.smart_notes.engine.rules import (
    rule_prerequisites,
    rule_source_fields,
)
from omnia.plugins.smart_notes.integration.batch import note_materializer

if TYPE_CHECKING:
    from omnia.plugins.smart_notes.config import (
        SmartNotesFieldConfig,
        SmartNotesFieldRule,
        SmartNotesNoteTypeConfig,
        SmartNotesSettings,
    )
    from omnia.plugins.smart_notes.engine import GenerationResult, GenerationService

_T = TypeVar("_T")

# The name Smart Notes publishes this capability under. A consumer uses the literal string
# (importing this module would defeat the point of the registry), so the value is frozen by a
# test on this side of the boundary and mirrored on the other.
REGENERATION_SERVICE = "smart_notes.regeneration"

# --- the status vocabulary --------------------------------------------------------------
# Exactly these strings, and only these. They are the wire format between Omnia and the
# clippers: renaming one is a breaking protocol change, not a refactor.
STATUS_GENERATED = "generated"  # produced and written to the note
STATUS_SKIPPED = "skipped"  # the engine's skip gate dropped it (nothing to work from)
STATUS_BLOCKED = "blocked"  # a precondition outside the field itself refused it
STATUS_ERROR = "error"  # it was attempted and the tool chain produced nothing
# No generation row targets this field (or it IS the base field, which is never generated).
STATUS_NO_RULE = "no_rule"
STATUS_RULE_OFF = "rule_off"  # a row exists but its Generate checkbox is unticked
STATUS_NOT_GENERATABLE = "not_generatable"  # this build cannot generate the row's type
# Only :meth:`RegenerationService.field_states` returns this one: it is the "would be
# attempted" preview state, and by definition no finished run ever ends in it.
STATUS_READY = "ready"

# A collection read/write handed to the Qt main thread gets this long before the caller gives
# up. Far longer than the read-only lookup service allows itself (5s), because by the time the
# write-back runs the provider budget has ALREADY been spent — abandoning the result to keep an
# HTTP response snappy would charge the user for nothing. A genuinely wedged main thread still
# degrades to an error instead of parking a worker thread forever.
_MAIN_THREAD_TIMEOUT_SECONDS = 30.0

logger = get_logger("smart_notes")


@dataclass(frozen=True)
class FieldOutcome:
    """What happened to ONE field in a regeneration request.

    ``status`` is one of this module's ``STATUS_*`` constants. ``message`` is the reason a human
    should be shown — empty only when the field generated, since success explains itself.
    ``text`` carries the field's new HTML (or its ``<img>``/``[sound:…]`` reference) when
    ``status`` is :data:`STATUS_GENERATED`, so a caller can repaint without re-reading the note.
    """

    field: str
    status: str
    message: str = ""
    text: str = ""


@dataclass(frozen=True)
class _NoteSnapshot:
    """A note read on the Qt main thread, flattened so a worker thread may use it safely.

    Holding the Anki ``Note`` itself would be the bug this class exists to prevent: the
    generation that follows runs for as long as the providers take, on a thread that must not
    touch the collection.
    """

    note_id: int
    note_type: str
    fields: dict[str, str]
    deck_ids: list[int]


class RegenerationService:
    """Turns Smart Notes' generation machinery into an interface a stranger can call safely.

    Published under :data:`REGENERATION_SERVICE` while the plugin is enabled and withdrawn on
    disable, so a consumer's ``lookup`` returning ``None`` IS the "Smart Notes is off" answer.
    """

    def __init__(
        self,
        settings_provider: Callable[[], Optional[SmartNotesSettings]],
        service: GenerationService,
        *,
        logger: Any = None,
    ) -> None:
        """Initialise the service.

        Args:
            settings_provider: Returns the CURRENT settings, or ``None`` when there is no
                collection to read them from. Called fresh on every request — the store
                re-reads the collection per action, so a toggle flipped mid-session (or on
                another device) applies to the very next click.
            service: The plugin's generation service, shared with every other entry point.
            logger: Where failures are recorded; defaults to the add-on's ``smart_notes``
                logger.
        """
        self._settings_provider = settings_provider
        self._service = service
        self._logger = logger if logger is not None else get_logger("smart_notes")

    # --- the published capability ----------------------------------------------------------
    def can_regenerate(self) -> bool:
        """Whether the user has left the clippers' regenerate buttons switched on.

        Note-independent, so a caller asks it once to decide whether to offer the buttons at
        all; :meth:`regenerate` re-checks it anyway, because the answer can change between the
        render and the click.
        """
        settings = self._settings_provider()
        return bool(settings is not None and settings.regenerate_from_clippers)

    def field_states(self, note_id: int) -> dict[str, str]:
        """Return one status per field of the note, generating NOTHING.

        The clipper renders a button and a tooltip per field from this, so it must be cheap and
        it must never raise: an unreadable note, a broken config, anything at all, degrades to
        an empty map (no buttons) rather than an exception the UI cannot act on.

        A field that would be attempted is :data:`STATUS_READY`; the rest carry the same reason
        codes :meth:`regenerate` would report. :data:`STATUS_BLOCKED` here is a PREDICTION — the
        hard prerequisites this build can see, evaluated against the note as it stands and
        assuming every other attempted field succeeds. It is deliberately optimistic (it can
        only under-report), because a run is the authority and a preview that refused a field
        the engine would have generated is worse than one that offered it.

        The master switch is NOT folded in: it is note-independent, :meth:`can_regenerate`
        already answers it, and duplicating it here would let the two disagree.
        """
        try:
            snapshot = self._read_note(note_id)
            config = self._config_for(snapshot)
            # ``None`` = every field of the note, resolved in ONE place (see :meth:`_classify`),
            # so this preview and the run it previews cannot drift into different field lists.
            order, refusals, candidates = self._classify(config, snapshot, None)
            blocked = self._predicted_blocks(config, snapshot, candidates)
            return {
                name: (
                    refusals[name].status
                    if name in refusals
                    else (STATUS_BLOCKED if name in blocked else STATUS_READY)
                )
                for name in order
            }
        except (
            Exception
        ):  # boundary: a UI asking "what can I offer?" gets an answer, always
            self._logger.exception(
                "smart_notes: could not read field states for note %s", note_id
            )
            return {}

    def regenerate(
        self, note_id: int, fields: Optional[Sequence[str]] = None
    ) -> list[FieldOutcome]:
        """Regenerate ``fields`` of the note (every field it has when ``None``).

        **Always overwrites.** This entry point exists to make a field AGAIN, so the per-field
        ``overwrite`` flag — which protects an automatic batch from clobbering work already
        done — is not what a user pressing a button labelled "regenerate" meant.

        The per-field ``enabled`` checkbox is honoured STRICTLY: a field whose Generate box is
        unticked comes back :data:`STATUS_RULE_OFF` and is not generated. The DAG is honoured
        among whatever was requested, so a field asked for together with the field it reads sees
        the fresh value, while a field asked for ALONE reads its dependency as the note holds it.

        Returns one :class:`FieldOutcome` per requested field, always, in the requested order —
        and for ``fields=None`` that is every field the NOTE has, in the note type's own order,
        exactly the list :meth:`field_states` previews. One field failing never stops another,
        and no per-field problem is raised — it is reported.

        Raises:
            Exception: Only when the note itself cannot be read (it was deleted, or Anki's main
                thread never answered). Everything after that point is an outcome, not a raise.
        """
        snapshot = self._read_note(note_id)
        settings = self._settings_provider()
        config = self._config_for(snapshot, settings)
        order, refusals, candidates = self._classify(
            config, snapshot, None if fields is None else list(fields)
        )
        if settings is None or not settings.regenerate_from_clippers:
            # Refusals are kept as they are: "there is no rule for this field" stays true
            # whatever the switch says, and pointing the user at a setting that would not help
            # them is worse than saying nothing.
            return [
                refusals.get(name)
                or FieldOutcome(
                    name,
                    STATUS_BLOCKED,
                    "Regeneration from the clippers is switched off "
                    "(Tools → Omnia → Smart Notes).",
                )
                for name in order
            ]
        if config is None or not candidates:
            return [refusals[name] for name in order]
        return self._run(snapshot, config, settings, order, refusals, candidates)

    # --- classification (shared by both public methods) -------------------------------------
    def _config_for(
        self,
        snapshot: _NoteSnapshot,
        settings: Optional[SmartNotesSettings] = None,
    ) -> Optional[SmartNotesNoteTypeConfig]:
        """Return the note type's smart-notes config, or ``None`` when it has none."""
        if settings is None:
            settings = self._settings_provider()
        if settings is None:
            return None
        return settings.note_type_config(snapshot.note_type)

    def _classify(
        self,
        config: Optional[SmartNotesNoteTypeConfig],
        snapshot: _NoteSnapshot,
        requested: Optional[list[str]],
    ) -> tuple[list[str], dict[str, FieldOutcome], list[str]]:
        """Split the request into what cannot be generated and what will be attempted.

        Returns ``(order, refusals, candidates)``: which field names to report on and in what
        order, the finished outcome of each one that will not be attempted, and the names that
        will.

        ``requested`` of ``None`` means "every field the NOTE has", which is the same list
        :meth:`field_states` asks about — deliberately, because the two must agree. Deriving it
        from the CONFIG instead is how "one status per field, always" quietly stopped holding
        for a whole-note request: an unconfigured note type collapsed to no fields at all (so
        the run answered ``[]`` while the preview reported ``no_rule`` for every field, and the
        clipper had nothing to render and nothing to say), and ``generatable_fields()`` filtered
        out exactly the rows whose ``rule_off``/``not_generatable`` refusal is the one thing the
        user needed to read. A note-derived order can emit the whole vocabulary; a
        config-derived one cannot.
        """
        order = requested if requested is not None else list(snapshot.fields)
        if config is None:
            return (
                order,
                {
                    name: FieldOutcome(
                        name,
                        STATUS_NO_RULE,
                        "This note type has no Smart Notes configuration.",
                    )
                    for name in order
                },
                [],
            )
        generatable = {row.field for row in config.generatable_fields()}
        rows = {row.field: row for row in config.fields}
        # Deck scope is a property of the whole config, not of one field, so it refuses every
        # candidate at once: the rules do not apply to the deck this note lives in.
        in_scope = not config.decks or any(
            applies_to_deck(config, deck_id) for deck_id in snapshot.deck_ids
        )
        refusals: dict[str, FieldOutcome] = {}
        candidates: list[str] = []
        for name in order:
            if name not in snapshot.fields:
                # The request names a field this note does not have (a stale clipper UI, a
                # renamed field). Checked here rather than at the write, so it is reported
                # instead of costing a provider call whose result has nowhere to go.
                refusals[name] = FieldOutcome(
                    name, STATUS_NO_RULE, f"This note has no field named “{name}”."
                )
            elif name not in generatable:
                refusals[name] = _refusal(config, rows.get(name), name)
            elif not in_scope:
                refusals[name] = FieldOutcome(
                    name,
                    STATUS_BLOCKED,
                    "This note's deck is outside the deck scope of its Smart Notes rules.",
                )
            else:
                candidates.append(name)
        # Deduplicated, because `candidates` becomes one RULE each and every rule is a provider
        # round trip on the user's paid key. A request naming one field fifty times would
        # otherwise buy fifty identical generations, and the body cap admits thousands of names.
        # `order` keeps its duplicates: it drives the reply, `outcomes` is keyed by name, so
        # every position the caller asked about still gets its answer.
        return order, refusals, list(dict.fromkeys(candidates))

    def _predicted_blocks(
        self,
        config: Optional[SmartNotesNoteTypeConfig],
        snapshot: _NoteSnapshot,
        candidates: list[str],
    ) -> set[str]:
        """Return the candidates whose HARD prerequisites are visibly unmet right now.

        A prerequisite counts as met when the note already holds a non-blank value for it, or
        when it is itself one of the fields about to be attempted. That makes this set a subset
        of what a real run would block, which is the direction :meth:`field_states` wants.
        """
        if config is None or not candidates:
            return set()
        wanted = set(candidates)
        present = {name.strip().lower() for name in wanted} | {
            name.strip().lower()
            for name, value in snapshot.fields.items()
            if str(value).strip()
        }
        return {
            rule.target_field
            for rule in compile_note_type_rules(config)
            if rule.target_field in wanted
            and any(
                field.strip().lower() not in present
                for field, kind in rule_prerequisites(rule)
                if kind == "hard"
            )
        }

    # --- the run ------------------------------------------------------------------------------
    def _run(
        self,
        snapshot: _NoteSnapshot,
        config: SmartNotesNoteTypeConfig,
        settings: SmartNotesSettings,
        order: list[str],
        refusals: dict[str, FieldOutcome],
        candidates: list[str],
    ) -> list[FieldOutcome]:
        """Generate the candidate fields, write them back, and account for every one of them."""
        rows = {row.field: row for row in config.fields}
        # A one-off config holding ONLY the requested rows, exactly as they are configured. Not
        # ``single_field_config``: that forces ``enabled=True`` for the editor menu, which here
        # would silently generate a field whose Generate box the user deliberately unticked.
        sub_config = config.copy(update={"fields": [rows[name] for name in candidates]})
        # Memoised per note so the media bytes are added ONCE: the chain needs the reference
        # during the run, the write-back needs it afterwards, and a second ``add_media_file``
        # would store a second copy under a renamed file the note never points at. Kept raw
        # (main-thread only) and wrapped for the generation phase below; the write-back already
        # runs on the main thread, so it calls this directly.
        materialize_once = note_materializer(snapshot.note_id)
        try:
            # Pooled, like every other generation path. "Generate all" on a note with ten
            # fields is ten provider round trips, and a person is watching a spinner while they
            # happen; serial would make the DAG's independent branches wait on each other for
            # no reason. The pool is built here and torn down by the context manager, and the
            # ceiling is the same `workers()` the batch runner and the editor button use, so
            # this surface cannot fan out wider than they do.
            with pooled_dispatch(settings.workers()) as dispatch:
                results, blocked, failed = self._service.generate_note(
                    sub_config,
                    dict(snapshot.fields),
                    allow_empty_fields=bool(settings.allow_empty_fields),
                    force_overwrite=True,
                    materialize=lambda rule, result: self._on_main(
                        lambda: materialize_once(rule, result)
                    ),
                    note_id=snapshot.note_id,
                    dispatch=dispatch,
                )
        except Exception as exc:
            # A whole-note failure — a hard dependency cycle in the note type's rules, or a
            # media write that could not reach the main thread. No single field is to blame, so
            # every attempted field carries the same reason and the caller still gets its full
            # list back rather than an exception it cannot render.
            self._logger.exception(
                "smart_notes: regeneration of note %s failed outright", snapshot.note_id
            )
            reason = str(exc) or exc.__class__.__name__
            return [
                refusals.get(name) or FieldOutcome(name, STATUS_ERROR, reason)
                for name in order
            ]
        written = self._write_back(snapshot.note_id, results, materialize_once)
        outcomes = dict(refusals)
        for rule, _result in results:
            name = rule.target_field
            outcomes[name] = (
                FieldOutcome(name, STATUS_GENERATED, "", written[name])
                if name in written
                else FieldOutcome(
                    name,
                    STATUS_ERROR,
                    "Generated, but the note could not be updated — see the Omnia log.",
                )
            )
        for block in blocked:
            outcomes[block.target_field] = FieldOutcome(
                block.target_field,
                STATUS_BLOCKED,
                f"Needs {_join(block.missing)}, which {_is_are(block.missing)} still empty.",
            )
        for failure in failed:
            # Both FailedField kinds land here: ``error`` (a tool broke) and ``unproductive``
            # (every tool declined). The chain's own summary — "cloze: word not found; ai: HTTP
            # 401" — is the message, because it is the only text that says WHICH step gave up.
            outcomes[failure.field] = FieldOutcome(
                failure.field,
                STATUS_ERROR,
                failure.error or "The generation chain produced nothing.",
            )
        self._account_for_skips(sub_config, snapshot, candidates, outcomes)
        return [outcomes[name] for name in order]

    def _account_for_skips(
        self,
        sub_config: SmartNotesNoteTypeConfig,
        snapshot: _NoteSnapshot,
        candidates: list[str],
        outcomes: dict[str, FieldOutcome],
    ) -> None:
        """Give a status to every candidate the engine never reported on.

        This is a DERIVATION, not a signal the engine emits. ``should_skip_rule`` drops a field
        silently, and ``generate_note`` offers no way to tell "skipped" from "never scheduled" —
        so a field appearing in none of ``results``/``blocked``/``failed`` was skipped, and
        (because this path forces overwrite, which rules out the already-filled branch) the only
        reason left is that every source it reads is blank.
        """
        missing = [name for name in candidates if name not in outcomes]
        if not missing:
            return
        rules = {
            rule.target_field: rule for rule in compile_note_type_rules(sub_config)
        }
        for name in missing:
            outcomes[name] = FieldOutcome(
                name,
                STATUS_SKIPPED,
                _skip_message(rules.get(name), snapshot.fields, outcomes),
            )

    # --- Anki glue: everything below touches the collection ------------------------------------
    def _read_note(self, note_id: int) -> _NoteSnapshot:
        """Read the note on the Qt main thread and flatten it for use off that thread."""

        def read() -> _NoteSnapshot:
            note = anki_compat.get_note(note_id)
            return _NoteSnapshot(
                note_id=note_id,
                note_type=_note_type_name(note),
                fields={name: note[name] for name in note.keys()},  # noqa: SIM118
                deck_ids=anki_compat.note_deck_ids(note),
            )

        return self._on_main(read)

    def _write_back(
        self,
        note_id: int,
        results: list[tuple[SmartNotesFieldRule, GenerationResult]],
        materialize: Callable[[Any, GenerationResult], str],
    ) -> dict[str, str]:
        """Write every generated result into the note; return ``{field: text}`` for what stuck.

        Re-reads the note rather than reusing the snapshot: generation took as long as the
        providers took, and the user may have edited the note meanwhile. A field that has gone
        missing since is left out of the returned map, which the caller turns into an error for
        that field alone.
        """
        if not results:
            return {}

        def write() -> dict[str, str]:
            note = anki_compat.get_note(note_id)
            written: dict[str, str] = {}
            for rule, result in results:
                name = rule.target_field
                if name not in note:
                    continue
                try:
                    text = materialize(rule, result)
                except Exception:  # one unwritable field must not discard its siblings
                    self._logger.exception(
                        "smart_notes: could not materialize field %s of note %s",
                        name,
                        note_id,
                    )
                    continue
                note[name] = text
                written[name] = text
            if written:
                anki_compat.update_note(note)
            return written

        return self._on_main(write)

    def _on_main(self, work: Callable[[], _T]) -> _T:
        """Run ``work`` on the Qt main thread and return its value (or re-raise its exception).

        Anki's collection may only be touched from the main thread, and this service is called
        from an HTTP worker — so the work is handed over and awaited. Called ON the main thread
        it simply runs inline (``taskman.run_on_main`` emits a same-thread Qt signal, which is a
        direct connection), so there is no deadlock here to avoid.

        Raises:
            TimeoutError: The main thread did not run the work within
                :data:`_MAIN_THREAD_TIMEOUT_SECONDS`.
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = work()
            except Exception as exc:  # carried back to the requesting thread
                box["error"] = exc
            finally:
                done.set()

        anki_compat.run_on_main(run)
        if not done.wait(_MAIN_THREAD_TIMEOUT_SECONDS):
            raise TimeoutError("Anki's main thread did not answer in time")
        if "error" in box:
            raise box["error"]
        value: _T = box["value"]
        return value


def _refusal(
    config: SmartNotesNoteTypeConfig,
    row: Optional[SmartNotesFieldConfig],
    name: str,
) -> FieldOutcome:
    """Explain why ``name`` is not a candidate, in the order the reasons exclude each other."""
    if name == config.base_field:
        return FieldOutcome(
            name,
            STATUS_NO_RULE,
            f"“{name}” is the input field these rules read — it is never generated.",
        )
    if row is None:
        return FieldOutcome(
            name, STATUS_NO_RULE, "No Smart Notes rule targets this field."
        )
    if not row.enabled:
        return FieldOutcome(
            name,
            STATUS_RULE_OFF,
            "Generate is unticked for this field — tick it in "
            "Tools → Omnia → Smart Notes.",
        )
    return FieldOutcome(
        name,
        STATUS_NOT_GENERATABLE,
        f"This version of Omnia cannot generate “{row.type}” fields — update Omnia.",
    )


def _skip_message(
    rule: Optional[SmartNotesFieldRule],
    fields: dict[str, str],
    outcomes: Optional[dict[str, FieldOutcome]] = None,
) -> str:
    """Say why the engine skipped ``rule``, naming a cause the user can act on.

    A blank source is the usual reason, but not always the real one: a source this same run
    tried and could not produce is blank for a reason of its own, and telling the user to fill
    it — or to turn on "generate even when sources are empty", which would change nothing —
    points away from the actual failure. So a source that already has a verdict wins.

    Args:
        rule: The compiled rule, or None when there is none.
        fields: The note's fields as they stood.
        outcomes: What the same run concluded about the other fields, if anything.
    """
    sources = rule_source_fields(rule) if rule is not None else []
    reported = outcomes or {}
    upstream = [
        name
        for name in sources
        if reported.get(name) is not None
        and reported[name].status in (STATUS_ERROR, STATUS_BLOCKED)
    ]
    if upstream:
        return f"Waiting on {_join(upstream)}, which did not generate — fix that field first."
    blank = [name for name in sources if not str(fields.get(name, "")).strip()]
    if not blank:
        return "Smart Notes found nothing to generate for this field."
    return (
        f"Nothing to work from: {_join(blank)} {_is_are(blank)} empty. Fill it, or turn "
        "on “Generate even when source fields are empty”."
    )


def _join(names: list[str]) -> str:
    """Render field names for a sentence: ``“A”``, ``“A” and “B”``, ``“A”, “B” and “C”``."""
    quoted = [f"“{name}”" for name in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def _is_are(names: list[str]) -> str:
    """The verb that agrees with a :func:`_join`-ed list of ``names``."""
    return "is" if len(names) == 1 else "are"


def _note_type_name(note: Any) -> str:
    """Return the note's note-type name across Anki versions (``note_type`` / ``model``)."""
    for attr in ("note_type", "model"):
        getter = getattr(note, attr, None)
        if callable(getter):
            data = getter()
            if isinstance(data, dict):
                return str(data.get("name", ""))
    return ""
