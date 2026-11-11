#!/usr/bin/env python3
"""Font handling for Yandex Bank receipts.

Yandex receipts (JasperReports / OpenPDF) embed YSText as Type0 /
CIDFontType2 (TrueType, ``/FontFile2``) subsets with ``/CIDToGIDMap
/Identity``.  A CID is therefore the glyph index (GID) inside the
embedded TTF.  Every subset keeps the full 911-glyph order of the
original YSText font and only fills in the outlines it uses, so a glyph
can be grown into one subset from another genuine Yandex receipt by
copying the outline at the *same* GID — the outline coordinates and
advances are byte-identical across donors (verified for digits 0-9).

Unlike Rocket (CFF charstrings), growing a Yandex font means updating
four objects together:

* ``FontFile2``  — the TTF stream (new outline, ``/Length1`` kept in sync)
* CIDFont ``/W`` — width of the new CID
* ``ToUnicode``  — CID -> character, so the text layer reads back
* ``CIDSet``     — the CID bitmap stream in the FontDescriptor
"""

from __future__ import annotations

import copy
import re
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Set, Tuple

from fontTools.ttLib import TTFont

# --------------------------------------------------------------------- CMaps

_CMAP_RANGE = re.compile(
    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>"
)
_CMAP_CHAR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")

# Exact header JasperReports/OpenPDF writes into Yandex ToUnicode streams.
_CMAP_TEMPLATE = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo
<< /Registry (TTX+0)
/Ordering (T42UV)
/Supplement 0
>> def
/CMapName /TTX+0 def
/CMapType 2 def
1 begincodespacerange
<0000><FFFF>
endcodespacerange
{count} beginbfrange
{ranges}
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end end
"""


def parse_tounicode(stream: bytes) -> Dict[int, str]:
    """Parse a ToUnicode CMap into {cid: character}."""
    mapping: Dict[int, str] = {}
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.DOTALL):
        for item in _CMAP_RANGE.finditer(block):
            low = int(item.group(1), 16)
            high = int(item.group(2), 16)
            target = int(item.group(3), 16)
            for step, cid in enumerate(range(low, high + 1)):
                mapping[cid] = chr(target + step)
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.DOTALL):
        for item in _CMAP_CHAR.finditer(block):
            mapping[int(item.group(1), 16)] = chr(int(item.group(2), 16))
    return mapping


def build_tounicode(mapping: Dict[int, str]) -> bytes:
    """Serialise {cid: character} in the donor's own TTX+0 CMap style."""
    lines = [
        f"<{cid:04x}><{cid:04x}><{ord(char):04x}>"
        for cid, char in sorted(mapping.items())
    ]
    text = _CMAP_TEMPLATE.format(count=len(lines), ranges="\n".join(lines))
    return text.encode("latin-1")


def invert_cmap(mapping: Dict[int, str]) -> Dict[str, int]:
    """Build {character: cid} from {cid: character}."""
    return {char: cid for cid, char in mapping.items()}


# ------------------------------------------------------- literal strings/CIDs


def decode_literal(inner: bytes) -> bytes:
    """Undo PDF literal-string escaping, returning the raw CID bytes."""
    out = bytearray()
    index = 0
    while index < len(inner):
        byte = inner[index]
        if byte != 0x5C:  # backslash
            out.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(inner):
            break
        esc = inner[index]
        index += 1
        if esc in b"nrtbf":
            out.append({ord("n"): 10, ord("r"): 13, ord("t"): 9,
                        ord("b"): 8, ord("f"): 12}[esc])
        elif esc in b"()\\":
            out.append(esc)
        elif esc in b"\r\n":
            # Line continuation: a backslash at end of line swallows it.
            if esc == 0x0D and index < len(inner) and inner[index] == 0x0A:
                index += 1
        elif 0x30 <= esc <= 0x37:  # up to three octal digits
            value = esc - 0x30
            for _ in range(2):
                if index < len(inner) and 0x30 <= inner[index] <= 0x37:
                    value = value * 8 + (inner[index] - 0x30)
                    index += 1
                else:
                    break
            out.append(value & 0xFF)
        else:
            out.append(esc)
    return bytes(out)


