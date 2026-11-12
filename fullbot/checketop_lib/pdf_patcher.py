#!/usr/bin/env python3
"""Универсальный патчер PDF-чеков: замена суммы с сохранением метаданных и структуры.
Поддерживает Альфа-Банк и ВТБ (разный формат CID в content stream).
"""
import re
import hashlib
import zlib
from pathlib import Path
from typing import Optional

# CMap: 0=0013, 1=0014, ..., 9=001C, space=0003, ₽=04@
# RUR: R=0152 or 0252, U=0155, depends on font; часто R=0x52→(01 52), U=(01 55)
DIGIT_CIDS = {str(i): bytes([0x00, 0x13 + i]) for i in range(10)}
SPACE_CID = b'\x00\x03'
RUBLE_CID = b'\x04@'


def format_amount(num: int, with_space_before_ruble: bool = True) -> str:
    """35726 -> '35 726 ' (пробел как разделитель тысяч)."""
    s = f"{num:,}".replace(",", " ")
    return s + (" " if with_space_before_ruble else "")


def format_amount_display(num: int) -> str:
    """Для отображения: 5000 -> '5 000', 2043 -> '2 043'."""
    return f"{num:,}".replace(",", " ")


def amount_to_cids(amount_str: str, currency: str = "ruble", rur_cids: tuple | None = None) -> list[bytes]:
    """currency: 'ruble' (₽) или 'rur'. rur_cids: (R, U, R) для варианта RUR."""
    cids = []
    for c in amount_str:
        if c == " ":
            cids.append(SPACE_CID)
        elif c in DIGIT_CIDS:
            cids.append(DIGIT_CIDS[c])
        else:
            raise ValueError(f"Неизвестный символ: {c}")
    if currency == "ruble":
        cids.append(RUBLE_CID)
    elif currency == "rur" and rur_cids:
        cids.extend(rur_cids)
    else:
        cids.append(RUBLE_CID)
    return cids


def build_amount_tj(cids: list[bytes], kern: str = "-11.11111", prefix: bytes = b"", suffix: bytes = b"") -> bytes:
    """Последний глиф (₽) — без кернинга после него. prefix/suffix для скобок []."""
    kern_b = kern.encode()
    parts = []
    for i, cid in enumerate(cids):
        h, l = cid[0], cid[1]
        glyph = bytes([0x28, h, l, 0x29])
        if i < len(cids) - 1:
            parts.append(glyph + kern_b + b" ")
        else:
            parts.append(glyph)
    return prefix + b"".join(parts) + suffix


def build_amount_variants(amount: int, bank: str, currency: str = "ruble") -> list[bytes]:
    """Банки: alfa, vtb. currency: ruble (₽) или rur ( RUR)."""
    out = []
    rur_options = (
        [
            (bytes([0x01, 0x52]), bytes([0x01, 0x55]), bytes([0x01, 0x52])),
            (bytes([0x00, 0x52]), bytes([0x00, 0x55]), bytes([0x00, 0x52])),
        ]
        if currency == "rur"
        else [None]
    )
    for rur_cids in rur_options:
        for space_before_ruble in (True, False):
            s = format_amount(amount, with_space_before_ruble=space_before_ruble)
            try:
                cids = amount_to_cids(
                    s, currency=currency, rur_cids=rur_cids if currency == "rur" else None
                )
            except ValueError:
                continue
            for kern in ("-11.11111", "-16.66667"):
                base = build_amount_tj(cids, kern=kern, prefix=b"", suffix=b"")
                out.append(base)
                if bank in ("vtb", "auto"):
                    out.append(b"[" + base + b"]")
                    out.append(base + b"]")
    return list(dict.fromkeys(out))


def count_glyphs(tj: bytes) -> int:
    """Количество глифов: кернингов + 1 (₽ без керна)."""
    return 1 + tj.count(b"-11.11111") + tj.count(b"-16.66667")


# Ширина глифа: kern -11.11111 (VTB) ≈ 9 pts, -16.66667 ≈ 6.5 pts
# Скан 09-03-26_03-47: '1 000 ₽' pts=9.025
PTS_PER_GLYPH = 6.5
PTS_PER_GLYPH_VTB = 9.0  # сумма ВТБ: правый край на уровне «Выполнено»


