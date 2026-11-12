# -*- coding: utf-8 -*-
"""MTS SBP Receipt Generator вЂ” 100% OpenPDF-Exact Binary Factory Subsetting.
- Zero fontTools: builds F2 (53, 56, sub-obj 55) and F3 (57, 60, sub-obj 59)
  using raw factory glyph slices and exact OpenPDF table layout/checksums.
- Exact CID subsetting: only CIDs actually rendered in 29 0 obj are included
  (zero orphan glyphs, sorted lowercase ToUnicode CMaps, grouped /W arrays).
- Non-breaking space U+00A0 (CID 97, width 200) as thousands separator in amounts \u22651000\u20bd.
- Exact OpenPDF float formatting (ByteBuffer.formatDouble: strips trailing zeros).
- Unique random 6-letter font subset prefixes + UUIDv4 CorrelationId (13th hex = '4').
"""
import re, secrets, pickle, os, struct, string, threading, uuid, random as _rnd
from datetime import datetime, timedelta, timezone
from mts_build_check15 import find_stream, parse_xref, build_xref, jcomp, tw, cksum
from mts_green_full import (
    W_F2L, W_F3L, parse_tounicode_cmap, parse_w_array, esc_pdf, unesc_pdf,
    enc_cid, names_bytes, enc_amt, w_f2_amt, w_f3_amt, pick_tm, get_objstm_obj
)

_DIR = os.path.dirname(os.path.abspath(__file__))
_F25 = os.environ.get("MTS_DONOR_SBP")
if not _F25 or not os.path.exists(_F25):
    _LINUX_DONOR = "/opt/mts-bot/donors/file25.pdf"
    _WIN_DONOR = "C:\\Users\\nemor\\OneDrive\\Desktop\\newmtsorig_\u043d\u0435\u043e\u0441\u043d\u043e\u0432\u043d\u0430\u044f  \u043f\u0430\u043f\u043a\u0430 \u043e\u0440\u0438\u0433\u0438\u043d\u0430\u043b\u043e\u0432\\file (25).pdf"
    _F25 = _LINUX_DONOR if os.path.exists(_LINUX_DONOR) else _WIN_DONOR

# в”Ђв”Ђ Load 100% factory raw glyph catalog в”Ђв”Ђ
with open(os.path.join(_DIR, "_glyph_catalog.pkl"), "rb") as _cat_fh:
    _cat = pickle.load(_cat_fh)
_f2_cat = _cat["f2"]  # char -> {cid, ucp, width, raw_glyph}
_f3_cat = _cat["f3"]  # char -> {cid, ucp, width, raw_glyph}
_f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp = _cat["f2_tables"]
_f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp = _cat["f3_tables"]

_cid2glyph2 = {v["cid"]: v["raw_glyph"] for v in _f2_cat.values()}
_cid2ucp2   = {v["cid"]: v["ucp"]       for v in _f2_cat.values()}
_cid2w2     = {v["cid"]: v["width"]     for v in _f2_cat.values()}
_full_u2cid = {ch: v["cid"] for ch, v in _f2_cat.items()}
_full_w2    = dict(_cid2w2)

_cid2glyph3 = {v["cid"]: v["raw_glyph"] for v in _f3_cat.values()}
_cid2ucp3   = {v["cid"]: v["ucp"]       for v in _f3_cat.values()}
_cid2w3     = {v["cid"]: v["width"]     for v in _f3_cat.values()}

# в”Ђв”Ђ Parse base donor f25 в”Ђв”Ђ
with open(_F25, "rb") as _f25_fh:
    _data = _f25_fh.read()
_cs = find_stream(_data, 29)
_ob = find_stream(_data, 31)
_od = _ob["dec"]
_first = int(re.search(rb"/First\s+(\d+)", _ob["hdr_m"].group(1)).group(1))

_base_u2cid, _base_cid2u = parse_tounicode_cmap(find_stream(_data, 56)["dec"])
_base_w2 = parse_w_array(get_objstm_obj(_od, _first, 55))

# Donor text fields
_old_dt_s = re.search(rb"\((\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}) \\\(UTC\+3\\\)\)", _od).group(1).decode()
_old_dt = datetime.strptime(_old_dt_s, "%d.%m.%Y %H:%M:%S")
_old_cd_s = re.search(rb"CreationDate\(D:(\d{14})Z\)", _data).group(1).decode()

_d_m = re.search(rb"1 0 0 1 ([\d.]+) 643\.35 Tm\s*/F2 12 Tf\s*0\.50196 0\.50196 0\.50196 rg\s*\(((?:\\.|[^\\)])*)\)Tj", _cs["dec"])
_x_date_old = _d_m.group(1)
_old_date_text = _old_dt_s + " (UTC+3)"
# Genuine right edge is EXACTLY 355.0 (verified on 15 genuine x-positions from the
# 07.11.2026 originals: every field x = 355.0 - sum(w)*0.012 with zero misses).
# The donor-derived 355.002 flipped 189.604 -> "189.61" instead of genuine "189.6".
_right_edge_date = 355.0

_sbp_full = re.search(rb"\(([AB][0-9A-Z]{31})\)", _od).group(1).decode()
_flag_ch, _node, _tail, _route = _sbp_full[14], _sbp_full[15], _sbp_full[16], _sbp_full[17:]

# Route is DOY-specific: (letter, suffix, valid 4-digit gateway codes, valid nodes)
# Extracted from 14 genuine originals вЂ” gateway codes rotate per date period.
_ROUTE_BY_DOY = {
    300: ("G", "0011871001", ["1000", "1001", "1013", "1014", "1016", "1017", "1018"], list("VEWTI3")),
    301: ("B", "0011880703", ["1001", "1004", "1007", "1013", "1014", "1016"], list("L4ODXH")),
    302: ("B", "0011880703", ["1004"], list("4")),
    303: ("B", "0011880703", ["1004"], list("4")),
    304: ("B", "0011881102", ["1003", "1004"], list("34")),
    # Green-proven invariant across Oct 28-31 + Nov 01-02 (Unknown29, 5644):
    # route4 is '1004', nodes in {0,3,4,D,E}. Other route4 values (1000/1014/
    # 1016/1018) failed the checker on every recent test receipt.
    305: ("B", "0011881102", ["1004"], list("034DE")),
    306: ("B", "0011881102", ["1004"], list("034DE")),
    307: ("B", "0011881102", ["1004", "1004", "1003"], list("034DET")),
    308: ("B", "0011881102", ["1002", "1003", "1004"], list("034DETO")),
}

