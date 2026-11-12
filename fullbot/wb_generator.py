# -*- coding: utf-8 -*-
"""WB Bank (Wildberries) SBP receipt generator — donor-exact JasperReports replica.

Builds receipts byte-structured like the genuine WB "Квитанция" PDFs
(OpenPDF 1.3.32 / JasperReports 7.0.3): same 29-object skeleton, sparse
GoogleSans subsets harvested from genuine donors (_wb_catalog.pkl).
"""
import os, re, zlib, pickle, struct, threading, random as _rnd
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_DONOR = os.environ.get("WB_DONOR") or os.path.join(_DIR, "wb_donor.pdf")
_DONOR_NOMSG = os.environ.get("WB_DONOR_NOMSG") or os.path.join(_DIR, "wb_donor_nomsg.pdf")
_CATALOG = os.environ.get("WB_CATALOG") or os.path.join(_DIR, "_wb_catalog.pkl")

try:
    from mts_build_check15 import jcomp as _jcomp
except Exception:
    _jcomp = None

def _deflate(data: bytes, tag: str) -> bytes:
    # The bank's stack compresses with Java Deflater (JZComp); Python zlib
    # emits a different deflate dialect and fails provenance checks.
    # NEVER silently fall back to zlib — fail loudly instead.
    if _jcomp is None:
        raise RuntimeError(
            "jcomp unavailable (mts_build_check15 import failed); "
            "refusing to emit Python-zlib deflate")
    try:
        return _jcomp(data, tag)
    except Exception as e:
        raise RuntimeError(
            f"JZComp deflate failed for WB stream '{tag}': {e}; "
            "refusing to fall back to Python zlib") from e

with open(_CATALOG, "rb") as _cat_fh:
    _blob = pickle.load(_cat_fh)
CAT = _blob["catalog"]            # face -> char -> {cid, gid, width, glyph}
EXTRA = _blob.get("extra", {})    # face -> gid -> glyph (composite components)
FT = _blob["face_tables"]         # face -> {head,hhea,maxp,hmtx,checkSumAdjustment,numGlyphs}
FACES = ("Medium", "Regular", "Bold")
RES_OF = {"Medium": "F2", "Regular": "F3", "Bold": "F4"}

# ───────────────────────── donor templates ─────────────────────────
def _load_raw(path):
    with open(path, "rb") as _fh:
        return _fh.read()

def _obj_bytes_from(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^%d 0 obj\r?\n" % num, raw)
    start = m.end()
    end = raw.index(b"endobj", start)
    return raw[start:end].rstrip(b"\r\n")

def _obj_stream_from(raw: bytes, num: int) -> bytes:
    body = _obj_bytes_from(raw, num)
    s = body.index(b"stream")
    e = body.rindex(b"endstream")
    data = body[s + 6:e].strip(b"\r\n")
    try:
        return zlib.decompress(data)
    except Exception:
        return data

def _parse_tounicode(raw: bytes) -> dict:
    txt = raw.decode("latin-1")
    mp = {}
    for sec in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sec):
            mp[int(a, 16)] = chr(int(b, 16))
    for sec in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", sec):
            lo, hi, dst = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
            if dst.startswith("["):
                vals = re.findall(r"<([0-9A-Fa-f]+)>", dst)
                for i, v in enumerate(vals):
                    if lo + i <= hi:
                        mp[lo + i] = chr(int(v, 16))
            else:
                start = int(dst[1:-1], 16)
                for i, cid in enumerate(range(lo, hi + 1)):
                    mp[cid] = chr(start + i)
    return mp

# donor's own cid->char per face (for decoding the template stream)
_DONOR_TU_XREF = {"Medium": 14, "Regular": 19, "Bold": 24}

def _load_template(path):
    raw = _load_raw(path)
    return {
        "raw": raw,
        "stream": _obj_stream_from(raw, 7).decode("latin-1"),
        "cid2char": {f: _parse_tounicode(_obj_stream_from(raw, x)) for f, x in _DONOR_TU_XREF.items()},
    }

TPL_MSG = _load_template(_DONOR)
TPL_NOMSG = _load_template(_DONOR_NOMSG)
DONOR_STREAM = TPL_MSG["stream"]
DONOR_CID2CHAR = TPL_MSG["cid2char"]

def _obj_bytes(num: int, raw: bytes = None) -> bytes:
    return _obj_bytes_from(raw if raw is not None else TPL_MSG["raw"], num)

def _obj_stream(num: int, raw: bytes = None) -> bytes:
    return _obj_stream_from(raw if raw is not None else TPL_MSG["raw"], num)

def _fmt(v: float) -> str:
    s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"

def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]

def _fmt_openpdf(val: float) -> str:
    """Exact OpenPDF ByteBuffer.formatDouble (float32 + 0.005 epsilon truncation)."""
    v = _f32(val)
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

def _openpdf_line_x(text: str, face: str, size: float, align: str) -> str:
    """Exact OpenPDF PdfLine widthLeft float32 subtraction + ByteBuffer.formatDouble.
    Verified 0 misses across all genuine WB originals (fixes 97f9bf68 224.83 and 'ВТБ' 526.02)."""
    wl = _f32(515.0)
    sz_f = _f32(size)
    k_f = _f32(0.001)
    for c in text:
        cw = CAT[face][c]["width"]
        char_w = _f32(_f32(cw * k_f) * sz_f)
        wl = _f32(wl - char_w)
    if align == "c":
        x = _f32(_f32(40.0) + _f32(wl / _f32(2.0)))
    else:
        x = _f32(_f32(40.0) + wl)
    return _fmt_openpdf(x)

def _text_width(text: str, face: str, size: float) -> float:
    return sum(CAT[face][c]["width"] for c in text) * size / 1000.0

