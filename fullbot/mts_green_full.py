# -*- coding: utf-8 -*-
r"""FULL universal MTS SBP receipt builder: changes ANY text field
(name / phone / bank / amount / account / SBP / trans) while keeping
53/56/57/60 (fonts+ToUnicode) and /W arrays 100% UNTOUCHED (the green formula).

Usage:
  py -3 mts_green_full.py <donor> <amount> <out> [--name ".."] [--phone ".."] [--bank ".."] [--date "DD.MM.YYYY HH:MM:SS"]
Example:
  py -3 mts_green_full.py "file (18).pdf" "8 900,00 ₽" out.pdf --bank "Т-Банк" --phone "+7 (999) 123-45-67" --date "24.10.2026 11:42:37"
"""
import os, sys, re
from datetime import datetime, timedelta
sys.stdout.reconfigure(encoding="utf-8")
from mts_build_check15 import find_stream, parse_xref, build_xref, jcomp, tw

W_F2L = [642, 347, 541, 584, 598, 575, 600, 496, 626, 600]
W_F3L = [655, 385, 574, 606, 631, 600, 618, 512, 645, 618]

# ---------- parsing helpers ----------
def parse_tounicode_cmap(dec):
    """returns (char->CID, CID->char) from a ToUnicode CMap (bfrange entries)."""
    u2c, c2u = {}, {}
    for a, b, c in re.findall(rb"<([0-9A-Fa-f]{4})><([0-9A-Fa-f]{4})><([0-9A-Fa-f]{4})>", dec):
        lo, hi, uni = int(a, 16), int(b, 16), int(c, 16)
        for cid in range(lo, hi + 1):
            ch = chr(uni + cid - lo)
            u2c[ch] = cid
            c2u[cid] = ch
    return u2c, c2u

def parse_w_array(w_dec):
    """CID -> width from a /W [...] array inside a CIDFont dict."""
    m = re.search(rb"/W\s*\[(.*)\]\s*/CIDToGIDMap", w_dec, re.S)
    out = {}
    for start, vals in re.findall(rb"(\d+)\[([\d ]+)\]", m.group(1)):
        c = int(start)
        for v in vals.split():
            out[c] = int(v)
            c += 1
    return out

