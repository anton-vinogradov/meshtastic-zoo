"""Лог истории меша в SQLite — фундамент под графики, uptime и алерты.

Каждый снимок топологии (data из scan.build) раскладывается в две таблицы:
  node_hist — по ноде за момент ts: online, заряд, напряжение, airtime, загрузка канала;
  link_hist — по плечу src→dst за момент ts: SNR и число хопов.
Отсюда фронт строит тренды/спарклайны, а детектор алертов (фаза 2) сравнивает
соседние срезы, чтобы поймать «нода упала / вернулась / села батарея».

Пишется только там, где крутится hub (на сервере) — БД в data/history.db (в .gitignore).
"""
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_hist(
  ts INTEGER, id TEXT, own INTEGER, online INTEGER,
  batt REAL, volt REAL, air REAL, chutil REAL, heard INTEGER,
  PRIMARY KEY(ts, id));
CREATE INDEX IF NOT EXISTS ix_node_id_ts ON node_hist(id, ts);
CREATE TABLE IF NOT EXISTS link_hist(
  ts INTEGER, src TEXT, dst TEXT, snr REAL, hops INTEGER,
  PRIMARY KEY(ts, src, dst));
CREATE INDEX IF NOT EXISTS ix_link_pair_ts ON link_hist(src, dst, ts);
CREATE TABLE IF NOT EXISTS rx_hist(
  ts INTEGER, node TEXT, src TEXT, n INTEGER,
  snr REAL, snr_sd REAL, rssi REAL,
  PRIMARY KEY(ts, node, src));
CREATE INDEX IF NOT EXISTS ix_rx_src_ts ON rx_hist(src, ts);
CREATE TABLE IF NOT EXISTS xlink_hist(
  ts INTEGER, a TEXT, b TEXT, snr REAL, via TEXT,
  PRIMARY KEY(ts, a, b, via));
CREATE INDEX IF NOT EXISTS ix_xlink_pair ON xlink_hist(a, b, ts);
CREATE TABLE IF NOT EXISTS metrics_hist(
  ts INTEGER PRIMARY KEY, chutil REAL, cache INTEGER, live INTEGER,
  est INTEGER, possus INTEGER, tracenbr INTEGER, keyless INTEGER,
  traces_done INTEGER, msgs INTEGER, chan INTEGER);