def _esc(cid_bytes: bytes) -> bytes:
    # OpenPDF StringUtils.escapeString: named escapes for 0x08/0x09/0x0A/0x0C/0x0D,
    # backslash for ( ) \\, every other byte raw (even 0x00). Verified against
    # genuine 07.11 originals ('n' cid 265 -> \\t, 'm' cid 264 -> \\b).
    return (cid_bytes.replace(b"\\", b"\\\\")
            .replace(b"\x08", b"\\b").replace(b"\x09", b"\\t")
            .replace(b"\x0a", b"\\n").replace(b"\x0c", b"\\f")
            .replace(b"\x0d", b"\\r")
            .replace(b"(", b"\\(").replace(b")", b"\\)"))

def _encode(text: str, face: str, cid_of: dict) -> bytes:
    out = bytearray()
    for c in text:
        out += struct.pack(">H", cid_of[(face, c)])
    return bytes(out)

def _decode(payload: bytes, face: str, cid2char=None) -> str:
    m = (cid2char or DONOR_CID2CHAR)[face]
    return "".join(m.get(struct.unpack(">H", payload[i:i + 2])[0], "?")
                   for i in range(0, len(payload) - 1, 2))

_UNESC_NAMED = {ord("b"): 0x08, ord("t"): 0x09, ord("n"): 0x0A,
                ord("f"): 0x0C, ord("r"): 0x0D}

def _unesc(s: str) -> bytes:
    out = bytearray()
    i = 0
    b = s.encode("latin-1")
    while i < len(b):
        if b[i:i + 1] == b"\\" and i + 1 < len(b):
            out.append(_UNESC_NAMED.get(b[i + 1], b[i + 1]))
            i += 2
        else:
            out.append(b[i])
            i += 1
    return bytes(out)

# ───────────────────────── font subset builder ─────────────────────────
def _cksum(b: bytes) -> int:
    if len(b) % 4:
        b += b"\x00" * (4 - len(b) % 4)
    # One bulk unpack+sum: identical to the old per-word loop because
    # modular addition is associative ((s+x) & 0xFFFFFFFF per step ==
    # sum(...) & 0xFFFFFFFF once).
    return sum(struct.unpack(f">{len(b) // 4}I", b)) & 0xFFFFFFFF

def _components_of(data: bytes):
    if not data or len(data) < 10:
        return []
    n = struct.unpack(">h", data[0:2])[0]
    if n >= 0:
        return []
    comps = []
    off = 10
    # Bounds guard: a truncated composite glyph must not crash with a bare
    # struct.error — treat the glyph as having no further components.
    while off + 4 <= len(data):
        flags, gid = struct.unpack(">HH", data[off:off + 4])
        off += 4
        off += 4 if flags & 0x0001 else 2
        if flags & 0x0008:
            off += 2
        elif flags & 0x0040:
            off += 4
        elif flags & 0x0080:
            off += 8
        comps.append(gid)
        if not (flags & 0x0020):
            break
    return comps


def _build_ttf(face: str, used: dict) -> bytes:
    """Sparse subset: glyphs at their master GIDs, donor tables verbatim.

    used: gid -> glyph_bytes."""
    ft = FT[face]
    num_g = ft["numGlyphs"]
    out_glyf = bytearray()
    loca = []
    for gid in range(num_g):
        loca.append(len(out_glyf))
        # Jasper/OpenPDF writes glyphs unpadded (loca deltas are exact).
        out_glyf += used.get(gid, b"")
    loca.append(len(out_glyf))
    head = bytearray(ft["head"])
    # indexToLocFormat: keep donor's; ensure long format (1) since we emit uint32
    struct.pack_into(">h", head, 50, 1)
    loca_b = struct.pack(f">{len(loca)}I", *loca)  # == b"".join(pack per offset)
    head_ck_input = bytearray(head)
    head_ck_input[8:12] = b"\x00\x00\x00\x00"
    entries = [
        (b"glyf", bytes(out_glyf)),
        (b"head", bytes(head_ck_input)),
        (b"hhea", ft["hhea"]),
        (b"hmtx", ft["hmtx"]),
        (b"loca", loca_b),
        (b"maxp", ft["maxp"]),
        (b"prep", ft["prep"]),
    ]
    # donor sfnt header: numTables=7, searchRange=64, entrySelector=2, rangeShift=48
    sfnt_hdr = bytes.fromhex("000100000007004000020030")
    pos = 12 + 16 * len(entries)
    built = []
    for tag, blob in entries:
        if pos % 4:
            pos += 4 - (pos % 4)
        built.append((tag, pos, blob))
        pos += len(blob)
    # OpenPDF pads the whole sfnt to a 4-byte boundary with trailing zeros,
    # and writes Length1 as that padded size. Without the pad the font is
    # 1 byte short and strict TrueType validators reject it as tampered.
    if pos % 4:
        pos += 4 - (pos % 4)
    font = bytearray(pos)
    font[:12] = sfnt_hdr
    for di, (tag, off, blob) in enumerate(sorted(built, key=lambda e: e[0])):
        o = 12 + di * 16
        font[o:o + 4] = tag
        struct.pack_into(">III", font, o + 4, _cksum(blob), off, len(blob))
    for tag, off, blob in built:
        font[off:off + len(blob)] = blob
    # restore the master font's checkSumAdjustment in place (inherited)
    head_off = next(off for tag, off, _ in built if tag == b"head")
    font[head_off + 8:head_off + 12] = ft["head"][8:12]
    return bytes(font)

def _build_tounicode(pairs) -> bytes:
    lines = [f"<{c:04x}><{c:04x}><{ord(ch):04x}>".encode() for c, ch in sorted(pairs.items())]
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
        + b"\n".join(lines) + b"\n"
        b"endbfrange\n"
        b"endcmap\n"
        b"CMapName currentdict /CMap defineresource pop\n"
        b"end end\n"
    )

