#!/usr/bin/env python3
"""Сервис для создания/редактирования выписок Альфа-Банка.

Блоковая структура:
  Блок 1 (Информация о счёте): номер счёта, ФИО клиента, адрес
  Блок 2 (Операции): транзакции — суммы, телефон, код, описание
  Блок 3 (Баланс счёта): входящий/исходящий остатки, поступления, расходы
"""
from __future__ import annotations

import re
import zlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
# Основной шаблон бота — готовая выписка 177414 (одна СБП, +7 в шрифте). Пересобрать: build_statement_zhukov_177414.py
_STATEMENT_TEMPLATE = "выписка_жуков_177414_СБП_одна_операция.pdf"
_BASE_ALFA_PDF_CANDIDATES = [
    BASE_DIR / _STATEMENT_TEMPLATE,
    Path.home() / "Downloads" / _STATEMENT_TEMPLATE,
]
BASE_PDF = next((p for p in _BASE_ALFA_PDF_CANDIDATES if p.exists()), _BASE_ALFA_PDF_CANDIDATES[0])

# ── Значения по умолчанию: выписка_жуков_177414_СБП_одна_операция.pdf ──

BLOCK2_DEFAULTS = {
    "входящий_остаток": "56 812,93",
    "поступления": "0,00",
    "расходы": "55 000,03",
    "исходящий_остаток": "1 812,90",
    "платежный_лимит": "1 812,90",
    "текущий_баланс": "1 812,90",
}

BLOCK3_DEFAULTS = {
    "номер_счета": "40817810980480002476",
    "клиент_имя": "Жуков Алексей",
    "клиент_отчество": "Ефимович",
    "индекс": "238340",
    "город": "Светлый",
    "дом_кв": "2А, кв. 34",
    # Суффикс после названия города в потоке (у Каштановой / Калининградской разная вёрстка)
    "_city_street_suffix": ", УЛИЦА Калининградская, \nд. ",
    "адрес_полный": (
        "238340, РОССИЯ, \n"
        "Калининградская область, \n"
        "ОБЛАСТЬ Калининградская, \n"
        "Светлый, УЛИЦА Калининградская, \n"
        "д. 2А, кв. 34"
    ),
}

BLOCK1_DEFAULTS = {
    "код_операции_расход": "C822502260006543",
    "телефон": "+7 (911) 858-45-",
    "телефон_окончание": "52",
    "сумма_расход": "55 000,03",
    "код_операции_приход": "—",
    "получатель_сокр": "—",
    "сумма_приход": "—",
}

BLOCK_LABELS = {
    "входящий_остаток": "Входящий остаток",
    "поступления": "Поступления",
    "расходы": "Расходы",
    "исходящий_остаток": "Исходящий остаток",
    "платежный_лимит": "Платежный лимит",
    "текущий_баланс": "Текущий баланс",
    "номер_счета": "Номер счета",
    "клиент_имя": "Клиент (Имя Фамилия)",
    "клиент_отчество": "Отчество",
    "индекс": "Индекс",
    "город": "Город",
    "дом_кв": "Дом, квартира",
    "адрес_полный": "Адрес (полный)",
    "код_операции_расход": "Код операции (расход)",
    "телефон": "Телефон",
    "телефон_окончание": "Окончание телефона",
    "сумма_расход": "Сумма расхода",
}

# Tm y-coordinates for amount positions in the template (from content stream)
_AMOUNT_TM_Y = {
    "входящий_остаток": 662.497,
    "поступления": 645.447,
    "расходы": 628.397,
    "исходящий_остаток": 611.347,
    "платежный_лимит": 594.297,
    "текущий_баланс": 537.497,
    "сумма_расход_tx": 427.104,
    "сумма_приход_tx": 403.006,
}

# Right edges for amount columns
_RIGHT_EDGE_HEADER = 566.979
_RIGHT_EDGE_TX = 568.028

# Current Tm x positions in the template (AM_17737975768134.pdf)
_ORIG_TM_X = {
    662.497: 527.427,
    645.447: 511.865,
    628.397: 516.314,
    611.347: 516.314,
    594.297: 516.314,
    537.497: 516.314,
    427.104: 514.700,
    403.006: 512.914,
}


