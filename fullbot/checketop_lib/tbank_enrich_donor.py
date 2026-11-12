#!/usr/bin/env python3
"""Enrich T-Bank donor PDFs by injecting missing digit glyphs into F2 (Medium/bold) font.

The F2 font subset in T-Bank receipts typically contains only 2-3 digit glyphs (e.g.
'0' and '2'), so amounts like '15 000' render as '000'. This script copies all missing
digit glyph outlines from F1 (Regular) into F2 (Medium) at the same GID positions
(both fonts use Identity CIDToGIDMap: CID = GID), then extends F2's /W width array.

Usage:
    python3 tbank_enrich_donor.py              # enrich all TBANK/*.pdf
    python3 tbank_enrich_donor.py TBANK/receipt_sbp_1.pdf  [output.pdf]
"""
from __future__ import annotations

import copy
import re
import sys
import zlib
from io import BytesIO
from pathlib import Path
from fontTools.ttLib import TTFont

BASE_DIR = Path(__file__).parent
TBANK_DIR = BASE_DIR / "TBANK"

# CIDs/GIDs for digits '0'-'9' (same for both F1 and F2 due to Identity mapping)
DIGIT_GIDS = list(range(305, 315))  # 305='0', 306='1', ..., 314='9'

# Desired F2 widths for digits 0-9 (from MEDIUM_WIDTHS table in tbank_cmap.py)
DIGIT_WIDTHS = {
    305: 570,  # '0'
    306: 509,  # '1'
    307: 540,  # '2'
    308: 540,  # '3'
    309: 540,  # '4'
    310: 540,  # '5'
    311: 540,  # '6'
    312: 540,  # '7'
    313: 540,  # '8'
    314: 540,  # '9'
}


def _find_font_stream(
    pdf_data: bytes, name_fragment: str
) -> tuple[int, int, int, bytes] | None:
    """Find a font's embedded stream by FontName fragment.

    Returns (stream_start, stream_len, len_num_start, decompressed_bytes).
    Matches FontDescriptor objects containing name_fragment in /FontName.
    """
    # First, find the FontDescriptor that has the name and get its FontFile2 obj ref
    for m in re.finditer(rb"(\d+)\s+0\s+obj", pdf_data):
        obj_start = m.start()
        chunk = pdf_data[obj_start : obj_start + 600]
        if b"/Type/FontDescriptor" not in chunk and b"FontDescriptor" not in chunk:
            continue
        if b"FontName" not in chunk:
            continue
        fn_m = re.search(rb"/FontName/([^\s/\]>]+)", chunk)
        if not fn_m:
            continue
        font_name = fn_m.group(1).decode("latin-1", errors="replace")
        if name_fragment not in font_name:
            continue
        ff2_m = re.search(rb"/FontFile2\s+(\d+)\s+0\s+R", chunk)
        if not ff2_m:
            continue
        stream_obj = int(ff2_m.group(1))

        # Now find that stream object
        pattern = rb"(" + str(stream_obj).encode() + rb")\s+0\s+obj\s*<<"
        for sm in re.finditer(pattern, pdf_data):
            if int(sm.group(1)) != stream_obj:
                continue
            schunk = pdf_data[sm.end() : sm.end() + 500]
            len_m = re.search(rb"/Length\s+(\d+)", schunk)
            end_m = re.search(rb">>\s*stream\r?\n", schunk)
            if not len_m or not end_m:
                continue
            stream_len = int(len_m.group(1))
            len_num_start = sm.end() + len_m.start(1)
            stream_start = sm.end() + end_m.end()
            raw = pdf_data[stream_start : stream_start + stream_len]
            try:
                dec = zlib.decompress(raw)
            except zlib.error:
                continue
            if len(dec) < 1000:
                continue
            return stream_start, stream_len, len_num_start, dec

    return None


def _glyph_is_empty(font: TTFont, gid: int) -> bool:
    """Return True if GID has no glyph outline (empty slot)."""
    order = font.getGlyphOrder()
    if gid >= len(order):
        return True
    glyf = font["glyf"]
    g = glyf[order[gid]]
    if g.numberOfContours == 0:
        return True
    if hasattr(g, "components") and g.components:
        return False
    return False


