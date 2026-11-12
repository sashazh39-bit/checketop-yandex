#!/usr/bin/env python3
"""
patch_spr_statement2.py

Produce a patched version of "Справка о движении средств (1).pdf" by:
  1. Starting from the already-patched Statement 1 (019e097f..._patched.pdf),
     which carries all correct transactions with 08.05.2026 dates.
  2. Merging the two 20:46 rows into one 20:48 +7 979 ₽ SBP deposit:
     - Row B (Tm_Y=438.78): modify times & amount in-segment (same-length swaps)
     - Row C (Tm_Y=413.78): delete, shift remaining rows up by 25 pt
  3. Updating пополнения total: 82 414,00 → 79 035,00
     (82 414 − 6 000 − 5 358 + 7 979 = 79 035)
  4. Swapping the document reference number: 4d94fb66 → 5cdc2048
  5. Restoring Statement 2's /ID.

Both PDFs are identical T-Bank templates (same person, same xref layout).
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

SOURCE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "019e097f-5f5a-77f0-b923-471fee944bc3_patched.pdf"
)
# Statement 2 is the BASE document: its fonts, /ID, and metadata are kept.
# Statement 1 patched content streams are injected into Statement 2's shell.
BASE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "Справка о движении средств (1).pdf"
)
OUTPUT = BASE.parent / "Справка о движении средств (1)_patched.pdf"


# ---------------------------------------------------------------------------
# CID encoding helpers  (CMap verified from the PDF)
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
# Document number raw byte sequences
# Verified by hex-comparing line 24 of the decompressed page-1 stream
# in both PDFs (both are 16-byte / 8-CID sequences, same length).
# ---------------------------------------------------------------------------

# "4d94fb66"
OLD_DOC_NUM = (
    b"\x01\x35"  # '4' = CID 0135
    b"\x00\x86"  # 'd' = CID 0086
    b"\x01\x3a"  # '9' = CID 013a
    b"\x01\x35"  # '4' = CID 0135
    b"\x00\x93"  # 'f' = CID 0093
    b"\x00\x80"  # 'b' = CID 0080
    b"\x01\x37"  # '6' = CID 0137
    b"\x01\x37"  # '6' = CID 0137
)

# "5cdc2048"
NEW_DOC_NUM = (
    b"\x01\x36"  # '5' = CID 0136
    b"\x00\x81"  # 'c' = CID 0081
    b"\x00\x86"  # 'd' = CID 0086
    b"\x00\x81"  # 'c' = CID 0081
    b"\x01\x33"  # '2' = CID 0133
    b"\x01\x31"  # '0' = CID 0131
    b"\x01\x35"  # '4' = CID 0135
    b"\x01\x39"  # '8' = CID 0139
)


# ---------------------------------------------------------------------------
# Page-2 totals
# ---------------------------------------------------------------------------

OLD_TOTAL = encode("82 414,00")  # 18 bytes
NEW_TOTAL = encode("79 035,00")  # 18 bytes (same length)


# ---------------------------------------------------------------------------
# Row-merge constants
# ---------------------------------------------------------------------------

ROW_B_Y = 438.78   # 20:46 / 20:47  +6 000.00  Пополнение. Система быстрых платежей
ROW_C_Y = 413.78   # 20:46 / 20:46  +5 358.00  Внутрибанковский перевод 5412889544

MERGE_SHIFT = 25.0  # one row removed → shift rows below up by 25 pt
Y_TOL = 0.15


def _near(a: float, b: float) -> bool:
    return abs(a - b) < Y_TOL


def _parse_tm(line: bytes) -> tuple[float, float] | None:
    """Return (x, y) from `1 0 0 1 x y Tm` (7 tokens), else None."""
    parts = line.split()
    if len(parts) == 7 and parts[-1] == b"Tm":
        try:
            return float(parts[4]), float(parts[5])
        except ValueError:
            pass
    return None


def _find_segment_starts(lines: list[bytes]) -> list[tuple[int, float]]:
    """
    Locate transaction row segments anchored on col-1 (x≈56) primary Tm.
    Secondary Tm lines (Y2 ≈ Y1 − 11.08) are skipped.
    """
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
            if not (_near(x, 56.0) and 100 < y < 540):
                continue
            if last_y is not None and _near(last_y - y, 11.08):
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


def _merge_and_compact(stream: bytes) -> bytes:
    """
    Step 1 – Modify Row B in-place (same-length CID byte swaps):
        20:46 → 20:48 (operation time)
        20:47 → 20:48 (clearing time)
        +6 000.00 → +7 979.00 (amount, both col3 and col4)

    Step 2 – Delete Row C entirely.

    Step 3 – Shift all remaining segments below Row C upward by 25 pt.
    """
    lines = stream.split(b"\n")
    seg_starts = _find_segment_starts(lines)
    print(f"[INFO] Found {len(seg_starts)} segments on page 1")

    # Build segment ranges
    seg_ranges: list[tuple[int, int, float]] = []
    for idx, (start, y) in enumerate(seg_starts):
        end = seg_starts[idx + 1][0] if idx + 1 < len(seg_starts) else len(lines)
        seg_ranges.append((start, end, y))

    for _, _, y in seg_ranges:
        if _near(y, ROW_B_Y):
            status = "MODIFY (Row B → 20:48 +7 979)"
        elif _near(y, ROW_C_Y):
            status = "DELETE (Row C)"
        elif y < ROW_C_Y - Y_TOL:
            status = f"shift +{MERGE_SHIFT:.0f} pt"
        else:
            status = "keep"
        print(f"       Y={y:.2f} → {status}")

    # ── Step 1: modify Row B in-place ────────────────────────────────────
    B_TIME_OLD  = encode("20:46")
    B_TIME_NEW  = encode("20:48")
    B_CLEAR_OLD = encode("20:47")
    B_CLEAR_NEW = encode("20:48")
    B_AMT_OLD   = encode("+6 000.00")
    B_AMT_NEW   = encode("+7 979.00")

    row_b_modified = False
    for s, e, y in seg_ranges:
        if _near(y, ROW_B_Y):
            for i in range(s, e):
                lines[i] = lines[i].replace(B_TIME_OLD,  B_TIME_NEW)
                lines[i] = lines[i].replace(B_CLEAR_OLD, B_CLEAR_NEW)
                lines[i] = lines[i].replace(B_AMT_OLD,   B_AMT_NEW)
            row_b_modified = True
            print("[OK] Row B modified in-place (20:48, +7 979.00)")
            break
    if not row_b_modified:
        print(f"[WARN] Row B (Y={ROW_B_Y}) not found in segments!")

    # ── Step 2+3: delete Row C and shift lower rows ───────────────────────
    deleted_set: set[int] = set()
    shift_set:   set[int] = set()

    for s, e, y in seg_ranges:
        if _near(y, ROW_C_Y):
            deleted_set.update(range(s, e))
        elif y < ROW_C_Y - Y_TOL:
            shift_set.update(range(s, e))

    if not deleted_set:
        print(f"[WARN] Row C (Y={ROW_C_Y}) not found — nothing deleted!")

    out: list[bytes] = []
    for i, line in enumerate(lines):
        if i in deleted_set:
            continue
        stripped = line.strip()
        if i in shift_set:
            # Shift Tm y values
            if b"Tm" in stripped:
                coords = _parse_tm(stripped)
                if coords is not None:
                    x, y = coords
                    new_y = y + MERGE_SHIFT
                    parts = stripped.split()
                    new_y_str = (
                        f"{new_y:.2f}".encode()
                        if b"." in parts[5]
                        else f"{int(new_y)}".encode()
                    )
                    out.append(b" ".join(parts[:5] + [new_y_str, b"Tm"]) + b"\n")
                    continue
            # Shift m/l y values
            if b" m" in stripped or b" l" in stripped:
                parts = stripped.split()
                if len(parts) == 3 and parts[2] in (b"m", b"l"):
                    try:
                        y = float(parts[1])
                        new_y = y + MERGE_SHIFT
                        new_y_str = (
                            f"{new_y:.2f}".encode()
                            if b"." in parts[1]
                            else f"{int(new_y)}".encode()
                        )
                        out.append(b" ".join([parts[0], new_y_str, parts[2]]) + b"\n")
                        continue
                    except ValueError:
                        pass
        out.append(line if line.endswith(b"\n") else line + b"\n")

    print(f"[INFO] Deleted {len(deleted_set)} lines (Row C)")
    return b"".join(out)


def _apply_raw_replacements(
    stream: bytes, replacements: list[tuple[bytes, bytes, str]]
) -> bytes:
    result = stream
    for old, new, desc in replacements:
        count = result.count(old)
        if count == 0:
            print(f"[WARN] Not found: {desc}")
        else:
            result = result.replace(old, new)
            print(f"[OK] {desc}: replaced {count} occurrence(s)")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    for path in (SOURCE, BASE):
        if not path.exists():
            print(f"[ERROR] Not found: {path}", file=sys.stderr)
            return 1

    # ── Read modified content streams from Statement 1 patched ────────────
    doc_src = fitz.open(str(SOURCE))
    print("=== Extracting streams from Statement 1 patched ===")

    # Page 1: apply merge + doc number swap
    print("\n=== Page 1: row merge + doc number swap ===")
    s1 = doc_src.xref_stream(8)
    s1 = _merge_and_compact(s1)
    s1 = _apply_raw_replacements(s1, [
        (OLD_DOC_NUM, NEW_DOC_NUM, "doc number 4d94fb66 → 5cdc2048"),
    ])

    # Page 2: update пополнения total
    print("\n=== Page 2: пополнения total ===")
    s2 = doc_src.xref_stream(13)
    s2 = _apply_raw_replacements(s2, [
        (OLD_TOTAL, NEW_TOTAL, "82 414,00 → 79 035,00"),
    ])
    doc_src.close()

    # ── Open Statement 2 as the BASE and inject modified streams ──────────
    # Statement 2's font objects remain intact, so CID 0081 → 'c' is valid.
    doc = fitz.open(str(BASE))
    doc.update_stream(8,  s1)
    doc.update_stream(13, s2)

    # ── Save ──────────────────────────────────────────────────────────────
    tmp = Path(tempfile.mktemp(suffix=".pdf"))
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    print(f"\n[INFO] Saved: {tmp}")

    # ── Restore Statement 2's /ID (it may be regenerated by fitz) ─────────
    raw_base = BASE.read_bytes()
    id_m = re.search(
        rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw_base
    )
    if id_m:
        orig_id1, orig_id2 = id_m.group(1), id_m.group(2)
        print(f"[INFO] Statement 2 /ID[0]: {orig_id1.decode()}")
        data = bytearray(tmp.read_bytes())
        pat = re.compile(
            rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]"
        )
        m = pat.search(bytes(data))
        if m and len(orig_id1) == len(m.group(1)):
            data[m.start(1) : m.end(1)] = orig_id1
            data[m.start(2) : m.end(2)] = orig_id2
            print("[INFO] /ID restored")
        tmp.write_bytes(bytes(data))

    shutil.move(str(tmp), str(OUTPUT))

    # ── Verify ────────────────────────────────────────────────────────────
    print("\n=== Verification ===")
    doc_out = fitz.open(str(OUTPUT))
    p1 = doc_out[0].get_text()
    p2 = doc_out[1].get_text()
    doc_out.close()

    checks = [
        ("08.05.2026 по 09.05.2026" in p1,      "Period header: 08.05.2026 по 09.05.2026"),
        ("20:48"      in p1,                      "20:48 operation present"),
        ("+7 979.00"  in p1,                      "+7 979.00 amount present"),
        ("Пополнение. Система" in p1,             "SBP description present in p1"),
        ("+5 358.00"  not in p1,                  "+5 358.00 (Row C) removed from p1"),
        ("+5 358.00"  not in p2,                  "+5 358.00 (Row C) absent from p2"),
        ("79 035,00"  in p2,                      "пополнения = 79 035,00"),
        ("5cdc2048"   in p1,                      "doc number = 5cdc2048"),
        ("4d94fb66"   not in p1,                  "old doc number 4d94fb66 gone"),
    ]

    all_ok = True
    for ok, name in checks:
        marker = "✓" if ok else "✗"
        print(f"  [{marker}] {name}")
        if not ok:
            all_ok = False

    size_src = SOURCE.stat().st_size
    size_out = OUTPUT.stat().st_size
    print(f"\n[OK] Output: {OUTPUT}")
    print(f"     Source size:  {size_src:,} bytes")
    print(f"     Output size:  {size_out:,} bytes")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
