#!/usr/bin/env python3
"""meshtastic-zoo hub — живой центр зоопарка.

Держит постоянные TCP-соединения со своими нодами и:
- слушает эфир: личные сообщения нодам копятся в data/messages.json;
- по /api/send отправляет ответ отправителю С НУЖНОЙ ноды;
- раз в topoEveryS пересобирает data/live.json из живых nodeDB
  (без переподключений — хрупким нодам так даже легче);
- отдаёт сайт и API на одном порту.

Запуск: python3 collector/hub.py       # сайт и API на :8814
Заменяет связку «http.server + scan.py --loop»; разовый scan.py остаётся.
"""
import asyncio
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import scan  # noqa: E402 — соседний модуль: CFG, build(), log()
import history  # noqa: E402 — лог истории в SQLite (графики/uptime/алерты)
import geocode  # noqa: E402 — геокодинг адресных имён нод (Фаза 6-В)

from pubsub import pub  # noqa: E402
import meshtastic.tcp_interface  # noqa: E402

CFG = scan.CFG
OUT_LIVE = ROOT.parent / "data" / "live.json"
GEO_ADDR = ROOT.parent / "data" / "geo_addr.json"   # {id: {lat,lon,q,verified,name}}
GEO_CACHE = ROOT.parent / "data" / "geo_cache.json"  # сырой кэш Nominatim
OUT_MSGS = ROOT.parent / "data" / "messages.json"
OUT_CHAN = ROOT.parent / "data" / "channel.json"
OUT_TGMAP = ROOT.parent / "data" / "tgmap.json"
PORT = 8814
# что можно менять из UI (остальное — только руками в config.json)
EDITABLE = ["subnets", "snrScale", "worldMaxAgeH", "cacheMaxAgeH",
            "topoEveryS", "rescanS", "mobile", "fragile"]

lock = threading.RLock()
conns = {}     # ip -> {"iface", "id", "num", "light", "last"}
messages = []  # личные: [{id, node, frm, frmName, text, ts, snr, read}]
channel = []   # публичный канал: [{id, pid, frm, frmName, text, ts, ch, gotBy}]
# маппинг для двустороннего Telegram-моста: telegram msg_id → {node, peer, ...}
# (ответ-цитата в Telegram на зеркалированный DM → отправка в меш от той ноды)
tgmap = {"offset": 0, "map": {}}
pending_traces = set()  # id адресатов, чей traceroute-ответ ждём (Фаза 4, ч.3)
traces = {}             # id → {path:[{id,snr}], ts} — результат последней трассировки


def log(msg):
    scan.log(msg)


def atomic_write(path, text):
    """Атомарная запись: temp рядом + fsync + os.replace. При краше/kill/reboot
    в момент записи целевой файл НЕ бьётся — на диске остаётся прошлая целая
    версия (или полностью новая). Без этого оборванный write_text давал пустой
    JSON → load падал → история сообщений терялась при перезагрузке."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_list(path):
    """Прочитать список из JSON; битый (напр. оборванный до атомарности) файл
    не теряем и не затираем — откладываем в .corrupt и стартуем с пустого."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except Exception as e:
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
            log(f"⚠ {path.name} повреждён ({e!r}) → отложен как {path.name}.corrupt")
        except Exception:
            pass
        return []


def load_messages():
    global messages, channel
    messages = load_list(OUT_MSGS)
    channel = load_list(OUT_CHAN)


def load_tgmap():
    global tgmap
    try:
        d = json.loads(OUT_TGMAP.read_text())
        if isinstance(d, dict) and isinstance(d.get("map"), dict):
            tgmap = {"offset": int(d.get("offset", 0)), "map": d["map"]}
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"⚠ tgmap: {e!r}")


def save_tgmap():
    with lock:
        # держим только последние ~500 связок, чтобы файл не пух
        keys = list(tgmap["map"])
        for k in keys[:-500]:
            del tgmap["map"][k]
        atomic_write(OUT_TGMAP, json.dumps(tgmap, ensure_ascii=False))


def save_messages():
    with lock:
        atomic_write(OUT_MSGS, json.dumps(messages, ensure_ascii=False, indent=1))


def save_channel():
    with lock:
        atomic_write(OUT_CHAN, json.dumps(channel, ensure_ascii=False, indent=1))


# ---------- связь с нодами ----------

def connect_node(ip):
    """Подключиться и держать; хрупким — два полных, потом лёгкий."""
    fragile = any(ip.startswith(p) for p in CFG.get("fragile", []))
    for no_nodes in ([False, False, True] if fragile else [False, False]):
        try:
            iface = meshtastic.tcp_interface.TCPInterface(hostname=ip, noNodes=no_nodes)
        except Exception as e:
            log(f"  {ip}: не подключилась ({e.__class__.__name__}), пробую ещё")
            time.sleep(2)
            continue
        time.sleep(1.5)
        my = {}
        try:
            my = iface.getMyNodeInfo() or {}
        except Exception:
            pass
        user = my.get("user") or {}
        num = getattr(getattr(iface, "myInfo", None), "my_node_num", None) or my.get("num")
        nid = user.get("id") or CFG.get("known", {}).get(ip) or (num and f"!{num:08x}")
        with lock:
            conns[ip] = dict(iface=iface, id=nid, num=num, light=no_nodes,
                             last=time.time())
        log(f"⛓ {ip} ({nid}) на связи{' [лёгкая]' if no_nodes else ''}")
        return
    log(f"  {ip}: подключить не удалось, следующая попытка при рескане")


def drop_node(ip):
    with lock:
        ent = conns.pop(ip, None)
    if ent and ent.get("iface"):
        try:
            ent["iface"].close()
        except Exception:
            pass


def ent_by_iface(interface):
    with lock:
        for ip, c in conns.items():
            if c.get("iface") is interface:
                return ip, c
    return None, None


rx_acc = {}          # (своя нода, передатчик) -> [(snr, rssi), ...] прямых приёмов
direct_live = {}     # id -> ts последнего ПРЯМОГО приёма (из живого потока пакетов)
rx_lock = threading.Lock()


