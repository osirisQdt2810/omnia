# Omnia — User Guide

A walkthrough of every feature, written to be followed in order on a machine with **no Omnia
installed**. It assumes nothing about the repository: everything here is done from AnkiWeb and
from Omnia's own settings dialog.

Tested on Windows 11 with Anki 25.09+. macOS and Ubuntu differ only where marked.

---

## 0. Before you start

### What "a clean machine" means

If you have used Omnia before, three things live outside the add-on folder and are **not**
removed when you uninstall it:

| Thing | Where | Removed by uninstall? |
|---|---|---|
| Feature settings (enabled plugins, per-plugin options) | inside `collection.anki2` | **No** |
| Provider settings + API keys | `user_files/config/` in the add-on folder | Yes |
| Desktop Clipper app | `%LOCALAPPDATA%\Programs\Omnia Desktop Clipper` | No — separate app |

The first row matters: your feature settings survive a reinstall and come straight back. That is
usually what you want — but it does mean a reinstall is **not** a way to get a clean slate
(§2.3).

### Back up your keys first

Provider keys live in the add-on folder and **are destroyed by an uninstall**. Copy this folder
somewhere safe before removing anything:

```
%APPDATA%\Anki2\addons21\<omnia-id>\user_files\config\
```

It contains `providers.toml` and a `.secrets\` folder holding one file per key. Restoring is
just copying it back (§8.4).

---

## 1. Install from AnkiWeb

1. Close Anki completely.
2. Open <https://ankiweb.net/shared/info/726991726> in a browser.
3. Note the **code** on that page (it is the number in the URL: `726991726`).
4. Start Anki → **Tools → Add-ons → Get Add-ons…**
5. Paste `726991726` → **OK**.
6. Restart Anki when prompted.

You should now see **Tools → Omnia** in the menu bar. That single entry is the whole UI.

> **Downloading the `.ankiaddon` file instead.** The AnkiWeb page has a Download button. If you
> use it, install with **Tools → Add-ons → Install from file…** and pick the downloaded
> `.ankiaddon`. Same result; the code method is fewer steps and gets updates automatically.

### Checking it loaded

**Tools → Add-ons** should list *Omnia — All-in-One Toolkit*. If it is greyed out or missing,
see §8.1.

---

## 2. The settings dialog

### 2.1 Opening it

**Tools → Omnia**. The window is a list of feature **cards**, grouped into sections:

| Section | Features |
|---|---|
| Reviewing | Auto Flip, Display Interval |
| Grading | Typing Accuracy, Overdue Guard |
| AI | Smart Notes |
| Editing | Note Maintenance |
| Integrations | Word Lookup |

### 2.2 How features work

Each card has two controls: a **toggle switch** and a **Configure…** button.

- Every feature is **off until you flip its toggle**. Flipping takes effect immediately — no
  restart. So does flipping it back, which is the fastest way to isolate a feature that is
  misbehaving.
- **Configure…** opens that feature's options. What you get depends on the feature:

| Feature | Configure… opens |
|---|---|
| Auto Flip, Display Interval, Typing Accuracy, Overdue Guard | A generic options form — changes apply on **OK**, Cancel discards them |
| Smart Notes, Note Maintenance, Word Lookup | A purpose-built panel of their own |

Smart Notes' panel is the largest, and has its own **⚙ Options** modal inside it (§3.1) — which
is why its settings are two levels deep rather than one.

### 2.3 Starting from a clean slate

Feature settings live in your collection, not in the add-on folder, so **reinstalling does not
reset them**, and there is no one-click reset. To start genuinely clean, untick every feature
and clear the options you changed, by hand, before you begin.

Practically this rarely matters: an unticked feature is inert. It matters when you are trying to
reproduce what a new user sees.

---

## 3. Connecting an AI provider

Only **Smart Notes** (§4.7) and the clippers need this. Every other feature works with no
account and no network.

### 3.1 Where keys are entered

The full path, which is easy to miss because it is behind a modal:

```
Tools → Omnia → Smart Notes → ⚙ Options → Usage & Keys → 🔑 Keys
```

The **⚙ Options** button is on the Smart Notes panel itself. The word "Keys" appears nowhere
until that modal is open *and* the **Usage & Keys** tab is selected.

The page shows **three cards** — Gemini · AI Studio, Gemini · Vertex AI, OpenRouter — and only
those three. Each has its own fields, a link to its console, and, for OpenRouter, a live credit
balance. Nothing you can do adds a fourth card (§3.3).

Keys are written to `user_files/config/.secrets/`, one file per key, never into your collection
and never into the collection sync.

### 3.2 Gemini · Vertex AI (service-account JSON)

Vertex does not use a simple API key. Its card has four fields:

| Field | Type | Notes |
|---|---|---|
| Project ID | text | Leave blank to read it from the JSON |
| Location | text | e.g. `us-central1` |
| **Service-account JSON** | **file picker** | This is the import |
| Access token (optional) | secret | Only for short-lived tokens; normally leave empty |

**To import:** click the file field, pick the service-account `.json` you downloaded from the
Google Cloud Console, and leave *Project ID* blank so it is read from the file. Then set
*Location* to the region your models are enabled in.

The $300 free credit and your quotas live in the GCP Console — Omnia deliberately does not
claim to show them, because they are not readable from a service-account key.

### 3.3 The other providers

| Provider | What it needs | Console |
|---|---|---|
| Gemini · AI Studio | API key | <https://aistudio.google.com/app/apikey> |
| OpenRouter | API key | <https://openrouter.ai/settings/credits> |

Those two, **plus Vertex (§3.2), are the only LLM providers there are**. The Keys page has
exactly three cards, and the Text subtab's dropdown offers exactly those three names — the set
is fixed, not open-ended.

> **If you have an OpenAI key:** OpenAI and other OpenAI-compatible endpoints are understood by
> the config layer but have **no key-entry UI and no dropdown entry**. Using one means editing
> `[llm.openai]` (or `[llm.openai_compatible]`) in
> `user_files/config/providers.toml` by hand — there is no path to it through the settings
> dialog, so do not go looking for a card that does not exist.

Inside **⚙ Options → Usage & Keys** there are four subtabs. Knowing which does what saves a lot
of hunting:

| Subtab | What you set there |
|---|---|
| **Text** | The active **LLM** provider and its text model |
| **Image** | The active **image** provider and its model |
| **Sound** | The active **TTS** provider and its voice |
| **🔑 Keys** | Credentials for every provider (§3.1) |

Do not skip **Image** if you generate picture fields — it is a first-class rule kind alongside
text and TTS, with its own provider and model.

These set the *defaults*. Each individual rule row can override the provider and model for
itself (§4.7).

The five tabs of the Options modal overall are **General**, **Usage & Keys**, **Tools**,
**Integrations** and **Advanced**.

### 3.4 Text-to-speech

TTS is configured in **⚙ Options → Usage & Keys → Sound**, separately from the LLM. The dropdown
offers five, and **four of them need no account at all**:

| Voice provider | Key needed? | Notes |
|---|---|---|
| **Edge TTS** | No | Free Microsoft voices; the best quality you can get without an account |
| **Google Translate TTS** | No | Free; pick a language rather than a named voice. Fine for vocabulary |
| **Piper** | No | Fully offline. Downloads a voice model on first use |
| **VietTTS** | No | A local open-source server — good Vietnamese, but you run the server |
| **Google Cloud TTS** | **Yes** | Paid, via your Google Cloud project |

If you just want audio working now, choose **Edge TTS** and move on.

---

## 4. The features, one at a time

Each subsection is: **what it does → turn it on → try it → what you should see.**

### 4.1 Auto Flip

Advances a card for you: question → answer → grade, on a timer. Useful for passive review and
for listening decks.

**Turn on:** tick *Auto Flip*.

**Options**

| Option | Meaning |
|---|---|
| `delay_question_seconds` | Seconds on the question before it flips |
| `delay_answer_seconds` | Seconds on the answer before it grades |
| `wait_for_audio` | Do not advance until the card's audio has finished |
| `show_timer` | Draw a countdown on the card |
| `per_deck` | Per-deck overrides (also reachable from the deck's options) |

**Try it:** set both delays to `3`, tick *show timer*, study any deck. The countdown appears and
the card advances on its own.

**Watch for:** with `wait_for_audio` off and a long audio field, the card flips mid-sentence.
That is the setting doing what it says, not a bug.

### 4.2 Display Interval

Shows the interval each answer button would give you, on the answer side.

**Turn on:** tick *Display Interval*.

**Options:** `text_color`, and `expose_to_templates` if you want to place the value yourself in
a card template rather than take the default position.

**Try it:** answer any card. The predicted next interval appears with the buttons.

### 4.3 Typing Accuracy

For typed cards: grades from **how accurately you typed** rather than from the button you press,
and adds an interactive accuracy panel to the Statistics screen.

**Turn on:** tick *Typing Accuracy*. Needs a note type with a `{{type:Field}}` on the template.

**Options**

| Option | Meaning |
|---|---|
| `threshold` | Accuracy below this is graded down |
| `pass_ease` | Which ease a passing answer gets |
| `show_stats` | Add the accuracy panel to Statistics |

**Try it:** study a typed card and deliberately mistype one letter. The grade reflects the
accuracy, not the button. Then open **Statistics** to see the panel.

### 4.4 Overdue Guard

Forces very overdue cards down to Hard/Again no matter which button you press — so a card you
have not seen in months cannot jump straight back to a long interval.

**Turn on:** tick *Overdue Guard*.

**Options**

| Option | Meaning |
|---|---|
| `ratio` | How many times its interval a card must be overdue to count |
| `min_days` | Floor, so short-interval cards are not caught |
| `force_again_after_days` | Past this, force *Again* outright |

**Try it:** find a badly overdue card and press **Easy**. It is graded down instead.

> Typing Accuracy and Overdue Guard both rewrite the grade. They cooperate rather than fight —
> both register on one shared pipeline instead of each patching Anki separately.

### 4.5 Note Maintenance

Cleans and reformats text your notes **already contain** with deterministic, provider-free
tasks — no tokens, no network, no provider configured. It is the complement to Smart Notes, not
a cheaper version of it.

**Turn on:** tick *Note Maintenance*, then choose which note types and which tasks to run.

**Try it on a copy first.** It edits notes in place. Select a handful in the Browser and run it
there before pointing it at a whole deck.

### 4.6 Word Lookup

Runs a small local service so the Desktop Clipper can ask *"is this word already in my
collection?"* and show you the matching notes.

**Turn on:** tick *Word Lookup*. It only listens on `127.0.0.1` — nothing is exposed to your
network.

**Options**

| Option | Meaning |
|---|---|
| `note_types` | Which note types to search |
| `search_fields` / `display_fields` | Fields to search in; fields to show |
| `match_word_forms` | Match "running" against "run" |
| `hidden_fields` | Never show these |
| `max_results`, `max_fields` | Result limits |
| `port` | Default `8766` — change only on a conflict |

**Try it:** see §6.3, which uses it from the Desktop Clipper.

### 4.7 Smart Notes

The AI feature: generates note fields (text and images) and audio, from an LLM/TTS provider.
Needs §3 done first.

**Turn on:** tick *Smart Notes*.

**Key options**

| Option | Meaning |
|---|---|
| `note_types` | Per-note-type rules: which field is generated from which prompt |
| `generate_at_review` | Fill missing fields as cards come up in review |
| `regenerate_when_batching` | Overwrite fields that already have content |
| `allow_empty_fields` | Accept an empty result instead of treating it as failure |
| `max_concurrent_generations` | **Fields** generated at once — not notes (see below) |
| `batch_notes_per_call` | Notes packed into a single provider call |

**About `max_concurrent_generations`:** the unit is *fields*, because a field is the unit of
work sent to a provider. One note with four generated fields is four units. Counting notes
would make the real load swing wildly with note shape.

**Try it:**

1. On the Smart Notes panel itself (not in ⚙ Options), pick your note type from the **Note
   type** dropdown. The **Fields** table below lists every field of that note type, one row
   each, with columns **Generate**, **Type** (`text` / `image` / `tts`), **Prompt**,
   **Provider**, **Model**, **Voice**, **Preview** and **Overwrite**. Tick *Generate* on a
   field and fill in its prompt — the per-row Provider/Model override the defaults you set in
   §3.3, so you can leave them alone at first.
2. Open the Browser, select two or three notes, **right-click → the Omnia generate action**.
   (Omnia also adds entries to the Browser *sidebar* context menu and to the *editor* context
   menu, so you can generate from whichever you have open.)
3. Watch the progress line; the fields fill in.

Start with **two notes**, not two hundred. Every generation costs provider credit.

---

## 5. Installing the clippers

The clippers capture text from **outside** Anki and turn it into cards. They are optional, and
each is a separate program installed from Omnia rather than hunted down in the OS.

Go to:

```
Tools → Omnia → Smart Notes → ⚙ Options → Integrations
```

Each row has an install button whose **label changes with state** — there is never a button
simply reading "Install", so look for the label that matches your situation:

| Situation | Desktop Clipper | Web Clipper |
|---|---|---|
| Not installed yet | **Install app** | **Set up…** |
| Installed, newer build available | **Upgrade** | **Upgrade** |
| Installed and current | **Up to date** *(greyed out)* | **Up to date** *(greyed out)* |

"Up to date" being disabled is correct, not a broken UI: there is nothing to do.

Each row also has a second button that acts on an already-installed clipper:

| Row | Second button | What it does |
|---|---|---|
| Omnia Web Clipper | **Reload** | Reloads the extension in the Chrome profile you used last and opens its Settings |
| Omnia Desktop Clipper | **Open** | Re-opens the installed desktop app |

Both start **disabled** and enable once the row reports the clipper as installed — offering them
before that would only fail in a way you could not act on.

### 5.1 Desktop Clipper

1. **Integrations → Omnia Desktop Clipper → Install app.** It clones, builds and installs; the
   row shows progress. First install takes a few minutes.
2. When it finishes, the app launches on its own. Afterwards use **Open**.
3. Grant permissions when asked:
   - **macOS:** Accessibility *and* Input Monitoring, in System Settings → Privacy & Security.
   - **Windows:** no special permission; allow it through the firewall prompt if one appears.

**Upgrading:** press **Upgrade** when the row offers it. If the button reads **Up to date** and
is greyed out, you already have the current build and there is nothing to press.

You do **not** need to quit the clipper first — the upgrade moves the running copy aside rather
than trying to delete files Windows has locked. Restart the clipper afterwards to be running the
new build.

### 5.2 Web Clipper

1. **Integrations → Omnia Web Clipper → Set up….** Omnia prepares the extension and opens a
   finish-install page.
2. Follow that page: open `chrome://extensions`, enable **Developer mode**, choose **Load
   unpacked**, and select the folder the page names.
