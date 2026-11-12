# -*- coding: utf-8 -*-
"""receipt_validator.py - validator of receipts from 6 banks (WB, MTS, VTB, RSHB, Ozon, Yandex).

Public API:
    validate(pdf_bytes) -> {"bank": str|None, "passed": bool,
                            "checks": [{"name","passed","detail"}, ...]}
    diagnose(pdf_bytes) -> str   # human-readable one-line-per-check report

Dependencies: stdlib + PyMuPDF (fitz) only. No pikepdf, no generators.
Detects bank by Producer/Creator//ID/BaseFont, checks PDF structure
(xref, trailer, /ID), fonts (CIDSet, ToUnicode, /W, FontFile2), text
(fitz get_text), op_id laws (era, flag, nodes, gateways), CreationDate lag,
deflate dialect (clean decompression + valid zlib header CM=8/FCHECK).

VTB card-transfer receipts ('Перевод на карту') carry no SBP op_id and no
phone by design: for them the op_id/phone checks report n/a and the card
laws are checked instead (both card masks, commission = VTB tariff 2%
exact to the kopeck min 99 RUB, 0 to VTB, amount + commission = total;
amounts may carry kopecks with a dot separator, e.g. '162.16 ₽').
CreationDate must not precede the displayed operation minute; it may
lag it by much (a receipt PDF is created at download: fresh corpus
+7..+63 s, old 07.10.2026 receipts downloaded 07.11.2026, +31 days).
SBP receipts of all banks are unaffected.

Strict structural checks transferred from strict_pdf_check.py (measured
0 false positives on the calibrated genuine receipts of all 6 banks and on 197/199
in-house files; the 2 misses are the known broken-xref VTB files):
startxref_exact, xref_offsets_exact, xref_objects_covered (the xref-stream
object itself is exempt from self-coverage), stream_length_exact,
eof_trailing_garbage, mupdf_open, mupdf_not_repaired, deflate_decompress,
deflate_zlib_header (CM=8 and (CMF<<8|FLG) % 31 == 0 - NOT the byte-exact
78 9c wrapper and NOT a byte-exact deflate strategy: genuine Java Deflater
output need not be reproducible by any Python zlib candidate, so no such
check is a failure here). The op_id era/node/gateway laws above stay as
they were; the strict candidate's copies of them are warnings only and
were NOT transferred as additional hard rules.
"""
from __future__ import annotations
import re, zlib
from datetime import datetime, timedelta, timezone

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

UTC = timezone.utc

# Era flip per bank (UTC instant of the op_id, not DOY):
_ERA_FLIP = {
    "WB":   datetime(2026, 11, 5, 22, 0, 0, tzinfo=UTC),
    "MTS":  datetime(2026, 11, 6, 20, 0, 0, tzinfo=UTC),
    "VTB":  datetime(2026, 11, 5, 22, 0, 0, tzinfo=UTC),
    "OZON": datetime(2026, 11, 6,  1, 30, 0, tzinfo=UTC),
    "RSHB": datetime(2000,  1,  1,  0, 0, 0, tzinfo=UTC),  # always G-era
    "YANDEX": datetime(2026, 11, 6, 20, 0, 0, tzinfo=UTC),
}

# (route, suffix) pairs attested on proven-genuine receipts across eras
# (project corpus + public genuine PDFs).  The binary flip model alone
# falsely cut historical genuine: Ozon G/0011870501 in October, MTS
# G/0011871001 on 26-27.10, Ozon B-IDs minutes after the Ozon flip, ...
_ATTESTED_ERA_PAIRS = {
    ("B", "0011750703"), ("G", "0011870501"), ("G", "0011870502"),
    ("B", "0011870702"), ("G", "0011871001"), ("B", "0011880703"),
    ("G", "0011880703"), ("B", "0011881102"), ("G", "0011890101"),
    ("B", "0011850501"), ("G", "0011770901"),
}
_SUFFIX_NEW, _SUFFIX_OLD = "0011890101", "0011881102"

_GW_A = {"1000","1001","1002","1003","1004","1006","1007","1008","1020"}
_GW_B = {"1009","1010","1011","1012","1013","1014","1016","1017","1018"}

_NODES = {
    "WB":   set("1249") | {"B", "E", "L", "W"} | set("235789") | set("ACIMPVZ"),
    "MTS":  set("CE4R") | set("034DETL") | set("OXH"),
    "VTB":  set("1245689") | set("5678AEHNOTUWP"),
    # RSHB clean corpus (23 unique, lag 0..60s): exactly the generator pool;
    # 'G' is 0/23 (was only in this validator pool), '1' is replica-only.
    "RSHB": set("2359BCDFHRUZ"),
    "OZON": set("124") | set("1234789") | set("ADEHIJLPSZ"),
    "YANDEX": set(),
}
_FLAGS = {
    "WB": {"0","1"}, "MTS": {"0","1","7"}, "VTB": {"0","1","2"},
    "RSHB": {"0","1","2"}, "OZON": {"0","1","2"}, "YANDEX": {"0","1","2"},
}

# Era-G (suffix 0011890101) STRICT pools from the proven-genuine corpus.
# Updated 2026-11-10: narrow era-G pools are retired for ALL banks. WB/MTS
# proved the node/gateway set keeps widening over time (WB 'D', MTS 'B'/'D'),
# and the Ozon era-G corpus is only 4 receipts (nodes {1,2,4}) — far too few
# to freeze a pool without false-redding fresh genuine Ozon receipts. The
# hard era-G laws for every bank are now: the 32-char grammar, the UTC-keyed
# era (route/suffix), the prefix<->gateway binding (A->1000..1008/1020,
# B->1009..1018; 0 violations across the whole corpus), flag and tail.
# Nodes use the wide alnum alphabet; gateways the full prefix-bound pools.
_GW_ERA_G = {}
_NODES_ERA_G = {}
# Displayed operation time (MSK, with seconds) minus the op_id UTC
# instant, era G genuine bands.  New live MTS transfers on 08.11 attest
# 2.842 / 5.116 / 5.713 seconds, extending the prior 6..11 sample.
# WB remains approximately 0..2 seconds after whole-second truncation.
_SHIFT_ERA_G = {"WB": (0, 2), "MTS": (2, 11)}

# MIR BIN (first 6) -> issuing bank for VTB card receipts.  A stated
# recipient bank contradicting the card BIN is cut by the external
# checker as inconsistent receipt data (case 07.11.2026: card
# 2200 24** **** 4335 = VTB BIN 220024 labelled "Совкомбанк", whose MIR
# BINs are 220027/220071/220074).  Only high-confidence BINs are listed.
_CARD_BIN_BANK = {
    "220024": "ВТБ", "220040": "ВТБ", "220065": "ВТБ",
    "220070": "Т-Банк",
    "220220": "Сбербанк", "220100": "Сбербанк",
    "220015": "Альфа-Банк",
    "220027": "Совкомбанк", "220071": "Совкомбанк", "220074": "Совкомбанк",
    "220432": "Озон Банк",
    "220001": "Газпромбанк", "220056": "Газпромбанк",
    "220030": "Райффайзен Банк",
}


