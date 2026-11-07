#!/usr/bin/env python3
"""VTB «-transfer receipt (Перевод на карту).

Rebuilds the openhtmltopdf.com / SFProDisplay-Regular Identity-H shell
from the donated 12-10-26_00-00.pdf files. Variable fields are patched
in the content stream; the logo Form XObject and, when possible, the
original font subset stay byte-identical to the donor.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import random
import re
import string
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
DONORS = HERE / "donors"
OUT_DIR = HERE / "out"
DESKTOP = Path("/Users/aleksandrzerebatav/Desktop")
JAVA_ROOT = Path("/Users/aleksandrzerebatav/Desktop/Проекты/чекетоп/vtb-generator")
IDENTITY_TTF = HERE / "vtb_identity.ttf"
MASTER_TTF = Path("/Users/aleksandrzerebatav/Desktop/Проекты/чекетоп/SFProDisplay-Regular.ttf")
PRIMARY = DONORS / "full.pdf"

PAGE_W = 281.2125
PAGE_H = 450.75
WALL = 257.25
COMMISSION = 99
MSK = timezone(timedelta(hours=3))

# CSS letter-spacing → TJ kern (PDFBox Identity-H)
KERN_9 = "-16.66667"
KERN_105 = "-21.42857"
KERN_135 = "-11.11111"
KERN_STAMP = "-8.33333"
LS_PT = 0.15  # |letter-spacing| at 9pt and 13.5pt

GREEN = "0 0.50196 0.3098"       # Выполнено
ORANGE = "0.8902 0.58039 0"      # В обработке (no donor yet)
INK = "0.13333 0.13333 0.13333"
BLUE_LINE = "0.22745 0.51373 0.9451"
STAMP_BLUE = "0.49804 0.67059 0.92941"

TITLE = "Чек-PDF"


# --------------------------------------------------------------------------- CID


def cid_of(ch: str) -> int:
    o = ord(ch)
    if ch == "₽":
        return 1088
    if 0x0400 <= o <= 0x04FF:
        return o - 500
    if 0x20 <= o <= 0x7E:
        return o - 29
    raise ValueError(f"no CID for {ch!r}")


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
    """PDFBox NumberFormatUtil.formatFloatFast(float, 5) used in content streams.

    Java float32 + 5 fraction digits, trailing zeros dropped. This is why genuine
    files write 221.10001 (from 221.1f) and 183.60001 (from 183.6f), not 221.1.
    """
    v = struct.unpack("f", struct.pack("f", float(value)))[0]
    integer = int(v)  # Java f2l, toward 0
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


# Genuine 13.5pt amount Tm X, keyed by total SFPro font units of "… ₽".
# Same units always share one X (digit 6/8/9 are all 599: 600 ₽ == 800 ₽ == 900 ₽).
AMOUNT_X_13_5 = {
    2295: "225.507",  # 811 / 118 / 181 (7 units narrower than 110)
    2302: "225.41251",  # 110
    2405: "224.02501",  # 147
    2406: "223.9875",  # 170
    2425: "223.76251",  # 120 / 210
    2444: "223.5",  # 150
    2458: "223.3125",  # 109 / 160 / 180
    2464: "223.20001",  # 140
    2465: "223.20001",  # 100
    2529: "222.33751",  # 207 / 270
    2553: "222",  # 325
    2567: "221.8125",  # 250
    2574: "221.7375",  # 230
    2579: "221.66251",  # 565
    2581: "221.625",  # 280
    2588: "221.55",  # 200
    2593: "221.47501",  # 350
    2607: "221.28751",  # 500
    2613: "221.21251",  # 340
    2614: "221.175",  # 300
    2621: "221.10001",  # 600 / 800 / 900
    2627: "221.02501",  # 400
}


def text_units(text: str, widths: Dict[int, int]) -> int:
    return sum(widths[cid_of(c)] for c in text)


def text_width_pt(text: str, size: float, widths: Dict[int, int], tracking: float) -> float:
    n = len(text)
    return (text_units(text, widths) + tracking * (n - 1)) * size / 1000.0


def right_x(text: str, size: float, widths: Dict[int, int]) -> float:
    """CSS `right: 24pt` + letter-spacing applied n times (PDFBox/openhtmltopdf)."""
    gw = text_units(text, widths) * size / 1000.0
    return WALL - (gw + LS_PT * len(text))


def center_shift(old: str, new: str, size: float, widths: Dict[int, int], tracking: float) -> float:
    return (text_width_pt(old, size, widths, tracking) - text_width_pt(new, size, widths, tracking)) / 2.0


# --------------------------------------------------------------------------- PDF parse


def parse_objects(data: bytes) -> Dict[int, bytes]:
    order = [(int(m.group(1)), m.start()) for m in re.finditer(rb"(\d+) 0 obj", data)]
    out: Dict[int, bytes] = {}
    for i, (n, s) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else data.find(b"xref", s)
        out[n] = data[s:end]
    return out


def stream_of(body: bytes) -> bytes:
    m = re.search(rb"stream\r?\n", body)
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


# --------------------------------------------------------------------------- font catalog


@dataclass
class Glyph:
    cid: int
    char: str
    width: int
    glyph: object  # fontTools Glyph
    lsb: int
    adv: int


class FontCatalog:
    def __init__(self) -> None:
        self.glyphs: Dict[int, Glyph] = {}
        self.notdef = None
        self.head_src: Optional[TTFont] = None
        self.widths: Dict[int, int] = {}
        self.ttf: Optional[TTFont] = None
        self.ttf_cmap = None

    def add_pdf(self, path: Path) -> None:
        data = path.read_bytes()
        objs = parse_objects(data)
        widths = parse_w(objs[11])
        self.widths.update(widths)
        touni = parse_tounicode(stream_of(objs[12]))
        cmap_raw = stream_of(objs[16])
        sub = TTFont(io.BytesIO(stream_of(objs[17])))
        if self.head_src is None:
            self.head_src = sub
            self.notdef = sub["glyf"][".notdef"]
        n = len(cmap_raw) // 2
        import struct

        for cid in range(n):
            sgid = struct.unpack(">H", cmap_raw[cid * 2 : cid * 2 + 2])[0]
            if not sgid or cid in self.glyphs:
                continue
            name = sub.getGlyphName(sgid)
            g = sub["glyf"][name]
            adv, lsb = sub["hmtx"][name]
            ch = touni.get(cid, "?")
            self.glyphs[cid] = Glyph(cid, ch, widths.get(cid, adv), g, lsb, adv)

    def add_ttf(self, path: Path) -> None:
        if not path.exists():
            return
        self.ttf = TTFont(path)
        self.ttf_cmap = self.ttf.getBestCmap()

    def ensure(self, ch: str) -> Glyph:
        cid = cid_of(ch)
        if cid in self.glyphs:
            return self.glyphs[cid]
        if self.ttf is None or self.ttf_cmap is None or ord(ch) not in self.ttf_cmap:
            raise KeyError(f"glyph {ch!r} (CID {cid}) not in donors or TTF")
        name = self.ttf_cmap[ord(ch)]
        g = self.ttf["glyf"][name]
        adv, lsb = self.ttf["hmtx"][name]
        width = int(round(adv * 1000 / 2048))
        glyph = Glyph(cid, ch, width, g, lsb, adv)
        self.glyphs[cid] = glyph
        self.widths[cid] = width
        return glyph

    def width_of(self, ch: str) -> int:
        cid = cid_of(ch)
        if cid in self.widths:
            return self.widths[cid]
        return self.ensure(ch).width


def load_catalog() -> FontCatalog:
    cat = FontCatalog()
    for name in ("full.pdf", "short.pdf", "full_sovcom.pdf", "full_alfa.pdf"):
        cat.add_pdf(DONORS / name)
    cat.add_ttf(MASTER_TTF)
    return cat


# --------------------------------------------------------------------------- subset builder


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
    assert src is not None
    buf = io.BytesIO()
    src.save(buf)
    font = TTFont(io.BytesIO(buf.getvalue()))

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
    # Keep the PDFBox table set: gasp, glyf, head, hhea, hmtx, loca, maxp.
    for tag in list(font.keys()):
        if tag not in ("GlyphOrder", "gasp", "glyf", "head", "hhea", "hmtx", "loca", "maxp"):
            del font[tag]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def build_cidtogidmap(cids: List[int], max_cid: int = 1088) -> bytes:
    import struct

    table = bytearray((max_cid + 1) * 2)
    for gid, cid in enumerate(cids, start=1):
        struct.pack_into(">H", table, cid * 2, gid)
    return bytes(table)


# --------------------------------------------------------------------------- content


def bt(font_size: str, x: str, y: str, tj: bytes) -> bytes:
    return (
        b"BT\n/F1 "
        + font_size.encode("ascii")
        + b" Tf\n1 0 0 1 "
        + x.encode("ascii")
        + b" "
        + y.encode("ascii")
        + b" Tm\n"
        + tj
        + b" TJ\nET\n"
    )


def format_amount(n: int) -> str:
    s = f"{n:,}".replace(",", " ")
    return f"{s} ₽"


def format_op_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y, %H:%M")


def format_creation(dt: datetime) -> str:
    return dt.strftime("D:%Y%m%d%H%M%S+03'00'")


def pdf_filename(dt: datetime) -> str:
    return dt.strftime("%d-%m-%y_00-00.pdf")


@dataclass
class CardFields:
    status: str  # "done" | "processing"
    dt: datetime
    payer_card: str
    account: str
    payer_name: str
    payee_card: str
    bank: str
    amount: int
    commission: int = COMMISSION

    @property
    def status_text(self) -> str:
        return "Выполнено" if self.status == "done" else "В обработке"

    @property
    def stamp_text(self) -> str:
        return "Операция выполнена" if self.status == "done" else "Операция в обработке"

    @property
    def status_rgb(self) -> str:
        return GREEN if self.status == "done" else ORANGE

    @property
    def total(self) -> int:
        return self.amount + self.commission

    def used_chars(self) -> str:
        parts = [
            "Денежный перевод",
            "Перевод на карту",
            "Статус",
            self.status_text,
            "Дата операции",
            format_op_dt(self.dt),
            "Номер карты",
            self.payer_card,
            "Счет списания",
            self.account,
            "Имя плательщика",
            self.payer_name,
            "Карта получателя",
            self.payee_card,
            "Банк получателя",
            self.bank,
            "Комиссия",
            format_amount(self.commission),
            "Сумма с комиссией",
            format_amount(self.total),
            "Сумма операции",
            format_amount(self.amount),
            "Банк ВТБ (ПАО)",
            self.stamp_text,
        ]
        seen = []
        for p in parts:
            for ch in p:
                if ch not in seen:
                    seen.append(ch)
        return "".join(seen)


# Original Tm x for donor-1 strings — keeps a same-fields rebuild byte-faithful.
_ORIG_X = {
    ("Денежный перевод", "10.5"): "93.075",
    ("Перевод на карту", "10.5"): "97.5",
    ("Статус", "9"): "24.75",
    ("Выполнено", "9"): "210.97501",
    ("Дата операции", "9"): "24.75",
    ("12.10.2026, 19:41", "9"): "185.28751",
    ("Номер карты", "9"): "24.75",
    ("220024******2458", "9"): "175.9875",
    ("Счет списания", "9"): "24.75",
    ("*9426", "9"): "230.8125",
    ("Имя плательщика", "9"): "24.75",
    ("Александр Евгеньевич Ж.", "9"): "149.8125",
    ("Карта получателя", "9"): "24.75",
    ("2200 39** **** 9515", "9"): "171.1125",
    ("Банк получателя", "9"): "24.75",
    ("Росбанк", "9"): "222.675",
    ("Комиссия", "9"): "24.75",
    ("99 ₽", "9"): "238.53751",
    ("Сумма с комиссией", "9"): "24.75",
    ("224 ₽", "9"): "233.51251",
    ("Сумма операции", "13.5"): "24.75",
    ("125 ₽", "13.5"): "224.02501",
    ("Банк ВТБ (ПАО)", "9"): "108.15",
    ("Операция выполнена", "9"): "97.2",
}


def tm_x(text: str, size_s: str, size: float, widths: Dict[int, int], align: str) -> str:
    key = (text, size_s)
    if key in _ORIG_X:
        return _ORIG_X[key]
    if align == "right" and abs(size - 13.5) < 1e-6:
        units = text_units(text, widths)
        if units in AMOUNT_X_13_5:
            return AMOUNT_X_13_5[units]
    if align == "left":
        return "24.75"
    if align == "stamp":
        orig = "Операция выполнена"
        if text == orig:
            return "97.2"
        dx = center_shift(orig, text, 9.0, widths, -8.33333)
        return format_float(97.2 + dx)
    if align == "title":
        w = text_width_pt(text, size, widths, -21.42857)
        return format_float((PAGE_W - w) / 2.0 - 3.48)  # donor titles sit ~3.48pt left of true center
    x = right_x(text, size, widths)
    return format_float(x)


def build_content(fields: CardFields, widths: Dict[int, int], short: bool = False) -> bytes:
    date_s = format_op_dt(fields.dt)
    fee_s = format_amount(fields.commission)
    total_s = format_amount(fields.total)
    amt_s = format_amount(fields.amount)
    page_h = "432.75" if short else "450.75"
    sep_top = (
        b"24 368.25 m\n257.25 368.25 l\n257.25 367.5 l\n24 367.5 l\n"
        if short
        else b"24 386.25 m\n257.25 386.25 l\n257.25 385.5 l\n24 385.5 l\n"
    )
    logo_y = b"384.75" if short else b"402.75"

    chunks: List[bytes] = []
    chunks.append(
        b"0.0375 w\n2 J\n0 j\n10 M\n[] 0 d\nq\n"
        + f"0 {page_h} m\n281.21249 {page_h} l\n281.21249 0.03748 l\n0 0.03748 l\n0 {page_h} l\n".encode(
            "ascii"
        )
        + b"h\nW\nn\n"
        + BLUE_LINE.encode("ascii")
        + b" rg\n"
        + sep_top
        + b"h\nf\n"
        b"257.25 102 m\n24 102 l\n24 102.75 l\n257.25 102.75 l\n"
        b"h\nf\n"
        + INK.encode("ascii")
        + b" rg\n"
    )

    def add(size_s: str, size: float, text: str, y: str, kern: str, align: str) -> None:
        x = tm_x(text, size_s, size, widths, align)
        chunks.append(bt(size_s, x, y, build_tj(text, kern)))

    if not short:
        add("10.5", 10.5, "Денежный перевод", "358.61249", KERN_105, "title")
    add("10.5", 10.5, "Перевод на карту", "340.61249", KERN_105, "title")
    add("9", 9.0, "Статус", "312", KERN_9, "left")
    chunks.append(fields.status_rgb.encode("ascii") + b" rg\n")
    add("9", 9.0, fields.status_text, "312.75", KERN_9, "right")
    chunks.append(INK.encode("ascii") + b" rg\n")
    add("9", 9.0, "Дата операции", "288", KERN_9, "left")
    add("9", 9.0, date_s, "288.75", KERN_9, "right")
    add("9", 9.0, "Номер карты", "264", KERN_9, "left")
    add("9", 9.0, fields.payer_card, "264.75", KERN_9, "right")
    add("9", 9.0, "Счет списания", "240", KERN_9, "left")
    add("9", 9.0, fields.account, "240.75", KERN_9, "right")
    add("9", 9.0, "Имя плательщика", "216", KERN_9, "left")
    add("9", 9.0, fields.payer_name, "216.75", KERN_9, "right")
    add("9", 9.0, "Карта получателя", "192", KERN_9, "left")
    add("9", 9.0, fields.payee_card, "192.75", KERN_9, "right")
    add("9", 9.0, "Банк получателя", "168", KERN_9, "left")
    add("9", 9.0, fields.bank, "168.75", KERN_9, "right")
    add("9", 9.0, "Комиссия", "144", KERN_9, "left")
    add("9", 9.0, fee_s, "144.75", KERN_9, "right")
    add("9", 9.0, "Сумма с комиссией", "119.99999", KERN_9, "left")
    add("9", 9.0, total_s, "120.74999", KERN_9, "right")
    add("13.5", 13.5, "Сумма операции", "71.62499", KERN_135, "left")
    add("13.5", 13.5, amt_s, "72.37499", KERN_135, "right")

    chunks.append(
        b"q\n1 0 0 1 91.125 "
        + logo_y
        + b" cm\n/Form1 Do\nQ\n"
        b"q\n0.99939 0.0349 -0.0349 0.99939 1.43366 -4.88421 cm\n"
        + STAMP_BLUE.encode("ascii")
        + b" rg\n"
        b"52.125 53.25 m\n229.125 53.25 l\n227.625 51.75 l\n53.625 51.75 l\n"
        b"h\nf\n"
        b"229.125 24 m\n52.125 24 l\n53.625 25.5 l\n227.625 25.5 l\n"
        b"h\nf\n"
        b"52.125 24 m\n52.125 53.25 l\n53.625 51.75 l\n53.625 25.5 l\n"
        b"h\nf\n"
        b"229.125 53.25 m\n229.125 24 l\n227.625 25.5 l\n227.625 51.75 l\n"
        b"h\nf\n"
        b"52.125 53.25 m\n229.125 53.25 l\n227.625 51.75 l\n53.625 51.75 l\n"
        b"h\nf\n"
        b"229.125 24 m\n52.125 24 l\n53.625 25.5 l\n227.625 25.5 l\n"
        b"h\nf\n"
        b"52.125 24 m\n52.125 53.25 l\n53.625 51.75 l\n53.625 25.5 l\n"
        b"h\nf\n"
        b"229.125 53.25 m\n229.125 24 l\n227.625 25.5 l\n227.625 51.75 l\n"
        b"h\nf\n"
    )
    add("9", 9.0, "Банк ВТБ (ПАО)", "39.82499", KERN_STAMP, "stamp")
    add("9", 9.0, fields.stamp_text, "30.82499", KERN_STAMP, "stamp")
    chunks.append(b"Q\nQ\n")
    return b"".join(chunks)


# --------------------------------------------------------------------------- PDF assemble


def flate_obj(num: int, payload: bytes, extra: str = "") -> bytes:
    """PDFBox/openhtmltopdf wrapper: zlib-6, stream\\r\\n, \\r\\nendstream."""
    comp = zlib.compress(payload, 6)
    extra_s = extra + "\n" if extra else ""
    hdr = (
        f"{num} 0 obj\n"
        f"<<\n/Length {len(comp)}\n{extra_s}/Filter /FlateDecode\n>>\n"
        f"stream\r\n"
    ).encode("latin-1")
    return hdr + comp + b"\r\nendstream\nendobj\n"


def dict_obj(num: int, body: str) -> bytes:
    return f"{num} 0 obj\n{body}endobj\n".encode("latin-1")


def title_hex(text: str) -> str:
    return "FEFF" + text.encode("utf-16-be").hex().upper()


def make_id(seed: str) -> str:
    h = hashlib.md5(seed.encode("utf-8") + random.randbytes(8)).hexdigest().upper()
    return f"[<{h}> <{h}>]"


def utf16be_info_title() -> str:
    return f"<{title_hex(TITLE)}>"


def _tm_y_span(cs: bytes, y: bytes) -> tuple[int, int, int]:
    """Return (tm_start, tj_start, after_tj) for `1 0 0 1 X Y Tm\\n[…] TJ`."""
    needle = b" " + y + b" Tm\n"
    i = cs.find(needle)
    if i < 0:
        raise ValueError(f"Tm y={y!r} not found")
    start = cs.rfind(b"1 0 0 1 ", 0, i)
    arr_start = i + len(needle)
    if cs[arr_start] != ord("["):
        raise ValueError("expected TJ array")
    depth = 1
    j = arr_start + 1
    n = len(cs)
    while j < n and depth:
        if cs[j] == ord("("):
            j += 1
            while j < n:
                if cs[j] == 0x5C:
                    j += 2
                    continue
                if cs[j] == ord(")"):
                    j += 1
                    break
                j += 1
            continue
        if cs[j] == ord("<"):
            j = cs.find(b">", j) + 1
            continue
        if cs[j] == ord("["):
            depth += 1
        elif cs[j] == ord("]"):
            depth -= 1
        j += 1
    k = j
    while k < n and cs[k] in b" \t":
        k += 1
    if cs[k : k + 2] != b"TJ":
        raise ValueError("expected TJ")
    return start, arr_start, k + 2


def visual_width(text: str, size: float, kern: float, widths: Dict[int, int]) -> float:
    n = len(text)
    return (text_units(text, widths) + kern * (n - 1)) * size / 1000.0


def _patch_tm_y(
    cs: bytearray,
    y: str,
    old: str,
    new: str,
    size: float,
    kern: str,
    widths: Dict[int, int],
    x_s: Optional[str] = None,
) -> None:
    yb = y.encode("ascii")
    start, arr_s, end = _tm_y_span(bytes(cs), yb)
    head = bytes(cs)[start:arr_s]
    m = re.match(rb"1 0 0 1 ([\d.]+) ", head)
    old_x_s = m.group(1).decode("ascii")
    old_x = float(old_x_s)
    old_units = text_units(old, widths)
    new_units = text_units(new, widths)
    if x_s is None:
        if new_units == old_units:
            x_s = old_x_s
        elif abs(size - 13.5) < 1e-6 and new_units in AMOUNT_X_13_5:
            x_s = AMOUNT_X_13_5[new_units]
        else:
            x = old_x + visual_width(old, size, float(kern), widths) - visual_width(
                new, size, float(kern), widths
            )
            x_s = format_float(x)
    block = (
        b"1 0 0 1 "
        + x_s.encode("ascii")
        + b" "
        + yb
        + b" Tm\n"
        + build_tj(new, kern)
        + b" TJ"
    )
    cs[start:end] = block


def wrap_pdf(objs: Dict[int, bytes], content: bytes, info: bytes, doc_id: str) -> bytes:
    parts: Dict[int, bytes] = {}
    for n in range(1, 19):
        parts[n] = objs[n] if objs[n].endswith(b"\n") else objs[n] + b"\n"
    parts[3] = info if info.endswith(b"\n") else info + b"\n"
    parts[5] = flate_obj(5, content)
    order = [1, 3, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    header = b"%PDF-1.4\n%\xf6\xe4\xfc\xdf\n"
    body = bytearray(header)
    offsets = {0: 0}
    for n in order:
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


# PDFBox stamps today's date into subset head.modified; VTB keeps the source font's.
SFPRO_CREATED = 3578999058
SFPRO_MODIFIED = 3794077016


def patch_subset_dates(ttf: bytes) -> bytes:
    """In-place head.created/modified + checksumAdjustment, keep PDFBox bytes."""
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
    # LONGDATETIME is 8 bytes, high 32 bits zero, seconds since 1904 in low 32.
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


def hybrid_from_java(
    java: bytes, donor: bytes, fields: CardFields, short: bool = False
) -> bytes:
    """Keep PDFBox font subset from a Java render; swap in donor Form1,
    full-font descriptor metrics, and a donor-shaped content stream.
    """
    objs = parse_objects(java)
    objs_d = parse_objects(donor)
    widths = parse_w(objs[11])
    widths.pop(0, None)
    content = build_content(fields, widths, short=short)
    for n in (1, 2, 4, 8):
        objs[n] = objs_d[n]
    objs[11] = objs[11].replace(b"/W [0 [0]\n ", b"/W [")
    fd = objs[15]
    fd = re.sub(
        rb"/FontBBox \[[^\]]+\]",
        b"/FontBBox [-266.11328 -241.21094 1251.9531 1074.2188]",
        fd,
    )
    fd = re.sub(rb"/CapHeight [0-9.]+", b"/CapHeight 704.58984", fd)
    fd = re.sub(rb"/XHeight [0-9.]+", b"/XHeight 507.8125", fd)
    fd = re.sub(rb"/StemV [0-9.]+", b"/StemV 197.34863", fd)
    objs[15] = fd
    ttf = patch_subset_dates(stream_of(objs[17]))
    objs[17] = flate_obj(17, ttf, extra=f"/Length1 {len(ttf)}")
    id_m = re.search(rb"/ID \[<([0-9A-Fa-f]+)> <([0-9A-Fa-f]+)>\]", java)
    doc_id = f"[<{id_m.group(1).decode()}> <{id_m.group(2).decode()}>]"
    return wrap_pdf(objs, content, objs[3], doc_id)


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fill_card_html(fields: CardFields, short: bool = False) -> Path:
    name = "card_short.html" if short else "card_template.html"
    tpl = (JAVA_ROOT / name).read_text(encoding="utf-8")
    html = (
        tpl.replace("{{DATE}}", _xml(format_op_dt(fields.dt)))
        .replace("{{PAYER_CARD}}", _xml(fields.payer_card))
        .replace("{{ACCOUNT}}", _xml(fields.account))
        .replace("{{PAYER}}", _xml(fields.payer_name))
        .replace("{{PAYEE_CARD}}", _xml(fields.payee_card))
        .replace("{{BANK}}", _xml(fields.bank))
        .replace("{{FEE}}", _xml(format_amount(fields.commission)))
        .replace("{{TOTAL}}", _xml(format_amount(fields.total)))
        .replace("{{AMOUNT}}", _xml(format_amount(fields.amount)))
    )
    if fields.status != "done":
        html = html.replace(">Выполнено<", f">{_xml(fields.status_text)}<")
        html = html.replace("class=\"val green\"", "class=\"val\"")
        html = html.replace(">Операция выполнена<", f">{_xml(fields.stamp_text)}<")
    dest = JAVA_ROOT / ("card_now_short.html" if short else "card_now.html")
    dest.write_text(html, encoding="utf-8")
    return dest


def render_java(html: Path) -> bytes:
    pdf_path = JAVA_ROOT / "card_now.pdf"
    lib = JAVA_ROOT / "lib"
    cp = ":".join(str(p) for p in sorted(lib.glob("*.jar")))
    classes = Path("/tmp/vtbgen")
    classes.mkdir(parents=True, exist_ok=True)
    src = JAVA_ROOT / "src/main/java/VtbGenerator.java"
    class_file = classes / "VtbGenerator.class"
    if not class_file.exists() or class_file.stat().st_mtime < src.stat().st_mtime:
        subprocess.check_call(
            ["javac", "-cp", cp, "-d", str(classes), str(src)],
        )
    subprocess.check_call(
        [
            "java",
            "-cp",
            f"{classes}:{cp}",
            "VtbGenerator",
            str(html),
            str(pdf_path),
            str(IDENTITY_TTF),
        ],
        cwd=str(JAVA_ROOT),
    )
    return pdf_path.read_bytes()


def generate_unique(fields: CardFields, short: bool = False) -> bytes:
    """New receipt: PDFBox font subset + donor logo/page chrome + our content stream."""
    html = fill_card_html(fields, short=short)
    java = render_java(html)
    donor = (DONORS / "short.pdf" if short else PASSING_DONOR).read_bytes()
    return hybrid_from_java(java, donor, fields, short=short)


def unique_output_path(dt: datetime, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / pdf_filename(dt)
    if not out.exists():
        return out
    n = 2
    while True:
        cand = out.with_name(out.stem + f" {n}.pdf")
        if not cand.exists():
            return cand
        n += 1


def patch_amount_keep_rest(donor: bytes, amount: int, old_amount: int, old_total: int) -> bytes:
    """Replace only operation amount and total; keep font, date, cards, metadata."""
    objs = parse_objects(donor)
    widths = parse_w(objs[11])
    cs = bytearray(stream_of(objs[5]))
    _patch_tm_y(cs, "120.74999", format_amount(old_total), format_amount(amount + COMMISSION), 9.0, KERN_9, widths)
    _patch_tm_y(cs, "72.37499", format_amount(old_amount), format_amount(amount), 13.5, KERN_135, widths)
    id_m = re.search(rb"/ID \[<([0-9A-Fa-f]+)> <([0-9A-Fa-f]+)>\]", donor)
    id_s = f"[<{id_m.group(1).decode()}> <{id_m.group(2).decode()}>]"
    return wrap_pdf(objs, bytes(cs), objs[3], id_s)


def patch_sbp_amount(
    donor: bytes,
    amount: int,
    old_amount: int,
    *,
    x_s: Optional[str] = None,
    doc_id: Optional[str] = None,
    info: Optional[bytes] = None,
) -> bytes:
    """SBP receipts have no commission line — patch only the 13.5pt operation amount."""
    objs = parse_objects(donor)
    widths = parse_w(objs[11])
    cs = bytearray(stream_of(objs[5]))
    _patch_tm_y(
        cs,
        "72.37499",
        format_amount(old_amount),
        format_amount(amount),
        13.5,
        KERN_135,
        widths,
        x_s=x_s,
    )
    if doc_id is None:
        id_m = re.search(rb"/ID \[<([0-9A-Fa-f]+)> <([0-9A-Fa-f]+)>\]", donor)
        doc_id = f"[<{id_m.group(1).decode()}> <{id_m.group(2).decode()}>]"
    return wrap_pdf(objs, bytes(cs), info if info is not None else objs[3], doc_id)


PASSING_DONOR = DONORS / "full_alfa.pdf"


def patch_new_payment(
    donor: bytes,
    *,
    amount: int,
    dt: datetime,
    old_amount: int,
    old_total: int,
    old_date: str,
) -> bytes:
    """Patch the passing donor: new amount/time/CreationDate/ID, PDFBox stream wrapper."""
    objs = parse_objects(donor)
    widths = parse_w(objs[11])
    cs = bytearray(stream_of(objs[5]))
    new_date = format_op_dt(dt)
    _patch_tm_y(cs, "288.75", old_date, new_date, 9.0, KERN_9, widths)
    _patch_tm_y(cs, "120.74999", format_amount(old_total), format_amount(amount + COMMISSION), 9.0, KERN_9, widths)
    _patch_tm_y(cs, "72.37499", format_amount(old_amount), format_amount(amount), 13.5, KERN_135, widths)
    created = format_creation(dt)
    info = re.sub(
        rb"/CreationDate \(D:[^)]+\)",
        f"/CreationDate ({created})".encode("ascii"),
        objs[3],
    )
    return wrap_pdf(objs, bytes(cs), info, make_id(created))


def assemble_pdf(
    fields: CardFields,
    catalog: FontCatalog,
    donor: bytes,
    keep_font: bool,
    used_cids: List[int],
    prefix: str,
) -> bytes:
    objs_d = parse_objects(donor)
    content = build_content(fields, catalog.widths)

    created = format_creation(fields.dt)
    donor_info = objs_d[3]
    donor_date = re.search(rb"/CreationDate \(([^)]*)\)", donor_info)
    if donor_date and donor_date.group(1).decode("latin-1") == created:
        info_obj = donor_info if donor_info.endswith(b"\n") else donor_info + b"\n"
    else:
        info = (
            f"<<\n"
            f"/CreationDate ({created})\n"
            f"/Producer (openhtmltopdf.com)\n"
            f"/viewport (width=device-width, initial-scale=1.0)\n"
            f"/Title {utf16be_info_title()}\n"
            f">>\n"
        )
        info_obj = dict_obj(3, info)

    parts: Dict[int, bytes] = {}
    for n in (1, 2, 4):
        parts[n] = objs_d[n] if objs_d[n].endswith(b"\n") else objs_d[n] + b"\n"
    parts[3] = info_obj
    donor_content = stream_of(objs_d[5])
    if content == donor_content:
        parts[5] = objs_d[5]
        if not parts[5].endswith(b"\n"):
            parts[5] += b"\n"
    else:
        parts[5] = flate_obj(5, content)
    # keep donor graphics objects 6,7,8,10,13,14,18 verbatim (same numbers)
    for n in (6, 7, 8, 10, 13, 14):
        parts[n] = objs_d[n]
        if not parts[n].endswith(b"\n"):
            parts[n] += b"\n"

    if keep_font:
        for n in (9, 11, 12, 15, 16, 17, 18):
            parts[n] = objs_d[n]
            if not parts[n].endswith(b"\n"):
                parts[n] += b"\n"
    else:
        font_name = f"{prefix}+SFProDisplay-Regular"
        touni = build_tounicode(used_cids, catalog)
        cidmap = build_cidtogidmap(used_cids)
        ttf = build_subset_ttf(used_cids, catalog)
        warr = build_w_array(used_cids, catalog)
        parts[9] = dict_obj(
            9,
            "<<\n/Type /Font\n"
            f"/BaseFont /{font_name}\n"
            "/Subtype /Type0\n/Encoding /Identity-H\n"
            "/DescendantFonts [11 0 R]\n/ToUnicode 12 0 R\n>>\n",
        )
        parts[11] = dict_obj(
            11,
            "<<\n/Type /Font\n/Subtype /CIDFontType2\n"
            f"/BaseFont /{font_name}\n"
            "/CIDSystemInfo 14 0 R\n/FontDescriptor 15 0 R\n"
            f"{warr}\n/CIDToGIDMap 16 0 R\n>>\n",
        )
        parts[12] = flate_obj(12, touni)
        # FontDescriptor metrics copied from donor (full-font values, identical across files)
        parts[15] = dict_obj(
            15,
            "<<\n/Type /FontDescriptor\n"
            f"/FontName /{font_name}\n"
            "/Flags 4\n/FontWeight 400.0\n/ItalicAngle 0.0\n"
            "/FontBBox [-266.11328 -241.21094 1251.9531 1074.2188]\n"
            "/Ascent 952.14844\n/Descent -241.21094\n"
            "/CapHeight 704.58984\n/XHeight 507.8125\n/StemV 197.34863\n"
            "/FontFile2 17 0 R\n/CIDSet 18 0 R\n>>\n",
        )
        parts[16] = flate_obj(16, cidmap)
        parts[17] = flate_obj(17, ttf, extra=f"/Length1 {len(ttf)}")
        parts[18] = objs_d[18]
        if not parts[18].endswith(b"\n"):
            parts[18] += b"\n"

    order = [1, 3, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    header = b"%PDF-1.4\n%\xf6\xe4\xfc\xdf\n"
    body = bytearray(header)
    offsets = {0: 0}
    for n in order:
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
        f"/ID {make_id(created)}\n/Size 19\n>>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(body) + b"".join(xref) + trailer


def donor_cids(donor: bytes) -> set:
    objs = parse_objects(donor)
    return set(parse_tounicode(stream_of(objs[12])))


def build(fields: CardFields, catalog: Optional[FontCatalog] = None) -> bytes:
    catalog = catalog or load_catalog()
    donor = PRIMARY.read_bytes()
    for ch in fields.used_chars():
        catalog.ensure(ch)
    used = sorted({cid_of(ch) for ch in fields.used_chars()})
    keep = set(used) <= donor_cids(donor)
    prefix = "AA" + "".join(random.choice(string.ascii_uppercase) for _ in range(4))
    return assemble_pdf(fields, catalog, donor, keep, used, prefix)


# --------------------------------------------------------------------------- message parse / CLI


DEFAULT = CardFields(
    status="done",
    dt=datetime(2026, 10, 12, 19, 41, 39, tzinfo=MSK),
    payer_card="220024******2458",
    account="*9426",
    payer_name="Александр Евгеньевич Ж.",
    payee_card="2200 39** **** 9515",
    bank="Росбанк",
    amount=125,
)


def parse_amount(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits)


def parse_message(text: str, now: Optional[datetime] = None) -> CardFields:
    """Fill CardFields from a free-form template message; defaults cover the rest."""
    now = now or datetime.now(MSK)
    f = CardFields(
        status=DEFAULT.status,
        dt=now.replace(microsecond=0),
        payer_card=DEFAULT.payer_card,
        account=DEFAULT.account,
        payer_name=DEFAULT.payer_name,
        payee_card=DEFAULT.payee_card,
        bank=DEFAULT.bank,
        amount=DEFAULT.amount,
    )
    low = text.lower()
    if "в обработке" in low or "обработк" in low:
        f.status = "processing"
    if "выполнен" in low:
        f.status = "done"

    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4})(?:[,\s]+(\d{1,2}:\d{2}))?", text)
    if m:
        d, t = m.group(1), m.group(2) or now.strftime("%H:%M")
        parts = d.split(".")
        dd, mm, yy = int(parts[0]), int(parts[1]), int(parts[2])
        if yy < 100:
            yy += 2000
        hh, mi = map(int, t.split(":"))
        f.dt = datetime(yy, mm, dd, hh, mi, now.second, tzinfo=MSK)

    nospace = text.replace(" ", "")
    m = re.search(r"(2200\d{2}\*{6}\d{4})", nospace)
    if m:
        f.payer_card = m.group(1)
    m = re.search(r"(2200\s+\d{2}\*{2}\s+\*{4}\s+\d{4})", text)
    if m:
        f.payee_card = re.sub(r"\s+", " ", m.group(1)).strip()

    m = re.search(r"(?<!\*)(\*\d{4})(?!\d)", text)
    if m:
        f.account = m.group(1)

    m = re.search(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.)", text)
    if m:
        f.payer_name = m.group(1)

    for bank in ("Росбанк", "Совкомбанк", "Альфа Банк", "Сбербанк", "Т-Банк", "Тинькофф"):
        if bank.lower() in low:
            f.bank = bank
            break

    m = re.search(r"(?:сумма|amount)\s*[:=]?\s*([\d\s]+)", text, re.I)
    if m:
        f.amount = parse_amount(m.group(1))
    else:
        amounts = [parse_amount(x) for x in re.findall(r"(\d[\d\s]{0,8})\s*₽", text)]
        amounts = [a for a in amounts if a and a != 99]
        if amounts:
            f.amount = amounts[-1]
    return f


def clone_donor() -> bytes:
    return PRIMARY.read_bytes()


def extract_text(data: bytes) -> List[str]:
    objs = parse_objects(data)
    touni = parse_tounicode(stream_of(objs[12]))
    cs = stream_of(objs[5])
    texts: List[str] = []

    def unescape(buf: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(buf):
            if buf[i] == 0x5C and i + 1 < len(buf):
                out.append(buf[i + 1])
                i += 2
            else:
                out.append(buf[i])
                i += 1
        return bytes(out)

    def decode(arr: bytes) -> str:
        text: List[str] = []
        i = 0
        n = len(arr)
        while i < n:
            if arr[i] == ord("<"):
                j = arr.find(b">", i)
                hx = arr[i + 1 : j].decode()
                if len(hx) % 2:
                    hx = "0" + hx
                raw = bytes.fromhex(hx)
                for k in range(0, len(raw), 2):
                    cid = (raw[k] << 8) | raw[k + 1]
                    text.append(touni.get(cid, "?"))
                i = j + 1
            elif arr[i] == ord("("):
                j = i + 1
                buf = bytearray()
                while j < n and arr[j] != ord(")"):
                    if arr[j] == 0x5C:
                        buf.append(arr[j + 1])
                        j += 2
                    else:
                        buf.append(arr[j])
                        j += 1
                for k in range(0, len(buf), 2):
                    if k + 1 < len(buf):
                        cid = (buf[k] << 8) | buf[k + 1]
                        text.append(touni.get(cid, "?"))
                i = j + 1
            else:
                i += 1
        return "".join(text)

    i = 0
    n = len(cs)
    while i < n:
        if cs[i] == ord("["):
            depth = 1
            j = i + 1
            while j < n and depth:
                if cs[j] == ord("("):
                    j += 1
                    while j < n:
                        if cs[j] == 0x5C:
                            j += 2
                            continue
                        if cs[j] == ord(")"):
                            j += 1
                            break
                        j += 1
                    continue
                if cs[j] == ord("<"):
                    j = cs.find(b">", j) + 1
                    continue
                if cs[j] == ord("["):
                    depth += 1
                elif cs[j] == ord("]"):
                    depth -= 1
                j += 1
            k = j
            while k < n and cs[k] in b" \t":
                k += 1
            if cs[k : k + 2] == b"TJ":
                texts.append(decode(cs[i:j]))
                i = k + 2
                continue
        i += 1
    return texts


def selftest() -> None:
    cat = load_catalog()
    donor = PRIMARY.read_bytes()
    rebuilt = build(DEFAULT, cat)
    got = extract_text(rebuilt)
    exp = extract_text(donor)
    if got != exp:
        print("TEXT MISMATCH")
        for a, b in zip(exp, got):
            if a != b:
                print(f"  {a!r} -> {b!r}")
        print("extra", got[len(exp) :])
        print("missing", exp[len(got) :])
        sys.exit(1)
    objs_d = parse_objects(donor)
    objs_r = parse_objects(rebuilt)
    cs_d = stream_of(objs_d[5])
    cs_r = stream_of(objs_r[5])
    print("text OK", got)
    print("content equal", cs_d == cs_r, "donor", len(cs_d), "rebuild", len(cs_r))
    if cs_d != cs_r:
        # first diff
        for i, (a, b) in enumerate(zip(cs_d, cs_r)):
            if a != b:
                print("first diff at", i, cs_d[max(0, i - 40) : i + 40], "vs", cs_r[max(0, i - 40) : i + 40])
                break
        else:
            print("length differs")
    print("size donor", len(donor), "rebuild", len(rebuilt))


def main() -> None:
    p = argparse.ArgumentParser(description="VTB card-transfer receipt")
    p.add_argument("--clone", action="store_true", help="write a byte copy of the donor")
    p.add_argument("--rebuild", action="store_true", help="same fields as donor 1")
    p.add_argument("--passing", action="store_true", help="patch the passing donor (file 4) with new amount/time/ID")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--message", help="template message")
    p.add_argument("--amount", type=int)
    p.add_argument("--status", choices=("done", "processing"))
    p.add_argument("--payer-card")
    p.add_argument("--account")
    p.add_argument("--payer-name")
    p.add_argument("--payee-card")
    p.add_argument("--bank")
    p.add_argument("--date")  # DD.MM.YYYY, HH:MM
    p.add_argument("-o", "--output")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.clone:
        data = clone_donor()
        out = Path(args.output) if args.output else OUT_DIR / "12-10-26_00-00.pdf"
        out.write_bytes(data)
        print("cloned", out, len(data))
        return

    if args.passing:
        donor = PASSING_DONOR.read_bytes()
        now = datetime.now(MSK).replace(microsecond=0)
        amount = args.amount if args.amount is not None else 13405
        date_s = format_op_dt(now)
        # Donor 4's only '7' lives in 274 ₽ — keep a 7 in date or amount so the subset stays honest.
        if "7" not in date_s and "7" not in format_amount(amount) and "7" not in format_amount(amount + COMMISSION):
            now = now.replace(minute=17 if now.minute < 55 else 47)
            date_s = format_op_dt(now)
        data = patch_new_payment(
            donor,
            amount=amount,
            dt=now,
            old_amount=274,
            old_total=373,
            old_date="12.10.2026, 19:50",
        )
        out = Path(args.output) if args.output else OUT_DIR / pdf_filename(now)
        out.write_bytes(data)
        print(out, len(data), date_s, format_amount(amount))
        return

    now = datetime.now(MSK)
    if args.rebuild:
        fields = DEFAULT
    elif args.message:
        fields = parse_message(args.message, now)
    else:
        fields = CardFields(
            status=args.status or "done",
            dt=now.replace(microsecond=0),
            payer_card=args.payer_card or DEFAULT.payer_card,
            account=args.account or DEFAULT.account,
            payer_name=args.payer_name or DEFAULT.payer_name,
            payee_card=args.payee_card or DEFAULT.payee_card,
            bank=args.bank or DEFAULT.bank,
            amount=args.amount if args.amount is not None else DEFAULT.amount,
        )
        if args.date:
            fields = parse_message(args.date, now)
            fields.payer_card = args.payer_card or DEFAULT.payer_card
            fields.account = args.account or DEFAULT.account
            fields.payer_name = args.payer_name or DEFAULT.payer_name
            fields.payee_card = args.payee_card or DEFAULT.payee_card
            fields.bank = args.bank or DEFAULT.bank
            fields.amount = args.amount if args.amount is not None else DEFAULT.amount
            fields.status = args.status or fields.status

    if args.status:
        fields.status = args.status
    if args.amount is not None:
        fields.amount = args.amount
    if args.payer_card:
        fields.payer_card = args.payer_card
    if args.account:
        fields.account = args.account if args.account.startswith("*") else "*" + args.account
    if args.payer_name:
        fields.payer_name = args.payer_name
    if args.payee_card:
        fields.payee_card = args.payee_card
    if args.bank:
        fields.bank = args.bank

    # --rebuild stays a donor twin. Everything else is a NEW document
    # (Java subset + new /ID), not a patched copy of a checked file.
    if args.rebuild:
        data = build(fields)
        dest_dir = OUT_DIR
    else:
        data = generate_unique(fields, short=False)
        dest_dir = DESKTOP
    out = Path(args.output) if args.output else unique_output_path(fields.dt, dest_dir)
    out.write_bytes(data)
    print(out, len(data), format_op_dt(fields.dt), fields.status_text, format_amount(fields.amount))


if __name__ == "__main__":
    main()
