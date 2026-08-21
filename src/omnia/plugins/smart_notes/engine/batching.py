"""LAYER 3 — one provider call for the SAME field across several notes (K-note batching).

The only clean axis for batching is *same field, many notes*: those calls share a provider, a
model and a prompt TEMPLATE by construction, and they sit in the same dependency level, so
grouping them re-orders nothing. Batching across FIELDS would mix provider/model pairs and
dependency levels, and is never done here.

**The failure this module is built around is positional misalignment.** Send K items, get K-1
back, zip the answers onto the notes by POSITION, and every note after the gap silently
receives another note's content — which is then written to the collection with no error
anywhere. So every item carries an explicit, opaque id; matching is by id and only by id; the
array index is read for iteration and for nothing else; and any note whose id did not come back
falls back to its own individual call. A response that renumbers, drops, duplicates or
reorders items therefore costs extra calls, never wrong content.

Two shapes are treated as unusable even though they parse, because both are a model that has
lost the id↔item correspondence while still looking well-formed:

* a **repeated id** discards EVERY copy of itself, not merely the copies after the first —
  the first copy is not "the good one", it is the one that arrived first (:func:`match_items`);
* a **collapsed answer** — one string returned for two or more items whose own inputs differ —
  discards every item in that group (:func:`collapsed_indexes`). The check is per GROUP, not per
  chunk: a reply that copies item 1's answer onto items 2-4 and answers item 5 properly is
  PARTIALLY collapsed, and a whole-chunk test sees nothing wrong with it.

What a chunk can degrade into, in order (:meth:`ChunkTask.run`):

===========================  =================================================================
situation                    action
===========================  =================================================================
every id matched             done — one call for K notes
some ids matched             apply those; the rest fall back to individual calls
nothing usable came back     halve the chunk ONCE, then individual calls
some answers collapsed       those items are dropped; they fall back like any unmatched note
provider error (not 429)     straight to individual calls — not a shape problem
provider error, 429          NO fan-out: every note in the chunk gets an ``error`` field
===========================  =================================================================

The 429 row is the one that looks inconsistent and is not: fanning K individual retries into a
rate-limit window amplifies exactly the thing that limited us, and ``kind="error"`` is what
keeps those notes out of ``empty_note_ids`` — the list whose consumer DELETES clipped notes.

**Context bleed is not solved here, and cannot be.** A model given ten items in one request may
let item 3's subject matter leak into item 7's answer. That is a QUALITY risk no amount of
parsing discipline can detect, let alone fix. It is addressed in the prompt (an explicit
isolation instruction, and each item carrying only the values its own template references) and
mitigated by a smaller K; the residual is real, and it is why this sits behind an env flag at
all — ``OMNIA_SMART_NOTES_BATCHING = -1`` turns the feature off entirely and restores the
pre-batching path. The flag ships at 10, so the residual is accepted by default and the escape
hatch is one variable away.

**Batching also widens one poisoned note's blast radius from 1 to K.** Note content is
interpolated into a request shared with K-1 other notes, so instruction-shaped text inside one
field — and smart-notes' first-class input path is a web clipper, i.e. text a stranger wrote —
is read by the model as part of the same conversation that is answering for its neighbours. The
values are ``json.dumps``-escaped, so the envelope itself cannot be broken; what cannot be
escaped is that a model reads them. Solo generation contains such a note to itself. This is a
second reason the feature is off unless someone turns it on deliberately.

Pure logic — no ``aqt``/``anki``. The one piece of shared mutable state (:class:`FieldBudget`)
is written from a dispatch worker and therefore takes a lock.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Optional, Protocol, Union

from omnia.core.logging import get_logger
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.llm.base import PromptParts
from omnia.plugins.smart_notes.config import DEFAULT_TOOL_NAME
from omnia.plugins.smart_notes.engine.generators import GenerationResult
from omnia.plugins.smart_notes.engine.interpolation import extract_field_refs
from omnia.plugins.smart_notes.engine.markdown import convert_markdown_to_html
from omnia.plugins.smart_notes.engine.tools.pipeline import PipelineResult, ToolAttempt

if TYPE_CHECKING:
    from omnia.core.concurrency.dispatch import Dispatch
    from omnia.core.providers import ProviderHub
    from omnia.plugins.smart_notes.config import SmartNotesFieldRule

logger = get_logger("smart_notes")

# Bytes of randomness in an item id (6 hex characters). NOT the note id — a 13-digit epoch is
# something a model may reformat or truncate, and it would leak a stable identifier into the
# prompt. NOT an ordinal either: "n0".."n9" invites renumbering, and a hallucinated ordinal
# looks perfectly plausible. An opaque token that does not come back is unambiguously a MISS.
_ID_BYTES = 3

# ```json … ``` around an otherwise fine answer is the single most common shape deviation, and
# refusing it would send a whole chunk down the fallback ladder for nothing.
_FENCE_RE = re.compile(r"\A\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?\s*```\s*\Z", re.DOTALL)

# The envelope's head, identical for every chunk of a field — which is precisely the shape
# LAYER 2 caches, so the two layers compose instead of competing. The user's template is quoted
# VERBATIM and UNINTERPOLATED between the fences: nothing here rewrites what the user wrote.
_ENVELOPE = (
    "You will perform the SAME task for several independent items.\n"
    "\n"
    "TASK (identical for every item; {{Field}} refers to that item's own values):\n"
    "<<<TASK\n"
    "__TASK__\n"
    "TASK>>>\n"
    "\n"
    "RULES\n"
    "- Treat every item in complete isolation. Never mention, reuse, or copy any content,\n"
    "  wording or subject matter from another item.\n"
    "- Substitute only that item's own values.\n"
    '- Return ONLY a JSON object: {"items":[{"id":"<the item\'s id, copied verbatim>",'
    '"content":"<the answer for that item>"}]}\n'
    "- Return exactly one object per item. Do not add, drop, merge or reorder ids.\n"
    "\n"
    "ITEMS\n"
)

# Vendor-neutral JSON Schema for the reply. Each provider adapts it to its own JSON mode (see
# LLMProvider.generate_json); a provider without one ignores it entirely and the parser below
# does the work. Deliberately minimal — no ``additionalProperties``, which some vendors' schema
# subsets reject.
ITEMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["id", "content"],
            },
        }
    },
    "required": ["items"],
}


class BatchShapeError(ValueError):
    """A batched response could not be read as ``{"items":[{"id","content"}]}``.

    Distinct from a :class:`~omnia.core.providers.errors.ProviderError`: the call SUCCEEDED and
    was paid for, the answer just came back in a shape we cannot route. The two lead to
    different rungs of the fallback ladder — a shape problem is worth one retry at half the
    size, a provider failure is not.
    """


@dataclass(frozen=True)
class FieldWork:
    """One field of one note, ready to run: the rule, its frozen inputs, and the solo route.

    ``solo`` is the field's ordinary route — its whole tool chain, through the pipeline, exactly
    as it runs when nothing is batched. Carrying it on the work item (rather than letting the
    batching layer reach for a pipeline) is what lets every fallback in this module be "just run
    it the normal way", and what keeps :class:`SoloPlanner` dependency-free.
    """

    rule: SmartNotesFieldRule
    fields: Mapping[str, str]
    note_id: int
    solo: Callable[[], PipelineResult]


class WaveTask(ABC):
    """One dispatchable unit of a wave, and the wave slots whose outcomes it answers for.

    A task is what reaches the :class:`~omnia.core.concurrency.dispatch.Dispatch`. It
    answers for one slot (the ordinary case) or for several (a chunk), which is the whole reason
    the wave is addressed by slot rather than by position in the dispatch list.
    """

    def __init__(self, slots: Sequence[int]) -> None:
        self.slots: tuple[int, ...] = tuple(slots)

    @abstractmethod
    def run(self) -> list[Union[PipelineResult, Exception]]:
        """Produce one outcome per entry of :attr:`slots`, in that same order."""


class SoloTask(WaveTask):
    """One field, one round trip: the pre-batching behaviour, unchanged."""

    def __init__(self, slot: int, work: FieldWork) -> None:
        super().__init__((slot,))
        self._work = work

    def run(self) -> list[Union[PipelineResult, Exception]]:
        """Run the field's own tool chain. A raise is caught by the dispatch, as before."""
        return [self._work.solo()]


