#!/usr/bin/env python3
"""
patch_spr_statement2_v3.py

Fixes three issues in the previous patched PDF:
  1. Garbled withdrawal description ('ч'→'л', 'Советск Ru[garbage]'→'Славск Ru')
  2. Page 1 only had 10 rows — now moves rows 11-16 from p2 to p1 (fills page)
  3. PDF not recognized by checker (caused by garbled text)
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
import struct

from fontTools.ttLib import TTFont

SOURCE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "019e097f-5f5a-77f0-b923-471fee944bc3_patched.pdf"
)
BASE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "Справка о движении средств (1).pdf"
)
OUTPUT = BASE.parent / "Справка о движении средств (1)_patched.pdf"

Y_TOL = 0.15

# ---------------------------------------------------------------------------
# CID helpers
# ---------------------------------------------------------------------------

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
    out = b""
    for ch in text:
        cid = UNI_TO_CID.get(ord(ch))
        if cid is None:
            raise ValueError(f"No CID for {ch!r} (U+{ord(ch):04X})")
        out += cid.to_bytes(2, "big")
    return out


# ---------------------------------------------------------------------------
# Document number byte sequences
# ---------------------------------------------------------------------------

OLD_DOC_NUM = (
    b"\x01\x35\x00\x86\x01\x3a\x01\x35\x00\x93\x00\x80\x01\x37\x01\x37"
)
NEW_DOC_NUM = (
    b"\x01\x36\x00\x81\x00\x86\x00\x81\x01\x33\x01\x31\x01\x35\x01\x39"
)

# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

OLD_TOTAL = encode("82 414,00")
NEW_TOTAL = encode("79 035,00")

# ---------------------------------------------------------------------------
# Row merge constants (for page 1)
# ---------------------------------------------------------------------------

ROW_B_Y    = 438.78   # 20:46/20:47 +6 000.00 Пополнение СБП  → modify to 20:48 +7 979.00
ROW_C_Y    = 413.78   # 20:46/20:46 +5 358.00 Внутрибанковский → delete
MERGE_SHIFT = 25.0

# ---------------------------------------------------------------------------
# Description fix byte sequences (raw PDF stream bytes, including PDF escapes)
# ---------------------------------------------------------------------------

# Line 641 in raw p1: first line of withdrawal description "Снятие наличных. Т-Банк,"
# ч = CID 0123.  Statement 2's F1 subset has a placeholder (0 contours) at GID 291.
# Fix: copy the real 'ч' glyph from Statement 1 patched's F1 TrueType into Statement 2's
# F1 TrueType (Step 10), add CID 0123→U+0447 to the ToUnicode CMap (Step 11), and add
# the advance-width entry to the W array (Step 12).  No font-switch needed; keep CID 0123
# as-is in the F1 content stream so the weight stays Regular.
OLD_DESC_LINE1 = None   # no content-stream replacement needed for desc line 1
NEW_DESC_LINE1 = None

# Line 645 in raw p1: second line "8537 Советск Ru[garbage]" → "8537 Славск Ru"
# 'а' (CID 010C) is PDF-escaped as \f → raw bytes 01 5C 66
OLD_DESC_LINE2 = (
    b"(\x01\x39\x01\x36\x01\x34\x01\x38\x00\x03"                  # 8537
    b"\x00\xfd\x01\x1b\x01\x0e\x01\x11\x01\x1f\x01\x1e\x01\x17"  # Советск
    b"\x00\x03\x00\x4d\x00\xce"                                    # ' Ru'
    b"\x00\xc3\x00\xc3\x00\x9a\x00\x75)Tj"                       # garbage
)
NEW_DESC_LINE2 = (
    b"(\x01\x39\x01\x36\x01\x34\x01\x38\x00\x03"                  # 8537
    b"\x00\xfd\x01\x18\x01\x5c\x66\x01\x0e\x01\x1e\x01\x17"      # Славск
    b"\x00\x03\x00\x4d\x00\xce)Tj"                                # ' Ru'
)


# ---------------------------------------------------------------------------
# Tm and m/l parsing helpers
# ---------------------------------------------------------------------------

def _parse_tm(line: bytes) -> tuple[float, float] | None:
    parts = line.split()
    if len(parts) == 7 and parts[-1] == b"Tm":
        try:
            return float(parts[4]), float(parts[5])
        except ValueError:
            pass
    return None


def _parse_ml(line: bytes) -> float | None:
    """Return Y from `x y m` or `x y l`, else None."""
    parts = line.split()
    if len(parts) == 3 and parts[2] in (b"m", b"l"):
        try:
            return float(parts[1])
        except ValueError:
            pass
    return None


def _shift_line_y(line: bytes, delta: float) -> bytes:
    """Apply Y-delta to Tm or m/l lines."""
    stripped = line.strip()
    # Try Tm
    coords = _parse_tm(stripped)
    if coords is not None:
        x, y = coords
        new_y = y + delta
        parts = stripped.split()
        new_y_str = f"{new_y:.2f}".encode() if b"." in parts[5] else f"{int(new_y)}".encode()
        return b" ".join(parts[:5] + [new_y_str, b"Tm"]) + b"\n"
    # Try m/l
    y_ml = _parse_ml(stripped)
    if y_ml is not None:
        parts = stripped.split()
        new_y = y_ml + delta
        new_y_str = f"{new_y:.2f}".encode() if b"." in parts[1] else f"{int(new_y)}".encode()
        return b" ".join([parts[0], new_y_str, parts[2]]) + b"\n"
    return line if line.endswith(b"\n") else line + b"\n"


def _apply_y_shift(lines: list[bytes], start: int, end: int, delta: float,
                   y_filter_min: float = 50.0, y_filter_max: float = 1000.0) -> list[bytes]:
    """Shift all Tm and m/l Y values in lines[start:end] by delta."""
    result = []
    for i, line in enumerate(lines):
        if start <= i < end:
            stripped = line.strip()
            coords = _parse_tm(stripped)
            if coords:
                x, y = coords
                if y_filter_min <= y <= y_filter_max:
                    result.append(_shift_line_y(line, delta))
                    continue
            y_ml = _parse_ml(stripped)
            if y_ml is not None:
                if y_filter_min <= y_ml <= y_filter_max:
                    result.append(_shift_line_y(line, delta))
                    continue
        result.append(line if line.endswith(b"\n") else line + b"\n")
    return result


# ---------------------------------------------------------------------------
# Segment finding (for page 1, Y range 100-540)
# ---------------------------------------------------------------------------

def _find_p1_segment_starts(lines: list[bytes]) -> list[tuple[int, float]]:
    """Locate p1 transaction row segments anchored at x≈56, 100 < y < 540."""
    in_bt = False
    results: list[tuple[int, float]] = []
    last_y: float | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == b"BT":
            in_bt = True
        elif stripped == b"ET":
            in_bt = False
        elif in_bt and b"Tm" in stripped:
            coords = _parse_tm(stripped)
            if coords is None:
                continue
            x, y = coords
            if not (abs(x - 56.0) < 1.0 and 100 < y < 540):
                continue
            if last_y is not None and abs(last_y - y - 11.08) < Y_TOL:
                last_y = y
                continue
            last_y = y
            seg_start = i
            for j in range(i - 1, max(i - 12, -1), -1):
                if lines[j].strip() == b"1 0 0 1 0 0 cm":
                    seg_start = j
                    break
            results.append((seg_start, y))

    return results


# ---------------------------------------------------------------------------
# Page 1 merge (delete ROW C, shift rows below up by 25 pt)
# ---------------------------------------------------------------------------

def _merge_and_compact(stream: bytes) -> bytes:
    lines = stream.split(b"\n")
    seg_starts = _find_p1_segment_starts(lines)
    print(f"  [merge] Found {len(seg_starts)} segments on p1")

    seg_ranges: list[tuple[int, int, float]] = []
    for idx, (start, y) in enumerate(seg_starts):
        end = seg_starts[idx + 1][0] if idx + 1 < len(seg_starts) else len(lines)
        seg_ranges.append((start, end, y))

    # Step 1: modify Row B in-place
    B_TIME_OLD  = encode("20:46")
    B_TIME_NEW  = encode("20:48")
    B_CLEAR_OLD = encode("20:47")
    B_CLEAR_NEW = encode("20:48")
    B_AMT_OLD   = encode("+6 000.00")
    B_AMT_NEW   = encode("+7 979.00")

    for s, e, y in seg_ranges:
        if abs(y - ROW_B_Y) < Y_TOL:
            for i in range(s, e):
                lines[i] = lines[i].replace(B_TIME_OLD,  B_TIME_NEW)
                lines[i] = lines[i].replace(B_CLEAR_OLD, B_CLEAR_NEW)
                lines[i] = lines[i].replace(B_AMT_OLD,   B_AMT_NEW)
            print("  [merge] Row B modified (20:48, +7 979.00)")
            break

    # Step 2+3: delete Row C, shift rows below
    deleted_set: set[int] = set()
    shift_set:   set[int] = set()

    for s, e, y in seg_ranges:
        if abs(y - ROW_C_Y) < Y_TOL:
            deleted_set.update(range(s, e))
        elif y < ROW_C_Y - Y_TOL:
            shift_set.update(range(s, e))

    print(f"  [merge] Deleting {len(deleted_set)} lines (Row C at Y={ROW_C_Y})")

    out: list[bytes] = []
    for i, line in enumerate(lines):
        if i in deleted_set:
            continue
        stripped = line.strip()
        if i in shift_set:
            coords = _parse_tm(stripped)
            if coords:
                x, y = coords
                new_y = y + MERGE_SHIFT
                parts = stripped.split()
                new_y_str = (
                    f"{new_y:.2f}".encode() if b"." in parts[5]
                    else f"{int(new_y)}".encode()
                )
                out.append(b" ".join(parts[:5] + [new_y_str, b"Tm"]) + b"\n")
                continue
            y_ml = _parse_ml(stripped)
            if y_ml is not None:
                parts = stripped.split()
                new_y = y_ml + MERGE_SHIFT
                new_y_str = (
                    f"{new_y:.2f}".encode() if b"." in parts[1]
                    else f"{int(new_y)}".encode()
                )
                out.append(b" ".join([parts[0], new_y_str, parts[2]]) + b"\n")
                continue
        out.append(line if line.endswith(b"\n") else line + b"\n")

    return b"".join(out)


def _apply_raw_replacements(
    stream: bytes, replacements: list[tuple[bytes, bytes, str]]
) -> bytes:
    result = stream
    for old, new, desc in replacements:
        count = result.count(old)
        if count == 0:
            print(f"  [WARN] Not found: {desc}")
        else:
            result = result.replace(old, new)
            print(f"  [OK] {desc}: replaced {count} occurrence(s)")
    return result


# ---------------------------------------------------------------------------
# Find the footer-start line index in a p1 stream
# The footer begins at the BT just before the bank-name Tm at Y≈202.70
# ---------------------------------------------------------------------------

def _find_footer_start(lines: list[bytes], footer_y_approx: float) -> int:
    """
    Return the line index of the BT that starts the page footer section.
    The bank-name Tm has x=106.52 and y≈footer_y_approx.
    """
    target_x = 106.52
    for i, line in enumerate(lines):
        s = line.strip()
        if b"Tm" in s:
            coords = _parse_tm(s)
            if coords and abs(coords[0] - target_x) < 0.5 and abs(coords[1] - footer_y_approx) < 5:
                # Walk back to find BT
                for j in range(i, max(i - 5, -1), -1):
                    if lines[j].strip() == b"BT":
                        # Go back one more to include the preceding cm
                        for k in range(j, max(j - 5, -1), -1):
                            if lines[k].strip() == b"1 0 0 1 0 0 cm":
                                return k
                        return j
    return len(lines)  # not found → keep everything


# ---------------------------------------------------------------------------
# Extract BASE p1 footer bytes
# ---------------------------------------------------------------------------

def _get_base_footer(base_p1_stream: bytes) -> bytes:
    """Return the footer bytes from Statement 2's BASE page 1."""
    lines = base_p1_stream.split(b"\n")
    # Bank name Tm in BASE is at Y=52.7
    footer_start = _find_footer_start(lines, 52.7)
    return b"\n".join(lines[footer_start:])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    for path in (SOURCE, BASE):
        if not path.exists():
            print(f"[ERROR] Not found: {path}", file=sys.stderr)
            return 1

    doc_src = fitz.open(str(SOURCE))

    # ── Read SOURCE streams ───────────────────────────────────────────────
    s1_raw = doc_src.xref_stream(8)   # p1 of Statement 1 patched
    s2_raw = doc_src.xref_stream(13)  # p2 of Statement 1 patched
    doc_src.close()

    # ── Step 1: Fix garbled description line 2 (Советск → Славск) ───────────
    # Line 1 ('Снятие наличных') keeps CID 0123 as-is; the 'ч' glyph is patched
    # into Statement 2's F1 font in Steps 10-12 so no content-stream change needed.
    print("\n=== Step 1: Fix garbled withdrawal description (line 2 only) ===")
    s1_fixed = _apply_raw_replacements(s1_raw, [
        (OLD_DESC_LINE2, NEW_DESC_LINE2, "desc line2: 'Советск Ru[garbage]' → 'Славск Ru'"),
    ])

    # ── Step 2: Apply p1 merge (delete ROW C, shift below) ───────────────
    print("\n=== Step 2: Page 1 row merge ===")
    s1_merged = _merge_and_compact(s1_fixed)

    # ── Step 3: Swap doc number in p1 ────────────────────────────────────
    print("\n=== Step 3: Doc number swap ===")
    s1_merged = _apply_raw_replacements(s1_merged, [
        (OLD_DOC_NUM, NEW_DOC_NUM, "doc number 4d94fb66 → 5cdc2048"),
    ])

    # ── Step 4: Find footer start in merged p1 ───────────────────────────
    print("\n=== Step 4: Find p1 footer start (Y≈202.70) ===")
    p1_lines = s1_merged.split(b"\n")
    footer_start_idx = _find_footer_start(p1_lines, 202.70)
    print(f"  Footer starts at line {footer_start_idx}/{len(p1_lines)}")
    p1_body = b"\n".join(p1_lines[:footer_start_idx])
    print(f"  p1 body: {len(p1_body)} bytes")

    # ── Step 5: Extract p2 rows 1-6 (lines 131..1195, skip segment 0 = col header) ─
    print("\n=== Step 5: Extract & shift p2 transaction rows 1-6 (Y delta = -523) ===")
    p2_lines = s2_raw.split(b"\n")
    # Segment 0 (lines 11-130): column header row at Y=813.78 — stays on p2
    # Segment 1 (lines 131-306): first actual transaction row at Y=786.78
    # Segment 6 (lines 1015-1194): 6th actual transaction row at Y=651.78
    # Segment 7 (line 1195+): 7th transaction row — stays on p2
    P2_SEG1_START = 131   # first actual transaction row on p2
    P2_SEG7_START = 1195  # first segment that stays on p2
    # Delta: bring segment 1 (Y=786.78) to p1 slot 11 (Y=263.78)
    # 263.78 - 786.78 = -523.00  (aligns borders EXACTLY with Statement 2 BASE layout)
    Y_DELTA_P2_TO_P1 = -523.0

    # Apply Y shift to lines 131..1195
    p2_shifted_all = _apply_y_shift(
        p2_lines, P2_SEG1_START, P2_SEG7_START, Y_DELTA_P2_TO_P1,
        y_filter_min=50.0, y_filter_max=950.0
    )
    p2_rows_1_6_bytes = b"\n".join(
        p2_shifted_all[P2_SEG1_START:P2_SEG7_START]
    )
    print(f"  Extracted and shifted {P2_SEG7_START - P2_SEG1_START} lines from p2")

    # ── Step 6: Get BASE p1 footer ────────────────────────────────────────
    print("\n=== Step 6: Get BASE p1 footer ===")
    doc_base_tmp = fitz.open(str(BASE))
    base_p1_stream = doc_base_tmp.xref_stream(8)
    doc_base_tmp.close()
    base_footer = _get_base_footer(base_p1_stream)
    print(f"  BASE footer: {len(base_footer)} bytes")

    # ── Build new p1 stream ───────────────────────────────────────────────
    print("\n=== Building new p1 stream ===")
    new_p1 = p1_body + b"\n" + p2_rows_1_6_bytes + b"\n" + base_footer
    print(f"  New p1 size: {len(new_p1)} bytes")

    # ── Step 7: Build new p2 stream ───────────────────────────────────────
    # Keep segment 0 (column header at Y=813.78) + shift segments 7-10 up to fill page.
    # Bring segment 7 (Y=616.78) → Y=786.78: delta = +170
    print("\n=== Step 7: Build new p2 stream (Y delta = +170) ===")
    Y_DELTA_P2_SHIFT = +170.0

    # Keep preamble + column header (lines 0..130) intact (no Y shift)
    p2_header_part = b"\n".join(p2_lines[:P2_SEG1_START])  # lines 0..130

    # Shift segments 7-10 + totals+signature (lines 1195..end) up by +170
    p2_remaining_shifted = _apply_y_shift(
        p2_lines, P2_SEG7_START, len(p2_lines), Y_DELTA_P2_SHIFT,
        y_filter_min=50.0, y_filter_max=950.0
    )
    p2_remaining_bytes = b"\n".join(p2_remaining_shifted[P2_SEG7_START:])

    # Fix total in p2
    print("\n=== Step 8: Update пополнения total in p2 ===")
    p2_remaining_bytes = _apply_raw_replacements(p2_remaining_bytes, [
        (OLD_TOTAL, NEW_TOTAL, "82 414,00 → 79 035,00"),
    ])

    new_p2 = p2_header_part + b"\n" + p2_remaining_bytes
    print(f"  New p2 size: {len(new_p2)} bytes")

    # ── Step 9: Inject content streams into BASE document ────────────────────
    print("\n=== Step 9: Inject into BASE document ===")
    doc = fitz.open(str(BASE))
    doc.update_stream(8,  new_p1)
    doc.update_stream(13, new_p2)

    # ── Step 10: Replace BASE F1 font binary entirely with ORIG F1 ─────────────
    # ORIG (QAHIIT+TinkoffSans-Regular from Statement 1) and BASE
    # (DXXQLA+TinkoffSans-Regular) have identical numGlyphs=476 and identical
    # CID→Unicode mapping for every character except 'ч' (which BASE lacks).
    # By substituting the entire font binary, 'ч' (GID 291) comes from the
    # authentic T-Bank binary — same bytes, same hint program, same global
    # hinting tables — guaranteeing CoreText renders it identically to Statement 1.
    print("\n=== Step 10: Replace BASE F1 binary with ORIG F1 (full swap) ===")
    FONT_XREF = 16

    ORIG_SOURCE = Path(
        "/Users/aleksandrzerebatav/Downloads/"
        "019e097f-5f5a-77f0-b923-471fee944bc3.pdf"
    )

    doc_orig = fitz.open(str(ORIG_SOURCE))
    orig_font_bytes = doc_orig.xref_stream(FONT_XREF)
    doc_orig.close()

    base_font_bytes = doc.xref_stream(FONT_XREF)   # keep for Step 12 W array
    doc.update_stream(FONT_XREF, orig_font_bytes)
    print(f"  [OK] Font replaced: BASE {len(base_font_bytes)} B → ORIG {len(orig_font_bytes)} B")

    # ── Step 11: Add CID 0123 → U+0447 to F1 ToUnicode CMap (xref 19) ────────
    print("\n=== Step 11: Patch F1 ToUnicode CMap (xref 19) ===")
    CMAP_XREF = 19
    tu = doc.xref_stream(CMAP_XREF).decode('latin-1')
    if '<0123>' not in tu:
        tu = tu.replace('92 beginbfrange', '93 beginbfrange')
        tu = tu.replace(
            '<0122><0122><0445>\n',
            '<0122><0122><0445>\n<0123><0123><0447>\n',
        )
        doc.update_stream(CMAP_XREF, tu.encode('latin-1'))
        print("  [OK] CID 0123 → U+0447 ('ч') added to ToUnicode CMap")
    else:
        print("  [skip] CID 0123 already present")

    # ── Step 12: Rebuild full W array from BASE hmtx (xref 18) ──────────────
    # Use fontTools to read the BASE hmtx (no save — read-only).
    # GID 291 already has aw=455 in BASE hmtx so the W array is already correct;
    # we rebuild it to ensure it isn't truncated by fitz.
    print("\n=== Step 12: Rebuild CIDFont W array from BASE hmtx (xref 18) ===")
    CIDFONT_XREF = 18
    DW = 1000
    tt_hmtx = TTFont(BytesIO(base_font_bytes))   # read ORIGINAL base (before patch — hmtx unchanged)
    w_parts: list[str] = []
    for gid, gname in enumerate(tt_hmtx.getGlyphOrder()):
        aw_g, _ = tt_hmtx['hmtx'].metrics[gname]
        if aw_g != DW:
            w_parts.append(f"{gid} [{aw_g}]")
    new_w_str = "[" + " ".join(w_parts) + "]"
    doc.xref_set_key(CIDFONT_XREF, "W", new_w_str)
    aw_ch = tt_hmtx['hmtx'].metrics[tt_hmtx.getGlyphOrder()[291]][0]
    print(f"  [OK] W array rebuilt ({len(w_parts)} entries, CID 291 width = {aw_ch})")

    tmp = Path(tempfile.mktemp(suffix=".pdf"))
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    print(f"  Saved to temp: {tmp}")

    # Restore Statement 2's /ID
    raw_base = BASE.read_bytes()
    id_m = re.search(
        rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw_base
    )
    if id_m:
        orig_id1, orig_id2 = id_m.group(1), id_m.group(2)
        print(f"  Restoring /ID[0]: {orig_id1.decode()}")
        data = bytearray(tmp.read_bytes())
        pat = re.compile(
            rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]"
        )
        m = pat.search(bytes(data))
        if m and len(orig_id1) == len(m.group(1)):
            data[m.start(1) : m.end(1)] = orig_id1
            data[m.start(2) : m.end(2)] = orig_id2
            print("  [OK] /ID restored")
        tmp.write_bytes(bytes(data))

    shutil.move(str(tmp), str(OUTPUT))

    # ── Verify ────────────────────────────────────────────────────────────
    print("\n=== Verification ===")
    doc_out = fitz.open(str(OUTPUT))
    p1_text = doc_out[0].get_text()
    p2_text = doc_out[1].get_text()
    doc_out.close()

    checks = [
        ("08.05.2026 по 09.05.2026"  in p1_text, "Period header: 08→09.05.2026"),
        ("20:48"                      in p1_text, "20:48 operation present"),
        ("+7 979.00"                  in p1_text, "+7 979.00 amount present"),
        ("+5 358.00"      not in p1_text, "+5 358.00 (Row C) gone from p1"),
            ("наличных"                  in p1_text, "'наличных' (with ч) present in p1"),
        ("Славск"                    in p1_text, "'Славск' present (not Советск)"),
        ("Советск"        not in p1_text, "'Советск' absent from p1"),
        ("79 035,00"                  in p2_text, "пополнения = 79 035,00"),
        ("5cdc2048"                   in p1_text, "doc number = 5cdc2048"),
    ]

    all_ok = True
    for ok, name in checks:
        marker = "✓" if ok else "✗"
        print(f"  [{marker}] {name}")
        if not ok:
            all_ok = False

    # Count rows visible on each page
    def count_rows(text: str) -> int:
        import re as _re
        return len(_re.findall(r'08\.05\.2026', text))

    p1_rows = count_rows(p1_text)
    p2_rows = count_rows(p2_text)
    print(f"\n  Page 1 date occurrences: {p1_rows}")
    print(f"  Page 2 date occurrences: {p2_rows}")

    size_src = SOURCE.stat().st_size
    size_out = OUTPUT.stat().st_size
    print(f"\n[OK] Output: {OUTPUT}")
    print(f"     Source size: {size_src:,} bytes")
    print(f"     Output size: {size_out:,} bytes")

    if not all_ok:
        print("\n[WARN] Some checks failed — review output")
        return 1

    # ── Step 13: Create "Банкомат. Т-Банк," variant (no 'ч' needed) ──────────
    print("\n=== Step 13: Create 'Банкомат' variant ===")
    OUTPUT_BANK = BASE.parent / "Справка о движении средств (1)_patched_bank.pdf"

    doc_bank = fitz.open(str(OUTPUT))

    # Build full char→CID map from the installed ToUnicode CMap (ORIG, which has ч)
    cmap_raw_bank = doc_bank.xref_stream(CMAP_XREF).decode('latin-1')
    _u2c: dict[int, int] = {}
    for _m in re.finditer(r'<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>', cmap_raw_bank):
        _u2c[int(_m.group(2), 16)] = int(_m.group(1), 16)
    for _m in re.finditer(r'<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>', cmap_raw_bank):
        _s, _e, _u = int(_m.group(1), 16), int(_m.group(2), 16), int(_m.group(3), 16)
        for _i in range(_e - _s + 1):
            _u2c[_u + _i] = _s + _i

    def _enc(text: str) -> bytes:
        out = b''
        for ch in text:
            cid = _u2c.get(ord(ch))
            if cid is None:
                raise ValueError(f"No CID for '{ch}' U+{ord(ch):04X}")
            out += cid.to_bytes(2, 'big')
        return out

    new_desc = _enc('Банкомат. Т-Банк,')
    new_tj   = b'(' + new_desc + b')Tj'

    # Find description Tj: look for '(' immediately followed by CIDs for 'Снятие н'
    # Pattern: 0x28='(' 0x00 0xFD(С) 0x01 0x1A(н) — no escaping needed for these bytes
    snyt_pattern = b'\x28\x00\xFD\x01\x1A'
    p1_raw_bank = doc_bank.xref_stream(8)
    pos = p1_raw_bank.find(snyt_pattern)
    if pos == -1:
        print("  [WARN] 'Снятие' Tj not found – skipping bank variant")
        doc_bank.close()
    else:
        # Find closing ')Tj' after the found position
        end_pos = p1_raw_bank.find(b')Tj', pos + len(snyt_pattern))
        if end_pos == -1:
            print("  [WARN] Closing ')Tj' not found")
            doc_bank.close()
        else:
            old_tj = p1_raw_bank[pos : end_pos + 3]
            new_p1_bank = p1_raw_bank[:pos] + new_tj + p1_raw_bank[end_pos + 3:]
            doc_bank.update_stream(8, new_p1_bank)
            print(f"  Replaced Tj: {len(old_tj)} B → {len(new_tj)} B")
            print(f"  New description: 'Банкомат. Т-Банк,'")

            tmp_bank = Path(tempfile.mktemp(suffix=".pdf"))
            doc_bank.save(str(tmp_bank), garbage=4, deflate=True)
            doc_bank.close()

            # Restore /ID
            raw_base_bank = BASE.read_bytes()
            id_m_bank = re.search(
                rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]",
                raw_base_bank,
            )
            if id_m_bank:
                data_bank = bytearray(tmp_bank.read_bytes())
                pat_bank = re.compile(
                    rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]"
                )
                mb2 = pat_bank.search(bytes(data_bank))
                if mb2 and len(id_m_bank.group(1)) == len(mb2.group(1)):
                    data_bank[mb2.start(1):mb2.end(1)] = id_m_bank.group(1)
                    data_bank[mb2.start(2):mb2.end(2)] = id_m_bank.group(2)
                tmp_bank.write_bytes(bytes(data_bank))

            shutil.move(str(tmp_bank), str(OUTPUT_BANK))
            print(f"  [OK] Saved: {OUTPUT_BANK}")
            print(f"       Size: {OUTPUT_BANK.stat().st_size:,} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