def _copy_glyph_at_gid(src_font: TTFont, dst_font: TTFont, gid: int) -> bool:
    """Copy glyph outline at GID from src into dst at the same GID slot."""
    src_order = src_font.getGlyphOrder()
    dst_order = dst_font.getGlyphOrder()
    if gid >= len(src_order) or gid >= len(dst_order):
        return False

    src_name = src_order[gid]
    dst_name = dst_order[gid]

    src_glyf = src_font["glyf"]
    dst_glyf = dst_font["glyf"]

    if src_name not in src_glyf:
        return False

    src_g = src_glyf[src_name]
    if src_g.numberOfContours == 0 and not (hasattr(src_g, "components") and src_g.components):
        return False

    dst_glyf[dst_name] = copy.deepcopy(src_g)

    # Copy hmtx metrics (advance width and lsb)
    src_hmtx = src_font["hmtx"]
    dst_hmtx = dst_font["hmtx"]
    if src_name in src_hmtx.metrics:
        dst_hmtx.metrics[dst_name] = src_hmtx.metrics[src_name]

    return True


def _delta_patch(
    pdf_data: bytes, stream_start: int, old_len: int, new_compressed: bytes
) -> bytes:
    """Replace a stream in pdf_data and delta-patch /Length, xref, startxref."""
    data = bytearray(pdf_data)
    delta = len(new_compressed) - old_len

    # 1. Replace stream bytes
    data[stream_start : stream_start + old_len] = new_compressed

    if delta == 0:
        return bytes(data)

    # 2. Patch /Length value for this stream (search backwards from stream_start)
    before = bytes(data[max(0, stream_start - 200) : stream_start])
    len_m = None
    for lm in re.finditer(rb"/Length\s+(\d+)", before):
        len_m = lm
    if len_m:
        abs_pos = (stream_start - len(before)) + len_m.start(1)
        old_len_str = len_m.group(1)
        new_len_str = str(stream_start - (stream_start - len(new_compressed) + len(new_compressed)) + len(new_compressed)).encode()
        # Simpler: just use len(new_compressed)
        new_len_str = str(len(new_compressed)).encode()
        data[abs_pos : abs_pos + len(old_len_str)] = new_len_str
        extra_delta = len(new_len_str) - len(old_len_str)
        if extra_delta != 0:
            # /Length digit count changed — update the delta
            # This is very rare (e.g. 999→1000), handle gracefully
            delta += extra_delta
            # stream_start also shifted
            stream_start += extra_delta

    # 3. Patch xref entries
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = (
                    f"{offset + delta:010d}".encode()
                )
        data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

    # 4. Patch startxref
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if sxref_m and delta != 0:
        old_sxref = int(sxref_m.group(1))
        if stream_start < old_sxref:
            new_sxref = old_sxref + delta
            pos = sxref_m.start(1)
            old_s = sxref_m.group(1)
            data[pos : pos + len(old_s)] = str(new_sxref).encode()

    return bytes(data)


def _patch_font_stream(
    pdf_data: bytes,
    stream_start: int,
    old_stream_len: int,
    len_num_start: int,
    new_font_bytes: bytes,
) -> bytes:
    """Compress new_font_bytes, replace font stream in PDF, fix /Length + xref."""
    new_compressed = zlib.compress(new_font_bytes, 9)
    delta = len(new_compressed) - old_stream_len

    data = bytearray(pdf_data)

    # 1. Replace stream
    data[stream_start : stream_start + old_stream_len] = new_compressed

    # 2. Update /Length
    old_len_str = str(old_stream_len).encode()
    new_len_str = str(len(new_compressed)).encode()
    len_delta = len(new_len_str) - len(old_len_str)
    data[len_num_start : len_num_start + len(old_len_str)] = new_len_str

    if len_delta != 0:
        # The /Length digits changed size — all subsequent byte offsets shift by this too
        # (len_num_start is before stream_start, so it shifts stream_start)
        delta += len_delta
        stream_start += len_delta

    # 3. Patch xref
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = (
                    f"{offset + delta:010d}".encode()
                )
        data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

    # 4. Patch startxref
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if sxref_m and delta != 0:
        old_sxref = int(sxref_m.group(1))
        if stream_start < old_sxref:
            new_sxref = old_sxref + delta
            pos = sxref_m.start(1)
            old_s = sxref_m.group(1)
            data[pos : pos + len(old_s)] = str(new_sxref).encode()

    return bytes(data)


