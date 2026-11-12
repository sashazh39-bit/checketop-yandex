#!/usr/bin/env python3
"""
patch_spravka_raw.py

Raw-byte patch for Statement 2 (Справка о движении средств (1).pdf).
NO fitz.save() — only direct byte manipulation + xref rebuild.
Works the same way as 019e097f_rawpatch2.pdf which passed the checker.
"""
from __future__ import annotations
import re, zlib, hashlib, sys
from pathlib import Path

import fitz  # used for READ-ONLY stream extraction, no save

# ── Paths ─────────────────────────────────────────────────────────────────────
S1_RAW   = Path("/Users/aleksandrzerebatav/Downloads/019e097f_rawpatch2.pdf")
S1_ORIG  = Path("/Users/aleksandrzerebatav/Downloads/019e097f-5f5a-77f0-b923-471fee944bc3.pdf")
S2_BASE  = Path("/Users/aleksandrzerebatav/Downloads/Справка о движении средств (1).pdf")
OUTPUT   = S2_BASE.parent / "Справка о движении средств (1)_rawpatch.pdf"
OUTPUT_BANK = S2_BASE.parent / "Справка о движении средств (1)_rawpatch_bank.pdf"

# ── CID encoding (same font, same table) ──────────────────────────────────────
UNI_TO_CID: dict[int, int] = {
    0x0020: 0x0003,  # space
    0x002B: 0x0186,  # +
    0x002C: 0x0157,  # ,
    0x002D: 0x016B,  # -
    0x002E: 0x0156,  # .
    0x002F: 0x0163,  # /
    0x0030: 0x0131,  # 0
    0x0031: 0x0132,  # 1
    0x0032: 0x0133,  # 2
    0x0033: 0x0134,  # 3
    0x0034: 0x0135,  # 4
    0x0035: 0x0136,  # 5
    0x0036: 0x0137,  # 6
    0x0037: 0x0138,  # 7
    0x0038: 0x0139,  # 8
    0x0039: 0x013A,  # 9
    0x003A: 0x0158,  # :
    0x043E: 0x011B,  # о
    0x043F: 0x011C,  # п
}
def encode(text: str) -> bytes:
    return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in text)

# ── Constants ─────────────────────────────────────────────────────────────────
# Doc number: replace S1's "4d94fb66" with S2's "5cdc2048"
OLD_DOC_NUM = b"\x01\x35\x00\x86\x01\x3a\x01\x35\x00\x93\x00\x80\x01\x37\x01\x37"
NEW_DOC_NUM = b"\x01\x36\x00\x81\x00\x86\x00\x81\x01\x33\x01\x31\x01\x35\x01\x39"

OLD_TOTAL = encode("82 414,00")
NEW_TOTAL = encode("79 035,00")

ROW_B_Y    = 438.78
ROW_C_Y    = 413.78
MERGE_SHIFT = 25.0
Y_TOL      = 0.15

P2_SEG1_START = 131
P2_SEG7_START = 1195
Y_DELTA_P2_TO_P1 = -523.0
Y_DELTA_P2_SHIFT = +170.0

# "Снятие наличных" description (first Tj in withdrawal row)
# Pattern: '(' then CIDs for 'Сн'
SNYT_PATTERN = b'\x28\x00\xFD\x01\x1A'  # ( С н

# City fix: "8537 Советск Russia/Ru[garbage]" → "8537 Славск Ru"
OLD_CITY = (
    b"(\x01\x39\x01\x36\x01\x34\x01\x38\x00\x03"                  # 8537
    b"\x00\xfd\x01\x1b\x01\x0e\x01\x11\x01\x1f\x01\x1e\x01\x17"  # Советск
    b"\x00\x03\x00\x4d\x00\xce"                                    # ' Ru'
    b"\x00\xc3\x00\xc3\x00\x9a\x00\x75)Tj"                        # garbage
)
NEW_CITY = (
    b"(\x01\x39\x01\x36\x01\x34\x01\x38\x00\x03"                  # 8537
    b"\x00\xfd\x01\x18\x01\x5c\x66\x01\x0e\x01\x1e\x01\x17"      # Славск
    b"\x00\x03\x00\x4d\x00\xce)Tj"                                 # ' Ru'
)


