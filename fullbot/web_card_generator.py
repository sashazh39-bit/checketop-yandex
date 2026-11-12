# -*- coding: utf-8 -*-
"""Web-form (site) MTS card receipt generator — donor 'Чек от 02.11.2026'.
Same architecture as card_generator.py: patch donor content stream, rebuild
font subsets (used CIDs only, sorted ToUnicode//W, donor checkSumAdjustment),
fresh subset prefixes, rebuilt /Names, Java-deflate streams, xref rebuild.
Fields: '2 ноября 2026, 00:51' date, full PAN masks, '126,00 RUB' amounts,
'225.00 RUB' total (dot!), TR 78... series."""
import re, secrets, pickle, os, zlib, random as _rnd, struct, string, uuid
from datetime import datetime, timedelta, timezone
from mts_build_check15 import jcomp, parse_xref, build_xref, tw, cksum
from mts_green_full import parse_tounicode_cmap, parse_w_array, esc_pdf, unesc_pdf, enc_cid

_DIR = os.path.dirname(os.path.abspath(__file__))
_FW = os.environ.get("MTS_DONOR_CARD_WEB") or r"C:\Users\nemor\Downloads\Telegram Desktop\Чек от 02.11.2026,00_51.pdf"

_cat = pickle.load(open(os.path.join(_DIR, "_glyph_catalog.pkl"), "rb"))
_cid2glyph2 = {v["cid"]: v["raw_glyph"] for v in _cat["f2"].values()}
_cid2ucp2 = {v["cid"]: v["ucp"] for v in _cat["f2"].values()}
_cid2w2c = {v["cid"]: v["width"] for v in _cat["f2"].values()}
_cid2glyph3 = {v["cid"]: v["raw_glyph"] for v in _cat["f3"].values()}
_cid2ucp3 = {v["cid"]: v["ucp"] for v in _cat["f3"].values()}
_cid2w3c = {v["cid"]: v["width"] for v in _cat["f3"].values()}

_data = open(_FW, "rb").read()


def _find_stream(data, obj_n):
    pat = re.compile(rb"(?:^|\n)" + str(obj_n).encode() + rb" 0 obj\n<<(.*?)>>stream\n", re.S)
    m = pat.search(data)
    hdr = m.group(1)
    slen = int(re.search(rb"/Length (\d+)", hdr).group(1))
    comp = data[m.end():m.end() + slen]
    dec = zlib.decompress(comp)
    obj_s = m.start() + 1 if data[m.start():m.start() + 1] == b"\n" else m.start()
    return {"start": m.end(), "len": slen, "dec": dec, "hdr": hdr, "m": m, "obj_start": obj_s}


_cs = _find_stream(_data, 29)
_ob = _find_stream(_data, 31)
_od = _ob["dec"]
_first = int(re.search(rb"/First (\d+)", _ob["hdr"]).group(1))
_hdr_toks = [x for x in _od[:_first].decode("latin1").split(" ") if x]
_pairs = [(int(_hdr_toks[i]), int(_hdr_toks[i + 1])) for i in range(0, len(_hdr_toks), 2)]
_objs_map = {}
for _idx, (_onum, _off) in enumerate(_pairs):
    _end = _pairs[_idx + 1][1] if _idx + 1 < len(_pairs) else len(_od) - _first
    _objs_map[_onum] = _od[_first + _off:_first + _end]

_tu51 = _find_stream(_data, 51)["dec"]
_tu55 = _find_stream(_data, 55)["dec"]
_u2cid_w, _cid2u_w = parse_tounicode_cmap(_tu51)
_u2cid_w3, _cid2u_w3 = parse_tounicode_cmap(_tu55)
_cid2u_w = {c: (ord(v) if isinstance(v, str) else v) for c, v in _cid2u_w.items()}
_cid2u_w3 = {c: (ord(v) if isinstance(v, str) else v) for c, v in _cid2u_w3.items()}

