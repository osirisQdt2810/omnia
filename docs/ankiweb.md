# Omnia — All-in-One Toolkit

One Anki add-on, many independent feature plugins. Tick a plugin on in the settings dialog and
that feature turns on; leave it off and it costs you nothing.

Open it from **Tools → Omnia**.

## What's in it

| Plugin | What it does |
|---|---|
| **Auto Flip** | Advances question → answer → grade after a delay you choose, waiting for the card's audio to finish first. |
| **Typing Accuracy** | Grades a typed card Again/Hard/Good/Easy from how accurately you typed it, with a stats panel on the Statistics screen. |
| **Display Interval** | Shows the next interval on the answer side — and reflects the grade your typing is about to produce, not an ungraded Good. |
| **Overdue Guard** | Caps the grade on a card you left sitting for months, so one lucky answer does not send it away for a year. |
| **Audio Speed** | Separate playback speeds for the front and the back, with shortcuts to nudge either side. |
| **Note Maintenance** | Batch, deterministic clean-up of text your notes already contain — strip IPA, reformat synonyms, find-and-replace across fields — with a diff preview before anything is written. No AI, no network. |
| **Smart Notes** | Fills note fields with an LLM or TTS — definitions, examples, cloze, images, audio — with a dependency graph so each field waits for what it reads. |
| **Word Lookup** | Answers "is this word already in my collection?" for the companion web and desktop clippers. |
| **Phrase Check** | Corrects a phrase you select in a clipper: every mistake as its own card with its own reason, then the sentence rewritten with the changes marked. |

## Sync — two computers, a few decks, no export files

**New in 0.1.0.** If you study on a laptop and a desktop, you already know the problem: AnkiWeb
syncs everything or nothing, and a large collection is not something you want on both machines.

Omnia copies **the decks you choose** from one computer to the other, over your own network, with
their media, their note types and your Omnia settings. No export file, no USB stick, no cloud in
between.

It works the way you would expect a remote-desktop tool to. Each computer shows an **ID** and an
**access code** — two short numbers. Type the other machine's two numbers into yours and it lists
what that machine holds: its deck tree with card counts, its note types, its configured features.
Pick what you want and press Copy.

- **It runs in the background.** Close the window and carry on studying. The Sync button fills up
  as it goes, and hovering it tells you how far along and roughly how long is left.
- **Nothing is copied over quietly.** Before it starts, you are told which decks and note types
  already exist on this computer — and which note types have the same name but *different fields*,
  which is the one case where something you already have could change. You choose what happens to
  a note that exists on both machines: leave yours alone, or replace it.
- **A backup is taken first**, into Anki's own backup folder, so it appears in Anki's own restore
  list.
- **The other machine is read-only.** It can be asked what it has and asked for a copy; nothing
  you send it can change anything on it.
- **Nothing is shared until you switch it on.** A fresh profile shares nothing and is not even
  given an access code until you open the panel. Five wrong codes and a machine stops answering
  for a minute.
- **API keys never travel.**

## Smart Notes, briefly

A field is filled by an ordered **chain of tools**, and the model is the last resort rather than
the first. `cloze` hides a word in its own example sentence for free; `cloze_audio` speaks that
sentence with the answer replaced by silence or a beep, and never speaks the answer; `ai` runs
only when the free tools decline. You can also describe a tool in plain words and have one
written for you — you read the code and run it before it is ever saved.

Providers: OpenAI, Anthropic, Gemini (API and Vertex), DeepSeek, Groq, OpenRouter, Ollama for
text; Google, OpenAI, ElevenLabs and a bundled offline voice for speech.

## Phrase Check — what is wrong with this sentence, and why

Select a phrase anywhere you are reading or writing, press the wand in the clipper, and Omnia
answers with a **list** of small fixes rather than a corrected paragraph. Each one is its own
card — what you wrote, what to write instead, and an Explanation button with the reason — and
below them the whole phrase rewritten with the changed words marked.

That shape is deliberate. "Your sentence should be X" teaches nothing, and one paragraph
explaining six unrelated problems is read by nobody.

Two registers, because the same sentence is wrong in different ways depending on whether it is
being said or written: *"I ain't got none"* is a mistake in an essay and ordinary in
conversation. The panel asks, and you can switch without losing your place.

Answers are remembered, so coming back to a phrase costs nothing. Nothing is written to your
collection — it only reads what you selected.

## Companion clippers

Two optional browser/desktop helpers capture a word and its sentence from anything you are
reading and send it to Anki, where Smart Notes fills the rest of the card in. They ask Word
Lookup first, so you can see whether the word is already in your collection before you add it
again — and the same selection can go to Phrase Check instead, if what you want is not "do I
have this?" but "is this right?".

## Requires

Anki 25.09 or newer. Nothing to install separately — third-party dependencies ship inside the
add-on, and the offline voice downloads itself on first use.

## Source, issues, and the full documentation

<https://github.com/osirisQdt2810/omnia>