def encode_literal(raw: bytes) -> bytes:
    """Escape raw CID bytes for inclusion in a PDF literal string.

    Uses the named escapes (``\\b \\t \\n \\f \\r`` plus ``\\(\\)\\\\``)
    that JasperReports/OpenPDF writes, so a re-encoded string has the
    same byte length as the donor's own encoding.
    """
    named = {0x08: b"b", 0x09: b"t", 0x0A: b"n", 0x0C: b"f", 0x0D: b"r"}
    out = bytearray()
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):  # ( ) backslash
            out.append(0x5C)
            out.append(byte)
        elif byte in named:
            out.append(0x5C)
            out.extend(named[byte])
        else:
            out.append(byte)
    return bytes(out)


def cids_of_text(text: str, char_to_cid: Dict[str, int]) -> List[int]:
    """Map a string to CIDs, raising KeyError naming the missing chars."""
    missing = sorted({char for char in text if char not in char_to_cid})
    if missing:
        raise KeyError(
            "font cannot represent: " + ", ".join(f"{c!r}" for c in missing)
        )
    return [char_to_cid[char] for char in text]


def raw_of_cids(cids: Iterable[int]) -> bytes:
    """Serialise CIDs as the 2-byte big-endian codes Identity-H expects."""
    return b"".join(int(cid).to_bytes(2, "big") for cid in cids)


def text_of_raw(raw: bytes, cid_to_char: Dict[int, str]) -> Tuple[str, List[int]]:
    """Decode raw 2-byte CID codes through a ToUnicode mapping."""
    cids = [
        int.from_bytes(raw[index:index + 2], "big")
        for index in range(0, len(raw) - 1, 2)
    ]
    return "".join(cid_to_char.get(cid, "\ufffd") for cid in cids), cids


# ---------------------------------------------------------------- /W widths


def parse_widths(dict_bytes: bytes) -> Dict[int, int]:
    """Parse a CIDFont /W array into {cid: width}."""
    match = re.search(rb"/W\s*\[", dict_bytes)
    if not match:
        return {}
    depth = 1
    index = match.end()
    while index < len(dict_bytes) and depth:
        if dict_bytes[index:index + 1] == b"[":
            depth += 1
        elif dict_bytes[index:index + 1] == b"]":
            depth -= 1
        index += 1
    body = dict_bytes[match.end():index - 1]

    widths: Dict[int, int] = {}
    tokens = re.findall(rb"\[|\]|-?\d+", body)
    cursor = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        if not re.match(rb"-?\d+$", token):
            cursor += 1
            continue
        start = int(token)
        cursor += 1
        if cursor < len(tokens) and tokens[cursor] == b"[":
            cursor += 1
            cid = start
            while cursor < len(tokens) and tokens[cursor] != b"]":
                widths[cid] = int(tokens[cursor])
                cid += 1
                cursor += 1
            cursor += 1
        elif cursor + 1 < len(tokens):
            last = int(tokens[cursor])
            value = int(tokens[cursor + 1])
            for cid in range(start, last + 1):
                widths[cid] = value
            cursor += 2
    return widths


def build_widths(widths: Dict[int, int]) -> bytes:
    """Serialise {cid: width} into a compact /W array body."""
    if not widths:
        return b"[]"
    parts: List[bytes] = []
    run_start: Optional[int] = None
    run: List[int] = []

    def flush() -> None:
        if run_start is None:
            return
        values = b" ".join(str(value).encode("ascii") for value in run)
        parts.append(str(run_start).encode("ascii") + b"[" + values + b"]")

    previous: Optional[int] = None
    for cid in sorted(widths):
        if previous is not None and cid == previous + 1:
            run.append(widths[cid])
        else:
            flush()
            run_start = cid
            run = [widths[cid]]
        previous = cid
    flush()
    return b"[" + b"".join(parts) + b"]"


def replace_widths(dict_bytes: bytes, widths: Dict[int, int]) -> bytes:
    """Swap the /W array inside a CIDFont dictionary."""
    match = re.search(rb"/W\s*\[", dict_bytes)
    if not match:
        insert_at = dict_bytes.rindex(b">>")
        return (
            dict_bytes[:insert_at]
            + b"/W " + build_widths(widths)
            + dict_bytes[insert_at:]
        )
    depth = 1
    index = match.end()
    while index < len(dict_bytes) and depth:
        if dict_bytes[index:index + 1] == b"[":
            depth += 1
        elif dict_bytes[index:index + 1] == b"]":
            depth -= 1
        index += 1
    return dict_bytes[:match.start()] + b"/W " + build_widths(widths) + dict_bytes[index:]


# ------------------------------------------------------------------- CIDSet


