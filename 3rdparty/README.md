# Omnia Web Clipper (Chrome extension)

A small Chrome extension that lets you highlight a **word or phrase** on any web page,
click a floating **"+"** button, and have that selection — together with the sentence and
paragraph around it — sent straight into your running Anki as a new note. The
[Omnia](../README.md) add-on's **Smart Notes** feature then fills in the rest of the card
(definition, example, audio, etc.) automatically.

This folder (`3rdparty/`) contains the **browser** side. It is intentionally **not** part of
the Python add-on — it talks to Anki over the network through the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on.

> New to Chrome extensions? You do not need to install anything from a store, write any
> code, or run a build step. You just point Chrome at the folder. Every step is below.

---

## 1. What it is and how the pieces fit together

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  Your web browser (Chrome / Edge / Brave - any Chromium browser)          │
 │                                                                           │
 │   1. You select "break the ice" on a web page                            │
 │      │                                                                    │
 │      ▼                                                                    │
 │   content.js  ──shows──►  floating "+" tooltip near the selection         │
 │      │                                                                    │
 │      │ 2. you click "+"                                                   │
 │      ▼                                                                    │
 │   capture = { selection, sentence, context, pageTitle, url }              │
 │      │                                                                    │
 │      │ 3. chrome.runtime.sendMessage                                      │
 │      ▼                                                                    │
 │   background.js (service worker)                                          │
 │      │  reads your settings (deck, note type, field mapping)              │
 │      │  4. HTTP POST  ───────────────────────────────────┐                │
 │      ▼                                                    │                │
 └──────┼────────────────────────────────────────────────── ┼────────────────┘
        │                                                    │
        │  http://127.0.0.1:8765   (AnkiConnect)             │
        ▼                                                    ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  Anki (running on your machine)                                           │
 │                                                                           │
 │   AnkiConnect add-on:  createDeck  →  addNote                             │
 │      │                                                                    │
 │      ▼                                                                    │
 │   New note in the "Omnia Capture" deck                                    │
 │      base field  = "break the ice"   (the word OR phrase you selected)    │
 │      Context/Sentence field = the surrounding text                        │
 │      │                                                                    │
 │      ▼                                                                    │
 │   5. Omnia Smart Notes — at REVIEW TIME — sees the empty generated        │
 │      fields and auto-fills them from the base field using your LLM/TTS.   │
 └─────────────────────────────────────────────────────────────────────────┘
```

**The data we capture** for each selection:

| key         | what it is                                              | typical Anki field |
| ----------- | ------------------------------------------------------- | ------------------ |
| `selection` | the exact text you highlighted (one word OR a phrase)   | the **base field** |
| `sentence`  | the sentence containing the selection                   | `Sentence`         |
| `context`   | the larger paragraph/block around it (capped, ~600 chars) | `Context`        |
| `pageTitle` | the page's `<title>`                                    | `Title` (optional) |
| `url`       | the page URL                                            | `Source` (optional)|

You decide which Anki note field each of these goes into (the "field mapping" on the
options page). The only one that really matters for Omnia is `selection` → your Omnia
**base field**.

### Why "phrase, not just word" matters

Omnia's base field is "a word OR a phrase". The clipper captures your **entire selection**
verbatim, so highlighting `break the ice` lands `break the ice` in the base field, and
Omnia generates a card for the whole idiom — not just the last word.

---

## 2. Files in this extension

```
3rdparty/
├── README.md                      ← this file
└── omnia-web-clipper/             ← load THIS folder as an unpacked extension
    ├── manifest.json              ← extension definition (Manifest V3)
    ├── background.js              ← service worker: talks to AnkiConnect
    ├── content.js                 ← runs on pages: selection → "+" tooltip → capture
    ├── shared.js                  ← shared defaults + AnkiConnect HTTP client
    ├── options.html / options.js  ← settings page (URL, deck, note type, field mapping, Test connection)
    ├── popup.html / popup.js       ← toolbar popup: AnkiConnect reachable? + link to options
    └── icons/
        ├── icon.svg               ← source artwork
        ├── icon16.png             ← toolbar icon
        ├── icon48.png             ← extensions page icon
        └── icon128.png            ← store/large icon
