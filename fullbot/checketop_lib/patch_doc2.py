#!/usr/bin/env python3
"""
patch_doc2.py — обработка Справки 06_14_07

Удалить строки:
  06:29 (-30.00)     — верхняя #1   slot=35pt  PDF_y=513.78
  06:18 (-10.00)     — верхняя #2   slot=25pt  PDF_y=478.78
  03:50 (+8003)      — как раньше   slot=25pt  PDF_y=418.78
  03:13 (+9027)      — как раньше   slot=25pt  PDF_y=368.78
  02:54 (+8003)      — как раньше   slot=25pt  PDF_y=258.78
  02:39 (+7227)      — как раньше   slot=25pt  PDF_y=233.78

Оставить:
  04:54 (-21 000.00 → -21 050.00), 03:41 (+5055), 03:12 (+20),
  03:04 (-24000), 03:03 (+10000), 02:19, 02:17, 01:58, 01:52

Пересчитать итоги:
  Пополнения: 56 522,00 → 24 262,00
  Расходы:    45 150,00 → 45 160,00
"""
from __future__ import annotations
import re, zlib, hashlib, sys
from pathlib import Path
import fitz

SOURCE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "Справка о движении средств-2026-05-13 06_14_07.pdf"
)
OUTPUT = SOURCE.parent / "Справка о движении средств-2026-05-13 06_14_07_patched.pdf"

# ── CID encoding ──────────────────────────────────────────────────────────────
# Mapping: Unicode code point → CID (2-byte, big-endian) for TinkoffSans-Regular
# Built from the font's ToUnicode CMap (xref 19) in the source PDF.
UNI_TO_CID: dict[int, int] = {
    0x003A: 0x0158,
    0x0020: 0x0003, 0x002B: 0x0186, 0x002C: 0x0157,
    0x002D: 0x016B, 0x002E: 0x0156,
    **{0x30+i: 0x0131+i for i in range(10)},
    # Uppercase Cyrillic (А=00EB, sequence includes Ё slot at 00F1)
    **{0x0410+i: 0x00EB+i for i in range(6)},   # А-Е → 00EB-00F0
    # Ё skipped (00F1), Ж(00F2), З(00F3) skipped in source but safely inferred
    0x0418: 0x00F4,  # И
    # Й skipped (00F5)
    **{0x041A+i: 0x00F6+i for i in range(10)},  # К-У → 00F6-00FF
    **{0x0424+i: 0x0100+i for i in range(2)},   # Ф,Х → 0100-0101
    0x0426: 0x0103,  # Ц
    0x0428: 0x0104,  # Ш
    0x0429: 0x0105,  # Щ
    0x042F: 0x010B,  # Я
    # Lowercase Cyrillic (а=010C, includes ё at 0112)
    **{0x0430+i: 0x010C+i for i in range(7)},   # а-ж (а,б,в,г,д,е,ж without ё slot? no)
    # Actually: а=010C,б=010D,в=010E,г=010F,д=0110,е=0111,ж=0113 (ё=0112 inserted)
    0x0430: 0x010C,  # а
    0x0431: 0x010D,  # б
    0x0432: 0x010E,  # в
    0x0433: 0x010F,  # г
    0x0434: 0x0110,  # д
    0x0435: 0x0111,  # е
    0x0436: 0x0113,  # ж (skip ё=0112)
    0x0437: 0x0114,  # з
    0x0438: 0x0115,  # и
    0x0439: 0x0116,  # й
    0x043A: 0x0117,  # к
    0x043B: 0x0118,  # л
    0x043C: 0x0119,  # м
    0x043D: 0x011A,  # н
    0x043E: 0x011B,  # о
    0x043F: 0x011C,  # п
    0x0440: 0x011D,  # р
    0x0441: 0x011E,  # с
    0x0442: 0x011F,  # т
    0x0443: 0x0120,  # у
    0x0444: 0x0121,  # ф
    0x0445: 0x0122,  # х
    0x0446: 0x0124,  # ц
    0x0448: 0x0125,  # ш
    0x044C: 0x0127,  # ь
    0x044B: 0x0129,  # ы
    0x044D: 0x012A,  # э
    0x044E: 0x012B,  # ю
    0x044F: 0x012C,  # я
}
def encode(t: str) -> bytes:
    return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in t)

