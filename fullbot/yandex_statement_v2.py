# -*- coding: utf-8 -*-
"""Генератор выписок Яндекс Банка v2: несколько операций, пагинация 1/2/3+ стр.

Реализация спроектирована по genuine-корпусу из 114 PDF (OpenPDF 1.3.32 /
JasperReports 6.21.2), из них 9 — выписки по договору «Счёт ЭДС»:
6 одностраничных, 2 двухстраничные, 1 трёхстраничная
(см. `_yandex_corpus.json`). Этот модуль НЕ редактирует и не зависит от
`yandex_generator.py` / `yandex_gen_stable.py`.

ЧЕСТНОЕ РАЗДЕЛЕНИЕ РЕЖИМОВ
==========================
Byte-faithful воспроизведение Jasper-вывода недостижимо: нет JasperReports,
нет полных (не subset) шрифтов YSText, нет исходной подписи-скана и неизвестен
случайный вход PDF /ID. Поэтому режимы разделены явно:

1. ``mode="donor-template"`` (функция :func:`generate_from_donor`) —
   байт-в-байт копия genuine-донора, в которой меняются ТОЛЬКО
   `/CreationDate` в Info и обе половины trailer `/ID` (32 lower-hex,
   ID[0]==ID[1], как в 114/114 корпуса). Размер файла, текст, число страниц,
   шрифты, потоки и рендер попиксельно идентичны донору. ЦЕНА: нельзя менять
   операции, суммы, ФИО и даты внутри документа — донор отдаётся как есть.

2. ``mode="synthetic"`` (функция :func:`generate_statement`) — PDF строится
   с нуля через PyMuPDF по геометрии, измеренной на genuine-донорах
   (координаты, шаг строк, правила переноса итогов). Поддерживает любое число
   операций и пагинацию, но НЕ является byte-faithful: шрифт — Arial/DejaVu
   вместо subset-YSText, подпись-скан отсутствует (опционально вставляется
   картинка из донора), producer/creator выставлены как у корпуса, однако
   реальный движок — PyMuPDF, что честно отражено в meta_out
   (``byte_faithful=False``, ``engine="PyMuPDF"``).

ЗАКОН ПАГИНАЦИИ (измерено на корпусе; экстраполяции помечены)
=============================================================
* Страница 595x842. Колонки таблицы (x): описание 20-190, дата/время 190-260,
  обработка 260-330, карта 330-380, сумма операции ..475, сумма ЭСП ..570.
* Первая страница: заголовок таблицы y=208.42, первая операция y=246.78.
  Продолжения с операциями: заголовок повторяется, y=24.42, операции с y=62.78.
* Высота банды операции = max(36, 12*L+12), где L — число строк описания
  (3 строки -> 48 пт, 2 строки -> 36 пт; обе точки измерены на донорах).
  Нижняя граница контента CONTENT_BOTTOM=670 пт: банда, не влезающая целиком,
  уходит на следующую страницу. Отсюда при 3-строчных описаниях:
  1-я страница — до 9 операций (9-я 2-строчная в genuine b34c... влезает,
  3-строчная уже нет), продолжения — до 12.
* «Исходящий остаток» ставится через 9.92 пт под последней бандой операции;
  «Всего расходных/приходных» — +28/+56 пт от него, блоком: если блок не
  влезает, он ЦЕЛИКОМ уходит на следующую страницу по y=29.92/57.92 без
  заголовка таблицы (так в обоих 2-страничных донорах и в 3-страничном).
  [Экстраполяция] если не влезает сам «Исходящий остаток», он уходит на
  новую страницу по y=29.92, а итоги следуют за ним там же.
* 0 операций: заголовка таблицы нет, «Исходящий остаток» на y=213.92,
  итоги на 241.92/269.92 (genuine 7abdb...).
* Подпись («С уважением,» ... адрес банка) — только на последней странице,
  на фиксированных y=720.78/776.70. На не-последних — «Продолжение на
  следующей странице» (y=773.42) и «Страница X из Y» (y=803.42) на всех.

ОГРАНИЧЕНИЯ СИНТЕТИКИ
=====================
* Перенос описания — greedy по словам/дефисам метриками Arial/DejaVu;
  Jasper с YSText может переносить в других местах (число строк L, а значит
  и граница страницы при пограничных длинах, может отличаться на 1 операцию).
* Группировка тысяч в суммах — неразрывный пробел (в корпусе сумм >= 1000
  в выписках нет, формат Jasper для них не наблюдаем).
* Колонка «Карта» в корпусе всегда пуста; значение card рисуется слева
  в колонке 330-380 — [экстраполяция], в корпусе не встречается.
"""
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import fitz