def on_receive(packet=None, interface=None):
    try:
        ip, ent = ent_by_iface(interface)
        if not ent:
            return
        ent["last"] = time.time()
        # ЛЮБОЙ пакет от ноды = она сейчас в эфире и свежа в базе ent → шлём ей
        # ждущие DM ровно в этот момент (событийная доставка без вытеснения ключа)
        fn = packet.get("from")
        if isinstance(fn, int):
            try_deliver_waiting(f"!{fn:08x}", ent)
        # Сигнальная жатва (фундамент геолокации): копим ПРЯМЫЕ приёмы
        # (hopStart==hopLimit — пакет не ретранслировался, rxSnr/rxRssi
        # относятся к самому передатчику) — сбрасываются в history в topo_loop
        try:
            hs, hl = packet.get("hopStart"), packet.get("hopLimit")
            snr = packet.get("rxSnr")
            # ПРЯМОЙ приём (hopStart==hopLimit, пакет не ретранслировался).
            if isinstance(fn, int) and fn != ent.get("num") and hs is not None and hs == hl:
                # directSeen из живого потока: точный момент прямого приёма (полнее
                # снимка nodeDB, который сэмплит раз в скан и затирается многохопом).
                # Храним (ts, snr, своя-нода-приёмник) — чтобы «недавно прямую» ноду
                # можно было показать ЧЁРНОЙ с живым плечом, а не потерять.
                with rx_lock:
                    direct_live[f"!{fn:08x}"] = (
                        time.time(), float(snr) if snr is not None else None, ent.get("id"))
                    if snr is not None:                # + сигнальная жатва
                        rx_acc.setdefault((ent.get("id"), f"!{fn:08x}"), []).append(
                            (float(snr), float(packet.get("rxRssi"))
                             if packet.get("rxRssi") is not None else None))
        except Exception:
            pass
        dec = (packet or {}).get("decoded") or {}
        if dec.get("portnum") == "ROUTING_APP":
            # квитанция: матчим requestId с id отправленных пакетов
            rid = dec.get("requestId") or packet.get("requestId")
            if not rid:
                return
            err = (dec.get("routing") or {}).get("errorReason")
            ok = err in (None, 0, "NONE")
            changed = False
            pki_fail = None  # нет ключа адресата → запросим и повторим (раз)
            with lock:
                for m in messages:
                    if not (m.get("kind") == "out" and m.get("pktId") == rid
                            and m.get("status") == "sent"):
                        continue
                    if ok:
                        m["status"] = "delivered"
                    elif (str(err) == "PKI_SEND_FAIL_PUBLIC_KEY" and CFG.get("autoKeyRequest", True)
                          and m.get("tries", 0) < CFG.get("keyMaxTries", 12)):
                        # ключа пока нет — ждём появления адресата в эфире
                        m["status"] = "waiting"
                        m.pop("detail", None)
                        m.setdefault("waitSince", int(time.time()))
                        pki_fail = m
                    else:
                        m["status"], m["detail"] = "failed", str(err)
                    changed = True
            if changed:
                save_messages()
                log(f"{'✓ доставлено' if ok else '✗ NAK ' + str(err)} (rid={rid})")
            if pki_fail:
                solicit_key(pki_fail["to"])  # попросить ключ; доставим, как услышим
            return
        if dec.get("portnum") == "TRACEROUTE_APP":
            # ответ на нашу трассировку: путь + SNR по хопам (Фаза 4, ч.3)
            fn, tn = packet.get("from"), packet.get("to")
            frm = f"!{fn:08x}" if isinstance(fn, int) else str(fn)
            try:
                from meshtastic import mesh_pb2
                import google.protobuf.json_format as jf
                rd = mesh_pb2.RouteDiscovery()
                rd.ParseFromString(dec.get("payload") or b"")
                ad = jf.MessageToDict(rd)
            except Exception as e:
                log(f"trace parse: {e!r}")
                ad = {}
            nums = [tn] + [int(x) for x in ad.get("route", [])] + [fn]  # мы → хопы → цель
            snrs = ad.get("snrTowards", [])
            # Жатва ЧУЖИХ звеньев (Фаза 6, фундамент): в маршруте видны связи
            # соседей МЕЖДУ СОБОЙ (nums[i]→nums[i+1] услышан на snrs[i]), которых
            # нет в nodeDB. Пишем ЛЮБУЮ подслушанную RouteDiscovery (и не нашу),
            # обе стороны (routeBack/snrBack) — каждое звено от узла с известной
            # позицией расширяет геометрию геолокации.
            try:
                hx, hts = [], int(time.time())
                for seq, sl in ((nums, snrs),
                                ([fn] + [int(x) for x in ad.get("routeBack", [])] + [tn],
                                 ad.get("snrBack", []))):
                    for i in range(len(seq) - 1):
                        if (i < len(sl) and sl[i] != -128 and isinstance(seq[i], int)
                                and isinstance(seq[i + 1], int)):
                            hx.append((hts, f"!{seq[i]:08x}", f"!{seq[i + 1]:08x}",
                                       round(sl[i] / 4, 2), "tr"))
                if hx:
                    history.record_xlinks(hx)
            except Exception:
                pass
            with lock:
                waited = frm in pending_traces
            if not waited:
                return
            path = []
            for i, num in enumerate(nums):
                e = {"id": f"!{num:08x}" if isinstance(num, int) else str(num)}
                if i > 0 and i - 1 < len(snrs) and snrs[i - 1] != -128:
                    e["snr"] = round(snrs[i - 1] / 4, 2)
                path.append(e)
            with lock:
                pending_traces.discard(frm)
                traces[frm] = {"path": path, "ts": int(time.time())}
            log(f"🧭 traceroute {frm}: {' → '.join(p['id'] for p in path)}")
            return
        if dec.get("portnum") == "NEIGHBORINFO_APP":
            # пассивная жатва: часть нод сама вещает список соседей с SNR —
            # бесплатные (0 эфира с нашей стороны) чужие звенья для геолокации
            try:
                ni = dec.get("neighborinfo")
                if not ni:
                    from meshtastic import mesh_pb2
                    import google.protobuf.json_format as jf
                    m = mesh_pb2.NeighborInfo()
                    m.ParseFromString(dec.get("payload") or b"")
                    ni = jf.MessageToDict(m)
                rep = ni.get("nodeId")
                hts, hx = int(time.time()), []
                for nb in ni.get("neighbors", []) or []:
                    nid, s = nb.get("nodeId"), nb.get("snr")
                    if isinstance(rep, int) and isinstance(nid, int) and s is not None:
                        hx.append((hts, f"!{nid:08x}", f"!{rep:08x}", float(s), "ni"))
                if hx:
                    history.record_xlinks(hx)
                    log(f"🌐 neighborinfo !{rep:08x}: {len(hx)} звеньев")
            except Exception:
                pass
            return
        if dec.get("portnum") != "TEXT_MESSAGE_APP":
            return
        to = packet.get("to")
        frm_num = packet.get("from")
        frm = f"!{frm_num:08x}" if isinstance(frm_num, int) else str(frm_num)
        u = ((dict(interface.nodes or {}).get(frm) or {}).get("user") or {})
        frm_name = u.get("longName") or u.get("shortName") or frm
        text = dec.get("text") or ""
        reply_id = dec.get("replyId") or dec.get("reply_id")

        # РЕАКЦИЯ (тапбэк): emoji=1 + reply_id → привязать к целевому сообщению
        if dec.get("emoji") and reply_id:
            with lock:
                tgt = find_by_pid(reply_id)
                if tgt is not None:
                    who = tgt.setdefault("reactions", {}).setdefault(text, [])
                    if frm not in who:
                        who.append(frm)
            save_messages()
            save_channel()
            log(f"👍 {frm_name} {text} → pkt {reply_id}")
            return

        if to in (0xFFFFFFFF, 4294967295, "^all", "!ffffffff"):
            # публичный канал (broadcast): один пакет слышат несколько наших нод —
            # группируем по id и копим, кто именно принял (с SNR)
            pid = packet.get("id")
            with lock:
                m = next((x for x in channel if pid and x.get("pid") == pid), None)
                if m is None:
                    m = dict(id=f"ch·{pid or int(time.time() * 1000)}", pid=pid, frm=frm,
                             frmName=frm_name, text=text, ts=int(time.time()),
                             ch=packet.get("channel", 0), gotBy={})
                    if reply_id:
                        m["replyTo"] = reply_id
                    channel.append(m)
                    del channel[:-300]
                # хопы приёма = hopStart − hopLimit (0 = услышали напрямую).
                # один пакет может прийти несколькими путями (оригинал +
                # ретрансляции) — держим ЛУЧШИЙ (наименьшее число хопов)
                hs, hl = packet.get("hopStart"), packet.get("hopLimit")
                hops = hs - hl if isinstance(hs, int) and isinstance(hl, int) and hs >= hl else None
                prev = m["gotBy"].get(ent["id"])
                prev_hops = prev.get("hops") if isinstance(prev, dict) else None
                if prev is None or (hops is not None and (prev_hops is None or hops < prev_hops)):
                    m["gotBy"][ent["id"]] = {"snr": packet.get("rxSnr"), "hops": hops}
            save_channel()
            log(f"📡 канал: {frm_name} → всем (принял {ent['id']}): {text[:50]!r}")
            return

        if to != ent.get("num"):
            return  # чужой DM — не наш
        msg = dict(id=f'{ent["id"]}·{packet.get("id")}', pid=packet.get("id"),
                   node=ent["id"], frm=frm, frmName=frm_name, text=text,
                   ts=int(time.time()), snr=packet.get("rxSnr"), read=False)
        if reply_id:
            msg["replyTo"] = reply_id
        with lock:
            if any(m["id"] == msg["id"] for m in messages):
                return
            messages.append(msg)
        save_messages()
        log(f"✉ {msg['frmName']} → {ent['id']}: {msg['text'][:60]!r}")
        if (CFG.get("alerts") or {}).get("dm", True):
            threading.Thread(target=mirror_dm, daemon=True,
                             args=(ent["id"], frm, msg["frmName"], msg.get("pid"), text)).start()
    except Exception as e:
        log(f"on_receive: {e!r}")