def _build_w(cid_width: dict) -> bytes:
    cids = sorted(cid_width)
    groups = []
    for c in cids:
        w = cid_width[c]
        if groups and groups[-1][0] + len(groups[-1][1]) == c:
            groups[-1][1].append(str(w))
        else:
            groups.append((c, [str(w)]))
    return "".join(f"{s}[{' '.join(ws)}]" for s, ws in groups).encode()

def _build_cidset(cids) -> bytes:
    mx = max(cids) if cids else 0
    bits = bytearray(mx // 8 + 1)
    for c in cids:
        bits[c // 8] |= 1 << (7 - (c % 8))
    return bytes(bits)

def _prefix() -> str:
    return "".join(_rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))

# ───────────────────────── dynamic codes ─────────────────────────
# Receipt numbers are allocated at print/download time (CreationDate), not
# at operation time: vs CreationDate the genuine 04.11 series is tight
# (slope 1.6998/s, residuals -23..+10), vs operation time it scatters +-100.
# The rate decays in the evening (like MTS TR): a bot receipt stamped
# 22:32 with the daytime 1.7/s model overshot the genuine 22:46 receipt
# (No 1021710958) by ~4200 numbers and was flagged. Anchors are therefore
# (print time MSK, number, rate-in-effect-after-the-anchor); re-anchor
# every morning and every evening from a fresh genuine.
_RCPT_ANCHORS = [
    (datetime(2026, 11, 4, 17, 57, 9), 1021685859, 1.7),     # daytime
    (datetime(2026, 11, 4, 22, 46, 33), 1021710958, 2.53),   # evening surge
    (datetime(2026, 11, 5, 0, 46, 46), 1021729195, 1.0),     # night (fe5e1c9a)
    (datetime(2026, 11, 6, 1, 15, 13), 1021896684, 1.87),    # fresh 06.11; 1.87/s = 04.11 17:02 -> 06.11 01:15 genuine slope
    # fresh 07.11 (3 genuines printed 01:48:15/21/27 MSK): realized slope
    # 06.11 01:15:13 -> 07.11 01:48:15 = 158361/88382s = 1.7918/s; the old
    # 1.87/s model overshot the genuine series by ~6.9k numbers (our own
    # wb_1022058013.pdf @22:13Z sat ~3k above genuine 1022055045 @22:48Z).
    (datetime(2026, 11, 7, 1, 48, 15), 1022055045, 1.79),
    # fresh 07.11 daytime cluster (49fd6b9b @13:21:27 №1022119588,
    # 97f9bf68 @13:21:33 №1022119597, be689d62 @13:21:38 №1022119603;
    # realized 01:48:15 -> 13:21:38 slope = 64558/41603s = 1.5518/s;
    # level_rate = 1.38 gives 1.38 * _shape_mult(13..16) = 1.70/s daytime):
    (datetime(2026, 11, 7, 13, 21, 38), 1022119603, 1.38),
    # Fresh 08.11 bank-made receipts. These are operation-time points, while
    # _RCPT_ANCHORS are print-time points; only a conservative floor is used
    # here so generated numbers never roll backwards against current receipts.
    (datetime(2026, 11, 8, 18, 59, 9), 1022275509, None),
    # Fresh 09.11 00:39 MSK print of receipt №1022309637 (CD 21:39:17Z).
    (datetime(2026, 11, 9, 0, 39, 17), 1022309637, None),
]
_RCPT_FILE = os.path.join(_DIR, "wb_used_no.txt")

# ─────────────── суточный профиль темпа серии (МСК) ───────────────
# Среднесуточный наклон сегментов (07.11: 1.79-1.90/с) — НЕ мгновенный
# темп: внутри суток серия идёт с разной скоростью, и экстраполяция от
# ночного якоря константным среднесуточным темпом уводит номер в
# будущее (к утру — на десятки тысяч). Мгновенный темп измерен прямо по
# кластерам подлинных печатей (пары genuine с известными CreationDate и
# номерами; темп = Δ№/ΔCD внутри кластера):
#   01:1x МСК 06.11: +30 № за 55 с  -> 0.55/с
#   01:48 МСК 07.11: +7 № за 12 с   -> 0.58/с
#   03:05 МСК 16.10: +60 № за 194 с -> 0.31/с
#   08:13 МСК 14.10: +20 № за 12 с  -> 1.67/с
#   17:0x МСК 04.11: +5560 № за 3272 с (8 окон) -> 1.70/с
#   вечер 17:57->22:46 МСК 04.11: сегмент 1.45/с; 22:46->00:46: 2.53/с
# При константе 1.79/с между печатями трио 01:48 за 12 с «должно» выйти
# ~21 номеров, по факту вышло 7 — модель завышает ночной темп в ~3 раза
# (Пуассон: P(X<=7 при λ=21.5) < 0.001). Профиль ниже — абсолютные темпы
# по часам МСК из этих окон/сегментов (промежуточные часы
# интерполированы); множитель формы нормируется на его среднее, а
# уровень (среднесуточный темп) по-прежнему оценивает _estimate_rate по
# последним сегментам якорей, поэтому суточный интеграл не меняется —
# меняется только распределение внутри суток.
_SERIES_HOUR_RATES_MSK = [
    1.4, 0.55, 0.45, 0.35, 0.4, 0.6, 1.0, 1.4, 1.67, 1.7, 1.7, 1.7,
    1.7, 1.7, 1.7, 1.7, 1.7, 1.6, 1.5, 1.5, 1.6, 1.9, 2.3, 2.2,
]  # индекс = час МСК 0..23
_SERIES_SHAPE_MEAN = sum(_SERIES_HOUR_RATES_MSK) / 24.0

def _shape_mult(hour_msk: int) -> float:
    return _SERIES_HOUR_RATES_MSK[hour_msk % 24] / _SERIES_SHAPE_MEAN

def _series_estimate(t0: datetime, n0: float, level_rate: float,
                     t1: datetime) -> float:
    """Номер серии в момент t1: интеграл level_rate * форма(час МСК)
    от якоря (t0, n0). За полные сутки даёт тот же прирост, что и
    константный level_rate (среднее формы = 1)."""
    if t1 <= t0:
        return float(n0) + (t1 - t0).total_seconds() * level_rate
    total = 0.0
    cur = t0
    while cur < t1:
        nxt = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step_end = min(nxt, t1)
        total += level_rate * _shape_mult(cur.hour) * (step_end - cur).total_seconds()
        cur = step_end
    return float(n0) + total
# Guards _RCPT_ANCHORS and _RCPT_FILE against concurrent use (bot handlers
# may call generate()/add_anchor() from several threads at once).
_RCPT_LOCK = threading.RLock()

# ────────────────── авто-переякорение (anchor_lib) ──────────────────
# Скорость больше не одна константа на якорь: она оценивается как медиана
# наклонов сегментов по последним _RATE_WINDOW якорям. Якоря пополняются
# из присланных оригиналов (бот, «обновить якорь») и сохраняются в
# wb_used_no.txt помеченными строками "A <дата> <время> <номер> # note"
# (старый isdigit-загрузчик их игнорирует, история append-only).
_RATE_WINDOW = 4          # сколько последних якорей участвуют в оценке
_RATE_MIN_SPAN = 600.0    # сегменты короче 10 минут слишком шумные
_RATE_MIN, _RATE_MAX = 0.3, 6.0   # правдоподобные наклоны, номер/с

try:
    import anchor_lib as _AL
except Exception:
    _AL = None

def _load_saved_anchors():
    """Якоря, добавленные в прошлые запуски (из _RCPT_FILE)."""
    if _AL is None:
        return []
    try:
        return _AL.load_anchors(_RCPT_FILE)
    except Exception:
        return []

def _plausible_in_row(row, t, n, rmin, rmax):
    """Правдоподобен ли якорь (t, n) относительно соседей ряда row:
    наклоны сегментов к ближайшим соседям по времени в [rmin, rmax].
    Отсекает мусорные/обрезанные строки файла при загрузке."""
    prev = [a for a in row if a[0] < t]
    nxt = [a for a in row if a[0] > t]
    if prev:
        seg = (n - prev[-1][1]) / (t - prev[-1][0]).total_seconds()
        if not (rmin <= seg <= rmax):
            return False
    if nxt:
        seg = (nxt[0][1] - n) / (nxt[0][0] - t).total_seconds()
        if not (rmin <= seg <= rmax):
            return False
    return True

def _merge_saved_anchors():
    """Дополнить _RCPT_ANCHORS сохранёнными якорями (rate=None: их
    скорость всё равно переоценивается окном). Мусор отфильтровывается
    теми же правилами, что и в add_anchor."""
    global _RCPT_ANCHORS
    merged = list(_RCPT_ANCHORS)
    for t, n in _load_saved_anchors():
        if any(t == at or n == an for at, an, _r in merged):
            continue
        if not (10 ** 9 <= n < 12 * 10 ** 8):
            continue
        if not _plausible_in_row(merged, t, n, _RATE_MIN, _RATE_MAX):
            continue
        merged.append((t, n, None))
        merged.sort(key=lambda a: a[0])
    _RCPT_ANCHORS = merged

_merge_saved_anchors()

def _estimate_rate(idx: int, fallback: float) -> float:
    """Скорость после якоря idx: если у якоря задана явно откалиброванная
    константа (stored), используем её; для динамических якорей (stored=None)
    оцениваем по окну последних сегментов."""
    if _RCPT_ANCHORS[idx][2] is not None:
        return float(_RCPT_ANCHORS[idx][2])
    pts = [(a[0], a[1]) for a in _RCPT_ANCHORS[:idx + 1]]
    r = None
    if _AL is not None:
        r = _AL.estimate_rate(pts, window=_RATE_WINDOW,
                              min_span_s=_RATE_MIN_SPAN,
                              lo=_RATE_MIN, hi=_RATE_MAX, default=None)
    else:
        rates = []
        for a, b in zip(pts[-_RATE_WINDOW:], pts[-_RATE_WINDOW:][1:]):
            ds = (b[0] - a[0]).total_seconds()
            if ds >= _RATE_MIN_SPAN and b[1] > a[1]:
                rates.append((b[1] - a[1]) / ds)
        if rates:
            rates.sort()
            r = min(_RATE_MAX, max(_RATE_MIN, rates[len(rates) // 2]))
    if r is None:
        r = fallback if fallback else 1.8
    return r

def current_rate(dt_msk: datetime = None) -> float:
    """Мгновенная скорость серии в момент dt_msk (для статуса): уровень
    по сегментам якорей * суточная форма на этот час МСК."""
    if dt_msk is None:
        dt_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    idx = 0
    for i, a in enumerate(_RCPT_ANCHORS):
        if dt_msk >= a[0]:
            idx = i
    return _estimate_rate(idx, _RCPT_ANCHORS[idx][2] or 1.8) * _shape_mult(dt_msk.hour)

def get_anchors() -> list:
    """Текущий ряд якорей (копия): [(dt_msk, номер, rate|None), ...]."""
    return list(_RCPT_ANCHORS)

def add_anchor(dt_msk: datetime, receipt_no: int, note: str = ""):
    """Зарегистрировать свежий якорь из присланного оригинала и сохранить
    его в wb_used_no.txt (append-only). Возвращает (ok, сообщение)."""
    global _RCPT_ANCHORS
    try:
        receipt_no = int(receipt_no)
    except Exception:
        return False, f"номер квитанции не число: {receipt_no!r}"
    if not (10 ** 9 <= receipt_no < 12 * 10 ** 8):
        return False, f"номер квитанции вне ряда 10xxxxxxxx: {receipt_no}"
    with _RCPT_LOCK:
        return _add_anchor_locked(dt_msk, receipt_no, note)

def _add_anchor_locked(dt_msk: datetime, receipt_no: int, note: str = ""):
    """Тело add_anchor под _RCPT_LOCK (валидация + мутация + сохранение
    атомарно, иначе два потока могут потерять/продублировать якорь)."""
    global _RCPT_ANCHORS
    now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if dt_msk > now + timedelta(minutes=10):
        return False, f"время якоря в будущем ({dt_msk:%d.%m.%Y %H:%M:%S})"
    if dt_msk < now - timedelta(days=45):
        return False, "якорь старше 45 суток — пользы нет"
    for at, an, _r in _RCPT_ANCHORS:
        if an == receipt_no:
            if abs((at - dt_msk).total_seconds()) < 2:
                return False, "такой якорь уже есть"
            return False, (f"конфликт: № {receipt_no} уже привязан "
                           f"к {at:%d.%m %H:%M:%S}")
    prev = [a for a in _RCPT_ANCHORS if a[0] < dt_msk]
    nxt = [a for a in _RCPT_ANCHORS if a[0] > dt_msk]
    if prev:
        t0, n0 = prev[-1][0], prev[-1][1]
        seg = (receipt_no - n0) / (dt_msk - t0).total_seconds()
        if not (_RATE_MIN <= seg <= _RATE_MAX):
            return False, (f"неправдоподобный наклон {seg:.2f}/с от якоря "
                           f"{t0:%d.%m %H:%M:%S} №{n0} — отклонено")
    if nxt:
        t1, n1 = nxt[0][0], nxt[0][1]
        seg = (n1 - receipt_no) / (t1 - dt_msk).total_seconds()
        if not (_RATE_MIN <= seg <= _RATE_MAX):
            return False, (f"неправдоподобный наклон {seg:.2f}/с до якоря "
                           f"{t1:%d.%m %H:%M:%S} №{n1} — отклонено")
    _RCPT_ANCHORS = sorted(_RCPT_ANCHORS + [(dt_msk, receipt_no, None)],
                           key=lambda a: a[0])
    saved = False
    if _AL is not None:
        saved = _AL.append_anchor(_RCPT_FILE, dt_msk, receipt_no, note)
    if not saved:  # запасной путь без anchor_lib
        try:
            with open(_RCPT_FILE, "a", encoding="utf-8") as f:
                f.write(f"A {dt_msk:%Y-%m-%d %H:%M:%S} {receipt_no}"
                        + (f" # {' '.join(str(note).split())}" if note else "") + "\n")
            saved = True
        except Exception:
            pass
    return True, (f"наклонов в окне: {min(len(_RCPT_ANCHORS), _RATE_WINDOW)}, "
                  f"скорость теперь {current_rate():.2f}/с"
                  + ("" if saved else " (не сохранён в файл — только в память!)"))

def _pick_receipt_no(dt_msk: datetime) -> int:
    # Целиком под локом: чтение файла + выбор номера + append атомарны,
    # иначе два потока получают один и тот же номер квитанции.
    with _RCPT_LOCK:
        idx = 0
        for i, a in enumerate(_RCPT_ANCHORS):
            if dt_msk >= a[0]:
                idx = i
        t, no, stored = _RCPT_ANCHORS[idx]
        rate = _estimate_rate(idx, stored or 1.8)
        # НЕ линейная экстраполяция: ночью/ранним утром константный
        # среднесуточный темп убегает вперёд от подлинной серии
        # (см. _SERIES_HOUR_RATES_MSK — измеренные окна кластеров).
        est = _series_estimate(t, no, rate, dt_msk)
        last = 0
        prev_append = 0
        try:
            with open(_RCPT_FILE, encoding="utf-8") as _fh:
                for ln in _fh:
                    ln = ln.strip()
                    if ln.isdigit():
                        last = max(last, int(ln))
                        prev_append = int(ln)
        except Exception:
            pass
        n = int(est) + _rnd.randint(-3, 3)
        if n <= last and last - est < 20:
            n = last + _rnd.randint(1, 3)
        # Быстрая серия генераций (секунды друг за другом): est почти тот
        # же, джиттер ±3 даёт дубль уже выданного номера. Поднимаемся над
        # последним дописанным номером — но только если тот рядом с оценкой:
        # далеко позади (переякорение/исправление модели вниз) не тянемся.
        if n <= prev_append and prev_append - est < 100:
            n = prev_append + _rnd.randint(1, 3)
        try:
            with open(_RCPT_FILE, "a", encoding="utf-8") as f:
                f.write(str(n) + "\n")
        except Exception:
            pass
        return n

# Hard NSPK law (genuine corpus): prefix 'A' is paired ONLY with
# gateways {1000,1001,1002,1003,1004,1006,1007,1008,1020} (1005 is
# never attested); prefix 'B' ONLY with {1009..1014,1016,1017,1018}
# (1015/1019 never attested).  Rolling them independently violated
# the law on ~50% of generations and caused unexplained WB failures.
_GATEWAYS_A = ["1000", "1001", "1002", "1003", "1004", "1006", "1007", "1008", "1020"]
_GATEWAYS_B = ["1009", "1010", "1011", "1012", "1013", "1014", "1016", "1017", "1018"]
# The full NSPK pools above are attested only on the OLD-era (route B) corpus.
# In era G (07-08.11) the genuine gateway set is narrow: prefix A pairs with
# {1000,1006,1008} and prefix B only with {1012,1013,1016,1017}. Rolling the
# full old-era pool under route G places receipts on gateways outside the
# proven-genuine profile.
_GATEWAYS_A_G = ["1000", "1006", "1008"]
_GATEWAYS_B_G = ["1012", "1013", "1016", "1017"]
# Node chars must exist in the harvested GoogleSans subset, otherwise
# _make_op_id picks a glyph we cannot render (KeyError).
_NODES = list("235789") + ["A", "C", "I", "M", "P", "V", "Z"]
# Nodes attested in the new era on strict genuine originals. 08.11 live WB
# receipts added 'S' (day), 'D' (evening 23:18) and 09.11 00:38 'C'
# alongside {1,B,E,L,W}; the genuine node set keeps widening, so the
# generator emits only the observed subset while the validator accepts
# the wide alnum alphabet.
_NODES_NEW = ["1", "B", "C", "D", "E", "L", "S", "W"]
# NSPK era flipped on 05.11.2026: last designated-genuine old ID
# 18:14:27Z, first new ID 22:11:07Z (VTB+WB both): route
# B/suffix 0011881102 -> G/0011890101.  Flip is placed at the round
# hour 22:00Z inside that attested interval, keyed by the ID's UTC
# instant, NOT by DOY (both eras occur on DOY 309).
_ERA_FLIP_UTC = datetime(2026, 11, 5, 22, 0, 0)

def _era(utc: datetime):
    return ("G", "0011890101") if utc >= _ERA_FLIP_UTC else ("B", "0011881102")

def _make_op_id(dt_msk: datetime) -> str:
    # Genuine SBP ids carry operation time minus 1 second (21/22 of Nov
    # genuines and 3/3 of 07.11 13:21 genuines have display - id == +1s;
    # rolling shift=0 or shift=2 broke wb_1022111804.pdf and PDF (70).pdf).
    utc = dt_msk - timedelta(hours=3) - timedelta(seconds=1)
    yday = utc.timetuple().tm_yday
    # Full genuine ms range 000..999 (attested across the NSPK corpus:
    # genuine WB G already carries 868; the old 50..799 window was a
    # batch-level distribution skew, not a genuine law).  Triple-digit
    # repdigits stay excluded (disputed risk marker, costless to avoid).
    mmm = _rnd.randint(0, 999)
    while len(set(f"{mmm:03d}")) == 1:
        mmm = _rnd.randint(0, 999)
    pfx = _rnd.choice("AB")
    route, suffix = _era(utc)
    # Gateway pool is era-specific (see _GATEWAYS_*_G): route G draws only
    # from the genuine era-G set, route B keeps the full NSPK old-era pool.
    if route == "G":
        gw = _rnd.choice(_GATEWAYS_A_G if pfx == "A" else _GATEWAYS_B_G)
    else:
        gw = _rnd.choice(_GATEWAYS_A if pfx == "A" else _GATEWAYS_B)
    node = _rnd.choice(_NODES_NEW if route == "G" else _NODES)
    # New era (route G): flag=0 is accepted by all checkers.
    # Old era (route B): flag=1 appeared ~22% of the time.
    flag = "0" if route == "G" else ("1" if _rnd.random() < 0.22 else "0")
    return (
        pfx + str(utc.year)[-1] + f"{yday:03d}"
        + f"{utc.hour:02d}{utc.minute:02d}{utc.second:02d}{mmm:03d}"
        + flag + node + "0"
        + route + gw + suffix
    )

def check_text(text: str, face: str = "Regular") -> list:
    return [c for c in text if c not in CAT[face]]

def format_amount(rub) -> str:
    if isinstance(rub, float) and rub == int(rub):
        rub = int(rub)
    if not isinstance(rub, int):
        # Genuine WB amounts are whole rubles; fractional/str amounts used
        # to die later with cryptic glyph/format errors (KeyError '.').
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(str(rub).strip().replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            raise ValueError(f"WB amount must be whole rubles, got {rub!r}") from None
        if d != d.to_integral_value():
            raise ValueError(f"WB amount must be whole rubles, got {rub!r}")
        rub = int(d)
    s = f"{rub:,}".replace(",", " ")
    return f"{s} \u20bd"

def format_phone(raw: str) -> str:
    d = "".join(c for c in (raw or "") if c.isdigit())
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    if len(d) != 10:
        return raw
    return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"

# ───────────────────────── main builder ─────────────────────────
_TJ_RE = re.compile(
    r"1 0 0 1 ([\d.]+) ([\d.]+) Tm\s*/(F\d+) ([\d.]+) Tf\s*\n"
    r"0\.14118 0\.14118 0\.16078 rg\s*\n\((.*?)\)Tj", re.S)
_FACE_OF_RES = {"F2": "Medium", "F3": "Regular", "F4": "Bold"}

def generate(recipient: str, phone: str, bank: str, amount_rub,
             message: str = "", dt: datetime = None,
             receipt_no: int = None, op_id: str = None,
             meta_out: dict = None) -> bytes:
    if dt is None:
        dt = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)
    now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    # CreationDate lag (CD - op_id) in the genuine G-era corpus is
    # 22..471s (holdout, med 46; clean immediate prints 32..75s).
    # age<22 emitted out-of-support lags (<22s) in ~11% of generations,
    # a statistical forgery marker for strict checkers. Keep the floor
    # at 22s (lag = age + 0..2s op_id shift, so lag >= 22 always).
    age = _rnd.randint(22, 75)
    op_dt = min(dt, now_msk) - timedelta(seconds=age) if abs((dt - now_msk).total_seconds()) < 300 else dt

    if receipt_no is None:
        receipt_no = _pick_receipt_no(now_msk)
    if op_id is None:
        op_id = _make_op_id(op_dt)

    tpl = TPL_MSG if (message or "").strip() else TPL_NOMSG
    stream = tpl["stream"]
    cmap = tpl["cid2char"]
    raw = tpl["raw"]

    amount_s = format_amount(amount_rub)
    dt_s = op_dt.strftime("%d.%m.%Y %H:%M:%S")
    fields = {
        "amount": amount_s,
        "datetime": dt_s,
        "recipient": recipient,
        "phone": format_phone(phone),
        "bank": bank,
        "message": message or "",
        "receipt": f"Квитанция № {receipt_no}",
    }
    # operation code wrapped: Jasper's op-id field is 247pt wide (verified
    # against genuine 2-line receipts: line1 widths up to 245.16 stay, the
    # next glyph that overflowed was always >= 248.18 -> field width 247).
    code_lines = []
    cur = ""
    for ch in op_id:
        if _text_width(cur + ch, "Regular", 17) <= 247.0:
            cur += ch
        else:
            code_lines.append(cur)
            cur = ch
    code_lines.append(cur)

    # donor dynamic values in order of appearance
    donor_vals = ["10 234 \u20bd", "04.11.2026 17:02:04", "Демид Андреевич В.",
                  "+7 921 102-58-53", "Альфа-Банк",
                  "А а Б б В в Г г Д д Е е Ё ё Ж ж З з И и",
                  "A63081402033620P0B1007001", "1881102", "Квитанция № 1021680370"]
    new_vals = [fields["amount"], fields["datetime"], fields["recipient"],
                fields["phone"], fields["bank"], fields["message"],
                code_lines[0], code_lines[1] if len(code_lines) > 1 else "",
                fields["receipt"]]
    align = ["c", "c", "r", "r", "r", "r", "r", "r", "l"]
    faces_seq = ["Medium", "Regular", "Regular", "Regular", "Regular",
                 "Regular", "Regular", "Regular", "Bold"]

    # CID == master GID (donors use /Identity), so no CID collisions are
    # possible and every glyph lands at its genuine font index.
    used = {f: {} for f in FACES}      # face -> gid -> glyph
    cid_of = {}                        # (face, char) -> cid (== gid)
    matches = list(_TJ_RE.finditer(stream))
    blocks = []
    for m in matches:
        x, y, res, size, payload = m.groups()
        face = _FACE_OF_RES[res]
        text = _decode(_unesc(payload), face, cmap)
        blocks.append((m, face, text))
    # dynamic Tj blocks by position in the donor stream
    if tpl is TPL_MSG:
        DYN_IDX = {0: 0, 1: 1, 7: 2, 9: 3, 11: 4, 13: 5, 16: 6, 17: 7, 18: 8}
    else:
        DYN_IDX = {0: 0, 1: 1, 7: 2, 9: 3, 11: 4, 14: 5, 15: 6, 16: 7}
        new_vals = [fields["amount"], fields["datetime"], fields["recipient"],
                    fields["phone"], fields["bank"],
                    code_lines[0], code_lines[1] if len(code_lines) > 1 else "",
                    fields["receipt"]]
        faces_seq = ["Medium", "Regular", "Regular", "Regular", "Regular",
                     "Regular", "Regular", "Bold"]
        align = ["c", "c", "r", "r", "r", "r", "r", "l"]

    # Font subsets must contain exactly the glyphs the document renders:
    # static label blocks plus the new dynamic values. Seeding from the
    # donor's own dynamic blocks leaks donor-only glyphs (e.g. '5', 'Д')
    # into ToUnicode/CIDSet/W, which genuine Jasper subsets never have.
    dyn_texts = [(faces_seq[i], new_vals[i]) for i in range(len(new_vals))]
    static_texts = [(face, text) for bi, (_, face, text) in enumerate(blocks)
                    if bi not in DYN_IDX]
    for face, text in static_texts + dyn_texts:
        for c in text:
            if (face, c) not in cid_of:
                entry = CAT[face][c]
                cid_of[(face, c)] = entry["gid"]
                used[face][entry["gid"]] = entry["glyph"]

    def rebuild(block_no, m, face, text):
        x0, y0, res, size = m.group(1), m.group(2), m.group(3), float(m.group(4))
        if block_no in DYN_IDX:
            i = DYN_IDX[block_no]
            new_text = new_vals[i]
            f2 = faces_seq[i]
            if align[i] in ("c", "r"):
                x = _openpdf_line_x(new_text, f2, size, align[i])
            else:
                x = x0
            payload = _esc(_encode(new_text, f2, cid_of))
            return (f"1 0 0 1 {x} {y0} Tm\n/{res} {_fmt(size)} Tf\n"
                    f"0.14118 0.14118 0.16078 rg\n(").encode("latin-1") + payload + b")Tj"
        payload = _esc(_encode(text, face, cid_of))
        return (f"1 0 0 1 {x0} {y0} Tm\n/{res} {_fmt(size)} Tf\n"
                f"0.14118 0.14118 0.16078 rg\n(").encode("latin-1") + payload + b")Tj"

    out = []
    pos = 0
    for bi, (m, face, text) in enumerate(blocks):
        out.append(stream[pos:m.start()].encode("latin-1"))
        out.append(rebuild(bi, m, face, text))
        pos = m.end()
    out.append(stream[pos:].encode("latin-1"))
    content = b"".join(out)

    # ── assemble objects ──
    pfx = {f: _prefix() for f in FACES}
    objs = {}
    objs[1] = _obj_bytes(1, raw)
    objs[2] = _obj_bytes(2, raw)
    objs[8] = _obj_bytes(8, raw)
    objs[9] = _obj_bytes(9, raw)
    objs[25] = _obj_bytes(25, raw)
    objs[26] = _obj_bytes(26, raw)
    objs[27] = _obj_bytes(27, raw)

    font_obj = {"Medium": (4, 11, 12, 13, 14, 10), "Regular": (5, 16, 17, 18, 19, 15), "Bold": (6, 21, 22, 23, 24, 20)}
    for face in FACES:
        t0, ff, fd, cf, tu, cs = font_obj[face]
        cid_width = {}
        for (f2, ch), cid in cid_of.items():
            if f2 == face:
                cid_width[cid] = CAT[face][ch]["width"]
        glyphs = {}
        if 0 in EXTRA.get(face, {}):
            glyphs[0] = EXTRA[face][0]   # .notdef — Jasper always includes it
        glyphs.update(used[face])
        # composite glyphs reference base outlines — pull only the
        # components this document actually needs (transitively)
        queue = list(glyphs.values())
        while queue:
            gdata = queue.pop()
            for comp_gid in _components_of(gdata):
                if comp_gid not in glyphs and comp_gid in EXTRA.get(face, {}):
                    glyphs[comp_gid] = EXTRA[face][comp_gid]
                    queue.append(EXTRA[face][comp_gid])
        font_bin = _build_ttf(face, glyphs)
        comp_font = _deflate(font_bin, 'wbff')
        objs[ff] = (f"<</Length1 {len(font_bin)}/Filter/FlateDecode/Length {len(comp_font)}>>stream\n".encode()
                    + comp_font + b"\nendstream")
        desc = _obj_bytes(fd, raw).decode("latin-1")
        desc = re.sub(r"/FontName\s*/[A-Z]{6}\+GoogleSans-[A-Za-z]+", f"/FontName/{pfx[face]}+GoogleSans-{face}", desc)
        objs[fd] = desc.encode("latin-1")
        cidfont = _obj_bytes(cf, raw).decode("latin-1")
        cidfont = re.sub(r"/BaseFont\s*/[A-Z]{6}\+GoogleSans-[A-Za-z]+", f"/BaseFont/{pfx[face]}+GoogleSans-{face}", cidfont)
        # replace the whole /W [...] span (balanced brackets)
        wm = re.search(r"/W\s*\[", cidfont)
        if wm:
            wi = wm.start()
            bstart = cidfont.index("[", wi)
            depth = 0
            bend = bstart
            for j in range(bstart, len(cidfont)):
                if cidfont[j] == "[":
                    depth += 1
                elif cidfont[j] == "]":
                    depth -= 1
                    if depth == 0:
                        bend = j
                        break
            cidfont = cidfont[:wi] + "/W [" + _build_w(cid_width).decode("latin-1") + "]" + cidfont[bend + 1:]
        cidfont = re.sub(r"/CIDToGIDMap (\d+) 0 R", "/CIDToGIDMap /Identity", cidfont)
        if "/CIDToGIDMap" not in cidfont:
            cidfont = cidfont.replace("/DW 1000", "/DW 1000\n  /CIDToGIDMap /Identity")
        objs[cf] = cidfont.encode("latin-1")
        pairs = {cid: ch for (f2, ch), cid in cid_of.items() if f2 == face}
        tu_b = _build_tounicode(pairs)
        comp_tu = _deflate(tu_b, 'wbtu')
        objs[tu] = (f"<</Filter/FlateDecode/Length {len(comp_tu)}>>stream\n".encode()
                    + comp_tu + b"\nendstream")
        cs_b = _build_cidset(sorted(used[face]))
        comp_cs = _deflate(cs_b, 'wbcs')
        objs[cs] = (f"<</Filter/FlateDecode/Length {len(comp_cs)}>>stream\n".encode()
                    + comp_cs + b"\nendstream")
        type0 = _obj_bytes(t0, raw).decode("latin-1")
        type0 = re.sub(r"/BaseFont\s*/[A-Z]{6}\+GoogleSans-[A-Za-z]+", f"/BaseFont/{pfx[face]}+GoogleSans-{face}", type0)
        objs[t0] = type0.encode("latin-1")

    content_c = _deflate(content, 'wbcnt')
    objs[7] = (f"<</Filter/FlateDecode/Length {len(content_c)}>>stream\n".encode()
               + content_c + b"\nendstream")
    # logo stream recompress
    logo_raw = _obj_stream(3, raw)
    comp_logo = _deflate(logo_raw, 'wblg')
    objs[3] = (re.sub(rb"/Length \d+", f"/Length {len(comp_logo)}".encode(), _obj_bytes(3, raw).split(b"stream")[0])
               + b"stream\n" + comp_logo + b"\nendstream")

    # OpenPDF writes the Info dict without spaces and stream dicts as
    # ">>stream"; both are byte-level provenance fingerprints.
    cd = (now_msk - timedelta(hours=3)).strftime("D:%Y%m%d%H%M%SZ")
    objs[28] = (f"<</Creator(JasperReports Library version 7.0.3-41034ca841d452f3305ba55b9042260aaa1ab5dd)"
                f"/CreationDate({cd})/Producer(OpenPDF 1.3.32)>>").encode()

    order = [int(m.group(1)) for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", raw)]
    if sorted(order) != list(range(1, 29)):
        order = list(range(1, 29))
    # CIDSet objects were written into font_obj cs slots (10/15/20) above via objs[cs]
    pdf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in order:
        offsets[num] = len(pdf)
        pdf += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += f"xref\n0 {len(order) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    # xref entries must be in object-number order (OpenPDF does this);
    # writing them in physical donor order corrupts the xref table.
    for num in sorted(offsets):
        pdf += f"{offsets[num]:010d} 00000 n \n".encode()
    doc_id = _rnd.randbytes(16).hex()
    pdf += (f"trailer\n<</Info 28 0 R/ID [<{doc_id}><{doc_id}>]/Root 27 0 R/Size {len(order) + 1}>>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()

    if meta_out is not None:
        meta_out.update({"receipt_no": receipt_no, "op_id": op_id, "dt": op_dt,
                         "amount": amount_s})
    return bytes(pdf)
