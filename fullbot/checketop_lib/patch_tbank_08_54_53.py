#!/usr/bin/env python3
"""
Patch /Users/aleksandrzerebatav/Downloads/Справка о движении средств-2026-05-13 08_54_53.pdf
Replace 10 transaction amounts (position-based) and recalculate totals.
"""
import re, struct, zlib

SRC  = "/Users/aleksandrzerebatav/Downloads/Справка о движении средств-2026-05-13 08_54_53.pdf"
DST  = "/Users/aleksandrzerebatav/Downloads/Справка о движении средств-2026-05-13 08_54_53_patched.pdf"

# ── CID helpers ─────────────────────────────────────────────────────────────
UNI_TO_CID: dict[int, int] = {
    0x0020: 0x0003, 0x002B: 0x0186, 0x002C: 0x0157,
    0x002D: 0x016B, 0x002E: 0x0156,
    **{0x30 + i: 0x0131 + i for i in range(10)},
}

def encode(text: str) -> bytes:
    return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in text)

def make_tj(text: str) -> bytes:
    """Build  (CID_bytes)Tj  line with trailing newline."""
    return b"(" + encode(text) + b")Tj\n"

# ── Amount replacements keyed by (pdf_y, x_col)  ────────────────────────────
# Rows ordered top-to-bottom:
#  1.  PDF_y=513.78  -30.00  → -30 000.00
#  2.  PDF_y=478.78  -99.00  → -9 999.00
#  3.  PDF_y=453.78  -98.00  → -9 998.00
#  4.  PDF_y=418.78  +500.00 → +50 000.00
#  5.  PDF_y=393.78  -10.00  → -10 000.00
#  6.  PDF_y=358.78  -10.00  → -10 001.00
#  7.  PDF_y=333.78  -10.00  → -10 001.00
#  8.  PDF_y=308.78  -30.00  → -30 003.00
#  9.  PDF_y=273.78  -10.00  → -10 001.00
# 10.  PDF_y=138.78  +20.00  → +45 000.00

# Build (old_tj_bytes → new_tj_bytes) per PDF_y (same value for x=199 and x=294)
def _row(old: str, new: str) -> tuple[bytes, bytes]:
    return make_tj(old), make_tj(new)

ROW_REPLACEMENTS: dict[float, tuple[bytes, bytes]] = {
    513.78: _row("-30.00 ",   "-30 000.00 "),
    478.78: _row("-99.00 ",   "-9 999.00 "),
    453.78: _row("-98.00 ",   "-9 998.00 "),
    418.78: _row("+500.00 ",  "+50 000.00 "),
    393.78: _row("-10.00 ",   "-10 000.00 "),
    358.78: _row("-10.00 ",   "-10 001.00 "),
    333.78: _row("-10.00 ",   "-10 001.00 "),
    308.78: _row("-30.00 ",   "-30 003.00 "),
    273.78: _row("-10.00 ",   "-10 001.00 "),
    138.78: _row("+20.00 ",   "+45 000.00 "),
}

# ── Totals (page 2, TJ array format) ────────────────────────────────────────
# Current:  Пополнения: 57 022,00  |  Расходы: 45 407,00
# New:      Пополнения: 151 502,00 |  Расходы: 165 113,00
#
# Total Пополнения change:  +500 → +50 000 (+49 500)  +  +20 → +45 000 (+44 980) = +94 480
# 57 022 + 94 480 = 151 502 ✓
#
# Total Расходы change:
#  -30→-30000 (+29970)  -99→-9999 (+9900)  -98→-9998 (+9900)
#  -10→-10000 (+9990)   -10→-10001(+9991) x2  -30→-30003(+29973)  -10→-10001(+9991)
# 45 407 + 29970+9900+9900+9990+9991+9991+29973+9991 = 45 407 + 119 706 = 165 113 ✓

def _tj_array(text: str) -> bytes:
    """Build  [(CID_bytes )]TJ  line (same format as source totals)."""
    return b"[(" + encode(text) + b")]TJ\n"

