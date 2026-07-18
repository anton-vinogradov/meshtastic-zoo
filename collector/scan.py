#!/usr/bin/env python3
"""meshtastic-zoo: скан подсетей → опрос нод по TCP API → data/live.json.

Один проход:  python3 collector/scan.py
Цикл:         python3 collector/scan.py --loop 300

Ничего не пишет в ноды — только читает. Схема live.json та же, что у
data/topology.js; фронтенд подхватывает файл сам раз в минуту.
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
                res.update(num=num, id=user.get("id"), short=user.get("shortName"),
                           long=user.get("longName"), role=user.get("role"),
                           hw=user.get("hwModel"), dm=my.get("deviceMetrics") or {},
                           db=dict(iface.nodes or {}))
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
    """Карточка телеметрии для панели подробностей."""
    dm = src.get("dm") or {}
    voltage = dm.get("voltage")
    out = dict(long=src.get("long"), role=src.get("role"),
               battery=dm.get("batteryLevel"),
               voltage=voltage if voltage and voltage > 0 else None,
               chUtil=dm.get("channelUtilization"), airTx=dm.get("airUtilTx"),
               uptime=dm.get("uptimeSeconds"))
    return {k: v for k, v in out.items() if v is not None}


def build(found, prev=None):
    """found: {ip: результат query или None} → структура для фронтенда."""
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
                         dm=i.get("dm") or {}, db=i.get("db") or {})

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
            if now - heard > max_age:
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
                                         ("role", u.get("role")), ("dm", e.get("deviceMetrics"))):
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
                                 ("role", u.get("role"))):
                    if val and not c.get(key):
                        c[key] = val

    # Внешний мир: кого слышит больше стационарных нод и громче; topN 0 = все
    topn = CFG["worldTopN"] or len(world_cand)
    world = sorted(world_cand.values(),
                   key=lambda c: (-len(c["hears"]), -c["best"]))[:topn]
    for c in world:
        rf.extend(dict(frm=o, to=n, snr=s, heard=hd) for o, n, s, hd in c["hears"])

    # Многохоповые ноды — ТОЛЬКО те, что были на карте раньше (в прошлом
    # live.json), но напрямую больше не слышны: бывшие прямые соседи,
    # деградировавшие в многохоп. Не весь меш (в nodeDB сотни дальних нод).
    direct_ids = set(stat) | {c["id"] for c in world}
    prev_ids = {n.get("id") for n in (prev.get("nodes", []) if prev else [])}
    hops_nodes = [c for c in hop_cand.values()
                  if c["id"] not in direct_ids and c["via"] and c["id"] in prev_ids]
    for c in hops_nodes:
        rf.append(dict(frm=c["id"], to=c["via"], snr=None, hops=c["hops"], heard=c["heard"]))

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
        if c.get("hw"):
            node["hw"] = c["hw"]
        info = node_info(c)
        if info:
            node["info"] = info
        nodes.append(node)

    out_links = []
    for l in rf:
        d = {"from": l["frm"], "to": l["to"], "type": "rf",
             "snr": None if l["snr"] is None else round(l["snr"], 2)}
        if l.get("hops"):
            d["hops"] = l["hops"]
        if l.get("heard"):
            d["heard"] = l["heard"]
        out_links.append(d)

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  updatedTs=int(now * 1000)),
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
