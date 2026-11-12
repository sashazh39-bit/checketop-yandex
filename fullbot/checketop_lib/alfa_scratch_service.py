#!/usr/bin/env python3
"""Alfa Bank receipt generation from scratch.

Builds a valid Oracle BI Publisher-style PDF from scratch for all 5 receipt
types (SBP, Alfa-Alfa, Card, Transgran, Mobile) using a universal donor PDF
for font/images.  Does NOT modify an existing template — the entire content
stream is constructed from user-provided field values.

The donor PDF (`шаблоны/alfa_universal_donor.pdf`) provides:
  - 3 images (header, logo, QR footer) — identical across all types
  - Embedded Tahoma CID font subset (CIDFontType2, Identity-H)
  - ToUnicode CMap + glyph widths (/W array)
  - FontDescriptor + FontFile2 (TTF data)

For characters not in the donor font subset the CMap is extended with
sequential CIDs (no Identity mapping to avoid collisions).
"""
from __future__ import annotations

import random
import re
import secrets
import string
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

_BASE_DIR = Path(__file__).resolve().parent
DONOR_PATH = _BASE_DIR / "шаблоны" / "alfa_universal_donor.pdf"

# ── Layout constants (from reference PDFs) ────────────────────────────
_CRLF = b"\r\n"

_FORMED_X = 484.048
_FORMED_Y = 795.444
_FORMED_SPACER_X = 559.069
_FORMED_SPACER_Y = 790.672
_DATE_FORMED_X = 452.788
_DATE_FORMED_Y = 779.15

_TITLE_X = 35.45
_TITLE_Y = 715.783
_TITLE_SPACER_Y = 732.437
_TITLE_BOTTOM_SPACER_Y = 690.436

_LEFT_X = 35.45
_RIGHT_X = 304.75
_CLIP_W = 255.1
_CLIP_H = 15.6

_ROW_Y_BASE = 682.698
_ROW_STEP = 42.894

_GREY_FORMED = b".494 .494 .513 RG .494 .494 .513 rg"
_GREY_LABEL = b".501 .501 .501 RG .501 .501 .501 rg"
_BLACK = b"0 0 0 RG 0 0 0 rg"

# Per-row Y offsets from the label Y
_SPACER_DY = -4.772
_CLIP_DY = -20.888
_VALUE_DY = -18.41


def _row_y(index: int) -> float:
    return _ROW_Y_BASE - index * _ROW_STEP


def _bottom_positions(num_left_rows: int) -> tuple[float, float, float, float]:
    """Return (spacer_28_y, im2_y, post_im2_y, named_dest_y) for given row count."""
    if num_left_rows == 5:
        return 451.221, 370.487, 367.986, 367.47
    return 494.115, 413.381, 410.88, 410.364


# ── Receipt type definitions ──────────────────────────────────────────

def _field(key: str, label: str, prompt: str, default: str = "") -> dict:
    return {"key": key, "label": label, "prompt": prompt, "default": default}


SBP_LEFT = [
    _field("amount", "Сумма\xa0перевода", "Сумма (целое число RUR)", "10"),
    _field("commission", "Комиссия", "Комиссия (целое RUR)", "0"),
    _field("datetime", "Дата\xa0и\xa0время\xa0перевода", "Дата/время (ДД.ММ.ГГГГ ЧЧ:ММ:СС мск)", ""),
    _field("operation_id", "Номер\xa0операции", "Номер операции", ""),
    _field("recipient", "Получатель", "Получатель (ФИО)", ""),
]
SBP_RIGHT = [
    _field("phone", "Номер\xa0телефона\xa0получателя", "Телефон (+7 (XXX) XXX-XX-XX)", ""),
    _field("bank", "Банк\xa0получателя", "Банк получателя", "Сбербанк"),
    _field("account", "Счёт\xa0списания", "Счёт списания (20 цифр)", ""),
    _field("sbp_id", "Идентификатор\xa0операции\xa0в\xa0СБП", "ID операции в СБП", ""),
    _field("message", "Сообщение\xa0получателю", "Сообщение получателю", "Перевод денежных средств"),
]

ALFA_LEFT = [
    _field("amount", "Сумма\xa0перевода", "Сумма (целое число RUR)", "10"),
    _field("commission", "Комиссия", "Комиссия (целое RUR)", "0"),
    _field("datetime", "Дата\xa0и\xa0время\xa0перевода", "Дата/время (ДД.ММ.ГГГГ ЧЧ:ММ:СС мск)", ""),
    _field("operation_id", "Номер\xa0операции", "Номер операции", ""),
]
ALFA_RIGHT = [
    _field("recipient", "Получатель", "Получатель (ФИО)", ""),
    _field("phone", "Номер\xa0телефона\xa0получателя", "Телефон получателя", ""),
    _field("account", "Счёт\xa0списания", "Счёт списания", ""),
    _field("message", "Сообщение\xa0получателю", "Сообщение получателю", "Перевод денежных средств"),
]

