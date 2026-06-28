"""Pure HTML/CSS/JS builder + row↔config mapping for the Smart Notes config page.

The Smart Notes dialog (``smart_notes_dialog.py``) is thin Qt/webview glue; all of this
page's markup AND the pure mapping between the note-type config model and the table rows
live here so they unit-test headless. Everything is inlined (no external assets) because the
host :class:`~omnia.gui.web_dialog.WebDialog` applies a strict CSP.

The page is note-type-centric: one always-present BASE (input) field that is never
generated, and one table row per other field describing how to generate it. It talks back to
Python through the WebDialog bridge with these ops:

* ``list_note_types`` → ``[name, ...]``
* ``load`` ``{note_type}`` → the note type's base field, all fields, rows, and providers
* ``set_base_field`` ``{note_type, base_field}`` → re-rendered rows for the new base
* ``create_field`` ``{note_type, field_name}`` → the note type's updated field names
* ``auto_smart`` ``{note_type, base_field, rows}`` → rows with prompts/types filled in
* ``save`` ``{note_type, base_field, rows}`` → ``{ok: true}`` once persisted

This module imports nothing from ``aqt``/``anki``; it only knows the config models.
"""

from __future__ import annotations

import json

from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)

_FIELD_TYPES = ("text", "tts", "image")


def rows_for_note_type(
    config: SmartNotesNoteTypeConfig | None,
    all_fields: list[str],
    base_field: str,
) -> list[SmartNotesFieldConfig]:
    """Return one :class:`SmartNotesFieldConfig` per NON-base field, merging saved + live.

    Every current field of the note type except ``base_field`` gets a row: a previously saved
    row is reused as-is (preserving the user's prompt/type/overrides), and a field with no
    saved row appears with defaults. Saved rows whose field no longer exists on the note type
    are dropped, and a saved row matching the base field is excluded. Order follows the note
    type's live field order so the table mirrors the editor.

    Args:
        config: The saved note-type config, or None when the note type has none yet.
        all_fields: The note type's current field names, in order.
        base_field: The designated base (input) field, never generated.

    Returns:
        The per-field rows the table renders, in ``all_fields`` order.
    """
    saved = {row.field: row for row in config.fields} if config is not None else {}
    rows: list[SmartNotesFieldConfig] = []
    for name in all_fields:
        if name == base_field:
            continue
        existing = saved.get(name)
        rows.append(
            existing.copy()
            if existing is not None
            else SmartNotesFieldConfig(field=name)
        )
    return rows


def resolve_base_field(
    config: SmartNotesNoteTypeConfig | None, all_fields: list[str]
) -> str:
    """Return the base field to show: the saved one (if still present) else the first field."""
    if config is not None and config.base_field in all_fields:
        return config.base_field
    return all_fields[0] if all_fields else ""


def field_configs_from_payload(
    rows: list[dict[str, object]],
) -> list[SmartNotesFieldConfig]:
    """Build :class:`SmartNotesFieldConfig`s from the JS-posted row dicts (one per non-base field).

    Each dict carries the row's editable state (``field``, ``enabled``, ``type``, ``prompt``,
    ``prompt_locked``, ``provider``, ``model``, ``voice``, ``overwrite``). A row with no
    ``field`` name is skipped; an invalid ``type`` falls back to ``"text"`` so a malformed
    payload can't raise during validation.

    Args:
        rows: The row dicts posted from the page.

    Returns:
        Validated field configs, ready to assemble into a note-type config.
    """
    configs: list[SmartNotesFieldConfig] = []
    for row in rows:
        name = str(row.get("field", "")).strip()
        if not name:
            continue
        field_type = str(row.get("type", "text"))
        if field_type not in _FIELD_TYPES:
            field_type = "text"
        configs.append(
            SmartNotesFieldConfig(
                field=name,
                enabled=bool(row.get("enabled", False)),
                type=field_type,
                prompt=str(row.get("prompt", "")),
                prompt_locked=bool(row.get("prompt_locked", False)),
                provider=str(row.get("provider", "")),
                model=str(row.get("model", "")),
                voice=str(row.get("voice", "")),
                overwrite=bool(row.get("overwrite", False)),
            )
        )
    return configs


