# Typing Accuracy

On a card where you type the answer, Anki marks each character you typed as right, wrong or
missing. Typing Accuracy turns that into a score, the correct characters over all the characters
Anki marks, and picks the review grade from it. The grade is chosen as the answer side
appears, and whichever answer button you then press (Enter and Space included) records that
grade. Every result is also logged for a panel on Anki's Statistics screen.

The score falls into up to three bands:

| Score | Grade | Default |
|---|---|---|
| below the pass mark | the fail grade | Hard |
| from the pass mark up to the high mark | the pass grade | Good |
| at or above the high mark | the high grade | Easy |

With the high mark off there are two bands, pass and fail. Setting a band's grade to `no`
leaves that band alone, so the button you press counts as usual.

## Where

*Tools → Omnia* → the **Typing Accuracy** tile → switch it on → *Configure…*. Both marks sit on
one track: the left handle is the pass mark and the right one the high mark. The box beside
each name takes an exact value (`0.72` is fine, while the handles move in steps of `0.05`).

If **Display Interval** is on, its "interval: …" label shows the interval of the grade that
will actually be recorded.

## Set it up

Typing Accuracy works on cards whose template asks you to type the answer, such as Anki's
built-in **Basic (type in the answer)** note type.

- **Two bands** (pass or fail): set the pass mark, for example `0.70`, and leave the high mark
  off.
- **Three bands** (fail, pass, or high): also set the high mark, for example `0.95`, by
  dragging the right handle or typing into its box.

With the high mark off, the track shows a single handle, and that handle is the pass mark.
Moving it never switches the high band on. To switch it on, press the bare track to the right
of the handle, or type a value into the high box; clearing that box switches it off again.
Exact values you type are kept as typed.

## How the score works

With a pass mark of `0.70` and a high mark of `0.95`, on the word `necessary`:

| You type | Score | Grade |
|---|---|---|
| `necessary` | 1.00 | Easy |
| `necesary` | 0.89 | Good |
| `nesesary` | 0.74 | Good |
| `nesry` | 0.56 | Hard |
| nothing | 0 | Hard |

The score is not "letters right out of letters in the word". When you are wrong, Anki shows
your line and, under an arrow, the correct one, and every marked character on both lines counts:
the matching letters twice, each gap once. The scores above are what Anki's own comparison
gives.

## The Statistics panel

With **Show stats** on, Anki's *Statistics* screen gains a Typing Accuracy card: a donut and a
Good / Bad / Miss / Empty breakdown of your typed answers. With it off, grading still works;
only the panel is hidden.

## When it looks wrong

| You see | Likely cause |
|---|---|
| No grade is chosen | The card has no type-in field, or that band's grade is set to `no` |
| Always **Hard** | The answer was empty, or the pass mark is higher than you think: check the value in its box |
| Never **Easy** | The high mark is off, or set above what you reach (1.00 needs a perfect answer) |
| The interval label shows the wrong grade | An older Omnia drew the label before choosing the grade: update Omnia |