def _route_for_doy(yday: int):
    """Return (letter, suffix, route4_pool, node_pool) for a day-of-year, using nearest known DOY."""
    if yday in _ROUTE_BY_DOY:
        return _ROUTE_BY_DOY[yday]
    return _ROUTE_BY_DOY[min(_ROUTE_BY_DOY, key=lambda d: abs(d - yday))]

# NSPK era flip at MTS (2026-11-06, inside the attested window):
# last genuine old-era id  B/0011881102 = A63101606221580D0B10040011881102 (16:06:22Z),
# first genuine new-era id G/0011890101 = A63102210428290C0G10020011890101 (22:10:42Z).
# Flip is keyed by the ID's UTC instant, NOT by DOY (both eras occur on DOY 310).
_ERA_FLIP_UTC = datetime(2026, 11, 6, 20, 0, 0)

# New era (attested 07-08.11.2026 originals incl. evening 23:05/23:07):
# route G/0011890101. The genuine gateway/node set WIDENS over time
# (A-side: 1000,1001,1002,1007; B-side: 1011,1012,1013; nodes 4,B,C,D,E,I,R),
# so the generator emits exactly the observed subset while the validator
# accepts the full prefix-bound NSPK pools for MTS era-G.
_GATEWAYS_NEW = {"A": ["1000", "1001", "1002", "1007"], "B": ["1011", "1012", "1013"]}
_NODES_NEW = ["4", "B", "C", "D", "E", "I", "R"]
_m_sbp1 = pick_tm(_cs["dec"], b"341.35"); _x_sbp1_old = _m_sbp1.group(1)
_m_sbp2 = re.search(rb"1 0 0 1 ([\d.]+) 325\.77 Tm\n0 0 0 rg\n\(((?:\\.|[^\\)])*)\)Tj", _cs["dec"])
_x_sbp2_old = _m_sbp2.group(1)
_n_line1 = len(unesc_pdf(_m_sbp1.group(2))) // 2
_w_line1_target = sum(_base_w2[_base_u2cid[c]] for c in _sbp_full[:_n_line1])
# Same hard 355.0 edge for the SBP line-1 (donor-derived 355.002 was off by 2 ULP
# of formatDouble's 0.005 rounding step; see _right_edge_date note above).
_right_edge_sbp = 355.0

# Pre-bucket valid 3-digit ms values (digits 1..9 only, no '0' anywhere in ms, not all 3 identical) by F2 digit width
_ms_by_w = {}
for _m in range(111, 1000):
    _ms_s = f"{_m:03d}"
    if "0" in _ms_s or len(set(_ms_s)) == 1:
        continue
    _ms_by_w.setdefault(sum(W_F2L[int(c)] for c in _ms_s), []).append(_ms_s)

_trans_old = re.search(rb"\((019\d{9})\)", _od).group(1).decode()
_w12 = tw(_trans_old)

_m_su = pick_tm(_cs["dec"], b"437.35")
_m_it = [m for m in re.finditer(rb"1 0 0 1 ([\d.]+) 260\.58 Tm\s*/F3 20 Tf\s*0 0 0 rg\s*\(((?:\\.|[^\\)])*)\)Tj", _cs["dec"]) if float(m.group(1)) > 100][0]
_x_su_old, _x_it_old = _m_su.group(1), _m_it.group(1)
_old_amt_raw = unesc_pdf(_m_su.group(2))
_old_amt_text = "".join(_base_cid2u.get(_old_amt_raw[i]<<8|_old_amt_raw[i+1],"?") for i in range(0,len(_old_amt_raw),2))

_acc_old = re.search(rb"\((\d{5} \d{5} \d{5} \d{5})\)", _od).group(1).decode()
_acc_x_old = pick_tm(_cs["dec"], b"469.35").group(1)
# Genuine account right edge (digits-only basis) is exactly 346.0 вЂ” the
# donor-derived 346.002 flips the last digit on some accounts (210.07 vs 210.06).
_acc_right_edge = 346.0

def _gv(y):
    m = pick_tm(_cs["dec"], y); r = unesc_pdf(m.group(2))
    return m.group(1), m.group(2), "".join(_base_cid2u.get(r[i]<<8|r[i+1],"?") for i in range(0,len(r),2))
_xno,_tno,_ = _gv(b"597.35"); _xpo,_tpo,_ = _gv(b"565.35"); _xbo,_tbo,_ = _gv(b"533.35")
_x_tr_old = pick_tm(_cs["dec"], b"373.35").group(1)

_base_o55 = get_objstm_obj(_od, _first, 55)
_base_o59 = get_objstm_obj(_od, _first, 59)

# Donor CID sets вЂ” reference only (NOT included in output; only used CIDs are subset)
_donor_f2_cids = set(_base_w2.keys())
_donor_f3_w = parse_w_array(_base_o59)
_donor_f3_cids = set(_donor_f3_w.keys())


_used_tr_codes = {
    _trans_old,
    "019124502670", "019124782560", "019124785620", "019124785920", "019159432020",
    "019159768630", "019159768930", "019159769830", "019159842260", "019160424140",
    "019209090070", "019209678080", "019209680780", "019209680870", "019209687080",
    "019209687800", "019209756660", "019209756960", "019209759660", "019209759960",
    "019209759990", "019209765690", "019209766650", "019209907000", "019209919980",
    "019209930190", "019209930910", "019209931060", "019209931090", "019209931900",
    "019209944980", "019209946840", "019209948460", "019209948490", "019209948640",
    "019209948940", "019209949840", "019209964480", "019209964840", "019209968440",
    "019209975990", "019209976590", "019209984460", "019209997590", "019211022340",
    "019211668740", "019211670340", "019211670430", "019211678390", "019211692860",
    "019211692890", "019211692980", "019211697830", "019211836790", "019211984220",
    "019242089390", "019242360300", "019242360680", "019242390890", "019242688690",
    "019242973090", "019242983060", "019242983090", "019242983600", "019242983900",
    "019242986030", "019242986680", "019242986860", "019242986890", "019242986980",
    "019242988690", "019242988960", "019242988990", "019242989030", "019242989300",
    "019242989680", "019242989860", "019242989980", "019242993080", "019242993800",
    "019242996010", "019242998300", "019242998980", "019242999880", "019243009450",
    "019243022690", "019243032080", "019275806880", "019275880030", "019276058880",
    "019276588080", "019276885800", "019276888500", "019278058300", "019278889500",
    "019279880580", "019280370580", "019280429630", "019280503780", "019280649320",
    "019280687580", "019280785860", "019280878950", "019282304990", "019282864690",
    "019284268690", "019284309260", "019292004730", "019292269140", "019292415500",
    "019292616420", "019292819100", "019292948070", "019292962410", "019293381490",
    "019293482690", "019320087850", "019320264980", "019320943200", "019323588440",
    "019330606730", "019330668790", "019332024090", "019332200490", "019332260040",
    "019333790900", "019337069980", "019338448250", "019339700930", "019340262030",
    "019340268920", "019340620320", "019340923200",
}
_USED_TR_FILE = os.path.join(_DIR, "mts_used_tr.txt")
if os.path.exists(_USED_TR_FILE):
    try:
        with open(_USED_TR_FILE, "r", encoding="utf-8") as _tr_fh:
            for _ln in _tr_fh:
                _s = _ln.strip()
                if _s.isdigit():
                    # Помеченные строки якорей ("A <дата> <время> <код> # ...")
                    # сюда не попадают — их читает _load_saved_refs().
                    _used_tr_codes.add(_s)
    except Exception:
        pass

