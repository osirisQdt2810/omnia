"""Word Lookup feature: answer "is this word already in my collection?" for the clippers.

The companion clippers float a magnifier next to their "+" — over whatever app you are reading
(desktop) or the page you are on (browser). Clicking it must show the matching card, but a
clipper is a separate process, and the decisions worth making centrally (which note types count,
which of a 35-field note type is worth showing, how hits are ranked) belong with the collection,
not duplicated in every client. Each client brings its own profile (see
:mod:`~omnia.plugins.word_lookup.config`); the logic is shared.

So this plugin owns the *logic* and exposes a small loopback endpoint; the clipper stays a thin
renderer. The split is deliberate:

* :mod:`~omnia.plugins.word_lookup.logic` — pure ranking/triage/cleaning (unit-tested headless);
* :mod:`~omnia.plugins.word_lookup.service` — the loopback HTTP surface + main-thread marshalling;
* this module — the only part that touches Anki: reading notes/cards out of the collection.

Everything the endpoint returns is display-ready, so a client never has to understand Anki's
field HTML, cloze markup, ``[sound:…]`` refs, or scheduler internals.

A client can also ask for a field to be REGENERATED (``POST /generate``). That work belongs to
smart_notes, and this plugin reaches it through the ``core/services`` seam rather than importing
it: smart_notes may be disabled or missing, and a lookup must answer exactly as it always did
when it is — turning one feature off may not degrade another.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.word_lookup.config import LookupProfile, WordLookupSettings
from omnia.plugins.word_lookup.logic import (
    LookupCard,
    LookupField,
    build_query,
    card_state,
    rank_cards,
    triage_fields,
)
from omnia.plugins.word_lookup.service import (
    LookupService,
    RegenerationDisabledError,
    RegenerationUnavailableError,
)

logger = get_logger("word_lookup")

# The service seam smart_notes publishes its regeneration on (see ``core/services``).
REGENERATION_SERVICE = "smart_notes.regeneration"
# Per-field generation states this module adds to the ones smart_notes reports: "there is
# nothing to ask" (no service at all) and "it knows no rule for this field".
STATE_UNAVAILABLE = "unavailable"
STATE_NO_RULE = "no_rule"

# The top-level ``regenerate_reason`` vocabulary: WHY a client may not ask for a regeneration.
# Two different problems used to reach the user as one message ("turn on Regenerate from
# clippers"), and only one of them names a box they can actually reach — the other is Smart
# Notes being off, where that box does not exist. Wire format, like the field states above.
# Nothing publishes regeneration at all (Smart Notes is off, or this build has no seam).
REASON_UNAVAILABLE = "unavailable"
REASON_OFF = "off"  # it is there, and the user unticked its clipper switch

# 32 bytes of entropy, url-safe (43 characters) — long enough that guessing it over loopback is
# not a threat model, short enough to paste into the web clipper's options page.
_TOKEN_BYTES = 32
_TOKEN_FILE = Path("clippers") / "lookup-token.txt"


def token_file_path(user_files_dir: Path) -> Path:
    """Where the desktop clipper reads the loopback token from."""
    return Path(user_files_dir) / _TOKEN_FILE


def write_token_file(user_files_dir: Path, token: str) -> Path:
    """Write ``token`` where the desktop clipper can read it, owner-only. Returns the path.

    The clipper is a separate process on the same machine, so a file under ``user_files`` is the
    one channel both sides already have (the browser extension cannot read files and is given
    the token by hand instead).

    Created through ``os.open`` with mode ``0o600`` rather than ``write_text`` then ``chmod``:
    the two-call version leaves the secret world-readable for the instant in between. The mode
    is honoured on POSIX; Windows ignores everything but the read-only bit, which is why the
    file lives in the profile's own ``user_files`` rather than anywhere shared.
    """
    path = token_file_path(user_files_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token)
    try:
        # os.open only applies the mode when it CREATES the file, so a token rotated into an
        # existing (possibly wider) file would otherwise keep the old permissions.
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("word_lookup: could not tighten the token file's permissions")
    return path


class RegenerationGateway:
    """word_lookup's view of smart_notes' regeneration, taken through the core service seam.

    The read path is TOTAL: smart_notes may be disabled, absent from this build, or raising,
    and a lookup must still answer exactly as it did before regeneration existed — so
    :meth:`can_regenerate` and :meth:`field_states` degrade to "unavailable" instead of failing.

    The write path is not: :meth:`regenerate` raises, because a client that asked to spend the
    user's LLM credits is owed a reason it can show them.
    """

    def __init__(self, service: Optional[Any]) -> None:
        self._service = service

    @classmethod
    def resolve(cls) -> RegenerationGateway:
        """Build a gateway around the published regeneration service (``None`` when absent)."""
        try:
            from omnia.core import services
        except ImportError:
            # The seam is not in this build at all. A lookup must not care.
            return cls(None)
        try:
            return cls(services.lookup(REGENERATION_SERVICE))
        except Exception:
            # Never let a broken seam take the lookup down with it.
            logger.exception("word_lookup: could not resolve %s", REGENERATION_SERVICE)
            return cls(None)

    @property
    def available(self) -> bool:
        """Whether a regeneration service is published at all."""
        return self._service is not None

    def can_regenerate(self) -> bool:
        """Whether a client may ask for a regeneration right now (the user's own switch)."""
        return not self.regenerate_reason()

    def regenerate_reason(self) -> str:
        """Why a client may not ask for a regeneration — ``""`` when it may.

        :meth:`can_regenerate` is derived from this rather than the other way round, so the flag
        and the reason a client shows for it can never disagree.

        Returns:
            :data:`REASON_UNAVAILABLE` (nobody publishes regeneration, or asking failed — either
            way there is no box to tick), :data:`REASON_OFF` (it is published and the user
            unticked "Regenerate from clippers"), or ``""``.
        """
        if self._service is None:
            return REASON_UNAVAILABLE
        try:
            enabled = bool(self._service.can_regenerate())
        except Exception:
            # A broken provider is not the user's setting: telling them to tick a box would be
            # a positive claim about a switch we never managed to read.
            logger.exception("word_lookup: can_regenerate failed")
            return REASON_UNAVAILABLE
        return "" if enabled else REASON_OFF

    def field_states(self, note_id: int, field_names: Sequence[str]) -> dict[str, str]:
        """Return one generation state per name in ``field_names``.

        Total by construction: every requested name gets a state, so a client never has to
        interpret a missing key. A name smart_notes reported on gets its state; the rest fall
        back to :data:`STATE_NO_RULE` when smart_notes ANSWERED and :data:`STATE_UNAVAILABLE`
        when it did not — because ``no_rule`` is a positive claim about the user's config ("add
        a rule in Anki"), and making it about a field whose rule exists sends them to fix
        something that is not broken.
        """
        if self._service is None:
            return dict.fromkeys(field_names, STATE_UNAVAILABLE)
        reported: dict[str, str] = {}
        try:
            reported = dict(self._service.field_states(int(note_id)) or {})
        except Exception:
            logger.exception("word_lookup: field_states failed for note %s", note_id)
        # An EMPTY map is smart_notes' own failure value, not an answer: its ``field_states``
        # catches everything and degrades to ``{}`` rather than raising, while a successful call
        # reports one state per field of the note — and a note type with no fields does not
        # exist. So "it said nothing" and "it broke" are the same case, and neither is a rule.
        fallback = STATE_NO_RULE if reported else STATE_UNAVAILABLE
        return {name: str(reported.get(name) or fallback) for name in field_names}

    def regenerate(self, note_id: int, fields: Optional[Sequence[str]]) -> list[Any]:
        """Regenerate ``fields`` (``None`` = all) and return smart_notes' per-field outcomes.

        Raises:
            RegenerationUnavailableError: smart_notes publishes no regeneration service.
            RegenerationDisabledError: the user switched clipper regeneration off.
        """
        if self._service is None:
            raise RegenerationUnavailableError(
                "Smart Notes is off, so there is nothing to regenerate with"
            )
        # Deliberately NOT self.can_regenerate(): on the write path an unexpected failure must
        # surface as a 500, not be reported to the user as "you turned this off".
        if not bool(self._service.can_regenerate()):
            raise RegenerationDisabledError(
                "Regenerating from a clipper is off — turn on "
                "“Regenerate from clippers” in Smart Notes → General."
            )
        return list(self._service.regenerate(int(note_id), fields))


@register("word_lookup")
class WordLookupPlugin(FeaturePlugin):
    """Serves word lookups from the collection to the companion desktop clipper."""

    name = "Word Lookup"
    description = "Let the clippers look a word up in your collection."
    # NOT "Integrations": that word belongs to the Smart Notes tab holding the clipper cards,
    # and a same-named category on the main grid read as that place while being somewhere else.
    # Not "AI" either, tempting as sitting beside Smart Notes is — lookup reads the collection
    # and calls no model, and that category is described as generating fields with one.
    group = "General"
    tooltip = (
        "Answers a clipper's magnifier: “is this word already in my collection?”\n"
        "\n"
        "• Runs a tiny service on 127.0.0.1 (loopback only — never reachable from the "
        "network).\n"
        "• Each clipper gets its own profile: the note types it searches, the fields it "
        "shows, how many hits it gets. Edit it in Tools → Omnia → Smart Notes → "
        "Integrations, with that clipper's “Lookup…” button — Configure here only holds the "
        "port and the token, which are shared by every clipper.\n"
        "• A matching note is returned display-ready: fields keep the note type's own order, "
        "the ones with nothing in them sort last (so a clipper can offer to fill them), and "
        "the card's state (new/learning/review + interval, reps, lapses) comes along.\n"
        "• A clipper may also ask Smart Notes to regenerate a field. That single write path "
        "needs a token, is refused for anything a web page started, and does nothing at all "
        "unless Smart Notes is on with “Regenerate from clippers” enabled.\n"
        "• Turn this off and the clipper's magnifier simply reports the lookup service is "
        "unavailable; nothing else changes."
    )
    order = 50
    config_model = WordLookupSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None
        self._service: Optional[LookupService] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        settings = self._settings()
        port = int(getattr(settings, "port", 8766))
        self._service = LookupService(
            self.lookup,
            media_dir=self._media_dir,
            generate=self.generate,
            token=self._ensure_token(ctx, settings),
            port=port,
            run_on_main=anki_compat.run_on_main,
        )
        if not self._service.start():
            logger.warning(
                "word_lookup: lookup service did not start (port %s in use?)", port
            )

    def on_disable(self, ctx: PluginContext) -> None:
        if self._service is not None:
            self._service.stop()
        self._service = None
        self._ctx = None
        # The token file is left in place on purpose: the socket is closed, so nothing can act
        # on the token, and the value stays in config either way. Deleting it would buy no
        # secrecy and would make a re-enable look like a first run to the desktop clipper.

    # -- the write path's key -------------------------------------------------------------

    def _ensure_token(self, ctx: PluginContext, settings: WordLookupSettings) -> str:
        """Return the clipper token, issuing and persisting one the first time.

        The token is what separates "the user's clipper" from "anything else that can open a
        socket on this machine" — every other process, and every page in the browser, can reach
        127.0.0.1 too. It is written to config (so it survives a restart) and to a file the
        desktop clipper reads.
        """
        token = str(getattr(settings, "token", "") or "").strip()
        if not token:
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            try:
                ctx.config.update_section(self.id, {"token": token})
            except Exception:
                # An unsaved token still works for this session, and the next start simply
                # issues another one into the file below. Losing the write path entirely
                # because config could not be written would be the worse failure.
                logger.exception("word_lookup: could not persist the clipper token")
        self._publish_token(ctx, token)
        return token

    @staticmethod
    def _publish_token(ctx: PluginContext, token: str) -> None:
        """Write the token to the file the desktop clipper reads (best effort)."""
        user_files = getattr(getattr(ctx, "paths", None), "user_files_dir", None)
        if user_files is None:
            return
        try:
            write_token_file(Path(user_files), token)
        except OSError:
            # The lookup itself never needs the token, so a read-only user_files degrades to
            # "the desktop clipper cannot regenerate" rather than "the feature will not start".
            logger.exception("word_lookup: could not write the clipper token file")

    # No ``custom_config_dialog``. What lookup returns is now a property of the CLIPPER asking,
    # not of the collection, so the picker is reached from each clipper's card in
    # Smart Notes → Integrations and opens scoped to that client. A second entry point here
    # could only edit the shared fallback, and it would sit in a card whose toggle is the one
    # thing this plugin still owns: whether the server runs at all.

    # -- the endpoint's payload ----------------------------------------------------------

    @staticmethod
    def _media_dir() -> str:
        """The collection's media folder, or ``""`` when there is no collection.

        Read lazily rather than captured at enable time: a profile switch swaps the collection
        underneath us, and a path captured once would serve the previous profile's media.
        """
        try:
            from aqt import mw

            return str(mw.col.media.dir()) if mw and mw.col else ""
        except Exception:
            return ""

    def lookup(self, word: str, client: str = "") -> dict[str, Any]:
        """Search the collection for ``word`` and return a display-ready payload.

        Runs on the Qt main thread (the service marshals it there) because it reads the
        collection. Never raises for an ordinary miss — a word that is simply not in the
        collection is a successful lookup with no cards.

        Args:
            word: The word/phrase the clipper captured.
            client: Which clipper asked, so it gets its own profile. ``""`` (an older build
                that sends none) resolves to the settings saved before profiles existed.

        Returns:
            ``{"word", "found", "truncated", "can_regenerate", "regenerate_reason",
            "cards": [...]}`` where each card carries its title, triaged fields, deck, tags and
            scheduling state. ``regenerate_reason`` is why ``can_regenerate`` is false, so a
            client renders one message per cause instead of guessing at the commoner one.
        """
        profile = self._settings().profile_for(client)
        gateway = RegenerationGateway.resolve()
        reason = gateway.regenerate_reason()
        query = build_query(
            word,
            tuple(profile.note_types),
            dict(profile.search_fields),
            match_word_forms=bool(profile.match_word_forms),
        )
        note_ids = anki_compat.find_note_ids(query) if query else []
        limit = max(1, int(profile.max_results))
        cards: list[LookupCard] = []
        for nid in note_ids[
            : limit * 3
        ]:  # over-read a little so ranking has room to reorder
            card = self._card_for_note(nid, profile, word)
            if card is not None:
                cards.append(card)
        ranked = rank_cards(cards, word)[:limit]
        return {
            "word": word,
            "found": bool(ranked),
            "truncated": len(note_ids) > limit,
            "can_regenerate": not reason,
            "regenerate_reason": reason,
            "cards": [self._card_payload(card, gateway) for card in ranked],
        }

    def generate(
        self, client: str, note_id: int, fields: Optional[list[str]]
    ) -> dict[str, Any]:
        """Regenerate a note's fields through smart_notes and report what they now hold.

        Runs on the HTTP worker thread, NOT the Qt main thread: generation calls LLM/TTS
        providers, and the main thread may not be held for that. smart_notes marshals the parts
        that touch the collection itself; only the read-back below hops over.

        Args:
            client: Which clipper asked (logged; the profiles do not change what is generated).
            note_id: The note to regenerate.
            fields: The field names to regenerate, or ``None`` for every field.

        Returns:
            ``{"note_id", "results": [{"field", "status", "message", "text", "audio",
            "images"}]}`` — one entry per field smart_notes reported on, carrying the STORED
            value so the clipper renders what the note actually holds now.

        Raises:
            RegenerationUnavailableError: smart_notes publishes no regeneration service.
            RegenerationDisabledError: the user switched clipper regeneration off.
        """
        logger.info(
            "word_lookup: %s asked to regenerate note %s (%s)",
            client or "an unnamed client",
            note_id,
            "all fields" if fields is None else ", ".join(fields),
        )
        outcomes = RegenerationGateway.resolve().regenerate(note_id, fields)
        stored = self._stored_fields(note_id)
        return {
            "note_id": int(note_id),
            "results": [self._outcome_payload(outcome, stored) for outcome in outcomes],
        }

    def _settings(self) -> WordLookupSettings:
        """The plugin's settings, read from the config repository on every request.

        "Read-through" rather than "re-read from disk": the repository answers from a merged
        cache that its own ``_reload`` refreshes, and a write through ``update_section`` goes
        through that. What matters here is that the value is not frozen at enable time.

        Not ``ctx.settings``, which is the snapshot ``PluginManager`` took at enable time and
        only ``manager.reload()`` refreshes — so a lookup profile saved from a clipper's
        “Lookup…” dialog was not served until Anki restarted, while the smart_notes settings the
        very same panel depends on applied at once (its store re-reads per request). One feature
        whose two settings behave differently is the defect; both are read-through now.

        The one setting this cannot make live is ``port``: it is bound to a socket at enable
        time, so its description says a change needs a restart rather than pretending otherwise.

        Falls back to the enable-time snapshot (then to the defaults) when the read fails — a
        config that cannot be parsed must degrade to a stale lookup, never to no lookup.
        """
        ctx = self._ctx
        fresh = self._read_settings(ctx)
        if fresh is not None:
            return fresh
        snapshot = getattr(ctx, "settings", None) if ctx is not None else None
        return (
            snapshot
            if isinstance(snapshot, WordLookupSettings)
            else WordLookupSettings()
        )

    def _read_settings(
        self, ctx: Optional[PluginContext]
    ) -> Optional[WordLookupSettings]:
        """Read this plugin's settings through the config repository (``None`` on any failure)."""
        config = getattr(ctx, "config", None) if ctx is not None else None
        if config is None:
            return None
        try:
            settings = config.feature_settings(self.id)
        except Exception:
            # Boundary: a config the models cannot parse (hand-edited, or written by a newer
            # Omnia) must not take the lookup service down with it.
            logger.exception(
                "word_lookup: could not re-read settings; using the snapshot"
            )
            return None
        return settings if isinstance(settings, WordLookupSettings) else None

    def _stored_fields(self, note_id: int) -> dict[str, LookupField]:
        """The note's fields as STORED, keyed by name and cleaned the way ``/lookup`` cleans.

        Read AFTER generating, so what the clipper renders is what the note now holds — not
        what the generator believed it wrote. Marshalled onto the Qt main thread through the
        service (a collection read), and empty when the note has since been deleted.

        NEVER raises. By the time this runs the note has already been rewritten and the
        provider budget has already been spent, so a read-back that cannot get the main thread
        inside the READ path's 5 s budget (a sync, the Browser opening) must not turn a finished,
        paid-for generation into a 503 the clients render as "Smart Notes is not available right
        now". An empty map degrades to what the generator reported (see
        :meth:`_outcome_payload`), so every outcome still reaches the client.
        """

        def read() -> dict[str, LookupField]:
            note = anki_compat.get_note_or_none(int(note_id))
            if note is None:
                return {}
            return {
                name: LookupField.from_raw(name, str(value))
                for name, value in note.items()
            }

        service = self._service
        if service is None:
            return read()
        try:
            return service.call_on_main(read)
        except Exception:
            logger.exception(
                "word_lookup: could not read note %s back after generating", note_id
            )
            return {}

    @staticmethod
    def _outcome_payload(
        outcome: Any, stored: dict[str, LookupField]
    ) -> dict[str, Any]:
        """Flatten one smart_notes ``FieldOutcome`` plus the field's stored value into JSON.

        Read defensively off the outcome: it comes from another feature's module, and a lookup
        response is not the place to discover that one attribute was renamed.
        """
        name = str(getattr(outcome, "field", "") or "")
        field = stored.get(name)
        if field is None:
            # Not on the note (renamed or removed between the request and the read-back): fall
            # back to what the generator reported, so the client still sees something.
            field = LookupField.from_raw(name, str(getattr(outcome, "text", "") or ""))
        return {
            "field": name,
            "status": str(getattr(outcome, "status", "") or ""),
            "message": str(getattr(outcome, "message", "") or ""),
            "text": field.text,
            "audio": list(field.audio),
            "images": list(field.images),
        }

    def _card_for_note(
        self, nid: int, profile: LookupProfile, word: str = ""
    ) -> Optional[LookupCard]:
        """Build one :class:`LookupCard` from a note id, or ``None`` if it can't be read.

        Best-effort per note: a note deleted between the search and this read, or an
        unexpected shape, is skipped rather than failing the whole lookup.
        """
        try:
            note = anki_compat.get_note(nid)
            note_type = self._note_type_name(
                note
            )  # needed for the per-note-type field list
            # items() preserves the NOTE TYPE's field order, which the triage uses as its
            # relevance signal — never sort or re-key this.
            ordered = [(name, str(value)) for name, value in note.items()]
            title, fields = triage_fields(
                ordered,
                word=word,
                max_fields=int(profile.max_fields),
                hidden=tuple(profile.hidden_fields),
                # An explicit per-note-type list wins over the automatic pick.
                only=tuple(profile.display_fields.get(note_type, [])),
            )
            first_card = self._first_card(note)
            return LookupCard(
                note_id=int(nid),
                note_type=note_type,
                deck=self._deck_name(first_card),
                title=title or str(nid),
                fields=tuple(fields),
                tags=tuple(getattr(note, "tags", []) or []),
                state=card_state(getattr(first_card, "type", 2) or 0),
                interval_days=int(getattr(first_card, "ivl", 0) or 0),
                reps=int(getattr(first_card, "reps", 0) or 0),
                lapses=int(getattr(first_card, "lapses", 0) or 0),
            )
        except Exception:
            logger.exception("word_lookup: could not read note %s", nid)
            return None

    @staticmethod
    def _note_type_name(note: Any) -> str:
        """The note's note-type name (best-effort across Anki versions)."""
        try:
            model = note.note_type()
        except Exception:
            return ""
        return str((model or {}).get("name", ""))

    @staticmethod
    def _first_card(note: Any) -> Any:
        """The note's first card, or ``None`` (a note always has one in practice)."""
        try:
            cards = note.cards()
        except Exception:
            return None
        return cards[0] if cards else None

    @staticmethod
    def _deck_name(card: Any) -> str:
        """The card's deck name, or ``""`` when it can't be resolved."""
        if card is None:
            return ""
        try:
            col = anki_compat.main_window().col
            return str(col.decks.name(card.did))
        except Exception:
            return ""

    @staticmethod
    def _card_payload(card: LookupCard, gateway: RegenerationGateway) -> dict[str, Any]:
        """Flatten a :class:`LookupCard` into JSON for the clipper."""
        states = gateway.field_states(card.note_id, [f.name for f in card.fields])
        return {
            "note_id": card.note_id,
            "note_type": card.note_type,
            "deck": card.deck,
            "title": card.title,
            "tags": list(card.tags),
            "state": card.state,
            "interval_days": card.interval_days,
            "reps": card.reps,
            "lapses": card.lapses,
            "fields": [
                WordLookupPlugin._field_payload(
                    f, states.get(f.name, STATE_UNAVAILABLE)
                )
                for f in card.fields
            ],
        }

    @staticmethod
    def _field_payload(field: LookupField, state: str) -> dict[str, Any]:
        """One field for the clipper: what it holds, and what could be generated into it."""
        return {
            "name": field.name,
            "text": field.text,
            "kind": field.kind,
            "audio": list(field.audio),
            "images": list(field.images),
            # Kept even when blank (see LookupField.is_empty) — the clipper styles a blank slot
            # differently, and it is the slot most worth generating into.
            "empty": field.is_empty,
            "state": state,
        }
