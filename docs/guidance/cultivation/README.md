# Cultivation

Cultivation shows two learning levels that move every day: your **English** level from your
Anki vocabulary, and your **DSA** level from your `dsa-journey` repository. Each level is a number
that rises with real practice and falls when you stop. It is also named twice, once on a scale you
already know and once as a cultivation realm.

- **English** reads your collection. Cultivation only reads it; it never changes a card, a note
  or a deck.
- **DSA** reads a small file that `dsa-journey` writes every time you run its `/commit-progress`
  command.
- Only what happens **from your start day on** counts. On the first day both levels start from
  scratch, and **Start over** sets the start day to today again.

## The levels

| Realm | English (words) | CEFR | DSA (rating) | Tier |
|---|---|---|---|---|
| Luyện Khí | below 1,500 | A1 | below 1,200 | Newbie |
| Trúc Cơ | 1,500 | A2 | 1,200 | Pupil |
| Kim Đan | 2,500 | B1 | 1,400 | Specialist |
| Nguyên Anh | 3,250 | B2 | 1,600 | Expert |
| Hóa Thần | 3,750 | C1 | 1,900 | Candidate Master |
| Luyện Hư | 4,500 | C2 | 2,100 | Master |
| Hợp Thể | 8,000 | C2+ | 2,400 | Grandmaster |
| Đại Thừa | 14,000 | C2+ | 2,600 | International Grandmaster |
| Chân Tiên | 17,000 | C2+ | 3,000 | Legendary Grandmaster |

There is no exam and no waiting period. The level is whatever your number reaches today, so it
can go down as well as up.

### English: words you can actually recall

For every card, Cultivation asks how likely you are to recall it right now, using the same memory
model as Anki's FSRS scheduler. That likelihood is weighted by how strong the card's evidence is:

| Card type | Weight |
|---|---|
| Typing the word | 1.0 |
| Recalling the English word | 0.9 |
| Recalling the meaning of a word you see | 0.8 |
| Recognising a word among options | 0.6 |

A word's strength is its best card, and your number is the sum over your words. The unit is
"words you know", which is why the levels sit at published vocabulary-size bands: the CEFR bands
from Milton's vocabulary-size research, then 8,000, 14,000 and 17,000 words (an educated native
speaker). Vocabulary is only one part of a CEFR level. Grammar, listening and speaking are not
measured here.

A word counts once you **review it on or after the start day**. Words you knew before the start
come back into the count as soon as you review them again. Forgetting lowers the number on its
own: a word you stop reviewing slowly fades out of it.

**Today** is what today's reviews added, measured against where your cards stood at the start of
the day, so overnight forgetting does not count against it. **Lifetime** adds up every gain and
never goes down.

### DSA: a rating, like on LeetCode

Each solution is a game against the problem. The problem's difficulty comes from published
LeetCode contest ratings; problems without one count as 1200 (Easy), 1600 (Medium) or 2000
(Hard). Solving a hard problem alone raises your rating a lot, and needing help on an easy one
lowers it a little. You start at 1500, LeetCode's own starting rating. After a week with no
attempts the rating slowly drifts down, but it never loses more than half of what you ever gained.

Copying a solution opens a **debt**. It closes when you solve the same problem again yourself, at
least a week later.

## Set it up

1. *Tools → Omnia* → the **Progress** tile → switch **Cultivation** on. That day becomes your
   start day.
2. Open *Tools → Cultivation*, then the **Setup** tab.
3. **Vocabulary decks**: tick the decks that hold your English words. With nothing ticked,
   every deck counts.
4. **Word field**: for each note type, pick the field that holds the English word. Notes with
   the same word are merged into one word.
5. **Card templates**: confirm what each card type asks of you. ★ marks the suggestion, and
   templates with a typing box are labelled *typed*.
6. Press **Save and recompute**.

If the English card warns that some cards have no FSRS memory state, turn FSRS on in *Deck
options → FSRS* and press *Optimize*. Without it, Cultivation can only estimate memory from the
card's interval, which is much rougher.

### DSA

Nothing to do in Anki. In `dsa-journey`, run `/commit-progress` as usual. When it commits a new
solution, it asks three questions about it:

| Question | Answers |
|---|---|
| status | `self` (alone), `hint`, `editorial` (read it, then wrote it yourself), `copied` |
| time | minutes until accepted, or until you gave up |
| wa | wrong submissions before it was accepted |

These answers are what make the rating honest. A solution without them still counts, but as a
weaker signal. When the command finishes it updates the DSA file, and an open Cultivation window
picks up the change within a few seconds.

## Where things are

- *Tools → Omnia* → **Progress** → **Cultivation**: the switch, and *Configure…* for the start
  day, the DSA file location and whether suspended cards count.
- *Tools → Cultivation*: the window itself.
  - **Overview**: both levels, today's change, streak, lifetime and the history chart.
  - **DSA**: open debts and your latest solutions, each with the problem's rating and how much it
    moved yours.
  - **Setup**: the start day with **Start over**, decks, word fields and card types.

## Tune it

The card weights, the solution-status scores, the time limits for a "fast" solve, the rating drift
and the level thresholds are all in the feature's section of the add-on's settings file. The
weights and the drift are judgement calls marked [HEURISTIC] there. If you have a LeetCode contest
rating, set **initial rating** to it so your DSA rating starts in the right place. To count older
history too, set **Counting since** in *Configure…* to an earlier day.

The DSA file lives in a `.omnia/cultivation` folder in your home folder. If you move it, set
**DSA feed file** to the new place, and point dsa-journey at the same file.

## When it looks wrong

| You see | What to do |
|---|---|
| English is 0 right after switching on | Expected: nothing is counted before your first review on the start day. Review some cards. |
| DSA says **No feed yet** | Run `/commit-progress` in dsa-journey once. Check that **DSA feed file** points where the repository writes. |
| DSA rating barely moves | Your solutions have no `status`, so each counts at half weight. Answer the three questions at commit time. |
| English is much higher than you expected | Check the decks in **Setup**. With no deck ticked, every deck counts, Japanese or medical cards included. |
| A word is counted twice | The two notes spell it differently (case and spaces are already ignored). Pick the right word field in **Setup**. |
| Today shows a dash | This is the first day recorded since the start. It fills in tomorrow. |