_base_o50 = _objs_map[50]
_base_o54 = _objs_map[54]
_w2 = {**_cid2w2c, **parse_w_array(_base_o50)}
_w3 = {**_cid2w3c, **parse_w_array(_base_o54)}
# char->cid: donor TU first (ground truth for this form), catalog as supplement
_u2cid2 = {**{ch: v["cid"] for ch, v in _cat["f2"].items()}, **_u2cid_w}
_u2cid3 = {**{ch: v["cid"] for ch, v in _cat["f3"].items()}, **_u2cid_w3}
_cid2u2 = {**_cid2ucp2, **_cid2u_w}
_cid2u3 = {**_cid2ucp3, **_cid2u_w3}


def _parse_ttf_tables(font: bytes):
    n = struct.unpack(">H", font[4:6])[0]
    tabs = {}
    for i in range(n):
        o = 12 + i * 16
        tag = font[o:o + 4].decode("latin1")
        ck, off, ln = struct.unpack(">III", font[o + 4:o + 16])
        tabs[tag] = (off, ln, ck)
    return tabs


def _donor_font_parts(obj_n):
    """(sfnt_hdr, head, hhea, hmtx, maxp, head_dir_ck, {gid: glyph_bytes}, num_glyphs)"""
    font = _find_stream(_data, obj_n)["dec"]
    tabs = _parse_ttf_tables(font)
    def tab(t):
        off, ln, ck = tabs[t]
        return font[off:off + ln], ck
    head, head_ck = tab("head")
    hhea, _ = tab("hhea")
    hmtx, _ = tab("hmtx")
    maxp, _ = tab("maxp")
    loca_off, loca_len, _ = tabs["loca"]
    glyf_off, glyf_len, _ = tabs["glyf"]
    num_g = struct.unpack(">H", maxp[4:6])[0]
    loc_fmt = struct.unpack(">h", head[50:52])[0]
    offs = []
    if loc_fmt == 0:
        for i in range(num_g + 1):
            offs.append(struct.unpack(">H", font[loca_off + i * 2:loca_off + i * 2 + 2])[0] * 2)
    else:
        for i in range(num_g + 1):
            offs.append(struct.unpack(">I", font[loca_off + i * 4:loca_off + i * 4 + 4])[0])
    glyphs = {}
    for gid in range(num_g):
        if offs[gid + 1] > offs[gid]:
            glyphs[gid] = font[glyf_off + offs[gid]:glyf_off + offs[gid + 1]]
    return font[:12], head, hhea, hmtx, maxp, head_ck, glyphs, num_g


_f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp, _f2_hck, _f2_donor_glyphs, _f2_numg = _donor_font_parts(48)
_f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp, _f3_hck, _f3_donor_glyphs, _f3_numg = _donor_font_parts(52)
# donor glyphs win; catalog supplements CIDs empty in donor subset
for _cid, _g in _f2_donor_glyphs.items():
    _cid2glyph2[_cid] = _g
for _cid, _g in _f3_donor_glyphs.items():
    _cid2glyph3[_cid] = _g


def _gt(y_s, font=b"2"):
    pat = rb"1 0 0 1 ([\d.]+) " + y_s.encode() + rb" Tm\s*/F" + font + rb" (?:12|20) Tf\s*(?:[\d. ]+) rg\s*\(((?:\\.|[^\\)])*)\)Tj"
    for m in re.finditer(pat, _cs["dec"]):
        if float(m.group(1)) <= 100:
            continue  # label at x=20; values are right-aligned
        x, tj = m.group(1), m.group(2)
        c2u = _cid2u_w if font == b"2" else _cid2u_w3
        raw = unesc_pdf(tj)
        txt = "".join(chr(c2u.get(raw[i] << 8 | raw[i + 1], 0x3F)) for i in range(0, len(raw), 2))
        return x, tj, txt
    raise ValueError(f"value field not found at y={y_s}")


