#!/usr/bin/env python3
"""Reusable font surgery module: add missing glyphs to a PDF's embedded Tahoma subset.

Approach:
  1. Extract the embedded TTF subset from a donor PDF.
  2. Load glyphs for missing Unicode codepoints from a full Tahoma TTF (or another donor).
  3. Append those glyphs (and their composite dependencies) to the subset.
  4. Rebuild the ToUnicode CMap stream with the new CID mappings.
  5. Extend the /W array with correct PDF-space widths for the new CIDs.
  6. Replace the font stream, CMap stream, and /W array in the PDF bytes.
  7. Update /Length, xref offsets, and startxref throughout.

Usage:
    from font_extend import extend_font_in_pdf, SYSTEM_TAHOMA

    new_pdf_bytes, updated_cmap = extend_font_in_pdf(
        pdf_bytes,
        chars_needed="Bobomurodov Kh.",
        glyph_source=SYSTEM_TAHOMA,
    )
"""
from __future__ import annotations

import re
import zlib
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Optional

# Default path to the full Tahoma TTF.
# Bundled copy (fonts/tahoma.ttf) is checked first so deployment servers
# (Render.com, etc.) don't require Microsoft Office.
_BUNDLED_TAHOMA = str(Path(__file__).parent / "fonts" / "tahoma.ttf")

_TAHOMA_CANDIDATES = [
    _BUNDLED_TAHOMA,
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts/tahoma.ttf",
    "/Applications/Microsoft Excel.app/Contents/Resources/DFonts/tahoma.ttf",
    "/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts/tahoma.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Tahoma.ttf",
    "/usr/share/fonts/truetype/tahoma.ttf",
]

SYSTEM_TAHOMA: Optional[str] = next(
    (p for p in _TAHOMA_CANDIDATES if Path(p).exists()), None
)


# ---------------------------------------------------------------------------
# PDF raw helpers
# ---------------------------------------------------------------------------

