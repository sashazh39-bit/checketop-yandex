#!/usr/bin/env python3
"""Patch 019e097f-5f5a-77f0-b923-471fee944bc3.pdf (T-Bank statement).

Changes:
1. "по 08.05.2026" → "по 09.05.2026"  (period end date in header)
2. "+30.00" → "+30 000.00"             (first transaction amount, both columns)
3. Remove 5 rows + compact table:
      21:03  +5 342.00  Внутрибанковский перевод 5880924161  (PDF Y=463.78)
      21:02  +5 021.00  Пополнение. Сбербанк Онлайн          (PDF Y=438.78)
      20:48  +7 979.00  Внутрибанковский перевод 5367748356  (PDF Y=388.78)
      20:47  +4 150.00  Внутрибанковский перевод 0115459642  (PDF Y=363.78)
      20:30  +5 267.00  Пополнение. Сбербанк Онлайн          (PDF Y=263.78)
4. Пополнения: 80 203,00 → 82 414,00

CID encodings (T-Bank uses raw-byte parenthesis format, not hex):
  Verified via CMap extraction from the PDF itself.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

SOURCE = Path(
    "/Users/aleksandrzerebatav/Downloads/"
    "019e097f-5f5a-77f0-b923-471fee944bc3.pdf"
)
OUTPUT = SOURCE.parent / "019e097f-5f5a-77f0-b923-471fee944bc3_patched.pdf"

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


# Pre-computed raw byte sequences for all 4 text changes
# These were verified to exist in the decompressed content streams.

# Change 1: period end date (same-length replacement, only '8'→'9')
OLD_DATE = encode("по 08.05.2026")   # 26 bytes
NEW_DATE = encode("по 09.05.2026")   # 26 bytes (same)

# Change 2: first transaction amount (replacement is 8 bytes longer)
OLD_AMOUNT = encode("+30.00")        # 12 bytes
NEW_AMOUNT = encode("+30 000.00")    # 20 bytes

# Change 4: пополнения total (same-length replacement)
OLD_TOTAL = encode("80 203,00")      # 18 bytes
NEW_TOTAL = encode("82 414,00")      # 18 bytes (same)


# ---------------------------------------------------------------------------
# Row-removal constants
# ---------------------------------------------------------------------------

DELETED_Y1: list[float] = [463.78, 438.78, 388.78, 363.78, 263.78]
ROW_HEIGHT = 25.0
Y_TOL = 0.15


def _near(a: float, b: float) -> bool:
    return abs(a - b) < Y_TOL


def _is_deleted(y: float) -> bool:
    return any(_near(y, dy) for dy in DELETED_Y1)


def _shift(y: float) -> float:
    """Upward shift (PDF coords): 25 per deleted row whose Y1 > y."""
    return ROW_HEIGHT * sum(1 for dy in DELETED_Y1 if dy > y + Y_TOL)


# ---------------------------------------------------------------------------
# Content stream manipulation
# ---------------------------------------------------------------------------

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
    """Return [(seg_start_line_idx, row_Y1)] for each transaction row.

    Anchored on the BT block whose Tm has x≈56 (column 1) and
    Y1 in the transaction area (100 < Y < 540).

    Each transaction row has two col-1 Tm lines: primary Y1 and
    secondary Y2 = Y1 - 11.08. We keep only PRIMARY (the higher one).
    Strategy: skip a Y if it is exactly 11.08 less than the previous
    detected Y (i.e. it is the secondary line of the same row).
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
            # Skip secondary Y lines (Y2 ≈ Y1 - 11.08)
            if last_y is not None and _near(last_y - y, 11.08):
                last_y = y
                continue
            last_y = y
            # Walk backward to find the nearest `1 0 0 1 0 0 cm`
            seg_start = i
            for j in range(i - 1, max(i - 12, -1), -1):
                if lines[j].strip() == b"1 0 0 1 0 0 cm":
                    seg_start = j
                    break
            results.append((seg_start, y))

    return results


