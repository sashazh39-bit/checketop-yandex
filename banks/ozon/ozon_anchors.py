# -*- coding: utf-8 -*-
"""Авто-переякорение Ozon Bank SBP op_id ↔ время + lag ID↔CreationDate.

Сосед anchor_lib.py (WB/МТС): тот же подход «скользящих окон», но под
32-символьный op_id Ozon.

Зачем: распределения узлов/шлюзов/флагов SBP ID и окно
lag CreationDate↔ID дрейфуют с развёртыванием НСПК. В ozon_generator.py
эти величины прошиты статично (_GW_A_NEW, _NODES_NEW, randint(5,13)
/ randint(5,19)). После flip-окна 06.11.2026 01:30Z B-мир и G-мир
сосуществовали ~30 минут; свежие 06–07.11.2026 оригиналы (4 шт.
G-эры) показывают: prefix=A, flag=0, node∈{1,2,4}, gateway∈{1000,
1004,1008}, lag=5..13с. Для B-эры (04–06.11.2026, ~27 шт.):
prefix∈{A,B}, flag∈{0,1,2}, node∈{1,2,3,4,7,8,9,A,D,E,H,I,J,L,P,S,Z},
gateway (A-строна){1000,1001,1002,1003,1004,1006,1007,1008,1020},
(B-сторона){1009,1010,1011,1012,1013,1014,1016,1017,1018}, lag≤19с.

API (минимальный, аналог anchor_lib):

  is_g_era(dt_utc)                    -> bool
  pick_op_id(dt_utc, rng=None)        -> str (32 символа, соблюдает законы)
  pick_lag(dt_utc, rng=None)          -> int (секунд, ID→CreationDate)
  load_anchors(path=None)             -> list dict
  append_anchor(path, op_id, id_dt_utc, cd_dt_utc=None, note="") -> bool
  add_anchor(op_id, id_dt_utc, cd_dt_utc=None, note="", path=None) -> (bool, str)
  parse_pdf(pdf_bytes)                -> dict  (op_id, id_dt_utc, cd_dt_utc, lag_s)

Файл хранения: ozon_anchors.txt (append-only), строка
  "OZ 2026-11-07T01:00:46Z A6311010046149020G10000011890101 lag=7.851 # note"

Модуль подключается в ozon_generator.py «мягко» (try/except на импорте),
генератор остаётся работоспособным без этого файла (fallback — встроенная
make_op_id со статичными константами).

Строение op_id (32 символа):
  [0]      prefix (A/B)
  [1]      year last digit
  [2:5]    DOY (001..366)
  [5:11]   HHMMSS (UTC)
  [11:14]  ms (3 цифры, не все одинаковые)
  [14]     flag (0/1/2)
  [15]     node (цифра или латинская)
  [16]     '0' (константа)
  [17]     route (B или G — эра)
  [18:22]  gateway (4 цифры)
  [22:32]  suffix (10 цифр: 0011890101 в G-эре, 0011881102 в B-эре)
"""
import os
import re
import random as _rnd
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_ANCHORS_PATH = os.path.join(_DIR, "ozon_anchors.txt")

# ──────────────────── эры и статические законы ────────────────────
# Flip B→G: внутри attested-окна B[01:18..01:47] / G[01:18..] выбираем
# 01:30Z (как и в ozon_generator._ERA_FLIP_UTC).
_ERA_FLIP_UTC = datetime(2026, 11, 6, 1, 30, 0)
_SUF_G = "0011890101"
_SUF_B = "0011881102"

# fallback-распределения (когда якорей < _MIN_ANCHORS той же эры).
# G-эра: 4 genuine якоря, веса совпадают с ozon_generator._GW_A_NEW.
_G_PREFIXES = [("A", 1)]
_G_FLAGS    = [("0", 1)]
_G_NODES    = [("1", 1), ("2", 2), ("4", 1)]
_G_GW       = [("1000", 1), ("1004", 2), ("1008", 1)]

