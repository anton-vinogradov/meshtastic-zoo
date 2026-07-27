#!/usr/bin/env python3
"""meshtastic-zoo hub — живой центр зоопарка.

Держит постоянные TCP-соединения со своими нодами. Три разделённых воркера:
- ПИСАТЕЛЬ (writer_loop): опрос своих нод + on_receive → персистентный кеш
  nodestore (одна строка на узел + таймстемпы);
- ЧИТАТЕЛЬ (reader_loop): nodestore → scan.build_from_store → data/live.json
  (статус чёрная/серая по таймерам last_direct, раскладка из кеша);
- ПРУНЕР (pruner_loop): удаляет узлы за пределами окна удержания.
Плюс: личные сообщения/канал (data/*.json), Telegram-мост, /api, отдача сайта.

Запуск: python3 collector/hub.py       # сайт и API на :8814
"""
import asyncio
import base64
import gzip
import ipaddress
import json
import os
import re
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
import nodestore  # noqa: E402 — персистентный кеш состояния узлов (переход)

from pubsub import pub  # noqa: E402
import meshtastic.tcp_interface  # noqa: E402

CFG = scan.CFG
OUT_LIVE = ROOT.parent / "data" / "live.json"
GEO_ADDR = ROOT.parent / "data" / "geo_addr.json"   # {id: {lat,lon,q,verified,name}}
GEO_CACHE = ROOT.parent / "data" / "geo_cache.json"  # сырой кэш Nominatim
OUT_MSGS = ROOT.parent / "data" / "messages.json"
OUT_CHAN = ROOT.parent / "data" / "channel.json"
OUT_TRACES = ROOT.parent / "data" / "traces.json"   # последняя трассировка на узел (персист)
OUT_TGMAP = ROOT.parent / "data" / "tgmap.json"
OUT_FAV = ROOT.parent / "data" / "favorites.json"   # id избранных — их НЕ прунит кеш
OUT_TIERS = ROOT.parent / "data" / "tiers.json"     # готовая разбивка узлов по тирам (воркер prep)
OUT_KEYASK = ROOT.parent / "data" / "keyasks.json"  # id → сколько раз просили ключ и когда
OUT_TRFAIL = ROOT.parent / "data" / "tracefail.json"  # id → {n, ts}: трасса не дошла (отрицательное доказательство)
PORT = 8814
# что можно менять из UI (остальное — только руками в config.json)
EDITABLE = ["subnets", "snrScale", "worldMaxAgeH", "directWindowH", "formerWindowH",
            "topoEveryS", "renderEveryS", "rescanS", "mobile", "fragile",
            "pingReply", "pingWords", "pingPrefix"]

lock = threading.RLock()
conns = {}     # ip -> {"iface", "id", "num", "light", "last"}
messages = []  # личные: [{id, node, frm, frmName, text, ts, snr, read}]
channel = []   # публичный канал: [{id, pid, frm, frmName, text, ts, ch, gotBy}]
# маппинг для двустороннего Telegram-моста: telegram msg_id → {node, peer, ...}
# (ответ-цитата в Telegram на зеркалированный DM → отправка в меш от той ноды)
tgmap = {"offset": 0, "map": {}}
pending_traces = set()  # id адресатов, чей traceroute-ответ ждём (Фаза 4, ч.3)
manual_pending = set()  # из них — запрошенные из интерфейса (их результат НЕ персистим)
id_mismatch = {}        # ip → {known, actual, name}: nodeid ноды ≠ config (рефлэш?)
_mismatch_logged = {}   # ip → actual: чтобы логать смену один раз
traces = {}             # id → {path:[{id,snr}], ts} — результат последней трассировки
START_TS = time.time()  # старт hub — для uptime на странице статуса
worker_beats = {}       # имя воркера → {ts, note}: пульс для /api/status
_traces_done = 0        # накопительный счётчик завершённых трассировок (для графиков)
_own_traces_done = 0    # накопит. трассировок СВОИХ пар (воркер свежести own↔own)
_pruned_total = 0       # накопит. удалённых прунером узлов
_tg_relayed = 0         # накопит. переслано telegram-мостом (mesh→tg)
_geocoded_count = 0     # текущее число имён с координатами (геокодер)


def beat(name, note=""):
    """Пульс фонового воркера (для страницы статуса): жив + что делал последним."""
    worker_beats[name] = {"ts": time.time(), "note": note}


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