def _remove_rows_and_compact(stream: bytes) -> bytes:
    """Remove 4 deleted rows and shift remaining row content upward."""
    lines = stream.split(b"\n")

    seg_starts = _find_segment_starts(lines)
    print(f"[INFO] Found {len(seg_starts)} transaction row segments")
    deleted_count = sum(1 for _, y in seg_starts if _is_deleted(y))
    print(f"[INFO] Deleting {deleted_count} rows")
    for _, y in seg_starts:
        status = "DELETE" if _is_deleted(y) else f"keep (shift={_shift(y):.0f})"
        print(f"       Y={y:.2f} → {status}")

    # Build exclusive end ranges
    seg_ranges: list[tuple[int, int, float]] = []
    for idx, (start, y1) in enumerate(seg_starts):
        end = seg_starts[idx + 1][0] if idx + 1 < len(seg_starts) else len(lines)
        seg_ranges.append((start, end, y1))

    deleted_set: set[int] = set()
    for s, e, y1 in seg_ranges:
        if _is_deleted(y1):
            deleted_set.update(range(s, e))

    shift_set: set[int] = set()
    for s, e, y1 in seg_ranges:
        if not _is_deleted(y1) and _shift(y1) > 0:
            shift_set.update(range(s, e))

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
                    new_y = y + _shift(y)
                    parts = stripped.split()
                    new_y_str = (
                        f"{new_y:.2f}".encode()
                        if b"." in parts[5]
                        else f"{int(new_y)}".encode()
                    )
                    out.append(b" ".join(parts[:5] + [new_y_str, b"Tm"]) + b"\n")
                    continue
            # Shift m/l y values  (`x y m` or `x y l`)
            if b" m" in stripped or b" l" in stripped:
                parts = stripped.split()
                if len(parts) == 3 and parts[2] in (b"m", b"l"):
                    try:
                        y = float(parts[1])
                        sh = _shift(y)
                        if sh > 0:
                            new_y = y + sh
                            new_y_str = (
                                f"{new_y:.2f}".encode()
                                if b"." in parts[1]
                                else f"{int(new_y)}".encode()
                            )
                            out.append(
                                b" ".join([parts[0], new_y_str, parts[2]]) + b"\n"
                            )
                            continue
                    except ValueError:
                        pass
        out.append(line if line.endswith(b"\n") else line + b"\n")

    return b"".join(out)


