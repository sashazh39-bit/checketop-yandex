#!/usr/bin/env python3
"""
patch_document25.py  v4  — вставка строки 03:41 обратно в начало

Текущий источник: Справка о движении средств-2026-05-13 04_00_54.pdf
  (уже содержит: строки 03:12→+45000, -24000, +10000 … итог 64 187,00)

Задача:
  1. Сдвинуть все существующие строки данных ВНИЗ на 25pt (в PDF-координатах)
  2. Вставить строку 03:41 (+5 055.00, договор 5633545499) на первое место
  3. Добавить горизонтальную границу ячейки y=499 для новой строки
  4. Обновить итог: 64 187 → 69 242 (Пополнения)
"""
from __future__ import annotations
import re, zlib, hashlib, sys
from pathlib import Path
import fitz

SOURCE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "Справка о движении средств-2026-05-13 04_00_54.pdf"
)
OUTPUT = SOURCE.parent / "Справка о движении средств-2026-05-13 04_00_54_patched.pdf"

# ── CID encoding (F1 font) ────────────────────────────────────────────────────
UNI_TO_CID: dict[int, int] = {
    0x003A: 0x0158,  # :
    0x0020: 0x0003,  # space
    0x002B: 0x0186,  # +
    0x002C: 0x0157,  # ,
    0x002D: 0x016B,  # -
    0x002E: 0x0156,  # .
    0x0030: 0x0131, 0x0031: 0x0132, 0x0032: 0x0133,
    0x0033: 0x0134, 0x0034: 0x0135, 0x0035: 0x0136,
    0x0036: 0x0137, 0x0037: 0x0138, 0x0038: 0x0139, 0x0039: 0x013A,
}

def encode(t: str) -> bytes:
    return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in t)

# Totals
OLD_TOTAL = encode("64 187,00")
NEW_TOTAL = encode("69 242,00")

# ── Pre-built bytes for the new 03:41 row ────────────────────────────────────
# Date "13.05.2026"
DATE_BYTES = encode("13.05.2026")
# Time "03:41"
TIME_03_41 = encode("03:41")
# Settlement time "04:25"
TIME_04_25 = encode("04:25")
# Amount "+5 055.00 " (trailing space like other amount blocks)
AMT_BYTES  = encode("+5 055.00 ")
# Card "8914"
CARD_BYTES = encode("8914")

# Description primary "Внутрибанковский перевод" — raw CID bytes (copied from stream)
# Decoded: В(00ED)н(011A)у(0120)т(011F)р(011D)и(0115)б(010D→01\r)а(010C→01\f)
#           н(011A)к(0117)о(011B)в(010E)с(011E)к(0117)и(0115)й(0116)
#           space(0003)п(011C)е(0111)р(011D)е(0111)в(010E)о(011B)д(0110)
DESC_PRIMARY = bytes.fromhex(
    "00ed011a0120011f011d0115"
    "015c72"    # б  (01 + escaped \r = 0x0D → CID 0x010D)
    "015c66"    # а  (01 + escaped \f = 0x0C → CID 0x010C)
    "011a0117011b010e011e0117011501160003011c0111011d0111010e011b0110"
)
# Description secondary "с договора 5633545499"
# с(011E)sp(0003)д(0110)о(011B)г(010F)о(011B)в(010E)о(011B)р(011D)а(010C→01\f)
# sp(0003)5(0136)6(0137)3(0134)3(0134)5(0136)4(0135)5(0136)4(0135)9(013A)9(013A)
DESC_SECONDARY = bytes.fromhex(
    "011e0003"
    "0110011b010f011b010e011b011d"
    "015c66"    # а  (01\f)
    "000301360137013401340136013501360135013a013a"
)

# ₽ glyph in /F3 font (CID 0x0432 as 2 bytes)
RUBLE_BYTES = bytes.fromhex("0432")


def _bt_block(x: int, y_val: float, content_lines: list[bytes]) -> bytes:
    """Build a BT...ET block with /F1 9 Tf at given coordinates."""
    y_str = f"{y_val:.2f}".encode()
    x_str = str(x).encode()
    parts: list[bytes] = [
        b"BT",
        b"1 0 0 1 " + x_str + b" " + y_str + b" Tm",
        b"/F1 9 Tf",
        b"0 0 0 rg",
    ]
    parts.extend(content_lines)
    parts.append(b"0 g")
    parts.append(b"ET")
    return b"\n".join(parts) + b"\n"


def _bt_amount(x: int, y_val: float) -> bytes:
    """Amount block: "+5 055.00 " in F1 then ₽ in F3."""
    y_str = f"{y_val:.2f}".encode()
    x_str = str(x).encode()
    return (
        b"BT\n"
        b"1 0 0 1 " + x_str + b" " + y_str + b" Tm\n"
        b"/F1 9 Tf\n"
        b"0 0 0 rg\n"
        b"(" + AMT_BYTES + b")Tj\n"
        b"0 g\n"
        b"/F3 9 Tf\n"
        b"0 0 0 rg\n"
        b"(" + RUBLE_BYTES + b")Tj\n"
        b"0 g\n"
        b"ET\n"
    )