OLD_POPO_TJ = _tj_array("57 022,00 ")
NEW_POPO_TJ = _tj_array("163 256,00 ")
OLD_RASH_TJ = _tj_array("45 407,00 ")
NEW_RASH_TJ = _tj_array("168 771,00 ")

# ── Low-level PDF helpers ────────────────────────────────────────────────────
def read_xref_and_data(data: bytes):
    """Parse cross-reference table; return (xref_offset, xref_lines, body)."""
    # Find startxref
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF", data)
    if not m:
        raise ValueError("startxref not found")
    xref_pos = int(m.group(1))
    return xref_pos

def get_xref_entry(data: bytes, xref_pos: int, obj_num: int) -> tuple[int, int]:
    """Return (offset, generation) for obj_num from xref table at xref_pos."""
    xref_block = data[xref_pos:]
    # skip "xref\n"
    lines = xref_block.split(b"\n")
    idx = 1  # skip "xref"
    while idx < len(lines):
        header_m = re.match(rb"(\d+)\s+(\d+)", lines[idx].strip())
        if header_m:
            start = int(header_m.group(1))
            count = int(header_m.group(2))
            idx += 1
            for i in range(count):
                if idx + i >= len(lines):
                    break
                entry = lines[idx + i].strip()
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", entry)
                if em and start + i == obj_num:
                    return int(em.group(1)), int(em.group(2))
            idx += count
        else:
            idx += 1
    raise ValueError(f"obj {obj_num} not found in xref")

def decompress_stream(data: bytes, xref_num: int, xref_pos: int) -> tuple[int, int, bytes]:
    """Find the stream for xref_num, return (stream_start_in_data, stream_len, decompressed)."""
    obj_offset, _ = get_xref_entry(data, xref_pos, xref_num)
    obj_data = data[obj_offset:]
    # find stream ... endstream
    sm = re.search(rb"stream\r?\n", obj_data)
    em = re.search(rb"\nendstream", obj_data)
    if not sm or not em:
        raise ValueError(f"stream markers not found in obj {xref_num}")
    stream_start = obj_offset + sm.end()
    stream_len   = em.start()          # relative to obj_data
    raw_stream   = obj_data[sm.end(): em.start()]
    # check /Filter
    header = obj_data[:sm.start()]
    if b"/FlateDecode" in header or b"FlateDecode" in header:
        return stream_start, len(raw_stream), zlib.decompress(raw_stream)
    return stream_start, len(raw_stream), raw_stream

def recompress(plain: bytes) -> bytes:
    return zlib.compress(plain, 9)

def update_stream_in_pdf(data: bytearray, xref_num: int, xref_pos: int,
                          new_plain: bytes) -> tuple[bytearray, int]:
    """Replace the compressed stream for xref_num; update /Length and shift xref entries."""
    obj_offset, _ = get_xref_entry(data, xref_pos, xref_num)
    obj_slice = bytes(data[obj_offset:])
    sm = re.search(rb"stream\r?\n", obj_slice)
    em = re.search(rb"\nendstream", obj_slice)
    old_stream_rel_start = sm.end()
    old_stream_rel_end   = em.start()
    old_len = old_stream_rel_end - old_stream_rel_start

    new_compressed = recompress(new_plain)
    new_len = len(new_compressed)
    delta = new_len - old_len

    # Fix /Length in header
    hdr = obj_slice[:sm.start()]
    hdr_new = re.sub(rb"/Length\s+\d+", b"/Length " + str(new_len).encode(), hdr)
    
    abs_start = obj_offset + old_stream_rel_start
    abs_end   = obj_offset + old_stream_rel_end

    # Build new data
    data = bytearray(bytes(data[:obj_offset]) + hdr_new + b"stream\n" +
                     new_compressed +
                     bytes(data[abs_end:]))

    return data, delta

