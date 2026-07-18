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
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import scan  # noqa: E402 — соседний модуль: CFG, build(), log()

from pubsub import pub  # noqa: E402
import meshtastic.tcp_interface  # noqa: E402

CFG = scan.CFG
OUT_LIVE = ROOT.parent / "data" / "live.json"
OUT_MSGS = ROOT.parent / "data" / "messages.json"
OUT_CHAN = ROOT.parent / "data" / "channel.json"
PORT = 8814
# что можно менять из UI (остальное — только руками в config.json)
EDITABLE = ["subnets", "snrScale", "worldMaxAgeH", "cacheMaxAgeH",
            "topoEveryS", "rescanS", "mobile", "fragile"]

lock = threading.RLock()
conns = {}     # ip -> {"iface", "id", "num", "light", "last"}
messages = []  # личные: [{id, node, frm, frmName, text, ts, snr, read}]
channel = []   # публичный канал: [{id, pid, frm, frmName, text, ts, ch, gotBy}]


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


def on_receive(packet=None, interface=None):
    try:
        ip, ent = ent_by_iface(interface)
        if not ent:
            return
        ent["last"] = time.time()
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
                    elif (str(err) == "PKI_SEND_FAIL_PUBLIC_KEY"
                          and CFG.get("autoKeyRequest", True) and not m.get("keyRetry")):
                        m["keyRetry"] = 1  # оставляем "sent" (⏳): запросим ключ и повторим
                        pki_fail = m
                    else:
                        m["status"], m["detail"] = "failed", str(err)
                    changed = True
            if changed:
                save_messages()
                log(f"{'✓ доставлено' if ok else '✗ NAK ' + str(err)} (rid={rid})")
            if pki_fail:
                auto_key_retry(pki_fail)
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
    """Своя онлайн-нода, которая ЛУЧШЕ всех слышит адресата напрямую (из
    live.json): у неё сильнее шанс, что запрос ключа/DM дойдёт и ответ
    вернётся. Возвращает id ноды или None."""
    try:
        live = json.loads(OUT_LIVE.read_text())
    except Exception:
        return None
    own = {n["id"] for n in live.get("nodes", []) if n.get("own")}
    best, best_snr = None, -1e9
    for l in live.get("links", []):
        if (l.get("from") == to and l.get("to") in own and not l.get("hops")
                and l.get("snr") is not None and l["snr"] > best_snr
                and ent_by_id(l["to"])):
            best, best_snr = l["to"], l["snr"]
    return best


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
            if not auto:
                m.pop("keyRetry", None)  # ручной повтор → снова разрешить автозапрос
        save_messages()
        log(f"{'🔁 авто-ретрай' if auto else '↻ ретрай'} {frm} → {m['to']}: {m['text'][:40]!r}")
        return True
    except Exception as e:
        with lock:
            m["status"] = "failed"
            m["detail"] = str(e)
        save_messages()
        return False


def auto_key_retry(m):
    """Нет ключа адресата → запросить ключ (с ноды, что лучше его слышит) и
    через keyRetryS повторить DM (раз)."""
    frm = best_sender_for(m.get("to")) or m.get("frm")
    ent = ent_by_id(frm) or ent_by_id(m.get("frm"))
    if not ent:
        with lock:
            m["status"], m["detail"] = "failed", "PKI_SEND_FAIL_PUBLIC_KEY"
        save_messages()
        return
    try:
        request_key(ent, m["to"])
    except Exception as e:
        log(f"🔑 запрос ключа не удался: {e!r}")
    delay = CFG.get("keyRetryS", 12)
    log(f"🔑 нет ключа {m['to']} — запросил у адресата с {ent['id']}, ретрай DM через {delay}с")
    threading.Timer(delay, lambda: resend(m, auto=True)).start()


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
                db=dict(iface.nodes or {}))


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
                data = scan.build(found, prev)
                atomic_write(OUT_LIVE, json.dumps(data, ensure_ascii=False, indent=1))
        except Exception as e:
            log(f"topo: {e!r}")
        # исходящие без квитанции дольше 90 с — помечаем честно
        try:
            dirty = False
            with lock:
                for m in messages:
                    if (m.get("kind") == "out" and m.get("status") == "sent"
                            and time.time() - m["ts"] > 90):
                        m["status"] = "noack"
                        dirty = True
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
        else:
            super().do_GET()

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
        else:
            self._json({"ok": False, "error": "нет такого API"}, 404)


def main():
    load_messages()
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=topo_loop, daemon=True).start()
    log(f"hub на http://localhost:{PORT} — сайт, /api/messages, /api/send, /api/read")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
