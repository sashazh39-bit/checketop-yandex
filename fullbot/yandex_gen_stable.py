# -*- coding: utf-8 -*-
"""Yandex Bank statement generator (PyMuPDF-based, stable module name).

Produces a 1-page Yandex Bank EDS-account statement with one incoming SBP
transfer. Signature: generate(recipient, phone, bank, amount, dt, meta_out).
"""
import os, re, pickle, hashlib, secrets
from datetime import datetime, timedelta, timezone
import fitz

_DIR = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.path.join(_DIR, "_yandex_catalog.pkl")
_CAT = None
def _load_cat():
    global _CAT
    if _CAT is None:
        try:
            with open(_CATALOG_PATH, "rb") as f:
                _CAT = pickle.load(f)
        except FileNotFoundError:
            _CAT = {"catalog": {}, "ttf": {}, "faces": (), "meta": {}}
    return _CAT

_FALLBACK_TTF = None
def _fallback_ttf():
    global _FALLBACK_TTF
    if _FALLBACK_TTF is not None:
        return _FALLBACK_TTF if _FALLBACK_TTF else None
    for p in [r"C:\Windows\Fonts\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(p):
            _FALLBACK_TTF = p; return p
    _FALLBACK_TTF = ""; return None

def _ys_ttf(face):
    cat = _load_cat()
    blob = cat.get("ttf", {}).get(face)
    if not blob: return None
    p = os.path.join(_DIR, "_yandex_fonts", f"{face}.ttf")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f: f.write(blob)
    return p

def _pick_font(text, face="YSText-Regular"):
    fb = _fallback_ttf()
    if fb: return fb, os.path.basename(fb)
    ys = _ys_ttf(face)
    if ys is not None: return ys, face
    return None, "helv"

_PROD = "OpenPDF 1.3.32"
_CREATOR = ("JasperReports Library version 6.21.2-"
            "8434a0bd7c3bbc37cbf916f2968d35e4b165821a")
_MSK = timezone(timedelta(hours=3))

def _creation_date(dt):
    if dt is None: dt = datetime.now(timezone.utc)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")

def _make_id(creation_str, isx_no):
    seed = (creation_str + "|" + isx_no + "|" + secrets.token_hex(8)).encode()
    h = hashlib.md5(seed).hexdigest()
    return h, h

def _make_isx_no(now_msk):
    return now_msk.strftime("%Y%m%d") + f"{secrets.randbelow(10**7):07d}"

PAGE_W, PAGE_H = 595.32, 841.92
_BANK_INTRO = [
    "АО «Яндекс Банк», лицензия Банка России № 3027 (далее — «Банк»),",
    "сообщает, что между Вами и Банком заключён договор",
    "№ Э20260929767871183 от 29.09.2026 в рамках продукта",
    "«Счёт ЭДС» (далее — «Договор»).",
]
_CLIENT_NAME = "Хабибуллин Ильяс Ильнурович"
_CLIENT_BIRTH = "Дата рождения 30.04.2008"
_CLIENT_PASS = "Паспорт РФ № 9222 096868"
_FOOTER_LINES = ["С уважением,",
    "Начальник отдела сопровождения операций по",
    "платёжным картам и сервисам", "T.C. Чиличихина", "Яндекс Банк"]
_BANK_ADDR = ("АО «Яндекс Банк», ул. Садовническая, д. 82 стр. 2, "
              "Москва, Россия, 115035, Тел.: +7 495 787-37-96, "
              "yabank.yandex.ru, welcome@bank.yandex.ru")
COL_DESC=20.0; COL_DATE=211.22; COL_PDATE=281.22; COL_SUM=414.59; COL_ESP=509.59

def _fmt_amount(amount):
    if amount is None: amount = 0
    sign = "+" if amount >= 0 else "–"
    rub = abs(int(amount))
    kop = abs(int(round((float(amount) - int(amount)) * 100)))
    return f"{sign}{rub},{kop:02d} ₽"

def _fmt_phone(phone):
    d = "".join(c for c in (phone or "") if c.isdigit())
    if len(d) == 11 and d[0] in "78": d = d[1:]
    if len(d) != 10: return phone
    return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"

def _split_name(name):
    parts = (name or "").strip().split()
    if len(parts) >= 2: return parts[0], " ".join(parts[1:])
    return (name or ""), ""

def generate(recipient, phone, bank, amount, dt=None, meta_out=None):
    if dt is None: dt = datetime.now(_MSK)
    elif dt.tzinfo is None: dt = dt.replace(tzinfo=_MSK)
    else: dt = dt.astimezone(_MSK)
    now_msk = datetime.now(_MSK)
    op_dt = dt - timedelta(seconds=secrets.randbelow(60) + 15)
    if op_dt > now_msk:
        op_dt = now_msk - timedelta(seconds=secrets.randbelow(60) + 15)
    isx_no = _make_isx_no(now_msk)
    creation_utc = op_dt - timedelta(seconds=secrets.randbelow(120) + 30)
    creation_str = _creation_date(creation_utc)
    id_a, id_b = _make_id(creation_str, isx_no)
    phone_f = _fmt_phone(phone)
    amount_f = _fmt_amount(amount)
    first, rest = _split_name(recipient)
    desc_l1 = f"Входящий перевод СБП, {first}"
    desc_l2 = f"{rest}, {phone_f},"
    desc_l3 = bank
    op_date = op_dt.strftime("%d.%m.%Y")
    op_time = op_dt.strftime("%H:%M")
    proc_date = op_date
    period_from = (op_dt - timedelta(days=1)).strftime("%d.%m.%Y")
    period_to = op_dt.strftime("%d.%m.%Y")
    stmt_date = now_msk.strftime("%d.%m.%Y")
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    def put(x, y, text, size, face="YSText-Regular", color=(0,0,0)):
        ff, fn = _pick_font(text, face)
        page.insert_text(fitz.Point(x, PAGE_H - y), text,
                         fontsize=size, fontname=fn if fn else "helv",
                         fontfile=ff, color=color)
    put(20.0, 24.27, "Выписка по договору", 24.0, "YSText-Medium")
    put(452.75, 21.78, f"Исх. № {isx_no}", 10.0)
    put(496.3, 36.78, f"Дата {stmt_date}", 10.0)
    y = 81.78
    for line in _BANK_INTRO:
        put(20.0, y, line, 10.0); y += 13.11
    put(420.63, 81.78, _CLIENT_NAME, 10.0, "YSText-Medium")
    put(446.59, 94.74, _CLIENT_BIRTH, 10.0)
    put(442.15, 107.69, _CLIENT_PASS, 10.0)
    put(20.0, 158.49, f"Выписка по Договору за период с {period_from} по {period_to}", 14.0)
    put(20.0, 185.92, f"Входящий остаток на {period_from}", 10.0, "YSText-Medium")
    put(COL_DESC, 208.42, "Описание операции", 8.0)
    put(210.25, 208.42, "Дата и время", 8.0)
    put(223.5, 217.8, "операции", 8.0)
    put(242.5, 227.18, "МСК", 8.0)
    put(289.94, 217.8, "обработки", 8.0)
    put(311.66, 208.42, "Дата", 8.0)
    put(312.5, 227.18, "МСК", 8.0)
    put(357.84, 208.42, "Карта", 8.0)
    put(414.59, 208.42, "Сумма в валюте", 8.0)
    put(438.5, 217.8, "операции", 8.0)
    put(509.59, 208.42, "Сумма в валюте", 8.0)
    put(553.62, 217.8, "ЭСП", 8.0)
    put(COL_DESC, 246.78, desc_l1, 10.0)
    put(COL_DESC, 258.5, desc_l2, 10.0)
    put(COL_DESC, 270.22, desc_l3, 10.0)
    put(COL_DATE, 246.78, op_date, 10.0)
    put(234.51, 257.66, f"в {op_time}", 8.0)
    put(COL_PDATE, 246.78, proc_date, 10.0)
    put(COL_SUM, 246.78, amount_f, 10.0)
    put(COL_ESP, 246.78, amount_f, 10.0)
    put(20.0, 294.78, f"Исходящий остаток за {period_to}", 10.0, "YSText-Medium")
    put(COL_SUM, 294.78, amount_f, 10.0)
    put(COL_ESP, 294.78, amount_f, 10.0)
    put(20.0, 342.78, "Всего расходных операций", 10.0)
    put(COL_SUM, 342.78, "0,00 ₽" if amount >= 0 else amount_f, 10.0)
    put(20.0, 354.5, "Всего приходных операций", 10.0)
    put(COL_SUM, 354.5, amount_f if amount >= 0 else "0,00 ₽", 10.0)
    fy = 700.0
    for line in _FOOTER_LINES:
        put(20.0, fy, line, 10.0); fy += 13.0
    put(20.0, fy + 6, _BANK_ADDR, 8.0)
    put(290.0, 820.0, "Страница 1 из 1", 10.0)
    doc.set_metadata({"producer": _PROD, "creator": _CREATOR,
        "creationDate": creation_str, "modDate": "",
        "title": "", "author": "", "subject": "", "keywords": ""})
    pdf = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    if meta_out is not None:
        meta_out.clear()
        meta_out.update({"isx_no": isx_no, "creation": creation_str, "op_dt": op_dt,
            "op_id": isx_no, "amount_fmt": amount_f, "phone_fmt": phone_f,
            "period_from": period_from, "period_to": period_to,
            "stmt_date": stmt_date, "id": (id_a, id_b), "donor": "yandex"})
    return pdf

if __name__ == "__main__":
    import sys
    meta = {}
    pdf = generate("Роман Андреевич В.", "+79118584552", "Озон Банк (Ozon)", 11, meta_out=meta)
    out = sys.argv[1] if len(sys.argv) > 1 else "_yandex_smoke.pdf"
    with open(out, "wb") as f: f.write(pdf)
    print(f"OK {len(pdf)} bytes isx={meta['isx_no']} amt={meta['amount_fmt']} -> {out}")