def find_by_pid(pid):
    """Сообщение (личное или канал) по mesh-id пакета — для реакций/цитат."""
    if not pid:
        return None
    for m in messages:
        if m.get("pid") == pid or m.get("pktId") == pid:
            return m
    for m in channel:
        if m.get("pid") == pid:
            return m
    return None


def send_reaction(iface, dest, emoji_char, reply_id):
    """Тапбэк-реакция: TEXT-пакет с emoji=1 и reply_id (в API нет — строим сами)."""
    import meshtastic.mesh_interface as mi
    mp = mi.mesh_pb2.MeshPacket()
    mp.decoded.payload = emoji_char.encode("utf-8")
    mp.decoded.portnum = mi.portnums_pb2.PortNum.TEXT_MESSAGE_APP
    mp.decoded.reply_id = int(reply_id)
    mp.decoded.emoji = 1
    mp.id = iface._generatePacketId()
    return iface._sendPacket(mp, dest, wantAck=False)


def ent_by_id(node_id):
    with lock:
        return next((c for c in conns.values()
                     if c.get("id") == node_id and c.get("iface")), None)


def best_sender_for(to):
    """Своя онлайн-нода для отправки DM адресату. Приоритет: у кого ЕСТЬ ключ
    адресата (иначе PKI-сбой гарантирован), среди них — кто громче слышит его
    напрямую. Если ни у кого нет ключа — самый громкий (дальше сработает
    автозапрос ключа). Возвращает id ноды или None."""
    try:
        live = json.loads(OUT_LIVE.read_text())
    except Exception:
        return None
    own = {n["id"] for n in live.get("nodes", []) if n.get("own")}
    tgt = next((n for n in live.get("nodes", []) if n.get("id") == to), None)
    keyby = set(tgt.get("keyBy", []) if tgt else [])
    # кандидаты: свои онлайн-ноды, слышащие адресата напрямую (snr) → (snr, id, есть_ключ)
    cands = [(l["snr"], l["to"], l["to"] in keyby) for l in live.get("links", [])
             if (l.get("from") == to and l.get("to") in own and not l.get("hops")
                 and l.get("snr") is not None and ent_by_id(l["to"]))]
    if not cands:  # напрямую никто не слышит — взять любую онлайн-ноду с ключом
        return next((oid for oid in keyby if ent_by_id(oid)), None)
    with_key = [c for c in cands if c[2]]
    return max(with_key or cands, key=lambda c: c[0])[1]


def request_key(ent, to):
    """Солицит ключа адресата: шлём ему наш NodeInfo с want_response — он
    отвечает своим NodeInfo (в нём publicKey), и наша нода узнаёт его ключ."""
    import meshtastic.mesh_interface as mi
    iface = ent["iface"]
    mu = iface.getMyUser() or {}
    u = mi.mesh_pb2.User(id=mu.get("id") or ent.get("id") or "",
                         long_name=mu.get("longName") or "",
                         short_name=mu.get("shortName") or "")
    iface.sendData(u, destinationId=to, wantResponse=True,
                   portNum=mi.portnums_pb2.PortNum.NODEINFO_APP)


