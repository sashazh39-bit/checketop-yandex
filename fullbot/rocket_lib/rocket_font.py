#!/usr/bin/env python3
"""Font and CMap handling for Rocket Bank receipts.

The receipts embed CID-keyed CFF subsets of RocketMono-Regular and
RocketSans-Wide. Every subset keeps the full identity CID space (239 and
209 slots respectively) and only fills in the charstrings actually used,
so a glyph can be moved between subsets of the same family by copying its
charstring bytecode verbatim. The Private dicts are identical across
subsets, which is what makes that copy safe: charstring width deltas are
encoded relative to nominalWidthX.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple

from fontTools.cffLib import CFFFontSet

# Cyrillic capitals map onto CIDs at a fixed offset in RocketMono-Regular.
CYRILLIC_CID_OFFSET = 0x0366

_CMAP_RANGE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_CMAP_CHAR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")

_CMAP_TEMPLATE = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo
<< /Registry (Adobe)
/Ordering (UCS)
/Supplement 0
>> def
/CMapName /Adobe-Identity-UCS def
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


# --------------------------------------------------------------------- CMaps


def parse_tounicode(stream: bytes) -> Dict[int, str]:
    """Parse a ToUnicode CMap into {cid: character}."""
    mapping: Dict[int, str] = {}
    body = stream

    for block in re.findall(rb"beginbfrange(.*?)endbfrange", body, re.DOTALL):
        for item in _CMAP_RANGE.finditer(block):
            low = int(item.group(1), 16)
            high = int(item.group(2), 16)
            target = int(item.group(3), 16)
            for step, cid in enumerate(range(low, high + 1)):
                mapping[cid] = chr(target + step)

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", body, re.DOTALL):
        for item in _CMAP_CHAR.finditer(block):
            mapping[int(item.group(1), 16)] = chr(int(item.group(2), 16))

    return mapping


def build_tounicode(mapping: Dict[int, str]) -> bytes:
    """Serialise {cid: character} back into a ToUnicode CMap stream."""
    lines = [
        f"<{cid:04x}><{cid:04x}><{ord(char):04x}>"
        for cid, char in sorted(mapping.items())
    ]
    text = _CMAP_TEMPLATE.format(count=len(lines), ranges="\n".join(lines))
    return text.encode("latin-1")


def invert_cmap(mapping: Dict[int, str]) -> Dict[str, int]:
    """Build {character: cid} from {cid: character}."""
    return {char: cid for cid, char in mapping.items()}


def encode_text(text: str, char_to_cid: Dict[str, int]) -> bytes:
    """Encode a string into a PDF hex string body using the font's CIDs.

    Raises KeyError naming every character the font cannot represent.
    """
    missing = sorted({char for char in text if char not in char_to_cid})
    if missing:
        raise KeyError(
            "font cannot represent: " + ", ".join(f"{c!r}" for c in missing)
        )
    return "".join(f"{char_to_cid[char]:04x}" for char in text).encode("ascii")


# ---------------------------------------------------------------- /W widths


def parse_widths(dict_bytes: bytes) -> Dict[int, int]:
    """Parse a CIDFont /W array into {cid: width}."""
    match = re.search(rb"/W\s*\[", dict_bytes)
    if not match:
        return {}

    depth = 1
    index = match.end()
    while index < len(dict_bytes) and depth:
        if dict_bytes[index : index + 1] == b"[":
            depth += 1
        elif dict_bytes[index : index + 1] == b"]":
            depth -= 1
        index += 1
    body = dict_bytes[match.end() : index - 1]

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
        return dict_bytes[:insert_at] + b"/W " + build_widths(widths) + dict_bytes[insert_at:]

    depth = 1
    index = match.end()
    while index < len(dict_bytes) and depth:
        if dict_bytes[index : index + 1] == b"[":
            depth += 1
        elif dict_bytes[index : index + 1] == b"]":
            depth -= 1
        index += 1
    return dict_bytes[: match.start()] + b"/W " + build_widths(widths) + dict_bytes[index:]


# ------------------------------------------------------------------ CFF work


class _CompileHost:
    """Stand-in for the TTFont that CFFFontSet.compile expects.

    Bounding boxes must not be recomputed: iText embeds the original
    font's FontBBox in both the CFF TopDict and the PDF FontDescriptor,
    and recalculating would narrow it to the glyphs we happen to carry.
    """

    recalcBBoxes = False


def _font_dict(cff: CFFFontSet):
    return cff[cff.fontNames[0]]


def read_cff(data: bytes) -> CFFFontSet:
    cff = CFFFontSet()
    cff.decompile(BytesIO(data), None, isCFF2=False)
    return cff


def family_of(data: bytes) -> str:
    """PostScript family name of a bare CFF font."""
    return read_cff(data).fontNames[0]


def _charstring_bytes(charstrings, name: str) -> bytes:
    glyph = charstrings[name]
    if glyph.bytecode is not None:
        return bytes(glyph.bytecode)
    glyph.compile()
    return bytes(glyph.bytecode or b"")


def filled_cids(data: bytes, *, min_size: int = 6) -> Dict[int, bytes]:
    """Return {cid: charstring bytes} for glyphs that carry real outlines.

    Subset slots that were never used still exist but hold a stub
    charstring (just a width and endchar), hence the size threshold.
    """
    charstrings = _font_dict(read_cff(data)).CharStrings
    result: Dict[int, bytes] = {}
    for name in charstrings.keys():
        match = re.fullmatch(r"cid(\d+)", name)
        if not match:
            continue
        payload = _charstring_bytes(charstrings, name)
        if len(payload) >= min_size:
            result[int(match.group(1))] = payload
    return result


def _parse_index(data: bytes, offset: int):
    """Parse a CFF INDEX at ``offset`` -> (items, total byte length)."""
    count = int.from_bytes(data[offset : offset + 2], "big")
    off_size = data[offset + 2]
    header = 3 + (count + 1) * off_size
    offsets = [
        int.from_bytes(
            data[offset + 3 + i * off_size : offset + 3 + (i + 1) * off_size],
            "big",
        )
        for i in range(count + 1)
    ]
    start = offset + header
    items = [
        data[start + offsets[i] - 1 : start + offsets[i + 1] - 1]
        for i in range(count)
    ]
    return items, header + offsets[-1] - 1, off_size


def _skip_index(data: bytes, offset: int) -> int:
    _items, total, _off_size = _parse_index(data, offset)
    return offset + total


def _charstrings_offset(data: bytes) -> int:
    """Offset of the CharStrings INDEX, read from the TopDict (op 17)."""
    header_size = data[2]
    cursor = _skip_index(data, header_size)  # Name INDEX
    top_dicts, _total, _off = _parse_index(data, cursor)
    dict_data = top_dicts[0]

    stack: list = []
    index = 0
    while index < len(dict_data):
        byte = dict_data[index]
        if byte == 12:  # two-byte operator
            index += 2
            stack = []
        elif byte <= 21:  # one-byte operator
            if byte == 17 and stack:
                return int(stack[-1])
            index += 1
            stack = []
        elif byte == 28:
            stack.append(int.from_bytes(dict_data[index + 1 : index + 3], "big", signed=True))
            index += 3
        elif byte == 29:
            stack.append(int.from_bytes(dict_data[index + 1 : index + 5], "big", signed=True))
            index += 5
        elif byte == 30:  # real number, nibbles until 0xF
            index += 1
            while index < len(dict_data):
                nibbles = dict_data[index]
                index += 1
                if nibbles & 0x0F == 0x0F or nibbles >> 4 == 0x0F:
                    break
            stack.append(0)
        elif 32 <= byte <= 246:
            stack.append(byte - 139)
            index += 1
        elif 247 <= byte <= 250:
            stack.append((byte - 247) * 256 + dict_data[index + 1] + 108)
            index += 2
        elif 251 <= byte <= 254:
            stack.append(-(byte - 251) * 256 - dict_data[index + 1] - 108)
            index += 2
        else:  # 255: fixed point, and anything unexpected
            index += 5 if byte == 255 else 1
            stack.append(0)
    raise ValueError("TopDict carries no CharStrings offset")


def _inject_cids_manual(data: bytes, glyphs: Dict[int, bytes]) -> bytes:
    """Splice charstrings into the CharStrings INDEX, byte-preserving.

    Everything before the INDEX — header, Name/String INDEXes, GlobalSubrs,
    TopDict, FDArray, charset — is byte-identical across every donor
    subset of a family (verified on the whole corpus), and the INDEX is
    the last structure in the file, so copying a donor charstring into a
    stub slot only touches that INDEX's data and offset table.  This is
    what the bank's own subsetter does; a fontTools recompile, by
    contrast, drops the GlobalSubrs and rewrites every table.
    """
    offset = _charstrings_offset(data)
    items, total, off_size = _parse_index(data, offset)
    for cid, payload in glyphs.items():
        if cid <= 0 or cid >= len(items):
            raise KeyError(f"CID {cid} outside the subset's identity space")
        items[cid] = payload

    data_size = sum(len(item) for item in items)
    needed = max(off_size, ((data_size + 1).bit_length() + 7) // 8)
    offsets = [1]
    for item in items:
        offsets.append(offsets[-1] + len(item))
    index_bytes = (
        len(items).to_bytes(2, "big")
        + bytes([needed])
        + b"".join(value.to_bytes(needed, "big") for value in offsets)
        + b"".join(items)
    )
    return data[:offset] + index_bytes + data[offset + total :]


def inject_cids(data: bytes, glyphs: Dict[int, bytes]) -> bytes:
    """Return a new CFF with the given {cid: charstring} slots filled in.

    Byte-preserving splice (see ``_inject_cids_manual``); falls back to
    a fontTools recompile only for CFF layouts the splice cannot parse.
    """
    try:
        return _inject_cids_manual(data, glyphs)
    except (ValueError, IndexError, KeyError):
        pass
    cff = read_cff(data)
    charstrings = _font_dict(cff).CharStrings

    for cid, payload in glyphs.items():
        name = f"cid{cid:05d}"
        if name not in charstrings:
            raise KeyError(f"CID {cid} outside the subset's identity space")
        glyph = charstrings[name]
        glyph.bytecode = payload
        glyph.program = None

    buffer = BytesIO()
    cff.compile(buffer, _CompileHost(), isCFF2=False)
    return buffer.getvalue()


class GlyphCache:
    """Charstrings and widths harvested from a set of donor receipts.

    Keyed by font family, so RocketMono-Regular and RocketSans-Wide glyph
    pools stay separate.
    """

    def __init__(self) -> None:
        self.glyphs: Dict[str, Dict[int, bytes]] = {}
        self.widths: Dict[str, Dict[int, int]] = {}
        self.chars: Dict[str, Dict[int, str]] = {}

    def add_font(
        self,
        family: str,
        glyphs: Dict[int, bytes],
        widths: Dict[int, int],
        chars: Dict[int, str],
    ) -> None:
        self.glyphs.setdefault(family, {}).update(glyphs)
        self.widths.setdefault(family, {}).update(widths)
        self.chars.setdefault(family, {}).update(chars)

    def available_chars(self, family: str) -> Dict[str, int]:
        """{character: cid} for everything harvested for this family."""
        pool = self.glyphs.get(family, {})
        mapping: Dict[str, int] = {}
        for cid, char in self.chars.get(family, {}).items():
            if cid in pool or char == " ":
                mapping[char] = cid
        return mapping

    def summary(self) -> str:
        lines = []
        for family in sorted(self.glyphs):
            chars = self.available_chars(family)
            printable = "".join(sorted(c for c in chars if c != " "))
            lines.append(f"{family}: {len(chars)} chars -> {printable}")
        return "\n".join(lines)