3. Afterwards, **Reload** re-loads it in your most recent Chrome profile and opens its Settings.
   It needs Chrome installed and the extension already loaded; with no Chrome it says so.

---

## 6. Using the clippers

### 6.1 Capturing with the Desktop Clipper

Select text in **any** application. Two gestures are recognised:

- **Double-click** a word (the two clicks within ~0.4 s and ~6 px of each other), or
- **Drag-select** a phrase (press → move at least ~8 px → release).

There are also global hotkeys, useful when a gesture is not detected:

| Action | Windows / Linux | macOS |
|---|---|---|
| Capture the current selection | `Ctrl+Shift+A` | `Cmd+Shift+A` |
| OCR a screen region | `Ctrl+Shift+O` | `Cmd+Shift+O` |

The tray menu's **Capture now** does the same as the capture hotkey.

On a successful gesture a small overlay appears next to the selection with two actions:

- **(+)** — send the selection to Anki as a new note.
- **(search)** — look the word up in your collection first (needs §4.6 on).

The clipper also captures the **surrounding context**, so the card carries the sentence the word
appeared in — including inside PDF viewers.

**If no overlay appears:** see §8.2.

### 6.2 Capturing with the Web Clipper

Select text on a page and use the extension's action. The selection, the page title and the URL
go to Anki. Configure what lands in which field in the extension's Settings (reachable via
**Reload**).