# B-эра: веса — ровные по каталогу, кроме флагов (генератор ставит 70/15/15).
_B_PREFIXES = [("A", 2), ("B", 1)]
_B_FLAGS    = [("0", 70), ("1", 15), ("2", 15)]
_B_NODES_POOL = list("1234789") + list("ADEHIJLPSZ")
_B_NODES    = [(ch, 1) for ch in _B_NODES_POOL]
_B_GW_A_POOL = ("1000", "1001", "1002", "1003", "1004",
                "1006", "1007", "1008", "1020")
_B_GW_B_POOL = ("1009", "1010", "1011", "1012", "1013",
                "1014", "1016", "1017", "1018")
_B_GW_A     = [(g, 1) for g in _B_GW_A_POOL]
_B_GW_B     = [(g, 1) for g in _B_GW_B_POOL]

_G_LAG_MIN, _G_LAG_MAX = 5, 13
_B_LAG_MIN, _B_LAG_MAX = 5, 19

_WINDOW_ANCHORS = 20   # размер «скользящего» окна
_MIN_ANCHORS    = 2    # если якорей меньше — чисто fallback

# ──────────────────── разбор op_id ────────────────────
_OPID_RE = re.compile(
    r"^([AB])(\d)(\d{3})(\d{6})(\d{3})([0-2])([0-9A-Z])(0)([BG])(\d{4})(\d{10})$"
)


def _parse_opid(s):
    """Распарсить op_id в dict или вернуть None."""
    if not isinstance(s, str) or len(s) != 32:
        return None
    m = _OPID_RE.match(s)
    if not m:
        return None
    pfx, yr, doy, hms, ms, flag, node, _zero, route, gw, suf = m.groups()
    try:
        year = 2020 + int(yr)
        doy_i = int(doy)
        h, mi, se = int(hms[0:2]), int(hms[2:4]), int(hms[4:6])
        if not (1 <= doy_i <= 366 and h < 24 and mi < 60 and se < 60):
            return None
        id_dt = datetime(year, 1, 1) + timedelta(
            days=doy_i - 1, hours=h, minutes=mi, seconds=se)
    except (ValueError, OverflowError):
        return None
    return {"pfx": pfx, "yr": yr, "doy": doy_i, "hh": h, "mm": mi, "ss": se,
            "ms": ms, "flag": flag, "node": node, "route": route,
            "gw": gw, "suf": suf, "id_dt_utc": id_dt}


# ──────────────────── эры ────────────────────
def _as_naive_utc(dt):
    if dt is None:
        raise TypeError("dt_utc is None")
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def is_g_era(dt_utc: datetime) -> bool:
    """True, если dt_utc >= flip-момента 2026-11-06 01:30Z."""
    return _as_naive_utc(dt_utc) >= _ERA_FLIP_UTC


# ──────────────────── хранение якорей ────────────────────
# Формат строки:
#   "OZ 2026-11-07T01:00:46Z A6311010046149020G10000011890101 lag=7.851 # note"
_ANCHOR_LINE = re.compile(
    r"^OZ\s+(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z"
    r"\s+(\S{32})(?:\s+lag=(-?\d+(?:\.\d+)?))?"
)

_LOAD_CACHE = {}   # path -> (mtime, anchors list)


def load_anchors(path: str = None) -> list:
    """Прочитать якоря из файла (по умолчанию ozon_anchors.txt).

    Возвращает list dict с полями
      op_id, id_dt_utc, cd_dt_utc, lag_s, fields (parsed opid dict).
    Мусорные/несоответствующие строки игнорируются молча (append-only
    история допускает ручные комментарии)."""
    path = path or _ANCHORS_PATH
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    entry = _LOAD_CACHE.get(path)
    if entry is not None and entry[0] == mt:
        return list(entry[1])
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                m = _ANCHOR_LINE.match(ln.strip())
                if not m:
                    continue
                y, mo, d, h, mi, s, opid, lag = m.groups()
                try:
                    id_dt = datetime(int(y), int(mo), int(d),
                                     int(h), int(mi), int(s))
                except ValueError:
                    continue
                fields = _parse_opid(opid)
                if fields is None:
                    continue
                lag_s = float(lag) if lag is not None else None
                cd_dt = (id_dt + timedelta(seconds=lag_s)
                         if lag_s is not None else None)
                out.append({"op_id": opid, "id_dt_utc": id_dt,
                            "cd_dt_utc": cd_dt, "lag_s": lag_s,
                            "fields": fields})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    _LOAD_CACHE[path] = (mt, out)
    return list(out)


