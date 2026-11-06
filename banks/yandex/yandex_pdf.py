#!/usr/bin/env python3
"""Low-level PDF layer for Yandex Bank receipts (donor-preserving).

This module is the Yandex analogue of ``rocket_pdf.py``: it parses the
donor PDF at the object/stream level *without* rebuilding it through
PyMuPDF/fitz or any other editor.

Design rule (see ``PROJECT.md``):

    A Yandex receipt is never redrawn.  The donor file is kept
    byte-for-byte; only a future patcher may change field values.

Consequently ``save()`` of an unmodified document writes the exact
original bytes.  Nothing is re-serialised, re-deflated, re-subset or
reordered on load/save, so a load -> save round-trip is byte-identical
by construction *and* is verified by ``test_yandex.py``.

What is parsed (JasperReports/OpenPDF-produced Yandex donors):

* ``%PDF-1.5`` header + binary comment line;
* the classic ``xref`` *table* (Yandex donors do not use xref streams
  or object streams: there are no ``/XRef`` / ``/ObjStm`` objects);
* the trailer dictionary (``/Root``, ``/Info``, ``/ID``, ``/Size``);
* every indirect object, in physical file order, with its dictionary
  bytes and -- for stream objects -- its *raw* (still compressed)
  stream bytes, taken at exactly ``/Length`` bytes after ``stream``;
* FlateDecode streams, decoded on demand with :mod:`zlib` (the raw
  bytes are never recompressed here);
* the three Type0 fonts used by the receipt -- ``YSText-Regular``
  (``/F1``), ``YSText-Medium`` (``/F2``) and ``YSLogotype-Regular``
  (``/F3``) -- including their CIDFontType2 descendants, font
  descriptors, embedded ``FontFile2`` programs, ``CIDSet`` streams,
  ``/W`` widths and ``ToUnicode`` CMaps;
* the page content stream, whose text is stored as two-byte CID codes
  (``Identity-H``) inside literal strings of ``Tj`` / ``TJ`` operators
  and can be decoded to Unicode through the fonts' ``ToUnicode`` maps.

Standard library only.  No field values are modified anywhere in this
module: this chat only learns to open a real Yandex receipt and save
it absolutely unchanged.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

PathLike = Union[str, Path]


class YandexPdfError(ValueError):
    """Raised when a file does not look like a supported Yandex donor."""


# --------------------------------------------------------------------------
# small byte-level helpers
# --------------------------------------------------------------------------

_OBJ_HEADER = re.compile(rb"(\d+)\s+(\d+)\s+obj\r?\n")
_LENGTH_DIRECT = re.compile(rb"/Length\s+(\d+)(?![\s\d]*R)")
_LENGTH_INDIRECT = re.compile(rb"/Length\s+\d+\s+\d+\s+R")
_REF = rb"(\d+)\s+\d+\s+R"

_LITERAL_ESCAPES = {
    ord("n"): 10,
    ord("r"): 13,
    ord("t"): 9,
    ord("b"): 8,
    ord("f"): 12,
    ord("("): 40,
    ord(")"): 41,
    ord("\\"): 92,
}


def _skip_literal(data: bytes, start: int) -> int:
    """Return the index just past the literal string starting at ``start``.

    ``data[start]`` must be ``(``.  Handles nested parentheses, backslash
    escapes and up-to-three-digit octal escapes.
    """
    if data[start : start + 1] != b"(":
        raise YandexPdfError(f"expected '(' at offset {start}")
    depth = 1
    i = start + 1
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash
            i += 2
            continue
        if c == 0x28:  # (
            depth += 1
        elif c == 0x29:  # )
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise YandexPdfError(f"unterminated literal string at offset {start}")


def parse_literal(data: bytes, start: int) -> Tuple[bytes, int]:
    """Decode the PDF literal string at ``start``; return (bytes, next)."""
    if data[start : start + 1] != b"(":
        raise YandexPdfError(f"expected '(' at offset {start}")
    out = bytearray()
    depth = 1
    i = start + 1
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash escape
            if i + 1 >= len(data):
                break
            nxt = data[i + 1]
            if 0x30 <= nxt <= 0x37:  # octal, up to 3 digits
                j = i + 1
                digits = bytearray()
                while j < len(data) and len(digits) < 3 and 0x30 <= data[j] <= 0x37:
                    digits.append(data[j])
                    j += 1
                out.append(int(bytes(digits), 8))
                i = j
                continue
            if nxt in (0x0D, 0x0A):  # line continuation
                i += 2
                if nxt == 0x0D and i < len(data) and data[i] == 0x0A:
                    i += 1
                continue
            out.append(_LITERAL_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    raise YandexPdfError(f"unterminated literal string at offset {start}")


def _dict_end(data: bytes, start: int) -> int:
    """Return the index just past the ``<< ... >>`` dict at ``start``."""
    if data[start : start + 2] != b"<<":
        raise YandexPdfError(f"expected '<<' at offset {start}")
    depth = 0
    i = start
    while i < len(data):
        pair = data[i : i + 2]
        if data[i : i + 1] == b"(":
            i = _skip_literal(data, i)
            continue
        if pair == b"<<":
            depth += 1
            i += 2
            continue
        if pair == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if data[i : i + 1] == b"<":  # hex string <...>
            close = data.find(b">", i + 1)
            if close == -1:
                break
            i = close + 1
            continue
        i += 1
    raise YandexPdfError(f"unterminated dictionary at offset {start}")


def _ref_after(dict_bytes: bytes, key: bytes) -> Optional[int]:
    """Resolve ``/Key N G R`` in ``dict_bytes`` to ``N`` (or None)."""
    match = re.search(re.escape(key) + rb"\s+" + _REF, dict_bytes)
    return int(match.group(1)) if match else None


def _decode_pdf_string(raw: bytes) -> str:
    """Decode a PDF literal-string payload (UTF-16BE BOM or byte string)."""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _parse_widths(dict_bytes: bytes) -> Dict[int, float]:
    """Parse a CIDFont ``/W`` array into ``{cid: width}``."""
    match = re.search(rb"/W\s*\[", dict_bytes)
    if not match:
        return {}
    # find the matching ']' (no nested arrays except width groups)
    start = match.end()
    depth = 1
    i = start
    while i < len(dict_bytes) and depth:
        if dict_bytes[i : i + 1] == b"[":
            depth += 1
        elif dict_bytes[i : i + 1] == b"]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    tokens = re.findall(rb"\[|\]|-?\d+(?:\.\d+)?", dict_bytes[start:i])
    widths: Dict[int, float] = {}
    pos = 0
    while pos < len(tokens):
        cid = int(float(tokens[pos]))
        pos += 1
        if pos < len(tokens) and tokens[pos] == b"[":
            pos += 1
            while pos < len(tokens) and tokens[pos] != b"]":
                widths[cid] = float(tokens[pos])
                cid += 1
                pos += 1
            pos += 1  # consume ']'
        else:
            if pos + 1 >= len(tokens):
                break
            last = int(float(tokens[pos]))
            width = float(tokens[pos + 1])
            pos += 2
            for code in range(cid, last + 1):
                widths[code] = width
    return widths


def parse_tounicode(decoded: bytes) -> Dict[int, str]:
    """Parse a ToUnicode CMap stream into ``{cid: unicode_text}``.

    Supports ``beginbfchar``/``endbfchar`` and both ``beginbfrange``
    forms (single destination and destination array).  Destinations
    are UTF-16BE hex strings, as written by OpenPDF for these donors.
    """
    text = decoded.decode("latin-1")
    mapping: Dict[int, str] = {}

    def dest(hex_str: str) -> str:
        raw = bytes.fromhex(hex_str)
        if len(raw) % 2 == 0 and len(raw) >= 2:
            try:
                return raw.decode("utf-16-be")
            except UnicodeDecodeError:
                pass
        return raw.decode("latin-1")

    for section in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section):
            mapping[int(src, 16)] = dest(dst)

    for section in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        # array form: <lo> <hi> [<d1> <d2> ...]
        for lo, hi, array in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[([^\]]*)\]", section
        ):
            destinations = re.findall(r"<([0-9A-Fa-f]+)>", array)
            for offset, dst in enumerate(destinations):
                mapping[int(lo, 16) + offset] = dest(dst)
        # scalar form: <lo> <hi> <dst> -- remove array lines first so
        # their destinations are not mistaken for range starts.
        scalar_text = re.sub(r"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\[[^\]]*\]", "", section)
        for lo, hi, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", scalar_text
        ):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            base = dest(dst)
            for offset, cid in enumerate(range(lo_i, hi_i + 1)):
                if offset == 0 or len(base) != 1:
                    mapping[cid] = (
                        base if offset == 0 else chr(ord(base) + offset)
                    )
                else:
                    mapping[cid] = chr(ord(base) + offset)
    return mapping


# --------------------------------------------------------------------------
# parsed structures
# --------------------------------------------------------------------------


class PdfObject:
    """One indirect object, exactly as it appears in the donor file."""

    def __init__(
        self,
        num: int,
        gen: int,
        offset: int,
        dict_bytes: bytes,
        stream_raw: Optional[bytes],
        span: Tuple[int, int],
    ):
        self.num = num
        self.gen = gen
        self.offset = offset
        self.dict_bytes = dict_bytes
        self.stream_raw = stream_raw
        # byte range [start, end) of "N G obj ... endobj" in the source
        self.span = span

    @property
    def has_stream(self) -> bool:
        return self.stream_raw is not None

    def decoded_stream(self) -> bytes:
        """Return the stream payload, inflating FlateDecode if present."""
        if self.stream_raw is None:
            raise YandexPdfError(f"object {self.num} has no stream")
        if b"/FlateDecode" in self.dict_bytes:
            return zlib.decompress(self.stream_raw)
        return self.stream_raw

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PdfObject {self.num} {self.gen} stream={self.has_stream}>"


@dataclass
class FontInfo:
    """A Type0 font and everything hanging off it in the donor."""

    resource: str  # e.g. "F1" (name in the page /Resources)
    type0_obj: int
    base_font: str  # e.g. "LRBYZY+YSText-Regular"
    font_name: str  # e.g. "YSText-Regular" (subset prefix stripped)
    subset_prefix: Optional[str]  # e.g. "LRBYZY", None if not subset
    encoding: Optional[str]  # e.g. "Identity-H"
    descendant_obj: Optional[int] = None  # CIDFontType2 object
    font_descriptor_obj: Optional[int] = None
    font_file_obj: Optional[int] = None  # FontFile2 (TrueType) stream
    cidset_obj: Optional[int] = None
    tounicode_obj: Optional[int] = None
    tounicode: Dict[int, str] = field(default_factory=dict)
    widths: Dict[int, float] = field(default_factory=dict)

    @property
    def unicode_to_cid(self) -> Dict[str, int]:
        """Reverse map (first CID wins), for a future patcher."""
        reverse: Dict[str, int] = {}
        for cid, text in sorted(self.tounicode.items()):
            reverse.setdefault(text, cid)
        return reverse

    def decode_cids(self, payload: bytes) -> str:
        """Decode two-byte Identity-H CID codes through ToUnicode."""
        if len(payload) % 2:
            raise YandexPdfError(
                f"font {self.font_name}: odd CID byte count {len(payload)}"
            )
        chars: List[str] = []
        for i in range(0, len(payload), 2):
            cid = int.from_bytes(payload[i : i + 2], "big")
            chars.append(self.tounicode.get(cid, "\ufffd"))
        return "".join(chars)


@dataclass
class TextRun:
    """One Tj/TJ text-showing operation from the content stream."""

    font_resource: str
    font_size: Optional[float]
    x: Optional[float]
    y: Optional[float]
    cid_bytes: bytes
    text: str


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------


class YandexPdfFile:
    """A parsed Yandex donor, saveable byte-for-byte as loaded."""

    def __init__(self, data: bytes):
        self.data = data
        self.header: bytes = b""
        self.trailer_dict: bytes = b""
        self.startxref: int = 0
        self.objects: Dict[int, PdfObject] = {}
        self.order: List[int] = []  # object numbers in physical file order
        self._fonts: Optional[Dict[str, FontInfo]] = None
        self._parse()

    # ------------------------------------------------------------- parsing

    def _parse(self) -> None:
        match = re.match(rb"%PDF-\d\.\d\r?\n(%[^\r\n]*\r?\n)?", self.data)
        if not match:
            raise YandexPdfError("not a PDF: missing %PDF- header")
        self.header = match.group(0)

        entries = self._xref_entries()
        for offset, num, gen in sorted(entries):
            obj = self._object_at(offset, num, gen)
            if obj.num in self.objects:
                raise YandexPdfError(f"duplicate object {obj.num}")
            self.objects[obj.num] = obj
            self.order.append(obj.num)

    def _xref_entries(self) -> List[Tuple[int, int, int]]:
        """Walk the classic xref table(s); return (offset, num, gen)."""
        last = None
        for last in re.finditer(rb"startxref\s+(\d+)", self.data):
            pass
        if last is None:
            raise YandexPdfError("no startxref found")
        self.startxref = int(last.group(1))

        merged: Dict[int, Tuple[int, int]] = {}
        xref_pos: Optional[int] = self.startxref
        seen_positions = set()
        while xref_pos is not None:
            if xref_pos in seen_positions:
                raise YandexPdfError("loop in xref /Prev chain")
            seen_positions.add(xref_pos)
            section = self.data[xref_pos:]
            if not section.startswith(b"xref"):
                # Yandex donors use a plain xref table.  An xref stream
                # would mean some other tool rewrote the file.
                raise YandexPdfError(
                    "cross-reference streams are not supported "
                    "(expected a classic xref table like the Yandex original)"
                )
            cursor = len(b"xref")
            trailer_dict = b""
            while True:
                stripped = section[cursor:].lstrip(b"\r\n ")
                cursor = len(section) - len(stripped)
                if stripped.startswith(b"trailer"):
                    dict_start = cursor + len(b"trailer")
                    while section[dict_start : dict_start + 1] in (b" ", b"\r", b"\n"):
                        dict_start += 1
                    dict_end = _dict_end(section, dict_start)
                    trailer_dict = section[dict_start:dict_end]
                    break
                head = re.match(rb"(\d+)\s+(\d+)[ \r\n]", stripped)
                if not head:
                    raise YandexPdfError("malformed xref subsection header")
                first_obj, count = int(head.group(1)), int(head.group(2))
                cursor += head.end()
                for index in range(count):
                    line_end = section.find(b"\n", cursor)
                    if line_end == -1:
                        raise YandexPdfError("unterminated xref entry")
                    line = section[cursor:line_end].strip()
                    entry = re.fullmatch(rb"(\d{10})\s+(\d{5})\s+([nf])", line)
                    if not entry:
                        raise YandexPdfError(f"malformed xref entry: {line!r}")
                    obj_num = first_obj + index
                    if entry.group(3) == b"n" and obj_num not in merged:
                        merged[obj_num] = (int(entry.group(1)), int(entry.group(2)))
                    cursor = line_end + 1
            if not self.trailer_dict:
                self.trailer_dict = trailer_dict
            prev = re.search(rb"/Prev\s+(\d+)", trailer_dict)
            xref_pos = int(prev.group(1)) if prev else None

        return [(offset, num, gen) for num, (offset, gen) in merged.items()]

    def _object_at(self, offset: int, expected_num: int, expected_gen: int) -> PdfObject:
        header = _OBJ_HEADER.match(self.data, offset)
        if not header:
            raise YandexPdfError(f"no object header at xref offset {offset}")
        num, gen = int(header.group(1)), int(header.group(2))
        if (num, gen) != (expected_num, expected_gen):
            raise YandexPdfError(
                f"xref says {expected_num} {expected_gen} R but offset "
                f"{offset} contains {num} {gen} obj"
            )
        cursor = header.end()

        if self.data[cursor : cursor + 2] != b"<<":
            # Not every indirect object is a dictionary: object 9 of a
            # Yandex donor, for example, is a destination array
            # "[1 0 R/XYZ 0 852 0]".  Such objects never carry a
            # stream; keep their whole body in dict_bytes.
            endobj = self.data.find(b"endobj", cursor)
            if endobj == -1:
                raise YandexPdfError(f"object {num}: missing endobj")
            dict_bytes = self.data[cursor:endobj].rstrip(b"\r\n")
            span = (offset, endobj + len(b"endobj"))
            return PdfObject(num, gen, offset, dict_bytes, None, span)
        dict_end = _dict_end(self.data, cursor)
        dict_bytes = self.data[cursor:dict_end]

        stream_raw: Optional[bytes] = None
        after_dict = dict_end
        if self.data[after_dict : after_dict + 6] == b"stream":
            if _LENGTH_INDIRECT.search(dict_bytes):
                raise YandexPdfError(
                    f"object {num}: indirect /Length is not used by Yandex donors"
                )
            length_match = _LENGTH_DIRECT.search(dict_bytes)
            if not length_match:
                raise YandexPdfError(f"object {num}: stream without direct /Length")
            size = int(length_match.group(1))
            stream_start = after_dict + len(b"stream")
            if self.data[stream_start : stream_start + 2] == b"\r\n":
                stream_start += 2
            elif self.data[stream_start : stream_start + 1] in (b"\n", b"\r"):
                stream_start += 1
            else:
                raise YandexPdfError(f"object {num}: 'stream' not followed by EOL")
            stream_end = stream_start + size
            stream_raw = self.data[stream_start:stream_end]
            if len(stream_raw) != size:
                raise YandexPdfError(f"object {num}: stream truncated")
            tail = self.data[stream_end : stream_end + 11]
            if not (tail.startswith(b"\nendstream") or tail.startswith(b"\r\nendstream")):
                raise YandexPdfError(
                    f"object {num}: stream bytes do not end where /Length says"
                )
            endobj = self.data.find(b"endobj", stream_end)
        else:
            endobj = self.data.find(b"endobj", dict_end)
        if endobj == -1:
            raise YandexPdfError(f"object {num}: missing endobj")
        span = (offset, endobj + len(b"endobj"))
        return PdfObject(num, gen, offset, dict_bytes, stream_raw, span)

    # ------------------------------------------------------------ accessors

    def obj(self, num: int) -> PdfObject:
        return self.objects[num]

    def find_objects(self, needle: bytes) -> List[PdfObject]:
        """All objects whose dictionary contains the given token."""
        return [self.objects[n] for n in self.order if needle in self.objects[n].dict_bytes]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def info(self) -> Dict[str, str]:
        """Document info dictionary (/Creator, /Producer, dates, ...)."""
        info_num = _ref_after(self.trailer_dict, b"/Info")
        if info_num is None or info_num not in self.objects:
            return {}
        dict_bytes = self.objects[info_num].dict_bytes
        result: Dict[str, str] = {}
        for key in (b"Creator", b"Producer", b"CreationDate", b"ModDate", b"Title"):
            match = re.search(rb"/" + key + rb"\s*\(", dict_bytes)
            if match:
                raw, _ = parse_literal(dict_bytes, match.end() - 1)
                result[key.decode("ascii")] = _decode_pdf_string(raw)
        return result

    def page_object(self) -> PdfObject:
        """The single /Type /Page object of the receipt."""
        for num in self.order:
            obj = self.objects[num]
            if re.search(rb"/Type\s*/Page(?![a-zA-Z])", obj.dict_bytes):
                return obj
        raise YandexPdfError("no /Type /Page object found")

    def content_stream(self) -> PdfObject:
        """The page's /Contents stream object."""
        page = self.page_object()
        contents = _ref_after(page.dict_bytes, b"/Contents")
        if contents is None:
            raise YandexPdfError("page has no /Contents reference")
        return self.objects[contents]

    # ---------------------------------------------------------------- fonts

    def fonts(self) -> Dict[str, FontInfo]:
        """Type0 fonts keyed by page-resource name (``F1``/``F2``/``F3``)."""
        if self._fonts is not None:
            return self._fonts
        page = self.page_object()
        font_dict_match = re.search(rb"/Font\s*<<", page.dict_bytes)
        if not font_dict_match:
            raise YandexPdfError("page resources have no /Font dictionary")
        font_dict_start = page.dict_bytes.index(b"<<", font_dict_match.start())
        font_dict_end = _dict_end(page.dict_bytes, font_dict_start)
        font_dict = page.dict_bytes[font_dict_start:font_dict_end]

        fonts: Dict[str, FontInfo] = {}
        for resource_b, ref_b in re.findall(rb"/(F\w+)\s+(\d+)\s+\d+\s+R", font_dict):
            resource = resource_b.decode("ascii")
            type0_num = int(ref_b)
            type0 = self.objects[type0_num]
            if b"/Type0" not in type0.dict_bytes:
                raise YandexPdfError(
                    f"font {resource} (object {type0_num}) is not /Type0: "
                    "Yandex receipts use Type0/Identity-H fonts"
                )
            base_match = re.search(rb"/BaseFont\s*/([^\s/<>\[\]()]+)", type0.dict_bytes)
            if not base_match:
                raise YandexPdfError(f"font object {type0_num}: no /BaseFont")
            base_font = base_match.group(1).decode("latin-1")
            if "+" in base_font:
                subset_prefix, font_name = base_font.split("+", 1)
            else:
                subset_prefix, font_name = None, base_font
            encoding_match = re.search(rb"/Encoding\s*/([^\s/<>\[\]()]+)", type0.dict_bytes)

            descendant = _ref_after(type0.dict_bytes, b"/DescendantFonts")
            if descendant is None:
                array_match = re.search(rb"/DescendantFonts\s*\[\s*(\d+)\s+\d+\s+R", type0.dict_bytes)
                descendant = int(array_match.group(1)) if array_match else None

            descriptor = font_file = cidset = None
            widths: Dict[int, float] = {}
            if descendant is not None:
                cid_font = self.objects[descendant]
                widths = _parse_widths(cid_font.dict_bytes)
                descriptor = _ref_after(cid_font.dict_bytes, b"/FontDescriptor")
            if descriptor is not None:
                descriptor_obj = self.objects[descriptor]
                font_file = _ref_after(descriptor_obj.dict_bytes, b"/FontFile2")
                cidset = _ref_after(descriptor_obj.dict_bytes, b"/CIDSet")

            tounicode_num = _ref_after(type0.dict_bytes, b"/ToUnicode")
            tounicode: Dict[int, str] = {}
            if tounicode_num is not None:
                tounicode = parse_tounicode(self.objects[tounicode_num].decoded_stream())

            fonts[resource] = FontInfo(
                resource=resource,
                type0_obj=type0_num,
                base_font=base_font,
                font_name=font_name,
                subset_prefix=subset_prefix,
                encoding=encoding_match.group(1).decode("latin-1") if encoding_match else None,
                descendant_obj=descendant,
                font_descriptor_obj=descriptor,
                font_file_obj=font_file,
                cidset_obj=cidset,
                tounicode_obj=tounicode_num,
                tounicode=tounicode,
                widths=widths,
            )
        self._fonts = fonts
        return fonts

    # ------------------------------------------------------------- text runs

    def text_runs(self) -> List[TextRun]:
        """Decode every Tj/TJ run of the content stream to Unicode.

        The CID bytes themselves are preserved on each run; this is a
        read-only view -- nothing in the document is changed by it.
        """
        fonts = self.fonts()
        content = self.content_stream().decoded_stream()
        runs: List[TextRun] = []
        pending: List[bytes] = []
        current_font: Optional[str] = None
        current_size: Optional[float] = None
        current_x: Optional[float] = None
        current_y: Optional[float] = None

        def flush(operator: bytes) -> None:
            if not pending:
                return
            if current_font is None or current_font not in fonts:
                raise YandexPdfError(f"{operator!r} with no known font selected")
            payload = b"".join(pending)
            pending.clear()
            runs.append(
                TextRun(
                    font_resource=current_font,
                    font_size=current_size,
                    x=current_x,
                    y=current_y,
                    cid_bytes=payload,
                    text=fonts[current_font].decode_cids(payload),
                )
            )

        i = 0
        while i < len(content):
            byte = content[i : i + 1]
            if byte == b"(":
                payload, i = parse_literal(content, i)
                pending.append(payload)
                continue
            if byte == b"<" and content[i : i + 2] != b"<<":
                close = content.find(b">", i + 1)
                if close == -1:
                    raise YandexPdfError("unterminated hex string in content stream")
                hex_text = re.sub(rb"\s+", b"", content[i + 1 : close])
                if len(hex_text) % 2:
                    hex_text += b"0"
                pending.append(bytes.fromhex(hex_text.decode("ascii")))
                i = close + 1
                continue
            font_match = re.match(rb"/(\w+)\s+(\d+(?:\.\d+)?)\s+Tf", content[i:])
            if font_match:
                current_font = font_match.group(1).decode("ascii")
                current_size = float(font_match.group(2))
                i += font_match.end()
                continue
            tm_match = re.match(
                rb"1\s+0\s+0\s+1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Tm",
                content[i:],
            )
            if tm_match:
                current_x = float(tm_match.group(1))
                current_y = float(tm_match.group(2))
                i += tm_match.end()
                continue
            word_match = re.match(rb"[A-Za-z'\"*]+", content[i:])
            if word_match:
                word = word_match.group(0)
                if word in (b"Tj", b"TJ"):
                    flush(word)
                i += word_match.end()
                continue
            i += 1
        return runs

    def extracted_text(self) -> List[str]:
        """Decoded text of all non-empty runs, in content-stream order."""
        return [run.text for run in self.text_runs() if run.text.strip()]

    # ---------------------------------------------------------------- saving

    def to_bytes(self) -> bytes:
        """The document bytes, absolutely unchanged from the donor.

        There is deliberately no re-serialisation path in this module:
        resaving parsed objects would reorder/rewrite bytes (framing,
        stream compression, xref offsets) and break byte identity with
        the original Yandex export.
        """
        return self.data

    def save(self, path: PathLike) -> None:
        """Write the donor bytes to ``path`` exactly as loaded."""
        Path(path).write_bytes(self.to_bytes())

    @classmethod
    def load(cls, path: PathLike) -> "YandexPdfFile":
        return cls(Path(path).read_bytes())


# Familiar alias, mirroring rocket_pdf.PdfFile.
PdfFile = YandexPdfFile


def load(path: PathLike) -> YandexPdfFile:
    """Load and parse a Yandex donor PDF (see :class:`YandexPdfFile`)."""
    return YandexPdfFile.load(path)


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import sys

    document = load(sys.argv[1])
    print(f"{sys.argv[1]}: {len(document.data)} bytes, sha256={document.sha256}")
    print(f"info: {document.info}")
    for name, font in sorted(document.fonts().items()):
        print(
            f"  /{name} obj{font.type0_obj} {font.base_font} "
            f"ToUnicode(obj{font.tounicode_obj})={len(font.tounicode)} entries"
        )
    for line in document.extracted_text():
        print(f"  {line}")