def encode_pdf_str(text: str) -> bytes:
    """Encode text → raw PDF literal-string bytes (same escaping as source PDF).

    The source PDF escapes: 0x0C→\\f, 0x0D→\\r, 0x28→\\(, 0x29→\\), 0x5C→\\\\.
    """
    out = bytearray()
    for ch in text:
        cid = UNI_TO_CID[ord(ch)]
        for b in [(cid >> 8) & 0xFF, cid & 0xFF]:
            if b == 0x0C:   out += b"\x5c\x66"  # \f
            elif b == 0x0D: out += b"\x5c\x72"  # \r
            elif b in (0x28, 0x29, 0x5C): out.append(0x5C); out.append(b)
            else: out.append(b)
    return bytes(out)

# ── Address replacement ───────────────────────────────────────────────────────
# Old address Tj raw bytes (extracted directly from source xref 8 stream, line 8):
ADDR_OLD_TJ = bytes.fromhex(
    "2800030133013401390138013601310157000300f6"
    "015c6601180115011a0115011a010f011d"
    "015c660110011e0117015c66012c"
    "000300fa015c7201180157"
    "000300ee000300fd011b010e0111011f011e011701570003"
    "00ff0118000300f8"
    "015c6601190115011a015c66016b011e0115"
    "015c720115011d012c0117015c660003"
    "015700030110015600030139015700030117010e015600030133"
    "29546a"
)
ADDR_NEW_TEXT = " 238750, Калининградская Обл, Г Советск, Ул Победы, д. 14, кв. 35"

def _make_addr_tj(text: str) -> bytes:
    return b"(" + encode_pdf_str(text) + b")Tj"

OLD_AMOUNT    = encode("-21 000.00")
NEW_AMOUNT    = encode("-21 050.00")
OLD_PLUS20_TJ = b"(" + encode("+20.00 ")     + b")Tj"
NEW_PLUS45_TJ = b"(" + encode("+45 000.00 ") + b")Tj"
# Page 2 totals: source has 56 522,00 / 45 150,00; final values after both changes:
# +20→+45 000 adds +44 980 to Пополнения: 56 522 - 32 260 (deleted rows) + 44 980 = 69 242
OLD_POPO   = encode("56 522,00")
NEW_POPO   = encode("69 242,00")
OLD_RASH   = encode("45 150,00")
NEW_RASH   = encode("45 160,00")

# ── Deletion map: (primary_pdf_y, slot_height_pt) ────────────────────────────
DELETE_ROWS: list[tuple[float, float]] = [
    (513.78, 35),   # 06:29  (3-line row)
    (478.78, 25),   # 06:18
    (418.78, 25),   # 03:50
    (368.78, 25),   # 03:13
    (258.78, 25),   # 02:54
    (233.78, 25),   # 02:39
]

# Secondary times and extra description lines to also delete
DELETE_SEC: set[float] = {502.70, 467.70, 407.70, 357.70, 247.70, 222.70}
DELETE_3RD: set[float] = {491.62}  # card-number line of row 06:29

ALL_DEL_YS: set[float] = (
    {dr[0] for dr in DELETE_ROWS} | DELETE_SEC | DELETE_3RD
)

Y_TOL = 0.30

def _shift(y: float) -> float:
    """Cumulative upward shift for kept element at PDF_y."""
    return sum(slot for (del_y, slot) in DELETE_ROWS if del_y > y + Y_TOL)

# ── BT/Tm helpers ─────────────────────────────────────────────────────────────
_TM_PAT = re.compile(rb"^(1 0 0 1 \d+ )(\d+(?:\.\d+)??)( Tm)$")
_ML_PAT  = re.compile(rb"^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(m|l)$")

