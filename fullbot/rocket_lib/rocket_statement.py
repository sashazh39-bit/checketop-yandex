#!/usr/bin/env python3
"""Rocket Bank account statement (выписка) patcher / generator.

The statement is a *different* document type from the Rocket receipts
in this folder (see BANK.md, «Выписка по счёту»): it is produced by
WeasyPrint 66.0, not iText.  A genuine statement therefore has

* an xref *stream* and an object stream (ObjStm), not a classic xref;
* no CreationDate/ModDate and no trailer /ID at all;
* exactly two document-wide TrueType font subsets
  (``SBKLGE+IBM-Semi-Bold`` and ``DUFPOI+Loos-Extended-Ultra-Bold-…``);
* every Flate stream recompressing byte-identically at zlib level 9.

As with the receipts, a statement is never redrawn: a genuine donor
from ``donors_statement/`` is patched field by field and re-saved in
the same one-pass WeasyPrint layout.  An unmodified round-trip
reproduces the donor byte for byte (Stage 2 of the statement chat).

Public entry point: ``generate_rocket_statement(...)`` (bottom).
"""

from __future__ import annotations

import copy
import re
import secrets
import sys
import zlib
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

MSK = timezone(timedelta(hours=3))
MAC_EPOCH_DELTA = 2082844800  # seconds between 1904-01-01 and 1970-01-01
FLATE_LEVEL = 9  # every Flate stream of every statement donor (78 da)


class StatementError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# small PDF-dictionary helpers
# --------------------------------------------------------------------------

def _dict_end(data: bytes, start: int) -> int:
    """Index just past the ``>>`` closing the dict starting at ``start``."""
    depth = 0
    i = start
    while i < len(data):
        two = data[i : i + 2]
        if two == b"<<":
            depth += 1
            i += 2
            continue
        if two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if data[i : i + 1] == b"(":
            i += 1
            while i < len(data):
                if data[i : i + 1] == b"\\" :
                    i += 2
                    continue
                if data[i : i + 1] == b")":
                    i += 1
                    break
                i += 1
            continue
        if data[i : i + 1] == b"<" and two != b"<<":
            j = data.find(b">", i)
            i = (j + 1) if j != -1 else len(data)
            continue
        i += 1
    raise StatementError("unterminated PDF dictionary")


def _set_dict_number(dict_bytes: bytes, key: bytes, value: int) -> bytes:
    """Replace ``/Key <number>`` (not ``/Key1`` …) inside a dict."""
    pattern = re.compile(re.escape(key) + rb"(?![0-9])\s*-?\d+")
    out, n = pattern.subn(key + b" " + str(value).encode(), dict_bytes, count=1)
    if n == 0:
        raise StatementError(f"dictionary carries no {key!r}: {dict_bytes[:120]!r}")
    return out


# --------------------------------------------------------------------------
# low-level WeasyPrint PDF (xref stream + ObjStm)
# --------------------------------------------------------------------------

class _StreamObject:
    def __init__(self, num: int, dict_bytes: bytes, raw: bytes, span: bytes):
        self.num = num
        self.dict_bytes = dict_bytes
        self.raw = raw                # stream payload as stored (compressed)
        self.span = span              # whole original ``N 0 obj … endobj\n``
        self._decoded: Optional[bytes] = None
        self.changed = False

    @property
    def is_flate(self) -> bool:
        return b"/FlateDecode" in self.dict_bytes

    @property
    def decoded(self) -> bytes:
        if self._decoded is None:
            self._decoded = zlib.decompress(self.raw) if self.is_flate else self.raw
        return self._decoded

    @decoded.setter
    def decoded(self, payload: bytes) -> None:
        if payload != self.decoded:
            self.changed = True
        self._decoded = payload


class StatementPdf:
    """A WeasyPrint PDF parsed well enough to be rebuilt byte-identically."""

    def __init__(self, data: bytes):
        self.data = data
        self.header: bytes = b""
        self.streams: Dict[int, _StreamObject] = {}
        self.stream_order: List[int] = []      # physical order in the file
        self.comp: Dict[int, bytes] = {}       # objects inside the ObjStm
        self.comp_order: List[int] = []
        self.objstm_num: Optional[int] = None
        self.xref_num: Optional[int] = None
        self.size: int = 0
        self.root_ref: bytes = b""
        self.info_ref: bytes = b""
        self._comp_changed = False
        self._parse()

    # ------------------------------------------------------------ parsing

    def _parse(self) -> None:
        data = self.data
        first = re.search(rb"(?m)^\d+ 0 obj\n", data)
        if not first:
            raise StatementError("no objects found (not a WeasyPrint PDF?)")
        self.header = data[: first.start()]
        for m in re.finditer(rb"(?m)^(\d+) 0 obj\n", data):
            num = int(m.group(1))
            dict_start = m.end()
            if data[dict_start : dict_start + 2] != b"<<":
                raise StatementError(f"object {num}: dictionary expected")
            dict_end = _dict_end(data, dict_start)
            dict_bytes = data[dict_start:dict_end]
            stream_kw = re.match(rb"\r?\nstream\r?\n", data[dict_end:])
            if not stream_kw:
                raise StatementError(
                    f"object {num}: physical non-stream object "
                    "(statement donors keep those in the ObjStm)"
                )
            length_m = re.search(rb"/Length(?![0-9])\s+(\d+)", dict_bytes)
            if not length_m:
                raise StatementError(f"object {num}: no direct /Length")
            size = int(length_m.group(1))
            payload_start = dict_end + stream_kw.end()
            raw = data[payload_start : payload_start + size]
            end_m = re.match(rb"\r?\nendstream\r?\nendobj\r?\n?", data[payload_start + size :])
            if not end_m:
                raise StatementError(f"object {num}: endstream not found")
            span = data[m.start() : payload_start + size + end_m.end()]
            obj = _StreamObject(num, dict_bytes, raw, span)
            self.streams[num] = obj
            self.stream_order.append(num)
            if b"/Type /ObjStm" in dict_bytes or b"/Type/ObjStm" in dict_bytes:
                self.objstm_num = num
            if b"/Type /XRef" in dict_bytes or b"/Type/XRef" in dict_bytes:
                self.xref_num = num
        if self.objstm_num is None or self.xref_num is None:
            raise StatementError("no ObjStm / XRef stream (not a statement donor?)")

        xref_dict = self.streams[self.xref_num].dict_bytes
        self.size = int(re.search(rb"/Size\s+(\d+)", xref_dict).group(1))
        self.root_ref = re.search(rb"/Root\s+(\d+\s+\d+\s+R)", xref_dict).group(1)
        info_m = re.search(rb"/Info\s+(\d+\s+\d+\s+R)", xref_dict)
        self.info_ref = info_m.group(1) if info_m else b""

        objstm = self.streams[self.objstm_num]
        n = int(re.search(rb"/N\s+(\d+)", objstm.dict_bytes).group(1))
        first_body = int(re.search(rb"/First\s+(\d+)", objstm.dict_bytes).group(1))
        decoded = objstm.decoded
        header_line = decoded[:first_body].decode("latin-1")
        numbers = [int(t) for t in header_line.split()]
        if len(numbers) != 2 * n:
            raise StatementError("ObjStm header does not match /N")
        pairs = [(numbers[i], numbers[i + 1]) for i in range(0, 2 * n, 2)]
        for idx, (num, offset) in enumerate(pairs):
            end = pairs[idx + 1][1] if idx + 1 < len(pairs) else len(decoded) - first_body
            self.comp[num] = decoded[first_body + offset : first_body + end]
            self.comp_order.append(num)

    # ------------------------------------------------------------ access

    def stream(self, num: int) -> _StreamObject:
        return self.streams[num]

    def set_stream_decoded(self, num: int, payload: bytes) -> None:
        self.streams[num].decoded = payload

    def comp_body(self, num: int) -> bytes:
        return self.comp[num]

    def set_comp_body(self, num: int, body: bytes) -> None:
        if self.comp.get(num) != body:
            self.comp[num] = body
            self._comp_changed = True

    def resolve(self, body: bytes, key: bytes) -> Optional[int]:
        m = re.search(re.escape(key) + rb"\s+\[?\s*(\d+)\s+\d+\s+R", body)
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------ saving

    def _objstm_decoded(self) -> bytes:
        offsets: List[int] = []
        cursor = 0
        for num in self.comp_order:
            offsets.append(cursor)
            cursor += len(self.comp[num])
        header_line = " ".join(
            f"{num} {off}" for num, off in zip(self.comp_order, offsets)
        ).encode("latin-1")
        return header_line + b"\n" + b"".join(self.comp[n] for n in self.comp_order)

    def _serialize_stream(self, obj: _StreamObject) -> bytes:
        if not obj.changed and obj.num not in (self.objstm_num, self.xref_num):
            return obj.span
        decoded = self._objstm_decoded() if obj.num == self.objstm_num else obj.decoded
        if obj.is_flate:
            raw = zlib.compress(decoded, FLATE_LEVEL)
        else:
            raw = decoded
        dict_bytes = obj.dict_bytes
        dict_bytes = _set_dict_number(dict_bytes, b"/Length", len(raw))
        if obj.num == self.objstm_num:
            dict_bytes = _set_dict_number(dict_bytes, b"/First", self._objstm_decoded().index(b"\n") + 1)
            dict_bytes = re.sub(rb"/N\s+\d+", b"/N " + str(len(self.comp_order)).encode(), dict_bytes)
        if b"/Length1" in dict_bytes:
            dict_bytes = _set_dict_number(dict_bytes, b"/Length1", len(decoded))
        return (
            f"{obj.num} 0 obj\n".encode()
            + dict_bytes
            + b"\nstream\n"
            + raw
            + b"\nendstream\nendobj\n"
        )

    def rebuild(self) -> bytes:
        chunks: List[bytes] = [self.header]
        offsets: Dict[int, int] = {}
        cursor = len(self.header)
        # The xref stream itself is written last; remember its place in order.
        order = [n for n in self.stream_order if n != self.xref_num] + [self.xref_num]
        for num in order:
            if num == self.xref_num:
                continue
            body = self._serialize_stream(self.streams[num])
            offsets[num] = cursor
            chunks.append(body)
            cursor += len(body)
        xref_offset = cursor
        offsets[self.xref_num] = xref_offset

        comp_index = {num: i for i, num in enumerate(self.comp_order)}
        entries = bytearray()
        for num in range(self.size):
            if num == 0:
                entries += bytes([0]) + (0).to_bytes(2, "big") + (65535).to_bytes(2, "big")
            elif num in comp_index:
                entries += bytes([2]) + self.objstm_num.to_bytes(2, "big") + comp_index[num].to_bytes(2, "big")
            elif num in offsets:
                entries += bytes([1]) + offsets[num].to_bytes(2, "big") + (0).to_bytes(2, "big")
            else:
                entries += bytes([0]) + (0).to_bytes(2, "big") + (65535).to_bytes(2, "big")
        xref_obj = self.streams[self.xref_num]
        xref_raw = zlib.compress(bytes(entries), FLATE_LEVEL)
        xref_dict = _set_dict_number(xref_obj.dict_bytes, b"/Length", len(xref_raw))
        xref_dict = _set_dict_number(xref_dict, b"/Size", self.size)
        xref_body = (
            f"{self.xref_num} 0 obj\n".encode()
            + xref_dict
            + b"\nstream\n"
            + xref_raw
            + b"\nendstream\nendobj\n"
        )
        tail = (
            b"startxref\n"
            + str(xref_offset).encode()
            + b"\n%%EOF\n"
        )
        return b"".join(chunks) + xref_body + tail

    def save(self, path) -> None:
        Path(path).write_bytes(self.rebuild())