def _find_w_array_bounds(pdf_data: bytes, obj_start: int, chunk: bytes) -> tuple[int, int] | None:
    """Find the byte range of the full /W [...] array in a CIDFont object.

    Returns (abs_w_start, abs_bracket_end) where abs_w_start is the position
    of the '/W' key and abs_bracket_end is the position just after the matching ']'.

    Uses proper balanced-bracket matching to handle nested sub-arrays like
    /W [3[200]305[570 509 540]] — the naive lazy regex \\[(.*?)\\] would stop
    at the first inner ']' and corrupt the replacement.
    """
    w_key_m = re.search(rb"/W\s*\[", chunk)
    if not w_key_m:
        return None

    abs_w_start = obj_start + w_key_m.start()
    bracket_open = obj_start + w_key_m.end() - 1  # position of the outer '['

    depth = 0
    search_limit = min(bracket_open + 2000, len(pdf_data))
    for i in range(bracket_open, search_limit):
        b = pdf_data[i : i + 1]
        if b == b"[":
            depth += 1
        elif b == b"]":
            depth -= 1
            if depth == 0:
                return abs_w_start, i + 1  # exclusive end

    return None  # unmatched bracket — shouldn't happen in valid PDFs


def _update_medium_w_array(pdf_data: bytes) -> bytes:
    """Replace F2's /W array in the CIDFont dict to include all digit widths."""
    # Find the CIDFont object with TinkoffSans-Medium and a /W array
    for m in re.finditer(rb"(\d+)\s+0\s+obj", pdf_data):
        obj_start = m.start()
        # Read enough to cover the obj dict
        chunk = pdf_data[obj_start : obj_start + 800]
        if b"CIDFontType2" not in chunk:
            continue
        if b"Medium" not in chunk:
            continue

        bounds = _find_w_array_bounds(pdf_data, obj_start, chunk)
        if not bounds:
            continue

        abs_w_start, abs_w_end = bounds

        # Build the new /W array content that covers all digits 305-314
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
            return pdf_data  # already done

        data = bytearray(pdf_data)
        data[abs_w_start:abs_w_end] = new_w_bytes

        delta = len(new_w_bytes) - (abs_w_end - abs_w_start)
        if delta == 0:
            return bytes(data)

        # Delta-patch xref
        xref_m = re.search(
            rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
            data,
        )
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > abs_w_start:
                    entries[em.start(1) : em.start(1) + 10] = (
                        f"{offset + delta:010d}".encode()
                    )
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if sxref_m and delta != 0:
            old_sxref = int(sxref_m.group(1))
            if abs_w_start < old_sxref:
                pos = sxref_m.start(1)
                old_s = sxref_m.group(1)
                data[pos : pos + len(old_s)] = str(old_sxref + delta).encode()

        return bytes(data)

    return pdf_data


def _replace_raw_stream(
    pdf_data: bytes,
    stream_start: int,
    old_stream_len: int,
    len_num_start: int,
    new_stream_bytes: bytes,
) -> bytes:
    """Replace an uncompressed PDF stream in-place and fix /Length + xref/startxref."""
    delta = len(new_stream_bytes) - old_stream_len

    data = bytearray(pdf_data)

    # 1. Replace stream bytes
    data[stream_start : stream_start + old_stream_len] = new_stream_bytes

    # 2. Update /Length number
    old_len_str = str(old_stream_len).encode()
    new_len_str = str(len(new_stream_bytes)).encode()
    len_delta = len(new_len_str) - len(old_len_str)
    data[len_num_start : len_num_start + len(old_len_str)] = new_len_str

    if len_delta != 0:
        delta += len_delta
        stream_start += len_delta

    if delta == 0:
        return bytes(data)

    # 3. Patch xref table
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = (
                    f"{offset + delta:010d}".encode()
                )
        data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

    # 4. Patch startxref
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if sxref_m:
        old_sxref = int(sxref_m.group(1))
        if stream_start < old_sxref:
            new_sxref = old_sxref + delta
            pos = sxref_m.start(1)
            old_s = sxref_m.group(1)
            data[pos : pos + len(old_s)] = str(new_sxref).encode()

    return bytes(data)