def resend(m, auto=False):
    """Переслать исходящее сообщение тому же адресату — но С ЛУЧШЕЙ ноды (кто
    сильнее слышит адресата), а не обязательно с исходной. Обновляем ту же
    запись (в т.ч. время — видно, что повтор реально ушёл), а не плодим новую."""
    frm = best_sender_for(m.get("to")) or m.get("frm")
    ent = ent_by_id(frm) or ent_by_id(m.get("frm"))
    if not ent:
        with lock:
            m["status"] = "failed"
        save_messages()
        return False
    frm = ent["id"]
    try:
        pkt = ent["iface"].sendText(m["text"], destinationId=m["to"], wantAck=True,
                                    replyId=m.get("replyTo") or None)
        with lock:
            m["frm"] = frm  # фактический отправитель
            m["pktId"] = getattr(pkt, "id", None)
            m["status"] = "sent"
            m["ts"] = int(time.time())
            m.pop("detail", None)
            m["tries"] = 0  # ручной повтор → сбросить счётчик, снова разрешить ожидание
            m.pop("waitSince", None)
        save_messages()
        log(f"↻ ретрай {frm} → {m['to']}: {m['text'][:40]!r}")
        return True
    except Exception as e:
        with lock:
            m["status"] = "failed"
            m["detail"] = str(e)
        save_messages()
        return False


def solicit_key(to):
    """Запросить ключ адресата (NodeInfo) с ноды, что лучше его слышит — чтобы
    он вскоре прислал свой NodeInfo (с ключом); его следующий пакет и станет
    моментом доставки (см. try_deliver_waiting)."""
    ent = ent_by_id(best_sender_for(to) or "")
    if not ent:
        return
    try:
        request_key(ent, to)
        log(f"🔑 запросил ключ у {to} (через {ent['id']})")
    except Exception as e:
        log(f"🔑 запрос ключа не удался: {e!r}")


def send_from(ent, m):
    """Отправить ждущий DM ИМЕННО с ноды ent: она только что услышала адресата,
    он свеж в её базе (с ключом), пока не вытеснило из 250-лимита."""
    try:
        pkt = ent["iface"].sendText(m["text"], destinationId=m["to"], wantAck=True,
                                    replyId=m.get("replyTo") or None)
        with lock:
            m["frm"], m["pktId"] = ent["id"], getattr(pkt, "id", None)
            m["status"], m["ts"] = "sent", int(time.time())
            m.pop("detail", None)
        save_messages()
        log(f"🎯 доставка по контакту: {ent['id']} → {m['to']}: {m['text'][:40]!r}")
    except Exception as e:
        with lock:
            m["status"] = "waiting"
        save_messages()
        log(f"🎯 контакт-отправка не удалась: {e!r}")


def try_deliver_waiting(frm_id, ent):
    """Адресат вышел в эфир (услышан ent) → шлём ждущие ему DM ПРЯМО СЕЙЧАС,
    пока он свеж в базе ent. Событийная доставка вместо слепых ретраев."""
    now = time.time()
    todo = []
    with lock:
        for m in messages:
            if (m.get("kind") == "out" and m.get("to") == frm_id
                    and m.get("status") == "waiting"
                    and now - m.get("lastTry", 0) >= 6):  # не чаще раза в 6с на сообщение
                m["lastTry"], m["tries"] = now, m.get("tries", 0) + 1
                todo.append(m)
    for m in todo:
        threading.Thread(target=send_from, args=(ent, m), daemon=True).start()


def on_lost(interface=None):
    ip, ent = ent_by_iface(interface)
    if ip:
        log(f"⛓✗ {ip}: соединение потеряно")
        drop_node(ip)


async def port_open(ip):
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection(str(ip), CFG["port"]), CFG["connectTimeoutS"])
        w.close()
        return str(ip)
    except (OSError, asyncio.TimeoutError):
        return None


def keeper():
    """Скан подсетей на новые ноды + вотчдог зависших соединений.
    Свои живые соединения портом НЕ трогаем — второй TCP-клиент
    вышибает первого."""
    while True:
        try:
            with lock:
                busy = {ip for ip, c in conns.items() if c.get("iface")}
            hosts = [ip for s in CFG["subnets"]
                     for ip in ipaddress.ip_network(s).hosts() if str(ip) not in busy]

            async def probe_all():
                sem = asyncio.Semaphore(128)

                async def one(h):
                    async with sem:
                        return await port_open(h)
                return [r for r in await asyncio.gather(*(one(h) for h in hosts)) if r]

            for ip in asyncio.run(probe_all()):
                if ip not in conns:
                    threading.Thread(target=connect_node, args=(ip,), daemon=True).start()
            # вотчдог: давно молчащие соединения пересобираем
            now = time.time()
            for ip, c in list(conns.items()):
                if c.get("iface") and now - c.get("last", 0) > 900:
                    log(f"⛓? {ip}: тишина >15 мин, переподключаю")
                    drop_node(ip)
        except Exception as e:
            log(f"keeper: {e!r}")
        time.sleep(CFG.get("rescanS", 300))


# ---------- топология ----------

def node_cfg(iface):
    """Метаданные + LoRa/device-конфиг СВОЕЙ ноды с подключённого iface —
    для раскрывающихся разделов в панели. Всё из памяти, без сети."""
    from meshtastic import config_pb2
    out = {}
    try:
        md = getattr(iface, "metadata", None)
        if md and getattr(md, "firmware_version", ""):
            out.update(fw=md.firmware_version, wifi=bool(md.hasWifi),
                       bt=bool(md.hasBluetooth), pkc=bool(md.hasPKC))
        lc = getattr(getattr(iface, "localNode", None), "localConfig", None)
        if lc and lc.HasField("lora"):
            lo, R = lc.lora, config_pb2.Config.LoRaConfig
            out.update(hops=lo.hop_limit, region=R.RegionCode.Name(lo.region),
                       preset=R.ModemPreset.Name(lo.modem_preset) if lo.use_preset else "custom",
                       txPower=lo.tx_power, txEnabled=bool(lo.tx_enabled),
                       boostedGain=bool(lo.sx126x_rx_boosted_gain))
        if lc and lc.HasField("device"):
            dv, D = lc.device, config_pb2.Config.DeviceConfig
            out.update(deviceRole=D.Role.Name(dv.role),
                       rebroadcast=D.RebroadcastMode.Name(dv.rebroadcast_mode),
                       nodeInfoSecs=dv.node_info_broadcast_secs)
    except Exception as e:
        log(f"node_cfg: {e!r}")
    return out


def snapshot(ent):
    iface = ent["iface"]
    my = {}
    try:
        my = iface.getMyNodeInfo() or {}
    except Exception:
        pass
    user = my.get("user") or {}
    return dict(num=ent.get("num"), id=ent.get("id"), short=user.get("shortName"),
                long=user.get("longName"), role=user.get("role"),
                hw=user.get("hwModel"), dm=my.get("deviceMetrics") or {},
                db=dict(iface.nodes or {}), cfg=node_cfg(iface))


# ---------- Telegram-алерты (Фаза 2) ----------
# Отправка через твой telegram.sh (ретраи/прокси/очередь). Токен и чат — в
# config.json (alerts.tgToken/tgChat, gitignore); пусто → молчим. Отдельный бот.
ALERT_BIN = shutil.which("telegram") or "/opt/telegram.sh-repo/telegram"
_batt_alerted = set()  # id нод, по которым уже слали «низкий заряд» (антидребезг)