def _bt_desc(primary_y: float, secondary_y: float) -> bytes:
    """Description block with two Tm positions inside one BT...ET."""
    py = f"{primary_y:.2f}".encode()
    sy = f"{secondary_y:.2f}".encode()
    return (
        b"BT\n"
        b"1 0 0 1 389 " + py + b" Tm\n"
        b"/F1 9 Tf\n"
        b"0 0 0 rg\n"
        b"(" + DESC_PRIMARY + b")Tj\n"
        b"0 g\n"
        b"1 0 0 1 389 " + sy + b" Tm\n"
        b"0 0 0 rg\n"
        b"(" + DESC_SECONDARY + b")Tj\n"
        b"0 g\n"
        b"ET\n"
    )


def _new_row_blocks(primary_y: float, secondary_y: float) -> bytes:
    """All BT blocks for the new 03:41 row."""
    return (
        _bt_block(56,  primary_y,   [b"(" + DATE_BYTES  + b")Tj"]) +
        _bt_block(56,  secondary_y, [b"(" + TIME_03_41  + b")Tj"]) +
        _bt_block(126, primary_y,   [b"(" + DATE_BYTES  + b")Tj"]) +
        _bt_block(126, secondary_y, [b"(" + TIME_04_25  + b")Tj"]) +
        _bt_amount(199, primary_y) +
        _bt_amount(294, primary_y) +
        _bt_desc(primary_y, secondary_y) +
        _bt_block(499, primary_y,   [b"(" + CARD_BYTES  + b")Tj"])
    )


def _new_border(y: float) -> bytes:
    """Horizontal row dividers at given y — each segment with proper stroke commands."""
    pairs = [(56,126),(126,196),(196,291),(291,386),(386,496),(496,539)]
    out = b""
    for x1, x2 in pairs:
        out += (
            b"1 w\n0 J\n0 0 0 RG\n[] 0 d\n"
            + f"{x1} {y:.2f} m\n{x2} {y:.2f} l\nS\n".encode()
        )
    return out


# ── Page 1 patch ──────────────────────────────────────────────────────────────
Y_SHIFT = 25.0        # shift existing data rows DOWN this much
HEADER_Y  = 529.70    # column headers — do not shift Tm below this value

_TM_DATA_PAT = re.compile(
    rb"^(1 0 0 1 \d+ )(\d+(?:\.\d+)??)( Tm)$"
)
_ML_PAT = re.compile(rb"^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(m|l)$")

BORDER_KEEP_Y = 524.0   # top border of data area (headers/data separator) — don't shift


def _patch_page1(stream: bytes) -> bytes:
    lines = stream.split(b"\n")
    out: list[bytes] = []

    for ln in lines:
        s = ln.strip()

        # Shift Tm for data rows (y < HEADER_Y - epsilon)
        m = _TM_DATA_PAT.match(s)
        if m:
            y = float(m.group(2))
            if y < HEADER_Y - 0.5:
                new_y = y - Y_SHIFT
                new_line = m.group(1) + f"{new_y:.2f}".encode() + m.group(3)
                out.append(new_line + b"\n")
                continue

        # Shift m/l border commands (y < BORDER_KEEP_Y)
        mm = _ML_PAT.match(s)
        if mm:
            y = float(mm.group(2))
            if 100 < y < BORDER_KEEP_Y - 0.5:
                new_y = y - Y_SHIFT
                new_line = mm.group(1) + b" " + f"{new_y:.2f}".encode() + b" " + mm.group(3)
                out.append(new_line + b"\n")
                continue

        out.append(ln if ln.endswith(b"\n") else ln + b"\n")

    result = b"".join(out)

    # Append new row blocks and new border at y=499 (bottom of new first row)
    result += _new_row_blocks(513.78, 502.70)
    result += _new_border(499.0)

    return result


def _patch_page2(stream: bytes) -> bytes:
    n = stream.count(OLD_TOTAL)
    result = stream.replace(OLD_TOTAL, NEW_TOTAL)
    print(f"  [p2] 64 187,00 → 69 242,00: {n}x")
    return result


# ── Raw xref patcher (same as before) ────────────────────────────────────────
def _find_xref_table(data: bytes) -> int:
    m = re.search(rb"xref\r?\n\d+ \d+\r?\n", data)
    if not m: raise ValueError("xref table not found")
    return m.start()


