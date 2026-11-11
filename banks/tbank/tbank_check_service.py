#!/usr/bin/env python3
"""T-Bank receipt PDF patching: amount-only and full-field replacement.

Core rules (derived from VTB's proven approach):
  1. NEVER add new operators to the content stream
  2. Only replace CID bytes within existing Tj operators
  3. Only adjust Tm x-coordinates for alignment
  4. Delta-based xref patching (never rebuild from scratch)
  5. Same-length padding for /ID, dates, Keywords
  6. Extract font widths from the actual PDF at runtime
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from tbank_cmap import (
    REGULAR_UNI_TO_CID,
    MEDIUM_UNI_TO_CID,
    REGULAR_WIDTHS,
    MEDIUM_WIDTHS,
    DEFAULT_WIDTH,
    encode_text,
    decode_text,
    text_width_pt,
    get_unsupported_chars,
    escape_pdf_literal,
    unescape_pdf_literal,
    cid_advance_units,
    cid_width_pt,
    find_tj_at_coords,
    extract_pdf_font_widths,
    can_encode_in_font,
    TJ_REGEX,
    TM_REGEX,
)

BASE_DIR = Path(__file__).parent

_TBANK_DIR = BASE_DIR / "TBANK"

def _find_tbank_donor(receipt_type: str) -> Path:
    """Find a donor PDF for the given receipt type from the TBANK/ folder.

    Falls back to ~/Downloads/ paths for backward compatibility.
    """
    candidates = sorted(_TBANK_DIR.glob("*.pdf")) if _TBANK_DIR.exists() else []
    if candidates:
        return candidates[0]
    fallback = {
        "sbp": Path.home() / "Downloads" / "receipt_23.03.2026 (2).pdf",
        "card": Path.home() / "Downloads" / "receipt_23.03.2026 (1).pdf",
        "transgran": Path.home() / "Downloads" / "receipt_23.03.2026 (3).pdf",
    }
    return fallback.get(receipt_type, fallback["sbp"])

def _pick_template(receipt_type: str) -> Path:
    """Pick the best template for a receipt type, preferring verified donors."""
    from tbank_check_service import donor_keywords_ok, _is_blocked_donor

    verified = sorted(
        p
        for p in (_TBANK_DIR.glob("tbank_sbp_verified_*.pdf") if _TBANK_DIR.exists() else [])
        if "_enriched" not in p.stem and donor_keywords_ok(p)
    )
    if verified:
        import random as _random
        return _random.choice(verified)
    enriched = sorted(
        p
        for p in (_TBANK_DIR.glob("*_enriched.pdf") if _TBANK_DIR.exists() else [])
        if not _is_blocked_donor(p) and donor_keywords_ok(p)
    )
    if enriched:
        import random as _random
        return _random.choice(enriched)
    fallback = _TBANK_DIR / "tbank_sbp_verified_12052026.pdf"
    return fallback


TEMPLATES = {
    "sbp": _TBANK_DIR / "receipt_sbp_1.pdf",
    "card": _TBANK_DIR / "receipt_sbp_1.pdf",
    "transgran": _TBANK_DIR / "receipt_sbp_1.pdf",
}

# ── Receipt subtypes and their field layouts ──────────────────────────

SBP_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 432.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left",  "tol_x": 8.0},
    # tol_x=50: amount can shift left significantly for long amounts (e.g. '11 300 ' from '1 200 ' donor)
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 412.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "type_label",   "label": "Тип перевода",         "y": 376.78, "x": 172.28, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "status",       "label": "Статус",               "y": 356.78, "x": 216.57, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "amount_small", "label": "Сумма",                "y": 336.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 14},
    {"key": "commission",   "label": "Комиссия",             "y": 315.78, "x": 198.1,  "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "sender",       "label": "Отправитель",          "y": 295.78, "x": 170.02, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "phone",        "label": "Телефон получателя",   "y": 275.78, "x": 178.72, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "receiver",     "label": "Получатель",           "y": 255.78, "x": 216.55, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "bank",         "label": "Банк получателя",      "y": 235.78, "x": 204.83, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "account",      "label": "Счет списания",        "y": 215.78, "x": 160.95, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "ident",        "label": "Идентификатор",        "y": 195.78, "x": 123.46, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
]

# Компактный вариант (нет банка/счёта/идентификатора): дата Y=364.54, Итого Y=344.39
SBP_SHORT_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 364.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left",  "tol_x": 8.0},
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 344.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "type_label",   "label": "Тип перевода",         "y": 308.78, "x": 172.28, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "status",       "label": "Статус",               "y": 288.78, "x": 216.57, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "amount_small", "label": "Сумма",                "y": 268.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 14},
    {"key": "commission",   "label": "Комиссия",             "y": 247.78, "x": 198.1,  "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "sender",       "label": "Отправитель",          "y": 227.78, "x": 188.54, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "phone",        "label": "Телефон получателя",   "y": 207.78, "x": 178.72, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "receiver",     "label": "Получатель",           "y": 187.78, "x": 216.55, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
]

# Длинный вариант (+банк получателя, +счёт, +идентификатор): дата Y=454.54, Итого Y=434.39
SBP_LONG_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 454.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left",  "tol_x": 8.0},
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 434.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "type_label",   "label": "Тип перевода",         "y": 398.78, "x": 172.28, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "status",       "label": "Статус",               "y": 378.78, "x": 216.57, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "amount_small", "label": "Сумма",                "y": 358.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 14},
    {"key": "commission",   "label": "Комиссия",             "y": 337.78, "x": 198.1,  "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "sender",       "label": "Отправитель",          "y": 317.78, "x": 188.54, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "phone",        "label": "Телефон получателя",   "y": 297.78, "x": 178.72, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "receiver",     "label": "Получатель",           "y": 277.78, "x": 216.55, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "bank",         "label": "Банк получателя",      "y": 257.78, "x": 204.83, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "account",      "label": "Счет списания",        "y": 237.78, "x": 160.95, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "ident",        "label": "Идентификатор",        "y": 217.78, "x": 123.46, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
]

CARD_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 324.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left"},
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 304.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "status",       "label": "Статус",               "y": 248.78, "x": 216.57, "font": "F1", "size": 9,  "align": "right"},
    {"key": "amount_small", "label": "Сумма",                "y": 228.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 14},
    {"key": "sender",       "label": "Отправитель",          "y": 207.78, "x": 188.54, "font": "F1", "size": 9,  "align": "right"},
    {"key": "card_to",      "label": "Карта получателя",     "y": 187.78, "x": 180.88, "font": "F1", "size": 9,  "align": "right"},
]

TRANSGRAN_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 404.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left"},
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 384.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "status",       "label": "Статус",               "y": 328.78, "x": 204.07, "font": "F1", "size": 9,  "align": "right"},
    {"key": "amount_small", "label": "Сумма",                "y": 308.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 14},
    {"key": "commission",   "label": "Комиссия",             "y": 287.78, "x": 198.1,  "font": "F1", "size": 9,  "align": "right"},
    {"key": "sender",       "label": "Отправитель",          "y": 267.78, "x": 188.54, "font": "F1", "size": 9,  "align": "right"},
    {"key": "phone",        "label": "Телефон получателя",   "y": 247.78, "x": 175.9,  "font": "F1", "size": 9,  "align": "right"},
    {"key": "receiver",     "label": "Получатель",           "y": 227.78, "x": 208.69, "font": "F1", "size": 9,  "align": "right"},
    {"key": "credited_amt", "label": "Сумма зачисления",     "y": 187.78, "x": 233.57, "font": "F1", "size": 9,  "align": "right"},
]

# Полный transgran, но вся вёрстка на −20 pt (часто MediaBox h≈471): сумма жирная y≈364.39
TRANSGRAN_SHIFTED_FIELDS: list[dict] = []
for row in TRANSGRAN_FIELDS:
    r = {**row, "y": row["y"] - 20.0}
    if r["key"] == "amount_bold":
        r["tol_x"] = max(float(r.get("tol_x", 8.0)), 50.0)
    elif r["key"] == "amount_small":
        r["tol_x"] = max(float(r.get("tol_x", 8.0)), 22.0)
    TRANSGRAN_SHIFTED_FIELDS.append(r)

# Короткий вариант transgran (MediaBox h≈451): все Y сдвинуты на -40pt
TRANSGRAN_SHORT_FIELDS = [
    {"key": "datetime",     "label": "Дата и время",         "y": 364.54, "x": 20.0,   "font": "F1", "size": 8,  "align": "left",  "tol_x": 8.0},
    # tol_x=50: маленькие суммы (напр. «20 ») после замены на «20 000 » сдвигают x на ~31pt
    {"key": "amount_bold",  "label": "Сумма (жирная)",       "y": 344.39, "x": 217.3,  "font": "F2", "size": 16, "align": "right", "tol_x": 50},
    {"key": "status",       "label": "Статус",               "y": 288.78, "x": 204.07, "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "amount_small", "label": "Сумма",                "y": 268.78, "x": 232.76, "font": "F1", "size": 9,  "align": "right", "tol_x": 22},
    {"key": "commission",   "label": "Комиссия",             "y": 247.78, "x": 198.1,  "font": "F1", "size": 9,  "align": "right", "tol_x": 8.0},
    {"key": "sender",       "label": "Отправитель",          "y": 227.78, "x": 188.54, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "phone",        "label": "Телефон получателя",   "y": 207.78, "x": 175.9,  "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "receiver",     "label": "Получатель",           "y": 187.78, "x": 208.69, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
    {"key": "credited_amt", "label": "Сумма зачисления",     "y": 147.78, "x": 233.57, "font": "F1", "size": 9,  "align": "right", "tol_x": 60.0},
]

# Короче transgran_short (MediaBox h≈431): все Y на −20 pt относительно макета h≈451.
# У крупных сумм (напр. «12 500») жирная строка чуть левее эталона — расширяем tol_x.
TRANSGRAN_SHORT_COMPACT_FIELDS: list[dict] = []
for row in TRANSGRAN_SHORT_FIELDS:
    r = {**row, "y": row["y"] - 20.0}
    if r["key"] == "amount_bold":
        r["tol_x"] = max(float(r.get("tol_x", 8.0)), 36.0)
    elif r["key"] == "amount_small":
        r["tol_x"] = max(float(r.get("tol_x", 8.0)), 18.0)
    TRANSGRAN_SHORT_COMPACT_FIELDS.append(r)

FIELDS_BY_TYPE = {
    "sbp": SBP_FIELDS,
    "sbp_short": SBP_SHORT_FIELDS,
    "sbp_long": SBP_LONG_FIELDS,
    "card": CARD_FIELDS,
    "transgran": TRANSGRAN_FIELDS,
    "transgran_shifted": TRANSGRAN_SHIFTED_FIELDS,
    "transgran_short": TRANSGRAN_SHORT_FIELDS,
    "transgran_short_compact": TRANSGRAN_SHORT_COMPACT_FIELDS,
}

RECEIPT_TYPES = (
    "sbp", "sbp_short", "sbp_long",
    "card",
    "transgran", "transgran_shifted", "transgran_short", "transgran_short_compact",
)

RECEIPT_TYPE_LABELS: dict[str, str] = {
    "sbp":              "По номеру телефона (СБП)",
    "sbp_short":        "По номеру телефона (СБП)",
    "sbp_long":         "По номеру телефона (СБП)",
    "card":             "По карте",
    "transgran":        "Трансграничный",
    "transgran_shifted": "Трансграничный",
    "transgran_short":  "Трансграничный",
    "transgran_short_compact": "Трансграничный",
}


def _get_field_labels(receipt_type: str) -> list[dict]:
    return FIELDS_BY_TYPE.get(receipt_type, SBP_FIELDS)


# ── Low-level PDF helpers (VTB-style) ─────────────────────────────────

def _find_page_stream(pdf_bytes: bytes) -> tuple[int, int, int, bytes]:
    """Find the page content stream using VTB-style regex.

    Returns (len_num_start, stream_data_start, compressed_len, decompressed).
    len_num_start = byte offset of the /Length number digits in the file.
    """
    for m in re.finditer(
        rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL
    ):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(pdf_bytes):
            continue
        try:
            dec = zlib.decompress(
                pdf_bytes[stream_start : stream_start + stream_len]
            )
        except Exception:
            continue
        if b"BT" not in dec:
            continue
        return len_num_start, stream_start, stream_len, dec
    raise ValueError("No BT content stream found in PDF")


def _detect_transgran_field_layout(pdf_bytes: bytes) -> str:
    """При h 461–509 выбрать «полный» transgran или сдвиг −20 pt (часто h≈471)."""
    try:
        _, _, _, dec = _find_page_stream(pdf_bytes)
    except ValueError:
        return "transgran"
    for fld in TRANSGRAN_FIELDS:
        if fld["key"] != "amount_bold":
            continue
        if find_tj_at_coords(
            dec,
            fld["y"],
            fld["x"],
            tol_y=float(fld.get("tol_y", 1.5)),
            tol_x=float(fld.get("tol_x", 28.0)),
        ):
            return "transgran"
    for fld in TRANSGRAN_SHIFTED_FIELDS:
        if fld["key"] != "amount_bold":
            continue
        if find_tj_at_coords(
            dec,
            fld["y"],
            fld["x"],
            tol_y=float(fld.get("tol_y", 1.5)),
            tol_x=float(fld.get("tol_x", 50.0)),
        ):
            return "transgran_shifted"
    return "transgran"


def _recompress_zero_delta(
    pdf_bytes: bytes,
    stream_start: int,
    old_stream_len: int,
    new_decompressed: bytes,
    len_num_start: int = 0,
) -> bytes:
    """Recompress content stream keeping EXACTLY the same compressed size.

    Tries multiple zlib levels/strategies and minimal padding to match the
    original compressed size. This preserves /Length, xref, and startxref —
    nothing shifts, so the PDF passes integrity checks.

    Genuine T-Bank receipts have only 1-2 trailing newlines. We strip any
    existing trailing whitespace and try MANY zlib variants before resorting
    to large padding, so the output looks indistinguishable from genuine.
    """
    target = old_stream_len

    # Strip existing trailing whitespace — we'll add minimal padding ourselves
    new_decompressed = new_decompressed.rstrip(b"\n\r \t") + b"\n"

    # Genuine T-Bank JasperReports PDFs always use zlib level 6, header 789c.
    # We MUST match this — external integrity checkers verify the zlib header.
    # Strategy: try ONLY level 6 first with increasing padding, then other levels
    # only as a last resort.

    def _try_level6(data: bytes) -> bytes | None:
        """Try all zlib strategies at level 6 only (header 789c)."""
        for strategy in (
            zlib.Z_DEFAULT_STRATEGY,
            zlib.Z_FILTERED,
            zlib.Z_RLE,
            zlib.Z_FIXED,
        ):
            for mem in (8, 9, 7, 6, 5, 4):
                co = zlib.compressobj(6, zlib.DEFLATED, 15, mem, strategy)
                c = co.compress(data) + co.flush()
                if len(c) == target:
                    return c
        return None

    def _try_any_level(data: bytes) -> bytes | None:
        """Fallback: try all levels (including non-6)."""
        for level in (7, 8, 9, 5, 4, 3, 2, 1):
            for strategy in (
                zlib.Z_DEFAULT_STRATEGY,
                zlib.Z_FILTERED,
                zlib.Z_HUFFMAN_ONLY,
                zlib.Z_RLE,
                zlib.Z_FIXED,
            ):
                for mem in (8, 9, 7, 6, 5, 4):
                    co = zlib.compressobj(level, zlib.DEFLATED, 15, mem, strategy)
                    c = co.compress(data) + co.flush()
                    if len(c) == target:
                        return c
        return None

    def _patch(compressed: bytes) -> bytes:
        data = bytearray(pdf_bytes)
        data[stream_start : stream_start + old_stream_len] = compressed
        return bytes(data)

    # Phase 1: level 6 only, no padding
    exact = _try_level6(new_decompressed)
    if exact:
        return _patch(exact)

    # Phase 2: level 6 with minimal padding (up to MAX_SAFE_PAD bytes).
    # Genuine T-Bank receipts end with exactly 1 trailing '\n'. Any padding
    # we add is detectable — so we cap at MAX_SAFE_PAD and bail out rather
    # than producing a receipt with hundreds of visible trailing newlines.
    MAX_SAFE_PAD = 8
    for pad_n in range(1, MAX_SAFE_PAD + 1):
        for pc in (b"\n", b" "):
            exact = _try_level6(new_decompressed + pc * pad_n)
            if exact:
                return _patch(exact)

    # Phase 3: any level (still minimal padding ≤ MAX_SAFE_PAD)
    # This keeps the decompressed content clean even if zlib header differs.
    exact = _try_any_level(new_decompressed)
    if exact:
        return _patch(exact)
    for pad_n in range(1, MAX_SAFE_PAD + 1):
        for pc in (b"\n", b" "):
            exact = _try_any_level(new_decompressed + pc * pad_n)
            if exact:
                return _patch(exact)

    # Fallback: delta-based patching (changes /Length, xref, startxref).
    # Preferable to large padding that reveals tampering.
    return _recompress_and_fix(
        pdf_bytes, len_num_start, stream_start, old_stream_len, new_decompressed
    )


def _recompress_and_fix(
    pdf_bytes: bytes,
    len_num_start: int,
    stream_start: int,
    old_stream_len: int,
    new_decompressed: bytes,
) -> bytes:
    """Recompress content stream and fix /Length, xref, startxref.

    Delta-based patching of xref entries. Used as fallback when
    zero-delta padding cannot match the original compressed size.
    """
    new_compressed = zlib.compress(new_decompressed, 6)
    delta = len(new_compressed) - old_stream_len

    data = bytearray(pdf_bytes)

    # 1. Replace stream data
    data[stream_start : stream_start + old_stream_len] = new_compressed

    # 2. Update /Length digits
    old_len_str = str(old_stream_len).encode()
    new_len_str = str(len(new_compressed)).encode()
    num_end = len_num_start + len(old_len_str)
    data[len_num_start:num_end] = new_len_str
    if len(new_len_str) != len(old_len_str):
        delta += len(new_len_str) - len(old_len_str)

    # 3. Patch xref entries with offset > stream_start
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(
            rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries
        ):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = (
                    f"{offset + delta:010d}".encode()
                )
        data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

    # 4. Patch startxref
    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if startxref_m and delta != 0:
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        old_pos_str = startxref_m.group(1)
        new_pos_str = str(old_pos + delta).encode()
        data[pos : pos + len(old_pos_str)] = new_pos_str

    return bytes(data)


def _format_amount_str(amount: float) -> str:
    """Format amount: 10 → '10', 1500 → '1 500', 1500.50 → '1 500.50'."""
    if amount == int(amount):
        n = int(amount)
        s = f"{n:,}".replace(",", " ")
    else:
        integer_part = int(amount)
        decimal_part = f"{amount:.2f}".split(".")[1]
        s = f"{integer_part:,}".replace(",", " ") + "." + decimal_part
    return s


# ── Stream replacement engine ─────────────────────────────────────────

def _replace_field_bytes(
    stream: bytes,
    target_y: float,
    target_x: float,
    new_text: str,
    font_size: float,
    right_aligned: bool = True,
    widths: dict[int, int] | None = None,
    font: str = "regular",
    tol_y: float = 1.5,
    tol_x: float = 8.0,
) -> bytes:
    """Replace a single Tj field at given coordinates.

    Only replaces CID bytes inside the existing Tj and adjusts the Tm
    x-coordinate for right-aligned fields. NEVER adds new operators.
    """
    if widths is None:
        widths = REGULAR_WIDTHS if font == "regular" else MEDIUM_WIDTHS

    found = find_tj_at_coords(
        stream, target_y, target_x, tol_y=tol_y, tol_x=tol_x
    )
    if found is None:
        return stream
    old_raw, tj_start, tj_end = found

    chunk = stream[:tj_start]
    tm_matches = list(re.finditer(TM_REGEX, chunk))
    anchor_x = float(tm_matches[-1].group(1)) if tm_matches else target_x

    new_raw = encode_text(new_text, font)
    new_escaped = escape_pdf_literal(new_raw)
    new_tj = b"(" + new_escaped + b")Tj"

    if right_aligned:
        old_width = cid_width_pt(old_raw, font_size, widths)
        new_width = cid_width_pt(new_raw, font_size, widths)
        wall = anchor_x + old_width
        new_x = wall - new_width

        if tm_matches:
            tm_m = tm_matches[-1]
            y_str = tm_m.group(2).decode()
            new_tm = f"1 0 0 1 {new_x:.2f} {y_str} Tm".encode()
            delta = len(new_tm) - (tm_m.end() - tm_m.start())
            stream = stream[: tm_m.start()] + new_tm + stream[tm_m.end() :]
            tj_start += delta
            tj_end += delta

    stream = stream[:tj_start] + new_tj + stream[tj_end:]
    return stream


def _find_nth_tj_at_y(
    stream: bytes,
    target_y: float,
    n: int,
    tol_y: float = 1.5,
) -> tuple[bytes, int, int] | None:
    """Find the Nth (0-based) Tj at a given Y coordinate."""
    tms = list(re.finditer(TM_REGEX, stream))
    tjs = list(re.finditer(TJ_REGEX, stream))
    count = 0
    for tj in tjs:
        closest_tm = None
        for tm in tms:
            if tm.end() <= tj.start():
                closest_tm = tm
            else:
                break
        if closest_tm is None:
            continue
        y = float(closest_tm.group(2))
        if abs(y - target_y) < tol_y:
            if count == n:
                raw = unescape_pdf_literal(tj.group(1))
                return raw, tj.start(), tj.end()
            count += 1
    return None


def _replace_nth_tj_at_y(
    stream: bytes,
    target_y: float,
    n: int,
    new_text: str,
    font: str = "regular",
    tol_y: float = 1.5,
) -> bytes:
    """Replace the Nth (0-based) Tj at a given Y coordinate."""
    found = _find_nth_tj_at_y(stream, target_y, n, tol_y)
    if found is None:
        return stream
    _, tj_start, tj_end = found
    new_raw = encode_text(new_text, font)
    new_escaped = escape_pdf_literal(new_raw)
    new_tj = b"(" + new_escaped + b")Tj"
    return stream[:tj_start] + new_tj + stream[tj_end:]


TJ_ARRAY_REGEX = rb"\[((?:[^\]]*?))\]TJ"


def _replace_tj_array_at_coords(
    stream: bytes,
    target_y: float,
    target_x: float,
    new_text: str,
    font: str = "regular",
    tol_y: float = 2.0,
    tol_x: float = 10.0,
) -> bytes:
    """Replace a TJ array at specific coordinates with new text."""
    tms = list(re.finditer(TM_REGEX, stream))
    tjs = list(re.finditer(TJ_ARRAY_REGEX, stream))
    for tj in tjs:
        closest_tm = None
        for tm in tms:
            if tm.end() <= tj.start():
                closest_tm = tm
            else:
                break
        if closest_tm is None:
            continue
        x = float(closest_tm.group(1))
        y = float(closest_tm.group(2))
        if abs(y - target_y) < tol_y and abs(x - target_x) < tol_x:
            new_raw = encode_text(new_text, font)
            new_escaped = escape_pdf_literal(new_raw)
            new_array = b"[(" + new_escaped + b")]TJ"
            return stream[: tj.start()] + new_array + stream[tj.end() :]
    return stream


def get_renderable_chars(pdf_bytes: bytes, font: str = "regular") -> set[int] | None:
    """Return set of Unicode code points that have actual glyph outlines in the PDF font.

    font: "regular" (F1/TinkoffSans-Regular) or "medium" (F2/TinkoffSans-Medium).
    Returns None if the font stream cannot be parsed (caller should skip the check).
    """
    try:
        from fontTools.ttLib import TTFont as _TTFont
        from io import BytesIO as _BytesIO
        import zlib as _zlib
        from tbank_cmap import REGULAR_UNI_TO_CID, MEDIUM_UNI_TO_CID

        name_fragment = "Regular" if font == "regular" else "Medium"
        uni_to_cid = REGULAR_UNI_TO_CID if font == "regular" else MEDIUM_UNI_TO_CID

        # Find FontDescriptor with the matching name and get its FontFile2 stream
        stream_bytes: bytes | None = None
        for m in re.finditer(rb"(\d+)\s+0\s+obj", pdf_bytes):
            chunk = pdf_bytes[m.start() : m.start() + 600]
            if b"FontDescriptor" not in chunk or b"FontName" not in chunk:
                continue
            fn_m = re.search(rb"/FontName/([^\s/\]>]+)", chunk)
            if not fn_m or name_fragment.encode() not in fn_m.group(1):
                continue
            ff2_m = re.search(rb"/FontFile2\s+(\d+)\s+0\s+R", chunk)
            if not ff2_m:
                continue
            stream_obj = int(ff2_m.group(1))

            pat = rb"(" + str(stream_obj).encode() + rb")\s+0\s+obj\s*<<"
            for sm in re.finditer(pat, pdf_bytes):
                if int(sm.group(1)) != stream_obj:
                    continue
                schunk = pdf_bytes[sm.end() : sm.end() + 500]
                len_m = re.search(rb"/Length\s+(\d+)", schunk)
                end_m = re.search(rb">>\s*stream\r?\n", schunk)
                if not len_m or not end_m:
                    continue
                slen = int(len_m.group(1))
                sstart = sm.end() + end_m.end()
                raw = pdf_bytes[sstart : sstart + slen]
                try:
                    stream_bytes = _zlib.decompress(raw)
                except _zlib.error:
                    continue
                break
            if stream_bytes:
                break

        if stream_bytes is None:
            return None

        tt = _TTFont(_BytesIO(stream_bytes))
        glyf_table = tt["glyf"]
        order = tt.getGlyphOrder()

        # Build set of GIDs that have non-empty glyph outlines
        non_empty_gids: set[int] = set()
        for gid, name in enumerate(order):
            g = glyf_table[name]
            if g.numberOfContours != 0:
                non_empty_gids.add(gid)
            elif hasattr(g, "components") and g.components:
                non_empty_gids.add(gid)
        tt.close()

        # Map Unicode → CID (= GID for Identity mapping), filter to non-empty
        renderable: set[int] = set()
        for uni, cid in uni_to_cid.items():
            if cid in non_empty_gids:
                renderable.add(uni)
        return renderable

    except Exception:
        return None


def _replace_all_fields_in_stream(
    stream: bytes,
    fields: list[dict],
    changes: dict[str, str],
    f1_widths: dict[int, int] | None = None,
    f2_widths: dict[int, int] | None = None,
    f1_renderable: set[int] | None = None,
    f2_renderable: set[int] | None = None,
) -> bytes:
    """Apply field replacements to a decompressed content stream.

    For F2 fields: checks if the new text can be encoded in the F2 font
    subset. If not, the field is SKIPPED (left unchanged).

    f1_renderable / f2_renderable: sets of Unicode code points with actual
    glyph outlines in the embedded font. If provided, fields with characters
    missing from the glyph set are skipped (prevents invisible text).
    """
    if f1_widths is None:
        f1_widths = REGULAR_WIDTHS
    if f2_widths is None:
        f2_widths = MEDIUM_WIDTHS

    for field in fields:
        key = field["key"]
        if key not in changes:
            continue
        new_val = changes[key]
        if not new_val:
            continue

        is_f2 = field["font"] == "F2"
        font_type = "medium" if is_f2 else "regular"
        widths = f2_widths if is_f2 else f1_widths

        if is_f2:
            # Use hardcoded MEDIUM_WIDTHS for the encodability check (extracted /W
            # table from the PDF is often a small subset and gives false negatives).
            if not can_encode_in_font(new_val, "medium", MEDIUM_WIDTHS):
                continue
            # Skip glyph-outline check for F2 (Medium) — the embedded font subset
            # only has glyphs from the donor's original amount (e.g. "250"), so a
            # new amount with different digits (e.g. "13 000") would fail the check
            # even though TinkoffSans Medium has all digits. We trust MEDIUM_UNI_TO_CID.
        else:
            # Skip field if any character lacks a glyph outline in the embedded font
            if f1_renderable is not None:
                missing_glyphs = [
                    ch for ch in new_val if ch != " " and ord(ch) not in f1_renderable
                ]
                if missing_glyphs:
                    continue

        # For F2 (Medium) fields always use MEDIUM_WIDTHS — the extracted /W table
        # from the donor PDF is a small subset (only the donor's original amount digits)
        # and produces wrong right-alignment for new amounts with different digits.
        effective_widths = MEDIUM_WIDTHS if is_f2 else widths

        stream = _replace_field_bytes(
            stream,
            target_y=field["y"],
            target_x=field["x"],
            new_text=new_val,
            font_size=field["size"],
            right_aligned=(field.get("align", "right") == "right"),
            widths=effective_widths,
            font=font_type,
            tol_y=field.get("tol_y", 1.5),
            tol_x=field.get("tol_x", 8.0),
        )
    return stream


# ── Metadata helpers (same-length padding, like VTB) ──────────────────

def _parse_keywords(pdf_bytes: bytes) -> tuple[str, str, str] | None:
    """Return (timestamp, hash, suffix) from /Keywords, or None."""
    kw_m = re.search(rb"/Keywords\(([^)]+)\)", pdf_bytes)
    if not kw_m:
        return None
    parts = kw_m.group(1).decode("latin-1").strip().split(" | ")
    if len(parts) < 3:
        return None
    return parts[0], parts[1], parts[2]


def _normalize_dt_str(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _is_blocked_donor(pdf_path: Path) -> bool:
    """Donors that fail external verification after patching."""
    if pdf_path.name.startswith("receipt_sbp_"):
        return True
    try:
        parsed = _parse_keywords(pdf_path.read_bytes())
    except Exception:
        return True
    if not parsed:
        return True
    return parsed[2] == "10893"


def donor_keywords_ok(pdf_path: str | Path) -> bool:
    """True if donor /Keywords match genuine T-Bank receipts that pass verification.

    Accepted:
      - UUID hash + suffix 10817/9686 (newer genuine receipts)
      - md5 hash + suffix 991 (older genuine receipts, e.g. receipt_05.05.2026)
    Rejected:
      - suffix 10893 (receipt_sbp_* templates — always fail verification)
    """
    pdf_path = Path(pdf_path)
    if _is_blocked_donor(pdf_path):
        return False
    raw = pdf_path.read_bytes()
    parsed = _parse_keywords(raw)
    if not parsed:
        return False

    _kw_ts, hash_part, suffix = parsed
    if "-" in hash_part and suffix in ("10817", "9686"):
        return True
    if "-" not in hash_part and suffix == "991":
        return True
    return False


def _update_keywords_ts_only(pdf_bytes: bytes, dt: Optional[datetime] = None) -> bytes:
    """Update ONLY the timestamp in /Keywords, keeping hash and suffix unchanged.

    The external integrity verifier checks that the Keywords timestamp matches
    the operation date shown in the receipt content. We must sync this even in
    preserve_integrity mode; everything else (hash, suffix, /ID, CreationDate)
    stays untouched so the file structure is preserved byte-for-byte.
    """
    dt = dt or datetime.now()
    data = bytearray(pdf_bytes)
    kw_m = re.search(rb"/Keywords\(([^)]+)\)", bytes(data))
    if not kw_m:
        return bytes(data)
    old_kw_bytes = kw_m.group(1)
    old_kw = old_kw_bytes.decode("latin-1")
    parts = old_kw.split(" | ")
    if len(parts) >= 3:
        ts = dt.strftime("%d.%m.%Y %H:%M:%S")
        new_kw = f"{ts} | {parts[1]} | {parts[2]}"
        new_kw_bytes = new_kw.encode("latin-1")
        padded = new_kw_bytes[: len(old_kw_bytes)].ljust(len(old_kw_bytes))
        data[kw_m.start(1) : kw_m.end(1)] = padded
    return bytes(data)


def _update_keywords(pdf_bytes: bytes, dt: Optional[datetime] = None) -> bytes:
    """Update /Keywords with new timestamp and hash, preserving byte length."""
    dt = dt or datetime.now()
    data = bytearray(pdf_bytes)
    kw_m = re.search(rb"/Keywords\(([^)]+)\)", bytes(data))
    if not kw_m:
        return bytes(data)
    old_kw_bytes = kw_m.group(1)
    old_kw = old_kw_bytes.decode("latin-1")
    parts = old_kw.split(" | ")
    if len(parts) >= 3:
        ts = dt.strftime("%d.%m.%Y %H:%M:%S")
        if "-" in parts[1]:
            new_hash = str(uuid.uuid4())
        else:
            new_hash = hashlib.md5(f"{ts}_{parts[2]}".encode()).hexdigest()
        new_kw = f"{ts} | {new_hash} | {parts[2]}"
        new_kw_bytes = new_kw.encode("latin-1")
        padded = new_kw_bytes[: len(old_kw_bytes)].ljust(len(old_kw_bytes))
        data[kw_m.start(1) : kw_m.end(1)] = padded
    return bytes(data)


def _update_dates(pdf_bytes: bytes, dt: Optional[datetime] = None) -> bytes:
    """Update /CreationDate and /ModDate, preserving byte length."""
    dt = dt or datetime.now()
    tz_pdf = "+03'00'"
    date_str = dt.strftime("D:%Y%m%d%H%M%S") + tz_pdf
    date_bytes = date_str.encode()

    data = bytearray(pdf_bytes)
    for tag in (b"/CreationDate", b"/ModDate"):
        m = re.search(tag + rb"\(([^)]+)\)", bytes(data))
        if m:
            old_val = m.group(1)
            new_val = date_bytes[: len(old_val)].ljust(len(old_val))
            data[m.start(1) : m.end(1)] = new_val
    return bytes(data)


def _update_doc_id(pdf_bytes: bytes) -> bytes:
    """Update /ID: keep ID[0] (permanent), regenerate only ID[1] (modification).

    Real T-Bank PDFs always have two DIFFERENT hex IDs. Setting both to the
    same hash is a telltale sign of modification.
    """
    data = bytearray(pdf_bytes)
    id_m = re.search(
        rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]", bytes(data)
    )
    if id_m:
        id1_orig = id_m.group(1)
        id2_orig = id_m.group(2)
        new_h = hashlib.md5(bytes(data)).hexdigest()
        new_id2 = new_h[:len(id2_orig)].encode()
        if len(new_id2) < len(id2_orig):
            new_id2 = new_id2.ljust(len(id2_orig), b"0")
        data[id_m.start(2) : id_m.end(2)] = new_id2
    return bytes(data)


# ── Extraction: read current field values ─────────────────────────────

def extract_fields(
    pdf_path: str | Path, receipt_type: str = "sbp"
) -> dict[str, str]:
    """Extract text values of known fields from a T-Bank receipt PDF."""
    pdf_bytes = Path(pdf_path).read_bytes()
    _, _, _, stream = _find_page_stream(pdf_bytes)

    fields = _get_field_labels(receipt_type)
    result = {}
    for field in fields:
        found = find_tj_at_coords(
            stream,
            field["y"],
            field["x"],
            tol_y=field.get("tol_y", 1.5),
            tol_x=field.get("tol_x", 8.0),
        )
        if found:
            raw_cid, _, _ = found
            font_type = "medium" if field["font"] == "F2" else "regular"
            decoded = decode_text(raw_cid, font_type)
            result[field["key"]] = decoded
    return result


# ── Amount-only patching ──────────────────────────────────────────────

def patch_amount(
    pdf_path: str | Path,
    new_amount: float,
    receipt_type: str = "sbp",
    output_path: Optional[str | Path] = None,
) -> bytes:
    """Replace only the amount fields in a T-Bank receipt."""
    pdf_bytes = Path(pdf_path).read_bytes()
    f1_widths, f2_widths = extract_pdf_font_widths(pdf_bytes)
    len_num_start, stream_start, stream_len, decompressed = _find_page_stream(
        pdf_bytes
    )

    # Amount fields use digits only — check glyph availability for F2
    f2_renderable = get_renderable_chars(pdf_bytes, "medium")

    amount_str = _format_amount_str(new_amount) + " "
    fields = _get_field_labels(receipt_type)
    changes = {}
    for field in fields:
        if field["key"] in ("amount_bold", "amount_small"):
            changes[field["key"]] = amount_str

    new_stream = _replace_all_fields_in_stream(
        decompressed, fields, changes, f1_widths, f2_widths,
        f1_renderable=None, f2_renderable=f2_renderable,
    )

    pdf_bytes = _recompress_zero_delta(
        pdf_bytes, stream_start, stream_len, new_stream, len_num_start=len_num_start
    )
    if output_path:
        Path(output_path).write_bytes(pdf_bytes)
    return pdf_bytes


# ── Full-field patching ───────────────────────────────────────────────

def patch_all_fields(
    pdf_path: str | Path,
    changes: dict[str, str],
    receipt_type: str = "sbp",
    output_path: Optional[str | Path] = None,
) -> bytes:
    """Replace multiple fields in a T-Bank receipt."""
    pdf_bytes = Path(pdf_path).read_bytes()
    f1_widths, f2_widths = extract_pdf_font_widths(pdf_bytes)
    len_num_start, stream_start, stream_len, decompressed = _find_page_stream(
        pdf_bytes
    )

    fields = _get_field_labels(receipt_type)

    for key, val in changes.items():
        field_def = next((f for f in fields if f["key"] == key), None)
        if field_def and field_def["font"] == "F2":
            continue
        bad = get_unsupported_chars(val, "regular")
        if bad:
            from tbank_cmap import format_unsupported_error

            raise ValueError(format_unsupported_error(bad))

    # Check glyph availability to skip fields that would render as invisible text
    f1_renderable = get_renderable_chars(pdf_bytes, "regular")
    f2_renderable = get_renderable_chars(pdf_bytes, "medium")

    new_stream = _replace_all_fields_in_stream(
        decompressed, fields, changes, f1_widths, f2_widths,
        f1_renderable=f1_renderable, f2_renderable=f2_renderable,
    )

    pdf_bytes = _recompress_zero_delta(
        pdf_bytes, stream_start, stream_len, new_stream, len_num_start=len_num_start
    )
    if output_path:
        Path(output_path).write_bytes(pdf_bytes)
    return pdf_bytes


# ── Convenience ───────────────────────────────────────────────────────

def patch_amount_only(
    pdf_path: str | Path,
    new_amount: float,
    receipt_type: str = "sbp",
    output_path: Optional[str | Path] = None,
) -> bytes:
    """User-facing: patch just the amount (both bold and small)."""
    return patch_amount(pdf_path, new_amount, receipt_type, output_path)


def _is_sbp_content(dec: bytes) -> bool:
    """Return True if content stream looks like SBP receipt (has commission/phone fields)."""
    for m in re.finditer(TJ_REGEX, dec):
        raw = unescape_pdf_literal(m.group(1))
        for font in ("regular", "medium"):
            text = decode_text(raw, font)
            if any(s in text for s in ("Комиссия", "Телефон", "По номеру", "Отправитель")):
                return True
    return False


def detect_receipt_type(pdf_path: str | Path) -> str:
    """Detect T-Bank receipt subtype using MediaBox height + content stream."""
    data = Path(pdf_path).read_bytes()
    mb_m = re.search(
        rb"/MediaBox\s*\[\s*\d+\s+\d+\s+(\d+)\s+(\d+)\s*\]", data
    )
    if mb_m:
        height = int(mb_m.group(2))
        if height <= 420:
            return "card"
        if height >= 540:
            # h≥540: longer SBP with extra fields (bank, account, ident)
            return "sbp_long"
        if height >= 510:
            return "sbp"
        # h≈451 — could be SBP_SHORT or transgran_short; h≈431 → transgran_short_compact
        if height <= 460:
            if height < 441:
                return "transgran_short_compact"
            # Distinguish SBP_SHORT from transgran_short by content
            try:
                _, _, _, dec = _find_page_stream(data)
                if _is_sbp_content(dec):
                    return "sbp_short"
            except Exception:
                pass
            return "transgran_short"
        if height < 510:
            return _detect_transgran_field_layout(data)
    return "sbp"


def is_tbank_pdf(pdf_path: str | Path) -> bool:
    """Check if a PDF is a T-Bank receipt."""
    data = Path(pdf_path).read_bytes()
    return (
        b"OpenPDF" in data
        and b"JasperReports" in data
        and (
            b"TinkoffSans" in data
            or b"tbank.ru" in data
            or b"TBANK" in data
        )
    )