"""

_lock = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def record(data, ts=None, fresh=180):
    """Врезать один срез топологии в историю. fresh — окно (с), в котором
    услышанная нода-сосед считается online (у своих online берётся явно)."""
    ts = int(ts or time.time())
    nrows, lrows = [], []
    for n in data.get("nodes", []) or []:
        info = n.get("info") or {}
        own = 1 if n.get("own") else 0
        heard = n.get("heard")
        if own:
            online = 1 if n.get("online") else 0
        else:
            online = 1 if (heard and ts - heard <= fresh) else 0
        nrows.append((ts, n.get("id"), own, online,
                      info.get("battery"), info.get("voltage"),
                      info.get("airTx"), info.get("chUtil"), heard))
    for l in data.get("links", []) or []:
        if l.get("type") != "rf":
            continue
        snr, hops = l.get("snr"), l.get("hops")
        if snr is None and hops is None:
            continue  # «нет данных» — не пишем
        lrows.append((ts, l.get("from"), l.get("to"), snr, hops))
    with _lock:
        c = _db()
        c.executemany("INSERT OR REPLACE INTO node_hist VALUES(?,?,?,?,?,?,?,?,?)", nrows)
        c.executemany("INSERT OR REPLACE INTO link_hist VALUES(?,?,?,?,?)", lrows)
        c.commit()
    return len(nrows), len(lrows)


def record_rx(rows):
    """Пер-пакетная сигнальная жатва (фундамент геолокации): агрегаты ПРЯМЫХ
    приёмов за интервал сброса. rows = [(ts, node, src, n, snr, snr_sd, rssi)].
    node — своя нода-приёмник, src — передатчик; snr_sd (дисперсия во времени)
    различает LOS/NLOS, rssi не сатурируется на сильном сигнале, как SNR."""
    if not rows:
        return
    with _lock:
        c = _db()
        c.executemany("INSERT OR REPLACE INTO rx_hist VALUES(?,?,?,?,?,?,?)", rows)
        c.commit()


def record_xlinks(rows):
    """Чужие звенья a→b (b услышал a на snr) — жатва traceroute (via='tr') и
    пассивных NeighborInfo (via='ni'). Расширяют геометрию: звено от узла с
    известной позицией — кольцо расстояния для его соседа."""
    if not rows:
        return
    with _lock:
        c = _db()
        c.executemany("INSERT OR REPLACE INTO xlink_hist VALUES(?,?,?,?,?)", rows)
        c.commit()


def rx_series(node, src, hours=24):
    since = int(time.time()) - int(hours) * 3600
    with _lock:
        rows = _db().execute(
            "SELECT ts, n, snr, snr_sd, rssi FROM rx_hist "
            "WHERE node=? AND src=? AND ts>=? ORDER BY ts", (node, src, since)).fetchall()
    return [dict(ts=r[0], n=r[1], snr=r[2], sd=r[3], rssi=r[4]) for r in rows]


def xlink_pairs(hours=168):
    """Сводка чужих звеньев за окно: пара, число замеров, средний/лучший SNR."""
    since = int(time.time()) - int(hours) * 3600
    with _lock:
        rows = _db().execute(
            "SELECT a, b, via, COUNT(*), AVG(snr), MAX(snr), MAX(ts) FROM xlink_hist "
            "WHERE ts>=? GROUP BY a, b, via ORDER BY COUNT(*) DESC", (since,)).fetchall()
    return [dict(a=r[0], b=r[1], via=r[2], n=r[3], snr=round(r[4], 2),
                 best=r[5], last=r[6]) for r in rows]


_METRIC_COLS = ("chutil", "cache", "live", "est", "possus", "tracenbr",
                "keyless", "traces_done", "msgs", "chan")


def record_metrics(m, ts=None):
    """Срез метрик воркеров/сервиса во времени (для графиков на странице статуса)."""
    ts = int(ts or time.time())
    with _lock:
        c = _db()
        c.execute("INSERT OR REPLACE INTO metrics_hist(ts," + ",".join(_METRIC_COLS) + ") "
                  "VALUES(" + ",".join("?" * (1 + len(_METRIC_COLS))) + ")",
                  (ts, *(m.get(k) for k in _METRIC_COLS)))
        c.commit()


def metrics_series(hours=6):
    """Ряд метрик за окно (сырой, 1 точка/цикл ридера ~60с)."""
    since = int(time.time()) - int(hours * 3600)
    with _lock:
        rows = _db().execute(
            "SELECT ts," + ",".join(_METRIC_COLS) + " FROM metrics_hist "
            "WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    return [dict(ts=r[0], **{k: r[i + 1] for i, k in enumerate(_METRIC_COLS)}) for r in rows]


def prune(days=30):
    """Удалить срезы старше days суток."""
    cutoff = int(time.time()) - int(days) * 86400
    with _lock:
        c = _db()
        for t in ("node_hist", "link_hist", "rx_hist", "xlink_hist", "metrics_hist"):
            c.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
        c.commit()


def node_series(node_id, hours=24):
    since = int(time.time()) - int(hours) * 3600
    with _lock:
        rows = _db().execute(
            "SELECT ts, online, batt, volt, air, chutil FROM node_hist "
            "WHERE id=? AND ts>=? ORDER BY ts", (node_id, since)).fetchall()
    return [dict(ts=r[0], online=r[1], batt=r[2], volt=r[3], air=r[4], chutil=r[5])
            for r in rows]


def link_series(src, dst, hours=24):
    since = int(time.time()) - int(hours) * 3600
    with _lock:
        rows = _db().execute(
            "SELECT ts, snr, hops FROM link_hist WHERE src=? AND dst=? AND ts>=? "
            "ORDER BY ts", (src, dst, since)).fetchall()
    return [dict(ts=r[0], snr=r[1], hops=r[2]) for r in rows]


def uptime(hours=24):
    """Доля срезов, где нода была online, в % — по всем нодам за период."""
    since = int(time.time()) - int(hours) * 3600
    with _lock:
        rows = _db().execute(
            "SELECT id, AVG(online)*100.0, COUNT(*), MAX(ts), MAX(own) FROM node_hist "
            "WHERE ts>=? GROUP BY id", (since,)).fetchall()
    return {r[0]: dict(pct=round(r[1], 1), n=r[2], last=r[3], own=bool(r[4]))
            for r in rows}


def node_counts(hours=24, bins=48):
    """Сколько узлов было на карте по времени: на каждый снимок ts в node_hist
    одна строка на ноду, значит COUNT(*) GROUP BY ts = число узлов в тот момент.
    Усредняем по bins корзинам за окно hours. Возвращаем ряд {ts,total,own} +
    точный текущий счёт (последний снимок)."""
    now = int(time.time())
    since = now - int(hours) * 3600
    bins = max(1, int(bins))
    bw = max(1, int(hours) * 3600 // bins)
    with _lock:
        rows = _db().execute(
            "SELECT ts, COUNT(*), COALESCE(SUM(own), 0) FROM node_hist "
            "WHERE ts>=? GROUP BY ts ORDER BY ts", (since,)).fetchall()
    acc = [[0, 0, 0] for _ in range(bins)]  # sum_total, sum_own, n
    for ts, c, o in rows:
        b = int((ts - since) / bw)
        if 0 <= b < bins:
            acc[b][0] += c
            acc[b][1] += o
            acc[b][2] += 1
    series = [dict(ts=int(since + i * bw + bw // 2),
                   total=round(a[0] / a[2]), own=round(a[1] / a[2]))
              for i, a in enumerate(acc) if a[2]]
    return dict(since=since, binSec=bw, series=series,
                nowTotal=(rows[-1][1] if rows else None),
                nowOwn=(rows[-1][2] if rows else None))


def stats():
    """Сводка для здоровья/диагностики: объём БД, охват по времени."""
    with _lock:
        c = _db()
        nn = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM node_hist").fetchone()
        nl = c.execute("SELECT COUNT(*) FROM link_hist").fetchone()
    return dict(node_rows=nn[0], link_rows=nl[0], first=nn[1], last=nn[2])
