"""The note type a saved correction becomes, and the card it renders as.

A correction is transient by design — the panel closes and it is gone. This is the other half of
that: the sentence you got wrong, kept as something you will be asked again.

**The front is the mistake, not the answer.** A card whose front already showed the correction
would be a card you cannot fail, which is a card that teaches nothing. So the front is the phrase
exactly as it was written, and the question is the one the panel answered.

**The back has to fit.** Anki gives a card one screenful and a scrollbar; a review where the
answer is half off the bottom is a review nobody finishes. Three things do the work:

* the fixes are a LIST of small cards, not prose — each one is three short lines;
* past a few of them the list splits into two columns, because a tall narrow list is the thing
  that overflows and a short wide one is not;
* the corrected sentence is pinned last, so whatever happens above it, the thing worth reading
  is where the eye ends up.

Beyond a dozen or so fixes a card will still scroll, and that is the honest limit of the idea —
a phrase with fifteen mistakes wants rewriting, not annotating.

Pure string building. No ``anki``, no ``aqt``, no collection: everything here is testable with
nothing installed, which matters because a card template is markup nobody reads until it is
wrong on screen.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any

#: The note type Omnia creates for saved corrections.
#:
#: Named for what it holds rather than for the plugin, because it shows up in Anki's own note
#: type list beside the user's decks and "phrase_check" means nothing there.
NOTE_TYPE_NAME = "Omnia Phrase Check"

#: The card template's name inside that note type. One card per note: a second, reversed one
#: would be asking someone to produce a mistake from its correction.
CARD_NAME = "Correction"

#: The fields, in order. The first is the sort field and Anki's duplicate check, which is why
#: the phrase itself leads: two saves of the same sentence should look like the duplicate it is.
FIELD_PHRASE = "Phrase"
FIELD_CORRECTED = "Corrected"
FIELD_FIXES = "Fixes"
FIELD_REGISTER = "Register"
FIELDS = (FIELD_PHRASE, FIELD_CORRECTED, FIELD_FIXES, FIELD_REGISTER)

#: From this many fixes the list splits into two columns. Three still reads as a list; four in
#: one column is when the answer starts running off the bottom of a review.
TWO_COLUMN_FROM = 4


def _esc(value: Any) -> str:
    """HTML-escape one value.

    Applied to everything without exception: a fix's words come from a model, and the phrase is
    whatever the user selected in a browser. Both end up inside a note field, and a note field is
    rendered as HTML for ever after.
    """
    return html.escape("" if value is None else str(value))


def fixes_html(fixes: Iterable[Any]) -> str:
    """The list of fixes, as the note's ``Fixes`` field.

    Each fix is ``before → after`` with its reason underneath. The two-column decision is made
    HERE rather than in the card's CSS, because CSS cannot count: a media query knows the screen
    and a container query knows the box, and neither knows whether there are two fixes or nine.

    Args:
        fixes: Objects with ``before``, ``after``, ``why``, ``kind`` and ``is_deletion``.

    Returns:
        HTML, or ``""`` when there is nothing to show — an empty field so the template's own
        ``{{#Fixes}}`` section can disappear rather than leaving a heading over nothing.
    """
    items = []
    for fix in fixes:
        before = _esc(getattr(fix, "before", ""))
        after = (
            '<span class="pc-gone">removed</span>'
            if getattr(fix, "is_deletion", False)
            else f'<span class="pc-after">{_esc(getattr(fix, "after", ""))}</span>'
        )
        kind = _esc(getattr(fix, "kind", "")).strip()
        why = _esc(getattr(fix, "why", "")).strip()
        items.append(
            '<li class="pc-fix">'
            f'<div class="pc-change"><span class="pc-before">{before}</span>'
            f'<span class="pc-arrow">&rarr;</span>{after}</div>'
            + (f'<div class="pc-kind">{kind}</div>' if kind else "")
            + (f'<div class="pc-why">{why}</div>' if why else "")
            + "</li>"
        )
    if not items:
        return ""
    columns = " pc-two" if len(items) >= TWO_COLUMN_FROM else ""
    return f'<ul class="pc-fixes{columns}">' + "".join(items) + "</ul>"


def corrected_html(runs: Iterable[Any]) -> str:
    """The rewritten phrase, with the changed words marked.

    The runs are computed by the add-on and stored as rendered HTML rather than recomputed at
    review time: a card is read years after it was made, and a template that re-derived the diff
    would be a second implementation of it — one that cannot be fixed without rewriting every
    note already saved.
    """
    parts = []
    for run in runs or ():
        # By TYPE, not by a try/except around the indexing: a string is indexable, so a stray
        # "nope" in the list would come out as the letter "n" marked by the letter "o" being
        # truthy — a corrupted sentence rather than a skipped entry.
        if not isinstance(run, (list, tuple)) or not run:
            continue
        text = run[0]
        is_new = bool(run[1]) if len(run) > 1 else False
        parts.append(
            f'<mark class="pc-new">{_esc(text)}</mark>' if is_new else _esc(text)
        )
    return "".join(parts)


def note_fields(correction: Any) -> dict[str, str]:
    """A correction as the note type's fields.

    Args:
        correction: A :class:`~omnia.plugins.phrase_check.correction.Correction`.

    Returns:
        ``{field name: HTML}`` for every field in :data:`FIELDS`.
    """
    return {
        FIELD_PHRASE: _esc(getattr(correction, "original", "")),
        FIELD_CORRECTED: corrected_html(_runs(correction)),
        FIELD_FIXES: fixes_html(getattr(correction, "fixes", ()) or ()),
        FIELD_REGISTER: _esc(getattr(correction, "mode", "")),
    }


def _runs(correction: Any) -> tuple[tuple[Any, bool], ...]:
    """The highlight runs, or the plain rewrite when they cannot be had."""
    try:
        runs = tuple(correction.highlighted())
    except Exception:
        runs = ()
    return runs or ((getattr(correction, "rewritten", ""), False),)


def front_template() -> str:
    """The question side: the phrase as it was written, and nothing that gives the answer away."""
    return (
        '<div class="pc-card pc-front">'
        '<div class="pc-ask">What is wrong with this?</div>'
        '<div class="pc-phrase">{{' + FIELD_PHRASE + "}}</div>"
        "{{#"
        + FIELD_REGISTER
        + '}}<div class="pc-register">as {{'
        + FIELD_REGISTER
        + "}}</div>{{/"
        + FIELD_REGISTER
        + "}}"
        "</div>"
    )


def back_template() -> str:
    """The answer side: every fix, then the corrected sentence, in that order.

    ``{{FrontSide}}`` is deliberately NOT reused. It would repeat the "What is wrong with this?"
    prompt above the answer, where the question is no longer being asked — and it would spend the
    vertical room the fixes need. The phrase is shown again, quietly, because a correction with
    nothing to compare against is half an answer.
    """
    return (
        '<div class="pc-card pc-back">'
        '<div class="pc-original">{{' + FIELD_PHRASE + "}}</div>"
        "{{#" + FIELD_FIXES + "}}{{" + FIELD_FIXES + "}}{{/" + FIELD_FIXES + "}}"
        '<div class="pc-final">'
        '<div class="pc-final-label">Corrected</div>'
        '<div class="pc-corrected">{{' + FIELD_CORRECTED + "}}</div>"
        "</div>"
        "</div>"
    )


def card_css() -> str:
    """The note type's stylesheet.

    Written for Anki's reviewer rather than for a browser, which constrains it more than it
    looks:

    * **No CSS variables for the palette.** Anki's night mode adds ``.night_mode`` to the body,
      and a card is also rendered in the card-layout preview, in the browser's preview pane and
      in AnkiMobile — all with different chrome. Plain rules under both selectors survive all of
      them; a token defined in one place and consumed in another does not, because a template
      can be copied out of this note type into a user's own without its ``:root``.
    * **No animation, no transition.** A review is a still image; motion on a card is noise
      during the one second someone is deciding whether they knew the answer.
    * **Columns rather than a scrollbar.** ``column-count`` is decades old and works everywhere
      Anki runs, and it is the only mechanism that turns a tall list into a short one without
      knowing anything about the screen.
    """
    return """
.pc-card {
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  color: #1d2230; text-align: left;
  max-width: 680px; margin: 0 auto; padding: 4px 10px 10px;
  line-height: 1.5;
}
.night_mode .pc-card { color: #e7e9f0; }

/* The question side is one short thing and is centred vertically-ish by its own padding; the
   answer side is a full page and starts at the top. */
.pc-front { padding-top: 26px; }
.pc-back { padding-top: 6px; }

/* -- the question side ---------------------------------------------------------------- */
.pc-ask {
  font-size: 12px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase;
  color: #8a8f9c; margin-bottom: 10px;
}
.night_mode .pc-ask { color: #8e95a6; }
.pc-phrase { font-size: 23px; font-weight: 600; line-height: 1.45; }
.pc-register {
  display: inline-block; margin-top: 12px; padding: 3px 11px; border-radius: 999px;
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.3px;
  color: #147a4b; background: rgba(31, 157, 99, 0.13);
}
.night_mode .pc-register { color: #5fd39b; background: rgba(31, 157, 99, 0.2); }

/* -- the answer side ------------------------------------------------------------------ */
/* The phrase again, quietly: a correction with nothing to compare against is half an answer.
   Struck through rather than merely greyed, so which of the two sentences is the wrong one is
   readable without relying on colour. */
.pc-original {
  font-size: 14px; color: #a2453c; text-decoration: line-through;
  text-decoration-color: rgba(162, 69, 60, 0.45);
  margin-bottom: 14px;
}
.night_mode .pc-original { color: #e0736a; text-decoration-color: rgba(224, 115, 106, 0.45); }

.pc-fixes { list-style: none; margin: 0 0 16px; padding: 0; }
/* Two columns once the list is long enough to run off the bottom. `break-inside: avoid` is what
   stops a fix being split across the gap, which is the failure that makes columns look broken
   rather than tidy. */
.pc-fixes.pc-two { column-count: 2; column-gap: 12px; }
.pc-fix {
  break-inside: avoid; -webkit-column-break-inside: avoid; page-break-inside: avoid;
  margin: 0 0 8px; padding: 8px 11px 9px;
  border: 1px solid rgba(20, 30, 60, 0.11); border-left: 3px solid rgba(31, 157, 99, 0.5);
  border-radius: 8px; background: rgba(246, 248, 252, 0.75);
}
.night_mode .pc-fix {
  border-color: rgba(255, 255, 255, 0.09); background: rgba(38, 42, 49, 0.6);
}
.pc-change { font-size: 14px; }
.pc-before {
  color: #a2453c; text-decoration: line-through;
  text-decoration-color: rgba(162, 69, 60, 0.5);
}
.night_mode .pc-before { color: #e0736a; }
.pc-arrow { color: #9aa1ac; margin: 0 6px; font-size: 12px; }
.pc-after { color: #147a4b; font-weight: 650; }
.night_mode .pc-after { color: #5fd39b; }
.pc-gone { color: #9aa1ac; font-style: italic; }
.pc-kind {
  margin-top: 5px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px;
  text-transform: uppercase; color: #8a8f9c;
}
.pc-why { margin-top: 5px; font-size: 12.5px; color: #4a515b; }
.night_mode .pc-why { color: #b9bfc8; }

/* -- the corrected sentence, last ----------------------------------------------------- */
.pc-final { border-top: 1px solid rgba(20, 30, 60, 0.12); padding-top: 11px; }
.night_mode .pc-final { border-top-color: rgba(255, 255, 255, 0.1); }
.pc-final-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase;
  color: #8a8f9c; margin-bottom: 5px;
}
.pc-corrected { font-size: 19px; font-weight: 600; line-height: 1.5; }
/* The changed words. A wash behind them as well as the weight: bold alone disappears in a
   sentence that already has some, and this is read at a glance. */
.pc-new {
  background: rgba(31, 157, 99, 0.18); color: inherit;
  border-radius: 3px; padding: 0 3px; font-weight: 700;
}
.night_mode .pc-new { background: rgba(31, 157, 99, 0.3); color: inherit; }

/* Anki centres card content by default; these undo it for this note type only. */
.card { text-align: left; }
"""
