/* meshtastic-zoo — рендер топологии в SVG.
   Единственный источник данных: data/live.json от сборщика
   (collector/scan.py), перечитывается раз в минуту. */
(function () {
  // Компактные «жетоны»: фото сверху, имя и подпись под ним — центр
  // карточки ближе к точке, а меньший размер даёт сильным связям сойтись
  // ближе (дистанции честнее пропорциональны сигналу)
  const CARD = { w: 102, h: 82, r: 10 };
  const WCARD = { w: 102, h: 82, r: 10 };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");

  // Язык интерфейса выбирается в настройках, хранится локально
  let lang = localStorage.getItem("mzLang") || "en";
  let showHops = localStorage.getItem("mzShowHops") !== "0";  // галочка в легенде

  // Лимит текста Meshtastic (payload ~200 байт), Enter-отправка, авто-обрезка.
  const MAX_MSG_BYTES = 200;
  const byteLen = (s) => new TextEncoder().encode(s).length;
  const setupInput = (inp, trigger) => {
    if (!inp) return;
    inp.maxLength = MAX_MSG_BYTES;              // грубый потолок по символам
    inp.title = `≤ ${MAX_MSG_BYTES} B`;
    inp.addEventListener("input", () => {       // точный лимит по БАЙТАМ (UTF-8)
      while (byteLen(inp.value) > MAX_MSG_BYTES) inp.value = inp.value.slice(0, -1);
    });
    inp.addEventListener("keydown", (e) => {    // Enter — отправить
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); trigger(); }
    });
  };

  // Цвет своих нод по подсети (палитра по умолчанию + выбор в настройках)
  const SUB_PALETTE = ["#4f4fd0", "#2f9e6b", "#c07a2d", "#a04ec0", "#c0504e", "#2f8fb0"];
  let subColors = {};
  try { subColors = JSON.parse(localStorage.getItem("mzSubColors") || "{}"); } catch { subColors = {}; }
  const subnetOf = (ip) => { const m = /^(\d+\.\d+\.\d+)\./.exec(ip || ""); return m ? m[1] : (ip || ""); };
  const lighten = (hex, f) => {
    const n = parseInt((hex || "#4f4fd0").slice(1), 16);
    let r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    r = Math.round(r + (255 - r) * f); g = Math.round(g + (255 - g) * f); b = Math.round(b + (255 - b) * f);
    return "#" + ((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1);
  };

  // Актуальная версия прошивки Meshtastic (последний стабильный релиз с GitHub)
  let latestFw = null;
  const semver = (v) => (String(v || "").match(/\d+\.\d+\.\d+/) || [""])[0];
  const fwStatus = (fw) => {
    const cur = semver(fw);
    if (!cur || !latestFw) return null;
    const c = cur.split(".").map(Number), l = latestFw.split(".").map(Number);
    let up = false;
    for (let k = 0; k < 3; k++) { if (l[k] > c[k]) { up = true; break; } if (l[k] < c[k]) break; }
    return up ? { text: t("fwUpdate", "v" + latestFw), color: "#e0a03c" }
              : { text: t("fwCurrent"), color: "#35c98e" };
  };
  async function fetchLatestFw() {
    try {
      const r = await fetch("https://api.github.com/repos/meshtastic/firmware/releases/latest", { cache: "no-store" });
      if (!r.ok) return;
      latestFw = semver((await r.json()).tag_name);
      if (latestFw && lastLive) render(lastLive);  // обновить открытую панель
    } catch { }
  }
  const T = {
    en: {
      callsign: "Callsign", model: "Model", role: "Role", battery: "Battery",
      wallPower: "wall power", voltage: "Voltage", uptime: "Uptime",
      chUtil: "Channel util", ownTx: "Own TX", lastSeen: "Last seen",
      online: "online (answers over TCP)", conversation: "Conversation",
      keyLabel: "Key", keyYes: "received", keyNo: "not received (can't DM)",
      secFw: "Firmware", secRadio: "Radio", secDevice: "Device",
      fwVer: "Version", fwLatest: "Latest", fwCurrent: "✓ up to date", fwUpdate: "update available: {0}",
      cHops: "Hops (hop limit)", cRegion: "Region", cPreset: "Modem preset",
      cTxPower: "TX power", cTxEnabled: "TX enabled", cBoosted: "RX boosted gain",
      cWifi: "WiFi", cBt: "Bluetooth", cPkc: "PKI keys", cRebroadcast: "Rebroadcast",
      cNodeInfo: "NodeInfo interval",
      secMesh: "Mesh", secPos: "Position", cHopsAway: "Hops away", direct: "direct",
      cViaMqtt: "Via MQTT", cLicensed: "Licensed (ham)",
      cLat: "Latitude", cLon: "Longitude", cAlt: "Altitude", openMap: "open on OpenStreetMap →",
      publicChannel: "Public channel", gotByLabel: "received by",
      chNoMsg: "no messages yet",
      hop: "{0} hop", hopTip: "{0} → {1}: reachable via {2} hop(s), not heard directly",
      showHops: "former neighbor", showHopsTip: "show former direct neighbors now reached via relays",
      resizeTip: "drag to resize",
      compose: "Compose", legs: "Legs", twoWay: "two-way", oneWay: "one-way",
      onAir: "on air", delivered: "delivered", error: "error", noAck: "no ack",
      waiting: "waiting for node", waitingHint: "no key yet — will send the moment this node is next heard on air",
      reply: "reply…", replyFrom: "reply from {0}", markRead: "mark as read", resend: "resend",
      reactPending: "sending…", replyToYou: "↩ to you",
      sendFromWhich: "send from which node", message: "message…", send: "send",
      close: "close", noData: "no data", ofIdeal100: "100% of ideal (SNR {0}…{1} dB)",
      noSnrData: "no SNR data", scan: "scan", stale: "stale!", justNow: "just now",
      nodeCount: "{0} nodes · {1} own · {2} neighbors",
      unitMin: "min", unitH: "h", unitD: "d", ago: "{0} ago", upD: "d", upH: "h", upM: "m",
      mailTip: "unread direct messages — click to open the node",
      noDataYet: "No data yet — run", heard: "heard {0}", ofIdeal: "of ideal",
      noDataTip: "{0} → {1}: no data — {2} has not heard {3} directly (neither in a scan nor in cache)",
      settings: "Settings", save: "Save", saved: "✓ saved", hubUnavail: "hub unavailable",
      storedHint: "stored in collector/config.json; the map picks it up on the next refresh (within a minute)",
      failedSend: "Failed to send:", failedSave: "Failed to save:",
      mapAria: "Mesh network map", language: "Language",
      fSubnets: "Site subnets & colors", fFloor: "0% quality at SNR, dB",
      fIdeal: "100% quality at SNR, dB", fKeep: "Keep a silent neighbor, hours",
      fCache: "Remember legs in cache, hours", fMap: "Map refresh, seconds",
      fDisc: "New-node discovery, seconds", fRoam: "Roaming nodes (id, one per line)",
      fFragile: "Slow subnets — polled lightly (prefix per line)",
      fSubColor: "Color of subnet {0}", addSubnet: "add subnet", remove: "remove",
      secHist: "History", histReach: "Reachability 24h", histHeard: "Heard 24h",
      histBatt: "Battery 24h", histChUtil: "Channel util 24h", histNone: "not enough history yet",
      histReachTip: "share of the last 24h this node answered over TCP",
      histHeardTip: "share of the last 24h this node was heard on the air",
    },
    ru: {
      callsign: "Позывной", model: "Модель", role: "Роль", battery: "Батарея",
      wallPower: "питание от сети", voltage: "Напряжение", uptime: "Аптайм",
      chUtil: "Загрузка эфира", ownTx: "Свой TX", lastSeen: "Видели",
      online: "онлайн (отвечает по TCP)", conversation: "Переписка",
      keyLabel: "Ключ", keyYes: "получен", keyNo: "не получен (DM нельзя)",
      secFw: "Прошивка", secRadio: "Радио", secDevice: "Устройство",
      fwVer: "Версия", fwLatest: "Актуальная", fwCurrent: "✓ актуальна", fwUpdate: "есть обновление: {0}",
      cHops: "Прыжки (hop limit)", cRegion: "Регион", cPreset: "Пресет модема",
      cTxPower: "Мощность TX", cTxEnabled: "TX включён", cBoosted: "Усиление RX",
      cWifi: "WiFi", cBt: "Bluetooth", cPkc: "PKI-ключи", cRebroadcast: "Ретрансляция",
      cNodeInfo: "Интервал NodeInfo",
      secMesh: "Сеть", secPos: "Позиция", cHopsAway: "Прыжков до неё", direct: "напрямую",
      cViaMqtt: "Через MQTT", cLicensed: "Лицензирована (ham)",
      cLat: "Широта", cLon: "Долгота", cAlt: "Высота", openMap: "открыть на OpenStreetMap →",
      publicChannel: "Публичный канал", gotByLabel: "приняли",
      chNoMsg: "пока пусто",
      hop: "{0} хоп", hopTip: "{0} → {1}: через {2} хоп(ов), напрямую не слышно",
      showHops: "бывший сосед", showHopsTip: "показывать бывших прямых соседей, теперь достижимых через ретрансляторы",
      resizeTip: "потяните, чтобы изменить ширину",
      compose: "Написать", legs: "Плечи", twoWay: "двусторонние", oneWay: "одиночные",
      onAir: "в эфире", delivered: "доставлено", error: "ошибка", noAck: "без квитанции",
      waiting: "жду ноду", waitingHint: "ключа пока нет — отправлю, как только нода снова выйдет в эфир",
      reply: "ответить…", replyFrom: "ответить с {0}", markRead: "прочитано", resend: "повторить",
      reactPending: "отправляется…", replyToYou: "↩ вам",
      sendFromWhich: "от лица какой ноды", message: "сообщение…", send: "отправить",
      close: "закрыть", noData: "нет данных", ofIdeal100: "100% от идеала (SNR {0}…{1} dB)",
      noSnrData: "нет данных об SNR", scan: "скан", stale: "устарело!", justNow: "только что",
      nodeCount: "узлов: {0} · свои {1} · соседи {2}",
      unitMin: "мин", unitH: "ч", unitD: "дн", ago: "{0} назад", upD: "д", upH: "ч", upM: "м",
      mailTip: "непрочитанные личные сообщения — клик откроет ноду",
      noDataYet: "Данных пока нет — запусти", heard: "слышно {0}", ofIdeal: "от идеала",
      noDataTip: "{0} → {1}: нет данных — {2} не слышала {3} напрямую (ни в скане, ни в кэше)",
      settings: "Настройки", save: "Сохранить", saved: "✓ сохранено", hubUnavail: "hub недоступен",
      storedHint: "хранится в collector/config.json; карта подхватит при следующем обновлении (до минуты)",
      failedSend: "Не отправилось:", failedSave: "Не сохранилось:",
      mapAria: "Карта mesh-сети", language: "Язык",
      fSubnets: "Подсети площадок и цвета", fFloor: "0% качества при SNR, дБ",
      fIdeal: "100% качества при SNR, дБ", fKeep: "Держать молчащего соседа, часов",
      fCache: "Помнить плечи в кэше, часов", fMap: "Обновление карты, секунд",
      fDisc: "Поиск новых нод, секунд", fRoam: "Кочующие ноды (id, по одному)",
      fFragile: "Медленные подсети — лёгкий опрос (префикс на строке)",
      fSubColor: "Цвет подсети {0}", addSubnet: "добавить подсеть", remove: "удалить",
      secHist: "История", histReach: "Достижимость 24ч", histHeard: "Слышно 24ч",
      histBatt: "Батарея 24ч", histChUtil: "Загрузка эфира 24ч", histNone: "истории пока мало",
      histReachTip: "доля последних 24ч, когда нода отвечала по TCP",
      histHeardTip: "доля последних 24ч, когда ноду было слышно в эфире",
    },
  };
  const t = (k, ...a) => {
    let s = (T[lang] && T[lang][k]) ?? T.en[k] ?? k;
    a.forEach((v, i) => { s = s.split("{" + i + "}").join(v); });
    return s;
  };

  // ===== История (Фаза 1): мини-графики из /api/history (кэш 30 с гасит частые перерисовки) =====
  const histCache = new Map();
  async function histFetch(path) {
    const c = histCache.get(path);
    if (c && Date.now() - c.ts < 30000) return c.data;
    let data = null;
    try { data = await (await fetch("/api/history/" + path, { cache: "no-store" })).json(); }
    catch { data = null; }
    histCache.set(path, { ts: Date.now(), data });
    return data;
  }
  // спарклайн-линия: pts=[{ts,v}], v==null рвёт линию на сегменты
  function spark(pts, { w = 132, h = 30, color = "#7bb0ff", min = null, max = null } = {}) {
    const vals = pts.map(p => p.v).filter(v => v != null);
    if (vals.length < 2) return `<span class="spark-empty">${t("histNone")}</span>`;
    const lo = min != null ? min : Math.min(...vals);
    const hi = max != null ? max : Math.max(...vals);
    const span = (hi - lo) || 1;
    const t0 = pts[0].ts, tspan = (pts[pts.length - 1].ts - t0) || 1;
    const X = ts => (1 + (ts - t0) / tspan * (w - 2)).toFixed(1);
    const Y = v => (h - 2 - (v - lo) / span * (h - 4)).toFixed(1);
    let d = "", pen = false;
    for (const p of pts) {
      if (p.v == null) { pen = false; continue; }
      d += (pen ? "L" : "M") + X(p.ts) + " " + Y(p.v) + " "; pen = true;
    }
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`
      + `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }
  // полоса online/heard по 30-мин бинам: красный→зелёный = доля времени «на связи»
  function upStrip(series, { hours = 24, w = 150, h = 12, bins = 48 } = {}) {
    const now = Date.now() / 1000, t0 = now - hours * 3600, bw = hours * 3600 / bins;
    const agg = Array.from({ length: bins }, () => ({ on: 0, n: 0 }));
    for (const p of series) {
      const b = Math.floor((p.ts - t0) / bw);
      if (b < 0 || b >= bins) continue;
      agg[b].n++; if (p.online) agg[b].on++;
    }
    const cw = w / bins;
    const rects = agg.map((a, i) =>
      `<rect x="${(i * cw).toFixed(2)}" y="0" width="${(cw + 0.4).toFixed(2)}" height="${h}" `
      + `fill="${a.n ? `hsl(${Math.round(a.on / a.n * 120)} 55% 45%)` : "rgba(255,255,255,.08)"}"/>`).join("");
    return `<svg class="ustrip" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${rects}</svg>`;
  }
  const pctOnline = (series) => {
    const s = series.filter(p => p.online != null);
    return s.length ? Math.round(s.filter(p => p.online).length / s.length * 100) : null;
  };

  function render(D) {
    // Галочка «многохопы» снята → убираем серые hop-ноды и их плечи из данных
    // до раскладки (карта тогда вписывается только по реальным нодам)
    if (!showHops) {
      const drop = new Set(D.nodes.filter(n => n.hop != null).map(n => n.id));
      if (drop.size) D = {
        ...D,
        nodes: D.nodes.filter(n => n.hop == null),
        links: D.links.filter(l => !drop.has(l.from) && !drop.has(l.to)),
      };
    }
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
      const scale = Math.min((W - 180) / spanX, (H - 200) / spanY);
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
          if (Math.abs(dx) < 114 && Math.abs(dy) < 94) {
            const over = (94 - Math.abs(dy)) / 2, s = dy >= 0 ? 1 : -1;
            a[1] = Math.max(48, Math.min(H - 48, a[1] - s * over));
            b[1] = Math.max(48, Math.min(H - 48, b[1] + s * over));
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
    // подсети своих нод и их цвета (переопределение из настроек или палитра)
    const ownSubnets = [...new Set(D.nodes.filter(n => n.own).map(n => subnetOf(n.sub)))].sort();
    const scolOf = (p) => subColors[p] || SUB_PALETTE[Math.max(0, ownSubnets.indexOf(p)) % SUB_PALETTE.length];

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
    // Порядок важен: специфичные модели — до общих
    const HW_IMG = [
      [/1_WATT/, "tbeam-1w.svg"], [/S3_CORE/, "tbeam-s3-core.svg"],
      [/CARDPUTER/, "m5stack_cardputer.svg"], [/C6L/, "m5_c6l.svg"],
      [/T_?DECK/, "t-deck.svg"], [/T_?ECHO/, "t-echo.svg"],
      [/PROMICRO/, "promicro.svg"],
      [/MESH_POCKET/, "heltec_mesh_pocket.svg"], [/T114/, "heltec-mesh-node-t114.svg"],
      [/WIRELESS_PAPER|_PAPER/, "heltec-wireless-paper.svg"],
      [/HELTEC_V3/, "heltec-v3.svg"], [/HELTEC/, "heltec_v4.svg"],
      [/STATION_G2/, "station-g2.svg"], [/T1000/, "tracker-t1000-e.svg"],
      [/NANO_G2/, "nano-g2-ultra.svg"], [/XIAO/, "seeed-xiao-s3.svg"],
      [/T3_S3|TLORA_T3/, "tlora-t3s3-v1.svg"], [/TLORA_C6/, "tlora-c6.svg"],
      [/TLORA/, "tlora-v2-1-1_6.svg"],
      [/RAK/, "rak4631.svg"], [/BEAM/, "tbeam.svg"], [/DIY/, "diy.svg"],
      [/PORTDUINO|NATIVE|LINUX/, "portduino.svg"],
      // обобщённые (после специфичных: CARDPUTER/PROMICRO и т.п. выигрывают)
      [/THINKNODE/, "thinknode.svg"], [/EBYTE/, "ebyte-esp32-s3.svg"],
      [/M5STACK/, "m5stack.svg"], [/PRIVATE_HW/, "private-hw.svg"],
      [/NRF52/, "nrf52-generic.svg"],
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
        // Раздвигаем совпавшие порты, но ВСЕГДА держим их в пределах грани
        // [lo, hi]: если с зазором gap не помещаются (у хабов вроде FCB
        // десятки плеч на одну грань) — ужимаем равномерно по всей грани,
        // иначе лишние порты вылезали за карточку и стрелка втыкалась в пустоту
        list.sort((p, q) => p.nat - q.nat);
        const lo = horiz ? n.cx - n.w / 2 + 12 : n.cy - n.h / 2 + 10;
        const hi = horiz ? n.cx + n.w / 2 - 12 : n.cy + n.h / 2 - 10;
        const gap = 13, span = Math.max(0, hi - lo);
        if (list.length <= 1) {
          if (list.length) list[0].nat = Math.max(lo, Math.min(hi, list[0].nat));
        } else if ((list.length - 1) * gap <= span) {
          for (let i = 1; i < list.length; i++)
            if (list[i].nat < list[i - 1].nat + gap) list[i].nat = list[i - 1].nat + gap;
          const over = list[list.length - 1].nat - hi;   // вдвинуть группу целиком внутрь
          if (over > 0) for (const p of list) p.nat -= over;
          for (const p of list) p.nat = Math.max(lo, Math.min(hi, p.nat));
        } else {
          for (let i = 0; i < list.length; i++) list[i].nat = lo + span * i / (list.length - 1);
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

    // --- Маршрутизация плеч с обходом чужих карточек ---
    // penAt: глубина проникновения точки в чужую карточку; segPen/curvePen —
    // сумма по семплам вдоль прямой/дуги; bestArc — лучшая дуга (сторона+изгиб)
    const PADX = 10, PADY = 8;
    const rects = Object.values(nodes).map(o => ({
      id: o.id, x0: o.cx - o.w / 2 - PADX, x1: o.cx + o.w / 2 + PADX,
      y0: o.cy - o.h / 2 - PADY, y1: o.cy + o.h / 2 + PADY,
    }));
    const penAt = (x, y, exF, exT) => {
      let s = 0;
      for (const r of rects) {
        if (r.id === exF || r.id === exT) continue;
        if (x > r.x0 && x < r.x1 && y > r.y0 && y < r.y1)
          s += Math.min(x - r.x0, r.x1 - x, y - r.y0, r.y1 - y);
      }
      return s;
    };
    const segPen = (ax, ay, bx, by, exF, exT, N = 16) => {
      let s = 0;
      for (let i = 1; i < N; i++) { const tt = i / N; s += penAt(ax + (bx - ax) * tt, ay + (by - ay) * tt, exF, exT); }
      return s;
    };
    const curvePen = (ax, ay, cx, cy, bx, by, exF, exT, N = 20) => {
      let s = 0;
      for (let i = 1; i < N; i++) {
        const tt = i / N, mt = 1 - tt;
        s += penAt(mt * mt * ax + 2 * mt * tt * cx + tt * tt * bx,
          mt * mt * ay + 2 * mt * tt * cy + tt * tt * by, exF, exT);
      }
      return s;
    };
    const portCands = (n, near) => {
      const hw = n.w / 2, hh = n.h / 2;
      const all = [near, [n.cx, n.cy - hh], [n.cx, n.cy + hh], [n.cx - hw, n.cy], [n.cx + hw, n.cy]];
      const uniq = [];
      for (const p of all)
        if (!uniq.some(q => Math.abs(q[0] - p[0]) < 6 && Math.abs(q[1] - p[1]) < 6)) uniq.push(p);
      return uniq;
    };
    const bestArc = (ax, ay, bx, by, exF, exT, base) => {
      const ddx = bx - ax, ddy = by - ay, ln = Math.hypot(ddx, ddy) || 1;
      let bp = base, bb = 0, bs = 0;
      for (const sgn of [1, -1]) for (const bnd of [24, 46, 74, 108]) {
        const cx = (ax + bx) / 2 + (-ddy / ln) * sgn * bnd * 2;
        const cy = (ay + by) / 2 + (ddx / ln) * sgn * bnd * 2;
        const p = curvePen(ax, ay, cx, cy, bx, by, exF, exT);
        if (p < bp - 0.5) { bp = p; bb = bnd; bs = sgn; }
      }
      return { bend: bb, sgn: bs, pen: bp, nx: -ddy / ln, ny: ddx / ln };
    };

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
      const label = l.hops ? t("hop", l.hops)
        : l.snr == null ? (l.note || t("noData")) : fmtSnr(l.snr);
      const tip = (l.hops
        ? t("hopTip", l.from, l.to, l.hops)
        : l.snr == null
          ? t("noDataTip", l.from, l.to, lbl2(l.to), lbl2(l.from))
          : `${l.from} → ${l.to}: SNR ${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}% ${t("ofIdeal")}`)
        + (l.heard ? ` · ${t("heard", fmtAgo(l.heard))}` : "");

      // Плечи внешних нод — приглушённые, чтобы не забивали картину
      const dim = a.world || b.world ? " dim" : "";
      const side = pairCount[k] > 1 ? ((pairSeen[k] = (pairSeen[k] || 0) + 1) === 1 ? 1 : -1) : 0;
      // Маршрут: near-порты → при пересечении дуга-объезд → если и она не
      // помогает, сменить точки старта/окончания на других гранях карточек
      // (позиции узлов при этом неизменны — двигаются только точки крепления)
      const exF = l.from, exT = l.to;
      const nearA = portPt[`${li}:${l.from}`] ?? edgePoint(a, b.cx, b.cy);
      const nearB = portPt[`${li}:${l.to}`] ?? edgePoint(b, a.cx, a.cy, 14);
      let route = { pa: nearA, pb: nearB, bend: 0, nxv: 0, nyv: 0 };
      const straight0 = segPen(nearA[0], nearA[1], nearB[0], nearB[1], exF, exT);
      if (straight0 > 0) {
        const a0 = bestArc(nearA[0], nearA[1], nearB[0], nearB[1], exF, exT, straight0);
        let best = {
          pa: nearA, pb: nearB, bend: a0.bend, nxv: a0.nx * a0.sgn, nyv: a0.ny * a0.sgn,
          cost: a0.pen * 3 + a0.bend * 0.02,
        };
        if (a0.pen > 1) { // дуга не вычистила — перебрать другие порты
          const candA = portCands(a, nearA), candB = portCands(b, nearB);
          for (const pa of candA) for (const pb of candB) {
            if (pa === nearA && pb === nearB) continue;
            const dev = Math.hypot(pa[0] - nearA[0], pa[1] - nearA[1])
              + Math.hypot(pb[0] - nearB[0], pb[1] - nearB[1]);
            const st = segPen(pa[0], pa[1], pb[0], pb[1], exF, exT);
            let bend = 0, nxv = 0, nyv = 0, pen = st;
            if (st > 0) {
              const ar = bestArc(pa[0], pa[1], pb[0], pb[1], exF, exT, st);
              bend = ar.bend; nxv = ar.nx * ar.sgn; nyv = ar.ny * ar.sgn; pen = ar.pen;
            }
            const cost = pen * 3 + dev * 0.03 + bend * 0.02;
            if (cost < best.cost) best = { pa, pb, bend, nxv, nyv, cost };
          }
        }
        route = best;
      }
      let [x1, y1] = route.pa, [x2, y2] = route.pb;
      const bend = route.bend, nxv = route.nxv, nyv = route.nyv;
      // контрольная точка дуги — по портам на гранях (до отступа концов)
      const qcx = (x1 + x2) / 2 + nxv * bend * 2;
      const qcy = (y1 + y2) / 2 + nyv * bend * 2;
      // Отступ концов — вдоль КАСАТЕЛЬНОЙ кривой (на дуге она ≠ прямая a→b),
      // иначе остриё стрелки уезжает вбок от грани. Малый зазор у стрелки —
      // чтобы она упиралась точно в грань узла, а не мимо угла.
      const along = (dx, dy, d) => { const l = Math.hypot(dx, dy) || 1; return [dx / l * d, dy / l * d]; };
      const [sdx, sdy] = along(qcx - x1, qcy - y1, 3);
      const [edx, edy] = along(x2 - qcx, y2 - qcy, 6);
      x1 += sdx; y1 += sdy; x2 -= edx; y2 -= edy;
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
      const isHop = n.hop != null;
      const scol = scolOf(subnetOf(n.sub));  // цвет своей ноды по её подсети
      const fill = isHop ? "#24242a" : n.world ? "var(--world-card)" : scol;
      const stroke = isHop ? "#55555c" : n.world ? "#3a3a3e" : lighten(scol, 0.35);
      const subFill = isHop ? "#7a7a80" : n.world ? "var(--muted)" : lighten(scol, 0.72);
      const long = (n.info || {}).long;
      const tipTxt = [long !== n.label ? long : null, n.hw, n.hint].filter(Boolean).join(" · ");
      const name = String(n.label);
      const nm = name.length > 14 ? name.slice(0, 13) + "…" : name;
      const sub = String(n.sub);
      const sb = sub.length > 16 ? sub.slice(0, 15) + "…" : sub;
      const photo = `<g transform="translate(${n.cx - 18}, ${y + 5})" clip-path="url(#ph)">
        <rect width="36" height="36" rx="6" fill="rgba(255,255,255,.06)"/>
        <image href="${hwImg(n.hw)}" width="36" height="36" preserveAspectRatio="xMidYMid meet"/></g>`;
      const stale = n.heard && !n.online && Date.now() / 1e3 - n.heard > 3 * 3600;
      const badge = n.online
        ? `<circle cx="${x + n.w - 9}" cy="${y + 9}" r="3.5" fill="#35c98e"/>`
        : n.heard ? `<text x="${x + n.w - 5}" y="${y + 12}" text-anchor="end" font-size="9"
            fill="${stale ? "#e0a03c" : "var(--muted)"}">${fmtAge(n.heard)}</text>` : "";
      const mailBadge = unread[n.id] ? `<g transform="translate(${x + 4}, ${y + 4})">
        <rect width="32" height="16" rx="8" fill="#e0a03c"/>
        <text x="16" y="12" text-anchor="middle" font-size="10" font-weight="700"
          fill="#141416">✉ ${unread[n.id]}</text></g>` : "";
      // замок в углу, если публичный ключ ноды ещё не получен (нельзя слать DM)
      const keyBadge = n.key === false
        ? `<text x="${x + 6}" y="${y + n.h - 6}" font-size="11">🔒</text>` : "";
      out.push(`<g class="node n-${n.id}" data-id="${n.id}">
        ${tipTxt ? `<title>${esc(tipTxt)}</title>` : ""}
        <rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${n.r}"
          fill="${fill}" stroke="${stroke}" stroke-width="1.5"${n.mobile ? ' stroke-dasharray="7 5"' : ""}/>
        ${photo}${badge}${mailBadge}${keyBadge}
        <text x="${n.cx}" y="${y + 55}" text-anchor="middle" fill="var(--text)"
          font-size="${nm.length > 10 ? 10 : 11.5}" font-weight="700">${esc(nm)}</text>
        <text x="${n.cx}" y="${y + 71}" text-anchor="middle" fill="${subFill}"
          font-size="9">${esc(sb)}</text>
      </g>`);
    }

    document.getElementById("map").innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img"
        aria-label="${t("mapAria")}"><defs>${rfMarkers.join("")}
        <clipPath id="ph"><rect width="36" height="36" rx="6"/></clipPath></defs>${out.join("\n")}</svg>`;

    // Панель подробностей ноды (по клику)
    const panel = document.getElementById("panel");
    const fmtUp = (s) => {
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      return (d ? d + " " + t("upD") + " " : "") + (h ? h + " " + t("upH") + " " : "") + m + " " + t("upM");
    };
    // имя ноды: сначала карточка на карте, затем карта имён всех известных
    // нод от hub (чтобы подписать даже адресата, ушедшего с карты), иначе id
    const NAMES = (D.meta && D.meta.names) || {};
    const lbl = (id) => (nodes[id] || {}).label || NAMES[id] || id;
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
        [t("lastSeen"), n.online ? t("online") : n.heard ? fmtAgo(n.heard) : "—"],
        ...(() => {
          // «Ключ» — про адресата, которому шлёшь; у своей ноды бессмысленно
          // (и противоречит статусу конкретных исходящих) — не показываем
          if (n.own || n.key == null) return [[null, null]];
          const kb = n.keyBy || [];
          const ownAll = (lastLive && lastLive.nodes || []).filter(x => x.own).map(x => x.id);
          if (!kb.length) return [[t("keyLabel"), "🔒 " + t("keyNo"), "#e0a03c"]];
          if (kb.length >= ownAll.filter(x => x !== id).length || kb.length >= ownAll.length)
            return [[t("keyLabel"), "✓ " + t("keyYes"), "#35c98e"]];
          // ключ есть лишь у части нод — назвать у кого (у остальных DM упадёт)
          return [[t("keyLabel"), "✓ " + kb.map(shortName).join(", "), "#8fce6a"]];
        })(),
      ].filter(([, v]) => v != null);
      // Раскрывающиеся разделы: прошивка / радио / устройство
      const cfg = n.cfg || {};
      const section = (title, rr) => {
        const f = rr.filter(([, v]) => v != null);
        if (!f.length) return "";
        return `<details class="psec"><summary>${esc(title)}</summary>` + f.map(([k, v, c]) =>
          `<div class="prow"><span>${esc(k)}</span><span${c ? ` style="color:${c}"` : ""}>${esc(String(v))}</span></div>`).join("") + `</details>`;
      };
      const yn = (b) => b == null ? null : (b ? "✓" : "✗");
      const fs = fwStatus(cfg.fw);
      const secFw = section(t("secFw"), [
        [t("fwVer"), cfg.fw],
        [t("fwLatest"), fs ? fs.text : (cfg.fw ? "…" : null), fs && fs.color],
      ]);
      const secRadio = section(t("secRadio"), [
        [t("cHops"), cfg.hops],
        [t("cRegion"), cfg.region],
        [t("cPreset"), cfg.preset],
        [t("cTxPower"), cfg.txPower == null ? null : cfg.txPower + " dBm"],
        [t("cTxEnabled"), yn(cfg.txEnabled)],
        [t("cBoosted"), yn(cfg.boostedGain)],
      ]);
      const secDev = section(t("secDevice"), [
        [t("battery"), i.battery == null ? null : i.battery > 100 ? t("wallPower") : i.battery + " %"],
        [t("voltage"), i.voltage == null ? null : i.voltage.toFixed(2) + " V"],
        [t("uptime"), i.uptime == null ? null : fmtUp(i.uptime)],
        [t("chUtil"), i.chUtil == null ? null : i.chUtil.toFixed(1) + " %"],
        [t("ownTx"), i.airTx == null ? null : i.airTx.toFixed(1) + " %"],
        [t("cWifi"), yn(cfg.wifi)], [t("cBt"), yn(cfg.bt)], [t("cPkc"), yn(cfg.pkc)],
        [t("cRebroadcast"), cfg.rebroadcast],
        [t("cNodeInfo"), cfg.nodeInfoSecs == null ? null : Math.round(cfg.nodeInfoSecs / 60) + " " + t("unitMin")],
      ]);
      // для чужих нод: как далеко в меше, через MQTT ли, лицензия; и позиция
      const hopsAway = n.own ? null : (n.hop != null ? n.hop : 0);
      const secMesh = n.own ? "" : section(t("secMesh"), [
        [t("cHopsAway"), hopsAway == null ? null : (hopsAway === 0 ? t("direct") : hopsAway)],
        [t("cViaMqtt"), i.mqtt ? "✓" : null],
        [t("cLicensed"), i.licensed ? "✓" : null],
      ]);
      // Позиция — сразу миникартой (OSM-эмбед с маркером; грузится лениво,
      // только когда раздел раскрыт). Координаты broadcast — приблизительные.
      const secPos = i.lat == null ? "" : (() => {
        const d = 0.008, bb = `${i.lon - d},${i.lat - d},${i.lon + d},${i.lat + d}`;
        return `<details class="psec"><summary>${esc(t("secPos"))}</summary>
          <iframe class="minimap" loading="lazy" referrerpolicy="no-referrer"
            src="https://www.openstreetmap.org/export/embed.html?bbox=${bb}&layer=mapnik&marker=${i.lat},${i.lon}"></iframe>
          <div class="prow"><span>${esc(t("cLat"))} / ${esc(t("cLon"))}</span><span>${i.lat.toFixed(5)}, ${i.lon.toFixed(5)}</span></div>
          ${i.alt == null ? "" : `<div class="prow"><span>${esc(t("cAlt"))}</span><span>${i.alt} m</span></div>`}
          <a class="osmlink" href="https://www.openstreetmap.org/?mlat=${i.lat}&mlon=${i.lon}#map=15/${i.lat}/${i.lon}" target="_blank" rel="noopener noreferrer">${esc(t("openMap"))}</a>
        </details>`;
      })();
      // История — наполняется асинхронно после отрисовки (fillHistory ниже)
      const secHist = `<details class="psec" id="sec-hist"><summary>${esc(t("secHist"))}</summary>`
        + `<div id="hist-body" class="hist-body">…</div></details>`;
      const sections = secFw + secRadio + secMesh + secDev + secPos + secHist;
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
        const val = l.hops ? t("hop", l.hops) : l.snr == null ? t("noData") : `${fmtSnr(l.snr)} dB · ${pctOf(l.snr)}%`;
        return `<div class="leg"><span class="dot" style="background:${col}"></span>
          <span class="who"${ttl ? ` title="${esc(ttl)}"` : ""}>${who}</span>
          <span style="color:${col}">${val}</span>
          ${l.heard ? `<span class="age">${fmtAge(l.heard)}</span>` : ""}
          ${l.snr != null ? `<span class="legspark" data-src="${esc(l.from)}" data-dst="${esc(l.to)}" data-col="${col}"></span>` : ""}</div>`;
      };
      const legs =
        (pairsL.length ? `<div class="psub">${t("twoWay")}</div>` : "") +
        pairsL.map(r => `<div class="pair" data-peer="${esc(r.other)}"><div class="pwho" title="${esc(lbl(r.other))}">⇄ ${esc(shortOf(r.other))}</div>
          ${legLine(r.out, "→")}${legLine(r.in, "←")}</div>`).join("") +
        (singles.length ? `<div class="psub">${t("oneWay")}</div>` : "") +
        `<div class="singles">${singles.map(r =>
          `<div data-peer="${esc(r.other)}">${legLine(r.in || r.out, (r.out ? "→ " : "← ") + esc(shortOf(r.other)), lbl(r.other))}</div>`).join("")}</div>`;
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
        waiting: ["🔑", t("waiting"), "#e0a03c"],
      };
      // читаемая расшифровка причины ошибки доставки (Routing.Error)
      const REASON = {
        NO_ROUTE: { en: "no route", ru: "нет маршрута" },
        GOT_NAK: { en: "rejected (NAK)", ru: "отказ (NAK)" },
        TIMEOUT: { en: "timeout", ru: "таймаут" },
        NO_INTERFACE: { en: "no interface", ru: "нет интерфейса" },
        MAX_RETRANSMIT: { en: "no ack (retries used up)", ru: "нет ACK (попытки исчерпаны)" },
        NO_CHANNEL: { en: "no channel", ru: "нет канала" },
        TOO_LARGE: { en: "message too large", ru: "сообщение слишком большое" },
        NO_RESPONSE: { en: "no response", ru: "нет ответа" },
        DUTY_CYCLE_LIMIT: { en: "airtime limit", ru: "лимит эфирного времени" },
        BAD_REQUEST: { en: "bad request", ru: "неверный запрос" },
        NOT_AUTHORIZED: { en: "not authorized", ru: "не авторизовано" },
        PKI_FAILED: { en: "encryption failed", ru: "шифрование не удалось" },
        PKI_UNKNOWN_PUBKEY: { en: "recipient key unknown", ru: "ключ адресата неизвестен" },
        PKI_SEND_FAIL_PUBLIC_KEY: {
          en: "no recipient key — encrypted DM can't be sent",
          ru: "нет ключа адресата — шифрованный DM не отправить",
        },
        RATE_LIMIT_EXCEEDED: { en: "rate limit", ru: "лимит частоты" },
      };
      const reasonText = (d) => { const r = REASON[d]; return r ? (r[lang] || r.en) : (d || ""); };
      const rowHtml = (m) => {
        const bySelf = m.frm === id;
        const peer = bySelf ? (m.kind === "out" ? m.to : m.node) : m.frm;
        const who = (bySelf ? "→ " : "← ") + esc(m.frmName && !bySelf ? m.frmName : lbl(peer));
        let foot = `<span class="age">${fmtAgo(m.ts)}</span>`;
        let errLine = "";
        if (m.kind === "out") {
          const [g, st, c] = STATUS[m.status] || ["", "", "var(--muted)"];
          const why = m.status === "failed" && m.detail ? reasonText(m.detail) : "";
          foot = `<span class="mstatus" style="color:${c}" title="${esc(st + (why ? " · " + why : ""))}">${g} ${esc(st)}</span>` + foot;
          if (m.status === "failed" || m.status === "waiting") {  // пояснение + ручной повтор
            const wait = m.status === "waiting";
            const note = wait ? t("waitingHint") : (why || "");
            errLine = `<div class="merr${wait ? " wait" : ""}">${note ? esc(note) + " · " : ""}<button class="resend" data-mid="${esc(m.id)}">↻ ${esc(t("resend"))}</button></div>`;
          }
        }
        const canRead = m.kind !== "out" && m.node === id && !m.read;
        // наведение на сообщение подсвечивает собеседника на графе (адресата
        // исходящего / отправителя входящего), если он там есть
        return `<div class="msg ${bySelf ? "self" : "peer"}${canRead ? " unread" : ""}"${peer ? ` data-peer="${esc(peer)}"` : ""}>
          <div class="mh"><span class="mfrom">${who}</span><span class="mfoot">${foot}</span></div>
          ${quoteHtml(m)}
          <div class="mtext">${linkify(m.text)}</div>${errLine}
          ${msgActions(m)}
          ${canRead ? `<div class="mact" data-mid="${esc(m.id)}" data-from="${esc(m.node)}" data-to="${esc(m.frm)}">
            <input class="reply" placeholder="${t("reply")}">
            <button class="msend" title="${esc(t("replyFrom", lbl(m.node)))}">➤</button>
            <button class="mok" title="${t("markRead")}">✓</button></div>` : ""}
        </div>`;
      };
      const msgHtml = thread.length
        ? `<div class="pmsgs"><b>${t("conversation")}</b><div class="thread">${thread.map(rowHtml).join("")}</div></div>`
        : "";

      // «Написать»: этой ноде — от лица любой своей онлайн-ноды. По умолчанию
      // — ближайшая: сначала та, что слышит адресата лучше всех, при отсутствии
      // прямой связи — ближайшая по карте (дистанция ∝ качество сигнала)
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
      const distTo = (v) => Math.hypot(v.cx - n.cx, v.cy - n.cy);
      // ключ адресата нужен ИМЕННО отправителю → сначала ноды с ключом
      const hasKey = (ownId) => (n.keyBy || []).includes(ownId);
      const owners = Object.values(nodes)
        .filter(v => v.own && v.online && v.id !== id)
        .sort((a2, b2) => (hasKey(b2.id) - hasKey(a2.id))
          || (qTo(b2.id) - qTo(a2.id)) || (distTo(a2) - distTo(b2)));
      const replyBar = replyDM ? `<div class="replybar">↩ ${esc((replyDM.text || "").slice(0, 40))}
        <button class="rcancel">×</button></div>` : "";
      const composeHtml = owners.length ? `<div class="pcompose"><b>${t("compose")}</b>
        ${replyBar}<div class="crow">
          <select class="cfrom" title="${t("sendFromWhich")}">${owners.map(o =>
            `<option value="${esc(o.id)}">${esc(o.short || o.label)}${n.key != null && !hasKey(o.id) ? " 🔒" : ""}</option>`).join("")}</select>
          <input class="reply cmsg" placeholder="${t("message")}">
          <button class="csend" title="${t("send")}">➤</button>
        </div></div>` : "";

      // какие разделы были раскрыты — вернём после перерисовки (иначе миникарта
      // и прочее схлопываются на каждом обновлении панели)
      const openSecs = new Set([...panel.querySelectorAll(".psec[open] > summary")]
        .map(s => s.textContent.trim()));
      panel.innerHTML = `
        <button id="pclose" aria-label="${t("close")}">×</button>
        <div class="phead"><img src="${hwImg(n.hw)}" alt="">
          <div><b>${esc(n.label)}</b>${i.long && i.long !== n.label
            ? `<div class="plong">${esc(i.long)}</div>` : ""}</div></div>
        ${rows.map(([k, v, c]) => `<div class="prow"><span>${k}</span><span${c ? ` style="color:${c}"` : ""}>${esc(String(v))}</span></div>`).join("")}
        ${sections}
        ${msgHtml}
        ${composeHtml}
        ${legs ? `<div class="plegs"><b>${t("legs")}</b>${legs}</div>` : ""}`;
      panel.querySelectorAll(".psec").forEach(d => {
        if (openSecs.has(d.querySelector("summary").textContent.trim())) d.open = true;
      });
      panel.classList.add("open");
      // Графики истории (Фаза 1) — асинхронно; кэш в histFetch гасит частые перерисовки
      (async () => {
        const body = panel.querySelector("#hist-body");
        if (body) {
          const res = await histFetch("node?id=" + encodeURIComponent(id) + "&hours=24");
          if (openId === id && panel.querySelector("#hist-body") === body) {
            const series = (res && res.series) || [];
            const hasUp = series.filter(p => p.online != null).length >= 2;
            const hasBatt = series.some(p => p.batt != null);
            if (!hasUp && !hasBatt) {
              body.innerHTML = `<div class="hist-none">${t("histNone")}</div>`;
            } else {
              const up = pctOnline(series);
              const tip = n.own ? t("histReachTip") : t("histHeardTip");
              let html = `<div class="hist-row" title="${esc(tip)}"><span>${esc(n.own ? t("histReach") : t("histHeard"))}</span>`
                + `<b class="hist-val">${up == null ? "—" : up + "%"}</b></div>${upStrip(series)}`;
              if (n.own) {
                const batt = series.map(p => ({ ts: p.ts, v: p.batt }));
                if (batt.some(p => p.v != null))
                  html += `<div class="hist-cap">${esc(t("histBatt"))}</div>` + spark(batt, { color: "#5bc98e", min: 0, max: 100 });
                const chu = series.map(p => ({ ts: p.ts, v: p.chutil }));
                if (chu.some(p => p.v != null))
                  html += `<div class="hist-cap">${esc(t("histChUtil"))}</div>` + spark(chu, { color: "#e0a03c", min: 0 });
              }
              body.innerHTML = html;
            }
          }
        }
        // SNR-спарклайны в плечах (по каждому направлению)
        for (const el of panel.querySelectorAll(".legspark[data-src]")) {
          if (openId !== id) break;
          const r = await histFetch("link?src=" + encodeURIComponent(el.dataset.src)
            + "&dst=" + encodeURIComponent(el.dataset.dst) + "&hours=24");
          const pts = ((r && r.series) || []).map(p => ({ ts: p.ts, v: p.snr }));
          if (pts.filter(p => p.v != null).length >= 2)
            el.innerHTML = spark(pts, { w: 56, h: 14, color: el.dataset.col || "#7bb0ff" });
        }
      })();
      // переписка прокручивается к последнему сообщению
      const th = panel.querySelector(".thread");
      if (th) th.scrollTop = th.scrollHeight;
      panel.querySelector("#pclose").onclick = () => { panel.classList.remove("open"); openId = null; applySel(); };
      // подсветить выбранную ноду на карте; наведение на плечо — синий контур соседа
      applySel();
      panel.querySelectorAll("[data-peer]").forEach(el => {
        el.addEventListener("mouseenter", () => hlPeer(el.dataset.peer, true));
        el.addEventListener("mouseleave", () => hlPeer(el.dataset.peer, false));
      });
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
        setupInput(inp, () => row.querySelector(".msend").click());
      });
      // ручной повтор упавшего DM
      panel.querySelectorAll(".resend").forEach(btn => {
        btn.onclick = async () => {
          btn.disabled = true;
          try {
            await fetch("/api/resend", { method: "POST", body: JSON.stringify({ id: btn.dataset.mid }) });
          } catch { }
          await afterAction();
        };
      });
      const cw = panel.querySelector(".pcompose");
      if (cw) {
        cw.querySelector(".rcancel")?.addEventListener("click", () => { replyDM = null; showPanel(id, true); });
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
              body: JSON.stringify({
                node: cw.querySelector(".cfrom").value, to: id, text,
                replyId: replyDM ? replyDM.pid : null,
              }),
            })).json();
          } catch { }
          btn.disabled = false;
          if (res.ok) { inp.value = ""; replyDM = null; await afterAction(); }
          else alert(t("failedSend") + " " + (res.error || "?"));
        };
        setupInput(cw.querySelector(".cmsg"), () => cw.querySelector(".csend").click());
      }
      // реакции/ответ на сообщения переписки
      const defSender = () => (owners[0] || {}).id;
      wireMsgActions(panel, {
        scope: "dm", to: id, sender: defSender,
        onReply: (pid, text) => { replyDM = { pid, text }; showPanel(id, true); panel.querySelector(".cmsg")?.focus(); },
        refresh: () => afterAction(),
        rerender: () => showPanel(id, true),
      });
    }

    // Наведение/выбор ноды: подсветка соседей + затемнение остального
    const svgEl = document.querySelector("#map svg");
    const clearLit = () => { for (const e of svgEl.querySelectorAll(".lit")) e.classList.remove("lit"); };
    const litNeighborhood = (id) => {
      svgEl.querySelector(`.n-${CSS.escape(id)}`)?.classList.add("lit");
      for (const e of svgEl.querySelectorAll(`.e-${CSS.escape(id)}`)) e.classList.add("lit");
      for (const l of D.links) {
        if (l.from === id) svgEl.querySelector(`.n-${CSS.escape(l.to)}`)?.classList.add("lit");
        if (l.to === id) svgEl.querySelector(`.n-${CSS.escape(l.from)}`)?.classList.add("lit");
      }
    };
    // выбранная нода: то же затемнение, что при наведении, + оранжевый контур
    const applySelection = () => {
      svgEl.classList.remove("focus");
      clearLit();
      for (const g of svgEl.querySelectorAll(".node.selected")) g.classList.remove("selected");
      if (!openId || !nodes[openId]) return;
      svgEl.classList.add("focus");
      svgEl.querySelector(`.n-${CSS.escape(openId)}`)?.classList.add("selected");
      litNeighborhood(openId);
    };
    applySel = applySelection;
    hlPeer = (pid, on) => svgEl.querySelector(`.n-${CSS.escape(pid)}`)?.classList.toggle("peerhi", on);
    for (const g of svgEl.querySelectorAll(".node")) {
      g.addEventListener("click", (ev) => { ev.stopPropagation(); showPanel(g.dataset.id, true); });
      g.addEventListener("mouseenter", () => {
        svgEl.classList.add("focus");
        clearLit();
        litNeighborhood(g.dataset.id);
      });
      g.addEventListener("mouseleave", () => applySelection()); // вернуться к выбору
    }
    applySelection();

    // Легенда: градиент «% от идеала» + прочее + источник данных
    const grad = `linear-gradient(90deg, ${[0, 25, 50, 75, 100]
      .map(p => `hsl(${hue(p)}, 62%, 55%)`).join(", ")})`;
    const stale = D.meta.updatedTs && Date.now() - D.meta.updatedTs > 15 * 60e3;
    document.getElementById("legend").innerHTML = `
      <span class="item">0%<span class="grad" style="background:${grad}"></span>
        ${t("ofIdeal100", fmtSnr(S.floor), fmtSnr(S.ideal))}</span>
      <span class="item"><span class="swatch dashed" style="border-color:#8a8a90"></span>${t("noSnrData")}</span>
      <label class="item toggle" title="${esc(t("showHopsTip"))}">
        <input type="checkbox" id="hopToggle" ${showHops ? "checked" : ""}>
        <span class="swatch dashed" style="border-color:#55555c"></span>${t("showHops")}</label>
      <span class="item">${t("nodeCount", D.nodes.length,
        D.nodes.filter(n => n.own).length, D.nodes.filter(n => !n.own).length)}</span>
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
  let applySel = () => {}, hlPeer = () => {}; // подсветка выбора/наведения (устанавливаются в render)
  let chan = [], chanSig = "", chanDraft = "";  // черновик канала переживает ре-рендеры
  async function refreshMsgs() {
    try {
      const r = await fetch("/api/messages", { cache: "no-store" });
      if (r.ok) msgs = (await r.json()).messages || [];
    } catch { /* hub не запущен — карта работает и без почты */ }
  }

  // ---- Публичный канал (левая панель) ----
  const fmtAgoM = (ts) => {
    const s = Math.max(0, Date.now() / 1e3 - ts);
    const a = s < 90 ? t("justNow") : s < 3600 ? Math.round(s / 60) + " " + t("unitMin")
      : s < 86400 ? Math.round(s / 3600) + " " + t("unitH") : Math.round(s / 86400) + " " + t("unitD");
    return a === t("justNow") ? a : t("ago", a);
  };
  const fmtSnrM = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v);

  // ---- Реакции и цитирование (общее для личных и канала) ----
  const REACTS = ["👍", "👎", "❤️", "🔥", "😂", "🎉", "👏", "👀", "😮", "😢", "🙏", "✅", "❌", "❓", "💯"];
  let replyDM = null, replyChan = null; // {pid, text} — на что отвечаем
  const findMsgByPid = (pid) => pid == null ? null
    : (msgs.find(m => m.pid === pid || m.pktId === pid) || chan.find(m => m.pid === pid) || null);
  const shortName = (id) => {
    const n = (lastLive && lastLive.nodes || []).find(x => x.id === id);
    return n ? (n.short || n.label) : (id || "").slice(-4);
  };
  const quoteHtml = (m) => {
    if (!m.replyTo) return "";
    const q = findMsgByPid(m.replyTo);
    const who = q ? esc(q.frmName || shortName(q.frm) || "") : "";
    const txt = q ? esc((q.text || "").slice(0, 70)) : "…";
    return `<div class="quote">${who ? `<b>${who}</b> ` : ""}${txt}</div>`;
  };
  // реакции «в процессе»: показываем сразу по клику (эфир медленный), пока не
  // подтвердится реальными данными или не протухнет ~25с
  const pendingReacts = new Map(); // `${pid}|${emoji}` -> ts
  // текст сообщения с кликабельными ссылками (экранируем всё, URL → <a>)
  const linkify = (text) => {
    const s = text || "", re = /https?:\/\/[^\s<]+/g;
    let out = "", last = 0, m;
    while ((m = re.exec(s))) {
      let url = m[0];
      const trail = url.match(/[.,;:!?)\]]+$/);  // не захватывать хвостовую пунктуацию
      if (trail) url = url.slice(0, -trail[0].length);
      out += esc(s.slice(last, m.index))
        + `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`;
      last = m.index + url.length;
    }
    return out + esc(s.slice(last));
  };
  const reactionsHtml = (m) => {
    const pid = m.pid ?? m.pktId;
    const r = m.reactions || {};
    const now = Date.now();
    for (const [k, ts] of pendingReacts) {   // снять подтверждённые/протухшие
      const i = k.indexOf("|"), kp = +k.slice(0, i), ke = k.slice(i + 1);
      if (kp === pid && ((r[ke] && r[ke].length) || now - ts > 25000)) pendingReacts.delete(k);
    }
    let html = Object.entries(r).filter(([, w]) => w.length)
      .map(([e, w]) => { const who = w.map(shortName).join(", ");
        return `<span class="rchip" data-emoji="${esc(e)}" title="${esc(who)}">${esc(e)} <span class="rwho">${esc(who)}</span></span>`;
      }).join("");
    for (const k of pendingReacts.keys()) {   // эмодзи «в процессе» для этого сообщения
      const i = k.indexOf("|"), kp = +k.slice(0, i), ke = k.slice(i + 1);
      if (kp === pid && !(r[ke] && r[ke].length))
        html += `<span class="rchip pending" data-emoji="${esc(ke)}" title="${esc(t("reactPending"))}">${esc(ke)} ⏳</span>`;
    }
    return html;
  };
  const msgActions = (m) => {
    const pid = m.pid ?? m.pktId;
    if (pid == null) return "";
    return `<div class="mact2" data-pid="${pid}" data-text="${esc(m.text || "")}">
      ${reactionsHtml(m)}
      <span class="picker">${REACTS.map(e => `<span class="pemoji" data-emoji="${esc(e)}">${e}</span>`).join("")}</span>
      <button class="addreact" title="reaction">＋</button>
      <button class="doreply" title="reply">↩</button></div>`;
  };
  // навесить обработчики реакций/ответа в контейнере
  function wireMsgActions(container, ctx) {
    container.querySelectorAll(".mact2").forEach(row => {
      const pid = +row.dataset.pid;
      row.querySelector(".addreact")?.addEventListener("click", (e) => {
        e.stopPropagation();
        row.querySelector(".picker").classList.toggle("open");
      });
      row.querySelectorAll(".pemoji, .rchip").forEach(el =>
        el.addEventListener("click", async (e) => {
          e.stopPropagation();
          const emoji = el.dataset.emoji;
          pendingReacts.set(pid + "|" + emoji, Date.now());
          ctx.rerender();          // сразу показать «в процессе»
          try {
            await fetch("/api/react", {
              method: "POST", body: JSON.stringify({
                node: ctx.sender(), replyId: pid, emoji,
                channel: ctx.scope === "channel", to: ctx.to || null,
              }),
            });
          } catch { }
          ctx.refresh();           // подтянуть реальные данные
        }));
      row.querySelector(".doreply")?.addEventListener("click", (e) => {
        e.stopPropagation();
        ctx.onReply(pid, row.dataset.text || "");
      });
    });
  }

  function renderChannel() {
    const el = document.getElementById("channel");
    if (!el) return;
    // фокус/курсор перед перерисовкой (сам текст живёт в chanDraft — переживает
    // даже отсутствие композера, когда ноды на миг офлайн)
    const prevIn = el.querySelector(".chmsg-in");
    const foc = prevIn && document.activeElement === prevIn;
    const caret = foc ? [prevIn.selectionStart, prevIn.selectionEnd] : null;
    const nodesById = {};
    (lastLive && lastLive.nodes || []).forEach(n => nodesById[n.id] = n);
    // «реплай мне»: сообщение с цитатой на broadcast МОЕЙ ноды, а автор — не я
    const ownIds = new Set(Object.values(nodesById).filter(n => n.own).map(n => n.id));
    const isReplyToMe = (m) => {
      if (!m.replyTo || ownIds.has(m.frm)) return false;
      const q = findMsgByPid(m.replyTo);
      return !!(q && ownIds.has(q.frm));
    };
    const S = (lastLive && lastLive.meta && lastLive.meta.snrScale) || { floor: -20, ideal: 10 };
    const col = (snr) => snr == null ? "#8a8a90"
      : `hsl(${Math.round(Math.min(1, Math.max(0, (snr - S.floor) / (S.ideal - S.floor))) * 100) * 1.4}, 62%, 55%)`;
    // значение gotBy: новый формат {snr, hops}, старый — просто число snr
    const gv = (v) => (v && typeof v === "object") ? v : { snr: v, hops: null };
    const feed = chan.slice(-100).map(m => {
      const got = Object.entries(m.gotBy || {}).map(([id, v]) => [id, gv(v)])
        .sort((a, b) => (a[1].hops ?? 9) - (b[1].hops ?? 9) || (b[1].snr ?? -99) - (a[1].snr ?? -99))
        .map(([id, g]) => {
          const nm = (nodesById[id] || {}).short || id.slice(-4);
          const hp = g.hops != null ? `<span class="hop">${esc(t("hop", g.hops))}</span>` : "";
          return `<span class="chip"><span class="dot" style="background:${col(g.snr)}"></span>${esc(nm)}${g.snr != null ? " " + fmtSnrM(g.snr) : ""}${hp}</span>`;
        }).join("");
      const r2me = isReplyToMe(m);
      return `<div class="chmsg${r2me ? " reply2me" : ""}">
        <div class="mh"><span class="mfrom">${esc(m.frmName || m.frm)}${r2me
          ? ` <span class="r2me-tag">${esc(t("replyToYou"))}</span>` : ""}</span><span>${fmtAgoM(m.ts)}</span></div>
        ${quoteHtml(m)}
        <div class="mtext">${linkify(m.text)}</div>
        ${got ? `<div class="chgot">${esc(t("gotByLabel"))}: ${got}</div>` : ""}
        ${msgActions(m)}
      </div>`;
    }).join("");
    const owners = (lastLive && lastLive.nodes || []).filter(n => n.own && n.online);
    const composeSel = owners.map(o => `<option value="${esc(o.id)}">${esc(o.short || o.label)}</option>`).join("");
    const replyBar = replyChan ? `<div class="replybar">↩ ${esc((replyChan.text || "").slice(0, 40))}
      <button class="rcancel">×</button></div>` : "";
    el.innerHTML = `
      <div class="chresize" title="${esc(t("resizeTip"))}"></div>
      <div class="chhead"><b>${esc(t("publicChannel"))}</b><button id="chclose" title="${esc(t("close"))}">‹</button></div>
      <div class="chfeed">${feed || `<div class="chempty">${esc(t("chNoMsg"))}</div>`}</div>
      ${owners.length ? `<div class="chcompose">${replyBar}<div class="crow2">
        <select class="chfrom">${composeSel}</select>
        <input class="chmsg-in" placeholder="${esc(t("message"))}">
        <button class="chsend" title="${esc(t("send"))}">➤</button></div></div>` : ""}`;
    const inp0 = el.querySelector(".chmsg-in");  // вернуть черновик и курсор
    if (inp0) {
      inp0.value = chanDraft;
      inp0.addEventListener("input", () => { chanDraft = inp0.value; });
      if (foc) { inp0.focus(); try { inp0.setSelectionRange(caret[0], caret[1]); } catch { } }
    }
    const feedEl = el.querySelector(".chfeed");
    if (feedEl) feedEl.scrollTop = feedEl.scrollHeight;
    el.querySelector("#chclose").onclick = () => setChan(false);
    el.querySelector(".rcancel")?.addEventListener("click", () => { replyChan = null; renderChannel(); });
    wireMsgActions(el, {
      scope: "channel", to: null,
      sender: () => (owners[0] || {}).id,
      onReply: (pid, text) => { replyChan = { pid, text }; renderChannel(); el.querySelector(".chmsg-in")?.focus(); },
      refresh: refreshChan,
      rerender: () => renderChannel(),
    });
    const cs = el.querySelector(".chsend");
    if (cs) cs.onclick = async () => {
      const inp = el.querySelector(".chmsg-in");
      const text = inp.value.trim();
      if (!text) { inp.focus(); return; }
      cs.disabled = true;
      let res = { ok: false };
      try {
        res = await (await fetch("/api/channel", {
          method: "POST", body: JSON.stringify({
            node: el.querySelector(".chfrom").value, text,
            replyId: replyChan ? replyChan.pid : null,
          }),
        })).json();
      } catch { }
      cs.disabled = false;
      if (res.ok) { inp.value = ""; chanDraft = ""; replyChan = null; await refreshChan(); }
      else alert(t("failedSend") + " " + (res.error || "?"));
    };
    setupInput(el.querySelector(".chmsg-in"), () => cs.click());
  }
  function setChan(open) {
    document.body.classList.toggle("chan-collapsed", !open);
    localStorage.setItem("mzChanOpen", open ? "1" : "0");
  }
  async function refreshChan() {
    try {
      const r = await fetch("/api/channel", { cache: "no-store" });
      if (r.ok) chan = (await r.json()).channel || [];
    } catch { return; }
    const sig = chan.map(m => m.id + Object.keys(m.gotBy || {}).length).join(",");
    if (sig !== chanSig) { chanSig = sig; renderChannel(); }
  }
  const refit = () => {
    clearTimeout(rsTimer);
    rsTimer = setTimeout(() => { if (lastLive) render(lastLive); }, 200);
  };
  window.addEventListener("resize", refit);
  new ResizeObserver(refit).observe(document.getElementById("map"));
  // Галочка «многохопы» в легенде: делегируем на #legend (он пересобирается)
  document.getElementById("legend").addEventListener("change", (e) => {
    if (e.target.id !== "hopToggle") return;
    showHops = e.target.checked;
    localStorage.setItem("mzShowHops", showHops ? "1" : "0");
    if (lastLive) render(lastLive);
  });
  // Ширина панели канала: применяем сохранённую + тянем за правый край
  const chEl = document.getElementById("channel");
  const savedChW = parseInt(localStorage.getItem("mzChanW"), 10);
  if (savedChW) chEl.style.width = savedChW + "px";
  let chDrag = false;
  chEl.addEventListener("mousedown", (e) => {
    if (!e.target.classList.contains("chresize")) return;
    e.preventDefault();
    chDrag = true;
    document.body.style.userSelect = "none";
  });
  window.addEventListener("mousemove", (e) => {
    if (!chDrag) return;
    chEl.style.width = Math.max(220, Math.min(680, e.clientX)) + "px";
  });
  window.addEventListener("mouseup", () => {
    if (!chDrag) return;
    chDrag = false;
    document.body.style.userSelect = "";
    localStorage.setItem("mzChanW", parseInt(chEl.style.width, 10) || 300);
  });
  // канал по умолчанию свёрнут; вкладка слева разворачивает
  if (localStorage.getItem("mzChanOpen") !== "1") document.body.classList.add("chan-collapsed");
  document.getElementById("chtab").onclick = () => setChan(true);
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#panel") && !e.target.closest(".node")) {
      document.getElementById("panel").classList.remove("open");
      openId = null;
      applySel();
    }
    if (!e.target.closest("#settings") && !e.target.closest("#gear")) {
      document.getElementById("settings").classList.remove("open");
    }
  });

  // ---- Настройки (⚙): читаются и сохраняются через hub ----
  const SET_FIELDS = [
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
    // редактор подсетей: строка = CIDR + цвет + удалить; цвет клиентский (localStorage)
    const palAt = (i) => SUB_PALETTE[i % SUB_PALETTE.length];
    const subRowHtml = (cidr, color) =>
      `<div class="subrow">
        <input class="sub-cidr" value="${esc(cidr)}" placeholder="10.0.0.0/24">
        <input class="sub-color" type="color" value="${color}">
        <button class="sub-del" type="button" title="${t("remove")}">×</button></div>`;
    const subnetEditor = `<div class="srow scol"><span>${t("fSubnets")}</span>
      <div id="sub-list">${(cfg.subnets || []).map((c, i) =>
        subRowHtml(c, subColors[subnetOf(c)] || palAt(i))).join("")}</div>
      <button id="sub-add" type="button">+ ${t("addSubnet")}</button></div>`;
    setEl.innerHTML = `<button id="sclose" aria-label="${t("close")}">×</button>
      <b class="stitle">${t("settings")}</b>
      <label class="srow"><span>${t("language")}</span>
        <select id="sf-lang">${langOpt("en", "English")}${langOpt("ru", "Русский")}</select></label>`
      + subnetEditor +
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
    // применить цвета подсетей из строк (клиентски) + мгновенно перерисовать
    const applyColors = () => {
      setEl.querySelectorAll("#sub-list .subrow").forEach(row => {
        const cidr = row.querySelector(".sub-cidr").value.trim();
        if (cidr) subColors[subnetOf(cidr)] = row.querySelector(".sub-color").value;
      });
      localStorage.setItem("mzSubColors", JSON.stringify(subColors));
      if (lastLive) render(lastLive);
    };
    const wireSubRow = (row) => {
      row.querySelector(".sub-color").oninput = applyColors;
      row.querySelector(".sub-cidr").oninput = applyColors;
      row.querySelector(".sub-del").onclick = () => { row.remove(); applyColors(); };
    };
    setEl.querySelectorAll("#sub-list .subrow").forEach(wireSubRow);
    setEl.querySelector("#sub-add").onclick = () => {
      const list = setEl.querySelector("#sub-list");
      const box = document.createElement("div");
      box.innerHTML = subRowHtml("", palAt(list.children.length));
      const row = box.firstElementChild;
      list.appendChild(row);
      wireSubRow(row);
      row.querySelector(".sub-cidr").focus();
    };
    setEl.querySelector("#ssave").onclick = async () => {
      const g = (k) => document.getElementById(sfId(k));
      const lines = (el) => el.value.split("\n").map(s => s.trim()).filter(Boolean);
      applyColors();  // зафиксировать цвета подсетей перед сохранением
      const body = {
        subnets: [...setEl.querySelectorAll("#sub-list .sub-cidr")]
          .map(i => i.value.trim()).filter(Boolean),
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
    renderChannel(); // подхватить свежие имена/качество узлов в списке «приняли»
  }

  // Быстрый опрос почты и канала: статусы обновляются в течение секунд
  let msgSig = "";
  async function msgTick() {
    await refreshMsgs();
    const sig = msgs.map(m => m.id + (m.read ? "1" : "0") + (m.status || "")).join(",");
    if (sig !== msgSig && lastLive) { msgSig = sig; render(lastLive); }
    await refreshChan();
  }

  tick();
  refreshChan();
  fetchLatestFw();
  setInterval(fetchLatestFw, 6 * 3600e3);  // раз в 6ч сверять актуальную прошивку
  setInterval(tick, 60e3);
  setInterval(msgTick, 6e3);
})();