def _apply_raw_replacements(
    stream: bytes, replacements: list[tuple[bytes, bytes, str]]
) -> bytes:
    """Replace raw byte sequences in a decompressed content stream.

    replacements: [(old_bytes, new_bytes, description), ...]
    """
    result = stream
    for old, new, desc in replacements:
        count = result.count(old)
        if count == 0:
            print(f"[WARN] Not found in stream: {desc}")
        else:
            result = result.replace(old, new)
            print(f"[OK] {desc}: replaced {count} occurrence(s)")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not SOURCE.exists():
        print(f"[ERROR] Source not found: {SOURCE}", file=sys.stderr)
        return 1

    # Verify byte sequences before starting
    print("=== Pre-flight: verifying CID byte sequences ===")
    doc_check = fitz.open(str(SOURCE))
    s1_check = doc_check.xref_stream(8)
    s2_check = doc_check.xref_stream(13)
    doc_check.close()

    checks = [
        (s1_check, OLD_DATE, "по 08.05.2026 (page 1)"),
        (s1_check, OLD_AMOUNT, "+30.00 (page 1)"),
        (s2_check, OLD_TOTAL, "80 203,00 (page 2)"),
    ]
    for stream_data, target, name in checks:
        n = stream_data.count(target)
        print(f"  {name}: {n} occurrence(s) {'✓' if n > 0 else '✗ MISSING'}")
    print()

    # ── Step 1: Read original document ID ─────────────────────────────────
    raw_orig = SOURCE.read_bytes()
    id_m = re.search(
        rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw_orig
    )
    orig_id1 = id_m.group(1) if id_m else None
    orig_id2 = id_m.group(2) if id_m else None
    if orig_id1:
        print(f"[INFO] Original /ID[0]: {orig_id1.decode()}")

    # ── Step 2: Open PDF and manipulate both page streams ─────────────────
    doc = fitz.open(str(SOURCE))

    # Page 1: row removal + amount + date replacements
    print("=== Page 1: row removal + text replacements ===")
    s1 = doc.xref_stream(8)
    s1 = _remove_rows_and_compact(s1)
    s1 = _apply_raw_replacements(s1, [
        (OLD_DATE,   NEW_DATE,   "по 08.05.2026 → по 09.05.2026"),
        (OLD_AMOUNT, NEW_AMOUNT, "+30.00 → +30 000.00"),
    ])

    # Tz correction: '+' is wider than '-' in TinkoffSans; scale the new amount
    # to match the visual width of "-50 000.00" (40.91pt → 42.74pt → Tz=95.72).
    # The trailing \x00\x03 is the space CID that follows the amount in the stream.
    _AMOUNT_TJ = b"(" + NEW_AMOUNT + b"\x00\x03)Tj"
    _AMOUNT_TJ_FIXED = b"95.72 Tz\n" + _AMOUNT_TJ + b"\n100 Tz"
    _tz_count = s1.count(_AMOUNT_TJ)
    s1 = s1.replace(_AMOUNT_TJ, _AMOUNT_TJ_FIXED)
    print(f"[OK] Tz correction applied to {_tz_count} occurrence(s)")

    doc.update_stream(8, s1)

    # Page 2: пополнения total replacement
    print("\n=== Page 2: пополнения total ===")
    s2 = doc.xref_stream(13)
    s2 = _apply_raw_replacements(s2, [
        (OLD_TOTAL, NEW_TOTAL, "80 203,00 → 82 414,00"),
    ])
    doc.update_stream(13, s2)

    # ── Step 3: Save via fitz ──────────────────────────────────────────────
    tmp = Path(tempfile.mktemp(suffix=".pdf"))
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    print(f"\n[INFO] Saved intermediate: {tmp}")

    # ── Step 4: Restore original /ID ──────────────────────────────────────
    if orig_id1 and orig_id2:
        data = bytearray(tmp.read_bytes())
        pat = re.compile(
            rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]"
        )
        m = pat.search(bytes(data))
        if m:
            # Replace IDs in-place (same length)
            old_hex1 = m.group(1)
            old_hex2 = m.group(2)
            if len(orig_id1) == len(old_hex1):
                data[m.start(1):m.end(1)] = orig_id1
                data[m.start(2):m.end(2)] = orig_id2
                print("[INFO] /ID restored")
            else:
                print(f"[WARN] /ID length mismatch: {len(orig_id1)} vs {len(old_hex1)}")
        tmp.write_bytes(bytes(data))

    # Copy to final output
    import shutil
    shutil.move(str(tmp), str(OUTPUT))

    # ── Step 5: Verify ────────────────────────────────────────────────────
    print("\n=== Verification ===")
    doc_out = fitz.open(str(OUTPUT))
    p1_text = doc_out[0].get_text()
    p2_text = doc_out[1].get_text()
    doc_out.close()

    # Check date period
    if "по 09.05.2026" in p1_text:
        print("[✓] Date period changed to 09.05.2026")
    else:
        print("[✗] Date period NOT changed")

    # Check amount
    if "+30 000.00" in p1_text or "30 000.00" in p1_text:
        print("[✓] Amount changed to +30 000.00")
    else:
        print("[✗] Amount NOT changed — checking raw span data")

    # Check deleted rows
    deleted_texts = [
        "5 342.00", "5 021.00", "7 979.00",
        "с договора 5880924161", "с договора 5367748356",
    ]
    all_deleted = all(t not in p1_text for t in deleted_texts)
    if all_deleted:
        print("[✓] Deleted rows are gone from page 1")
    else:
        still_there = [t for t in deleted_texts if t in p1_text]
        print(f"[✗] Some deleted content still visible: {still_there}")

    # Check пополнения
    if "82 414,00" in p2_text:
        print("[✓] Пополнения updated to 82 414,00")
    elif "80 203,00" in p2_text:
        print("[✗] Пополнения NOT updated (still 80 203,00)")
    else:
        print("[?] Пополнения value unclear in extracted text")

    size_in = SOURCE.stat().st_size
    size_out = OUTPUT.stat().st_size
    print(f"\n[OK] Output: {OUTPUT}")
    print(f"     Input size:  {size_in:,} bytes")
    print(f"     Output size: {size_out:,} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
