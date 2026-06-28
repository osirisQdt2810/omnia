# Omnia Web Clipper (Chrome extension)

A small Chrome extension that lets you capture a **word or phrase** from any web page and
send it — together with the sentence and paragraph around it — straight into your running
Anki as a new note. There are two capture paths:

- **Double-click a word** → a floating **"+"** appears next to it; click it to send.
- **Right-click a phrase** → choose **"Send to Anki (Omnia)"** from the context menu.

You first pick **which deck and note type** captures land in on the options page (real
dropdowns loaded from AnkiConnect). The [Omnia](../README.md) add-on's **Smart Notes**
feature then fills in the rest of the card (definition, example, audio, etc.) automatically.

An **Enabled** master toggle (in the toolbar popup and on the options page) turns both
capture paths on/off at once.

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
 │   1a. Double-click "break" → content.js shows a floating "+" near it      │
 │   1b. Right-click "break the ice" → "Send to Anki (Omnia)" context menu   │
 │      │                                                                    │
 │      ▼                                                                    │
 │   content.js  ──builds──►  capture for the current selection              │
 │      │                                                                    │
 │      │ 2. click "+"  (or pick the menu item)                              │
 │      ▼                                                                    │
 │   capture = { selection, sentence, context, pageTitle, url }              │
 │      │                                                                    │
 │      │ 3. chrome.runtime.sendMessage / contextMenus.onClicked             │
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
    ├── manifest.json              ← extension definition (Manifest V3); contextMenus permission
    ├── background.js              ← service worker: context menu + addNote via AnkiConnect
    ├── content.js                 ← runs on pages: "+" tooltip, surrounding-text capture, toasts
    ├── shared.js                  ← shared defaults + AnkiConnect HTTP client
    ├── options.html / options.js  ← settings page: Test connection → deck/note-type/field dropdowns, toggles
    ├── popup.html / popup.js       ← toolbar quick-options: Enabled toggle, deck/note type, reachability
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
4. Back in the extension's **options page** (toolbar popup → "Options"):
   1. Click **Test connection**. It calls AnkiConnect `version` (showing
      **Ankiconnect — Connected ✓** or **Not Connected ✗**), then loads `deckNames` and
      `modelNames` into the **Deck** and **Note type** dropdowns.
   2. Pick your **Deck** and **Note type** from the dropdowns (no typing — they are the
      real names from your collection; the deck is created automatically if missing).
   3. Choosing a note type calls `modelFieldNames` for it and fills the **field-mapping**
      dropdowns with that note type's real field names. Map:
      - `selection` → your base field (e.g. `Word`) — this is the important one,
      - `sentence` → your sentence field (e.g. `Sentence`),
      - `context` → your context field (e.g. `Context`),
      - `url`/`pageTitle` → optional source fields, or `(skip)`.
   4. Click **Save**.

   The **General** section has two green On/Off pill toggles: **Enabled** (the master
   switch for both capture paths) and **Double-click "+"** (show the floating "+" on
   selection). Flipping a pill is saved immediately.

> **Phrase support:** because the base field accepts a word *or* a phrase, you can clip
> `give the cold shoulder` and Omnia will generate the card for the whole expression.

---

## 6. Daily use

1. Make sure **Anki is running** and the extension's **Enabled** toggle is on (toolbar
   popup or options page).
2. Capture, two ways:
   - **A word:** double-click it. A small blue **"+"** appears next to it — click it.
   - **A phrase:** select it, **right-click**, and choose **"Send to Anki (Omnia)"**.
3. A toast confirms **"Added to Anki: …"** (or shows a clear error).
4. The new note lands in your chosen deck (created automatically if missing). Review it in
   Anki, and Omnia fills in the generated fields on first review.

Click the toolbar icon any time for **Quick options**: the **Enabled** toggle, the
currently-selected **Deck** and **Note type**, AnkiConnect reachability, and an **Options**
button. When **Enabled** is off, neither the "+" nor the right-click action does anything.

---

## 7. Troubleshooting