def note_type_config_from_payload(
    note_type: str, base_field: str, rows: list[dict[str, object]]
) -> SmartNotesNoteTypeConfig:
    """Assemble a :class:`SmartNotesNoteTypeConfig` from the posted note type, base, and rows."""
    return SmartNotesNoteTypeConfig(
        note_type=note_type,
        base_field=base_field,
        fields=field_configs_from_payload(rows),
    )


def merge_note_type_into(
    note_types: list[SmartNotesNoteTypeConfig], updated: SmartNotesNoteTypeConfig
) -> list[SmartNotesNoteTypeConfig]:
    """Return ``note_types`` with ``updated`` replacing its same-name entry (or appended).

    Used by the save handler: the dialog edits one note type at a time, so persisting it must
    merge that note type into the existing list without disturbing the others.
    """
    merged = [nt for nt in note_types if nt.note_type != updated.note_type]
    merged.append(updated)
    return merged


def row_to_payload(row: SmartNotesFieldConfig) -> dict[str, object]:
    """Serialize one field config to the dict the page consumes (kept in sync with the JS)."""
    return {
        "field": row.field,
        "enabled": row.enabled,
        "type": row.type,
        "prompt": row.prompt,
        "prompt_locked": row.prompt_locked,
        "provider": row.provider,
        "model": row.model,
        "voice": row.voice,
        "overwrite": row.overwrite,
    }


def load_payload(
    note_type: str,
    config: SmartNotesNoteTypeConfig | None,
    all_fields: list[str],
    providers: list[str],
) -> dict[str, object]:
    """Build the ``load`` op's response: base field, fields, rows, and providers."""
    base_field = resolve_base_field(config, all_fields)
    rows = rows_for_note_type(config, all_fields, base_field)
    return {
        "note_type": note_type,
        "base_field": base_field,
        "all_fields": all_fields,
        "rows": [row_to_payload(row) for row in rows],
        "providers": providers,
    }


def build_smart_notes_html(*, dark: bool, init: dict[str, object] | None = None) -> str:
    """Build the full Smart Notes config page HTML, with the initial data baked in.

    The selectors + first note type's rows are seeded from ``init`` (``window.__SN_INIT``) so
    the page renders fully populated on load WITHOUT an init ``pycmd`` callback — Anki's bridge
    callback channel isn't ready the instant the page's inline script runs, so an init
    ``list_note_types``/``load`` round-trip is dropped and the dialog comes up blank. User
    actions (change note type, set base, create field, auto-smart, save) happen later, when the
    bridge is ready, so they keep using ``pycmd``.

    Args:
        dark: Render the dark palette (Anki night mode) when True, else the light palette.
        init: ``{note_types, note_type, base_field, all_fields, rows, providers}`` for the
            initially-selected note type. None/empty falls back to a JS ``list_note_types``.

    Returns:
        A complete, self-contained HTML document string.
    """
    return _PAGE_TEMPLATE.format(
        theme_class="omnia-dark" if dark else "omnia-light",
        css=_CSS,
        types_json=json.dumps(_FIELD_TYPES),
        init_json=json.dumps(init) if init else "null",
        js=_JS,
    )


# Re-exported for the dialog so it doesn't duplicate the literal anywhere.
FIELD_TYPES = _FIELD_TYPES


