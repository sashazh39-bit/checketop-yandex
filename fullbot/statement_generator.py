# -*- coding: utf-8 -*-
"""EXI-BANK statement generator — byte-exact OpenPDF/JasperReports reconstruction.

Rebuilds both page content streams from scratch using the exact operator
patterns of the originals, then subsets the three MTSSans faces
(Regular/Medium/Bold) with zero orphan glyphs, OpenPDF physical table order
and preserved factory head/hhea/hmtx/maxp tables + directory checksums.

Layout rules (derived from 7 genuine originals, 100% fit):
    totals_y     = min(406.29, table_bottom - 30.71)
    disclaimer_y = totals_y - 33.00
    sig_y        = white_box_y - 9.71
"""
import os, re, zlib, struct, pickle, secrets, uuid, random as _rnd
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_DONOR = os.environ.get("EXI_DONOR") or os.path.join(
    r"C:\Users\nemor\Downloads\Telegram Desktop", "769fbeac-fadd-4ff7-93bc-b0789e934823.pdf")

# ─────────────────────────── glyph catalog ───────────────────────────
_cat_blob = pickle.load(open(os.path.join(_DIR, "_stmt_catalog_v3.pkl"), "rb"))
CAT = _cat_blob["catalog"]           # face -> char -> {cid,width,glyph}
FT = _cat_blob["face_tables"]        # face -> factory tables + checksums

FACES = ("Regular", "Medium", "Bold")
RES_OF = {"Regular": "F4", "Medium": "F2", "Bold": "F3"}
FACE_OF = {"F4": "Regular", "F2": "Medium", "F3": "Bold"}

U2CID = {f: {ch: v["cid"] for ch, v in CAT[f].items()} for f in FACES}
W = {f: {ch: v["width"] for ch, v in CAT[f].items()} for f in FACES}
CID2GLYPH = {f: {v["cid"]: v["glyph"] for v in CAT[f].values()} for f in FACES}
CID2UCP = {f: {v["cid"]: ord(ch) for ch, v in CAT[f].items()} for f in FACES}
CID2W = {f: {v["cid"]: v["width"] for v in CAT[f].values()} for f in FACES}


class MissingGlyphs(ValueError):
    def __init__(self, face, chars):
        self.face, self.chars = face, chars
        super().__init__(f"{face}: no glyphs for {''.join(sorted(set(chars)))!r}")


def check_text(s: str, face: str) -> list:
    """Return the list of characters of *s* that the face cannot render."""
    return [c for c in s if c not in U2CID[face]]


def text_w(s: str, face: str, size: float) -> float:
    """Advance width of *s* in points."""
    miss = check_text(s, face)
    if miss:
        raise MissingGlyphs(face, miss)
    return sum(W[face][c] for c in s) * size / 1000.0


def f32(x: float) -> float:
    """Round to IEEE single precision — JasperReports keeps coordinates as float."""
    return struct.unpack("f", struct.pack("f", x))[0]


