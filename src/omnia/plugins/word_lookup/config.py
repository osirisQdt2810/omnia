"""Word Lookup settings (the plugin's own Pydantic v1 config).

The generic settings form is derived from this model via
:func:`omnia.core.config.schema.schema_from_model`.

**Per-client profiles.** Two clippers now call the same loopback service — the browser
extension and the desktop app — and they are not looking at the same thing: the desktop
clipper floats over a book and wants the whole note, the web clipper sits next to a web page
and usually wants two fields. So every *content* setting (which note types are searched, which
fields are shown, …) lives in a :class:`LookupProfile` under ``clients``, keyed by the client
that asked. Only :attr:`WordLookupSettings.port` and :attr:`WordLookupSettings.token` stay
top-level: there is exactly ONE server, and its port is machine-specific (a port that is free
on this machine may be taken on the next one, so it must not be a per-client — or synced —
choice).

**Upgrades.** Every version before this one wrote those seven settings as FLAT top-level keys.
They are no longer declared here, which is precisely why they survive: ``PersistedModel``
allows unknown keys and round-trips them verbatim (ADR-010), and
:meth:`WordLookupSettings.legacy_profile` reads them back as the profile for *both* clippers.
An upgrade therefore keeps the user's configuration instead of silently resetting it, and a
downgrade still finds the keys it wrote.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError

from omnia.core.config.base import PersistedModel
from omnia.core.logging import get_logger

logger = get_logger("word_lookup")

# The clipper keys, i.e. the ``client=`` values the two companion apps send. Same vocabulary as
# smart_notes' integration registry, deliberately NOT imported from it: word_lookup must keep
# answering with smart_notes disabled or absent, so it may not depend on one of its modules.
WEB_CLIPPER = "web_clipper"
DESKTOP_CLIPPER = "desktop_clipper"
CLIENT_KEYS: tuple[str, ...] = (WEB_CLIPPER, DESKTOP_CLIPPER)


class LookupProfile(PersistedModel):
    """What ONE client is served: the note types it searches and the fields it sees.

    Held per client in :attr:`WordLookupSettings.clients`. The defaults are what every client
    was served before profiles existed, so an unconfigured client behaves exactly as before.
    """

    note_types: list[str] = Field(
        default_factory=list,
        title="Searchable note types",
        description=(
            "Note type names the lookup searches (one per line).\n"
            "• Empty = search the WHOLE collection.\n"
            "• Listing the few note types you actually study makes hits precise and fast."
        ),
    )
    max_results: int = Field(
        5,
        ge=1,
        le=25,
        title="Max results",
        description="How many matching notes to return for one lookup.",
    )
    max_fields: int = Field(
        8,
        ge=1,
        le=30,
        title="Max fields per card",
        description=(
            "How many of a note's fields to show under the title.\n"
            "• Fields with nothing in them sort LAST, so a 35-field note type stays readable "
            "while a never-filled field is still offered (that is the one worth generating).\n"
            "• Fields are otherwise kept in the note type's own field order."
        ),
    )
    search_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        title="Fields to search, per note type",
        description=(
            "``{note type: [field, …]}``. A note type listed here is searched ONLY in those "
            "fields; anything not listed is searched across all of its fields.\n"
            "• A listed field matches the word as a WHOLE WORD: looking up 'port' finds "
            "'port' and 'port of call', but not 'important' or 'Portion'.\n"
            "• Narrowing to the headword field (e.g. Word) stops a hit on a word merely "
            "mentioned inside another card's examples or synonyms.\n"
            "• Matching is case-insensitive — Anki folds case itself, so LEVEL, Level and "
            "level are the same search."
        ),
    )
    display_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        title="Fields to show, per note type",
        description=(
            "``{note type: [field, …]}``. Listed fields are shown in the order given; a note "
            "type that is not listed falls back to the automatic pick (the first "
            "``max_fields`` fields, in the note type's own field order)."
        ),
    )
    match_word_forms: bool = Field(
        True,
        title="Also match other forms of the word",
        description=(
            "Look up plausible base forms too, so double-clicking an inflected word still "
            "finds the card.\n"
            "• 'loved' also tries 'love'; 'studies' tries 'study'; 'running' tries 'run'.\n"
            "• Off = match only the word exactly as captured."
        ),
    )
    hidden_fields: list[str] = Field(
        default_factory=list,
        title="Never show these fields",
        description=(
            "Field names to always hide in the lookup result (one per line, "
            "case-insensitive) — e.g. bookkeeping fields like 'Note ID'."
        ),
    )


class WordLookupSettings(PersistedModel):
    """Settings for looking a word up in the collection from a companion clipper."""

    clients: dict[str, LookupProfile] = Field(
        default_factory=dict,
        title="Per-clipper lookup profiles",
        description=(
            "``{client: profile}`` — what each clipper searches and shows. Keys are "
            "``web_clipper`` and ``desktop_clipper``; a client with no profile falls back to "
            "the settings saved before profiles existed, then to the defaults."
        ),
    )
    port: int = Field(
        8766,
        ge=1024,
        le=65535,
        title="Lookup service port",
        description=(
            "Loopback port the clippers call to run a lookup.\n"
            "• Bound to 127.0.0.1 ONLY — never reachable from the network.\n"
            "• One server for every client, so this is deliberately NOT a per-client setting.\n"
            "• Change it only if another program already uses this port.\n"
            "• Takes effect after Anki restarts (or after you switch Word Lookup off and on "
            "again) — the port is bound to a socket when the feature starts. Every other "
            "lookup setting applies to the very next lookup."
        ),
    )
    token: str = Field(
        "",
        title="Clipper access token",
        description=(
            "Shared secret a clipper must send (header ``X-Omnia-Token``) to REGENERATE a "
            "field. Looking a word up never needs it — only the write path does.\n"
            "• Issued automatically the first time the feature is enabled, and written to "
            "``user_files/clippers/lookup-token.txt`` so the desktop clipper can read it.\n"
            "• Clear it to have a new one issued (then paste the new value into the web "
            "clipper)."
        ),
    )

    def profile_for(self, client: str) -> LookupProfile:
        """Return the profile ``client`` must be served with.

        Resolution order: the named client's profile → the flat settings saved before profiles
        existed → the defaults. An unknown or missing client RESOLVES rather than raising:
        older clipper builds send no ``client`` at all, and they have to keep working exactly
        as they did.

        Args:
            client: The ``client=`` value the request carried (``""`` when it carried none).
        """
        profile = self.clients.get(client.strip()) if client else None
        return profile if profile is not None else self.legacy_profile()

    def legacy_profile(self) -> LookupProfile:
        """The profile made from the flat keys written before per-client settings existed.

        Those keys are not declared on this model any more, so they arrive as ``extra`` values
        (kept and round-tripped, see the module docstring) and are read back here. Anything the
        stored data does not carry keeps its default, so a fresh install lands on the defaults
        by the same path.
        """
        stored = {
            key: value
            for key, value in self.dict().items()
            if key in LookupProfile.__fields__
        }
        try:
            profile: LookupProfile = LookupProfile.parse_obj(stored)
            return profile
        except ValidationError:
            # A value the old model would have rejected (hand-edited, out of range) must not
            # take the whole lookup down with it — that config was already broken before this
            # upgrade, and a working lookup on defaults beats no lookup at all.
            logger.warning(
                "word_lookup: ignoring unusable legacy settings; using the defaults"
            )
            return LookupProfile()


def store_profile(
    raw: dict[str, Any],
    client: str,
    profile: dict[str, Any],
    *,
    port: int,
    settings: WordLookupSettings,
) -> dict[str, Any]:
    """Build the section update that saves ONE client's lookup profile.

    Lives here, not in the dialog, so the rule can be tested without Qt — and so there is one
    copy of it. ``ConfigRepository.update_section`` merges SHALLOWLY, which means writing
    ``clients`` replaces the WHOLE map: an update built from an empty dict would silently
    delete every other clipper's profile, including a client key a newer build added that this
    one cannot name (ADR-010, one layer above the models). So it starts from what is stored.

    The same rule applies INSIDE one client's entry, which is why ``settings`` is needed. The
    dialog renders six of :class:`LookupProfile`'s seven settings, so an entry rebuilt from what
    it posts drops ``hidden_fields`` — and any per-profile setting a newer Omnia added — on
    every save. The entry is therefore seeded with what this client RESOLVES to today (its
    stored entry, else the flat pre-profile settings it is currently served from) and the
    posted keys are written over that: keep what you could not render.

    The no-``client`` branch needs no seeding: it writes the six keys as top-level settings, and
    ``update_section`` merges top-level keys, so a flat ``hidden_fields`` is left alone.

    Args:
        raw: The section exactly as stored (``ConfigRepository.raw_section``).
        client: The clipper this profile belongs to. Empty edits the flat fallback that older
            clipper builds — the ones that send no ``client`` — are still served from.
        profile: The content settings the dialog rendered.
        port: The server's port. Top level however this was reached: there is one server and
            both clippers talk to it, so a per-client port could not mean anything.
        settings: The plugin's settings as loaded, for the fallback this client is served from
            when it has no stored entry yet.

    Returns:
        The mapping to hand to ``update_section``.
    """
    if client:
        clients = dict(raw.get("clients") or {})
        entry = dict(clients.get(client) or settings.legacy_profile().dict())
        entry.update(profile)
        clients[client] = entry
        section: dict[str, Any] = {"clients": clients}
    else:
        section = dict(profile)
    section["port"] = port
    return section
