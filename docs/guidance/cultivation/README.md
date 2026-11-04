# Cultivation

Cultivation gives two long-running learning streams a daily level in the style of a
cultivation novel. You build up power, pass a trial to break through to the next realm, and if
you neglect your study the power fades and the realm can drop.

- **Anki vocabulary** (the main one) is read from your collection. Cultivation only reads it;
  it never changes a card, a note or a deck.
- **DSA** is read from your `dsa-journey` repository, through a small file the repository writes
  every time you run its `/commit-progress` command.

## What the numbers mean

| On the card | Name | What it is |
|---|---|---|
| Tu vi | power | Your current ability. Real practice raises it and neglect lowers it. |
| Cảnh giới, L1–L9 | realm | Your level. It only changes by the rules below. |
| Thiên kiếp | trial | A test you must pass to enter a realm for the first time. |
| Lifetime | — | All the power you ever gained. It never goes down. |
| Streak | — | Days in a row with some study. One missed day per week is forgiven ("freeze"). |

The realms, from L1 to L9, are Luyện Khí, Trúc Cơ, Kim Đan, Nguyên Anh, Hóa Thần, Luyện Hư,
Hợp Thể, Đại Thừa and Chân Tiên.

### Anki power: words you can actually recall

For every card, Cultivation asks how likely you are to recall it right now, using the same
memory model Anki's FSRS scheduler uses. That likelihood is weighted by how strong the card's
evidence is. Typing the word counts most (1.0), recalling the English word comes next (0.9),
recalling the meaning of a word you see is lower (0.8), and recognising a word among options
counts least (0.6). A word's strength is its best card, and power is the sum over all your
words. The unit is "words you know", which is why the realms sit at published vocabulary-size
milestones:

| Realm | Power |
|---|---|
| Luyện Khí | 0 |
| Trúc Cơ | 1,000 |
| Kim Đan | 2,000 |
| Nguyên Anh | 3,000 |
| Hóa Thần | 5,000 |
| Luyện Hư | 8,000 |
| Hợp Thể | 10,000 |
| Đại Thừa | 14,000 |
| Chân Tiên | 17,000 |

Reviewing a word you already know well adds little today, but it keeps that word from fading.
Forgetting lowers power on its own, with no penalty rule needed. Words you knew before Anki are
not counted yet, and the card says so.

**Today** is the difference your reviews made, measured against where your cards stood at the
start of the day, so overnight forgetting does not count against it. The first day after you
switch Cultivation on shows no figure for today, because there is no earlier day to compare with.

### DSA power: a rating, like a chess rating

Each solution in `dsa-journey` is a game against the problem. The problem's difficulty comes
from published LeetCode contest ratings; problems without one count as 1200 (Easy), 1600
(Medium) or 2000 (Hard). Solving a hard problem alone raises your rating a lot, and needing help
on an easy one lowers it. After a week with no attempts the rating slowly drifts down, but it
never loses more than half of what you ever gained.

| Realm | Rating |
|---|---|
| Luyện Khí | below 1200 |
| Trúc Cơ | 1200 |
| Kim Đan | 1400 |
| Nguyên Anh | 1600 |
| Hóa Thần | 1900 |
| Luyện Hư | 2100 |
| Hợp Thể | 2400 |
| Đại Thừa | 2600 |
| Chân Tiên | 3000 |

### How a realm changes

- When your power reaches the next realm for the first time, the card shows **Thiên kiếp
  ready**. Passing the trial takes you in.
- A realm you held before needs no trial. Reaching its power again is enough.
- If power falls below your realm, the card turns **bất ổn** (unstable). The realm only drops
  after 14 days below it, so a bad week costs nothing.
- After a failed trial there is a 7-day cooldown, time to practise what you missed.
- On the very first day, Cultivation places you in the realm your power already holds and marks
  it **provisional**, since you have not passed a trial for it.

## Set it up

1. *Tools → Omnia* → the **Progress** tile → switch **Cultivation** on.
2. Open *Tools → Cultivation*, then the **Setup** tab.
3. **Vocabulary decks**: tick the decks that hold your English words. With nothing ticked,
   every deck counts.
4. **Word and meaning fields**: for each note type, pick the field holding the English word and
   the one holding its meaning. Notes with the same word are merged into one word.
5. **Card templates**: confirm what each card type asks of you. ★ marks the suggestion, and
   templates with a typing box are labelled *typed*.
6. Press **Save and recompute**.

If the Anki card warns that some cards have no FSRS memory state, turn FSRS on in *Deck
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
weaker signal. When the command finishes, it updates the DSA file, and an open Cultivation
window picks up the change within a few seconds.

## Trials

**Anki.** You get 50 words you reviewed before, never one from the last 24 hours. Half come from
words you started since your last breakthrough. If most of your words have typing cards, you see
the meaning and type the word, and a single typo is forgiven in words of 7 letters or more.
Otherwise you see the word, type its meaning, and judge yourself honestly. You pass with 43 of
50. You can close the trial and continue it later. If you fail, the window shows a search for the
missed words: paste it into *Browse*, or use it for a filtered deck to practise them.

**DSA.** Cultivation picks five rated problems you have never solved, close to the next realm's
rating and from at least two different contests. Solve them, in order, within 14 days. A problem
counts when its status is `self` and its time is within the limit: 20 minutes below 1400, 40
minutes up to 1899, and 60 minutes from 1900. Three of the five is a pass. The trial is checked
each time the DSA file updates. *Give up* ends it as a failure.

## Tune it

Every number above comes from the research the feature is built on. The ones marked
[HEURISTIC] in *Configure…* and in the feature's section of the add-on's settings file are
judgement calls you may calibrate: the card weights, the typo rule, the time limits, the
solution-status scores, the rating drift, the 14-day buffer and the 7-day cooldown. If you have
a LeetCode contest rating, set **initial rating** to it so your DSA rating starts in the right
place.

The DSA file lives in a `.omnia/cultivation` folder in your home folder. If you move it, set
**DSA feed file** to the new place, and point dsa-journey at the same file.

## When it looks wrong

| You see | What to do |
|---|---|
| DSA says **No feed yet** | Run `/commit-progress` in dsa-journey once. Check that **DSA feed file** points where the repository writes. |
| DSA rating barely moves | Your solutions have no `status`, so each counts at half weight. Answer the three questions at commit time. |
| Anki power is much higher than you expected | Check the decks in **Setup**. With no deck ticked, every deck counts, Japanese or medical cards included. |
| A word is counted twice | The two notes spell it differently (case and spaces are already ignored). Pick the right word field in **Setup**. |
| Today shows a dash | This is the first day Cultivation recorded. It fills in tomorrow. |
| The realm says provisional | It was set from your history, not by a trial. Pass the next trial to earn the next realm for real. |