class WavePlanner(Protocol):
    """Turns a wave's field work into the tasks a dispatch actually runs."""

    def plan(self, works: Sequence[FieldWork]) -> list[WaveTask]:
        """Return tasks covering every index of ``works`` exactly once."""
        ...  # pragma: no cover - protocol


class SoloPlanner:
    """One task per field — the pre-batching path, and exactly what ``K = 1`` means.

    Costs nothing and knows nothing: no provider, no envelope, no id matching. Batching ships
    ON (``OMNIA_SMART_NOTES_BATCHING`` defaults to 10), so this is what ``-1`` restores rather
    than what most users run. It stays a genuinely separate path so that turning batching off
    yields the code that existed before it, not a batching path narrowed to a width of one.
    """

    def plan(self, works: Sequence[FieldWork]) -> list[WaveTask]:
        """Wrap every field in its own task, in wave order."""
        return [SoloTask(index, work) for index, work in enumerate(works)]


# Stateless, so one shared instance serves every caller.
SOLO_PLANNER = SoloPlanner()


def chunk_key(rule: SmartNotesFieldRule) -> Optional[tuple[str, ...]]:
    """The key rules are grouped on, or ``None`` when the rule must not be batched at all.

    The key is ``(note type, field, provider, model, template)``. Within one cohort and one
    round the note type and the template are already fixed, so provider/model could not differ
    either — the full tuple is computed anyway so that a future per-note provider override can
    never silently merge two different models into one call. It is the same ``(provider,
    model)`` pair :meth:`~omnia.core.providers.ProviderHub.llm` caches on, which is what
    guarantees one chunk maps to exactly one provider instance.

    ``None`` — run it alone — for anything whose "one call" is not one text completion:

    * a non-text rule: image and tts return BYTES, one synthesis per note;
    * a rule whose chain is not the lone parameter-less ``ai`` tool: a deterministic or
      user-authored tool may make no provider call at all, and a chain has fallbacks a single
      merged request cannot express;
    * a rule with no template: its prompt IS one field's value, so there is no shared
      instruction to amortise and nothing the envelope would be quoting.
    """
    if rule.kind != "text":
        return None
    if len(rule.tools) != 1:
        return None
    only = rule.tools[0]
    if only.name != DEFAULT_TOOL_NAME or only.params:
        return None
    if not rule.prompt:
        return None
    return (
        rule.note_type,
        rule.target_field,
        rule.provider,
        rule.model,
        rule.prompt,
    )


