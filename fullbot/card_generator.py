# -*- coding: utf-8 -*-
"""Card Transfer Receipt Generator — mobile-app form (file 31 / file 49).
58-object layout: '•• XXXX' card masks, '1\xa0234,00 ₽' amounts, 630... TR.
- Font subsets: exactly the used CIDs, sorted ToUnicode//W, inherited
  checkSumAdjustment, fresh subset prefix per face per document.
- Operation time aged 50-300s when 'now' so CreationDate lands at ~now.
"""
import re, secrets, pickle, os, zlib, random as _rnd, struct, string, uuid
from datetime import datetime, timedelta, timezone
from mts_build_check15 import jcomp, parse_xref, build_xref, tw, cksum
from mts_green_full import parse_tounicode_cmap, parse_w_array, esc_pdf, unesc_pdf, enc_cid

_DIR = os.path.dirname(os.path.abspath(__file__))
_F31 = os.environ.get("MTS_DONOR_CARD") or r"C:\Users\nemor\Downloads\Telegram Desktop\file (31).pdf"

# ── Load factory raw glyph catalog ──
_cat = pickle.load(open(os.path.join(_DIR, "_glyph_catalog.pkl"), "rb"))
_f2_cat = _cat["f2"]
_f3_cat = _cat["f3"]
_f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp = _cat["f2_tables"]
_f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp = _cat["f3_tables"]

_cid2glyph2 = {v["cid"]: v["raw_glyph"] for v in _f2_cat.values()}
_cid2ucp2 = {v["cid"]: v["ucp"] for v in _f2_cat.values()}
_cid2w2 = {v["cid"]: v["width"] for v in _f2_cat.values()}
_u2cid2_full = {ch: v["cid"] for ch, v in _f2_cat.items()}

_cid2glyph3 = {v["cid"]: v["raw_glyph"] for v in _f3_cat.values()}
_cid2ucp3 = {v["cid"]: v["ucp"] for v in _f3_cat.values()}
_cid2w3 = {v["cid"]: v["width"] for v in _f3_cat.values()}
_u2cid3_full = {ch: v["cid"] for ch, v in _f3_cat.items()}

# ── Parse donor PDF ──
_data = open(_F31, "rb").read()


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

_tu49 = _find_stream(_data, 49)["dec"]
_u2cid, _cid2u = parse_tounicode_cmap(_tu49)

_base_o48 = _objs_map[48]
_base_o52 = _objs_map[52]
_w2 = {**_cid2w2, **parse_w_array(_base_o48)}
_w3 = {**_cid2w3, **parse_w_array(_base_o52)}

_donor_f2_cids = set(parse_w_array(_base_o48).keys())
_donor_f3_cids = set(parse_w_array(_base_o52).keys())


def _gt(y_s):
    pat = rb"1 0 0 1 ([\d.]+) " + y_s.encode() + rb" Tm\s*/F2 12 Tf\s*(?:[\d. ]+) rg\s*\(((?:\\.|[^\\)])*)\)Tj"
    for m in re.finditer(pat, _cs["dec"]):
        if float(m.group(1)) > 100:
            raw = unesc_pdf(m.group(2))
            txt = "".join(_cid2u.get(raw[i] << 8 | raw[i + 1], "?") for i in range(0, len(raw), 2))
            return m.group(1), m.group(2), txt
    return None, None, None


_date_x, _date_tj, _date_txt = _gt("499.35")
_card1_x, _card1_tj, _card1_txt = _gt("453.35")
_card2_x, _card2_tj, _card2_txt = _gt("421.35")
_amt_x, _amt_tj, _amt_txt = _gt("389.35")
_comm_x, _comm_tj, _comm_txt = _gt("357.35")
_tr_x, _tr_tj, _tr_txt = _gt("325.35")

_old_dt_s = re.search(rb"\((\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}) \\\(UTC\+3\\\)\)", _od).group(1).decode()
_old_date_text = _old_dt_s + " (UTC+3)"
_right_edge_date = float(_date_x) + sum(_w2[_u2cid[c]] for c in _old_date_text) * 0.012

_old_cd_s = re.search(rb"CreationDate\(D:(\d{14})Z\)", _data).group(1).decode()
_old_cd = datetime.strptime(_old_cd_s, "%Y%m%d%H%M%S")
_old_dt = datetime.strptime(_old_dt_s, "%d.%m.%Y %H:%M:%S")
_old_utc = _old_dt - timedelta(hours=3)
_cd_delta = _old_cd - _old_utc

