#!/usr/bin/env python3
"""meshtastic-zoo: логика построения карты (модуль для hub.py).

`build_from_store(store, found)` — ЧИТАТЕЛЬ: собирает live.json из персистентного
кеша nodestore (одна строка на узел + таймстемпы), а не из волатильного снимка.
Плюс раскладка (weighted SMACOF), калибровка/геолокация по сигналу. Опрос нод и
запись в кеш — на стороне hub.py (воркеры writer/reader/pruner)."""
import ipaddress
import json
import math
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
# дефолты для окон удержания — чтобы устаревший config.json (без ключей после
# миграции на кеш) не давал None ни в классификации, ни в /api/config.
CFG.setdefault("directWindowH", 24)
CFG.setdefault("formerWindowH", 1)
OUT = ROOT.parent / "data" / "live.json"


def layout(ids, des, wts=None, seed=None):
    """Жадная затравка «от самых связных» + weighted SMACOF.

    seed={id:(x,y)} — позиции из прошлого live.json: засеянные ноды стартуют
    ОТТУДА (стабильны между сканами/рестартами, карта не перетасовывается),
    новые доразмещаются жадно вокруг них, SMACOF лишь дошлифовывает.

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
    # засеянные (из prev) фиксируем ПЕРВЫМИ — стабильная база, новые лягут вокруг
    if seed:
        for nid in ids:
            if nid in seed and seed[nid][0] is not None:
                placed[nid] = (float(seed[nid][0]), float(seed[nid][1]))
    for k, nid in enumerate(order):
        if nid in placed:
            continue
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
    # мягкий якорь к прошлой позиции (временное сглаживание): засеянные ноды
    # тянутся к seed, карта не дрожит скан-к-скану и бесшовна на рестарте, но
    # раскладка всё же адаптируется к изменившимся плечам. Новые нод якоря нет.
    aw = 0.5 if seed else 0.0
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
            if aw and i in seed and seed[i][0] is not None:
                nx += aw * float(seed[i][0]); ny += aw * float(seed[i][1]); den += aw
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
    if len(anchors) < 2:
        return {"ok": False, "reason": "anchors", "nCal": 0}
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

    def coarse_est(nid, allow_single=False):
        """ГРУБАЯ оценка БЕЗ калибровки дальности: взвешенный по громкости
        центроид слышащих площадок (тянется к тем, кто слышит громче) +
        ближайшая площадка. ≥2 площадки → центроид; 1 площадка → «рядом с ней»
        (без триангуляции, unc больше) — но ТОЛЬКО если allow_single (для
        подозрительных GPS, чтобы опровергнуть далёкий заявленный GPS; обычным
        одну площадку не показываем, иначе десятки бессмысленных «у площадки»).
        Неопределённость большая — это «примерно здесь», не точка. coarse=True."""
        hs = heard.get(nid, [])
        sites = {a for a, _ in hs}
        if len(sites) < 2 and not (allow_single and sites):
            return None
        wl = [(a, max(0.1, s + 21.0)) for a, s in hs]
        W = sum(w for _, w in wl)
        la = sum(anchors[a][0] * w for a, w in wl) / W
        lo = sum(anchors[a][1] * w for a, w in wl) / W
        near = max(hs, key=lambda x: x[1])[0]
        if len(sites) < 2:
            unc = 3.0                     # одна площадка — только «рядом», без триангуляции
        else:
            spread = max((_hav_km(anchors[near], anchors[a]) for a, _ in hs), default=0.0)
            unc = max(1.5, spread * 2.0)
        return dict(lat=round(la, 6), lon=round(lo, 6), unc=round(unc, 2),
                    by=len(hs), coarse=True, near=near)

    def apply_coarse():
        c = 0
        for node in nodes:
            nid = node["id"]
            if nid in anchors or node.get("own") or node.get("est"):
                continue
            # GPS-нодам грубую даём ТОЛЬКО если их позиция под подозрением (posSus)
            # — иначе доверяем их GPS и не засоряем карту дублем.
            sus = bool(node.get("posSus"))
            if nid in gps and not sus:
                continue
            e = coarse_est(nid, allow_single=sus)   # одну площадку — только подозрительным
            if e:
                node["est"] = e
                c += 1
        return c

    # Калибровка для ТОЧНОЙ билатерации: регрессия snr = A + B·log10(d), нужен
    # реальный отрицательный наклон И корреляция. Плоская подгонка (B≈0, r≈0 —
    # типично для co-located якорей в шумном городском меше) не даёт колец (всё
    # ~0/300 км) → НЕ молчим, а уходим на грубую оценку центроидом (coarse_est).
    A = B = r = None
    if len(cal) >= 4:
        n = len(cal)
        sx = sum(x for x, _ in cal); sy = sum(y for _, y in cal)
        sxx = sum(x * x for x, _ in cal); sxy = sum(x * y for x, y in cal)
        syy = sum(y * y for _, y in cal)
        den = n * sxx - sx * sx
        if abs(den) >= 1e-9:
            B = (n * sxy - sx * sy) / den
            A = (sy - B * sx) / n
            vy = n * syy - sy * sy
            r = (n * sxy - sx * sy) / math.sqrt(den * vy) if vy > 1e-9 else 0.0
    calib_ok = A is not None and B < 0 and r <= -0.4
    if not calib_ok:                       # точных колец нет → только грубые оценки
        c = apply_coarse()
        return {"ok": bool(c), "reason": "coarse", "nCal": len(cal),
                "r": (round(r, 2) if r is not None else None), "nCoarse": c}

    def snr_km(snr):
        # показатель клампим в безопасный диапазон: без этого при малом |B| он
        # улетает в ±1e3 и 10**exp переполняет float (OverflowError).
        return min(300.0, max(0.01, 10 ** max(-4.0, min(4.0, (snr - A) / B))))

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
    n_coarse = apply_coarse()             # ноды без точной оценки → грубая (центроид)
    n_est = sum(1 for nd in nodes if nd.get("est") and not nd["est"].get("coarse"))
    return {"ok": True, "reason": "ok", "nCal": len(cal), "r": round(r, 2),
            "nEst": n_est, "nCoarse": n_coarse}


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
    HARD = 60.0                 # км: >HARD прямой приём почти наверняка ложь при любом SNR
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
        # Плаузибельная макс. дальность ПРЯМОГО линка убывает с силой сигнала (без
        # калибровки — эвристика): сильный сигнал = близко, слабый может быть дальним.
        # Заявлено СИЛЬНО дальше плаузибельного = позиция врёт/устарела. Раньше порог
        # был фиксированный (>40км или >15км&snr>0) и пропускал средне-громкие на
        # 20–40км (Kuusamo: −3.5дБ на 28.6км — 55% качества, для ручного узла нереально).
        # монотонно по SNR (сильнее → ближе): −20→46км, 0→14, +10→8(пол). Заявлено
        # дальше plausible → сомнительно (med); сильно дальше (×3) или >60км → ложь (high).
        plausible = max(8.0, 14.0 - snr * 1.6)
        level = None
        if dkm > HARD or dkm > plausible * 3.0:
            level = "high"
        elif dkm > plausible:
            level = "med"
        if level:
            n["posSus"] = dict(km=round(dkm, 1), snr=snr, by=by, n=len(hs), level=level)

def build_from_store(store, found=None, xlinks=None):
    """ЧИТАТЕЛЬ (этап 2, воркер №2): собрать live.json из персистентного кеша
    nodestore, а не из волатильного снимка. Статус чёрная/серая — по таймерам
    last_direct (directWindowH / +formerWindowH). Свои ноды/keys_by/cfg/telemetry
    — из живого опроса `found` (свежие), остальное — из кеша. Раскладка засеяна
    сохранёнными x,y. Прунер удаляет вышедших за окно отдельно."""
    now = time.time()
    known, subnets = CFG.get("known", {}), CFG["subnets"]
    directW = CFG.get("directWindowH", 24) * 3600
    formerW = CFG.get("formerWindowH", 1) * 3600

    def subnet_of(ip):
        a = ipaddress.ip_address(ip)
        return next((s for s in subnets if a in ipaddress.ip_network(s)), None)

    # свои ноды из живого опроса
    stat = {}
    for ip, info in (found or {}).items():
        nid = (info or {}).get("id") or known.get(ip)
        if not nid:
            continue
        i = info or {}
        stat[nid] = dict(id=nid, ip=ip, subnet=subnet_of(ip),
                         short=i.get("short") or nid[-4:], hw=i.get("hw"),
                         long=i.get("long"), role=i.get("role"),
                         dm=i.get("dm") or {}, db=i.get("db") or {}, cfg=i.get("cfg") or {})
    keys_by = {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if isinstance(e, dict) and (e.get("user") or {}).get("publicKey"):
                keys_by.setdefault(oid, set()).add(nid)
    own = set(stat)

    def src_of(n):  # поля, которые читает node_info()
        s = dict(long=n.get("name"), role=n.get("role"), mqtt=n.get("mqtt"),
                 lic=n.get("licensed"),
                 dm={"batteryLevel": n.get("batt"), "voltage": n.get("volt"),
                     "channelUtilization": n.get("chutil"), "airUtilTx": n.get("air"),
                     "uptimeSeconds": n.get("uptime")})
        if n.get("lat") is not None:
            s["pos"] = {"latitudeI": int(n["lat"] * 1e7),
                        "longitudeI": int((n.get("lon") or 0) * 1e7), "altitude": n.get("alt")}
        return s

    world, hops_nodes, rf, direct_seen, seed_pos, names = [], [], [], {}, {}, {}
    for n in store:
        nid = n["id"]
        if n.get("name"):
            names[nid] = n["name"]
        if nid in own:
            continue
        if n.get("x") is not None:
            seed_pos[nid] = (n["x"], n["y"])
        ld = n.get("last_direct") or 0
        hb = n.get("heard_by") or {}
        dlegs = [(o, e) for o, e in hb.items()
                 if o in own and not e.get("hops") and e.get("snr") is not None]
        if ld and now - ld < directW and dlegs:            # ЧЁРНАЯ (прямой сосед)
            direct_seen[nid] = int(ld)
            c = src_of(n)
            c.update(id=nid, short=n.get("name") or nid[-4:], hw=n.get("hw"),
                     heard=int(ld), best=max(e["snr"] for _, e in dlegs))
            world.append(c)
            for o, e in dlegs:
                rf.append(dict(frm=nid, to=o, snr=e["snr"], heard=int(e.get("ts") or ld)))
        elif ld and now - ld < directW + formerW:          # СЕРАЯ (бывший 0)
            direct_seen[nid] = int(ld)
            relay = [(o, e) for o, e in hb.items() if o in own and e.get("hops")]
            best = min(relay, key=lambda x: x[1]["hops"]) if relay else None
            hops = best[1]["hops"] if best else (n.get("hops") or 1)
            via = best[0] if best else (next(iter(own), None))
            c = src_of(n)
            c.update(id=nid, short=n.get("name") or nid[-4:], hw=n.get("hw"),
                     hops=hops, heard=int(n.get("last_heard") or ld), silent=not best)
            hops_nodes.append(c)
            if via:
                rf.append(dict(frm=nid, to=via, snr=None, hops=hops,
                               heard=int(n.get("last_heard") or ld)))
        # иначе — за окном, пропускаем (прунер удалит ключ)

    # плечи между своими (из nodeDB) + обе стрелки
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if oid == nid or not isinstance(e, dict) or oid not in own:
                continue
            if not (e.get("hopsAway") or 0) and e.get("snr") is not None:
                rf.append(dict(frm=oid, to=nid, snr=e["snr"],
                               heard=int(e.get("lastHeard") or now)))
    for nid in own:
        direct_seen[nid] = int(now)
    have = {(l["frm"], l["to"]) for l in rf}
    sids = sorted(stat)
    for i1, a in enumerate(sids):
        for b in sids[i1 + 1:]:
            fwd, rev = (a, b) in have, (b, a) in have
            if fwd != rev:
                frm, to = ((b, a) if fwd else (a, b))
                rf.append(dict(frm=frm, to=to, snr=None, heard=None))

    node_ids = own | {c["id"] for c in world} | {c["id"] for c in hops_nodes}
    names.update({nid: (n.get("long") or n["short"]) for nid, n in stat.items()})

    # желаемые дистанции/веса (как в build)
    S = CFG["snrScale"]

    def pct(snr):
        return max(0.0, min(1.0, (snr - S["floor"]) / (S["ideal"] - S["floor"])))

    pair = {}
    for l in rf:
        if l["snr"] is None:
            continue
        k = tuple(sorted((l["frm"], l["to"])))
        p = pair.setdefault(k, {"ps": [], "heard": 0})
        p["ps"].append(pct(l["snr"]))
        p["heard"] = max(p["heard"], l.get("heard") or 0)
    des, wts = {}, {}
    for k, p in pair.items():
        ps = p["ps"]
        q = (max(ps) + (min(ps) if len(ps) > 1 else 0)) / 2 if k[0] in stat and k[1] in stat else max(ps)
        des[k] = 0.12 + (1 - q) * 0.62
        age = now - p["heard"] if p["heard"] else 9e9
        fresh = 1.2 if age < 900 else 1.0 if age < 3600 else 0.8 if age < 6 * 3600 else 0.6
        wts[k] = (1.6 if len(ps) >= 2 else 1.0) * fresh
    D0 = 0.12 + 0.62
    for l in rf:
        if l.get("hops"):
            k = tuple(sorted((l["frm"], l["to"])))
            if k not in des:
                des[k] = D0 * (2 - 0.5 ** (l["hops"] - 1))
                wts[k] = 0.4
    pos = layout(sorted(node_ids), des, wts, seed=seed_pos or None)

    # карточки
    nodes = []
    for nid in sorted(stat):
        n = stat[nid]
        x, y = pos[nid]
        node = dict(id=nid, label=n.get("long") or n["short"], sub=n["ip"], short=n["short"],
                    own=True, x=round(x, 4), y=round(y, 4), online=True, heard=int(now),
                    key=bool(keys_by.get(nid)), keyBy=sorted(keys_by.get(nid, ())))
        if n.get("hw"):
            node["hw"] = n["hw"]
        if node_info(n):
            node["info"] = node_info(n)
        if n.get("cfg"):
            node["cfg"] = n["cfg"]
        if nid in CFG.get("mobile", []):
            node.update(mobile=True, hint="roaming node, IP changes")
        nodes.append(node)
    for c in world + hops_nodes:
        x, y = pos[c["id"]]
        node = dict(id=c["id"], label=c.get("long") or c["short"], sub=c["id"], short=c["short"],
                    x=round(x, 4), y=round(y, 4), heard=c.get("heard") or None,
                    key=bool(keys_by.get(c["id"])), keyBy=sorted(keys_by.get(c["id"], ())))
        if c.get("hops"):
            node["hop"] = c["hops"]
        if c.get("silent"):
            node["silent"] = True
        if c.get("hw"):
            node["hw"] = c["hw"]
        if node_info(c):
            node["info"] = node_info(c)
        nodes.append(node)

    # Призрак переименованной своей ноды: сосед с ИМЕНЕМ как у подключённой своей
    # = её старый node-id (сменился при перепрошивке). Прячем, чтобы не было дубля.
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

    # геолокация: адрес → флаг вранья → оценка. Флаг ДО оценки, чтобы грубую
    # прикидку по сигналу дать и GPS-нодам с ПОДОЗРИТЕЛЬНОЙ позицией (posSus) —
    # показать, где нода на самом деле, против её заявленного далёкого GPS.
    try:
        addr = json.loads((OUT.parent / "geo_addr.json").read_text())
        for n in nodes:
            r = addr.get(n["id"])
            if r and (n.get("info") or {}).get("lat") is None:
                n["addr"] = dict(lat=r["lat"], lon=r["lon"], q=r.get("q"),
                                 verified=bool(r.get("verified")))
    except Exception:
        pass
    try:
        flag_position_lies(nodes, out_links, CFG.get("geo") or {})
    except Exception as e:
        log(f"posflag: {e!r}")
    geocal = None
    try:
        geocal = estimate_positions(nodes, out_links, CFG.get("geo") or {}, xlinks=xlinks)
    except Exception as e:
        log(f"estimate: {e!r}")

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  updatedTs=int(now * 1000), directSeen=direct_seen, names=names,
                  geoCal=geocal),
        nodes=nodes, links=out_links)