CARD_LEFT = [
    _field("amount", "Сумма\xa0перевода", "Сумма (целое число RUR)", "10"),
    _field("commission", "Комиссия", "Комиссия (целое RUR)", "0"),
    _field("card_sender", "Номер\xa0карты\xa0отправителя", "Карта отправителя (220015******XXXX)", ""),
    _field("card_recipient", "Номер\xa0карты\xa0получателя", "Карта получателя (220015******XXXX)", ""),
]
CARD_RIGHT = [
    _field("datetime", "Дата\xa0и\xa0время\xa0перевода", "Дата/время (ДД.ММ.ГГГГ ЧЧ:ММ:СС мск)", ""),
    _field("auth_code", "Код\xa0авторизации", "Код авторизации (6 символов)", ""),
    _field("terminal", "Код\xa0терминала", "Код терминала (6 цифр)", ""),
    _field("operation_id", "Номер\xa0операции\xa0в\xa0банке", "Номер операции в банке", ""),
]

TRANSGRAN_LEFT = [
    _field("amount", "Сумма\xa0перевода", "Сумма (целое число RUR)", "10"),
    _field("commission", "Комиссия", "Комиссия (целое RUR)", "0"),
    _field("rate", "Курс\xa0конвертации", "Курс (напр. 1 RUR = 140.5800 UZS)", ""),
    _field("credited", "Сумма\xa0зачисления\xa0банком\xa0получателя", "Сумма зачисления (напр. 1 405,80 UZS)", ""),
    _field("datetime", "Дата\xa0и\xa0время\xa0перевода", "Дата/время (ДД.ММ.ГГГГ ЧЧ:ММ:СС мск)", ""),
]
TRANSGRAN_RIGHT = [
    _field("recipient", "Получатель", "Получатель (ФИО)", ""),
    _field("phone", "Номер\xa0телефона\xa0получателя", "Телефон получателя", ""),
    _field("account", "Счёт\xa0списания", "Счёт списания (20 цифр)", ""),
    _field("operation_id", "Номер\xa0операции\xa0в\xa0банке", "Номер операции в банке", ""),
]

MOBILE_LEFT = [
    _field("amount", "Сумма\xa0платежа", "Сумма (целое число RUR)", "100"),
    _field("commission", "Комиссия", "Комиссия (целое RUR)", "0"),
    _field("datetime", "Дата\xa0и\xa0время\xa0платежа", "Дата/время (ДД.ММ.ГГГГ ЧЧ:ММ:СС мск)", ""),
    _field("operation_id", "Номер\xa0операции", "Номер операции", ""),
]
MOBILE_RIGHT = [
    _field("payer", "Плательщик", "Плательщик (ФИО)", ""),
    _field("account", "Счёт\xa0списания", "Счёт списания (20 цифр)", ""),
    _field("phone", "Номер\xa0телефона", "Номер телефона", ""),
    _field("provider", "Провайдер", "Провайдер (билайн, МТС, …)", "билайн"),
]

RECEIPT_TYPES = {
    "sbp": {
        "title": "Квитанция\xa0о\xa0переводе\xa0по\xa0СБП\xa0",
        "left": SBP_LEFT,
        "right": SBP_RIGHT,
    },
    "alfa": {
        "title": "Квитанция\xa0о\xa0переводе\xa0клиенту\xa0Альфа-Банка\xa0",
        "left": ALFA_LEFT,
        "right": ALFA_RIGHT,
    },
    "card": {
        "title": "Квитанция\xa0о\xa0переводе\xa0с\xa0карты\xa0на\xa0карту\xa0",
        "left": CARD_LEFT,
        "right": CARD_RIGHT,
    },
    "transgran": {
        "title": "Квитанция\xa0о\xa0переводе\xa0за\xa0рубеж\xa0по\xa0номеру\xa0телефона\xa0",
        "left": TRANSGRAN_LEFT,
        "right": TRANSGRAN_RIGHT,
    },
    "mobile": {
        "title": "Квитанция\xa0об\xa0оплате\xa0мобильной\xa0связи\xa0",
        "left": MOBILE_LEFT,
        "right": MOBILE_RIGHT,
    },
}


# ── CMap / CID helpers ────────────────────────────────────────────────