_trans_old = _tr_txt

_f3_matches = list(re.finditer(
    rb"1 0 0 1 ([\d.]+) 260\.58 Tm\s*/F3 20 Tf\s*0 0 0 rg\s*\(((?:\\.|[^\\)])*)\)Tj",
    _cs["dec"]))
_old_f3_block = _f3_matches[-1].group(0)
_old_f3_x = _f3_matches[-1].group(1)


def _fmt_coord(val: float) -> bytes:
    """Exact OpenPDF ByteBuffer.formatDouble (float32 + epsilon truncation)."""
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


def card_names_bytes(s):
    raw = bytearray()
    for ch in s:
        raw.append(ord(ch) & 0xFF)
    return esc_pdf(bytes(raw))


def format_card_amount(rub: int) -> str:
    s = f"{rub:,}".replace(",", "\xa0")
    return f"{s},00 \u20bd"


def commission(amount: int) -> int:
    # MTS Dengi (EXI-Bank) card tariff: 1.5%, minimum 99 RUB. Genuine receipts
    # charge 99 RUB even on 34-1234 RUB transfers; half-up rounding to rubles.
    return max(99, (amount * 15 + 500) // 1000)


def _w_f2(s):
    return sum(_w2[_u2cid[c]] for c in s)


def _w_f3(s):
    return sum(_w3[_u2cid3_full[c]] for c in s)


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


def _build_openpdf_ttf(used_cids, cid2glyph, sfnt_hdr, head_b, hhea_b, hmtx_b, maxp_b, head_dir_ck, num_g=351):
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


def _build_openpdf_tounicode(used_cids, cid2ucp):
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


def _build_openpdf_w(used_cids, cid2w, base_subobj):
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


# Card TR suffix behaves as a slow bank-side counter (same lesson as SBP):
# genuine anchors 28.10.2026 13:06:42 -> 759689, 30.10.2026 18:16:13 -> 864102
# give ~0.5455/s. Uniform-random suffixes land outside the plausible band for
# the operation datetime and the checker flags them.
_TR_ANCHOR_DT = datetime(2026, 10, 30, 18, 16, 13)
_TR_ANCHOR_VAL = 864102
_TR_RATE = 104413 / 191371  # suffix units per second


_USED_CARD_TR_PATH = os.path.join(_DIR, "used_card_tr.txt")
_used_card_tr = set()
try:
    with open(_USED_CARD_TR_PATH, encoding="utf-8") as _f:
        _used_card_tr = {ln.strip() for ln in _f if ln.strip()}
except Exception:
    pass


import threading as _threading
_CARD_TR_LOCK = _threading.Lock()


def _card_tr_fresh(code: str) -> bool:
    # check+add+append under one lock: the bot generates in 2 threads
    # and the bare sequence had a duplicate-code race window.
    with _CARD_TR_LOCK:
        if code in _used_card_tr:
            return False
        _used_card_tr.add(code)
        try:
            with open(_USED_CARD_TR_PATH, "a", encoding="utf-8") as _f:
                _f.write(code + "\n")
        except Exception:
            pass
        return True


def _make_card_tr(dt: datetime, target_width: int = None) -> str:
    """prefix6 = last_digit_of_year + DOY(3) + HH(2); suffix = counter value
    extrapolated from the genuine anchors for this datetime (+/- jitter).
    Codes are registered persistently: bank counters never repeat."""
    prefix6 = f"{str(dt.year)[-1]}{dt.timetuple().tm_yday:03d}{dt.hour:02d}"
    base = _TR_ANCHOR_VAL + _TR_RATE * (dt - _TR_ANCHOR_DT).total_seconds()
    for _ in range(80):
        s = int(base + _rnd.randint(-4000, 4000)) % 1000000
        code = prefix6 + f"{s:06d}"
        if code != _trans_old and _card_tr_fresh(code):
            return code
    return prefix6 + f"{int(base) % 1000000:06d}"


def generate_card(amount: int, card_from: str = "8428", card_to: str = "8429",
                  dt: datetime = None, meta_out: dict = None) -> bytes:
    """Generate a 100% OpenPDF-exact mobile-form card transfer receipt PDF."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise ValueError(f"card amount must be positive, got {amount!r}")
    if dt is None:
        dt = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    elif getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)

    # Age a "now" operation a bit so CreationDate lands at ~now (never in the future).
    _now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if dt > _now_msk + timedelta(seconds=60):
        dt = _now_msk
    if abs((dt - _now_msk).total_seconds()) < 120:
        dt = dt - timedelta(seconds=_rnd.randint(25, 90))

    # Strip full PAN mask if caller passed "220383******7826"
    cf4 = re.sub(r"\D", "", card_from)[-4:] or "8428"
    ct4 = re.sub(r"\D", "", card_to)[-4:] or "8429"

    comm = commission(amount)
    total = amount + comm

    dt_str = dt.strftime("%d.%m.%Y %H:%M:%S")
    new_date_text = dt_str + " (UTC+3)"
    utc = dt - timedelta(hours=3)
    new_cd = utc + timedelta(seconds=_rnd.randint(18, 45))
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if new_cd > _now_utc + timedelta(seconds=15):
        new_cd = _now_utc + timedelta(seconds=_rnd.randint(2, 15))

    amt_str = format_card_amount(amount)
    comm_str = format_card_amount(comm)
    total_str = format_card_amount(total)

    card1_text = f"\u2022\u2022 {cf4}"
    card2_text = f"\u2022\u2022 {ct4}"

    tr_new = _make_card_tr(dt, target_width=_w_f2(_trans_old))

    x_date = _fmt_coord(_right_edge_date - _w_f2(new_date_text) * 0.012)
    x_amt = _fmt_coord(355.0 - _w_f2(amt_str) * 0.012)
    x_comm = _fmt_coord(355.0 - _w_f2(comm_str) * 0.012)
    x_card1 = _fmt_coord(355.0 - _w_f2(card1_text) * 0.012)
    x_card2 = _fmt_coord(355.0 - _w_f2(card2_text) * 0.012)
    x_tr = _fmt_coord(355.0 - _w_f2(tr_new) * 0.012)
    x_total = _fmt_coord(355.0 - _w_f3(total_str) * 0.020)

    p = bytearray(_cs["dec"])

    def _patch_f2(p, old_x, y_s, old_tj, new_text):
        new_x = {
            b"499.35": x_date, b"453.35": x_card1, b"421.35": x_card2,
            b"389.35": x_amt, b"357.35": x_comm, b"325.35": x_tr,
        }[y_s]
        p = bytearray(bytes(p).replace(
            b"1 0 0 1 " + old_x + b" " + y_s + b" Tm",
            b"1 0 0 1 " + new_x + b" " + y_s + b" Tm", 1))
        old_blob = b"(" + old_tj + b")Tj"
        anchor = p.find(new_x + b" " + y_s + b" Tm")
        i = p.find(old_blob, anchor)
        new_blob = b"(" + enc_cid(new_text, _u2cid) + b")Tj"
        return bytearray(bytes(p)[:i] + new_blob + bytes(p)[i + len(old_blob):])

    p = _patch_f2(p, _date_x, b"499.35", _date_tj, new_date_text)
    p = _patch_f2(p, _card1_x, b"453.35", _card1_tj, card1_text)
    p = _patch_f2(p, _card2_x, b"421.35", _card2_tj, card2_text)
    p = _patch_f2(p, _amt_x, b"389.35", _amt_tj, amt_str)
    p = _patch_f2(p, _comm_x, b"357.35", _comm_tj, comm_str)
    p = _patch_f2(p, _tr_x, b"325.35", _tr_tj, tr_new)

    total_cid = enc_cid(total_str, _u2cid3_full)
    new_f3 = b"1 0 0 1 " + x_total + b" 260.58 Tm\n/F3 20 Tf\n0 0 0 rg\n(" + total_cid + b")Tj"
    p = bytearray(bytes(p).replace(_old_f3_block, new_f3))

    raw_29 = bytes(p)
    content_comp = jcomp(raw_29, "cc")

    used_f2, used_f3 = _extract_used_cids(raw_29)
    full_f2, full_f3 = used_f2, used_f3

    f2_ttf_raw = _build_openpdf_ttf(full_f2, _cid2glyph2, _f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp, 0x155FC126)
    f2_tu_raw = _build_openpdf_tounicode(full_f2, _cid2ucp2)
    new_o48 = _build_openpdf_w(full_f2, _cid2w2, _base_o48)
    f2_comp = jcomp(f2_ttf_raw, "cf2")
    tu2_comp = jcomp(f2_tu_raw, "ctu2")

    f3_ttf_raw = _build_openpdf_ttf(full_f3, _cid2glyph3, _f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp, 0x15B3C133)
    f3_tu_raw = _build_openpdf_tounicode(full_f3, _cid2ucp3)
    new_o52 = _build_openpdf_w(full_f3, _cid2w3, _base_o52)
    f3_comp = jcomp(f3_ttf_raw, "cf3")
    tu3_comp = jcomp(f3_tu_raw, "ctu3")

    items = [
        ("JR_PAGE_ANCHOR_0_1", b"[1 0 R/XYZ 0 586 0] "),
        ("\u0418\u0442\u043e\u0433\u043e", b"[1 0 R/XYZ 20 280.58 0] "),
        ("\u041a\u0430\u0440\u0442\u0430 \u0437\u0430\u0447\u0438\u0441\u043b\u0435\u043d\u0438\u044f",
         b"[1 0 R/XYZ 20 433.35 0] "),
        ("\u041a\u0430\u0440\u0442\u0430 \u0441\u043f\u0438\u0441\u0430\u043d\u0438\u044f",
         b"[1 0 R/XYZ 20 465.35 0] "),
        ("\u041a\u043e\u0434 \u0442\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u0438",
         b"[1 0 R/XYZ 20 337.35 0] "),
        ("\u041a\u043e\u043c\u0438\u0441\u0441\u0438\u044f",
         b"[1 0 R/XYZ 20 369.35 0] "),
        ("\u0421\u0443\u043c\u043c\u0430",
         b"[1 0 R/XYZ 20 401.35 0] "),
        (amt_str, b"[1 0 R/XYZ " + x_amt + b" 401.35 0] "),
        (total_str, b"[1 0 R/XYZ " + x_total + b" 280.58 0] "),
        (new_date_text, b"[1 0 R/XYZ " + x_date + b" 511.35 0] "),
        (tr_new, b"[1 0 R/XYZ " + x_tr + b" 337.35 0] "),
        (comm_str, b"[1 0 R/XYZ " + x_comm + b" 369.35 0] "),
        (card1_text, b"[1 0 R/XYZ " + x_card1 + b" 465.35 0] "),
        (card2_text, b"[1 0 R/XYZ " + x_card2 + b" 433.35 0] "),
    ]

    _seen_names = {}
    for _u, _xyz in items:
        if _u not in _seen_names:
            _seen_names[_u] = _xyz
    si = sorted(_seen_names.items(), key=lambda x: x[0])
    objs = dict(_objs_map)
    for k, (name_key, xyz) in enumerate(si):
        objs[32 + k] = xyz if xyz.endswith(b" ") else xyz + b" "
    for _stale in range(32 + len(si), 46):
        objs.pop(_stale, None)

    names_entries = b"".join(
        b"(" + card_names_bytes(name_key) + b") " + str(32 + k).encode() + b" 0 R"
        for k, (name_key, _) in enumerate(si))
    objs[54] = b"<</Names[" + names_entries + b"]>> "
    objs[48] = new_o48
    objs[52] = new_o52

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
    # (amount 99 == commission "99,00 ₽", same card last4 both sides)
    # collapse objects and the donor /N left MuPDF rejecting the file.
    os_hdr = re.sub(rb"/N \d+", b"/N " + str(len(np2)).encode(), os_hdr)
    if new_first != _first:
        os_hdr = re.sub(rb"/First \d+", b"/First " + str(new_first).encode(), os_hdr)
    os_end = out.find(b"\nendstream\nendobj\n", os_m.end()) + len(b"\nendstream\nendobj\n")
    out = (out[:os_m.start(1)] + os_hdr + b">>stream\n" + os_comp
           + b"\nendstream\nendobj\n" + out[os_end:])

    for obj_n, comp, raw_len in [
        (46, f2_comp, len(f2_ttf_raw)),
        (49, tu2_comp, None),
        (50, f3_comp, len(f3_ttf_raw)),
        (53, tu3_comp, None),
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

    xr_m = re.search(rb"58 0 obj\n<<(.*?)>>stream\n", out, re.S)
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
               + str(found[58]).encode() + b"\n%%EOF\n")
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
                         "card_from": cf4, "card_to": ct4})
    return bytes(out)
