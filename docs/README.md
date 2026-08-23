# Omnia

**Seven Anki features in one add-on.** Each is a plugin with its own on/off switch: enable what
you want, and the rest stays inert — no extra menus, no clutter.

> Anki 25.09+ / 26.x · Windows, macOS and Linux · MIT licensed ·
> [source](https://github.com/osirisQdt2810/omnia) ·
> [report a bug](https://github.com/osirisQdt2810/omnia/issues)
>
> This page lives in the repository at `docs/`, so it is versioned with the code it documents
> and needs no separate site to keep in sync.

---

## Install

1. Download `omnia.ankiaddon` from the [latest release](https://github.com/osirisQdt2810/omnia/releases),
   or install from AnkiWeb.
2. In Anki: **Tools → Add-ons → Install from file…** and pick the file.
3. **Restart Anki.** You now have a **Tools → Omnia** menu.

That is the whole install. Nothing is pip-installed into your Anki and nothing is downloaded on
install — dependencies ship inside the add-on. Everything else is done in the GUI.

The package is deliberately small (~1 MB). The offline **piper** voice models (~60 MB each) are
not bundled, so you do not re-download them on every update; the first time you actually
synthesize with a piper voice, Omnia fetches it once per machine and reuses it forever.

### Where your settings live

Plugin settings live **in your collection**, so they sync between your devices through AnkiWeb
automatically. API keys are the one exception: they stay in a local file and are deliberately
**never** synced. Installing on a second machine means repeating the install and re-entering
your keys — nothing else.

---

## Smart Notes

Point it at a note type, say which field feeds which, and it fills the rest: definitions,
example sentences, synonyms, translations, IPA, images, and spoken audio.

### The model is the last resort, not the first

This is what separates Omnia from a plain "call ChatGPT" add-on. Every generated field runs an
**ordered chain of tools**, and the LLM is reached only when the free ones decline:

| Tool | Cost | What it does |
|---|---|---|
| `cloze` | free | Wraps the target word in its example sentence as `{{c1::…}}`, matching inflections both ways (`run` ⇄ `ran`, `survive` ⇄ `survived`). |
| `cloze_audio` | TTS only | Speaks the sentence with the answer replaced by silence or a beep — a listening cloze. It never speaks the answer: if it cannot mask it, it fails rather than giving the answer away. |
| `ai` | tokens | The LLM path. |
| `user:<yours>` | free | A tool you describe once in plain words. The generated Python is shown to you, tested, and saved as a file that then runs offline forever. |

A field configured `cloze → ai` costs **nothing** when the word really is in the sentence, and
only reaches the provider when it is not.

### Setting up a provider

**Tools → Omnia** → the **Smart Notes** plugin's **Configure** → the **Usage & Keys** tab. Pick
your LLM and TTS provider, choose a model, paste your API key. The dialog writes the config for
you; you never create or edit a file by hand.

| Kind | Supported |
|---|---|
| LLM | Gemini (AI Studio **and** Vertex AI), OpenAI, OpenRouter, any OpenAI-compatible endpoint |
| TTS | Google, Microsoft Edge, OpenAI, offline Piper |

Your keys stay on your machine and are sent only to the provider you chose.

### Trying it on Google's free credit

Google Cloud gives new customers **$300 in credit, valid for 90 days**, and Gemini on Vertex AI
is covered by it — enough to fill a substantial deck before you have paid anything.

Two caveats, so nothing is a surprise:

- **A credit card is required at signup.** The trial will not start without one.
- The credit does **not** cover third-party *partner* models offered through Vertex (Claude,
  Llama). It covers Google's own Gemini models — which is what Omnia talks to by default.

When the 90 days end, the trial account closes unless you upgrade; you are only billed for usage
beyond the credit.

### Batch generation

Generating a whole deck runs several notes concurrently, and groups the same field across notes
into a single request. Measured against a real provider on 100 notes of a real note type:

| | requests | change |
|---|--:|--:|
| one note at a time | 1300 | — |
| grouped, 10 notes per call | 769 and 820 across two runs | **−39%** |

Fewer requests is what keeps a large batch under your provider's rate limit. Whether it also
finishes *sooner* is not settled — two live sessions disagreed — so expect fewer requests and do
not count on fewer seconds either way.

---

## The other six

### Typing Accuracy
Grades a typed card Again/Hard/Good/Easy from *how accurately* you typed it, instead of demanding
a perfect match. A single wrong letter no longer costs you the card. Adds an interactive accuracy
panel to Anki's Statistics screen.

### Auto Flip
Auto-advances question → answer → grade after a delay you set, waiting for the card's audio to
finish first. Hands-free review, useful for listening decks.

### Display Interval
Shows the predicted next interval on the answer side, and exposes it to your card templates so
you can style it yourself.

### Overdue Guard
Forces very overdue cards to Hard/Again whatever you press, so a six-month gap cannot be marked
Easy and pushed another year away.

### Note Maintenance
Batch clean-up and reformatting of notes you already have — deterministic, **no AI, no tokens**.
Configured per note type, several note types in one run. Preview the diff first, then apply with
full undo.

### Word Lookup
Searches a word across your whole collection with word-form matching, from the reviewer or from
a companion clipper.

---

## Companion clippers

Two optional tools push a word plus its surrounding context into your running Anki, where Smart
Notes generates the rest of the card:

| Tool | Capture from |
|---|---|
| [Web Clipper](https://github.com/osirisQdt2810/omnia-web-clipper) | any web page (Chrome/Chromium) |
| [Desktop Clipper](https://github.com/osirisQdt2810/omnia-desktop-clipper) | any desktop app, plus screen OCR (macOS/Windows/Linux) |

Enable each in **Tools → Omnia** → **Smart Notes → Configure → Integrations**.

---

## Questions and bugs

Open an issue at [github.com/osirisQdt2810/omnia/issues](https://github.com/osirisQdt2810/omnia/issues).
