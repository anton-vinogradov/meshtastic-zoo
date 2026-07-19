#!/usr/bin/env python3
"""meshtastic-zoo: скан подсетей → опрос нод по TCP API → data/live.json.

Один проход:  python3 collector/scan.py
Цикл:         python3 collector/scan.py --loop 300

Читает ноды по TCP; единственная запись — выставление времени своей ноде со
сбитыми часами (lastHeard=0 после ребута, GPS в помещении фикс не берёт).
Схема live.json та же, что у data/topology.js; фронтенд подхватывает файл сам
раз в минуту.
"""
import asyncio
import ipaddress
import json
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT.parent / "data" / "live.json"


def layout(ids, des, wts=None):
    """Жадная затравка «от самых связных» + weighted SMACOF.

    Затравка: ядро — ноды с наибольшей связностью, остальные по убыванию
    (кандидаты на окружностях желаемых дистанций вокруг размещённых
    соседей). Затем — стресс-мажоризация (SMACOF): итеративно двигаем
    каждый узел в взвешенно-оптимальную точку по всем его плечам, минимизируя
    суммарный «стресс» несоответствия дистанций. Веса `wts` — доверие к
    плечу (двусторонние/свежие тянут сильнее). Это математически честнее
    жадной пружины: находит наилучший компромисс, когда SNR-дистанции
    противоречивы (треугольник не сходится из-за мощности/асимметрии).
    Возвращает СЫРЫЕ координаты; посадку под окно делает рендерер."""
    wts = wts or {}
    neigh = {}
    for (a, b), d in des.items():
        q = max(0.0, 1 - (d - 0.12) / 0.62)
        neigh.setdefault(a, {})
        neigh.setdefault(b, {})
        neigh[a][b] = max(neigh[a].get(b, 0.0), q)
        neigh[b][a] = max(neigh[b].get(a, 0.0), q)
    order = sorted(ids, key=lambda n: (len(neigh.get(n, {})),
                                       sum(neigh.get(n, {}).values()), n),
                   reverse=True)

    placed = {}
    for k, nid in enumerate(order):
        if not placed:
            placed[nid] = (0.0, 0.0)
            continue
        anchors = []
        for o, q in neigh.get(nid, {}).items():
            if o in placed:
                key = (nid, o) if (nid, o) in des else (o, nid)
                anchors.append((placed[o], des[key], 0.3 + 0.7 * q))
        pen_w = 0.6 + 2.4 * k / max(1, len(order) - 1)
        cx = sum(p[0] for p in placed.values()) / len(placed)
        cy = sum(p[1] for p in placed.values()) / len(placed)
        cands = []
        if anchors:
            for (ax, ay), d, _ in anchors:
                for j in range(24):
                    ang = j * math.tau / 24
                    cands.append((ax + d * math.cos(ang), ay + d * math.sin(ang)))
        else:
            for r in (0.9, 1.3):
                for j in range(24):
                    ang = j * math.tau / 24
                    cands.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        others = [placed[o] for o in placed if o not in neigh.get(nid, {})]

        def score(c):
            s = 0.0
            for (ax, ay), d, w in anchors:
                s += w * (math.hypot(c[0] - ax, c[1] - ay) - d) ** 2
            for ox, oy in others:
                dist = math.hypot(c[0] - ox, c[1] - oy)
                if dist < 0.42:
                    s += pen_w * (0.42 - dist) ** 2
            return s

        placed[nid] = min(cands, key=score)

    # Weighted SMACOF (стресс-мажоризация). Список плеч на узел:
    # (сосед, желаемая дистанция, вес доверия).
    pos = {nid: list(p) for nid, p in placed.items()}
    idl = list(ids)
    adj = {nid: [] for nid in idl}
    for (a, b), d in des.items():
        if a in adj and b in adj:
            w = wts.get((a, b), 1.0)
            adj[a].append((b, d, w))
            adj[b].append((a, d, w))
    for _ in range(240):
        # шаг мажоризации (Гаусса–Зейделя, обновление на месте — быстрее сходится):
        # каждый узел → взвешенное среднее «идеальных» точек по всем его плечам
        for i in idl:
            nx = ny = den = 0.0
            xi, yi = pos[i]
            for (j, d, w) in adj[i]:
                xj, yj = pos[j]
                dx, dy = xi - xj, yi - yj
                dist = math.hypot(dx, dy) or 1e-6
                nx += w * (xj + d * dx / dist)
                ny += w * (yj + d * dy / dist)
                den += w
            if den > 0:
                pos[i][0] = nx / den
                pos[i][1] = ny / den
        # лёгкое расталкивание несвязанных — только против наложения,
        # реже и слабее, чтобы не искажать связанные дистанции
        for i in range(len(idl)):
            for j in range(i + 1, len(idl)):
                a, b = idl[i], idl[j]
                if (a, b) in des or (b, a) in des:
                    continue
                dx = pos[b][0] - pos[a][0]
                dy = pos[b][1] - pos[a][1]
                dist = math.hypot(dx, dy) or 1e-6
                if dist < 0.28:
                    mv = (0.28 - dist) / dist * 0.06
                    pos[a][0] -= dx * mv; pos[a][1] -= dy * mv
                    pos[b][0] += dx * mv; pos[b][1] += dy * mv
    return {nid: (round(p[0], 4), round(p[1], 4)) for nid, p in pos.items()}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def scan(cidr):
    """IP подсети с открытым портом API."""
    sem = asyncio.Semaphore(128)

    async def probe(ip):
        async with sem:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(str(ip), CFG["port"]), CFG["connectTimeoutS"])
                w.close()
                return str(ip)
            except (OSError, asyncio.TimeoutError):
                return None

    hits = await asyncio.gather(*(probe(ip) for ip in ipaddress.ip_network(cidr).hosts()))
    return [h for h in hits if h]