def _parse_cmap(data: bytes) -> dict[int, str]:
    """Parse first ToUnicode CMap -> {unicode_codepoint: CID_hex_4char_upper}."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        uni_to_cid: dict[int, str] = {}
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
            if uni_to_cid:
                return uni_to_cid
        if b"beginbfrange" in dec:
            for mm in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec
            ):
                s = int(mm.group(1), 16)
                e = int(mm.group(2), 16)
                u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    uni_to_cid[u + i] = f"{s + i:04X}"
            if uni_to_cid:
                return uni_to_cid
    return {}


def _find_font_stream(data: bytes) -> dict | None:
    """Find the embedded TTF font program stream in the PDF."""
    for m in re.finditer(
        rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n",
        data,
        re.DOTALL,
    ):
        dict_part = m.group(2) + m.group(4)
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_num_start = m.start(3)
        if b"/Length1" not in dict_part or stream_len < 500:
            continue
        raw_stream = data[stream_start: stream_start + stream_len]
        try:
            ttf_bytes = zlib.decompress(raw_stream)
        except zlib.error:
            continue
        if ttf_bytes[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            return {
                "stream_start": stream_start,
                "stream_len": stream_len,
                "len_num_start": len_num_start,
                "compressed": raw_stream,
                "ttf": ttf_bytes,
            }
    return None


def _find_cmap_stream(data: bytes) -> dict | None:
    """Find the ToUnicode CMap stream in the PDF."""
    for m in re.finditer(
        rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n",
        data,
        re.DOTALL,
    ):
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_num_start = m.start(3)
        if stream_start + stream_len > len(data):
            continue
        raw_stream = data[stream_start: stream_start + stream_len]
        try:
            dec = zlib.decompress(raw_stream)
        except zlib.error:
            continue
        if b"beginbfchar" in dec or b"beginbfrange" in dec:
            return {
                "stream_start": stream_start,
                "stream_len": stream_len,
                "len_num_start": len_num_start,
                "dec": dec,
            }
    return None


def _update_xref(data: bytearray, changed_start: int, delta: int) -> None:
    """Shift all xref offsets > changed_start by delta, update startxref."""
    if delta == 0:
        return
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > changed_start:
                entries[em.start(1): em.start(1) + 10] = f"{offset + delta:010d}".encode()
        data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if sxref_m and changed_start < int(sxref_m.group(1)):
        pos = sxref_m.start(1)
        old_pos = int(sxref_m.group(1))
        data[pos: pos + len(str(old_pos))] = str(old_pos + delta).encode()


def _replace_stream(
    data: bytearray,
    stream_start: int,
    stream_len: int,
    len_num_start: int,
    new_compressed: bytes,
) -> bytearray:
    """Replace a stream in the PDF bytes, updating /Length and xref."""
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_compressed)).encode()
    len_delta = len(new_len_str) - len(old_len_str)
    size_delta = len(new_compressed) - stream_len + len_delta

    data = bytearray(
        bytes(data[:stream_start]) + new_compressed + bytes(data[stream_start + stream_len:])
    )
    data[len_num_start: len_num_start + len(old_len_str)] = new_len_str
    _update_xref(data, stream_start, size_delta)
    return data


# ---------------------------------------------------------------------------
# ToUnicode CMap builder
# ---------------------------------------------------------------------------

def _build_tounicode_stream(cmap: dict[int, str]) -> bytes:
    """Build a ToUnicode CMap stream matching Oracle BI Publisher format exactly.

    Matches the format found in real Alfa-Bank receipts:
      /CIDSystemInfo << /Registry (Oracle) /Ordering(UCS) /Supplement 0 >> def
      /CMapName /Oracle-Identity-UCS def
      1 begincodespacerange
      <0000> <FFFF>
      endcodespacerange
      N beginbfchar
      ...
      endbfchar
      endcmap
      CMapName currentdict /CMap defineresource pop
      end end
    """
    lines = [
        b"/CIDInit /ProcSet findresource begin",
        b"12 dict begin begincmap /CIDSystemInfo",
        b"<< /Registry (Oracle) /Ordering(UCS) /Supplement 0 >> def",
        b"/CMapName /Oracle-Identity-UCS def",
        b"1 begincodespacerange",
        b"<0000> <FFFF>",
        b"endcodespacerange",
        f"{len(cmap)} beginbfchar".encode(),
    ]
    for uni, cid in sorted(cmap.items(), key=lambda x: int(x[1], 16)):
        lines.append(f"<{cid}> <{uni:04X}>".encode())
    lines += [
        b"endbfchar",
        b"endcmap",
        b"CMapName currentdict /CMap defineresource pop",
        b"end end",
    ]
    return b"\r\n".join(lines)


def _patch_existing_cmap(dec: bytes, new_entries: dict[int, str]) -> bytes:
    """Surgically insert new bfchar entries into an existing Oracle CMap stream.

    Takes the donor's decompressed CMap, finds the 'endbfchar' line, inserts
    the new <CID> <Unicode> lines immediately before it, and updates the
    entry count on the 'N beginbfchar' line. This preserves the exact byte
    structure of the original CMap.

    Parameters
    ----------
    dec:         Decompressed CMap stream bytes.
    new_entries: {unicode_int: CID_hex_4char} for the glyphs to add.

    Returns
    -------
    New decompressed CMap bytes with the extra entries inserted.
    """
    if not new_entries:
        return dec

    # Determine the line ending style used in this CMap
    if b"\r\n" in dec:
        eol = b"\r\n"
    elif b"\r" in dec:
        eol = b"\r"
    else:
        eol = b"\n"

    # Build new bfchar lines
    new_lines = eol.join(
        f"<{cid}> <{uni:04X}>".encode()
        for uni, cid in sorted(new_entries.items(), key=lambda x: int(x[1], 16))
    )

    # Insert before 'endbfchar'
    if b"endbfchar" not in dec:
        raise ValueError("endbfchar not found in CMap stream")
    dec = dec.replace(b"endbfchar", new_lines + eol + b"endbfchar", 1)

    # Update 'N beginbfchar' count
    m = re.search(rb"(\d+)\s+beginbfchar", dec)
    if m:
        old_count = int(m.group(1))
        new_count = old_count + len(new_entries)
        dec = dec[:m.start(1)] + str(new_count).encode() + dec[m.end(1):]

    return dec


# ---------------------------------------------------------------------------
# /W array updater
# ---------------------------------------------------------------------------

def _update_w_array(data: bytearray, new_entries: dict[int, int]) -> bytearray:
    """Append new CID width entries to the /W array.

    new_entries: {CID_int: pdf_space_width_int}
    PDF space width = hmtx advance * 1000 / UPM.
    """
    if not new_entries:
        return data

    m_w = re.search(rb"/W\s*\[", bytes(data))
    if not m_w:
        return data

    depth = 1
    i = m_w.end()
    data_bytes = bytes(data)
    while i < len(data_bytes) and depth > 0:
        b = data_bytes[i: i + 1]
        if b == b"[":
            depth += 1
        elif b == b"]":
            depth -= 1
        i += 1
    w_end = i  # position AFTER closing ]

    # Detect line ending used in the /W block
    w_block = data_bytes[m_w.start(): w_end]
    if b"\r\n" in w_block:
        eol = b"\r\n"
    elif b"\n" in w_block:
        eol = b"\n"
    else:
        eol = b"\n"

    # One entry per line, matching Oracle BI Publisher format: " NN [WWW]"
    extra = b""
    for cid, width in sorted(new_entries.items()):
        extra += eol + f" {cid} [{width}]".encode()

    old_w = data_bytes[m_w.start(): w_end]
    # Insert before the final ']' — preserve existing closing bracket + space
    new_w = data_bytes[m_w.start(): w_end - 1] + extra + b" ]"
    data = bytearray(data_bytes[: m_w.start()] + new_w + data_bytes[w_end:])
    delta = len(new_w) - len(old_w)
    if delta:
        _update_xref(data, m_w.start(), delta)
    return data


# ---------------------------------------------------------------------------
# Font glyph transplant (fonttools)
# ---------------------------------------------------------------------------

def _transplant_glyphs(
    subset_ttf: bytes,
    source_ttf: bytes,
    chars_to_add: list[int],
) -> tuple[bytes, dict[int, int]]:
    """Add glyphs for unicode codepoints from source_ttf into subset_ttf.

    Returns (new_ttf_bytes, {unicode_codepoint: new_CID_int}).
    """
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph as TTGlyph

    subset = TTFont(BytesIO(subset_ttf))
    source = TTFont(BytesIO(source_ttf))

    # getBestCmap() returns {unicode_int: glyph_name_str}
    source_cmap = source.getBestCmap() or {}
    subset_go = list(subset.getGlyphOrder())
    source_go = list(source.getGlyphOrder())

    subset_glyf = subset["glyf"]
    source_glyf = source["glyf"]
    subset_hmtx = subset["hmtx"]
    source_hmtx = source["hmtx"]
    source_upm = source["head"].unitsPerEm
    subset_upm = subset["head"].unitsPerEm

    new_cids: dict[int, int] = {}

    def _collect_deps(glyph_name: str) -> list[str]:
        deps: list[str] = []
        to_visit = [glyph_name]
        seen: set[str] = set()
        while to_visit:
            name = to_visit.pop()
            if name in seen:
                continue
            seen.add(name)
            deps.append(name)
            if name in source_glyf.keys():
                g = source_glyf[name]
                if g.isComposite():
                    for comp in g.components:
                        to_visit.append(comp.glyphName)
        return deps

    for uni in chars_to_add:
        src_glyph_name = source_cmap.get(uni)  # glyph name string
        if src_glyph_name is None:
            continue
        if src_glyph_name not in source_go:
            continue
        all_deps = _collect_deps(src_glyph_name)

        donor_to_new: dict[str, str] = {}
        for dep_name in all_deps:
            new_gid = len(subset_go)
            new_name = f"ext_{new_gid}_{dep_name}"
            subset_go.append(new_name)
            donor_to_new[dep_name] = new_name

            if dep_name in source_glyf.keys():
                dep_glyph = source_glyf[dep_name]
                if dep_glyph.isComposite():
                    new_glyph = deepcopy(dep_glyph)
                    for comp in new_glyph.components:
                        if comp.glyphName in donor_to_new:
                            comp.glyphName = donor_to_new[comp.glyphName]
                    subset_glyf[new_name] = new_glyph
                else:
                    subset_glyf[new_name] = deepcopy(dep_glyph)
            else:
                subset_glyf[new_name] = TTGlyph()

            if dep_name in source_hmtx.metrics:
                adv, lsb = source_hmtx.metrics[dep_name]
                # Scale advance from source UPM to subset UPM
                scaled_adv = round(adv * subset_upm / source_upm)
                subset_hmtx.metrics[new_name] = (scaled_adv, lsb)
            else:
                subset_hmtx.metrics[new_name] = (556, 0)

        # Fix any unresolved composite component refs
        main_new_name = donor_to_new.get(src_glyph_name)
        if main_new_name and main_new_name in subset_glyf.keys():
            main_g = subset_glyf[main_new_name]
            if main_g.isComposite():
                for comp in main_g.components:
                    if comp.glyphName in donor_to_new:
                        comp.glyphName = donor_to_new[comp.glyphName]

        new_cid = subset_go.index(main_new_name)
        new_cids[uni] = new_cid

    subset.setGlyphOrder(subset_go)
    subset["maxp"].numGlyphs = len(subset_go)

    buf = BytesIO()
    subset.save(buf)
    return buf.getvalue(), new_cids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def missing_chars(pdf_bytes: bytes, text: str) -> list[int]:
    """Return list of Unicode codepoints in text that are absent from the PDF's CMap."""
    cmap = _parse_cmap(pdf_bytes)
    missing: list[int] = []
    seen: set[int] = set()
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0  # treat space as NBSP (Oracle BI Publisher style)
        if cp in seen:
            continue
        seen.add(cp)
        if cp not in cmap and cp != 0xFFFF:
            missing.append(cp)
    return missing