def _norm_bank_name(s: str) -> str:
    s = (s or "").strip().replace("\u2011", "-").casefold()
    if s.startswith("t-"):  # Latin T in 'T-Банк' vs Cyrillic Т
        s = "т-" + s[2:]
    return s

_BANK_FP = {
    "WB":   {"producer": "OpenPDF 1.3.32",
             "creator_re": r"^JasperReports Library version 7\.0\.3",
             "id_required": True, "id_equal": True,
             "fonts": ["GoogleSans-Medium","GoogleSans-Regular","GoogleSans-Bold"]},
    "MTS":  {"producer_re": r"OpenPDF 1\.3\.30",
             "creator_re": r"^dbo-print-forms",
             "id_required": True, "id_equal": False,
             "fonts": []},
    "VTB":  {"producer_re": r"openhtmltopdf", "creator_re": None,
             "id_required": True, "id_equal": True,
             "fonts": ["SFProDisplay-Regular"]},
    "RSHB": {"producer": "OpenPDF 1.3.32",
             "creator_re": r"^JasperReports Library version 7\.0\.1",
             "id_required": True, "id_equal": True,
             "fonts": ["Onest-SemiBold","Onest-Regular"]},
    "OZON": {"producer_re": r"^Skia/PDF m\d+", "creator_re": r"^Chromium",
             "id_required": False, "id_equal": None,
             "fonts": ["GTEestiProDisplay-Regular","GTEestiProDisplay-Bold"]},
    "YANDEX": {"producer_re": r"OpenPDF 1\.3\.32",
             "creator_re": r"^JasperReports Library version 6\.21",
             "id_required": True, "id_equal": True,
             "fonts": ["YSText-Regular","YSText-Medium"]},
}

_OPID_RE = re.compile(r"[AB][0-9A-Z]{31}")
_PHONE_RE = re.compile(r"\+7\s*\(?\s*\d{3}\s*\)?\s*\d{3}[\s\u2011\-]?\d{2}[\s\u2011\-]?\d{2}|[78]\d{10}")
_AMOUNT_RE = re.compile(r"\d[\d\s\u00a0]*[.,]?\d*\s*\u20bd")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})[^\d]*?(\d{1,2}:\d{2}(?::\d{2})?)")
# ─────────────────────────────────────────────────────────────────────
# Low-level PDF parsing (no pikepdf)
# ─────────────────────────────────────────────────────────────────────
def _find_trailer(raw: bytes) -> bytes:
    m = list(re.finditer(rb"trailer\s*<<", raw))
    if not m:
        return b""
    start = m[-1].start()
    depth = 0
    i = raw.find(b"<<", start) + 2
    while i < len(raw):
        if raw[i:i+2] == b"<<":
            depth += 1; i += 2
        elif raw[i:i+2] == b">>":
            if depth == 0:
                return raw[start:i+2]
            depth -= 1; i += 2
        else:
            i += 1
    return raw[start:]

def _trailer_id(raw: bytes):
    m = re.search(rb"/ID\s*\[<([0-9a-fA-F]{32})>\s*<([0-9a-fA-F]{32})>\]", raw)
    if not m:
        return None
    return (m.group(1).decode(), m.group(2).decode())