def atomic_write_bytes(path, data):
    """То же, что atomic_write, но для бинарных файлов (предсжатые .gz-срезы)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
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


favorites = set()   # id избранных узлов: не прунятся из кеша, помечены звездой


def load_favorites():
    global favorites
    try:
        d = json.loads(OUT_FAV.read_text())
        if isinstance(d, list):
            favorites = set(d)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"⚠ favorites: {e!r}")


def save_favorites():
    with lock:
        atomic_write(OUT_FAV, json.dumps(sorted(favorites), ensure_ascii=False))


def load_traces():
    """Поднять сохранённые трассировки (переживают рестарт hub и F5 сайта)."""
    global traces
    try:
        d = json.loads(OUT_TRACES.read_text())
        if isinstance(d, dict):
            traces = d
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"⚠ traces: {e!r}")


def save_traces():
    with lock:
        if len(traces) > 3000:           # держим последние ~3000 по времени (в памяти)
            for k in sorted(traces, key=lambda k: traces[k].get("ts", 0))[:-3000]:
                del traces[k]
        # На диск — только авто-survey (для traceNbr через рестарт); ручные,
        # запрошенные из интерфейса, живут лишь в памяти этой сессии. Отбрасываем
        # РУЧНЫЕ ПУТИ, а не запись целиком: в одной записи теперь лежат трассы с
        # разных своих нод, и раньше одна ручная уносила с собой все автоматические.
        persist = {}
        for k, v in traces.items():
            by = {s2: r for s2, r in (v.get("by") or {}).items() if not r.get("manual")}
            if by:
                best = min(by.values(), key=lambda r: (len(r.get("path") or []),
                                                       -(r.get("ts") or 0)))
                persist[k] = {"path": best.get("path"), "ts": best.get("ts") or 0, "by": by}
            elif not v.get("by") and not v.get("manual"):
                persist[k] = v          # легаси-запись, ещё не прошедшая мерж
        atomic_write(OUT_TRACES, json.dumps(persist, ensure_ascii=False))


def merge_trace(tgt, path, src, is_manual):
    """Влить результат трассы ОТ КОНКРЕТНОЙ своей ноды в запись узла.

    Запись ключуется целью, а трассировать цель можно с любой своей ноды — и
    режим «все по очереди» гнал их одну за другой, каждая затирала предыдущую.
    Итог парадоксальный: ноду трассируешь ЧТОБЫ подтвердить соседство, а она с
    карты соседей пропадает — потому что последней ответила дальняя нода (3
    хопа) и перезаписала ответ ближней (1 хоп, тот самый сосед).

    Теперь пути живут по источникам в `by`, а наружу (`path`/`ts`, их читают
    scan.py и очередь) отдаётся ЛУЧШИЙ СВЕЖИЙ: кратчайший, при равенстве —
    новейший. Свежесть та же, что у соседства (traceRecheckH), поэтому
    устаревший короткий путь уступает место актуальному длинному, а не
    замораживает «сосед» навсегда. Вызывать под lock."""
    now = int(time.time())
    rec = traces.get(tgt) or {}
    by = dict(rec.get("by") or {})
    if not by and rec.get("path"):
        by[(rec["path"][0] or {}).get("id") or "?"] = {
            "path": rec["path"], "ts": rec.get("ts") or 0,
            **({"manual": True} if rec.get("manual") else {})}
    by[src] = {"path": path, "ts": now, **({"manual": True} if is_manual else {})}
    cut = now - CFG.get("traceRecheckH", 24) * 3600
    fresh = [v for v in by.values() if (v.get("ts") or 0) >= cut and v.get("path")]
    best = min(fresh, key=lambda v: (len(v["path"]), -(v.get("ts") or 0))) if fresh else by[src]
    # `ts` — время ЛУЧШЕГО пути (по нему судят свежесть соседства), а `ans` — когда
    # нам вообще последний раз ОТВЕТИЛИ. Разные вещи: ответ по длинному пути не
    # двигает `ts` (лучший путь остаётся коротким и старым), и проверка «ответила
    # ли нода» по `ts` считала живой ответ неответом — со штрафом и снятием.
    traces[tgt] = {"path": best["path"], "ts": best.get("ts") or now, "by": by,
                   "ans": max(int(v.get("ts") or 0) for v in by.values()),
                   **({"manual": True} if best.get("manual") else {})}


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
        # АВТО-ДЕТЕКТ РЕФЛЭША: реальный nodeid ≠ тому, что ждёт config[known] по этому
        # IP → нода перепрошита (id сменился), config устарел. Флажим для статуса + лог.
        actual = user.get("id") or (num and f"!{num:08x}")
        cfg_id = CFG.get("known", {}).get(ip)
        with lock:
            if actual and cfg_id and actual != cfg_id:
                nm = (CFG.get("names") or {}).get(cfg_id, cfg_id)
                id_mismatch[ip] = {"known": cfg_id, "actual": actual, "name": nm}
                if _mismatch_logged.get(ip) != actual:
                    _mismatch_logged[ip] = actual
                    log(f"⚠ {ip}: config ждёт {cfg_id} ({nm}), а нода теперь {actual} — "
                        f"обнови names/known (рефлэш?)")
            else:
                id_mismatch.pop(ip, None)
                _mismatch_logged.pop(ip, None)
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
                # он в эфире и слышен напрямую — лучший момент попросить ключ
                maybe_solicit_key(f"!{fn:08x}", True)
            note_relay(packet, ent, hs, hl, snr, fn)   # бесплатные улики смежности
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
            notes = []       # телеграм-статусы (отправляем ПОСЛЕ выхода из lock)
            with lock:
                for m in messages:
                    # поздний ACK/NAK поднимает и уже помеченное noack (таймаут 90с мог
                    # опередить квитанцию на медленном многохопе) → чинит ложный статус
                    if not (m.get("kind") == "out" and m.get("pktId") == rid
                            and m.get("status") in ("sent", "noack")):
                        continue
                    if ok:
                        m["status"] = "delivered"
                    elif (str(err) in ("PKI_SEND_FAIL_PUBLIC_KEY", "PKI_UNKNOWN_PUBKEY")
                          and CFG.get("autoKeyRequest", True)
                          and m.get("tries", 0) < CFG.get("keyMaxTries", 12)):
                        # Две РАЗНЫЕ беды, лечение одно (обмен NodeInfo):
                        #   SEND_FAIL — у НАС нет ключа адресата, шифровать нечем;
                        #   UNKNOWN_PUBKEY — адресат не знает НАШЕГО, расшифровать нечем.
                        # Ждём появления адресата в эфире и повторяем.
                        m["status"] = "waiting"
                        m.pop("detail", None)
                        m.setdefault("waitSince", int(time.time()))
                        pki_fail = m
                    else:
                        m["status"], m["detail"] = "failed", str(err)
                    changed = True
                    t = tg_note(m)
                    if t:
                        notes.append(t)
            if changed:
                save_messages()
                log(f"{'✓ доставлено' if ok else '✗ NAK ' + str(err)} (rid={rid})")
            tg_send_batch(notes)
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
            # СВОЙ ответ отличаем по requestId: матч только по отправителю пускал
            # чужие RouteDiscovery (они к нам реально приходят — см. жатву xlink выше),
            # а с пробами в один хоп чужой ответ с пустым route[] выглядел бы как
            # «путь длиной 1 хоп» и покрасил бы несуществующего соседа.
            rq = dec.get("requestId") or packet.get("requestId")
            with lock:
                want = _trace_req.get(frm)
                waited = frm in pending_traces and (want is None or rq == want)
                if waited:
                    _trace_req.pop(frm, None)
            if not waited:
                return
            path = []
            for i, num in enumerate(nums):
                e = {"id": f"!{num:08x}" if isinstance(num, int) else str(num)}
                if i > 0 and i - 1 < len(snrs) and snrs[i - 1] != -128:
                    e["snr"] = round(snrs[i - 1] / 4, 2)
                path.append(e)
            global _traces_done
            _traces_done += 1
            with lock:
                pending_traces.discard(frm)
                is_manual = frm in manual_pending   # запрошена из интерфейса?
                manual_pending.discard(frm)
                merge_trace(frm, path, path[0]["id"] if path else "?", is_manual)
            # персистим и РУЧНЫЕ: путь до узла теперь строится по трассе, и после
            # рестарта он должен остаться таким же (а неудачи его снимают, см.
            # note_trace_result — иначе рестарт возвращал бы устаревшее «сосед»)
            save_traces()
            note_trace_result(frm, True)
            # Трасса открыла каналы между узлами (и подтвердила соседство) —
            # пересобираем карту СРАЗУ, не ожидая тика: обновляем срез чужих звеньев
            # и будим читателя. Иначе результат ручной трассы виден лишь через минуту.
            global last_xlinks
            try:
                last_xlinks = history.xlink_pairs(hours=CFG.get("xlinkHours", 336))
            except Exception as e:
                log(f"trace xlinks: {e!r}")
            # Ответ мы ПРИНЯЛИ САМИ — это прямое плечо с честным rxSnr, и его надо
            # сбросить в кеш ЗДЕСЬ же. Иначе оно ждёт такта писателя (topoEveryS) и
            # доезжает на карту рендером позже встречного «нас слышат» из snrTowards:
            # ручная трассировка перерисовывала одно плечо из пары, второе — спустя
            # минуту. Пишем только если приём был прямым (буфер заполняется при
            # hopStart==hopLimit), через реле — плеча нет и выдумывать нечего.
            with rx_lock:
                dv = direct_live.get(frm)
            if dv and dv[1] is not None:
                nodestore.note_leg(frm, dv[2] or ent.get("id"), dv[1], 0,
                                   ts=int(dv[0]), src="rx")
            render_now.set()
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
        frm_name = node_name(frm, u)
        text = dec.get("text") or ""
        reply_id = dec.get("replyId") or dec.get("reply_id")
        # «Писатель» канала/лички → ОПЕРАТИВНО в store: узел, вышедший в эфир с
        # текстом, должен сразу попасть на карту и в очередь воркеров (ключи/трассы),
        # не дожидаясь, пока его засемплит nodeDB опрашиваемой ноды. Своё эхо — мимо.
        if isinstance(frm_num, int) and frm not in {c.get("id") for c in list(conns.values())}:
            hs2, hl2 = packet.get("hopStart"), packet.get("hopLimit")
            mhops = hs2 - hl2 if isinstance(hs2, int) and isinstance(hl2, int) and hs2 >= hl2 else None
            now_ts = int(time.time())
            nodestore.upsert(frm, ts=now_ts, name=(frm_name if frm_name != frm else None))
            nodestore.note_leg(frm, ent["id"], packet.get("rxSnr"), mhops, ts=now_ts, src="rx")

        # РЕАКЦИЯ (тапбэк): emoji=1 + reply_id → привязать к целевому сообщению
        if dec.get("emoji") and reply_id:
            own_set = own_ids()
            react_q = None
            with lock:
                tgt = find_by_pid(reply_id)
                if tgt is not None:
                    who = tgt.setdefault("reactions", {}).setdefault(text, [])
                    if frm not in who:
                        who.append(frm)
                        # реакция на НАШЕ сообщение — то же событие «нам ответили»,
                        # только одним символом; шлём один раз на пару (кто, что)
                        if frm not in own_set and (tgt.get("kind") == "out"
                                                   or tgt.get("frm") in own_set):
                            react_q = tgt.get("text") or ""
            save_messages()
            save_channel()
            log(f"👍 {frm_name} {text} → pkt {reply_id}")
            if react_q is not None and (CFG.get("alerts") or {}).get("chanReact", True):
                threading.Thread(target=mirror_chan_reply, daemon=True,
                                 args=(ent["id"], frm_name, "", react_q, text),
                                 kwargs={"pid": None}).start()
            return

        if to in (0xFFFFFFFF, 4294967295, "^all", "!ffffffff"):
            # публичный канал (broadcast): один пакет слышат несколько наших нод —
            # группируем по id и копим, кто именно принял (с SNR)
            pid = packet.get("id")
            # Кого считать «нашим»: ответ на СВОЁ сообщение (kind=out) или на
            # сообщение своей ноды. Считаем ДО lock — own_ids() берёт тот же
            # RLock, и хотя он реентрантный, порядок захвата держим простым.
            own_set = own_ids()
            reply_q = None
            fresh_pkt = False              # карточка создана ЗДЕСЬ = пакет новый
            with lock:
                m = next((x for x in channel if pid and x.get("pid") == pid), None)
                if m is None:
                    fresh_pkt = True
                    # уведомляем ОДИН раз на пакет: этот же broadcast прилетит с
                    # каждой своей ноды, но карточка канала создаётся только здесь
                    if reply_id and frm not in own_set:
                        tgt = find_by_pid(reply_id)
                        if tgt and (tgt.get("kind") == "out" or tgt.get("frm") in own_set):
                            reply_q = tgt.get("text") or ""
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
            if reply_q is not None and (CFG.get("alerts") or {}).get("chanReply", True):
                threading.Thread(target=mirror_chan_reply, daemon=True,
                                 args=(ent["id"], frm_name, text, reply_q),
                                 kwargs={"pid": pid}).start()
            # АВТООТВЕТ НА PING. Только на новый пакет (иначе ответим столько раз,
            # сколько своих нод его услышали) и только на ЧУЖОЙ — на свои нельзя,
            # иначе два инстанса меша перепингуют друг друга до бесконечности.
            # Кулдаун помечаем ПРИ ПЛАНИРОВАНИИ: пока ждём сбора приёмов, второй
            # ping от того же не должен породить второй ответ.
            if (fresh_pkt and frm not in own_set and CFG.get("pingReply", True)
                    and is_ping(text)):
                now_p = time.time()
                with lock:
                    quiet = (now_p - _ping_last.get(frm, 0) >= CFG.get("pingCooldownS", 600)
                             and now_p - _ping_last_any >= CFG.get("pingGapS", 60))
                    if quiet:
                        _ping_last[frm] = now_p
                if quiet:
                    threading.Thread(target=ping_reply, daemon=True,
                                     args=(pid, frm, frm_name)).start()
                else:
                    log(f"🏓 ping от {frm_name}: кулдаун, не отвечаем")
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


def has_key_for(node, peer):
    """Есть ли у нашей ноды `node` публичный ключ адресата `peer`? В live.json у
    адресата keyBy перечисляет наши ноды, знающие его ключ. True/False, либо None
    если адресат ещё не в live.json (неизвестно — не делаем поспешных выводов)."""
    try:
        live = json.loads(OUT_LIVE.read_text())
    except Exception:
        return None
    tgt = next((n for n in live.get("nodes", []) if n.get("id") == peer), None)
    if tgt is None:
        return None
    return node in set(tgt.get("keyBy", []))


def request_key(ent, to):
    """Солицит ключа адресата: шлём ему наш NodeInfo с want_response — он
    отвечает своим NodeInfo (в нём publicKey), и наша нода узнаёт его ключ.

    Кладём и СВОЙ публичный ключ: при NAK PKI_UNKNOWN_PUBKEY проблема обратная —
    это АДРЕСАТ не знает нашего ключа и потому не может расшифровать. Без
    public_key в анонсе он бы так и не узнал его, и лечения бы не вышло."""
    import meshtastic.mesh_interface as mi
    iface = ent["iface"]
    mu = iface.getMyUser() or {}
    u = mi.mesh_pb2.User(id=mu.get("id") or ent.get("id") or "",
                         long_name=mu.get("longName") or "",
                         short_name=mu.get("shortName") or "")
    pk = mu.get("publicKey")
    if pk:
        try:                       # строка приходит base64, протобуф ждёт байты
            u.public_key = base64.b64decode(pk) if isinstance(pk, str) else pk
        except Exception as e:
            log(f"🔑 свой ключ в NodeInfo не вложен: {e!r}")
    iface.sendData(u, destinationId=to, wantResponse=True,
                   portNum=mi.portnums_pb2.PortNum.NODEINFO_APP)


def resend(m, auto=False):
    """Переслать исходящее сообщение тому же адресату — но С ЛУЧШЕЙ ноды (кто
    сильнее слышит адресата), а не обязательно с исходной. Обновляем ту же
    запись (в т.ч. время — видно, что повтор реально ушёл), а не плодим новую."""
    frm = best_sender_for(m.get("to")) or m.get("frm")
    ent = ent_by_id(frm) or ent_by_id(m.get("frm"))
    # сброс tg.last делаем АТОМАРНО с записью статуса (не заранее): иначе в окне до
    # обновления статуса writer_loop мог бы повторно зеркалить устаревший статус
    if not ent:
        with lock:
            m["status"] = "failed"
            if m.get("tg"):
                m["tg"]["last"] = "sent"  # повтор → перевзвести финальное уведомление
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
            if m.get("tg"):
                m["tg"]["last"] = "sent"
        save_messages()
        log(f"↻ ретрай {frm} → {m['to']}: {m['text'][:40]!r}")
        return True
    except Exception as e:
        with lock:
            m["status"] = "failed"
            m["detail"] = str(e)
            if m.get("tg"):
                m["tg"]["last"] = "sent"
        save_messages()
        return False


_solicit_last = {}  # id → ts последнего solicit_key (антидубль эфира)
_solicit_any = 0.0  # ts ЛЮБОГО событийного запроса ключа (общий газ на эфир)
_key_asks = {}      # id → {n, ts}: сколько раз просили ключ и когда (видно в панели)


def load_key_asks():
    global _key_asks
    try:
        d = json.loads(OUT_KEYASK.read_text())
        _key_asks = {k: v for k, v in d.items() if isinstance(v, dict)}
    except Exception:
        _key_asks = {}


def save_key_asks():
    try:
        with lock:
            # держим только последние — файл не должен расти вечно
            items = sorted(_key_asks.items(), key=lambda kv: -(kv[1].get("ts") or 0))[:2000]
            d = dict(items)
            _key_asks.clear()
            _key_asks.update(d)
        atomic_write(OUT_KEYASK, json.dumps(d, ensure_ascii=False))
    except Exception as e:
        log(f"keyasks: {e!r}")


def note_key_ask(nid):
    """Запомнить факт запроса ключа — панель показывает «спрашивали N раз»."""
    with lock:
        a = _key_asks.setdefault(nid, {"n": 0, "ts": 0})
        a["n"] = int(a.get("n") or 0) + 1
        a["ts"] = int(time.time())
    save_key_asks()


def any_online_ent():
    with lock:
        return next((c for c in conns.values() if c.get("iface")), None)


OUT_HEARSUS = ROOT.parent / "data" / "hearsus.json"   # id → {ts, own}: узел слышит НАС
_hears_us = {}
_trace_req = {}           # id цели → id нашего последнего traceroute-запроса
_relay_map = (0.0, ({}, {}))   # (ts, (кандидаты, все носители)) по байту — кеш на минуту
_relay_stats = {"pkt": 0, "field": 0, "relayed": 0, "resolved": 0, "ambig": 0, "own": 0,
                "leg": 0, "hearsus": 0}   # видно на статусе: работает ли жатва


def load_hears_us():
    global _hears_us
    try:
        d = json.loads(OUT_HEARSUS.read_text())
        _hears_us = {k: v for k, v in d.items() if isinstance(v, dict)}
    except Exception:
        _hears_us = {}


def purge_byte_collisions():
    """Снять улики «слышит нас», выписанные по байту НАШЕЙ ЖЕ ноды.

    Свои ноды переизлучают наш трафик постоянно, а в карту байтов они не попадали
    — кредит за переизлучение уходил случайному чужому узлу с тем же последним
    байтом. Такая улика живёт relayProofH часов, обновляется каждым новым промахом
    и всё это время держит узел в тире «соседи» И вне очереди трассировки: узел,
    который трассировка уже опровергла, не проверялся больше никогда.
    Разово чистим накопленное — источник закрыт в relay_byte_map()."""
    try:
        own = {n["id"] for n in json.loads(OUT_LIVE.read_text()).get("nodes", [])
               if n.get("own") and n.get("id")}
        ob = {int(i[1:], 16) & 0xFF for i in own}
    except Exception:
        return 0
    bad = []
    with lock:
        for k in list(_hears_us):
            try:
                if k not in own and (int(k[1:], 16) & 0xFF) in ob:
                    bad.append(k)
                    _hears_us.pop(k, None)
            except Exception:
                pass
    if bad:
        save_hears_us()
    return len(bad)


def save_hears_us():
    try:
        with lock:
            d = dict(sorted(_hears_us.items(), key=lambda kv: -(kv[1].get("ts") or 0))[:2000])
            _hears_us.clear()
            _hears_us.update(d)
        atomic_write(OUT_HEARSUS, json.dumps(d, ensure_ascii=False))
    except Exception as e:
        log(f"hearsus: {e!r}")


def own_ids():
    with lock:
        return {c["id"] for c in conns.values() if c.get("id")}


_name_cache = (0.0, {})


def node_name(nid, u=None):
    """Человеческое имя узла для подписи: override из конфига (идентичность = id,
    имя ноды она может менять) → что прислал сам пакет → наш кеш → id.

    Кеш обязателен: реакции и тапбэки идут без user-блока, и подпись
    отправителя вырождалась в сырой id, хотя имя лежит в базе."""
    global _name_cache
    cfg = (CFG.get("names") or {}).get(nid)
    if cfg:
        return cfg
    if u:
        nm = u.get("longName") or u.get("shortName")
        if nm:
            return nm
    now = time.time()
    ts, m = _name_cache
    if now - ts > 60:
        try:
            m = nodestore.names()
            _name_cache = (now, m)
        except Exception:
            pass
    return m.get(nid) or nid


def backfill_names():
    """Разово подписать историю: где отправитель записан сырым id (пакет пришёл
    без user-блока — так приходят реакции), подставить имя из кеша. Иначе старые
    сообщения навсегда остаются подписанными «!aca96630»."""
    n = 0
    with lock:
        for arr in (channel, messages):
            for m in arr:
                f = m.get("frm")
                if f and m.get("frmName") == f:
                    nm = node_name(f)
                    if nm and nm != f:
                        m["frmName"] = nm
                        n += 1
    if n:
        save_channel()
        save_messages()
    return n


def relay_byte_map():
    """Два пула по последнему байту NodeNum: (КАНДИДАТЫ, ВСЕ НОСИТЕЛИ).

    В заголовке лежит только последний байт, а носителей у байта в базе в среднем
    три. Поэтому два вопроса решаются РАЗНЫМИ множествами:
      кандидаты   — в кого вообще можно разрешить. Переизлучивший передал пакет
                    НАМ, значит был в прямом радиодоступе: берём только узлы,
                    которых мы слышим напрямую (без hop) и недавно. Свои — тоже,
                    включая онлайн: без них кредит за наше же переизлучение
                    доставался чужому узлу с тем же байтом (замер: байт своей
                    ноды разрешался в чужого в 12-16% срезов, и так был выписан
                    «сосед», которого четыре трассы подряд опровергли);
      носители    — кто ВООБЩЕ недавно в эфире с этим байтом, включая узлы в
                    хопах. В ответ они не годятся, но доказывают неоднозначность.
    Раньше пул был один и брался по любому приёму: 87% кандидатов оказывались
    узлами в 2-5 хопах, физически не способными передать нам пакет напрямую, —
    и каждому выписывалось «прямое» плечо, которое трасса тут же опровергала."""
    global _relay_map
    now = time.time()
    ts, m = _relay_map
    if now - ts < 60:
        return m
    res, seen, win = {}, {}, CFG.get("relayResolveMin", 30) * 60
    try:
        for n in json.loads(OUT_LIVE.read_text()).get("nodes", []):
            h = n.get("heard")
            if not h or now - h >= win:
                continue
            try:
                b = int(n["id"][1:], 16) & 0xFF
            except Exception:
                continue
            seen.setdefault(b, []).append(n["id"])
            if n.get("own") or n.get("hop") is None:
                res.setdefault(b, []).append(n["id"])
    except Exception:
        pass
    m = (res, seen)
    _relay_map = (now, m)
    return m


def note_relay(packet, ent, hs, hl, snr, frm_num):
    """ЖАТВА relay_node — бесплатные доказательства смежности из каждого пакета.
    relay_node лежит в ОТКРЫТОМ заголовке и называет того, кто передал нам этот
    пакет последним. Два вывода:
      1) пакет пришёл переизлучённым (hopStart>hopLimit) → передавшего мы слышим
         НАПРЯМУЮ, и rxSnr — честный SNR этого линка (в отличие от nodeDB);
      2) переизлучили НАШ пакет на первом хопе → он слышит НАС. Вместе с (1) это
         доказанная двусторонняя смежность, и всё это без единого нашего запроса."""
    _relay_stats["pkt"] += 1
    rn = packet.get("relayNode")
    if not isinstance(rn, int) or not rn:
        return                     # прошивка зануляет поле у непереизлучённых
    _relay_stats["field"] += 1
    relayed = isinstance(hs, int) and isinstance(hl, int) and hs > hl
    if relayed:
        _relay_stats["relayed"] += 1
    res, seen = relay_byte_map()
    byte = rn & 0xFF
    ids = res.get(byte) or []
    # «Других кандидатов среди свежих нет» — ЕЩЁ НЕ однозначность: раньше молчание
    # о носителе читалось как его отсутствие, и 25 из 29 «уникальных» байтов имели
    # других известных носителей. Поэтому: разрешаем только в единственного
    # кандидата И только если других носителей этого байта в эфире не слышно.
    if len(ids) != 1 or len(seen.get(byte) or []) != 1:
        _relay_stats["ambig"] += 1
        return                     # байт неоднозначен — молчим, а не угадываем
    rid = ids[0]
    if rid == ent.get("id") or rid in own_ids():
        _relay_stats["own"] += 1   # наше же переизлучение: кредит не выписываем никому
        return
    _relay_stats["resolved"] += 1
    if relayed and snr is not None:
        nodestore.note_leg(rid, ent["id"], float(snr), 0, ts=int(time.time()), src="relay")
        _relay_stats["leg"] += 1
    if relayed and hs - hl == 1 and isinstance(frm_num, int) \
            and f"!{frm_num:08x}" in own_ids():
        with lock:
            e = _hears_us.setdefault(rid, {"n": 0})
            e["ts"], e["own"] = int(time.time()), ent.get("id")
            e["n"] = int(e.get("n") or 0) + 1
            first = e["n"] == 1
        _relay_stats["hearsus"] += 1
        save_hears_us()
        if first:
            log(f"📶 {rid} переизлучил наш пакет — слышит нас (через {ent.get('id')})")


def anyone_has_key(nid):
    """Знает ли ХОТЬ ОДНА своя нода публичный ключ узла — по её nodeDB в памяти
    (без чтения live.json: вызывается на каждый принятый пакет)."""
    with lock:
        ifaces = [c["iface"] for c in conns.values() if c.get("iface")]
    for i in ifaces:
        try:
            if (((i.nodes or {}).get(nid) or {}).get("user") or {}).get("publicKey"):
                return True
        except Exception:
            pass
    return False


def maybe_solicit_key(nid, direct):
    """Ключ просим В МОМЕНТ, когда узел только что вышел в эфир и слышен НАПРЯМУЮ:
    он точно жив и в зоне, значит ответ на NodeInfo дойдёт. Пассивный keyfetch_loop
    берёт только «свежих» (heard < keyFetchFreshMin), а большинство keyless-нод
    бьётся раз в 1-3 часа и в это окно почти не попадает — поэтому и копился
    хвост без ключей. Порядок проверок: сначала дешёвые счётчики, потом эфир."""
    global _solicit_any
    if not direct or not CFG.get("keyFetchEnabled", True):
        return
    now = time.time()
    if now - _solicit_any < CFG.get("keySolicitGapS", 20):
        return                       # общий газ: пачка пакетов не превращается в пачку запросов
    if now - _keyfetch_last.get(nid, 0) < CFG.get("keyFetchPerNodeH", 3) * 3600:
        return
    if anyone_has_key(nid):
        return                       # дешёвая проверка (nodeDB в памяти) — ДО чтения live.json
    cu = chan_util()
    if cu is not None and cu > CFG.get("busyChUtil", 35):
        return                       # эфир занят — не мешаем
    _keyfetch_last[nid] = now
    _solicit_any = now
    log(f"🔑 {nid} в эфире и без ключа — запрашиваю сразу")
    threading.Thread(target=solicit_key, args=(nid,), daemon=True).start()


def solicit_key(to, force=False):
    """Запросить ключ адресата (NodeInfo) — он ответит своим NodeInfo (с ключом).
    Отправитель: нода, которая лучше слышит адресата; если напрямую его не слышит
    никто — ЛЮБАЯ своя онлайн-нода (запрос уйдёт через ретрансляторы, иначе такие
    узлы не спросить вовсе). Троттл keySolicitGapS защищает эфир от дублей;
    force=True — ручной запрос из панели, он игнорирует троттл.
    Возвращает id ноды-отправителя или False."""
    now = time.time()
    if not force and now - _solicit_last.get(to, 0) < CFG.get("keySolicitGapS", 20):
        return False
    ent = ent_by_id(best_sender_for(to) or "") or any_online_ent()
    if not ent:
        return False
    try:
        request_key(ent, to)
        _solicit_last[to] = now  # занимаем троттл только если запрос реально ушёл
        note_key_ask(to)
        log(f"🔑 запросил ключ у {to} (через {ent['id']}{', вручную' if force else ''})")
        return ent["id"]
    except Exception as e:
        log(f"🔑 запрос ключа не удался: {e!r}")
        return False


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
            with lock:
                beat("keeper", f"{sum(1 for c in conns.values() if c.get('iface'))} нод на связи")
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
    db = dict(iface.nodes or {})
    # авто-фикс часов: если ВСЕ lastHeard в nodeDB = 0, у ноды сбиты часы (ребут,
    # GPS в помещении фикс не берёт) — выставляем текущее (по локальному TCP, в
    # эфир не идёт). Самоограничивается: как переслышит соседа — метки не нулевые.
    try:
        hs = [e.get("lastHeard") or 0 for e in db.values() if isinstance(e, dict)]
        if hs and max(hs) == 0:
            iface.localNode.setTime(int(time.time()))
            log(f"    {ent.get('id')}: часы были сбиты (lastHeard=0) — выставил время")
    except Exception:
        pass
    return dict(num=ent.get("num"), id=ent.get("id"), short=user.get("shortName"),
                long=user.get("longName"), role=user.get("role"),
                hw=user.get("hwModel"), dm=my.get("deviceMetrics") or {},
                db=db, cfg=node_cfg(iface))


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


def send_dm(node, to, text, reply_id=None, tg=None):
    """Отправить личное с ноды node адресату to + записать исходящее (как /api/send).
    `tg` — контекст телеграм-моста {peer, last}: если задан, смена статуса доставки
    зеркалится в чат (см. tg_note). Возвращает (ok, err)."""
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
        if tg:
            out["tg"] = tg
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


# Смена статуса доставки исходящего DM → строка для зеркалирования в Telegram.
_TG_DLV = {
    "delivered": "✅ доставлено",
    "waiting":   "⏳ у адресата ещё нет нашего ключа — запросил, доставлю, как выйдет в эфир",
    "noack":     "⚠️ без подтверждения — ACK не пришёл за отведённое время",
    "failed":    "❌ не доставлено",
}


def tg_note(m):
    """Вызывать ПОД lock. Если у исходящего m есть телеграм-контекст (m['tg']) и его
    статус доставки сменился с последнего уведомления — зафиксировать новый статус и
    вернуть текст для tg_send (слать ПОСЛЕ выхода из lock, в отдельном потоке). Иначе
    None. Идемпотентно: один статус — не более одного сообщения."""
    ctx = m.get("tg")
    if not ctx or not (CFG.get("alerts") or {}).get("tgDelivery", True):
        return None
    st = m.get("status")
    label = _TG_DLV.get(st)
    if not label or ctx.get("last") == st:
        return None
    ctx["last"] = st
    peer = ctx.get("peer") or m.get("to")
    body = (m.get("text") or "").replace("\n", " ")[:50]
    det = f"\n{m['detail']}" if st == "failed" and m.get("detail") else ""
    return f"{label}\n→ {peer}: «{body}»{det}"


def tg_send_batch(notes):
    """Отправить пачку статусов доставки в Telegram ПОСЛЕДОВАТЕЛЬНО в одном фоновом
    потоке: не плодим по потоку-субпроцессу на сообщение (после простоя разом
    таймаутит много исходящих) и уважаем rate-limit чата."""
    if notes:
        threading.Thread(target=lambda: [tg_send(t) for t in notes], daemon=True).start()


def mirror_chan_reply(node, frm_name, text, quoted, react=None, pid=None):
    """Ответ в ОБЩЕМ канале на наше сообщение → в Telegram.

    Личку зеркалим давно, а канал молчал: ответ на нашу же реплику видно было
    только в интерфейсе. Шлём не весь канал (он шумный), а именно ответы нам —
    то же правило, по которому интерфейс красит сообщение «вам ответили».

    Регистрируем msg_id в tgmap с меткой chan — чтобы ответ-цитата из Telegram
    ушёл ОБРАТНО В КАНАЛ (без этого он падал в «без связки с DM» и терялся)."""
    global _tg_relayed
    own = (CFG.get("names") or {}).get(node, node)
    q = (quoted or "").replace("\n", " ")[:60]
    head = (f"💬 Реакция {react} в общем канале → {own}" if react
            else f"💬 Ответ в общем канале → {own}")
    who = f"\nот {frm_name}" + ("" if react else f":\n{text}")
    ids = tg_send(head + who + (f"\n\n↩ на наше: «{q}»" if q else ""))
    if ids:
        _tg_relayed += 1
        with lock:
            for mid in ids:
                tgmap["map"][str(mid)] = dict(node=node, chan=True, pid=pid,
                                              peerName=frm_name)
        save_tgmap()


def mirror_dm(node, peer, peer_name, pid, text):
    """Входящий DM → в Telegram; запомнить msg_id→(нода,адресат) для ответа-цитаты."""
    global _tg_relayed
    own = (CFG.get("names") or {}).get(node, node)
    ids = tg_send(f"📡 Meshtastic DM → {own}\nот {peer_name}:\n{text}")
    if ids:
        _tg_relayed += 1
        with lock:
            for mid in ids:
                tgmap["map"][str(mid)] = dict(node=node, peer=peer, peerName=peer_name, pid=pid)
        save_tgmap()


_ping_last = {}           # id отправителя → ts последнего автоответа (личный кулдаун)
_ping_last_any = 0.0      # ts любого автоответа (общий троттл на канал)
PING_WORDS = ["ping", "пинг", "test", "тест", "проверка", "hi", "привет"]  # дефолт; правится в ⚙
# Досыпаем дефолты в CFG (в память, не в файл): иначе /api/config отдаёт null и
# в ⚙ словарь выглядит ПУСТЫМ, хотя автоответчик работает по значениям из кода.
CFG.setdefault("pingWords", list(PING_WORDS))
CFG.setdefault("pingReply", True)
CFG.setdefault("pingPrefix", "")   # напр. «Богатырский на связи!» — откуда отвечаем


def is_ping(text):
    """Слово-триггер ЦЕЛИКОМ (можно со знаками/эмодзи вокруг), а не подстрокой:
    иначе бот влезал бы в разговор, где слово просто упомянуто."""
    t = re.sub(r"^[\W_]+|[\W_]+$", "", (text or "").strip(), flags=re.U).casefold()
    if not t:
        return False
    words = CFG.get("pingWords") or PING_WORDS
    return any(t == str(w).strip().casefold() for w in words if str(w).strip())


def ping_reply(pid, frm, frm_name):
    """Ответ на ping в общем канале: кто из наших его слышал, с каким SNR и через
    сколько хопов. ЭФИРА НА ПРОБУ НЕ ТРАТИМ — приёмы этого же пакета уже собраны
    в gotBy; ответ это один broadcast. Отвечает нода, услышавшая лучше всех:
    её вероятнее услышат в ответ."""
    global _ping_last_any
    time.sleep(CFG.get("pingWaitS", 8))    # дать остальным своим нодам услышать пакет
    with lock:
        m = next((x for x in channel if x.get("pid") == pid), None)
        got = dict((m or {}).get("gotBy") or {})
    if not got:
        return
    cu = chan_util()
    if cu is not None and cu > CFG.get("busyChUtil", 35):
        log(f"🏓 ping от {frm_name}: канал занят ({cu:.0f}%) — молчим")
        return
    def rank(item):                        # ближе по хопам, затем громче
        v = item[1] if isinstance(item[1], dict) else {}
        return (v.get("hops") if v.get("hops") is not None else 9,
                -(v.get("snr") if v.get("snr") is not None else -99))
    order = sorted(got.items(), key=rank)
    ent = next((e for e in (ent_by_id(i) for i, _ in order) if e), None)
    if not ent:
        return
    # SNR ОСМЫСЛЕН ТОЛЬКО ПРИ 0 ХОПОВ: у ретранслированной копии он описывает
    # передатчик последнего реле, а не пингующего, — сообщать его как «вот как мы
    # тебя слышим» значит врать. Поэтому: прямые — с цифрой, дальние — только
    # числом хопов, свёрнутые по группам.
    nm_of = lambda i: (CFG.get("names") or {}).get(i, i[-4:])
    direct, relayed = [], {}
    for nid, v in order:
        v = v if isinstance(v, dict) else {}
        h = v.get("hops")
        if h == 0:
            s = v.get("snr")
            direct.append(f"{nm_of(nid)} {s:+.1f}" if s is not None else nm_of(nid))
        else:
            relayed.setdefault(h if h is not None else "?", []).append(nm_of(nid))
    bits = []
    if direct:
        bits.append("напрямую: " + ", ".join(direct))
    for h in sorted(relayed, key=lambda x: (x == "?", x)):
        bits.append(f"через {h}🐇: " + ", ".join(relayed[h]))
    if not direct:
        bits.append("напрямую не слышим")
    # Префикс — примерное местоположение наших нод: пингующему полезно знать,
    # ОТКУДА ему ответили (сигнал сам по себе этого не говорит). Настраивается.
    pre = str(CFG.get("pingPrefix") or "").strip()
    txt = clip_bytes((pre + " " if pre else "") + "🏓 " + " · ".join(bits), 200)
    try:
        ent["iface"].sendText(txt, replyId=pid or None)
    except Exception as e:
        log(f"🏓 ping-ответ: {e!r}")
        return
    with lock:
        _ping_last_any = time.time()
    log(f"🏓 ping от {frm_name} → ответили с {ent['id']}: {txt}")


def chan_pid_from_note(note):
    """Восстановить pid сообщения по тексту канального уведомления бота.

    Уведомление выглядит как «💬 Ответ в общем канале → FCA\\nот Вася:\\nтекст…».
    Связки в tgmap может не быть (уведомление старше моста), но автор и текст в
    нём есть — находим ту же реплику в ленте канала и берём её pid, чтобы ответ
    ушёл ЦИТАТОЙ, а не отдельным сообщением. None — если не нашли."""
    mt = re.match(r"^💬[^\n]*\nот ([^\n:]+):\n(.*?)(?:\n\n↩ на наше:|$)", note or "", re.S)
    if not mt:
        return None                      # реакция (без текста) или чужой формат
    name, body = mt.group(1).strip(), mt.group(2).strip()
    with lock:
        for c in reversed(channel):      # свежие первыми: имена в меше не уникальны
            if (c.get("frmName") or "").strip() == name \
                    and (c.get("text") or "").strip() == body:
                return c.get("pid")
    return None


def tg_to_chan(m, text):
    """Ответ-цитата из Telegram на КАНАЛЬНОЕ уведомление → broadcast в общий канал
    (тем же путём, что и композер на сайте: iface.sendText). Broadcast без ACK,
    поэтому подтверждаем отправку в Telegram сразу."""
    global _tg_relayed
    text = clip_bytes(text, 200)
    node = m.get("node")
    ent = ent_by_id(node)
    if not ent:                       # исходная нода оффлайн — шлём с любой живой
        with lock:
            ent = next((c for c in conns.values() if c.get("iface")), None)
    if not ent:
        tg_send("⚠️ не отправил в общий канал: нет ноды на связи")
        return
    try:
        ent["iface"].sendText(text, replyId=m.get("pid") or None)
    except Exception as e:
        log(f"📩→📡 канал: {e!r}")
        tg_send(f"⚠️ не отправил в общий канал: {e}")
        return
    _tg_relayed += 1
    own = (CFG.get("names") or {}).get(ent["id"], ent["id"])
    log(f"📩→📡 Telegram → общий канал ({ent['id']}): {text[:40]!r}")
    tg_send(f"📡 ушло в общий канал от {own}")


def tg_to_mesh(m, text):
    """Ответ-цитата из Telegram → в меш, с гарантией ключа и статусом доставки.
    Отправитель: своя онлайн-нода, у которой ЕСТЬ ключ адресата (иначе PKI-сбой);
    если ключа нет ни у кого — заранее его запрашиваем и уведомляем чат. Дальнейший
    статус (доставлено/без ACK/не доставлено) зеркалится через tg-контекст."""
    if m.get("chan"):                 # уведомление из общего канала → отвечаем в канал
        return tg_to_chan(m, text)
    node, peer = m.get("node"), m.get("peer")
    peer_name = m.get("peerName") or peer
    text = clip_bytes(text, 200)
    with lock:
        orig_online = any(c.get("id") == node and c.get("iface") for c in conns.values())
    best = best_sender_for(peer)  # приоритет: своя нода с ключом адресата, громче слышащая
    if not orig_online:
        node = best or node  # исходная оффлайн — шлём с лучшей слышащей
    elif best and has_key_for(node, peer) is False and has_key_for(best, peer):
        node = best          # у исходной ключа нет, а у best есть — берём best
    tg = {"peer": peer_name, "last": "sent"}
    # ключа нет ни у кого, НО есть онлайн-нода для отправки → заранее солиситим и
    # обещаем отложенную доставку (send_dm создаст waiting-запись, её добьёт
    # try_deliver_waiting). Нет онлайн-ноды — не обещаем: send_dm ниже честно
    # ответит «нода не на связи» одним сообщением, без ложного «доставлю».
    if (CFG.get("autoKeyRequest", True) and ent_by_id(node)
            and has_key_for(node, peer) is False):
        solicit_key(peer)
        tg["last"] = "waiting"
        tg_send(f"🔑 у нас пока нет ключа {peer_name} — запросил; доставлю, как выйдет в эфир")
    ok, err = send_dm(node, peer, text, reply_id=m.get("pid"), tg=tg)
    if ok:
        global _tg_relayed
        _tg_relayed += 1
        log(f"📩→📡 Telegram-ответ ушёл: {node} → {peer}: {text[:40]!r}")
    else:
        log(f"📩→📡 не отправлено ({err})")
        tg_send(f"⚠️ не отправил {peer_name}: {err}")


def tg_poll_loop():
    """Поллинг getUpdates: ответ-цитата в Telegram на зеркалированный DM → в меш."""
    load_tgmap()
    while True:
        a = CFG.get("alerts") or {}
        tok = a.get("tgToken")
        if not (a.get("enabled", True) and a.get("tgReply", True) and tok):
            beat("tg", "выключен")
            time.sleep(30)
            continue
        proxy = a.get("tgProxy") or ""
        beat("tg", "поллинг")
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
            # принимаем команды ТОЛЬКО из своих чатов (tgChat): message_id в Telegram
            # нумеруются ПО ЧАТУ, поэтому ответ из чужого чата может случайно попасть
            # в нашу связку и уйти в эфир от нашего имени
            allow = {c for c in str(a.get("tgChat") or "").replace(",", " ").split()}
            for upd in data.get("result", []):
                tgmap["offset"] = max(tgmap.get("offset", 0), upd.get("update_id", 0))
                dirty = True
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str(((msg.get("chat") or {}).get("id", "")))
                if allow and chat_id not in allow:
                    log(f"tg_poll: пропущено сообщение из чужого чата {chat_id}")
                    continue
                # /chan <текст> — написать в ОБЩИЙ КАНАЛ, не дожидаясь чужого ответа
                # (реплай-путь работает лишь когда нам ответили; этот — всегда)
                cm = re.match(r"^/(?:chan|c)(?:@\S+)?(?:\s+(.*))?$", text, re.S | re.I)
                if cm:
                    body_txt = (cm.group(1) or "").strip()
                    if body_txt:
                        threading.Thread(target=tg_to_chan, daemon=True,
                                         args=({"node": None}, body_txt)).start()
                    else:
                        tg_send("напиши так: /chan текст сообщения")
                    continue
                rt = msg.get("reply_to_message") or {}
                with lock:
                    m = tgmap["map"].get(str(rt.get("message_id")))
                if text and m:
                    threading.Thread(target=tg_to_mesh, args=(m, text), daemon=True).start()
                elif text:
                    # Связки нет (уведомление старше моста или вытеснено из карты).
                    # Но по тексту исходного сообщения БОТА видно, что это было
                    # канальное уведомление — тогда просто шлём в канал (теряется
                    # только цитата), вместо бесполезного «не понял».
                    rt_txt = (rt.get("text") or "")
                    from_bot = ((rt.get("from") or {}).get("is_bot") is True)
                    if from_bot and rt_txt.startswith("💬"):
                        # связки нет, но pid цитируемого ищем по тексту уведомления —
                        # чтобы ответ остался ОТВЕТОМ, а не отдельным сообщением
                        qpid = chan_pid_from_note(rt_txt)
                        log(f"tg_poll: канальное уведомление без связки → в канал "
                            f"(цитата: {'да' if qpid else 'нет'}): {text[:40]!r}")
                        threading.Thread(target=tg_to_chan, daemon=True,
                                         args=({"node": None, "pid": qpid}, text)).start()
                    else:
                        # НЕ молчим: раньше непонятое сообщение только писалось в лог,
                        # и со стороны Telegram это выглядело как «не долетело».
                        log(f"tg_poll: не распознано {text[:40]!r} (это ответ: {bool(rt)})")
                        old = " (это уведомление старше моста — связки нет)" if from_bot else ""
                        tg_send(f"не понял 🤔{old}\n"
                                "• в общий канал: /chan текст сообщения\n"
                                "• в личку: ответь (reply) на «📡 Meshtastic DM»\n"
                                "• ответ в канал: ответь (reply) на «💬 …в общем канале»")
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


def feed_store(found):
    """Влить снимок nodeDB в персистентный кеш узлов (переходный этап: кеш
    НАПОЛНЯЕТСЯ, но build() пока читает по-старому). Своя нода + кого она слышит
    (per-leg в heard_by), плюс всё, что узел о себе прислал (имя/поза/телеметрия)."""
    now = int(time.time())
    for ip, info in (found or {}).items():
        if not info or not info.get("id"):
            continue
        oid = info["id"]
        nodestore.upsert(oid, ts=now, own=True,
                         name=info.get("long") or info.get("short"),
                         hw=info.get("hw"), role=info.get("role"), ip=ip,
                         cfg=json.dumps(info.get("cfg")) if info.get("cfg") else None)
        for did, e in (info.get("db") or {}).items():
            if did == oid or not isinstance(e, dict):
                continue
            # hopsAway в mesh.proto — optional: прошивка выставляет поле только
            # когда знает дистанцию. Отсутствие = НЕИЗВЕСТНО, а не «прямой приём»
            hops = e.get("hopsAway")
            # lastHeard=0 — прошивка НЕ ЗНАЕТ, когда слышала (сбитые часы, свежий
            # ребут). Раньше стояло `or now`, и незнание становилось «слышим прямо
            # сейчас»: замер показал 203 из 457 чужих узлов (44%) с выдуманной
            # свежестью, завышение до 60 ч. Такой узел не мог стать молчащим ни при
            # каком молчании — правило присутствия у него не срабатывало никогда.
            heard = int(e.get("lastHeard") or 0)
            if heard:
                nodestore.note_leg(did, oid, e.get("snr"), hops, ts=heard, src="db")
            u = e.get("user") or {}
            f = {}
            if u.get("longName") or u.get("shortName"):
                f["name"] = u.get("longName") or u.get("shortName")
            if u.get("hwModel"):
                f["hw"] = u.get("hwModel")
            if u.get("role"):
                f["role"] = u.get("role")
            if u.get("publicKey"):
                f["has_key"] = 1
            if e.get("viaMqtt"):
                f["mqtt"] = 1
            if u.get("isLicensed"):
                f["licensed"] = 1
            p = e.get("position") or {}
            if p.get("latitudeI"):
                f.update(lat=p["latitudeI"] / 1e7, lon=(p.get("longitudeI") or 0) / 1e7,
                         alt=p.get("altitude"), pos_ts=heard or None)
            dm = e.get("deviceMetrics") or {}
            if dm:
                f.update(batt=dm.get("batteryLevel"), volt=dm.get("voltage"),
                         chutil=dm.get("channelUtilization"), air=dm.get("airUtilTx"),
                         uptime=dm.get("uptimeSeconds"), dm_ts=heard or None)
            if f:
                nodestore.upsert(did, ts=heard, **f)   # heard=0 → свежесть не трогаем


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


_chan_util_cache = (0.0, None)   # (ts, значение): live.json ~300КБ, парсить на каждый вызов дорого


def chan_util():
    """Макс. загрузка канала (%) по своим нодам из live.json, или None.
    Мемоизировано на chanUtilCacheS: значение меняется медленно (телеметрия нод),
    а зовут его все эфирные воркеры каждый такт и приёмный путь — незачем
    перечитывать и разбирать 300КБ JSON каждый раз."""
    global _chan_util_cache
    now = time.time()
    ts, val = _chan_util_cache
    if now - ts < CFG.get("chanUtilCacheS", 15):
        return val
    try:
        data = json.loads(OUT_LIVE.read_text())
    except Exception:
        _chan_util_cache = (now, None)
        return None
    ch = [(n.get("info") or {}).get("chUtil") for n in data.get("nodes", []) if n.get("own")]
    ch = [c for c in ch if c is not None]
    val = max(ch) if ch else None
    _chan_util_cache = (now, val)
    return val


def paced_sleep(base_s):
    """ЕДИНОЕ правило темпа для ВСЕХ фоновых ЭФИРНЫХ процессов: адаптируем паузу
    к загрузке канала — в ТИШИНЕ «топим газ» (чаще, но не быстрее paceFloorS),
    спокойно — чуть чаще, средне — базовый темп, занято — реже. Возвращает
    текущий chUtil (или None), чтобы воркер мог ещё и пропустить такт в перегруз."""
    cu = chan_util()
    q = CFG.get("quietChUtil", 8)
    floor = max(30, CFG.get("paceFloorS", 60))
    if cu is None:
        d = base_s
    elif cu < q:                            # почти тишина → газу
        d = max(floor, base_s * 0.3)
    elif cu < q * 2.5:                       # спокойно
        d = max(floor, base_s * 0.6)
    elif cu < CFG.get("busyChUtil", 35):     # средне — базовый темп
        d = base_s
    else:                                    # занято — тормозим
        d = base_s * 2.5
    time.sleep(d)
    return cu


def send_trace(ent, to, hops):
    """Отправить traceroute БЕЗ библиотечного ожидания. Штатный sendTraceRoute
    внутри зовёт waitForTraceRoute (waitFactor × responseTimeoutSecs — до 20 минут!):
    воркер вставал на минуты, а ручная проба успевала «не ответить» задолго до
    настоящего ответа. Ответ ловит on_receive, вердикт выносим своим таймаутом."""
    from meshtastic import mesh_pb2, portnums_pb2
    # wantAck НЕ ставим: при want_ack и hop_limit=0 прошивка молча заменяет лимит
    # на дефолтный (Router.cpp), и «проба ровно на один хоп» перестаёт быть таковой
    pkt = ent["iface"].sendData(mesh_pb2.RouteDiscovery(), destinationId=to,
                                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                                wantResponse=True, hopLimit=hops)
    rid = getattr(pkt, "id", None)
    if rid:
        with lock:
            _trace_req[to] = rid     # ответ примем только с этим requestId
    return rid


def trace_answered(to, started):
    """Ответила ли нода ПОСЛЕ момента started. Смотрим на `ans` (последний ответ
    любой из наших нод), а не на `ts` лучшего пути: длинный ответ лучший путь не
    двигает, и проверка по `ts` записывала ответившую ноду в молчуны."""
    e = traces.get(to) or {}
    return max(int(e.get("ans") or 0), int(e.get("ts") or 0)) >= started


def await_trace(to, started):
    """Дождаться ответа на пробу (traceWaitS) — True, если пришёл СВЕЖИЙ путь."""
    deadline = time.time() + CFG.get("traceWaitS", 45)
    while time.time() < deadline:
        with lock:
            if trace_answered(to, started):
                return True
        time.sleep(1)
    return False


_survey_last = {}   # id -> ts последней фоновой трассировки
_trace_fails = {}   # id -> {n, ts}: сколько проб ПОДРЯД без ответа и когда последняя


def load_trace_fails():
    global _trace_fails
    try:
        d = json.loads(OUT_TRFAIL.read_text())
        _trace_fails = {k: v for k, v in d.items() if isinstance(v, dict)}
    except Exception:
        _trace_fails = {}


def save_trace_fails():
    """Отрицательные доказательства ОБЯЗАНЫ переживать рестарт: иначе после
    перезапуска узел снова считается подтверждённым, хотя трасса до него не дошла."""
    try:
        with lock:
            d = dict(sorted(_trace_fails.items(),
                            key=lambda kv: -(kv[1].get("ts") or 0))[:2000])
            _trace_fails.clear()
            _trace_fails.update(d)
        atomic_write(OUT_TRFAIL, json.dumps(d, ensure_ascii=False))
    except Exception as e:
        log(f"tracefail: {e!r}")


def note_trace_result(target, ok, src=None):
    """Итог пробы трассировки. Неудача — ОТРИЦАТЕЛЬНОЕ доказательство: после
    traceFailDrop неответов подряд снимаем подтверждение соседства, и обязательно
    с диска. Иначе старая успешная трасса держит узел «соседом» до перепроверки
    (сутки), а рестарт хаба ещё и восстанавливает её из traces.json — узел не
    уходит с карты, сколько ни трассируй.

    Снимаем ПУТЬ НЕОТВЕТИВШЕЙ НОДЫ, а не запись целиком: цель трассируется с
    разных своих нод, и «FADV не достучался» ничего не говорит о том, что FCB
    достучался минуту назад за один хоп. Раньше одна такая неудача сносила все
    пути сразу — узел мигал на карте прямо во время режима «все по очереди»."""
    drop, n = False, 0
    with lock:
        if ok:
            _trace_fails.pop(target, None)   # ответила — отрицательные улики снимаем
        else:
            e = _trace_fails.setdefault(target, {"n": 0, "ts": 0})
            n = e["n"] = int(e.get("n") or 0) + 1
            e["ts"] = int(time.time())
            if n >= CFG.get("traceFailDrop", 4):
                rec = traces.get(target) or {}
                by = {k: v for k, v in (rec.get("by") or {}).items() if k != src}
                if src and by:
                    cut = time.time() - CFG.get("traceRecheckH", 24) * 3600
                    fresh = [v for v in by.values()
                             if (v.get("ts") or 0) >= cut and v.get("path")]
                    if fresh:
                        best = min(fresh, key=lambda v: (len(v["path"]), -(v.get("ts") or 0)))
                        rec.update(path=best["path"], ts=best.get("ts") or 0, by=by,
                                   ans=max(int(v.get("ts") or 0) for v in by.values()))
                        traces[target] = rec
                    else:
                        traces.pop(target, None)
                        drop = True
                else:
                    traces.pop(target, None)
                    drop = True
    save_trace_fails()
    if drop:
        save_traces()
        log(f"🧭 соседство не подтверждено: {target} не ответил {n} раз(а) — снял")
    return drop


def trace_loop():
    """ОТДЕЛЬНЫЙ воркер ТРАССИРОВКИ (Фаза 6): traceroute по кругу — подтверждает
    прямых соседей (traceNbr), разрешает зеркала est, жнёт чужие звенья. Темп по
    загрузке канала (paced_sleep): в тишину чаще, в час пик реже/пропуск. Один
    узел за такт, очередь по давности."""
    time.sleep(15)
    while True:
        cu = paced_sleep(CFG.get("traceEveryS", 300))
        beat("trace", f"тик · chUtil {round(cu, 1) if cu is not None else '?'}%")
        try:
            if not CFG.get("traceEnabled", True):
                continue
            if cu is not None and cu > CFG.get("busyChUtil", 35):
                continue                     # перегруз — пропускаем такт даже после паузы
            try:
                data = json.loads(OUT_LIVE.read_text())
            except Exception:
                continue
            # УМНАЯ ОЧЕРЕДЬ (сходимость): backlog = ЧЁРНЫЕ неподтверждённые, которых
            # ещё НЕ проверяли — нет успешной трассы в `traces`. Проверенный (получили
            # ответ, сосед он или нет) выходит из очереди навсегда до перепроверки: не
            # долбим = экономим эфир, а «осталось» честно тает к 0. Приоритет — по
            # МОЩНОСТИ СИГНАЛА (best): сильный вероятнее реальный сосед. Не ответила →
            # `_survey_last` держит retry-gap = уходит в конец очереди.
            now = time.time()
            gap = CFG.get("traceRetryGapS", 900)           # не пробовать одну чаще
            recheck = CFG.get("traceRecheckH", 24) * 3600  # проверенных перепроверяем раз/сутки
            def fresh(i):
                """Рано пробовать снова? Обычный gap, а для молчащих — экспоненциальный
                backoff: n-й неответ подряд → пауза min(900·2ⁿ, 24ч). Без него ~250
                глухих узлов вечно конкурируют с полезными кандидатами (замерено: 77%
                проб уходило в узлы с нулём ответов, один получил 34 пробы)."""
                last = _survey_last.get(i, 0)
                n = int((_trace_fails.get(i) or {}).get("n") or 0)
                need = min(gap * (2 ** n), 24 * 3600) if n else gap
                return now - last < need

            def probe(n):
                """Кого вообще можно трассировать. Своя нода на TCP — не цель (мы и так
                говорим с ней напрямую), но своя БЕЗ TCP (Cardputer уехал с WiFi) —
                обычный узел меша, и другого способа узнать, жива ли она, нет. Раньше
                исключение по `own` было безусловным: такая нода не пробовалась НИКОГДА
                и висела на карте по уликам многочасовой давности."""
                return not n.get("own") or not n.get("online")
            # КАНДИДАТЫ по СВЕЖЕСТИ ПРИЁМА, а не по nodeDB-SNR: этот SNR принадлежит
            # громкому реле, а не узлу, поэтому сортировка по нему детерминированно
            # выбирала недостижимые фантомы (29 из топ-30). Свежесть же честна: кого
            # слышали минуту назад, тот сейчас в эфире и может ответить.
            cand_win = CFG.get("traceCandMin", 180) * 60
            todo = [(n["id"], n.get("heard") or 0) for n in data.get("nodes", [])
                    if probe(n) and n.get("hop") is None
                    and not (n.get("traceNbr") or n.get("relayNbr"))
                    and n.get("heard") and now - n["heard"] < cand_win
                    and n["id"] not in traces and not fresh(n["id"])]
            amb = [(n["id"], n.get("best")) for n in data.get("nodes", [])
                   if n.get("est") and not n["est"].get("side") and probe(n)
                   and not fresh(n["id"])]
            stale = [(n["id"], n.get("best")) for n in data.get("nodes", [])
                     if probe(n) and n["id"] in traces and not fresh(n["id"])
                     and now - traces[n["id"]].get("ts", 0) >= recheck]
            # ПРИОРИТЕТ ОЧЕРЕДИ. Тир «соседи» наполняют только пути длиной 1 хоп,
            # поэтому вперёд идут те, у кого шанс подтвердиться максимален:
            #  1) уже подтверждались соседом, но улика просрочена — перепроверка;
            #  2) несли наш трафик в нашей трассе (traceRelay) — почти наверняка рядом;
            # и только потом слепой перебор «прямых по nodeDB» (их 250+, и трассы
            # показывают, что реально это 3-9 хопов). Раньше pool = todo or amb or
            # stale голодал перепроверку: todo не пустеет никогда.
            with lock:
                tr_snap = {k: (v.get("ts") or 0, len((v.get("path") or [])))
                           for k, v in traces.items()}
            renbr = [(n["id"], n.get("best")) for n in data.get("nodes", [])
                     if probe(n) and not fresh(n["id"])
                     and tr_snap.get(n["id"], (0, 0))[1] == 2
                     and now - tr_snap[n["id"]][0] >= recheck]
            relay = [(n["id"], n.get("best")) for n in data.get("nodes", [])
                     if n.get("traceRelay") and not fresh(n["id"]) and n["id"] not in tr_snap]
            pool = renbr or relay or todo or amb or stale
            kind = ("перепроверка" if pool is renbr else "ретранслятор" if pool is relay
                    else "новый" if pool is todo else "уточнение" if pool is amb else "рекеш")
            if not pool:
                continue  # нечего проверять / всё недавно пробовали — не душним
            # Сколько узлов проверить за такт: карта показывает соседями только
            # подтверждённых трассой, поэтому очередь надо разбирать за часы, а не
            # за сутки. В тишину берём пачку (traceBatch), в средний эфир — один.
            batch = (CFG.get("traceBatch", 3)
                     if cu is not None and cu < CFG.get("quietChUtil", 8) else 1)
            targets = [x[0] for x in sorted(
                pool, key=lambda x: -(x[1] if x[1] is not None else -999))[:batch]]
            # честный остаток: кандидаты, у которых НЕТ подтверждения (ни трассой, ни
            # двусторонностью). Раньше считалось «нет записи в traces» — счётчик не
            # убывал от подтверждений и показывал 251 при трёх новых соседях
            n_todo = sum(1 for n in data.get("nodes", []) if probe(n)
                         and n.get("hop") is None
                         and not (n.get("traceNbr") or n.get("relayNbr")))
            # ВЕЕР вместо очереди: on_receive асинхронный, поэтому шлём всю пачку со
            # стаггером и ждём ОДНО окно на всех. Было send→wait×K (такт до 315 с),
            # стало K отправок + одно окно (замер: p50 ответа 1 с, p95 15 с).
            # Перекрытие проб к РАЗНЫМ целям на успех не влияет (χ², p=0.91).
            started, sent = int(time.time()), {}   # цель → своя нода, с которой слали
            for target in targets:
                _survey_last[target] = now
                sender = best_sender_for(target)
                ent = ent_by_id(sender) if sender else None
                if not ent:
                    with lock:
                        ent = next((c for c in conns.values() if c.get("iface")), None)
                if not ent:
                    break
                with lock:
                    pending_traces.add(target)
                beat("trace", f"{kind} {target} · осталось {n_todo}"
                              + (f" · веер {len(targets)}" if len(targets) > 1 else ""))
                log(f"🧭 trace [{kind}]: {target} с {ent.get('id')} (осталось {n_todo}, chUtil {cu})")
                try:
                    send_trace(ent, target, CFG.get("traceHops", 0))
                    sent[target] = ent.get("id")
                except Exception as e:
                    log(f"🧭 trace {target}: {e!r}")
                    note_trace_result(target, False, src=ent.get("id"))
                    with lock:
                        pending_traces.discard(target)
                if len(targets) > 1:
                    time.sleep(CFG.get("traceStaggerS", 4))   # не бить пачкой в один слот
            if sent:
                deadline = time.time() + CFG.get("traceWaitS", 15)
                while time.time() < deadline and any(
                        not trace_answered(t, started) for t in sent):
                    time.sleep(1)
                for target, src in sent.items():
                    ok = trace_answered(target, started)
                    note_trace_result(target, ok, src=src)
                    with lock:
                        pending_traces.discard(target)
        except Exception as e:
            log(f"trace: {e!r}")


_own_trace_i = 0


def own_trace_loop():
    """Воркер СВЕЖЕСТИ КАНАЛА МЕЖ СВОИМИ УЗЛАМИ: по кругу traceroute от одной своей
    ноды к другой. Дёшево, потому что ОДНА проба даёт SNR сразу в ОБЕ стороны (в
    ответе routeBack — измерения snrTowards на dst и snrBack на src), т.е. полное
    качество звена src↔dst одним пакетом. Результат жнётся в xlink_hist (via='tr'),
    откуда карта берёт свежие own↔own плечи. Темп по загрузке канала (в тишину чаще)."""
    global _own_trace_i, _own_traces_done
    time.sleep(25)
    while True:
        cu = paced_sleep(CFG.get("ownTraceEveryS", 600))
        beat("otrace", f"тик · chUtil {round(cu, 1) if cu is not None else '?'}%")
        try:
            if not CFG.get("ownTraceEnabled", True):
                continue
            if cu is not None and cu > CFG.get("busyChUtil", 35):
                continue
            with lock:
                owns = [{"id": c["id"], "iface": c["iface"]}
                        for c in conns.values() if c.get("iface") and c.get("id")]
            if len(owns) < 2:
                continue
            # направленные пары (src→dst) по кругу — каждая проба и так двунаправленна,
            # но обход всех направлений даёт замер с обеих сторон как источника
            pairs = [(a, b) for a in owns for b in owns if a["id"] != b["id"]]
            src, dst = pairs[_own_trace_i % len(pairs)]
            _own_trace_i += 1
            with lock:
                pending_traces.add(dst["id"])
            beat("otrace", f"{src['id']} → {dst['id']}")
            log(f"🧭 otrace: {src['id']} → {dst['id']} (chUtil {cu})")
            try:
                started = int(time.time())
                send_trace({"iface": src["iface"], "id": src["id"]}, dst["id"], 3)
                if await_trace(dst["id"], started):
                    _own_traces_done += 1
            except Exception as e:
                log(f"🧭 otrace {dst['id']}: {e!r}")
            with lock:
                pending_traces.discard(dst["id"])
        except Exception as e:
            log(f"otrace: {e!r}")


_keyfetch_last = {}   # id -> ts последнего запроса ключа


def keyfetch_loop():
    """Тихий фоновый ДОБОР ключей у keyless-нод: по ОДНОЙ за такт, темп по загрузке
    канала (paced_sleep — в тишину чаще), пропуск в перегруз, одна нода не чаще
    keyFetchPerNodeH часов. Две очереди: сначала БЛИЖАЙШИЕ (слышим напрямую, громкие
    раньше), потом те, кого лишь СЛЫШАЛИ (через хопы) — так ключи ближайших соседей
    приходят раньше, чем запись о них вытеснит из базы ноды."""
    time.sleep(35)   # дать первому скану наполнить live.json
    while True:
        cu = paced_sleep(CFG.get("keyFetchEveryS", 300))
        beat("keyfetch", f"тик · chUtil {round(cu, 1) if cu is not None else '?'}%")
        try:
            if not CFG.get("keyFetchEnabled", True):
                continue
            if cu is not None and cu > CFG.get("busyChUtil", 35):
                continue                                # эфир занят — пропускаем такт
            try:
                data = json.loads(OUT_LIVE.read_text())
            except Exception:
                continue
            now = time.time()
            # окно «был в эфире недавно»: 30 мин отсекало почти всех (keyless-ноды
            # бьются раз в 1-3 часа), а воркер и так берёт не больше одной за такт
            fresh = CFG.get("keyFetchFreshMin", 180) * 60
            per = CFG.get("keyFetchPerNodeH", 3) * 3600
            heard2 = CFG.get("keyFetchHeardMin", 720) * 60   # окно для 2-й очереди
            keyless = [n for n in data.get("nodes", [])
                       if not n.get("own") and not n.get("key")]
            ready = lambda n: (n.get("heard")
                               and now - _keyfetch_last.get(n["id"], 0) >= per)
            # 1-я ОЧЕРЕДЬ — БЛИЖАЙШИЕ соседи: слышим напрямую, громкие раньше тихих.
            # Их ключи нужны в первую очередь: именно им пишут DM, и именно они
            # успеют ответить, пока запись о них не вытеснило из базы ноды.
            t1 = sorted((n for n in keyless if n.get("hop") is None
                         and now - n["heard"] < fresh and ready(n)),
                        key=lambda n: (-(n["best"] if n.get("best") is not None else -999),
                                       0 if n.get("posSus") else 1,
                                       _keyfetch_last.get(n["id"], 0)))
            # 2-я ОЧЕРЕДЬ — те, кого лишь СЛЫШАЛИ (сейчас доступны через хопы):
            # запрос уйдёт через ретрансляторы, свежих спрашиваем раньше
            t2 = sorted((n for n in keyless if n.get("hop") is not None
                         and now - n["heard"] < heard2 and ready(n)),
                        key=lambda n: -(n.get("heard") or 0))
            pool, tier = (t1, "сосед") if t1 else (t2, "через хопы")
            if not pool:
                # видно, сколько ещё без ключа: основную работу делает событийный
                # путь (maybe_solicit_key при приёме), этот воркер — добор в тишину
                beat("keyfetch", f"нет подходящих · без ключа {len(keyless)}")
                continue
            target = pool[0]["id"]
            _keyfetch_last[target] = now
            beat("keyfetch", f"добор {tier} {target} · очередь {len(t1)}+{len(t2)}")
            log(f"🔑 добор ключа у {target} ({tier}, очередь {len(t1)}+{len(t2)})")
            solicit_key(target)
            # у кого ключ появился — счётчик запросов больше не нужен
            keyed = {n["id"] for n in data.get("nodes", []) if n.get("key")}
            with lock:
                stale = [k for k in _key_asks if k in keyed]
                for k in stale:
                    _key_asks.pop(k, None)
            if stale:
                save_key_asks()
        except Exception as e:
            log(f"keyfetch: {e!r}")


def geocode_loop():
    """Геокодинг адресных имён нод (Фаза 6-В, пул мягких якорей): раз в сутки
    превращаем «Pulkovskoe 65» → координаты через Nominatim. Кэш персистентный,
    новые имена дёргаем по 1/сек. Верификация по GPS, если нода его вещает."""
    time.sleep(20)  # дать первому скану наполнить live.json/имена
    while True:
        try:
            if CFG.get("geocodeEnabled", True):
                _do_geocode()
            beat("geocode", "проход")
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
    global _geocoded_count
    _geocoded_count = sum(1 for v in addr.values() if v)
    if new:
        atomic_write(GEO_ADDR, json.dumps(addr, ensure_ascii=False, indent=1))
        log(f"🏠 геокодинг: +{new} имён, всего с координатами {_geocoded_count}")


last_found = {}    # последний снимок своих нод (писатель → читатель)
last_xlinks = []
render_now = threading.Event()   # «пересобери карту сейчас» (например, пришла трассировка)


def _store_keep_s():
    # Кэш держит ноду ровно пока она может быть на карте: «прямой» + «бывший».
    # Дальше build_from_store её всё равно отбрасывает, а график истории живёт в
    # отдельной history.db — worldMaxAgeH (окно графика) сюда не относится.
    return (CFG.get("directWindowH", 24) + CFG.get("formerWindowH", 1)) * 3600


def writer_loop():
    """Воркер №1 (ПИСАТЕЛЬ): опрос своих нод → nodestore. Про отображение не
    думает; on_receive пишет туда же событийно. Плюс таймауты исходящих."""
    global last_found, last_xlinks
    while True:
        try:
            with lock:
                live = {ip: c for ip, c in conns.items() if c.get("iface")}
            if live:
                found = {ip: snapshot(c) for ip, c in live.items()}
                last_found = found
                try:
                    last_xlinks = history.xlink_pairs(hours=CFG.get("xlinkHours", 336))
                except Exception:
                    pass
                feed_store(found)
                with rx_lock:
                    dlive = dict(direct_live)
                for did, dv in dlive.items():       # точные прямые из потока (батч)
                    nodestore.note_leg(did, dv[2] or "?", dv[1], 0, ts=int(dv[0]), src="rx")
                rx_flush()
                beat("writer", f"опрос {len(found)} нод")
        except Exception as e:
            log(f"writer: {e!r}")
        try:                                        # статусы исходящих (из topo)
            dirty = False
            notes = []
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
                    t = tg_note(m)
                    if t:
                        notes.append(t)
                        dirty = True
            if dirty:
                save_messages()
            tg_send_batch(notes)
        except Exception:
            pass
        time.sleep(CFG.get("topoEveryS", 60))


def reader_loop():
    """Воркер №2 (ЧИТАТЕЛЬ): nodestore → live.json. Идёт по всем ключам,
    таймеры last_direct (24ч чёрная / +1ч серая), раскладка, геолокация.
    Независим от прихода данных. Свои ноды/keys_by — из last_found."""
    time.sleep(4)
    while True:
        try:
            if last_found:
                store = nodestore.load(_store_keep_s())
                with lock:
                    asks_snap = {k: dict(v) for k, v in _key_asks.items()}
                    hears_snap = {k: dict(v) for k, v in _hears_us.items()}
                # призракам нужен ШИРОКИЙ снимок: store грузится окном карты
                # (25 ч), а узел из чужой трассы может быть слышан нами куда
                # реже — без него призрак остаётся безымянным и без своего GPS
                cache_wide = nodestore.load(CFG.get("xlinkHours", 336) * 3600)
                data = scan.build_from_store(store, found=last_found, xlinks=last_xlinks,
                                             asks=asks_snap, hears_us=hears_snap,
                                             traces=dict(traces), favorites=set(favorites),
                                             cache_wide=cache_wide)
                atomic_write(OUT_LIVE, json.dumps(data, ensure_ascii=False, indent=1))
                hist_tick(data)
                nodestore.save_positions({n["id"]: (n.get("x"), n.get("y"))
                                          for n in data.get("nodes", []) if n.get("x") is not None})
                check_batt(data)
                ln = data.get("nodes", [])
                beat("reader", f"{len(ln)} нод на карте")
                try:                        # срез метрик во времени (графики статуса)
                    history.record_metrics({
                        "chutil": chan_util(), "cache": nodestore.stats().get("nodes"),
                        "live": len(ln), "traces_done": _traces_done,
                        "est": sum(1 for n in ln if n.get("est")),
                        "possus": sum(1 for n in ln if n.get("posSus")),
                        "tracenbr": sum(1 for n in ln if n.get("traceNbr")),
                        "keyless": sum(1 for n in ln if not n.get("own") and not n.get("key")),
                        "msgs": len(messages), "chan": len(channel),
                        "own_online": sum(1 for c in list(conns.values()) if c.get("iface")),
                        "pruned": _pruned_total, "geocoded": _geocoded_count,
                        "tg_relayed": _tg_relayed, "own_traces": _own_traces_done,
                        # остаток трассировки = ЕЩЁ НЕ ПРОВЕРЕННЫЕ чёрные (не traceNbr и
                        # нет успешной трассы в traces) — честный сигнал сходимости
                        "trace_todo": sum(1 for n in ln if not n.get("own")
                                          and n.get("hop") is None and not n.get("traceNbr")
                                          and n["id"] not in traces)})
                except Exception as e:
                    log(f"metrics: {e!r}")
        except Exception as e:
            import traceback
            log(f"reader: {e!r}\n{traceback.format_exc()}")
        # обычный темп, но просыпаемся раньше по render_now (пришла трассировка)
        render_now.wait(CFG.get("renderEveryS", 60))
        render_now.clear()


def node_tier(n):
    """Тир узла — та же классификация, по которой карта показывает уровни:
    own (своя) · nbr (подтверждён: трасса дошла или доказана двусторонность) ·
    heard (слышим напрямую, подтверждения нет) · former (через хопы)."""
    if n.get("own"):
        return "own"
    if n.get("traceNbr") or n.get("relayNbr"):
        return "nbr"          # подтверждён: трасса дошла или доказана двусторонность
    if n.get("hop") is None:
        return "heard"        # слышим напрямую, но подтверждения нет
    return "former"


def prep_loop():
    """Воркер №4 (ПОДГОТОВКА): как только reader обновил live.json — заранее
    раскладывает узлы по тирам (data/tiers.json) и кладёт рядом ПРЕДСЖАТЫЕ .gz
    для live.json/tiers.json. Клиенту и статусу не нужно ничего вычислять на
    запросе: _static отдаёт готовый .gz, а разбивка «свои/соседи/бывшие» уже
    посчитана. Работает по mtime — лишний раз не пересчитывает."""
    last_mtime = 0
    while True:
        try:
            try:
                mtime = OUT_LIVE.stat().st_mtime
            except OSError:
                mtime = 0
            if mtime and mtime != last_mtime:
                raw = OUT_LIVE.read_bytes()
                data = json.loads(raw)
                nodes = data.get("nodes", []) or []
                tier = {n["id"]: node_tier(n) for n in nodes if n.get("id")}
                cnt = {t: sum(1 for v in tier.values() if v == t)
                       for t in ("own", "nbr", "heard", "former")}
                cnt["est"] = sum(1 for n in nodes if n.get("est"))
                out = {"updated": (data.get("meta") or {}).get("updated"),
                       "counts": cnt, "tier": tier}
                body = json.dumps(out, ensure_ascii=False)
                atomic_write(OUT_TIERS, body)
                # предсжатие: запросы больше не тратят CPU на gzip каждого ответа
                atomic_write_bytes(OUT_LIVE.with_name(OUT_LIVE.name + ".gz"),
                                   gzip.compress(raw, 6))
                atomic_write_bytes(OUT_TIERS.with_name(OUT_TIERS.name + ".gz"),
                                   gzip.compress(body.encode(), 6))
                last_mtime = mtime
                beat("prep", f"свои {cnt['own']} · подтв. соседи {cnt['nbr']} · "
                             f"слышим {cnt['heard']} · бывшие {cnt['former']} · est {cnt['est']}")
            else:
                beat("prep", "актуально")
        except Exception as e:
            log(f"prep: {e!r}")
        time.sleep(CFG.get("prepEveryS", 15))


def pruner_loop():
    """Воркер №3 (ПРУНЕР): удаляет узлы, вышедшие за все лимиты (кроме своих и избранных)."""
    global _pruned_total
    while True:
        time.sleep(CFG.get("pruneEveryS", 300))
        try:
            n = nodestore.prune(_store_keep_s(), keep_ids=favorites)
            _pruned_total += n
            beat("pruner", f"удалено {n} · всего {_pruned_total}")
        except Exception as e:
            log(f"pruner: {e!r}")


# ---------- HTTP: статика + API ----------

class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 + keep-alive: браузер тянет страницу (html, app.js, css, live.json,
    # /api/*) по ОДНОМУ соединению вместо нового на каждый файл. Важно не столько
    # для сервера, сколько для сети: установка нового TCP к этому хосту у части
    # клиентов залипает на секунды, и десяток соединений превращался в десяток
    # залипаний. Все ответы ставят Content-Length (_json/_static/штатный
    # обработчик), поэтому кадрирование корректно; timeout закрывает простаивающие.
    protocol_version = "HTTP/1.1"
    timeout = 30

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT.parent), **kw)

    def log_message(self, *a):
        pass

    # Текстовая статика, которую отдаём сами: gzip + ETag (см. _static).
    TEXT_TYPES = {".json": "application/json; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".svg": "image/svg+xml",
                  ".html": "text/html; charset=utf-8"}

    def end_headers(self):
        p = urlparse(self.path).path
        if p.startswith("/api/"):
            pass                        # API ставит no-store сам
        elif p.startswith("/img/") or p.startswith("/vendor/"):
            # Иконки устройств и вендорный leaflet практически не меняются, но весят
            # больше всего (img/hw ~1.3МБ, leaflet 162КБ). С no-cache браузер
            # ревалидировал ДЕСЯТКИ файлов на каждой перезагрузке — теперь берёт из
            # своего кэша, не тратя ни запроса. Обновление иконок — раз в жизнь.
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            # app.js/style.css/index.html — всегда свежие (ревалидация дешёвая: ETag→304)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _static(self):
        """Текстовая статика своими руками: gzip (SVG-иконки, app.js, css) + ETag/304,
        чтобы перезагрузка не тянула тело повторно. True = запрос обработали."""
        p = urlparse(self.path).path
        ctype = self.TEXT_TYPES.get(os.path.splitext(p)[1].lower())
        if not ctype:
            return False
        fs = self.translate_path(self.path)
        try:
            st = os.stat(fs)
        except OSError:
            return False
        if not os.path.isfile(fs):
            return False
        etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return True
        want_gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        # Готовый .gz рядом с файлом (его пишет воркер prep для live.json/tiers.json)
        # свежее самого файла → отдаём как есть, не тратя CPU на сжатие в запросе.
        if want_gz:
            try:
                sc = os.stat(fs + ".gz")
                if sc.st_mtime >= st.st_mtime:
                    body = open(fs + ".gz", "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return True
            except OSError:
                pass
        try:
            raw = open(fs, "rb").read()
        except OSError:
            return False
        gz = len(raw) > 512 and want_gz
        body = gzip.compress(raw, 6) if gz else raw
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("ETag", etag)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        gz = code == 200 and len(body) > 512 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
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
        elif self.path.startswith("/api/status/history"):
            try:
                hours = float((parse_qs(urlparse(self.path).query).get("hours") or [24])[0])
            except ValueError:
                hours = 24
            # корзина = час (или мельче для коротких окон), но не больше 48 точек
            bins = max(1, min(48, round(hours)))
            self._json({"series": history.metrics_series(hours=hours, bins=bins)})
        elif self.path.startswith("/api/status"):
            self._json(self._status())
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
            # диагностика: как каждая своя нода ВИДИТ узел прямо сейчас (сырой nodeDB):
            # есть ли в базе, есть ли publicKey (pk=длина, 0=нет ключа), хопы, роль,
            # + размер базы ноды (близко к 250 = вытеснение по LRU).
            tid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            out = {}
            with lock:
                live = [c for c in conns.values() if c.get("iface")]
            for c in live:
                try:
                    db = dict(c["iface"].nodes or {})
                except Exception:
                    db = {}
                e = db.get(tid)
                info = {"dbSize": len(db), "inDb": isinstance(e, dict)}
                if isinstance(e, dict):
                    lh = e.get("lastHeard") or 0
                    u = e.get("user") or {}
                    info.update(hopsAway=e.get("hopsAway"), snr=e.get("snr"), lastHeard=lh,
                                ageMin=round((time.time() - lh) / 60, 1),
                                pk=len(u.get("publicKey") or ""), role=u.get("role"), hw=u.get("hwModel"))
                out[c["id"]] = info
            self._json({"id": tid, "seenBy": out})
        elif not self._static():   # текстовая статика: gzip + ETag/304
            super().do_GET()

    def _status(self):
        """Сводка для страницы статуса: uptime, пульс воркеров, свои ноды, данные."""
        now = time.time()
        with lock:
            conns_snap = [(ip, c) for ip, c in conns.items()]
        try:
            live = json.loads(OUT_LIVE.read_text())
        except Exception:
            live = {"nodes": [], "meta": {}}
        lnodes = live.get("nodes", [])
        own_live = {n["id"]: n for n in lnodes if n.get("own")}
        workers = {name: {"age": round(now - b["ts"], 1), "note": b.get("note", "")}
                   for name, b in worker_beats.items()}
        nodes = []
        for ip, c in conns_snap:
            cid = c.get("id")
            info = (own_live.get(cid) or {}).get("info") or {}
            try:
                db = len(dict(c["iface"].nodes or {})) if c.get("iface") else None
            except Exception:
                db = None
            nodes.append({"id": cid, "ip": ip, "connected": bool(c.get("iface")),
                          "name": (own_live.get(cid) or {}).get("label") or cid,
                          "silent": round(now - c.get("last", 0), 0) if c.get("last") else None,
                          "dbSize": db, "chUtil": info.get("chUtil"),
                          "batt": info.get("battery"), "uptime": info.get("uptime")})
        ns = nodestore.stats()
        meta = live.get("meta", {})
        liveAge = round(now - int((meta.get("updatedTs") or 0) / 1000), 0) if meta.get("updatedTs") else None
        data = {"cacheNodes": ns.get("nodes"), "cacheDirect3m": ns.get("direct_3min"),
                "liveNodes": len(lnodes), "liveAge": liveAge,
                "messages": len(messages), "channel": len(channel), "traces": len(traces),
                "est": sum(1 for n in lnodes if n.get("est")),
                "posSus": sum(1 for n in lnodes if n.get("posSus")),
                "traceNbr": sum(1 for n in lnodes if n.get("traceNbr")),
                "keyless": sum(1 for n in lnodes if not n.get("own") and not n.get("key"))}
        with lock:
            mism = [{"ip": ip, **v} for ip, v in id_mismatch.items()]
        return {"uptime": round(now - START_TS, 0), "now": int(now),
                "chUtil": chan_util(), "workers": workers, "nodes": nodes, "data": data,
                "mismatches": mism, "relay": dict(_relay_stats)}

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
        elif self.path == "/api/key":
            # ручной запрос ключа из панели: сбрасываем кулдаун и шлём сразу
            nid = (body.get("id") or "").strip()
            if not nid:
                self._json({"ok": False, "error": "нужен id"}, 400)
                return
            if anyone_has_key(nid):
                self._json({"ok": True, "have": True})
                return
            with lock:
                _keyfetch_last.pop(nid, None)
            via = solicit_key(nid, force=True)
            with lock:
                asks = int((_key_asks.get(nid) or {}).get("n") or 0)
            self._json({"ok": bool(via), "via": via or None, "asks": asks,
                        **({} if via else {"error": "нет своей ноды на связи"})})
        elif self.path == "/api/favorite":
            # избранное: узел не прунится из кеша (для мобильных/важных, что молчат)
            fid, on = body.get("id"), body.get("on", True)
            if not fid:
                self._json({"ok": False, "error": "нет id"}, 400)
                return
            with lock:
                if on:
                    favorites.add(fid)
                else:
                    favorites.discard(fid)
            save_favorites()
            self._json({"ok": True, "fav": on})
        elif self.path == "/api/config":
            clean = {}
            for k in EDITABLE:
                if k not in body:
                    continue
                v = body[k]
                if k in ("subnets", "mobile", "fragile", "pingWords"):
                    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
                        continue
                elif k == "pingReply":
                    if not isinstance(v, bool):
                        continue
                elif k == "pingPrefix":
                    if not isinstance(v, str) or len(v.encode()) > 80:
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
                started = int(time.time())
                with lock:
                    pending_traces.add(to)
                    manual_pending.add(to)   # метка «запрошена из интерфейса»
                    # Запись НЕ стираем. Раньше стирали («путь строим заново»), и в
                    # режиме «все по очереди» каждая следующая нода сносила улики
                    # предыдущих: на время пробы узел оставался вовсе без трасс и
                    # мигал на карте. Ответ этой ноды заменит её же путь при мерже,
                    # пути остальных останутся на месте.
                try:
                    send_trace(ent, to, 7)   # ручная — ищем ПОЛНЫЙ путь, лимит 7
                except Exception as e:
                    log(f"🧭 trace {to}: {e!r}")
                answered = await_trace(to, started)
                with lock:
                    pending_traces.discard(to)
                # неответ на РУЧНУЮ пробу — тоже доказательство: снимаем соседство
                # (иначе узел висел бы «соседом» по старой трассе, а рестарт хаба
                # поднимал бы её с диска)
                note_trace_result(to, answered, src=ent.get("id"))
            threading.Thread(target=_trace, daemon=True).start()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "нет такого API"}, 404)


def main():
    load_messages()
    load_traces()
    load_tgmap()
    load_favorites()
    load_key_asks()
    load_trace_fails()
    load_hears_us()
    try:
        n = nodestore.repair_freshness()
        if n:
            log(f"🩹 свежесть без улик исправлена у {n} узлов (метки от сбитых часов nodeDB)")
    except Exception as e:
        log(f"repair_freshness: {e}")
    try:
        n = purge_byte_collisions()
        if n:
            log(f"🩹 снято ложных «слышит нас» по байту своей ноды: {n}")
    except Exception as e:
        log(f"purge_byte_collisions: {e}")
    try:
        n = backfill_names()
        if n:
            log(f"🩹 подписано имён в истории вместо сырых id: {n}")
    except Exception as e:
        log(f"backfill_names: {e}")
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    for _w in ("keeper", "writer", "reader", "prep", "pruner", "tg", "trace", "otrace", "keyfetch", "geocode"):
        beat(_w, "запуск…")   # чтобы все воркеры сразу видны на странице статуса
    threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=writer_loop, daemon=True).start()   # №1 опрос → кеш
    threading.Thread(target=reader_loop, daemon=True).start()   # №2 кеш → live.json
    threading.Thread(target=pruner_loop, daemon=True).start()   # №3 чистка кеша
    threading.Thread(target=prep_loop, daemon=True).start()     # №4 тиры + предсжатие
    threading.Thread(target=tg_poll_loop, daemon=True).start()  # Telegram→меш ответы
    threading.Thread(target=trace_loop, daemon=True).start()    # отдельный воркер трассировки (темп по каналу)
    threading.Thread(target=own_trace_loop, daemon=True).start()# свежесть канала меж своими (own↔own traceroute)
    threading.Thread(target=keyfetch_loop, daemon=True).start() # тихий добор ключей у keyless (темп по каналу)
    threading.Thread(target=geocode_loop, daemon=True).start()  # геокодинг адресных имён
    log(f"hub на http://localhost:{PORT} — сайт, /api/messages, /api/send, /api/read")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
