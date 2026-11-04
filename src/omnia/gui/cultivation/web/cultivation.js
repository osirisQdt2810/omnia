(function () {
  "use strict";

  // ---------------------------------------------------------------------------------------
  // Bridge
  // ---------------------------------------------------------------------------------------
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "cultivation", op: op, data: data || {}}), cb);
  }

  const $ = (id) => document.getElementById(id);

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function num(value, digits) {
    if (value == null || isNaN(value)) return "—";
    return Number(value).toLocaleString("en-US", {maximumFractionDigits: digits || 0});
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
  // Status cards: the score is the headline, the level follows it both ways
  // ---------------------------------------------------------------------------------------
  function header(card, label, unit) {
    const pct = Math.round((card.progress || 0) * 100);
    const toward = card.next_realm
      ? "→ " + esc(card.next_label) + " · " + esc(card.next_realm) + " at <b>" + num(card.next_threshold) + "</b>"
      : "the highest level";
    return '<div class="cv-card-top">' +
      '<div class="cv-domain">' + label + " · since " + esc(card.start) + "</div>" +
      '<div class="cv-realm">' + esc(card.label) + " · " + esc(card.realm) + "<small>L" + card.level + "</small></div>" +
      '<div class="cv-power"><b>' + num(card.score) + "</b> " + unit + " " + toward + "</div>" +
      '<div class="cv-bar"><span style="width:' + pct + '%"></span></div>' +
      '<div class="cv-bar-label"><span>' + pct + "% of the way</span><span>" +
      (card.to_next != null ? num(card.to_next) + " to go" : "") + "</span></div></div>";
  }

  function stat(label, value, note) {
    return '<div class="cv-stat"><div class="cv-stat-label">' + label + '</div><div class="cv-stat-value">' +
      value + "</div>" + (note ? '<div class="cv-stat-note">' + note + "</div>" : "") + "</div>";
  }

  function streakStat(card) {
    const s = card.streak || {};
    return stat("Streak", num(s.days) + (s.active_today ? " ✓" : ""),
      s.freeze_ready ? "1 missed day forgiven per week" : "this week's free miss is used");
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
    el.innerHTML = header(card, "📘 English · vocabulary", "words") + '<div class="cv-card-body">' +
      '<div class="cv-stats">' +
      stat("Today", signed(card.gain, 1), num(h.new_today) + " new · " + num(h.reviews_today) + " reviews · " + num(h.minutes_today) + " min") +
      stat("Retention 30d", retention, bandNote) +
      streakStat(card) +
      stat("Lifetime", num(card.lifetime), "words gained, never decreases") +
      stat("Words", num(card.counted_words), "reviewed since the start, of " + num(card.words)) +
      stat("Overdue", num(h.overdue), "review cards") +
      "</div>" +
      chart(card.history, "var(--anki-from)", card.next_threshold) +
      '<div class="cv-line">A word counts once you review it on or after the start day. CEFR here is the ' +
      "vocabulary side of the scale only.</div>" +
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
    el.innerHTML = header(card, "🧗 DSA · Elo rating", "rating") + '<div class="cv-card-body">' +
      '<div class="cv-stats">' +
      stat("Today", signed(card.gain, 1), card.last_attempt ? "last attempt " + esc(card.last_attempt) : "no attempt yet") +
      stat("Peak", num(card.rating_peak), "neglect never takes more than half") +
      streakStat(card) +
      stat("Lifetime", num(card.lifetime), "rating gained, never decreases") +
      stat("Attempts", num(card.attempts), num(card.unknown) + " without @status") +
      stat("Debts", num(openDebts), openDebts ? "copied, not yet re-solved" : "none open") +
      "</div>" +
      chart(card.history, "var(--dsa-from)", card.next_threshold) +
      '<div class="cv-line">Feed from ' + esc(card.generated_at || "?") + ".</div></div>";
    renderDsaDetail(card);
  }

  function renderDsaDetail(card) {
    const debts = (card.debts || []).map((d) => "<tr><td>" + d.lc_id + "</td><td>" + esc(d.title || "") +
      "</td><td>" + esc(d.opened) + '</td><td class="cv-num">' + (d.warn ? '<span class="cv-pill cv-pill-bad">' + d.days + " d</span>" : d.days + " d") +
      "</td></tr>").join("");
    const recent = (card.recent || []).map((a) => "<tr><td>" + esc(a.day) + "</td><td>" + a.lc_id + ". " +
      esc(a.title || "") + "</td><td>" + esc(a.status) + '</td><td class="cv-num">' + (a.rating == null ? "—" : a.rating) +
      '</td><td class="cv-num">' + signed(a.delta, 1) + '</td><td class="cv-muted">' + esc(a.note) + "</td></tr>").join("");
    $("cv-dsa-detail").innerHTML =
      '<div class="cv-box"><h2>Debts (tâm ma)</h2>' + (debts
        ? '<table class="cv-table"><tr><th>LC</th><th>Title</th><th>Opened</th><th class="cv-num">Open for</th></tr>' + debts + "</table>" +
          '<div class="cv-line">A debt closes when you solve the problem again yourself (self or hint) at least a week later.</div>'
        : '<div class="cv-line">No open debts.</div>') + "</div>" +
      '<div class="cv-box"><h2>Latest attempts</h2>' + (recent
        ? '<table class="cv-table"><tr><th>Day</th><th>Problem</th><th>Status</th>' +
          '<th class="cv-num">Problem rating</th><th class="cv-num">Your change</th><th>Note</th></tr>' + recent + "</table>"
        : '<div class="cv-line">No attempts since the start day yet.</div>') + "</div>";
  }

  // ---------------------------------------------------------------------------------------
  // Setup tab (start day, decks, word fields, template modalities)
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
      '</td><td><select class="cv-word" data-nt="' + esc(n.name) + '">' + options(n.fields, n.word_field) + "</select></td></tr>").join("");
    const tmpls = setup.templates.map((t) => "<tr><td>" + esc(t.note_type) + "</td><td>" + esc(t.template) +
      (t.typed ? ' <span class="cv-pill cv-pill-gold">typed</span>' : "") + '</td><td class="cv-num">' + num(t.cards) +
      '</td><td><select class="cv-mod" data-key="' + esc(t.key) + '">' +
      MODALITIES.map((m) => '<option value="' + m[0] + '"' + (m[0] === t.modality ? " selected" : "") + ">" + m[1] +
        (m[0] === t.suggested ? " ★" : "") + "</option>").join("") + "</select>" +
      (t.confirmed ? "" : ' <span class="cv-pill cv-pill-warn">suggested</span>') + "</td></tr>").join("");
    $("cv-setup").innerHTML =
      '<div class="cv-box"><h2>Counting since ' + esc(card.start) + "</h2>" +
      '<div class="cv-line">Only words reviewed and problems solved from this day on count. Starting over forgets the ' +
      "recorded days and counts from today; your cards and solutions are not touched.</div>" +
      '<div class="cv-setup-foot"><button class="cv-btn cv-btn-quiet" id="cv-start-over">Start over from today</button></div></div>' +
      '<div class="cv-setup-grid"><div class="cv-box"><h2>Vocabulary decks</h2>' +
      '<div class="cv-line">Only cards in these decks (and their subdecks) count. None ticked = every deck.</div>' +
      '<div class="cv-deck-list">' + decks + "</div></div>" +
      '<div class="cv-box"><h2>Word field</h2><div class="cv-line">One note = one word. ' +
      "Notes with the same word are merged.</div>" +
      '<table class="cv-table"><tr><th>Note type</th><th>Word</th></tr>' + types + "</table></div></div>" +
      '<div class="cv-box"><h2>Card templates</h2><div class="cv-line">How strong a card\'s evidence is: typing the word ' +
      "beats recalling it, which beats recognising it. ★ marks the suggestion.</div>" +
      '<table class="cv-table"><tr><th>Note type</th><th>Template</th><th class="cv-num">Cards</th><th>Modality</th></tr>' + tmpls +
      '</table></div><div class="cv-setup-foot"><span class="cv-muted" id="cv-setup-msg"></span>' +
      '<button class="cv-btn" id="cv-setup-save">Save and recompute</button></div>';
    $("cv-setup-save").addEventListener("click", saveSetup);
    $("cv-start-over").addEventListener("click", startOver);
  }

  function saveSetup() {
    const values = {decks: [], word_fields: {}, modality: {}};
    document.querySelectorAll(".cv-deck").forEach((c) => { if (c.checked) values.decks.push(c.value); });
    document.querySelectorAll(".cv-word").forEach((s) => { values.word_fields[s.dataset.nt] = s.value; });
    document.querySelectorAll(".cv-mod").forEach((s) => { values.modality[s.dataset.key] = s.value; });
    $("cv-setup-save").disabled = true;
    send("save_setup", values, () => {
      $("cv-setup-msg").textContent = "Saved — recomputing…";
      setTimeout(() => { const b = $("cv-setup-save"); if (b) b.disabled = false; }, 1200);
    });
  }

  function startOver() {
    const btn = $("cv-start-over");
    // Two clicks rather than a native confirm(): the webview has no dialog chrome of its own.
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "Click again to forget the history and count from today";
      setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = "Start over from today"; } }, 4000);
      return;
    }
    btn.disabled = true;
    send("start_over", {}, () => toast("Counting from today"));
  }

  // ---------------------------------------------------------------------------------------
  // Entry points for Python
  // ---------------------------------------------------------------------------------------
  window.cultivation = {
    update: function (domain, card) {
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
