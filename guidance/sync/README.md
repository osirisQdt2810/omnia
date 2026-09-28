# Sync — pulling decks, note types and settings from another computer

Sync copies a chosen part of one computer's collection — decks with their cards and media, note
types, and Omnia's own feature settings — onto another, directly between the two machines. It is
not AnkiWeb and goes through no server: the computer that **shares** (the source) answers
read-only; the computer that **pulls** (the target) is the only one that changes.

## Where

*Tools → Omnia* → the **Sync** button on the header row (not a plugin tile). The window has two
halves:

- **This computer** — the sharing switch, this machine's **ID** (eleven digits) and **Access code**
  (nine digits), each with **Copy**, and **New code**.
- **The other computer** — "Type the two numbers it shows." → **Check**. When it connects it says
  *Connected to <machine>.* and the picker window opens.

## Before you start

- Two computers with Omnia installed, **each on a throwaway profile** — a pull imports into the
  target's collection.
- The target must be able to reach the source directly: the same network, or a mesh VPN between
  them (addresses in `100.64.0.0/10`, as Tailscale uses, are recognised). The ID encodes the
  source's address, which is why it is long.
- Nothing listens until sharing is switched on, and no profile has an access code until it opens
  the Sync window.

## Recipes

### 1. One computer, talking to itself

The quickest check that pairing and the picker work. Sync → switch sharing **on** → Copy the ID
and the code → paste both into **The other computer** → **Check**. Expected: *Connected to …*
and the picker opens on this collection's own deck tree, note types and features. Close it
without copying — pulling a collection into itself teaches nothing.

### 2. A real pull between two computers

1. On the source: sharing on; read out its ID and code.
2. On the target: type them in → **Check** → the picker opens.
3. Choose a small deck that has audio or images, and one feature's settings (e.g. Smart Notes).
   Before anything moves, the picker says what the copy would land on — for example that a deck
   of the same name already exists and will receive the cards, losing nothing.
4. **Copy.** You may close the window and keep studying: the **Sync** button fills up as the copy
   progresses, and hovering it shows how far along it is. The bar sweeps with *"Packing it up on
   the other computer…"* until the source reports a size, then shows a percentage.

Expected on the target when it finishes:
- A sentence counting what arrived — decks, notes, media, settings.
- The deck is there, with its cards; its audio plays and its images show.
- **A backup was taken first**, into Anki's own backup folder, so it is in Anki's own list of
  backups (*Open Backup…* in the profile manager) — undo a bad pull from there.
- The chosen settings are applied. Notes and note types import **only if newer**: two machines
  that also sync through AnkiWeb share note ids, so most of what arrives already exists and is
  left alone rather than duplicated.

### 3. A note type on its own

In the picker, click a note type that no chosen deck needs, then copy. Expected: the definition
arrives with no notes — the way to set up a second machine to author the same kind of card before
there is anything to put in it. A chip has three states: needed by a chosen deck (click to leave
it behind), left behind (click to take it back), wanted by nothing (click to bring just the
definition).

### 4. The guessing lockout

On the target, type the right ID with a wrong code five times. Expected: the source stops
answering for a minute, and the target says so. The nine-digit code is only safe because of this
lockout — the two are one design.

### 5. A new code retires the old one

On the source, press **New code**. The target's next **Check** with the old code fails; the new
one works.

### 6. Sharing off means gone

Switch sharing off on the source. The target's **Check** now says *Nothing is sharing on that
machine right now — open Omnia there and turn sharing on.* Any package that was packed and never
collected is deleted when sharing stops.

## Automated tests

```bash
pytest tests/core/test_sync_*.py tests/gui/test_sync_*.py -q
```

These cover pairing and its check digit, reachability, the inventory, selection and the deck
tree, the package, clash detection, the session and lockout, progress arithmetic, and the
target's job. None of them opens a socket to another machine — recipe 2 is the only test of the
real transfer.

## When it looks wrong

| You see | Likely cause |
|---|---|
| *Nothing is sharing on that machine right now…* | Sharing is off on the source, or its profile is closed |
| *The other machine did not answer in time. It may be asleep…* | The source is asleep, off the network, or unreachable from the target (firewall, different network without a VPN) |
| *Something answered at that ID, but it was not Omnia…* | A mistyped ID that happens to point at something else |
| A lockout message | Five wrong codes; wait a minute and type it carefully |
| *The copy stopped part way through. Nothing was added to this collection — try again.* | The connection dropped mid-transfer; the target added nothing, so retrying is safe |
| A big deck takes minutes before the percentage appears | Packing is done up front on the source, media and all; it has a 15-minute limit of its own |