```

No npm, no bundler, no external libraries or CDNs — plain HTML/CSS/JS that loads directly.

---

## 3. Load the extension into Chrome (step by step)

1. Open Chrome and go to `chrome://extensions` (type it in the address bar).
2. Turn on **Developer mode** — the toggle is in the **top-right** corner.
3. Click **Load unpacked** (top-left).
4. In the folder picker, select this folder:
   `…/anki/addons/addons/3rdparty/omnia-web-clipper`
   (pick the **`omnia-web-clipper`** folder itself, the one that contains `manifest.json`).
5. The extension card "Omnia Web Clipper" appears. **Copy its ID** — a long string like
   `abcdefghijklmnopabcdefghijklmnop`. You will paste it into AnkiConnect's config in the
   next section.
6. (Optional) Click the puzzle-piece icon in the toolbar and **pin** Omnia Web Clipper so
   its popup is one click away.

> Whenever you change a file in this folder, return to `chrome://extensions` and click the
> **reload** (circular arrow) icon on the extension card.

---

## 4. Set up AnkiConnect (allow the extension to talk to Anki)

The clipper sends an HTTP request from your browser to Anki. Browsers block cross-origin
requests unless the server explicitly allows the caller's **origin**. Your extension's
origin is `chrome-extension://<your-extension-id>`, and that origin must be added to
AnkiConnect's allow-list.

1. In Anki, open **Tools → Add-ons**, select **AnkiConnect**, and click **Config**.
2. You will see JSON like this (the defaults relevant here):

   ```json
   {
     "apiKey": null,
     "webBindAddress": "127.0.0.1",
     "webBindPort": 8765,
     "webCorsOriginList": ["http://localhost"]
   }
   ```

3. Add your extension's origin to `webCorsOriginList`. Use the ID you copied in step 3.5:

   ```json
   {
     "apiKey": null,
     "webBindAddress": "127.0.0.1",
     "webBindPort": 8765,
     "webCorsOriginList": [
       "http://localhost",
       "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
     ]
   }
   ```

   For quick testing only, you may instead use `"webCorsOriginList": ["*"]` to allow any
   origin. This is convenient but less safe (any web page could then post to your local
   AnkiConnect) — prefer the explicit `chrome-extension://…` entry once it works.

4. Click **OK**, then **fully restart Anki** so AnkiConnect re-reads its config.

### About `apiKey`

If your AnkiConnect config sets `"apiKey": null` (the default), leave the **API key** box
on the extension's options page **empty**. If you set an `apiKey` string in AnkiConnect,
put the same string in the options page; the extension sends it as the `key` field on every
request.

### Why a `chrome-extension://…` origin and not just `http://localhost`?

The default `webCorsOriginList` only contains `http://localhost`, which is for web pages
served from localhost. A browser **extension** has a different origin
(`chrome-extension://<id>`), which is **not** in the default list — so without step 4.3 the
request is rejected by CORS and the extension shows a "Could not reach AnkiConnect …" error.

---

## 5. Set up Omnia (so captured cards fill themselves)

The clipper only creates a note with the base field + context filled in. Omnia generates
the rest. To wire that up:

1. **Pick or create a note type** with at least:
   - a **base field** — the input, e.g. `Word` (this holds the captured word/phrase), and
   - a **context field** — e.g. `Sentence` and/or `Context` (holds the surrounding text),
   - plus the fields you want Omnia to generate (e.g. `Meaning`, `Example`, `Audio`).
2. In Anki, open **Tools → Omnia → Smart Notes → Configure**.
   - Select that note type.
   - Set its **base field** to your input field (e.g. `Word`).
   - For each field you want generated, enable it and either write a prompt or let
     **auto-smart** propose prompts for the whole note type. Auto-smart writes a prompt for
     every generatable field, referencing the base field (`{{Word}}`) and any fields you
     already filled, like `{{Sentence}}`.
3. Turn on **"Generate empty smart fields at review time"**
   (`generate_at_review`). With this on, when a freshly captured card first comes up for
   review, Omnia notices the empty generated fields and fills them from the base field (and
   the captured sentence/context) using your configured LLM/TTS providers.
4. Back in the extension's **options page** (toolbar popup → "Open options"), set the
   **field mapping** so:
   - `selection` → your base field name (e.g. `Word`),
   - `sentence` → your sentence field (e.g. `Sentence`),
   - `context` → your context field (e.g. `Context`),
   - `url`/`pageTitle` → optional source fields, or leave blank.

   Use **Test connection** on the options page: it calls AnkiConnect `version`, lists your
   note types (`modelNames`), and prints the **exact field names** of your chosen note type
   (`modelFieldNames`) so you can copy them into the mapping without guessing.