_date_x, _date_tj, _date_txt = _gt("531.35")
_tr_x, _tr_tj, _tr_txt = _gt("453.35")
_pan1_x, _pan1_tj, _pan1_txt = _gt("421.35")
_pan2_x, _pan2_tj, _pan2_txt = _gt("389.35")
_amt_x, _amt_tj, _amt_txt = _gt("357.35")
_comm_x, _comm_tj, _comm_txt = _gt("325.35")
_tot_x, _tot_tj, _tot_txt = _gt("260.58", b"3")

_old_cd_s = re.search(rb"CreationDate\(D:(\d{14})Z\)", _data).group(1).decode()

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _w_f2(s):
    return sum(_w2[_u2cid2[c]] for c in s)


def _w_f3(s):
    return sum(_w3[_u2cid3[c]] for c in s)


_edges = {
    "date": float(_date_x) + _w_f2(_date_txt) * 0.012,
    "tr": float(_tr_x) + _w_f2(_tr_txt) * 0.012,
    "pan1": float(_pan1_x) + _w_f2(_pan1_txt) * 0.012,
    "pan2": float(_pan2_x) + _w_f2(_pan2_txt) * 0.012,
    "amt": float(_amt_x) + _w_f2(_amt_txt) * 0.012,
    "comm": float(_comm_x) + _w_f2(_comm_txt) * 0.012,
    "tot": float(_tot_x) + _w_f3(_tot_txt) * 0.020,
}