| Symptom                                           | Likely cause / fix |
| ------------------------------------------------- | ------------------ |
| "Could not reach AnkiConnect …"                   | Anki not running, AnkiConnect not installed, or the `chrome-extension://<id>` origin is missing from `webCorsOriginList`. Re-check section 4 and **restart Anki**. |
| "model was not found" / "deck was not found"      | The note type name in options doesn't match Anki exactly. Use **Test connection** to see the real names; note type and field names are case-sensitive. |
| "cannot create note because it is a duplicate"    | Turn on **Allow duplicate notes** in options, or change the existing note. |
| No "+" appears                                    | Check **Enabled** and **Double-click "+"** are on (popup/options). Some pages block content scripts (e.g. `chrome://` pages, the Web Store). Try a normal article page. Reload the extension after editing files. |
| No "Send to Anki (Omnia)" in the right-click menu | The menu only shows when text is **selected**. If it still does nothing, check **Enabled** is on and that the page allows content scripts (so the surrounding sentence/context can be read). |
| Wrong field got the text                          | Fix the **field mapping** in options — pick the right note-type field from the dropdown for each capture key. |

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

## 10. Distribution — installing on another machine (without "Load unpacked" each time)

**Short answer:** publish it once to the **Chrome Web Store as an _Unlisted_ item**, then on
any other computer you just open the store link (or sign into Chrome with the same Google
account and **Chrome Sync** installs it automatically). "Unlisted" means it is NOT searchable
or public — only people with the link can install it, which is what you want for a personal
tool.

### Package it for upload
```bash
bash 3rdparty/omnia-web-clipper/package.sh   # → 3rdparty/omnia-web-clipper-0.1.0.zip
```
The zip has `manifest.json` at its root (a Web Store requirement).

### Publish (one-time)
1. Go to the **Chrome Web Store Developer Dashboard**: https://chrome.google.com/webstore/devconsole — pay the **one-time $5** registration fee.
2. **New item → upload** `omnia-web-clipper-<version>.zip`.
3. Fill the listing: name, a short description, the 128px icon (already in the zip), and 1+
   screenshot. Under **Privacy practices**, declare honestly: the extension stores its
   settings locally (`chrome.storage`) and sends captured text **only to your own local
   AnkiConnect** (`127.0.0.1`) — it collects/transmits no data to any remote server. The
   broad host/`<all_urls>` access is "to read the selected text on the page you clip from".
4. Set **Visibility → Unlisted**, then **Submit for review** (usually hours, sometimes a few
   days). Once approved you get a permanent install link; open it on any machine to install,
   and Chrome Sync mirrors it to your other signed-in machines.

### Important: keep the extension id STABLE (so AnkiConnect CORS is set once)
AnkiConnect must allow the extension's `chrome-extension://<id>` origin (README §4). The id
differs between an unpacked load (random, per machine) and the published item (stable). To
make the id identical EVERYWHERE — unpacked dev installs and the published store item — pin
it with a manifest `key`:
```bash
# generate a private key + derive the manifest "key" (public key, base64)
openssl genrsa 2048 > omnia-clipper.pem
KEY=$(openssl rsa -in omnia-clipper.pem -pubout -outform DER 2>/dev/null | openssl base64 -A)
# add  "key": "<KEY>"  to manifest.json (keep omnia-clipper.pem private, OUT of git)
```
With a pinned `key`, the id is the same on every machine, so you add the
`chrome-extension://<id>` origin to AnkiConnect's `webCorsOriginList` **once**.

### Free alternative (no store, no review): GitHub + Load unpacked
This whole folder already lives in the repo under `3rdparty/omnia-web-clipper/`. On another
machine: clone/pull the repo (or download a release zip), then `chrome://extensions →
Developer mode → Load unpacked → pick the folder` (README §3). Without a pinned `key` the id
is random per machine, so either pin the `key` (above) or re-add that machine's
`chrome-extension://<id>` to AnkiConnect's CORS list.

### Why not just host a `.crx` file to click-install?
Chrome deliberately **blocks installing `.crx` files from outside the Web Store** for normal
users (a security measure; off-store installs need enterprise policy). So a self-hosted
`.crx` is not a practical "click to install" path — use the Web Store (Unlisted) or Load
unpacked. (Edge can install Chrome extensions and has its own store; Firefox is the one
browser that allows self-hosting a signed add-on, but this extension is MV3/Chrome-targeted.)