def extend_font_in_pdf(
    pdf_bytes: bytes,
    chars_needed: str,
    glyph_source: Optional[str] = None,
) -> tuple[bytes, dict[int, str]]:
    """Add missing glyphs for chars_needed into the PDF's embedded font subset.

    Parameters
    ----------
    pdf_bytes:      Raw bytes of the donor PDF.
    chars_needed:   String of all characters that must be renderable.
    glyph_source:   Path to a full TTF/OTF to copy glyphs from.
                    Defaults to SYSTEM_TAHOMA if None.

    Returns
    -------
    (new_pdf_bytes, cmap)  where cmap is {unicode_int: CID_hex_4char}.

    Raises
    ------
    RuntimeError if the font program or CMap stream cannot be found,
    or if glyph_source is unavailable.
    """
    source_path = glyph_source or SYSTEM_TAHOMA
    if source_path is None or not Path(source_path).exists():
        raise RuntimeError(
            "No glyph source font found. Install Microsoft Office or pass "
            "glyph_source='/path/to/tahoma.ttf'."
        )

    # Determine which codepoints are actually missing
    cmap = _parse_cmap(pdf_bytes)
    if not cmap:
        raise RuntimeError("ToUnicode CMap not found in PDF.")

    to_add: list[int] = []
    for ch in chars_needed:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp != 0xFFFF and cp not in cmap and cp not in to_add:
            to_add.append(cp)

    if not to_add:
        # Nothing to add — return unchanged PDF with existing cmap
        return pdf_bytes, cmap

    # Extract embedded font TTF
    font_info = _find_font_stream(pdf_bytes)
    if font_info is None:
        raise RuntimeError("Embedded font stream (TTF) not found in PDF.")

    cmap_info = _find_cmap_stream(pdf_bytes)
    if cmap_info is None:
        raise RuntimeError("ToUnicode CMap stream not found in PDF.")

    # Load source font
    source_ttf = Path(source_path).read_bytes()

    # Transplant glyphs
    new_ttf, new_cid_map = _transplant_glyphs(font_info["ttf"], source_ttf, to_add)

    if not new_cid_map:
        raise RuntimeError(
            f"No glyphs could be transplanted for codepoints: "
            f"{[chr(cp) for cp in to_add]}"
        )

    # Update CMap dict
    updated_cmap = dict(cmap)
    for uni_cp, new_cid_int in new_cid_map.items():
        updated_cmap[uni_cp] = f"{new_cid_int:04X}"

    # Compute /W additions (PDF text-space = advance * 1000 / UPM)
    from fontTools.ttLib import TTFont as TTF2
    new_font_obj = TTF2(BytesIO(new_ttf))
    upm = new_font_obj["head"].unitsPerEm
    go = new_font_obj.getGlyphOrder()
    hmtx = new_font_obj["hmtx"]
    w_additions: dict[int, int] = {}
    for uni_cp, new_cid_int in new_cid_map.items():
        gname = go[new_cid_int] if new_cid_int < len(go) else None
        raw_adv = hmtx.metrics.get(gname, (556, 0))[0] if gname else 556
        w_additions[new_cid_int] = round(raw_adv * 1000 / upm)

    # Build new CMap stream — surgically patch the donor's original CMap
    # to preserve Oracle BI Publisher's exact byte structure (Registry, Ordering, etc.)
    donor_cmap_dec = cmap_info["dec"]
    new_entries_only = {uni_cp: f"{new_cid_int:04X}" for uni_cp, new_cid_int in new_cid_map.items()}
    try:
        new_cmap_dec = _patch_existing_cmap(donor_cmap_dec, new_entries_only)
    except (ValueError, Exception):
        # Fallback: rebuild with Oracle format
        new_cmap_dec = _build_tounicode_stream(updated_cmap)

    new_cmap_comp = zlib.compress(new_cmap_dec, 6)
    new_font_comp = zlib.compress(new_ttf, 6)

    # Apply changes to PDF bytes — replace LATER offsets first to avoid shift errors
    data = bytearray(pdf_bytes)

    # Which stream comes last in file?
    if font_info["stream_start"] > cmap_info["stream_start"]:
        # Replace font first (it's later), then CMap
        data = _replace_stream(
            data,
            font_info["stream_start"],
            font_info["stream_len"],
            font_info["len_num_start"],
            new_font_comp,
        )
        # Update /Length1 (uncompressed font size)
        _update_length1(data, font_info["stream_start"], len(new_ttf))
        # After font replacement, CMap offsets may have shifted — re-find it
        cmap_info2 = _find_cmap_stream(bytes(data))
        if cmap_info2:
            data = _replace_stream(
                data,
                cmap_info2["stream_start"],
                cmap_info2["stream_len"],
                cmap_info2["len_num_start"],
                new_cmap_comp,
            )
    else:
        # Replace CMap first (it's later), then font
        data = _replace_stream(
            data,
            cmap_info["stream_start"],
            cmap_info["stream_len"],
            cmap_info["len_num_start"],
            new_cmap_comp,
        )
        font_info2 = _find_font_stream(bytes(data))
        if font_info2:
            data = _replace_stream(
                data,
                font_info2["stream_start"],
                font_info2["stream_len"],
                font_info2["len_num_start"],
                new_font_comp,
            )
            _update_length1(data, font_info2["stream_start"], len(new_ttf))

    # Extend /W array
    data = _update_w_array(data, w_additions)

    return bytes(data), updated_cmap