def _import_cid():
    """Lazy-import cid_patch_amount utilities."""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from cid_patch_amount import (
        patch_replacements,
        _parse_tounicode,
        _extend_tounicode_identity,
        _encode_cid,
    )
    return patch_replacements, _parse_tounicode, _extend_tounicode_identity, _encode_cid


# ── Font metrics ──────────────────────────────────────────────────────────

def _extract_font_widths(pdf_bytes: bytes) -> dict[int, int]:
    """Extract CID→width mapping from first /W array (Font F1)."""
    pattern = rb'/W\s*\['
    for m in re.finditer(pattern, pdf_bytes):
        depth = 0
        for i in range(m.end() - 1, min(m.end() + 5000, len(pdf_bytes))):
            if pdf_bytes[i:i+1] == b'[':
                depth += 1
            elif pdf_bytes[i:i+1] == b']':
                depth -= 1
                if depth == 0:
                    w_str = pdf_bytes[m.end():i].decode('latin1')
                    tokens = []
                    for t in re.finditer(r'\[([^\]]*)\]|(\d+)', w_str):
                        if t.group(1) is not None:
                            tokens.append(('arr', [float(x) for x in t.group(1).split()]))
                        else:
                            tokens.append(('num', int(t.group(2))))
                    widths = {}
                    j = 0
                    while j < len(tokens):
                        if tokens[j][0] == 'num':
                            if j + 1 < len(tokens) and tokens[j+1][0] == 'arr':
                                cid = tokens[j][1]
                                for k, w in enumerate(tokens[j+1][1]):
                                    widths[cid + k] = int(w)
                                j += 2
                            else:
                                j += 1
                        else:
                            j += 1
                    return widths
                    break
    return {}


def _text_width_pt(text: str, uni_to_cid: dict, cid_widths: dict, font_size: float = 8.0) -> float:
    """Calculate text width in points."""
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        cid_hex = uni_to_cid.get(cp)
        if cid_hex:
            cid_num = int(cid_hex, 16)
            total += cid_widths.get(cid_num, 556)
        else:
            total += 556
    return total * font_size / 1000


def _calc_tm_x(text: str, right_edge: float, uni_to_cid: dict, cid_widths: dict) -> float:
    """Calculate Tm x for right-aligned text."""
    w = _text_width_pt(text, uni_to_cid, cid_widths, 8.0)
    return round(right_edge - w, 3)


# ── Character validation ─────────────────────────────────────────────────

def get_available_chars(pdf_path: Path | None = None) -> set[str]:
    """Get characters available in the PDF's fonts."""
    path = pdf_path or BASE_PDF
    if not path.exists():
        return set()
    _, _parse_tounicode, _, _ = _import_cid()
    data = path.read_bytes()
    uni_to_cid = _parse_tounicode(data)
    return {chr(cp) for cp in uni_to_cid if 0 < cp < 0x110000}


def validate_text(text: str, pdf_path: Path | None = None) -> list[str]:
    """Return list of characters in text that are NOT available in the font."""
    available = get_available_chars(pdf_path)
    missing = []
    seen = set()
    for ch in text:
        if ch not in available and not ch.isspace() and ch not in seen:
            missing.append(ch)
            seen.add(ch)
    return missing


def get_alfa_template_op_c() -> str:
    """Код операции C… из текущего BASE_PDF (первая операция scan_alfa_block2).

    Для AM_1774109927283 — C162103261572702; для AM_1774134591446 — C822502260006543.
    """
    if not BASE_PDF.exists():
        return "C162103261572702"
    try:
        from vyписка_service import scan_alfa_block2

        for op in scan_alfa_block2(BASE_PDF):
            nid = (op.get("номер_операции") or "").strip()
            if nid.startswith("C") and len(nid) >= 10:
                return nid
    except Exception:
        pass
    return "C162103261572702"


# Код C… встречается в нескольких Tj/CMap — глобальную замену выносим во 2-ю фазу (несколько проходов).


