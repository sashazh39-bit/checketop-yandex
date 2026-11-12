#!/usr/bin/env python3
"""
Patch Справка_о_движении_средств_2026_05_14_19_58_32.pdf:
1. Delete top row (+25 000.00 at 19:38)
2. Shift remaining rows/totals/signature up by 25pt
3. Change Пополнения total: 33 000,00 → 8 000,00
4. Replace phone +79003517080 → +79213567045
"""
import re, zlib

SRC = "/Users/aleksandrzerebatav/Downloads/Справка_о_движении_средств_2026_05_14_19_58_32.pdf"
DST = "/Users/aleksandrzerebatav/Downloads/Справка_о_движении_средств_2026_05_14_19_58_32_patched.pdf"

# ── CID helpers ──────────────────────────────────────────────────────────────
UNI_TO_CID: dict[int, int] = {
    0x0020: 0x0003, 0x002B: 0x0186, 0x002C: 0x0157,
    0x002D: 0x016B, 0x002E: 0x0156,
    **{0x30 + i: 0x0131 + i for i in range(10)},
}
def encode(text: str) -> bytes:
    return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in text)

# ── Constants ────────────────────────────────────────────────────────────────
# Row 1 PDF_y values to DELETE
DELETE_Y = {513.78, 502.70}
# Rows/totals/signature below row 1 — shift UP by this amount
SHIFT_DELTA = 25.0
# Borders to DELETE (y=499 was row1/row2 separator)
BORDER_DELETE_Y = {499}
# Borders to SHIFT UP by 25 (y=474 → 499, y=439 → 464)
BORDER_SHIFT_Y = {474, 439}

# Phone replacement (same byte length — safe direct replace)
PHONE_OLD = b"(" + encode("+79003517080") + b")Tj"
PHONE_NEW = b"(" + encode("+79213567045") + b")Tj"

# Пополнения total: TJ array format (old → new)
POPO_OLD = b"[(" + encode("33 000,00 ") + b")]TJ"
POPO_NEW = b"[(" + encode("8 000,00 ")  + b")]TJ"

# ── Low-level PDF xref helpers ───────────────────────────────────────────────
def read_xref_pos(data: bytes) -> int:
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF", data)
    return int(m.group(1))

def get_xref_entry(data: bytes, xref_pos: int, obj_num: int) -> tuple[int, int]:
    xref_block = data[xref_pos:]
    lines = xref_block.split(b"\n")
    idx = 1
    while idx < len(lines):
        hm = re.match(rb"(\d+)\s+(\d+)", lines[idx].strip())
        if hm:
            start = int(hm.group(1)); count = int(hm.group(2)); idx += 1
            for i in range(count):
                if idx + i >= len(lines): break
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", lines[idx + i].strip())
                if em and start + i == obj_num:
                    return int(em.group(1)), int(em.group(2))
            idx += count
        else:
            idx += 1
    raise ValueError(f"obj {obj_num} not found in xref")

def decompress_obj_stream(data: bytes, xref_num: int, xref_pos: int) -> tuple[int, int, bytes]:
    obj_off, _ = get_xref_entry(data, xref_pos, xref_num)
    obj_data = data[obj_off:]
    sm = re.search(rb"stream\r?\n", obj_data)
    em = re.search(rb"\nendstream", obj_data)
    raw = obj_data[sm.end(): em.start()]
    header = obj_data[:sm.start()]
    if b"FlateDecode" in header:
        return obj_off, len(raw), zlib.decompress(raw)
    return obj_off, len(raw), raw

def update_stream_in_pdf(data: bytearray, xref_num: int, xref_pos: int,
                          new_plain: bytes) -> tuple[bytearray, int]:
    obj_off, _ = get_xref_entry(bytes(data), xref_pos, xref_num)
    obj_slice = bytes(data[obj_off:])
    sm = re.search(rb"stream\r?\n", obj_slice)
    em = re.search(rb"\nendstream", obj_slice)
    old_len = em.start() - sm.end()
    new_comp = zlib.compress(new_plain, 9)
    new_len  = len(new_comp)
    delta    = new_len - old_len
    hdr = obj_slice[:sm.start()]
    hdr_new = re.sub(rb"/Length\s+\d+", b"/Length " + str(new_len).encode(), hdr)
    abs_end = obj_off + em.start()
    data = bytearray(bytes(data[:obj_off]) + hdr_new + b"stream\n" +
                     new_comp + bytes(data[abs_end:]))
    return data, delta

def shift_xref_entries(data: bytearray, xref_pos: int, above_offset: int,
                        delta: int) -> bytearray:
    """Increment all xref offsets > above_offset by delta."""
    xref_view = bytes(data[xref_pos:])
    lines = xref_view.split(b"\n")
    result = []; idx = 0; current_start = 0
    while idx < len(lines):
        line = lines[idx]
        hm = re.match(rb"(\d+)\s+(\d+)\s*$", line.strip())
        if hm:
            current_start = int(hm.group(1)); count = int(hm.group(2))
            result.append(line); idx += 1
            for i in range(count):
                if idx >= len(lines): break
                el = lines[idx]; idx += 1
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", el.strip())
                if em and em.group(3) != b"f":
                    off = int(em.group(1))
                    if off > above_offset:
                        new_off = off + delta
                        result.append(f"{new_off:010d} {em.group(2).decode()} {em.group(3).decode()} ".encode())
                        continue
                result.append(el)
        else:
            result.append(line); idx += 1
    new_xref = b"\n".join(result)
    return bytearray(bytes(data[:xref_pos]) + new_xref)

# ── Stream patcher ────────────────────────────────────────────────────────────

def _fmt_y(y: float) -> bytes:
    """Format y-coordinate for Tm line (strip trailing zeros like PDF)."""
    s = f"{y:.2f}".rstrip('0').rstrip('.')
    return s.encode()