def parse_batch_items(raw: str) -> list[tuple[Any, Any]]:
    """Read a batched response into its ``(id, content)`` pairs.

    Accepts ``{"items": [...]}`` or a bare top-level list, with or without a Markdown code
    fence. Anything else raises :class:`BatchShapeError` — the caller's cue to fall back, never
    to guess.

    Args:
        raw: The provider's answer, verbatim.

    Returns:
        One ``(id, content)`` pair per object in the array, unvalidated: deciding what is
        usable is :func:`match_items`' job, and it needs to see the rejects to count them.

    Raises:
        BatchShapeError: The answer was not JSON, or carried no array of items.
    """
    text = raw.strip()
    fence = _FENCE_RE.match(text)
    if fence is not None:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise BatchShapeError(f"response was not JSON ({exc})") from exc
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise BatchShapeError("response carried no 'items' array")
    return [
        (entry.get("id"), entry.get("content"))
        for entry in data
        if isinstance(entry, dict)
    ]


def match_items(
    pairs: Sequence[tuple[Any, Any]], by_id: Mapping[str, int]
) -> dict[int, str]:
    """Route each returned item onto the note whose id it CARRIES — never onto its neighbour.

    This function is the whole safety property of LAYER 3, so it is worth stating what it does
    NOT do: it never reads a pair's position in ``pairs``. The index in the loop below exists
    only to walk the list. If K items were sent and K-1 come back, the notes that came back are
    matched and the one that did not is simply absent from the result — it cannot be handed
    another note's answer, because nothing here maps position to note.

    An id that is unknown (the model invented or renumbered it) is DISCARDED rather than applied
    to a best guess, and so is a non-string or blank ``content``.

    **A REPEATED id discards EVERY copy of itself, not just the copies after the first.** An id
    that comes back twice is a model that has lost the id↔item correspondence, and the first
    copy is not "the good one" — it is simply the one that arrived first. Keeping it and
    dropping the rest is the wrong-content-no-error outcome this whole module exists to make
    impossible: a duplicate whose first copy holds another item's answer writes that answer onto
    the note whose id it wears, silently, while the note that was actually answered falls back
    to a correct solo call and looks fine. Counting the ids up front and skipping the whole
    equivalence class costs one pass and turns the case back into what the ladder handles:
    unmatched notes, individual calls, extra cost, no corruption.

    Args:
        pairs: The ``(id, content)`` pairs :func:`parse_batch_items` read out.
        by_id: The ids this chunk actually sent, mapped to their note's index.

    Returns:
        ``{note index: content}`` for the items that matched, in no particular order.
    """
    seen = Counter(item_id for item_id, _content in pairs if isinstance(item_id, str))
    matched: dict[int, str] = {}
    for item_id, content in pairs:
        if not isinstance(item_id, str) or seen[item_id] > 1:
            continue  # unknown shape, or an id the model answered more than once
        index = by_id.get(item_id)
        if index is None:
            continue  # an id we never sent — never applied to anything
        if not isinstance(content, str) or not content.strip():
            continue
        matched[index] = content
    return matched