def alert(text):
    a = CFG.get("alerts") or {}
    if not a.get("enabled", True):
        return
    tok, chat = a.get("tgToken"), a.get("tgChat")
    if not tok or not chat:
        return  # не настроено — тихо выходим
    def _send():
        try:
            cmd = [ALERT_BIN, "-t", str(tok), "-a", "3"]
            for c in str(chat).replace(",", " ").split():
                cmd += ["-c", c]
            cmd.append(text)
            r = subprocess.run(cmd, timeout=90, capture_output=True)
            if r.returncode != 0:
                log(f"alert rc={r.returncode}: {r.stderr.decode('utf-8', 'replace')[:120]}")
        except Exception as e:
            log(f"alert: {e!r}")
    threading.Thread(target=_send, daemon=True).start()


def check_batt(data):
    """Низкий заряд своих нод: шлём раз при переходе ниже порога, перевзвод —
    когда заряд поднимется выше порога+5% (гистерезис против дребезга)."""
    a = CFG.get("alerts") or {}
    if not a.get("lowBatt", True):
        return
    thr = a.get("lowBattPct", 20)
    for n in data.get("nodes", []) or []:
        if not n.get("own"):
            continue
        b = (n.get("info") or {}).get("battery")
        if b is None or b > 100:  # >100% = питание от сети → игнор
            continue
        nid = n.get("id")
        name = (CFG.get("names") or {}).get(nid, nid)
        if b < thr and nid not in _batt_alerted:
            _batt_alerted.add(nid)
            alert(f"🔋 Meshtastic: {name} — заряд {b}% (ниже {thr}%)")
        elif b >= thr + 5 and nid in _batt_alerted:
            _batt_alerted.discard(nid)


def clip_bytes(s, limit=200):
    b = (s or "").encode("utf-8")
    return s if len(b) <= limit else b[:limit].decode("utf-8", "ignore")


def send_dm(node, to, text, reply_id=None):
    """Отправить личное с ноды node адресату to + записать исходящее (как /api/send).
    Возвращает (ok, err)."""
    with lock:
        ent = next((c for c in conns.values() if c.get("id") == node and c.get("iface")), None)
    if not ent or not to or not text:
        return False, "нода не на связи или пустой текст"
    try:
        pkt = ent["iface"].sendText(text, destinationId=to, wantAck=True, replyId=reply_id or None)
        pid = getattr(pkt, "id", None)
        out = dict(kind="out", id=f"out·{pid or int(time.time() * 1000)}", pktId=pid,
                   frm=node, to=to, text=text, ts=int(time.time()), status="sent", read=True)
        if reply_id:
            out["replyTo"] = reply_id
        with lock:
            messages.append(out)
        save_messages()
        log(f"➤ {node} → {to}: {text[:60]!r} (pkt {pid})")
        return True, None
    except Exception as e:
        return False, str(e)


def tg_send(text):
    """Отправить в Telegram через telegram.sh с -I; вернуть список message_id."""
    a = CFG.get("alerts") or {}
    tok, chat = a.get("tgToken"), a.get("tgChat")
    if not (a.get("enabled", True) and tok and chat):
        return []
    ids = []
    try:
        cmd = [ALERT_BIN, "-t", str(tok), "-a", "3", "-I"]
        for c in str(chat).replace(",", " ").split():
            cmd += ["-c", c]
        cmd.append(text)
        r = subprocess.run(cmd, timeout=90, capture_output=True)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            p = line.split()
            if len(p) >= 3 and p[0] == "msgid":
                try:
                    ids.append(int(p[2]))
                except ValueError:
                    pass
    except Exception as e:
        log(f"tg_send: {e!r}")
    return ids


def mirror_dm(node, peer, peer_name, pid, text):
    """Входящий DM → в Telegram; запомнить msg_id→(нода,адресат) для ответа-цитаты."""
    own = (CFG.get("names") or {}).get(node, node)
    ids = tg_send(f"📡 Meshtastic DM → {own}\nот {peer_name}:\n{text}")
    if ids:
        with lock:
            for mid in ids:
                tgmap["map"][str(mid)] = dict(node=node, peer=peer, peerName=peer_name, pid=pid)
        save_tgmap()


def tg_to_mesh(m, text):
    """Ответ-цитата из Telegram → отправить в меш от исходной ноды (или лучшей)."""
    node, peer = m.get("node"), m.get("peer")
    text = clip_bytes(text, 200)
    with lock:
        online = any(c.get("id") == node and c.get("iface") for c in conns.values())
    if not online:
        alt = best_sender_for(peer)  # исходная нода оффлайн — шлём с лучшей слышащей
        if alt:
            node = alt["id"]
    ok, err = send_dm(node, peer, text, reply_id=m.get("pid"))
    if ok:
        log(f"📩→📡 Telegram-ответ ушёл: {node} → {peer}: {text[:40]!r}")
    else:
        log(f"📩→📡 не отправлено ({err})")
        tg_send(f"⚠️ не отправил {m.get('peerName', peer)}: {err}")


def tg_poll_loop():
    """Поллинг getUpdates: ответ-цитата в Telegram на зеркалированный DM → в меш."""
    load_tgmap()
    while True:
        a = CFG.get("alerts") or {}
        tok = a.get("tgToken")
        if not (a.get("enabled", True) and a.get("tgReply", True) and tok):
            time.sleep(30)
            continue
        proxy = a.get("tgProxy") or ""
        try:
            off = tgmap.get("offset", 0)
            url = (f"https://api.telegram.org/bot{tok}/getUpdates?timeout=25"
                   f"&offset={off + 1}&allowed_updates=%5B%22message%22%5D")
            # URL (с токеном) — через stdin-конфиг (-K -), чтобы токен НЕ светился
            # в списке процессов (ps) при долгом long-poll
            cmd = ["curl", "-s", "--max-time", "35", "-K", "-"]
            if proxy:
                cmd += ["-x", proxy]
            r = subprocess.run(cmd, timeout=45, capture_output=True,
                               input=('url = "%s"\n' % url).encode())
            data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
            if not data.get("ok"):
                if data.get("error_code") == 409:  # другой getUpdates — редко, не спамим
                    time.sleep(10)
                time.sleep(5)
                continue
            dirty = False
            for upd in data.get("result", []):
                tgmap["offset"] = max(tgmap.get("offset", 0), upd.get("update_id", 0))
                dirty = True
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                rt = msg.get("reply_to_message") or {}
                with lock:
                    m = tgmap["map"].get(str(rt.get("message_id")))
                if text and m:
                    threading.Thread(target=tg_to_mesh, args=(m, text), daemon=True).start()
                elif text and rt:
                    log("tg_poll: ответ в Telegram без связки с DM "
                        "(отвечай именно на сообщение «📡 Meshtastic DM»)")
            if dirty:
                save_tgmap()
        except Exception as e:
            log(f"tg_poll: {e!r}")
            time.sleep(5)


