"""Персистентный кеш СОСТОЯНИЯ узлов (SQLite) — единый источник истины вместо
пересборки карты из волатильного снимка nodeDB каждый скан + жонглирования
файлами (live.json-как-prev, directSeen, direct_live, cache-плечи, silent…).

По каждому узлу копим ВСЁ, что он о себе прислал, с таймстемпами; на старте
поднимаем из кеша. Статус прямая/релей/ушла — чистая функция таймстемпов
(last_direct / last_relay / now), без флапа снимка. Протухание — по last_heard.

Схема: одна строка на узел. `heard_by` (JSON) = {своя_нода: {snr,hops,ts}} —
сохраняет per-leg инфу (кто кого слышит), нужную для линков и геолокации.
Живёт там же, где history.db (на сервере, в .gitignore).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "nodes.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_state(
  id TEXT PRIMARY KEY,
  name TEXT, hw TEXT, role TEXT, own INTEGER DEFAULT 0,
  last_heard INTEGER, last_direct INTEGER, last_relay INTEGER,
  snr REAL, hops INTEGER,
  lat REAL, lon REAL, alt INTEGER, pos_ts INTEGER,
  batt REAL, volt REAL, chutil REAL, air REAL, uptime INTEGER, dm_ts INTEGER,
  has_key INTEGER, mqtt INTEGER, licensed INTEGER,
  heard_by TEXT, cfg TEXT, ip TEXT, subnet TEXT,
  x REAL, y REAL, updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_ns_heard ON node_state(last_heard);
"""

_lock = threading.Lock()
_conn = None

# колонки, которые перезаписываем «свежайшим ненулевым» при upsert
_MERGE = ("name", "hw", "role", "snr", "hops", "lat", "lon", "alt", "pos_ts",
          "batt", "volt", "chutil", "air", "uptime", "dm_ts",
          "has_key", "mqtt", "licensed", "cfg", "ip", "subnet")


def _db():
    global _conn
    if _conn is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _row(c, nid):
    return c.execute("SELECT * FROM node_state WHERE id=?", (nid,)).fetchone()


def upsert(nid, ts=None, own=False, **fields):
    """Влить в узел `nid` то, что он о себе прислал. ts — время наблюдения:
    двигает last_heard; поля из _MERGE перезаписываются только НЕ-None значением
    (не затираем известное пустым). last_direct/last_relay ставит note_leg."""
    ts = int(ts or time.time())
    with _lock:
        c = _db()
        cur = _row(c, nid)
        data = dict(cur) if cur else {"id": nid}
        data["last_heard"] = max(data.get("last_heard") or 0, ts)
        data["updated"] = int(time.time())
        if own:
            data["own"] = 1
        for k in _MERGE:
            v = fields.get(k)
            if v is not None:
                data[k] = v
        _write(c, data)


def note_leg(nid, own_id, snr, hops, ts=None, own_node=False):
    """Зафиксировать, что СВОЯ нода own_id услышала узел nid (hops=0 = прямой).
    Обновляет heard_by[own_id] и last_direct/last_relay + last_heard."""
    ts = int(ts or time.time())
    direct = not hops
    with _lock:
        c = _db()
        cur = _row(c, nid)
        data = dict(cur) if cur else {"id": nid}
        hb = {}
        if data.get("heard_by"):
            try:
                hb = json.loads(data["heard_by"])
            except Exception:
                hb = {}
        hb[own_id] = {"snr": snr, "hops": hops or 0, "ts": ts}
        data["heard_by"] = json.dumps(hb)
        data["last_heard"] = max(data.get("last_heard") or 0, ts)
        if direct:
            data["last_direct"] = max(data.get("last_direct") or 0, ts)
            if snr is not None:
                data["snr"] = snr
            data["hops"] = 0
        else:
            data["last_relay"] = max(data.get("last_relay") or 0, ts)
            if not data.get("last_direct") or data["last_direct"] < ts - 1:
                data["hops"] = hops
        if own_node:
            data["own"] = 1
        data["updated"] = int(time.time())
        _write(c, data)


def save_positions(pos):
    """pos = {id: (x,y)} — сохранить позиции раскладки для carry-forward."""
    with _lock:
        c = _db()
        c.executemany("UPDATE node_state SET x=?, y=? WHERE id=?",
                      [(p[0], p[1], nid) for nid, p in pos.items()])
        c.commit()


def load(max_age_s):
    """Все узлы, слышанные не дольше max_age_s назад → список dict (сырые поля).
    Это и есть «поднять из кеша на старте / на каждый build»."""
    since = int(time.time()) - int(max_age_s)
    with _lock:
        rows = _db().execute(
            "SELECT * FROM node_state WHERE last_heard>=? OR own=1", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("heard_by"):
            try:
                d["heard_by"] = json.loads(d["heard_by"])
            except Exception:
                d["heard_by"] = {}
        else:
            d["heard_by"] = {}
        for j in ("cfg",):
            if d.get(j):
                try:
                    d[j] = json.loads(d[j])
                except Exception:
                    d[j] = None
        out.append(d)
    return out


def prune(max_age_s, keep_ids=None):
    """Удаляет протухшие (кроме своих и избранных keep_ids). Возвращает число удалённых."""
    cut = int(time.time()) - int(max_age_s)
    keep = [k for k in (keep_ids or ()) if k]
    q = "DELETE FROM node_state WHERE last_heard<? AND own=0"
    args = [cut]
    if keep:
        q += " AND id NOT IN (%s)" % ",".join("?" * len(keep))
        args += keep
    with _lock:
        c = _db()
        cur = c.execute(q, args)
        c.commit()
        return cur.rowcount or 0


def stats():
    with _lock:
        c = _db()
        n = c.execute("SELECT COUNT(*) FROM node_state").fetchone()[0]
        now = int(time.time())
        direct = c.execute("SELECT COUNT(*) FROM node_state WHERE last_direct>=?",
                           (now - 180,)).fetchone()[0]
    return dict(nodes=n, direct_3min=direct)


def _write(c, data):
    cols = [k for k in data if k in _COLS]
    ph = ",".join("?" for _ in cols)
    c.execute(f"INSERT OR REPLACE INTO node_state ({','.join(cols)}) VALUES ({ph})",
              [data[k] for k in cols])
    c.commit()


_COLS = ("id", "name", "hw", "role", "own", "last_heard", "last_direct", "last_relay",
         "snr", "hops", "lat", "lon", "alt", "pos_ts", "batt", "volt", "chutil",
         "air", "uptime", "dm_ts", "has_key", "mqtt", "licensed", "heard_by",
         "cfg", "ip", "subnet", "x", "y", "updated")