def _fmt_coord(val: float) -> bytes:
    v = struct.unpack("f", struct.pack("f", val))[0]
    if abs(v) < 0.000015:
        return b"0"
    neg = v < 0
    d = -v if neg else v
    sign = "-" if neg else ""
    if d < 1.0:
        d += 0.000005
        if d >= 1:
            return (sign + "1").encode("latin1")
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
        return (sign + s).encode("latin1")
    if d <= 32767:
        d += 0.005
        iv = int(d * 100)
        s = str(iv // 100)
        frac = iv % 100
        if frac:
            s += "." + str(frac // 10)
            if frac % 10:
                s += str(frac % 10)
        return (sign + s).encode("latin1")
    return (sign + str(int(d + 0.5))).encode("latin1")


def _names_bytes(s):
    return esc_pdf(bytes(ord(ch) & 0xFF for ch in s))


def commission(amount: int) -> int:
    # MTS Dengi (EXI-Bank) card tariff: 1.5%, minimum 99 RUB. Genuine receipts
    # charge 99 RUB even on 34-1234 RUB transfers; half-up rounding to rubles.
    return max(99, (amount * 15 + 500) // 1000)


def _group(rub: int) -> str:
    return f"{rub:,}".replace(",", "\xa0")


def fmt_amount(rub: int) -> str:
    return f"{_group(rub)},00 RUB"


def fmt_total(rub: int) -> str:
    return f"{_group(rub)}.00 RUB"


def fmt_date(dt: datetime) -> str:
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M}"


# TR 78-series: '78' + 10-digit zero-padded counter; one genuine anchor.
_TRW_ANCHOR_DT = datetime(2026, 11, 2, 0, 51, 0)
_TRW_ANCHOR_VAL = 7861912
_TRW_RATE = 1.5  # guess: payments per second bank-wide on the web channel


_USED_WEB_TR_PATH = os.path.join(_DIR, "used_web_tr.txt")
_used_web_tr = set()
try:
    with open(_USED_WEB_TR_PATH, encoding="utf-8") as _f:
        _used_web_tr = {ln.strip() for ln in _f if ln.strip()}
except Exception:
    pass


import threading as _threading
_WEB_TR_LOCK = _threading.Lock()


def _web_tr_fresh(code: str) -> bool:
    # check+add+append under one lock (2 generation threads in the bot).
    with _WEB_TR_LOCK:
        if code in _used_web_tr:
            return False
        _used_web_tr.add(code)
        try:
            with open(_USED_WEB_TR_PATH, "a", encoding="utf-8") as _f:
                _f.write(code + "\n")
        except Exception:
            pass
        return True


def _make_web_tr(dt: datetime) -> str:
    base = _TRW_ANCHOR_VAL + _TRW_RATE * (dt - _TRW_ANCHOR_DT).total_seconds()
    for _ in range(80):
        v = int(base + _rnd.randint(-2500, 2500)) % 10000000000
        code = "78" + f"{v:010d}"
        if code != _tr_txt and _web_tr_fresh(code):
            return code
    return "78" + f"{int(base) % 10000000000:010d}"


def _extract_used_cids(cs_dec: bytes):
    used_f2, used_f3 = set(), set()
    cur_f = None
    for tok in re.finditer(rb"/F(\d)\s+\d+\s+Tf|\(((?:\\.|[^\\)])*)\)Tj", cs_dec):
        if tok.group(1) is not None:
            cur_f = tok.group(1)
        else:
            raw = unesc_pdf(tok.group(2))
            cids = {(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)}
            if cur_f == b"2":
                used_f2 |= cids
            elif cur_f == b"3":
                used_f3 |= cids
    return used_f2, used_f3


def _build_ttf(used_cids, cid2glyph, sfnt_hdr, head_b, hhea_b, hmtx_b, maxp_b, head_dir_ck, num_g):
    out_glyf = bytearray()
    new_loca = []
    for gid in range(num_g):
        new_loca.append(len(out_glyf))
        if gid in used_cids:
            chunk = cid2glyph.get(gid, b"")
            if len(chunk) % 2:
                chunk += b"\x00"
            out_glyf += chunk
    new_loca.append(len(out_glyf))
    loca_b = b"".join(struct.pack(">H", off // 2) for off in new_loca)
    head_data = bytearray(head_b)
    head_data[8:12] = b"\x00\x00\x00\x00"
    entries = [
        (b"glyf", bytes(out_glyf), cksum(bytes(out_glyf))),
        (b"head", bytes(head_data), head_dir_ck),
        (b"hhea", hhea_b, cksum(hhea_b)),
        (b"hmtx", hmtx_b, cksum(hmtx_b)),
        (b"loca", loca_b, cksum(loca_b)),
        (b"maxp", maxp_b, cksum(maxp_b)),
    ]
    pos = 108
    built = []
    for tag, blob, ck in entries:
        if pos % 4:
            pos += 4 - (pos % 4)
        built.append((tag, pos, blob, ck))
        pos += len(blob)
    font = bytearray(pos)
    font[:12] = sfnt_hdr
    dir_order = sorted(range(len(entries)), key=lambda i: entries[i][0])
    for di, ei in enumerate(dir_order):
        tag, off, blob, ck = built[ei]
        o = 12 + di * 16
        font[o:o + 4] = tag
        struct.pack_into(">III", font, o + 4, ck, off, len(blob))
    for tag, off, blob, ck in built:
        font[off:off + len(blob)] = blob
    head_off = built[1][1]
    font[head_off + 8:head_off + 12] = head_b[8:12]
    return bytes(font)


def _build_tu(used_cids, cid2ucp):
    cids = sorted(used_cids)
    lines = [f"<{c:04x}><{c:04x}><{cid2ucp[c]:04x}>".encode() for c in cids]
    body = b"\n".join(lines)
    return (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CIDSystemInfo\n"
        b"<< /Registry (TTX+0)\n"
        b"/Ordering (T42UV)\n"
        b"/Supplement 0\n"
        b">> def\n"
        b"/CMapName /TTX+0 def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n"
        b"<0000><FFFF>\n"
        b"endcodespacerange\n"
        + f"{len(lines)} beginbfrange\n".encode()
        + body + b"\n"
        b"endbfrange\n"
        b"endcmap\n"
        b"CMapName currentdict /CMap defineresource pop\n"
        b"end end\n"
    )


def _build_w(used_cids, cid2w, base_subobj):
    cids = sorted(used_cids)
    groups = []
    for c in cids:
        w = cid2w[c]
        if groups and groups[-1][0] + len(groups[-1][1]) == c:
            groups[-1][1].append(str(w))
        else:
            groups.append((c, [str(w)]))
    w_inner = "".join(f"{start}[{' '.join(ws)}]" for start, ws in groups).encode()
    return re.sub(rb"/W\s*\[.*?\]\s*/CIDToGIDMap", b"/W [" + w_inner + b"]/CIDToGIDMap", base_subobj, count=1, flags=re.S)


def generate_web_card(amount: int, pan_from: str = "220383******7826",
                      pan_to: str = "220070******8429", dt: datetime = None,
                      tr: str = None, meta_out: dict = None) -> bytes:
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise ValueError(f"card amount must be positive, got {amount!r}")
    if dt is None:
        dt = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    elif getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)

    _now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if dt > _now_msk + timedelta(seconds=60):
        dt = _now_msk
    if abs((dt - _now_msk).total_seconds()) < 300:
        dt = dt - timedelta(seconds=_rnd.randint(150, 300))

    comm = commission(amount)
    total = amount + comm
    date_text = fmt_date(dt)
    amt_str = fmt_amount(amount)
    comm_str = fmt_amount(comm)
    tot_str = fmt_total(total)
    tr_new = tr if tr is not None else _make_web_tr(dt)

    utc = dt - timedelta(hours=3)
    new_cd = utc + timedelta(seconds=_rnd.randint(50, 140))
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if new_cd > _now_utc + timedelta(seconds=15):
        new_cd = _now_utc + timedelta(seconds=_rnd.randint(2, 15))

    x_date = _fmt_coord(_edges["date"] - _w_f2(date_text) * 0.012)
    x_tr = _fmt_coord(_edges["tr"] - _w_f2(tr_new) * 0.012)
    x_pan1 = _fmt_coord(_edges["pan1"] - _w_f2(pan_from) * 0.012)
    x_pan2 = _fmt_coord(_edges["pan2"] - _w_f2(pan_to) * 0.012)
    x_amt = _fmt_coord(_edges["amt"] - _w_f2(amt_str) * 0.012)
    x_comm = _fmt_coord(_edges["comm"] - _w_f2(comm_str) * 0.012)
    x_tot = _fmt_coord(_edges["tot"] - _w_f3(tot_str) * 0.020)

    p = bytearray(_cs["dec"])

    def _patch(p, old_x, y_s, old_tj, new_text, new_x, u2cid):
        p = bytearray(bytes(p).replace(
            b"1 0 0 1 " + old_x + b" " + y_s + b" Tm",
            b"1 0 0 1 " + new_x + b" " + y_s + b" Tm", 1))
        old_blob = b"(" + old_tj + b")Tj"
        anchor = p.find(new_x + b" " + y_s + b" Tm")
        i = p.find(old_blob, anchor)
        new_blob = b"(" + enc_cid(new_text, u2cid) + b")Tj"
        return bytearray(bytes(p)[:i] + new_blob + bytes(p)[i + len(old_blob):])

    p = _patch(p, _date_x, b"531.35", _date_tj, date_text, x_date, _u2cid2)
    p = _patch(p, _tr_x, b"453.35", _tr_tj, tr_new, x_tr, _u2cid2)
    p = _patch(p, _pan1_x, b"421.35", _pan1_tj, pan_from, x_pan1, _u2cid2)
    p = _patch(p, _pan2_x, b"389.35", _pan2_tj, pan_to, x_pan2, _u2cid2)
    p = _patch(p, _amt_x, b"357.35", _amt_tj, amt_str, x_amt, _u2cid2)
    p = _patch(p, _comm_x, b"325.35", _comm_tj, comm_str, x_comm, _u2cid2)
    p = _patch(p, _tot_x, b"260.58", _tot_tj, tot_str, x_tot, _u2cid3)

    raw_29 = bytes(p)
    content_comp = jcomp(raw_29, "cc")

    used_f2, used_f3 = _extract_used_cids(raw_29)
    f2_ttf = _build_ttf(used_f2, _cid2glyph2, _f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp, _f2_hck, _f2_numg)
    f2_tu = _build_tu(used_f2, _cid2u2)
    new_o50 = _build_w(used_f2, _w2, _base_o50)
    f2_comp = jcomp(f2_ttf, "cf2")
    tu2_comp = jcomp(f2_tu, "ctu2")

    f3_ttf = _build_ttf(used_f3, _cid2glyph3, _f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp, _f3_hck, _f3_numg)
    f3_tu = _build_tu(used_f3, _cid2u3)
    new_o54 = _build_w(used_f3, _w3, _base_o54)
    f3_comp = jcomp(f3_ttf, "cf3")
    tu3_comp = jcomp(f3_tu, "ctu3")

    items = [
        ("JR_PAGE_ANCHOR_0_1", b"[1 0 R/XYZ 0 618 0] "),
        ("Итого", b"[1 0 R/XYZ 20 280.58 0] "),
        ("Код транзакции", b"[1 0 R/XYZ 20 465.35 0] "),
        ("Комиссия", b"[1 0 R/XYZ 20 337.35 0] "),
        ("Платеж сформирован", b"[1 0 R/XYZ 20 497.35 0] "),
        ("Получатель", b"[1 0 R/XYZ 20 401.35 0] "),
        ("Сайт «МТС Деньги»", b"[1 0 R/XYZ 246.34 497.35 0] "),
        ("Списание с", b"[1 0 R/XYZ 20 433.35 0] "),
        ("Сумма платежа", b"[1 0 R/XYZ 20 369.35 0] "),
        (amt_str, b"[1 0 R/XYZ " + x_amt + b" 369.35 0] "),
        (tot_str, b"[1 0 R/XYZ " + x_tot + b" 280.58 0] "),
        (date_text, b"[1 0 R/XYZ " + x_date + b" 543.35 0] "),
        (tr_new, b"[1 0 R/XYZ " + x_tr + b" 465.35 0] "),
        (comm_str, b"[1 0 R/XYZ " + x_comm + b" 337.35 0] "),
        (pan_from, b"[1 0 R/XYZ " + x_pan1 + b" 433.35 0] "),
        (pan_to, b"[1 0 R/XYZ " + x_pan2 + b" 401.35 0] "),
    ]
    _seen_names = {}
    for _u, _xyz in items:
        if _u not in _seen_names:
            _seen_names[_u] = _xyz
    si = sorted(_seen_names.items(), key=lambda x: x[0])
    objs = dict(_objs_map)
    for k, (name_key, xyz) in enumerate(si):
        objs[32 + k] = xyz if xyz.endswith(b" ") else xyz + b" "
    for _stale in range(32 + len(si), 48):
        objs.pop(_stale, None)
    names_entries = b"".join(
        b"(" + _names_bytes(name_key) + b") " + str(32 + k).encode() + b" 0 R"
        for k, (name_key, _) in enumerate(si))
    objs[56] = b"<</Names[" + names_entries + b"]>> "
    objs[50] = new_o50
    objs[54] = new_o54

    _pfx_map: dict[bytes, bytes] = {}
    for _blob in objs.values():
        for _m in re.finditer(rb"([A-Z]{6})\+MTSSans", _blob):
            if _m.group(1) not in _pfx_map:
                _pfx_map[_m.group(1)] = "".join(
                    _rnd.choice(string.ascii_uppercase) for _ in range(6)).encode()
    for _on, _blob in list(objs.items()):
        for _old, _new in _pfx_map.items():
            if _old + b"+" in _blob:
                _blob = _blob.replace(_old + b"+", _new + b"+")
        objs[_on] = _blob

    body = bytearray()
    np2 = []
    for on, _ in _pairs:
        if on not in objs:
            continue
        np2.append((on, len(body)))
        body += objs[on]
    rh = (" ".join(f"{a} {b}" for a, b in np2) + " ").encode("latin1")
    new_first = len(rh)
    os_comp = jcomp(bytes(rh + body), "co")

    out = bytearray(_data)

    cs_m = re.search(rb"29 0 obj\n<<(.*?)>>stream\n", out, re.S)
    cs_hdr = re.sub(rb"/Length \d+", b"/Length " + str(len(content_comp)).encode(), cs_m.group(1))
    cs_end = out.find(b"\nendstream\nendobj\n", cs_m.end()) + len(b"\nendstream\nendobj\n")
    out = (out[:cs_m.start(1)] + cs_hdr + b">>stream\n" + content_comp
           + b"\nendstream\nendobj\n" + out[cs_end:])

    os_m = re.search(rb"31 0 obj\n<<(.*?)>>stream\n", out, re.S)
    os_hdr = re.sub(rb"/Length \d+", b"/Length " + str(len(os_comp)).encode(), os_m.group(1))
    # /N must match the rebuilt ObjStm body: duplicate /Names keys
    # (amount 99 == commission, same PAN both sides) collapse objects
    # and the donor /N left MuPDF rejecting the file (0 pages).
    os_hdr = re.sub(rb"/N \d+", b"/N " + str(len(np2)).encode(), os_hdr)
    if new_first != _first:
        os_hdr = re.sub(rb"/First \d+", b"/First " + str(new_first).encode(), os_hdr)
    os_end = out.find(b"\nendstream\nendobj\n", os_m.end()) + len(b"\nendstream\nendobj\n")
    out = (out[:os_m.start(1)] + os_hdr + b">>stream\n" + os_comp
           + b"\nendstream\nendobj\n" + out[os_end:])

    for obj_n, comp, raw_len in [
        (48, f2_comp, len(f2_ttf)),
        (51, tu2_comp, None),
        (52, f3_comp, len(f3_ttf)),
        (55, tu3_comp, None),
    ]:
        s = out.find(f"{obj_n} 0 obj\n".encode())
        e = out.find(b"\nendobj\n", s) + len(b"\nendobj\n")
        if raw_len:
            hdr = f"{obj_n} 0 obj\n<</Length1 {raw_len}/Filter/FlateDecode/Length {len(comp)}>>stream\n".encode()
        else:
            hdr = f"{obj_n} 0 obj\n<</Filter/FlateDecode/Length {len(comp)}>>stream\n".encode()
        out = out[:s] + hdr + comp + b"\nendstream\nendobj\n" + out[e:]

    out = bytearray(bytes(out).replace(
        f"D:{_old_cd_s}Z".encode(),
        f"D:{new_cd.strftime('%Y%m%d%H%M%S')}Z".encode()))

    xr_m = re.search(rb"60 0 obj\n<<(.*?)>>stream\n", out, re.S)
    xr_len = int(re.search(rb"/Length (\d+)", xr_m.group(1)).group(1))
    xr_dec = zlib.decompress(out[xr_m.end():xr_m.end() + xr_len])
    rows = parse_xref(xr_dec)
    found = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", out)}
    new_rows = [(1, found[n], b) if t == 1 else (t, a, b)
                for n, (t, a, b) in enumerate(rows)]
    xref_new = jcomp(build_xref(new_rows), "cx")
    xr_hdr = re.sub(rb"/Length \d+", b"/Length " + str(len(xref_new)).encode(), xr_m.group(1))
    xr_tail = (bytes(out[xr_m.start():xr_m.start(1)]) + xr_hdr + b">>stream\n"
               + xref_new + b"\nendstream\nendobj\nstartxref\n"
               + str(found[60]).encode() + b"\n%%EOF\n")
    out = out[:xr_m.start()] + xr_tail

    nc = uuid.uuid4().hex[:16]
    oc = re.search(rb"CorrelationId:([0-9a-f]{16})", out)
    if oc:
        out[oc.start(1):oc.end(1)] = nc.encode()
    ni1, ni2 = secrets.token_hex(16), secrets.token_hex(16)
    oi = re.search(rb"/ID\s*\[<([0-9a-f]{32})><([0-9a-f]{32})>\]", out)
    if oi:
        out[oi.start(1):oi.end(1)] = ni1.encode()
        out[oi.start(2):oi.end(2)] = ni2.encode()

    if meta_out is not None:
        meta_out.update({"dt": dt, "tr": tr_new, "comm": comm, "total": total,
                         "pan_from": pan_from, "pan_to": pan_to})
    return bytes(out)
