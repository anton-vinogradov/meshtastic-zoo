/* meshtastic-zoo — рендер топологии в SVG.
   Единственный источник данных: data/live.json от сборщика
   (collector/scan.py), перечитывается раз в минуту. */
(function () {
  const W = 960, M = 18;
  const GAP = 26, DEF_H = { subnet: 150, world: 640 };
  const CARD = { w: 190, h: 64, r: 11 };
  const WCARD = { w: 200, h: 62, r: 11 };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  function render(D) {
    // Вертикальная раскладка: зоны стопкой в порядке из данных, число зон любое.
    // Высота зоны — z.h из данных, иначе дефолт по типу; высота холста считается сама.
    const zoneBoxes = {};
    let H = M;
    for (const z of D.zones) {
      const h = z.h ?? DEF_H[z.kind] ?? DEF_H.world;
      zoneBoxes[z.id] = { x: M, y: H, w: W - M * 2, h, ...z };
      H += h + GAP;
    }
    H += M - GAP;

    // Позиции нод
    const nodes = {};
    for (const n of D.nodes) {
      const z = zoneBoxes[n.zone];
      if (!z) continue;
      const world = z.kind === "world";
      const c = world ? WCARD : CARD;
      const cx = z.x + 40 + n.x * (z.w - 80);
      const cy = world ? z.y + 30 + (n.y ?? 0.5) * (z.h - 60) : z.y + z.h / 2;
      nodes[n.id] = { ...n, cx, cy, w: c.w, h: c.h, r: c.r, world };
    }

    // % от идеала по SNR и непрерывный цвет: 0% — красный (hue 0), 100% — зелёный (hue 140)
    const S = D.meta.snrScale;
    const pctOf = (snr) => Math.round(
      Math.min(1, Math.max(0, (snr - S.floor) / (S.ideal - S.floor))) * 100);
    const hue = (pct) => pct * 1.4;
    const colorOf = (l) => l.snr == null ? "#8a8a90" : `hsl(${hue(pctOf(l.snr))}, 62%, 55%)`;
    const fmtSnr = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v);

    // Точка на границе карточки по направлению к (tx,ty), с зазором
    function edgePoint(n, tx, ty, gap = 8) {
      const dx = tx - n.cx, dy = ty - n.cy;
      const sx = (n.w / 2 + gap) / Math.abs(dx || 1e-9);
      const sy = (n.h / 2 + gap) / Math.abs(dy || 1e-9);
      const s = Math.min(sx, sy);
      return [n.cx + dx * s, n.cy + dy * s];
    }

    let out = [];

    // Зоны
    for (const z of Object.values(zoneBoxes)) {
      const dash = z.kind === "world" ? 'stroke-dasharray="7 6"' : "";
      out.push(`<rect x="${z.x}" y="${z.y}" width="${z.w}" height="${z.h}" rx="16"
        fill="${z.kind === "world" ? "none" : "var(--zone-bg)"}" stroke="var(--zone-stroke)" ${dash}/>`);
      out.push(`<text x="${z.x + 22}" y="${z.y + 34}" fill="var(--text)"
        font-size="17" font-weight="700">${esc(z.label)}</text>`);
    }

    // Рёбра (под карточками); маркеры стрелок копятся сюда — свой цвет на каждое плечо.
    // Встречные плечи одной пары разносим перпендикулярно, чтобы не слипались.
    const edgeSvg = [], rfMarkers = [];
    const pairCount = {}, pairSeen = {};
    for (const l of D.links) {
      if (l.type !== "rf") continue;
      const k = [l.from, l.to].sort().join("|");
      pairCount[k] = (pairCount[k] || 0) + 1;
    }

    for (const l of D.links) {
      const a = nodes[l.from], b = nodes[l.to];
      if (!a || !b) continue;
      const cls = `edge e-${l.from} e-${l.to}`;

      if (l.type === "lan") {
        edgeSvg.push(`<line class="${cls}" x1="${a.cx}" y1="${a.cy}" x2="${b.cx}" y2="${b.cy}"
          stroke="var(--lan)" stroke-width="3"/>`);
        continue;
      }

      // RF: пунктир со стрелкой к услышавшей ноде, цвет = % от идеала, подпись = SNR
      const col = colorOf(l);
      const mid = `arr${rfMarkers.length}`;
      rfMarkers.push(`<marker id="${mid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7"
        markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${col}"/></marker>`);

      const k = [l.from, l.to].sort().join("|");
      const side = pairCount[k] > 1 ? ((pairSeen[k] = (pairSeen[k] || 0) + 1) === 1 ? 1 : -1) : 0;
      let [x1, y1] = edgePoint(a, b.cx, b.cy);
      let [x2, y2] = edgePoint(b, a.cx, a.cy, 14);
      if (side) {
        const len = Math.hypot(x2 - x1, y2 - y1) || 1;
        const ox = (-(y2 - y1) / len) * 6 * side, oy = ((x2 - x1) / len) * 6 * side;
        x1 += ox; y1 += oy; x2 += ox; y2 += oy;
      }
      const t = l.labelT ?? (side === 1 ? 0.42 : side === -1 ? 0.58 : 0.5);
      const lx = x1 + (x2 - x1) * t, ly = y1 + (y2 - y1) * t;
      const label = l.snr == null ? (l.note || "не изм.") : fmtSnr(l.snr);
      const tip = l.snr == null
        ? `${l.from} ↔ ${l.to}: не измерено`
        : `${l.from} → ${l.to}: SNR ${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}% от идеала`;
      edgeSvg.push(`<g class="${cls}"><title>${esc(tip)}</title>
        <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
          stroke="${col}" stroke-width="2" stroke-dasharray="6 6" marker-end="url(#${mid})"/>
        <text x="${lx + 10}" y="${ly - 8}" fill="${col}" font-size="14.5"
          font-weight="600" paint-order="stroke" stroke="var(--bg)" stroke-width="5">${esc(label)}</text></g>`);
    }
    out.push(...edgeSvg);

    // Карточки нод (поверх рёбер)
    for (const n of Object.values(nodes)) {
      const x = n.cx - n.w / 2, y = n.cy - n.h / 2;
      const fill = n.world ? "var(--world-card)" : "var(--card-fill)";
      const stroke = n.world ? "#3a3a3e" : "var(--card-stroke)";
      const subFill = n.world ? "var(--muted)" : "var(--card-sub)";
      out.push(`<g class="node n-${n.id}" data-id="${n.id}">
        ${n.hint ? `<title>${esc(n.hint)}</title>` : ""}
        <rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.r}"
          fill="${fill}" stroke="${stroke}" stroke-width="1.5"${n.mobile ? ' stroke-dasharray="7 5"' : ""}/>
        <text x="${n.cx}" y="${n.cy - 6}" text-anchor="middle" fill="var(--text)"
          font-size="17" font-weight="700">${esc(n.label)}</text>
        <text x="${n.cx}" y="${n.cy + 17}" text-anchor="middle" fill="${subFill}"
          font-size="13">${esc(n.sub)}</text>
      </g>`);
    }

    document.getElementById("map").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img"
        aria-label="Карта mesh-сети"><defs>${rfMarkers.join("")}</defs>${out.join("\n")}</svg>`;

    // Наведение на ноду — подсветить её линки и соседей
    const svgEl = document.querySelector("#map svg");
    for (const g of svgEl.querySelectorAll(".node")) {
      g.addEventListener("mouseenter", () => {
        const id = g.dataset.id;
        svgEl.classList.add("focus");
        g.classList.add("lit");
        for (const e of svgEl.querySelectorAll(`.e-${CSS.escape(id)}`)) e.classList.add("lit");
        for (const l of D.links) {
          if (l.from === id) svgEl.querySelector(`.n-${CSS.escape(l.to)}`)?.classList.add("lit");
          if (l.to === id) svgEl.querySelector(`.n-${CSS.escape(l.from)}`)?.classList.add("lit");
        }
      });
      g.addEventListener("mouseleave", () => {
        svgEl.classList.remove("focus");
        for (const e of svgEl.querySelectorAll(".lit")) e.classList.remove("lit");
      });
    }

    // Легенда: градиент «% от идеала» + прочее + источник данных
    const grad = `linear-gradient(90deg, ${[0, 25, 50, 75, 100]
      .map(p => `hsl(${hue(p)}, 62%, 55%)`).join(", ")})`;
    const stale = D.meta.updatedTs && Date.now() - D.meta.updatedTs > 15 * 60e3;
    document.getElementById("legend").innerHTML = `
      <span class="item">0%<span class="grad" style="background:${grad}"></span>
        100% от идеала (SNR ${fmtSnr(S.floor)}…${fmtSnr(S.ideal)} dB)</span>
      <span class="item"><span class="swatch dashed" style="border-color:#8a8a90"></span>не измерено</span>
      <span class="item"><span class="swatch" style="border-color:var(--lan)"></span>LAN</span>
      <span class="item">скан · ${esc(D.meta.updated)}
        ${stale ? '<b style="color:#e0a03c">· устарело!</b>' : ""}</span>`;
  }

  // ---- Опрос live.json раз в минуту; без него — подсказка запустить сборщик ----
  async function loadLive() {
    try {
      const r = await fetch("data/live.json?ts=" + Date.now(), { cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  let lastStamp = "";
  async function tick() {
    const live = await loadLive();
    if (!live) {
      if (lastStamp !== "empty") {
        lastStamp = "empty";
        document.getElementById("map").innerHTML =
          '<p class="empty">Данных пока нет — запусти <code>python3 collector/scan.py</code></p>';
        document.getElementById("legend").innerHTML = "";
      }
      return;
    }
    lastStamp = live.meta.updated;
    render(live); // перерисовка дешёвая, заодно обновляет индикатор устаревания
  }
  tick();
  setInterval(tick, 60e3);
})();
