#!/usr/bin/env python3
"""T-Bank statement PDF patching and generation.

Template: «Справка о движении средств.pdf» (A4, 595×842)
Blocks:
  1 — Header: ФИО, дата формирования, Исх. №
  2 — О продукте: дата договора, номер договора, номер счёта
  3 — Операции: 3 lines (карта, СБП, пополнение)
  4 — Итог: Пополнения, Расходы (auto-calculated)

Follows VTB rules: no new operators, CID-only replacement, delta xref.
"""
from __future__ import annotations

import hashlib
import re
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from tbank_cmap import (
    encode_text,
    decode_text,
    get_unsupported_chars,
    format_unsupported_error,
    text_width_pt,
    escape_pdf_literal,
    unescape_pdf_literal,
    find_tj_at_coords,
    extract_pdf_font_widths,
    can_encode_in_font,
    REGULAR_UNI_TO_CID,
    REGULAR_CID_TO_UNI,
    TJ_REGEX,
    TM_REGEX,
)
from tbank_check_service import (
    _find_page_stream,
    _recompress_and_fix,
    _update_dates,
    _update_doc_id,
    _replace_field_bytes,
    _replace_nth_tj_at_y,
    _find_nth_tj_at_y,
    _replace_tj_array_at_coords,
)

BASE_DIR = Path(__file__).parent

STATEMENT_TEMPLATE_CANDIDATES = [
    BASE_DIR / "Справка о движении средств.pdf",
    Path.home() / "Downloads" / "Справка о движении средств.pdf",
]
BASE_STATEMENT = next(
    (p for p in STATEMENT_TEMPLATE_CANDIDATES if p.exists()),
    STATEMENT_TEMPLATE_CANDIDATES[-1],
)

# ── Statement field coordinates ───────────────────────────────────────

BLOCK1_FIELDS = {
    "ish_number": {"y": 716.87, "x": 56.0, "font": "F1", "size": 10},
    "date_form": {"y": 716.87, "x": 492.44, "font": "F1", "size": 10},
    "fio": {"y": 694.82, "x": 56.0, "font": "F2", "size": 9},
}

BLOCK2_FIELDS = {
    "contract_date": {"y": 624.82, "font": "F1", "size": 9},
    "contract_number": {"y": 606.82, "font": "F1", "size": 9},
    "account_number": {"y": 588.82, "font": "F1", "size": 9},
}

OP_CARD_Y = 513.78
OP_CARD_TIME_Y = 502.7
OP_SBP_Y = 488.78
OP_SBP_TIME_Y = 477.7
OP_SBP_PHONE_Y = 466.62
OP_DEPOSIT_Y = 453.78
OP_DEPOSIT_TIME_Y = 442.7

OPERATION_COLUMNS = {
    "op_date": 56.0,
    "op_time": 56.0,
    "list_date": 126.0,
    "list_time": 126.0,
    "amount_cur": 199.0,
    "amount_op": 294.0,
    "desc": 389.0,
    "number": 499.0,
}

TOTAL_DEPOSIT_Y = 428.52
TOTAL_EXPENSE_Y = 413.52
TOTAL_X = 126.0

STATEMENT_DEFAULTS = {
    "ish_number": "c15149e4",
    "date_form": "23.03.2026",
    "fio": "",
    "contract_date": "19.10.2023",
    "contract_number": "5058619266",
    "account_number": "40817810800079927645",
    "op1_date": "23.03.2026",
    "op1_time": "14:32",
    "op1_list_date": "23.03.2026",
    "op1_list_time": "14:33",
    "op1_amount": "-10.00 ",
    "op1_desc": "",
    "op1_number": "8429",
    "op2_date": "23.03.2026",
    "op2_time": "14:31",
    "op2_list_date": "23.03.2026",
    "op2_list_time": "14:31",
    "op2_amount": "-10.00 ",
    "op2_phone": "+79118584552",
    "op2_desc": "",
    "op2_number": "8429",
    "op3_date": "23.03.2026",
    "op3_time": "14:25",
    "op3_list_date": "23.03.2026",
    "op3_list_time": "14:26",
    "op3_amount": "+130.00 ",
    "op3_desc": "",
    "op3_number": "8429",
}


# ── Keywords / Ish. No. helpers ───────────────────────────────────────

_STMT_AVAILABLE_HEX = set("0123456789ce")


def _compute_new_keywords(
    pdf_bytes: bytes,
) -> tuple[str | None, str | None, str | None]:
    """Compute new Keywords and Ish number.

    The Ish number uses only hex chars available in the statement's F1
    font subset (0-9, c, e). Unavailable hex chars are substituted.
    Returns (new_kw_string, ish_8chars, timestamp) or (None, None, None).
    """
    kw_m = re.search(rb"/Keywords\(([^)]+)\)", pdf_bytes)
    if not kw_m:
        return None, None, None
    old_kw = kw_m.group(1).decode("latin-1")
    parts = old_kw.split(" | ")
    if len(parts) < 3:
        return None, None, None

    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    new_hash = hashlib.md5(f"{ts}_{parts[2]}".encode()).hexdigest()

    ish_chars = []
    _sub = {"a": "9", "b": "8", "d": "0", "f": "3"}
    for ch in new_hash[:8]:
        ish_chars.append(_sub.get(ch, ch))
    ish = "".join(ish_chars)

    new_kw = f"{ts} | {ish}{new_hash[8:]} | {parts[2]}"
    return new_kw, ish, ts