_hist_last = 0.0
_prune_last = 0.0


def hist_tick(data):
    """Врезать срез в историю (не чаще histEveryS) и раз в час чистить старьё."""
    global _hist_last, _prune_last
    now = time.time()
    if now - _hist_last >= CFG.get("histEveryS", 60):
        try:
            history.record(data, fresh=CFG.get("topoEveryS", 60) * 3)
            _hist_last = now
        except Exception as e:
            log(f"hist: {e!r}")
    if now - _prune_last >= 3600:
        try:
            history.prune(days=CFG.get("histDays", 30))
            _prune_last = now
        except Exception as e:
            log(f"hist-prune: {e!r}")


def rx_flush():
    """Сброс сигнального аккумулятора в history: агрегат (n, avg, sd, rssi) на
    пару приёмник×передатчик за прошедший интервал. sd — маркер LOS/NLOS."""
    with rx_lock:
        acc = dict(rx_acc)
        rx_acc.clear()
        cut = time.time() - 2 * 3600            # прунинг direct_live (>2ч не нужен)
        for k in [k for k, v in direct_live.items() if v[0] < cut]:
            direct_live.pop(k, None)
    ts, rows = int(time.time()), []
    for (node, src), vals in acc.items():
        if not node:
            continue
        sn = [v[0] for v in vals]
        rs = [v[1] for v in vals if v[1] is not None]
        n = len(sn)
        avg = sum(sn) / n
        sd = (sum((x - avg) ** 2 for x in sn) / n) ** 0.5 if n > 1 else 0.0
        rows.append((ts, node, src, n, round(avg, 2), round(sd, 2),
                     round(sum(rs) / len(rs), 1) if rs else None))
    try:
        history.record_rx(rows)
    except Exception as e:
        log(f"rx_flush: {e!r}")


_survey_last = {}  # id -> ts последней фоновой трассировки


def survey_loop():
    """Фоновая жатва чужих звеньев (Фаза 6, фундамент): редкий traceroute по
    кругу. Активная проба эфира — бережно: раз в surveyEveryS (дефолт 15 мин),
    пропуск при загруженном канале, один узел за такт, очередь по давности."""
    while True:
        time.sleep(max(120, CFG.get("surveyEveryS", 900)))
        try:
            if not CFG.get("surveyEnabled", True):
                continue
            try:
                data = json.loads(OUT_LIVE.read_text())
            except Exception:
                continue
            # эфир занят — пропускаем такт (не добавляем трафика в час пик)
            ch = [(n.get("info") or {}).get("chUtil")
                  for n in data.get("nodes", []) if n.get("own")]
            ch = [c for c in ch if c is not None]
            if ch and max(ch) > CFG.get("surveyMaxChUtil", 25):
                continue
            # приоритет — ноды с НЕРАЗРЕШЁННЫМ зеркалом (есть est, но сторона
            # не выбрана): одна трассировка через ретранслятор разрешит сторону.
            # Иначе — круг по всем чужим по давности.
            amb = [n["id"] for n in data.get("nodes", [])
                   if n.get("est") and not n["est"].get("side") and not n.get("own")]
            cand = amb or [n["id"] for n in data.get("nodes", [])
                           if not n.get("own") and n.get("id")]
            if not cand:
                continue
            now = time.time()
            target = min(cand, key=lambda i: _survey_last.get(i, 0))
            if now - _survey_last.get(target, 0) < 6 * 3600:
                continue  # весь круг опрошен недавно — не душним
            _survey_last[target] = now
            sender = best_sender_for(target)
            ent = ent_by_id(sender) if sender else None
            if not ent:
                with lock:
                    ent = next((c for c in conns.values() if c.get("iface")), None)
            if not ent:
                continue
            with lock:
                pending_traces.add(target)
            log(f"🧭 survey: трассирую {target} с {ent.get('id')}")
            try:
                ent["iface"].sendTraceRoute(target, 7)  # блокируется до ответа
            except Exception as e:
                log(f"🧭 survey {target}: {e!r}")
            with lock:
                pending_traces.discard(target)
        except Exception as e:
            log(f"survey: {e!r}")


def geocode_loop():
    """Геокодинг адресных имён нод (Фаза 6-В, пул мягких якорей): раз в сутки
    превращаем «Pulkovskoe 65» → координаты через Nominatim. Кэш персистентный,
    новые имена дёргаем по 1/сек. Верификация по GPS, если нода его вещает."""
    time.sleep(20)  # дать первому скану наполнить live.json/имена
    while True:
        try:
            if CFG.get("geocodeEnabled", True):
                _do_geocode()
        except Exception as e:
            log(f"geocode: {e!r}")
        time.sleep(CFG.get("geocodeEveryS", 86400))


def _do_geocode():
    try:
        data = json.loads(OUT_LIVE.read_text())
    except Exception:
        return
    try:
        addr = json.loads(GEO_ADDR.read_text())
    except Exception:
        addr = {}
    names = (data.get("meta") or {}).get("names") or {}
    byid = {n["id"]: n for n in data.get("nodes", []) or []}
    own = {n["id"] for n in data.get("nodes", []) if n.get("own")}
    # центр кучки якорей — санити «геокод не в другом городе»
    geo = CFG.get("geo") or {}
    aps = [(g["lat"], g["lon"]) for g in geo.values()
           if isinstance(g, dict) and g.get("lat") is not None]
    ctr = (sum(p[0] for p in aps) / len(aps), sum(p[1] for p in aps) / len(aps)) if aps else None
    new = 0
    for nid, nm in names.items():
        if nid in addr or nid in own:         # уже пробовали / своя нода — пропуск
            continue
        if not geocode.normalize(nm):         # на адрес не похоже — без сети
            continue
        g = geocode.geocode(nm, str(GEO_CACHE))
        if not g:
            addr[nid] = None                  # запомнить «пусто», чтобы не повторять
            new += 1
            continue
        # санити: геокод дальше 80 км от кластера якорей = ошибочный (чужой город)
        if ctr and geocode._hav_km(ctr, (g["lat"], g["lon"])) > 80:
            addr[nid] = None
            new += 1
            continue
        rec = dict(lat=g["lat"], lon=g["lon"], q=g["q"], name=nm,
                   place=g.get("place", False), ts=int(time.time()), verified=False)
        info = (byid.get(nid) or {}).get("info") or {}
        if info.get("lat") is not None:       # есть GPS — сверяем геокод с ним
            d = geocode._hav_km((info["lat"], info["lon"]), (g["lat"], g["lon"]))
            rec["gpsKm"] = round(d, 2)
            rec["verified"] = d < 0.6
        addr[nid] = rec
        new += 1
    if new:
        atomic_write(GEO_ADDR, json.dumps(addr, ensure_ascii=False, indent=1))
        got = sum(1 for v in addr.values() if v)
        log(f"🏠 геокодинг: +{new} имён, всего с координатами {got}")