> **Phrase support:** because the base field accepts a word *or* a phrase, you can clip
> `give the cold shoulder` and Omnia will generate the card for the whole expression.

---

## 6. Daily use

1. Make sure **Anki is running**.
2. On any normal web page, **select a word or phrase** (or double-click a word).
3. A small blue **"+"** appears next to your selection. Click it.
4. A toast confirms **"Added to Anki: …"** (or shows a clear error).
5. The new note lands in your **Omnia Capture** deck (created automatically if missing).
   Review it in Anki, and Omnia fills in the generated fields on first review.

Click the toolbar icon any time to see whether AnkiConnect is reachable and to open the
options page.

---

## 7. Troubleshooting

| Symptom                                           | Likely cause / fix |
| ------------------------------------------------- | ------------------ |
| "Could not reach AnkiConnect …"                   | Anki not running, AnkiConnect not installed, or the `chrome-extension://<id>` origin is missing from `webCorsOriginList`. Re-check section 4 and **restart Anki**. |
| "model was not found" / "deck was not found"      | The note type name in options doesn't match Anki exactly. Use **Test connection** to see the real names; note type and field names are case-sensitive. |
| "cannot create note because it is a duplicate"    | Turn on **Allow duplicate notes** in options, or change the existing note. |
| No "+" appears                                    | Some pages block content scripts (e.g. `chrome://` pages, the Web Store). Try a normal article page. Reload the extension after editing files. |
| Wrong field got the text                          | Fix the **field mapping** in options — the left column is the capture key, the box is the Anki field name. |

---

## 8. Known limitation: PDFs

Full PDF support in the browser is limited. Chrome renders PDFs in a **sandboxed internal
viewer** (`chrome-extension://…/pdf-viewer`) into which ordinary content scripts are **not
injected**, so the floating "+" will not appear on those PDFs. The clipper works on:

- normal web pages (HTML), and
- **text-layer** PDFs opened through a viewer that exposes a real text selection in the page
  DOM (some web-based PDF viewers do this).

It will **not** work inside Chrome's built-in PDF viewer or on image-only/scanned PDFs that
have no selectable text. For PDFs, a practical workaround is to open the document in a
web-based reader that renders selectable HTML text, or to copy the passage into a normal
page. This is a browser sandbox constraint, not something the extension can override; we
document it here rather than over-engineering around it.

---

## 9. Design note — v2: instant auto-generation (not built yet)

**Today:** capture creates a note; Omnia fills the generated fields **at review time**
(`generate_at_review`). Simple, robust, and requires no new Anki endpoint. The only "cost"
is that the card is incomplete until its first review.

**A tighter loop (future):** trigger generation **immediately on capture**. Three options,
roughly in order of effort:

1. **AnkiConnect-only nudge.** After `addNote`, call `guiBrowse` with a query that selects
   the new note, then have an Omnia "Generate selected" browser action the user triggers —
   or, better, an Omnia hook on note-add that auto-queues generation for notes in the
   capture deck / with the `omnia-web-clipper` tag. No new network surface; reuses Anki's
   own UI and Omnia's existing batch generation.
2. **Hook note-add inside Omnia.** Omnia subscribes to Anki's "note added" signal and, for
   notes matching a configured deck/tag, runs the same Smart Notes generation it already
   runs at review time — off the Qt main thread via `QueryOp`. The clipper would just tag
   captures (it already adds `omnia-web-clipper`) and Omnia would react. This keeps all LLM
   logic in the add-on and needs no new endpoint.
3. **A small local Omnia endpoint.** Omnia could expose a tiny localhost HTTP receiver
   (its own, or piggy-backing on AnkiConnect's `addNote` + a custom action) that the
   extension posts to directly, generating fields synchronously before the note is even
   saved. This is the most "instant" but the most invasive — it adds a network surface and
   threading concerns to the add-on, so it is deliberately deferred.

**Recommendation:** option 2 (note-add hook keyed on the capture tag/deck) is the cleanest
upgrade — it reuses Omnia's existing generation engine and the tag the clipper already
sends, with no new protocol. This section is documented design only; nothing here is
implemented in this wave.