### 6.3 Word lookup, and the image preview

With **Word Lookup** on, the *(search)* action opens a panel showing the notes that already
contain the word, with their fields.

**Image previews** in that panel are served by Omnia itself. They used to be fetched through
AnkiConnect — a *separate* add-on — so on a machine without AnkiConnect every image read
"Image unavailable" while the panel around it worked perfectly. That is fixed: images now come
from the same service that answers the lookup, and AnkiConnect is no longer required.

If an image still does not appear, the message now tells you which failure it was:

| Message | Meaning |
|---|---|
| *Image not found in Anki* | The fetch returned nothing — the file is missing from the collection's media folder |
| *Image format not supported* | The file arrived, but Qt has no plugin for that format |

The audio button behaves the same way; hover it for the reason.

---

## 7. A suggested first session

If you just want to see everything work, in order:

1. Install from AnkiWeb (§1), open **Tools → Omnia**.
2. Tick **Display Interval**. Study one card — the interval shows. *(No setup, proves the
   add-on is live.)*
3. Tick **Auto Flip**, delays `3`/`3`, timer on. Study one card — it advances itself.
4. Tick **Typing Accuracy**. Study a typed card, mistype a letter — the grade follows accuracy.
5. Import your Vertex service-account JSON (§3.2), tick **Smart Notes**, add one rule, generate
   on **two** notes.