def _find_f2_tounicode_obj(pdf_data: bytes) -> int | None:
    """Return the object number of F2's ToUnicode stream.

    Uses the /Resources → /Font → /F2 chain to get the correct Type0 font
    object, then follows its /ToUnicode reference.  This avoids false matches
    on the string "Medium" inside the binary TTF font data.
    """
    # Step 1: find the page /Resources dict
    resources_m = re.search(rb"/Resources\s*<<([^>]*(?:<<[^>]*>>)*[^>]*)>>", pdf_data)
    if not resources_m:
        # Try indirect reference: /Resources N 0 R
        res_ref_m = re.search(rb"/Resources\s+(\d+)\s+0\s+R", pdf_data)
        if res_ref_m:
            res_obj_num = int(res_ref_m.group(1))
            res_obj_m = re.search(
                rb"(\b" + str(res_obj_num).encode() + rb")\s+0\s+obj\s*\n?<<",
                pdf_data,
            )
            if res_obj_m and int(res_obj_m.group(1)) == res_obj_num:
                resources_m = re.search(
                    rb"<<([^>]*)>>",
                    pdf_data[res_obj_m.end() : res_obj_m.end() + 800],
                )
                if resources_m:
                    resources_m = type(
                        "M", (), {"group": lambda s, n: resources_m.group(n)}
                    )()
    if not resources_m:
        return None

    # Step 2: find /Font dict inside /Resources
    # Look for /F2 reference directly in the raw PDF bytes
    f2_ref_m = re.search(rb"/F2\s+(\d+)\s+0\s+R", pdf_data)
    if not f2_ref_m:
        return None

    f2_obj_num = int(f2_ref_m.group(1))

    # Step 3: find the F2 Type0 font object and its /ToUnicode reference
    pat = re.compile(rb"(\d+)\s+0\s+obj\s*\n?<<")
    for m in re.finditer(pat, pdf_data):
        if int(m.group(1)) != f2_obj_num:
            continue
        # Read the object header (not the font stream itself)
        hdr = pdf_data[m.end() : m.end() + 500]
        tu_m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", hdr)
        if tu_m:
            return int(tu_m.group(1))
        break

    return None


def _update_f2_tounicode(pdf_data: bytes) -> bytes:
    """Add digit CID→Unicode entries to F2 (Medium) ToUnicode CMap.

    T-Bank's F2 font subset only includes ToUnicode entries for digits present
    in the original document.  After enrichment we inject extra digit glyphs,
    but without corresponding ToUnicode entries fitz decodes e.g. CID 0x0133
    as U+0133 ('ĳ') instead of U+0032 ('2').  This function adds a compact
    bfrange for all 10 digits (CIDs 0x0131–0x013a → U+0030–U+0039) to the F2
    ToUnicode stream.
    """
    tou_obj_num = _find_f2_tounicode_obj(pdf_data)
    if tou_obj_num is None:
        return pdf_data  # F2 ToUnicode not found — skip silently

    # Locate the ToUnicode stream object by scanning raw bytes
    pat = re.compile(rb"(\d+)\s+0\s+obj\s*\n?<<")
    for sm in re.finditer(pat, pdf_data):
        if int(sm.group(1)) != tou_obj_num:
            continue
        hdr_end = min(sm.end() + 600, len(pdf_data))
        chunk = pdf_data[sm.end():hdr_end]
        len_m = re.search(rb"/Length\s+(\d+)", chunk)
        end_m = re.search(rb">>\s*stream\r?\n", chunk)
        if not len_m or not end_m:
            break

        old_len = int(len_m.group(1))
        len_num_start = sm.end() + len_m.start(1)
        stream_start = sm.end() + end_m.end()
        raw_stream = pdf_data[stream_start : stream_start + old_len]

        # Decompress if compressed
        compressed = b"/Filter" in chunk[:500]
        try:
            old_stream = zlib.decompress(raw_stream) if compressed else raw_stream
        except Exception:
            old_stream = raw_stream
            compressed = False

        # Skip if digit range already present
        if b"<013a>" in old_stream or (b"<0131><013a>" in old_stream):
            return pdf_data
        # Check if all 10 digits already explicitly listed
        digit_cids_present = sum(
            1 for cid in range(0x0131, 0x013B)
            if f"<{cid:04x}>".encode() in old_stream
        )
        if digit_cids_present >= 10:
            return pdf_data

        # Build new bfrange block for digits 0–9
        digit_range = b"\n1 beginbfrange\n<0131><013a><0030>\nendbfrange\n"

        # Insert before "endcmap"
        endcmap_pos = old_stream.rfind(b"endcmap")
        if endcmap_pos == -1:
            break
        insert_at = endcmap_pos
        while insert_at > 0 and old_stream[insert_at - 1:insert_at] in (b"\n", b"\r"):
            insert_at -= 1

        new_stream = old_stream[:insert_at] + digit_range + old_stream[insert_at:]

        # Do NOT increment any existing beginbfrange count — we are adding a
        # completely separate bfrange block, not appending to an existing one.
        # Multiple beginbfrange...endbfrange sections are valid PDF CMap syntax.

        # Re-compress if the original was compressed
        final_stream = zlib.compress(new_stream, 6) if compressed else new_stream

        # If was not compressed but file has /Filter tag somehow, use plain
        return _replace_raw_stream(
            pdf_data, stream_start, old_len, len_num_start, final_stream
        )

    return pdf_data  # Could not update — return unchanged