def shift_xref(data: bytearray, xref_pos: int, patched_obj: int,
               patched_obj_offset: int, delta: int) -> bytearray:
    """Shift all xref offsets for objects located after patched_obj_offset by delta."""
    xref_block_start = xref_pos
    xref_view = bytes(data[xref_block_start:])
    lines = xref_view.split(b"\n")
    result_lines = []
    idx = 0
    in_header = True
    current_start = 0
    while idx < len(lines):
        line = lines[idx]
        hm = re.match(rb"(\d+)\s+(\d+)\s*$", line.strip())
        if hm and in_header:
            current_start = int(hm.group(1))
            count_n = int(hm.group(2))
            result_lines.append(line)
            idx += 1
            for i in range(count_n):
                if idx >= len(lines):
                    break
                entry_line = lines[idx]
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", entry_line.strip())
                if em:
                    off = int(em.group(1)); gen = em.group(2); typ = em.group(3)
                    obj_n = current_start + i
                    if typ == b"f":
                        result_lines.append(entry_line)
                    elif off > patched_obj_offset:
                        new_off = off + delta
                        result_lines.append(f"{new_off:010d} {gen.decode()} {typ.decode()} ".encode())
                    else:
                        result_lines.append(entry_line)
                else:
                    result_lines.append(entry_line)
                idx += 1
            in_header = False
        else:
            result_lines.append(line)
            idx += 1
    
    new_xref = b"\n".join(result_lines)
    return bytearray(bytes(data[:xref_block_start]) + new_xref)

# ── Page stream patchers ─────────────────────────────────────────────────────

def _patch_stream(plain: bytes, replacements: dict) -> bytes:
    """
    Process BT blocks in `plain`.  For each block whose first_tm_y matches a
    key in `replacements`, replace the old_tj bytes with new_tj bytes.
    """
    lines = plain.split(b"\n")
    in_bt = False
    buf: list[bytes] = []
    out: list[bytes] = []
    first_y: float | None = None
    first_x: int | None = None

    def flush_block(buf_lines, y, replacement):
        """Apply replacement within buf_lines if found."""
        if replacement is None:
            return buf_lines
        old_tj, new_tj = replacement
        new_buf = []
        for bl in buf_lines:
            if bl.rstrip(b"\r\n") + b"\n" == old_tj:
                new_buf.append(new_tj.rstrip(b"\n"))
            else:
                new_buf.append(bl)
        return new_buf

    for ln in lines:
        s = ln.strip()
        if s == b"BT":
            in_bt = True
            buf = [ln]
            first_y = None
            first_x = None
        elif in_bt:
            buf.append(ln)
            if first_y is None:
                m = re.match(rb"1 0 0 1 (\d+) (\d+(?:\.\d+)?) Tm$", s)
                if m:
                    first_x = int(m.group(1))
                    first_y = float(m.group(2))
            if s == b"ET":
                in_bt = False
                rep = None
                if first_y is not None:
                    # tolerance 0.1 pt
                    for target_y, repl in replacements.items():
                        if abs(first_y - target_y) < 0.1 and first_x in (199, 294):
                            rep = repl
                            break
                buf = flush_block(buf, first_y, rep)
                out.extend(buf)
                buf = []
                first_y = None
        else:
            out.append(ln)

    return b"\n".join(out)


def _patch_page1(plain: bytes) -> bytes:
    return _patch_stream(plain, ROW_REPLACEMENTS)


