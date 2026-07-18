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
PORT = 8814
# что можно менять из UI (остальное — только руками в config.json)
EDITABLE = ["subnets", "snrScale", "worldMaxAgeH", "cacheMaxAgeH",
            "topoEveryS", "rescanS", "mobile", "fragile"]

lock = threading.RLock()
conns = {}     # ip -> {"iface", "id", "num", "light", "last"}
messages = []  # [{id, node, frm, frmName, text, ts, snr, read}]


def log(msg):
    scan.log(msg)


def load_messages():
    global messages
    try:
        messages = json.loads(OUT_MSGS.read_text())
    except Exception:
        messages = []


def save_messages():
    with lock:
        OUT_MSGS.write_text(json.dumps(messages, ensure_ascii=False, indent=1))


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
        if dec.get("portnum") != "TEXT_MESSAGE_APP":
            return
        if packet.get("to") != ent.get("num"):
            return  # broadcast или чужое — не личное этой ноде
        frm_num = packet.get("from")
        frm = f"!{frm_num:08x}" if isinstance(frm_num, int) else str(frm_num)
        u = ((dict(interface.nodes or {}).get(frm) or {}).get("user") or {})
        msg = dict(id=f'{ent["id"]}·{packet.get("id")}', node=ent["id"], frm=frm,
                   frmName=u.get("longName") or u.get("shortName") or frm,
                   text=dec.get("text") or "", ts=int(time.time()),
                   snr=packet.get("rxSnr"), read=False)
        with lock:
            if any(m["id"] == msg["id"] for m in messages):
                return
            messages.append(msg)
        save_messages()
        log(f"✉ {msg['frmName']} → {ent['id']}: {msg['text'][:60]!r}")
    except Exception as e:
        log(f"on_receive: {e!r}")


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
                OUT_LIVE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        except Exception as e:
            log(f"topo: {e!r}")
        time.sleep(CFG.get("topoEveryS", 60))


# ---------- HTTP: статика + API ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT.parent), **kw)

    def log_message(self, *a):
        pass

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
            try:
                ent["iface"].sendText(text, destinationId=to)
                log(f"➤ {node} → {to}: {text[:60]!r}")
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
                (ROOT / "config.json").write_text(
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