def _find_f1_tounicode_obj(pdf_data: bytes) -> int | None:
    """Return the object number of F1's ToUnicode stream (via /F1 font reference)."""
    f1_ref_m = re.search(rb"/F1\s+(\d+)\s+0\s+R", pdf_data)
    if not f1_ref_m:
        return None
    f1_obj_num = int(f1_ref_m.group(1))
    pat = re.compile(rb"(\d+)\s+0\s+obj\s*\n?<<")
    for m in re.finditer(pat, pdf_data):
        if int(m.group(1)) != f1_obj_num:
            continue
        hdr = pdf_data[m.end() : m.end() + 500]
        tu_m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", hdr)
        if tu_m:
            return int(tu_m.group(1))
        break
    return None


def _update_f1_tounicode(pdf_data: bytes) -> bytes:
    """Add ALL Cyrillic CID→Unicode entries to F1 (Regular) ToUnicode CMap.

    T-Bank F1 font subsets only contain ToUnicode entries for characters present
    in the original document.  When we patch in bank names or sender names
    containing characters not in the original (e.g. 'б' for 'Сбербанк' when the
    donor had 'ВТБ'), fitz decodes those CIDs to wrong Unicode values.  This
    function adds bfchar entries for every Cyrillic CID in REGULAR_CID_TO_UNI
    that is missing from the existing ToUnicode CMap.
    """
    from tbank_cmap import REGULAR_CID_TO_UNI

    tou_obj_num = _find_f1_tounicode_obj(pdf_data)
    if tou_obj_num is None:
        return pdf_data

    pat = re.compile(rb"(\d+)\s+0\s+obj\s*\n?<<")
    for sm in re.finditer(pat, pdf_data):
        if int(sm.group(1)) != tou_obj_num:
            continue
        hdr_end = min(sm.end() + 600, len(pdf_data))
        chunk = pdf_data[sm.end():hdr_end]
        len_m = re.search(rb"/Length\s+(\d+)", chunk)
        end_m = re.search(rb">>\s*stream\r?\n", chunk)
        if not len_m or not end_m:
            break

        old_len = int(len_m.group(1))
        len_num_start = sm.end() + len_m.start(1)
        stream_start = sm.end() + end_m.end()
        raw_stream = pdf_data[stream_start : stream_start + old_len]

        compressed = b"/Filter" in chunk[:500]
        try:
            old_stream = zlib.decompress(raw_stream) if compressed else raw_stream
        except Exception:
            old_stream = raw_stream
            compressed = False

        # Build bfchar entries for all Cyrillic CIDs not yet in the stream
        new_entries: list[bytes] = []
        for cid, uni in sorted(REGULAR_CID_TO_UNI.items()):
            # Only add Cyrillic (and Cyrillic-adjacent) characters
            if not (0x0400 <= uni <= 0x045F):
                continue
            cid_hex = f"<{cid:04x}>".encode()
            if cid_hex in old_stream:
                continue  # already mapped
            new_entries.append(f"<{cid:04x}> <{uni:04x}>".encode())

        if not new_entries:
            return pdf_data  # nothing to add

        n = len(new_entries)
        new_block = (
            f"\n{n} beginbfchar\n".encode()
            + b"\n".join(new_entries)
            + b"\nendbfchar\n"
        )

        # Insert before "endcmap"
        endcmap_pos = old_stream.rfind(b"endcmap")
        if endcmap_pos == -1:
            break
        insert_at = endcmap_pos
        while insert_at > 0 and old_stream[insert_at - 1:insert_at] in (b"\n", b"\r"):
            insert_at -= 1

        new_stream = old_stream[:insert_at] + new_block + old_stream[insert_at:]

        final_stream = zlib.compress(new_stream, 6) if compressed else new_stream
        print(f"  F1 ToUnicode: added {n} Cyrillic bfchar entries")
        return _replace_raw_stream(
            pdf_data, stream_start, old_len, len_num_start, final_stream
        )

    return pdf_data