def append_anchor(path, op_id, id_dt_utc, cd_dt_utc=None, note=""):
    """Дописать якорь в файл (append-only). True при успехе."""
    try:
        path = path or _ANCHORS_PATH
        id_dt = _as_naive_utc(id_dt_utc)
        if _parse_opid(op_id) is None:
            return False
        lag_s = None
        if cd_dt_utc is not None:
            cd_dt = _as_naive_utc(cd_dt_utc)
            lag_s = (cd_dt - id_dt).total_seconds()
        line = f"OZ {id_dt:%Y-%m-%dT%H:%M:%SZ} {op_id}"
        if lag_s is not None:
            line += f" lag={lag_s:g}"
        note = " ".join(str(note or "").split())
        if note:
            line += f" # {note}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # инвалидация кэша
        _LOAD_CACHE.pop(path, None)
        return True
    except Exception:
        return False


def add_anchor(op_id, id_dt_utc, cd_dt_utc=None, note="", path=None):
    """Публичная проверенная обёртка над append_anchor. (ok, message).

    Валидирует op_id (длина 32, парсится, hh/mm/ss и DOY совпадают с
    id_dt_utc), сверяет route с эрой флипа, отклоняет дубликаты."""
    fields = _parse_opid(op_id)
    if fields is None:
        return False, f"op_id не распознан как Ozon SBP: {op_id!r}"
    try:
        id_dt = _as_naive_utc(id_dt_utc)
    except Exception as e:
        return False, f"id_dt_utc некорректен: {e}"
    if (id_dt.hour, id_dt.minute, id_dt.second) != (
            fields["hh"], fields["mm"], fields["ss"]):
        return False, (f"время внутри id "
                       f"({fields['hh']:02}:{fields['mm']:02}:{fields['ss']:02}) "
                       f"≠ id_dt_utc ({id_dt:%H:%M:%S})")
    if id_dt.timetuple().tm_yday != fields["doy"]:
        return False, (f"DOY внутри id ({fields['doy']}) "
                       f"≠ id_dt_utc ({id_dt.timetuple().tm_yday})")
    # согласованность route↔suffix
    if fields["route"] == "G" and fields["suf"] != _SUF_G:
        return False, f"route=G, но suffix={fields['suf']!r} (ожидался {_SUF_G})"
    if fields["route"] == "B" and fields["suf"] != _SUF_B and id_dt >= datetime(2026, 10, 15):
        # более старые B-suffix (0011870501, 0011870702) встречаются в архиве
        return False, f"route=B, но suffix={fields['suf']!r} (ожидался {_SUF_B})"
    # после flip не должны появляться новые B-якоря (attested overlap
    # закрылся 01:47Z 06.11; после 02:00Z приём B-якорей = ручная ошибка)
    if (id_dt >= datetime(2026, 11, 6, 2, 0, 0)
            and fields["route"] != "G"):
        return False, (f"после 02:00Z 06.11 допустимы только G-якоря; "
                       f"у этого id route={fields['route']}")
    # дубликаты по op_id
    for a in load_anchors(path):
        if a["op_id"] == op_id:
            return False, "такой якорь уже есть"
    ok = append_anchor(path or _ANCHORS_PATH, op_id, id_dt, cd_dt_utc, note)
    return ok, ("якорь добавлен" if ok else "не удалось записать файл")


# ──────────────────── скользящий выбор ────────────────────
def _weighted(choices, rng):
    """Вернуть value из [(value, weight)...] с вероятностями, пропорц. весам."""
    tot = sum(max(0, w) for _, w in choices)
    if tot <= 0:
        return choices[-1][0]
    r = rng.random() * tot
    acc = 0.0
    for v, w in choices:
        if w <= 0:
            continue
        acc += w
        if r < acc:
            return v
    return choices[-1][0]