6. Install the **Desktop Clipper** (§5.1). Select a word in a browser or PDF, press **(+)**.
7. Tick **Word Lookup**, select a word you know is in your collection, press **(search)** — the
   panel opens with the note and its image preview.

---

## 8. Troubleshooting

### 8.1 The add-on does not load

- **Tools → Add-ons** — if Omnia is listed but disabled, enable it and restart.
- Anki must be **25.09 or newer**.
- Read the log: `%APPDATA%\Anki2\addons21\<omnia-id>\user_files\omnia.log`. It records startup
  and any plugin that failed to enable.

### 8.2 The Desktop Clipper shows no overlay

1. Is the clipper actually running? Check the tray icon; **Open** in Integrations re-launches it.
2. **macOS:** re-grant Accessibility *and* Input Monitoring. Both are needed — with only one,
   the gesture is seen but the overlay cannot be drawn.
3. Some applications refuse to expose their text to other programs. Try the same selection in a
   plain text editor to tell an app problem from a clipper problem.

### 8.3 "I have an OpenAI key and there is no card for it"

There isn't one, and you have not missed a setting. The Keys page has exactly three cards
(Gemini · AI Studio, Gemini · Vertex AI, OpenRouter) and the Text dropdown offers exactly those
three names.

OpenAI and other OpenAI-compatible endpoints are supported by the config layer only. To use one,
close Anki and edit `user_files/config/providers.toml` by hand:

```toml
[llm]
provider = "openai"

[llm.openai]
api_key = "sk-…"
base_url = "https://api.openai.com/v1"
text_model = "…"
```

Then reopen Anki. `[llm.openai_compatible]` works the same way for a self-hosted endpoint.

### 8.4 Restoring your provider keys

Copy your backed-up folder back over:

```
%APPDATA%\Anki2\addons21\<omnia-id>\user_files\config\
```

Restart Anki. The Keys page shows the restored providers (values masked).

### 8.5 Port already in use

Word Lookup defaults to `8766`. If something else holds it, change `port` in the Word Lookup
options — and set the same port in the Desktop Clipper's settings, or the two stop talking.

### 8.6 Where the logs are

Omnia writes one log, crashes included:

```
%APPDATA%\Anki2\addons21\<omnia-id>\user_files\omnia.log
```

It rotates at 2 MB and keeps three older files beside it — `omnia.log.1`, `.2`, `.3`. `omnia.log`
is always the newest; reach for the numbered ones only when chasing something that happened a
while ago.

When reporting a problem, the last ~50 lines of `omnia.log` are usually enough.

The Desktop Clipper has no log viewer in its UI — its tray menu is only **Enabled**, **Capture
now**, **Settings…** and **Quit**. To see what it is doing, run it from a terminal so its output
goes to the console.

---

## 9. Uninstalling

1. **Tools → Add-ons → Omnia → Delete.** *(Back up `user_files\config\` first — §0.)*
2. The Desktop Clipper is a **separate application** and is not removed with the add-on. Quit it
   from the tray, then delete `%LOCALAPPDATA%\Programs\Omnia Desktop Clipper`. If Windows
   refuses because a file is in use, the app is still running.
3. The Web Clipper is removed in `chrome://extensions`.
4. Your **feature settings stay in the collection**, so reinstalling brings them back. There is
   no reset control — if you want them gone, untick the features and clear their options before
   you uninstall.