def enrich_pdf(input_path: Path, output_path: Path) -> bool:
    """Enrich a T-Bank donor PDF by injecting digit glyphs into F2 font.

    Returns True if the output was written (modified or already enriched).
    """
    pdf_data = input_path.read_bytes()

    # Find F1 (Regular) and F2 (Medium) font streams
    f1_info = _find_font_stream(pdf_data, "Regular")
    f2_info = _find_font_stream(pdf_data, "Medium")

    if not f1_info:
        print(f"[ERROR] F1 (Regular) font stream not found in {input_path.name}")
        return False
    if not f2_info:
        print(f"[ERROR] F2 (Medium) font stream not found in {input_path.name}")
        return False

    f1_start, f1_len, f1_len_pos, f1_bytes = f1_info
    f2_start, f2_len, f2_len_pos, f2_bytes = f2_info

    # Load fonts
    f1_font = TTFont(BytesIO(f1_bytes))
    f2_font = TTFont(BytesIO(f2_bytes))

    # Always force-copy ALL digit glyphs from F1 to F2.
    #
    # Why force-copy instead of checking _glyph_is_empty first:
    # PDF font subsets often contain "ghost" digit GIDs — glyphs that have
    # numberOfContours != 0 (so _glyph_is_empty returns False) but whose
    # actual outlines are zero-area placeholders that render as invisible.
    # Checking emptiness alone is not reliable; always copying ensures F2
    # gets proper renderable digit outlines from F1.
    print(f"[INFO] {input_path.name}: Force-copying all digit GIDs from F1 to F2")

    copied = []
    for gid in DIGIT_GIDS:
        if _copy_glyph_at_gid(f1_font, f2_font, gid):
            copied.append(gid)
        else:
            print(f"  [WARN] Could not copy GID {gid} from F1")

    if not copied:
        print(f"[ERROR] No digit glyphs copied — F1 source may be empty")
        f1_font.close()
        f2_font.close()
        return False

    # Serialize enriched F2
    buf = BytesIO()
    f2_font.save(buf)
    new_f2_bytes = buf.getvalue()
    f1_font.close()
    f2_font.close()

    digits_str = "".join(chr(0x30 + (gid - 305)) for gid in copied)
    print(f"  Injected digits: {digits_str}  ({len(f2_bytes)} -> {len(new_f2_bytes)} bytes)")

    # Patch F2 font stream in PDF
    pdf_data = _patch_font_stream(pdf_data, f2_start, f2_len, f2_len_pos, new_f2_bytes)

    # Update F2 /W array in CIDFont dict to include all digit widths
    pdf_data = _update_medium_w_array(pdf_data)

    # Update F2 ToUnicode CMap to include digit CIDs → Unicode mappings.
    # Without this, fitz (and PDF text extractors) cannot decode the new
    # digit glyphs we injected, producing garbage like U+0133 ('ĳ') for '2'.
    pdf_data = _update_f2_tounicode(pdf_data)

    # Update F1 ToUnicode CMap to include ALL Cyrillic characters.
    # The F1 subset only contains characters from the original receipt.  When
    # we patch in a bank name like 'Сбербанк' into a donor that originally had
    # 'ВТБ', the 'б' CID has no ToUnicode entry → fitz decodes it as garbage.
    pdf_data = _update_f1_tounicode(pdf_data)

    output_path.write_bytes(pdf_data)
    print(f"  Saved enriched PDF: {output_path}")
    return True


def enrich_all(tbank_dir: Path = TBANK_DIR) -> list[Path]:
    """Enrich all PDFs in tbank_dir (skip already-enriched and blocked donors).

    Returns list of output paths.
    """
    from tbank_check_service import _is_blocked_donor, donor_keywords_ok

    outputs = []
    for pdf_path in sorted(tbank_dir.glob("*.pdf")):
        if "_enriched" in pdf_path.stem:
            continue
        if _is_blocked_donor(pdf_path) or not donor_keywords_ok(pdf_path):
            continue
        out = pdf_path.with_stem(pdf_path.stem + "_enriched")
        if out.exists():
            outputs.append(out)
            continue
        if enrich_pdf(pdf_path, out):
            outputs.append(out)
    return outputs


def main() -> int:
    if len(sys.argv) >= 2:
        inp = Path(sys.argv[1])
        out = Path(sys.argv[2]) if len(sys.argv) >= 3 else inp.with_stem(inp.stem + "_enriched")
        return 0 if enrich_pdf(inp, out) else 1
    else:
        outputs = enrich_all()
        if not outputs:
            print("[WARN] No PDFs enriched")
        return 0


if __name__ == "__main__":
    sys.exit(main())