__all__ = [
    "Operation", "PagePlan", "DonorTemplateError",
    "generate_statement", "generate_from_donor", "generate",
    "make_sbp_description", "paginate", "wrap_description",
    "fmt_signed", "fmt_balance", "fmt_plain", "check_text",
    "find_donor", "GENUINE_PRODUCER", "GENUINE_CREATOR",
]

# ---------------------------------------------------------------- constants
PAGE_W, PAGE_H = 595.0, 842.0
MSK = timezone(timedelta(hours=3))
GENUINE_PRODUCER = "OpenPDF 1.3.32"
GENUINE_CREATOR = ("JasperReports Library version 6.21.2-"
                   "8434a0bd7c3bbc37cbf916f2968d35e4b165821a")

COL_DESC_X, COL_DESC_W = 20.0, 170.0          # 20..190
COL_DT_RIGHT = 260.0                          # дата/время операции
COL_PROC_RIGHT = 330.0                        # дата обработки
COL_CARD_X = 335.0                            # карта (в корпусе пусто)
COL_SUM_RIGHT = 475.0                         # сумма в валюте операции
COL_ESP_RIGHT = 570.0                         # сумма в валюте ЭСП / правый край
RIGHT_EDGE = 570.0

HDR_TEXT_Y1 = 208.42      # заголовок таблицы, 1-я страница
HDR_BAND_TOP1, HDR_BAND_BOT1 = 204.0, 239.0
FIRST_OP_Y1 = 246.78
HDR_TEXT_YC = 24.42       # заголовок таблицы, продолжение
HDR_BAND_TOPC, HDR_BAND_BOTC = 20.0, 55.0
FIRST_OP_YC = 62.78
CONTENT_BOTTOM = 670.0    # нижняя граница банд/итогов (измерено: 659 влезает, 679 нет)
OUTGOING_GAP = 9.92       # от нижней линии последней операции до «Исходящий остаток»
TOTALS_STEP = 28.0
SPILL_TOP_Y = 29.92       # y «Всего расходных» на вынесенной странице итогов

CLIENT_DEFAULTS = dict(
    client_name="Хабибуллин Ильяс Ильнурович",
    client_birth="30.04.2008",
    client_passport="9222 096868",
    contract_no="Э20260929767871183",
    contract_date="29.09.2026",
)
_BANK_INTRO = (
    "АО «Яндекс Банк», лицензия Банка России № 3027 (далее — «Банк»),",
    "сообщает, что между Вами и Банком заключён договор",
    None,  # № договора ... в рамках продукта — собирается из параметров
    "«Счёт ЭДС» (далее — «Договор»).",
)
_SIGNATORY = ("С уважением,", "Начальник отдела сопровождения операций по",
              "платёжным картам и сервисам", "T.C. Чиличихина")
_BANK_ADDR = ("АО «Яндекс Банк», ул. Садовническая, д. 82 стр. 2, "
              "Москва, Россия, 115035, Тел.: +7 495 787-37-96, "
              "yabank.yandex.ru, welcome@bank.yandex.ru")
_TABLE_HEADER = (  # (текст, x-левый или None, правый край или None, y-сдвиг)
    ("Описание операции", COL_DESC_X, None, 0.0),
    ("Дата и время", None, COL_DT_RIGHT, 0.0),
    ("операции", None, COL_DT_RIGHT, 9.38),
    ("МСК", None, COL_DT_RIGHT, 18.76),
    ("Дата", None, COL_PROC_RIGHT, 0.0),
    ("обработки", None, COL_PROC_RIGHT, 9.38),
    ("МСК", None, COL_PROC_RIGHT, 18.76),
    ("Карта", 357.84, None, 0.0),
    ("Сумма в валюте", None, COL_SUM_RIGHT, 0.0),
    ("операции", None, COL_SUM_RIGHT, 9.38),
    ("Сумма в валюте", None, COL_ESP_RIGHT, 0.0),
    ("ЭСП", None, COL_ESP_RIGHT, 9.38),
)