def topo_loop():
    while True:
        try:
            with lock:
                live = {ip: c for ip, c in conns.items() if c.get("iface")}
            if live:
                found = {ip: snapshot(c) for ip, c in live.items()}
                prev = None
                try:
                    prev = json.loads(OUT_LIVE.read_text())
                except Exception:
                    pass
                xlinks = []
                try:
                    xlinks = history.xlink_pairs(hours=CFG.get("xlinkHours", 336))
                except Exception:
                    pass
                with rx_lock:
                    dlive = dict(direct_live)         # прямые приёмы из живого потока
                data = scan.build(found, prev, xlinks=xlinks, direct_live=dlive)
                atomic_write(OUT_LIVE, json.dumps(data, ensure_ascii=False, indent=1))
                hist_tick(data)
                rx_flush()
                check_batt(data)
        except Exception as e:
            log(f"topo: {e!r}")
        # исходящие без квитанции дольше 90 с — помечаем честно; «waiting»
        # дольше keyWaitMin (адресат так и не появился в эфире) — сдаёмся
        try:
            dirty = False
            now, wait_ttl = time.time(), CFG.get("keyWaitMin", 120) * 60
            with lock:
                for m in messages:
                    if m.get("kind") != "out":
                        continue
                    if m.get("status") == "sent" and now - m["ts"] > 90:
                        m["status"], dirty = "noack", True
                    elif (m.get("status") == "waiting"
                          and now - m.get("waitSince", now) > wait_ttl):
                        m["status"], m["detail"], dirty = "failed", "PKI_SEND_FAIL_PUBLIC_KEY", True
            if dirty:
                save_messages()
        except Exception:
            pass
        time.sleep(CFG.get("topoEveryS", 60))


