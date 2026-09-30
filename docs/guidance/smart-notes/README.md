# Smart Notes

Smart Notes fills a note's fields (text, images and audio) from one base field, usually the
word. You describe once, per note type, how each field is made; Omnia then fills new notes as
you add them, or a whole deck at once.

## Where things are

| To… | Go to |
|---|---|
| Set it up | *Tools → Omnia* → **Smart Notes** → *Configure…* (the "Smart Notes ✨" window) |
| Choose providers, enter keys, see usage, try a prompt | That window → **⚙ Options** → **Usage & Keys** |
| Change how generation behaves | That window → **⚙ Options** → **General** |
| Fill one note | In the Add or Browse editor: the **✨** button, or `Ctrl+Shift+G` |
| Fill one field | Right-click inside the field → **✨ Omnia · Generate this field** |
| Fill many notes | Browser → select notes → right-click → **✨ Omnia · Generate Smart Fields**. It is also on a deck or note type in the Browser's sidebar |
| See the order fields are made in | The window's **Dependencies** view → **▶ Preview gen order** |

The window shows one note type at a time: its **Note type**, **Base field** and **Decks** at the
top, then the **Fields** table (or the **Dependencies** view), and along the bottom
**+ Create field**, **✨ Auto-prompt**, **✦ Improve all prompts**, **⚙ Options**, **⤓ Export**
and **⤒ Import**. Press **Save** to keep your changes.

## Set it up

1. **A provider.** In **⚙ Options → Usage & Keys**, pick a default for each kind on the
   **Text**, **Image** and **Sound** tabs, enter keys under **🔑 Keys**, and check each with
   **Run test**:
   - *Text and images:* a hosted provider with its API key, or your own server (see
     [Self-hosted models](../local-server/README.md)).
   - *Audio:* `google_translate` and `edge_tts` need no key. `google_cloud` (the Neural2 and
     WaveNet voices) needs a Google Cloud project with **billing enabled**.
2. **A note type.** Pick the note type and its base field, for example `Word`. **Decks** limits
   generation to some decks; with none ticked, every deck counts.
3. **A row per field.** In the Fields table, give each field you want filled a **Type** (text,
   image or sound) and a prompt, and tick **Generate**. **✨ Auto-prompt** drafts the prompts for
   you. For example:

   | Field | Type | Prompt | Provider |
   |---|---|---|---|
   | Definition | text | `Write a one-sentence definition of {{Word}}.` | your provider |
   | Example | text | `One natural example sentence using {{Word}}.` | your provider |
   | Picture | image | `A simple flat illustration of {{Word}}.` | your provider |
   | Example audio | sound | reads the Example field | `edge_tts` |

   **Tools** can fill a field without an AI: a cloze made from another field, or a tool of your
   own from **⚙ Options → Tools**. The default is the AI alone.

   `{{Word}}` is replaced by that field of the note. A row without a prompt sends the base field
   itself. **Provider** and **Model** left on *(inherit)* use the defaults from **Usage & Keys**.
   **Preview** tries a row's prompt on a random note.

## Use it

- **One note:** add a note with only `Word` filled and press **✨**. Text fields fill, the
  picture arrives as an image (its file name starts with `omnia-`), and the audio field gets the
  example read aloud. **Usage & Keys → Usage** counts each call against the provider that made it.
- **Many notes:** select them in the Browser and run **✨ Omnia · Generate Smart Fields**. When
  it finishes, a summary counts the fields that were generated, skipped, blocked and failed.
- **One field:** right-click it → **✨ Omnia · Generate this field**.

### The options

**⚙ Options → General** applies to every note type. Hover an option for its full explanation.

| Option | On |
|---|---|
| Pre-generate at review | When a card is shown, its empty smart fields are filled in the background |
| Generate in the background | A batch runs without Anki's progress window, so you can keep studying. Its progress shows on the Smart Notes tile in Omnia's settings, where you can also stop it |
| Regenerate when batching | A batch also regenerates fields that already have content |
| When overwriting, replace | Whose work a regeneration may replace (below) |
| Regenerate from clippers | The Web and Desktop Clippers may regenerate a note's fields |
| Allow empty sources | A field is generated even when every field its prompt uses is empty |

### When a field waits

A field waits only for the fields its tool actually reads. A prompt that uses `{{Definition}}`
waits until Definition has content; a row without a prompt waits for the base field. A waiting
field is reported as **blocked**, the message names the missing field, and no request is sent
for it, so it costs nothing.

In the **Dependencies** view you can draw arrows between fields: drag from one field's border
onto another. An arrow sets the **order** fields are made in; it makes a field wait only if
that field's tool reads the other one. **▶ Preview gen order** animates the order and marks a
field blocked only when an input it reads is missing.

### What a regeneration may replace

A field that already has content is left alone unless you ask for it to be regenerated: tick
**Overwrite** in its row (for that field, every time), or turn on **Regenerate when batching**
(for every field, during a batch). **When overwriting, replace** then decides whose work may be
thrown away:

- *Anything*: every filled field is regenerated.
- *Only what Omnia generated (and you have not edited)*: keeps everything you typed or edited
  yourself.
- *Only what Omnia did NOT generate*: keeps the audio and images Omnia already made, so you do
  not pay for them twice.

Omnia recognises its own work because it marks every text it writes with an invisible
fingerprint and names its media files `omnia-…`. A field that Omnia wrote and you then edited
counts as yours. Fields filled before this option existed carry no mark, so they count as not
Omnia's.

When an image or audio field is regenerated, the old file goes to Anki's media trash only if
Omnia made it. An image you pasted, or another add-on's audio, is never removed.

### The pace of generated audio

In a sound row's **Voice** cell, the speed picker slows the voice down (0.7× to 0.9×) or speeds
it up (1.1× to 1.5×). **Speed: inherit** uses the add-on's default rate.

## When it looks wrong

| You see | Likely cause |
|---|---|
| Nothing happens on ✨ | The note type is not set up in Smart Notes, or no row has **Generate** ticked |
| Every field of a batch failed | The provider: check it with **Run test** under **Usage & Keys** |
| A field is blocked | The message names the missing field its tool reads. Fill that field, or change the tool |
| Audio fails with `403 … billing` | The `google_cloud` voices need billing; enable it, or switch the row to `edge_tts` |
| An image field fails but text works | The provider has no image model. Set **Image model** on its Keys card |
| A regeneration kept an old value | Neither **Overwrite** nor **Regenerate when batching** is on, or **When overwriting, replace** protects that field |
| A message is too short to tell what went wrong | Anki → *Tools → Add-ons* → Omnia → **View Files** → `user_files/omnia.log` has the full story |