# Guards _used_tr_codes/_TR_REFS + _USED_TR_FILE: candidate search, anchor
# insertion and file appends must be atomic, otherwise two threads can be
# issued the same transaction code or lose an anchor.
_TR_LOCK = threading.RLock()

def _save_used_tr(code: str):
    _used_tr_codes.add(code)
    try:
        with open(_USED_TR_FILE, "a", encoding="utf-8") as _f:
            _f.write(code + "\n")
    except Exception:
        pass

# в”Ђв”Ђ Available charset в”Ђв”Ђ
AVAIL_UPPER = sorted(c for c in _full_u2cid if '\u0410' <= c <= '\u042F')
AVAIL_LOWER = sorted(c for c in _full_u2cid if '\u0430' <= c <= '\u044F')
AVAIL_YO = '\u0451' in _full_u2cid

def check_name(text: str) -> list[str]:
    return [c for c in text if c not in _full_u2cid and c not in (' ', '-', '.', '(', ')')]

def format_amount(rub: int) -> str:
    if isinstance(rub, float) and rub == int(rub):
        rub = int(rub)
    if not isinstance(rub, int):
        # Genuine MTS amounts are whole rubles (",00 ₽"); a fractional
        # amount used to render garbage like "1 234.56,00 ₽".
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(str(rub).strip().replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            raise ValueError(f"MTS amount must be whole rubles, got {rub!r}") from None
        if d != d.to_integral_value():
            raise ValueError(f"MTS amount must be whole rubles, got {rub!r}")
        rub = int(d)
    return f"{rub:,}".replace(",", "\xa0") + ",00 \u20bd"

def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]

def _fmt_coord(val: float) -> bytes:
    """Exact OpenPDF ByteBuffer.formatDouble (float32 + epsilon truncation),
    as proven byte-exact in the statement generator."""
    v = _f32(val)
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

def _extract_used_cids(cs_dec: bytes):
    """Return (used_f2_cids, used_f3_cids) from all Tj strings in 29 0 obj."""
    used_f2 = set()
    used_f3 = set()
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
    """Build TrueTypeFontSubSet binary вЂ” exact OpenPDF physical order, preserved head/hhea/maxp."""
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
    head_data[8:12] = b'\x00\x00\x00\x00'

    # OpenPDF physical order: glyf FIRST, then head, hhea, hmtx, loca, maxp
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
        font[o:o+4] = tag
        struct.pack_into(">III", font, o + 4, ck, off, len(blob))
    for tag, off, blob, ck in built:
        font[off:off+len(blob)] = blob

    # Genuine OpenPDF subsets INHERIT checkSumAdjustment from the master font
    # (F2=0x5A86FC34, F3=0x5EF1CA80 in every genuine receipt) - it is never
    # recomputed per subset. Writing the spec value (0xB1B0AFBA - sum) leaves
    # a 4-byte fingerprint that differs from every genuine receipt.
    head_off = built[1][1]  # head is entries[1] (after glyf)
    font[head_off + 8:head_off + 12] = head_b[8:12]
    return bytes(font)

def _build_openpdf_tounicode(used_cids, cid2ucp, donor_cids=None):
    """Build ToUnicode CMap: donor entries first (sorted), new entries appended (GREEN pattern)."""
    if donor_cids is not None:
        donor_sorted = sorted(c for c in used_cids if c in donor_cids)
        new_cids = sorted(c for c in used_cids if c not in donor_cids)
        cids = donor_sorted + new_cids
    else:
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

def _build_openpdf_w(used_cids, cid2w, base_subobj, donor_cids=None):
    """Build /W array: donor groups preserved + new CIDs appended (GREEN pattern)."""
    if donor_cids is not None:
        donor_sorted = sorted(c for c in used_cids if c in donor_cids)
        new_cids = sorted(c for c in used_cids if c not in donor_cids)
        # Build donor portion with merged consecutive groups
        groups = []
        for c in donor_sorted:
            w = cid2w[c]
            if groups and groups[-1][0] + len(groups[-1][1]) == c:
                groups[-1][1].append(str(w))
            else:
                groups.append((c, [str(w)]))
        w_inner = "".join(f"{start}[{' '.join(ws)}]" for start, ws in groups).encode()
        # Append new CIDs as separate groups (space before each)
        for c in new_cids:
            w_inner += f" {c}[{cid2w[c]}]".encode()
    else:
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

def _jasper_wrap_sbp(sbp_full: str, u2cid: dict, w2: dict, max_w: int = 13916):
    """Wrap 32-char SBP string at the exact character where width exceeds max_w (167.0pt / 0.012 = 13916.67)."""
    acc_w = 0
    split_idx = len(sbp_full)
    for idx, ch in enumerate(sbp_full):
        cw = w2[u2cid[ch]]
        if acc_w + cw > max_w:
            split_idx = idx
            break
        acc_w += cw
    l1 = sbp_full[:split_idx]
    l2 = sbp_full[split_idx:]
    w1 = sum(w2[u2cid[c]] for c in l1)
    w2_sum = sum(w2[u2cid[c]] for c in l2)
    return l1, l2, w1, w2_sum