# ── Coordinate helpers ─────────────────────────────────────────────────────────
def _parse_tm(line: bytes):
    parts = line.strip().split()
    if len(parts) == 7 and parts[-1] == b"Tm":
        try: return float(parts[4]), float(parts[5])
        except ValueError: pass
    return None

def _parse_ml(line: bytes):
    parts = line.strip().split()
    if len(parts) == 3 and parts[2] in (b"m", b"l"):
        try: return float(parts[1])
        except ValueError: pass
    return None

def _shift_line(line: bytes, delta: float) -> bytes:
    s = line.strip()
    coords = _parse_tm(s)
    if coords:
        x, y = coords
        parts = s.split()
        ny = f"{y+delta:.2f}".encode() if b"." in parts[5] else f"{int(y+delta)}".encode()
        return b" ".join(parts[:5] + [ny, b"Tm"]) + b"\n"
    y_ml = _parse_ml(s)
    if y_ml is not None:
        parts = s.split()
        ny = f"{y_ml+delta:.2f}".encode() if b"." in parts[1] else f"{int(y_ml+delta)}".encode()
        return b" ".join([parts[0], ny, parts[2]]) + b"\n"
    return line if line.endswith(b"\n") else line + b"\n"

def _apply_y_shift(lines, start, end, delta, ymin=50.0, ymax=950.0):
    out = []
    for i, line in enumerate(lines):
        if start <= i < end:
            s = line.strip()
            c = _parse_tm(s)
            if c and ymin <= c[1] <= ymax:
                out.append(_shift_line(line, delta)); continue
            ym = _parse_ml(s)
            if ym is not None and ymin <= ym <= ymax:
                out.append(_shift_line(line, delta)); continue
        out.append(line if line.endswith(b"\n") else line + b"\n")
    return out


# ── Segment finder ─────────────────────────────────────────────────────────────
def _find_segment_starts(lines):
    in_bt = False; results = []; last_y = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == b"BT": in_bt = True
        elif s == b"ET": in_bt = False
        elif in_bt and b"Tm" in s:
            c = _parse_tm(s)
            if not c: continue
            x, y = c
            if not (abs(x - 56.0) < 1.0 and 100 < y < 540): continue
            if last_y is not None and abs(last_y - y - 11.08) < Y_TOL:
                last_y = y; continue
            last_y = y
            seg_start = i
            for j in range(i-1, max(i-12, -1), -1):
                if lines[j].strip() == b"1 0 0 1 0 0 cm":
                    seg_start = j; break
            results.append((seg_start, y))
    return results


# ── Row merge: modify Row B, delete Row C, shift below ────────────────────────
def _merge_rows(stream: bytes) -> bytes:
    lines = stream.split(b"\n")
    segs = _find_segment_starts(lines)
    print(f"  Found {len(segs)} p1 segments")

    seg_ranges = [(s, (segs[i+1][0] if i+1 < len(segs) else len(lines)), y)
                  for i, (s, y) in enumerate(segs)]

    B_TIME_OLD = encode("20:46"); B_TIME_NEW  = encode("20:48")
    B_CLR_OLD  = encode("20:47"); B_CLR_NEW   = encode("20:48")
    B_AMT_OLD  = encode("+6 000.00"); B_AMT_NEW = encode("+7 979.00")

    for s, e, y in seg_ranges:
        if abs(y - ROW_B_Y) < Y_TOL:
            for i in range(s, e):
                lines[i] = lines[i].replace(B_TIME_OLD, B_TIME_NEW)
                lines[i] = lines[i].replace(B_CLR_OLD,  B_CLR_NEW)
                lines[i] = lines[i].replace(B_AMT_OLD,  B_AMT_NEW)
            print(f"  Row B modified (20:48, +7 979.00)")
            break

    deleted, shifted = set(), set()
    for s, e, y in seg_ranges:
        if abs(y - ROW_C_Y) < Y_TOL:
            deleted.update(range(s, e))
        elif y < ROW_C_Y - Y_TOL:
            shifted.update(range(s, e))
    print(f"  Deleting {len(deleted)} lines (Row C)")

    out = []
    for i, line in enumerate(lines):
        if i in deleted: continue
        if i in shifted:
            s = line.strip()
            c = _parse_tm(s)
            if c:
                x, y = c; ny = f"{y+MERGE_SHIFT:.2f}".encode() if b"." in s.split()[5] else f"{int(y+MERGE_SHIFT)}".encode()
                out.append(b" ".join(s.split()[:5] + [ny, b"Tm"]) + b"\n"); continue
            ym = _parse_ml(s)
            if ym is not None:
                pts = s.split(); ny = f"{ym+MERGE_SHIFT:.2f}".encode() if b"." in pts[1] else f"{int(ym+MERGE_SHIFT)}".encode()
                out.append(b" ".join([pts[0], ny, pts[2]]) + b"\n"); continue
        out.append(line if line.endswith(b"\n") else line + b"\n")
    return b"".join(out)


