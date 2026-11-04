(function () {
  "use strict";

  // ---------------------------------------------------------------------------------------
  // Bridge
  // ---------------------------------------------------------------------------------------
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "cultivation", op: op, data: data || {}}), cb);
  }

  const $ = (id) => document.getElementById(id);
  const state = {anki: null, dsa: null, trial: null};

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function num(value, digits) {
    if (value == null || isNaN(value)) return "—";
    return Number(value).toLocaleString("en-US", {maximumFractionDigits: digits || 0, minimumFractionDigits: 0});
  }
  function signed(value, digits) {
    if (value == null || isNaN(value)) return '<span class="cv-muted">—</span>';
    const cls = value > 0 ? "cv-pos" : value < 0 ? "cv-neg" : "";
    return '<span class="' + cls + '">' + (value > 0 ? "+" : "") + num(value, digits) + "</span>";
  }
  function toast(text) {
    const el = $("cv-toast");
    el.textContent = text;
    el.classList.remove("cv-hidden");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.add("cv-hidden"), 2200);
  }

  // ---------------------------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------------------------
  document.querySelectorAll(".cv-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".cv-tab").forEach((t) => t.classList.toggle("cv-tab-on", t === tab));
      document.querySelectorAll(".cv-panel").forEach((p) =>
        p.classList.toggle("cv-hidden", p.dataset.panel !== tab.dataset.tab));
    });
  });
  $("cv-refresh").addEventListener("click", () => {
    $("cv-refresh").disabled = true;
    send("refresh", {}, () => setTimeout(() => { $("cv-refresh").disabled = false; }, 800));
  });

  // ---------------------------------------------------------------------------------------
  // The history chart (inline SVG — no external assets under the webview's CSP)
  // ---------------------------------------------------------------------------------------
  function chart(points, color, threshold) {
    if (!points || points.length < 2) {
      return '<div class="cv-line">The chart fills in as days are recorded.</div>';
    }
    const W = 320, H = 70, pad = 4;
    const values = points.map((p) => p.value);
    let lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    if (threshold != null) { hi = Math.max(hi, threshold); lo = Math.min(lo, threshold); }
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const x = (i) => pad + (i * (W - 2 * pad)) / (points.length - 1);
    const y = (v) => H - pad - ((v - lo) * (H - 2 * pad)) / (hi - lo);
    let line = "";
    points.forEach((p, i) => { line += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.value).toFixed(1); });
    const area = line + "L" + x(points.length - 1).toFixed(1) + " " + (H - pad) + "L" + pad + " " + (H - pad) + "Z";
    const guide = threshold != null
      ? '<line x1="0" x2="' + W + '" y1="' + y(threshold).toFixed(1) + '" y2="' + y(threshold).toFixed(1) + '"></line>'
      : "";
    return '<svg class="cv-chart" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none">' + guide +
      '<path class="cv-area" fill="' + color + '" d="' + area + '"></path>' +
      '<path class="cv-stroke" stroke="' + color + '" d="' + line + '"></path></svg>' +
      '<div class="cv-bar-label cv-muted"><span>' + esc(points[0].day) + "</span><span>" +
      esc(points[points.length - 1].day) + "</span></div>";
  }

  // ---------------------------------------------------------------------------------------
  // Status cards
  // ---------------------------------------------------------------------------------------
  function header(card, label) {
    const pct = Math.round((card.progress || 0) * 100);
    const toward = card.next_name
      ? "→ " + esc(card.next_name) + " at <b>" + num(card.next_threshold) + "</b>"
      : "the highest realm";
    return '<div class="cv-card-top">' +
      '<div class="cv-domain">' + label + (card.provisional ? " · provisional" : "") + "</div>" +
      '<div class="cv-realm">' + esc(card.realm_name) + "<small>L" + card.realm + "</small></div>" +
      '<div class="cv-power">Tu vi <b>' + num(card.power, card.domain === "dsa" ? 0 : 0) + "</b> " + toward + "</div>" +
      '<div class="cv-bar"><span style="width:' + pct + '%"></span></div>' +
      '<div class="cv-bar-label"><span>' + pct + "% of this realm</span><span>" +
      (card.to_next != null ? num(card.to_next) + " to go" : "") + "</span></div></div>";
  }

  function statusLine(card) {
    const parts = [];
    if (card.status === "unstable") {
      parts.push('<div class="cv-warn-box"><b>Bất ổn</b> — below this realm\'s threshold. It drops in ' +
        card.days_until_demotion + " day(s) unless Tu vi recovers.</div>");
    }
    return parts.join("");
  }

  function streakStat(card) {
    const s = card.streak || {};
    return stat("Streak", num(s.days) + (s.active_today ? " ✓" : ""),
      s.freeze_ready ? "freeze ready" : "freeze used this week");
  }

  function stat(label, value, note) {
    return '<div class="cv-stat"><div class="cv-stat-label">' + label + '</div><div class="cv-stat-value">' +
      value + "</div>" + (note ? '<div class="cv-stat-note">' + note + "</div>" : "") + "</div>";
  }

  function trialRow(card, domain) {
    const t = card.trial || {};
    if (t.running) {
      const r = t.running;
      const what = domain === "anki"
        ? r.answered + " / " + r.prompts.length + " answered"
        : r.cleared + " / " + r.items.length + " cleared · until " + esc(r.deadline);
      return '<div class="cv-trial-row cv-eligible"><span><b>Thiên kiếp → ' + esc(r.target_name) +
        "</b> in progress · " + what + '</span><button class="cv-btn cv-btn-small" data-act="' +
        (domain === "anki" ? "anki-resume" : "dsa-tab") + '">' + (domain === "anki" ? "Continue" : "Open") + "</button></div>";
    }
    if (card.status === "eligible") {
      return '<div class="cv-trial-row cv-eligible"><span><b>Thiên kiếp ready</b> — pass it to enter ' +
        esc(card.next_name) + '.</span><button class="cv-btn cv-btn-small" data-act="' + domain +
        '-start">Begin trial</button></div>';
    }
    let text;
    if (card.status === "cooldown") {
      text = "Thiên kiếp: cooling down after a failed trial — next attempt from " + esc(card.cooldown_until) + ".";
    } else if (card.status === "peak") {
      text = "Chân Tiên — there is no realm above.";
    } else if (card.next_realm && card.next_realm <= card.peak_realm) {
      text = "Re-entering " + esc(card.next_name) + " needs no trial: you held it before. " + num(card.to_next) + " to go.";
    } else {
      text = "Thiên kiếp: not eligible yet (need " + num(card.to_next) + " more).";
    }
    let last = "";
    if (t.last && t.last.passed === false && t.last.search) {
      last = '<div class="cv-line">Missed words from the last trial: <span class="cv-mono">' +
        esc(t.last.search.slice(0, 80)) + (t.last.search.length > 80 ? "…" : "") +
        '</span> <button class="cv-btn cv-btn-quiet cv-btn-small" data-copy="' + esc(t.last.search) +
        '">Copy search</button></div>';
    }
    return '<div class="cv-trial-row"><span class="cv-muted">' + text + "</span></div>" + last;
  }

  function renderAnki(card) {
    const el = $("cv-anki");
    el.className = "cv-card cv-anki";
    if (card.missing) {
      el.innerHTML = '<div class="cv-loading">Could not read the collection: ' + esc(card.error) + "</div>";
      return;
    }
    const h = card.health || {};
    const band = card.retention_band;
    const retention = h.retention == null ? "—" : num(h.retention * 100, 1) + "%";
    const bandNote = band === "ok" ? '<span class="cv-pill cv-pill-ok">healthy</span>'
      : band === "low" ? '<span class="cv-pill cv-pill-bad">too low</span>'
      : band === "high" ? '<span class="cv-pill cv-pill-warn">could go faster</span>' : "no reviews yet";
    const warnings = [];
    if (band === "low") warnings.push("Retention under 80%: too many new cards, or reviews are too late.");
    if (band === "high") warnings.push("Retention over 95%: you could learn faster (FSRS aims for 90%).");
    if (card.without_fsrs) {
      warnings.push(num(card.without_fsrs) + " reviewed cards have no FSRS memory state, so their interval stands in for stability. " +
        "Deck options → FSRS → enable, then Optimize.");
    }
    el.innerHTML = header(card, "📘 Anki · vocabulary") + '<div class="cv-card-body">' + statusLine(card) +
      '<div class="cv-stats">' +
      stat("Today", signed(card.gain, 1), num(h.new_today) + " new · " + num(h.reviews_today) + " reviews · " + num(h.minutes_today) + " min") +
      stat("Retention 30d", retention, bandNote) +
      streakStat(card) +
      stat("Lifetime", num(card.lifetime), "never decreases") +
      stat("Words", num(card.reviewed_words), "reviewed of " + num(card.words)) +
      stat("Overdue", num(h.overdue), "review cards") +
      "</div>" + trialRow(card, "anki") +
      chart(card.history, "var(--anki-from)", card.next_threshold) +
      '<div class="cv-line">Counts Anki-tracked words only — no vocabulary-size test baseline yet.</div>' +
      warnings.map((w) => '<div class="cv-warn-box">' + esc(w) + "</div>").join("") +
      "</div>";
  }

  function renderDsa(card) {
    const el = $("cv-dsa");
    el.className = "cv-card cv-dsa";
    if (card.missing) {
      el.innerHTML = '<div class="cv-card-top"><div class="cv-domain">🧗 DSA · dsa-journey</div>' +
        '<div class="cv-realm">No feed yet</div></div><div class="cv-card-body"><div class="cv-line">' +
        (card.error ? "The feed could not be read: " + esc(card.error) + "<br>" : "") +
        "Run <b>/commit-progress</b> in dsa-journey (its last step writes the feed), or " +
        "<span class=\"cv-mono\">python3 scripts/cultivation_feed.py</span> there.<br>Expected at " +
        '<span class="cv-mono">' + esc(card.feed_path) + "</span></div></div>";
      $("cv-dsa-detail").innerHTML = el.innerHTML;
      return;
    }
    const openDebts = (card.debts || []).length;
    el.innerHTML = header(card, "🧗 DSA · Elo θ") + '<div class="cv-card-body">' + statusLine(card) +
      '<div class="cv-stats">' +
      stat("Today", signed(card.gain, 1), card.last_attempt ? "last attempt " + esc(card.last_attempt) : "") +
      stat("Peak θ", num(card.theta_peak), "keeps half of all gains") +
      streakStat(card) +
      stat("Lifetime", num(card.lifetime), "Σ of rising days") +
      stat("Attempts", num(card.attempts), num(card.unknown) + " without @status") +
      stat("Debts", num(openDebts), openDebts ? "copied, not yet re-solved" : "none open") +
      "</div>" + trialRow(card, "dsa") +
      chart(card.history, "var(--dsa-from)", card.next_threshold) +
      '<div class="cv-line">Feed from ' + esc(card.generated_at || "?") + ".</div></div>";
    renderDsaDetail(card);
  }

  function renderDsaDetail(card) {
    const t = card.trial || {};
    let trial = "";
    if (t.running) {
      const r = t.running;
      trial = '<div class="cv-box"><h2>Thiên kiếp → ' + esc(r.target_name) + "</h2>" +
        '<div class="cv-line">Clear <b>' + r.pass_mark + " of " + r.items.length + "</b> by <b>" + esc(r.deadline) +
        "</b>. A problem counts with <span class=\"cv-mono\">@status: self</span> and an <span class=\"cv-mono\">@time</span> within its limit. " +
        "Solve them in order; the window updates after each /commit-progress. Seed " + r.seed + ".</div>" +
        '<table class="cv-table"><tr><th>#</th><th>Problem</th><th class="cv-num">Rating</th><th>Contest</th><th>State</th></tr>' +
        r.items.map((p, i) => "<tr><td>" + (i + 1) + '</td><td><span class="cv-link" data-url="' + esc(p.url) + '">' +
          p.id + ". " + esc(p.title) + '</span></td><td class="cv-num">' + p.rating + "</td><td>" + esc(p.contest) +
          "</td><td>" + (p.solved_on ? '<span class="cv-pill cv-pill-ok">cleared ' + esc(p.solved_on) + "</span>"
            : (p.note ? '<span class="cv-pill cv-pill-warn">' + esc(p.note) + "</span>" : '<span class="cv-muted">open</span>')) +
          "</td></tr>").join("") + "</table>" +
        '<div class="cv-setup-foot"><button class="cv-btn cv-btn-quiet cv-btn-small" data-act="dsa-abandon" data-id="' +
        r.id + '">Give up (counts as a fail)</button></div></div>';
    }
    const debts = (card.debts || []).map((d) => "<tr><td>" + d.lc_id + "</td><td>" + esc(d.title || "") +
      "</td><td>" + esc(d.opened) + '</td><td class="cv-num">' + (d.warn ? '<span class="cv-pill cv-pill-bad">' + d.days + " d</span>" : d.days + " d") +
      "</td></tr>").join("");
    const recent = (card.recent || []).map((a) => "<tr><td>" + esc(a.day) + "</td><td>" + a.lc_id + ". " +
      esc(a.title || "") + "</td><td>" + esc(a.status) + '</td><td class="cv-num">' + (a.rating == null ? "—" : a.rating) +
      '</td><td class="cv-num">' + signed(a.delta, 1) + '</td><td class="cv-muted">' + esc(a.note) + "</td></tr>").join("");
    $("cv-dsa-detail").innerHTML = trial +
      '<div class="cv-box"><h2>Debts (tâm ma)</h2>' + (debts
        ? '<table class="cv-table"><tr><th>LC</th><th>Title</th><th>Opened</th><th class="cv-num">Open for</th></tr>' + debts + "</table>" +
          '<div class="cv-line">A debt closes when you solve the problem again yourself (self or hint) at least a week later.</div>'
        : '<div class="cv-line">No open debts.</div>') + "</div>" +
      '<div class="cv-box"><h2>Latest attempts</h2><table class="cv-table"><tr><th>Day</th><th>Problem</th><th>Status</th>' +
      '<th class="cv-num">Rating</th><th class="cv-num">Δθ</th><th>Note</th></tr>' + recent + "</table></div>";
  }

  // ---------------------------------------------------------------------------------------
  // Setup tab (decks, word/meaning fields, template modalities)
  // ---------------------------------------------------------------------------------------
  const MODALITIES = [
    ["recognition", "Recognition (choose among options)"],
    ["passive_recall", "Passive recall (word → meaning)"],
    ["active_recall", "Active recall (meaning → word)"],
    ["active_recall_typed", "Typed active recall"],
  ];

  function renderSetup(card) {
    const setup = card.setup;
    if (!setup) return;
    const chosen = new Set(setup.selected_decks || []);
    const decks = setup.decks.map((d) => '<label><input type="checkbox" class="cv-deck" value="' + esc(d) + '"' +
      (chosen.has(d) ? " checked" : "") + "> " + esc(d) + "</label>").join("");
    const options = (fields, value) => fields.map((f) => '<option value="' + esc(f) + '"' + (f === value ? " selected" : "") + ">" + esc(f) + "</option>").join("");
    const types = setup.note_types.map((n) => "<tr><td>" + esc(n.name) + (n.confirmed ? "" : ' <span class="cv-pill cv-pill-warn">suggested</span>') +
      '</td><td><select class="cv-word" data-nt="' + esc(n.name) + '">' + options(n.fields, n.word_field) + "</select></td>" +
      '<td><select class="cv-meaning" data-nt="' + esc(n.name) + '">' + options(n.fields, n.meaning_field) + "</select></td></tr>").join("");
    const tmpls = setup.templates.map((t) => "<tr><td>" + esc(t.note_type) + "</td><td>" + esc(t.template) +
      (t.typed ? ' <span class="cv-pill cv-pill-gold">typed</span>' : "") + '</td><td class="cv-num">' + num(t.cards) +
      '</td><td><select class="cv-mod" data-key="' + esc(t.key) + '">' +
      MODALITIES.map((m) => '<option value="' + m[0] + '"' + (m[0] === t.modality ? " selected" : "") + ">" + m[1] +
        (m[0] === t.suggested ? " ★" : "") + "</option>").join("") + "</select>" +
      (t.confirmed ? "" : ' <span class="cv-pill cv-pill-warn">suggested</span>') + "</td></tr>").join("");
    $("cv-setup").innerHTML =
      '<div class="cv-setup-grid"><div class="cv-box"><h2>Vocabulary decks</h2>' +
      '<div class="cv-line">Only cards in these decks (and their subdecks) count. None ticked = every deck.</div>' +
      '<div class="cv-deck-list">' + decks + "</div></div>" +
      '<div class="cv-box"><h2>Word and meaning fields</h2><div class="cv-line">One note = one word. ' +
      "Notes with the same word are merged; the meaning is what the trial shows or checks.</div>" +
      '<table class="cv-table"><tr><th>Note type</th><th>Word</th><th>Meaning</th></tr>' + types + "</table></div></div>" +
      '<div class="cv-box"><h2>Card templates</h2><div class="cv-line">How strong a card\'s evidence is: typing the word ' +
      "beats recalling it, which beats recognising it. ★ marks the suggestion.</div>" +
      '<table class="cv-table"><tr><th>Note type</th><th>Template</th><th class="cv-num">Cards</th><th>Modality</th></tr>' + tmpls +
      '</table></div><div class="cv-setup-foot"><span class="cv-muted" id="cv-setup-msg"></span>' +
      '<button class="cv-btn" id="cv-setup-save">Save and recompute</button></div>';
    $("cv-setup-save").addEventListener("click", saveSetup);
  }

  function saveSetup() {
    const values = {decks: [], word_fields: {}, meaning_fields: {}, modality: {}};
    document.querySelectorAll(".cv-deck").forEach((c) => { if (c.checked) values.decks.push(c.value); });
    document.querySelectorAll(".cv-word").forEach((s) => { values.word_fields[s.dataset.nt] = s.value; });
    document.querySelectorAll(".cv-meaning").forEach((s) => { values.meaning_fields[s.dataset.nt] = s.value; });
    document.querySelectorAll(".cv-mod").forEach((s) => { values.modality[s.dataset.key] = s.value; });
    $("cv-setup-save").disabled = true;
    send("save_setup", values, () => {
      $("cv-setup-msg").textContent = "Saved — recomputing…";
      setTimeout(() => { const b = $("cv-setup-save"); if (b) b.disabled = false; }, 1200);
    });
  }

  // ---------------------------------------------------------------------------------------
  // Anki trial (Thiên kiếp) — a quiz in a modal; answers are graded in Python
  // ---------------------------------------------------------------------------------------
  function openTrial(payload) {
    if (!payload || payload.error) { toast((payload && payload.error) || "Could not start the trial."); return; }
    state.trial = payload;
    $("cv-trial").classList.remove("cv-hidden");
    showQuestion();
  }

  function closeTrial() {
    state.trial = null;
    $("cv-trial").classList.add("cv-hidden");
  }

  function showQuestion() {
    const t = state.trial;
    const i = t.answered;
    if (i >= t.prompts.length) { finishTrial(); return; }
    const typed = t.mode === "typed";
    $("cv-trial-box").innerHTML =
      "<h2>Thiên kiếp → " + esc(t.target_name) + '</h2><div class="cv-line">' +
      (typed ? "Type the English word for this meaning." : "Recall the meaning, type it, then judge yourself honestly.") +
      " Pass mark " + t.pass_mark + " / " + t.prompts.length + ".</div>" +
      '<div class="cv-progress"><span style="width:' + Math.round((i / t.prompts.length) * 100) + '%"></span></div>' +
      '<div class="cv-q">' + esc(t.prompts[i].prompt) + "</div>" +
      '<input type="text" class="cv-q-input" id="cv-answer" autocomplete="off" spellcheck="false" placeholder="' +
      (typed ? "English word" : "Meaning") + '">' +
      '<div class="cv-feedback" id="cv-feedback"></div>' +
      '<div class="cv-q-foot"><span class="cv-muted">' + (i + 1) + " / " + t.prompts.length + " · " + t.correct + ' correct</span>' +
      '<span><button class="cv-btn cv-btn-quiet" id="cv-trial-later">Continue later</button> ' +
      '<button class="cv-btn" id="cv-submit">' + (typed ? "Check" : "Reveal") + "</button></span></div>";
    const input = $("cv-answer");
    input.focus();
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") $("cv-submit").click(); });
    $("cv-trial-later").addEventListener("click", closeTrial);
    $("cv-submit").addEventListener("click", () => submitAnswer(input.value));
  }

  function submitAnswer(response) {
    const t = state.trial;
    $("cv-submit").disabled = true;
    send("anki_trial_answer", {id: t.id, index: t.answered, response: response}, (res) => {
      if (!res || res.error) { toast((res && res.error) || "Could not record the answer."); return; }
      if (res.reveal !== undefined) { selfGrade(res, response); return; }
      recordResult(res);
    });
  }

  function selfGrade(res, response) {
    $("cv-feedback").innerHTML = 'Stored meaning: <b>' + esc(res.reveal) + "</b>";
    $("cv-submit").outerHTML = '<span><button class="cv-btn cv-btn-quiet" id="cv-no">I missed it</button> ' +
      '<button class="cv-btn" id="cv-yes">I knew it</button></span>';
    const grade = (ok) => send("anki_trial_answer",
      {id: state.trial.id, index: state.trial.answered, response: response, self_grade: ok}, recordResult);
    $("cv-yes").addEventListener("click", () => grade(true));
    $("cv-no").addEventListener("click", () => grade(false));
  }

  function recordResult(res) {
    if (!res || res.error) { toast((res && res.error) || "Could not record the answer."); return; }
    const t = state.trial;
    t.answered = res.answered;
    t.correct = res.score;
    const fb = $("cv-feedback");
    if (fb) {
      fb.innerHTML = res.correct ? '<span class="cv-pill cv-pill-ok">correct</span>'
        : '<span class="cv-pill cv-pill-bad">missed</span> <b>' + esc(res.expected) + "</b>";
    }
    setTimeout(showQuestion, res.correct ? 450 : 1400);
  }

  function finishTrial() {
    send("anki_trial_finish", {id: state.trial.id}, (res) => {
      if (!res || res.error) { toast((res && res.error) || "Could not finish the trial."); return; }
      $("cv-trial-box").innerHTML = "<h2>" + (res.passed ? "Đột phá! Breakthrough." : "The tribulation held.") + "</h2>" +
        '<div class="cv-q">' + res.score + " / " + res.total + "</div>" +
        '<div class="cv-line">' + (res.passed
          ? "You enter the next realm."
          : "Practise the missed words, then try again after the cooldown. Paste this search into the Browse window or a filtered deck:") +
        "</div>" + (res.search ? '<div class="cv-mono">' + esc(res.search) + '</div>' : "") +
        '<div class="cv-q-foot"><span></span><span>' + (res.search ? '<button class="cv-btn cv-btn-quiet" data-copy="' +
        esc(res.search) + '">Copy search</button> ' : "") + '<button class="cv-btn" id="cv-trial-close">Close</button></span></div>';
      $("cv-trial-close").addEventListener("click", closeTrial);
      state.trial = null;
    });
  }

  // ---------------------------------------------------------------------------------------
  // Delegated clicks
  // ---------------------------------------------------------------------------------------
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act], [data-copy], [data-url]");
    if (!el) return;
    if (el.dataset.copy) { send("copy", {text: el.dataset.copy}); toast("Copied"); return; }
    if (el.dataset.url) { send("open_url", {url: el.dataset.url}); return; }
    const act = el.dataset.act;
    if (act === "anki-start") send("anki_trial_start", {}, openTrial);
    else if (act === "anki-resume") openTrial(state.anki && state.anki.trial && state.anki.trial.running);
    else if (act === "dsa-start") {
      el.disabled = true;
      send("dsa_trial_start", {}, (res) => { if (res && res.error) toast(res.error); });
    } else if (act === "dsa-tab") document.querySelector('.cv-tab[data-tab="dsa"]').click();
    else if (act === "dsa-abandon") send("trial_abandon", {domain: "dsa", id: Number(el.dataset.id)});
  });

  // ---------------------------------------------------------------------------------------
  // Entry points for Python
  // ---------------------------------------------------------------------------------------
  window.cultivation = {
    update: function (domain, card) {
      state[domain] = card;
      if (domain === "anki") { renderAnki(card); renderSetup(card); } else { renderDsa(card); }
    },
  };

  // Anki injects `pycmd` after the page's own scripts have run, so the first request waits for
  // the bridge instead of throwing a ReferenceError and leaving both cards on "Reading…".
  function whenBridgeReady(start) {
    if (typeof pycmd === "function") { start(); return; }
    setTimeout(() => whenBridgeReady(start), 50);
  }
  whenBridgeReady(() => {
    send("refresh");
    // dsa-journey rewrites the feed after every /commit-progress; Python only answers when it changed.
    setInterval(() => send("poll"), 4000);
  });
})();