_TR_REFS = [
    (datetime(2026, 10, 27, 9, 2, 36), 19209680870),
    (datetime(2026, 10, 27, 10, 0, 35), 19211692890),
    (datetime(2026, 10, 28, 7, 29, 57), 19242983060),
    (datetime(2026, 10, 28, 7, 32, 29), 19243032080),
    (datetime(2026, 11, 1, 0, 1, 29), 19361457350),
    (datetime(2026, 11, 1, 21, 16, 32), 19388519240),
    (datetime(2026, 11, 3, 16, 51, 0), 19445540010),
    (datetime(2026, 11, 4, 9, 32, 27), 19461421000),
    # Fresh new-era anchors (07.11.2026 originals; op time MSK):
    (datetime(2026, 11, 7, 1, 10, 48), 19544683700),
    (datetime(2026, 11, 7, 1, 16, 3), 19544730490),
    (datetime(2026, 11, 7, 13, 16, 57), 19557892470),
    (datetime(2026, 11, 7, 13, 20, 31), 19557999930),
    # Fresh 08.11 bank-made transfers (three same-minute originals; op time MSK):
    (datetime(2026, 11, 8, 18, 58, 2), 19593200180),
    (datetime(2026, 11, 8, 18, 58, 22), 19593206750),
    (datetime(2026, 11, 8, 18, 58, 43), 19593214350),
    # Fresh evening transfers (08.11 23:05/23:07 MSK):
    (datetime(2026, 11, 8, 23, 5, 34), 19597263140),
    (datetime(2026, 11, 8, 23, 7, 46), 19597284220),
]

# Genuine segment rates run 304-502/s (night ~150-300/s, daytime ~420-502/s;
# 07.11 13:16:57 -> 13:20:31 realized slope = 107460/214s = 502.15/s).
# Это теперь ЗАПАСНАЯ константа: рабочая скорость оценивается окном по
# последним якорям (_estimate_tr_rate), см. anchor_lib.
_TR_RATE = 420

# ────────────────── авто-переякорение (anchor_lib) ──────────────────
# Якоря пополняются из присланных оригиналов (бот, «обновить якорь») и
# сохраняются в mts_used_tr.txt помеченными строками
# "A <дата> <время> <код> # note" (append-only; загрузчик выше их
# игнорирует благодаря isdigit-фильтру).
_RATE_WINDOW = 3          # скорость = медиана наклонов по последним N якорям
_RATE_MIN_SPAN = 180.0    # учитываем свежие кластеры от 3 минут (13:16->13:20 = 214с)
_TR_RATE_MIN, _TR_RATE_MAX = 100.0, 900.0   # правдоподобные наклоны, ед/с

try:
    import anchor_lib as _AL
except Exception:
    _AL = None

def _load_saved_refs():
    """Якоря, добавленные в прошлые запуски (из _USED_TR_FILE)."""
    if _AL is None:
        return []
    try:
        return _AL.load_anchors(_USED_TR_FILE)
    except Exception:
        return []

def _plausible_in_row(row, t, n, rmin, rmax):
    """Правдоподобен ли якорь (t, n) относительно соседей ряда row:
    наклоны сегментов к ближайшим соседям по времени в [rmin, rmax].
    Отсекает мусорные/обрезанные строки файла при загрузке."""
    prev = [r for r in row if r[0] < t]
    nxt = [r for r in row if r[0] > t]
    if prev:
        seg = (n - prev[-1][1]) / (t - prev[-1][0]).total_seconds()
        if not (rmin <= seg <= rmax):
            return False
    if nxt:
        seg = (nxt[0][1] - n) / (nxt[0][0] - t).total_seconds()
        if not (rmin <= seg <= rmax):
            return False
    return True

def _merge_saved_refs():
    """Дополнить _TR_REFS сохранёнными якорями; мусор отфильтровывается
    теми же правилами, что и в add_anchor."""
    global _TR_REFS
    merged = list(_TR_REFS)
    for t, n in _load_saved_refs():
        if any(t == rt or n == rn for rt, rn in merged):
            continue
        if not f"{n:012d}".startswith("019"):
            continue
        if not _plausible_in_row(merged, t, n, _TR_RATE_MIN, _TR_RATE_MAX):
            continue
        merged.append((t, n))
        merged.sort(key=lambda r: r[0])
    _TR_REFS = merged

_merge_saved_refs()