ESC = {0x0A: b"\\n", 0x0D: b"\\r", 0x09: b"\\t", 0x08: b"\\b", 0x0C: b"\\f",
       0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\"}
def esc_pdf(raw):
    out = bytearray()
    for byte in raw:
        out += ESC.get(byte, bytes([byte]))
    return bytes(out)

def unesc_pdf(raw):
    out = bytearray(); i = 0
    while i < len(raw):
        if raw[i:i+1] == b"\\" and i + 1 < len(raw):
            c = raw[i+1]
            mp = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C,
                  0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if c in mp:
                out.append(mp[c]); i += 2; continue
        out.append(raw[i]); i += 1
    return bytes(out)

def enc_cid(s, u2cid):
    raw = bytearray()
    for ch in s:
        raw += u2cid[ch].to_bytes(2, "big")
    return esc_pdf(bytes(raw))

def names_bytes(s):
    """/Names key encoding: OpenPDF writes the LOW BYTE of each code point
    (Cyrillic А=0x10.., ё=0x51, Ё=0x01, ₽=0xBD, NBSP=0xA0); then escape.
    The old cp1251-0xB0 formula coincides for all Cyrillic except ё/Ё, where
    it produced 0x08 (escaped as \\b, +1 byte) instead of genuine 0x51."""
    raw = bytearray()
    for ch in s:
        raw.append(ord(ch) & 0xFF)
    return esc_pdf(bytes(raw))

CID_BASIC = {" ": 3, "\xa0": 0x61, ",": 0x0F, "0": 0x13, "1": 0x14, "2": 0x15,
             "3": 0x16, "4": 0x17, "5": 0x18, "6": 0x19, "7": 0x1A, "8": 0x1B,
             "9": 0x1C, "₽": 0xF3}
def enc_amt(s):
    return b"".join(CID_BASIC[c].to_bytes(2, "big") for c in s)

def dec_amt(b):
    out = []
    for i in range(0, len(b), 2):
        c = (b[i] << 8) | b[i + 1]
        if c == 0xF3: out.append("₽")
        elif c == 0x03: out.append(" ")
        elif c == 0x0F: out.append(",")
        else: out.append(str(c - 0x13))
    return "".join(out)

def w_f2_amt(s):
    return sum(W_F2L[int(c)] if c.isdigit() else 200 if c == "\xa0" else 250 if c == " " else 235 if c == "," else 612 for c in s)

def w_f3_amt(s):
    return sum(W_F3L[int(c)] if c.isdigit() else 200 if c == "\xa0" else 250 if c == " " else 265 if c == "," else 658 for c in s)

def pick_tm(dec, y_s, min_x=100.0):
    pat = rb"1 0 0 1 ([\d.]+) " + y_s + rb" Tm\s*/F2 12 Tf\s*0 0 0 rg\s*\(((?:\\.|[^\\)])*)\)Tj"
    best = None
    for m in re.finditer(pat, dec):
        if float(m.group(1)) > min_x:
            best = m
    return best

def get_objstm_obj(od, first, onum):
    hdr = od[:first].decode("latin1").split()
    pairs = {int(hdr[i]): int(hdr[i + 1]) for i in range(0, len(hdr), 2)}
    off = pairs[onum]
    nxt = min([v for k, v in pairs.items() if v > off], default=len(od) - first)
    return od[first + off:first + nxt]

# ---------- main ----------
def main():
    args = sys.argv[1:]
    if args and args[0] == "--batch":
        import json
        jobs = json.load(open(args[1], encoding="utf-8"))
        for jb in jobs:
            run(jb["donor"], jb["amount"], jb["out"],
                {k: v for k, v in jb.items() if k in ("name", "phone", "bank", "date")})
        return
    opts = {}
    for k in ("--name", "--phone", "--bank", "--date"):
        if k in args:
            i = args.index(k)
            opts[k[2:]] = args[i + 1]
            del args[i:i + 2]
    run(args[0], args[1], args[2], opts)

def run(donor_path, amount_str, out_path, opts):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    data = bytearray(open(donor_path, "rb").read())
    cs = find_stream(data, 29)
    ob = find_stream(data, 31)
    od = ob["dec"]
    first = int(re.search(rb"/First\s+(\d+)", ob["hdr_m"].group(1)).group(1))

    # donor maps
    u2cid, cid2u = parse_tounicode_cmap(find_stream(data, 56)["dec"])
    w2 = parse_w_array(get_objstm_obj(od, first, 55))

    # ---- amount (as in green native) ----
    m_su = pick_tm(cs["dec"], b"437.35")
    m_it = None
    for m in re.finditer(rb"1 0 0 1 ([\d.]+) 260\.58 Tm\s*/F3 20 Tf\s*0 0 0 rg\s*\(((?:\\.|[^\\)])*)\)Tj", cs["dec"]):
        if float(m.group(1)) > 100:
            m_it = m
    x_su_old, x_it_old = m_su.group(1), m_it.group(1)
    old_amt_text = dec_amt(unesc_pdf(m_su.group(2)))
    x_su_s = f"{355.0 - w_f2_amt(amount_str) * 0.012:.2f}"
    x_it_s = f"{355.0 - w_f3_amt(amount_str) * 0.020:.2f}"

    # ---- account ----
    acc_old = re.search(rb"\((\d{5} \d{5} \d{5} \d{5})\)", od).group(1).decode()
    acc_x_old = pick_tm(cs["dec"], b"469.35").group(1)
    d_old = acc_old[-1]
    d_new = "9" if d_old == "8" else "8"
    acc_new = acc_old[:-1] + d_new
    acc_x_new = f"{float(acc_x_old) + (W_F2L[int(d_old)] - W_F2L[int(d_new)]) * 0.012:.2f}"

    # ---- SBP / trans / date ----
    # Salt from the output name so two builds of one donor never reuse the same
    # transaction code / SBP seq (the nearest candidate is what already went green).
    salt = sum(os.path.basename(out_path).encode()) or 1
    sbp_m = re.search(rb"\(([AB][0-9A-Z]{31})\)", od)
    sbp_full = sbp_m.group(1) if sbp_m else None
    trans_old = re.search(rb"\((019\d{9})\)", od).group(1).decode()
    old_date_s = re.search(rb"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})", od)
    receipt_sec = int(old_date_s.group(6))
    new_dt = None
    if "date" in opts:
        new_dt = datetime.strptime(opts["date"], "%d.%m.%Y %H:%M:%S")
        receipt_sec = new_dt.second
    s6_new, sbp_new_s, sbp_line1_new, x_sbp1_new = None, None, None, None
    date_meta_utc = None
    if sbp_full:
        sbp_s = sbp_full.decode()
        s6_old, node, tail = sbp_s[9:15], sbp_s[15], sbp_s[16]
        if new_dt:
            # full SBP regen: prefix = letter + year digit + day-of-year + HHMM (UTC=local-3h)
            utc = new_dt - timedelta(hours=3)
            prefix = sbp_s[0] + str(utc.year)[-1] + f"{utc.timetuple().tm_yday:03d}" + f"{utc.hour:02d}{utc.minute:02d}"
            route = sbp_s[18:32]
            seq8 = None
            for ss in range(max(receipt_sec - 1, 1), 0, -1):
                for mid in range(100, 1000):
                    cand6 = f"{ss:02d}{mid:03d}{sbp_s[14]}"
                    if "000" not in cand6 and cand6 != s6_old:
                        seq8 = cand6 + node + tail
                        break
                if seq8:
                    break
            assert seq8, "no valid seq8"
            sbp_new_s = prefix + seq8 + "G" + route
            line1 = sbp_new_s[:23]
            # donor line1: find its Tm X and right edge, recompute X for new line1
            c0 = u2cid[sbp_s[0]]
            pos1 = cs["dec"].find(bytes([0, c0, 0, 0x13 + int(sbp_s[1])]))
            tm1 = None
            for mm in re.finditer(rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm", cs["dec"][:pos1]):
                tm1 = mm
            x1_old = float(tm1.group(1))
            x1_old_b = tm1.group(1)
            old_line1 = sbp_s[:23]
            r1 = x1_old + sum(w2[u2cid[c]] for c in old_line1) * 0.012
            x_sbp1_new = f"{r1 - sum(w2[u2cid[c]] for c in line1) * 0.012:.2f}"
            sbp_line1_new = line1
            s6_new = seq8[:6]
            # Keep THIS donor's own gap between the printed time and CreationDate.
            old_utc = datetime(int(old_date_s.group(3)), int(old_date_s.group(2)), int(old_date_s.group(1)),
                               int(old_date_s.group(4)), int(old_date_s.group(5)), int(old_date_s.group(6))) - timedelta(hours=3)
            old_meta = datetime.strptime(re.search(rb"CreationDate\(D:(\d{14})Z\)", bytes(data)).group(1).decode(), "%Y%m%d%H%M%S")
            date_meta_utc = utc + (old_meta - old_utc)
            print(f"date -> {opts['date']} (UTC+3)  SBP {sbp_s} -> {sbp_new_s}  line1 X {x1_old} -> {x_sbp1_new}  meta_delta={(old_meta-old_utc).total_seconds():.0f}s")
        else:
            w6 = tw(s6_old)
            cands = [f"{ss:02d}{mid:03d}{fl}"
                     for ss in range(1, max(receipt_sec, 2)) for mid in range(1000) for fl in (0, 1)
                     if (lambda c: c != s6_old and "000" not in c and tw(c) == w6)(f"{ss:02d}{mid:03d}{fl}")]
            s6_new = cands[(len(cands) // 2 + salt) % len(cands)] if cands else s6_old
            sbp_new_s = sbp_s[:9] + s6_new + node + tail + sbp_s[17:]
    w12 = tw(trans_old)
    tc = [trans_old[:6] + f"{x:05d}0" for x in range(100000)
          if "000" not in f"{x:05d}" and (lambda c: c != trans_old and tw(c) == w12)(trans_old[:6] + f"{x:05d}0")]
    tc.sort(key=lambda t: abs(int(t) - int(trans_old)))
    s12_new = tc[salt % len(tc)] if tc else trans_old

    # ---- F2 text field overrides: (Y, XYZ_Y) ----
    FIELDS = {"name": (b"597.35", b"609.35"), "phone": (b"565.35", b"577.35"), "bank": (b"533.35", b"545.35")}
    field_edits = []   # (y_bytes, old_tj_raw, new_tj_esc, old_x, new_x, old_name_key, new_name_key)
    for key, val in opts.items():
        if key == "date":
            continue
        y_b, xyz_y = FIELDS[key]
        m = pick_tm(cs["dec"], y_b)
        assert m, f"field {key} not found at Y={y_b.decode()}"
        missing = [c for c in val if c not in u2cid]
        assert not missing, f"{key}: donor F2 lacks glyphs: {missing} — use another donor!"
        new_x = f"{355.0 - sum(w2[u2cid[c]] for c in val) * 0.012:.2f}"
        old_raw = unesc_pdf(m.group(2))
        old_text = "".join(cid2u[old_raw[i] << 8 | old_raw[i + 1]] for i in range(0, len(old_raw), 2))
        field_edits.append((y_b, xyz_y, m.group(2), enc_cid(val, u2cid), m.group(1), new_x.encode(), old_text, val))
        print(f"{key}: {old_text!r} -> {val!r}  X {m.group(1).decode()} -> {new_x}")

    # ---- date (gray 0.50196 line at Y=643.35) ----
    date_edit = None
    if new_dt:
        d_m = re.search(rb"1 0 0 1 ([\d.]+) 643\.35 Tm\s*/F2 12 Tf\s*0\.50196 0\.50196 0\.50196 rg\s*\(((?:\\.|[^\\)])*)\)Tj", cs["dec"])
        assert d_m, "gray date line not found"
        old_date_raw = unesc_pdf(d_m.group(2))
        old_date_text = "".join(cid2u[old_date_raw[i] << 8 | old_date_raw[i + 1]] for i in range(0, len(old_date_raw), 2))
        new_date_text = opts["date"] + " (UTC+3)"
        missing = [c for c in new_date_text if c not in u2cid]
        assert not missing, f"date chars missing in F2: {missing}"
        r_date = float(d_m.group(1)) + sum(w2[old_date_raw[i] << 8 | old_date_raw[i + 1]] for i in range(0, len(old_date_raw), 2)) * 0.012
        x_date_new = f"{r_date - sum(w2[u2cid[c]] for c in new_date_text) * 0.012:.2f}"
        date_edit = (d_m.group(1), x_date_new.encode(), d_m.group(2), enc_cid(new_date_text, u2cid), old_date_text, new_date_text)
        print(f"date: {old_date_text!r} -> {new_date_text!r}  X {d_m.group(1).decode()} -> {x_date_new}")

    # ---- patch 29 0 obj ----
    p29 = bytearray(cs["dec"])
    p29 = bytearray(bytes(p29).replace(enc_amt(acc_old), enc_amt(acc_new), 1))
    p29 = bytearray(bytes(p29).replace(b"1 0 0 1 " + acc_x_old + b" 469.35 Tm",
                                       b"1 0 0 1 " + acc_x_new.encode() + b" 469.35 Tm", 1))
    p29 = bytearray(bytes(p29).replace(enc_amt(old_amt_text), enc_amt(amount_str)))
    p29 = bytearray(bytes(p29)
        .replace(b"1 0 0 1 " + x_su_old + b" 437.35 Tm", b"1 0 0 1 " + x_su_s.encode() + b" 437.35 Tm", 1)
        .replace(b"1 0 0 1 " + x_it_old + b" 260.58 Tm", b"1 0 0 1 " + x_it_s.encode() + b" 260.58 Tm", 1))
    if sbp_full and not new_dt:
        c0 = u2cid[sbp_full[0:1].decode()]
        pos = p29.find(bytes([0, c0, 0, 0x13 + int(sbp_full[1:2])]))
        assert pos >= 0, "SBP start not found in content stream"
        for k, ch in enumerate(s6_new):
            p29[pos + (9 + k) * 2] = 0
            p29[pos + (9 + k) * 2 + 1] = 0x13 + int(ch)
    if sbp_full and new_dt:
        c0 = u2cid[sbp_full[0:1].decode()]
        pos = p29.find(bytes([0, c0, 0, 0x13 + int(sbp_full[1:2])]))
        assert pos >= 0, "SBP line1 not found in content stream"
        tm1 = None
        for mm in re.finditer(rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm", p29[:pos]):
            tm1 = mm
        p29 = bytearray(bytes(p29).replace(
            b"1 0 0 1 " + tm1.group(1) + b" " + tm1.group(2) + b" Tm",
            b"1 0 0 1 " + x_sbp1_new.encode() + b" " + tm1.group(2) + b" Tm", 1))
        pos = p29.find(bytes([0, c0, 0, 0x13 + int(sbp_full[1:2])]))
        tj_end = p29.find(b")Tj", pos)
        assert pos >= 0 and tj_end > pos, "SBP line1 Tj not found"
        new_l1_raw = esc_pdf(b"".join(u2cid[c].to_bytes(2, "big") for c in sbp_line1_new))
        p29 = bytearray(bytes(p29)[:pos] + new_l1_raw + bytes(p29)[tj_end:])
    if date_edit:
        old_x, new_x, old_tj, new_tj, old_txt, new_txt = date_edit
        p29 = bytearray(bytes(p29).replace(b"1 0 0 1 " + old_x + b" 643.35 Tm",
                                           b"1 0 0 1 " + new_x + b" 643.35 Tm", 1))
        old_tj_line = b"(" + old_tj + b")Tj"
        idx = p29.find(old_tj_line, p29.find(new_x + b" 643.35 Tm"))
        assert idx >= 0, "date Tj not found"
        p29 = bytearray(bytes(p29)[:idx] + b"(" + new_tj + b")Tj" + bytes(p29)[idx + len(old_tj_line):])
    p29 = bytearray(bytes(p29).replace(enc_amt(trans_old), enc_amt(s12_new), 1))
    for y_b, xyz_y, old_tj, new_tj, old_x, new_x, old_txt, new_txt in field_edits:
        old_block = b"1 0 0 1 " + old_x + b" " + y_b + b" Tm"
        assert old_block in p29
        p29 = bytearray(bytes(p29).replace(old_block, b"1 0 0 1 " + new_x + b" " + y_b + b" Tm", 1))
        old_tj_line = b"(" + old_tj + b")Tj"
        idx = p29.find(old_tj_line, p29.find(new_x + b" " + y_b + b" Tm"))
        assert idx >= 0
        p29 = bytearray(bytes(p29)[:idx] + b"(" + new_tj + b")Tj" + bytes(p29)[idx + len(old_tj_line):])
    content_comp = jcomp(bytes(p29), "cs_gf")

    # ---- patch 31 0 obj ----
    old_name_b = old_amt_text.replace("₽", "\xbd").encode("latin1")
    new_name_b = amount_str.replace("₽", "\xbd").encode("latin1")
    replacements = [
        (old_name_b, new_name_b),
        (b"[1 0 R/XYZ " + x_su_old + b" 449.35 0]", b"[1 0 R/XYZ " + x_su_s.encode() + b" 449.35 0]"),
        (b"[1 0 R/XYZ " + acc_x_old + b" 481.35 0]", b"[1 0 R/XYZ " + acc_x_new.encode() + b" 481.35 0]"),
        (b"(" + acc_old.encode() + b")", b"(" + acc_new.encode() + b")"),
    ]
    if sbp_full:
        replacements.append((sbp_full, sbp_new_s.encode()))
    if new_dt and sbp_full:
        replacements.append((b"[1 0 R/XYZ " + x1_old_b + b" 353.35 0]",
                             b"[1 0 R/XYZ " + x_sbp1_new.encode() + b" 353.35 0]"))
    if date_edit:
        _ox, _nx, _ot, _nt, _otxt, _ntxt = date_edit
        replacements.append((b"(" + names_bytes(_otxt) + b")", b"(" + names_bytes(_ntxt) + b")"))
        replacements.append((b"[1 0 R/XYZ " + _ox + b" 655.35 0]", b"[1 0 R/XYZ " + _nx + b" 655.35 0]"))
    replacements.append((trans_old.encode(), s12_new.encode()))
    for y_b, xyz_y, old_tj, new_tj, old_x, new_x, old_txt, new_txt in field_edits:
        replacements.append((b"(" + names_bytes(old_txt) + b")", b"(" + names_bytes(new_txt) + b")"))
        replacements.append((b"[1 0 R/XYZ " + old_x + b" " + xyz_y + b" 0]",
                             b"[1 0 R/XYZ " + new_x + b" " + xyz_y + b" 0]"))
    grew = []
    q = bytearray(od)
    for old_b, new_b in replacements:
        idx = od.find(old_b)
        assert idx >= 0, f"not found in ObjStm: {old_b[:50]}"
        if len(new_b) != len(old_b):
            grew.append((idx, len(new_b) - len(old_b)))
        q = q.replace(old_b, new_b, 1)
    hdr_toks = [x for x in od[:first].decode("latin1").split(" ") if x]
    pairs = [(int(hdr_toks[i]), int(hdr_toks[i + 1])) for i in range(0, len(hdr_toks), 2)]
    fixed = []
    for onum, off in pairs:
        abs_start = first + off
        add = sum(delta for thr, delta in grew if abs_start > thr)
        fixed.append((onum, off + add))
    rebuilt_hdr = (" ".join(f"{a} {b}" for a, b in fixed) + " ").encode("latin1")
    assert len(rebuilt_hdr) == first, f"ObjStm header {len(rebuilt_hdr)} != {first}"
    q[:first] = rebuilt_hdr
    os_comp = jcomp(bytes(q), "os_gf")

    # ---- assemble ----
    out = bytearray(data)
    for info, comp in sorted([(cs, content_comp), (ob, os_comp)], key=lambda x: x[0]["start"], reverse=True):
        hs = info["hdr_m"].start(1)
        a, b_sp = info["len_span"]
        out[hs + a: hs + b_sp] = str(len(comp)).encode()
        out = out[:info["start"]] + comp + out[info["start"] + info["len"]:]
    xr = find_stream(data, 65)
    rows = parse_xref(xr["dec"])
    found = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", out)}
    new_rows = [(1, found[num], b_val) if t == 1 else (t, a_val, b_val) for num, (t, a_val, b_val) in enumerate(rows)]
    xref_new = jcomp(build_xref(new_rows), "xr_gf")
    m65 = re.search(rb"65 0 obj\n<<(.*?)>>stream\n", out)
    hdr65 = re.sub(rb"/Length\s+\d+", b"/Length " + str(len(xref_new)).encode(), m65.group(1), count=1)
    new_tail = bytes(out[m65.start():m65.start(1)]) + hdr65 + b">>stream\n" + xref_new + b"\nendstream\nendobj\nstartxref\n" + str(found[65]).encode() + b"\n%%EOF\n"
    out = out[:m65.start()] + new_tail
    if date_meta_utc:
        meta_new = date_meta_utc.strftime("D:%Y%m%d%H%M%SZ").encode()
        out_b, n_meta = re.subn(rb"D:\d{14}Z", meta_new, bytes(out))
        assert n_meta == 2, f"metadata date count {n_meta} != 2"
        out = bytearray(out_b)
    open(out_path, "wb").write(out)

    # ---- verify ----
    chk = open(out_path, "rb").read()
    cs2, ob2 = find_stream(chk, 29), find_stream(chk, 31)
    assert enc_amt(amount_str) in cs2["dec"] and new_name_b in ob2["dec"]
    for y_b, xyz_y, old_tj, new_tj, old_x, new_x, old_txt, new_txt in field_edits:
        assert b"(" + new_tj + b")Tj" in cs2["dec"], f"{new_txt} missing in content"
        assert b"(" + names_bytes(new_txt) + b")" in ob2["dec"], f"{new_txt} missing in names"
    if date_edit:
        assert b"(" + date_edit[3] + b")Tj" in cs2["dec"], "new date missing in content"
        assert b"(" + names_bytes(date_edit[5]) + b")" in ob2["dec"], "new date missing in names"
        assert date_meta_utc.strftime("D:%Y%m%d%H%M%SZ").encode() in chk, "metadata date missing"
    if sbp_full and new_dt:
        assert sbp_new_s.encode() in ob2["dec"], "new SBP missing in names"
    for on in (53, 56, 57, 60):
        f_o, f_n = find_stream(data, on), find_stream(chk, on)
        assert f_o["len"] == f_n["len"] and data[f_o["start"]:f_o["start"] + f_o["len"]] == chk[f_n["start"]:f_n["start"] + f_n["len"]]
    print(f"SAVED {out_path} ({len(out)}B, donor {len(data)}B)  fonts UNTOUCHED")

if __name__ == "__main__":
    main()
