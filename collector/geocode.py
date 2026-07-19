"""Геокодинг адресных имён нод (Фаза 6-В, пул мягких якорей). Питерский меш сам
подписывается адресами (Pulkovskoe 65, Zhukovskogo 7-9) — превращаем имя в
координаты через Nominatim (OSM). Кэш на файл (адреса статичны), rate-limit
1 запрос/сек. Доверие даёт ВЫЗЫВАЮЩИЙ (сверка с GPS/сигналом) — сырой геокод
может ошибаться на неоднозначной улице. Чистый stdlib."""
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

NOMINATIM = "https://nominatim.openstreetmap.org/search"
CITY = "Санкт-Петербург, Россия"
_last_req = [0.0]

# Обратная транслитерация Latin→Cyrillic: Nominatim плохо ищет питерские улицы
# латиницей («Srednerogatskaya» не находит, «Среднерогатская» находит в 110 м от
# реального GPS). Жадно, длинные сочетания первыми. Неоднозначно (translit
# many-to-one), но для нечёткого поиска Nominatim хватает; сверка вызывающим.
# «ts→ц» намеренно НЕ включён: в питерских улицах «тс» (окончания -тская)
# частотнее «ц», а «ts→ц» ломало «Srednerogatskaya»→«среднерогацкайа».
_TR = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"),
       ("ch", "ч"), ("sh", "ш"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"),
       ("ye", "е"), ("iy", "ий"), ("yy", "ый"), ("j", "ж"), ("x", "кс"),
       ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"),
       ("z", "з"), ("i", "и"), ("y", "й"), ("k", "к"), ("l", "л"), ("m", "м"),
       ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
       ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "к"), ("w", "в"), ("q", "к")]


def translit_ru(s):
    """Латиница → кириллица (жадно). Кириллицу/цифры/дефис оставляем как есть."""
    out, i, low = [], 0, s.lower()
    while i < len(s):
        if not ("a" <= low[i] <= "z"):
            out.append(s[i]); i += 1; continue
        for lat, cyr in _TR:
            if low.startswith(lat, i):
                out.append(cyr); i += len(lat); break
        else:
            out.append(s[i]); i += 1
    return "".join(out)

# мусор из имён: тип железа, роль, скобки, суффиксы — всё, что не адрес
_JUNK = re.compile(
    r"\[[^\]]*\]|\b(base|node|home|bot|solar|static|mobile|roam|gateway|rep|repeater|"
    r"heltec|esp32\w*|promicro|tbeam|rak\w*|t-?echo|t-?deck|cardputer|portduino|"
    r"custom|diy|ultimate|dinamic|ximv\d|west|east|north|south|"
    r"mf|lf|ham|prim|kolp|nakenaked)\b", re.I)


def normalize(name):
    """Имя ноды → строка-адрес или None, если на адрес не похоже. Возвращает
    (query, is_place): is_place=True для посёлков (номер = не дом, отбросить)."""
    place = re.match(r"^\s*(Мартышкино|Ломоносов|Ораниенбаум|Кронштадт|Сестрорецк|"
                     r"Петергоф|Пушкин|Колпино|Шушары)\b", name, re.I)
    s = _JUNK.sub(" ", name)
    s = re.sub(r"[_/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # вставить пробел между буквами и приклеенными цифрами: svetlanovskiy105
    s = re.sub(r"([А-Яа-яA-Za-z])(\d)", r"\1 \2", s)
    # диапазон домов «7-9» → «7»; корпус «7k1»/«7к1» оставить как «7к1»
    s = re.sub(r"(\d+)\s*-\s*\d+", r"\1", s)
    s = re.sub(r"(\d+)\s*[kк]\s*(\d)", r"\1к\2", s)
    s = re.sub(r"[.,]0\b", "", s)                     # «2.0» → «2»
    if not re.search(r"[А-Яа-яA-Za-z]{4,}", s):
        return None
    if place:
        # посёлок: только название, номер = индекс ноды, не дом
        return place.group(1), True
    if not re.search(r"\d", s):
        return None
    return s, False


def geocode(name, cache_path, verbose=False):
    """Имя → {lat,lon,display,q} или None. Кэш по нормализованному запросу."""
    norm = normalize(name)
    if not norm:
        return None
    q, is_place = norm
    cache = {}
    p = Path(cache_path)
    if p.exists():
        try:
            cache = json.loads(p.read_text())
        except Exception:
            cache = {}
    if q in cache:
        return cache[q] or None
    # латинские имена улиц → кириллица (Nominatim их так находит)
    qc = translit_ru(q) if re.search(r"[A-Za-z]", q) else q
    query = f"{qc}, {CITY}"
    dt = time.time() - _last_req[0]
    if dt < 1.1:                                       # rate-limit Nominatim 1/сек
        time.sleep(1.1 - dt)
    url = NOMINATIM + "?" + urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0})
    res = None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "meshtastic-zoo/1.0 (mesh node geocoding; contact via github anton-vinogradov)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            arr = json.load(r)
        _last_req[0] = time.time()
        if arr:
            it = arr[0]
            res = {"lat": float(it["lat"]), "lon": float(it["lon"]),
                   "display": it.get("display_name", "")[:80], "q": qc,
                   "place": is_place}
    except Exception as e:
        if verbose:
            print("geocode error", name, repr(e))
        return None
    cache[q] = res
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return res


def _hav_km(a, b):
    r = math.radians
    dla, dlo = r(b[0] - a[0]), r(b[1] - a[1])
    h = math.sin(dla / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(dlo / 2) ** 2
    return 2 * 6371 * math.asin(min(1, math.sqrt(h)))