# The whole document. The CSP forbids external assets; everything is inline. Curly braces in
# the CSS/JS are doubled so ``str.format`` only fills the named placeholders.
_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body class="{theme_class}">
<div class="sn-shell">
  <header class="sn-header">
    <div class="sn-title">✨ Smart Notes</div>
    <div class="sn-subtitle">Your senior language master — pick a base word and let AI build every other field.</div>
  </header>
  <div class="sn-toolbar">
    <label class="sn-field">
      <span class="sn-label">Note type</span>
      <select id="sn-note-type"></select>
    </label>
    <label class="sn-field">
      <span class="sn-label" title="The always-present INPUT field (a single word OR a phrase). It is never generated.">Base field</span>
      <select id="sn-base-field"></select>
    </label>
  </div>
  <div class="sn-table-wrap">
    <table class="sn-table">
      <thead>
        <tr>
          <th>Field</th>
          <th title="Generate this field?">On</th>
          <th>Type</th>
          <th>Prompt — reference fields as {{{{Base}}}}</th>
          <th title="Inherit the central provider when blank">Provider</th>
          <th title="Inherit the central model when blank">Model</th>
          <th title="Overwrite the field even if it already has content">Overwrite</th>
        </tr>
      </thead>
      <tbody id="sn-rows"></tbody>
    </table>
    <div id="sn-empty" class="sn-empty"></div>
  </div>
  <footer class="sn-footer">
    <div class="sn-footer-left">
      <button id="sn-create" class="sn-btn">+ Create field</button>
      <button id="sn-auto" class="sn-btn sn-btn-magic">✨ Auto-smart</button>
      <span id="sn-msg" class="sn-msg"></span>
    </div>
    <div class="sn-footer-right">
      <button id="sn-cancel" class="sn-btn">Cancel</button>
      <button id="sn-save" class="sn-btn sn-btn-primary">Save</button>
    </div>
  </footer>