HEADER_TM_Y = 529.70   # last column-header Tm row — don't shift above this
BORDER_KEEP_Y = 524.0  # header/data separator border — keep at y=524

def _patch_page1(stream: bytes) -> bytes:
    lines = stream.split(b"\n")
    out: list[bytes] = []
    in_bt = False
    buf:   list[bytes] = []
    first_tm_y: float | None = None

    deleted_bt = 0
    shifted_bt = 0

    def flush_buf():
        nonlocal deleted_bt, shifted_bt
        if first_tm_y is None:
            for bl in buf:
                out.append(bl if bl.endswith(b"\n") else bl + b"\n")
            return

        # Delete?
        if any(abs(first_tm_y - dy) < Y_TOL for dy in ALL_DEL_YS):
            deleted_bt += 1
            return

        delta = _shift(first_tm_y)
        if delta < 0.01:
            for bl in buf:
                out.append(bl if bl.endswith(b"\n") else bl + b"\n")
        else:
            shifted_bt += 1
            for bl in buf:
                m = _TM_PAT.match(bl.strip())
                if m:
                    ny = f"{float(m.group(2)) + delta:.2f}".encode()
                    out.append(m.group(1) + ny + m.group(3) + b"\n")
                else:
                    out.append(bl if bl.endswith(b"\n") else bl + b"\n")

    for ln in lines:
        s = ln.strip()

        if s == b"BT":
            in_bt = True
            buf = [ln]
            first_tm_y = None
            continue

        if in_bt:
            buf.append(ln)
            if first_tm_y is None:
                m = _TM_PAT.match(s)
                if m:
                    first_tm_y = float(m.group(2))
            if s == b"ET":
                in_bt = False
                flush_buf()
                buf = []
                first_tm_y = None
            continue

        # m/l border shift
        mm = _ML_PAT.match(s)
        if mm:
            y = float(mm.group(2))
            # Skip the gray table-end border (originally at y=65, shifted → y=225,
            # which falls inside the 01:39 row and causes the visible gray stripe)
            if abs(y - 65.0) < 0.5:
                continue
            if 50 < y < BORDER_KEEP_Y - 0.1:
                delta = _shift(y)
                new_y = y + delta
                new_line = mm.group(1) + b" " + f"{new_y:.2f}".encode() + b" " + mm.group(3)
                out.append(new_line + b"\n")
                continue

        out.append(ln if ln.endswith(b"\n") else ln + b"\n")

    result = b"".join(out)
    print(f"  [p1] BT deleted={deleted_bt}  shifted={shifted_bt}")

    # -21 000.00 → -21 050.00
    n = result.count(OLD_AMOUNT)
    result = result.replace(OLD_AMOUNT, NEW_AMOUNT)
    print(f"  [p1] -21 000.00 → -21 050.00: {n}x")

    # +20.00 → +45 000.00  (appears in amount columns x=199 and x=294)
    n2 = result.count(OLD_PLUS20_TJ)
    result = result.replace(OLD_PLUS20_TJ, NEW_PLUS45_TJ)
    print(f"  [p1] +20.00 → +45 000.00: {n2}x")

    # Address replacement: use exact bytes from source stream
    new_tj = _make_addr_tj(ADDR_NEW_TEXT)
    n3 = result.count(ADDR_OLD_TJ)
    result = result.replace(ADDR_OLD_TJ, new_tj)
    print(f"  [p1] address replaced: {n3}x")
    return result


def _extract_row_blocks(stream: bytes, target_ys: set[float]) -> list[bytes]:
    """Extract BT blocks from stream whose first Tm y is in target_ys."""
    lines = stream.split(b"\n")
    result: list[bytes] = []
    in_bt = False; buf: list[bytes] = []; first_y: float | None = None
    for ln in lines:
        s = ln.strip()
        if s == b"BT":
            in_bt = True; buf = [ln]; first_y = None
        elif in_bt:
            buf.append(ln)
            if first_y is None:
                m = re.match(rb"1 0 0 1 (\d+) (\d+(?:\.\d+)?) Tm$", s)
                if m: first_y = float(m.group(2))
            if s == b"ET":
                if first_y and any(abs(first_y - ty) < Y_TOL for ty in target_ys):
                    result.append(b"\n".join(buf))
                in_bt = False; buf = []
    return result