def _estimate_tr_rate() -> float:
    """Рабочая скорость ряда: медиана наклонов последних сегментов
    (окно _RATE_WINDOW якорей); запасной вариант — константа _TR_RATE."""
    pts = [(r[0], r[1]) for r in _TR_REFS]
    r = None
    if _AL is not None:
        r = _AL.estimate_rate(pts, window=_RATE_WINDOW,
                              min_span_s=_RATE_MIN_SPAN,
                              lo=_TR_RATE_MIN, hi=_TR_RATE_MAX, default=None)
    else:
        rates = []
        for a, b in zip(pts[-_RATE_WINDOW:], pts[-_RATE_WINDOW:][1:]):
            ds = (b[0] - a[0]).total_seconds()
            if ds >= _RATE_MIN_SPAN and b[1] > a[1]:
                rates.append((b[1] - a[1]) / ds)
        if rates:
            rates.sort()
            r = min(_TR_RATE_MAX, max(_TR_RATE_MIN, rates[len(rates) // 2]))
    return float(r if r is not None else _TR_RATE)

def current_rate() -> float:
    """Скорость, которая применилась бы сейчас (для статуса в боте)."""
    return _estimate_tr_rate()

def get_refs() -> list:
    """Текущий ряд опорных точек (копия): [(dt_msk, код), ...]."""
    return list(_TR_REFS)

def add_anchor(dt_msk: datetime, tr_code, note: str = ""):
    """Зарегистрировать свежий якорь из присланного оригинала и сохранить
    его в mts_used_tr.txt (append-only). Возвращает (ok, сообщение)."""
    global _TR_REFS
    try:
        num = int(str(tr_code).strip())
    except Exception:
        return False, f"код транзакции не число: {tr_code!r}"
    if not f"{num:012d}".startswith("019"):
        return False, f"код вне ряда 019xxxxxxxxx0: {tr_code}"
    now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if dt_msk > now + timedelta(minutes=10):
        return False, f"время якоря в будущем ({dt_msk:%d.%m.%Y %H:%M:%S})"
    if dt_msk < now - timedelta(days=45):
        return False, "якорь старше 45 суток — пользы нет"
    # Валидация + мутация _TR_REFS + append в файл атомарно под локом,
    # иначе два потока могут потерять/продублировать якорь.
    with _TR_LOCK:
        for rt, rn in _TR_REFS:
            if rn == num:
                if abs((rt - dt_msk).total_seconds()) < 2:
                    return False, "такой якорь уже есть"
                return False, (f"конфликт: код {num:012d} уже привязан "
                               f"к {rt:%d.%m %H:%M:%S}")
        prev = [r for r in _TR_REFS if r[0] < dt_msk]
        nxt = [r for r in _TR_REFS if r[0] > dt_msk]
        if prev:
            t0, n0 = prev[-1]
            seg = (num - n0) / (dt_msk - t0).total_seconds()
            if not (_TR_RATE_MIN <= seg <= _TR_RATE_MAX):
                return False, (f"неправдоподобный наклон {seg:.1f}/с от якоря "
                               f"{t0:%d.%m %H:%M:%S} {n0:012d} — отклонено")
        if nxt:
            t1, n1 = nxt[0]
            seg = (n1 - num) / (t1 - dt_msk).total_seconds()
            if not (_TR_RATE_MIN <= seg <= _TR_RATE_MAX):
                return False, (f"неправдоподобный наклон {seg:.1f}/с до якоря "
                               f"{t1:%d.%m %H:%M:%S} {n1:012d} — отклонено")
        _TR_REFS = sorted(_TR_REFS + [(dt_msk, num)], key=lambda r: r[0])
        saved = False
        if _AL is not None:
            saved = _AL.append_anchor(_USED_TR_FILE, dt_msk, num, note)
        if not saved:  # запасной путь без anchor_lib
            try:
                with open(_USED_TR_FILE, "a", encoding="utf-8") as f:
                    f.write(f"A {dt_msk:%Y-%m-%d %H:%M:%S} {num}"
                            + (f" # {' '.join(str(note).split())}" if note else "") + "\n")
                saved = True
            except Exception:
                pass
        return True, (f"наклонов в окне: {min(len(_TR_REFS), _RATE_WINDOW)}, "
                      f"скорость теперь {current_rate():.1f}/с"
                      + ("" if saved else " (не сохранён в файл — только в память!)"))

def _pick_trans_code(dt: datetime, min_tr: str = None) -> str:
    """Pick a fresh 12-digit transaction code (019...0) using closest genuine reference point."""
    with _TR_LOCK:
        return _pick_trans_code_locked(dt, min_tr=min_tr)

def _pick_trans_code_locked(dt: datetime, min_tr: str = None) -> str:
    best = min(_TR_REFS, key=lambda r: abs((dt - r[0]).total_seconds()))
    ref_dt, ref_tr = best
    delta_s = (dt - ref_dt).total_seconds()
    rate = _estimate_tr_rate()
    est_val = ref_tr + int(delta_s * rate)
    noise = _rnd.randint(-5000, 5000) * 10
    est_val += noise
    est_s = f"{est_val:012d}"
    if not est_s.startswith("019"):
        est_val = ref_tr + int(delta_s * rate)
    base = (est_val // 10) * 10
    for offset in range(0, 50000):
        for sign in (1, -1):
            cand_val = base + sign * offset * 10
            code = f"{cand_val:012d}"
            if len(code) != 12 or not code.startswith("019"):
                continue
            if code in _used_tr_codes:
                continue
            if min_tr and int(code) <= int(min_tr):
                continue
            _save_used_tr(code)
            return code
    # Never silently emit a code outside the 019 series (September /
    # December estimates used to return 0177.../0214... codes that the
    # project's own add_anchor rejects as out-of-series).
    raise ValueError(
        f"transaction code estimate out of 019 series for dt={dt} (base={base})")


def _sbp_candidates(utc: datetime) -> list:
    """Candidate SBP op-ids for an operation UTC instant (era-aware).

    SBP — NSPK format: prefix(1) year(1) doy(3) hhmm(4) ss(2) mmm(3) flag(1)
    node(1) tail(1) route(15).  Flag has NO ms dependence (refuted on 204
    old-era genuine ids: ms=118/160/486 carry '1', ms=967 carries '7'); in the
    new era G/0011890101 flag is ALWAYS '0'.

    New era (G/0011890101): prefix is random ~50/50 (corpus: Unknown 2 (3)=A,
    Unknown (35)=B), NOT donor-locked, and the gateway follows the prefix
    (A->1002, B->1012).  Emitting the random gateway under the donor's 'A'
    prefix broke the binding on ~50% of receipts (final op_id audit 07.11).
    Old era: donor-locked prefix + DOY route table.
    """
    cands = []
    # Genuine new-era MTS lags (display second - id second) are 2..11s
    # (07.11: 6,6,10,11; fresh 08.11: 2.842/5.116/5.713 -> integers 3/6).
    # The validator enforces the whole-second band 2..11.
    lags = list(range(2, 12))
    _rnd.shuffle(lags)
    for lag in lags:
        st = utc - timedelta(seconds=lag)
        yday = st.timetuple().tm_yday
        if st >= _ERA_FLIP_UTC:
            pfx_letter = _rnd.choice("AB")
            cur_route = "G" + _rnd.choice(_GATEWAYS_NEW[pfx_letter]) + "0011890101"
            cur_node = _rnd.choice(_NODES_NEW)
            flag = _rnd.choice(("0", "1"))
        else:
            pfx_letter = _sbp_full[0]
            route_letter, route_suffix, route4_pool, node_pool = _route_for_doy(yday)
            cur_route = route_letter + _rnd.choice(route4_pool) + route_suffix
            cur_node = _rnd.choice(node_pool)
            # Old-era nonzero flag rate: 12/204 genuine ids ('1' x11, '7' x1).
            _r = _rnd.random()
            flag = "1" if _r < 0.055 else ("7" if _r < 0.06 else "0")
        pfx = pfx_letter + str(st.year)[-1] + f"{yday:03d}" + f"{st.hour:02d}{st.minute:02d}"
        ss_s = f"{st.second:02d}"
        # Genuine ms DOES contain triples in 3/223 ids (000/777/222), but the
        # receipt_32153 incident linked a 777 triple to a checker flag - keep
        # avoiding them (costless, removes a disputed risk marker).
        mmm = _rnd.randint(0, 999)
        while len(set(f"{mmm:03d}")) == 1:
            mmm = _rnd.randint(0, 999)
        mmm_s = f"{mmm:03d}"
        sbp_cand = pfx + ss_s + mmm_s + flag + cur_node + "0" + cur_route
        if len(sbp_cand) == 32 and sbp_cand != _sbp_full:
            cands.append(sbp_cand)
    if not cands:
        raise ValueError(f"No SBP candidates for utc={utc}.")
    return cands


def generate(name: str, phone: str, bank: str, amount_rub: int,
             dt: datetime = None, min_tr: str = None,
             account_last4: str = None,
             sbp_override: str = None, tr_override: str = None,
             cd_override: str = None, acc_full_override: str = None,
             preserve_second: bool = False, meta_out: dict = None) -> bytes:
    """Generate a 100% OpenPDF-exact MTS SBP receipt PDF."""
    if dt is None:
        dt = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    elif getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)
    # Genuine printed seconds span the full 00-59 range; do not clamp.

    # A fresh transfer's receipt is issued seconds-to-minutes AFTER the
    # operation, and its CreationDate must not be in the future at check
    # time. If the op time is "now", age it slightly (30-75s) and land CD
    # at ~now so the receipt can be submitted immediately.
    _now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if dt > _now_msk + timedelta(seconds=60):
        dt = _now_msk
    _fresh = sbp_override is None and abs((dt - _now_msk).total_seconds()) < 120
    if _fresh:
        dt = dt - timedelta(seconds=_rnd.randint(30, 75))

    amount_str = format_amount(amount_rub)
    dt_str = dt.strftime("%d.%m.%Y %H:%M:%S")
    new_date_text = dt_str + " (UTC+3)"
    utc = dt - timedelta(hours=3)
    if _fresh:
        new_cd = utc + timedelta(seconds=_rnd.randint(35, 85))
    else:
        new_cd = utc + timedelta(seconds=_rnd.randint(90, 420))
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if _fresh and new_cd > _now_utc + timedelta(seconds=10):
        new_cd = _now_utc + timedelta(seconds=_rnd.randint(2, 10))
    elif new_cd > _now_utc + timedelta(seconds=20):
        new_cd = _now_utc + timedelta(seconds=_rnd.randint(3, 20))

    u2cid = _full_u2cid
    w2 = _full_w2

    # Date X вЂ” dynamic from right edge (355.00pt), formatted with OpenPDF formatDouble
    x_date_new = _fmt_coord(_right_edge_date - sum(w2[u2cid[c]] for c in new_date_text) * 0.012)

    if sbp_override is not None:
        cands = [sbp_override]
    else:
        cands = _sbp_candidates(utc)

    # Transaction code
    tr = tr_override if tr_override is not None else _pick_trans_code(dt, min_tr=min_tr)
    x_tr_new = _fmt_coord(355.0 - sum(w2[u2cid[c]] for c in tr) * 0.012)

    # Account вЂ” 5 digits or full override. Recompute the CBR check digit
    # (position 9, EXI BIK 044030889 weights 7,1,3) so assembled accounts
    # satisfy the same invariant every genuine receipt account satisfies.
    def _exi_acct_key(d20: str) -> str:
        for kd in "0123456789":
            cand = d20[:8] + kd + d20[9:]
            s = "889" + cand
            if sum((int(ch) * (7, 1, 3)[i % 3]) % 10 for i, ch in enumerate(s)) % 10 == 0:
                return cand
        return d20

    if acc_full_override:
        acc_new = acc_full_override
    elif account_last4 and utc < _ERA_FLIP_UTC:
        acc_digits = _exi_acct_key(_acc_old.replace(" ", "")[:15] + account_last4)
        acc_new = f"{acc_digits[:5]} {acc_digits[5:10]} {acc_digits[10:15]} {acc_digits[15:20]}"
    elif account_last4:
        # New era: NEVER use old donor stem 40914 81070 00021 (which leaked into
        # receipt_32846.pdf as 40914 81070 00021 93262). Keep the genuine new-era
        # stem 40817 81020 00025 intact (check digit '2' at pos 8) and ensure the
        # 5-digit suffix satisfies the CBR check invariant.
        _stem_digits = "408178102000025"
        _suf5 = re.sub(r"\D", "", str(account_last4))[-5:].zfill(5)
        # Preserve the user's suffix EXACTLY: satisfy the CBR invariant by
        # recomputing the check digit at position 8 (inside the stem, via
        # _exi_acct_key, as the old-era branch does).  The previous loop
        # mutated the LAST suffix digit instead, corrupting the user's
        # account_last4 in ~90% of cases (1234 -> ...01231 on the receipt
        # while the bot caption showed *1234).
        acc_digits = _exi_acct_key(_stem_digits + _suf5)
        acc_new = f"{acc_digits[:5]} {acc_digits[5:10]} {acc_digits[10:15]} {acc_digits[15:20]}"
    else:
        # Default account: keep the genuine new-era stem 40817 81020 00025
        # (Unknown 2 (3).pdf, 07.11) INTACT — its CBR check digit at pos 8 is
        # '2', valid for the stem alone — and append a random 5-digit suffix
        # that preserves the check digit, so the assembled 20-digit account
        # both passes the CBR invariant AND matches the genuine stem
        # byte-for-byte. The old code mutated the stem's last digit (5->8)
        # and passed only 15 digits to _exi_acct_key (which expects 20),
        # producing a 15-digit account (3 groups) instead of the genuine
        # 20-digit (4 groups) — a visible structural fingerprint.
        acc_stem = "40817 81020 00025"
        _stem_digits = acc_stem.replace(" ", "")  # 15 digits, check digit '2' at pos 8
        while True:
            _suf = f"{_rnd.randint(0, 99999):05d}"
            acc_digits = _stem_digits + _suf
            _s = "889" + acc_digits
            if sum((int(_ch) * (7, 1, 3)[_i % 3]) % 10 for _i, _ch in enumerate(_s)) % 10 == 0:
                break
        acc_new = f"{acc_digits[:5]} {acc_digits[5:10]} {acc_digits[10:15]} {acc_digits[15:20]}"
    acc_x_new = _fmt_coord(_acc_right_edge - sum(W_F2L[int(c)] for c in acc_new if c.isdigit()) * 0.012)

    xsu = _fmt_coord(355.0 - w_f2_amt(amount_str) * 0.012)
    xit = _fmt_coord(355.0 - w_f3_amt(amount_str) * 0.020)
    xnn = _fmt_coord(355.0 - sum(w2[u2cid[c]] for c in name) * 0.012)
    xpn = _fmt_coord(355.0 - sum(w2[u2cid[c]] for c in phone) * 0.012)
    xbn = _fmt_coord(355.0 - sum(w2[u2cid[c]] for c in bank) * 0.012)

    def _build_cs(sbp_cand):
        def _splice(buf, anchor, old_tj, new_text, what):
            # find() возвращает -1 при промахе; срез по -1 молча корраптил
            # бы весь контент-стрим — падаем с явной ошибкой.
            i = buf.find(old_tj, buf.find(anchor))
            if i < 0:
                raise ValueError(f"MTS donor content anchor lost: {what}")
            return bytearray(bytes(buf)[:i] + b"(" + enc_cid(new_text, u2cid)
                             + b")Tj" + bytes(buf)[i + len(old_tj):])
        l1_s, l2_s, w_l1, w_l2 = _jasper_wrap_sbp(sbp_cand, u2cid, w2)
        x_sbp1 = _fmt_coord(_right_edge_sbp - w_l1 * 0.012)
        x_sbp2 = _fmt_coord(355.0 - w_l2 * 0.012)
        p = bytearray(_cs["dec"])
        otj = b"(" + _d_m.group(2) + b")Tj"
        p = bytearray(bytes(p).replace(b"1 0 0 1 " + _x_date_old + b" 643.35 Tm", b"1 0 0 1 " + x_date_new + b" 643.35 Tm", 1))
        p = _splice(p, x_date_new + b" 643.35 Tm", otj, new_date_text, "date Tj")
        p = bytearray(bytes(p).replace(enc_amt(_acc_old), enc_amt(acc_new), 1))
        p = bytearray(bytes(p).replace(b"1 0 0 1 " + _acc_x_old + b" 469.35 Tm", b"1 0 0 1 " + acc_x_new + b" 469.35 Tm", 1))
        p = bytearray(bytes(p).replace(enc_amt(_old_amt_text), enc_amt(amount_str)))
        p = bytearray(bytes(p).replace(b"1 0 0 1 " + _x_su_old + b" 437.35 Tm", b"1 0 0 1 " + xsu + b" 437.35 Tm", 1)
            .replace(b"1 0 0 1 " + _x_it_old + b" 260.58 Tm", b"1 0 0 1 " + xit + b" 260.58 Tm", 1))
        p = bytearray(bytes(p).replace(b"1 0 0 1 " + _x_sbp1_old + b" 341.35 Tm", b"1 0 0 1 " + x_sbp1 + b" 341.35 Tm", 1))
        otj2 = b"(" + _m_sbp1.group(2) + b")Tj"
        p = _splice(p, x_sbp1 + b" 341.35 Tm", otj2, l1_s, "sbp line1 Tj")
        p = bytearray(bytes(p).replace(b"1 0 0 1 " + _x_sbp2_old + b" 325.77 Tm", b"1 0 0 1 " + x_sbp2 + b" 325.77 Tm", 1))
        otj_l2 = b"(" + _m_sbp2.group(2) + b")Tj"
        p = _splice(p, x_sbp2 + b" 325.77 Tm", otj_l2, l2_s, "sbp line2 Tj")
        if x_tr_new != _x_tr_old:
            p = bytearray(bytes(p).replace(b"1 0 0 1 " + _x_tr_old + b" 373.35 Tm", b"1 0 0 1 " + x_tr_new + b" 373.35 Tm", 1))
        p = bytearray(bytes(p).replace(enc_amt(_trans_old), enc_amt(tr), 1))
        for yb, ox, nx, ot, nt in [(b"597.35", _xno, xnn, _tno, name), (b"565.35", _xpo, xpn, _tpo, phone), (b"533.35", _xbo, xbn, _tbo, bank)]:
            p = bytearray(bytes(p).replace(b"1 0 0 1 " + ox + b" " + yb + b" Tm", b"1 0 0 1 " + nx + b" " + yb + b" Tm", 1))
            ot2 = b"(" + ot + b")Tj"
            p = _splice(p, nx + b" " + yb + b" Tm", ot2, nt, f"value Tj @{yb.decode()}")
        return bytes(p), x_sbp1

    # First shuffled candidate: bank codes are issued independently of the
    # PDF's compressed length; selecting by length skews digit statistics.
    sbp = cands[0]
    raw_29, x_sbp1_new = _build_cs(sbp)
    content_comp = jcomp(raw_29, "cg")

    # в”Ђв”Ђ Font subsets: ALWAYS rebuilt from exactly the used CIDs в”Ђв”Ђ
    # (Keeping the donor's fonts verbatim reuses its glyph set + subset prefix
    # on every receipt вЂ” orphan glyphs and a constant fingerprint.)
    used_f2, used_f3 = _extract_used_cids(raw_29)
    full_f2 = used_f2
    full_f3 = used_f3
    f2_ttf_raw = _build_openpdf_ttf(full_f2, _cid2glyph2, _f2_sfnt, _f2_head, _f2_hhea, _f2_hmtx, _f2_maxp, 0x155FC126)
    # Bank ToUnicode//W are FULLY sorted (proven on genuine 2026 receipts);
    # the old donor-first+append ordering corrupts both for out-of-donor chars.
    f2_tu_raw  = _build_openpdf_tounicode(full_f2, _cid2ucp2)
    new_o55    = _build_openpdf_w(full_f2, _cid2w2, _base_o55)
    f2_comp    = jcomp(f2_ttf_raw, "df2")
    tu2_comp   = jcomp(f2_tu_raw, "dtu2")
    f3_ttf_raw = _build_openpdf_ttf(full_f3, _cid2glyph3, _f3_sfnt, _f3_head, _f3_hhea, _f3_hmtx, _f3_maxp, 0x15B3C133)
    f3_tu_raw  = _build_openpdf_tounicode(full_f3, _cid2ucp3)
    new_o59    = _build_openpdf_w(full_f3, _cid2w3, _base_o59)
    f3_comp    = jcomp(f3_ttf_raw, "df3")
    tu3_comp   = jcomp(f3_tu_raw, "dtu3")

    # в”Ђв”Ђ Build ObjStm (31 0 obj) в”Ђв”Ђ
    im = [("JR_PAGE_ANCHOR_0_1", b"[1 0 R/XYZ 0 730 0] "),
        ("\u041f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044c", b"[1 0 R/XYZ 20 609.35 0] "),
        ("\u0422\u0435\u043b\u0435\u0444\u043e\u043d \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044f", b"[1 0 R/XYZ 20 577.35 0] "),
        ("\u0411\u0430\u043d\u043a \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u0435\u043b\u044f", b"[1 0 R/XYZ 20 545.35 0] "),
        ("\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", b"[1 0 R/XYZ 20 513.35 0] "),
        ("\u0421\u0447\u0435\u0442 \u0441\u043f\u0438\u0441\u0430\u043d\u0438\u044f", b"[1 0 R/XYZ 20 481.35 0] "),
        ("\u0421\u0443\u043c\u043c\u0430", b"[1 0 R/XYZ 20 449.35 0] "),
        ("\u041a\u043e\u043c\u0438\u0441\u0441\u0438\u044f", b"[1 0 R/XYZ 20 417.35 0] "),
        ("\u041a\u043e\u0434 \u0442\u0440\u0430\u043d\u0437\u0430\u043a\u0446\u0438\u0438", b"[1 0 R/XYZ 20 385.35 0] "),
        ("\u041a\u043e\u0434 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0438 \u0421\u0411\u041f", b"[1 0 R/XYZ 20 353.35 0] "),
        ("\u0418\u0442\u043e\u0433\u043e", b"[1 0 R/XYZ 20 280.58 0] "),
        (new_date_text, b"[1 0 R/XYZ " + x_date_new + b" 655.35 0] "),
        (name, b"[1 0 R/XYZ " + xnn + b" 609.35 0] "), (phone, b"[1 0 R/XYZ " + xpn + b" 577.35 0] "),
        (bank, b"[1 0 R/XYZ " + xbn + b" 545.35 0] "), ("\u041f\u0435\u0440\u0435\u0432\u043e\u0434 \u0421\u0411\u041f", b"[1 0 R/XYZ 280.95 513.35 0] "),
        (acc_new, b"[1 0 R/XYZ " + acc_x_new + b" 481.35 0] "),
        (amount_str, b"[1 0 R/XYZ " + xsu + b" 449.35 0] "),
        ("0,00 \u20bd", b"[1 0 R/XYZ 318.72 417.35 0] "),
        (tr, b"[1 0 R/XYZ " + x_tr_new + b" 385.35 0] "),
        (sbp, b"[1 0 R/XYZ " + x_sbp1_new + b" 353.35 0] ")]

    ht = [x for x in _od[:_first].decode("latin1").split(" ") if x]
    pairs = [(int(ht[i]), int(ht[i+1])) for i in range(0, len(ht), 2)]
    objs = {}
    for idx, (on, off) in enumerate(pairs):
        end = pairs[idx+1][1] if idx+1 < len(pairs) else len(_od) - _first
        objs[on] = _od[_first+off:_first+end]
    # /Names is a name tree: keys are unique (the bank collapses duplicates,
    # first occurrence wins - the donor's amount key keeps the amount XYZ).
    _seen_names = {}
    for _u, _xyz in im:
        if _u not in _seen_names:
            _seen_names[_u] = _xyz
    si = sorted(_seen_names.items(), key=lambda x: x[0])
    for k, (u, xyz) in enumerate(si):
        objs[32+k] = xyz if xyz.endswith(b" ") else xyz + b" "
    for _stale in range(32 + len(si), 54):
        objs.pop(_stale, None)
    objs[61] = b"<</Names[" + b"".join(b"(" + names_bytes(u) + b") " + f"{32+k} 0 R".encode() for k, (u, _) in enumerate(si)) + b"]>> "
    objs[55] = new_o55
    objs[59] = new_o59

    # Fresh subset prefix per face (genuine: unique per document; reusing the
    # donor's prefix on every receipt is a constant fingerprint).
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
    for on, _ in pairs:
        if on not in objs:
            continue
        np2.append((on, len(body)))
        body += objs[on]
    rh = (" ".join(f"{a} {b}" for a, b in np2) + " ").encode("latin1")
    new_first = len(rh)
    os_comp = jcomp(bytes(rh + body), "og")

    # в”Ђв”Ђ Assemble PDF в”Ђв”Ђ
    out = bytearray(_data)
    for obj_n, comp in [(29, content_comp), (31, os_comp)]:
        m_obj = re.search(str(obj_n).encode() + rb" 0 obj\n<<(.*?)>>stream\n", out, re.S)
        new_hdr = re.sub(rb"/Length \d+", b"/Length " + str(len(comp)).encode(), m_obj.group(1))
        if obj_n == 31:
            # /N must equal the actual object count in the rebuilt ObjStm
            # body.  When /Names keys collapse (amount 0 makes the amount
            # key equal the commission key; name == bank; ...), the body
            # holds fewer objects than the donor header claims and MuPDF
            # rejects the file ("corrupt object stream", 0 pages).
            new_hdr = re.sub(rb"/N \d+", b"/N " + str(len(np2)).encode(), new_hdr)
            if new_first != _first:
                new_hdr = re.sub(rb"/First \d+", b"/First " + str(new_first).encode(), new_hdr)
        obj_end = out.find(b"\nendstream\nendobj\n", m_obj.end()) + len(b"\nendstream\nendobj\n")
        out = bytearray(out[:m_obj.start(1)] + new_hdr + b">>stream\n" + comp
              + b"\nendstream\nendobj\n" + out[obj_end:])

    cd_str = cd_override if cd_override is not None else new_cd.strftime('%Y%m%d%H%M%S')
    out = bytearray(bytes(out).replace(f"D:{_old_cd_s}Z".encode(), f"D:{cd_str}Z".encode()))

    for obj_n, comp, raw_len in [
        (53, f2_comp, len(f2_ttf_raw)),
        (56, tu2_comp, None),
        (57, f3_comp, len(f3_ttf_raw)),
        (60, tu3_comp, None),
    ]:
        s = out.find(f"{obj_n} 0 obj\n".encode())
        e = out.find(b"\nendobj\n", s) + len(b"\nendobj\n")
        if raw_len:
            hdr = f"{obj_n} 0 obj\n<</Length1 {raw_len}/Filter/FlateDecode/Length {len(comp)}>>stream\n".encode()
        else:
            hdr = f"{obj_n} 0 obj\n<</Filter/FlateDecode/Length {len(comp)}>>stream\n".encode()
        out = out[:s] + hdr + comp + b"\nendstream\nendobj\n" + out[e:]

    # Xref
    xr = find_stream(_data, 65); rows = parse_xref(xr["dec"])
    found = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", out)}
    nr = [(1, found[n], b) if t == 1 else (t, a, b) for n, (t, a, b) in enumerate(rows)]
    xn = jcomp(build_xref(nr), "xg")
    m65 = re.search(rb"65 0 obj\n<<(.*?)>>stream\n", out)
    h65 = re.sub(rb"/Length\s+\d+", b"/Length " + str(len(xn)).encode(), m65.group(1), count=1)
    out = (out[:m65.start()] + bytes(out[m65.start():m65.start(1)]) + h65
           + b">>stream\n" + xn + b"\nendstream\nendobj\nstartxref\n"
           + str(found[65]).encode() + b"\n%%EOF\n")

    # CorrelationId вЂ” UUIDv4 first 16 hex (13th char = '4', matching all genuine originals)
    nc = uuid.uuid4().hex[:16]
    oc = re.search(rb"CorrelationId:([0-9a-f]{16})", out)
    out[oc.start(1):oc.end(1)] = nc.encode()
    ni1, ni2 = secrets.token_hex(16), secrets.token_hex(16)
    oi = re.search(rb"/ID\s*\[<([0-9a-f]{32})><([0-9a-f]{32})>\]", out)
    out[oi.start(1):oi.end(1)] = ni1.encode()
    out[oi.start(2):oi.end(2)] = ni2.encode()

    if meta_out is not None:
        meta_out.update({"dt": dt, "tr": tr, "sbp": sbp, "account": acc_new})
    return bytes(out)