# ---------- HTTP: статика + API ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT.parent), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # статику отдаём без кэша, чтобы браузер всегда брал свежий app.js/css
        # (API уже ставит no-store сам)
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/messages"):
            with lock:
                self._json({"messages": messages[-200:]})
        elif self.path.startswith("/api/channel"):
            with lock:
                self._json({"channel": channel[-200:]})
        elif self.path.startswith("/api/config"):
            with lock:
                self._json({k: CFG.get(k) for k in EDITABLE})
        elif self.path.startswith("/api/geo"):
            with lock:
                self._json({"geo": CFG.get("geo", {})})
        elif self.path.startswith("/api/trace"):
            tid = (parse_qs(urlparse(self.path).query).get("to") or [""])[0]
            with lock:
                self._json({"trace": traces.get(tid), "pending": tid in pending_traces})
        elif self.path.startswith("/api/history/"):
            self._history()
        elif self.path.startswith("/api/dbentry"):
            # диагностика: как каждая своя нода ВИДИТ узел прямо сейчас (сырой nodeDB)
            tid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            out = {}
            with lock:
                live = [c for c in conns.values() if c.get("iface")]
            for c in live:
                try:
                    e = dict(c["iface"].nodes or {}).get(tid)
                except Exception:
                    e = None
                if isinstance(e, dict):
                    lh = e.get("lastHeard") or 0
                    out[c["id"]] = {"hopsAway": e.get("hopsAway"), "snr": e.get("snr"),
                                    "lastHeard": lh, "ageMin": round((time.time() - lh) / 60, 1)}
            self._json({"id": tid, "seenBy": out})
        else:
            super().do_GET()

    def _history(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def g(k, d=None):
            v = q.get(k)
            return v[0] if v else d
        try:
            hours = float(g("hours", 24) or 24)
        except ValueError:
            hours = 24
        try:
            if u.path.endswith("/uptime"):
                self._json({"uptime": history.uptime(hours=hours)})
            elif u.path.endswith("/node"):
                self._json({"series": history.node_series(g("id"), hours=hours)})
            elif u.path.endswith("/link"):
                self._json({"series": history.link_series(g("src"), g("dst"), hours=hours)})
            elif u.path.endswith("/nodecount"):
                self._json({"nc": history.node_counts(hours=hours, bins=int(float(g("bins", 48) or 48)))})
            elif u.path.endswith("/stats"):
                self._json({"stats": history.stats()})
            elif u.path.endswith("/rx"):
                # сигнальный ряд приёмник×передатчик (Фаза 6: RSSI + дисперсия SNR)
                self._json({"series": history.rx_series(g("node"), g("src"), hours=hours)})
            elif u.path.endswith("/xlinks"):
                # чужие звенья из traceroute/NeighborInfo (Фаза 6: геометрия)
                self._json({"pairs": history.xlink_pairs(hours=hours)})
            else:
                self._json({"ok": False, "error": "нет такого эндпоинта"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": repr(e)}, 500)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "плохой JSON"}, 400)
            return
        if self.path == "/api/read":
            ids = set(body.get("ids") or [])
            with lock:
                for m in messages:
                    if m["id"] in ids:
                        m["read"] = True
            save_messages()
            self._json({"ok": True})
        elif self.path == "/api/resend":
            with lock:
                m = next((x for x in messages if x.get("id") == body.get("id")
                          and x.get("kind") == "out"), None)
            if not m:
                self._json({"ok": False, "error": "нет такого сообщения"}, 404)
                return
            self._json({"ok": resend(m)})
        elif self.path == "/api/send":
            node, to = body.get("node"), body.get("to")
            text = (body.get("text") or "").strip()
            with lock:
                ent = next((c for c in conns.values()
                            if c.get("id") == node and c.get("iface")), None)
            if not ent or not to or not text:
                self._json({"ok": False,
                            "error": "нода не на связи или пустой текст"}, 400)
                return
            reply_id = body.get("replyId")
            try:
                pkt = ent["iface"].sendText(text, destinationId=to, wantAck=True,
                                            replyId=reply_id or None)
                pid = getattr(pkt, "id", None)
                out = dict(kind="out", id=f"out·{pid or int(time.time() * 1000)}",
                           pktId=pid, frm=node, to=to, text=text,
                           ts=int(time.time()), status="sent", read=True)
                if reply_id:
                    out["replyTo"] = reply_id
                with lock:
                    messages.append(out)
                save_messages()
                log(f"➤ {node} → {to}: {text[:60]!r} (pkt {pid})")
                self._json({"ok": True, "msgId": out["id"]})
            except Exception as e:
                self._json({"ok": False, "error": repr(e)}, 500)
        elif self.path == "/api/channel":
            node = body.get("node")
            text = (body.get("text") or "").strip()
            reply_id = body.get("replyId")
            with lock:
                ent = next((c for c in conns.values()
                            if c.get("id") == node and c.get("iface")), None)
            if not ent or not text:
                self._json({"ok": False, "error": "нода не на связи или пустой текст"}, 400)
                return
            try:
                ent["iface"].sendText(text, replyId=reply_id or None)  # broadcast
                log(f"📡 {node} → канал: {text[:60]!r}")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": repr(e)}, 500)
        elif self.path == "/api/react":
            node = body.get("node")
            reply_id = body.get("replyId")
            emoji = (body.get("emoji") or "").strip()
            channel_react = bool(body.get("channel"))
            to = body.get("to")
            with lock:
                ent = next((c for c in conns.values()
                            if c.get("id") == node and c.get("iface")), None)
            if not ent or not reply_id or not emoji:
                self._json({"ok": False, "error": "нужны node, replyId, emoji"}, 400)
                return
            try:
                dest = "^all" if channel_react else (to or "^all")
                send_reaction(ent["iface"], dest, emoji, reply_id)
                # оптимистично добавим свою реакцию (её эхо мы не услышим)
                with lock:
                    tgt = find_by_pid(reply_id)
                    if tgt is not None:
                        who = tgt.setdefault("reactions", {}).setdefault(emoji, [])
                        if node not in who:
                            who.append(node)
                save_messages()
                save_channel()
                log(f"👍 {node} {emoji} → pkt {reply_id}")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": repr(e)}, 500)
        elif self.path == "/api/config":
            clean = {}
            for k in EDITABLE:
                if k not in body:
                    continue
                v = body[k]
                if k in ("subnets", "mobile", "fragile"):
                    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
                        continue
                elif k == "snrScale":
                    if (not isinstance(v, dict)
                            or not all(isinstance(v.get(f), (int, float)) for f in ("floor", "ideal"))
                            or v["floor"] >= v["ideal"]):
                        continue
                elif not isinstance(v, (int, float)) or v <= 0:
                    continue
                clean[k] = v
            if not clean:
                self._json({"ok": False, "error": "нечего применить"}, 400)
                return
            with lock:
                CFG.update(clean)
                try:
                    disk = json.loads((ROOT / "config.json").read_text())
                except Exception:
                    disk = {}
                disk.update(clean)
                atomic_write(ROOT / "config.json",
                             json.dumps(disk, ensure_ascii=False, indent=2) + "\n")
            log(f"⚙ конфиг обновлён: {', '.join(clean)}")
            self._json({"ok": True})
        elif self.path == "/api/geo":
            # размещение своих нод на гео-карте (у них GPS выключен): позиция +
            # антенна (omni/dir + азимут/ширина). lat==null → снять размещение.
            node = body.get("node")
            if not node:
                self._json({"ok": False, "error": "нужен node"}, 400)
                return
            with lock:
                geo = CFG.setdefault("geo", {})
                if body.get("lat") is None:
                    geo.pop(node, None)
                else:
                    try:
                        lat, lon = float(body["lat"]), float(body["lon"])
                    except (TypeError, ValueError, KeyError):
                        self._json({"ok": False, "error": "плохие координаты"}, 400)
                        return
                    ant = "dir" if body.get("ant") == "dir" else "omni"
                    entry = dict(lat=round(lat, 6), lon=round(lon, 6), ant=ant)
                    if ant == "dir":
                        entry["dir"] = int(body.get("dir", 0)) % 360
                        entry["beam"] = max(10, min(360, int(body.get("beam", 90))))
                    geo[node] = entry
                try:
                    disk = json.loads((ROOT / "config.json").read_text())
                except Exception:
                    disk = {}
                disk["geo"] = geo
                atomic_write(ROOT / "config.json",
                             json.dumps(disk, ensure_ascii=False, indent=2) + "\n")
            self._json({"ok": True, "geo": CFG.get("geo", {})})
        elif self.path == "/api/trace":
            # traceroute (АКТИВНАЯ проба: шлёт пакет в эфир) от ноды node к to
            node, to = body.get("node"), body.get("to")
            with lock:
                ent = next((c for c in conns.values()
                            if c.get("id") == node and c.get("iface")), None)
            if not ent or not to:
                self._json({"ok": False, "error": "нода не на связи"}, 400)
                return

            def _trace():
                with lock:
                    pending_traces.add(to)
                    traces.pop(to, None)
                try:
                    ent["iface"].sendTraceRoute(to, 7)  # блокируется до ответа/таймаута
                except Exception as e:
                    log(f"🧭 trace {to}: {e!r}")
                # ответ ловит on_receive; если не пришёл — снимаем ожидание по таймауту
                time.sleep(2)
                with lock:
                    pending_traces.discard(to)
            threading.Thread(target=_trace, daemon=True).start()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "нет такого API"}, 404)


def seed_direct_live():
    """При старте засеять direct_live прямыми плечами из прошлого live.json,
    чтобы рестарт НЕ осыпал карту в «бывшие соседи» до набора живого потока:
    первый же build увидит недавно-прямые ноды чёрными. Ноды, реально ушедшие,
    сами протухнут (плечо старше settle → обычный путь в серые/прочь)."""
    try:
        d = json.loads(OUT_LIVE.read_text())
    except Exception:
        return
    own = {n["id"] for n in d.get("nodes", []) if n.get("own")}
    n = 0
    with rx_lock:
        for l in d.get("links", []):
            if (l.get("type") == "rf" and not l.get("hops") and l.get("snr") is not None
                    and l.get("to") in own and l.get("heard")):
                fid, ts = l["from"], l["heard"]
                cur = direct_live.get(fid)
                if not cur or cur[0] < ts:
                    direct_live[fid] = (ts, float(l["snr"]), l["to"])
                    n += 1
    if n:
        log(f"↺ засеяно direct_live из прошлого live.json: {n} прямых плеч")


def main():
    load_messages()
    load_tgmap()
    seed_direct_live()          # бесшовный рестарт: прямые ноды не осыпаются
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=topo_loop, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()  # Telegram→меш ответы
    threading.Thread(target=survey_loop, daemon=True).start()   # жатва чужих звеньев
    threading.Thread(target=geocode_loop, daemon=True).start()  # геокодинг адресных имён
    log(f"hub на http://localhost:{PORT} — сайт, /api/messages, /api/send, /api/read")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