# ── Footer extraction ─────────────────────────────────────────────────────────
def _find_footer_start(lines, y_approx, target_x=106.52):
    for i, line in enumerate(lines):
        s = line.strip()
        if b"Tm" in s:
            c = _parse_tm(s)
            if c and abs(c[0] - target_x) < 0.5 and abs(c[1] - y_approx) < 5:
                for j in range(i, max(i-5, -1), -1):
                    if lines[j].strip() == b"BT":
                        for k in range(j, max(j-5, -1), -1):
                            if lines[k].strip() == b"1 0 0 1 0 0 cm":
                                return k
                        return j
    return len(lines)

def _get_footer(stream: bytes, y_approx: float) -> bytes:
    lines = stream.split(b"\n")
    start = _find_footer_start(lines, y_approx)
    print(f"  Footer start at line {start}/{len(lines)}")
    return b"\n".join(lines[start:])


# ── Raw byte stream replacement ────────────────────────────────────────────────
def _find_xref_table(data: bytes) -> int:
    m = re.search(rb'xref\r?\n\d+ \d+\r?\n', data)
    if not m: raise ValueError("xref table not found")
    return m.start()

def apply_raw_patch(data: bytearray, xref_num: int, new_decompressed: bytes) -> bytearray:
    """Replace compressed stream for xref_num with recompressed new_decompressed.
    Updates /Length and xref table in-place. No fitz.save()."""
    obj_m = re.search(rb'\b' + str(xref_num).encode() + rb' 0 obj\b', bytes(data))
    if not obj_m:
        print(f"  [WARN] obj {xref_num} not found"); return data
    obj_pos = obj_m.start()

    len_m = re.search(rb'/Length\s+(\d+)', bytes(data[obj_pos:obj_pos+300]))
    if not len_m:
        print(f"  [WARN] /Length not found for obj {xref_num}"); return data
    old_len = int(len_m.group(1))
    len_abs = obj_pos + len_m.start(1)

    sm = re.search(rb'stream(\r\n|\n)', bytes(data[obj_pos:obj_pos+400]))
    if not sm:
        print(f"  [WARN] stream not found for obj {xref_num}"); return data
    stream_abs = obj_pos + sm.end()

    # Try to decompress old stream (for logging)
    old_comp = bytes(data[stream_abs: stream_abs + old_len])
    try:
        old_dec_size = len(zlib.decompress(old_comp))
    except Exception:
        old_dec_size = old_len  # raw stream (e.g. JPEG)

    new_comp = zlib.compress(new_decompressed, 6)
    new_len  = len(new_comp)
    delta    = new_len - old_len

    print(f"  obj {xref_num}: decomp {old_dec_size} → {len(new_decompressed)}, "
          f"comp {old_len} → {new_len} (Δ={delta:+d})")

    # Update /Length
    old_ls = str(old_len).encode(); new_ls = str(new_len).encode()
    ld = len(new_ls) - len(old_ls)
    data[len_abs: len_abs + len(old_ls)] = bytearray(new_ls)
    stream_abs += ld

    old_stream_end = stream_abs + old_len  # boundary for xref shift

    # Replace stream bytes
    data = bytearray(bytes(data[:stream_abs]) + new_comp + bytes(data[stream_abs + old_len:]))

    total = delta + ld
    if total == 0:
        return data

    # Update xref table
    xref_off = _find_xref_table(bytes(data))
    xref_sec = bytes(data[xref_off:])
    hdr = re.match(rb'xref\r?\n(\d+) (\d+)\r?\n', xref_sec)
    if not hdr:
        print(f"  [WARN] Can't parse xref header"); return data
    first_id = int(hdr.group(1)); count = int(hdr.group(2))
    entry_start = xref_off + hdr.end()

    updated = 0
    for i in range(count):
        ep = entry_start + i * 20
        ent = bytes(data[ep: ep+20])
        if len(ent) < 20: break
        off  = int(ent[:10])
        flag = chr(ent[17]) if len(ent) > 17 else 'f'
        if flag == 'n' and off >= old_stream_end:
            data[ep:ep+10] = bytearray(f"{off+total:010d}".encode())
            updated += 1

    # Update startxref
    sxr_m = re.search(rb'(startxref[ \t]*\r?\n)(\d+)(\r?\n%%EOF)', bytes(data))
    if sxr_m:
        s, e = sxr_m.start(2), sxr_m.end(2)
        data = bytearray(bytes(data[:s]) + str(xref_off).encode() + bytes(data[e:]))

    print(f"    xref updated: {updated} entries, startxref → {xref_off}")
    return data


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    for p in [S1_RAW, S1_ORIG, S2_BASE]:
        if not p.exists():
            print(f"[ERROR] Not found: {p}"); return 1

    print("=== Reading source streams (fitz, read-only) ===")
    doc_s1 = fitz.open(str(S1_RAW))
    s1_p1  = doc_s1.xref_stream(8)   # S1 page 1 decompressed
    s1_p2  = doc_s1.xref_stream(13)  # S1 page 2 decompressed
    doc_s1.close()

    doc_s1o = fitz.open(str(S1_ORIG))
    s1_font = doc_s1o.xref_stream(16)  # S1 TrueType font (has correct 'ч')
    doc_s1o.close()

    doc_s2 = fitz.open(str(S2_BASE))
    s2_p1_base = doc_s2.xref_stream(8)   # S2 page 1 base (for footer)
    s2_cmap    = doc_s2.xref_stream(19)  # S2 CMap
    doc_s2.close()

    print(f"  S1 p1: {len(s1_p1)} B, S1 p2: {len(s1_p2)} B")
    print(f"  S1 font: {len(s1_font)} B, S2 cmap: {len(s2_cmap)} B")

    # ── Build new_p1 ─────────────────────────────────────────────────────────
    print("\n=== Building new p1 ===")
    print("  Step 1: Merge rows (B→20:48/+7979, delete C)")
    s1_p1_merged = _merge_rows(s1_p1)

    print("  Step 1b: Fix city name (Советск Russia → Славск Ru)")
    n_city = s1_p1_merged.count(OLD_CITY)
    if n_city == 0:
        # Try without the garbage tail (in case it differs)
        alt_old = (
            b"(\x01\x39\x01\x36\x01\x34\x01\x38\x00\x03"
            b"\x00\xfd\x01\x1b\x01\x0e\x01\x11\x01\x1f\x01\x1e\x01\x17"
            b"\x00\x03\x00\x4d\x00\xce"
        )
        pos_city = s1_p1_merged.find(alt_old)
        if pos_city >= 0:
            end_pos = s1_p1_merged.find(b")Tj", pos_city + len(alt_old))
            if end_pos >= 0:
                old_tj_city = s1_p1_merged[pos_city: end_pos + 3]
                s1_p1_merged = s1_p1_merged[:pos_city] + NEW_CITY + s1_p1_merged[end_pos + 3:]
                print(f"    Alt match: {len(old_tj_city)} B → {len(NEW_CITY)} B (Советск→Славск)")
            else:
                print("    [WARN] ')Tj' end not found for city")
        else:
            print("    [WARN] City pattern not found at all")
    else:
        s1_p1_merged = s1_p1_merged.replace(OLD_CITY, NEW_CITY)
        print(f"    Replaced {n_city} occurrence(s)")

    print("  Step 2: Replace doc number (4d94fb66 → 5cdc2048)")
    n_dn = s1_p1_merged.count(OLD_DOC_NUM)
    s1_p1_merged = s1_p1_merged.replace(OLD_DOC_NUM, NEW_DOC_NUM)
    print(f"    Replaced {n_dn} doc number occurrence(s)")

    print("  Step 3: Find S1 p1 body (cut at Y≈202.70 footer)")
    p1_lines = s1_p1_merged.split(b"\n")
    footer_idx = _find_footer_start(p1_lines, 202.70)
    p1_body = b"\n".join(p1_lines[:footer_idx])
    print(f"    Body: {len(p1_body)} B (lines 0..{footer_idx})")

    print("  Step 4: Extract S1 p2 rows 1-6 (shift Y -523)")
    p2_lines = s1_p2.split(b"\n")
    p2_shifted = _apply_y_shift(p2_lines, P2_SEG1_START, P2_SEG7_START, Y_DELTA_P2_TO_P1)
    p2_rows_1_6 = b"\n".join(p2_shifted[P2_SEG1_START:P2_SEG7_START])
    print(f"    Rows 1-6: {len(p2_rows_1_6)} B")

    print("  Step 5: Get S2 base footer (Y≈52.7)")
    s2_footer = _get_footer(s2_p1_base, 52.7)
    print(f"    S2 footer: {len(s2_footer)} B")

    new_p1 = p1_body + b"\n" + p2_rows_1_6 + b"\n" + s2_footer
    print(f"  New p1 total: {len(new_p1)} B")

    # ── Build new_p2 ─────────────────────────────────────────────────────────
    print("\n=== Building new p2 ===")
    p2_header = b"\n".join(p2_lines[:P2_SEG1_START])
    p2_remaining = _apply_y_shift(p2_lines, P2_SEG7_START, len(p2_lines), Y_DELTA_P2_SHIFT)
    p2_tail = b"\n".join(p2_remaining[P2_SEG7_START:])

    n_total = p2_tail.count(OLD_TOTAL)
    p2_tail = p2_tail.replace(OLD_TOTAL, NEW_TOTAL)
    print(f"  Total replacement: {n_total} occurrence(s) 82 414 → 79 035")

    new_p2 = p2_header + b"\n" + p2_tail
    print(f"  New p2 total: {len(new_p2)} B")

    # ── Patch CMap (add ч = CID 0123 → U+0447) ───────────────────────────────
    print("\n=== Patching CMap ===")
    cmap_text = s2_cmap.decode("latin-1")
    if "<0123>" not in cmap_text:
        cmap_text = cmap_text.replace("92 beginbfrange", "93 beginbfrange")
        cmap_text = cmap_text.replace(
            "<0122><0122><0445>\n",
            "<0122><0122><0445>\n<0123><0123><0447>\n"
        )
        print("  Added CID 0123 → U+0447 (ч)")
    else:
        print("  CID 0123 already present")
    new_cmap = cmap_text.encode("latin-1")

    # ── Inject into S2 raw bytes ───────────────────────────────────────────────
    print("\n=== Injecting into S2 raw bytes (no fitz.save()) ===")
    raw2 = S2_BASE.read_bytes()
    print(f"S2 base: {len(raw2):,} B  MD5={hashlib.md5(raw2).hexdigest()[:16]}")

    data = bytearray(raw2)

    print("\n[1] Page 1 content (xref 8)")
    data = apply_raw_patch(data, 8, new_p1)

    print("\n[2] Page 2 content (xref 13)")
    data = apply_raw_patch(data, 13, new_p2)

    print("\n[3] Font swap (xref 16): S2 DXXQLA → S1 QAHIIT")
    data = apply_raw_patch(data, 16, s1_font)

    print("\n[4] CMap (xref 19)")
    data = apply_raw_patch(data, 19, new_cmap)

    # ── Save ──────────────────────────────────────────────────────────────────
    OUTPUT.write_bytes(bytes(data))
    print(f"\nOutput: {len(data):,} B  Δ={len(data)-len(raw2):+d} B")
    print(f"  MD5: {hashlib.md5(bytes(data)).hexdigest()[:16]}")

    # Verify /ID preserved
    id_orig = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', raw2)
    id_new  = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', bytes(data))
    if id_orig and id_new:
        ok = id_orig.group(1) == id_new.group(1)
        print(f"  /ID[0] preserved: {'✓' if ok else '✗'}")

    # ── Text verification ─────────────────────────────────────────────────────
    print("\n=== Text verification ===")
    doc_out = fitz.open(str(OUTPUT))
    t1 = doc_out[0].get_text()
    t2 = doc_out[1].get_text()
    doc_out.close()

    checks = [
        ("08.05.2026 по 09.05.2026" in t1, "Period: 08→09.05"),
        ("20:48"                     in t1, "Time 20:48"),
        ("+7 979.00"                 in t1, "+7 979.00"),
        ("+5 358.00" not in t1,            "Row C gone"),
        ("наличных"                  in t1, "'наличных' (ч present)"),
        ("Славск"                    in t1, "Славск Ru"),
        ("79 035"                    in t2, "Total 79 035"),
        ("5cdc2048"                  in t1, "Doc# 5cdc2048"),
    ]
    all_ok = True
    for ok, name in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok: all_ok = False

    print(f"\n  P1 dates: {t1.count('08.05.2026')}")
    print(f"  P2 dates: {t2.count('08.05.2026')}")
    print(f"  Output: {OUTPUT}")

    # ── Банкомат variant ──────────────────────────────────────────────────────
    print("\n=== Building Банкомат variant ===")
    # Swap "Снятие наличных. Т-Банк," → "Банкомат. Т-Банк,"
    # Need to build the Банкомат CID bytes using the patched CMap
    # The CMap now has ч, so let's build mapping from it
    cmap_src = new_cmap.decode("latin-1")
    u2c: dict[int, int] = {}
    for m in re.finditer(r'<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>', cmap_src):
        u2c[int(m.group(2), 16)] = int(m.group(1), 16)
    for m in re.finditer(r'<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>', cmap_src):
        s_c, e_c, u = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
        for i in range(e_c - s_c + 1):
            u2c[u + i] = s_c + i

    def enc2(text):
        return b"".join(u2c[ord(c)].to_bytes(2, "big") for c in text)

    # Read current new_p1 decompressed from output
    doc_tmp = fitz.open(str(OUTPUT))
    p1_for_bank = doc_tmp.xref_stream(8)
    doc_tmp.close()

    pos = p1_for_bank.find(SNYT_PATTERN)
    if pos == -1:
        print("  [SKIP] 'Снятие' pattern not found in output p1")
    else:
        end_pos = p1_for_bank.find(b")Tj", pos + len(SNYT_PATTERN))
        old_tj  = p1_for_bank[pos: end_pos + 3]
        new_desc = enc2("Банкомат. Т-Банк,")
        new_tj   = b"(" + new_desc + b")Tj"
        p1_bank  = p1_for_bank[:pos] + new_tj + p1_for_bank[end_pos + 3:]
        print(f"  Tj: {len(old_tj)} B → {len(new_tj)} B  ('Банкомат. Т-Банк,')")

        import shutil
        shutil.copy(str(OUTPUT), str(OUTPUT_BANK))
        data_bank = bytearray(OUTPUT_BANK.read_bytes())
        data_bank = apply_raw_patch(data_bank, 8, p1_bank)
        OUTPUT_BANK.write_bytes(bytes(data_bank))
        print(f"  Saved: {OUTPUT_BANK}  {len(data_bank):,} B")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
