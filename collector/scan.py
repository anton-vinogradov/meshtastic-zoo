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
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT.parent / "data" / "live.json"
ROW = 3  # внешних нод в ряду (шире — карточки начнут перекрываться)


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

    # «Полка» внешней ноды — по тяге: кого слышат только верхние площадки,
    # тот у верхней полосы (короткие плечи), только нижние — у нижней,
    # смешанные — в середине. Так линии не тянутся через всю карту.
    def pull(c):
        ups = downs = 0
        for h in c["hears"]:
            if stat[h[1]]["subnet"] == subnets[0]:
                ups += 1
            else:
                downs += 1
        return (ups - downs) / max(1, ups + downs)

    def shelf_of(c):
        p = pull(c)
        return "top" if p > 0.5 else "bottom" if p < -0.5 else "mid"

    shelves = {"top": [], "mid": [], "bottom": []}
    for c in world:
        shelves[shelf_of(c)].append(c)
    total_rows = sum(-(-len(v) // ROW) for v in shelves.values()) or 1

    # Зоны — только полосы подсетей; между первой и второй — свободная
    # область (не рисуется), в ней живут внешние ноды. Высота — под полки.
    gap_h = max(CFG["gapH"], 175 * total_rows + 170)
    zones, zid = [], {}
    for i, s in enumerate(subnets):
        zid[s] = f"net{i}"
        zones.append(dict(id=f"net{i}", kind="subnet", label=s))
        if i == 0:
            zones.append(dict(id="gap0", kind="gap", h=gap_h))

    # Стационарные: равномерно по полосе в порядке IP
    nodes, xpos, by_sub = [], {}, {}
    for n in stat.values():
        by_sub.setdefault(n["subnet"], []).append(n)
    for s, ns in by_sub.items():
        ns.sort(key=lambda n: ipaddress.ip_address(n["ip"]))
        for i, n in enumerate(ns):
            x = (i + 1) / (len(ns) + 1)
            xpos[n["id"]] = x
            node = dict(id=n["id"], label=n["short"], sub=n["ip"], zone=zid[s], x=x,
                        online=True, heard=int(now))
            if n.get("hw"):
                node["hw"] = n["hw"]
            info = node_info(n)
            if info:
                node["info"] = info
            if n["id"] in CFG.get("mobile", []):
                node.update(mobile=True, hint="кочующая нода, IP меняется")
            nodes.append(node)

    # Внешние ноды — по полкам, в полке ряды по ROW штук со сдвигом
    # в шахматном порядке; правый коридор оставлен межплощадочным плечам
    def avg_x(c):
        return sum(xpos.get(h[1], 0.5) for h in c["hears"]) / len(c["hears"])
    for name, base, step in (("top", 0.06, 1), ("mid", 0.5, 1), ("bottom", 0.94, -1)):
        lst = sorted(shelves[name], key=avg_x)
        rows = -(-len(lst) // ROW)
        for j, c in enumerate(lst):
            row, col = divmod(j, ROW)
            in_row = min(ROW, len(lst) - row * ROW)
            x = 0.10 + (row % 2) * 0.06 + (0.48 * col / (in_row - 1) if in_row > 1 else 0.22)
            if name == "mid":
                y = base + (row - (rows - 1) / 2) * 0.16
            else:
                y = base + step * row * 0.16
            node = dict(id=c["id"], label=c["short"], sub=c["id"], zone="gap0",
                        x=round(min(x, 0.64), 3), y=round(min(0.96, max(0.04, y)), 3),
                        heard=c["heard"] or None)
            if c.get("hw"):
                node["hw"] = c["hw"]
            info = node_info(c)
            if info:
                node["info"] = info
            nodes.append(node)

    # Кэш: плечи из прошлого live.json, которых не хватило в этом скане
    # (нода могла отдаться лёгким хендшейком без своей базы)
    node_ids = {n["id"] for n in nodes}
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

    # Между своими нодами всегда обе стрелки: недостающее направление — «не изм.»
    sids = sorted(stat)
    for i1, a in enumerate(sids):
        for b in sids[i1 + 1:]:
            fwd, rev = (a, b) in have, (b, a) in have
            if fwd != rev:
                frm, to = ((b, a) if fwd else (a, b))
                rf.append(dict(frm=frm, to=to, snr=None, heard=None))

    # LAN-цепочки внутри полос
    lan_pairs = [(a["id"], b["id"]) for ns in by_sub.values() for a, b in zip(ns, ns[1:])]

    out_links = [{"from": a, "to": b, "type": "lan"} for a, b in lan_pairs]
    for l in rf:
        d = {"from": l["frm"], "to": l["to"], "type": "rf",
             "snr": None if l["snr"] is None else round(l["snr"], 2)}
        if l.get("heard"):
            d["heard"] = l["heard"]
        out_links.append(d)

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  updatedTs=int(now * 1000)),
        zones=zones,
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