def split_alfa_pairs_defer_global_op_c(
    pairs: list[tuple[str, str]],
    template_op_c: str | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Первая фаза — все пары, кроме точной замены эталонного C-кода на новый.

    Вторую фазу обрабатывает apply_deferred_op_c_replacements (несколько проходов).
    """
    toc = template_op_c if template_op_c is not None else get_alfa_template_op_c()
    first: list[tuple[str, str]] = []
    deferred: list[tuple[str, str]] = []
    for old, new in pairs:
        if old == toc and new != toc:
            deferred.append((old, new))
            continue
        first.append((old, new))
    return first, deferred


def apply_deferred_op_c_replacements(
    pdf_path: Path,
    deferred: list[tuple[str, str]],
    *,
    max_passes: int = 8,
) -> None:
    """Повторяет CID-замену old→new, пока находятся вхождения (разные шрифты/потоки)."""
    if not deferred:
        return
    patch_replacements, _, _, _ = _import_cid()
    # Один целевой new на old (последний выигрывает при дубликатах)
    by_old: dict[str, str] = {}
    for o, n in deferred:
        by_old[o] = n
    for old, new in by_old.items():
        if not old or old == new:
            continue
        for _ in range(max_passes):
            if not patch_replacements(pdf_path, pdf_path, [(old, new)]):
                break


# ── Auto-recalculate balances ─────────────────────────────────────────────

def recalc_balances(
    входящий: float,
    поступления: float,
    расходы: float,
) -> dict[str, str]:
    """Recalculate derived amounts and return all Block 2 values."""
    исходящий = входящий + поступления - расходы
    return {
        "входящий_остаток": _fmt_amount(входящий),
        "поступления": _fmt_amount(поступления),
        "расходы": _fmt_amount(расходы),
        "исходящий_остаток": _fmt_amount(исходящий),
        "платежный_лимит": _fmt_amount(исходящий),
        "текущий_баланс": _fmt_amount(исходящий),
    }


def _fmt_amount(val: float) -> str:
    """Format: 7 000,00 or 94,82"""
    if val == int(val) and val >= 1:
        s = f"{int(val):,}".replace(",", " ") + ",00"
    else:
        integer = int(val)
        frac = round((val - integer) * 100)
        int_str = f"{integer:,}".replace(",", " ") if integer >= 1000 else str(integer)
        s = f"{int_str},{frac:02d}"
    return s


def parse_amount(s: str) -> float | None:
    """Parse '7 000,00' or '94,82' or '10000' → float."""
    s = s.strip().replace(" ", "").replace("\u00a0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ── Extract data from check PDF ──────────────────────────────────────────

def extract_from_check(check_path: Path) -> dict[str, str]:
    """Extract fields from a check PDF for auto-filling the statement."""
    try:
        from receipt_extractor import extract_from_receipt
    except ImportError:
        return {}
    data = extract_from_receipt(check_path)
    result = {}
    if data.get("amount"):
        amt = float(data["amount"])
        result["сумма_приход"] = _fmt_amount(amt)
        result["сумма_расход"] = _fmt_amount(amt)
        result["поступления"] = _fmt_amount(amt)
        result["расходы"] = _fmt_amount(amt)
    if data.get("phone_recipient"):
        phone = data["phone_recipient"]
        if len(phone) >= 11:
            parts = re.match(r'(\+7\s*\(\d{3}\)\s*\d{3}-\d{2}-)', phone)
            if parts:
                result["телефон"] = parts.group(1)
                rest = phone[len(parts.group(1)):]
                if rest:
                    result["телефон_окончание"] = rest[:2]
    if data.get("fio_recipient"):
        fio = data["fio_recipient"]
        parts = fio.split()
        if len(parts) >= 2:
            result["клиент_имя"] = f"{parts[0]} {parts[1]}"
            initials = f"{parts[0][0]}. {parts[1][0]}."
            if len(parts) >= 3:
                result["клиент_отчество"] = parts[2]
                initials = f"{parts[0][0]}. {parts[2][0]}."
            result["получатель_сокр"] = f"{parts[0]} {initials}"
    if data.get("operation_id"):
        result["код_операции_расход"] = data["operation_id"]
    return result


# ── Main patch function ──────────────────────────────────────────────────

def patch_alfa_statement(
    changes: dict[str, str],
    output_path: Path,
    base_pdf: Path | None = None,
) -> tuple[bool, str]:
    """
    Apply changes to the Alfa Bank statement template.

    changes: dict mapping field keys to new values.
    Returns (success, error_message).
    """
    base = base_pdf or BASE_PDF
    if not base.exists():
        return False, f"Шаблон не найден: {base}"

    patch_replacements, _parse_tounicode, _extend_tounicode_identity, _encode_cid = _import_cid()

    # Merge defaults with changes
    all_fields = {}
    all_fields.update(BLOCK2_DEFAULTS)
    all_fields.update(BLOCK3_DEFAULTS)
    all_fields.update(BLOCK1_DEFAULTS)
    current = dict(all_fields)

    # Build CID replacement pairs: (old_text, new_text)
    phase1_replacements = []
    phase2_substring = []
    changed_amounts = {}

    for key, new_val in changes.items():
        old_val = current.get(key)
        if old_val is None or new_val == old_val:
            continue

        if key in ("входящий_остаток", "поступления", "расходы",
                    "исходящий_остаток", "платежный_лимит", "текущий_баланс"):
            old_rur = f"{old_val} RUR"
            new_rur = f"{new_val} RUR"
            phase1_replacements.append((old_rur, new_rur))
            changed_amounts[key] = new_val

        elif key == "сумма_расход":
            old_rur = f"-{old_val} RUR"
            new_rur = f"-{new_val} RUR"
            phase1_replacements.append((old_rur, new_rur))
            changed_amounts["сумма_расход_tx"] = new_val

        elif key == "сумма_приход":
            changed_amounts["сумма_приход_tx"] = new_val

        elif key == "телефон":
            phase2_substring.append((current["телефон"], new_val))

        elif key == "телефон_окончание":
            old_full = f"{current['телефон_окончание']}. Без НДС."
            new_full = f"{new_val}. Без НДС."
            phase1_replacements.append((old_full, new_full))

        elif key == "код_операции_расход":
            phase1_replacements.append((current[key], new_val))
            phase2_substring.append((current[key], new_val))

        elif key == "получатель_сокр":
            phase2_substring.append((current[key], new_val))

        elif key == "адрес_полный":
            old_addr = current.get("адрес_полный", "")
            if old_addr and old_addr != new_val:
                phase1_replacements.append((old_addr, new_val))

        elif key in ("номер_счета", "клиент_имя", "клиент_отчество",
                      "индекс", "город", "дом_кв", "код_операции_приход"):
            if key == "индекс":
                old_full = f"{current['индекс']}, РОССИЯ,"
                new_full = f"{new_val}, РОССИЯ,"
                phase1_replacements.append((old_full, new_full))
            elif key == "город":
                suf = current.get("_city_street_suffix", ", УЛИЦА Каштановая, д.")
                old_full = f"{current['город']}{suf}"
                new_full = f"{new_val}{suf}"
                phase1_replacements.append((old_full, new_full))
            else:
                phase1_replacements.append((current[key], new_val))

    # Deduplicate: longer patterns first to avoid partial matches
    phase1_replacements = list(dict.fromkeys(phase1_replacements))
    phase1_replacements.sort(key=lambda x: len(x[0]), reverse=True)

    # Handle bold transaction amount (F3 font)
    bold_amount_changed = "сумма_приход" in changes and changes["сумма_приход"] != current["сумма_приход"]

    # ── Execute patching ──────────────────────────────────────────────
    tmp = Path(tempfile.mktemp(suffix=".pdf"))

    try:
        # Phase 1: full hex CID replacements
        if phase1_replacements:
            patch_replacements(base, tmp, phase1_replacements)
        else:
            shutil.copy2(base, tmp)

        # Phase 2: substring replacements + bold amount
        data = bytearray(tmp.read_bytes())
        uni_to_cid = _parse_tounicode(data)
        if not uni_to_cid:
            return False, "ToUnicode CMap не найден"

        required = set()
        for _, new_val in phase2_substring:
            for c in new_val:
                cp = ord(c)
                required.add(0xA0 if cp == 0x20 else cp)
                if cp == 0x20:
                    required.add(0x20)
        if bold_amount_changed:
            new_amt = changes["сумма_приход"]
            for c in f"{new_amt} RUR":
                cp = ord(c)
                required.add(0xA0 if cp == 0x20 else cp)
                if cp == 0x20:
                    required.add(0x20)
        data, uni_to_cid = _extend_tounicode_identity(data, required, uni_to_cid)

        sub_count = 0
        f3_cmap = {
            ord('1'): '0011', ord('0'): '0012', ord(' '): '0010',
            ord(','): '0013', ord('R'): '0014', ord('U'): '0015',
        }
        old_bold_hex = _encode_f3(current.get("сумма_приход", "0,00") + " RUR", f3_cmap)

        # Collect streams to patch (can't modify data during re.finditer)
        pdf_data = bytes(data)
        stream_patches = []
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(pdf_data):
                continue
            try:
                dec = zlib.decompress(pdf_data[stream_start:stream_start + stream_len])
            except zlib.error:
                continue
            if b"BT" not in dec:
                continue

            new_dec = dec

            for old_val, new_val in phase2_substring:
                old_cid = _encode_cid(old_val, uni_to_cid)
                new_cid = _encode_cid(new_val, uni_to_cid)
                if not old_cid or not new_cid:
                    continue
                old_inner = old_cid[1:-1]
                new_inner = new_cid[1:-1]
                if old_inner in new_dec:
                    new_dec = new_dec.replace(old_inner, new_inner)
                    sub_count += 1

            if bold_amount_changed and old_bold_hex:
                new_amt = changes["сумма_приход"] + " RUR"
                new_bold_hex = _encode_f3(new_amt, f3_cmap)
                if new_bold_hex:
                    old_tag = b"<" + old_bold_hex + b">"
                    new_tag = b"<" + new_bold_hex + b">"
                    if old_tag in new_dec:
                        new_dec = new_dec.replace(old_tag, new_tag)
                        sub_count += 1
                else:
                    new_f1_cid = _encode_cid(new_amt, uni_to_cid)
                    if new_f1_cid and old_bold_hex:
                        for nl in [b"\r\n", b"\n"]:
                            old_block = b"/F3" + nl + b" 8 Tf" + nl + b"<" + old_bold_hex + b"> Tj"
                            new_block = b"/F1" + nl + b" 8 Tf" + nl + new_f1_cid + b" Tj"
                            if old_block in new_dec:
                                new_dec = new_dec.replace(old_block, new_block)
                                sub_count += 1
                                break

            if new_dec != dec:
                stream_patches.append((stream_start, stream_len, len_num_start, new_dec))

        # Apply patches in reverse order (so offsets stay valid)
        data = bytearray(pdf_data)
        for stream_start, stream_len, len_num_start, new_dec in reversed(stream_patches):
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            data = bytearray(
                bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
            )
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)
            _update_xref(data, stream_start, delta)

        if stream_patches:
            tmp.write_bytes(data)

        # Phase 3: Tm position adjustments
        data = bytearray(tmp.read_bytes())
        uni_to_cid_final = _parse_tounicode(data)
        cid_widths = _extract_font_widths(bytes(data))

        # F3 widths for bold amount
        f3_widths = _extract_font_widths_nth(bytes(data), 2)

        tm_adjustments = []
        for field_key, tm_y in _AMOUNT_TM_Y.items():
            orig_x = _ORIG_TM_X.get(tm_y)
            if orig_x is None:
                continue

            if field_key == "сумма_приход_tx":
                new_text = (changes.get("сумма_приход", current.get("сумма_приход", "0,00")) + " RUR")
                right_edge = _RIGHT_EDGE_TX
                new_x = _calc_tm_x_f3(new_text, right_edge, f3_cmap, f3_widths)
            elif field_key == "сумма_расход_tx":
                new_text = "-" + (changes.get("сумма_расход", current["сумма_расход"]) + " RUR")
                right_edge = _RIGHT_EDGE_TX
                new_x = _calc_tm_x(new_text, right_edge, uni_to_cid_final, cid_widths)
            else:
                amt_key = field_key
                new_val = changes.get(amt_key, BLOCK2_DEFAULTS.get(amt_key, ""))
                new_text = new_val + " RUR"
                right_edge = _RIGHT_EDGE_HEADER
                new_x = _calc_tm_x(new_text, right_edge, uni_to_cid_final, cid_widths)

            old_tm = f"{orig_x:.3f} {tm_y:.3f} Tm".encode()
            new_tm = f"{new_x:.3f} {tm_y:.3f} Tm".encode()
            if len(old_tm) == len(new_tm) and old_tm != new_tm:
                tm_adjustments.append((old_tm, new_tm))

        tm_count = 0
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(data):
                continue
            try:
                dec = zlib.decompress(bytes(data[stream_start:stream_start + stream_len]))
            except zlib.error:
                continue
            if b"BT" not in dec:
                continue
            new_dec = dec
            for old_tm, new_tm in tm_adjustments:
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, new_tm)
                    tm_count += 1
            if new_dec != dec:
                new_raw = zlib.compress(new_dec, 9)
                delta = len(new_raw) - stream_len
                old_len_str = str(stream_len).encode()
                new_len_str = str(len(new_raw)).encode()
                if len(new_len_str) != len(old_len_str):
                    delta += len(new_len_str) - len(old_len_str)
                data = bytearray(
                    bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
                )
                num_end = len_num_start + len(old_len_str)
                data[len_num_start:num_end] = new_len_str
                _update_xref(data, stream_start, delta)

        tmp.write_bytes(data)
        shutil.move(str(tmp), str(output_path))
        return True, ""

    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(e)


def _encode_f3(text: str, f3_cmap: dict) -> bytes | None:
    """Encode text using F3 (Bold) CMap."""
    parts = []
    for ch in text:
        cp = ord(ch)
        if cp not in f3_cmap:
            return None
        parts.append(f3_cmap[cp])
    return "".join(parts).encode()


def _calc_tm_x_f3(text: str, right_edge: float, f3_cmap: dict, f3_widths: dict) -> float:
    """Calculate Tm x for F3 (Bold) font."""
    total = 0
    for ch in text:
        cp = ord(ch)
        cid_hex = f3_cmap.get(cp)
        if cid_hex:
            cid_num = int(cid_hex, 16)
            total += f3_widths.get(cid_num, 556)
        else:
            total += 556
    w = total * 8.0 / 1000
    return round(right_edge - w, 3)


def _extract_font_widths_nth(pdf_bytes: bytes, n: int) -> dict[int, int]:
    """Extract CID→width from the Nth /W array (0-indexed)."""
    pattern = rb'/W\s*\['
    idx = 0
    for m in re.finditer(pattern, pdf_bytes):
        if idx < n:
            idx += 1
            continue
        depth = 0
        for i in range(m.end() - 1, min(m.end() + 5000, len(pdf_bytes))):
            if pdf_bytes[i:i+1] == b'[':
                depth += 1
            elif pdf_bytes[i:i+1] == b']':
                depth -= 1
                if depth == 0:
                    w_str = pdf_bytes[m.end():i].decode('latin1')
                    tokens = []
                    for t in re.finditer(r'\[([^\]]*)\]|(\d+)', w_str):
                        if t.group(1) is not None:
                            tokens.append(('arr', [float(x) for x in t.group(1).split()]))
                        else:
                            tokens.append(('num', int(t.group(2))))
                    widths = {}
                    j = 0
                    while j < len(tokens):
                        if tokens[j][0] == 'num':
                            if j + 1 < len(tokens) and tokens[j+1][0] == 'arr':
                                cid = tokens[j][1]
                                for k, w in enumerate(tokens[j+1][1]):
                                    widths[cid + k] = int(w)
                                j += 2
                            else:
                                j += 1
                        else:
                            j += 1
                    return widths
                    break
    return {}


def _replace_stream(data: bytearray, stream_start: int, stream_len: int,
                    len_num_start: int, new_dec: bytes) -> None:
    """Compress new_dec, replace stream in data, update /Length and xref."""
    new_raw = zlib.compress(new_dec, 9)
    delta = len(new_raw) - stream_len
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    if len(new_len_str) != len(old_len_str):
        delta += len(new_len_str) - len(old_len_str)
    data[:] = bytearray(
        bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
    )
    num_end = len_num_start + len(old_len_str)
    data[len_num_start:num_end] = new_len_str
    _update_xref(data, stream_start, delta)


def _update_xref(data: bytearray, stream_start: int, delta: int) -> None:
    """Update xref offsets and startxref after stream modification."""
    if delta == 0:
        return
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1):em.start(1) + 10] = f"{offset + delta:010d}".encode()
        data[xref_m.start(3):xref_m.end(3)] = bytes(entries)
    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if startxref_m and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        data[pos:pos + len(str(old_pos))] = str(old_pos + delta).encode()


# ── Right-alignment post-patch ────────────────────────────────────────────

# Right edges for Block 3 amounts and Block 2 tx amount, verified from
# AM_1774109927283.pdf: right_edge = orig_tm_x + _text_width_pt(orig_text)
_TM_RIGHT_EDGES: dict[float, float] = {
    662.497: 566.979,  # входящий_остаток
    645.447: 566.979,  # поступления
    628.397: 566.979,  # расходы
    611.347: 566.979,  # исходящий_остаток
    594.297: 566.979,  # платежный_лимит
    537.497: 566.979,  # текущий_баланс
    427.104: 568.028,  # Block 2 op amount (AM_1774109927283)
    # Доп. y для AM_1774134591446 (если подставлен как запасной шаблон)
    554.547: 566.979,
    466.152: 566.979,
    456.953: 566.979,
    444.154: 566.979,
    434.955: 566.979,
    420.056: 566.979,
}

# Tolerance for y-coordinate matching (floating point from different PDFs)
_TM_Y_TOL = 0.1


def adjust_amount_tm_positions(out_path: Path) -> None:
    """Re-align Block 2/3 amount+RUR strings so RUR stays at its template right edge.

    After CID text replacement the Tm x position is stale — the text grows
    leftward but its Tm anchor stays at the original position.  This function
    recalculates Tm x for each known amount y-position using CID glyph widths
    so the combined 'amount RUR' string right-aligns to the same edge as the
    template.
    """
    raw = out_path.read_bytes()
    from cid_patch_amount import _parse_tounicode
    uni_to_cid = _parse_tounicode(raw)
    if not uni_to_cid:
        return
    cid_to_char: dict[str, str] = {v: chr(k) for k, v in uni_to_cid.items()}
    cid_widths = _extract_font_widths(raw)
    if not cid_widths:
        return

    # Collect all stream positions using an immutable snapshot so the regex
    # iterator never holds a reference to the mutable bytearray.
    stream_infos: list[tuple[int, int, int]] = []  # (stream_start, stream_len, len_num_start)
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", raw, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(raw):
            continue
        try:
            dec = zlib.decompress(raw[stream_start:stream_start + stream_len])
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue
        stream_infos.append((stream_start, stream_len, m.start(2)))

    if not stream_infos:
        return

    data = bytearray(raw)
    modified = False

    for stream_start, stream_len, len_num_start in reversed(stream_infos):
        try:
            dec = zlib.decompress(bytes(data[stream_start:stream_start + stream_len]))
        except zlib.error:
            continue
        new_dec = _adjust_tms_in_stream(dec, cid_to_char, cid_widths)
        if new_dec is dec:
            continue

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)
        # Rebuild data without in-place resize (avoids BufferError)
        data = bytearray(
            bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
        )
        data[len_num_start:len_num_start + len(old_len_str)] = new_len_str
        _update_xref(data, stream_start, delta)
        modified = True

    if modified:
        out_path.write_bytes(bytes(data))


def _decode_cid_tj(hex_bytes: bytes, cid_to_char: dict[str, str]) -> str:
    """Decode a CID hex string (from a Tj operator) to Unicode text."""
    hex_str = hex_bytes.decode("ascii", errors="replace")
    result = []
    for i in range(0, len(hex_str) - 3, 4):
        cid_hex = hex_str[i:i + 4].upper()
        result.append(cid_to_char.get(cid_hex, "\ufffd"))
    return "".join(result)


def _adjust_tms_in_stream(
    dec: bytes, cid_to_char: dict[str, str], cid_widths: dict[int, int]
) -> bytes:
    """Return dec with Tm x values corrected for right-alignment at known y positions.

    Returns the original dec object unchanged if no adjustments were needed.
    """
    tm_tj = re.compile(rb"([\d.]+) ([\d.]+) Tm\r?\n<([0-9A-Fa-f]+)> Tj")
    replacements: list[tuple[bytes, bytes]] = []
    for m in tm_tj.finditer(dec):
        old_x_str = m.group(1)
        y_str = m.group(2)
        hex_content = m.group(3)
        try:
            tm_y = float(y_str)
        except ValueError:
            continue
        right_edge = None
        for known_y, edge in _TM_RIGHT_EDGES.items():
            if abs(tm_y - known_y) < _TM_Y_TOL:
                right_edge = edge
                break
        if right_edge is None:
            continue
        text = _decode_cid_tj(hex_content, cid_to_char)
        if not any(ch.isdigit() for ch in text):
            continue
        w = _text_width_pt_from_cid(text, cid_to_char, cid_widths)
        new_x = round(right_edge - w, 3)
        new_x_str = f"{new_x:.3f}".encode()
        # Only replace when same byte length (avoids stream length changes)
        if len(new_x_str) != len(old_x_str):
            continue
        if new_x_str == old_x_str:
            continue
        old_tm = old_x_str + b" " + y_str + b" Tm"
        new_tm = new_x_str + b" " + y_str + b" Tm"
        replacements.append((old_tm, new_tm))

    if not replacements:
        return dec
    result = dec
    for old_tm, new_tm in replacements:
        result = result.replace(old_tm, new_tm)
    return result


def _text_width_pt_from_cid(
    text: str, cid_to_char: dict[str, str], cid_widths: dict[int, int],
    font_size: float = 8.0,
) -> float:
    """Calculate text width (pt) using the CID width table.

    cid_to_char maps cid_hex_str -> char, so we invert it to char -> cid_hex.
    """
    char_to_cid: dict[str, str] = {ch: cid_hex for cid_hex, ch in cid_to_char.items()}
    total = 0
    for ch in text:
        cid_hex = char_to_cid.get(ch)
        if cid_hex:
            cid_num = int(cid_hex, 16)
            total += cid_widths.get(cid_num, 556)
        else:
            total += 556
    return total * font_size / 1000


# ── Convenience: format blocks for display ────────────────────────────────

def format_block1(vals: dict | None = None) -> str:
    """Format Block 1 — Информация о счёте (Реквизиты)."""
    v = dict(BLOCK3_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"📋 Блок 1 — Информация о счёте\n"
        f"├ Счёт: {v['номер_счета']}\n"
        f"├ Клиент: {v['клиент_имя']}\n"
        f"├ Отчество: {v['клиент_отчество']}\n"
        f"├ Индекс: {v['индекс']}\n"
        f"├ Город: {v['город']}\n"
        f"└ Дом/кв: {v['дом_кв']}"
    )


def format_block2(vals: dict | None = None) -> str:
    """Format Block 2 — Операции."""
    v = dict(BLOCK1_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"💳 Блок 2 — Операции\n"
        f"├ Код (расход): {v['код_операции_расход']}\n"
        f"├ Телефон: {v['телефон']}{v['телефон_окончание']}\n"
        f"├ Сумма расхода: -{v['сумма_расход']} RUR\n"
        f"├ Код (приход): {v['код_операции_приход']}\n"
        f"├ Получатель: {v['получатель_сокр']}\n"
        f"└ Сумма прихода: {v['сумма_приход']} RUR"
    )


def format_block3(vals: dict | None = None) -> str:
    """Format Block 3 — Баланс счёта."""
    v = dict(BLOCK2_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"📊 Блок 3 — Баланс счёта\n"
        f"├ Входящий остаток: {v['входящий_остаток']} RUR\n"
        f"├ Поступления: {v['поступления']} RUR\n"
        f"├ Расходы: {v['расходы']} RUR\n"
        f"├ Исходящий остаток: {v['исходящий_остаток']} RUR\n"
        f"├ Платежный лимит: {v['платежный_лимит']} RUR\n"
        f"└ Текущий баланс: {v['текущий_баланс']} RUR"
    )