def fmt(v: float) -> str:
    """Exact port of OpenPDF ByteBuffer.formatDouble (non-high-precision path).

    Coordinates arrive as Java floats, so narrow to single precision first; the
    Java original then adds an epsilon and truncates, which decides half-way
    values (51.265 -> "51.26", 50.125 -> "50.13").
    """
    v = f32(v)
    if abs(v) < 0.000015:
        return "0"
    neg = v < 0
    d = -v if neg else v
    sign = "-" if neg else ""
    if d < 1.0:
        d += 0.000005
        if d >= 1:
            return sign + "1"
        iv = int(d * 100000)
        s = "0." + str(iv // 10000)
        if iv % 10000:
            s += str((iv // 1000) % 10)
            if iv % 1000:
                s += str((iv // 100) % 10)
                if iv % 100:
                    s += str((iv // 10) % 10)
                    if iv % 10:
                        s += str(iv % 10)
        return sign + s
    if d <= 32767:
        d += 0.005
        iv = int(d * 100)
        s = str(iv // 100)
        frac = iv % 100
        if frac:
            s += "." + str(frac // 10)
            if frac % 10:
                s += str(frac % 10)
        return sign + s
    return sign + str(int(d + 0.5))


def enc(s: str, face: str) -> bytes:
    """Encode text as a 2-byte-per-CID PDF literal string with OpenPDF escaping."""
    miss = check_text(s, face)
    if miss:
        raise MissingGlyphs(face, miss)
    out = bytearray()
    for ch in s:
        cid = U2CID[face][ch]
        for b in (cid >> 8, cid & 0xFF):
            if b in (0x28, 0x29, 0x5C):
                out += b"\\" + bytes([b])
            elif b == 0x0D:
                out += b"\\r"
            elif b == 0x0A:
                out += b"\\n"
            elif b == 0x09:
                out += b"\\t"
            elif b == 0x08:
                out += b"\\b"
            elif b == 0x0C:
                out += b"\\f"
            else:
                out.append(b)
    return bytes(out)


# ─────────────────────── content-stream emitters ───────────────────────
SEP = b"1 0 0 1 0 0 cm\n[] 0 d\n2 J\n"


def _txt(x, y, res, size, body, rg="0 0 0"):
    return (b"BT\n1 0 0 1 " + fmt(x).encode() + b" " + fmt(y).encode() + b" Tm\n"
            b"/" + res.encode() + b" " + str(size).encode() + b" Tf\n"
            + rg.encode() + b" rg\n(" + body + b")Tj\n0 g\nET\n")


def _txt2(x, y1, y2, res, size, b1, b2):
    """Two-line cell (used by the 'Дата совершения / операции по карте' header)."""
    return (b"BT\n1 0 0 1 " + fmt(x).encode() + b" " + fmt(y1).encode() + b" Tm\n"
            b"/" + res.encode() + b" " + str(size).encode() + b" Tf\n0 0 0 rg\n("
            + b1 + b")Tj\n0 g\nET\n"
            b"BT\n1 0 0 1 " + fmt(x).encode() + b" " + fmt(y2).encode() + b" Tm\n"
            b"/" + res.encode() + b" " + str(size).encode() + b" Tf\n0 0 0 rg\n("
            + b2 + b")Tj\n0 g\nET\n")


def _line(x1, y1, x2, y2):
    return (b"0.5 w\n0 J\n0 0 0 RG\n[] 0 d\n"
            + fmt(x1).encode() + b" " + fmt(y1).encode() + b" m\n"
            + fmt(x2).encode() + b" " + fmt(y2).encode() + b" l\nS\n")


def _cell(x, w, top, inner, h=30):
    """White fill + inner content + the four 0.5pt borders of one table cell."""
    out = bytearray()
    out += b"[] 0 d\n2 J\n1 1 1 rg\n"
    out += (fmt(x).encode() + b" " + fmt(top).encode() + b" "
            + fmt(w).encode() + b" " + fmt(-h).encode() + b" re\nf\n")
    out += b"1 0 0 1 0 0 cm\n"
    out += inner
    out += SEP
    bot = top - h
    out += _line(x - 0.25, top, x + w + 0.25, top)
    out += _line(x, top + 0.25, x, bot - 0.25)
    out += _line(x - 0.25, bot, x + w + 0.25, bot)
    out += _line(x + w, top + 0.25, x + w, bot - 0.25)
    return bytes(out)


COLS_P1 = ((30, 46), (76, 143), (219, 128), (347, 214))
COLS_P2 = ((31, 46), (77, 143), (220, 128), (348, 214))
TXT_P1 = (None, 79, 222, 350)
TXT_P2 = (None, 80, 223, 351)
TOP_P1, TOP_P2 = 563, 795
ROW_H = 30

HDR_NO = "№"
HDR_DATE1, HDR_DATE2 = "Дата совершения", "операции по карте"
HDR_AMT, HDR_DESC = "Сумма операции", "Описание операции"


def _table(rows, cols, txtx, top, no_x_hdr):
    """Header band + one band per row. *rows* = list of (date, amount, desc)."""
    out = bytearray()
    # header band
    out += _cell(cols[0][0], cols[0][1], top,
                 _txt(no_x_hdr, top - 18.71, "F3", 10, enc(HDR_NO, "Bold")))
    out += _cell(cols[1][0], cols[1][1], top,
                 _txt2(txtx[1], top - 9.71, top - 21.71, "F3", 10,
                       enc(HDR_DATE1, "Bold"), enc(HDR_DATE2, "Bold")))
    out += _cell(cols[2][0], cols[2][1], top,
                 _txt(txtx[2], top - 9.71, "F3", 10, enc(HDR_AMT, "Bold")))
    out += _cell(cols[3][0], cols[3][1], top,
                 _txt(txtx[3], top - 9.71, "F3", 10, enc(HDR_DESC, "Bold")))
    # data bands
    for i, (dt_s, amt_s, desc) in enumerate(rows, 1):
        band = top - ROW_H * i
        ty = band - 9.71
        num = str(i)
        cx = cols[0][0] + cols[0][1] / 2.0 - text_w(num, "Regular", 10) / 2.0
        out += _cell(cols[0][0], cols[0][1], band,
                     _txt(cx, ty, "F4", 10, enc(num, "Regular")))
        out += _cell(cols[1][0], cols[1][1], band,
                     _txt(txtx[1], ty, "F4", 10, enc(dt_s, "Regular")))
        out += _cell(cols[2][0], cols[2][1], band,
                     _txt(txtx[2], ty, "F4", 10, enc(amt_s, "Regular")))
        out += _cell(cols[3][0], cols[3][1], band,
                     _txt(txtx[3], ty, "F4", 10, enc(desc, "Regular")))
    return bytes(out)


PREAMBLE = (b"q\nBT\n36 806 Td\nET\nQ\n2 J\nBT\n1 0 0 1 0 842 Tm\n/F1 10 Tf\n()Tj\nET\n"
            b"1 0 0 1 0 0 cm\n")
GAP = b"1 0 0 1 0 0 cm\n[] 0 d\n2 J\n1 0 0 1 0 0 cm\n"
CM = b"1 0 0 1 0 0 cm\n"


def _txt_just(x, y, res, size, body, scale, width):
    """Justified paragraph: JasperReports emits a sheared Tm plus a no-op Td pair."""
    xs, ys = fmt(x).encode(), fmt(y).encode()
    return (b"BT\n1 0 0 1 " + xs + b" " + ys + b" Tm\n"
            b"1 0 " + scale.encode() + b" 1 " + xs + b" " + ys + b" Tm\n"
            b"/" + res.encode() + b" " + str(size).encode() + b" Tf\n0 0 0 rg\n("
            + body + b")Tj\n0 g\n"
            b"1 0 0 1 " + fmt(x + width).encode() + b" " + ys + b" Tm\n"
            b"-" + fmt(width).encode() + b" 0 Td\nET\n")


def _txt_multi(x, ys, res, size, bodies):
    """Several lines inside one BT/ET (Tf emitted once, then Tm/rg/Tj per line)."""
    out = bytearray(b"BT\n1 0 0 1 " + fmt(x).encode() + b" " + fmt(ys[0]).encode() + b" Tm\n"
                    b"/" + res.encode() + b" " + str(size).encode() + b" Tf\n"
                    b"0 0 0 rg\n(" + bodies[0] + b")Tj\n0 g\n")
    for y, b in zip(ys[1:], bodies[1:]):
        out += (b"1 0 0 1 " + fmt(x).encode() + b" " + fmt(y).encode() + b" Tm\n"
                b"0 0 0 rg\n(" + b + b")Tj\n0 g\n")
    out += b"ET\n"
    return bytes(out)


def _image(name, w, h, x, y):
    xs, ys = fmt(x).encode(), fmt(y).encode()
    return (b"q " + fmt(w).encode() + b" 0 0 " + fmt(h).encode() + b" " + xs + b" " + ys
            + b" cm /" + name.encode() + b" Do Q\n"
            b"BT\n1 0 0 1 " + xs + b" " + ys + b" Tm\n"
            + fmt(w).encode() + b" 0 Td\n-" + fmt(w).encode() + b" 0 Td\nET\n")


def _round_box(x0, y0, x1, y1, r=10.0):
    """Rounded rectangle, drawn once filled white then once stroked grey."""
    k = f32(r * 0.552)
    def path():
        p = bytearray()
        p += fmt(x0 + r).encode() + b" " + fmt(y0).encode() + b" m\n"
        p += fmt(x1 - r).encode() + b" " + fmt(y0).encode() + b" l\n"
        p += (fmt(x1 - r + k).encode() + b" " + fmt(y0).encode() + b" "
              + fmt(x1).encode() + b" " + fmt(y0 + r - k).encode() + b" "
              + fmt(x1).encode() + b" " + fmt(y0 + r).encode() + b" c\n")
        p += fmt(x1).encode() + b" " + fmt(y1 - r).encode() + b" l\n"
        p += (fmt(x1).encode() + b" " + fmt(y1 - r + k).encode() + b" "
              + fmt(x1 - r + k).encode() + b" " + fmt(y1).encode() + b" "
              + fmt(x1 - r).encode() + b" " + fmt(y1).encode() + b" c\n")
        p += fmt(x0 + r).encode() + b" " + fmt(y1).encode() + b" l\n"
        p += (fmt(x0 + r - k).encode() + b" " + fmt(y1).encode() + b" "
              + fmt(x0).encode() + b" " + fmt(y1 - r + k).encode() + b" "
              + fmt(x0).encode() + b" " + fmt(y1 - r).encode() + b" c\n")
        p += fmt(x0).encode() + b" " + fmt(y0 + r).encode() + b" l\n"
        p += (fmt(x0).encode() + b" " + fmt(y0 + r - k).encode() + b" "
              + fmt(x0 + r - k).encode() + b" " + fmt(y0).encode() + b" "
              + fmt(x0 + r).encode() + b" " + fmt(y0).encode() + b" c\n")
        return bytes(p)
    return (b"/GS1 gs\n/GS2 gs\n1 1 1 rg\n1 w\n2 J\n0.72941 0.75686 0.78039 RG\n[] 0 d\n"
            + path() + b"f\n" + path() + b"S\n/GS3 gs\n[] 0 d\n")

BANK_SITE = "www.exibank.ru"
BANK_PHONE = "8 800 250-32-77"
BANK_ADDR = ("Российская Федерация, 199178, г. Санкт-Петербург, "
             "Малый пр. В.О., д.54, к.4, лит. В, пом.1-Н")
BANK_LIC = "Базовая банковская лицензия №2530 от 12.09.2019 г."
BAL_NOTE = "*Доступный остаток с учетом заблокированных средств"
DISCLAIMER = ("При несогласии с операцией обратитесь в Банк",
              "с письменным заявлением не позднее дня,",
              "следующего за днем получения уведомления",
              "Банка о совершении операции.")
FOOTNOTE = "*Будут зачислены в соответствии с условиями по опции"
SIG = ("С уважением,", "Служба поддержки клиентов", '"ЭКСИ-БАНК" (АО)')
NO_OPS = "За указанный период операции отсутствуют"
GREEN = "0.07059 0.69804 0.24706"


BAL_NOTE_SCALE, BAL_NOTE_W = "0.21256", 210.59
FOOTNOTE_SCALE, FOOTNOTE_W = "0.21256", 209.3


def _page1(name, card, account, balance, stmt_date, per_from, per_to,
           rows, total_in, total_out):
    n = len(rows)
    tot_y = min(406.29, (TOP_P1 - ROW_H - ROW_H * n) - 30.71)
    amt_y = tot_y - 0.94
    dis_y = tot_y - 33.0

    o = bytearray(PREAMBLE)
    o += _txt(133, amt_y, "F2", 12, enc(total_in, "Medium"), GREEN) + GAP
    o += _txt(380, amt_y, "F2", 12, enc(total_out, "Medium")) + GAP
    o += _txt(30, 576.41, "F3", 14,
              enc(f"Операции за период с {per_from} по {per_to}", "Bold")) + GAP
    o += _txt(330, 653.26, "F4", 9, enc(f"Номер карты: (MIR) {card}", "Regular")) + GAP
    o += _txt(330, 638.26, "F4", 9, enc(f"Номер счета: {account}", "Regular")) + GAP
    o += _txt(330, 623.26, "F4", 9,
              enc(f"Доступный остаток* на {stmt_date}: {balance}", "Regular")) + GAP
    o += _txt_just(330, 609.23, "F4", 8, enc(BAL_NOTE, "Regular"),
                   BAL_NOTE_SCALE, BAL_NOTE_W) + GAP
    o += _txt(31, 653.26, "F4", 9, enc(name, "Regular")) + GAP
    o += _txt(26, 687.75, "F4", 26, enc(f"Счет-выписка от {stmt_date}", "Regular")) + GAP
    o += _txt(30, 762.23, "F4", 8, enc(BANK_SITE, "Regular"))
    o += _txt(30, 752.63, "F4", 8, enc(BANK_PHONE, "Regular"))
    o += _txt(30, 743.03, "F4", 8, enc(BANK_ADDR, "Regular"))
    o += _txt(30, 733.43, "F4", 8, enc(BANK_LIC, "Regular")) + GAP
    o += _txt(30, tot_y, "F3", 10, enc("Всего поступлений:", "Bold")) + GAP
    o += _txt(293, tot_y, "F3", 10, enc("Всего списаний:", "Bold")) + CM
    o += _table(rows, COLS_P1, TXT_P1, TOP_P1, 45.54)
    o += b"[] 0 d\n2 J\n[] 0 d\n2 J\n"
    o += _image("img1", 110, 73.42, 485, 768.58)
    o += _round_box(30, dis_y - 63.29, 280, dis_y + 14.71)
    o += CM
    o += _txt_multi(40, [dis_y - 12 * i for i in range(4)], "F4", 10,
                    [enc(l, "Regular") for l in DISCLAIMER]) + GAP
    o += _txt_just(40, dis_y - 52.06, "F4", 8, enc(FOOTNOTE, "Regular"),
                   FOOTNOTE_SCALE, FOOTNOTE_W) + CM + b"[] 0 d\n2 J\n[] 0 d\n2 J\n" + CM
    o += _txt(296.53, 47.29, "F4", 10, enc("1", "Regular")) + GAP
    o += _txt(320, 47.29, "F4", 10, enc("2", "Regular")) + GAP
    o += _txt(304.58, 47.29, "F4", 10, enc("из", "Regular")) + CM + b"[] 0 d\n2 J\n"
    return bytes(o)


def _page2(stmt_date, holds):
    h = len(holds)
    box_y = min(695.0, (TOP_P2 - ROW_H - ROW_H * h) - 37.0) if h else 695.0
    sig_y = box_y - 9.71

    o = bytearray(PREAMBLE)
    o += _txt(30, 808.41, "F3", 14,
              enc(f"Заблокированные средства на {stmt_date}:", "Bold")) + GAP
    o += b"/GS4 gs\n1 1 1 rg\n"
    o += b"30 " + fmt(box_y).encode() + b" 150 -40 re\nf\n"
    for i, ln in enumerate(SIG):
        o += _txt(30, sig_y - 12 * i, "F4", 10, enc(ln, "Regular"))
    o += CM
    if h:
        o += _table(holds, COLS_P2, TXT_P2, TOP_P2, 46.54)
        o += b"[] 0 d\n2 J\n[] 0 d\n2 J\n"
    else:
        o += b"[] 0 d\n2 J\n" + CM
        o += _txt(31, 785.29, "F4", 10, enc(NO_OPS, "Regular")) + CM
        o += b"[] 0 d\n2 J\n[] 0 d\n2 J\n[] 0 d\n2 J\n"
    o += _image("img2", 193, 99.67, 370, box_y - 68.67)
    o += CM
    o += _txt(294.59, 47.29, "F4", 10, enc("2", "Regular")) + GAP
    o += _txt(320, 47.29, "F4", 10, enc("2", "Regular")) + GAP
    o += _txt(304.58, 47.29, "F4", 10, enc("из", "Regular")) + CM + b"[] 0 d\n2 J\n"
    return bytes(o)


# ───────────────────────── font subsetting ─────────────────────────
def cksum(b: bytes) -> int:
    if len(b) % 4:
        b += b"\x00" * (4 - len(b) % 4)
    s = 0
    for i in range(0, len(b), 4):
        s = (s + struct.unpack(">I", b[i:i + 4])[0]) & 0xFFFFFFFF
    return s


def used_cids(stream: bytes):
    """CIDs actually drawn, per face, from a finished content stream."""
    out = {f: set() for f in FACES}
    cur = None
    for tok in re.finditer(rb"/(F\d)\s+[\d.]+\s+Tf|\(((?:\\.|[^\\)])*)\)Tj", stream):
        if tok.group(1):
            cur = FACE_OF.get(tok.group(1).decode())
        elif cur:
            raw = _unesc(tok.group(2))
            out[cur] |= {(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw) - 1, 2)}
    return out


def _unesc(b: bytes) -> bytes:
    o = bytearray(); i = 0
    mp = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 40, 0x29: 41, 0x5C: 92}
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):
            nx = b[i + 1]
            if nx in mp:
                o.append(mp[nx]); i += 2; continue
            if 0x30 <= nx <= 0x37:
                j = 0; s = b[i + 1:i + 4]
                while j < 3 and j < len(s) and 0x30 <= s[j] <= 0x37:
                    j += 1
                o.append(int(b[i + 1:i + 1 + j].decode(), 8)); i += 1 + j; continue
            o.append(nx); i += 2; continue
        o.append(c); i += 1
    return bytes(o)


def build_ttf(face: str, cids: set) -> bytes:
    """TrueTypeFontSubSet in OpenPDF physical order: glyf, head, hhea, hmtx, loca, maxp."""
    ft = FT[face]
    ng = ft["numGlyphs"]
    g2 = CID2GLYPH[face]
    glyf = bytearray()
    loca = []
    for gid in range(ng):
        loca.append(len(glyf))
        if gid in cids:
            chunk = g2.get(gid, b"")
            if len(chunk) % 4:                # OpenPDF pads each glyph to 4 bytes
                chunk += b"\x00" * (4 - len(chunk) % 4)
            glyf += chunk
    loca.append(len(glyf))
    loca_b = b"".join(struct.pack(">H", o // 2) for o in loca)

    entries = [
        (b"glyf", bytes(glyf), cksum(bytes(glyf))),
        (b"head", ft["head"], ft["head_ck"]),   # factory head verbatim, incl. checkSumAdjustment
        (b"hhea", ft["hhea"], ft["hhea_ck"]),
        (b"hmtx", ft["hmtx"], ft["hmtx_ck"]),
        (b"loca", loca_b, cksum(loca_b)),
        (b"maxp", ft["maxp"], ft["maxp_ck"]),
    ]
    pos = 108
    built = []
    for tag, blob, ck in entries:
        if pos % 4:
            pos += 4 - (pos % 4)
        built.append((tag, pos, blob, ck))
        pos += len(blob)

    font = bytearray(pos)
    font[:12] = ft["sfnt"]
    order = sorted(range(len(entries)), key=lambda i: entries[i][0])
    for di, ei in enumerate(order):
        tag, off, blob, ck = built[ei]
        o = 12 + di * 16
        font[o:o + 4] = tag
        struct.pack_into(">III", font, o + 4, ck, off, len(blob))
    for tag, off, blob, ck in built:
        font[off:off + len(blob)] = blob
    return bytes(font)


def build_tounicode(face: str, cids: set) -> bytes:
    c2u = CID2UCP[face]
    lines = [f"<{c:04x}><{c:04x}><{c2u[c]:04x}>".encode() for c in sorted(cids)]
    return (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo\n<< /Registry (TTX+0)\n/Ordering (T42UV)\n/Supplement 0\n>> def\n"
            b"/CMapName /TTX+0 def\n/CMapType 2 def\n1 begincodespacerange\n<0000><FFFF>\n"
            b"endcodespacerange\n" + f"{len(lines)} beginbfrange\n".encode()
            + b"\n".join(lines) + b"\nendbfrange\nendcmap\n"
            b"CMapName currentdict /CMap defineresource pop\nend end\n")


def build_w(face: str, cids: set) -> bytes:
    c2w = CID2W[face]
    groups = []
    for c in sorted(cids):
        if groups and groups[-1][0] + len(groups[-1][1]) == c:
            groups[-1][1].append(str(c2w[c]))
        else:
            groups.append((c, [str(c2w[c])]))
    return ("".join(f"{s}[{' '.join(ws)}]" for s, ws in groups)).encode()


# ─────────────────────── PDF assembly ───────────────────────
try:
    from mts_build_check15 import jcomp as _jcomp_java
except Exception:
    _jcomp_java = None


def jcomp(data: bytes, tag: str = "st") -> bytes:
    """Deflate byte-identically to java.util.zip.Deflater (what OpenPDF uses).

    Python's zlib emits a different bit layout for the same input, so route
    through the real JVM deflater when it is available.
    NEVER silently fall back to zlib — fail loudly instead.
    """
    if _jcomp_java is None:
        raise RuntimeError(
            "jcomp unavailable (mts_build_check15 import failed); "
            "refusing to emit Python-zlib deflate")
    try:
        return _jcomp_java(data, tag)
    except Exception as e:
        raise RuntimeError(
            f"JZComp deflate failed for statement stream '{tag}': {e}; "
            "refusing to fall back to Python zlib") from e


def names_bytes(text: str) -> bytes:
    """OpenPDF writes destination names as the low byte of each code point."""
    out = bytearray()
    for ch in text:
        b = ord(ch) & 0xFF
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b == 0x0D:
            out += b"\\r"
        elif b == 0x0A:
            out += b"\\n"
        elif b == 0x09:
            out += b"\\t"
        elif b == 0x08:
            out += b"\\b"
        elif b == 0x0C:
            out += b"\\f"
        else:
            out.append(b)
    return bytes(out)


# Boilerplate anchor names, exactly as JasperReports writes them: multi-line
# fields are joined into one name and double quotes arrive HTML-escaped.
A_BANK_BLOCK = f"{BANK_SITE} \n({BANK_PHONE} \n({BANK_ADDR} \n({BANK_LIC}"
A_DISCLAIMER = " ".join(DISCLAIMER)
A_SIGNATURE = (f"{SIG[0]} \n{SIG[1]}\n"
               + SIG[2].replace('"', "&quot;"))
A_DATE_HDR = f"{HDR_DATE1} {HDR_DATE2}"
A_AMT_HDR_P1 = HDR_AMT + " "        # the page-1 cell carries a trailing space
A_AMT_HDR_P2 = HDR_AMT


def _anchors(name, card, account, balance, stmt_date, per_from, per_to,
             rows, hrows, total_in, total_out):
    """(sort_key, x_text, dest_y, page_obj) for every anchor, in stream order.

    Destinations sit one font size above the text baseline.
    """
    n, h = len(rows), len(hrows)
    tot_y = min(406.29, (TOP_P1 - ROW_H - ROW_H * n) - 30.71)
    amt_y = tot_y - 0.94
    dis_y = tot_y - 33.0
    box_y = min(695.0, (TOP_P2 - ROW_H - ROW_H * h) - 37.0) if h else 695.0
    sig_y = box_y - 9.71

    a = [("JR_PAGE_ANCHOR_0_1", "0", 842 + 10, 1),
         (total_in, "133", amt_y + 12, 1),
         (total_out, "380", amt_y + 12, 1),
         (f"Операции за период с {per_from} по {per_to}", "30", 576.41 + 14, 1),
         (f"Номер карты: (MIR) {card}", "330", 653.26 + 9, 1),
         (f"Номер счета: {account}", "330", 638.26 + 9, 1),
         (f"Доступный остаток* на {stmt_date}: {balance}", "330", 623.26 + 9, 1),
         (BAL_NOTE, "330", 609.23 + 8, 1),
         (name, "31", 653.26 + 9, 1),
         (f"Счет-выписка от {stmt_date}", "26", 687.75 + 26, 1),
         (A_BANK_BLOCK, "30", 762.23 + 8, 1),
         ("Всего поступлений:", "30", tot_y + 10, 1),
         ("Всего списаний:", "293", tot_y + 10, 1),
         (HDR_NO, "45.54", TOP_P1 - 18.71 + 10, 1),
         (A_DATE_HDR, "79", TOP_P1 - 9.71 + 10, 1),
         (A_AMT_HDR_P1, "222", TOP_P1 - 9.71 + 10, 1),
         (HDR_DESC, "350", TOP_P1 - 9.71 + 10, 1)]
    for i, (dt_s, amt_s, desc) in enumerate(rows, 1):
        ty = TOP_P1 - ROW_H * i - 9.71
        num = str(i)
        cx = fmt(COLS_P1[0][0] + COLS_P1[0][1] / 2.0 - text_w(num, "Regular", 10) / 2.0)
        a += [(num, cx, ty + 10, 1), (dt_s, "79", ty + 10, 1),
              (amt_s, "222", ty + 10, 1), (desc, "350", ty + 10, 1)]
    a += [(A_DISCLAIMER, "40", dis_y + 10, 1),
          (FOOTNOTE, "40", dis_y - 52.06 + 8, 1),
          ("1", "296.53", 47.29 + 10, 1),
          ("2", "320", 47.29 + 10, 1),
          ("из", "304.58", 47.29 + 10, 1),
          ("JR_PAGE_ANCHOR_0_2", "0", 842 + 10, 14),
          (f"Заблокированные средства на {stmt_date}:", "30", 808.41 + 14, 14),
          (A_SIGNATURE, "30", sig_y + 10, 14)]
    if h:
        a += [(HDR_NO, "46.54", TOP_P2 - 18.71 + 10, 14),
              (A_DATE_HDR, "80", TOP_P2 - 9.71 + 10, 14),
              (A_AMT_HDR_P2, "223", TOP_P2 - 9.71 + 10, 14),
              (HDR_DESC, "351", TOP_P2 - 9.71 + 10, 14)]
        for i, (dt_s, amt_s, desc) in enumerate(hrows, 1):
            ty = TOP_P2 - ROW_H * i - 9.71
            num = str(i)
            cx = fmt(COLS_P2[0][0] + COLS_P2[0][1] / 2.0 - text_w(num, "Regular", 10) / 2.0)
            a += [(num, cx, ty + 10, 14), (dt_s, "80", ty + 10, 14),
                  (amt_s, "223", ty + 10, 14), (desc, "351", ty + 10, 14)]
    else:
        a += [(NO_OPS, "31", 785.29 + 10, 14)]
    a += [("2", "294.59", 47.29 + 10, 14),
          ("2", "320", 47.29 + 10, 14),
          ("из", "304.58", 47.29 + 10, 14)]
    return a


_DON = open(_DONOR, "rb").read()


def _donor_objects():
    objs = {}
    for m in re.finditer(rb"(\d+) 0 obj\b", _DON):
        n = int(m.group(1)); s = m.end()
        e = _DON.find(b"endobj", s)
        body = _DON[s:e]
        sm = re.search(rb">>\s*stream\r?\n", body)
        if sm:
            sd = body[sm.end():]
            em = sd.rfind(b"endstream")
            objs[n] = (body[:sm.start() + 2], sd[:em].rstrip(b"\r\n"))
        else:
            objs[n] = (body, None)
    return objs


_DOBJ = _donor_objects()
IMG_GRAY = _DOBJ[6]
IMG_RGB = _DOBJ[7]
IMG_JPG = _DOBJ[16]

FD_FLAGS = {"Medium": 32, "Bold": 262176, "Regular": 32}
FD_BBOX = {"Medium": "[-170 -214 1315 916]", "Bold": "[-170 -214 1404 919]",
           "Regular": "[-166 -214 1227 903]"}


def _subset_prefix():
    return "".join(_rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))


def _fix_account_key(acc: str) -> str:
    """Recompute the CBR check digit (position 9) for EXI-Bank BIK 044030889.

    OPT-IN ONLY: genuine accounts exist that do NOT satisfy this check
    (e.g. 40914810700002139661), so callers must not apply this by default.
    No-op for non-20-digit input.
    """
    if not isinstance(acc, str) or len(acc) != 20 or not acc.isdigit():
        return acc
    for d in "0123456789":
        cand = acc[:8] + d + acc[9:]
        s = "889" + cand
        if sum((int(ch) * (7, 1, 3)[i % 3]) % 10 for i, ch in enumerate(s)) % 10 == 0:
            return cand
    return acc


# Backwards-compatible public aliases for the opt-in helper above.
# Applying it is always the caller's explicit choice; generate_statement
# itself never mutates the account unless fix_account_key=True.
fix_account_digit = _fix_account_key
acct_with_key = _fix_account_key


def generate_statement(name, card, account, balance, operations,
                       holds=(), stmt_date=None, period_from=None, period_to=None,
                       total_in=None, total_out=None, created=None,
                       _prefixes=None, _corr=None, _ids=None,
                       fix_account_key=False):
    """Build a complete EXI-BANK statement PDF.

    operations / holds: iterable of (datetime_text, amount_text, description).

    ``account`` is printed verbatim by default (genuine behaviour). Pass
    ``fix_account_key=True`` only to explicitly recompute the CBR check
    digit (9th digit, BIK 044030889) before rendering.
    """
    if fix_account_key:
        account = _fix_account_key(account)
    rows = [tuple(r) for r in operations]
    hrows = [tuple(r) for r in holds]
    if stmt_date is None:
        stmt_date = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
    period_from = period_from or stmt_date
    period_to = period_to or stmt_date
    if total_in is None or total_out is None:
        # Money is Decimal from parse to print: float() used to silently
        # skip comma/NBSP amounts ("1 234,56 RUR" -> totals 0) and the
        # :g format printed 1.23457e+06 for >= 1e6 (no 'e' glyph -> crash).
        from decimal import Decimal, InvalidOperation
        inc = exp = Decimal(0)
        for _, amt, _d in rows:
            v = (amt.replace("RUR", "").replace(" ", "")
                 .replace("\u00a0", "").replace(",", "."))
            try:
                f = Decimal(v)
            except InvalidOperation:
                continue
            if f >= 0:
                inc += f
            else:
                exp += -f
        total_in = total_in or f"+ {inc:f} RUR"
        total_out = total_out or f"- {exp:f} RUR"

    p1 = _page1(name, card, account, balance, stmt_date,
                period_from, period_to, rows, total_in, total_out)
    p2 = _page2(stmt_date, hrows)

    # ── font subsets ──
    u1, u2 = used_cids(p1), used_cids(p2)
    used = {f: (u1[f] | u2[f]) for f in FACES}
    pfx = dict(_prefixes) if _prefixes else {f: _subset_prefix() for f in FACES}
    ttf = {f: build_ttf(f, used[f]) for f in FACES}
    tu = {f: build_tounicode(f, used[f]) for f in FACES}
    warr = {f: build_w(f, used[f]) for f in FACES}

    # ── destinations: first occurrence wins, sorted by the anchor text ──
    dests = {}
    for txt, x, y, pg in _anchors(name, card, account, balance, stmt_date,
                                  period_from, period_to, rows, hrows,
                                  total_in, total_out):
        dests.setdefault(txt, (x, y, pg))
    keys = sorted(dests)
    D = len(keys)
    F = 18 + D
    dest_num = {k: 18 + i for i, k in enumerate(keys)}

    NUM = {
        "Medium": (F, F + 1, F + 2, F + 3),
        "Bold": (F + 4, F + 5, F + 6, F + 7),
        "Regular": (F + 8, F + 9, F + 10, F + 11),
    }
    N_NAMES, N_DESTS, N_CAT, N_INFO, N_XREF = F + 12, F + 13, F + 14, F + 15, F + 16
    SIZE = N_XREF + 1

    # ── ObjStm member objects ──
    stm = {}
    stm[1] = (b"<</Tabs/S/Group<</S/Transparency/Type/Group/CS/DeviceRGB>>/Contents 11 0 R"
              b"/Type/Page/Resources<</ColorSpace<</CS/DeviceRGB>>/ExtGState<</GS1 8 0 R"
              b"/GS2 9 0 R/GS3 10 0 R>>/Font<</F1 2 0 R/F2 3 0 R/F3 4 0 R/F4 5 0 R>>"
              b"/XObject<</img1 7 0 R/img0 6 0 R>>>>/Parent 12 0 R"
              b"/MediaBox[0 0 595 842]>> ")
    stm[14] = (b"<</Tabs/S/Group<</S/Transparency/Type/Group/CS/DeviceRGB>>/Contents 17 0 R"
               b"/Type/Page/Resources<</ColorSpace<</CS/DeviceRGB>>/ExtGState<</GS4 15 0 R>>"
               b"/Font<</F1 2 0 R/F3 4 0 R/F4 5 0 R>>/XObject<</img2 16 0 R>>>>"
               b"/Parent 12 0 R/MediaBox[0 0 595 842]>> ")
    for k in keys:
        x, y, pg = dests[k]
        stm[dest_num[k]] = (f"[{pg} 0 R/XYZ {x} {fmt(y)} 0] ").encode()
    stm[2] = b"<</Subtype/Type1/Type/Font/BaseFont/Helvetica/Encoding/WinAnsiEncoding>> "
    for face, res in (("Medium", 3), ("Bold", 4), ("Regular", 5)):
        ff, fd, cf, tun = NUM[face]
        full = f"{pfx[face]}+MTSSans-{face}"
        stm[fd] = (f"<</Descent -229/CapHeight 690/StemV 80/Type/FontDescriptor"
                   f"/FontFile2 {ff} 0 R/Flags {FD_FLAGS[face]}/FontBBox{FD_BBOX[face]}"
                   f"/FontName/{full}/ItalicAngle 0/Ascent 730>> ").encode()
        stm[cf] = (b"<</DW 1000/Subtype/CIDFontType2/CIDSystemInfo<</Supplement 0"
                   b"/Registry(Adobe)/Ordering(Identity)>>/Type/Font/BaseFont/"
                   + full.encode() + f"/FontDescriptor {fd} 0 R/W [".encode()
                   + warr[face] + b"]/CIDToGIDMap/Identity>> ")
        stm[res] = (b"<</Subtype/Type0/Type/Font/BaseFont/" + full.encode()
                    + b"/Encoding/Identity-H/DescendantFonts[" + f"{cf}".encode()
                    + b" 0 R]/ToUnicode " + f"{tun}".encode() + b" 0 R>> ")
    stm[8] = b"<</ca 0>> "
    stm[9] = b"<</ca 0>> "
    stm[10] = b"<</ca 1>> "
    stm[15] = b"<</ca 1>> "
    stm[12] = b"<</Kids[1 0 R 14 0 R]/Type/Pages/Count 2>> "
    stm[N_NAMES] = (b"<</Names[" + b"".join(
        b"(" + names_bytes(k) + b") " + f"{dest_num[k]} 0 R".encode() for k in keys)
        + b"]>> ")
    stm[N_DESTS] = f"<</Dests {N_NAMES} 0 R>> ".encode()

    order = ([1, 14] + [dest_num[k] for k in keys] + [2]
             + [NUM["Medium"][1], NUM["Medium"][2], 3]
             + [NUM["Bold"][1], NUM["Bold"][2], 4]
             + [NUM["Regular"][1], NUM["Regular"][2], 5]
             + [8, 9, 10, 15, 12, N_NAMES, N_DESTS])
    head = bytearray(); body = bytearray()
    for on in order:
        head += f"{on} {len(body)} ".encode()
        body += stm[on]
    objstm_raw = bytes(head) + bytes(body)
    objstm_first = len(head)
    objstm_comp = jcomp(objstm_raw, "os")

    # ── serialise ──
    if created is None:
        created = datetime.now(timezone.utc).replace(tzinfo=None)
    cd = created.strftime("%Y%m%d%H%M%S")
    # 13th hex char is always '4' in every original (UUIDv4 prefix)
    corr = _corr or uuid.uuid4().hex[:16]

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offs = {}

    def put_stream(num, hdr_inner, payload):
        offs[num] = len(out)
        out.extend(f"{num} 0 obj\n<<".encode() + hdr_inner + b">>stream\n"
                   + payload + b"\nendstream\nendobj\n")

    def put_plain(num, bodyb):
        offs[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode() + bodyb + b"\nendobj\n")

    def inner(hdr):
        """Strip the leading newline + '<<' and the trailing '>>' of a donor header."""
        return hdr.lstrip(b"\r\n")[2:-2]

    put_stream(6, inner(IMG_GRAY[0]), IMG_GRAY[1])
    put_stream(7, inner(IMG_RGB[0]), IMG_RGB[1])
    c1 = jcomp(p1, "p1")
    put_stream(11, f"/Filter/FlateDecode/Length {len(c1)}".encode(), c1)
    put_stream(16, inner(IMG_JPG[0]), IMG_JPG[1])
    c2 = jcomp(p2, "p2")
    put_stream(17, f"/Filter/FlateDecode/Length {len(c2)}".encode(), c2)
    for face in ("Medium", "Bold", "Regular"):
        ff, fd, cf, tun = NUM[face]
        fc = jcomp(ttf[face], "f" + face[0])
        put_stream(ff, f"/Length1 {len(ttf[face])}/Filter/FlateDecode/Length {len(fc)}".encode(), fc)
        tc = jcomp(tu[face], "t" + face[0])
        put_stream(tun, f"/Filter/FlateDecode/Length {len(tc)}".encode(), tc)
    put_plain(N_CAT, f"<</Names {N_DESTS} 0 R/Type/Catalog/Pages 12 0 R"
                     f"/ViewerPreferences<</PrintScaling/AppDefault>>>>".encode())
    put_plain(N_INFO, (f"<</ModDate(D:{cd}Z)/Creator(dbo-print-forms)"
                       f"/CreationDate(D:{cd}Z)/Producer(OpenPDF 1.3.30.jaspersoft.2)"
                       f"/Subject(CorrelationId:{corr})/Author(MTS Bank)"
                       f"/Title(Printable document)>>").encode())
    put_stream(13, f"/Filter/FlateDecode/Type/ObjStm/Length {len(objstm_comp)}"
                   f"/First {objstm_first}/N {len(order)}".encode(), objstm_comp)

    # ── xref stream ──
    xoff = len(out)
    stm_index = {on: i for i, on in enumerate(order)}
    rows_x = bytearray()
    for n in range(SIZE):
        if n == 0:
            rows_x += bytes([0]) + (0).to_bytes(2, "big") + (65535).to_bytes(2, "big")
        elif n in offs:
            rows_x += bytes([1]) + offs[n].to_bytes(2, "big") + (0).to_bytes(2, "big")
        elif n in stm_index:
            rows_x += bytes([2]) + (13).to_bytes(2, "big") + stm_index[n].to_bytes(2, "big")
        else:
            rows_x += bytes([0]) + (0).to_bytes(2, "big") + (65535).to_bytes(2, "big")
    xcomp = jcomp(bytes(rows_x), "xr")
    id1, id2 = _ids if _ids else (secrets.token_hex(16), secrets.token_hex(16))
    out.extend(f"{N_XREF} 0 obj\n<</Info {N_INFO} 0 R/Filter/FlateDecode/Type/XRef"
               f"/W[1 2 2]/Index[0 {SIZE}]/ID [<{id1}><{id2}>]/Root {N_CAT} 0 R"
               f"/Length {len(xcomp)}/Size {SIZE}>>stream\n".encode()
               + xcomp + b"\nendstream\nendobj\n")
    out.extend(f"startxref\n{xoff}\n%%EOF\n".encode())
    return bytes(out)