def _patch_page2(plain: bytes) -> bytes:
    """Replace totals TJ lines (simple bytes substitution – they are unique)."""
    plain = plain.replace(OLD_POPO_TJ.rstrip(b"\n"), NEW_POPO_TJ.rstrip(b"\n"))
    plain = plain.replace(OLD_RASH_TJ.rstrip(b"\n"), NEW_RASH_TJ.rstrip(b"\n"))
    return plain


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    import fitz
    with open(SRC, "rb") as f:
        data = bytearray(f.read())

    xref_pos = read_xref_and_data(bytes(data))
    print(f"xref_pos={xref_pos}")

    # Verify expected xref numbers
    doc = fitz.open(SRC)
    p1_xref = doc[0].get_contents()[0]
    p2_xref = doc[1].get_contents()[0]
    doc.close()
    print(f"page1_xref={p1_xref}, page2_xref={p2_xref}")

    # ── Patch page 1 ────────────────────────────────────────────────────────
    obj1_off, _ = get_xref_entry(bytes(data), xref_pos, p1_xref)
    _, _, plain1 = decompress_stream(bytes(data), p1_xref, xref_pos)
    new_plain1 = _patch_page1(plain1)

    replaced_count = sum(
        1 for y, (old_tj, new_tj) in ROW_REPLACEMENTS.items()
        if new_tj.rstrip(b"\n") in new_plain1
    )
    print(f"page1: {replaced_count}/{len(ROW_REPLACEMENTS)} amounts replaced")

    data, delta1 = update_stream_in_pdf(data, p1_xref, xref_pos, new_plain1)
    print(f"page1 stream delta={delta1}")

    if delta1 != 0:
        xref_pos += delta1  # xref shifted because obj8 stream shrank/grew
        data = shift_xref(data, xref_pos, p1_xref, obj1_off, delta1)
        # Update startxref value at end of file
        data = bytearray(re.sub(
            rb"startxref\s+\d+\s+%%EOF",
            b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF",
            bytes(data)
        ))

    # ── Patch page 2 ────────────────────────────────────────────────────────
    obj2_off, _ = get_xref_entry(bytes(data), xref_pos, p2_xref)
    _, _, plain2 = decompress_stream(bytes(data), p2_xref, xref_pos)
    new_plain2 = _patch_page2(plain2)

    popo_ok = NEW_POPO_TJ.rstrip(b"\n") in new_plain2
    rash_ok  = NEW_RASH_TJ.rstrip(b"\n") in new_plain2
    print(f"page2: Пополнения replaced={popo_ok}, Расходы replaced={rash_ok}")

    data, delta2 = update_stream_in_pdf(data, p2_xref, xref_pos, new_plain2)
    print(f"page2 stream delta={delta2}")

    if delta2 != 0:
        xref_pos += delta2
        data = shift_xref(data, xref_pos, p2_xref, obj2_off, delta2)
        data = bytearray(re.sub(
            rb"startxref\s+\d+\s+%%EOF",
            b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF",
            bytes(data)
        ))

    # ── Write output ─────────────────────────────────────────────────────────
    with open(DST, "wb") as f:
        f.write(data)
    print(f"\nWrote {len(data)} bytes → {DST}")

    # ── Quick text verification ───────────────────────────────────────────────
    import fitz as fitz2
    doc2 = fitz2.open(DST)
    p1_words = doc2[0].get_text()
    p2_words = doc2[1].get_text()
    doc2.close()

    checks = [
        ("p1 -30 000.00",  "-30 000.00" in p1_words),
        ("p1 -9 999.00",   "-9 999.00"  in p1_words),
        ("p1 -9 998.00",   "-9 998.00"  in p1_words),
        ("p1 +50 000.00",  "+50 000.00" in p1_words),
        ("p1 -10 000.00",  "-10 000.00" in p1_words),
        ("p1 -10 001.00",  p1_words.count("-10 001.00") >= 3),
        ("p1 -30 003.00",  "-30 003.00" in p1_words),
        ("p1 +45 000.00",  "+45 000.00" in p1_words),
        ("p1 no old -30.00", "-30.00" not in p1_words),
        ("p1 no old -10.00", "-10.00" not in p1_words),
        ("p1 no old +20.00", "+20.00" not in p1_words),
        ("p1 no old +500.00","+500.00" not in p1_words),
        ("p2 Пополнения 163 256", "163 256" in p2_words),
        ("p2 Расходы 168 771",    "168 771" in p2_words),
    ]

    print("\nVerification:")
    all_ok = True
    for name, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\nAll checks passed ✓")
    else:
        print("\nSome checks FAILED ✗")


if __name__ == "__main__":
    main()