def collapsed_indexes(
    matched: Mapping[int, str], values: Mapping[int, Mapping[str, str]]
) -> set[int]:
    """The matched items that must be DISCARDED because they share one answer.

    The failure this catches is the commonest one in K-item batching and the only one the id
    discipline cannot see: the model answers one item, then repeats that answer under other ids
    it was given. Every id is correct, every id is unique, nothing is missing — and those notes
    are written one note's content.

    **Collapse is per GROUP, not per chunk.** An earlier version asked "did every matched item
    come back with the same string?", which only fires when the model collapses the WHOLE reply.
    A model that answers items 1-4 with item 1's text and then gives item 5 its own answer
    defeats that test completely — two distinct strings in the chunk, nothing discarded, four
    notes silently written a fifth note's content. So the question is asked of each equal-content
    group on its own: any answer string shared by two or more items whose own inputs DIFFER is
    unusable, and every item in that group is dropped (there is no way to tell which of them the
    model was actually answering). Full collapse is then just the case where the one group is the
    whole chunk, and it takes the same route it always did — nothing left to route, so the
    caller's ladder halves once and then falls back per note.

    Identical INPUTS are exempt, because two notes that really do carry the same word should get
    the same definition, and calling that a failure would fan a duplicate-heavy deck out into
    individual calls forever. A group that mixes both — some items sharing inputs, one not — is
    discarded whole for the same reason as above: the shared answer cannot be attributed.

    The cost of the wider check is extra calls on a legitimately low-cardinality field (a
    "part of speech" prompt whose honest answer is "noun" for half the deck): those notes fall
    back to the calls they would have made with batching off. That is the trade this module
    makes everywhere — never wrong content, sometimes more calls.

    Args:
        matched: What :func:`match_items` routed, keyed by the sender's item index.
        values: Each item's own interpolated values, keyed the same way.

    Returns:
        The indexes to drop from ``matched`` (empty when nothing collapsed).
    """
    groups: dict[str, list[int]] = {}
    for index, content in matched.items():
        groups.setdefault(content, []).append(index)
    collapsed: set[int] = set()
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        inputs = {
            json.dumps(values.get(index, {}), sort_keys=True, ensure_ascii=False)
            for index in indexes
        }
        if len(inputs) > 1:
            collapsed.update(indexes)
    return collapsed


