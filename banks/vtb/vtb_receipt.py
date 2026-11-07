#!/usr/bin/env python3
"""Field-level model of a genuine VTB receipt (openhtmltopdf.com).

A VTB SBP or card receipt is a one-page PDFBox/openhtmltopdf export:
PDF-1.4 header with catalog Version 1.7, 18 objects, classic xref,
``stream\\r\\n`` wrappers, zlib level 6, one Type0 Identity-H subset
of SFProDisplay-Regular.  CID mapping is formulaic (ASCII ``ord-29``,
Cyrillic ``ord-500``, ``₽`` = 1088); ``*`` is CID 13 and is written
as ``<000D>`` because 0x0D cannot live inside a PDF literal.

A receipt is never redrawn: a genuine donor is patched field by field
and re-saved in the same one-pass PDFBox layout.  An unmodified
round-trip reproduces the donor byte for byte.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import re
import string
import struct
import zlib
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

_HERE = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
FLATE_LEVEL = 6
WALL = 257.25
LS_PT = 0.15
KERN_9 = "-16.66667"
KERN_105 = "-21.42857"
KERN_135 = "-11.11111"
KERN_STAMP = "-8.33333"
NB_HYPHEN = "\u2011"  # U+2011, the hyphen printed in phones and «Т‑Банк»
TITLE = "Чек-PDF"
OBJECT_ORDER = [1, 3, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
SFPRO_CREATED = 3578999058
SFPRO_MODIFIED = 3794077016

FORMAT_SBP = "sbp"
FORMAT_CARD = "card"


class ReceiptError(RuntimeError):
    pass


def cid_of(ch: str) -> int:
    o = ord(ch)
    if ch == "₽":
        return 1088
    if 0x0400 <= o <= 0x04FF:
        return o - 500
    if 0x20 <= o <= 0x7E:
        return o - 29
    if ch == NB_HYPHEN:
        # Measured on every SBP donor: U+2011 sits at CID 16 (0x0010).
        return 16
    raise ReceiptError(f"no CID for {ch!r}")


def encode_glyph(cid: int) -> bytes:
    h, l = cid >> 8, cid & 0xFF
    if h == 0x0D or l == 0x0D:
        return f"<{cid:04X}>".encode("ascii")

    def esc(b: int) -> bytes:
        if b in (0x28, 0x29, 0x5C):
            return b"\\" + bytes([b])
        return bytes([b])

    return b"(" + esc(h) + esc(l) + b")"


def build_tj(text: str, kern: str) -> bytes:
    cids = [cid_of(c) for c in text]
    parts: List[bytes] = []
    kb = kern.encode("ascii")
    for i, cid in enumerate(cids):
        g = encode_glyph(cid)
        if i < len(cids) - 1:
            parts.append(g + kb + b" ")
        else:
            parts.append(g)
    return b"[" + b"".join(parts) + b"]"


def format_float(value: float) -> str:
    """PDFBox NumberFormatUtil.formatFloatFast(float, 5)."""
    v = struct.unpack("f", struct.pack("f", float(value)))[0]
    integer = int(v)
    negative = v < 0
    if negative:
        integer = -integer
    power = 100_000
    fraction = int((abs(float(v)) - abs(int(v))) * power + 0.5)
    if fraction >= power:
        integer += 1
        fraction -= power
    out = ("-" if negative else "") + str(integer)
    if fraction > 0:
        out += "." + f"{fraction:05d}".rstrip("0")
    return out


def kern_of(size: float) -> str:
    if abs(size - 13.5) < 1e-6:
        return KERN_135
    if abs(size - 10.5) < 1e-6:
        return KERN_105
    if abs(size - 9.0) < 1e-6:
        return KERN_9
    return format_float(-LS_PT * 1000.0 / size)


def parse_objects(data: bytes) -> Dict[int, bytes]:
    order = [(int(m.group(1)), m.start()) for m in re.finditer(rb"(?m)^(\d+) 0 obj", data)]
    out: Dict[int, bytes] = {}
    xref = re.search(rb"\nxref\n", data)
    xref_at = xref.start() + 1 if xref else data.find(b"xref")
    for i, (n, s) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else xref_at
        out[n] = data[s:end]
    return out


def stream_of(body: bytes) -> bytes:
    m = re.search(rb"stream\r?\n", body)
    if not m:
        raise ReceiptError("object has no stream")
    start = m.end()
    end = body.find(b"endstream", start)
    raw = body[start:end]
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith((b"\n", b"\r")):
        raw = raw[:-1]
    if b"/FlateDecode" in body[: m.start()]:
        return zlib.decompress(raw)
    return raw


def parse_w(body: bytes) -> Dict[int, int]:
    m = re.search(rb"/W\s*\[(.*?)]\s*/CIDToGIDMap", body, re.DOTALL)
    if not m:
        return {}
    tokens = re.findall(rb"\[|\]|-?\d+", m.group(1))
    widths: Dict[int, int] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (b"[", b"]"):
            i += 1
            continue
        start = int(tok)
        if i + 1 < len(tokens) and tokens[i + 1] == b"[":
            i += 2
            cid = start
            while i < len(tokens) and tokens[i] != b"]":
                widths[cid] = int(tokens[i])
                cid += 1
                i += 1
            i += 1
            continue
        i += 1
    return widths


def parse_tounicode(data: bytes) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for a, b, c in re.findall(
        rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", data
    ):
        lo, hi, uni = int(a, 16), int(b, 16), int(c, 16)
        for i, cid in enumerate(range(lo, hi + 1)):
            mapping[cid] = chr(uni + i)
    return mapping


def decode_tj(array: str, cmap: Dict[int, str]) -> str:
    text: List[str] = []
    i = 0
    n = len(array)
    while i < n:
        ch = array[i]
        if ch == "<":
            j = array.index(">", i)
            cid = int(array[i + 1 : j], 16)
            text.append(cmap.get(cid, "\ufffd"))
            i = j + 1
        elif ch == "(":
            i += 1
            buf = bytearray()
            while i < n:
                if array[i] == "\\":
                    i += 1
                    if i < n:
                        buf.append(ord(array[i]))
                        i += 1
                    continue
                if array[i] == ")":
                    i += 1
                    break
                buf.append(ord(array[i]))
                i += 1
            if len(buf) >= 2:
                cid = (buf[0] << 8) | buf[1]
                text.append(cmap.get(cid, "\ufffd"))
        elif ch.isspace():
            i += 1
        else:
            m = re.match(r"-?[\d.]+", array[i:])
            i += len(m.group(0)) if m else 1
    return "".join(text)


def flate_obj(num: int, payload: bytes, extra: str = "") -> bytes:
    """PDFBox/openhtmltopdf wrapper: zlib-6, stream\\r\\n, \\r\\nendstream."""
    comp = zlib.compress(payload, FLATE_LEVEL)
    extra_s = extra + "\n" if extra else ""
    hdr = (
        f"{num} 0 obj\n"
        f"<<\n/Length {len(comp)}\n{extra_s}/Filter /FlateDecode\n>>\n"
        f"stream\r\n"
    ).encode("latin-1")
    return hdr + comp + b"\r\nendstream\nendobj\n"


def dict_obj(num: int, body: str) -> bytes:
    return f"{num} 0 obj\n{body}endobj\n".encode("latin-1")


def wrap_pdf(objs: Dict[int, bytes], content: bytes, info: bytes, doc_id: str) -> bytes:
    parts: Dict[int, bytes] = {}
    for n in range(1, 19):
        if n not in objs:
            raise ReceiptError(f"donor is missing object {n}")
        parts[n] = objs[n] if objs[n].endswith(b"\n") else objs[n] + b"\n"
    parts[3] = info if info.endswith(b"\n") else info + b"\n"
    parts[5] = flate_obj(5, content)
    header = b"%PDF-1.4\n%\xf6\xe4\xfc\xdf\n"
    body = bytearray(header)
    offsets = {0: 0}
    for n in OBJECT_ORDER:
        offsets[n] = len(body)
        body.extend(parts[n])
        if not parts[n].endswith(b"\n"):
            body.extend(b"\n")
    xref_pos = len(body)
    xref = [b"xref\n0 19\n", b"0000000000 65535 f\r\n"]
    for n in range(1, 19):
        xref.append(f"{offsets[n]:010d} 00000 n\r\n".encode("ascii"))
    trailer = (
        f"trailer\n<<\n/Root 1 0 R\n/Info 3 0 R\n"
        f"/ID {doc_id}\n/Size 19\n>>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(body) + b"".join(xref) + trailer


def make_id() -> str:
    h = hashlib.md5(os.urandom(16)).hexdigest().upper()
    return f"[<{h}> <{h}>]"


def format_creation(dt: datetime) -> str:
    local = dt.astimezone(MSK) if dt.tzinfo else dt.replace(tzinfo=MSK)
    return local.strftime("D:%Y%m%d%H%M%S+03'00'")


def patch_subset_dates(ttf: bytes) -> bytes:
    data = bytearray(ttf)
    ntables = struct.unpack_from(">H", data, 4)[0]
    head_off = None
    for i in range(ntables):
        rec = 12 + i * 16
        tag = bytes(data[rec : rec + 4])
        off = struct.unpack_from(">I", data, rec + 8)[0]
        if tag == b"head":
            head_off = off
            break
    if head_off is None:
        return ttf
    struct.pack_into(">Q", data, head_off + 20, SFPRO_CREATED)
    struct.pack_into(">Q", data, head_off + 28, SFPRO_MODIFIED)
    struct.pack_into(">I", data, head_off + 8, 0)
    padded = bytes(data) + b"\x00" * ((4 - len(data) % 4) % 4)
    total = 0
    for i in range(0, len(padded), 4):
        total = (total + struct.unpack_from(">I", padded, i)[0]) & 0xFFFFFFFF
    adj = (0xB1B0AFBA - total) & 0xFFFFFFFF
    struct.pack_into(">I", data, head_off + 8, adj)
    return bytes(data)


@dataclass
class GlyphInfo:
    cid: int
    char: str
    width: int
    glyph: object
    lsb: int
    adv: int


class FontCatalog:
    """SFPro glyphs harvested from genuine VTB openhtmltopdf receipts."""

    def __init__(self) -> None:
        self.glyphs: Dict[int, GlyphInfo] = {}
        self.widths: Dict[int, int] = {}
        self.head_src: Optional[TTFont] = None
        self.notdef = None

    def add_pdf(self, path) -> None:
        data = Path(path).read_bytes()
        if not data.startswith(b"%PDF"):
            return
        objs = parse_objects(data)
        if 11 not in objs or 12 not in objs or 16 not in objs or 17 not in objs:
            return
        widths = parse_w(objs[11])
        self.widths.update(widths)
        touni = parse_tounicode(stream_of(objs[12]))
        cmap_raw = stream_of(objs[16])
        sub = TTFont(BytesIO(stream_of(objs[17])))
        if self.head_src is None:
            self.head_src = sub
            self.notdef = sub["glyf"][".notdef"]
        n = len(cmap_raw) // 2
        for cid in range(n):
            sgid = struct.unpack(">H", cmap_raw[cid * 2 : cid * 2 + 2])[0]
            if not sgid or cid in self.glyphs:
                continue
            name = sub.getGlyphName(sgid)
            g = sub["glyf"][name]
            adv, lsb = sub["hmtx"][name]
            ch = touni.get(cid, "?")
            self.glyphs[cid] = GlyphInfo(cid, ch, widths.get(cid, adv), g, lsb, adv)

    def has_char(self, ch: str) -> bool:
        try:
            return cid_of(ch) in self.glyphs
        except ReceiptError:
            return False

    def width_of(self, ch: str) -> int:
        cid = cid_of(ch)
        if cid in self.widths:
            return self.widths[cid]
        if cid in self.glyphs:
            return self.glyphs[cid].width
        raise ReceiptError(f"no donor glyph for {ch!r}")

    def summary(self) -> str:
        chars = "".join(sorted({g.char for g in self.glyphs.values() if g.char != "?"}))
        return f"SFProDisplay-Regular: {len(self.glyphs)} glyphs -> {chars}"


_CATALOG: Optional[FontCatalog] = None


def default_glyph_paths() -> List[Path]:
    paths: List[Path] = []
    for folder in (_HERE / "donors_sbp", _HERE / "donors_card"):
        if folder.is_dir():
            paths.extend(sorted(folder.glob("*.pdf")))
    return paths


def load_catalog(paths: Optional[List[Path]] = None) -> FontCatalog:
    global _CATALOG
    if paths is None and _CATALOG is not None:
        return _CATALOG
    cat = FontCatalog()
    for path in paths or default_glyph_paths():
        try:
            cat.add_pdf(path)
        except Exception:  # noqa: BLE001
            continue
    if paths is None:
        _CATALOG = cat
    return cat


def build_tounicode(cids: List[int], catalog: FontCatalog) -> bytes:
    mapping = [(cid, catalog.glyphs[cid].char) for cid in cids]
    ranges: List[str] = []
    i = 0
    while i < len(mapping):
        cid0, ch0 = mapping[i]
        j = i
        while (
            j + 1 < len(mapping)
            and mapping[j + 1][0] == mapping[j][0] + 1
            and ord(mapping[j + 1][1]) == ord(mapping[j][1]) + 1
        ):
            j += 1
        ranges.append(f"<{cid0:04X}> <{mapping[j][0]:04X}> <{ord(ch0):04X}>")
        i = j + 1
    body = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "\n"
        "begincmap\n"
        "/CIDSystemInfo\n"
        "<< /Registry (Adobe)\n"
        "/Ordering (UCS)\n"
        "/Supplement 0\n"
        ">> def\n"
        "\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "\n"
        "1 begincodespacerange\n"
        "<0000> <FFFF>\n"
        "endcodespacerange\n"
        "\n"
        f"{len(ranges)} beginbfrange\n"
        + "\n".join(ranges)
        + "\nendbfrange\n"
        "\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end\n"
    )
    return body.encode("latin-1")


def build_w_array(cids: List[int], catalog: FontCatalog) -> str:
    groups: List[str] = []
    i = 0
    while i < len(cids):
        j = i
        while j + 1 < len(cids) and cids[j + 1] == cids[j] + 1:
            j += 1
        widths = " ".join(str(catalog.widths[c]) for c in cids[i : j + 1])
        groups.append(f"{cids[i]} [{widths}]")
        i = j + 1
    inner = "\n ".join(groups)
    return f"/W [{inner}\n]"


def build_subset_ttf(cids: List[int], catalog: FontCatalog) -> bytes:
    src = catalog.head_src
    if src is None:
        raise ReceiptError("font catalog has no donor TTF")
    buf = io.BytesIO()
    src.save(buf)
    font = TTFont(BytesIO(buf.getvalue()))
    names = [".notdef"] + [f"g{cid:04X}" for cid in cids]
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    nd_name = font.getGlyphOrder()[0]
    new_glyphs = {".notdef": glyf[nd_name]}
    new_metrics = {".notdef": hmtx[nd_name]}
    for cid, name in zip(cids, names[1:]):
        g = catalog.glyphs[cid]
        new_glyphs[name] = g.glyph
        new_metrics[name] = (g.adv, g.lsb)
    glyf.glyphs = new_glyphs
    hmtx.metrics = new_metrics
    font.setGlyphOrder(names)
    font["maxp"].numGlyphs = len(names)
    font["hhea"].numberOfHMetrics = len(names)
    font["head"].indexToLocFormat = 1
    for tag in list(font.keys()):
        if tag not in ("GlyphOrder", "gasp", "glyf", "head", "hhea", "hmtx", "loca", "maxp"):
            del font[tag]
    out = io.BytesIO()
    font.save(out)
    return patch_subset_dates(out.getvalue())


def build_cidtogidmap(cids: List[int], max_cid: int = 1088) -> bytes:
    table = bytearray((max_cid + 1) * 2)
    for gid, cid in enumerate(cids, start=1):
        struct.pack_into(">H", table, cid * 2, gid)
    return bytes(table)


def build_cidset(cids: List[int], max_cid: int = 1088) -> bytes:
    size = (max_cid + 8) // 8
    bitmap = bytearray(size)
    for cid in cids:
        bitmap[cid // 8] |= 1 << (7 - (cid % 8))
    return bytes(bitmap)


def visual_width(text: str, size: float, kern: float, widths: Dict[int, int]) -> float:
    n = len(text)
    units = sum(widths[cid_of(c)] for c in text) + kern * max(n - 1, 0)
    return units * size / 1000.0


@dataclass
class TextRun:
    font: str
    size: float
    x: float
    y: float
    text: str
    tm_span: Tuple[int, int]
    tj_span: Tuple[int, int]
    align: str = "left"  # left / right / center / stamp
    desired: Optional[str] = None

    def __post_init__(self):
        if self.desired is None:
            self.desired = self.text


class Receipt:
    """A genuine VTB receipt opened for field-level patching."""

    def __init__(self, path, catalog: Optional[FontCatalog] = None):
        self.path = str(path)
        self.data = Path(path).read_bytes()
        self.objs = parse_objects(self.data)
        self.catalog = catalog or load_catalog()
        self.cmap = parse_tounicode(stream_of(self.objs[12]))
        self.widths = parse_w(self.objs[11])
        self.content = stream_of(self.objs[5])
        self.content_text = self.content.decode("latin-1")
        page = self.objs[4].decode("latin-1", "replace")
        mb = re.search(r"/MediaBox \[([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)\]", page)
        self.page_h = float(mb.group(4)) if mb else 0.0
        tag_m = re.search(rb"/BaseFont /([A-Z]{6})\+", self.objs[9])
        self.subset_tag = tag_m.group(1).decode() if tag_m else "AAAAAA"
        self.runs: List[TextRun] = []
        self._parse_runs()
        self._classify_align()
        self.fields = self._map_fields()
        self._dirty = False
        self.rebuild_font = False
        self.creation_dt: Optional[datetime] = None
        self.doc_id: Optional[str] = None

    @property
    def format(self) -> str:
        title = next((r.text for r in self.runs if r.size > 10), "")
        if "СБП" in title:
            return FORMAT_SBP
        return FORMAT_CARD

    def _parse_runs(self) -> None:
        content = self.content_text
        for m in re.finditer(
            r"/F1 ([\d.]+) Tf\n1 0 0 1 ([\d.]+) ([\d.]+) Tm\n\[(.*?)\] TJ",
            content,
            re.S,
        ):
            size = float(m.group(1))
            x = float(m.group(2))
            y = float(m.group(3))
            text = decode_tj(m.group(4), self.cmap)
            tm_start = m.start(2) - len("1 0 0 1 ")
            # tm_span covers `1 0 0 1 X Y Tm\n[…] TJ`
            self.runs.append(
                TextRun(
                    font="F1",
                    size=size,
                    x=x,
                    y=y,
                    text=text,
                    tm_span=(m.start(0), m.end(0)),
                    tj_span=(m.start(4), m.end(4)),
                )
            )

    def _classify_align(self) -> None:
        for run in self.runs:
            if abs(run.x - 24.75) < 0.05:
                run.align = "left"
            elif abs(run.y - 39.82499) < 0.05 or abs(run.y - 30.82499) < 0.05:
                run.align = "stamp"
            elif run.size > 10 and run.y > 320:
                run.align = "center"
            else:
                run.align = "right"

    def _map_fields(self) -> Dict[str, object]:
        by_text = [(i, r) for i, r in enumerate(self.runs)]
        labels = {r.text.strip(): i for i, r in by_text}

        def after(label: str):
            idx = labels.get(label)
            if idx is None:
                return None
            for j in range(idx + 1, len(self.runs)):
                run = self.runs[j]
                if run.align == "left" and abs(run.x - 24.75) < 0.05:
                    # wrapped continuation of the same label
                    if run.text in (
                        "плательщика",
                        "получателя",
                        "СБП",
                        "Банк)",
                    ):
                        continue
                    # next left label
                    break
                if run.align in ("right", "center") or run.x > 80:
                    return run
            return None

        fields: Dict[str, object] = {}
        title_runs = [r for r in self.runs if r.size > 10]
        if title_runs:
            fields["title"] = title_runs[0]
            if len(title_runs) > 1:
                fields["headline"] = title_runs[1]
        fields["status"] = after("Статус")
        fields["date"] = after("Дата операции")
        fields["account"] = after("Счет списания")
        fields["sender"] = after("Имя плательщика") or after("Имя")
        fields["recipient"] = after("Получатель")
        fields["phone_to"] = after("Телефон получателя") or after("Телефон")
        bank = after("Банк получателя")
        fields["bank_to"] = bank
        # second line of a wrapped bank (Wildberries)
        if bank is not None:
            for run in self.runs:
                if run is bank:
                    continue
                if abs(run.y - (bank.y - 12.0)) < 1.0 and run.align == "right" and "Банк)" in run.text:
                    fields["bank_to_2"] = run
                    break
        msg = after("Сообщение")
        if msg is not None:
            fields["message"] = msg
            extra = []
            for run in self.runs:
                if run is msg:
                    continue
                if run.align != "right" or run.size != 9:
                    continue
                if not (msg.y - 40 < run.y < msg.y):
                    continue
                # wrapped message lines have no left-side label on the same row
                has_label = any(
                    other.align == "left"
                    and abs(other.x - 24.75) < 0.05
                    and abs(other.y - (run.y - 0.75)) < 1.5
                    for other in self.runs
                    if other is not run
                )
                if not has_label:
                    extra.append(run)
            extra.sort(key=lambda r: -r.y)
            if extra:
                fields["message_extra"] = extra
        sbp = after("ID операции в СБП") or after("ID операции в")
        if sbp is not None:
            fields["sbp_1"] = sbp
            for run in self.runs:
                if run is sbp:
                    continue
                if abs(run.y - (sbp.y - 12.0)) < 1.5 and run.align == "right" and re.fullmatch(r"[0-9A-Z]{6,8}", run.text):
                    fields["sbp_2"] = run
                    break
        fields["amount"] = after("Сумма операции")
        fields["payer_card"] = after("Номер карты")
        fields["card"] = after("Карта получателя")
        fields["fee"] = after("Комиссия")
        fields["total"] = after("Сумма с комиссией")
        # drop Nones
        return {k: v for k, v in fields.items() if v is not None}

    def field_text(self, name: str) -> str:
        val = self.fields.get(name)
        if val is None:
            raise ReceiptError(f"field {name!r} is not in this receipt")
        if isinstance(val, list):
            return "".join(r.text for r in val)
        if name == "sbp_id":
            return self.sbp_id
        return val.text

    @property
    def sbp_id(self) -> str:
        a = self.fields.get("sbp_1")
        b = self.fields.get("sbp_2")
        if a is None:
            return ""
        return a.text + (b.text if b is not None else "")

    @property
    def printed_fields(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key, val in self.fields.items():
            if key in ("sbp_1", "sbp_2"):
                continue
            if key == "message_extra":
                continue
            if key == "bank_to_2":
                continue
            if isinstance(val, TextRun):
                out[key] = val.text
        if "sbp_1" in self.fields:
            out["sbp_id"] = self.sbp_id
        if "message" in self.fields:
            extra = self.fields.get("message_extra") or []
            out["message"] = self.fields["message"].text + "".join(r.text for r in extra)
        if "bank_to" in self.fields and "bank_to_2" in self.fields:
            out["bank_to"] = self.fields["bank_to"].text + " " + self.fields["bank_to_2"].text
        return out

    def set_run(self, run: TextRun, text: str) -> None:
        if text == run.desired:
            return
        missing = [ch for ch in text if not self.catalog.has_char(ch)]
        if missing:
            uniq = []
            for ch in missing:
                if ch not in uniq:
                    uniq.append(ch)
            raise ReceiptError(
                f"no donor glyph for {uniq!r} in SFProDisplay-Regular; "
                "add a genuine VTB receipt containing it to donors_sbp/ or donors_card/"
            )
        run.desired = text
        self._dirty = True
        donor_set = set(self.cmap.values())
        if any(ch not in donor_set for ch in text):
            self.rebuild_font = True

    def set(self, name: str, value: str) -> None:
        if name == "sbp_id":
            if len(value) != 32:
                raise ReceiptError(f"SBP id must be 32 chars, got {value!r}")
            first, second = value[:25], value[25:]
            self.set_run(self.fields["sbp_1"], first)
            if "sbp_2" in self.fields:
                self.set_run(self.fields["sbp_2"], second)
            return
        if name == "message":
            if "message" not in self.fields:
                raise ReceiptError("this donor has no message row")
            lines = value if isinstance(value, list) else [value]
            self.set_run(self.fields["message"], lines[0])
            extra = self.fields.get("message_extra") or []
            for run, line in zip(extra, lines[1:]):
                self.set_run(run, line)
            return
        if name == "bank_to" and "bank_to_2" in self.fields:
            # keep the donor wrap: first line + ' Банк)'
            if value.endswith(" Банк)") or "\n" in value:
                parts = value.replace("\n", " ").rsplit(" ", 1)
                self.set_run(self.fields["bank_to"], parts[0] if len(parts) == 2 else value)
                if len(parts) == 2:
                    self.set_run(self.fields["bank_to_2"], parts[1])
                return
            # single-line bank on a wrapping template: put the whole name
            # on line 1 and keep the donor's second line as a closing ')'.
            self.set_run(self.fields["bank_to"], value)
            return
        run = self.fields.get(name)
        if not isinstance(run, TextRun):
            raise ReceiptError(f"unknown field {name!r}; have {sorted(self.fields)}")
        self.set_run(run, value)

    def _new_x(self, run: TextRun, text: str) -> str:
        kern = float(kern_of(run.size))
        old_w = visual_width(run.text, run.size, kern, self.catalog.widths)
        new_w = visual_width(text, run.size, kern, self.catalog.widths)
        if run.align == "right":
            return format_float(run.x + old_w - new_w)
        if run.align == "center":
            return format_float(run.x + (old_w - new_w) / 2.0)
        if run.align == "stamp":
            return format_float(run.x + (old_w - new_w) / 2.0)
        return format_float(run.x)

    def _apply_runs(self) -> bytes:
        content = self.content_text
        edits: List[Tuple[int, int, str]] = []
        for run in self.runs:
            text = run.desired if run.desired is not None else run.text
            if text == run.text:
                continue
            x_s = self._new_x(run, text)
            y_s = format_float(run.y) if "." in f"{run.y}" else str(run.y)
            # keep the donor's original y token (float formatting of the
            # existing number) so unchanged neighbours stay put.
            y_token = content[run.tm_span[0] : run.tm_span[1]]
            ym = re.search(r"1 0 0 1 [\d.]+ ([\d.]+) Tm", y_token)
            y_keep = ym.group(1) if ym else format_float(run.y)
            block = (
                f"/F1 {run.size if run.size != int(run.size) else int(run.size) if abs(run.size - int(run.size)) < 1e-9 else run.size} Tf\n"
            )
            # Preserve the donor's exact Tf size token.
            sm = re.match(r"/F1 ([\d.]+) Tf\n", y_token)
            size_tok = sm.group(1) if sm else format_float(run.size)
            block = (
                f"/F1 {size_tok} Tf\n"
                f"1 0 0 1 {x_s} {y_keep} Tm\n"
            ).encode("ascii") + build_tj(text, kern_of(run.size)) + b" TJ"
            edits.append((run.tm_span[0], run.tm_span[1], block.decode("latin-1")))
        for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
            content = content[:start] + repl + content[end:]
        return content.encode("latin-1")

    def _used_cids(self) -> List[int]:
        seen = []
        have = set()
        for run in self.runs:
            text = run.desired if run.desired is not None else run.text
            for ch in text:
                cid = cid_of(ch)
                if cid not in have:
                    have.add(cid)
                    seen.append(cid)
        return sorted(seen)

    def _rebuild_font_objects(self, objs: Dict[int, bytes], prefix: str) -> None:
        catalog = self.catalog
        cids = self._used_cids()
        for cid in cids:
            if cid not in catalog.glyphs:
                raise ReceiptError(f"catalog is missing CID {cid}")
        font_name = f"{prefix}+SFProDisplay-Regular"
        touni = build_tounicode(cids, catalog)
        cidmap = build_cidtogidmap(cids)
        ttf = build_subset_ttf(cids, catalog)
        warr = build_w_array(cids, catalog)
        cidset = build_cidset(cids)
        objs[9] = dict_obj(
            9,
            "<<\n/Type /Font\n"
            f"/BaseFont /{font_name}\n"
            "/Subtype /Type0\n/Encoding /Identity-H\n"
            "/DescendantFonts [11 0 R]\n/ToUnicode 12 0 R\n>>\n",
        )
        objs[11] = dict_obj(
            11,
            "<<\n/Type /Font\n/Subtype /CIDFontType2\n"
            f"/BaseFont /{font_name}\n"
            "/CIDSystemInfo 14 0 R\n/FontDescriptor 15 0 R\n"
            f"{warr}\n/CIDToGIDMap 16 0 R\n>>\n",
        )
        objs[12] = flate_obj(12, touni)
        fd = self.objs[15]
        fd = re.sub(rb"/FontName /[A-Z]{6}\+", f"/FontName /{prefix}+".encode(), fd)
        objs[15] = fd if fd.endswith(b"\n") else fd + b"\n"
        objs[16] = flate_obj(16, cidmap)
        objs[17] = flate_obj(17, ttf, extra=f"/Length1 {len(ttf)}")
        objs[18] = flate_obj(18, cidset)

    def save(self, path) -> None:
        dest = Path(path)
        if not self._dirty and self.creation_dt is None and self.doc_id is None:
            dest.write_bytes(self.data)
            return
        content = self._apply_runs() if self._dirty else self.content
        objs = dict(self.objs)
        info = objs[3]
        if self.creation_dt is not None:
            created = format_creation(self.creation_dt)
            info = re.sub(
                rb"/CreationDate \(D:[^)]+\)",
                f"/CreationDate ({created})".encode("ascii"),
                info,
            )
        doc_id = self.doc_id or make_id()
        if self.rebuild_font:
            prefix = "".join(random.choice(string.ascii_uppercase) for _ in range(6))
            self._rebuild_font_objects(objs, prefix)
        raw = wrap_pdf(objs, content, info, doc_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        if self.creation_dt is not None:
            stamp = self.creation_dt.astimezone(MSK).timestamp()
            os.utime(dest, (stamp, stamp))