def _remap_all_y(block: bytes, y_map: dict[float, float]) -> bytes:
    """Replace ALL Tm y values in a BT block using the provided mapping."""
    lines = block.split(b"\n")
    out = []
    for ln in lines:
        m = re.match(rb"(1 0 0 1 \d+ )(\d+(?:\.\d+)?)( Tm)$", ln.strip())
        if m:
            cur_y = float(m.group(2))
            new_y = None
            for oy, ty in y_map.items():
                if abs(cur_y - oy) < Y_TOL:
                    new_y = ty; break
            if new_y is not None:
                out.append(m.group(1) + f"{new_y:.2f}".encode() + m.group(3))
                continue
        out.append(ln)
    return b"\n".join(out)


def _make_p1_rows(p2_stream: bytes) -> bytes:
    """Build page-1 BT blocks for 01:51 and 01:39 rows from page-2 content."""
    Y_MAP = {
        786.78: 258.78,
        775.70: 247.70,
        761.78: 233.78,
        750.70: 222.70,
    }
    p2_target = set(Y_MAP.keys())
    blocks = _extract_row_blocks(p2_stream, p2_target)

    out = b""
    for blk in blocks:
        out += _remap_all_y(blk, Y_MAP) + b"\n"
    return out


def _new_border_p1(y: float) -> bytes:
    """Add border row on page 1 (same format as existing borders)."""
    pairs = [(56,126),(126,196),(196,291),(291,386),(386,496),(496,539)]
    out = b""
    for x1, x2 in pairs:
        out += (
            b"1 w\n0 J\n0 0 0 RG\n[] 0 d\n"
            + f"{x1} {y:.2f} m\n{x2} {y:.2f} l\nS\n".encode()
        )
    return out


def _patch_page2(stream: bytes) -> bytes:
    """Delete column headers + row data, shift totals/signature up by 75pt, fix totals."""
    # y values to DELETE (column headers + 01:51 + 01:39)
    P2_DEL_YS: set[float] = {
        813.78, 802.70,   # column headers
        786.78, 775.70,   # row 01:51
        761.78, 750.70,   # row 01:39
    }
    # borders to DELETE (rows that no longer exist)
    P2_DEL_BORDERS: set[float] = {797.0, 772.0, 747.0}
    SHIFT_UP = 75.0  # shift totals + signature up by this amount

    lines = stream.split(b"\n")
    out: list[bytes] = []
    in_bt = False; buf: list[bytes] = []; first_y: float | None = None
    deleted_bt = 0; shifted_bt = 0

    def flush():
        nonlocal deleted_bt, shifted_bt
        if first_y is None:
            for bl in buf:
                out.append(bl if bl.endswith(b"\n") else bl + b"\n")
            return
        if any(abs(first_y - dy) < Y_TOL for dy in P2_DEL_YS):
            deleted_bt += 1; return
        # Shift up
        shifted_bt += 1
        for bl in buf:
            m = re.match(rb"(1 0 0 1 \d+ )(\d+(?:\.\d+)?)( Tm)$", bl.strip())
            if m:
                ny = f"{float(m.group(2)) + SHIFT_UP:.2f}".encode()
                out.append(m.group(1) + ny + m.group(3) + b"\n")
            else:
                out.append(bl if bl.endswith(b"\n") else bl + b"\n")

    _ML_P2 = re.compile(rb"^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(m|l)$")

    for ln in lines:
        s = ln.strip()
        if s == b"BT":
            in_bt = True; buf = [ln]; first_y = None; continue
        if in_bt:
            buf.append(ln)
            if first_y is None:
                m = re.match(rb"1 0 0 1 (\d+) (\d+(?:\.\d+)?) Tm$", s)
                if m: first_y = float(m.group(2))
            if s == b"ET":
                in_bt = False; flush(); buf = []; first_y = None
            continue
        # Delete row borders
        mm = _ML_P2.match(s)
        if mm:
            y = float(mm.group(2))
            if any(abs(y - dy) < Y_TOL for dy in P2_DEL_BORDERS):
                continue  # skip — deleted row border
        out.append(ln if ln.endswith(b"\n") else ln + b"\n")

    result = b"".join(out)
    print(f"  [p2] BT deleted={deleted_bt}  shifted={shifted_bt}")

    n1 = result.count(OLD_POPO); result = result.replace(OLD_POPO, NEW_POPO)
    n2 = result.count(OLD_RASH); result = result.replace(OLD_RASH, NEW_RASH)
    print(f"  [p2] Пополнения 56 522→69 242: {n1}x")
    print(f"  [p2] Расходы    45 150→45 160: {n2}x")
    return result