class FieldBudget:
    """Chooses how many notes one call may carry, per field, and learns from the answers.

    Two things shrink K. An OUTPUT BUDGET: a chunk asks for one answer per item in a single
    completion, so K items must fit in one output cap — start from a pessimistic per-item
    estimate and refine it from what the field's answers actually cost. And a HALVING: once a
    field's chunk has come back unusable, that field keeps the smaller size for the rest of the
    run, which is what makes "truncation → smaller K" durable instead of a per-call coin flip.

    The estimate only ever grows (so K only ever shrinks): a heuristic that could grow K again
    on one short answer would oscillate for the rest of a long batch. Nothing is PERSISTED —
    a stored heuristic is config drift with no UI to explain or reset it.

    Read on the driver thread at plan time and written from a dispatch worker at call time,
    hence the lock. Cheap: two dict operations per chunk.
    """

    # What one item's answer is assumed to cost before this run has seen one. Deliberately
    # generous — the cost of guessing high is a smaller first chunk, the cost of guessing low is
    # a truncated answer and a wasted call.
    _FIRST_ITEM_TOKENS = 512
    # A floor, so a field whose answers are one word does not compute a K of several hundred.
    _MIN_ITEM_TOKENS = 128

    def __init__(self, requested: int, *, output_tokens: int) -> None:
        self._requested = max(1, int(requested))
        self.output_tokens = max(1, int(output_tokens))
        self._lock = threading.Lock()
        self._per_item: dict[tuple[str, ...], int] = {}
        self._ceiling: dict[tuple[str, ...], int] = {}

    def size_for(self, key: tuple[str, ...]) -> int:
        """How many notes a chunk of ``key`` may carry right now (at least 1)."""
        with self._lock:
            per_item = self._per_item.get(key, self._FIRST_ITEM_TOKENS)
            ceiling = self._ceiling.get(key, self._requested)
        return max(1, min(self._requested, ceiling, self.output_tokens // per_item))

    def tokens_for(self, key: tuple[str, ...], items: int) -> int:
        """The ``max_tokens`` ONE chunk of ``items`` asks for: the per-item estimate times K.

        A chunk MUST send a cap, and the solo path deliberately does not. The difference is not
        an oversight, it is the difference between the two requests. A solo call produces one
        answer, and the provider's own ceiling is a reasonable bound for it; a chunk produces K
        answers inside one completion, where the provider's ceiling is shared between them and
        the K+1'th token is the one that silently cuts the last item in half. Sending a cap the
        chunk's own size implies turns that from a property of the provider's defaults into a
        stated contract: this many items, this much room each. When the answers do not fit, the
        reply truncates, :func:`parse_batch_items` refuses it, and the ladder halves — every time
        and on every provider, instead of whenever a vendor default happens to be tight.

        A FLAT cap (what this used to send) makes the same request cost a different amount of
        room per item depending on how many items it carries, so the same field truncates at K=10
        and not at K=5 for reasons nothing here can see.
        """
        with self._lock:
            per_item = self._per_item.get(key, self._FIRST_ITEM_TOKENS)
        return max(1, min(self.output_tokens, per_item * max(1, int(items))))

    def observe(self, key: tuple[str, ...], longest_chars: int) -> None:
        """Record the longest answer ``key`` has produced, in characters.

        Converted at the usual four-characters-per-token rule of thumb, with half again on top:
        the estimate has to cover the NEXT answer, not the last one, and under-estimating costs
        a truncated chunk.
        """
        estimate = max(int(longest_chars / 4 * 1.5), self._MIN_ITEM_TOKENS)
        with self._lock:
            self._per_item[key] = max(self._per_item.get(key, 0), estimate)

    def unusable(self, key: tuple[str, ...], size: int) -> None:
        """Record that a chunk of ``key`` came back unreadable, and shrink accordingly.

        Two things move together, because an unusable answer is evidence about both: the field
        keeps chunks of at most ``size`` for the rest of the run, AND its per-item estimate
        doubles. Capping the size alone would be pointless now that the output cap is
        proportional to K (:meth:`tokens_for`) — half the items asking for half the tokens gives
        each item exactly the room that just failed, so the retry would truncate identically. The
        commonest cause of an unreadable reply IS truncation, and the answer to truncation is
        more room per item, not merely fewer items.
        """
        with self._lock:
            self._ceiling[key] = min(
                self._ceiling.get(key, self._requested), max(1, int(size))
            )
            per_item = self._per_item.get(key, self._FIRST_ITEM_TOKENS)
            self._per_item[key] = min(self.output_tokens, per_item * 2)


@dataclass(frozen=True)
class _ChunkAttempt:
    """What one batched call came back with: what matched, and how it failed if it did.

    ``responded`` is the distinction the ladder turns on. An answer that ARRIVED and could not
    be routed is a shape problem — plausibly a truncated one — and is worth exactly one retry at
    half the size. A call that never produced an answer at all is a provider problem, and half
    of a failing request is still a failing request: that goes straight to individual calls,
    where the retry policy and per-field isolation apply.
    """

    matched: dict[int, str] = dataclass_field(default_factory=dict)
    error: Optional[Exception] = None
    # A 429 is the one failure that must not fan out into K individual retries.
    rate_limited: bool = False
    responded: bool = False


class ChunkTask(WaveTask):
    """K notes' worth of ONE field, answered by a single provider call — or safely by fewer.

    Owns the whole fallback ladder documented at the top of this module. Nothing it does can
    produce a wrong-content-no-error outcome: every route out of here either applies content the
    provider tagged with that exact note's id, or runs the note's own ordinary call, or records
    an explicit field error.
    """

    def __init__(
        self,
        slots: Sequence[int],
        works: Sequence[FieldWork],
        *,
        providers: ProviderHub,
        budget: FieldBudget,
        key: tuple[str, ...],
    ) -> None:
        super().__init__(slots)
        self._works = list(works)
        self._providers = providers
        self._budget = budget
        self._key = key

    def run(self) -> list[Union[PipelineResult, Exception]]:
        """Resolve every note in the chunk, one way or another, in slot order."""
        resolved: dict[int, PipelineResult] = {}
        self._resolve(list(range(len(self._works))), resolved, halving=True)
        return [resolved[index] for index in range(len(self._works))]

    def _resolve(
        self,
        indexes: list[int],
        resolved: dict[int, PipelineResult],
        *,
        halving: bool,
    ) -> None:
        """Fill ``resolved`` for every index in ``indexes``, descending the ladder as needed."""
        attempt = self._send(indexes)
        if attempt.rate_limited:
            # No fan-out. K individual retries inside a rate-limit window amplify precisely the
            # thing that limited us — and ``kind="error"`` (which an error attempt produces)
            # keeps every one of these notes out of ``empty_note_ids``, whose consumer deletes
            # clipped notes. A provider outage must never look like "nothing to make here".
            error = attempt.error or ProviderError("rate limited", status_code=429)
            for index in indexes:
                resolved[index] = _errored(error)
            self._log("rate-limited, no fallback", indexes, attempt.error)
            return
        for index, content in attempt.matched.items():
            resolved[index] = _produced(content)
        missing = [index for index in indexes if index not in resolved]
        if not missing:
            return
        if halving and attempt.responded and not attempt.matched and len(missing) > 1:
            # An answer arrived and none of it could be routed — a truncated reply, a refusal,
            # or ids we never sent. Halve ONCE and remember the smaller size; halving
            # recursively would turn one broken provider into a 2K-call storm.
            self._budget.unusable(self._key, max(1, len(missing) // 2))
            self._log("unusable response, halving", missing, attempt.error)
            middle = len(missing) // 2
            for half in (missing[:middle], missing[middle:]):
                self._resolve(half, resolved, halving=False)
            return
        self._log("falling back per note", missing, attempt.error)
        for index in missing:
            resolved[index] = self._solo(index)

    def _send(self, indexes: list[int]) -> _ChunkAttempt:
        """Make ONE batched call for ``indexes`` and report what could be routed from it."""
        by_id: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        values_by_index: dict[int, Mapping[str, str]] = {}
        for index in indexes:
            item_id = secrets.token_hex(_ID_BYTES)
            while item_id in by_id:  # pragma: no cover - 1-in-16M, but silence is worse
                item_id = secrets.token_hex(_ID_BYTES)
            by_id[item_id] = index
            values_by_index[index] = self._values(self._works[index])
            items.append({"id": item_id, "values": values_by_index[index]})
        rule = self._works[indexes[0]].rule
        parts = PromptParts(
            _ENVELOPE.replace("__TASK__", rule.prompt),
            json.dumps(items, ensure_ascii=False),
        )
        llm = self._providers.llm(model=rule.model, provider=rule.provider)
        try:
            raw, _usage = llm.generate_json(
                # A COPY: the schema is a module constant handed to provider code that is
                # contractually allowed to "adapt it to its own wire format", and every chunk of
                # a wave would otherwise pass the same object from a different thread. No
                # shipped provider mutates it; one deepcopy per chunk is cheaper than finding
                # out that a future one does.
                parts,
                schema=deepcopy(ITEMS_SCHEMA),
                # Sized to THIS chunk, not to the module's ceiling — see FieldBudget.tokens_for
                # for why a chunk caps its output where a solo call does not.
                max_tokens=self._budget.tokens_for(self._key, len(indexes)),
            )
        except ProviderError as exc:
            # No answer arrived, so there is no shape to retry smaller: an individual call
            # restores the retry policy and per-field isolation, which is the best response to a
            # provider that is failing.
            return _ChunkAttempt(error=exc, rate_limited=exc.status_code == 429)
        except Exception as exc:  # a broken provider must not escape the wave
            return _ChunkAttempt(error=exc)
        try:
            pairs = parse_batch_items(raw)
        except BatchShapeError as exc:
            return _ChunkAttempt(error=exc, responded=True)
        matched = match_items(pairs, by_id)
        collapsed = collapsed_indexes(matched, values_by_index)
        if collapsed:
            # One answer returned for several items whose own inputs differ: the model answered
            # one of them and copied it across the others, and there is no way to tell which. The
            # group is dropped — the notes in it become unmatched, which is the case the ladder
            # already handles. When the collapse was total nothing is left, so this still lands on
            # the "arrived but nothing could be routed" rung, exactly as the old whole-chunk check
            # did; when it was partial the items the model really did answer are still applied.
            self._log("collapsed answers, discarding", sorted(collapsed), None)
            matched = {
                index: content
                for index, content in matched.items()
                if index not in collapsed
            }
        if matched:
            self._budget.observe(
                self._key, max(len(content) for content in matched.values())
            )
        return _ChunkAttempt(matched=matched, responded=True)

    def _values(self, work: FieldWork) -> dict[str, str]:
        """The note's values for the refs its own template names — and nothing else.

        A smaller payload, and less of another note's material in front of the model: context
        bleed cannot be parsed away, so the only lever is not putting the material there.
        """
        seen: dict[str, str] = {}
        for ref in extract_field_refs(work.rule.prompt):
            name = ref.strip()
            if name and name not in seen:
                seen[name] = str(work.fields.get(name, ""))
        return seen

    def _solo(self, index: int) -> PipelineResult:
        """Run one note's field the ordinary way, after the chunk could not answer for it."""
        work = self._works[index]
        try:
            return work.solo()
        except Exception as exc:  # mirrors the pipeline's own per-field isolation
            logger.exception(
                "smart_notes: batch fallback failed for field %r on note %s",
                work.rule.target_field,
                work.note_id,
            )
            return _errored(exc)

    def _log(
        self, what: str, indexes: Sequence[int], error: Optional[Exception]
    ) -> None:
        """Record a ladder step with the chunk's field and the notes it affected."""
        logger.info(
            "smart_notes: batch %r for field %r: %s (notes %s)%s",
            self._key[0] or "?",
            self._key[1],
            what,
            ", ".join(str(self._works[index].note_id) for index in indexes),
            f" — {error}" if error is not None else "",
        )


class FieldBatchRunner:
    """Groups a wave's eligible fields into one call per (field, provider, model, template).

    Everything else in the wave — media, tool chains, template-less fields, and any group that
    came out a single note wide — is planned as an ordinary :class:`SoloTask`, so turning
    batching on never changes how those fields are generated.
    """

    def __init__(
        self,
        providers: ProviderHub,
        *,
        notes_per_call: int,
        output_budget_tokens: int = 8192,
    ) -> None:
        self._providers = providers
        self._budget = FieldBudget(notes_per_call, output_tokens=output_budget_tokens)

    def plan(self, works: Sequence[FieldWork]) -> list[WaveTask]:
        """Return the wave's tasks, ordered by the first slot each answers for.

        Only notes whose gate already said "dispatch" are here at all: the block gate, the skip
        predicate and the overwrite rule ran on the driver thread before the wave was built
        (:meth:`~omnia.plugins.smart_notes.engine.note_run.NoteRun.next_dispatch`), so "only
        notes that actually need the field enter a chunk" costs this method nothing.
        """
        tasks: list[WaveTask] = []
        groups: dict[tuple[str, ...], list[int]] = {}
        for index, work in enumerate(works):
            key = chunk_key(work.rule)
            if key is None:
                tasks.append(SoloTask(index, work))
            else:
                groups.setdefault(key, []).append(index)
        for key, indexes in groups.items():
            size = self._budget.size_for(key)
            for start in range(0, len(indexes), size):
                slots = indexes[start : start + size]
                if len(slots) == 1:
                    # A chunk of one is a solo call wearing an envelope: same round trip, more
                    # tokens, and a parse step that can only lose.
                    tasks.append(SoloTask(slots[0], works[slots[0]]))
                    continue
                tasks.append(
                    ChunkTask(
                        slots,
                        [works[index] for index in slots],
                        providers=self._providers,
                        budget=self._budget,
                        key=key,
                    )
                )
        # Nothing downstream depends on this (outcomes are addressed by slot), but it keeps a
        # sequentially dispatched wave running in the order the notes were selected.
        tasks.sort(key=lambda task: task.slots[0])
        return tasks


def run_wave(
    tasks: Sequence[WaveTask], slots: int, dispatch: Dispatch
) -> list[Union[PipelineResult, Exception]]:
    """Dispatch a planned wave and scatter each task's outcomes back into wave order.

    A task answers for one slot (an ordinary field) or several (a K-note chunk), so the wave is
    addressed by SLOT and never by position in the dispatch list. Every slot is filled before
    this returns, because a hole would reach ``NoteRun.commit`` as something that is neither a
    result nor an exception.

    A task that RAISED is attributed to every slot it owned. That is a last-resort net, not the
    normal route: a chunk resolves its own failures into per-note outcomes precisely so one bad
    response cannot be charged to K notes. The same goes for a task that answers with the wrong
    number of outcomes — a bug, but one that must surface as K explicit field errors rather than
    as content silently landing on the wrong note.
    """
    # Typed loosely for the fill: every slot is written before this returns, so no None
    # survives — but a partially built list cannot be typed as if it were already complete.
    outcomes: list[Any] = [None] * slots
    outcomes_by_task = dispatch.run([task.run for task in tasks])
    # strict: Dispatch promises one result per thunk, in order. A plain zip would SILENTLY
    # truncate a short return and leave those slots None — the very state the comment
    # above says cannot survive. Fail loudly instead of shipping a half-filled note.
    for task, produced in zip(tasks, outcomes_by_task, strict=True):
        if isinstance(produced, Exception):
            for slot in task.slots:
                outcomes[slot] = produced
            continue
        if len(produced) != len(task.slots):  # pragma: no cover - defensive
            broken = RuntimeError(
                f"generation task answered for {len(produced)} of "
                f"{len(task.slots)} fields"
            )
            logger.error("smart_notes: %s", broken)
            for slot in task.slots:
                outcomes[slot] = broken
            continue
        for slot, outcome in zip(task.slots, produced, strict=True):
            outcomes[slot] = outcome
    return outcomes


def _produced(content: str) -> PipelineResult:
    """Wrap batched content in the SAME tail an individually generated field produces.

    Markdown rendering and the ``ai`` provenance stamp both happen here for the same reason:
    without them a batched field would render differently from the identical field generated
    alone, and the batch summary's "fell back to a later tool" count would start firing on
    fields that did no such thing.
    """
    return PipelineResult(
        GenerationResult(
            "text", text=convert_markdown_to_html(content), tool=DEFAULT_TOOL_NAME
        ),
        (ToolAttempt(DEFAULT_TOOL_NAME, "produced"),),
    )


def _errored(exc: Exception) -> PipelineResult:
    """An exhausted chain carrying ``exc``, so the note records a field ERROR.

    The ``kind`` matters more than the message: ``"error"`` is what
    :class:`~omnia.plugins.smart_notes.engine.note_run.NoteRun` turns into a
    ``FailedField(kind="error")``, and that is what keeps the note out of ``empty_note_ids`` and
    therefore out of the clip discarder. A chunk failure miscategorised as "nothing to make
    here" would turn a provider outage into deleted user notes.
    """
    return PipelineResult(
        None, (ToolAttempt(DEFAULT_TOOL_NAME, "error", str(exc), error=exc),)
    )
