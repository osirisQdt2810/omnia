# Sync

Sync copies the parts of one computer's collection you choose (decks with their cards and
media, note types, and Omnia's own feature settings) onto another computer, directly between
the two. It is not AnkiWeb and goes through no server. The computer that **shares** only
answers; the computer that **pulls** is the only one that changes.

## Where

*Tools → Omnia* → the **Sync** button in the header row. The window has two halves:

- **This computer**: the sharing switch, this computer's **ID** (eleven digits) and **Access
  code** (nine digits), each with **Copy**, and **New code**.
- **The other computer**: type the two numbers it shows, then press **Check**.

## Before you start

- Omnia on both computers. A pull imports into the collection of the computer that pulls, so
  try it on a spare profile first.
- The two computers must reach each other directly: the same network, or a mesh VPN between
  them such as Tailscale. The ID carries the sharing computer's address, which is why it is long.
- Nothing listens until sharing is switched on.

## Copy from another computer

1. **On the computer that shares:** open Sync, switch sharing **on**, and read out its ID and
   access code.
2. **On the computer that pulls:** type both into **The other computer** and press **Check**.
   It says *Connected to …* and the picker opens.
3. **Choose** what to copy: decks, note types, and the settings of any Omnia feature. Before
   anything moves, the picker says where each choice will land, for example that a deck of the
   same name already exists and will receive the cards.
4. **Copy.** You can close the window and keep studying. The **Sync** button fills up as the
   copy runs, and hovering it shows how far along it is. It first shows *Packing it up on the
   other computer…* while the sharing computer prepares everything, then a percentage.

When it finishes, a sentence counts what arrived: decks, notes, media and settings. Then:

- The decks are there with their cards, audio and images.
- **A backup was taken first**, into Anki's own backup folder. To undo a pull, restore that
  backup with *Open Backup…* in Anki's profile manager.
- The chosen settings are applied. Notes and note types are imported **only if newer**, so two
  computers that also sync through AnkiWeb do not end up with duplicates.

### A note type on its own

In the picker, click a note type that no chosen deck needs: it arrives without notes, ready for
you to add cards of that kind on the second computer. A note type's chip has three states:
needed by a chosen deck (click to leave it behind), left behind (click to take it back), and
wanted by nothing (click to bring just the note type).

## Keeping it private

- **Five wrong codes** in a row make the sharing computer stop answering for a minute. That
  lockout is what keeps a nine-digit code safe.
- **New code** retires the old one at once: anyone who had it can no longer connect.
- **Sharing off** closes the door. A package that was packed and never collected is deleted.

## When it looks wrong

| You see | Likely cause |
|---|---|
| *Nothing is sharing on that machine right now…* | Sharing is off on the other computer, or its profile is closed |
| *The other machine did not answer in time. It may be asleep…* | The other computer is asleep, off the network, or out of reach (a firewall, or a different network without a VPN) |
| *Something answered at that ID, but it was not Omnia…* | A mistyped ID that happens to point at something else |
| A lockout message | Five wrong codes; wait a minute and type the code carefully |
| *The copy stopped part way through. Nothing was added to this collection — try again.* | The connection dropped. Nothing was added, so trying again is safe |
| A big deck takes minutes before the percentage appears | The sharing computer packs everything, media included, before sending; it allows itself 15 minutes |