def query(ip, no_nodes):
    """Опрос одной ноды в отдельном потоке (библиотека умеет зависать)."""
    res, err = {}, []

    def work():
        try:
            import meshtastic.tcp_interface
            iface = meshtastic.tcp_interface.TCPInterface(hostname=ip, noNodes=no_nodes)
            try:
                time.sleep(1.5)  # добрать хвост nodeDB
                my = iface.getMyNodeInfo() or {}
                user = my.get("user") or {}
                num = getattr(getattr(iface, "myInfo", None), "my_node_num", None) or my.get("num")
                nodes_db = dict(iface.nodes or {})
                res.update(num=num, id=user.get("id"), short=user.get("shortName"),
                           long=user.get("longName"), role=user.get("role"),
                           hw=user.get("hwModel"), dm=my.get("deviceMetrics") or {},
                           db=nodes_db)
                # Автосведение часов: T-Beam после перезагрузки теряет время
                # (GPS в помещении фикс не берёт), и тогда ВСЕ метки lastHeard в
                # его nodeDB = 0 — метки времени сообщений/телеметрии кривые, а
                # прямые связи такой ноды держатся лишь на подстраховке в build().
                # Если видим сбитые часы — выставляем текущее (админ-сообщение по
                # локальному TCP, в эфир не идёт). По TCP достижимы только свои
                # ноды, так что пишем лишь в собственное железо.
                heards = [e.get("lastHeard") or 0 for e in nodes_db.values()
                          if isinstance(e, dict)]
                if heards and max(heards) == 0:
                    try:
                        iface.localNode.setTime(int(time.time()))
                        res["clockFixed"] = True
                        log(f"    {ip}: часы были сбиты (lastHeard=0) — выставил время")
                    except Exception as e2:
                        log(f"    {ip}: setTime не удался: {e2!r}")
            finally:
                iface.close()
        except Exception as e:
            err.append(repr(e))

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(CFG["queryTimeoutS"])
    if t.is_alive() or err or res.get("num") is None:
        log(f"    попытка не удалась: {'timeout' if t.is_alive() else (err or ['пусто'])[0]}")
        return None
    if not res.get("id"):
        res["id"] = f"!{res['num']:08x}"
    return res


def query_with_retries(ip):
    """Хрупким нодам (.77 и т.п.) полный хендшейк даём дважды, потом лёгкий."""
    fragile = any(ip.startswith(p) for p in CFG.get("fragile", []))
    for no_nodes in ([False, False, True] if fragile else [False, False]):
        r = query(ip, no_nodes)
        if r:
            return r
        time.sleep(1)
    return None


def node_info(src):
    """Карточка телеметрии/сведений для панели подробностей."""
    dm = src.get("dm") or {}
    voltage = dm.get("voltage")
    pos = src.get("pos") or {}
    lat, lon = pos.get("latitudeI"), pos.get("longitudeI")
    out = dict(long=src.get("long"), role=src.get("role"),
               battery=dm.get("batteryLevel"),
               voltage=voltage if voltage and voltage > 0 else None,
               chUtil=dm.get("channelUtilization"), airTx=dm.get("airUtilTx"),
               uptime=dm.get("uptimeSeconds"),
               mqtt=True if src.get("mqtt") else None,
               licensed=True if src.get("lic") else None,
               lat=lat / 1e7 if lat else None, lon=lon / 1e7 if lon else None,
               alt=pos.get("altitude"))
    return {k: v for k, v in out.items() if v is not None}


