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
| **Smart Notes** | Fills note fields with an LLM or TTS — definitions, examples, cloze, images, audio — with a dependency graph so each field waits for what it reads. |
| **Word Lookup** | Answers "is this word already in my collection?" for the companion web and desktop clippers. |

## Smart Notes, briefly

A field is filled by an ordered **chain of tools**, and the model is the last resort rather than
the first. `cloze` hides a word in its own example sentence for free; `cloze_audio` speaks that
sentence with the answer replaced by silence or a beep, and never speaks the answer; `ai` runs
only when the free tools decline. You can also describe a tool in plain words and have one
written for you — you read the code and run it before it is ever saved.

Providers: OpenAI, Anthropic, Gemini (API and Vertex), DeepSeek, Groq, OpenRouter, Ollama for
text; Google, OpenAI, ElevenLabs and a bundled offline voice for speech.

## Requires

Anki 25.09 or newer. Nothing to install separately — third-party dependencies ship inside the
add-on, and the offline voice downloads itself on first use.

## Source, issues, and the full documentation

<https://github.com/osirisQdt2810/omnia>