def compute_tm_shift(old_glyphs: int, new_glyphs: int, pts_per_glyph: float = PTS_PER_GLYPH) -> float:
    return (new_glyphs - old_glyphs) * pts_per_glyph


def update_id(data: bytearray) -> bool:
    id_m = re.search(rb'/ID\s*\[\s*(<[0-9a-fA-F]+>\s*<[0-9a-fA-F]+>)\s*\]', bytes(data))
    if id_m:
        old_id = id_m.group(1)
        # Oracle BI Publisher always emits lowercase hex in /ID — never uppercase
        h = hashlib.md5(bytes(data)).hexdigest().lower()
        new_id = f"<{h}> <{h}>".encode()
        data[id_m.start(1) : id_m.end(1)] = new_id[: len(old_id)].ljust(len(old_id))
        return True
    return False


def _same_format(a: bytes, b: bytes) -> bool:
    """Проверка: оба с [ или без, оба с ] в конце или без."""
    a_bracket = a.startswith(b"[")
    b_bracket = b.startswith(b"[")
    a_trailing = a.endswith(b"]")
    b_trailing = b.endswith(b"]")
    return a_bracket == b_bracket and a_trailing == b_trailing


def patch_amount(
    data: bytes | bytearray,
    amount_from: int,
    amount_to: int,
    bank: str = "auto",
    zlib_level: int = 6,
) -> tuple[bool, Optional[str], Optional[bytes]]:
    """
    Заменить сумму в PDF. bank: 'alfa' | 'vtb' | 'auto'.
    Возвращает (ok, err, new_data).
    """
    data = bytearray(data)
    banks = ["alfa", "vtb"] if bank == "auto" else [bank]
    currencies = ["ruble", "rur"]
    pairs = []
    for b in banks:
        for curr in currencies:
            old_v = build_amount_variants(amount_from, b, currency=curr)
            new_v = build_amount_variants(amount_to, b, currency=curr)
            for old_b in old_v:
                for new_b in new_v:
                    if old_b != new_b and _same_format(old_b, new_b):
                        pairs.append((old_b, new_b))

    content_changed = False
    matched_old = None
    matched_new = None

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue

        for old_b, new_b in pairs:
            if old_b in dec and old_b != new_b:
                dec = dec.replace(old_b, new_b)
                matched_old = old_b
                matched_new = new_b
                content_changed = True
                break
        if not content_changed:
            continue

        # Tm: для ВТБ (-11.11111) не меняем — patch_from_values выровняет по wall
        if b"-11.11111" not in matched_old:
            old_glyphs = count_glyphs(matched_old)
            new_glyphs = count_glyphs(matched_new)
            for pattern in (rb"1 0 0 1 ([\d.]+) (72\.37499) Tm", rb"1 0 0 1 ([\d.]+) (120\.74999) Tm"):
                match = re.search(pattern, dec)
                if match:
                    try:
                        x = float(match.group(1))
                    except ValueError:
                        continue
                    y_part = match.group(2).decode()
                    shift = compute_tm_shift(old_glyphs, new_glyphs)
                    new_x = x - shift
                    new_tm = f"1 0 0 1 {new_x:.5f} {y_part} Tm".encode()
                    old_tm = match.group(0)
                    if abs(new_x - x) > 0.01:
                        dec = dec.replace(old_tm, new_tm, 1)
                    break

        new_raw = zlib.compress(dec, zlib_level)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        num_end = len_num_start + len(old_len_str)
        new_data = bytearray(data[:stream_start]) + new_raw + data[stream_start + stream_len :]
        new_data = new_data[:len_num_start] + new_len_str + new_data[num_end:]

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", bytes(new_data))
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            new_data = new_data[: xref_m.start(3)] + bytes(entries) + new_data[xref_m.end(3) :]

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", bytes(new_data))
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            new_data = new_data[:pos] + str(old_pos + delta).encode() + new_data[pos + len(str(old_pos)) :]

        result = bytearray(new_data)
        update_id(result)
        return True, None, bytes(result)

    if not content_changed:
        return False, (
            "Сумма не найдена. Проверь:\n"
            "• Верно ли указана текущая сумма в чеке (10, 50, 100, 600 и т.д.)\n"
            "• Выбери другой банк или «Авто»"
        ), None


