/* meshtastic-zoo — рендер топологии в SVG.
   Единственный источник данных: data/live.json от сборщика
   (collector/scan.py), перечитывается раз в минуту. */
(function () {
  // Квадратные «жетоны»: фото сверху, имя и подпись под ним — центр
  // карточки ближе к точке, дистанции читаются честнее
  const CARD = { w: 120, h: 88, r: 11 };
  const WCARD = { w: 120, h: 88, r: 11 };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  // Язык интерфейса выбирается в настройках, хранится локально
  let lang = localStorage.getItem("mzLang") || "en";
  const T = {
    en: {
      callsign: "Callsign", model: "Model", role: "Role", battery: "Battery",
      wallPower: "wall power", voltage: "Voltage", uptime: "Uptime",
      chUtil: "Channel util", ownTx: "Own TX", lastSeen: "Last seen",
      online: "online (answers over TCP)", conversation: "Conversation",
      compose: "Compose", legs: "Legs", twoWay: "two-way", oneWay: "one-way",
      onAir: "on air", delivered: "delivered", error: "error", noAck: "no ack",
      reply: "reply…", replyFrom: "reply from {0}", markRead: "mark as read",
      sendFromWhich: "send from which node", message: "message…", send: "send",
      close: "close", noData: "no data", ofIdeal100: "100% of ideal (SNR {0}…{1} dB)",
      noSnrData: "no SNR data", scan: "scan", stale: "stale!", justNow: "just now",
      unitMin: "min", unitH: "h", unitD: "d", ago: "{0} ago", upD: "d", upH: "h", upM: "m",
      mailTip: "unread direct messages — click to open the node",
      noDataYet: "No data yet — run", heard: "heard {0}", ofIdeal: "of ideal",
      noDataTip: "{0} → {1}: no data — {2} has not heard {3} directly (neither in a scan nor in cache)",
      settings: "Settings", save: "Save", saved: "✓ saved", hubUnavail: "hub unavailable",
      storedHint: "stored in collector/config.json; the map picks it up on the next refresh (within a minute)",
      failedSend: "Failed to send:", failedSave: "Failed to save:",
      mapAria: "Mesh network map", language: "Language",
      fSubnets: "Site subnets (one per line)", fFloor: "0% quality at SNR, dB",
      fIdeal: "100% quality at SNR, dB", fKeep: "Keep a silent neighbor, hours",
      fCache: "Remember legs in cache, hours", fMap: "Map refresh, seconds",
      fDisc: "New-node discovery, seconds", fRoam: "Roaming nodes (id, one per line)",
      fFragile: "Slow subnets — polled lightly (prefix per line)",
    },
    ru: {
      callsign: "Позывной", model: "Модель", role: "Роль", battery: "Батарея",
      wallPower: "питание от сети", voltage: "Напряжение", uptime: "Аптайм",
      chUtil: "Загрузка эфира", ownTx: "Свой TX", lastSeen: "Видели",
      online: "онлайн (отвечает по TCP)", conversation: "Переписка",
      compose: "Написать", legs: "Плечи", twoWay: "двусторонние", oneWay: "одиночные",
      onAir: "в эфире", delivered: "доставлено", error: "ошибка", noAck: "без квитанции",
      reply: "ответить…", replyFrom: "ответить с {0}", markRead: "прочитано",
      sendFromWhich: "от лица какой ноды", message: "сообщение…", send: "отправить",
      close: "закрыть", noData: "нет данных", ofIdeal100: "100% от идеала (SNR {0}…{1} dB)",
      noSnrData: "нет данных об SNR", scan: "скан", stale: "устарело!", justNow: "только что",
      unitMin: "мин", unitH: "ч", unitD: "дн", ago: "{0} назад", upD: "д", upH: "ч", upM: "м",
      mailTip: "непрочитанные личные сообщения — клик откроет ноду",
      noDataYet: "Данных пока нет — запусти", heard: "слышно {0}", ofIdeal: "от идеала",
      noDataTip: "{0} → {1}: нет данных — {2} не слышала {3} напрямую (ни в скане, ни в кэше)",
      settings: "Настройки", save: "Сохранить", saved: "✓ сохранено", hubUnavail: "hub недоступен",
      storedHint: "хранится в collector/config.json; карта подхватит при следующем обновлении (до минуты)",
      failedSend: "Не отправилось:", failedSave: "Не сохранилось:",
      mapAria: "Карта mesh-сети", language: "Язык",
      fSubnets: "Подсети площадок (по одной на строку)", fFloor: "0% качества при SNR, дБ",
      fIdeal: "100% качества при SNR, дБ", fKeep: "Держать молчащего соседа, часов",
      fCache: "Помнить плечи в кэше, часов", fMap: "Обновление карты, секунд",
      fDisc: "Поиск новых нод, секунд", fRoam: "Кочующие ноды (id, по одному)",
      fFragile: "Медленные подсети — лёгкий опрос (префикс на строке)",
    },
  };
  const t = (k, ...a) => {
    let s = (T[lang] && T[lang][k]) ?? T.en[k] ?? k;
    a.forEach((v, i) => { s = s.split("{" + i + "}").join(v); });
    return s;
  };

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
      const scale = Math.min((W - 210) / spanX, (H - 230) / spanY);
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
          if (Math.abs(dx) < 134 && Math.abs(dy) < 104) {
            const over = (104 - Math.abs(dy)) / 2, s = dy >= 0 ? 1 : -1;
            a[1] = Math.max(55, Math.min(H - 55, a[1] - s * over));
            b[1] = Math.max(55, Math.min(H - 55, b[1] + s * over));
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

    // Непрочитанные личные сообщения: счётчики для маркеров
    const unread = {};
    let unreadTotal = 0;
    for (const m of msgs) {
      if (m.read) continue;
      unreadTotal++;
      unread[m.node] = (unread[m.node] || 0) + 1;
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
      return s < 90 ? t("justNow") : s < 3600 ? Math.round(s / 60) + " " + t("unitMin")
        : s < 86400 ? Math.round(s / 3600) + " " + t("unitH") : Math.round(s / 86400) + " " + t("unitD");
    };
    const fmtAgo = (ts) => { const a = fmtAge(ts); return a === t("justNow") ? a : t("ago", a); };

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
        // порт — на грани, которую линия реально пересекает;
        // bias даёт встречным плечам пары стабильный порядок с обоих
        // концов — «мосты» идут двумя параллельными линиями, не крестом
        const dx = o.cx - n.cx, dy = o.cy - n.cy;
        const side = Math.abs(dx) / n.w > Math.abs(dy) / n.h
          ? (dx < 0 ? "left" : "right")
          : (dy < 0 ? "top" : "bottom");
        ((ports[n.id] ??= { top: [], bottom: [], left: [], right: [] })[side])
          .push({ li, ox: o.cx, oy: o.cy, bias: l.from < l.to ? 0.01 : -0.01 });
      }
    });
    for (const [nid, sides] of Object.entries(ports)) {
      const n = nodes[nid];
      for (const [side, list] of Object.entries(sides)) {
        const horiz = side === "top" || side === "bottom";
        // естественная точка — пересечение луча к оппоненту с гранью
        for (const p of list) {
          const dx = p.ox - n.cx, dy = p.oy - n.cy;
          const nat = horiz
            ? n.cx + dx * (n.h / 2) / Math.max(1e-6, Math.abs(dy))
            : n.cy + dy * (n.w / 2) / Math.max(1e-6, Math.abs(dx));
          const lo = horiz ? n.cx - n.w / 2 + 12 : n.cy - n.h / 2 + 10;
          const hiB = horiz ? n.cx + n.w / 2 - 12 : n.cy + n.h / 2 - 10;
          p.nat = Math.max(lo, Math.min(hiB, nat)) + p.bias;
        }
        // совпавшие точки раздвигаются минимальным зазором
        list.sort((p, q) => p.nat - q.nat);
        const gap = 13;
        for (let i = 1; i < list.length; i++) {
          if (list[i].nat < list[i - 1].nat + gap) list[i].nat = list[i - 1].nat + gap;
        }
        let hi = horiz ? n.cx + n.w / 2 - 12 : n.cy + n.h / 2 - 10;
        for (let i = list.length - 1; i >= 0; i--) {
          if (list[i].nat > hi) list[i].nat = hi;
          hi = list[i].nat - gap;
        }
        for (const p of list) {
          portPt[`${p.li}:${nid}`] =
            side === "top" ? [p.nat, n.cy - n.h / 2] :
            side === "bottom" ? [p.nat, n.cy + n.h / 2] :
            side === "left" ? [n.cx - n.w / 2, p.nat] :
            [n.cx + n.w / 2, p.nat];
        }
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
      const label = l.snr == null ? (l.note || t("noData")) : fmtSnr(l.snr);
      const tip = (l.snr == null
        ? t("noDataTip", l.from, l.to, lbl2(l.to), lbl2(l.from))
        : `${l.from} → ${l.to}: SNR ${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}% ${t("ofIdeal")}`)
        + (l.heard ? ` · ${t("heard", fmtAgo(l.heard))}` : "");

      // Плечи внешних нод — приглушённые, чтобы не забивали картину
      const dim = a.world || b.world ? " dim" : "";
      const side = pairCount[k] > 1 ? ((pairSeen[k] = (pairSeen[k] || 0) + 1) === 1 ? 1 : -1) : 0;
      let [x1, y1] = portPt[`${li}:${l.from}`] ?? edgePoint(a, b.cx, b.cy);
      let [x2, y2] = portPt[`${li}:${l.to}`] ?? edgePoint(b, a.cx, a.cy, 14);
      const dl = Math.hypot(x2 - x1, y2 - y1) || 1;
      const ux = (x2 - x1) / dl, uy = (y2 - y1) / dl;
      x1 += ux * 3; y1 += uy * 3; x2 -= ux * 11; y2 -= uy * 11;

      // Объезд чужих карточек: концы линии на месте (честная дистанция
      // не меняется), но сама линия гнётся дугой в ту сторону, где
      // помех меньше — с учётом всех карточек вдоль пути
      let bend = 0, nxv = 0, nyv = 0;
      {
        const ddx = x2 - x1, ddy = y2 - y1;
        const len2 = ddx * ddx + ddy * ddy || 1;
        let needL = 0, needR = 0;
        for (const o of Object.values(nodes)) {
          if (o.id === l.from || o.id === l.to) continue;
          const tp = ((o.cx - x1) * ddx + (o.cy - y1) * ddy) / len2;
          if (tp < 0.08 || tp > 0.92) continue;
          const dxo = o.cx - (x1 + ddx * tp), dyo = o.cy - (y1 + ddy * tp);
          const need = 92 - Math.hypot(dxo, dyo);
          if (need <= 0) continue;
          if ((dxo * ddy - dyo * ddx) >= 0) needL = Math.max(needL, need);
          else needR = Math.max(needR, need);
        }
        if (needL || needR) {
          const s = needL >= needR ? -1 : 1; // гнём от более мешающей стороны
          bend = Math.min(90, (s === -1 ? needL : needR) * 1.5);
          const ln = Math.sqrt(len2);
          nxv = (-ddy / ln) * s; nyv = (ddx / ln) * s;
        }
      }
      const qcx = (x1 + x2) / 2 + nxv * bend * 2;
      const qcy = (y1 + y2) / 2 + nyv * bend * 2;
      const geom = bend
        ? `<path d="M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${qcx.toFixed(1)} ${qcy.toFixed(1)}
            ${x2.toFixed(1)} ${y2.toFixed(1)}" fill="none" stroke="${col}" stroke-width="2"
            stroke-dasharray="6 6" marker-end="url(#${mid})"/>`
        : `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
            stroke="${col}" stroke-width="2" stroke-dasharray="6 6" marker-end="url(#${mid})"/>`;

      // Подпись — «пилюля» прямо на линии (на дуге — по кривой),
      // повёрнутая вдоль неё: принадлежность очевидна, фон читается
      const lt = l.labelT ?? (side === 1 ? 0.38 : side === -1 ? 0.62 : 0.5);
      const qp = (tq, p0, pc, p1) =>
        (1 - tq) ** 2 * p0 + 2 * (1 - tq) * tq * pc + tq ** 2 * p1;
      const lx = bend ? qp(lt, x1, qcx, x2) : x1 + (x2 - x1) * lt;
      const ly = bend ? qp(lt, y1, qcy, y2) : y1 + (y2 - y1) * lt;
      const ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
      const rot = (ang > 90 || ang < -90) ? ang + 180 : ang;
      const tw = label.length * 7.6 + 16;
      edgeSvg.push(`<g class="${cls}${dim}"><title>${esc(tip)}</title>
        ${geom}
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
      const nm = name.length > 15 ? name.slice(0, 14) + "…" : name;
      const sub = String(n.sub);
      const sb = sub.length > 17 ? sub.slice(0, 16) + "…" : sub;
      const photo = `<g transform="translate(${n.cx - 20}, ${y + 6})" clip-path="url(#ph)">
        <rect width="40" height="40" rx="7" fill="rgba(255,255,255,.06)"/>
        <image href="${hwImg(n.hw)}" width="40" height="40" preserveAspectRatio="xMidYMid meet"/></g>`;
      const stale = n.heard && !n.online && Date.now() / 1e3 - n.heard > 3 * 3600;
      const badge = n.online
        ? `<circle cx="${x + n.w - 10}" cy="${y + 10}" r="3.5" fill="#35c98e"/>`
        : n.heard ? `<text x="${x + n.w - 6}" y="${y + 13}" text-anchor="end" font-size="9.5"
            fill="${stale ? "#e0a03c" : "var(--muted)"}">${fmtAge(n.heard)}</text>` : "";
      const mailBadge = unread[n.id] ? `<g transform="translate(${x + 5}, ${y + 5})">
        <rect width="34" height="17" rx="8.5" fill="#e0a03c"/>
        <text x="17" y="12.5" text-anchor="middle" font-size="10.5" font-weight="700"
          fill="#141416">✉ ${unread[n.id]}</text></g>` : "";
      out.push(`<g class="node n-${n.id}" data-id="${n.id}">
        ${tipTxt ? `<title>${esc(tipTxt)}</title>` : ""}
        <rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.r}"
          fill="${fill}" stroke="${stroke}" stroke-width="1.5"${n.mobile ? ' stroke-dasharray="7 5"' : ""}/>
        ${photo}${badge}${mailBadge}
        <text x="${n.cx}" y="${y + 61}" text-anchor="middle" fill="var(--text)"
          font-size="${nm.length > 11 ? 10.5 : 12}" font-weight="700">${esc(nm)}</text>
        <text x="${n.cx}" y="${y + 77}" text-anchor="middle" fill="${subFill}"
          font-size="9.5">${esc(sb)}</text>
      </g>`);
    }

    document.getElementById("map").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img"
        aria-label="${t("mapAria")}"><defs>${rfMarkers.join("")}
        <clipPath id="ph"><rect width="40" height="40" rx="7"/></clipPath></defs>${out.join("\n")}</svg>`;

    // Панель подробностей ноды (по клику)
    const panel = document.getElementById("panel");
    const fmtUp = (s) => {
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      return (d ? d + " " + t("upD") + " " : "") + (h ? h + " " + t("upH") + " " : "") + m + " " + t("upM");
    };
    const lbl = (id) => (nodes[id] || { label: id }).label;
    async function markRead(ids) {
      try {
        await fetch("/api/read", { method: "POST", body: JSON.stringify({ ids }) });
      } catch { }
      for (const m of msgs) if (ids.includes(m.id)) m.read = true;
    }
    function showPanel(id, force) {
      // не перерисовывать панель под руками, пока пользователь пишет ответ
      if (!force) {
        const ae = document.activeElement;
        const busy = [...panel.querySelectorAll(".reply")].some(i => i.value)
          || (ae && panel.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "SELECT"));
        if (busy) return;
      }
      const n = nodes[id];
      if (!n) { panel.classList.remove("open"); openId = null; return; }
      document.getElementById("settings").classList.remove("open"); // взаимоисключение
      openId = id;
      const i = n.info || {};
      const rows = [
        ["ID", n.id],
        [t("callsign"), n.short && n.short !== n.label ? n.short : null],
        ["IP", n.sub !== n.id ? n.sub : null],
        [t("model"), n.hw],
        [t("role"), i.role],
        [t("battery"), i.battery == null ? null : i.battery > 100 ? t("wallPower") : i.battery + " %"],
        [t("voltage"), i.voltage == null ? null : i.voltage.toFixed(2) + " V"],
        [t("uptime"), i.uptime == null ? null : fmtUp(i.uptime)],
        [t("chUtil"), i.chUtil == null ? null : i.chUtil.toFixed(1) + " %"],
        [t("ownTx"), i.airTx == null ? null : i.airTx.toFixed(1) + " %"],
        [t("lastSeen"), n.online ? t("online") : n.heard ? fmtAgo(n.heard) : "—"],
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
      // короткий позывной в списке (полное имя — в тултипе), чтобы не обрезалось
      const shortOf = (nid) => (nodes[nid] || {}).short || lbl(nid);
      const legLine = (l, who, ttl) => {
        const col = colorOf(l);
        const val = l.snr == null ? t("noData") : `${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}%`;
        return `<div class="leg"><span class="dot" style="background:${col}"></span>
          <span class="who"${ttl ? ` title="${esc(ttl)}"` : ""}>${who}</span>
          <span style="color:${col}">${val}</span>
          ${l.heard ? `<span class="age">${fmtAge(l.heard)}</span>` : ""}</div>`;
      };
      const legs =
        (pairsL.length ? `<div class="psub">${t("twoWay")}</div>` : "") +
        pairsL.map(r => `<div class="pair"><div class="pwho" title="${esc(lbl(r.other))}">⇄ ${esc(shortOf(r.other))}</div>
          ${legLine(r.out, "→")}${legLine(r.in, "←")}</div>`).join("") +
        (singles.length ? `<div class="psub">${t("oneWay")}</div>` : "") +
        `<div class="singles">${singles.map(r =>
          legLine(r.in || r.out, (r.out ? "→ " : "← ") + esc(shortOf(r.other)), lbl(r.other))).join("")}</div>`;
      // История переписки этой ноды, хронологически. Дедуп: у сообщения
      // между двумя своими нодами есть и отправленная, и принятая копия —
      // для своей ноды берём отправленное этой нодой и принятое ей;
      // для внешней — отправленное ей и принятое от неё.
      const isOwn = !!n.own;
      const thread = msgs.filter(m => m.kind === "out"
        ? (isOwn ? m.frm === id : m.to === id)
        : (isOwn ? m.node === id : m.frm === id))
        .sort((a, b) => a.ts - b.ts).slice(-40);
      const STATUS = {
        sent: ["⏳", t("onAir"), "var(--muted)"],
        delivered: ["✓", t("delivered"), "#35c98e"],
        failed: ["✗", t("error"), "#e05656"],
        noack: ["⚠", t("noAck"), "#e0a03c"],
      };
      const rowHtml = (m) => {
        const bySelf = m.frm === id;
        const peer = bySelf ? (m.kind === "out" ? m.to : m.node) : m.frm;
        const who = (bySelf ? "→ " : "← ") + esc(m.frmName && !bySelf ? m.frmName : lbl(peer));
        let foot = `<span class="age">${fmtAgo(m.ts)}</span>`;
        if (m.kind === "out") {
          const [g, st, c] = STATUS[m.status] || ["", "", "var(--muted)"];
          foot = `<span class="mstatus" style="color:${c}" title="${esc(st + (m.detail ? " · " + m.detail : ""))}">${g} ${esc(st)}</span>` + foot;
        }
        const canRead = m.kind !== "out" && m.node === id && !m.read;
        return `<div class="msg ${bySelf ? "self" : "peer"}${canRead ? " unread" : ""}">
          <div class="mh"><span class="mfrom">${who}</span><span class="mfoot">${foot}</span></div>
          <div class="mtext">${esc(m.text)}</div>
          ${canRead ? `<div class="mact" data-mid="${esc(m.id)}" data-from="${esc(m.node)}" data-to="${esc(m.frm)}">
            <input class="reply" placeholder="${t("reply")}">
            <button class="msend" title="${esc(t("replyFrom", lbl(m.node)))}">➤</button>
            <button class="mok" title="${t("markRead")}">✓</button></div>` : ""}
        </div>`;
      };
      const msgHtml = thread.length
        ? `<div class="pmsgs"><b>${t("conversation")}</b><div class="thread">${thread.map(rowHtml).join("")}</div></div>`
        : "";

      // «Написать»: этой ноде — от лица любой своей онлайн-ноды
      // (по умолчанию та, что слышит адресата лучше всех)
      const qTo = (ownId) => {
        let best = -1;
        for (const l of D.links) {
          if (l.type !== "rf" || l.snr == null) continue;
          if ((l.from === id && l.to === ownId) || (l.from === ownId && l.to === id)) {
            best = Math.max(best, pctOf(l.snr));
          }
        }
        return best;
      };
      const owners = Object.values(nodes)
        .filter(v => v.own && v.online && v.id !== id)
        .sort((a2, b2) => qTo(b2.id) - qTo(a2.id));
      const composeHtml = owners.length ? `<div class="pcompose"><b>${t("compose")}</b>
        <div class="crow">
          <select class="cfrom" title="${t("sendFromWhich")}">${owners.map(o =>
            `<option value="${esc(o.id)}">${esc(o.short || o.label)}</option>`).join("")}</select>
          <input class="reply cmsg" placeholder="${t("message")}">
          <button class="csend" title="${t("send")}">➤</button>
        </div></div>` : "";

      panel.innerHTML = `
        <button id="pclose" aria-label="${t("close")}">×</button>
        <div class="phead"><img src="${hwImg(n.hw)}" alt="">
          <div><b>${esc(n.label)}</b>${i.long && i.long !== n.label
            ? `<div class="plong">${esc(i.long)}</div>` : ""}</div></div>
        ${rows.map(([k, v]) => `<div class="prow"><span>${k}</span><span>${esc(String(v))}</span></div>`).join("")}
        ${msgHtml}
        ${composeHtml}
        ${legs ? `<div class="plegs"><b>${t("legs")}</b>${legs}</div>` : ""}`;
      panel.classList.add("open");
      // переписка прокручивается к последнему сообщению
      const th = panel.querySelector(".thread");
      if (th) th.scrollTop = th.scrollHeight;
      panel.querySelector("#pclose").onclick = () => { panel.classList.remove("open"); openId = null; };
      // после действия — обновить msgs и перерисовать (в т.ч. маркеры почты)
      const afterAction = async () => { await refreshMsgs(); forcePanel = true; render(lastLive); };
      panel.querySelectorAll(".msg .mact").forEach(row => {
        const inp = row.querySelector(".reply");
        const mid = row.dataset.mid, from = row.dataset.from, to = row.dataset.to;
        row.querySelector(".msend").onclick = async () => {
          const text = inp.value.trim();
          if (!text) { inp.focus(); return; }
          row.querySelector(".msend").disabled = true;
          let res = { ok: false, error: t("hubUnavail") };
          try {
            res = await (await fetch("/api/send", {
              method: "POST", body: JSON.stringify({ node: from, to, text }),
            })).json();
          } catch { }
          if (res.ok) { await markRead([mid]); await afterAction(); }
          else {
            row.querySelector(".msend").disabled = false;
            alert(t("failedSend") + " " + (res.error || "?"));
          }
        };
        row.querySelector(".mok").onclick = async () => {
          await markRead([mid]); await afterAction();
        };
      });
      const cw = panel.querySelector(".pcompose");
      if (cw) {
        cw.querySelector(".csend").onclick = async () => {
          const inp = cw.querySelector(".cmsg");
          const text = inp.value.trim();
          if (!text) { inp.focus(); return; }
          const btn = cw.querySelector(".csend");
          btn.disabled = true;
          let res = { ok: false, error: t("hubUnavail") };
          try {
            res = await (await fetch("/api/send", {
              method: "POST",
              body: JSON.stringify({ node: cw.querySelector(".cfrom").value, to: id, text }),
            })).json();
          } catch { }
          btn.disabled = false;
          if (res.ok) { inp.value = ""; await afterAction(); }
          else alert(t("failedSend") + " " + (res.error || "?"));
        };
      }
    }

    // Наведение на ноду — подсветить её линки и соседей; клик — панель
    const svgEl = document.querySelector("#map svg");
    for (const g of svgEl.querySelectorAll(".node")) {
      g.addEventListener("click", (ev) => { ev.stopPropagation(); showPanel(g.dataset.id, true); });
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
        ${t("ofIdeal100", fmtSnr(S.floor), fmtSnr(S.ideal))}</span>
      <span class="item"><span class="swatch dashed" style="border-color:#8a8a90"></span>${t("noSnrData")}</span>
      <span class="item">${t("scan")} · ${esc(D.meta.updated)}
        ${stale ? `<b style="color:#e0a03c">· ${t("stale")}</b>` : ""}</span>`;

    // Общий маркер непрочитанной почты
    const mailEl = document.getElementById("mail");
    if (unreadTotal) {
      mailEl.hidden = false;
      mailEl.textContent = `✉ ${unreadTotal}`;
      mailEl.title = t("mailTip");
      mailEl.onclick = () => {
        const nid = Object.keys(unread).find(k => nodes[k]);
        if (nid) showPanel(nid, true);
      };
    } else {
      mailEl.hidden = true;
    }

    if (openId) showPanel(openId, forcePanel); // обновить открытую панель свежими данными
    forcePanel = false;
  }

  // ---- Опрос live.json раз в минуту; без него — подсказка запустить сборщик ----
  async function loadLive() {
    try {
      const r = await fetch("data/live.json?ts=" + Date.now(), { cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  let lastStamp = "", openId = null, lastLive = null, rsTimer = null, msgs = [], forcePanel = false;
  async function refreshMsgs() {
    try {
      const r = await fetch("/api/messages", { cache: "no-store" });
      if (r.ok) msgs = (await r.json()).messages || [];
    } catch { /* hub не запущен — карта работает и без почты */ }
  }
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
    if (!e.target.closest("#settings") && !e.target.closest("#gear")) {
      document.getElementById("settings").classList.remove("open");
    }
  });

  // ---- Настройки (⚙): читаются и сохраняются через hub ----
  const SET_FIELDS = [
    ["subnets", "fSubnets", "area"],
    ["snrScale.floor", "fFloor", "num"],
    ["snrScale.ideal", "fIdeal", "num"],
    ["worldMaxAgeH", "fKeep", "num"],
    ["cacheMaxAgeH", "fCache", "num"],
    ["topoEveryS", "fMap", "num"],
    ["rescanS", "fDisc", "num"],
    ["mobile", "fRoam", "area"],
    ["fragile", "fFragile", "area"],
  ];
  const setEl = document.getElementById("settings");
  const sfId = (k) => "sf-" + k.replace(".", "-");

  async function openSettings() {
    let cfg = {};
    try {
      cfg = await (await fetch("/api/config", { cache: "no-store" })).json();
    } catch {
      setEl.innerHTML = `<b class='stitle'>${t("settings")}</b><div class='shint'>${t("hubUnavail")}</div>`;
      setEl.classList.add("open");
      return;
    }
    const val = (k) => k.includes(".")
      ? (cfg[k.split(".")[0]] || {})[k.split(".")[1]]
      : cfg[k];
    const langOpt = (v, name) => `<option value="${v}"${lang === v ? " selected" : ""}>${name}</option>`;
    setEl.innerHTML = `<button id="sclose" aria-label="${t("close")}">×</button>
      <b class="stitle">${t("settings")}</b>
      <label class="srow"><span>${t("language")}</span>
        <select id="sf-lang">${langOpt("en", "English")}${langOpt("ru", "Русский")}</select></label>` +
      SET_FIELDS.map(([k, label, kind]) => kind === "area"
        ? `<label class="srow"><span>${t(label)}</span>
            <textarea id="${sfId(k)}" rows="2">${esc((val(k) || []).join("\n"))}</textarea></label>`
        : `<label class="srow"><span>${t(label)}</span>
            <input id="${sfId(k)}" type="number" step="any" value="${val(k) ?? ""}"></label>`
      ).join("") +
      `<button id="ssave">${t("save")}</button>
       <div class="shint">${t("storedHint")}</div>`;
    setEl.classList.add("open");
    document.getElementById("panel").classList.remove("open");
    openId = null;
    setEl.querySelector("#sclose").onclick = () => setEl.classList.remove("open");
    setEl.querySelector("#sf-lang").onchange = (e) => {
      lang = e.target.value;
      localStorage.setItem("mzLang", lang);
      document.getElementById("gear").title = t("settings");
      if (lastLive) render(lastLive);
      openSettings(); // перестроить настройки на новом языке
    };
    setEl.querySelector("#ssave").onclick = async () => {
      const g = (k) => document.getElementById(sfId(k));
      const lines = (el) => el.value.split("\n").map(s => s.trim()).filter(Boolean);
      const body = {
        subnets: lines(g("subnets")),
        snrScale: { floor: +g("snrScale.floor").value, ideal: +g("snrScale.ideal").value },
        worldMaxAgeH: +g("worldMaxAgeH").value,
        cacheMaxAgeH: +g("cacheMaxAgeH").value,
        topoEveryS: +g("topoEveryS").value,
        rescanS: +g("rescanS").value,
        mobile: lines(g("mobile")),
        fragile: lines(g("fragile")),
      };
      const btn = setEl.querySelector("#ssave");
      btn.disabled = true;
      let res = { ok: false, error: t("hubUnavail") };
      try {
        res = await (await fetch("/api/config", {
          method: "POST", body: JSON.stringify(body),
        })).json();
      } catch { }
      btn.disabled = false;
      if (res.ok) {
        btn.textContent = t("saved");
        setTimeout(() => { btn.textContent = t("save"); }, 1800);
      } else {
        alert(t("failedSave") + " " + (res.error || "?"));
      }
    };
  }
  document.getElementById("gear").onclick = () => {
    if (setEl.classList.contains("open")) setEl.classList.remove("open");
    else openSettings();
  };
  document.getElementById("gear").title = t("settings");
  async function tick() {
    await refreshMsgs();
    const live = await loadLive();
    if (!live) {
      if (lastStamp !== "empty") {
        lastStamp = "empty";
        document.getElementById("map").innerHTML =
          `<p class="empty">${t("noDataYet")} <code>python3 collector/hub.py</code></p>`;
        document.getElementById("legend").innerHTML = "";
      }
      return;
    }
    lastStamp = live.meta.updated;
    lastLive = live;
    render(live); // перерисовка дешёвая, заодно обновляет индикатор устаревания
  }

  // Быстрый опрос почты: статус доставки и маркеры обновляются в течение
  // секунд, а не раз в минуту; при набранном тексте панель не трогаем
  let msgSig = "";
  async function msgTick() {
    await refreshMsgs();
    const sig = msgs.map(m => m.id + (m.read ? "1" : "0") + (m.status || "")).join(",");
    if (sig !== msgSig && lastLive) { msgSig = sig; render(lastLive); }
  }

  tick();
  setInterval(tick, 60e3);
  setInterval(msgTick, 6e3);
})();
