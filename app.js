/* meshtastic-zoo — рендер топологии в SVG.
   Единственный источник данных: data/live.json от сборщика
   (collector/scan.py), перечитывается раз в минуту. */
(function () {
  const W = 960, M = 18;
  const GAP = 26, DEF_H = { subnet: 150, gap: 640 };
  const CARD = { w: 190, h: 64, r: 11 };
  const WCARD = { w: 200, h: 62, r: 11 };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  function render(D) {
    // Вертикальная раскладка: зоны стопкой в порядке из данных, число зон любое.
    // Высота зоны — z.h из данных, иначе дефолт по типу; высота холста считается сама.
    const zoneBoxes = {};
    let H = M;
    for (const z of D.zones) {
      const h = z.h ?? DEF_H[z.kind] ?? DEF_H.gap;
      zoneBoxes[z.id] = { x: M, y: H, w: W - M * 2, h, ...z };
      H += h + GAP;
    }
    H += M - GAP;

    // Позиции нод
    const nodes = {};
    for (const n of D.nodes) {
      const z = zoneBoxes[n.zone];
      if (!z) continue;
      const world = z.kind !== "subnet";
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
    const fmtAge = (ts) => {
      const s = Math.max(0, Date.now() / 1e3 - ts);
      return s < 90 ? "только что" : s < 3600 ? Math.round(s / 60) + " мин"
        : s < 86400 ? Math.round(s / 3600) + " ч" : Math.round(s / 86400) + " дн";
    };
    const fmtAgo = (ts) => { const a = fmtAge(ts); return a === "только что" ? a : a + " назад"; };

    // Изображения девайсов (официальные рендеры из Meshtastic web-flasher)
    const HW_IMG = [
      [/1_WATT/, "tbeam-1w.svg"], [/S3_CORE/, "tbeam-s3-core.svg"],
      [/CARDPUTER/, "m5stack_cardputer.svg"], [/HELTEC_V3/, "heltec-v3.svg"],
      [/HELTEC/, "heltec_v4.svg"], [/RAK/, "rak4631.svg"], [/BEAM/, "tbeam.svg"],
    ];
    const hwImg = (hw) => {
      const h = String(hw || "").toUpperCase();
      const m = HW_IMG.find(([re]) => re.test(h));
      return "img/hw/" + (m ? m[1] : "unknown.svg");
    };

    // Точка на границе карточки по направлению к (tx,ty), с зазором
    function edgePoint(n, tx, ty, gap = 8) {
      const dx = tx - n.cx, dy = ty - n.cy;
      const sx = (n.w / 2 + gap) / Math.abs(dx || 1e-9);
      const sy = (n.h / 2 + gap) / Math.abs(dy || 1e-9);
      const s = Math.min(sx, sy);
      return [n.cx + dx * s, n.cy + dy * s];
    }

    let out = [];

    // Полосы площадок; gap-области не рисуются — это просто свободное место
    for (const z of Object.values(zoneBoxes)) {
      if (z.kind !== "subnet") continue;
      out.push(`<rect x="${z.x}" y="${z.y}" width="${z.w}" height="${z.h}" rx="16"
        fill="var(--zone-bg)" stroke="var(--zone-stroke)"/>`);
      out.push(`<text x="${z.x + 22}" y="${z.y + 34}" fill="var(--text)"
        font-size="17" font-weight="700">${esc(z.label)}</text>`);
    }

    // Рёбра (под карточками); маркеры стрелок копятся сюда — свой цвет на каждое плечо.
    // Встречные плечи одной пары разносим перпендикулярно, чтобы не слипались.
    const edgeSvg = [], rfMarkers = [];
    const pairCount = {}, pairSeen = {};
    const crossLane = {};
    let laneN = 0;
    for (const l of D.links) {
      if (l.type !== "rf") continue;
      const k = [l.from, l.to].sort().join("|");
      pairCount[k] = (pairCount[k] || 0) + 1;
    }

    // Порты: точки входа плеч раскладываются веером по граням карточек,
    // чтобы стрелки не слипались в одной точке
    const ports = {}, portPt = {};
    D.links.forEach((l, li) => {
      if (l.type !== "rf") return;
      const a = nodes[l.from], b = nodes[l.to];
      if (!a || !b || (!a.world && !b.world && a.zone !== b.zone)) return; // коридорные мимо
      for (const [n, o] of [[a, b], [b, a]]) {
        const side = o.cy < n.cy - n.h / 2 ? "top" : o.cy > n.cy + n.h / 2 ? "bottom"
          : o.cx < n.cx ? "left" : "right";
        ((ports[n.id] ??= { top: [], bottom: [], left: [], right: [] })[side])
          .push({ li, ox: o.cx, oy: o.cy });
      }
    });
    for (const [nid, sides] of Object.entries(ports)) {
      const n = nodes[nid];
      for (const [side, list] of Object.entries(sides)) {
        list.sort((p, q) => (side === "left" || side === "right") ? p.oy - q.oy : p.ox - q.ox);
        list.forEach((p, i) => {
          const f = (i + 1) / (list.length + 1);
          portPt[`${p.li}:${nid}`] =
            side === "top" ? [n.cx - n.w / 2 + f * n.w, n.cy - n.h / 2] :
            side === "bottom" ? [n.cx - n.w / 2 + f * n.w, n.cy + n.h / 2] :
            side === "left" ? [n.cx - n.w / 2, n.cy - n.h / 2 + f * n.h] :
            [n.cx + n.w / 2, n.cy - n.h / 2 + f * n.h];
        });
      }
    }

    for (const [li, l] of D.links.entries()) {
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
      const label = l.snr == null ? (l.note || "не изм.") : fmtSnr(l.snr);
      const tip = (l.snr == null
        ? `${l.from} ↔ ${l.to}: не измерено`
        : `${l.from} → ${l.to}: SNR ${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}% от идеала`)
        + (l.heard ? ` · слышно ${fmtAgo(l.heard)}` : "");

      if (!a.world && !b.world && a.zone !== b.zone) {
        // Межплощадочное плечо — обходом по правому коридору, чтобы не
        // тонуло среди внешних нод: дорожка на пару, туда и обратно рядом
        if (!(k in crossLane)) crossLane[k] = laneN++;
        const dir = (pairSeen[k] = (pairSeen[k] || 0) + 1); // 1 туда, 2 обратно
        const bx = W - M - 20 - crossLane[k] * 34 - (dir - 1) * 9;
        const gd = a.cy < b.cy ? 1 : -1;
        const sx = a.cx + a.w / 2 - 22 - (dir - 1) * 12;
        const sy = a.cy + gd * a.h / 2;
        const runY = sy + gd * (16 + (dir - 1) * 10);
        const ey = b.cy - 6 + (dir - 1) * 12;
        const ex = b.cx + b.w / 2 + 14;
        const labY = (runY + ey) / 2 + (dir === 1 ? -6 : 16);
        edgeSvg.push(`<g class="${cls}"><title>${esc(tip)}</title>
          <path d="M ${sx} ${sy} V ${runY} H ${bx} V ${ey} H ${ex}" fill="none"
            stroke="${col}" stroke-width="2.5" stroke-dasharray="7 5"
            stroke-linejoin="round" marker-end="url(#${mid})"/>
          <text x="${bx - 10}" y="${labY}" text-anchor="end" fill="${col}" font-size="14.5"
            font-weight="600" paint-order="stroke" stroke="var(--bg)"
            stroke-width="5">${esc(label)}</text></g>`);
        continue;
      }

      // Плечи внешних нод — приглушённые, чтобы не забивали картину
      const dim = a.world || b.world ? " dim" : "";
      const side = pairCount[k] > 1 ? ((pairSeen[k] = (pairSeen[k] || 0) + 1) === 1 ? 1 : -1) : 0;
      let [x1, y1] = portPt[`${li}:${l.from}`] ?? edgePoint(a, b.cx, b.cy);
      let [x2, y2] = portPt[`${li}:${l.to}`] ?? edgePoint(b, a.cx, a.cy, 14);
      const dl = Math.hypot(x2 - x1, y2 - y1) || 1;
      const ux = (x2 - x1) / dl, uy = (y2 - y1) / dl;
      x1 += ux * 3; y1 += uy * 3; x2 -= ux * 11; y2 -= uy * 11;
      const t = l.labelT ?? (side === 1 ? 0.42 : side === -1 ? 0.58 : 0.5);
      const lx = x1 + (x2 - x1) * t, ly = y1 + (y2 - y1) * t;
      edgeSvg.push(`<g class="${cls}${dim}"><title>${esc(tip)}</title>
        <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
          stroke="${col}" stroke-width="2" stroke-dasharray="6 6" marker-end="url(#${mid})"/>
        <text x="${lx + 10}" y="${ly - 8}" fill="${col}" font-size="14"
          font-weight="600" paint-order="stroke" stroke="var(--bg)" stroke-width="5">${esc(label)}</text></g>`);
    }
    out.push(...edgeSvg);

    // Мини-пиктограммы моделей железа (22×22, контурные)
    const HW_ICONS = {
      tbeam: '<rect x="1.5" y="8" width="19" height="9" rx="2"/><line x1="5" y1="8" x2="5" y2="2.5"/><circle cx="5" cy="2.5" r="1.2"/><circle cx="14.5" cy="12.5" r="2.2"/>',
      chip: '<rect x="5" y="5" width="12" height="12" rx="2"/><path d="M8 5V2 M14 5V2 M8 20v-3 M14 20v-3 M5 8H2 M5 14H2 M20 8h-3 M20 14h-3"/>',
      cardputer: '<rect x="2.5" y="3.5" width="17" height="15" rx="2"/><rect x="5" y="6" width="12" height="5" rx="1"/><path d="M6 14.5h.01 M9 14.5h.01 M12 14.5h.01 M15 14.5h.01 M7.5 16.5h.01 M10.5 16.5h.01 M13.5 16.5h.01"/>',
      heltec: '<rect x="3" y="6" width="16" height="11" rx="2"/><rect x="6" y="8.5" width="7" height="6" rx="1"/><line x1="16.5" y1="6" x2="16.5" y2="2.5"/>',
      rak: '<rect x="4" y="7" width="14" height="10" rx="2"/><line x1="7" y1="7" x2="7" y2="3"/><rect x="12.5" y="10" width="3.5" height="4"/>',
      antenna: '<line x1="11" y1="19" x2="11" y2="8"/><circle cx="11" cy="5.5" r="1.6"/><path d="M7 2.5a6 6 0 0 0 0 7 M15 2.5a6 6 0 0 1 0 7 M7.5 19h7"/>',
    };
    const hwKey = (hw) => {
      const h = String(hw || "").toUpperCase();
      if (!h) return null;
      if (h.includes("CARDPUTER")) return "cardputer";
      if (h.includes("BEAM")) return h.includes("S3") ? "chip" : "tbeam";
      if (h.includes("HELTEC")) return "heltec";
      if (h.includes("RAK")) return "rak";
      return "antenna";
    };

    // Карточки нод (поверх рёбер)
    for (const n of Object.values(nodes)) {
      const x = n.cx - n.w / 2, y = n.cy - n.h / 2;
      const fill = n.world ? "var(--world-card)" : "var(--card-fill)";
      const stroke = n.world ? "#3a3a3e" : "var(--card-stroke)";
      const subFill = n.world ? "var(--muted)" : "var(--card-sub)";
      const long = (n.info || {}).long;
      const tipTxt = [long !== n.label ? long : null, n.hw, n.hint].filter(Boolean).join(" · ");
      const name = String(n.label);
      const nm = name.length > 18 ? name.slice(0, 17) + "…" : name;
      // Пиктограмма — фолбэк, поверх неё фото девайса (если файл есть)
      const ik = hwKey(n.hw);
      const icon = ik ? `<g transform="translate(${x + 17}, ${n.cy - 11})" fill="none"
          stroke="${n.world ? "#9d9da4" : "#cdcdf6"}" stroke-width="1.5"
          stroke-linecap="round" stroke-linejoin="round">${HW_ICONS[ik]}</g>` : "";
      const photo = `<g transform="translate(${x + 7}, ${n.cy - 21})" clip-path="url(#ph)">
        <rect width="42" height="42" rx="8" fill="rgba(255,255,255,.06)"/>
        <image href="${hwImg(n.hw)}" width="42" height="42" preserveAspectRatio="xMidYMid meet"/></g>`;
      const stale = n.heard && !n.online && Date.now() / 1e3 - n.heard > 3 * 3600;
      const badge = n.online
        ? `<circle cx="${x + n.w - 12}" cy="${y + 12}" r="4" fill="#35c98e"/>`
        : n.heard ? `<text x="${x + n.w - 8}" y="${y + 15}" text-anchor="end" font-size="10.5"
            fill="${stale ? "#e0a03c" : "var(--muted)"}">${fmtAge(n.heard)}</text>` : "";
      out.push(`<g class="node n-${n.id}" data-id="${n.id}">
        ${tipTxt ? `<title>${esc(tipTxt)}</title>` : ""}
        <rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.r}"
          fill="${fill}" stroke="${stroke}" stroke-width="1.5"${n.mobile ? ' stroke-dasharray="7 5"' : ""}/>
        ${icon}${photo}${badge}
        <text x="${n.cx + 14}" y="${n.cy - 6}" text-anchor="middle" fill="var(--text)"
          font-size="${nm.length > 13 ? 12.5 : nm.length > 9 ? 15 : 17}"
          font-weight="700">${esc(nm)}</text>
        <text x="${n.cx + 14}" y="${n.cy + 17}" text-anchor="middle" fill="${subFill}"
          font-size="13">${esc(n.sub)}</text>
      </g>`);
    }

    document.getElementById("map").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img"
        aria-label="Карта mesh-сети"><defs>${rfMarkers.join("")}
        <clipPath id="ph"><rect width="42" height="42" rx="8"/></clipPath></defs>${out.join("\n")}</svg>`;

    // Панель подробностей ноды (по клику)
    const panel = document.getElementById("panel");
    const fmtUp = (s) => {
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      return (d ? d + " д " : "") + (h ? h + " ч " : "") + m + " м";
    };
    const lbl = (id) => (nodes[id] || { label: id }).label;
    function showPanel(id) {
      const n = nodes[id];
      if (!n) { panel.classList.remove("open"); openId = null; return; }
      openId = id;
      const i = n.info || {};
      const rows = [
        ["ID", n.id],
        ["IP", n.sub !== n.id ? n.sub : null],
        ["Модель", n.hw],
        ["Роль", i.role],
        ["Батарея", i.battery == null ? null : i.battery > 100 ? "питание от сети" : i.battery + " %"],
        ["Напряжение", i.voltage == null ? null : i.voltage.toFixed(2) + " В"],
        ["Аптайм", i.uptime == null ? null : fmtUp(i.uptime)],
        ["Эфир занят", i.chUtil == null ? null : i.chUtil.toFixed(1) + " %"],
        ["Свой TX", i.airTx == null ? null : i.airTx.toFixed(1) + " %"],
        ["Видели", n.online ? "онлайн (отвечает по TCP)" : n.heard ? fmtAgo(n.heard) : "—"],
      ].filter(([, v]) => v != null);
      const legs = D.links
        .filter(l => l.type === "rf" && (l.from === id || l.to === id))
        .map(l => {
          const col = colorOf(l);
          const val = l.snr == null ? "не изм." : `${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}%`;
          const dirTxt = l.from === id ? `→ ${lbl(l.to)}` : `← ${lbl(l.from)}`;
          return `<div class="leg"><span class="dot" style="background:${col}"></span>
            <span class="who">${esc(dirTxt)}</span><span style="color:${col}">${val}</span>
            ${l.heard ? `<span class="age">${fmtAge(l.heard)}</span>` : ""}</div>`;
        }).join("");
      panel.innerHTML = `
        <button id="pclose" aria-label="закрыть">×</button>
        <div class="phead"><img src="${hwImg(n.hw)}" alt="">
          <div><b>${esc(n.label)}</b>${i.long && i.long !== n.label
            ? `<div class="plong">${esc(i.long)}</div>` : ""}</div></div>
        ${rows.map(([k, v]) => `<div class="prow"><span>${k}</span><span>${esc(String(v))}</span></div>`).join("")}
        ${legs ? `<div class="plegs"><b>Плечи</b>${legs}</div>` : ""}`;
      panel.classList.add("open");
      panel.querySelector("#pclose").onclick = () => { panel.classList.remove("open"); openId = null; };
    }

    // Наведение на ноду — подсветить её линки и соседей; клик — панель
    const svgEl = document.querySelector("#map svg");
    for (const g of svgEl.querySelectorAll(".node")) {
      g.addEventListener("click", (ev) => { ev.stopPropagation(); showPanel(g.dataset.id); });
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

    if (openId) showPanel(openId); // обновить открытую панель свежими данными
  }

  // ---- Опрос live.json раз в минуту; без него — подсказка запустить сборщик ----
  async function loadLive() {
    try {
      const r = await fetch("data/live.json?ts=" + Date.now(), { cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  let lastStamp = "", openId = null;
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#panel") && !e.target.closest(".node")) {
      document.getElementById("panel").classList.remove("open");
      openId = null;
    }
  });
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