# ── Raw xref patcher ──────────────────────────────────────────────────────────
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
    old_end = stream_abs - ld + old_len
    updated = 0
    for idx in range(count):
        ep = entry_start + idx * 20
        ent = bytes(data[ep:ep+20])
        if len(ent) < 20: break
        off = int(ent[:10]); flag = chr(ent[17]) if len(ent) > 17 else "f"
        if flag == "n" and off >= old_end:
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

    # Also read ORIGINAL page 2 stream for row extraction
    doc2 = fitz.open(str(SOURCE))
    p2_orig = doc2.xref_stream(13)
    doc2.close()

    print("=== Page 1 ===")
    new_p1 = _patch_page1(p1_raw)
    # Append 01:51 and 01:39 rows to page 1
    extra = _make_p1_rows(p2_orig)
    new_p1 += extra
    # Add borders at y=244 (bottom of 01:51) and y=219 (bottom of 01:39)
    new_p1 += _new_border_p1(244.0) + _new_border_p1(219.0)
    print(f"  [p1] added rows 01:51+01:39 ({len(extra)} bytes) + 2 borders")

    print("=== Page 2 ===")
    new_p2 = _patch_page2(p2_orig)

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
        ("5498760510" not in t1, "Row 03:50 deleted"),
        ("5633545499" in t1,     "Row 03:41 present"),
        ("5646460199" not in t1, "Row 03:13 deleted"),
        ("0290486745" not in t1, "Row 02:54 deleted"),
        ("5615584565" not in t1, "Row 02:39 deleted"),
        ("06:29"      not in t1, "Row 06:29 deleted"),
        ("06:18"      not in t1, "Row 06:18 deleted"),
        ("-21 050.00"  in t1,    "-21 050.00 present"),
        ("-21 000.00" not in t1, "-21 000.00 gone"),
        ("+45 000.00"  in t1,    "+45 000.00 present"),
        ("+20.00"      not in t1, "+20.00 gone"),
        ("01:51"       in t1,    "Row 01:51 on page 1"),
        ("01:39"       in t1,    "Row 01:39 on page 1"),
        ("Дата и время" not in t2, "Col headers removed from page 2"),
        ("01:51"       not in t2,  "Row 01:51 not on page 2"),
        ("69 242"      in t2,    "Пополнения 69 242,00"),
        ("45 160"      in t2,    "Расходы 45 160,00"),
        ("С уважением" in t2,    "Signature on page 2"),
        ("Победы"      in t1,    "New street Победы present"),
        ("Мамина"      not in t1, "Old street Мамина removed"),
    ]
    all_ok = True
    for ok, name in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok: all_ok = False

    print("\n--- Page 1 table (top→bottom) ---")
    doc_out = fitz.open(str(OUTPUT))
    words = sorted(doc_out[0].get_text("words"), key=lambda w: (w[3], w[0]))
    prev_y = None
    for w in words:
        if not (265 < w[3] < 500): continue
        if prev_y is None or abs(w[3]-prev_y) > 2:
            print(f"\n  y={w[3]:.1f}: ", end="")
            prev_y = w[3]
        print(w[4], end=" ")
    print()
    doc_out.close()

    print(f"\n  Output: {OUTPUT}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
