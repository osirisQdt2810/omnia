# Typed Accuracy — grading typed answers into bands

On a card with a type-in answer, Anki marks each character of what you typed as right, wrong or
missing. Typing Accuracy turns that into a ratio — correct characters over all the characters
Anki marks — and
**stages** an ease from it: when the answer side appears it picks the grade, and whichever review
button you then press (Enter and Space included) answers with the staged grade. Every result is
also logged for a panel on Anki's Statistics screen.

The ratio falls into up to three bands:

| Ratio | Staged ease | Default |
|---|---|---|
| below the pass mark (`threshold`) | fail ease | Hard |
| from the pass mark up to the high mark | pass ease | Good |
| at or above the high mark (`high_threshold`) | high ease | Easy |

With the high mark off (stored as `0`, or anything at or below the pass mark) there are two bands,
pass and fail. Setting any ease to `no` stages nothing for that band, so your own press stands.

## Where

*Tools → Omnia* → the **Typing Accuracy** tile → switch it on → *Configure…*. The two marks share
one track: the left handle is the pass mark, the right one the high mark, and each has a box
beside its name that takes an exact value (`0.72` is fine; the handles move in steps of `0.05`).

If **Display Interval** is on, its "interval: …" label is redrawn once the grade is staged, so it
shows the interval of the grade that will actually be recorded.

## Setup

A throwaway profile, and a deck of **Basic (type in the answer)** cards — Anki's built-in note type
whose template carries `{{type:Back}}`. Put a few words with obvious misspellings to test against
on the Back side: `necessary`, `accommodate`, `rhythm`.

## Recipes

### 1. Two bands (the high mark off)

Configure: pass mark `0.70`, high mark **off**. Review `necessary`:

| You type | Ratio | Expected |
|---|---|---|
| `necessary` | 1.00 | pass → **Good** |
| `necesary` | 0.89 | pass → **Good** |
| `nesesary` | 0.74 | pass → **Good** |
| `nesry` | 0.56 | fail → **Hard** |
| nothing | 0 | fail → **Hard** (an empty answer counts as 0) |

Press any answer button each time; the review is recorded with the staged grade.

The ratio is not "letters right out of letters in the word". When you are wrong, Anki shows your
line and, under an arrow, the correct one, and every marked character on both lines counts — the
matching letters twice, each gap once. The numbers above are Anki 25.09's own comparison, counted
the way Omnia counts it.

### 2. Three bands

Set the high mark to `0.95` — drag the right handle, or type `0.95` in its box. Now `necessary`
typed exactly is **Easy** (1.00), `necesary` is **Good** (0.89), and `nesry` is still **Hard**
(0.56).

### 3. The off state stays off

With the high mark off there is one visible handle, and it is the pass mark:

- Drag it: the pass mark moves; the high box still says *off*.
- Moving only the pass mark never switches the high band on — in either direction, and back.
- Press the bare track to the **right** of the handle, or type a value into the high box: that is
  what opens the band. Clearing the high box turns it off again.

### 4. An exact value survives

Type `0.72` in the pass-mark box, save, and reopen the panel: it still reads `0.72` — neither the
track's 0.05 steps nor merely opening the panel may round it.

### 5. The Statistics panel

With **Show stats** on, open Anki's *Statistics*: a Typed Accuracy card shows a donut and a
Good / Bad / Miss / Empty breakdown of the answers you just logged. With it off, grading still
runs; only the panel is hidden.

## Automated tests

```bash
pytest tests/plugins/typed_accuracy -q                                   # the bands, the store
pytest tests/gui/test_settings_html.py -q -k "Handle or Typed or Stored or OffStays"   # the track
```

The track's tests drive the shipped `settings.js` in node through its own event listeners, so
they need `node` on the PATH; without it they skip.

## When it looks wrong

| You see | Likely cause |
|---|---|
| Nothing is staged | The card has no type-in field (`{{type:…}}`), or the band's ease is `no` |
| Always **Hard** | The answer was empty, or the pass mark is higher than you think — check the box value |
| Never **Easy** | The high mark is off, or set above what you can reach (1.00 needs a perfect answer) |
| The interval label shows the wrong grade | Display Interval is off, or an older Omnia that drew it before the grade was staged |