def _update_statement_keywords(
    pdf_bytes: bytes, new_kw: str
) -> bytes:
    """Update /Keywords in the PDF metadata, preserving byte length."""
    data = bytearray(pdf_bytes)
    kw_m = re.search(rb"/Keywords\(([^)]+)\)", bytes(data))
    if not kw_m:
        return bytes(data)
    old_kw_bytes = kw_m.group(1)
    new_kw_bytes = new_kw.encode("latin-1")
    padded = new_kw_bytes[: len(old_kw_bytes)].ljust(len(old_kw_bytes))
    data[kw_m.start(1) : kw_m.end(1)] = padded
    return bytes(data)


# ── Main patching function ────────────────────────────────────────────

def patch_tbank_statement(
    changes: dict[str, str],
    output_path: Optional[str | Path] = None,
    base_pdf: Optional[str | Path] = None,
) -> bytes:
    """Patch a T-Bank statement with new field values.

    Totals are auto-calculated from operation amounts.
    FIO (F2 field): only modified if all characters exist in the Medium
    font subset of the PDF. Otherwise left unchanged.
    """
    pdf_path = Path(base_pdf) if base_pdf else BASE_STATEMENT
    if not pdf_path.exists():
        raise FileNotFoundError(f"Statement template not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    f1_widths, f2_widths = extract_pdf_font_widths(pdf_bytes)
    len_num_start, stream_start, stream_len, decompressed = _find_page_stream(
        pdf_bytes
    )

    new_kw, new_ish, _ts = _compute_new_keywords(pdf_bytes)

    # ── Block 1: Header ──────────────────────────────────────────────

    if new_ish:
        found = find_tj_at_coords(decompressed, 716.87, 56.0)
        if found:
            old_raw, tj_start, tj_end = found
            ish_text = "\u0418\u0441\u0445. \u2116 " + new_ish
            new_raw = encode_text(ish_text, "regular")
            new_escaped = escape_pdf_literal(new_raw)
            new_tj = b"(" + new_escaped + b")Tj"
            decompressed = (
                decompressed[:tj_start] + new_tj + decompressed[tj_end:]
            )

    if "ish_number" in changes:
        found = find_tj_at_coords(decompressed, 716.87, 56.0)
        if found:
            old_raw, tj_start, tj_end = found
            ish_text = "\u0418\u0441\u0445. \u2116 " + changes["ish_number"]
            new_raw = encode_text(ish_text, "regular")
            new_escaped = escape_pdf_literal(new_raw)
            new_tj = b"(" + new_escaped + b")Tj"
            decompressed = (
                decompressed[:tj_start] + new_tj + decompressed[tj_end:]
            )

    if "date_form" in changes:
        decompressed = _replace_field_bytes(
            decompressed,
            716.87,
            492.44,
            changes["date_form"],
            font_size=10.0,
            right_aligned=False,
            widths=f1_widths,
            font="regular",
        )

    if "fio" in changes:
        fio_val = changes["fio"]
        if can_encode_in_font(fio_val, "medium", f2_widths):
            decompressed = _replace_field_bytes(
                decompressed,
                694.82,
                56.0,
                fio_val,
                font_size=9.0,
                right_aligned=False,
                widths=f2_widths,
                font="medium",
            )

    # ── Block 2: О продукте ──────────────────────────────────────────

    for field_key, y_coord in [
        ("contract_date", 624.82),
        ("contract_number", 606.82),
        ("account_number", 588.82),
    ]:
        if field_key in changes:
            new_text = " " + changes[field_key]
            decompressed = _replace_nth_tj_at_y(
                decompressed, y_coord, 1, new_text, font="regular"
            )

    # ── Block 3: Operations ──────────────────────────────────────────

    for op_prefix, date_y, time_y, phone_y in [
        ("op1", OP_CARD_Y, OP_CARD_TIME_Y, None),
        ("op2", OP_SBP_Y, OP_SBP_TIME_Y, OP_SBP_PHONE_Y),
        ("op3", OP_DEPOSIT_Y, OP_DEPOSIT_TIME_Y, None),
    ]:
        if f"{op_prefix}_date" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                date_y,
                56.0,
                changes[f"{op_prefix}_date"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

        if f"{op_prefix}_list_date" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                date_y,
                126.0,
                changes[f"{op_prefix}_list_date"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

        if f"{op_prefix}_time" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                time_y,
                56.0,
                changes[f"{op_prefix}_time"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

        if f"{op_prefix}_list_time" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                time_y,
                126.0,
                changes[f"{op_prefix}_list_time"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

        if f"{op_prefix}_amount" in changes:
            amt = changes[f"{op_prefix}_amount"]
            decompressed = _replace_field_bytes(
                decompressed, date_y, 199.0, amt, 9.0,
                right_aligned=False, widths=f1_widths,
            )
            decompressed = _replace_field_bytes(
                decompressed, date_y, 294.0, amt, 9.0,
                right_aligned=False, widths=f1_widths,
            )

        if f"{op_prefix}_number" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                date_y,
                499.0,
                changes[f"{op_prefix}_number"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

        if phone_y and f"{op_prefix}_phone" in changes:
            decompressed = _replace_field_bytes(
                decompressed,
                phone_y,
                389.0,
                changes[f"{op_prefix}_phone"],
                9.0,
                right_aligned=False,
                widths=f1_widths,
            )

    # ── Block 4: Totals (auto-calculate) ─────────────────────────────

    deposit_total = 0.0
    expense_total = 0.0
    for op_prefix in ("op1", "op2", "op3"):
        amt_key = f"{op_prefix}_amount"
        amt_str = changes.get(amt_key, STATEMENT_DEFAULTS.get(amt_key, "0"))
        amt_str = (
            amt_str.replace(" ", "")
            .replace("\u20bd", "")
            .replace("₽", "")
            .strip()
        )
        try:
            val = float(amt_str)
        except ValueError:
            val = 0.0
        if val > 0:
            deposit_total += val
        else:
            expense_total += abs(val)

    deposit_text = (
        changes.get("total_deposit")
        or f"{deposit_total:,.2f}".replace(",", " ").replace(".", ",") + " "
    )
    expense_text = (
        changes.get("total_expense")
        or f"{expense_total:,.2f}".replace(",", " ").replace(".", ",") + " "
    )

    decompressed = _replace_tj_array_at_coords(
        decompressed, TOTAL_DEPOSIT_Y, TOTAL_X, deposit_text
    )
    decompressed = _replace_tj_array_at_coords(
        decompressed, TOTAL_EXPENSE_Y, TOTAL_X, expense_text
    )

    # ── Finalize ─────────────────────────────────────────────────────

    pdf_bytes = _recompress_and_fix(
        pdf_bytes, len_num_start, stream_start, stream_len, decompressed
    )

    if new_kw:
        pdf_bytes = _update_statement_keywords(pdf_bytes, new_kw)

    from datetime import datetime as _dt
    now = _dt.now()
    pdf_bytes = _update_dates(pdf_bytes, now)
    pdf_bytes = _update_doc_id(pdf_bytes)

    if output_path:
        Path(output_path).write_bytes(pdf_bytes)
    return pdf_bytes


# ── Extraction ────────────────────────────────────────────────────────

def extract_statement_fields(pdf_path: str | Path) -> dict[str, str]:
    """Extract known field values from a T-Bank statement PDF."""
    pdf_bytes = Path(pdf_path).read_bytes()
    _, _, _, stream = _find_page_stream(pdf_bytes)

    result = {}

    found = find_tj_at_coords(stream, 716.87, 492.44)
    if found:
        result["date_form"] = decode_text(found[0])

    found = find_tj_at_coords(stream, 694.82, 56.0)
    if found:
        result["fio"] = decode_text(found[0], "medium")

    for key, y_val in [
        ("contract_date", 624.82),
        ("contract_number", 606.82),
        ("account_number", 588.82),
    ]:
        found = _find_nth_tj_at_y(stream, y_val, 1)
        if found:
            result[key] = decode_text(found[0]).lstrip()

    for prefix, dy, ty, phone_y in [
        ("op1", OP_CARD_Y, OP_CARD_TIME_Y, None),
        ("op2", OP_SBP_Y, OP_SBP_TIME_Y, OP_SBP_PHONE_Y),
        ("op3", OP_DEPOSIT_Y, OP_DEPOSIT_TIME_Y, None),
    ]:
        found = find_tj_at_coords(stream, dy, 56.0)
        if found:
            result[f"{prefix}_date"] = decode_text(found[0])
        found = find_tj_at_coords(stream, ty, 56.0)
        if found:
            result[f"{prefix}_time"] = decode_text(found[0])
        found = find_tj_at_coords(stream, dy, 126.0)
        if found:
            result[f"{prefix}_list_date"] = decode_text(found[0])
        found = find_tj_at_coords(stream, ty, 126.0)
        if found:
            result[f"{prefix}_list_time"] = decode_text(found[0])
        found = find_tj_at_coords(stream, dy, 199.0)
        if found:
            result[f"{prefix}_amount"] = decode_text(found[0])
        found = find_tj_at_coords(stream, dy, 499.0)
        if found:
            result[f"{prefix}_number"] = decode_text(found[0])
        if phone_y:
            found = find_tj_at_coords(stream, phone_y, 389.0)
            if found:
                result[f"{prefix}_phone"] = decode_text(found[0])

    kw_m = re.search(rb"/Keywords\(([^)]+)\)", pdf_bytes)
    if kw_m:
        parts = kw_m.group(1).decode("latin-1").split(" | ")
        if len(parts) >= 2:
            result["ish_number"] = parts[1][:8]

    return result