def apply_raw_patch(data: bytearray, xref_num: int, new_dec: bytes) -> bytearray:
    obj_m = re.search(rb"\b" + str(xref_num).encode() + rb" 0 obj\b", bytes(data))
    if not obj_m: print(f"  [WARN] obj {xref_num} not found"); return data
    obj_pos = obj_m.start()

    len_m = re.search(rb"/Length\s+(\d+)", bytes(data[obj_pos:obj_pos+300]))
    if not len_m: print(f"  [WARN] /Length missing"); return data
    old_len = int(len_m.group(1))
    len_abs = obj_pos + len_m.start(1)

    sm = re.search(rb"stream(\r\n|\n)", bytes(data[obj_pos:obj_pos+400]))
    if not sm: return data
    stream_abs = obj_pos + sm.end()

    new_comp = zlib.compress(new_dec, 6)
    new_len  = len(new_comp)
    delta    = new_len - old_len
    print(f"  obj {xref_num}: comp {old_len}→{new_len} (Δ={delta:+d})")

    old_ls = str(old_len).encode(); new_ls = str(new_len).encode()
    ld = len(new_ls) - len(old_ls)
    data[len_abs:len_abs+len(old_ls)] = bytearray(new_ls)
    stream_abs += ld

    data = bytearray(bytes(data[:stream_abs]) + new_comp + bytes(data[stream_abs+old_len:]))
    total = delta + ld
    if total == 0: return data

    xref_off = _find_xref_table(bytes(data))
    xref_sec = bytes(data[xref_off:])
    hdr = re.match(rb"xref\r?\n(\d+) (\d+)\r?\n", xref_sec)
    if not hdr: print("  [WARN] xref parse failed"); return data
    count = int(hdr.group(2)); entry_start = xref_off + hdr.end()
    updated = 0
    for idx in range(count):
        ep = entry_start + idx * 20
        ent = bytes(data[ep:ep+20])
        if len(ent) < 20: break
        off = int(ent[:10]); flag = chr(ent[17]) if len(ent) > 17 else "f"
        if flag == "n" and off >= stream_abs - ld + old_len:
            data[ep:ep+10] = bytearray(f"{off+total:010d}".encode())
            updated += 1
    sxr_m = re.search(rb"(startxref[ \t]*\r?\n)(\d+)(\r?\n%%EOF)", bytes(data))
    if sxr_m:
        s, e = sxr_m.start(2), sxr_m.end(2)
        data = bytearray(bytes(data[:s]) + str(xref_off).encode() + bytes(data[e:]))
    print(f"    xref {updated} entries, startxref→{xref_off}")
    return data


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    if not SOURCE.exists():
        print(f"[ERROR] Not found: {SOURCE}"); return 1

    doc = fitz.open(str(SOURCE))
    p1_raw = doc.xref_stream(8)
    p2_raw = doc.xref_stream(13)
    doc.close()

    print("=== Page 1 ===")
    new_p1 = _patch_page1(p1_raw)
    print(f"  stream: {len(p1_raw)} → {len(new_p1)} bytes")

    print("=== Page 2 ===")
    new_p2 = _patch_page2(p2_raw)

    print("\n=== Injecting raw bytes ===")
    raw = SOURCE.read_bytes()
    print(f"Source: {len(raw):,} B  MD5={hashlib.md5(raw).hexdigest()[:16]}")

    data = bytearray(raw)
    data = apply_raw_patch(data, 8,  new_p1)
    data = apply_raw_patch(data, 13, new_p2)

    OUTPUT.write_bytes(bytes(data))
    print(f"\nOutput: {len(data):,} B  Δ={len(data)-len(raw):+d} B")

    id_o = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", raw)
    id_n = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", bytes(data))
    print(f"  /ID preserved: {'✓' if id_o and id_n and id_o.group(1)==id_n.group(1) else '✗'}")

    print("\n=== Text verification ===")
    doc_out = fitz.open(str(OUTPUT))
    t1 = doc_out[0].get_text()
    t2 = doc_out[1].get_text()
    doc_out.close()

    checks = [
        ("5633545499" in t1,      "Row 03:41 present (first row)"),
        ("+5 055.00"  in t1,      "+5 055.00 present"),
        ("+45 000.00" in t1,      "+45 000.00 present"),
        ("-24 000.00" in t1,      "-24 000.00 present"),
        ("+10 000.00" in t1,      "+10 000.00 present"),
        ("5498760510" not in t1,  "Deleted row 03:50 absent"),
        ("5646460199" not in t1,  "Deleted row 03:13 absent"),
        ("69 242"     in t2,      "Пополнения 69 242,00"),
        ("24 110"     in t2,      "Расходы 24 110,00"),
    ]
    all_ok = True
    for ok, name in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok: all_ok = False

    # Print first few rows from top for visual check
    print("\n--- Page 1 top rows (fitz words, top→bottom) ---")
    doc_out = fitz.open(str(OUTPUT))
    words = sorted(doc_out[0].get_text("words"), key=lambda w: (-w[3], w[0]))
    prev_y = None
    count = 0
    for w in words:
        if w[3] < 200: break  # stop before page footer area
        if w[3] > 340: continue  # skip page header, start from table area
        if prev_y is None or abs(w[3] - prev_y) > 2:
            if count > 0: print()
            print(f"  y={w[3]:.1f}: ", end="")
            prev_y = w[3]
        print(w[4], end=" ")
        count += 1
    print()
    doc_out.close()

    print(f"\n  Output: {OUTPUT}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