</div>
<script>var SN_TYPES = {types_json};</script>
<script>window.__SN_INIT = {init_json};</script>
<script>{js}</script>
</body>
</html>"""


# Light/dark are driven by a body class (same approach as the settings page). Custom
# properties hold the per-theme colors so one stylesheet adapts to Anki's theme.
_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
body {
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 13px;
}
body.omnia-light {
  --bg-top: #f7f8fb; --bg-bottom: #eef1f7; --fg: #1d2230; --muted: #6b7280;
  --card-top: rgba(255,255,255,0.95); --card-bottom: rgba(246,248,252,0.9);
  --card-border: rgba(20,30,60,0.12); --accent: #5b6ef5; --accent-2: #8a5cf6;
  --row: rgba(255,255,255,0.7); --row-alt: rgba(240,243,250,0.7);
  --input-bg: #ffffff; --input-border: rgba(20,30,60,0.18);
  --shadow: rgba(20,30,60,0.12); --head: rgba(91,110,245,0.10); --fail: #c0392b;
}
body.omnia-dark {
  --bg-top: #1b1e27; --bg-bottom: #14161d; --fg: #e7e9f0; --muted: #9aa3b2;
  --card-top: rgba(46,51,66,0.7); --card-bottom: rgba(32,36,48,0.6);
  --card-border: rgba(255,255,255,0.10); --accent: #7c8cff; --accent-2: #a685ff;
  --row: rgba(46,51,66,0.45); --row-alt: rgba(36,40,52,0.45);
  --input-bg: rgba(20,22,30,0.7); --input-border: rgba(255,255,255,0.16);
  --shadow: rgba(0,0,0,0.45); --head: rgba(124,140,255,0.16); --fail: #ff6b6b;
}
body {
  color: var(--fg);
  background: linear-gradient(160deg, var(--bg-top), var(--bg-bottom));
}
.sn-shell { display: flex; flex-direction: column; height: 100%; padding: 0 16px 12px; }
.sn-header {
  padding: 18px 4px 10px;
  background: linear-gradient(160deg, var(--bg-top), var(--bg-bottom));
}
.sn-title {
  font-size: 21px; font-weight: 800; letter-spacing: 0.2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.sn-subtitle { color: var(--muted); margin-top: 3px; }
.sn-toolbar { display: flex; gap: 16px; padding: 6px 2px 12px; flex-wrap: wrap; }
.sn-field { display: flex; flex-direction: column; gap: 4px; }
.sn-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; color: var(--muted);
}
select, input[type="text"], textarea {
  font-family: inherit; font-size: 13px; color: var(--fg);
  background: var(--input-bg); border: 1px solid var(--input-border);
  border-radius: 8px; padding: 6px 8px; outline: none;
}
select:focus, input:focus, textarea:focus { border-color: var(--accent); }
#sn-note-type, #sn-base-field { min-width: 200px; }
.sn-table-wrap {
  flex: 1; min-height: 0; overflow: auto; border-radius: 12px;
  border: 1px solid var(--card-border); box-shadow: 0 1px 3px var(--shadow);
  background: linear-gradient(150deg, var(--card-top), var(--card-bottom));
}
.sn-table { width: 100%; border-collapse: collapse; }
.sn-table thead th {
  position: sticky; top: 0; z-index: 2; text-align: left;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--muted);
  padding: 9px 8px; background: var(--head); backdrop-filter: blur(4px);
  border-bottom: 1px solid var(--card-border);
}
.sn-table tbody tr { border-bottom: 1px solid var(--card-border); }
.sn-table tbody tr:nth-child(odd) { background: var(--row); }
.sn-table tbody tr:nth-child(even) { background: var(--row-alt); }
.sn-table td { padding: 7px 8px; vertical-align: top; }
.sn-fieldname { font-weight: 650; padding-top: 8px; }
.sn-prompt { width: 100%; min-width: 220px; min-height: 38px; resize: vertical; line-height: 1.4; }
.sn-prompt-cell { display: flex; gap: 6px; align-items: flex-start; }
.sn-lock {
  cursor: pointer; border: 1px solid var(--input-border); border-radius: 8px;
  background: var(--input-bg); font-size: 14px; padding: 5px 8px; line-height: 1;
  transition: border-color 0.15s ease, transform 0.1s ease;
}
.sn-lock:hover { border-color: var(--accent); }
.sn-lock:active { transform: scale(0.94); }
.sn-lock.sn-locked { border-color: var(--accent); background: var(--head); }
.sn-provider, .sn-model { min-width: 110px; }
.sn-check { width: 17px; height: 17px; accent-color: var(--accent); cursor: pointer; }
.sn-center { text-align: center; }
.sn-empty { padding: 22px; text-align: center; color: var(--muted); }
.sn-footer {
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px; padding-top: 12px; flex-wrap: wrap;
}
.sn-footer-left, .sn-footer-right { display: flex; gap: 10px; align-items: center; }
.sn-btn {
  border: 1px solid var(--card-border); border-radius: 9px; cursor: pointer;
  padding: 7px 14px; font-size: 13px; color: var(--fg); background: var(--card-top);
  transition: border-color 0.15s ease, transform 0.1s ease, opacity 0.15s ease;
}
.sn-btn:hover { border-color: var(--accent); }
.sn-btn:active { transform: scale(0.97); }
.sn-btn:disabled { opacity: 0.5; cursor: default; transform: none; }
.sn-btn-primary {
  color: #fff; border-color: transparent;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
}
.sn-btn-magic { border-color: var(--accent); }
.sn-msg { color: var(--muted); font-size: 12px; }
.sn-msg.sn-err { color: var(--fail); }
.sn-spin {
  display: inline-block; width: 13px; height: 13px; margin-right: 6px;
  border: 2px solid var(--muted); border-top-color: transparent; border-radius: 50%;
  vertical-align: -2px; animation: sn-rot 0.7s linear infinite;
}
@keyframes sn-rot { to { transform: rotate(360deg); } }
"""


