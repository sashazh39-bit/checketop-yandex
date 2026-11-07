#!/usr/bin/env python3
"""Font helpers for WB Bank SBP receipts.

WB receipts embed Google Sans (Regular / Medium / Bold) as Type0 /
CIDFontType2 subsets with ``/CIDToGIDMap /Identity``.  CID == GID in the
original 7525-glyph Google Sans, and OpenPDF keeps that order: unused
slots are empty outlines with the original ``hmtx`` advances still in
place.  A character can therefore be grown into a template by copying
the glyf bytes at the *same* GID from another genuine WB receipt — the
outlines of digit ``1`` are byte-identical across all four donors.

ToUnicode / ``/W`` / CIDSet / prune are the same OpenPDF TTX+0 pipeline
as Yandex (``yandex_font``); glyf injection is local so a fontTools
re-save never rewrites ``head.modified`` or table order.
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yandex_font  # noqa: E402

# Re-export the CMap / width / literal / prune helpers used by the receipt.
parse_tounicode = yandex_font.parse_tounicode
build_tounicode = yandex_font.build_tounicode
parse_widths = yandex_font.parse_widths
replace_widths = yandex_font.replace_widths
encode_literal = yandex_font.encode_literal
decode_literal = yandex_font.decode_literal
cids_of_text = yandex_font.cids_of_text
raw_of_cids = yandex_font.raw_of_cids
text_of_raw = yandex_font.text_of_raw
parse_cidset = yandex_font.parse_cidset
build_cidset_exact = yandex_font.build_cidset_exact
prune_ttf = yandex_font.prune_ttf
glyph_has_outline = yandex_font.glyph_has_outline
read_ttf = yandex_font.read_ttf
invert_cmap = yandex_font.invert_cmap
num_glyphs = yandex_font.num_glyphs

_SPACE_CHARS = set(" \u00a0\u2009\u202f")


# --------------------------------------------------------------------------
# raw SFNT (same layout OpenPDF writes; table order preserved)
# --------------------------------------------------------------------------

def _sfnt_tables(data: bytes):
    num = struct.unpack(">H", data[4:6])[0]
    records = []
    for index in range(num):
        rec = data[12 + index * 16 : 12 + (index + 1) * 16]
        tag = rec[:4].decode("latin-1")
        checksum, offset, length = struct.unpack(">III", rec[4:16])
        records.append((tag, checksum, offset, length))
    return records


def _sfnt_checksum(payload: bytes) -> int:
    if len(payload) % 4:
        payload = payload + b"\0" * (4 - len(payload) % 4)
    total = 0
    for (value,) in struct.iter_unpack(">I", payload):
        total = (total + value) & 0xFFFFFFFF
    return total


def _loca_offsets(ttf_bytes: bytes):
    records = _sfnt_tables(ttf_bytes)
    tables = {tag: ttf_bytes[off : off + ln] for tag, _c, off, ln in records}
    loc_format = struct.unpack(">h", tables["head"][50:52])[0]
    loca_raw = tables["loca"]
    if loc_format == 0:
        offsets = [v * 2 for (v,) in struct.iter_unpack(">H", loca_raw)]
    else:
        offsets = [v for (v,) in struct.iter_unpack(">I", loca_raw)]
    return records, tables, loc_format, offsets


def glyph_blob(ttf_bytes: bytes, gid: int) -> bytes:
    """Raw ``glyf`` slice of ``gid`` (empty bytes when the slot is vacant)."""
    _records, tables, _fmt, offsets = _loca_offsets(ttf_bytes)
    if gid < 0 or gid + 1 >= len(offsets):
        return b""
    return tables["glyf"][offsets[gid] : offsets[gid + 1]]


def glyph_components(blob: bytes) -> Tuple[int, ...]:
    """Component GIDs of a composite glyf blob (empty when simple)."""
    if len(blob) < 10:
        return ()
    contours = struct.unpack(">h", blob[:2])[0]
    if contours >= 0:
        return ()
    components = []
    pos = 10
    while pos + 4 <= len(blob):
        flags, comp_gid = struct.unpack(">HH", blob[pos : pos + 4])
        pos += 4
        pos += 4 if flags & 0x0001 else 2
        if flags & 0x0008:
            pos += 2
        elif flags & 0x0040:
            pos += 4
        elif flags & 0x0080:
            pos += 8
        components.append(comp_gid)
        if not flags & 0x0020:
            break
    return tuple(components)


def inject_raw_glyphs(ttf_bytes: bytes, gid_blobs: Dict[int, bytes]) -> bytes:
    """Drop raw glyf blobs into existing GID slots; keep every other table.

    Empty unused slots already occupy a GID in these subsets, so a
    harvested outline of the same GID replaces a zero-length glyph
    without growing ``maxp.numGlyphs`` or rewriting ``hmtx``.
    """
    records, tables, loc_format, offsets = _loca_offsets(ttf_bytes)
    glyf = tables["glyf"]
    num = len(offsets) - 1
    new_glyf = bytearray()
    new_offsets = []
    for gid in range(num):
        new_offsets.append(len(new_glyf))
        if gid in gid_blobs:
            blob = gid_blobs[gid]
        else:
            blob = glyf[offsets[gid] : offsets[gid + 1]]
        new_glyf += blob
        if len(new_glyf) % 2:
            new_glyf += b"\0"
    new_offsets.append(len(new_glyf))
    if loc_format == 0:
        tables["loca"] = b"".join(struct.pack(">H", off // 2) for off in new_offsets)
    else:
        tables["loca"] = b"".join(struct.pack(">I", off) for off in new_offsets)
    tables["glyf"] = bytes(new_glyf)

    out = bytearray(ttf_bytes[:12])
    body = bytearray()
    offset = 12 + len(records) * 16
    new_records = []
    for tag, checksum, _off, _ln in sorted(records, key=lambda r: r[2]):
        payload = tables[tag]
        if tag in ("glyf", "loca"):
            checksum = _sfnt_checksum(payload)
        new_records.append((tag, checksum, offset, len(payload)))
        body += payload
        body += b"\0" * ((4 - len(payload) % 4) % 4)
        offset += len(payload) + ((4 - len(payload) % 4) % 4)
    for tag, checksum, off, ln in new_records:
        out += tag.encode("latin-1") + struct.pack(">III", checksum, off, ln)
    out += body
    return bytes(out)


# --------------------------------------------------------------------------
# glyph pool harvested from genuine WB donors
# --------------------------------------------------------------------------

@dataclass
class DonorGlyph:
    char: str
    cid: int
    blob: bytes
    width: int
    components: Dict[int, bytes] = field(default_factory=dict)


class GlyphPool:
    """Outlines harvested from genuine WB Google Sans subsets, by family."""

    def __init__(self) -> None:
        self.glyphs: Dict[str, Dict[str, DonorGlyph]] = {}

    def add_font(
        self,
        family: str,
        ttf_bytes: bytes,
        cid_to_char: Dict[int, str],
        widths: Dict[int, int],
    ) -> None:
        pool = self.glyphs.setdefault(family, {})
        for cid, char in cid_to_char.items():
            if char in pool:
                continue
            blob = glyph_blob(ttf_bytes, cid)
            components = {
                gid: glyph_blob(ttf_bytes, gid) for gid in glyph_components(blob)
            }
            pool[char] = DonorGlyph(
                char=char,
                cid=cid,
                blob=blob,
                width=int(widths.get(cid, 1000)),
                components=components,
            )

    def get(self, family: str, char: str) -> Optional[DonorGlyph]:
        return self.glyphs.get(family, {}).get(char)

    def available_chars(self, family: str) -> Set[str]:
        return set(self.glyphs.get(family, {}))

    def summary(self) -> str:
        lines = []
        for family in sorted(self.glyphs):
            chars = "".join(
                sorted(
                    c for c in self.glyphs[family] if c not in _SPACE_CHARS
                )
            )
            lines.append(f"{family}: {len(self.glyphs[family])} -> {chars}")
        return "\n".join(lines)
