
  function makeCardElement() {
    const tpl = window.__TA_HTML_TEMPLATE;
    if (!tpl) {
      dbg("[JSDBG] missing __TA_HTML_TEMPLATE");
      return null;
    }
    const wrap = document.createElement("div");
    wrap.innerHTML = tpl;
    return wrap.firstElementChild;
  }

  function adoptCardClass(cardEl) {
    const sample =
      document.querySelector("section") ||
      document.querySelector(".card") ||
      document.querySelector("[class*='card']");
    if (sample && sample.className) {
      const keep = new Set(cardEl.className.split(/\s+/).filter(Boolean));
      const add = sample.className.split(/\s+/).filter(Boolean);
      for (const c of add) keep.add(c);
      cardEl.className = Array.from(keep).join(" ");
    }
  }

  function ensureMounted() {
    // Remove duplicates if Anki / observer storms created multiple nodes with the same id.
    try {
      const all = Array.from(document.querySelectorAll("#ta-card"));
      if (all.length > 1) {
        for (let i = 1; i < all.length; i++) all[i].remove();
      }
    } catch (e) {}

    let card = document.getElementById("ta-card");

    // If the card exists but looks "corrupted" (children wiped by re-render), rebuild it.
    try {
      if (card) {
        const hasTitle = !!card.querySelector(".ta-title");
        const hasDonut = !!card.querySelector("#ta_donut_wrap");
        const hasStats = !!card.querySelector("#ta_good_val") && !!card.querySelector("#ta_total_val");
        if (!hasTitle || !hasDonut || !hasStats) {
          card.remove();
          card = null;
        }
      }
    } catch (e) {
      // If anything goes wrong during validation, force rebuild.
      try {
        if (card) card.remove();
      } catch (e2) {}
      card = null;
    }

    if (card) return card;

    const container = findGridContainer();
    if (!container) return null;

    card = makeCardElement();
    if (!card) return null;

    adoptCardClass(card);

    // Ensure the id is correct even if the template changes.
    try {
      card.id = "ta-card";
    } catch (e) {}

    card.style.width = "auto";
    card.style.maxWidth = "none";
    card.style.minWidth = "0";
    card.style.gridColumn = "span 1";

    // Guard to prevent observer self-trigger loops during insertion.
    window.__TA_MOUNTING = true;
    try {
      const firstChild = container.querySelector("section, div");
      if (firstChild) container.insertBefore(card, firstChild);
      else container.insertBefore(card, container.firstChild || null);
    } finally {
      window.__TA_MOUNTING = false;
    }

    dbg("[JSDBG] mounted ta-card into grid container");
    return card;
  }

  function arcPath(cx, cy, r, startAngle, endAngle) {
    const rad = (a) => (a * Math.PI) / 180;
    const x1 = cx + r * Math.cos(rad(startAngle));
    const y1 = cy + r * Math.sin(rad(startAngle));
    const x2 = cx + r * Math.cos(rad(endAngle));
    const y2 = cy + r * Math.sin(rad(endAngle));
    const large = endAngle - startAngle > 180 ? 1 : 0;
    return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
  }

  function renderDonut(svg, parts) {
    const total = parts.reduce((s, p) => s + (p.value || 0), 0);
    svg.innerHTML = "";

    const cx = 60, cy = 60, r = 46, stroke = 18;

    const base = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    base.setAttribute("cx", cx);
    base.setAttribute("cy", cy);
    base.setAttribute("r", r);
    base.setAttribute("fill", "none");
    base.setAttribute("stroke", "rgba(255,255,255,0.08)");
    base.setAttribute("stroke-width", stroke);
    svg.appendChild(base);

    if (!total) return;

    let angle = -90;
    for (const p of parts) {
      const v = p.value || 0;
      if (v <= 0) continue;
      const span = (v / total) * 360;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", arcPath(cx, cy, r, angle, angle + span));
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", p.color);
      path.setAttribute("stroke-width", stroke);
      path.setAttribute("stroke-linecap", "butt");
      svg.appendChild(path);
      angle += span;
    }
  }