def _era_anchors(anchors, want_g: bool):
    """Отфильтровать якоря нужной эры (route-признак)."""
    rt = "G" if want_g else "B"
    return [a for a in anchors if a["fields"]["route"] == rt]


def _hist(items):
    d = {}
    for x in items:
        d[x] = d.get(x, 0) + 1
    return sorted(d.items())


def _sliding_weights(anchors, key, fallback,
                     window=_WINDOW_ANCHORS, min_anchors=_MIN_ANCHORS):
    """Эмпирическая гистограмма key по последним `window` якорям.

    При меньшем, чем `min_anchors`, числе якорей возвращается копия
    fallback-распределения (список (value, weight))."""
    if not anchors or len(anchors) < min_anchors:
        return list(fallback)
    sub = sorted(anchors, key=lambda a: a["id_dt_utc"])[-window:]
    h = _hist(a["fields"][key] for a in sub)
    return h or list(fallback)


def pick_op_id(dt_utc: datetime, rng=None, path: str = None) -> str:
    """Собрать 32-символьный Ozon SBP op_id на момент dt_utc (UTC).

    Префикс/флаг/узел/шлюз берутся из скользящего окна последних
    якорей той же эры (если их ≥ _MIN_ANCHORS); иначе — из встроенных
    fallback-распределений. Жёсткие инварианты эры (G: pfx=A, flag=0,
    node∈{1,2,4}, gw∈{1000,1004,1008}) гарантируются и при пустом файле,
    и при нестандартных якорях."""
    r = rng or _rnd
    utc = _as_naive_utc(dt_utc)
    g = is_g_era(utc)
    route, suffix = ("G", _SUF_G) if g else ("B", _SUF_B)
    # ms: 3 цифры, не все одинаковые (как в генераторе — see _ozon_lag_law.py)
    while True:
        ms = r.randint(0, 999)
        if len(set("%03d" % ms)) > 1:
            break
    anchors = _era_anchors(load_anchors(path), g)
    if g:
        pfx = _weighted(_sliding_weights(anchors, "pfx", _G_PREFIXES), r)
        flag = _weighted(_sliding_weights(anchors, "flag", _G_FLAGS), r)
        node = _weighted(_sliding_weights(anchors, "node", _G_NODES), r)
        gw = _weighted(_sliding_weights(anchors, "gw", _G_GW), r)
        # жёсткие инварианты G-эры
        if pfx != "A":
            pfx = "A"
        if flag != "0":
            flag = "0"
        if node not in {"1", "2", "4"}:
            node = _weighted(_G_NODES, r)
        if gw not in {"1000", "1004", "1008"}:
            gw = _weighted(_G_GW, r)
    else:
        pfx = _weighted(_sliding_weights(anchors, "pfx", _B_PREFIXES), r)
        if pfx not in ("A", "B"):
            pfx = r.choice("AB")
        flag = _weighted(_sliding_weights(anchors, "flag", _B_FLAGS), r)
        if flag not in ("0", "1", "2"):
            flag = "0"
        node = _weighted(_sliding_weights(anchors, "node", _B_NODES), r)
        if node not in _B_NODES_POOL:
            node = r.choice(_B_NODES_POOL)
        # gateway: веса считаем только по якорям с тем же pfx и только по
        # допустимому пулу (A/B раздельно)
        gw_pool = _B_GW_A_POOL if pfx == "A" else _B_GW_B_POOL
        pool_set = set(gw_pool)
        fallback_pool = _B_GW_A if pfx == "A" else _B_GW_B
        gw_hist = _sliding_weights(
            [a for a in anchors if a["fields"]["pfx"] == pfx],
            "gw", fallback_pool)
        gw_hist = [(g_, w) for g_, w in gw_hist if g_ in pool_set] or list(fallback_pool)
        gw = _weighted(gw_hist, r)
        if gw not in pool_set:
            gw = r.choice(gw_pool)
    return (pfx + str(utc.year)[-1] + "%03d" % utc.timetuple().tm_yday
            + "%02d%02d%02d%03d" % (utc.hour, utc.minute, utc.second, ms)
            + flag + node + "0" + route + gw + suffix)