def _hav_km(a, b):
    r = math.radians
    dla, dlo = r(b[0] - a[0]), r(b[1] - a[1])
    h = math.sin(dla / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(dlo / 2) ** 2
    return 2 * 6371 * math.asin(min(1, math.sqrt(h)))


def estimate_positions(nodes, links, geo, xlinks=None):
    """Оценка географических позиций GPS-less нод по сигналу (Фазы 5/6-Б).
    Якоря = свои размещённые ноды (geo), SNR→расстояние калибруется на
    GPS-соседях, мультилатерация для нод, слышимых ≥2 якорями.

    Наши якоря — фактически 2 co-located точки, поэтому геометрия ДВУзначна:
    два зеркальных кандидата по разные стороны базы (билатерация). Знак стороны
    разрешаем ЧУЖИМИ звеньями (xlink_hist из traceroute/NeighborInfo): звено
    узла X↔R к позиционированному ретранслятору R (GPS/геокод) → кандидат,
    согласный с дистанцией/стороной R, побеждает. Без свидетельств — честно
    раздуваем неопределённость (как раньше). node['est']={lat,lon,unc,by,side}."""
    anchors = {nid: (g["lat"], g["lon"]) for nid, g in (geo or {}).items()
               if isinstance(g, dict) and g.get("lat") is not None}
    if len(anchors) < 3:
        return
    gps = {}
    for n in nodes:
        i = n.get("info") or {}
        if i.get("lat") is not None:
            gps[n["id"]] = (i["lat"], i["lon"])
    # кто-кого-слышит по якорям + калибровочные пары (якорь↔GPS-сосед)
    heard, cal = {}, []
    for l in links:
        if l.get("type") != "rf" or l.get("snr") is None:
            continue
        frm, to = l.get("from"), l.get("to")
        if to in anchors:
            heard.setdefault(frm, []).append((to, l["snr"]))
            if frm in gps:
                d = _hav_km(anchors[to], gps[frm])
                if d > 0.02:
                    cal.append((math.log10(d), l["snr"]))
    if len(cal) < 4:
        return
    # регрессия snr = A + B·log10(d), B<0 (сигнал падает с расстоянием)
    n = len(cal)
    sx = sum(x for x, _ in cal); sy = sum(y for _, y in cal)
    sxx = sum(x * x for x, _ in cal); sxy = sum(x * y for x, y in cal)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return
    B = (n * sxy - sx * sy) / den
    A = (sy - B * sx) / n
    if B >= 0:
        return

    def snr_km(snr):
        return min(300.0, 10 ** ((snr - A) / B))

    aps = list(anchors.values())
    clat = sum(p[0] for p in aps) / len(aps)
    clon = sum(p[1] for p in aps) / len(aps)
    kmlat = 111.32
    kmlon = 111.32 * math.cos(math.radians(clat))
    xy = lambda la, lo: ((lo - clon) * kmlon, (la - clat) * kmlat)  # noqa: E731

    # позиционированные узлы (кандидаты в «третий якорь»): якоря / GPS-соседи /
    # геокоженные адреса, с весом доверия (сверенный адрес=1, GPS=0.7, мягкий=0.4)
    posref = {a: (*xy(*p), 1.0) for a, p in anchors.items()}
    for nid, p in gps.items():
        posref.setdefault(nid, (*xy(*p), 0.7))
    for node in nodes:
        a = node.get("addr")
        if a and node["id"] not in posref:
            posref[node["id"]] = (*xy(a["lat"], a["lon"]), 1.0 if a.get("verified") else 0.4)

    # индекс чужих звеньев по узлу: {id: [(другой конец, snr), ...]}
    xl_by = {}
    for p in (xlinks or []):
        for k, o in ((p.get("a"), p.get("b")), (p.get("b"), p.get("a"))):
            if k and o:
                xl_by.setdefault(k, []).append((o, p.get("snr")))

    def circ_x(c1, r1, c2, r2):
        """Точки пересечения двух окружностей (2 зеркальных, или 1 компромисс на
        линии если кольца не сходятся) в координатах (x,y) км."""
        dx, dy = c2[0] - c1[0], c2[1] - c1[1]
        D = math.hypot(dx, dy)
        if D < 1e-6:
            return []
        if D > r1 + r2 or D < abs(r1 - r2):   # не пересекаются → точка на линии
            a = max(-r1, min(D + r2, (D * D + r1 * r1 - r2 * r2) / (2 * D)))
            return [(c1[0] + a * dx / D, c1[1] + a * dy / D)]
        a = (D * D + r1 * r1 - r2 * r2) / (2 * D)
        h = math.sqrt(max(0.0, r1 * r1 - a * a))
        mxp, myp = c1[0] + a * dx / D, c1[1] + a * dy / D
        return [(mxp - dy / D * h, myp + dx / D * h),
                (mxp + dy / D * h, myp - dx / D * h)]

    def resolve_side(nid, cands):
        """Выбрать кандидата по чужим звеньям к позиционированным узлам."""
        if len(cands) < 2:
            return cands[0], None
        v = [0.0, 0.0]; resolver = None; rw = 0.0
        for R, snr in xl_by.get(nid, []):
            pr = posref.get(R)
            if not pr or R == nid:
                continue
            rx, ry, w = pr
            d = [math.hypot(c[0] - rx, c[1] - ry) for c in cands]
            if abs(d[0] - d[1]) < 0.4:        # R почти на оси — сторону не различает
                continue
            if snr is not None:               # согласие с кольцом дальности от R
                dt = snr_km(snr)
                b0 = abs(d[0] - dt) <= abs(d[1] - dt)
            else:                             # X в радиусе приёма R → ближе лучше
                b0 = d[0] <= d[1]
            v[0 if b0 else 1] += w
            if w > rw:
                rw, resolver = w, R
        if v[0] + v[1] >= 0.6 and abs(v[0] - v[1]) >= 0.6:
            return (cands[0] if v[0] >= v[1] else cands[1]), resolver
        return cands[0], None                 # не разрешили — произвольная сторона

    for node in nodes:
        nid = node["id"]
        if nid in gps or nid in anchors or node.get("own"):
            continue
        hs = heard.get(nid, [])
        if len(hs) < 2:
            continue
        # co-located якоря кластеризуем в ЭФФЕКТИВНЫЕ точки (центр + кольцо-радиус)
        cl = {}
        for a, s in hs:
            ax_, ay_ = xy(*anchors[a])
            cl.setdefault((round(ax_ / 0.06), round(ay_ / 0.06)), []).append(
                (ax_, ay_, snr_km(s), max(0.1, s + 21.0)))
        eff = []
        for items in cl.values():
            ws = sum(it[3] for it in items)
            eff.append((sum(it[0] * it[3] for it in items) / ws,
                        sum(it[1] * it[3] for it in items) / ws,
                        sum(it[2] * it[3] for it in items) / ws, ws))
        if len(eff) < 2:              # 1 эффективная точка — только кольцо, не ставим
            continue
        side = None
        if len(eff) >= 3:            # настоящая трилатерация — градиентный спуск
            W = sum(e[3] for e in eff)
            x = sum(e[0] * e[3] for e in eff) / W
            y = sum(e[1] * e[3] for e in eff) / W
            for _ in range(400):
                gx = gy = 0.0
                for px, py, r_, w in eff:
                    dd = math.hypot(x - px, y - py) or 1e-6
                    k = 2 * w * (dd - r_) / dd; gx += k * (x - px); gy += k * (y - py)
                x -= 0.08 * gx / W; y -= 0.08 * gy / W
            res = math.sqrt(sum(w * (math.hypot(x - px, y - py) - r_) ** 2
                                for px, py, r_, w in eff) / W)
        else:                        # 2 точки: пересечение колец → зеркало → выбор
            (c1x, c1y, r1, _), (c2x, c2y, r2, _) = eff[0], eff[1]
            cands = circ_x((c1x, c1y), r1, (c2x, c2y), r2)
            if not cands:
                continue
            (x, y), side = resolve_side(nid, cands)
            if len(cands) == 2 and not side:      # неразрешённое зеркало — разброс велик
                res = 0.5 * math.hypot(cands[0][0] - cands[1][0], cands[0][1] - cands[1][1])
            else:                                 # разрешено / единственная — по SNR-шуму
                res = max(0.1, 0.25 * (r1 + r2) / 2)
        node["est"] = dict(lat=round(clat + y / kmlat, 6), lon=round(clon + x / kmlon, 6),
                           unc=round(res, 2), by=len(hs))
        if side:
            node["est"]["side"] = side


def flag_position_lies(nodes, links, geo):
    """Детектор вранья позиций (Фаза 6-А): нода вещает GPS, но мы слышим её
    ПРЯМУЮ (0 хопов) с сигналом, несовместимым с заявленной дальностью. Классика
    — «шутник» с координатами в другом городе (за 50-300 км), которого мы при
    этом принимаем напрямую (значит он рядом). Прямой приём из «другого города»
    физически невозможен для этих раций → флаг. Тест почти без ложных срабатываний
    (порог дальности щедрый), проверять оценку позиции НЕ нужно — только опровергнуть
    заявленную. Ставит node['posSus']={km,snr,by,level}."""
    anchors = {nid: (g["lat"], g["lon"]) for nid, g in (geo or {}).items()
               if isinstance(g, dict) and g.get("lat") is not None}
    if not anchors:
        return
    byid = {n["id"]: n for n in nodes}
    # прямые RF-приёмы: наша якорь-нода `to` услышала `frm` без ретрансляции
    direct = {}
    for l in links:
        if l.get("type") != "rf" or l.get("hops") or l.get("snr") is None:
            continue
        if l.get("to") in anchors:
            direct.setdefault(l.get("from"), []).append((l["to"], l["snr"]))
    HARD, SOFT = 60.0, 40.0     # км: >HARD почти наверняка ложь; >SOFT — сомнительно
    STRONG_KM, STRONG_SNR = 15.0, 0.0   # близкая громкая, а заявлена далеко
    for nid, hs in direct.items():
        n = byid.get(nid)
        if not n or n.get("own"):
            continue
        info = n.get("info") or {}
        if info.get("lat") is None or info.get("mqtt"):   # MQTT-позиция не по эфиру
            continue
        pos = (info["lat"], info["lon"])
        # берём якорь, что слышит ГРОМЧЕ всего — сильнейшее свидетельство близости
        by, snr = max(hs, key=lambda x: x[1])
        dkm = _hav_km(anchors[by], pos)
        level = None
        if dkm > HARD:
            level = "high"
        elif dkm > SOFT or (dkm > STRONG_KM and snr > STRONG_SNR):
            level = "med"
        if level:
            n["posSus"] = dict(km=round(dkm, 1), snr=snr, by=by,
                               n=len(hs), level=level)


def build(found, prev=None, xlinks=None, direct_live=None):
    """found: {ip: результат query или None} → структура для фронтенда.
    xlinks — чужие звенья из history для разрешения зеркала (Фаза 6-Б).
    direct_live — {id: ts} прямых приёмов из ЖИВОГО потока пакетов hub'а: полнее
    периодического снимка nodeDB (тот сэмплит раз в скан и затирается многохопом),
    поэтому directSeen точнее и ноды не мигают в «бывшие соседи» при флапе."""
    now = time.time()
    max_age = CFG["worldMaxAgeH"] * 3600
    known, names = CFG.get("known", {}), CFG.get("names", {})
    subnets = CFG["subnets"]

    def subnet_of(ip):
        a = ipaddress.ip_address(ip)
        return next((s for s in subnets if a in ipaddress.ip_network(s)), None)

    # Стационарные ноды: у кого открыт порт; id — из опроса или из known
    stat = {}
    for ip, info in found.items():
        nid = (info or {}).get("id") or known.get(ip)
        if not nid:
            log(f"  {ip}: порт открыт, но радио-id неизвестен — пропуск")
            continue
        i = info or {}
        stat[nid] = dict(id=nid, ip=ip, subnet=subnet_of(ip),
                         short=i.get("short") or names.get(nid, nid[-4:]),
                         hw=i.get("hw"), long=i.get("long"), role=i.get("role"),
                         dm=i.get("dm") or {}, db=i.get("db") or {}, cfg=i.get("cfg") or {})

    # У каких СВОИХ нод есть публичный ключ каждой ноды. Ключ нужен именно
    # отправителю: без него его DM падает с PKI_SEND_FAIL_PUBLIC_KEY, даже
    # если ключ есть у другой моей ноды. keys_by[oid] = {свои ноды с ключом}.
    keys_by = {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if isinstance(e, dict) and (e.get("user") or {}).get("publicKey"):
                keys_by.setdefault(oid, set()).add(nid)

    # Линки: запись в nodeDB ноды N про ноду X = «N слышит X» → плечо X→N.
    # Прямые (hopsAway 0, с SNR) — цветные; многохоповые (hopsAway ≥ 1) —
    # серые, с числом хопов вместо силы сигнала.
    rf, world_cand, hop_cand = [], {}, {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if oid == nid or not isinstance(e, dict):
                continue
            heard = e.get("lastHeard") or 0
            # lastHeard=0 = у слышащей ноды не выставлены часы (напр. после
            # перепрошивки — как у FC1), а НЕ «древняя запись». Запись взята из
            # живого nodeDB только что опрошенной ноды с валидным SNR/hops —
            # значит текущая: штампуем now, иначе теряем все прямые связи такой
            # ноды (A↔FC1 и т.п.). По возрасту фильтруем только реальные метки.
            if not heard:
                heard = int(now)
            elif now - heard > max_age:
                continue
            hops = e.get("hopsAway", 0) or 0
            snr = e.get("snr")
            u = e.get("user") or {}
            if hops == 0 and snr is not None:
                if oid in stat:
                    rf.append(dict(frm=oid, to=nid, snr=snr, heard=int(heard)))
                else:
                    c = world_cand.setdefault(oid, dict(id=oid, best=-99, heard=0, hears=[],
                                                        short=u.get("shortName") or oid[-4:]))
                    c["best"] = max(c["best"], snr)
                    c["hears"].append((oid, nid, snr, int(heard)))
                    if heard >= c["heard"]:
                        c["heard"] = int(heard)
                        for key, val in (("hw", u.get("hwModel")), ("long", u.get("longName")),
                                         ("role", u.get("role")), ("dm", e.get("deviceMetrics")),
                                         ("mqtt", e.get("viaMqtt")), ("lic", u.get("isLicensed")),
                                         ("pos", e.get("position"))):
                            if val:
                                c[key] = val
            elif hops >= 1:
                # многохоповый: помним минимальное число хопов и через кого
                c = hop_cand.setdefault(oid, dict(id=oid, hops=99, heard=0, via=None,
                                                  short=u.get("shortName") or oid[-4:]))
                if hops < c["hops"] or (hops == c["hops"] and heard >= c["heard"]):
                    c["hops"], c["via"] = hops, nid
                c["heard"] = max(c["heard"], int(heard))
                for key, val in (("hw", u.get("hwModel")), ("long", u.get("longName")),
                                 ("role", u.get("role")), ("mqtt", e.get("viaMqtt")),
                                 ("lic", u.get("isLicensed")), ("pos", e.get("position"))):
                    if val and not c.get(key):
                        c[key] = val

    # Живой поток: ноду, услышанную напрямую < settle назад, держим ПРЯМОЙ
    # (чёрной) с живым плечом, даже если снимок nodeDB показывает её сейчас
    # многохопом — это флап РЧ, не деградация. Иначе такая нода пропадала бы
    # (ни в world, ни серой). doid — своя нода-приёмник, должна быть на связи.
    settle_s = CFG.get("hopSettleMin", 3) * 60
    for did, dv in (direct_live or {}).items():
        dts, dsnr, doid = dv
        if (dsnr is None or now - dts > settle_s or did in stat
                or did in world_cand or doid not in stat):
            continue
        world_cand[did] = dict(id=did, best=dsnr, heard=int(dts), short=did[-4:],
                               hears=[(did, doid, dsnr, int(dts))])

    # Внешний мир: кого слышит больше стационарных нод и громче; topN 0 = все
    topn = CFG["worldTopN"] or len(world_cand)
    world = sorted(world_cand.values(),
                   key=lambda c: (-len(c["hears"]), -c["best"]))[:topn]
    for c in world:
        rf.extend(dict(frm=o, to=n, snr=s, heard=hd) for o, n, s, hd in c["hears"])

    # Многохоповые ноды — прямые соседи, УСТОЙЧИВО деградировавшие в многохоп
    # (а не мгновенно флапнувшие: прямые соседи постоянно скачут 0↔1↔2 хопа —
    # это норма РЧ, проверено на живых nodeDB). `direct_seen` (id → ts, когда
    # ноду в последний раз слышали НАПРЯМУЮ, 0 хопов) переживает live.json.
    # Нода показывается серой, если прямого приёма нет уже settle..stale:
    #   settle (hopSettleMin) — минимум «тишины по прямому», отсекает флап;
    #   stale  (hopStaleMin)  — отдельное «протухание» многохопа: после него
    #                           ноду забываем (дефолт 1 час).
    # Плюс потолок по хопам: 3+ сразу после прямого — это кружной маршрут меша.
    direct_ids = set(stat) | {c["id"] for c in world}
    hop_max = CFG.get("hopMaxShow", 2)
    settle = CFG.get("hopSettleMin", 3) * 60
    stale = CFG.get("hopStaleMin", 60) * 60
    prev_meta = prev.get("meta", {}) if prev else {}
    direct_seen = {k: v for k, v in (prev_meta.get("directSeen") or {}).items()
                   if now - v <= stale}
    for nid in stat:
        direct_seen[nid] = int(now)
    for c in world:  # world = слышно напрямую → обновляем время прямого приёма
        direct_seen[c["id"]] = max(direct_seen.get(c["id"], 0), c["heard"] or int(now))
    # + прямые приёмы из живого потока пакетов (полнее снимка nodeDB): нода,
    # услышанная напрямую хоть раз за окно, остаётся прямой, не мигая в «бывшие».
    # Только с snr — чтобы «недавно прямую» можно было и показать чёрной (плечо);
    # редкий прямой пакет без rxSnr не трогаем, иначе нода зависла бы невидимой.
    for did, dv in (direct_live or {}).items():
        if dv[1] is not None and now - dv[0] <= stale:
            direct_seen[did] = max(direct_seen.get(did, 0), int(dv[0]))
    hops_nodes = []
    for c in hop_cand.values():
        if c["id"] in direct_ids or not c["via"] or c["hops"] > hop_max:
            continue
        ts = direct_seen.get(c["id"])
        if ts is not None and settle <= now - ts <= stale:
            hops_nodes.append(c)
    for c in hops_nodes:
        rf.append(dict(frm=c["id"], to=c["via"], snr=None, hops=c["hops"], heard=c["heard"]))

    # Молчащие бывшие соседи: были прямыми (directSeen в окне settle..stale), но
    # сейчас НЕ слышны никем (нет ни в world, ни в hop_cand) — часто это ноды, с
    # которыми только что был контакт, и они не должны исчезать молча. Держим
    # серыми, если в прошлом live.json есть их карточка (позиция) и свежее
    # (≤cacheMaxAgeH) плечо к уже размещённой ноде — кладём по этому «якорю».
    heard_now = {c["id"] for c in world} | set(hop_cand)
    placed = set(stat) | {c["id"] for c in world} | {c["id"] for c in hops_nodes}
    prev_nodes = {n["id"]: n for n in prev.get("nodes", [])} if prev else {}
    ttl_c = CFG.get("cacheMaxAgeH", 6) * 3600
    for x, ts in list(direct_seen.items()):
        if x in placed or x in heard_now or not (settle <= now - ts <= stale):
            continue
        pn = prev_nodes.get(x)
        if not pn or pn.get("x") is None:
            continue
        # якорь: свежее плечо к размещённой ноде — прямое (snr) в первый цикл
        # молчания ИЛИ уже синтезированное hops-плечо в последующие (иначе
        # молчащая держалась бы лишь один цикл и снова пропадала)
        leg = next((l for l in prev.get("links", [])
                    if l.get("from") == x and l.get("to") in placed and l.get("heard")
                    and now - l["heard"] <= ttl_c
                    and (l.get("snr") is not None or l.get("hops"))), None)
        if not leg:
            continue
        hp = pn.get("hop") or 1
        hops_nodes.append(dict(id=x, hops=hp, via=leg["to"], short=pn.get("short") or x[-4:],
                               long=pn.get("label"), hw=pn.get("hw"), heard=ts, silent=True))
        rf.append(dict(frm=x, to=leg["to"], snr=None, hops=hp, heard=ts))

    # Кэш: плечи из прошлого live.json, которых не хватило в этом скане
    # (нода могла отдаться лёгким хендшейком без своей базы)
    node_ids = direct_ids | {c["id"] for c in hops_nodes}
    have = {(l["frm"], l["to"]) for l in rf}
    if prev:
        ttl = CFG.get("cacheMaxAgeH", 24) * 3600
        for l in prev.get("links", []):
            key = (l.get("from"), l.get("to"))
            if (l.get("type") != "rf" or key in have or l.get("snr") is None
                    or not l.get("heard") or now - l["heard"] > ttl
                    or key[0] not in node_ids or key[1] not in node_ids):
                continue
            rf.append(dict(frm=key[0], to=key[1], snr=l["snr"], heard=l["heard"]))
            have.add(key)

    # Между своими нодами всегда обе стрелки: недостающее направление — «нет данных»
    sids = sorted(stat)
    for i1, a in enumerate(sids):
        for b in sids[i1 + 1:]:
            fwd, rev = (a, b) in have, (b, a) in have
            if fwd != rev:
                frm, to = ((b, a) if fwd else (a, b))
                rf.append(dict(frm=frm, to=to, snr=None, heard=None))

    # Честная раскладка без зон: желаемая дистанция пары — из лучшего
    # качества её плеч (чем зеленее, тем ближе друг к другу)
    S = CFG["snrScale"]

    def pct(snr):
        return max(0.0, min(1.0, (snr - S["floor"]) / (S["ideal"] - S["floor"])))

    # Качество пары + вес надёжности для укладки (weighted SMACOF).
    # Для пары СВОИХ нод обратка знаема — берём среднее двух направлений
    # (отсутствующее = 0, одностороннее соседство сомнительно). У внешних
    # «слух» недоступен — берём лучшее известное. Вес: двусторонние и
    # свежие замеры тянут сильнее (им больше веры), старые/односторонние —
    # мягче.
    pair_info = {}
    for l in rf:
        if l["snr"] is None:
            continue
        key = tuple(sorted((l["frm"], l["to"])))
        pi = pair_info.setdefault(key, {"ps": [], "heard": 0})
        pi["ps"].append(pct(l["snr"]))
        pi["heard"] = max(pi["heard"], l.get("heard") or 0)
    des, wts = {}, {}
    for key, pi in pair_info.items():
        ps = pi["ps"]
        if key[0] in stat and key[1] in stat:
            q = (max(ps) + (min(ps) if len(ps) > 1 else 0)) / 2
        else:
            q = max(ps)
        des[key] = 0.12 + (1 - q) * 0.62
        two_way = len(ps) >= 2
        age = now - pi["heard"] if pi["heard"] else 9e9
        fresh = 1.2 if age < 900 else 1.0 if age < 3600 else 0.8 if age < 6 * 3600 else 0.6
        wts[key] = (1.6 if two_way else 1.0) * fresh

    # Многохоповые: 1 хоп = дистанция при 0% сигнала (D0), дальше каждый хоп
    # добавляет половину предыдущего шага (+50%, +25%, +12.5%…) — не улетают
    D0 = 0.12 + 0.62
    for l in rf:
        if l.get("hops"):
            key = tuple(sorted((l["frm"], l["to"])))
            if key not in des:
                des[key] = D0 * (2 - 0.5 ** (l["hops"] - 1))
                wts[key] = 0.4  # непрямая связь — тянет слабо

    pos = layout(sorted(node_ids), des, wts)

    # Карточки нод
    nodes = []
    for nid in sorted(stat):
        n = stat[nid]
        label = n.get("long") or n["short"]
        x, y = pos[nid]
        node = dict(id=nid, label=label, sub=n["ip"], short=n["short"], own=True,
                    x=round(x, 4), y=round(y, 4), online=True, heard=int(now),
                    key=bool(keys_by.get(nid)), keyBy=sorted(keys_by.get(nid, ())))
        if n.get("hw"):
            node["hw"] = n["hw"]
        info = node_info(n)
        if info:
            node["info"] = info
        if n.get("cfg"):
            node["cfg"] = n["cfg"]
        if nid in CFG.get("mobile", []):
            node.update(mobile=True, hint="roaming node, IP changes")
        nodes.append(node)
    for c in world:
        label = c.get("long") or c["short"]
        x, y = pos[c["id"]]
        node = dict(id=c["id"], label=label, sub=c["id"], short=c["short"],
                    x=round(x, 4), y=round(y, 4), heard=c["heard"] or None,
                    key=bool(keys_by.get(c["id"])), keyBy=sorted(keys_by.get(c["id"], ())))
        if c.get("hw"):
            node["hw"] = c["hw"]
        info = node_info(c)
        if info:
            node["info"] = info
        nodes.append(node)
    for c in hops_nodes:
        label = c.get("long") or c["short"]
        x, y = pos[c["id"]]
        node = dict(id=c["id"], label=label, sub=c["id"], short=c["short"], hop=c["hops"],
                    x=round(x, 4), y=round(y, 4), heard=c["heard"] or None,
                    key=bool(keys_by.get(c["id"])), keyBy=sorted(keys_by.get(c["id"], ())))
        if c.get("silent"):
            node["silent"] = True
        if c.get("hw"):
            node["hw"] = c["hw"]
        info = node_info(c)
        if info:
            node["info"] = info
        nodes.append(node)

    # Призрак переименованной своей ноды: heard-нода с ИМЕНЕМ как у подключённой
    # своей = её старый node-id (сменился при перепрошивке/сбросе). Прячем, чтобы
    # не было дубля «FerretClub 1» на карте.
    own_labels = {n["label"] for n in nodes if n.get("own")}
    kept = [n for n in nodes if n.get("own") or n.get("label") not in own_labels]
    if len(kept) != len(nodes):
        drop = {n["id"] for n in nodes} - {n["id"] for n in kept}
        nodes = kept
        rf = [l for l in rf if l["frm"] not in drop and l["to"] not in drop]

    out_links = []
    for l in rf:
        d = {"from": l["frm"], "to": l["to"], "type": "rf",
             "snr": None if l["snr"] is None else round(l["snr"], 2)}
        if l.get("hops"):
            d["hops"] = l["hops"]
        if l.get("heard"):
            d["heard"] = l["heard"]
        out_links.append(d)

    # Карта id → имя для ВСЕХ известных нод (из nodeDB), чтобы фронт мог
    # подписать даже ноду, которой нет на карте (напр. адресата старого DM,
    # уже ушедшего из видимости). Дёшево — словарь id→имя.
    names = {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if not isinstance(e, dict) or oid in names:
                continue
            u = e.get("user") or {}
            nm = u.get("longName") or u.get("shortName")
            if nm:
                names[oid] = nm
    for nid, n in stat.items():
        names.setdefault(nid, n.get("long") or n.get("short") or nid)

    # Фаза 6-В: геокод адресного имени GPS-less нодам (пул якорей из
    # data/geo_addr.json от hub.geocode_loop) — ДО оценки позиций, чтобы
    # адресные узлы служили ретрансляторами-якорями для разрешения стороны
    try:
        addr = json.loads((OUT.parent / "geo_addr.json").read_text())
        for n in nodes:
            r = addr.get(n["id"])
            if r and (n.get("info") or {}).get("lat") is None:
                n["addr"] = dict(lat=r["lat"], lon=r["lon"], q=r.get("q"),
                                 verified=bool(r.get("verified")))
    except Exception:
        pass
    # Фаза 5/6-Б: оценка позиций GPS-less нод + разрешение зеркала по xlink
    try:
        estimate_positions(nodes, out_links, CFG.get("geo") or {}, xlinks=xlinks)
    except Exception as e:
        log(f"estimate: {e!r}")
    # Фаза 6-А: пометить ноды, чей заявленный GPS противоречит прямому приёму
    try:
        flag_position_lies(nodes, out_links, CFG.get("geo") or {})
    except Exception as e:
        log(f"posflag: {e!r}")

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  updatedTs=int(now * 1000), directSeen=direct_seen, names=names),
        nodes=nodes,
        links=out_links,
    )


def run_once():
    prev = None
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        pass
    found = {}
    for s in CFG["subnets"]:
        log(f"скан {s} …")
        try:
            ips = asyncio.run(scan(s))
        except Exception as e:
            log(f"  скан не удался: {e!r}")
            ips = []
        log(f"  порт {CFG['port']} открыт: {', '.join(ips) or '—'}")
        for ip in ips:
            log(f"  опрос {ip} …")
            found[ip] = query_with_retries(ip)
    data = build(found, prev)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    log(f"→ data/live.json: нод {len(data['nodes'])}, линков {len(data['links'])}")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        period = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            run_once()
            log(f"пауза {period} с")
            time.sleep(period)
    else:
        run_once()