def _parse_cmap_from_bytes(data: bytes) -> dict[int, str]:
    """Parse all ToUnicode CMap entries → {unicode_int: cid_hex_4char}."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
        if b"beginbfrange" in dec:
            for mm in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec
            ):
                s = int(mm.group(1), 16)
                e = int(mm.group(2), 16)
                u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    uni_to_cid[u + i] = f"{s + i:04X}"
    return uni_to_cid


def _encode_text(text: str, uni_to_cid: dict[int, str]) -> bytes:
    """Encode unicode text → CID hex string like <001F000E...>."""
    parts = []
    for ch in text:
        cp = ord(ch)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            raise ValueError(f"Character {ch!r} (U+{cp:04X}) not in CMap")
        parts.append(uni_to_cid[cp])
    return ("<" + "".join(parts) + ">").encode()


def _collect_required_chars(rtype: dict, values: dict[str, str], formed_text: str, formed_date: str) -> set[int]:
    """Collect all Unicode codepoints needed for the receipt."""
    chars: set[int] = set()
    for text in [formed_text, formed_date, rtype["title"]]:
        for ch in text:
            chars.add(ord(ch))
    for is_left, field_list in ((True, rtype["left"]), (False, rtype["right"])):
        for field in field_list:
            for ch in field["label"]:
                chars.add(ord(ch))
            # Use the processed value (same as _get_value) to capture RUR, NBSP, etc.
            val_text = _get_value(values, field["key"], is_left=is_left)
            for ch in val_text:
                chars.add(ord(ch))
    # Always include NBSP (used as spacer throughout)
    chars.add(0xA0)
    chars.discard(0)
    return chars


def _extend_cmap(
    uni_to_cid: dict[int, str],
    required_chars: set[int],
) -> dict[int, str]:
    """Extend CMap with new CID assignments for missing characters.

    Uses sequential CIDs starting above the current maximum to avoid collisions.
    Also maps regular space (0x20) to NBSP CID if NBSP is available.
    """
    uni_to_cid = dict(uni_to_cid)

    if 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
        uni_to_cid[0x20] = uni_to_cid[0xA0]

    used_cids = set(uni_to_cid.values())
    max_cid = max(int(c, 16) for c in used_cids) if used_cids else 0
    next_cid = max_cid + 1

    for cp in sorted(required_chars):
        if cp in uni_to_cid:
            continue
        if cp < 0x20:
            continue
        cid_hex = f"{next_cid:04X}"
        while cid_hex in used_cids:
            next_cid += 1
            cid_hex = f"{next_cid:04X}"
        uni_to_cid[cp] = cid_hex
        used_cids.add(cid_hex)
        next_cid += 1

    return uni_to_cid


# ── Donor extraction ──────────────────────────────────────────────────

def _extract_stream(data: bytes, obj_num: int) -> tuple[bytes, bytes]:
    """Extract (header_before_stream, raw_stream_data) for an object with stream."""
    pattern = rf"{obj_num}\s+0\s+obj\r?\n(.*?)stream\r?\n".encode()
    m = re.search(pattern, data, re.DOTALL)
    if not m:
        raise ValueError(f"Object {obj_num} not found")
    header = data[m.start(): m.end()]
    length_m = re.search(rb"/Length\s+(\d+)", m.group(1))
    slen = int(length_m.group(1))
    stream_data = data[m.end(): m.end() + slen]
    return header, stream_data


def _extract_object(data: bytes, obj_num: int) -> bytes:
    """Extract full object bytes (N 0 obj ... endobj\\r\\n)."""
    pattern = rf"{obj_num}\s+0\s+obj\b".encode()
    m = re.search(pattern, data)
    if not m:
        raise ValueError(f"Object {obj_num} not found")
    end = data.find(b"endobj", m.start())
    return data[m.start(): end + 6]


def _extract_widths(data: bytes) -> str:
    """Extract /W array string from object 11."""
    obj = _extract_object(data, 11)
    w_m = re.search(rb"/W\s*(\[.*?\])\s*>>", obj, re.DOTALL)
    if not w_m:
        raise ValueError("No /W array found in CIDFont")
    return w_m.group(1).decode("latin-1")


def _load_donor(donor_path: Path) -> dict:
    """Load and parse the universal donor PDF."""
    data = donor_path.read_bytes()

    uni_to_cid = _parse_cmap_from_bytes(data)
    if not uni_to_cid:
        raise ValueError("No ToUnicode CMap in donor PDF")

    images = {}
    for obj_num in (5, 6, 7):
        header, stream = _extract_stream(data, obj_num)
        images[obj_num] = (header, stream)

    _, font_stream = _extract_stream(data, 13)
    font_decompressed = zlib.decompress(font_stream)

    _, cmap_stream = _extract_stream(data, 14)
    cmap_decompressed = zlib.decompress(cmap_stream)

    widths_str = _extract_widths(data)

    font_desc = _extract_object(data, 12)
    font_name_m = re.search(rb"/FontName\s*/(\S+)", font_desc)
    orig_font_name = font_name_m.group(1).decode("latin-1") if font_name_m else "XXXXXX+Tahoma"

    return {
        "raw": data,
        "images": images,
        "font_ttf": font_decompressed,
        "cmap_raw": cmap_decompressed,
        "widths_str": widths_str,
        "uni_to_cid": uni_to_cid,
        "orig_font_name": orig_font_name,
    }


# ── Content stream builder ────────────────────────────────────────────

def _format_amount_value(amount_str: str) -> str:
    """Format amount for PDF value field: 'N RUR ' with NBSP."""
    try:
        val = int(amount_str.replace(" ", "").replace("\xa0", ""))
    except (ValueError, AttributeError):
        return amount_str
    s = f"{val:,}".replace(",", "\xa0")
    return f"{s}\xa0RUR\xa0"


def _get_value(values: dict[str, str], key: str, is_left: bool) -> str:
    """Get field value, formatting amounts and adding trailing NBSP for left column."""
    raw = values.get(key, "")
    if not raw:
        return "\xa0"

    if key in ("amount", "commission"):
        return _format_amount_value(raw)

    text = raw.replace(" ", "\xa0")
    if is_left and not text.endswith("\xa0"):
        text += "\xa0"
    return text


def _build_content_stream(
    receipt_type: str,
    values: dict[str, str],
    uni_to_cid: dict[int, str],
    formed_text: str,
    formed_date: str,
) -> bytes:
    """Build the complete content stream (uncompressed) for a receipt."""
    rtype = RECEIPT_TYPES[receipt_type]
    left_fields = rtype["left"]
    right_fields = rtype["right"]
    num_left = len(left_fields)
    spacer_28_y, im2_y, post_im2_y, _ = _bottom_positions(num_left)

    def enc(text: str) -> bytes:
        return _encode_text(text, uni_to_cid)

    NBSP = enc("\xa0")
    parts: list[bytes] = []

    def w(*args: bytes):
        for a in args:
            parts.append(a)
            parts.append(_CRLF)

    # ── Images ──
    w(b"q 297 0 0 35 262.85 35.45 cm /Im0 Do Q")
    w(b"q 30 0 0 46 35.45 760.45 cm /Im1 Do Q")

    # ── Formed header ──
    w(_GREY_FORMED)
    w(b"BT")
    w(f"1 0 0 1 {_FORMED_X} {_FORMED_Y} Tm".encode())
    w(b"/F1")
    w(b" 11 Tf")
    w(enc(formed_text) + b" Tj")
    w(_BLACK)
    w(f"1 0 0 1 {_FORMED_SPACER_X} {_FORMED_SPACER_Y} Tm".encode())
    w(b"/F1")
    w(b" 2.5 Tf")
    w(NBSP + b" Tj")
    w(_GREY_FORMED)
    w(f"1 0 0 1 {_DATE_FORMED_X} {_DATE_FORMED_Y} Tm".encode())
    w(b"/F1")
    w(b" 11 Tf")
    w(enc(formed_date) + b" Tj")

    # ── Title ──
    w(_BLACK)
    w(f"1 0 0 1 {_TITLE_X} {_TITLE_SPACER_Y} Tm".encode())
    w(b"/F1")
    w(b" 28 Tf")
    w(NBSP + b" Tj")
    w(f"1 0 0 1 {_TITLE_X} {_TITLE_Y} Tm".encode())
    w(b"/F1")
    w(b" 21 Tf")
    w(enc(rtype["title"]) + b" Tj")
    w(f"1 0 0 1 {_TITLE_X} {_TITLE_BOTTOM_SPACER_Y} Tm".encode())
    w(NBSP + b" Tj")

    # ── Left column fields ──
    for i, field in enumerate(left_fields):
        y_label = _row_y(i)
        y_spacer = round(y_label + _SPACER_DY, 3)
        y_clip = round(y_label + _CLIP_DY, 3)
        y_value = round(y_label + _VALUE_DY, 3)
        val_text = _get_value(values, field["key"], is_left=True)

        w(_GREY_LABEL)
        w(f"1 0 0 1 {_LEFT_X} {y_label} Tm".encode())
        w(b"/F1")
        w(b" 11 Tf")
        w(enc(field["label"]) + b" Tj")
        w(_BLACK)
        w(f"1 0 0 1 {_LEFT_X} {y_spacer} Tm".encode())
        w(b"/F1")
        w(b" 2.5 Tf")
        w(NBSP + b" Tj")
        w(b"ET")
        w(f"q {_LEFT_X} {y_clip} {_CLIP_W} {_CLIP_H} re W n".encode())
        w(b"BT")
        w(f"1 0 0 1 {_LEFT_X} {y_value} Tm".encode())
        w(b"/F1")
        w(b" 12 Tf")
        w(enc(val_text) + b" Tj")
        w(b"ET")
        w(b"Q")

        if i < num_left - 1:
            w(b"0 0 0 RG")
            w(b"0 0 0 rg")
            w(b"BT")
            w(b"/F1 12 Tf")
        else:
            w(b"0 0 0 RG")
            w(b"0 0 0 rg")
            w(b"BT")
            w(b"/F1 12 Tf")

    # ── Bottom section (28pt spacer + Im2 + post-Im2 spacer) ──
    w(f"1 0 0 1 {_LEFT_X} {spacer_28_y} Tm".encode())
    w(b"/F1")
    w(b" 28 Tf")
    w(NBSP + b" Tj")
    w(b"ET")
    w(f"q 201 0 0 85.09 {_LEFT_X} {im2_y} cm /Im2 Do Q".encode())
    w(b"BT")
    w(f"1 0 0 1 {_LEFT_X} {post_im2_y} Tm".encode())
    w(b"/F1")
    w(b" 2.5 Tf")
    w(NBSP + b" Tj")

    # ── Right column fields ──
    for i, field in enumerate(right_fields):
        y_label = _row_y(i)
        y_spacer = round(y_label + _SPACER_DY, 3)
        y_clip = round(y_label + _CLIP_DY, 3)
        y_value = round(y_label + _VALUE_DY, 3)
        val_text = _get_value(values, field["key"], is_left=False)

        w(_GREY_LABEL)
        w(f"1 0 0 1 {_RIGHT_X} {y_label} Tm".encode())
        w(b"/F1")
        w(b" 11 Tf")
        w(enc(field["label"]) + b" Tj")
        w(_BLACK)
        w(f"1 0 0 1 {_RIGHT_X} {y_spacer} Tm".encode())
        w(b"/F1")
        w(b" 2.5 Tf")
        w(NBSP + b" Tj")
        w(b"ET")
        w(f"q {_RIGHT_X} {y_clip} {_CLIP_W} {_CLIP_H} re W n".encode())
        w(b"BT")
        w(f"1 0 0 1 {_RIGHT_X} {y_value} Tm".encode())
        w(b"/F1")
        w(b" 12 Tf")
        w(enc(val_text) + b" Tj")
        w(b"ET")
        w(b"Q")

        if i < len(right_fields) - 1:
            w(b"0 0 0 RG")
            w(b"0 0 0 rg")
            w(b"BT")
            w(b"/F1 12 Tf")

    # ── Trailing empty BT/ET ──
    w(b"0 0 0 RG")
    w(b"0 0 0 rg")
    w(b"BT")
    w(b"/F1 12 Tf")
    w(b"ET")

    return b"".join(parts)


# ── CMap builder ──────────────────────────────────────────────────────

def _build_cmap_stream(uni_to_cid: dict[int, str]) -> bytes:
    """Build a ToUnicode CMap stream (uncompressed) with beginbfchar entries."""
    entries = []
    for uni, cid in sorted(uni_to_cid.items(), key=lambda x: int(x[1], 16)):
        entries.append(f"<{cid}> <{uni:04X}>")

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo",
        "<< /Registry (Oracle)/Ordering(UCS)/Supplement 0 >> def",
        "/CMapName /Oracle-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000><FFFF>",
        "endcodespacerange",
        f"{len(entries)} beginbfchar",
    ]
    lines.extend(entries)
    lines.append("endbfchar")
    lines.append("endcmap")
    lines.append("CMapName currentdict /CMap defineresource pop")
    lines.append("end")
    lines.append("end")

    return ("\r\n".join(lines) + "\r\n").encode("latin-1")


# ── Widths builder ────────────────────────────────────────────────────

def _build_widths(uni_to_cid: dict[int, str], orig_widths: str) -> str:
    """Build /W array incorporating original widths + defaults for new CIDs."""
    orig_map: dict[int, int] = {}
    for m in re.finditer(r"(\d+)\s*\[(\d+)\]", orig_widths):
        cid = int(m.group(1))
        width = int(m.group(2))
        orig_map[cid] = width

    all_cids = sorted(set(int(c, 16) for c in uni_to_cid.values()))
    parts = []
    for cid in all_cids:
        w = orig_map.get(cid, 600)
        parts.append(f"{cid} [{w}]")

    return "[ " + " ".join(parts) + " ]"


# ── PDF assembler ─────────────────────────────────────────────────────

def _random_font_tag() -> str:
    """Generate a random 6-letter uppercase tag like 'CTKKCD'."""
    return "".join(random.choices(string.ascii_uppercase, k=6))


def _assemble_pdf(
    donor: dict,
    content_stream_raw: bytes,
    uni_to_cid: dict[int, str],
    named_dest_y: float,
) -> bytes:
    """Assemble a complete PDF binary from components."""
    font_tag = _random_font_tag()
    font_name = f"{font_tag}+Tahoma"

    content_compressed = zlib.compress(content_stream_raw, 6)
    cmap_raw = _build_cmap_stream(uni_to_cid)
    cmap_compressed = zlib.compress(cmap_raw, 6)
    font_compressed = zlib.compress(donor["font_ttf"], 6)
    widths_str = _build_widths(uni_to_cid, donor["widths_str"])

    offsets: dict[int, int] = {}
    buf = bytearray()

    def write(data: bytes):
        buf.extend(data)

    def mark_obj(n: int):
        offsets[n] = len(buf)

    LF = _CRLF

    # ── Header ──
    write(b"%PDF-1.6" + LF)

    # ── Object 5 (Image 0: header banner) ──
    mark_obj(5)
    h5, s5 = donor["images"][5]
    write(b"5 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /XObject" + LF)
    write(b"/Subtype /Image" + LF)
    write(b"/Filter /FlateDecode" + LF)
    write(f"/Length {len(s5)}".encode() + LF)
    write(b"/Width 900" + LF)
    write(b"/Height 105" + LF)
    write(b"/BitsPerComponent 8" + LF)
    write(b"/ColorSpace /DeviceRGB" + LF)
    write(b">>" + LF)
    write(b"stream" + LF)
    write(s5)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 6 (Image 1: logo) ──
    mark_obj(6)
    _, s6 = donor["images"][6]
    write(b"6 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /XObject" + LF)
    write(b"/Subtype /Image" + LF)
    write(b"/Filter /FlateDecode" + LF)
    write(f"/Length {len(s6)}".encode() + LF)
    write(b"/Width 90" + LF)
    write(b"/Height 138" + LF)
    write(b"/BitsPerComponent 8" + LF)
    write(b"/ColorSpace /DeviceRGB" + LF)
    write(b">>" + LF)
    write(b"stream" + LF)
    write(s6)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 7 (Image 2: QR footer) ──
    mark_obj(7)
    _, s7 = donor["images"][7]
    write(b"7 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /XObject" + LF)
    write(b"/Subtype /Image" + LF)
    write(b"/Filter /FlateDecode" + LF)
    write(f"/Length {len(s7)}".encode() + LF)
    write(b"/Width 603" + LF)
    write(b"/Height 258" + LF)
    write(b"/BitsPerComponent 8" + LF)
    write(b"/ColorSpace /DeviceRGB" + LF)
    write(b">>" + LF)
    write(b"stream" + LF)
    write(s7)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 8 (Page) ──
    mark_obj(8)
    write(b"8 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /Page" + LF)
    write(b"/Parent 3 0 R" + LF)
    write(b"/Resources 4 0 R" + LF)
    write(b"/Contents 9 0 R" + LF)
    write(b"/MediaBox[ 0 0 595.3 841.9 ]" + LF)
    write(b"/CropBox[ 0 0 595.3 841.9 ]" + LF)
    write(b"/Rotate 0" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 9 (Content stream) ──
    mark_obj(9)
    write(b"9 0 obj" + LF)
    write(f"<< /Length {len(content_compressed)} /Filter /FlateDecode >>".encode() + LF)
    write(b"stream" + LF)
    write(content_compressed)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 1 (Catalog) ──
    mark_obj(1)
    write(b"1 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /Catalog" + LF)
    write(b"/Pages 3 0 R" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 2 (Info) ──
    mark_obj(2)
    write(b"2 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Producer (Oracle BI Publisher 12.2.1.4.0)" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 3 (Pages) ──
    mark_obj(3)
    write(b"3 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /Pages" + LF)
    write(b"/Kids [ 8 0 R ]" + LF)
    write(b"/Count 1" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 4 (Resources) ──
    mark_obj(4)
    write(b"4 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/ProcSet [ /PDF /Text ]" + LF)
    write(b"/Font << /F1 10 0 R >>" + LF)
    write(b"/XObject << /Im0 5 0 R /Im1 6 0 R /Im2 7 0 R >>" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 10 (Type0 Font) ──
    mark_obj(10)
    write(b"10 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /Font" + LF)
    write(b"/Subtype /Type0" + LF)
    write(f"/BaseFont /{font_name}".encode() + LF)
    write(b"/Encoding /Identity-H" + LF)
    write(b"/DescendantFonts [ 11 0 R ]" + LF)
    write(b"/ToUnicode 14 0 R" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 11 (CIDFont) ──
    mark_obj(11)
    write(b"11 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /Font" + LF)
    write(b"/Subtype /CIDFontType2" + LF)
    write(f"/BaseFont /{font_name}".encode() + LF)
    write(b"/FontDescriptor 12 0 R" + LF)
    write(b"/CIDSystemInfo << /Registry (Adobe)/Ordering (Identity)/Supplement 0 >>" + LF)
    write(b"/DW 1000" + LF)
    write(f"/W {widths_str}".encode() + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 12 (FontDescriptor) ──
    mark_obj(12)
    write(b"12 0 obj" + LF)
    write(b"<<" + LF)
    write(b"/Type /FontDescriptor" + LF)
    write(b"/Ascent 1000" + LF)
    write(b"/CapHeight 727" + LF)
    write(b"/Descent -206" + LF)
    write(b"/Flags 4" + LF)
    write(b"/FontBBox [ -599 -207 1338 1034 ]" + LF)
    write(f"/FontName /{font_name}".encode() + LF)
    write(b"/ItalicAngle 0.0" + LF)
    write(b"/StemV 0" + LF)
    write(b"/FontFile2 13 0 R" + LF)
    write(b">>" + LF)
    write(b"endobj" + LF)

    # ── Object 13 (FontFile2 — embedded TTF) ──
    mark_obj(13)
    write(b"13 0 obj" + LF)
    write(f"<< /Filter /FlateDecode /Length {len(font_compressed)} /Length1 {len(donor['font_ttf'])} >>".encode() + LF)
    write(b"stream" + LF)
    write(font_compressed)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 14 (ToUnicode CMap) ──
    mark_obj(14)
    write(b"14 0 obj" + LF)
    write(f"<< /Filter /FlateDecode /Length {len(cmap_compressed)} >>".encode() + LF)
    write(b"stream" + LF)
    write(cmap_compressed)
    write(LF + b"endstream" + LF)
    write(b"endobj" + LF)

    # ── Object 15, 16 (Named destinations) ──
    dest_str = f"[ 8 0 R /XYZ {_LEFT_X} {named_dest_y} null ]".encode()
    mark_obj(15)
    write(b"15 0 obj" + LF)
    write(dest_str + LF)
    write(b"endobj" + LF)

    mark_obj(16)
    write(b"16 0 obj" + LF)
    write(dest_str + LF)
    write(b"endobj" + LF)

    # ── xref table ──
    xref_offset = len(buf)
    write(b"xref" + LF)
    write(b"0 17" + LF)
    write(b"0000000000 65535 f \r\n")
    for obj_n in range(1, 17):
        off = offsets[obj_n]
        write(f"{off:010d} 00000 n \r\n".encode())

    # ── Trailer ──
    id1 = secrets.token_hex(16)
    id2 = id1  # Oracle BI Publisher always has ID[0] == ID[1]

    write(b"trailer" + LF)
    write(b"<<" + LF)
    write(b"/Size 17" + LF)
    write(b"/Root 1 0 R" + LF)
    write(b"/Info 2 0 R" + LF)
    write(f"/ID [<{id1}><{id2}>]".encode() + LF)
    write(b">>" + LF)

    write(b"startxref" + LF)
    write(str(xref_offset).encode() + LF)
    write(b"%%EOF" + LF)

    return bytes(buf)


# ── Operation ID generation ───────────────────────────────────────────

def _generate_operation_id(prefix: str, date_str: str) -> str:
    """Generate a plausible operation ID from type prefix and date.

    prefix: 'C16' for SBP, 'C07' for alfa, 'Z09' for card, 'C82' for transgran,
            'A01' for mobile.
    date_str: 'DD.MM.YYYY'
    """
    parts = date_str.split(".")
    dd, mm = parts[0], parts[1]
    yy = parts[2][2:] if len(parts[2]) == 4 else parts[2]
    date_block = f"{dd}{mm}{yy}"
    seq = "".join(str(random.randint(0, 9)) for _ in range(7))
    return f"{prefix}{date_block}{seq}"


_OP_ID_PREFIXES = {
    "sbp": "C16",
    "alfa": "C07",
    "card": "Z09",
    "transgran": "C82",
    "mobile": "A01",
}


# ── Public API ────────────────────────────────────────────────────────

def get_fields_for_scratch(receipt_type: str) -> list[dict]:
    """Return field definitions with labels/defaults for the bot wizard UI."""
    rtype = RECEIPT_TYPES.get(receipt_type)
    if not rtype:
        raise ValueError(f"Unknown receipt type: {receipt_type}")
    result = []
    for field in rtype["left"] + rtype["right"]:
        result.append({
            "key": field["key"],
            "label": field["label"].replace("\xa0", " "),
            "prompt": field["prompt"],
            "default": field["default"],
        })
    return result


def generate_alfa_scratch(
    receipt_type: str,
    values: dict[str, str],
    *,
    donor_pdf: Optional[str | Path] = None,
    output_path: Optional[str | Path] = None,
    formed_at: Optional[str] = None,
) -> bytes:
    """Generate an Alfa Bank receipt PDF from scratch.

    Args:
        receipt_type: One of 'sbp', 'alfa', 'card', 'transgran', 'mobile'.
        values: Dict mapping field keys to string values.
        donor_pdf: Path to donor PDF (defaults to шаблоны/alfa_universal_donor.pdf).
        output_path: If given, also write the PDF to this path.
        formed_at: Override for the "Сформирована" datetime (default: now).

    Returns:
        PDF file bytes.
    """
    if receipt_type not in RECEIPT_TYPES:
        raise ValueError(f"Unknown receipt type: {receipt_type}. Use: {list(RECEIPT_TYPES)}")

    donor_path = Path(donor_pdf) if donor_pdf else DONOR_PATH
    if not donor_path.exists():
        raise FileNotFoundError(f"Donor PDF not found: {donor_path}")

    donor = _load_donor(donor_path)
    rtype = RECEIPT_TYPES[receipt_type]
    num_left = len(rtype["left"])
    _, _, _, named_dest_y = _bottom_positions(num_left)

    now = datetime.now()
    if formed_at:
        formed_text = "Сформирована"
        formed_date = formed_at.replace(" ", "\xa0")
    else:
        formed_text = "Сформирована"
        formed_date = now.strftime("%d.%m.%Y") + "\xa0" + now.strftime("%H:%M") + "\xa0мск"

    # Auto-generate operation_id if not provided but datetime is
    dt_val = values.get("datetime", "")
    if dt_val and not values.get("operation_id"):
        dt_match = re.match(r"(\d{2}\.\d{2}\.\d{4})", dt_val)
        if dt_match:
            prefix = _OP_ID_PREFIXES.get(receipt_type, "C16")
            values = dict(values)
            values["operation_id"] = _generate_operation_id(prefix, dt_match.group(1))

    required = _collect_required_chars(rtype, values, formed_text, formed_date)
    uni_to_cid = _extend_cmap(donor["uni_to_cid"], required)
    # Keep ONLY the characters actually used in this receipt — no donor leftovers.
    uni_to_cid = {cp: cid for cp, cid in uni_to_cid.items() if cp in required}

    content_raw = _build_content_stream(
        receipt_type, values, uni_to_cid, formed_text, formed_date
    )

    pdf_bytes = _assemble_pdf(donor, content_raw, uni_to_cid, named_dest_y)

    if output_path:
        Path(output_path).write_bytes(pdf_bytes)

    return pdf_bytes


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Alfa Bank PDF receipt generator (from scratch)")
    parser.add_argument("type", choices=list(RECEIPT_TYPES.keys()), help="Receipt type")
    parser.add_argument("output", help="Output PDF path")
    parser.add_argument("--donor", help="Donor PDF path (default: шаблоны/alfa_universal_donor.pdf)")
    parser.add_argument("--formed-at", help="Override 'Сформирована' date (DD.MM.YYYY HH:MM мск)")

    for rtype_name, rtype in RECEIPT_TYPES.items():
        for field in rtype["left"] + rtype["right"]:
            parser.add_argument(
                f"--{field['key'].replace('_', '-')}",
                help=f"[{rtype_name}] {field['prompt']}",
            )

    args = parser.parse_args()
    vals: dict[str, str] = {}
    rtype = RECEIPT_TYPES[args.type]
    for field in rtype["left"] + rtype["right"]:
        v = getattr(args, field["key"].replace("-", "_"), None)
        if v:
            vals[field["key"]] = v
        elif field["default"]:
            vals[field["key"]] = field["default"]

    try:
        pdf = generate_alfa_scratch(
            args.type,
            vals,
            donor_pdf=args.donor,
            output_path=args.output,
            formed_at=args.formed_at,
        )
        print(f"[OK] Generated {args.output} ({len(pdf)} bytes)")
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
