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
CANVAS_H = 1150  # холст рендера 960×CANVAS_H; x/y нод — доли холста
ASPECT = CANVAS_H / 960


def layout(ids, des, seed):
    """Силовая укладка без зон. des: {(a,b): желаемая дистанция в долях
    ширины холста} — из качества плеч; seed: стартовые позиции (прошлый
    прогон — карта не скачет между сканами). Несвязанные пары активно
    расталкиваются (их дистанция ничем не измерена — можно занимать
    свободное место), итог поворачивается по главной оси разброса и
    вписывается в холст ОДНИМ масштабом — пропорции дистанций честные."""
    pos = {}
    for nid in ids:
        x, y = seed.get(nid, (0.5, 0.5))
        pos[nid] = [x, y * ASPECT]
    idl = list(ids)
    steps = 600
    for it in range(steps):
        t = 1.0 - it / steps
        for (a, b), d in des.items():
            if a not in pos or b not in pos:
                continue
            dx = pos[b][0] - pos[a][0]
            dy = pos[b][1] - pos[a][1]
            dist = math.hypot(dx, dy) or 1e-6
            mv = (dist - d) / dist * 0.2 * t
            pos[a][0] += dx * mv; pos[a][1] += dy * mv
            pos[b][0] -= dx * mv; pos[b][1] -= dy * mv
        for i in range(len(idl)):
            for j in range(i + 1, len(idl)):
                a, b = idl[i], idl[j]
                if (a, b) in des or (b, a) in des:
                    continue
                dx = pos[b][0] - pos[a][0]
                dy = pos[b][1] - pos[a][1]
                dist = math.hypot(dx, dy) or 1e-6
                if dist < 0.55:
                    mv = (0.55 - dist) / dist * 0.22 * t
                    pos[a][0] -= dx * mv; pos[a][1] -= dy * mv
                    pos[b][0] += dx * mv; pos[b][1] += dy * mv

    # PCA-поворот: главная ось разброса — вдоль длинной (вертикальной)
    # стороны холста; из четырёх ориентаций берём ближайшую к затравке
    n = len(pos) or 1
    mx = sum(p[0] for p in pos.values()) / n
    my = sum(p[1] for p in pos.values()) / n
    sxx = sum((p[0] - mx) ** 2 for p in pos.values())
    syy = sum((p[1] - my) ** 2 for p in pos.values())
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pos.values())
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    base = math.pi / 2 - theta

    def transformed(rot, mirror):
        c, s = math.cos(rot), math.sin(rot)
        out = {}
        for nid, p in pos.items():
            dx, dy = p[0] - mx, p[1] - my
            if mirror:
                dx = -dx
            out[nid] = [dx * c - dy * s, dx * s + dy * c]
        return out

    def seed_cost(cand):
        cost = c_n = 0
        for nid, p in cand.items():
            if nid in seed:
                sx, sy = seed[nid]
                cost += math.hypot(p[0] - (sx - 0.5), p[1] - (sy * ASPECT - 0.5 * ASPECT))
                c_n += 1
        return cost if c_n else 0

    cands = [transformed(base + k * math.pi, m) for k in (0, 1) for m in (False, True)]
    pts = min(cands, key=seed_cost)

    # равномерное вписывание в поля холста (один масштаб по обеим осям)
    xs = [p[0] for p in pts.values()]
    ys = [p[1] for p in pts.values()]
    dx_span = (max(xs) - min(xs)) or 1e-6
    dy_span = (max(ys) - min(ys)) or 1e-6
    scale = min(0.74 / dx_span, 0.88 * ASPECT / dy_span)
    for nid, p in pts.items():
        x = 0.5 + (p[0] - (min(xs) + max(xs)) / 2) * scale
        y = (0.5 * ASPECT + (p[1] - (min(ys) + max(ys)) / 2) * scale) / ASPECT
        pos[nid] = [x, y]

    # раздвижка перекрывшихся карточек по вертикали
    cw, ch = 215 / 960, 82 / CANVAS_H
    for _ in range(300):
        moved = False
        for i in range(len(idl)):
            for j in range(i + 1, len(idl)):
                a, b = pos[idl[i]], pos[idl[j]]
                dx, dy = b[0] - a[0], b[1] - a[1]
                if abs(dx) < cw and abs(dy) < ch:
                    over = (ch - abs(dy)) / 2
                    s = 1 if dy >= 0 else -1
                    a[1] = max(0.05, min(0.95, a[1] - s * over))
                    b[1] = max(0.05, min(0.95, b[1] + s * over))
                    moved = True
        if not moved:
            break
    return {nid: (p[0], p[1]) for nid, p in pos.items()}


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

    # Линки: запись в nodeDB ноды N про ноду X = «N слышит X» → плечо X→N.
    # Берём только прямые (hopsAway 0) и свежие.
    rf, world_cand = [], {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if oid == nid or not isinstance(e, dict):
                continue
            snr, heard = e.get("snr"), e.get("lastHeard") or 0
            if snr is None or e.get("hopsAway", 0) != 0 or now - heard > max_age:
                continue
            if oid in stat:
                rf.append(dict(frm=oid, to=nid, snr=snr, heard=int(heard)))
            else:
                u = e.get("user") or {}
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

    # Внешний мир: кого слышит больше стационарных нод и громче; topN 0 = все
    topn = CFG["worldTopN"] or len(world_cand)
    world = sorted(world_cand.values(),
                   key=lambda c: (-len(c["hears"]), -c["best"]))[:topn]
    for c in world:
        rf.extend(dict(frm=o, to=n, snr=s, heard=hd) for o, n, s, hd in c["hears"])

    # Кэш: плечи из прошлого live.json, которых не хватило в этом скане
    # (нода могла отдаться лёгким хендшейком без своей базы)
    node_ids = set(stat) | {c["id"] for c in world}
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

    des = {}
    for l in rf:
        if l["snr"] is None:
            continue
        key = tuple(sorted((l["frm"], l["to"])))
        des[key] = min(des.get(key, 9), 0.16 + (1 - pct(l["snr"])) * 0.60)

    # Затравка позиций: прошлый прогон; новичкам — свои ноды по площадкам
    # сверху вниз, внешние возле самого громкого слушателя
    seed = {}
    if prev:
        for n in prev.get("nodes", []):
            if n.get("x") is not None and n.get("y") is not None:
                seed[n["id"]] = (n["x"], n["y"])
    site_y = {s: 0.14 + 0.72 * i / max(1, len(subnets) - 1)
              for i, s in enumerate(subnets)}
    for i, nid in enumerate(sorted(stat)):
        seed.setdefault(nid, (0.25 + 0.25 * (i % 3), site_y[stat[nid]["subnet"]]))
    for c in world:
        if c["id"] in seed:
            continue
        loud = max(c["hears"], key=lambda h: h[2])[1]
        lx, ly = seed.get(loud, (0.5, 0.5))
        j = int(c["id"][1:], 16) % 97 / 97
        seed[c["id"]] = (min(0.9, max(0.1, lx + (j - 0.5) * 0.5)), ly)
    pos = layout(sorted(node_ids), des, seed)

    # Карточки нод
    nodes = []
    for nid in sorted(stat):
        n = stat[nid]
        label = n.get("long") or n["short"]
        sub = n["ip"] if label == n["short"] else f'{n["short"]} · {n["ip"]}'
        x, y = pos[nid]
        node = dict(id=nid, label=label, sub=sub, own=True,
                    x=round(x, 4), y=round(y, 4), online=True, heard=int(now))
        if n.get("hw"):
            node["hw"] = n["hw"]
        info = node_info(n)
        if info:
            node["info"] = info
        if nid in CFG.get("mobile", []):
            node.update(mobile=True, hint="кочующая нода, IP меняется")
        nodes.append(node)
    for c in world:
        label = c.get("long") or c["short"]
        sub = c["id"] if label == c["short"] else f'{c["short"]} · {c["id"]}'
        x, y = pos[c["id"]]
        node = dict(id=c["id"], label=label, sub=sub,
                    x=round(x, 4), y=round(y, 4), heard=c["heard"] or None)
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
        if l.get("heard"):
            d["heard"] = l["heard"]
        out_links.append(d)

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  canvasH=CANVAS_H,
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