# ---------------------------------------------------------------- fonts
_FONT_CANDIDATES = {
    "regular": ("YANDEX_V2_FONT_REGULAR",
                r"C:\Windows\Fonts\arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    "medium": ("YANDEX_V2_FONT_MEDIUM",
               r"C:\Windows\Fonts\arialbd.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
}
_FONT_PATHS, _FONTS = {}, {}


def _font_path(kind):
    if kind not in _FONT_PATHS:
        env, *paths = _FONT_CANDIDATES[kind]
        found = os.environ.get(env) or next((p for p in paths if os.path.exists(p)), None)
        if not found:
            raise RuntimeError(
                f"Не найден TTF для '{kind}': нужен Arial/DejaVu/Liberation "
                f"(или переменная окружения {env}). Base-14 шрифты не годятся: "
                "у них нет кириллицы.")
        _FONT_PATHS[kind] = found
    return _FONT_PATHS[kind]


def _font(kind):
    if kind not in _FONTS:
        _FONTS[kind] = fitz.Font(fontfile=_font_path(kind))
    return _FONTS[kind]


def check_text(text):
    """Символы `text`, отсутствующие в шрифте синтетического режима."""
    font = _font("regular")
    return sorted({c for c in (text or "") if not font.has_glyph(ord(c))})


# ---------------------------------------------------------------- money/dates
def _dec(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_money(value, sign_mode):
    v = _dec(value)
    q = abs(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = f"{int(q):,}".replace(",", "\u00A0")
    body = f"{whole},{int((q - int(q)) * 100):02d} ₽"
    if sign_mode == "signed":
        if v > 0:
            return f"+{body}"
        if v < 0:
            return f"–{body}"
        return body
    return f"–{body}" if v < 0 else body


def fmt_signed(value):
    """Сумма операции/итога: +11,00 ₽ / –10,00 ₽ / 0,00 ₽ (как в корпусе)."""
    return _fmt_money(value, "signed")


def fmt_balance(value):
    """Остаток без знака плюс: 109,00 ₽."""
    return _fmt_money(value, "balance")


fmt_plain = fmt_balance


def _fmt_date(d):
    return d.strftime("%d.%m.%Y") if isinstance(d, (date, datetime)) else str(d)


def _creation_str(creation_dt):
    if creation_dt is None:
        creation_dt = datetime.now(timezone.utc)
    if creation_dt.tzinfo is None:
        creation_dt = creation_dt.replace(tzinfo=timezone.utc)
    return creation_dt.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")


def _make_doc_id(creation_str, isx_no, fixed=None):
    if fixed is not None:
        if not re.fullmatch(r"[0-9a-f]{32}", fixed):
            raise ValueError("doc_id должен быть 32 lower-hex символа")
        return fixed
    seed = f"{creation_str}|{isx_no}|{secrets.token_hex(8)}".encode()
    return hashlib.md5(seed).hexdigest()  # закон корпуса: ID[0]==ID[1], MD5-формат


_ID_RE = re.compile(rb"/ID\s*\[<([0-9a-fA-F]{32})><([0-9a-fA-F]{32})>\]")
_CREATION_RE = re.compile(rb"(/CreationDate\s*\(D:)\d{14}(Z\))")


def _force_id(pdf_bytes, doc_id):
    """Записать doc_id в обе половины trailer /ID (длина не меняется)."""
    enc = doc_id.encode()
    out, n = _ID_RE.subn(b"/ID [<" + enc + b"><" + enc + b">]", pdf_bytes)
    return out, n > 0


# ---------------------------------------------------------------- model
@dataclass
class Operation:
    """Одна операция выписки. amount: Decimal/float/str, знак = направление
    (+ приход, - расход). description — готовый текст описания; если пуст,
    собирается через :func:`make_sbp_description` из sender/bank."""
    dt: datetime
    amount: object = Decimal("0")
    description: str = ""
    sender: str = ""
    phone: str = ""
    bank: str = ""
    outgoing: bool = False
    processing_date: date = None
    card: str = ""

    def __post_init__(self):
        if isinstance(self.dt, str):
            self.dt = datetime.fromisoformat(self.dt)
        if self.dt.tzinfo is None:
            self.dt = self.dt.replace(tzinfo=MSK)
        self.amount = _dec(self.amount)
        if not self.description:
            self.description = make_sbp_description(
                self.sender, self.phone, self.bank, self.outgoing)
        if self.processing_date is None:
            self.processing_date = self.dt.date()

    @classmethod
    def from_dict(cls, d):
        return cls(**d) if not isinstance(d, cls) else d


def _fmt_phone(phone):
    digs = "".join(c for c in (phone or "") if c.isdigit())
    if len(digs) == 11 and digs[0] in "78":
        digs = digs[1:]
    if len(digs) != 10:
        return phone or ""
    return f"+7 {digs[0:3]} {digs[3:6]}-{digs[6:8]}-{digs[8:10]}"


def make_sbp_description(sender, phone, bank, outgoing=False):
    """«Входящий/Исходящий перевод СБП, Имя, +7 ..., Банк» — как в корпусе."""
    kind = "Исходящий перевод СБП" if outgoing else "Входящий перевод СБП"
    parts = [kind, (sender or "").strip(), _fmt_phone(phone), (bank or "").strip()]
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- wrapping
def _split_units(text):
    """Единицы переноса: слова, разрезанные также сразу после дефиса
    (Jasper в корпусе рвёт «Турпал-али», «Т-Банк», «345-11-49» по дефису)."""
    units = []
    for word in (text or "").split():
        chunks = word.split("-")
        for i, ch in enumerate(chunks):
            if i < len(chunks) - 1:
                units.append((ch + "-", False))   # дальше без пробела
            elif ch:
                units.append((ch, True))          # дальше пробел допустим
    return units


def wrap_description(text, width=COL_DESC_W, size=10.0, font=None):
    """Greedy-перенос описания по ширине колонки метриками реального шрифта."""
    font = font or _font("regular")
    lines, cur = [], ""
    for unit, space_after in _split_units(text):
        cand = unit if not cur else cur + ("" if cur.endswith("-") else " ") + unit
        if font.text_length(cand, fontsize=size) > width and cur:
            # единица не влезает в текущую строку — переносим её целиком
            # (прежний while здесь зацикливался навсегда, если сама
            # единица шире колонки: состояние cur/cand не менялось)
            lines.append(cur)
            cur, cand = "", unit
        if font.text_length(cand, fontsize=size) > width:
            # одно длинное слово без дефисов шире колонки — рвём по символам
            rest = cand
            while font.text_length(rest, fontsize=size) > width:
                k = len(rest)
                while k > 1 and font.text_length(rest[:k], fontsize=size) > width:
                    k -= 1
                lines.append(rest[:k])
                rest = rest[k:]
            cur = rest
        else:
            cur = cand
        _ = space_after
    if cur:
        lines.append(cur)
    return lines or [""]


def _band_height(n_lines):
    return max(36.0, 12.0 * n_lines + 12.0)


@dataclass
class PagePlan:
    ops: list = field(default_factory=list)   # [(Operation, [строки описания])]
    outgoing_here: bool = False
    totals_here: bool = False
    outgoing_y: float = None
    totals_y: tuple = None                    # (расход_y, приход_y)


def paginate(wrapped_ops):
    """Разложить [(Operation, строки)] по страницам по закону корпуса.

    Возвращает list[PagePlan]. Подпись/«Продолжение» не планируются — они
    зависят только от is_last и рисуются в рендере."""
    pages, cur, band_top = [], [], HDR_BAND_BOT1
    for op, lines in wrapped_ops:
        h = _band_height(len(lines))
        if cur and band_top + h > CONTENT_BOTTOM:
            pages.append(PagePlan(ops=cur))
            cur, band_top = [], HDR_BAND_BOTC
        cur.append((op, lines))
        band_top += h
    if cur or not pages:
        pages.append(PagePlan(ops=cur))
    # исходящий остаток: на странице последних операций, если влезает
    last = pages[-1]
    if last.ops:
        top = HDR_BAND_BOT1 if len(pages) == 1 else HDR_BAND_BOTC
        for _, lines in last.ops:
            top += _band_height(len(lines))
        out_y = top + OUTGOING_GAP
    else:
        out_y = 185.92 + TOTALS_STEP  # 0 операций: genuine 213.92
    if last.ops and out_y + 12.0 > CONTENT_BOTTOM:
        pages.append(PagePlan(outgoing_here=True, outgoing_y=SPILL_TOP_Y))
    else:
        last.outgoing_here, last.outgoing_y = True, out_y
    # итоги: блоком после исходящего, иначе целиком на новую страницу
    host = pages[-1]
    exp_y = host.outgoing_y + TOTALS_STEP
    if exp_y + TOTALS_STEP + 12.0 > CONTENT_BOTTOM:
        pages.append(PagePlan(totals_here=True,
                              totals_y=(SPILL_TOP_Y, SPILL_TOP_Y + TOTALS_STEP)))
    else:
        host.totals_here, host.totals_y = True, (exp_y, exp_y + TOTALS_STEP)
    return pages


# ---------------------------------------------------------------- synthetic render
class _Canvas:
    def __init__(self, page):
        self.page = page
        self._files = {"regular": _font_path("regular"), "medium": _font_path("medium")}
        self._names = {"regular": "Y2R", "medium": "Y2M", "logo": "Y2M"}

    def text(self, x_right_or_left, y_top, s, size, kind="regular", right=False,
             center=None):
        font = _font("medium" if kind in ("medium", "logo") else "regular")
        w = font.text_length(s, fontsize=size)
        x = x_right_or_left - w if right else x_right_or_left
        if center is not None:
            x = center - w / 2.0
        baseline_top = y_top + 0.85 * size
        # Координаты PyMuPDF — с началом в левом верхнем углу, y вниз:
        # точка вставки = базовая линия в той же системе.
        self.page.insert_text(
            fitz.Point(x, baseline_top), s, fontsize=size,
            fontname="Y2M" if kind in ("medium", "logo") else "Y2R",
            fontfile=self._files["medium" if kind in ("medium", "logo") else "regular"],
            color=(0, 0, 0))

    def line(self, y_top, color=(0.749, 0.749, 0.749), width=0.25):
        self.page.draw_line(fitz.Point(COL_DESC_X, y_top),
                            fitz.Point(RIGHT_EDGE, y_top),
                            color=color, width=width)


def _draw_header_block(cv, first_page, p):
    y_text = HDR_TEXT_Y1 if first_page else HDR_TEXT_YC
    band_top, band_bot = (HDR_BAND_TOP1, HDR_BAND_BOT1) if first_page else (HDR_BAND_TOPC, HDR_BAND_BOTC)
    cv.line(band_top, color=(0, 0, 0), width=1.0)
    for txt, x, right, dy in _TABLE_HEADER:
        if right is not None:
            cv.text(right, y_text + dy, txt, 8.0, right=True)
        else:
            cv.text(x, y_text + dy, txt, 8.0)
    cv.line(band_bot)
    return band_bot


def _draw_op(cv, op, lines, band_top):
    y = band_top + 7.78
    for i, ln in enumerate(lines):
        cv.text(COL_DESC_X, y + i * 11.72, ln, 10.0)
    cv.text(COL_DT_RIGHT, y, op.dt.strftime("%d.%m.%Y"), 10.0, right=True)
    cv.text(COL_DT_RIGHT, y + 10.88, f"в {op.dt.strftime('%H:%M')}", 8.0, right=True)
    cv.text(COL_PROC_RIGHT, y, _fmt_date(op.processing_date), 10.0, right=True)
    if op.card:
        cv.text(COL_CARD_X, y, op.card, 10.0)
    amt = fmt_signed(op.amount)
    cv.text(COL_SUM_RIGHT, y, amt, 10.0, right=True)
    cv.text(COL_ESP_RIGHT, y, amt, 10.0, right=True)
    bottom = band_top + _band_height(len(lines))
    cv.line(bottom)
    return bottom


def _extract_signature_image(donor_path):
    """Картинка подписи с последней страницы донора (если донор доступен)."""
    try:
        doc = fitz.open(donor_path)
        page = doc[-1]
        best = None
        for img in page.get_images(full=True):
            bbox = page.get_image_bbox(img)
            if bbox.width > 50 and bbox.height > 20 and (best is None or bbox.width > best[1].width):
                best = (img[0], bbox)
        if best is None:
            return None
        pix = fitz.Pixmap(doc, best[0])
        if pix.n > 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        data = pix.tobytes("png")
        doc.close()
        return data
    except Exception:
        return None


def generate_statement(operations, *, opening_balance=0, period_from=None,
                       period_to=None, statement_date=None, isx_no=None,
                       creation_dt=None, doc_id=None, signature_donor=None,
                       meta_out=None, **client_kwargs):
    """Synthetic-режим: выписка с N операциями и пагинацией по закону корпуса.

    Возвращает PDF (bytes). NOT byte-faithful — см. docstring модуля.
    `operations` — список Operation или dict с теми же полями."""
    client = dict(CLIENT_DEFAULTS)
    unknown = set(client_kwargs) - set(client)
    if unknown:
        raise TypeError(f"Неизвестные параметры клиента: {sorted(unknown)}")
    client.update(client_kwargs)
    ops = [Operation.from_dict(o) for o in (operations or [])]
    opening = _dec(opening_balance)
    total_in = sum((o.amount for o in ops if o.amount > 0), Decimal("0"))
    total_out = sum((o.amount for o in ops if o.amount < 0), Decimal("0"))
    closing = opening + total_in + total_out

    if ops:
        if period_from is None:
            period_from = min(o.dt.date() for o in ops)
        if period_to is None:
            period_to = max(o.dt.date() for o in ops)
    if period_from is None or period_to is None:
        # Empty statement: period used to stay None and print literally
        # ("за период с None по None"); fall back to the statement date.
        _fallback = statement_date or period_to or period_from or datetime.now(MSK).date()
        period_from = period_from or _fallback
        period_to = period_to or _fallback
    if period_from is not None and period_to is not None and period_from > period_to:
        raise ValueError("period_from позже period_to")
    if statement_date is None:
        statement_date = period_to or datetime.now(MSK).date()
    if isx_no is None:
        isx_no = statement_date.strftime("%Y%m%d") + f"{secrets.randbelow(10**7):07d}"
    creation = _creation_str(creation_dt)
    doc_id_v = _make_doc_id(creation, isx_no, doc_id)

    wrapped = [(o, wrap_description(o.description)) for o in ops]
    pages = paginate(wrapped)
    n_pages = len(pages)
    sig_png = _extract_signature_image(signature_donor) if signature_donor else None

    doc = fitz.open()
    for idx, plan in enumerate(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        cv = _Canvas(page)
        is_first, is_last = idx == 0, idx == n_pages - 1
        if is_first:
            cv.text(COL_DESC_X, 24.27, "Выписка по договору", 24.0, "medium")
            cv.text(RIGHT_EDGE, 21.78, f"Исх. № {isx_no}", 10.0, right=True)
            cv.text(RIGHT_EDGE, 36.78, f"Дата {_fmt_date(statement_date)}", 10.0, right=True)
            intro = [client["contract_no"] and
                     f"№ {client['contract_no']} от {client['contract_date']} в рамках продукта"
                     if ln is None else ln for ln in _BANK_INTRO]
            for i, ln in enumerate(intro):
                cv.text(COL_DESC_X, 81.78 + i * 13.11, ln, 10.0)
            cv.text(RIGHT_EDGE, 81.78, client["client_name"], 10.0, "medium", right=True)
            cv.text(RIGHT_EDGE, 94.74, f"Дата рождения {client['client_birth']}", 10.0, right=True)
            cv.text(RIGHT_EDGE, 107.69, f"Паспорт РФ № {client['client_passport']}", 10.0, right=True)
            cv.text(COL_DESC_X, 158.49,
                    f"Выписка по Договору за период с {_fmt_date(period_from)} по {_fmt_date(period_to)}",
                    14.0)
            cv.text(COL_DESC_X, 185.92, f"Входящий остаток на {_fmt_date(period_from)}",
                    10.0, "medium")
            cv.text(COL_ESP_RIGHT, 185.92, fmt_balance(opening), 10.0, "medium", right=True)
        band_top = None
        if plan.ops:
            band_top = _draw_header_block(cv, is_first, page)
            for op, lines in plan.ops:
                band_top = _draw_op(cv, op, lines, band_top)
        if plan.outgoing_here:
            cv.text(COL_DESC_X, plan.outgoing_y,
                    f"Исходящий остаток за {_fmt_date(period_to)}", 10.0, "medium")
            cv.text(COL_ESP_RIGHT, plan.outgoing_y, fmt_balance(closing),
                    10.0, "medium", right=True)
        if plan.totals_here:
            exp_y, inc_y = plan.totals_y
            cv.text(COL_DESC_X, exp_y, "Всего расходных операций", 10.0, "medium")
            cv.text(COL_ESP_RIGHT, exp_y, fmt_signed(total_out), 10.0, "medium", right=True)
            cv.text(COL_DESC_X, inc_y, "Всего приходных операций", 10.0, "medium")
            cv.text(COL_ESP_RIGHT, inc_y, fmt_signed(total_in), 10.0, "medium", right=True)
        # подвал страницы
        if is_last:
            for i, ln in enumerate(_SIGNATORY):
                cv.text(COL_DESC_X, 720.78 + i * 11.72, ln, 10.0)
            if sig_png:
                page.insert_image(fitz.Rect(290.0, 677.0, 460.46, 774.0), stream=sig_png)
            cv.text(566.66, 747.12, "Яндекс Банк", 20.0, "logo", right=True)
            addr_size = 8.4
            while _font("regular").text_length(_BANK_ADDR, fontsize=addr_size) > 550 and addr_size > 5:
                addr_size -= 0.2
            cv.text(COL_DESC_X, 776.70, _BANK_ADDR, addr_size)
        else:
            cv.text(0, 773.42, "Продолжение на следующей странице", 8.0, center=297.5)
        cv.text(RIGHT_EDGE, 803.42, f"Страница {idx + 1} из {n_pages}", 8.0, right=True)

    doc.set_metadata({"producer": GENUINE_PRODUCER, "creator": GENUINE_CREATOR,
                      "creationDate": creation, "modDate": "",
                      "title": "", "author": "", "subject": "", "keywords": ""})
    raw = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    pdf, forced = _force_id(raw, doc_id_v)

    if meta_out is not None:
        meta_out.clear()
        meta_out.update({
            "mode": "synthetic", "engine": "PyMuPDF", "byte_faithful": False,
            "pages": n_pages, "ops_per_page": [len(p.ops) for p in pages],
            "isx_no": isx_no, "creation": creation, "doc_id": doc_id_v,
            "id_written": forced,
            "opening": str(opening), "closing": str(closing),
            "total_in": str(total_in), "total_out": str(total_out),
            "fonts": dict(_FONT_PATHS), "signature_image": bool(sig_png),
            "limitations": ["synthetic: шрифт Arial/DejaVu вместо YSText subset",
                            "подпись-скан только при signature_donor",
                            "не byte-faithful Jasper/OpenPDF"],
        })
    return pdf


# ---------------------------------------------------------------- donor-template
class DonorTemplateError(ValueError):
    pass


_KNOWN_DONORS = {
    1: "33c8e075-893b-4abf-9bd1-c730a4edb729.pdf",
    2: "2460ac89-1d68-4239-9a1c-5c5baace9ede.pdf",
    3: "b34c97a3-af09-488d-858e-b1d25a7cb93b.pdf",
}
_DONOR_DIRS = (os.environ.get("YANDEX_DONORS_DIR"),
               r"C:\Users\nemor\Downloads\Telegram Desktop",
               "/opt/mts-bot/yandex_donors")


def find_donor(pages, donors_dir=None):
    """Найти genuine-донор с заданным числом страниц (1/2/3) в корпусе."""
    dirs = [donors_dir] if donors_dir else [d for d in _DONOR_DIRS if d]
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        known = os.path.join(d, _KNOWN_DONORS.get(pages, ""))
        if pages in _KNOWN_DONORS and os.path.exists(known):
            return known
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".pdf"):
                continue
            try:
                with fitz.open(os.path.join(d, name)) as doc:
                    if doc.page_count == pages and "Выписка по договору" in doc[0].get_text():
                        return os.path.join(d, name)
            except Exception:
                continue
    return None


def generate_from_donor(donor_path, *, creation_dt=None, doc_id=None, meta_out=None):
    """Donor-template: копия genuine-донора; меняются только CreationDate и /ID.

    Операции/суммы/ФИО изменить НЕЛЬЗЯ — для этого есть synthetic-режим.
    Размер и рендер выходного файла идентичны донору."""
    with open(donor_path, "rb") as f:
        raw = f.read()
    donor_md5 = hashlib.md5(raw).hexdigest()
    with fitz.open(donor_path) as doc:
        pages = doc.page_count
        meta = doc.metadata
        text0 = doc[0].get_text() if pages else ""
    if meta.get("producer") != GENUINE_PRODUCER or "JasperReports" not in (meta.get("creator") or ""):
        raise DonorTemplateError(f"Донор не похож на genuine Jasper-выписку: {meta}")
    if "Выписка по договору" not in text0:
        raise DonorTemplateError("Донор не является выпиской по договору")
    creation = _creation_str(creation_dt)
    m_isx = re.search(r"Исх\. № (\d{15})", text0)
    isx_no = m_isx.group(1) if m_isx else "000000000000000"
    doc_id_v = _make_doc_id(creation, isx_no, doc_id)
    out, n_cre = _CREATION_RE.subn(rb"\g<1>" + creation[2:-1].encode() + rb"\g<2>", raw)
    if n_cre == 0:
        raise DonorTemplateError("В доноре не найден /CreationDate")
    out, forced = _force_id(out, doc_id_v)
    if not forced:
        raise DonorTemplateError("В доноре не найден trailer /ID [<..><..>]")
    if len(out) != len(raw):
        raise DonorTemplateError("Внутренняя ошибка: длина файла изменилась")
    if meta_out is not None:
        meta_out.clear()
        meta_out.update({
            "mode": "donor-template", "engine": "byte-copy",
            "byte_faithful": "metadata-only (CreationDate + /ID)",
            "donor": os.path.basename(donor_path), "donor_md5": donor_md5,
            "pages": pages, "creation": creation, "doc_id": doc_id_v,
            "isx_no": isx_no, "changed": ["CreationDate", "/ID"],
            "limitations": ["операции/суммы/ФИО донора изменить нельзя"],
        })
    return out


def generate(*args, meta_out=None, mode="synthetic", donor_path=None, **kwargs):
    """Единая точка входа. mode='donor-template' требует donor_path и
    запрещает передавать операции (иначе — DonorTemplateError)."""
    if mode == "donor-template":
        if args and args[0]:
            raise DonorTemplateError(
                "donor-template не принимает операции: донор отдаётся как есть. "
                "Для своих операций используйте mode='synthetic'.")
        if not donor_path:
            raise DonorTemplateError("Для donor-template нужен donor_path")
        return generate_from_donor(donor_path, meta_out=meta_out, **kwargs)
    if mode != "synthetic":
        raise ValueError(f"Неизвестный режим: {mode!r}")
    operations = args[0] if args else kwargs.pop("operations", [])
    return generate_statement(operations, meta_out=meta_out, **kwargs)


if __name__ == "__main__":  # smoke: 21 операция -> 3 страницы
    import sys
    ops = [Operation(datetime(2026, 10, 4, 12, 0, tzinfo=MSK),
                     Decimal("10"), sender="Никита Валерьевич У.",
                     phone="+79931349557", bank="Озон Банк (Ozon)")
           for _ in range(21)]
    m = {}
    data = generate_statement(ops, opening_balance=0, meta_out=m)
    out = sys.argv[1] if len(sys.argv) > 1 else "_yandex_v2_smoke.pdf"
    with open(out, "wb") as f:
        f.write(data)
    print(f"OK {len(data)} bytes pages={m['pages']} ops/page={m['ops_per_page']} -> {out}")