def _is_tbank_pdf(data: bytes) -> bool:
    """Detect T-Bank receipt by characteristic markers."""
    return (
        b"OpenPDF" in data
        and b"JasperReports" in data
        and (b"TinkoffSans" in data or b"tbank.ru" in data or b"TBANK" in data)
    )


def _patch_tbank(
    input_path: Path,
    output_path: Path,
    amount_to: int,
) -> tuple[bool, Optional[str]]:
    """T-Bank patching: only content stream + xref, NO metadata changes."""
    try:
        from tbank_check_service import (
            detect_receipt_type,
            patch_amount as tbank_patch_amount,
        )

        rtype = detect_receipt_type(str(input_path))
        tbank_patch_amount(
            str(input_path),
            float(amount_to),
            receipt_type=rtype,
            output_path=str(output_path),
        )
        return True, None
    except Exception as e:
        return False, f"Ошибка T-Bank патча: {e}"


def patch_pdf_file(
    input_path: str | Path,
    output_path: str | Path,
    amount_from: int,
    amount_to: int,
    bank: str = "auto",
) -> tuple[bool, Optional[str]]:
    inp = Path(input_path)
    out = Path(output_path)
    if not inp.exists():
        return False, f"Файл не найден: {inp}"
    try:
        data = inp.read_bytes()
    except Exception as e:
        return False, f"Ошибка чтения: {e}"

    # T-Bank: dedicated path that preserves integrity (no metadata changes)
    if bank == "tbank" or (bank == "auto" and _is_tbank_pdf(data)):
        out.parent.mkdir(parents=True, exist_ok=True)
        return _patch_tbank(inp, out, amount_to)

    ok, err, new_data = patch_amount(data, amount_from, amount_to, bank=bank)
    if ok and new_data is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(new_data)
        except Exception as e:
            return False, f"Ошибка записи: {e}"
        return True, None

    # Fallback: Альфа-Банк — hex CID (ToUnicode CMap)
    if bank in ("alfa", "auto"):
        try:
            from cid_patch_amount import patch_amount as cid_patch
            from cid_patch_amount import patch_replacements as cid_replacements

            new_rur = format_amount_display(amount_to) + " RUR"
            new_ru = format_amount_display(amount_to) + " р."

            for old_str, new_str in [
                (format_amount_display(amount_from) + " RUR", new_rur),
                (str(amount_from) + " RUR", new_rur),
                (format_amount_display(amount_from) + " р.", new_ru),
                (str(amount_from) + " р.", new_ru),
            ]:
                if cid_patch(inp, out, old_str, new_str):
                    return True, None

            if "RUR" in new_rur:
                for old_fb in ["10 RUR ", "10 RUR\u00a0", "10 RUR", "50 RUR ", "50 RUR\u00a0", "50 RUR", "500 RUR ", "500 RUR\u00a0", "500 RUR"]:
                    if cid_patch(inp, out, old_fb, new_rur):
                        return True, None

            if cid_replacements(inp, out, [
                (format_amount_display(amount_from) + " р.", new_ru),
                (str(amount_from) + " р.", new_ru),
            ]):
                return True, None
        except Exception:
            pass

    return False, err


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 5:
        print("Использование: python3 pdf_patcher.py input.pdf output.pdf FROM TO [bank]")
        print("  bank: alfa | vtb | auto")
        sys.exit(1)
    ok, err = patch_pdf_file(
        sys.argv[1], sys.argv[2],
        int(sys.argv[3].replace(" ", "")),
        int(sys.argv[4].replace(" ", "")),
        sys.argv[5] if len(sys.argv) > 5 else "auto",
    )
    if ok:
        print("[OK] Сохранено")
    else:
        print(f"[ERROR] {err}")
        sys.exit(1)