def _update_length1(data: bytearray, font_stream_start: int, new_ttf_len: int) -> None:
    """Update /Length1 (uncompressed TTF size) in the font's object dict."""
    search_start = max(0, font_stream_start - 1000)
    chunk = bytes(data[search_start:font_stream_start])
    m = re.search(rb"/Length1\s+(\d+)", chunk)
    if not m:
        return
    abs_pos = search_start + m.start(1)
    old_val = m.group(1)
    new_val = str(new_ttf_len).encode()
    if len(new_val) <= len(old_val):
        # Same or shorter: in-place with space padding
        data[abs_pos: abs_pos + len(old_val)] = new_val.ljust(len(old_val))
    else:
        # Longer: direct replacement (may shift xref, but /Length1 is in a dict
        # before the stream so it doesn't affect stream offsets)
        data[abs_pos: abs_pos + len(old_val)] = new_val


def check_chars_covered(pdf_bytes: bytes, text: str) -> tuple[bool, list[str]]:
    """Check whether all chars in text are renderable in the PDF.

    Returns (all_covered, missing_char_list).
    """
    cmap = _parse_cmap(pdf_bytes)
    missing: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp not in cmap and cp != 0xFFFF:
            if ch not in missing:
                missing.append(ch)
    return len(missing) == 0, missing


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Usage: python3 font_extend.py input.pdf output.pdf 'text with new chars'")
        sys.exit(1)

    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    text = sys.argv[3]

    pdf = inp.read_bytes()
    ok, miss = check_chars_covered(pdf, text)
    if ok:
        print("[OK] All characters already in font — no extension needed.")
        out.write_bytes(pdf)
        sys.exit(0)

    print(f"[INFO] Missing chars: {miss}")
    new_pdf, new_cmap = extend_font_in_pdf(pdf, text)
    out.write_bytes(new_pdf)
    ok2, miss2 = check_chars_covered(new_pdf, text)
    if ok2:
        print(f"[OK] Extended font written to {out}")
    else:
        print(f"[WARN] Still missing after extension: {miss2}", file=sys.stderr)
        sys.exit(1)