def load_statement_pdf(path) -> StatementPdf:
    return StatementPdf(Path(path).read_bytes())


# --------------------------------------------------------------------------
# fonts (TrueType subsets, CID == GID)
# --------------------------------------------------------------------------

def parse_tounicode(decoded: bytes) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", decoded, re.DOTALL):
        for a, b in re.findall(rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            mapping[int(a, 16)] = chr(int(b, 16))
    return mapping


_TOUNICODE_HEAD = """/CIDInit /ProcSet findresource begin
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
<0000> <ffff>
endcodespacerange
"""


def build_tounicode(entries: List[Tuple[int, str]]) -> bytes:
    lines = [f"<{cid:04x}> <{ord(ch):04x}>" for cid, ch in entries]
    body = (
        _TOUNICODE_HEAD
        + f"{len(lines)} beginbfchar\n"
        + "\n".join(lines)
        + "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )
    return body.encode("latin-1")


def parse_widths(dict_bytes: bytes) -> Dict[int, int]:
    m = re.search(rb"/W\s*\[", dict_bytes)
    if not m:
        return {}
    depth = 1
    i = m.end()
    while i < len(dict_bytes) and depth:
        c = dict_bytes[i : i + 1]
        if c == b"[":
            depth += 1
        elif c == b"]":
            depth -= 1
        i += 1
    tokens = re.findall(rb"\[|\]|-?\d+", dict_bytes[m.end() : i - 1])
    widths: Dict[int, int] = {}
    cur = 0
    while cur < len(tokens):
        tok = tokens[cur]
        if not re.fullmatch(rb"-?\d+", tok):
            cur += 1
            continue
        start = int(tok)
        cur += 1
        if cur < len(tokens) and tokens[cur] == b"[":
            cur += 1
            cid = start
            while cur < len(tokens) and tokens[cur] != b"]":
                widths[cid] = int(tokens[cur])
                cid += 1
                cur += 1
            cur += 1
        elif cur + 1 < len(tokens):
            last, value = int(tokens[cur]), int(tokens[cur + 1])
            for cid in range(start, last + 1):
                widths[cid] = value
            cur += 2
    return widths


def build_widths(widths: Dict[int, int]) -> bytes:
    cids = sorted(widths)
    groups: List[str] = []
    i = 0
    while i < len(cids):
        j = i
        while j + 1 < len(cids) and cids[j + 1] == cids[j] + 1:
            j += 1
        run = cids[i : j + 1]
        groups.append(f"{run[0]} [" + " ".join(str(widths[c]) for c in run) + "]")
        i = j + 1
    return ("[" + " ".join(groups) + "]").encode("latin-1")


def replace_widths(dict_bytes: bytes, widths: Dict[int, int]) -> bytes:
    m = re.search(rb"/W\s*\[", dict_bytes)
    if not m:
        raise StatementError("CIDFont carries no /W")
    depth = 1
    i = m.end()
    while i < len(dict_bytes) and depth:
        c = dict_bytes[i : i + 1]
        if c == b"[":
            depth += 1
        elif c == b"]":
            depth -= 1
        i += 1
    return dict_bytes[: m.start()] + b"/W " + build_widths(widths) + dict_bytes[i:]


@dataclass
class FontSlot:
    resource: str
    type0_num: int
    cidfont_num: int
    descriptor_num: int
    fontfile_num: int
    tounicode_num: int
    basefont: str            # e.g. IBM-Semi-Bold
    tag: str                 # e.g. SBKLGE
    cid_to_char: Dict[int, str] = dc_field(default_factory=dict)
    widths: Dict[int, int] = dc_field(default_factory=dict)

    @property
    def char_to_cid(self) -> Dict[str, int]:
        return {ch: cid for cid, ch in self.cid_to_char.items()}


@dataclass
class TextRun:
    page: int
    content_num: int
    font: str
    size: float
    x: float
    y: float
    text: str
    glyphs: List[dict] = dc_field(default_factory=list)  # {cid, adj:[tokens]}
    tj_span: Tuple[int, int] = (0, 0)
    tm_x_span: Tuple[int, int] = (0, 0)
    tm_y_span: Tuple[int, int] = (0, 0)
    desired_text: Optional[str] = None
    desired_x: Optional[float] = None
    desired_y: Optional[float] = None
    right_align: bool = False
    label_run: Optional["TextRun"] = None

    def __post_init__(self):
        if self.desired_text is None:
            self.desired_text = self.text


@dataclass
class RowBox:
    page: int
    y: float
    h: float
    node_span: Tuple[int, int]
    content_num: int


def _parse_tj(array: str) -> List[dict]:
    glyphs: List[dict] = []
    i = 0
    while i < len(array):
        ch = array[i]
        if ch == "<":
            j = array.index(">", i)
            hex_body = array[i + 1 : j]
            if hex_body == "":
                if glyphs:
                    glyphs[-1]["adj"].append("EMPTY")
            else:
                for k in range(0, len(hex_body), 4):
                    glyphs.append({"cid": int(hex_body[k : k + 4], 16), "adj": []})
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            m = re.match(r"-?[\d.]+", array[i:])
            if not m:
                i += 1
                continue
            if glyphs:
                glyphs[-1]["adj"].append(m.group(0))
            i += len(m.group(0))
    return glyphs


_TJ_TOKEN_RE = re.compile(
    r"/([A-Za-z0-9]+)\s+([\d.]+)\s+Tf"
    r"|1\s+0\s+0\s+-1\s+([\d.]+)\s+([\d.]+)\s+Tm"
    r"|\[(.*?)\]\s*TJ",
    re.DOTALL,
)


def _num(value: float) -> str:
    text = f"{value:.6f}"
    return text


# --------------------------------------------------------------------------
# statement model
# --------------------------------------------------------------------------

class Statement:
    """A genuine statement donor opened for field-level patching."""

    def __init__(self, path, glyph_donor_paths=None):
        self.path = str(path)
        self.pdf = load_statement_pdf(path)
        self.glyph_donor_paths = (
            [str(p) for p in glyph_donor_paths]
            if glyph_donor_paths is not None
            else [str(p) for p in sorted((_HERE / "donors_statement").glob("*.pdf"))]
        )
        self.generation_dt: Optional[datetime] = None  # UTC, fonts' head.modified
        self.pages: List[dict] = []
        self.fonts: Dict[str, FontSlot] = {}
        self.contents: Dict[int, str] = {}   # page index -> decoded content (latin-1)
        self.runs: List[TextRun] = []
        self.rows: List[RowBox] = []
        self._load_structure()
        self._parse_runs()
        self._parse_rows()
        self._font_pool = None
        self._adj_model = None

    # ------------------------------------------------------------ structure

    def _load_structure(self) -> None:
        pdf = self.pdf
        # fonts: every Type0 object lives in the ObjStm
        resource_by_type0: Dict[int, str] = {}
        for num, body in pdf.comp.items():
            for res, ref in re.findall(rb"/([A-Z]{6})\s+(\d+)\s+0\s+R", body):
                resource_by_type0[int(ref)] = res.decode()
        for num, body in sorted(pdf.comp.items()):
            if b"/Subtype /Type0" not in body and b"/Subtype/Type0" not in body:
                continue
            m = re.search(rb"/BaseFont\s*/([A-Z]{6})\+([A-Za-z0-9\-]+)", body)
            if not m:
                continue
            tag, basefont = m.group(1).decode(), m.group(2).decode()
            descendant = pdf.resolve(body, b"/DescendantFonts")
            tounicode = pdf.resolve(body, b"/ToUnicode")
            if descendant is None or tounicode is None:
                continue
            cid_body = pdf.comp_body(descendant)
            descriptor = pdf.resolve(cid_body, b"/FontDescriptor")
            fontfile = pdf.resolve(pdf.comp_body(descriptor), b"/FontFile2")
            self.fonts[resource_by_type0.get(num, tag)] = FontSlot(
                resource=resource_by_type0.get(num, tag),
                type0_num=num,
                cidfont_num=descendant,
                descriptor_num=descriptor,
                fontfile_num=fontfile,
                tounicode_num=tounicode,
                basefont=basefont,
                tag=tag,
                cid_to_char=parse_tounicode(pdf.stream(tounicode).decoded),
                widths=parse_widths(cid_body),
            )
        if not self.fonts:
            raise StatementError("no Type0 fonts found (not a statement?)")
        # pages in Kids order
        pages_num = None
        for num, body in pdf.comp.items():
            if (b"/Type /Pages" in body or b"/Type/Pages" in body) and b"/Kids" in body:
                pages_num = num
                kids = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", body.split(b"/Kids", 1)[1].split(b"]", 1)[0])]
                break
        if pages_num is None:
            raise StatementError("no /Pages object")
        for kid in kids:
            body = pdf.comp_body(kid)
            if b"/Type /Page" not in body and b"/Type/Page" not in body:
                continue
            content_num = pdf.resolve(body, b"/Contents")
            self.pages.append({"obj": kid, "content": content_num})
            self.contents[len(self.pages) - 1] = pdf.stream(content_num).decoded.decode("latin-1")

    def _parse_runs(self) -> None:
        for page_idx, page in enumerate(self.pages):
            content = self.contents[page_idx]
            font = ""
            size = 0.0
            x = y = 0.0
            tm_x_span = tm_y_span = (0, 0)
            for m in _TJ_TOKEN_RE.finditer(content):
                if m.group(1):
                    font, size = m.group(1), float(m.group(2))
                elif m.group(3):
                    x, y = float(m.group(3)), float(m.group(4))
                    tm_x_span = (m.start(3), m.end(3))
                    tm_y_span = (m.start(4), m.end(4))
                elif m.group(5) is not None:
                    glyphs = _parse_tj(m.group(5))
                    if not glyphs or font not in self.fonts:
                        continue
                    slot = self.fonts[font]
                    text = "".join(slot.cid_to_char.get(g["cid"], "\ufffd") for g in glyphs)
                    self.runs.append(
                        TextRun(
                            page=page_idx,
                            content_num=page["content"],
                            font=font,
                            size=size,
                            x=x,
                            y=y,
                            text=text,
                            glyphs=glyphs,
                            tj_span=(m.start(5), m.end(5)),
                            tm_x_span=tm_x_span,
                            tm_y_span=tm_y_span,
                        )
                    )

    def _parse_rows(self) -> None:
        for page_idx, page in enumerate(self.pages):
            content = self.contents[page_idx]
            # q/Q node stack (operators sit on their own lines)
            stack: List[dict] = []
            nodes: List[dict] = []
            for m in re.finditer(r"(?m)^(q|Q)[ \t]*$", content):
                if m.group(1) == "q":
                    stack.append({"start": m.start(), "children": 0})
                    if len(stack) > 1:
                        stack[-2]["children"] += 1
                elif stack:
                    node = stack.pop()
                    node["end"] = m.end()
                    nodes.append(node)
            for node in nodes:
                seg = content[node["start"] : node["end"]]
                if " BT" in seg or not (500 < len(seg) < 1500):
                    continue
                rect = re.search(r"37\.795276\s+([\d.]+)\s+718\.110236\s+([\d.]+)\s+re", seg)
                if rect:
                    self.rows.append(
                        RowBox(
                            page=page_idx,
                            y=float(rect.group(1)),
                            h=float(rect.group(2)),
                            node_span=(node["start"], node["end"]),
                            content_num=page["content"],
                        )
                    )
        self.rows.sort(key=lambda r: (r.page, r.y))

    # ------------------------------------------------------------- helpers

    def runs_on_page(self, page: int) -> List[TextRun]:
        return [r for r in self.runs if r.page == page]

    def ttfont_of(self, slot: FontSlot):
        from fontTools.ttLib import TTFont

        return TTFont(BytesIO(self.pdf.stream(slot.fontfile_num).decoded), recalcTimestamp=False)

    def font_modified(self, slot: FontSlot) -> datetime:
        tt = self.ttfont_of(slot)
        return datetime.fromtimestamp(tt["head"].modified - MAC_EPOCH_DELTA, tz=timezone.utc)

    def set_run_text(self, run: TextRun, text: str, *, right_align: bool = False, label_run: Optional[TextRun] = None) -> None:
        run.desired_text = text
        if right_align:
            run.right_align = True
        if label_run is not None:
            run.label_run = label_run

    # -------------------------------------------------- adjustment statistics

    def adjustment_model(self):
        """Per-glyph TJ adjustments measured on the statement donors.

        Returns ``(pair, char, default, float_token)`` where the first
        three map to the integer TJ adjustment WeasyPrint writes after
        a glyph and ``float_token`` is the fractional token that always
        follows the empty ``<>`` for that font/size.
        """
        if self._adj_model is not None:
            return self._adj_model
        import collections

        pair: dict = {}
        char_adj: dict = {}
        floats: dict = {}
        defaults: dict = {}
        pair_counts: dict = {}
        char_counts: dict = {}
        for donor in self.glyph_donor_paths:
            try:
                other = Statement(donor, glyph_donor_paths=[])
            except Exception:  # noqa: BLE001 – a stray file must not break us
                continue
            for run in other.runs:
                key_fs = (run.font, round(run.size, 3))
                glyphs = run.glyphs
                for idx, glyph in enumerate(glyphs):
                    adj = glyph["adj"]
                    nums = [t for t in adj if t != "EMPTY"]
                    if len(nums) >= 2:
                        floats.setdefault(key_fs, nums[-1])
                    if not nums:
                        continue
                    int_tok = nums[0] if len(nums) >= 2 else nums[-1]
                    ch = run.text[idx] if idx < len(run.text) else "?"
                    nxt = run.text[idx + 1] if idx + 1 < len(run.text) else ""
                    pair_counts.setdefault((run.font, round(run.size, 3), ch, nxt), collections.Counter())[int_tok] += 1
                    char_counts.setdefault((run.font, round(run.size, 3), ch), collections.Counter())[int_tok] += 1
        for key, counter in pair_counts.items():
            pair[key] = counter.most_common(1)[0][0]
        for key, counter in char_counts.items():
            char_adj[key] = counter.most_common(1)[0][0]
        # modal integer per font/size across characters
        by_fs: dict = {}
        for (font, size, _ch), tok in char_adj.items():
            by_fs.setdefault((font, round(size, 3)), []).append(tok)
        for key, toks in by_fs.items():
            defaults[key] = collections.Counter(toks).most_common(1)[0][0] if toks else "-22"
        self._adj_model = (pair, char_adj, defaults, floats)
        return self._adj_model

    # ---------------------------------------------------------- font pool

    def font_pool(self):
        """{tt family: {char: (TTFont, gid)}} harvested from statement donors."""
        if self._font_pool is not None:
            return self._font_pool
        pool: Dict[str, Dict[str, tuple]] = {}
        for donor in self.glyph_donor_paths:
            try:
                other = Statement(donor, glyph_donor_paths=[])
            except Exception:  # noqa: BLE001
                continue
            for slot in other.fonts.values():
                tt = other.ttfont_of(slot)
                family = tt["name"].getDebugName(4) or slot.basefont
                cmap = tt.getBestCmap()
                entry = pool.setdefault(family, {})
                for code, gname in cmap.items():
                    ch = chr(code)
                    if ch not in entry:
                        entry[ch] = (tt, tt.getGlyphID(gname), other, slot)
        self._font_pool = pool
        return pool

    # ---------------------------------------------------------- font rebuild

    def _rebuild_font(self, slot: FontSlot, used_entries: List[Tuple[int, str]]) -> None:
        """Re-subset one TTF to exactly ``used_entries`` [(cid, char)].

        Glyphs are only ever copied from genuine statement donors of
        the same family (component glyphs of composites included);
        with the donor's own character set and its own head.modified
        this reproduces the donor font byte for byte.
        """
        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables._g_l_y_f import Glyph

        used_chars = [ch for _cid, ch in used_entries]
        tt = self.ttfont_of(slot)
        family = tt["name"].getDebugName(4) or slot.basefont
        pool = self.font_pool().get(family, {})
        order = tt.getGlyphOrder()
        cmap_now = tt.getBestCmap()

        # source (TTFont, glyph name) for every glyph we must keep
        source_for_gid: Dict[int, tuple] = {}
        queue: List[Tuple[int, object, str]] = []
        for ch in used_chars:
            if ord(ch) in cmap_now:
                gname = cmap_now[ord(ch)]
                gid = tt.getGlyphID(gname)
                queue.append((gid, tt, gname))
            elif ch in pool:
                src_tt, gid, _donor, _slot = pool[ch]
                queue.append((gid, src_tt, src_tt.getGlyphOrder()[gid]))
            else:
                raise StatementError(
                    f"no donor glyph for {ch!r} in {family}; "
                    "add a genuine statement containing it to donors_statement/"
                )
        keep: Dict[int, tuple] = {}
        while queue:
            gid, src_tt, src_gname = queue.pop()
            if gid in keep:
                continue
            keep[gid] = (src_tt, src_gname)
            glyph = src_tt["glyf"][src_gname]
            if glyph.isComposite():
                for comp in glyph.components:
                    comp_gid = src_tt.getGlyphID(comp.glyphName)
                    queue.append((comp_gid, src_tt, src_tt.getGlyphOrder()[comp_gid]))

        for gid, gname in enumerate(order):
            if gid == 0:
                continue
            if gid in keep:
                src_tt, src_gname = keep[gid]
                if src_tt is not tt:
                    glyph = copy.deepcopy(src_tt["glyf"][src_gname])
                    if glyph.isComposite():
                        for comp in glyph.components:
                            comp_gid = src_tt.getGlyphID(comp.glyphName)
                            comp.glyphName = order[comp_gid]
                    tt["glyf"][gname] = glyph
                    tt["hmtx"][gname] = src_tt["hmtx"][src_gname]
            else:
                empty = Glyph()
                empty.numberOfContours = 0
                tt["glyf"][gname] = empty
                if gname in tt["hmtx"].metrics:
                    tt["hmtx"][gname] = (0, 0)

        new_cmap = {}
        for ch in used_chars:
            if ord(ch) in cmap_now:
                gid = tt.getGlyphID(cmap_now[ord(ch)])
            else:
                gid = pool[ch][1]
            new_cmap[ord(ch)] = order[gid]
        for table in tt["cmap"].tables:
            table.cmap = dict(new_cmap)

        # GDEF class definitions cover exactly the cmap glyphs (class 1)
        try:
            class_defs = tt["GDEF"].table.GlyphClassDef.classDefs
            keep_names = set(new_cmap.values())
            for name in list(class_defs):
                if name not in keep_names:
                    del class_defs[name]
            for name in keep_names:
                class_defs[name] = 1
        except Exception:  # noqa: BLE001 – GDEF shape differs; leave it
            pass

        if self.generation_dt is not None:
            tt["head"].modified = int(self.generation_dt.timestamp()) + MAC_EPOCH_DELTA

        buf = BytesIO()
        tt.save(buf)
        font_bytes = buf.getvalue()
        self.pdf.set_stream_decoded(slot.fontfile_num, font_bytes)

        slot.cid_to_char = {gid: ch for gid, ch in used_entries}
        # widths from the rebuilt font
        new_tt = TTFont(BytesIO(font_bytes), recalcTimestamp=False)
        upem = new_tt["head"].unitsPerEm
        new_order = new_tt.getGlyphOrder()
        slot.widths = {
            gid: int(new_tt["hmtx"][new_order[gid]][0] * 1000 / upem + 0.5)
            for gid, _ch in used_entries
        }
        cid_body = replace_widths(self.pdf.comp_body(slot.cidfont_num), slot.widths)
        self.pdf.set_comp_body(slot.cidfont_num, cid_body)
        self.pdf.set_stream_decoded(slot.tounicode_num, build_tounicode(used_entries))

    # ---------------------------------------------------------- encoding

    def _encode_run(self, run: TextRun, slot: FontSlot) -> str:
        pair, char_adj, defaults, floats = self.adjustment_model()
        char_to_cid = slot.char_to_cid
        text = run.desired_text
        missing = [ch for ch in text if ch not in char_to_cid]
        if missing:
            raise StatementError(f"font {slot.basefont} cannot render {missing!r}")
        key_fs = (run.font, round(run.size, 3))
        float_tok = floats.get(key_fs)
        parts: List[str] = []
        same_len = len(text) == len(run.text)
        for i, ch in enumerate(text):
            cid = char_to_cid[ch]
            parts.append(f"<{cid:04x}>")
            if i == len(text) - 1:
                break
            if same_len and i < len(run.glyphs):
                tokens = run.glyphs[i]["adj"]
                for tok in tokens:
                    parts.append("<>" if tok == "EMPTY" else tok)
            else:
                nxt = text[i + 1] if i + 1 < len(text) else ""
                int_tok = (
                    pair.get((run.font, round(run.size, 3), ch, nxt))
                    or char_adj.get((run.font, round(run.size, 3), ch))
                    or defaults.get(key_fs, "-22")
                )
                parts.append(str(int_tok))
                parts.append("<>")
                if float_tok is not None:
                    parts.append(str(float_tok))
        return "".join(parts)

    def _run_width(self, run: TextRun, slot: FontSlot, text: str, adj_of) -> float:
        total = 0.0
        for i, ch in enumerate(text):
            cid = slot.char_to_cid.get(ch)
            if cid is None:
                continue
            width = slot.widths.get(cid, 600) / 1000.0 * run.size
            if i < len(text) - 1:
                adj = adj_of(i, ch, text)
                width -= sum(adj) / 1000.0 * run.size
            total += width
        return total

    # ---------------------------------------------------------- save

    def save(self, path) -> None:
        changed = any(r.desired_text != r.text or r.desired_x is not None or r.desired_y is not None for r in self.runs)
        if not changed and self.generation_dt is None:
            self.pdf.save(path)
            return

        # 1. fonts: exactly the characters the new document uses,
        #    in first-appearance order (pages in order, runs in stream order)
        used_order: Dict[str, List[Tuple[int, str]]] = {res: [] for res in self.fonts}
        seen: Dict[str, set] = {res: set() for res in self.fonts}
        for page_idx in range(len(self.pages)):
            for run in self.runs_on_page(page_idx):
                slot = self.fonts[run.font]
                cmap = slot.char_to_cid
                for ch in run.desired_text:
                    if ch in seen[run.font]:
                        continue
                    seen[run.font].add(ch)
                    if ch in cmap:
                        used_order[run.font].append((cmap[ch], ch))
                    else:
                        # gid will be resolved from the pool at font rebuild;
                        # record with the pool gid
                        tt = self.ttfont_of(slot)
                        family = tt["name"].getDebugName(4) or slot.basefont
                        pool = self.font_pool().get(family, {})
                        if ch not in pool:
                            raise StatementError(
                                f"no donor glyph for {ch!r} in {family}"
                            )
                        used_order[run.font].append((pool[ch][1], ch))
        for res, slot in self.fonts.items():
            self._rebuild_font(slot, used_order[res])

        # 2. content streams: re-encode changed runs (offsets shift from
        #    the end of each stream backwards)
        pair, char_adj, defaults, floats = self.adjustment_model()

        def adj_numbers(run, glyphs):
            def of(i, ch, text):
                if i < len(glyphs):
                    return [float(t) for t in glyphs[i]["adj"] if t != "EMPTY"]
                return [0.0]
            return of

        def new_adj_of(run):
            def new_adj(i, ch, text):
                if len(text) == len(run.text) and i < len(run.glyphs):
                    return [float(t) for t in run.glyphs[i]["adj"] if t != "EMPTY"]
                key = (run.font, round(run.size, 3))
                int_tok = (
                    pair.get((run.font, round(run.size, 3), ch, text[i + 1] if i + 1 < len(text) else ""))
                    or char_adj.get((run.font, round(run.size, 3), ch))
                    or defaults.get(key, "-22")
                )
                nums = [float(int_tok)]
                ft = floats.get(key)
                if ft is not None:
                    nums.append(float(ft))
                return nums
            return new_adj

        # right-aligned values keep their right edge; a paired label
        # (day balance / totals) shifts by the same delta, as in donors
        for run in self.runs:
            if run.desired_text != run.text and run.right_align:
                slot = self.fonts[run.font]
                orig_width = self._run_width(run, slot, run.text, adj_numbers(run, run.glyphs))
                new_width = self._run_width(run, slot, run.desired_text, new_adj_of(run))
                run.desired_x = (run.x + orig_width) - new_width
                if run.label_run is not None:
                    run.label_run.desired_x = run.label_run.x + (run.desired_x - run.x)

        for page_idx in range(len(self.pages)):
            content = self.contents[page_idx]
            edits: List[Tuple[int, int, str]] = []
            for run in self.runs_on_page(page_idx):
                slot = self.fonts[run.font]
                if run.desired_text != run.text:
                    encoded = self._encode_run(run, slot)
                    edits.append((run.tj_span[0], run.tj_span[1], encoded))
                if run.desired_x is not None and run.desired_x != run.x:
                    edits.append((run.tm_x_span[0], run.tm_x_span[1], _num(run.desired_x)))
                if run.desired_y is not None and run.desired_y != run.y:
                    edits.append((run.tm_y_span[0], run.tm_y_span[1], _num(run.desired_y)))
            for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
                content = content[:start] + replacement + content[end:]
            if edits:
                self.pdf.set_stream_decoded(self.pages[page_idx]["content"], content.encode("latin-1"))

        self.pdf.save(path)
        if self.generation_dt is not None:
            import os
            import subprocess
            import shutil

            stamp = (self.generation_dt + timedelta(seconds=5)).timestamp()
            os.utime(path, (stamp, stamp))
            if shutil.which("SetFile"):
                local = (self.generation_dt + timedelta(seconds=5)).astimezone()
                text = local.strftime("%m/%d/%Y %H:%M:%S")
                subprocess.run(["SetFile", "-d", text, "-m", text, str(path)], check=False, capture_output=True)


# --------------------------------------------------------------------------
# parsed statement fields / operations
# --------------------------------------------------------------------------

MONTHS_GENITIVE = [
    "ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ",
    "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ",
]
_DAY_RE = re.compile(r"^(\d{2}) (" + "|".join(MONTHS_GENITIVE) + r") (\d{4})$")
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def parse_money(text: str) -> Decimal:
    body = str(text).strip()
    body = body.replace("\u00a0RUR", "").replace(" RUR", "").replace("RUR", "")
    body = body.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    return Decimal(body)


def format_money(value, *, signed: bool = False) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = ""
    if signed:
        sign = "+" if amount > 0 else ("-" if amount < 0 else "")
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    grouped = f"{whole:,}".replace(",", " ")
    return f"{sign}{grouped},{cents:02d}\u00a0RUR"


def _parse_date(text: str) -> date:
    m = _DATE_RE.match(text.strip())
    if not m:
        raise StatementError(f"bad date {text!r}")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def format_day(day: date) -> str:
    return f"{day.day:02d} {MONTHS_GENITIVE[day.month - 1]} {day.year}"


@dataclass
class ParsedOp:
    row: RowBox
    time_run: TextRun
    code_run: TextRun
    desc_runs: List[TextRun]
    amount_run: TextRun
    code: str
    amount: Decimal
    description: str

    @property
    def time_text(self) -> str:
        return self.time_run.text


@dataclass
class DayGroup:
    date_value: date
    date_run: TextRun
    balance_value_run: TextRun
    ops: List[ParsedOp] = dc_field(default_factory=list)


@dataclass
class ParsedStatement:
    period_runs: List[TextRun]
    issue_runs: List[TextRun]
    fio_run: TextRun
    account_run: TextRun
    header_values: Dict[str, Optional[TextRun]]  # opening/income/closing/expenses/last
    days: List[DayGroup]
    total_income_label: Optional[TextRun]
    total_income_value: Optional[TextRun]
    total_expense_label: Optional[TextRun]
    total_expense_value: Optional[TextRun]
    ops_page: Optional[int]


def _value_below(stmt: "Statement", label_text: str) -> Optional[TextRun]:
    for run in stmt.runs:
        if run.text.strip() != label_text:
            continue
        candidates = [
            r for r in stmt.runs
            if r.page == run.page and r.font != run.font and r.text.strip() != label_text
            and run.y < r.y < run.y + 32 and abs(r.x - run.x) < 90
        ]
        candidates.sort(key=lambda r: (r.y, abs(r.x - run.x)))
        if candidates:
            return candidates[0]
    return None


def parse_statement(stmt: "Statement") -> ParsedStatement:
    period_runs = [r for r in stmt.runs if re.fullmatch(r"С \d{2}\.\d{2}\.\d{4} ПО \d{2}\.\d{2}\.\d{4}", r.text)]
    issue_runs = [
        r for r in stmt.runs
        if _DATE_RE.fullmatch(r.text) and r.y < 100 and r.x > 650
    ]
    fio_run = next((r for r in stmt.runs if r.page == 0 and 140 < r.y < 170 and r.font == "DUFPOI"), None)
    account_run = next((r for r in stmt.runs if r.text.startswith("ДЕБЕТОВАЯ КАРТА РОКЕТ")), None)
    if fio_run is None or account_run is None or not period_runs:
        raise StatementError("statement header runs not found")

    header_values = {
        "opening": _value_below(stmt, "ВХОДЯЩИЙ ОСТАТОК"),
        "income": _value_below(stmt, "ПОСТУПЛЕНИЯ"),
        "closing": _value_below(stmt, "ИСХОДЯЩИЙ ОСТАТОК"),
        "expenses": _value_below(stmt, "РАСХОДЫ"),
        "last": _value_below(stmt, "ДАТА ПОСЛЕДНЕГО ДВИЖЕНИЯ"),
    }

    days: List[DayGroup] = []
    day_by_key: Dict[Tuple[int, float], DayGroup] = {}
    ops_page = None
    for row in sorted(stmt.rows, key=lambda r: (r.page, r.y)):
        row_runs = [
            r for r in stmt.runs
            if r.page == row.page and row.y < r.y < row.y + row.h + 2
        ]
        time_run = next((r for r in row_runs if re.fullmatch(r"\d{2}:\d{2}", r.text) and r.x < 90), None)
        code_run = next((r for r in row_runs if re.fullmatch(r"\d{11}", r.text) and 90 < r.x < 180), None)
        if time_run is None or code_run is None:
            continue
        desc_runs = sorted([r for r in row_runs if 180 < r.x < 660 and r is not code_run], key=lambda r: r.y)
        amount_run = next((r for r in row_runs if r.x >= 660 and r.text.strip().endswith("RUR")), None)
        if amount_run is None or not desc_runs:
            continue
        # day header: closest day-date run above this row on the same page
        day_run = None
        for r in stmt.runs:
            if r.page == row.page and _DAY_RE.fullmatch(r.text) and r.y < row.y:
                if day_run is None or r.y > day_run.y:
                    day_run = r
        if day_run is None:
            continue
        key = (row.page, day_run.y)
        if key not in day_by_key:
            dm = _DAY_RE.fullmatch(day_run.text)
            day_value = date(int(dm.group(3)), MONTHS_GENITIVE.index(dm.group(2)) + 1, int(dm.group(1)))
            balance_run = next(
                (r for r in stmt.runs if r.page == row.page and abs(r.y - day_run.y) < 3 and r.x > 600 and r.font == "DUFPOI"),
                None,
            )
            group = DayGroup(date_value=day_value, date_run=day_run, balance_value_run=balance_run)
            day_by_key[key] = group
            days.append(group)
        day_by_key[key].ops.append(
            ParsedOp(
                row=row,
                time_run=time_run,
                code_run=code_run,
                desc_runs=desc_runs,
                amount_run=amount_run,
                code=code_run.text,
                amount=parse_money(amount_run.text),
                description=" ".join(r.text for r in desc_runs),
            )
        )
        ops_page = row.page

    def _total(label: str):
        lab = next((r for r in stmt.runs if r.text.strip() == label), None)
        if lab is None:
            return None, None
        val = next(
            (r for r in stmt.runs if r.page == lab.page and abs(r.y - lab.y) < 3 and r.x > lab.x and r.text.strip().endswith("RUR")),
            None,
        )
        return lab, val

    ti_lab, ti_val = _total("ВСЕГО ПОСТУПЛЕНИЙ:")
    te_lab, te_val = _total("ВСЕГО РАСХОДОВ:")
    return ParsedStatement(
        period_runs=period_runs,
        issue_runs=issue_runs,
        fio_run=fio_run,
        account_run=account_run,
        header_values=header_values,
        days=days,
        total_income_label=ti_lab,
        total_income_value=ti_val,
        total_expense_label=te_lab,
        total_expense_value=te_val,
        ops_page=ops_page,
    )


# --------------------------------------------------------------------------
# account number (key digit, CBR letter No. 515)
# --------------------------------------------------------------------------

#: Conditional bank number that reproduces the genuine donor key.
#: CBR No. 515: prefix = last 3 BIC digits, weights 7,1,3 repeating,
#: key = last digit of ((sum with key=0) mod 10) * 3.  Of Sovcombank's
#: three BICs (044525987 / 044525360 / 045004763) only prefix 763
#: reproduces the donor account's key digit 4 (see BANK.md).
ACCOUNT_BIC_PREFIX = "763"
_ACCOUNT_WEIGHTS = [7, 1, 3]


def account_key(prefix_account: str, bic_prefix: str = ACCOUNT_BIC_PREFIX) -> int:
    """Key digit (position 9) for a 20-digit account with key zeroed inside."""
    digits = list(prefix_account)
    if len(digits) != 20:
        raise StatementError(f"account must be 20 digits, got {prefix_account!r}")
    digits[8] = "0"
    seq = bic_prefix + "".join(digits)
    total = sum(
        (int(ch) * _ACCOUNT_WEIGHTS[i % 3]) % 10 for i, ch in enumerate(seq)
    )
    return ((total % 10) * 3) % 10


def make_account(account_or_tail: str, *, random_tail: bool = False) -> str:
    """Full 20-digit account; change the last 5 digits -> new key digit.

    Accepts a full account (its key is recomputed/checked), only the
    last 5 digits, or nothing with ``random_tail=True``.
    """
    text = re.sub(r"\D", "", str(account_or_tail or ""))
    if random_tail and not text:
        tail = "".join(secrets.choice("0123456789") for _ in range(5))
        base = "4081781045023" + "13" + tail
    elif len(text) == 5:
        tail = text
        base = "4081781045023" + "13" + tail
    elif len(text) == 20:
        base = text
    else:
        raise StatementError(
            f"account must be 20 digits or a 5-digit tail, got {account_or_tail!r}"
        )
    key = account_key(base)
    return base[:8] + str(key) + base[9:]


def check_account(account: str) -> bool:
    digits = re.sub(r"\D", "", str(account))
    return len(digits) == 20 and account_key(digits) == int(digits[8])


# --------------------------------------------------------------------------
# descriptions / wrapping
# --------------------------------------------------------------------------

_DESC_CHAR_W = 5.6945   # IBM advance in content units (600/1000*8.816 + TJ adj)
_DESC_MAX_W = 449.0     # measured: longest genuine line is 443.8, next word never fits


def wrap_description(text: str) -> List[str]:
    words = text.split(" ")
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if len(candidate) * _DESC_CHAR_W <= _DESC_MAX_W or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def description_sbp_out(dt: datetime, account: str, phone10: str, sbp_id: str) -> str:
    return (
        f"ПЕРЕВОД СОГЛАСНО РАСПОРЯЖЕНИЮ / СБП / {dt:%d.%m.%Y %H:%M} / "
        f"СЧЕТ ПЛАТЕЛЬЩИКА {account} ТЕЛ. {phone10}. БЕЗ НДС / {sbp_id} "
        f"СЧЕТ КОРРЕСПОНДЕНТА 30232 810 4 5014 0000088 "
        f"(ФИЛИАЛ \"КОРПОРАТИВНЫЙ\" ПАО \"СОВКОМБАНК\")."
    )


def description_sbp_in(dt: datetime, phone10: str, sbp_id: str) -> str:
    return (
        f"ЗАЧИСЛЕНИЕ ПЕРЕВОДА ДЕНЕЖНЫХ СРЕДСТВ/СБП/{dt:%Y-%m-%d %H:%M:%S}/"
        f"007{phone10}/ {sbp_id} "
        f"СЧЕТ КОРРЕСПОНДЕНТА 30233 810 3 5014 0133326 "
        f"(ФИЛИАЛ \"КОРПОРАТИВНЫЙ\" ПАО \"СОВКОМБАНК\")."
    )


def description_card(dt: datetime, account: str, sender_rocket: str, card6: str, card_last4: str) -> str:
    return (
        f"ВНЕШНИЙ ПЕРЕВОД СО СЧЕТА {account} {sender_rocket} НА "
        f"КАРТУ {card6}*{card_last4} {dt:%Y.%m.%d %H:%M} УСТРОЙСТВО 0017I001739"
    )


def description_topup(dt: datetime, auth_number: str, card_last4: str, amount_plain: str) -> str:
    return (
        f"ПОПОЛНЕНИЕ ДЕНЕЖНЫХ СРЕДСТВ АВТОРИЗАЦИЯ №{auth_number},"
        f"{dt:%d.%m.%Y} {dt:%H:%M},2200********{card_last4}, "
        f"{amount_plain}RUR,SAINTPETERSBU,MCC "
        f"6536,28294804\\RU\\SAINTPETERSBU\\VTB\\ 28294804\\"
    )


def _q_nodes(content: str) -> List[dict]:
    stack: List[dict] = []
    nodes: List[dict] = []
    for m in re.finditer(r"(?m)^(q|Q)[ \t]*$", content):
        if m.group(1) == "q":
            stack.append({"start": m.start(), "children": 0})
            if len(stack) > 1:
                stack[-2]["children"] += 1
        elif stack:
            node = stack.pop()
            node["end"] = m.end()
            nodes.append(node)
    return nodes


def _map_row_y(y: float, old_top: float, old_h: float, new_top: float, new_h: float) -> float:
    if y <= old_top + old_h / 2:
        return new_top + (y - old_top)
    return new_top + new_h + (y - (old_top + old_h))


def _transform_row_segment(seg: str, old_top: float, old_h: float, new_top: float, new_h: float) -> str:
    out_lines = []
    for line in seg.split("\n"):
        tokens = line.split()
        if not tokens:
            out_lines.append(line)
            continue
        op = tokens[-1]
        try:
            if op in ("m", "l") and len(tokens) == 3:
                tokens[1] = _num(_map_row_y(float(tokens[1]), old_top, old_h, new_top, new_h))
            elif op == "c" and len(tokens) == 7:
                for idx in (1, 3, 5):
                    tokens[idx] = _num(_map_row_y(float(tokens[idx]), old_top, old_h, new_top, new_h))
            elif op == "re" and len(tokens) == 5:
                tokens[1] = _num(_map_row_y(float(tokens[1]), old_top, old_h, new_top, new_h))
                if abs(float(tokens[3]) - old_h) < 0.01:
                    tokens[3] = _num(new_h)
        except ValueError:
            pass
        out_lines.append(" ".join(tokens))
    return "\n".join(out_lines)


def _shift_graphics_segment(seg: str, dy: float) -> str:
    out_lines = []
    for line in seg.split("\n"):
        tokens = line.split()
        if not tokens:
            out_lines.append(line)
            continue
        op = tokens[-1]
        try:
            if op in ("m", "l") and len(tokens) == 3:
                tokens[1] = _num(float(tokens[1]) + dy)
            elif op == "c" and len(tokens) == 7:
                for idx in (1, 3, 5):
                    tokens[idx] = _num(float(tokens[idx]) + dy)
            elif op == "re" and len(tokens) == 5:
                tokens[1] = _num(float(tokens[1]) + dy)
            elif op == "cm" and len(tokens) == 7 and tokens[:4] == ["1", "0", "0", "1"]:
                tokens[5] = _num(float(tokens[5]) + dy)
        except ValueError:
            pass
        out_lines.append(" ".join(tokens))
    return "\n".join(out_lines)


def relayout_statement_ops(stmt: "Statement", line_counts: List[int]) -> None:
    """Re-lay the single day group of ``stmt`` to ``line_counts`` rows.

    Row background segments are transformed (not redrawn), the ops
    BT-block is rebuilt from the template's own runs, totals/bank
    footer runs and the stamp image shift by the same delta.  After
    the call ``stmt`` is re-parsed, so its runs again match the
    content streams.
    """
    parsed = parse_statement(stmt)
    ops = [op for day in parsed.days for op in day.ops]
    if not ops:
        raise StatementError("relayout needs a template with operations")
    page = parsed.ops_page
    content = stmt.contents[page]
    line_step = 13.22532275
    desc_offset = ops[0].desc_runs[0].y - ops[0].row.y
    for op in ops:
        if len(op.desc_runs) > 1:
            line_step = op.desc_runs[1].y - op.desc_runs[0].y
            break
    heights = [(n + 2) * line_step for n in line_counts]
    first_top = ops[0].row.y
    new_tops: List[float] = []
    cursor = first_top
    for h in heights:
        new_tops.append(cursor)
        cursor += h
    new_bottom = cursor
    old_bottom = ops[-1].row.y + ops[-1].row.h
    dy = new_bottom - old_bottom

    # --- ops BT block -------------------------------------------------
    bt_starts = [m for m in re.finditer(r"(?m)^BT$", content)]
    bt_ends = [m for m in re.finditer(r"(?m)^ET$", content)]
    first_span = ops[0].time_run.tj_span
    bt_start = max(m.start() for m in bt_starts if m.start() < first_span[0])
    bt_end = min(m.end() for m in bt_ends if m.end() > first_span[1])
    block_runs = [
        r for r in stmt.runs
        if r.page == page and bt_start <= r.tj_span[0] < bt_end
    ]
    op_run_ids = set()
    for op in ops:
        op_run_ids.add(id(op.time_run))
        op_run_ids.add(id(op.code_run))
        op_run_ids.add(id(op.amount_run))
        for r in op.desc_runs:
            op_run_ids.add(id(r))
    last_op_idx = max(i for i, r in enumerate(block_runs) if id(r) in op_run_ids)
    tail_runs = block_runs[last_op_idx + 1 :]

    specs: List[Tuple[TextRun, float, float]] = []
    for i, n in enumerate(line_counts):
        source = next((op for op in ops if len(op.desc_runs) == n), ops[0])
        top = new_tops[i]
        first_y = top + desc_offset
        mid_y = first_y + line_step * (n - 1) / 2
        specs.append((source.time_run, source.time_run.x, mid_y))
        specs.append((source.code_run, source.code_run.x, mid_y))
        for j in range(n):
            tmpl = source.desc_runs[min(j, len(source.desc_runs) - 1)]
            specs.append((tmpl, tmpl.x, first_y + line_step * j))
        specs.append((source.amount_run, source.amount_run.x, mid_y))
    for r in tail_runs:
        specs.append((r, r.x, r.y + dy))

    if tail_runs and max(y for _t, _x, y in specs[-len(tail_runs) :]) > 990:
        raise StatementError("relayout would push totals/footer off the page")

    parts = ["BT"]
    last_font = None
    for tmpl, x, y in specs:
        parts.append(f"1 0 0 -1 {_num(x)} {_num(y)} Tm")
        if (tmpl.font, tmpl.size) != last_font:
            parts.append(f"/{tmpl.font} {_num(tmpl.size)} Tf")
            last_font = (tmpl.font, tmpl.size)
        parts.append(f"[{content[tmpl.tj_span[0] : tmpl.tj_span[1]]}] TJ")
    parts.append("ET")
    new_bt = "\n".join(parts)

    edits: List[Tuple[int, int, str]] = [(bt_start, bt_end, new_bt)]

    # --- row backgrounds ---------------------------------------------
    for idx, op in enumerate(ops):
        start, end = op.row.node_span
        if idx < len(line_counts):
            seg = content[start:end]
            edits.append((start, end, _transform_row_segment(seg, op.row.y, op.row.h, new_tops[idx], heights[idx])))
        else:
            edits.append((start, end, ""))
    if len(line_counts) > len(ops):
        insert_pos = ops[-1].row.node_span[1]
        inserted = []
        for idx in range(len(ops), len(line_counts)):
            source = next((op for op in ops if abs(op.row.h - heights[idx]) < 0.01), ops[-1])
            seg = content[source.row.node_span[0] : source.row.node_span[1]]
            inserted.append(_transform_row_segment(seg, source.row.y, source.row.h, new_tops[idx], heights[idx]))
        edits.append((insert_pos, insert_pos, "\n" + "\n".join(inserted)))

    # --- stamp image on the same page ---------------------------------
    if abs(dy) > 0.001:
        candidates = []
        for node in _q_nodes(content):
            seg = content[node["start"] : node["end"]]
            if " Do" in seg and "/i745" in seg and " re" in seg and "1 0 0 1" in seg and len(seg) < 600:
                candidates.append((len(seg), node, seg))
        if candidates:
            _ln, node, seg = min(candidates)
            edits.append((node["start"], node["end"], _shift_graphics_segment(seg, dy)))

    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        content = content[:start] + replacement + content[end:]
    stmt.contents[page] = content
    stmt.pdf.set_stream_decoded(stmt.pages[page]["content"], content.encode("latin-1"))
    stmt.runs = []
    stmt.rows = []
    stmt._parse_runs()
    stmt._parse_rows()


def rocket_person(value: str) -> str:
    """ФИО -> Rocket print form ``ИМЯ ОТЧЕСТВО Ф.`` for card descriptions."""
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{given.upper()} {patronymic.upper()} {surname[0].upper()}."
    if len(tokens) == 2:
        surname, given = tokens
        return f"{given.upper()} {surname[0].upper()}."
    # already in Rocket form?
    if tokens and len(tokens[-1].rstrip(".")) == 1:
        return " ".join(t.upper() for t in tokens)
    raise StatementError(f"cannot parse person name {value!r}")


def phone10_of(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        raise StatementError(f"phone must have 10 significant digits, got {value!r}")
    return digits


# --------------------------------------------------------------------------
# receipt import + random fill (based on genuine donor operations)
# --------------------------------------------------------------------------

def operation_from_receipt(path, *, card_middle_digits: Optional[str] = None) -> dict:
    """One statement operation taken from a genuine Rocket receipt PDF.

    SBP receipt -> outgoing SBP operation (code = document number
    without ``M``, SBP id byte-for-byte, amount negative); card
    receipt -> card operation.  A card receipt only shows the first
    and last four digits, so the two middle digits the statement
    prints (``220039*9515``) must be given or generated by the caller.
    """
    import rocket_receipt

    receipt = rocket_receipt.Receipt(str(path))
    f = receipt.fields
    dt = datetime.strptime(f["date"][:16], "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
    amount = Decimal(f["amount"].replace("₽", "").replace("\u00a0", "").replace(" ", "").replace(",", "."))
    code = f["doc_number"].lstrip("M")
    if receipt.format == rocket_receipt.FORMAT_CARD:
        mask = f["card"]
        groups = re.findall(r"\d+", mask)
        return {
            "kind": "card",
            "datetime": dt,
            "amount": amount,
            "code": code,
            "card_first4": groups[0],
            "card_last4": groups[-1],
            "card_middle_digits": card_middle_digits,
            "_receipt": str(path),
        }
    return {
        "kind": "sbp_out",
        "datetime": dt,
        "amount": amount,
        "code": code,
        "sbp_id": f["sbp_id"],
        "phone": f.get("phone_from", ""),
        "_receipt": str(path),
    }


def _classify_description(description: str) -> str:
    if description.startswith("ЗАЧИСЛЕНИЕ"):
        return "sbp_in"
    if description.startswith("ПЕРЕВОД СОГЛАСНО"):
        return "sbp_out"
    if description.startswith("ВНЕШНИЙ"):
        return "card"
    if description.startswith("ПОПОЛНЕНИЕ"):
        return "topup"
    return "other"


def _donor_operation_pool() -> dict:
    pool = {"amounts": {}, "sbp": [], "cards": [], "auths": [], "topup_cards": []}
    for path in sorted((_HERE / "donors_statement").glob("*.pdf")):
        try:
            stmt = Statement(str(path), glyph_donor_paths=[])
            parsed = parse_statement(stmt)
        except Exception:  # noqa: BLE001
            continue
        for day in parsed.days:
            for op in day.ops:
                kind = _classify_description(op.description)
                pool["amounts"].setdefault(kind, []).append(abs(op.amount))
                sbp = re.search(r"\b[AB][0-9A-Z]{31}\b", op.description)
                if sbp and kind in ("sbp_in", "sbp_out"):
                    hm = op.time_text.split(":")
                    moment = datetime(
                        day.date_value.year, day.date_value.month, day.date_value.day,
                        int(hm[0]), int(hm[1]), tzinfo=MSK,
                    )
                    pool["sbp"].append((moment, sbp.group(0)))
                card = re.search(r"КАРТУ (\d{6})\*(\d{4})", op.description)
                if card:
                    pool["cards"].append((card.group(1), card.group(2)))
                auth = re.search(r"АВТОРИЗАЦИЯ №(\d+)", op.description)
                if auth:
                    pool["auths"].append(auth.group(1))
                top_card = re.search(r"2200\*\*\*\*\*\*\*\*(\d{4})", op.description)
                if top_card:
                    pool["topup_cards"].append(top_card.group(1))
    return pool


def _as_date(value, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(MSK).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise StatementError(f"cannot parse {name} {value!r}")


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(MSK) if value.tzinfo else value.replace(tzinfo=MSK)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 12, 0, tzinfo=MSK)
    text = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=MSK)
        except ValueError:
            continue
    raise StatementError(f"cannot parse operation datetime {value!r}")


def _prepare_operation(spec: dict, ctx: dict, rng) -> dict:
    if not isinstance(spec, dict):
        raise StatementError(f"operation must be a dict, got {spec!r}")
    spec = dict(spec)
    if "receipt_path" in spec:
        spec = {**operation_from_receipt(spec["receipt_path"], card_middle_digits=spec.get("card_middle_digits")), **{k: v for k, v in spec.items() if k not in ("receipt_path",)}}
    kind = spec.get("kind", "sbp_out")
    if kind not in ("sbp_out", "sbp_in", "card", "topup"):
        raise StatementError(f"unknown operation kind {kind!r}")
    dt = _as_datetime(spec.get("datetime") or spec.get("date") or spec.get("dt"))
    amount = Decimal(str(spec.get("amount", "0")).replace("\u00a0", "").replace(" ", "").replace(",", ".").replace("₽", ""))
    if amount == 0:
        raise StatementError("operation amount is zero")
    if kind in ("sbp_out", "card"):
        amount = -abs(amount)
    else:
        amount = abs(amount)
    code = spec.get("code") or spec.get("doc_number")
    if code:
        code = str(code).lstrip("M")
        if not re.fullmatch(r"\d{11}", code):
            raise StatementError(f"operation code must be 11 digits, got {code!r}")
    op = {
        "kind": kind,
        "dt": dt,
        "amount": amount,
        "code": code,
        "sbp_id": spec.get("sbp_id"),
        "phone": phone10_of(spec.get("phone") or ctx["phone"]),
        "description": spec.get("description"),
        "_explicit": True,
    }
    if kind == "card":
        card_text = str(spec.get("card", ""))
        groups = re.findall(r"\d+", card_text)
        all_digits = "".join(groups)
        first6 = spec.get("card_first6")
        if first6:
            first6 = re.sub(r"\D", "", str(first6))
            last4 = spec.get("card_last4") or (groups[-1] if groups else None)
        elif "*" not in card_text and len(all_digits) == 16:
            first6, last4 = all_digits[:6], all_digits[-4:]
        elif len(groups) >= 2 and len(groups[0]) == 6:
            first6, last4 = groups[0], groups[-1]
        elif groups or spec.get("card_first4"):
            first4 = str(spec.get("card_first4") or groups[0][:4])
            middle = spec.get("card_middle_digits")
            if middle is None:
                middle = "".join(rng.choice("0123456789") for _ in range(2))
            first6 = first4 + str(middle)
            last4 = spec.get("card_last4") or groups[-1]
        else:
            first6 = "2200" + "".join(rng.choice("0123456789") for _ in range(2))
            last4 = spec.get("card_last4") or "".join(rng.choice("0123456789") for _ in range(4))
        if not (re.fullmatch(r"\d{6}", str(first6)) and re.fullmatch(r"\d{4}", str(last4))):
            raise StatementError(f"card needs first6 + last4, got {first6!r}/{last4!r}")
        op["card_first6"] = str(first6)
        op["card_last4"] = str(last4)
    if kind == "topup":
        op["auth_number"] = str(spec["auth_number"]) if spec.get("auth_number") else None
        op["card_last4"] = str(spec.get("card_last4") or rng.choice(["4432", "3832"]))
    return op


def _finalise_description(op: dict, ctx: dict) -> str:
    if op.get("description"):
        return op["description"]
    dt = op["dt"]
    if op["kind"] == "sbp_out":
        return description_sbp_out(dt, ctx["account"], op["phone"], op["sbp_id"])
    if op["kind"] == "sbp_in":
        return description_sbp_in(dt, op["phone"], op["sbp_id"])
    if op["kind"] == "card":
        return description_card(dt, ctx["account"], rocket_person(ctx["holder"]), op["card_first6"], op["card_last4"])
    if op["kind"] == "topup":
        amount = abs(op["amount"])
        plain = str(int(amount)) if amount == int(amount) else format(amount.normalize(), "f")
        return description_topup(dt, op["auth_number"], op["card_last4"], plain)
    raise StatementError(op["kind"])


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

_STATEMENT_TEMPLATES = {
    0: "statement_rocket_18.pdf",
    6: "statement_rocket_12.pdf",
    9: "statement_rocket_14.pdf",
    10: "statement_rocket_13.pdf",
}
_TEMPLATE_1_4 = "statement_rocket_15.pdf"
_TEMPLATE_5_6 = "statement_rocket_12.pdf"
_TEMPLATE_7_9 = "statement_rocket_14.pdf"


def generate_rocket_statement(
    period_start,
    period_end,
    issue_date,
    holder_name: str,
    account: Optional[str] = None,
    *,
    account_tail: Optional[str] = None,
    random_account_tail: bool = False,
    opening_balance=None,
    operations: Optional[list] = None,
    operation_count: Optional[int] = None,
    receipt_paths: Optional[list] = None,
    phone: Optional[str] = None,
    operation_date=None,
    donor_path: Optional[str] = None,
    glyph_donor_paths: Optional[list] = None,
    output_path: Optional[str] = None,
    generated_at=None,
    random_seed: Optional[int] = None,
    fill_random: bool = True,
) -> Path:
    """Generate a Rocket account statement PDF and return its path.

    ``period_start``/``period_end``/``issue_date`` are dates
    (``date``, ``datetime`` or ``"16.10.2026"``); ``holder_name`` is a
    full ФИО; ``account`` is a full 20-digit account (key digit is
    recomputed), a 5-digit tail, or empty with
    ``account_tail``/``random_account_tail=True`` — changing the last
    five digits recomputes the key digit (position 9) and every
    account occurrence in the document.

    ``operations`` is a list of dicts
    ``{"kind": "sbp_out"|"sbp_in"|"card"|"topup", "datetime": …,
    "amount": …, "code"/"sbp_id"/"card"/…: …}`` and/or
    ``{"receipt_path": "receipt.pdf"}``.  ``receipt_paths=[…]`` is the
    convenient form of the same thing: each genuine Rocket receipt
    becomes one operation of the statement (code = its document
    number without ``M``, SBP id byte-for-byte, see BANK.md).  If
    ``operation_count`` is larger than the operations/receipts given
    and ``fill_random`` is true (default), the remaining operations
    are generated from genuine donor operations of the same kinds
    (amounts, card masks, SBP serial eras and the document counter
    are all taken from the donor corpora, never invented from
    scratch).  All operations must lie on one calendar day inside
    the period — that is the day group the donor layout prints.
    """
    import random as _random

    rng = _random.Random(random_seed) if random_seed is not None else _random.Random(secrets.randbits(64))

    p_start = _as_date(period_start, name="period_start")
    p_end = _as_date(period_end, name="period_end")
    issue = _as_date(issue_date, name="issue_date")
    if p_end < p_start:
        raise StatementError("period_end is before period_start")
    holder_title = " ".join(str(holder_name or "").strip().split())
    if not holder_title:
        raise StatementError("holder_name is empty")
    if holder_title.isupper() or holder_title.islower():
        holder_title = " ".join(token.capitalize() for token in holder_title.split())
    holder = holder_title.upper()

    donor_account = "40817810450231364892"
    if account_tail is not None:
        tail = re.sub(r"\D", "", str(account_tail))
        if len(tail) != 5:
            raise StatementError("account_tail must be 5 digits")
        base_acc = re.sub(r"\D", "", str(account)) if account else donor_account
        if len(base_acc) != 20:
            raise StatementError("account must be 20 digits when account_tail is used")
        account_digits = make_account(base_acc[:15] + tail)
    elif random_account_tail:
        tail = "".join(rng.choice("0123456789") for _ in range(5))
        base_acc = re.sub(r"\D", "", str(account)) if account else donor_account
        if len(base_acc) != 20:
            base_acc = donor_account
        account_digits = make_account(base_acc[:15] + tail)
    elif account:
        text = re.sub(r"\D", "", str(account))
        if len(text) == 20:
            account_digits = make_account(text)
        elif len(text) == 5:
            account_digits = make_account(donor_account[:15] + text)
        else:
            raise StatementError(f"account must be 20 digits or a 5-digit tail, got {account!r}")
    else:
        account_digits = donor_account
    if not check_account(account_digits):
        raise StatementError(f"account key does not validate: {account_digits}")

    ctx = {
        "account": account_digits,
        "holder": holder,
        "phone": phone10_of(phone) if phone else "9632972008",
    }

    explicit: List[dict] = []
    for spec in operations or []:
        explicit.append(_prepare_operation(spec, ctx, rng))
    for path in receipt_paths or []:
        explicit.append(_prepare_operation(operation_from_receipt(path), ctx, rng))

    total_count = operation_count if operation_count is not None else len(explicit)
    if total_count < len(explicit):
        raise StatementError(
            f"operation_count={total_count} is smaller than the {len(explicit)} operations given"
        )
    if total_count > 10:
        raise StatementError("statement layout supports at most 10 operations (donor templates)")
    fill_count = total_count - len(explicit)
    if fill_count and not fill_random:
        raise StatementError("operation_count exceeds given operations and fill_random=False")

    # day for fill operations
    if operation_date is not None:
        fill_day = _as_date(operation_date, name="operation_date")
    elif explicit:
        fill_day = explicit[0]["dt"].date()
    else:
        span = (p_end - p_start).days
        fill_day = p_start + timedelta(days=rng.randrange(span + 1) if span else 0)

    pool = _donor_operation_pool()
    fills: List[dict] = []
    if fill_count:
        for i in range(fill_count):
            if not explicit and i == 0:
                kind = "sbp_in"
            else:
                kind = rng.choices(["sbp_out", "card", "sbp_in", "topup"], weights=[62, 24, 7, 7])[0]
            amounts = pool["amounts"].get(kind) or [Decimal("10")]
            amount = rng.choice(amounts)
            minute = rng.randrange(8 * 60, 21 * 60)
            dt = datetime(fill_day.year, fill_day.month, fill_day.day, minute // 60, minute % 60, rng.randrange(60), tzinfo=MSK)
            spec = {"kind": kind, "datetime": dt, "amount": amount, "_explicit": False}
            if kind == "card" and pool["cards"]:
                first6, _l4 = rng.choice(pool["cards"])
                spec["card_first6"] = first6
                spec["card_last4"] = "".join(rng.choice("0123456789") for _ in range(4))
            fills.append(_prepare_operation(spec, ctx, rng))
            fills[-1]["_explicit"] = False

    all_ops = sorted(explicit + fills, key=lambda o: o["dt"])
    for op in all_ops:
        if not (p_start <= op["dt"].date() <= p_end):
            raise StatementError(
                f"operation {op['dt']:%d.%m.%Y} is outside the period {p_start:%d.%m.%Y}-{p_end:%d.%m.%Y}"
            )
    days_used = {op["dt"].date() for op in all_ops}
    if len(days_used) > 1:
        raise StatementError(
            f"operations span several days {sorted(days_used)}; the donor layout prints one day group — "
            "give operations of one day (random fill uses operation_date / the receipt's day)"
        )

    opening = Decimal(str(opening_balance).replace(",", ".")) if opening_balance is not None else None

    # --- template choice ------------------------------------------------
    if donor_path:
        template = Path(donor_path)
    elif total_count == 0:
        template = _HERE / "donors_statement" / _STATEMENT_TEMPLATES[0]
    elif total_count <= 4:
        template = _HERE / "donors_statement" / _TEMPLATE_1_4
    elif total_count <= 6:
        template = _HERE / "donors_statement" / _TEMPLATE_5_6
    elif total_count <= 9:
        template = _HERE / "donors_statement" / _TEMPLATE_7_9
    else:
        template = _HERE / "donors_statement" / _STATEMENT_TEMPLATES[10]
    if not template.is_file():
        raise FileNotFoundError(f"statement donor not found: {template}")

    stmt_probe = Statement(str(template), glyph_donor_paths=glyph_donor_paths)
    template_opening = None
    probe_parsed = parse_statement(stmt_probe)
    if probe_parsed.header_values.get("opening") is not None:
        template_opening = parse_money(probe_parsed.header_values["opening"].text)
    if opening is None:
        opening = template_opening if template_opening is not None else Decimal("0")

    # --- balance repair / validation -------------------------------------
    for _attempt in range(12):
        running = opening
        bad_index = None
        for idx, op in enumerate(all_ops):
            running += op["amount"]
            if running < 0:
                bad_index = idx
                break
        if bad_index is None:
            break
        deficit = -running
        convertible = [o for o in all_ops[: bad_index + 1] if not o.get("_explicit")]
        if convertible:
            target = convertible[0]
            target["kind"] = "sbp_in"
            target["amount"] = abs(target["amount"]) + deficit + Decimal("100")
            target.pop("description", None)
        else:
            movable = [o for o in all_ops if not o.get("_explicit")]
            if not movable:
                raise StatementError(
                    f"operations overdraw the opening balance {opening} at {all_ops[bad_index]['dt']}"
                )
            target = movable[0]
            target["kind"] = "sbp_in"
            target["amount"] = deficit + Decimal("100")
            earlier = all_ops[bad_index]["dt"] - timedelta(minutes=5)
            if earlier.date() == all_ops[bad_index]["dt"].date():
                target["dt"] = earlier
            all_ops = sorted(all_ops, key=lambda o: o["dt"])
    else:
        raise StatementError("could not keep the statement balance non-negative")

    income_total = sum((op["amount"] for op in all_ops if op["amount"] > 0), Decimal("0"))
    expense_total = -sum((op["amount"] for op in all_ops if op["amount"] < 0), Decimal("0"))
    closing = opening + income_total - expense_total

    # --- linked codes / SBP ids -------------------------------------------
    try:
        import engine as _engine

        pool_paths = _engine._donor_candidates() + _engine._card_donor_candidates()
        doc_points = _engine._corpus_doc_points(pool_paths) + _engine._card_doc_points(pool_paths)
    except Exception:  # noqa: BLE001 – counter model is an enhancement
        _engine = None
        doc_points = []
    # codes are the bank's sequential document counter: they must
    # strictly grow with time, also between genuine receipt anchors
    _idx = 0
    _prev_code: Optional[int] = None
    while _idx < len(all_ops):
        if all_ops[_idx]["code"] is not None:
            _prev_code = int(all_ops[_idx]["code"])
            _idx += 1
            continue
        _seg_end = _idx
        while _seg_end < len(all_ops) and all_ops[_seg_end]["code"] is None:
            _seg_end += 1
        _next_code = int(all_ops[_seg_end]["code"]) if _seg_end < len(all_ops) else None
        _segment = all_ops[_idx:_seg_end]
        for _k, _op in enumerate(_segment):
            candidate = None
            if _engine is not None:
                try:
                    candidate = int(_engine.make_doc_number("", _op["dt"], doc_points)[1:])
                except Exception:  # noqa: BLE001
                    candidate = None
            if candidate is None:
                candidate = 73550000000 + rng.randrange(100000)
            if _prev_code is not None and candidate <= _prev_code:
                candidate = _prev_code + rng.randrange(800, 25000)
            if _next_code is not None and candidate >= _next_code:
                if _prev_code is not None and _next_code - _prev_code > len(_segment) + 1:
                    candidate = _prev_code + int((_next_code - _prev_code) * (_k + 1) / (len(_segment) + 1))
                elif _prev_code is not None:
                    candidate = _prev_code + 1 + _k
                else:
                    candidate = _next_code - (len(_segment) - _k) * rng.randrange(800, 25000)
            if _prev_code is not None and candidate <= _prev_code:
                candidate = _prev_code + 1
            _op["code"] = f"{candidate:011d}"
            _prev_code = candidate
        _idx = _seg_end
    for op in all_ops:
        if op["kind"] in ("sbp_in", "sbp_out") and not op.get("sbp_id"):
            if _engine is not None and pool["sbp"]:
                template_sbp = min(pool["sbp"], key=lambda item: abs((item[0] - op["dt"]).total_seconds()))[1]
                if op["dt"].second == 0 and op["dt"].microsecond == 0:
                    # printed fields carry only minutes; inherit the
                    # template's seconds, as the receipt engine does
                    try:
                        op["dt"] = op["dt"].replace(second=_engine._donor_sbp_seconds(template_sbp))
                    except Exception:  # noqa: BLE001
                        pass
                op["sbp_id"] = _engine.make_sbp_id(op["dt"], template_sbp)
            elif pool["sbp"]:
                op["sbp_id"] = pool["sbp"][0][1]
            else:
                raise StatementError("no donor SBP id to build a statement operation from")
        if op["kind"] == "topup" and not op.get("auth_number"):
            base_auth = int(rng.choice(pool["auths"])) if pool["auths"] else 12756317462
            op["auth_number"] = str(base_auth + rng.randrange(1000, 900000))
        op["description"] = _finalise_description(op, ctx)
        op["lines"] = wrap_description(op["description"])

    # --- layout ------------------------------------------------------------
    stmt = Statement(str(template), glyph_donor_paths=glyph_donor_paths)
    parsed = parse_statement(stmt)
    template_ops = [op for day in parsed.days for op in day.ops]
    if all_ops:
        desired_line_counts = [len(op["lines"]) for op in all_ops]
        template_line_counts = [len(op.desc_runs) for op in template_ops]
        if desired_line_counts != template_line_counts:
            relayout_statement_ops(stmt, desired_line_counts)
            parsed = parse_statement(stmt)
            template_ops = [op for day in parsed.days for op in day.ops]
        if len(template_ops) != len(all_ops):
            raise StatementError(
                f"template laid out {len(template_ops)} operations, need {len(all_ops)}"
            )

    # --- patch fields -------------------------------------------------------
    for run in parsed.period_runs:
        stmt.set_run_text(run, f"С {p_start:%d.%m.%Y} ПО {p_end:%d.%m.%Y}")
    for run in parsed.issue_runs:
        stmt.set_run_text(run, f"{issue:%d.%m.%Y}")
    stmt.set_run_text(parsed.fio_run, holder)
    stmt.set_run_text(parsed.account_run, f"ДЕБЕТОВАЯ КАРТА РОКЕТ {account_digits}\u00a0RUR")

    def _set_header(key: str, text: str) -> None:
        run = parsed.header_values.get(key)
        if run is not None:
            stmt.set_run_text(run, text)

    _set_header("opening", format_money(opening))
    _set_header("income", format_money(income_total))
    _set_header("closing", format_money(closing))
    _set_header("expenses", format_money(expense_total))
    if all_ops:
        _set_header("last", f"{max(op['dt'] for op in all_ops):%d.%m.%Y}")
        group = parsed.days[0]
        stmt.set_run_text(group.date_run, format_day(all_ops[0]["dt"].date()))
        if group.balance_value_run is not None:
            balance_label = next(
                (r for r in stmt.runs if r.text.strip() == "БАЛАНС НА НАЧАЛО ДНЯ:" and r.page == group.date_run.page and abs(r.y - group.date_run.y) < 3),
                None,
            )
            stmt.set_run_text(group.balance_value_run, format_money(opening), right_align=True, label_run=balance_label)
    if parsed.total_income_value is not None:
        stmt.set_run_text(parsed.total_income_value, format_money(income_total), right_align=True, label_run=parsed.total_income_label)
    if parsed.total_expense_value is not None:
        stmt.set_run_text(parsed.total_expense_value, format_money(expense_total), right_align=True, label_run=parsed.total_expense_label)

    for slot_op, op in zip(template_ops, all_ops):
        stmt.set_run_text(slot_op.time_run, f"{op['dt']:%H:%M}")
        stmt.set_run_text(slot_op.code_run, op["code"])
        if len(slot_op.desc_runs) != len(op["lines"]):
            raise StatementError("description line count does not match the laid-out rows")
        for run, line in zip(slot_op.desc_runs, op["lines"]):
            stmt.set_run_text(run, line)
        stmt.set_run_text(slot_op.amount_run, format_money(op["amount"], signed=True), right_align=True)

    # outline title carries the holder name too, in title case
    # (genuine donors: first outline is the holder, not "Выписка…")
    old_fio = parsed.fio_run.text
    if old_fio != holder:
        for num, body in list(stmt.pdf.comp.items()):
            m = re.search(rb"/Title\s*<([0-9a-fA-F]+)>", body)
            if not m:
                continue
            try:
                title = bytes.fromhex(m.group(1).decode()).decode("utf-16-be").lstrip("\ufeff")
            except Exception:  # noqa: BLE001
                continue
            if title.upper() == old_fio:
                new_hex = ("\ufeff" + holder_title).encode("utf-16-be").hex().encode()
                stmt.pdf.set_comp_body(num, body[: m.start(1)] + new_hex + body[m.end(1) :])

    if generated_at is None:
        gen_dt = datetime.now(MSK)
    else:
        gen_dt = _as_datetime(generated_at)
    stmt.generation_dt = gen_dt.astimezone(timezone.utc)

    if output_path is None:
        out = _HERE / "out" / f"rocket_statement_{p_start:%Y%m%d}_{p_end:%Y%m%d}_{total_count}ops.pdf"
    else:
        out = Path(output_path)
    if out.resolve() == template.resolve():
        raise StatementError("output_path must not overwrite the donor")
    out.parent.mkdir(parents=True, exist_ok=True)
    stmt.save(str(out))

    raw = out.read_bytes() if out.is_file() else b""
    if not raw.startswith(b"%PDF-") or raw.count(b"startxref") != 1 or raw.count(b"%%EOF") != 1:
        out.unlink(missing_ok=True)
        raise StatementError("generated statement failed the structural sanity check")
    return out