def pick_lag(dt_utc: datetime, rng=None, path: str = None) -> int:
    """Секунд между ID-временем и CreationDate.

    G-эра 5..13, B-эра 5..19. При наличии ≥3 якорей той же эры с
    измеренным lag диапазон сужается до наблюдаемого [floor(min),
    ceil(max)] — но не выходит за встроенные скобки эры."""
    r = rng or _rnd
    utc = _as_naive_utc(dt_utc)
    g = is_g_era(utc)
    lo, hi = (_G_LAG_MIN, _G_LAG_MAX) if g else (_B_LAG_MIN, _B_LAG_MAX)
    era = _era_anchors(load_anchors(path), g)
    # отсекаем явные выбросы (парсерные артефакты >19с в B-эре редко, но бывают)
    lags = [a["lag_s"] for a in era
            if a["lag_s"] is not None and 0.5 <= a["lag_s"] <= hi + 10]
    if len(lags) >= 3:
        lags.sort()
        import math
        lo_emp = max(1, int(math.floor(lags[0])))
        hi_emp = max(lo_emp, int(math.ceil(lags[-1])))
        lo = max(lo, lo_emp)
        hi = min(hi, hi_emp)
        if lo > hi:
            lo = _G_LAG_MIN if g else _B_LAG_MIN
            hi = _G_LAG_MAX if g else _B_LAG_MAX
    return r.randint(lo, hi)


# ──────────────────── распознавание PDF ────────────────────
_PDF_DATE = re.compile(
    r"(?:D:?)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})"
    r"(Z|[+-]\d{2}'?\d{0,2}'?|)"
)


def _pdf_date_to_utc(s):
    m = _PDF_DATE.match((s or "").strip())
    if not m:
        return None
    try:
        base = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    tzs = m.group(7) or "Z"
    if tzs.startswith("Z"):
        return base
    mm = re.match(r"([+-])(\d{2})'?(\d{0,2})", tzs)
    if not mm:
        return base
    off = timedelta(hours=int(mm.group(2)), minutes=int(mm.group(3) or 0))
    if mm.group(1) == "-":
        off = -off
    return base - off


_IDRE_PDF = re.compile(r"([AB]\d{13}\d[0-9A-Z]0[BG]\d{14})")


def parse_pdf(pdf_bytes: bytes) -> dict:
    """Извлечь из genuine Ozon-чека: op_id, id_dt_utc, cd_dt_utc, lag_s.

    Бросает ValueError, если не похоже на Ozon SBP чек."""
    import fitz  # ленивый импорт
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"не PDF или битый файл ({e})")
    try:
        if doc.page_count < 1:
            raise ValueError("в PDF нет страниц")
        text = doc[0].get_text() or ""
        m = _IDRE_PDF.search(text)
        if not m:
            raise ValueError("не нашёл Ozon SBP op_id в тексте")
        op_id = m.group(1)
        fields = _parse_opid(op_id)
        if fields is None:
            raise ValueError(f"распознан, но не парсится: {op_id!r}")
        id_dt_utc = fields["id_dt_utc"]
        raw = (doc.metadata or {}).get("creationDate") or ""
        cd = _pdf_date_to_utc(raw)
        if cd is None:
            m2 = re.search(
                rb"/CreationDate\s*\(?(D:\d{14}[^)\s]*)\)?", pdf_bytes)
            if m2:
                cd = _pdf_date_to_utc(m2.group(1).decode("latin-1", "replace"))
        lag_s = (cd - id_dt_utc).total_seconds() if cd else None
        return {"op_id": op_id, "id_dt_utc": id_dt_utc, "cd_dt_utc": cd,
                "lag_s": lag_s, "fields": fields}
    finally:
        try:
            doc.close()
        except Exception:
            pass