def parse_cidset(stream: bytes) -> Set[int]:
    """Return the CID set encoded by a /CIDSet bitmap stream."""
    return {
        cid
        for cid in range(len(stream) * 8)
        if stream[cid // 8] >> (7 - (cid % 8)) & 1
    }


def build_cidset(existing: bytes, cids: Set[int]) -> bytes:
    """Return the CIDSet bitmap with ``cids`` set, keeping donor length."""
    if not cids:
        return bytes(existing)
    size = max(len(existing), (max(cids) + 8) // 8)
    bitmap = bytearray(size)
    bitmap[:len(existing)] = existing
    for cid in cids:
        bitmap[cid // 8] |= 1 << (7 - (cid % 8))
    return bytes(bitmap)


def build_cidset_exact(existing: bytes, cids: Set[int]) -> bytes:
    """CIDSet bitmap with exactly ``cids`` set, keeping donor length.

    Genuine Jasper/OpenPDF subsets list exactly the CIDs their text
    uses; after a field edit the donor's extra CIDs must be cleared,
    not just the new ones added (see ``build_cidset``).
    """
    size = max(len(existing), (max(cids) + 8) // 8) if cids else len(existing)
    bitmap = bytearray(size)
    for cid in cids:
        bitmap[cid // 8] |= 1 << (7 - (cid % 8))
    return bytes(bitmap)


# ------------------------------------------------------------------ TTF work


def read_ttf(data: bytes) -> TTFont:
    return TTFont(BytesIO(data))


def glyph_has_outline(ttf: TTFont, cid: int) -> bool:
    """True when glyph ``cid`` carries an outline (or is a composite)."""
    order = ttf.getGlyphOrder()
    if cid < 0 or cid >= len(order):
        return False
    glyph = ttf["glyf"][order[cid]]
    if glyph.isComposite():
        return True
    try:
        coords, _endpts, _flags = glyph.getCoordinates(ttf["glyf"])
    except Exception:  # noqa: BLE001 - a broken glyph counts as missing
        return False
    return len(coords) > 0


@dataclass
class DonorGlyph:
    """One glyph harvested from a genuine Yandex donor receipt."""

    char: str
    cid: int  # CID in the donor the glyph was harvested from
    glyph: object  # fontTools glyf Glyph (deep-copied, donor-independent)
    hmtx: Tuple[int, int]
    width: int  # /W width in the donor
    # Component outlines of a composite glyph: {component_cid: Glyph}.
    components: Dict[int, object] = field(default_factory=dict)


class GlyphPool:
    """Outlines and widths harvested from genuine Yandex receipts.

    Keyed by font family (``YSText-Regular``), so glyphs are never mixed
    into a different YSText weight.
    """

    def __init__(self) -> None:
        self.glyphs: Dict[str, Dict[str, DonorGlyph]] = {}

    def add_font(
        self,
        family: str,
        ttf_bytes: bytes,
        cid_to_char: Dict[int, str],
        widths: Dict[int, int],
    ) -> None:
        ttf = read_ttf(ttf_bytes)
        order = ttf.getGlyphOrder()
        glyf = ttf["glyf"]
        pool = self.glyphs.setdefault(family, {})
        for cid, char in cid_to_char.items():
            if char in pool or cid < 0 or cid >= len(order):
                continue
            name = order[cid]
            glyph = glyf[name]
            components: Dict[int, object] = {}
            if glyph.isComposite():
                for component in glyph.components:
                    match = re.fullmatch(r"glyph(\d+)", component.glyphName)
                    if match:
                        comp_cid = int(match.group(1))
                        components[comp_cid] = copy.deepcopy(
                            glyf[component.glyphName]
                        )
            pool[char] = DonorGlyph(
                char=char,
                cid=cid,
                glyph=copy.deepcopy(glyph),
                hmtx=tuple(ttf["hmtx"][name]),
                width=widths.get(cid, int(ttf["hmtx"][name][0])),
                components=components,
            )

    def get(self, family: str, char: str) -> Optional[DonorGlyph]:
        return self.glyphs.get(family, {}).get(char)

    def available_chars(self, family: str) -> Dict[str, int]:
        """{character: donor cid} for everything harvested for ``family``."""
        return {
            char: glyph.cid for char, glyph in self.glyphs.get(family, {}).items()
        }

    def summary(self) -> str:
        lines = []
        for family in sorted(self.glyphs):
            chars = self.available_chars(family)
            printable = "".join(sorted(c for c in chars if c not in " \xa0"))
            lines.append(f"{family}: {len(chars)} chars -> {printable}")
        return "\n".join(lines)


def inject_glyphs(ttf_bytes: bytes, additions: Dict[int, DonorGlyph]) -> bytes:
    """Return a new TTF with the given {cid: donor glyph} slots filled in.

    Only used when a glyph is genuinely grown: the subset keeps the full
    glyph order, so a donor outline drops into the same GID it came from.
    """
    ttf = read_ttf(ttf_bytes)
    order = ttf.getGlyphOrder()
    glyf = ttf["glyf"]
    for cid, donor in additions.items():
        if cid < 0 or cid >= len(order):
            raise KeyError(f"CID {cid} outside the subset's glyph order")
        name = order[cid]
        # Composite glyphs need their component outlines present too.
        for comp_cid, comp_glyph in donor.components.items():
            if 0 <= comp_cid < len(order) and not glyph_has_outline(ttf, comp_cid):
                glyf[order[comp_cid]] = copy.deepcopy(comp_glyph)
        glyf[name] = copy.deepcopy(donor.glyph)
        ttf["hmtx"][name] = donor.hmtx
    buffer = BytesIO()
    ttf.save(buffer)
    return buffer.getvalue()


def num_glyphs(ttf_bytes: bytes) -> int:
    return int(read_ttf(ttf_bytes)["maxp"].numGlyphs)


# --------------------------------------------------------- TTF subset pruning
#
# Genuine Jasper/OpenPDF subsets contain exactly the glyphs their text
# uses (verified on the donor corpus: ToUnicode set == CIDSet == filled
# outlines == characters in the content stream, space aside).  After a
# field edit, glyphs that only the donor's values used must be emptied
# again, or the subset itself proves the text was changed afterwards.
#
# The pruning below works on the raw ``glyf``/``loca`` tables and
# reassembles the SFNT in the donor's own table order with the donor's
# header and checksums, so pruning a font to its original glyph set is
# byte-identical to the donor font (asserted in the tests).  A
# fontTools re-save would also move the tables physically and refresh
# ``head.modified`` — a fingerprint no bank export carries.


def _sfnt_tables(data: bytes):
    """[(tag, checksum, offset, length)] of an SFNT, in file order."""
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


def _glyph_components(glyf: bytes, offsets, gid: int):
    """Component GIDs of a composite glyph (empty list when simple)."""
    raw = glyf[offsets[gid] : offsets[gid + 1]]
    if len(raw) < 10:
        return []
    contours = struct.unpack(">h", raw[:2])[0]
    if contours >= 0:
        return []
    components = []
    pos = 10
    while pos + 4 <= len(raw):
        flags, comp_gid = struct.unpack(">HH", raw[pos : pos + 4])
        pos += 4
        pos += 4 if flags & 0x0001 else 2  # arguments
        if flags & 0x0008:
            pos += 2
        elif flags & 0x0040:
            pos += 4
        elif flags & 0x0080:
            pos += 8
        components.append(comp_gid)
        if not flags & 0x0020:  # WE_HAVE_MORE_COMPONENTS
            break
    return components


def prune_ttf(ttf_bytes: bytes, keep_cids: Set[int]) -> bytes:
    """Empty every glyph whose CID is not in ``keep_cids``.

    CID == GID in these subsets (``/CIDToGIDMap /Identity``).  Glyph 0
    and the components of kept composite glyphs are always kept.  All
    tables other than ``glyf``/``loca`` are copied byte-for-byte
    (``head`` included), so a no-op prune returns the donor bytes.
    """
    records = _sfnt_tables(ttf_bytes)
    tables = {tag: ttf_bytes[off : off + ln] for tag, _c, off, ln in records}
    glyf = tables["glyf"]
    head = tables["head"]
    loc_format = struct.unpack(">h", head[50:52])[0]
    loca_raw = tables["loca"]
    if loc_format == 0:
        offsets = [v * 2 for (v,) in struct.iter_unpack(">H", loca_raw)]
    else:
        offsets = [v for (v,) in struct.iter_unpack(">I", loca_raw)]
    num_glyphs = len(offsets) - 1

    keep = {cid for cid in keep_cids if 0 <= cid < num_glyphs} | {0}
    stack = list(keep)
    while stack:
        gid = stack.pop()
        for comp in _glyph_components(glyf, offsets, gid):
            if comp not in keep and comp < num_glyphs:
                keep.add(comp)
                stack.append(comp)

    new_glyf = bytearray()
    new_offsets = []
    for gid in range(num_glyphs):
        new_offsets.append(len(new_glyf))
        if gid in keep:
            new_glyf += glyf[offsets[gid] : offsets[gid + 1]]
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
