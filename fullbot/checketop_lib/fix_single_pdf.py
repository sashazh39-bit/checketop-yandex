#!/usr/bin/env python3
"""Fix a single T-Bank generated PDF: inject digit glyphs from F1 into F2 (bold).

Usage:
    python fix_single_pdf.py <path/to/Tbank_SBP_*.pdf>

Output file: same name with '_fixed' suffix.
"""
from __future__ import annotations

import re
import sys
import zlib
from io import BytesIO
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("[ERROR] fontTools not installed. Run: pip install fonttools")
    sys.exit(1)

DIGIT_GIDS = list(range(305, 315))  # GIDs for '0'-'9' in TinkoffSans

DIGIT_WIDTHS = {
    305: 570, 306: 509, 307: 540, 308: 540, 309: 540,
    310: 540, 311: 540, 312: 540, 313: 540, 314: 540,
}


def _find_font_stream(pdf_data: bytes, font_variant: str):
    """Find a TinkoffSans-{variant} FontFile2 stream. Returns (start, length, len_pos, decompressed_bytes)."""
    variant_bytes = font_variant.encode()
    for m in re.finditer(rb"\d+ 0 obj", pdf_data):
        obj_start = m.start()
        chunk = pdf_data[obj_start: obj_start + 600]
        if b"TinkoffSans" not in chunk or variant_bytes not in chunk:
            continue
        if b"FontFile2" not in chunk:
            continue

        len_m = re.search(rb"/Length\s+(\d+)", chunk)
        if not len_m:
            continue
        font_len = int(len_m.group(1))
        len_pos = obj_start + len_m.start(1)

        stream_m = re.search(rb"stream\r?\n", chunk)
        if not stream_m:
            # stream marker might be beyond 600 bytes — search wider
            wide = pdf_data[obj_start: obj_start + 1200]
            stream_m = re.search(rb"stream\r?\n", wide)
            if not stream_m:
                continue
        stream_start = obj_start + stream_m.end()

        raw = pdf_data[stream_start: stream_start + font_len]
        try:
            font_bytes = zlib.decompress(raw)
        except zlib.error:
            font_bytes = raw

        return stream_start, font_len, len_pos, font_bytes

    return None


def _copy_glyph_at_gid(src_font: TTFont, dst_font: TTFont, gid: int) -> bool:
    """Copy a glyph from src to dst at the same GID position."""
    import copy

    src_table = src_font.get("glyf")
    dst_table = dst_font.get("glyf")
    if src_table is None or dst_table is None:
        return False

    src_order = src_font.getGlyphOrder()
    dst_order = dst_font.getGlyphOrder()

    if gid >= len(src_order) or gid >= len(dst_order):
        return False

    src_name = src_order[gid]
    dst_name = dst_order[gid]

    src_glyph = src_table[src_name]
    if src_glyph is None:
        return False

    # Deep-copy to avoid cross-font references
    dst_table[dst_name] = copy.deepcopy(src_glyph)

    # Also copy metrics (hmtx)
    src_hmtx = src_font.get("hmtx")
    dst_hmtx = dst_font.get("hmtx")
    if src_hmtx and dst_hmtx and src_name in src_hmtx.metrics:
        dst_hmtx.metrics[dst_name] = src_hmtx.metrics[src_name]

    return True


def _find_w_array_bounds(pdf_data: bytes, obj_start: int, chunk: bytes):
    """Find the full /W [...] array bounds using balanced-bracket matching."""
    w_key_m = re.search(rb"/W\s*\[", chunk)
    if not w_key_m:
        return None

    abs_w_start = obj_start + w_key_m.start()
    bracket_open = obj_start + w_key_m.end() - 1

    depth = 0
    search_limit = min(bracket_open + 2000, len(pdf_data))
    for i in range(bracket_open, search_limit):
        b = pdf_data[i: i + 1]
        if b == b"[":
            depth += 1
        elif b == b"]":
            depth -= 1
            if depth == 0:
                return abs_w_start, i + 1

    return None


