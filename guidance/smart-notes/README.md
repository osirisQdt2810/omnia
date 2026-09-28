# Smart Notes — testing field generation by hand

Smart Notes fills a note's fields — text, images and audio — from a base field (usually the
word), using providers and per-field tool chains you configure per note type. This guide walks
the features that are easiest to get subtly wrong: which provider runs, when a field waits, and
what a regeneration is allowed to replace.

## Where things are

| To… | Go to |
|---|---|
| Configure | *Tools → Omnia* → **Smart Notes** tile → *Configure…* (the "Smart Notes ✨" window) |
| Generate one note | In the editor (Add or Browse): the **✨** button, or `Ctrl+Shift+G` |
| Generate one field | Right-click inside the field → **✨ Omnia · Generate this field** |
| Generate many notes | Browser → select notes → right-click → **✨ Omnia · Generate Smart Fields** (also on a deck or note type in the Browser sidebar) |
| See the order fields generate in | Smart Notes window → the graph view → **▶ Preview gen order** |
| Providers, keys, usage, a test box | Smart Notes window → **Usage & Keys** |
| Something failed and the UI says why only briefly | `addons21/<add-on folder>/user_files/omnia.log` — the folder is `omnia` for a development install, `726991726` when installed from AnkiWeb |

The window's tabs: **General** (the note type, its base field, the Fields table, and the options
below it), **Usage & Keys**, **Tools**, **Integrations**, **Advanced**.

## Setup

1. A throwaway profile (*File → Switch Profile → Add*).
2. A note type to generate into — for example `Vocab` with `Word`, `Definition`, `Example`,
   `Example audio` and `Picture`. A copy of a real vocabulary note type works just as well.
3. A provider that works right now:
   - **Text and images:** your own endpoint (see [`../local-server/`](../local-server/README.md))
     or any hosted one with a valid key. Check it first with **Usage & Keys → Text / Image →
     Run test**; there is no point testing Smart Notes on a provider that fails there.
   - **Audio:** `google_translate` and `edge_tts` need no key. `google_cloud` (the Neural2 /
     WaveNet voices) needs a Google Cloud project with **billing enabled**; without it every
     synthesis fails with `403 … requires billing to be enabled`.
4. In the Smart Notes window → **General**: pick the note type, set the base field to `Word`, and
   configure a few rows, for example:

   | Field | Kind | Prompt | Provider |
   |---|---|---|---|
   | Definition | text | `Write a one-sentence definition of {{Word}}.` | your endpoint |
   | Example | text | `One natural example sentence using {{Word}}.` | your endpoint |
   | Picture | image | `A simple flat illustration of {{Word}}.` | your endpoint |
   | Example audio | sound | *(reads the Example field)* | `edge_tts` |

## Recipes

Each recipe says what to do and what you should see. Start every one from notes whose generated
fields are empty unless it says otherwise.

### 1. One note, every kind

Add a note with `Word = ephemeral`, press **✨**. Expected: Definition and Example fill with text,
Picture gets an image (its media file is named `omnia-…`), Example audio gets a `[sound:…]` of the
example sentence. **Usage & Keys → Usage** counts the calls against the provider that made them.

### 2. A batch, and its summary

Select about ten notes in the Browser — include one with `Word` **empty** — and run **✨ Omnia ·
Generate Smart Fields**. Expected: a progress bar, then a summary that counts generated, skipped,
blocked and failed fields. The note with an empty `Word` is listed as blocked *because it needs
`Word`* — and no request is sent for it, so the Usage count does not move for that note.

### 3. A field waits only for what its tool reads

This is the case that once reported thousands of notes as "also waiting on" a field their tool
never opens.

1. Give one row a tool chain that reads a field other than the base — e.g. a **Cloze** tool whose
   source is `Example`.
2. In the graph view, also draw a **hard** dependency from an unrelated field (say `Definition`)
   onto that row.
3. Pick a note where the tool's source (`Example`) is filled and `Definition` is empty, and
   generate.

Expected: the row generates; nothing says it waits on `Definition` — that edge only orders
generation now, it does not gate it. **▶ Preview gen order** agrees: the node is not marked
blocked. Now switch the row's chain to **AI** with a prompt that uses `{{Definition}}` and
generate again: it now waits on `Definition`, because now the tool reads it. The blocked message
always names the field the tool actually reads.

### 4. A row with no prompt waits for the base field

Clear the prompt of a text row (a promptless row sends the base field itself as the prompt). On a
note with `Word` empty, generate. Expected: the row is blocked, needing `Word`, and the model is
not called. Fill `Word` and generate again: it generates normally.

### 5. What a regeneration may replace

Under the Fields table on **General**:

- **Regenerate when batching** — ON: a batch refreshes fields that already have content; OFF:
  it only fills blank ones.
- **When overwriting, replace** — whose work may be thrown away when it does:
  *Anything* / *Only what Omnia generated (and you have not edited)* / *Only what Omnia did NOT
  generate*.

Omnia marks every text value it writes with an invisible fingerprint and names its media
`omnia-…`, which is how it tells its own output from yours.

1. Generate a note (recipe 1). Then edit one generated field by hand — change a word of the
   Definition — and leave the others alone.
2. Turn **Regenerate when batching** ON and set **When overwriting, replace** to *Only what Omnia
   generated (and you have not edited)*. Batch that note.
   Expected: the fields you did not touch are regenerated; **your edited Definition is kept**.
3. Set it to *Anything* and batch again. Expected: everything is regenerated, your edit included.
4. Regenerating an image or audio field replaces its media. The file it replaced goes to Anki's
   media trash only if Omnia wrote it (`omnia-…`); an image you pasted or another add-on's audio is
   never removed — and an identical regeneration never trashes the file it just wrote.

### 6. The pace of generated audio

In the **Voice** cell of a sound row, set the speed picker to **0.8× slower**, regenerate that
field, and play it: it is audibly slower. **Speed: inherit** uses the central `[tts] speed`.

### 7. The gen-order preview

Graph view → **▶ Preview gen order**, leaving the optional seeds unticked. Fields light up in the
order they generate; a node is marked blocked only when an input its tool reads is missing, and
the message says which one.

## Automated tests

On your computer, in the repo's venv:

```bash
# No keys and no quota. The free voices (edge_tts, google_translate) still reach the network:
pytest tests/plugins/smart_notes -q -m "not llm and not tts and not integration and not live_endpoint"

# Live generation through whichever provider your live config points at:
pytest tests/plugins/smart_notes/test_smart_notes.py::TestSmartNotesLLMGenReal -q
pytest tests/plugins/smart_notes/test_smart_notes.py::TestSmartNotesTTSGenReal -q
```

The live tests read credentials only from the gitignored `user_files/config/providers.toml` (or
the directory named by `OMNIA_TEST_CONFIG`). A provider without credentials is skipped; one that
answers with a quota, billing or rate limit is recorded as `xfail`, not as a failure — so a run
full of `xfail` means your keys or billing, not the code.

## When it looks wrong

| You see | Likely cause |
|---|---|
| Nothing happens on ✨ | The note type is not configured in Smart Notes, or no row has **Generate** ticked |
| Every field of a batch "failed" | The provider — check it with **Run test** under Usage & Keys first |
| A field is "blocked" | Read the message: it names the missing field the tool reads. Fill it, or change the tool |
| Audio fails with `403 … billing` | The `google_cloud` voices; enable billing, or switch the row to `edge_tts` |
| An image field fails but text works | The provider has no image model: set **Image model** on its Keys card |
| A regeneration kept an old value | **Regenerate when batching** is OFF, or **When overwriting, replace** protects it (recipe 5) |