def patch_stream(plain: bytes) -> bytes:
    lines = plain.split(b"\n")
    out: list[bytes] = []
    in_bt = False
    buf: list[bytes] = []
    first_y: float | None = None
    first_x: int | None = None

    def flush_bt(buf_lines: list[bytes], fy: float | None) -> list[bytes]:
        """Return (possibly modified) BT block, or [] to delete it."""
        if fy is None:
            return buf_lines
        # ── Delete row-1 blocks ──────────────────────────────────────────
        for dy in DELETE_Y:
            if abs(fy - dy) < 0.1:
                return []   # delete entire block
        # ── Shift remaining blocks upward ────────────────────────────────
        if fy < 513.0:  # everything below deleted row
            new_y = fy + SHIFT_DELTA
            new_buf = []
            for bl in buf_lines:
                s = bl.strip()
                m = re.match(rb"(1 0 0 1 \d+ )(\d+(?:\.\d+)?)( Tm)$", s)
                if m:
                    old_y_val = float(m.group(2))
                    new_y_val = old_y_val + SHIFT_DELTA
                    new_y_bytes = _fmt_y(new_y_val)
                    new_bl = b"1 0 0 1 " + re.search(rb"1 0 0 1 (\d+)", s).group(1) + b" " + new_y_bytes + b" Tm"
                    # preserve original indentation/whitespace
                    indent = bl[:len(bl) - len(bl.lstrip())]
                    new_buf.append(indent + new_bl)
                else:
                    new_buf.append(bl)
            return new_buf
        return buf_lines

    # ── Process line by line ─────────────────────────────────────────────
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        if s == b"BT":
            in_bt = True
            buf = [ln]
            first_y = None
            first_x = None
            i += 1
            continue

        if in_bt:
            buf.append(ln)
            if first_y is None:
                m = re.match(rb"1 0 0 1 (\d+) (\d+(?:\.\d+)?) Tm$", s)
                if m:
                    first_x = int(m.group(1))
                    first_y = float(m.group(2))
            if s == b"ET":
                in_bt = False
                result = flush_bt(buf, first_y)
                out.extend(result)
                buf = []
                first_y = None
            i += 1
            continue

        # ── m/l drawing commands ─────────────────────────────────────────
        mm = re.match(rb"(\d+(?:\.\d+)?) (\d+(?:\.\d+)?) (m|l)$", s)
        if mm:
            x_val = float(mm.group(1))
            y_val = float(mm.group(2))
            cmd   = mm.group(3)
            if int(y_val) in BORDER_DELETE_Y:
                i += 1; continue     # delete this border line
            if int(y_val) in BORDER_SHIFT_Y:
                new_y_val = y_val + SHIFT_DELTA
                new_y_str = _fmt_y(new_y_val)
                indent = ln[:len(ln) - len(ln.lstrip())]
                out.append(indent + f"{int(x_val)} ".encode() + new_y_str + b" " + cmd)
                i += 1; continue

        out.append(ln)
        i += 1

    plain_new = b"\n".join(out)

    # ── Phone replacement ─────────────────────────────────────────────────
    plain_new = plain_new.replace(PHONE_OLD, PHONE_NEW)

    # ── Пополнения total replacement ──────────────────────────────────────
    plain_new = plain_new.replace(POPO_OLD, POPO_NEW)

    return plain_new

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import fitz

    with open(SRC, "rb") as f:
        data = bytearray(f.read())

    xref_pos = read_xref_pos(bytes(data))
    doc = fitz.open(SRC)
    p1_xref = doc[0].get_contents()[0]
    doc.close()
    print(f"xref_pos={xref_pos}, p1_xref={p1_xref}")

    obj_off, _ = get_xref_entry(bytes(data), xref_pos, p1_xref)
    _, _, plain = decompress_obj_stream(bytes(data), p1_xref, xref_pos)
    print(f"plain stream size: {len(plain)}")

    new_plain = patch_stream(plain)
    print(f"new plain stream size: {len(new_plain)}")

    # Verify patch results in plain text before writing
    def has(s): return s.encode() in new_plain or encode(s) in new_plain

    print(f"Phone replaced: {PHONE_NEW in new_plain}")
    print(f"Пополнения 8 000 replaced: {POPO_NEW in new_plain}")

    data, delta = update_stream_in_pdf(data, p1_xref, xref_pos, new_plain)
    print(f"stream delta={delta}")

    if delta != 0:
        xref_pos += delta
        data = shift_xref_entries(data, xref_pos, obj_off, delta)
        data = bytearray(re.sub(
            rb"startxref\s+\d+\s+%%EOF",
            b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF",
            bytes(data)
        ))

    with open(DST, "wb") as f:
        f.write(data)
    print(f"\nWrote {len(data)} bytes → {DST}")

    # ── Text verification ─────────────────────────────────────────────────
    doc2 = fitz.open(DST)
    txt = doc2[0].get_text()
    doc2.close()

    checks = [
        ("no +25 000.00",    "+25 000.00" not in txt),
        ("no 19:38 time",    "19:38" not in txt),
        ("+8 000.00 present", "+8 000.00" in txt),
        ("-10.55 present",    "-10.55" in txt),
        ("phone new",         "+79213567045" in txt),
        ("no phone old",      "+79003517080" not in txt),
        ("Пополнения 8 000",  "8 000,00" in txt),
        ("no old 33 000",     "33 000,00" not in txt),
    ]

    print("\nVerification:")
    all_ok = True
    for name, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok: all_ok = False

    if all_ok:
        print("\nAll checks passed ✓")
    else:
        print("\nSome checks FAILED ✗")

if __name__ == "__main__":
    main()