def _update_medium_w_array(pdf_data: bytes) -> bytes:
    """Replace F2 /W array to include all digit widths."""
    for m in re.finditer(rb"\d+ 0 obj", pdf_data):
        obj_start = m.start()
        chunk = pdf_data[obj_start: obj_start + 800]
        if b"CIDFontType2" not in chunk or b"Medium" not in chunk:
            continue

        bounds = _find_w_array_bounds(pdf_data, obj_start, chunk)
        if not bounds:
            continue

        abs_w_start, abs_w_end = bounds
        new_w_content = (
            "3[200]"
            "244[675]271[390]283[544]287[418]"
            f"305[{DIGIT_WIDTHS[305]} {DIGIT_WIDTHS[306]} {DIGIT_WIDTHS[307]} "
            f"{DIGIT_WIDTHS[308]} {DIGIT_WIDTHS[309]} {DIGIT_WIDTHS[310]} "
            f"{DIGIT_WIDTHS[311]} {DIGIT_WIDTHS[312]} {DIGIT_WIDTHS[313]} "
            f"{DIGIT_WIDTHS[314]}]"
        ).encode()
        new_w_bytes = b"/W [" + new_w_content + b"]"

        old_w_bytes = pdf_data[abs_w_start:abs_w_end]
        if old_w_bytes == new_w_bytes:
            print("[INFO] /W array already correct")
            return pdf_data

        data = bytearray(pdf_data)
        data[abs_w_start:abs_w_end] = new_w_bytes
        delta = len(new_w_bytes) - (abs_w_end - abs_w_start)

        if delta != 0:
            xref_m = re.search(
                rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
                data,
            )
            if xref_m:
                entries = bytearray(xref_m.group(3))
                for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                    offset = int(em.group(1))
                    if offset > abs_w_start:
                        entries[em.start(1): em.start(1) + 10] = (
                            f"{offset + delta:010d}".encode()
                        )
                data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

            sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
            if sxref_m:
                old_sxref = int(sxref_m.group(1))
                if abs_w_start < old_sxref:
                    pos = sxref_m.start(1)
                    old_s = sxref_m.group(1)
                    data[pos: pos + len(old_s)] = str(old_sxref + delta).encode()

        print(f"[INFO] /W array updated (delta={delta:+d} bytes)")
        return bytes(data)

    print("[WARN] F2 CIDFont object not found — /W not updated")
    return pdf_data


def _patch_font_stream(pdf_data: bytes, stream_start: int, old_len: int,
                       len_pos: int, new_font_bytes: bytes) -> bytes:
    """Replace F2 font stream, fix /Length and xref/startxref."""
    new_compressed = zlib.compress(new_font_bytes, 9)
    delta = len(new_compressed) - old_len

    data = bytearray(pdf_data)
    data[stream_start: stream_start + old_len] = new_compressed

    old_len_str = str(old_len).encode()
    new_len_str = str(len(new_compressed)).encode()
    len_delta = len(new_len_str) - len(old_len_str)
    data[len_pos: len_pos + len(old_len_str)] = new_len_str

    if len_delta != 0:
        delta += len_delta
        stream_start += len_delta

    if delta != 0:
        xref_m = re.search(
            rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
            data,
        )
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1): em.start(1) + 10] = (
                        f"{offset + delta:010d}".encode()
                    )
            data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

        sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if sxref_m:
            old_sxref = int(sxref_m.group(1))
            if stream_start < old_sxref:
                pos = sxref_m.start(1)
                old_s = sxref_m.group(1)
                data[pos: pos + len(old_s)] = str(old_sxref + delta).encode()

    return bytes(data)


def fix_pdf(input_path: Path) -> Path:
    """Enrich F2 font in a generated T-Bank PDF. Returns output path."""
    pdf_data = input_path.read_bytes()
    print(f"[INFO] Input: {input_path} ({len(pdf_data)} bytes)")

    f1_info = _find_font_stream(pdf_data, "Regular")
    f2_info = _find_font_stream(pdf_data, "Medium")

    if not f1_info:
        print("[ERROR] F1 (Regular) font stream not found")
        sys.exit(1)
    if not f2_info:
        print("[ERROR] F2 (Medium) font stream not found")
        sys.exit(1)

    f1_start, f1_len, f1_len_pos, f1_bytes = f1_info
    f2_start, f2_len, f2_len_pos, f2_bytes = f2_info
    print(f"[INFO] F1 Regular: {f1_len} bytes compressed")
    print(f"[INFO] F2 Medium:  {f2_len} bytes compressed")

    f1_font = TTFont(BytesIO(f1_bytes))
    f2_font = TTFont(BytesIO(f2_bytes))

    print("[INFO] Copying all digit GIDs (305-314) from F1 → F2 ...")
    copied = []
    for gid in DIGIT_GIDS:
        if _copy_glyph_at_gid(f1_font, f2_font, gid):
            copied.append(gid)
        else:
            print(f"  [WARN] Could not copy GID {gid}")

    if not copied:
        print("[ERROR] No digits copied — aborting")
        sys.exit(1)

    digits_str = "".join(chr(0x30 + (g - 305)) for g in copied)
    print(f"[INFO] Copied digits: {digits_str}")

    buf = BytesIO()
    f2_font.save(buf)
    new_f2_bytes = buf.getvalue()
    f1_font.close()
    f2_font.close()

    print(f"[INFO] F2 size: {f2_len} → {len(new_f2_bytes)} bytes (decompressed)")
    pdf_data = _patch_font_stream(pdf_data, f2_start, f2_len, f2_len_pos, new_f2_bytes)
    pdf_data = _update_medium_w_array(pdf_data)

    stem = input_path.stem
    out_path = input_path.with_name(stem + "_fixed.pdf")
    out_path.write_bytes(pdf_data)
    print(f"[OK] Saved: {out_path} ({len(pdf_data)} bytes)")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fix_single_pdf.py <path/to/Tbank_SBP_*.pdf>")
        sys.exit(1)
    fix_pdf(Path(sys.argv[1]))
