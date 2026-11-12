# -*- coding: utf-8 -*-
"""Авто-переякорение номерных рядов: WB «Квитанция №» и МТС «Код транзакции».

Зачем: скорость счётчиков банка дрейфует в течение суток, а жёстко
прошитые якоря в wb_generator._RCPT_ANCHORS / mts_generator._TR_REFS
устаревают за часы. Этот модуль даёт:

1. parse_pdf()           — распознать присланный оригинал (PyMuPDF):
                           WB -> (CreationDate -> MSK, № квитанции),
                           МТС -> (дата операции (UTC+3), код транзакции).
2. load/append_anchor()  — якоря хранятся прямо в used-файлах
                           (wb_used_no.txt / mts_used_tr.txt) помеченными
                           строками "A YYYY-MM-DD HH:MM:SS NUM # note",
                           история append-only, старые числовые строки
                           не трогаются; старые загрузчики (isdigit-фильтр)
                           такие строки просто игнорируют.
3. estimate_rate()       — скорость ряда как медиана наклонов сегментов
                           по последним N якорям (а не одна константа).

Генераторы вызывают эти функции мягко (try/except на импорте), поэтому
каждый генератор остаётся работоспособным и без этого модуля.
"""
import re
from datetime import datetime, timedelta

# ─────────────────────────── хранение якорей ───────────────────────────
# Формат строки в used-файле: "A 2026-11-07 01:48:15 1022055045 # uid=... src=bot"
_ANCHOR_LINE = re.compile(
    r"^A\s+(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})\s+(\d{1,13})\b")


def load_anchors(path: str) -> list:
    """Прочитать сохранённые якоря из used-файла.

    Возвращает [(dt_msk_naive, num), ...] в порядке файла. Числовые
    строки (использованные номера) пропускаются.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                m = _ANCHOR_LINE.match(ln.strip())
                if not m:
                    continue
                y, mo, d, h, mi, s, num = m.groups()
                try:
                    out.append((datetime(int(y), int(mo), int(d),
                                         int(h), int(mi), int(s)), int(num)))
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def append_anchor(path: str, dt: datetime, num: int, note: str = "") -> bool:
    """Дописать якорь в used-файл (история не удаляется, только append)."""
    try:
        line = f"A {dt:%Y-%m-%d %H:%M:%S} {int(num)}"
        note = " ".join(str(note or "").split())
        if note:
            line += f" # {note}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


# ─────────────────────────── оценка скорости ───────────────────────────
def median(vals) -> float:
    v = sorted(vals)
    n = len(v)
    if n % 2:
        return float(v[n // 2])
    return 0.5 * (v[n // 2 - 1] + v[n // 2])


def estimate_rate(anchors, window: int = 4, min_span_s: float = 600.0,
                  lo: float = None, hi: float = None, default: float = None):
    """Скорость ряда по последним `window` якорям: медиана наклонов
    сегментов между соседними якорями.

    anchors  — [(dt, num), ...], отсортированные по времени.
    Сегменты короче min_span_s (микро-сегменты шумные) и сегменты с
    неположительным наклоном отбрасываются. Медиана устойчива к одному
    плохому якорю. Результат клампится в [lo, hi]; если годных сегментов
    нет — возвращается default.
    """
    pts = list(anchors)[-max(2, int(window)):]
    rates = []
    for (t0, n0), (t1, n1) in zip(pts, pts[1:]):
        ds = (t1 - t0).total_seconds()
        if ds < min_span_s:
            continue
        r = (n1 - n0) / ds
        if r > 0:
            rates.append(r)
    if not rates:
        return default
    r = median(rates)
    if lo is not None:
        r = max(lo, r)
    if hi is not None:
        r = min(hi, r)
    return r


# ─────────────────────────── парсинг PDF ───────────────────────────
_PDF_DATE = re.compile(
    r"(?:D:?)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(Z|[+-]\d{2}'?\d{0,2}'?|)")


def pdf_date_to_msk(s: str):
    """'D:20261106224815Z' / 'D:20261107014815+03'00'' -> naive MSK datetime."""
    m = _PDF_DATE.match((s or "").strip())
    if not m:
        return None
    try:
        base = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    tzs = m.group(7) or "Z"
    if tzs.startswith("Z"):
        utc = base
    else:
        mm = re.match(r"([+-])(\d{2})'?(\d{0,2})", tzs)
        if not mm:
            utc = base  # неизвестная зона — считаем UTC
        else:
            off = timedelta(hours=int(mm.group(2)),
                            minutes=int(mm.group(3) or 0))
            if mm.group(1) == "-":
                off = -off
            utc = base - off
    return utc + timedelta(hours=3)  # MSK naive, как ждут генераторы


def _creation_date_msk(doc, pdf_bytes: bytes):
    """CreationDate PDF (время печати/скачивания) -> naive MSK."""
    try:
        raw = (doc.metadata or {}).get("creationDate") or ""
    except Exception:
        raw = ""
    dt = pdf_date_to_msk(raw)
    if dt is not None:
        return dt
    m = re.search(rb"/CreationDate\s*\(?(D:\d{14}[^)\s]*)\)?", pdf_bytes)
    if m:
        return pdf_date_to_msk(m.group(1).decode("latin-1", "replace"))
    return None


def parse_pdf(pdf_bytes: bytes) -> dict:
    """Распознать оригинал чека и вытащить якорь.

    Возвращает:
      WB : {"kind": "wb",  "dt": datetime MSK (время печати), "num": int}
      МТС: {"kind": "mts", "dt": datetime MSK (время операции), "num": int,
            "tr": str}
    Бросает ValueError с понятной причиной, если не распознано.
    """
    import fitz  # PyMuPDF; импорт ленивый, чтобы генераторы не зависели от него
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"это не PDF или файл битый ({e})")
    try:
        if doc.page_count < 1:
            raise ValueError("в PDF нет страниц")
        text = doc[0].get_text() or ""
        # WB: номер выделяется в момент печати (CreationDate), не в момент
        # операции — проверено на genuine-серии 04.11 (slope 1.6998/s).
        m = re.search(r"Квитанция\s*№\s*(\d{7,12})", text)
        if m:
            dt = _creation_date_msk(doc, pdf_bytes)
            if dt is None:
                raise ValueError("не нашёл /CreationDate (нужно время печати)")
            return {"kind": "wb", "dt": dt, "num": int(m.group(1))}
        # МТС: код транзакции выделяется в момент операции — берём время,
        # напечатанное на чеке ('дд.мм.гггг чч:мм:сс (UTC+3)').
        m = re.search(r"Код\s+транзакции[\s:：]*(\d{12})", text)
        if m:
            d = re.search(
                r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})"
                r"\s*\(UTC\s*\+\s*3\)", text)
            if not d:
                raise ValueError("не нашёл дату операции «(UTC+3)» в тексте чека")
            dt = datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)),
                          int(d.group(4)), int(d.group(5)), int(d.group(6)))
            return {"kind": "mts", "dt": dt, "num": int(m.group(1)),
                    "tr": m.group(1)}
        raise ValueError("не похоже ни на чек ВБ («Квитанция №»), "
                         "ни на чек МТС («Код транзакции»)")
    finally:
        try:
            doc.close()
        except Exception:
            pass