# All page state lives in JS: it loads note types, renders rows from the ``load`` payload,
# reads them back into dicts for auto_smart/save, and posts each op through the bridge.
_JS = r"""
(function () {
  var noteTypeSel = document.getElementById("sn-note-type");
  var baseSel = document.getElementById("sn-base-field");
  var tbody = document.getElementById("sn-rows");
  var emptyEl = document.getElementById("sn-empty");
  var msgEl = document.getElementById("sn-msg");
  var autoBtn = document.getElementById("sn-auto");
  var saveBtn = document.getElementById("sn-save");
  var providers = [];

  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({ plugin: "smart_notes", op: op, data: data }), cb);
  }
  function setMsg(text, isErr) {
    msgEl.className = "sn-msg" + (isErr ? " sn-err" : "");
    msgEl.innerHTML = text || "";
  }
  function opt(value, label, selected) {
    var o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }
  function fill(select, values, current) {
    select.innerHTML = "";
    values.forEach(function (v) { select.appendChild(opt(v, v, v === current)); });
  }

  function makeCheckbox(cls, checked) {
    var c = document.createElement("input");
    c.type = "checkbox"; c.className = "sn-check " + cls; c.checked = !!checked;
    return c;
  }
  function makeSelect(cls, values, current) {
    var s = document.createElement("select");
    s.className = cls;
    values.forEach(function (v) {
      s.appendChild(opt(v, v === "" ? "(inherit)" : v, v === current));
    });
    return s;
  }
  function makeInput(cls, value, placeholder) {
    var i = document.createElement("input");
    i.type = "text"; i.className = cls; i.value = value || "";
    if (placeholder) i.placeholder = placeholder;
    return i;
  }

  function renderRow(row) {
    var tr = document.createElement("tr");
    tr.setAttribute("data-field", row.field);

    var tdName = document.createElement("td");
    tdName.className = "sn-fieldname"; tdName.textContent = row.field;
    tr.appendChild(tdName);

    var tdOn = document.createElement("td"); tdOn.className = "sn-center";
    tdOn.appendChild(makeCheckbox("sn-enabled", row.enabled));
    tr.appendChild(tdOn);

    var tdType = document.createElement("td");
    tdType.appendChild(makeSelect("sn-type", SN_TYPES, row.type || "text"));
    tr.appendChild(tdType);

    var tdPrompt = document.createElement("td");
    var wrap = document.createElement("div"); wrap.className = "sn-prompt-cell";
    var ta = document.createElement("textarea");
    ta.className = "sn-prompt"; ta.value = row.prompt || "";
    ta.placeholder = "e.g. Give the IPA for {{" + (baseSel.value || "Base") + "}}";
    var lock = document.createElement("button");
    lock.type = "button"; lock.className = "sn-lock" + (row.prompt_locked ? " sn-locked" : "");
    lock.textContent = row.prompt_locked ? "🔒" : "🔓";
    lock.title = "Lock to protect this prompt/type from Auto-smart";
    lock.addEventListener("click", function () {
      var locked = lock.classList.toggle("sn-locked");
      lock.textContent = locked ? "🔒" : "🔓";
    });
    wrap.appendChild(ta); wrap.appendChild(lock);
    tdPrompt.appendChild(wrap);
    tr.appendChild(tdPrompt);

    var providerVals = [""].concat(providers);
    var tdProvider = document.createElement("td");
    tdProvider.appendChild(makeSelect("sn-provider", providerVals, row.provider || ""));
    tr.appendChild(tdProvider);

    var tdModel = document.createElement("td");
    tdModel.appendChild(makeInput("sn-model", row.model, "(inherit)"));
    tr.appendChild(tdModel);

    var tdOver = document.createElement("td"); tdOver.className = "sn-center";
    tdOver.appendChild(makeCheckbox("sn-overwrite", row.overwrite));
    tr.appendChild(tdOver);

    return tr;
  }

  function renderRows(rows) {
    tbody.innerHTML = "";
    rows.forEach(function (row) { tbody.appendChild(renderRow(row)); });
    emptyEl.textContent = rows.length
      ? ""
      : "This note type has no other fields to generate. Add one with “+ Create field”.";
  }

  function collectRows() {
    var rows = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      var lock = tr.querySelector(".sn-lock");
      rows.push({
        field: tr.getAttribute("data-field"),
        enabled: tr.querySelector(".sn-enabled").checked,
        type: tr.querySelector(".sn-type").value,
        prompt: tr.querySelector(".sn-prompt").value,
        prompt_locked: lock.classList.contains("sn-locked"),
        provider: tr.querySelector(".sn-provider").value,
        model: tr.querySelector(".sn-model").value,
        voice: tr.getAttribute("data-voice") || "",
        overwrite: tr.querySelector(".sn-overwrite").checked
      });
    });
    return rows;
  }

  function applyLoad(res) {
    if (!res) return;
    providers = res.providers || [];
    fill(baseSel, res.all_fields || [], res.base_field || "");
    renderRows(res.rows || []);
    setMsg("");
  }

  function loadNoteType() {
    var nt = noteTypeSel.value;
    if (!nt) { renderRows([]); return; }
    send("load", { note_type: nt }, applyLoad);
  }

  noteTypeSel.addEventListener("change", loadNoteType);
  baseSel.addEventListener("change", function () {
    send("set_base_field", { note_type: noteTypeSel.value, base_field: baseSel.value }, applyLoad);
  });

  document.getElementById("sn-create").addEventListener("click", function () {
    var name = window.prompt("New field name:");
    if (!name) return;
    send("create_field", { note_type: noteTypeSel.value, field_name: name }, function (res) {
      if (res && res.all_fields) {
        fill(baseSel, res.all_fields, baseSel.value);
        send("set_base_field", { note_type: noteTypeSel.value, base_field: baseSel.value }, applyLoad);
      } else if (res && res.error) {
        setMsg(res.error, true);
      }
    });
  });

  // Auto-smart runs the LLM OFF the Qt main thread, so its result can't come back through the
  // synchronous bridge callback. The handler kicks the work off (returning immediately) and
  // pushes the outcome back by calling window.__snAutoResult — leaving the button disabled and
  // the spinner up until then.
  window.__snAutoResult = function (res) {
    autoBtn.disabled = false;
    if (res && res.rows) {
      renderRows(res.rows);
      setMsg("Auto-smart filled in the enabled, unlocked fields.", false);
    } else {
      setMsg((res && res.error) || "Auto-smart failed — see logs.", true);
    }
  };
  autoBtn.addEventListener("click", function () {
    autoBtn.disabled = true;
    setMsg('<span class="sn-spin"></span>Auto-smart is writing prompts…', false);
    send("auto_smart", {
      note_type: noteTypeSel.value,
      base_field: baseSel.value,
      rows: collectRows()
    }, null);
  });

  saveBtn.addEventListener("click", function () {
    saveBtn.disabled = true;
    send("save", {
      note_type: noteTypeSel.value,
      base_field: baseSel.value,
      rows: collectRows()
    }, function (res) {
      saveBtn.disabled = false;
      if (res && res.ok) {
        setMsg("Saved.", false);
      } else {
        setMsg((res && res.error) || "Could not save — see logs.", true);
      }
    });
  });

  document.getElementById("sn-cancel").addEventListener("click", function () {
    send("cancel", {}, null);
  });

  // Initial state is baked into window.__SN_INIT (server-side) so the page renders populated
  // without an init pycmd callback — Anki's bridge callback channel isn't ready the instant
  // this inline script runs. Fall back to a live list_note_types only if nothing was baked.
  var INIT = window.__SN_INIT || null;
  if (INIT && INIT.note_types && INIT.note_types.length) {
    fill(noteTypeSel, INIT.note_types, INIT.note_type || INIT.note_types[0] || "");
    applyLoad(INIT);
  } else {
    send("list_note_types", {}, function (names) {
      fill(noteTypeSel, names || [], (names && names[0]) || "");
      loadNoteType();
    });
  }
})();
"""