def _obj_body(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^" + str(num).encode() + rb"\s+0\s+obj\r?\n", raw)
    if not m:
        return b""
    start = m.end()
    end = raw.find(b"endobj", start)
    return raw[start:end] if end > 0 else b""

def _info_dict(raw: bytes) -> dict:
    out = {}
    tr = _find_trailer(raw)
    # MTS uses an XRef stream (PDF 1.5+) instead of a classic trailer;
    # the trailer dict lives in the object pointed to by startxref.
    if not tr:
        tr = _xref_stream_dict(raw)
    mi = re.search(rb"/Info\s+(\d+)\s+\d+\s+R", tr)
    if mi:
        body = _obj_body(raw, int(mi.group(1)))
        if body:
            for key in (b"/Producer", b"/Creator", b"/CreationDate", b"/ModDate"):
                mm = re.search(re.escape(key) + rb"\s*\(([^)]*)\)", body)
                if mm:
                    out[key.decode()] = mm.group(1).decode("latin-1", "replace")
            mm = re.search(rb"/Title\s*<([0-9A-Fa-f]+)>", body)
            if mm and "/Title" not in out:
                out["/Title"] = "<hex:" + mm.group(1).decode()[:16] + ">"
    return out

def _startxref_obj(raw: bytes) -> int:
    """Object number of the XRef stream (PDF 1.5+), or 0 if classic xref."""
    m = re.search(rb"startxref\s+(\d+)\s*%%EOF", raw)
    if not m:
        return 0
    pos = int(m.group(1))
    # find the object that starts at or near this byte offset
    om = re.search(rb"(?m)^(\d+)\s+0\s+obj", raw[pos:pos+200])
    return int(om.group(1)) if om else 0

def _xref_stream_dict(raw: bytes) -> bytes:
    """Return the dict of the XRef stream object (PDF 1.5+ cross-reference stream)."""
    n = _startxref_obj(raw)
    if not n:
        return b""
    body = _obj_body(raw, n)
    # the dict is everything before 'stream'
    si = body.find(b"stream")
    return body[:si] if si >= 0 else body

def _xref_ok(raw: bytes) -> bool:
    # Classic xref table
    m = re.search(rb"\nxref\s*\n\s*\d+\s+\d+\s*\n", raw)
    if m:
        seg = raw[m.end():m.end()+25]
        return bool(re.match(rb"0000000000\s+65535\s+f\s*\r?\n", seg))
    # PDF 1.5+ XRef stream (MTB): accept as valid structure
    return _startxref_obj(raw) > 0

def _flate_streams(raw: bytes):
    out = []
    for m in re.finditer(rb"(?m)^(\d+)\s+0\s+obj", raw):
        n = int(m.group(1))
        start = m.end()
        end = raw.find(b"endobj", start)
        if end < 0:
            continue
        body = raw[start:end]
        if b"/FlateDecode" not in body:
            continue
        si = body.find(b"stream")
        if si < 0:
            continue
        s = si + 6
        if body[s:s+2] == b"\r\n": s += 2
        elif body[s:s+1] == b"\n": s += 1
        lm = re.search(rb"/Length\s+(\d+)", body[:si])
        if lm:
            data = body[s:s+int(lm.group(1))]
        else:
            e = body.rfind(b"endstream")
            if e < 0:
                continue
            data = body[s:e]
            if data.endswith(b"\n"): data = data[:-1]
            if data.endswith(b"\r"): data = data[:-1]
        hdr = data[:2] if len(data) >= 2 else b""
        ok = False
        try:
            zlib.decompress(data); ok = True
        except Exception:
            pass
        out.append((n, hdr, ok, int(lm.group(1)) if lm else None, len(data)))
    return out

def _all_decompressed(raw: bytes) -> bytes:
    """Concatenate every FlateDecode stream body (decompressed). MTS packs its
    font objects inside compressed object streams, so font keys like
    /FontFile2, /ToUnicode, /W only appear after decompression."""
    out = b""
    for m in re.finditer(rb"(?m)^(\d+)\s+0\s+obj", raw):
        start = m.end(); end = raw.find(b"endobj", start)
        if end < 0:
            continue
        body = raw[start:end]
        if b"/FlateDecode" not in body:
            continue
        si = body.find(b"stream")
        if si < 0:
            continue
        s = si + 6
        if body[s:s+2] == b"\r\n": s += 2
        elif body[s:s+1] == b"\n": s += 1
        # prefer declared /Length for exact extraction
        lm = re.search(rb"/Length\s+(\d+)", body[:si])
        if lm:
            data = body[s:s+int(lm.group(1))]
        else:
            e = body.rfind(b"endstream")
            if e < 0:
                continue
            data = body[s:e]
            if data.endswith(b"\n"): data = data[:-1]
            if data.endswith(b"\r"): data = data[:-1]
        try:
            out += zlib.decompress(data)
        except Exception:
            pass
    return out

def _font_objects(raw: bytes):
    """Font descriptor keys (/FontFile2, /CIDSet) live in separate FontDescriptor
    objects; MTS even packs them inside compressed object streams. So we scan
    the raw bytes AND all decompressed stream bodies for presence."""
    haystack = raw + _all_decompressed(raw)
    return [{
        "has_fontfile2": b"/FontFile2" in haystack,
        "has_cidset": b"/CIDSet" in haystack,
        "has_tounicode": b"/ToUnicode" in haystack,
        "has_w": bool(re.search(rb"/W\s*\[", haystack)),
    }]
# ─────────────────────────────────────────────────────────────────────
# Strict structural checks (transferred from strict_pdf_check.py)
# Only the rules measured at 0 false positives on the genuine corpus are
# hard failures in validate(): startxref_exact, xref_offsets_exact,
# xref_objects_covered, stream_length_exact, eof_trailing_garbage,
# mupdf_open / mupdf_not_repaired, deflate_decompress, deflate_zlib_header.
# ─────────────────────────────────────────────────────────────────────
_OBJ_HDR_RE = re.compile(rb"(?m)(?:(?<=\n)|^)(\d+) (\d+) obj(?=[\s<(\[/]|$)")

def _obj_headers(raw: bytes):
    """[(num, gen, offset)] - all direct object headers in the file."""
    return [(int(m.group(1)), int(m.group(2)), m.start())
            for m in _OBJ_HDR_RE.finditer(raw)]

def _obj_body_span(raw: bytes, hdr_end: int):
    """(body_start, body_end); body_end is the position of 'endobj'."""
    end = raw.find(b"endobj", hdr_end)
    return hdr_end, (end if end >= 0 else len(raw))

def _obj_dict_and_stream(raw: bytes, num: int, gen: int = 0):
    """For object num: (dict_bytes, data_start|None, header_offset).

    dict_bytes is everything up to the 'stream' keyword (or the whole
    body without endobj)."""
    pat = re.compile(rb"(?m)(?:(?<=\n)|^)" + str(num).encode() + rb" "
                     + str(gen).encode() + rb" obj(?=[\s<(\[/]|$)")
    m = pat.search(raw)
    if not m:
        return b"", None, -1
    _, bend = _obj_body_span(raw, m.end())
    body = raw[m.end():bend]
    si = body.find(b"stream")
    while si >= 0 and body[max(0, si - 3):si] == b"end":
        si = body.find(b"stream", si + 6)
    if si < 0:
        return body, None, m.start()
    return body[:si], m.end() + si + 6, m.start()

def _resolve_length(raw: bytes, dct: bytes):
    """-> (declared:int|None, indirect:bool). /Length direct or 'N G R'."""
    m = re.search(rb"/Length\s+(\d+)\s+(\d+)\s+R", dct)
    if m:
        body, _, _ = _obj_dict_and_stream(raw, int(m.group(1)), int(m.group(2)))
        mm = re.search(rb"^\s*(\d+)", body)
        return (int(mm.group(1)) if mm else None), True
    # (?![0-9A-Za-z]) rejects /Length1, /Length2, /Length3 (fonts).
    m = re.search(rb"/Length(?![0-9A-Za-z])\s+(\d+)", dct)
    if m:
        return int(m.group(1)), False
    return None, False

def _stream_data_span(raw: bytes, data_start: int, declared: int | None):
    """-> (data:bytes, exact_by_declared:bool).

    Trust declared first: data = raw[start:start+declared], immediately
    followed by (EOL)?endstream. Otherwise locate endstream and strip the
    single EOL before it (PDF 32000 7.3.8.2: the EOL before endstream is
    not part of the stream data)."""
    if declared is not None:
        end = data_start + declared
        tail = raw[end:end + 11]
        if (tail.startswith(b"endstream") or tail.startswith(b"\nendstream")
                or tail.startswith(b"\rendstream")
                or tail.startswith(b"\r\nendstream")):
            return raw[data_start:end], True
    pos = raw.find(b"endstream", data_start)
    if pos < 0:
        return raw[data_start:], False
    data = raw[data_start:pos]
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith((b"\n", b"\r")):
        data = data[:-1]
    return data, False

def _parse_streams(raw: bytes):
    """All streams of direct objects.

    -> [{num, gen, offset, declared, indirect, data, exact, eol_ok,
         is_flate, dict}]"""
    out = []
    for num, gen, off in _obj_headers(raw):
        m = _OBJ_HDR_RE.match(raw, off)
        _, bend = _obj_body_span(raw, m.end())
        body = raw[m.end():bend]
        si = body.find(b"stream")
        while si >= 0 and body[max(0, si - 3):si] == b"end":
            si = body.find(b"stream", si + 6)
        if si < 0:
            continue
        dct = body[:si]
        ds = m.end() + si + 6
        # After 'stream' the spec requires CRLF or LF.
        eol_ok = True
        if raw[ds:ds + 2] == b"\r\n":
            ds += 2
        elif raw[ds:ds + 1] in (b"\n", b"\r"):
            ds += 1
        else:
            eol_ok = False
        declared, indirect = _resolve_length(raw, dct)
        data, exact = _stream_data_span(raw, ds, declared)
        out.append({
            "num": num, "gen": gen, "offset": off, "dict": dct,
            "declared": declared, "indirect": indirect, "data": data,
            "exact": exact, "eol_ok": eol_ok,
            "is_flate": b"/FlateDecode" in dct,
        })
    return out

def _classic_xref_coverage(raw: bytes):
    """Exact offsets of all classic xref sections.

    -> (covered:{num: offset} from in-use 'n' entries of every section,
        n_sections, bad, total, first_bad)"""
    covered: dict[int, int] = {}
    sections = list(re.finditer(rb"(?m)^xref[ \t]*\r?$", raw))
    bad, total, first_bad = 0, 0, ""
    for sec in sections:
        pos = sec.end()
        if raw[pos:pos + 1] == b"\n":
            pos += 1
        while True:
            hm = re.match(rb"(\d+) (\d+)[ \t]*\r?\n", raw[pos:pos + 32])
            if not hm:
                break
            first, count = int(hm.group(1)), int(hm.group(2))
            pos += hm.end()
            for i in range(count):
                em = re.match(rb"(\d{10}) (\d{5}) ([nf])(?:[ \t]|\r)?\n?",
                              raw[pos:pos + 24])
                if not em:
                    bad += 1
                    if not first_bad:
                        first_bad = f"obj {first + i}: unparsable entry"
                    nl = raw.find(b"\n", pos)
                    pos = (nl + 1) if nl >= 0 else len(raw)
                    continue
                off, gen, typ = int(em.group(1)), int(em.group(2)), em.group(3)
                pos += em.end()
                if typ != b"n":
                    continue
                total += 1
                num = first + i
                covered[num] = off
                expect = b"%d %d obj" % (num, gen)
                if raw[off:off + len(expect)] != expect:
                    bad += 1
                    if not first_bad:
                        first_bad = (f"obj {num}: xref->{off}, there="
                                     f"{raw[off:off + 24]!r}")
    return covered, len(sections), bad, total, first_bad

def _xref_stream_entries(raw: bytes, sx: int):
    """Entries of the xref stream whose object starts at offset sx.

    -> ({num: offset} for type-1 entries, detail:str, xref_obj_num)
       or (None, err, None). The xref-stream object is not part of its
       own coverage: a type-1 entry for itself is not required (genuine
       MTS/OpenPDF files are written that way - verified on donors)."""
    hm = _OBJ_HDR_RE.match(raw, sx)
    if not hm:
        return None, "no object header at startxref", None
    num = int(hm.group(1))
    dct, ds, _ = _obj_dict_and_stream(raw, num, int(hm.group(2)))
    if ds is None or b"/XRef" not in dct:
        return None, f"object {num} at startxref is not an XRef stream", None
    wm = re.search(rb"/W\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s*\]", dct)
    sm = re.search(rb"/Size\s+(\d+)", dct)
    if not wm or not sm:
        return None, "XRef stream without /W or /Size", None
    w = (int(wm.group(1)), int(wm.group(2)), int(wm.group(3)))
    size = int(sm.group(1))
    im = re.search(rb"/Index\s*\[([0-9\s]+)\]", dct)
    pairs = []
    if im:
        vals = [int(x) for x in im.group(1).split()]
        pairs = list(zip(vals[0::2], vals[1::2]))
    else:
        pairs = [(0, size)]
    declared, _ = _resolve_length(raw, dct)
    # ds from _obj_dict_and_stream is right after the word 'stream'.
    if raw[ds:ds + 2] == b"\r\n":
        ds += 2
    elif raw[ds:ds + 1] in (b"\n", b"\r"):
        ds += 1
    data, _ = _stream_data_span(raw, ds, declared)
    if b"/FlateDecode" in dct:
        try:
            data = zlib.decompress(data)
        except Exception as e:
            return None, f"XRef stream deflate broken: {e}", None
    width = sum(w)
    if width == 0:
        return None, "XRef stream /W sums to 0", None
    covered: dict[int, int] = {}
    p = 0
    for first, count in pairs:
        for i in range(count):
            ent = data[p:p + width]
            p += width
            if len(ent) < width:
                return covered, "XRef stream truncated", num
            f0 = int.from_bytes(ent[:w[0]], "big") if w[0] else 1
            f1 = int.from_bytes(ent[w[0]:w[0] + w[1]], "big") if w[1] else 0
            if f0 == 1:
                covered[first + i] = f1
    return covered, (f"xref-stream obj {num}, /W={list(w)}, entries type1="
                     f"{len(covered)}"), num

def _startxref_target(raw: bytes):
    """-> (startxref_value|None, covered_from_stream|None, xref_obj|None,
           passed, detail) for the startxref_exact check."""
    ms = list(re.finditer(rb"startxref\s*(\d+)\s*%%EOF", raw))
    if not ms:
        return None, None, None, False, "no startxref/%%EOF found"
    sx = int(ms[-1].group(1))
    if raw[sx:sx + 4] == b"xref":
        return (sx, None, None, True,
                f"startxref={sx} -> classic xref (of {len(ms)} marker(s))")
    covered, detail, xref_obj = _xref_stream_entries(raw, sx)
    if covered is not None:
        return sx, covered, xref_obj, True, f"startxref={sx} -> {detail}"
    return (sx, None, None, False,
            f"startxref={sx} does not point at xref/xref-stream: {detail}; "
            f"bytes there: {raw[sx:sx + 32]!r}")
# ─────────────────────────────────────────────────────────────────────
# Bank detection
# ─────────────────────────────────────────────────────────────────────
def _detect_bank(raw: bytes):
    info = _info_dict(raw)
    prod = info.get("/Producer") or ""
    crea = info.get("/Creator") or ""
    tid = _trailer_id(raw)
    id_present = tid is not None
    bases = {b.decode() for b in re.findall(rb"/BaseFont\s*/([A-Z]{6}\+[A-Za-z\-]+)", raw)}

    if any("YSText" in b for b in bases) or \
       re.search(_BANK_FP["YANDEX"]["creator_re"] or r"", crea):
        return "YANDEX", prod, crea, id_present, bases
    if re.search(_BANK_FP["OZON"]["producer_re"], prod) and \
       re.search(_BANK_FP["OZON"]["creator_re"] or r"", crea) and not id_present:
        return "OZON", prod, crea, id_present, bases
    if re.search(_BANK_FP["VTB"]["producer_re"], prod):
        return "VTB", prod, crea, id_present, bases
    if re.search(_BANK_FP["MTS"]["creator_re"] or r"", crea):
        return "MTS", prod, crea, id_present, bases
    if re.search(_BANK_FP["WB"]["creator_re"] or r"", crea):
        return "WB", prod, crea, id_present, bases
    if re.search(_BANK_FP["RSHB"]["creator_re"] or r"", crea):
        return "RSHB", prod, crea, id_present, bases
    if any("GoogleSans" in b for b in bases):
        return "WB", prod, crea, id_present, bases
    if any("Onest" in b for b in bases):
        return "RSHB", prod, crea, id_present, bases
    if any("SFProDisplay" in b for b in bases):
        return "VTB", prod, crea, id_present, bases
    if any("GTEestiProDisplay" in b for b in bases):
        return "OZON", prod, crea, id_present, bases
    return None, prod, crea, id_present, bases

def _extract_opid(text: str):
    """MTS/VTB/WB wrap op_id across 2 lines - search whitespace-stripped text."""
    no_ws = re.sub(r"\s+", "", text)
    m = _OPID_RE.search(no_ws)
    return m.group(0) if m else None

def _parse_opid(op_id: str):
    if not op_id or len(op_id) != 32 or op_id[0] not in "AB":
        return None
    try:
        year = 2020 + int(op_id[1])
        doy = int(op_id[2:5]); hh = int(op_id[5:7]); mm = int(op_id[7:9])
        ss = int(op_id[9:11]); ms = int(op_id[11:14])
        flag = op_id[14]; node = op_id[15]; tail = op_id[16]
        route = op_id[17]; gw = op_id[18:22]; suffix = op_id[22:32]
        base = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=doy-1)
        # Include the ms field: CD-ID lags are measured against the full
        # ID instant (genuine Ozon G band 5.32..12.59s is ms-exact).
        op_utc = base.replace(hour=hh, minute=mm, second=ss) + timedelta(milliseconds=ms)
        return {"year": year, "doy": doy, "hh": hh, "mm": mm, "ss": ss, "ms": ms,
                "flag": flag, "node": node, "tail": tail, "route": route,
                "gw": gw, "suffix": suffix, "op_utc": op_utc}
    except Exception:
        return None
# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────
def validate(pdf_bytes: bytes) -> dict:
    """Validate a receipt PDF. Returns {bank, passed, checks}."""
    checks = []
    def ok(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    if not pdf_bytes or not isinstance(pdf_bytes, (bytes, bytearray)) \
       or pdf_bytes[:5] != b"%PDF-":
        ok("pdf_header", False, "not a PDF (no %PDF- header)")
        return {"bank": None, "passed": False, "checks": checks}

    raw = bytes(pdf_bytes)

    # 1. Bank detection
    bank, prod, crea, id_present, bases = _detect_bank(raw)
    ok("bank_detect", bank is not None,
        f"bank={bank} producer={prod!r} creator={crea!r} fonts={sorted(bases)}")
    if bank is None:
        ok("producer_known", False, f"unknown Producer={prod!r}")
        ok("creator_known", False, f"unknown Creator={crea!r}")
        return {"bank": None, "passed": False, "checks": checks}

    fp = _BANK_FP[bank]

    # 2. Producer / Creator
    if "producer" in fp:
        ok("producer", prod == fp["producer"], f"{prod!r} vs {fp['producer']!r}")
    else:
        ok("producer", bool(re.search(fp["producer_re"], prod)), f"{prod!r}")
    if fp.get("creator_re"):
        ok("creator", bool(re.search(fp["creator_re"], crea)), f"{crea!r}")
    else:
        ok("creator", True, "Creator not required for this bank")

    # 3. PDF structure: xref, trailer, /ID
    ok("xref_table", _xref_ok(raw), "xref (classic table or XRef stream) in place")

    # 3b. Strict structural checks (transferred from strict_pdf_check.py;
    # measured 0 FP on genuine receipts of all banks - see module docstring).
    idx_eof = raw.rfind(b"%%EOF")
    garbage = raw[idx_eof + 5:] if idx_eof >= 0 else b""
    ok("eof_trailing_garbage",
       bool(idx_eof >= 0 and garbage.strip(b"\r\n \t\x00") == b""),
       f"{len(garbage)} byte(s) after last %%EOF, "
       f"non-ws: {garbage.strip()[:24]!r}")
    _sx, stream_covered, xref_obj, sx_ok, sx_detail = _startxref_target(raw)
    ok("startxref_exact", sx_ok, sx_detail)
    covered, n_sections, xref_bad, xref_total, xref_first_bad = \
        _classic_xref_coverage(raw)
    if stream_covered is not None:
        bad_stream = [
            f"obj {n}: xref-stream->{o}, there={raw[o:o + 24]!r}"
            for n, o in stream_covered.items()
            if raw[o:o + len(b"%d" % n)] != b"%d" % n
            or not re.match(rb"%d \d+ obj" % n, raw[o:o + 32])]
        ok("xref_offsets_exact", not bad_stream,
           f"{len(stream_covered)} type-1 entries, bad={len(bad_stream)}"
           + (f"; first: {bad_stream[0]}" if bad_stream else ""))
        covered = stream_covered
    elif n_sections:
        ok("xref_offsets_exact", xref_bad == 0,
           f"{xref_total} in-use entries in {n_sections} section(s), "
           f"bad={xref_bad}"
           + (f"; first: {xref_first_bad}" if xref_first_bad else ""))
    else:
        ok("xref_offsets_exact", False,
           "no classic xref and no xref stream at startxref")
    if covered:
        # An object superseded by a later revision must be pointed at its
        # LAST header occurrence. The xref-stream object itself is exempt
        # from self-coverage (see _xref_stream_entries).
        last_off: dict[int, int] = {}
        for n_obj, _g_obj, o_obj in _obj_headers(raw):
            if n_obj != xref_obj:
                last_off[n_obj] = o_obj
        missing = [f"obj {n_obj} @{last_off[n_obj]}, xref->{covered.get(n_obj)}"
                   for n_obj in last_off if covered.get(n_obj) != last_off[n_obj]]
        ok("xref_objects_covered", not missing,
           f"{len(last_off)} direct objects, uncovered/mismatched="
           f"{len(missing)}" + (f"; first: {missing[0]}" if missing else ""))
    else:
        ok("xref_objects_covered", False, "no xref entries parsed")
    streams = _parse_streams(raw)
    bad_len = []
    for s in streams:
        if s["declared"] is None:
            bad_len.append(f"obj {s['num']}: no /Length")
        elif not s["exact"] or len(s["data"]) != s["declared"]:
            bad_len.append(f"obj {s['num']}: declared={s['declared']} "
                           f"actual={len(s['data'])} exact={s['exact']}")
        if not s["eol_ok"]:
            bad_len.append(f"obj {s['num']}: no EOL after 'stream'")
    ok("stream_length_exact", not bad_len and bool(streams),
       f"{len(streams)} streams, bad={len(bad_len)}"
       + (f"; first: {bad_len[0]}" if bad_len else "; all /Length exact"))

    tr = _find_trailer(raw) or _xref_stream_dict(raw)
    ok("trailer_root", b"/Root" in tr, "trailer/xref-stream has /Root")
    ok("trailer_info", b"/Info" in tr, "trailer/xref-stream has /Info")
    if fp["id_required"]:
        ok("id_present", id_present, "/ID present in trailer")
        if id_present:
            tid = _trailer_id(raw)
            eq = tid[0] == tid[1]
            if fp["id_equal"] is True:
                ok("id_halves_equal", eq, f"both halves equal: {tid[0]}")
            elif fp["id_equal"] is False:
                ok("id_halves_equal", not eq,
                   f"halves differ (UUIDv4+secrets): {tid[0]} != {tid[1]}")
            else:
                ok("id_halves_equal", True, "/ID format checked")
    else:
        ok("id_absent", not id_present, "Skia writes no /ID - /ID absent")

    # 4. Fonts: CIDSet, ToUnicode, /W, FontFile2
    # /CIDSet is emitted by OpenPDF (WB/VTB/RSHB) but NOT by MTS (OpenPDF 1.3.30,
    # no CIDSet) or Skia/Chromium (Ozon) - optional for those two banks.
    fonts = _font_objects(raw)
    cidset_required = bank not in ("MTS", "OZON")
    cidset_present = any(f["has_cidset"] for f in fonts)
    if cidset_required:
        ok("font_cidset", cidset_present, f"CIDSet present: {cidset_present}")
    else:
        ok("font_cidset", True, f"{bank} does not emit /CIDSet (optional)")
    ok("font_tounicode", any(f["has_tounicode"] for f in fonts),
       f"ToUnicode in {sum(f['has_tounicode'] for f in fonts)} fonts")
    ok("font_w_array", any(f["has_w"] for f in fonts),
       f"/W array in {sum(f['has_w'] for f in fonts)} fonts")
    ok("font_file2", any(f["has_fontfile2"] for f in fonts),
       f"FontFile2 in {sum(f['has_fontfile2'] for f in fonts)} fonts")
    if fp["fonts"]:
        fam = fp["fonts"][0].split("-")[0]
        ok("font_family", any(fam in b for b in bases),
           f"expect {fam}*, found: {sorted(bases)}")
    else:
        ok("font_family", True, "MTS uses /F2 /F3 without /BaseFont in Type0")

    # 5. Text via fitz (+ strict MuPDF checks: a file MuPDF had to repair
    # has broken xref/offsets even if the text still extracts).
    text = ""
    mupdf_open_ok, mupdf_repaired, mupdf_npages = False, False, 0
    mupdf_err = ""
    if fitz is not None:
        try:
            doc = fitz.open(stream=raw, filetype="pdf")
            try:
                text = "".join(p.get_text() for p in doc)
                mupdf_repaired = bool(doc.is_repaired)
                mupdf_npages = len(doc)
                mupdf_open_ok = True
            finally:
                doc.close()
        except Exception as e:
            mupdf_err = str(e)
            ok("text_extract", False, f"fitz failed: {e}")
    else:
        mupdf_err = "PyMuPDF (fitz) not installed"
        ok("text_extract", False, "PyMuPDF (fitz) not installed")
    ok("text_extract", bool(text.strip()),
       f"{len(text)} chars" if text else "empty text")
    ok("mupdf_open", mupdf_open_ok and mupdf_npages >= 1,
       f"opened, {mupdf_npages} page(s)" if mupdf_open_ok
       else f"fitz.open failed: {mupdf_err}")
    ok("mupdf_not_repaired", mupdf_open_ok and not mupdf_repaired,
       "MuPDF repaired the file (broken xref/offsets)" if mupdf_repaired
       else ("MuPDF did not need repair" if mupdf_open_ok
             else "not opened - cannot prove absence of repair"))

    # 6. op_id laws (VTB card transfers carry no SBP op_id by design;
    # their field/arithmetic laws are checked in sections 7 and 9 instead)
    is_vtb_card = bool(bank == "VTB" and text
                       and "Перевод на карту" in text
                       and "Карта получателя" in text)
    # Other bot products without an SBP op_id by design (they used to be
    # falsely cut by the op_id laws): Ozon card transfers carry a
    # 'po-...' document id, MTS cards a 12-digit transaction code, EXI
    # statements and Yandex statements are documents, not receipts.
    is_ozon_card = bool(bank == "OZON" and text
                        and re.search(r"\bpo-[0-9a-fA-F-]{8,}", text))
    # Internal Ozon transfers ('Перевод', Основной счёт) carry no
    # operation ID at all in the genuine corpus.  Require the ID to be
    # truly absent (the label phrase alone is unreliable: genuine SBP
    # receipts break it across lines).
    is_ozon_transfer = bool(bank == "OZON" and text
                            and not _OPID_RE.search(re.sub(r"\s+", "", text))
                            and "Основной счёт" in text)
    is_mts_card = bool(bank == "MTS" and text
                       and "Код транзакции" in text
                       and "Карта списания" in text)
    is_exi_stmt = bool(bank == "MTS" and text and "Счет-выписка" in text)
    is_yandex_stmt = bool(bank == "YANDEX" and text
                          and "Выписка по договору" in text)
    is_no_opid_product = bool(is_vtb_card or is_ozon_card or is_ozon_transfer
                              or is_mts_card or is_exi_stmt or is_yandex_stmt)
    op_id = _extract_opid(text) if text else None
    if is_no_opid_product:
        ok("opid_present", op_id is None,
           "no SBP op_id in this product by design" if op_id is None
           else f"unexpected SBP op_id in a no-opid product: {op_id}")
    else:
        ok("opid_present", op_id is not None,
           f"op_id={op_id}" if op_id else "32-char op_id not found in text")
    parsed = _parse_opid(op_id) if op_id else None
    if is_no_opid_product:
        for nm in ("opid_format", "opid_era", "opid_flag", "opid_node",
                   "opid_gateway", "opid_tail"):
            ok(nm, True, "n/a: product without an SBP op_id by design")
    elif parsed:
        ok("opid_format", True,
           f"pfx={op_id[0]} year={parsed['year']} doy={parsed['doy']} "
           f"time={parsed['hh']:02d}:{parsed['mm']:02d}:{parsed['ss']:02d} "
           f"flag={parsed['flag']} node={parsed['node']} route={parsed['route']} "
           f"gw={parsed['gw']} suffix={parsed['suffix']}")
        flip = _ERA_FLIP[bank]
        new_era = parsed["op_utc"] >= flip
        if bank == "RSHB":
            ok("opid_era", parsed["route"] == "G" and parsed["suffix"] == _SUFFIX_NEW,
               f"RSHB always G/{_SUFFIX_NEW}, found {parsed['route']}/{parsed['suffix']}")
        elif (parsed["route"], parsed["suffix"]) in _ATTESTED_ERA_PAIRS:
            ok("opid_era", True,
               f"attested era pair {parsed['route']}/{parsed['suffix']}")
        elif new_era:
            ok("opid_era", parsed["route"] == "G" and parsed["suffix"] == _SUFFIX_NEW,
               f"new era -> G/{_SUFFIX_NEW}, found {parsed['route']}/{parsed['suffix']}")
        else:
            # old era: route B; suffix is DOY/bank-specific (0011871001, 0011880703,
            # 0011881102, ...) - accept any 10-digit suffix with route B.
            ok("opid_era", parsed["route"] == "B",
               f"old era -> route B, found {parsed['route']} (suffix {parsed['suffix']})")
        ok("opid_flag", parsed["flag"] in _FLAGS[bank],
           f"flag={parsed['flag']!r} valid for {bank}: {sorted(_FLAGS[bank])}")
        if parsed["suffix"] == _SUFFIX_NEW and bank in _NODES_ERA_G:
            ok("opid_node", parsed["node"] in _NODES_ERA_G[bank],
               f"node={parsed['node']!r} era-G strict pool for {bank}: "
               f"{sorted(_NODES_ERA_G[bank])}")
        else:
            # Node is a single NSPK digit/Latin letter. Per-bank narrow pools
            # were retired 2026-11-10: every bank's genuine node set keeps
            # widening over time, so a frozen pool only false-reds real
            # receipts. Forgery detection relies on era, prefix<->gateway
            # binding, counters and dedup — never on the node alphabet.
            ok("opid_node", parsed["node"].isalnum() and len(parsed["node"]) == 1,
               f"node={parsed['node']!r} (single alnum NSPK node)")
        if parsed["suffix"] == _SUFFIX_NEW and bank in _GW_ERA_G:
            era_pool = _GW_ERA_G[bank][op_id[0]]
            ok("opid_gateway", parsed["gw"] in era_pool,
               f"gw={parsed['gw']} era-G strict pool for {bank}/{op_id[0]}: "
               f"{sorted(era_pool)}")
        else:
            gw_pool = _GW_A if op_id[0] == "A" else _GW_B
            ok("opid_gateway", parsed["gw"] in gw_pool,
               f"gw={parsed['gw']} for prefix {op_id[0]}")
        ok("opid_tail", parsed["tail"] == "0", f"tail digit = 0, found {parsed['tail']!r}")
        if parsed["suffix"] == _SUFFIX_NEW and bank in _SHIFT_ERA_G:
            dm = _DATE_RE.search(text or "")
            shift = None
            if dm and dm.group(2).count(":") == 2:
                try:
                    disp_wall = datetime.strptime(
                        dm.group(1) + " " + dm.group(2), "%d.%m.%Y %H:%M:%S")
                    # Display shift is a whole-second law (the displayed
                    # time truncates the operation instant), so compare
                    # against the ID second, not the ms-exact instant.
                    id_second = (parsed["op_utc"].replace(tzinfo=None)
                                 - timedelta(milliseconds=parsed["ms"]))
                    shift = ((disp_wall - timedelta(hours=3))
                             - id_second).total_seconds()
                except ValueError:
                    shift = None
            if shift is not None:
                lo, hi = _SHIFT_ERA_G[bank]
                ok("opid_display_shift", lo <= shift <= hi,
                   f"display-ID shift = {shift:.0f}s (era-G genuine "
                   f"{lo}..{hi}s for {bank})")
    else:
        for nm in ("opid_format","opid_era","opid_flag","opid_node","opid_gateway","opid_tail"):
            ok(nm, False, "op_id not parsed")

    # 7. CreationDate lag
    info = _info_dict(raw)
    cd = info.get("/CreationDate", "")
    cd_dt = None
    if cd:
        m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", cd)
        if m:
            tzoff = 0
            mm = re.search(r"([+\-]\d{2})'(\d{2})'$", cd)
            if mm:
                sign = 1 if mm.group(1)[0] == "+" else -1
                tzoff = sign * (int(mm.group(1)) * 60 + int(mm.group(2))) * 60
            tz = timezone(timedelta(seconds=tzoff)) if tzoff else UTC
            cd_dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz)
    ok("creation_date", cd_dt is not None,
       f"CreationDate={cd!r} -> {cd_dt.isoformat() if cd_dt else 'not parsed'}")
    if cd_dt and parsed:
        lag = (cd_dt - parsed["op_utc"]).total_seconds()
        ok("creation_lag", 0 <= lag <= 604800, f"lag CD-op = {lag:.1f}s (0..7d)")
        now = datetime.now(UTC)
        ok("creation_not_future", (now - cd_dt).total_seconds() > -21600,
           f"CD vs now = {(cd_dt-now).total_seconds():.0f}s")
    elif cd_dt and (is_vtb_card or is_ozon_card or is_ozon_transfer):
        # No op_id to anchor the exact second: the displayed time is
        # minute-precision, and the PDF is created at download time —
        # from +7 s (fresh receipts) to +31 days (old receipts pulled
        # from history).  The hard law is only: not before the operation.
        dm = _DATE_RE.search(text or "")
        disp_dt = None
        if dm:
            try:
                # Displayed operation time is MSK wall time; the PDF
                # CreationDate may carry UTC (Ozon/Skia) or +03 (VTB).
                disp_dt = datetime.strptime(
                    dm.group(1) + " " + dm.group(2),
                    "%d.%m.%Y %H:%M").replace(
                        tzinfo=timezone(timedelta(hours=3)))
            except ValueError:
                disp_dt = None
        if disp_dt is not None:
            lag = (cd_dt - disp_dt).total_seconds()
            ok("creation_lag", lag >= 0,
               f"lag CD-displayed minute = {lag:.1f}s (>= 0, card; "
               "PDF created at download)")
            now = datetime.now(UTC)
            ok("creation_not_future", (now - cd_dt).total_seconds() > -21600,
               f"CD vs now = {(cd_dt-now).total_seconds():.0f}s")
        else:
            ok("creation_lag", False, "card receipt date not parsed")
    elif cd_dt and is_no_opid_product:
        # Statements / MTS cards: CreationDate is the document creation
        # instant, not anchored to one operation's second.
        ok("creation_lag", True,
           "document product: CD not operation-anchored")
        now = datetime.now(UTC)
        ok("creation_not_future", (now - cd_dt).total_seconds() > -21600,
           f"CD vs now = {(cd_dt-now).total_seconds():.0f}s")
    else:
        ok("creation_lag", False, "cannot compute lag (no op_id or CD)")

    # 8. Deflate (strict): every FlateDecode stream must decompress and
    # carry a valid zlib header (CM=8, FCHECK). The byte-exact wrapper
    # (78 9c) and any byte-exact deflate strategy are NOT hard rules:
    # genuine files contain other valid headers (e.g. 78 da, 78 01) and
    # Java Deflater output is not reproducible by Python zlib candidates.
    flate = [s for s in streams if s["is_flate"]]
    ok("flate_streams_present", len(flate) > 0,
       f"{len(flate)} FlateDecode streams")
    if not flate:
        ok("deflate_decompress", False, "no FlateDecode streams")
        ok("deflate_zlib_header", False, "no FlateDecode streams")
    else:
        bad_dec, bad_hdr = [], []
        for s in flate:
            data = s["data"]
            tag = f"obj {s['num']}"
            if len(data) < 2 or (data[0] & 0x0F) != 8 \
                    or ((data[0] << 8) + data[1]) % 31 != 0:
                bad_hdr.append(tag)
            try:
                zlib.decompress(data)
            except Exception as e:
                bad_dec.append(f"{tag}: {e}")
        ok("deflate_decompress", not bad_dec,
           f"{len(flate) - len(bad_dec)}/{len(flate)} streams decompress"
           + (f"; bad: {bad_dec[:3]}" if bad_dec else ""))
        ok("deflate_zlib_header", not bad_hdr,
           "zlib CMF/FLG (CM=8, FCHECK) valid for all streams"
           if not bad_hdr else
           f"zlib CMF/FLG (CM=8, FCHECK) valid; bad: {bad_hdr[:3]}")

    # 9. Receipt fields in text
    if text:
        ph = _PHONE_RE.search(text)
        am = _AMOUNT_RE.search(text)
        dt = _DATE_RE.search(text)
        if is_no_opid_product:
            ok("text_phone", True,
               "no recipient phone required in this product (by design)")
        else:
            ok("text_phone", bool(ph), f"phone: {ph.group(0) if ph else 'none'}")
        if not am and is_exi_stmt:
            am = re.search(r"[\d][\d\s\u00a0]*[.,]\d{2}\s*RUR", text)
        ok("text_amount", bool(am), f"amount: {am.group(0) if am else 'none'}")
        ok("text_date", bool(dt), f"date: {dt.group(0) if dt else 'none'}")
        if is_vtb_card:
            ok("card_sender_mask",
               bool(re.search(r"\d{6}\*{6}\d{4}", text)),
               "sender mask dddddd******dddd")
            _sm = re.search(r"(\d{6})\*{6}\d{4}", text)
            _smap = _CARD_BIN_BANK.get(_sm.group(1)) if _sm else None
            ok("card_sender_bin",
               _smap is None or _smap == "ВТБ",
               f"sender BIN {_sm.group(1) if _sm else '?'} -> "
               f"{_smap or 'unknown'} (sender card must be a VTB card)")
            ok("card_recipient_mask",
               bool(re.search(r"\d{4} \d{2}\*{2} \*{4} \d{4}", text)),
               "recipient mask 'dddd dd** **** dddd'")

            def _labelled_amount_kop(label):
                # Bank format: '8 108 ₽' or with kopecks '162.16 ₽' /
                # '8 270.16 ₽' (dot separator).  Returns integer kopecks.
                m = re.search(r"(?m)^" + label
                              + r"\s*\n\s*([\d][\d\s\u00a0]*(?:[.,]\d{2})?) ₽",
                              text)
                if not m:
                    return None
                s = re.sub(r"[\s\u00a0]", "", m.group(1)).replace(",", ".")
                rub, _, frac = s.partition(".")
                try:
                    return int(rub) * 100 + int((frac + "00")[:2])
                except ValueError:
                    return None

            amt_op = _labelled_amount_kop("Сумма операции")
            comm = _labelled_amount_kop("Комиссия")
            tot = _labelled_amount_kop("Сумма с комиссией")
            bm = re.search(r"(?m)^Банк получателя\s*\n\s*([^\n]+)", text)
            rec_bank = bm.group(1).strip() if bm else ""
            bin_m = re.search(r"(\d{4}) (\d{2})\*{2} \*{4} \d{4}", text)
            bin6 = (bin_m.group(1) + bin_m.group(2)) if bin_m else None
            mapped_bank = _CARD_BIN_BANK.get(bin6 or "")
            if mapped_bank:
                rec_norm = _norm_bank_name(rec_bank)
                map_norm = _norm_bank_name(mapped_bank)
                ok("card_bin_bank_match",
                   rec_norm == map_norm or map_norm in rec_norm,
                   f"recipient BIN {bin6} -> {mapped_bank}, "
                   f"receipt bank={rec_bank!r}")
            else:
                ok("card_bin_bank_match", True,
                   f"recipient BIN {bin6} not in known table - unchecked")
            if amt_op is not None:
                exp_comm = (0 if rec_bank.replace("\u2011", "-") in ("ВТБ", "VTB")
                            else max(9900, (amt_op * 2 + 50) // 100))
            else:
                exp_comm = None
            ok("card_commission",
               comm is not None and exp_comm is not None and comm == exp_comm,
               f"commission_kop={comm} expected_kop={exp_comm} "
               f"(VTB tariff: 2% exact to kopeck, min 99 RUB, 0 to VTB; "
               f"bank={rec_bank!r})")
            ok("card_total",
               tot is not None and amt_op is not None and comm is not None
               and tot == amt_op + comm,
               f"total_kop={tot} amount+commission_kop="
               f"{(amt_op + comm) if amt_op is not None and comm is not None else None}")
    else:
        for nm in ("text_phone","text_amount","text_date"):
            ok(nm, False, "no text")

    passed = all(c["passed"] for c in checks)
    return {"bank": bank, "passed": passed, "checks": checks}


def diagnose(pdf_bytes: bytes) -> str:
    """Human-readable one-line-per-check report for the bot."""
    r = validate(pdf_bytes)
    if r["passed"]:
        head = f"\u2705 \u0427\u0435\u043a \u0432\u0430\u043b\u0438\u0434\u0435\u043d ({r['bank']})"
    else:
        bn = r["bank"] or "\u0431\u0430\u043d\u043a \u043d\u0435 \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0451\u043d"
        head = f"\u274c \u0427\u0435\u043a \u041d\u0415 \u043f\u0440\u043e\u0448\u0451\u043b \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443 ({bn})"
    lines = [head, ""]
    for c in r["checks"]:
        mark = "\u2705" if c["passed"] else "\u274c"
        lines.append(f"{mark} {c['name']}: {c['detail']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, json
    data = open(sys.argv[1], "rb").read()
    print(json.dumps(validate(data), ensure_ascii=False, indent=2))
