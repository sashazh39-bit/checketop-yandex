#!/usr/bin/env python3
"""Font engine for MTS receipts -- reproduces the genuine OpenPDF
font-subset pipeline byte-for-byte (see banks/mts/BANK.md for evidence).

Genuine MTS receipts embed two subset TrueType fonts (MTSSans-Regular,
MTSSans-Medium) built by OpenPDF's ``TrueTypeFontSubSet`` from the
*original* MTSSans v1.0 files (created 24.05.2019, unitsPerEm 1000,
351 glyphs).  The subsetter keeps original GIDs (CID == GID), rebuilds
only ``glyf``/``loca`` and copies ``head``/``hhea``/``hmtx``/``maxp``
verbatim from the original font.

The bank's v1.0 TTFs are not public (the publicly downloadable MTS Sans
is v1.003, re-mastered at 2048 upm with different outlines -- see
BANK.md), so this module recovers the v1.0 font from the donors
themselves:

* ``head``/``hhea``/``maxp`` and the full 351-entry ``hmtx`` are copied
  verbatim into every genuine subset -- they are lifted from any donor;
* glyph outlines (``glyf`` data per GID) are harvested from the donor
  pool (``FontPool``): a character absent from every genuine donor is
  honestly refused, never synthesised from a foreign font version;
* the char->GID map comes from the donors' ToUnicode CMaps (ground
  truth of the v1.0 ``cmap``).

``build_subset`` is a faithful port of OpenPDF's ``TrueTypeFontSubSet``
(table order, checksums, offsets, padding), ``build_tounicode`` and
``build_w_array`` reproduce ``TrueTypeFontUnicode.getToUnicode`` /
``getCIDFontType2`` byte-for-byte.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

__all__ = ["EmbeddedSubset", "FontPool", "FontCoverageError", "OtfGlyphSource",
           "build_subset", "build_tounicode", "build_w_array"]


class FontCoverageError(Exception):
    """A character/glyph is not covered by the donor glyph pool."""


# --------------------------------------------------------------------------
# synthetic glyphs: the 2019 CFF master converted to TTF glyf data
# --------------------------------------------------------------------------

class OtfGlyphSource:
    """MTSSans v1.0 in its CFF flavour (MTS's own 2019 brand package,
    created 24.05.2019 -- the exact `head.created` of the bank's TTF,
    see BANK.md §5), converted on the fly to quadratic glyf data.

    The bank embeds the *TTF flavour* whose outlines were built
    independently (different point topology), so converted outlines are
    visually identical but NOT byte-identical to the bank's -- they
    fill only the characters no genuine donor covers.  GIDs map as
    ``ttf_gid = otf_gid + 2`` (the TTF flavour carries two extra
    leading glyphs; verified on every donor-covered character).
    """

    def __init__(self, otf_path, gid_shift: int = 2, max_err: float = 1.0):
        from fontTools.ttLib import TTFont
        self.font = TTFont(str(otf_path), lazy=False)
        self.shift = gid_shift
        self.max_err = max_err
        cff = self.font["CFF "].cff
        self.charstrings = cff[cff.fontNames[0]].CharStrings
        self.order = self.font.getGlyphOrder()
        self.cmap = self.font.getBestCmap()
        self._glyph_cache: dict = {}

    def available_chars(self):
        return [chr(cp) for cp in self.cmap.keys()]

    def ttf_gid(self, char: str):
        name = self.cmap.get(ord(char))
        if name is None:
            return None
        return self.font.getGlyphID(name) + self.shift

    def _convert(self, otf_gid: int) -> bytes:
        """CFF charstring -> quadratic glyf binary (cu2qu, TT direction)."""
        from fontTools.pens.recordingPen import RecordingPen
        from fontTools.pens.cu2quPen import Cu2QuPen
        from fontTools.ttLib.tables._g_l_y_f import Glyph
        cs = self.charstrings[self.order[otf_gid]]
        pen = RecordingPen()
        cs.draw(pen)
        qpen = RecordingPen()
        cu2qu = Cu2QuPen(qpen, max_err=self.max_err, reverse_direction=True)
        for op, args in pen.value:
            getattr(cu2qu, op)(*args)
        try:
            cu2qu.close()
        except Exception:  # noqa: BLE001 - open contour is fine to keep
            pass
        contours = []
        current = []
        for op, args in qpen.value:
            if op == "moveTo":
                if current:
                    contours.append(current)
                current = [(args[0], False)]
            elif op == "lineTo":
                current.append((args[0], True))
            elif op == "qCurveTo":
                for point in args[:-1]:
                    current.append((point, False))
                current.append((args[-1], True))
            elif op in ("closePath", "endPath"):
                if current:
                    contours.append(current)
                    current = []
        if current:
            contours.append(current)
        coords, ends, flags = [], [], []
        for contour in contours:
            for (x, y), on_curve in contour:
                coords.append((int(round(x)), int(round(y))))
                flags.append(1 if on_curve else 0)
            ends.append(len(coords) - 1)
        if not coords:
            return b""
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
        from fontTools.ttLib.tables.ttProgram import Program
        glyph = Glyph()
        glyph.numberOfContours = len(ends)
        coords_gc = GlyphCoordinates()
        coords_gc.extend(coords)
        glyph.coordinates = coords_gc
        glyph.flags = flags
        glyph.endPtsOfContours = ends
        glyph.program = Program()
        glyph.recalcBounds(None)
        data = glyph.compile(None)
        if len(data) % 2:
            data += b"\x00"
        return data

    def glyf_bytes(self, ttf_gid: int) -> bytes:
        if ttf_gid not in self._glyph_cache:
            otf_gid = ttf_gid - self.shift
            if otf_gid < 0 or otf_gid >= len(self.order):
                self._glyph_cache[ttf_gid] = None
            else:
                self._glyph_cache[ttf_gid] = self._convert(otf_gid)
        return self._glyph_cache[ttf_gid]


# --------------------------------------------------------------------------
# minimal SFNT reader (only what the subset pipeline needs)
# --------------------------------------------------------------------------

class _Sfnt:
    """Read table slices out of a TrueType file (subset or full)."""

    def __init__(self, data: bytes):
        if len(data) < 12 or data[:4] != b"\x00\x01\x00\x00":
            raise ValueError("not a TrueType sfnt")
        self.data = data
        num_tables = struct.unpack(">H", data[4:6])[0]
        self.tables: Dict[str, Tuple[int, int, int]] = {}  # tag -> (check, off, len)
        pos = 12
        for _ in range(num_tables):
            tag = data[pos:pos + 4].decode("latin1")
            check, off, length = struct.unpack(">III", data[pos + 4:pos + 16])
            self.tables[tag] = (check, off, length)
            pos += 16

    def table(self, tag: str) -> bytes:
        check, off, length = self.tables[tag]
        return self.data[off:off + length]

    def checksum(self, tag: str) -> int:
        return self.tables[tag][0]


class EmbeddedSubset:
    """A genuine embedded subset font (FontFile2) plus its ToUnicode."""

    def __init__(self, font_bytes: bytes, tounicode_text: str):
        sfnt = _Sfnt(font_bytes)
        self.sfnt = sfnt
        self.head = sfnt.table("head")
        self.hhea = sfnt.table("hhea")
        self.hmtx = sfnt.table("hmtx")
        self.maxp = sfnt.table("maxp")
        self.num_glyphs = struct.unpack(">H", self.maxp[4:6])[0]
        short_loca = struct.unpack(">h", self.head[50:52])[0] == 0
        loca_raw = sfnt.table("loca")
        glyf_off = sfnt.tables["glyf"][1]
        glyf = sfnt.table("glyf")
        if short_loca:
            self.loca = [struct.unpack(">H", loca_raw[i:i + 2])[0] * 2
                         for i in range(0, len(loca_raw), 2)]
        else:
            self.loca = [struct.unpack(">I", loca_raw[i:i + 4])[0]
                         for i in range(0, len(loca_raw), 4)]
        # glyph k data lives inside the packed subset glyf at [loca[k], loca[k+1])
        self.glyph_data: Dict[int, bytes] = {}
        for gid in range(self.num_glyphs):
            start, end = self.loca[gid], self.loca[gid + 1]
            if end > start:
                self.glyph_data[gid] = glyf[start:end]
        self.widths: List[int] = [
            struct.unpack(">H", self.hmtx[i * 4:i * 4 + 2])[0]
            for i in range(self.num_glyphs)
        ]
        self.char_to_gid: Dict[str, int] = _parse_tounicode(tounicode_text)


def _parse_tounicode(text: str) -> Dict[str, int]:
    """Parse an OpenPDF ToUnicode CMap: ``<cid><cid><unicode>`` lines."""
    mapping: Dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("<"):
            continue
        parts = [p for p in line.replace("><", " ").strip("<>").split()]
        if len(parts) != 3 or parts[0] != parts[1]:
            continue
        cid = int(parts[0], 16)
        uni = int(parts[2], 16)
        if uni <= 0xFFFF:
            mapping[chr(uni)] = cid
        else:
            # surrogate pair target (never used on these receipts)
            uni -= 0x10000
            mapping[chr(0xD800 + uni // 0x400) + chr(0xDC00 + uni % 0x400)] = cid
    return mapping


# --------------------------------------------------------------------------
# glyph pool: the recovered v1.0 font, harvested from genuine donors
# --------------------------------------------------------------------------

class FontPool:
    """Recovered original MTSSans v1.0 for one family (Regular/Medium).

    Shared tables come from any genuine donor subset (they are verbatim
    copies of the original font's tables); glyph outlines accumulate
    from every donor in the pool.
    """

    def __init__(self) -> None:
        self.head: Optional[bytes] = None
        self.hhea: Optional[bytes] = None
        self.hmtx: Optional[bytes] = None
        self.maxp: Optional[bytes] = None
        self.checksums: Dict[str, int] = {}
        self.num_glyphs = 0
        self.widths: List[int] = []
        self.glyph_data: Dict[int, bytes] = {}
        self.known_gids: set = set()   # gids usable (incl. empty outlines)
        self.synthetic_gids: set = set()  # gids converted from the CFF master
        self.char_to_gid: Dict[str, int] = {}

    def absorb(self, subset: EmbeddedSubset) -> None:
        if self.head is None:
            self.head, self.hhea = subset.head, subset.hhea
            self.hmtx, self.maxp = subset.hmtx, subset.maxp
            self.num_glyphs = subset.num_glyphs
            self.widths = list(subset.widths)
            for tag in ("head", "hhea", "hmtx", "maxp"):
                self.checksums[tag] = subset.sfnt.checksum(tag)
        else:
            # another donor of the same v1.0 font: tables must match
            if (subset.head, subset.hhea, subset.hmtx, subset.maxp) != (
                    self.head, self.hhea, self.hmtx, self.maxp):
                raise ValueError("donor subset carries different font tables "
                                 "(not the same MTSSans v1.0)")
        for gid, data in subset.glyph_data.items():
            existing = self.glyph_data.get(gid)
            if existing is not None and existing != data:
                raise ValueError(f"conflicting glyph data for GID {gid}")
            self.glyph_data[gid] = data
        # every ToUnicode gid is usable, even with an empty outline (space)
        self.known_gids.update(subset.char_to_gid.values())
        self.known_gids.update(subset.glyph_data.keys())
        for char, gid in subset.char_to_gid.items():
            existing = self.char_to_gid.get(char)
            if existing is not None and existing != gid:
                raise ValueError(f"conflicting GID for {char!r}")
            self.char_to_gid[char] = gid

    def fill_from(self, other: "FontPool") -> None:
        """Copy glyphs this pool does not yet have (first-writer wins)."""
        if other.head is None:
            return
        if self.head is None:
            self.head, self.hhea = other.head, other.hhea
            self.hmtx, self.maxp = other.hmtx, other.maxp
            self.num_glyphs = other.num_glyphs
            self.widths = list(other.widths)
            self.checksums = dict(other.checksums)
        for gid, data in other.glyph_data.items():
            if gid not in self.glyph_data:
                self.glyph_data[gid] = data
                if gid in other.synthetic_gids:
                    self.synthetic_gids.add(gid)
        self.known_gids.update(other.known_gids)
        for char, gid in other.char_to_gid.items():
            self.char_to_gid.setdefault(char, gid)

    def absorb_synthetic(self, source: "OtfGlyphSource") -> int:
        """Fill NOT-yet-covered characters from a converted CFF master.

        Donor-harvested glyphs always win: only gaps are synthesised.
        Returns the number of synthesised characters.  Synthesised GIDs
        are tracked in ``self.synthetic_gids`` (BANK.md §5).
        """
        added = 0
        for char in source.available_chars():
            ttf_gid = source.ttf_gid(char)
            if ttf_gid is None or ttf_gid in self.known_gids:
                continue
            data = source.glyf_bytes(ttf_gid)
            if data is None:
                continue
            self.glyph_data[ttf_gid] = data
            self.known_gids.add(ttf_gid)
            self.synthetic_gids.add(ttf_gid)
            self.char_to_gid.setdefault(char, ttf_gid)
            added += 1
        return added

    # -- queries ---------------------------------------------------------

    def gid_of(self, char: str) -> Optional[int]:
        return self.char_to_gid.get(char)

    def has_char(self, char: str) -> bool:
        gid = self.char_to_gid.get(char)
        return gid is not None and gid in self.known_gids

    def width_of_gid(self, gid: int) -> int:
        return self.widths[gid]

    def width_of(self, char: str) -> int:
        gid = self.char_to_gid[char]
        return self.widths[gid]

    def missing_chars(self, text: str) -> List[str]:
        return sorted({c for c in text if not self.has_char(c)})

    @property
    def head_bbox(self) -> Tuple[int, int, int, int]:
        assert self.head is not None
        return struct.unpack(">hhhh", self.head[36:44])


# --------------------------------------------------------------------------
# OpenPDF TrueTypeFontSubSet port
# --------------------------------------------------------------------------

_ENTRY_SELECTORS = (0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4)
_TABLE_ORDER = ("cvt ", "fpgm", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "prep")

_ARG_1_AND_2_ARE_WORDS = 1
_WE_HAVE_A_SCALE = 8
_MORE_COMPONENTS = 32
_WE_HAVE_AN_X_AND_Y_SCALE = 64
_WE_HAVE_A_TWO_BY_TWO = 128


def _checksum(data: bytes) -> int:
    """OpenPDF ``calculateChecksum`` over the padded table bytes."""
    total = 0
    for i in range(0, len(data), 4):
        total = (total + struct.unpack(">I", data[i:i + 4])[0]) & 0xFFFFFFFF
    return total


def _composite_components(data: bytes) -> List[int]:
    """Component GIDs of a composite glyph (OpenPDF ``checkGlyphComposite``)."""
    if len(data) < 10:
        return []
    (num_contours,) = struct.unpack(">h", data[:2])
    if num_contours >= 0:
        return []
    pos = 10  # skip numContours + bbox
    components = []
    while True:
        flags, gid = struct.unpack(">HH", data[pos:pos + 4])
        components.append(gid)
        pos += 4
        if not (flags & _MORE_COMPONENTS):
            break
        if flags & _ARG_1_AND_2_ARE_WORDS:
            skip = 4
        else:
            skip = 2
        if flags & _WE_HAVE_A_SCALE:
            skip += 2
        elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
            skip += 4
        if flags & _WE_HAVE_A_TWO_BY_TWO:
            skip += 8
        pos += skip
    return components


def build_subset(pool: FontPool, text_gids: List[int]) -> bytes:
    """Rebuild the FontFile2 subset exactly like OpenPDF.

    ``text_gids`` are the GIDs used by the document text (the contents of
    FontDetails' longTag); GID 0 and composite components are added by
    the subsetter itself, as in ``flatGlyphs``.
    """
    if pool.head is None:
        raise FontCoverageError("empty font pool")
    glyphs_used = set(text_gids)
    glyphs_used.add(0)
    # flatGlyphs: pull in composite components (none in MTSSans v1.0,
    # but keep the exact behaviour)
    queue = list(glyphs_used)
    while queue:
        gid = queue.pop()
        data = pool.glyph_data.get(gid, b"")
        for comp in _composite_components(data):
            if comp not in glyphs_used:
                glyphs_used.add(comp)
                queue.append(comp)
    active = sorted(glyphs_used)

    # createNewGlyphTables: pack used glyphs contiguously in GID order
    glyf = bytearray()
    new_loca = [0] * (pool.num_glyphs + 1)
    ptr = 0
    active_set = set(active)
    for gid in range(pool.num_glyphs):
        new_loca[gid] = ptr
        if gid in active_set:
            data = pool.glyph_data.get(gid, b"")
            if data:
                glyf += data
                ptr += len(data)
    new_loca[pool.num_glyphs] = ptr
    glyf_real_size = ptr
    while len(glyf) % 4:
        glyf.append(0)

    # locaTobytes: short loca (v1.0 indexToLocFormat == 0)
    loca_out = bytearray()
    for off in new_loca:
        loca_out += struct.pack(">H", off // 2)
    loca_real_size = len(loca_out)
    while len(loca_out) % 4:
        loca_out.append(0)

    # assembleFont: table set and order of tableNamesSimple
    present = {"glyf", "head", "hhea", "hmtx", "loca", "maxp"}
    table_names = [t for t in _TABLE_ORDER
                   if t in present and t not in ("cvt ", "fpgm", "prep")]
    tables_used = 2 + len([t for t in table_names if t not in ("glyf", "loca")])

    bodies: Dict[str, bytes] = {
        "glyf": bytes(glyf),
        "head": pool.head,
        "hhea": pool.hhea,
        "hmtx": pool.hmtx,
        "loca": bytes(loca_out),
        "maxp": pool.maxp,
    }
    lengths: Dict[str, int] = {
        "glyf": glyf_real_size,
        "head": len(pool.head),
        "hhea": len(pool.hhea),
        "hmtx": len(pool.hmtx),
        "loca": loca_real_size,
        "maxp": len(pool.maxp),
    }
    checksums = {
        "glyf": _checksum(bytes(glyf)),
        "head": pool.checksums["head"],
        "hhea": pool.checksums["hhea"],
        "hmtx": pool.checksums["hmtx"],
        "loca": _checksum(bytes(loca_out)),
        "maxp": pool.checksums["maxp"],
    }

    ref = 16 * tables_used + 12
    out = bytearray()
    out += struct.pack(">IHHHH", 0x00010000, tables_used,
                       (1 << _ENTRY_SELECTORS[tables_used]) * 16,
                       _ENTRY_SELECTORS[tables_used],
                       (tables_used - (1 << _ENTRY_SELECTORS[tables_used])) * 16)
    offsets: Dict[str, int] = {}
    cursor = ref
    for name in table_names:
        offsets[name] = cursor
        cursor += (lengths[name] + 3) & ~3
    for name in table_names:
        tag = name.encode("latin1")
        out += tag
        out += struct.pack(">III", checksums[name], offsets[name], lengths[name])
    for name in table_names:
        body = bodies[name]
        out += body
        out += b"\x00" * ((4 - len(body) % 4) % 4)
    return bytes(out)


# --------------------------------------------------------------------------
# TrueTypeFontUnicode serialisation (ToUnicode CMap and /W array)
# --------------------------------------------------------------------------

def _to_hex(value: int) -> str:
    if value < 0x10000:
        return "<%04x>" % value
    value -= 0x10000
    high = value // 0x400 + 0xD800
    low = value % 0x400 + 0xDC00
    return "[<%04x%04x>]" % (high, low)


def build_tounicode(metrics: List[Tuple[int, int, int]]) -> bytes:
    """``getToUnicode``: metrics are (gid, width, unicode) sorted by gid."""
    buf = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo\n"
        "<< /Registry (TTX+0)\n"
        "/Ordering (T42UV)\n"
        "/Supplement 0\n"
        ">> def\n"
        "/CMapName /TTX+0 def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<0000><FFFF>\n"
        "endcodespacerange\n"
    )
    size = 0
    for k, (gid, _width, uni) in enumerate(metrics):
        if size == 0:
            if k != 0:
                buf += "endbfrange\n"
            size = min(100, len(metrics) - k)
            buf += f"{size} beginbfrange\n"
        size -= 1
        from_to = _to_hex(gid)
        buf += from_to + from_to + _to_hex(uni) + "\n"
    buf += (
        "endbfrange\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end end\n"
    )
    return buf.encode("latin1")


def build_w_array(metrics: List[Tuple[int, int, int]]) -> bytes:
    """``getCIDFontType2`` /W literal: runs of consecutive GIDs."""
    buf = "["
    last_number = -10
    first_time = True
    for gid, width, _uni in metrics:
        if width == 1000:
            continue
        if gid == last_number + 1:
            buf += f" {width}"
        else:
            if not first_time:
                buf += "]"
            first_time = False
            buf += f"{gid}[{width}"
        last_number = gid
    if len(buf) > 1:
        buf += "]]"
    return buf.encode("latin1")
