/* meshtastic-zoo — рендер топологии в SVG.
   Единственный источник данных: data/live.json от сборщика
   (collector/scan.py), перечитывается раз в минуту. */
(function () {
  // Квадратные «жетоны»: фото сверху, имя и подпись под ним — центр
  // карточки ближе к точке, дистанции читаются честнее
  const CARD = { w: 148, h: 96, r: 12 };
  const WCARD = { w: 148, h: 96, r: 12 };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  function render(D) {
    // Холст подстраивается под пропорции окна — свободного места не остаётся
    const box = document.getElementById("map").getBoundingClientRect();
    const H = 1150;
    const W = Math.max(720, Math.min(3200,
      Math.round(H * (box.width && box.height ? box.width / box.height : 0.84))));

    // Посадка сырого облака позиций сборщика: PCA-поворот главной осью
    // вдоль длинной стороны окна, ОДИН масштаб по обеим осям (пропорции
    // дистанций честные), детерминированный выбор из 4 ориентаций
    const ids = D.nodes.map(n => n.id);
    const px = {};
    if (ids.length) {
      const pts = D.nodes.map(n => [n.x, n.y]);
      const nN = pts.length;
      const mx = pts.reduce((s, p) => s + p[0], 0) / nN;
      const my = pts.reduce((s, p) => s + p[1], 0) / nN;
      let sxx = 0, syy = 0, sxy = 0;
      for (const [x, y] of pts) {
        sxx += (x - mx) ** 2; syy += (y - my) ** 2; sxy += (x - mx) * (y - my);
      }
      const theta = 0.5 * Math.atan2(2 * sxy, sxx - syy);
      const target = W >= H ? 0 : Math.PI / 2;
      const cands = [];
      for (const k of [0, 1]) for (const m of [1, -1]) {
        const rot = target - theta + k * Math.PI;
        const c = Math.cos(rot), s = Math.sin(rot);
        cands.push(pts.map(([x, y]) =>
          [(x - mx) * m * c - (y - my) * s, (x - mx) * m * s + (y - my) * c]));
      }
      const fi = ids.indexOf([...ids].sort()[0]);
      cands.sort((A, B) => (A[fi][1] - B[fi][1]) || (A[fi][0] - B[fi][0]));
      const P = cands[0];
      const xs = P.map(p => p[0]), ys = P.map(p => p[1]);
      const spanX = (Math.max(...xs) - Math.min(...xs)) || 1e-6;
      const spanY = (Math.max(...ys) - Math.min(...ys)) || 1e-6;
      const scale = Math.min((W - 260) / spanX, (H - 250) / spanY);
      const cx0 = (Math.max(...xs) + Math.min(...xs)) / 2;
      const cy0 = (Math.max(...ys) + Math.min(...ys)) / 2;
      ids.forEach((id, i) => {
        px[id] = [W / 2 + (P[i][0] - cx0) * scale, H / 2 + (P[i][1] - cy0) * scale];
      });
      // раздвижка перекрывшихся карточек по вертикали
      for (let r = 0; r < 300; r++) {
        let moved = false;
        for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
          const a = px[ids[i]], b = px[ids[j]];
          const dx = b[0] - a[0], dy = b[1] - a[1];
          if (Math.abs(dx) < 162 && Math.abs(dy) < 112) {
            const over = (112 - Math.abs(dy)) / 2, s = dy >= 0 ? 1 : -1;
            a[1] = Math.max(60, Math.min(H - 60, a[1] - s * over));
            b[1] = Math.max(60, Math.min(H - 60, b[1] + s * over));
            moved = true;
          }
        }
        if (!moved) break;
      }
    }

    const nodes = {};
    for (const n of D.nodes) {
      const world = !n.own;
      const c = world ? WCARD : CARD;
      const [cx, cy] = px[n.id] ?? [W / 2, H / 2];
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
    const lbl2 = (id) => (nodes[id] || { label: id }).label;

    // Рёбра (под карточками); маркеры стрелок копятся сюда — свой цвет на каждое плечо.
    // Встречные плечи одной пары разносим перпендикулярно, чтобы не слипались.
    const edgeSvg = [], rfMarkers = [];
    const pairCount = {}, pairSeen = {};
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
      if (!a || !b) return;
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
      if (!a || !b || l.type !== "rf") continue;
      const cls = `edge e-${l.from} e-${l.to}`;

      // RF: пунктир со стрелкой к услышавшей ноде, цвет = % от идеала, подпись = SNR
      const col = colorOf(l);
      const mid = `arr${rfMarkers.length}`;
      rfMarkers.push(`<marker id="${mid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7"
        markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${col}"/></marker>`);

      const k = [l.from, l.to].sort().join("|");
      const label = l.snr == null ? (l.note || "нет данных") : fmtSnr(l.snr);
      const tip = (l.snr == null
        ? `${l.from} → ${l.to}: нет данных — ${lbl2(l.to)} не слышала ${lbl2(l.from)} напрямую (ни в скане, ни в кэше)`
        : `${l.from} → ${l.to}: SNR ${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}% от идеала`)
        + (l.heard ? ` · слышно ${fmtAgo(l.heard)}` : "");

      // Плечи внешних нод — приглушённые, чтобы не забивали картину
      const dim = a.world || b.world ? " dim" : "";
      const side = pairCount[k] > 1 ? ((pairSeen[k] = (pairSeen[k] || 0) + 1) === 1 ? 1 : -1) : 0;
      let [x1, y1] = portPt[`${li}:${l.from}`] ?? edgePoint(a, b.cx, b.cy);
      let [x2, y2] = portPt[`${li}:${l.to}`] ?? edgePoint(b, a.cx, a.cy, 14);
      const dl = Math.hypot(x2 - x1, y2 - y1) || 1;
      const ux = (x2 - x1) / dl, uy = (y2 - y1) / dl;
      x1 += ux * 3; y1 += uy * 3; x2 -= ux * 11; y2 -= uy * 11;
      // Подпись — «пилюля» прямо на линии, повёрнутая вдоль неё:
      // принадлежность очевидна, фон гарантирует читаемость
      const t = l.labelT ?? (side === 1 ? 0.38 : side === -1 ? 0.62 : 0.5);
      const lx = x1 + (x2 - x1) * t, ly = y1 + (y2 - y1) * t;
      const ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
      const rot = (ang > 90 || ang < -90) ? ang + 180 : ang;
      const tw = label.length * 7.6 + 16;
      edgeSvg.push(`<g class="${cls}${dim}"><title>${esc(tip)}</title>
        <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
          stroke="${col}" stroke-width="2" stroke-dasharray="6 6" marker-end="url(#${mid})"/>
        <g transform="translate(${lx.toFixed(1)}, ${ly.toFixed(1)}) rotate(${rot.toFixed(1)})">
          <rect x="${-tw / 2}" y="-10" width="${tw}" height="20" rx="10"
            fill="var(--bg)" fill-opacity="0.92" stroke="${col}" stroke-opacity="0.65"/>
          <text y="4.5" text-anchor="middle" fill="${col}" font-size="13"
            font-weight="700">${esc(label)}</text>
        </g></g>`);
    }
    out.push(...edgeSvg);

    // Карточки нод (поверх рёбер)
    for (const n of Object.values(nodes)) {
      const x = n.cx - n.w / 2, y = n.cy - n.h / 2;
      const fill = n.world ? "var(--world-card)" : "var(--card-fill)";
      const stroke = n.world ? "#3a3a3e" : "var(--card-stroke)";
      const subFill = n.world ? "var(--muted)" : "var(--card-sub)";
      const long = (n.info || {}).long;
      const tipTxt = [long !== n.label ? long : null, n.hw, n.hint].filter(Boolean).join(" · ");
      const name = String(n.label);
      const nm = name.length > 16 ? name.slice(0, 15) + "…" : name;
      const sub = String(n.sub);
      const sb = sub.length > 21 ? sub.slice(0, 20) + "…" : sub;
      const photo = `<g transform="translate(${n.cx - 22}, ${y + 8})" clip-path="url(#ph)">
        <rect width="44" height="44" rx="8" fill="rgba(255,255,255,.06)"/>
        <image href="${hwImg(n.hw)}" width="44" height="44" preserveAspectRatio="xMidYMid meet"/></g>`;
      const stale = n.heard && !n.online && Date.now() / 1e3 - n.heard > 3 * 3600;
      const badge = n.online
        ? `<circle cx="${x + n.w - 11}" cy="${y + 11}" r="4" fill="#35c98e"/>`
        : n.heard ? `<text x="${x + n.w - 7}" y="${y + 14}" text-anchor="end" font-size="10"
            fill="${stale ? "#e0a03c" : "var(--muted)"}">${fmtAge(n.heard)}</text>` : "";
      out.push(`<g class="node n-${n.id}" data-id="${n.id}">
        ${tipTxt ? `<title>${esc(tipTxt)}</title>` : ""}
        <rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.r}"
          fill="${fill}" stroke="${stroke}" stroke-width="1.5"${n.mobile ? ' stroke-dasharray="7 5"' : ""}/>
        ${photo}${badge}
        <text x="${n.cx}" y="${y + 68}" text-anchor="middle" fill="var(--text)"
          font-size="${nm.length > 12 ? 11.5 : 13.5}" font-weight="700">${esc(nm)}</text>
        <text x="${n.cx}" y="${y + 85}" text-anchor="middle" fill="${subFill}"
          font-size="10.5">${esc(sb)}</text>
      </g>`);
    }

    document.getElementById("map").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img"
        aria-label="Карта mesh-сети"><defs>${rfMarkers.join("")}
        <clipPath id="ph"><rect width="44" height="44" rx="8"/></clipPath></defs>${out.join("\n")}</svg>`;

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
      // Плечи: двусторонние пары («мосты») — группами, одиночные — отдельно,
      // всё отсортировано по качеству
      const byOther = {};
      for (const l of D.links) {
        if (l.type !== "rf" || (l.from !== id && l.to !== id)) continue;
        const other = l.from === id ? l.to : l.from;
        const r = (byOther[other] ??= { other });
        r[l.from === id ? "out" : "in"] = l;
      }
      const qOf = (l) => (l && l.snr != null ? pctOf(l.snr) : -1);
      const bestQ = (r) => Math.max(qOf(r.in), qOf(r.out));
      const rel = Object.values(byOther);
      const pairsL = rel.filter(r => r.in && r.out).sort((a, b) => bestQ(b) - bestQ(a));
      const singles = rel.filter(r => !(r.in && r.out)).sort((a, b) => bestQ(b) - bestQ(a));
      const legLine = (l, who) => {
        const col = colorOf(l);
        const val = l.snr == null ? "нет данных" : `${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}%`;
        return `<div class="leg"><span class="dot" style="background:${col}"></span>
          <span class="who">${who}</span><span style="color:${col}">${val}</span>
          ${l.heard ? `<span class="age">${fmtAge(l.heard)}</span>` : ""}</div>`;
      };
      const legs =
        (pairsL.length ? `<div class="psub">двусторонние</div>` : "") +
        pairsL.map(r => `<div class="pair"><div class="pwho">⇄ ${esc(lbl(r.other))}</div>
          ${legLine(r.out, "→")}${legLine(r.in, "←")}</div>`).join("") +
        (singles.length ? `<div class="psub">одиночные</div>` : "") +
        `<div class="singles">${singles.map(r =>
          legLine(r.in || r.out, (r.out ? "→ " : "← ") + esc(lbl(r.other)))).join("")}</div>`;
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
      <span class="item"><span class="swatch dashed" style="border-color:#8a8a90"></span>нет данных об SNR</span>
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

  let lastStamp = "", openId = null, lastLive = null, rsTimer = null;
  const refit = () => {
    clearTimeout(rsTimer);
    rsTimer = setTimeout(() => { if (lastLive) render(lastLive); }, 200);
  };
  window.addEventListener("resize", refit);
  new ResizeObserver(refit).observe(document.getElementById("map"));
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
    lastLive = live;
    render(live); // перерисовка дешёвая, заодно обновляет индикатор устаревания
  }
  tick();
  setInterval(tick, 60e3);
})();
