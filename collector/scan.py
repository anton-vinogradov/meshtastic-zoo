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


def build(found):
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
        stat[nid] = dict(id=nid, ip=ip, subnet=subnet_of(ip),
                         short=(info or {}).get("short") or names.get(nid, nid[-4:]),
                         db=(info or {}).get("db") or {})

    # Линки: запись в nodeDB ноды N про ноду X = «N слышит X» → плечо X→N.
    # Берём только прямые (hopsAway 0) и свежие.
    links, world_cand = [], {}
    for nid, n in stat.items():
        for oid, e in n["db"].items():
            if oid == nid or not isinstance(e, dict):
                continue
            snr, heard = e.get("snr"), e.get("lastHeard") or 0
            if snr is None or e.get("hopsAway", 0) != 0 or now - heard > max_age:
                continue
            if oid in stat:
                links.append(dict(link=(oid, nid), snr=snr))
            else:
                u = e.get("user") or {}
                c = world_cand.setdefault(oid, dict(id=oid, best=-99, hears=[],
                                                    short=u.get("shortName") or oid[-4:]))
                c["best"] = max(c["best"], snr)
                c["hears"].append((oid, nid, snr))

    # Внешний мир: кого слышит больше стационарных нод и громче
    world = sorted(world_cand.values(),
                   key=lambda c: (-len(c["hears"]), -c["best"]))[: CFG["worldTopN"]]
    for c in world:
        links.extend(dict(link=(o, n), snr=s) for o, n, s in c["hears"])

    # Зоны: полоса на каждую подсеть, «внешний мир» после первой
    zones, zid = [], {}
    for i, s in enumerate(subnets):
        zid[s] = f"net{i}"
        zones.append(dict(id=f"net{i}", kind="subnet", label=s))
        if i == 0:
            zones.append(dict(id="world", kind="world", label="Внешний мир", h=CFG["worldH"]))

    # Стационарные: равномерно по полосе в порядке IP
    nodes, xpos, by_sub = [], {}, {}
    for n in stat.values():
        by_sub.setdefault(n["subnet"], []).append(n)
    for s, ns in by_sub.items():
        ns.sort(key=lambda n: ipaddress.ip_address(n["ip"]))
        for i, n in enumerate(ns):
            x = (i + 1) / (len(ns) + 1)
            xpos[n["id"]] = x
            node = dict(id=n["id"], label=n["short"], sub=n["ip"], zone=zid[s], x=x)
            if n["id"] in CFG.get("mobile", []):
                node.update(mobile=True, hint="кочующая нода, IP меняется")
            nodes.append(node)

    # Мир: x — среднее по слышащим его нодам, y — лесенкой слева направо
    def avg_x(c):
        return sum(xpos.get(h[1], 0.5) for h in c["hears"]) / len(c["hears"])
    for j, c in enumerate(sorted(world, key=avg_x)):
        y = 0.12 + (0.72 * j / (len(world) - 1) if len(world) > 1 else 0.3)
        nodes.append(dict(id=c["id"], label=c["short"], sub=c["id"], zone="world",
                          x=min(0.9, max(0.1, avg_x(c))), y=round(y, 3)))

    # LAN-цепочки внутри полос
    lan = [dict(link=(a["id"], b["id"]), lan=True)
           for ns in by_sub.values() for a, b in zip(ns, ns[1:])]

    return dict(
        meta=dict(title="meshtastic-zoo", snrScale=CFG["snrScale"],
                  updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  updatedTs=int(now * 1000)),
        zones=zones,
        nodes=nodes,
        links=[dict(**{"from": l["link"][0], "to": l["link"][1]},
                    type="lan" if l.get("lan") else "rf",
                    **({} if l.get("lan") else {"snr": round(l["snr"], 2)}))
               for l in lan + links],
    )


def run_once():
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
    data = build(found)
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
