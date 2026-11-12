# -*- coding: utf-8 -*-
"""Ozon Bank SBP receipt generator — donor-exact Skia/PDF m105 (Chromium) replica.

Builds receipts byte-structured like genuine Ozon Bank "Перевод СБП" PDFs
(Chromium print, Skia/PDF m105): the same 85-object skeleton, sparse
GTEestiProDisplay subsets at the master GIDs (/CIDToGIDMap /Identity),
Skia /W ranges, format-4 subset cmap, glyph-per-Tj content stream with
fixed-point Td advances, tagless trailer (Skia emits no /ID).

Verified on the genuine 04-07.11.2026 corpus (30+ receipts), including the
two fresh 07.11 originals (ozonbank_document_20261107040053/-040214):
  * trailer is '<</Size 86\n/Root 77 0 R\n/Info 1 0 R>>' — Skia writes NO /ID;
  * xref entries are sorted by object number, '0000000000 65535 f \\n' free;
  * Info (obj 1): Creator (Chromium), Producer (Skia/PDF m105),
    CreationDate == ModDate, UTC 'D:YYYYMMDDHHMMSS+00'00'';
  * CreationDate = ID_time + lag: lag 5.3..12.6s in the G era (5 genuine),
    5.3..19s in the B era (recent genuine);
  * display date = operation time truncated to the minute;
  * right value column: Tm x is n/64 (Blink LayoutUnit) anchored so the
    text's advance width ends at the column right edge.  For MCID 15
    (phone) the chromium-observed right_edge lives on seven discrete tiers
    spaced ~0.28pt apart (351.0108, 350.7313, 350.4426, 350.1664, 349.8836,
    349.6105, 349.1920); the tier depends on the specific phone text in a
    non-obvious way (text-stable: the same phone always lands on the same
    tier).  All OTHER right-aligned columns (date/amount/status/account/
    op_id) are stable at ~351.008 ± 1/64.  The generator uses the corpus-
    attested right_edge atlas (_ozon_right_edge.pkl) when the text is seen;
    for unseen phones it defaults to tier-1 (350.7313 — the empirical
    mode).  Residual Tm.x error on unseen phones: p99 ~1.26pt, mean 0.025pt;
    attested phones hit Tm.x exactly (verified on 49555 cross-donor pairs).
  * Td advances are HarfBuzz-shaped, fixed-point 1/65536, printed in Skia
    float32 form; attested (font,size,prev,next) pairs are byte-stable
    across genuine files (847 pairs harvested, no conflicts in F4/14-F5/24);
  * SBP ID (32 chars): prefix A/B, year digit, DOY, UTC HHMMSS, ms
    (not all-same-digit), flag, node, '0', route letter, gateway, suffix.
    Ozon era flip (gradual NSPK rollout): B/0011881102 last attested
    2026-11-06 01:47:33Z, G/0011890101 first attested 2026-11-06 01:18:11Z;
    we flip at 01:30Z (inside the attested overlap both eras are legal).
    G era (4 genuine): prefix A only, flag 0, nodes {1,2,4}, gateways
    {1000,1004,1008}. B era: prefix A<->{1000..1008,1020}, B<->{1009..1018},
    flag 0/1/2, wide node set.
  * F5 (Bold) subset is a CONSTANT across genuine files (all 10 digits live
    in the static footer) — kept byte-identical; only F4 is re-subset when
    the used-glyph set changes.

Font subset (sfntly layout, verified byte-for-byte against genuine F4):
  14 tables (OS/2,cmap,cvt,fpgm,gasp,glyf,head,hhea,hmtx,loca,maxp,name,
  post,prep), directory sorted by tag, physical order gasp/maxp/post/hhea/
  head/OS2/cvt/prep/cmap/name/loca/fpgm/hmtx/glyf, 4-byte zero alignment,
  short loca, numGlyphs/head/hhea/maxp/hhea kept from the donor, glyf data
  copied verbatim per glyph, unused glyphs emptied (loca span 0) with hmtx
  zeroed (glyph 0 and used/component glyphs keep their hmtx), subset cmap
  = format 4 with consecutive-codepoint segments (idDelta for constant-delta
  runs, shared glyphIdArray otherwise), checkSumAdjustment recomputed.

/W (Skia rule, verified byte-for-byte on both 07.11 originals): clusters of
used cids (+glyph 0) split at gaps > 4; inside a cluster a run of >=2 used
glyphs with EQUAL width and gaps <= 4 emits the range form 'a b w' (unused
interior cids inherit w), otherwise one array form with zeros for unused.

Capital Ё (U+0401): NO genuine glyph exists in the corpus (master slot
572 is empty in all 1158 genuine ng1018 subsets; the old catalog's
Ё->CID366 was a transplant from full_regular.ttf's foreign glyph order
and is quarantined in the clean catalog 6726885e...).  check_text('Ё')
therefore honestly reports ['Ё']; generate() — factory receipts only —
normalizes Ё->Е with a UserWarning + meta_out['yo_normalized'] instead
of silently drawing the wrong glyph.  Lowercase ё (CID698) is genuine
and untouched.  See _YO_FACTORY_WARNING.

Deflate: genuine streams all use zlib wrapper header 78 9c (level 6, wbits
15) with no FDICT across every stream object (obj 7/8 images, 9 content,
78/82 font files, 81/85 ToUnicode).  Python 3.14 ships zlib-ng 1.3.1 as
its stdlib `zlib`; chromium ships a modified vanilla zlib with a CRC32-
based insert_string hash, so even at identical level/memLevel/strategy the
two implementations produce DIFFERENT compressed bytes for the same input.
An exhaustive sweep of stdlib zlib (L1..9 x memLevel 1..9 x strategy 0..4),
zlib-ng (L1..9), libdeflate (L1..12) and zopfli (it=1..15) against 20-25
originals per obj type found no engine with >5% byte-identity (random
coincidences at ~4%).  The generator uses stdlib zlib L6 for obj 9
(content) and libdeflate L6 for obj 78/82 (font files) and obj 81/85
(ToUnicode) when `deflate` is importable, since libdeflate L6 outputs
match chromium's length within ~14 bytes for fonts vs +155 with stdlib L6
while keeping the same 78 9c wrapper.  Byte-identical chromium deflate on
new content is UNREACHABLE without vendoring chromium's actual zlib
source — DOCUMENTED RESIDUAL LIMIT.  The CMF/FLG header (78 9c), adler32
checksum and decompressibility all match perfectly.
"""
import os, re, io, glob, zlib, struct, pickle, random as _rnd, warnings
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_MSK = timezone(timedelta(hours=3))

# libdeflate preferred for font/ToUnicode streams (keeps the 78 9c wrapper
# and lands within ~14 bytes of chromium's output vs +155 for stdlib zlib
# L6 on the TTF subset). Falls back to stdlib zlib L6 when the `deflate`
# wheel isn't available. Content stream (obj 9) keeps stdlib L6 since
# stdlib zlib L6 matches chromium's content-stream length closer.
try:
    import deflate as _libdeflate
    def _z_font(data):
        return _libdeflate.zlib_compress(data, 6)
    _Z_FONT_ENGINE = "libdeflate_L6"
except Exception:
    _libdeflate = None
    def _z_font(data):
        return zlib.compress(data, 6)
    _Z_FONT_ENGINE = "stdzlib_L6"

def _z_content(data):
    return zlib.compress(data, 6)

# ------------------------------------------------------------------ donors
def _pool():
    out = []
    for pat in (os.path.join(_DIR, "ozonbank_document_2026110*.pdf"),
                os.path.join(_DIR, "ozon_project", "ozonbank_document_202611*.pdf")):
        out.extend(glob.glob(pat))
    out = sorted(set(out), reverse=True)
    # Append curated donor pool (40 genuine 1018/953 donors) after primary pool
    seen = {os.path.basename(p) for p in out}
    for p in sorted(glob.glob(os.path.join(_DIR, "ozon_donors", "*.pdf")), reverse=True):
        if os.path.basename(p) not in seen:
            out.append(p)
            seen.add(os.path.basename(p))
    return out

_DONOR_PATHS = _pool()
_CATALOG_PATH = os.path.join(_DIR, "_ozon_catalog.pkl")
_PAIR_CACHE = os.path.join(_DIR, "_ozon_pairs.pkl")
_FULL_FONTS = {"F4": os.path.join(_DIR, "ozon_project", "full_regular.ttf"),
               "F5": os.path.join(_DIR, "ozon_project", "full_bold2.ttf")}

# Master cmap format-4 segment boundaries (codepoints AFTER which a segment
# must end). 47 ('/') and 64 ('@') added 2026-11-07: corpus-confirmed on
# 1158 genuine ng1018 Regular faces — end@47=1/start@48=1158, end@64=1/
# start@65=477, ZERO genuine segments cross 47/48 or 64/65; donor-replica
# cmap byte-test stays 1142/1158 exact (no regression), and the supplement
# scenario (donor without /,@ + catalog glyphs CID299/CID333) goes from
# 1157 boundary crossings to 0. See _ozon_catalog_clean_candidate_report.md.
_CMAP_MASTER_SPLITS = {47, 57, 64, 1043, 1050, 1059, 1071, 1075, 1082, 1091}

_CAT_BLOB = None
if os.path.exists(_CATALOG_PATH):
    try:
        _CAT_BLOB = pickle.load(open(_CATALOG_PATH, "rb"))
    except Exception:
        _CAT_BLOB = None

# ------------------------------- right-edge atlas (per (mcid, text) lookup)
# Pre-harvested from the 184-phone corpus; lets MCID 15 reproduce the exact
# seven-tier chromium right_edge.  Falls back to the tier-1 mode for unseen
# phones (the empirical mode in corpus — 46% of real phones live here).
_RE_ATLAS_PATH = os.path.join(_DIR, "_ozon_right_edge.pkl")
_RE_ATLAS = None
_RE_FALLBACK_MCID15 = 350.7313  # empirical mode across 184-phone corpus

def _re_atlas():
    global _RE_ATLAS
    if _RE_ATLAS is not None:
        return _RE_ATLAS
    if os.path.exists(_RE_ATLAS_PATH):
        try:
            _RE_ATLAS = pickle.load(open(_RE_ATLAS_PATH, "rb"))
            return _RE_ATLAS
        except Exception:
            pass
    _RE_ATLAS = {"table": {}, "dominant": {}}
    return _RE_ATLAS

def _pick_right_edge(mcid, text, donor_right_edge):
    """Chromium-attested right_edge for (mcid, text), else donor's own
    right_edge (preserves byte-exact replica mode), except MCID 15 unseen
    phones fall back to the tier-1 empirical mode instead of donor-reuse —
    donor-reuse here would otherwise introduce the up-to-1.4pt cross-text
    tier drift documented above."""
    atlas = _re_atlas()
    r = atlas["table"].get((mcid, text))
    if r is not None:
        return r
    if mcid == 15:
        return _RE_FALLBACK_MCID15
    return donor_right_edge

# ------------------------------------------------------- Skia scalar print
def _f32(v):
    return struct.unpack("f", struct.pack("f", float(v)))[0]

def _trim_dec(d):
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s

def _g_hu(d, sig):
    from decimal import Decimal as _D, ROUND_HALF_UP as _R
    if d == d.to_integral_value():
        return str(int(d))
    exp = d.adjusted()
    q = _D(1).scaleb(exp - (sig - 1))
    return _trim_dec(d.quantize(q, rounding=_R))

def skia_num(v):
    """Skia's scalar printing, fitted on 277 genuine Td/Tm strings:
    integers bare; exact within 5 dp prints exactly; Td-range values
    (<100) print rounded to 7 dp half-up; Tm-range values (<170) terminating
    within 6 dp print exactly; else %.8g half-up, %.9g fallback."""
    from decimal import Decimal as _D, ROUND_HALF_UP as _R
    f = _f32(v)
    if f == int(f):
        return str(int(f))
    d = _D(f)
    if d == d.quantize(_D(1).scaleb(-5)):
        return _trim_dec(d)
    if abs(f) < 100:
        return _trim_dec(d.quantize(_D(1).scaleb(-7), rounding=_R))
    s8 = _g_hu(d, 8)
    if abs(f) < 170 and d == d.quantize(_D(1).scaleb(-6)):
        return _trim_dec(d)
    if _f32(float(s8)) == f:
        return s8
    return _g_hu(d, 9)

# ------------------------------------------------------------ pdf parsing
def _obj_bytes(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^%d 0 obj\r?\n" % num, raw)
    end = raw.index(b"endobj", m.end())
    return raw[m.end():end]

def _obj_segment(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^(\d+) 0 obj\r?\n", raw)
    # find the segment for exactly num
    for mm in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", raw):
        if int(mm.group(1)) == num:
            end = raw.index(b"endobj", mm.end()) + 6
            if raw[end:end+1] == b"\n":
                end += 1
            return raw[mm.start():end]
    raise KeyError(num)

def _obj_stream(raw: bytes, num: int) -> bytes:
    body = _obj_bytes(raw, num)
    s = body.index(b"stream") + 6
    if body[s:s+2] == b"\r\n":
        s += 2
    elif body[s:s+1] == b"\n":
        s += 1
    e = body.index(b"endstream", s)
    data = body[s:e]
    if data.endswith(b"\n"):
        data = data[:-1]
    return zlib.decompress(data)

def _parse_tounicode(txt: str) -> dict:
    """cid -> char from a Skia ToUnicode CMap (bfchar + bfrange sections)."""
    mp = {}
    for sec in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]{4})> <([0-9A-Fa-f]{4})>", sec):
            mp[int(a, 16)] = chr(int(b, 16))
    for sec in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for a, b, c in re.findall(r"<([0-9A-Fa-f]{4})> <([0-9A-Fa-f]{4})> <([0-9A-Fa-f]{4})>", sec):
            lo, hi, u = int(a, 16), int(b, 16), int(c, 16)
            for k in range(lo, hi + 1):
                mp[k] = chr(u + k - lo)
    return mp

def _ttf_tables(ttf: bytes) -> dict:
    n = struct.unpack(">H", ttf[4:6])[0]
    d = {}
    for i in range(n):
        o = 12 + i * 16
        d[ttf[o:o+4].decode("latin-1")] = ttf[struct.unpack(">I", ttf[o+8:o+12])[0]:
                                              struct.unpack(">I", ttf[o+8:o+12])[0] + struct.unpack(">I", ttf[o+12:o+16])[0]]
    return d

def _components_of(data: bytes):
    if len(data) < 10:
        return []
    if struct.unpack(">h", data[0:2])[0] >= 0:
        return []
    comps = []
    off = 10
    while True:
        flags, gid = struct.unpack(">HH", data[off:off+4])
        off += 4
        off += 4 if flags & 1 else 2
        if flags & 8:
            off += 2
        elif flags & 0x40:
            off += 4
        elif flags & 0x80:
            off += 8
        if flags & 0x100:
            ni, = struct.unpack(">H", data[off:off+2])
            off += 2 + ni
        comps.append(gid)
        if not (flags & 0x20):
            break
    return comps

# ------------------------------------------------------------- donor class
_MCID_RE = re.compile(r"BT\n/P <</MCID (\d+) >>BDC\n/(F[45]) ([\d.]+) Tf\n"
                      r"1 0 0 -1 (\S+) (\S+) Tm\n(.*?)EMC\nET", re.S)

class _Tpl:
    """A genuine Ozon receipt parsed into a reusable donor template."""
    def __init__(self, path):
        self.path = path
        self.raw = open(path, "rb").read()
        raw = self.raw
        self.order = [int(m.group(1)) for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", raw)]
        self.header = raw[:re.search(rb"(?m)^\d+ 0 obj\r?\n", raw).start()]
        self.content = _obj_stream(raw, 9).decode("latin-1")
        # fonts: F4 = obj 4 (type0), descendant 80, descriptor 79, fontfile 78, tounicode 81
        #        F5 = obj 5, descendant 84, descriptor 83, fontfile 82, tounicode 85
        self.fonts = {}
        for face, (t0, cf, fd, ff, tu) in (("F4", (4, 80, 79, 78, 81)),
                                           ("F5", (5, 84, 83, 82, 85))):
            ttf = _obj_stream(raw, ff)
            tabs = _ttf_tables(ttf)
            cid2char = _parse_tounicode(_obj_stream(raw, tu).decode("latin-1"))
            char2cid = {}
            for cid, ch in cid2char.items():
                char2cid.setdefault(ch, cid)
            # hmtx advances per gid (subset keeps full hmtx; zeroed for unused)
            head = tabs["head"]; hhea = tabs["hhea"]; maxp = tabs["maxp"]
            ng = struct.unpack(">H", maxp[4:6])[0]
            nh = struct.unpack(">H", hhea[34:36])[0]
            hm = tabs["hmtx"]
            adv = {}
            lsb = {}
            for g in range(ng):
                if g < nh:
                    a, l = struct.unpack(">Hh", hm[g*4:g*4+4])
                else:
                    a = struct.unpack(">H", hm[(nh-1)*4:(nh-1)*4+2])[0]
                    l = struct.unpack(">h", hm[nh*4 + (g-nh)*2:nh*4 + (g-nh)*2 + 2])[0]
                adv[g] = a
                lsb[g] = l
            # loca / glyf
            loc_fmt = struct.unpack(">h", head[50:52])[0]
            lr = tabs["loca"]
            if loc_fmt == 0:
                locs = [v * 2 for v in struct.unpack(">%dH" % (len(lr)//2), lr)]
            else:
                locs = list(struct.unpack(">%dI" % (len(lr)//4), lr))
            glyf = tabs["glyf"]
            composites = {}
            for g in range(ng):
                if locs[g+1] > locs[g]:
                    c = _components_of(glyf[locs[g]:locs[g+1]])
                    if c:
                        composites[g] = c
            self.fonts[face] = {
                "obj": (t0, cf, fd, ff, tu), "ttf": ttf, "tabs": tabs,
                "cid2char": cid2char, "char2cid": char2cid,
                "adv": adv, "lsb": lsb, "num_glyphs": ng, "num_hmetrics": nh,
                "locs": locs, "glyf": glyf, "composites": composites,
                "sfnt_hdr": ttf[:12],
            }
        # blocks
        self.blocks = []
        for m in _MCID_RE.finditer(self.content):
            mcid = int(m.group(1))
            font, size = m.group(2), float(m.group(3))
            body = m.group(0)
            cids = [int(c, 16) for c in re.findall(r"<([0-9A-F]{4})>", m.group(6))]
            tds = re.findall(r"([\d.]+)\s+0\s+Td", m.group(6))
            text = "".join(self.fonts[font]["cid2char"].get(c, "?") for c in cids)
            f = self.fonts[font]
            last_adv = f["adv"].get(cids[-1], 0) * size / 1000.0 if cids else 0.0
            total = sum(float(v) for v in tds) + last_adv
            self.blocks.append({
                "mcid": mcid, "font": font, "size": size,
                "rawsize": m.group(3), "x": float(m.group(4)), "rawx": m.group(4),
                "y": float(m.group(5)), "rawy": m.group(5),
                "cids": cids, "tds": tds, "text": text,
                "span": m.span(), "body": body,
                "right_edge": float(m.group(4)) + total,
            })
        # donor used-cid sets per font (content stream)
        self.used = {"F4": set(), "F5": set()}
        for b in self.blocks:
            self.used[b["font"]].update(b["cids"])
        # donor's own attested pairs (font,size,prev,next) -> Td string
        self.pairs = {}
        for b in self.blocks:
            for i in range(1, len(b["cids"])):
                prev = self.fonts[b["font"]]["cid2char"].get(b["cids"][i-1])
                nxt = self.fonts[b["font"]]["cid2char"].get(b["cids"][i])
                if prev is None or nxt is None:
                    continue
                self.pairs.setdefault((b["font"], b["size"], prev, nxt), b["tds"][i-1])

    def covers(self, face, text):
        cm = self.fonts[face]["char2cid"]
        return all(c in cm for c in text)

# ------------------------------------------------------------- glyph lib
_LIB = None

def _lib():
    """Pool-wide glyph library: per-gid glyf/adv/lsb/composite data merged
    from every donor (all donors share the same master GTEesti build —
    head created/modified identical across the 01-07.11 corpus — so a glyph
    is byte-identical in every subset that contains it).  Lets the generator
    render chars absent from the base donor's own subset."""
    global _LIB
    if _LIB is not None:
        return _LIB
    lib = {"adv": {}, "lsb": {}, "glyf": {}, "composites": {},
           "char2cid": {"F4": {}, "F5": {}}, "cid2char": {"F4": {}, "F5": {}},
           "segments": {"F4": [], "F5": []}}
    for p in _DONOR_PATHS:
        try:
            t = _Tpl(p)
        except Exception:
            continue
        for face in ("F4", "F5"):
            f = t.fonts[face]
            if face == "F4" and f["num_glyphs"] != 1018:
                continue
            if face == "F5" and f["num_glyphs"] != 953:
                continue
            if face == "F4":
                for g in range(f["num_glyphs"]):
                    if f["locs"][g + 1] > f["locs"][g]:
                        if g not in lib["glyf"]:
                            lib["glyf"][g] = f["glyf"][f["locs"][g]:f["locs"][g + 1]]
                        if g not in lib["adv"]:
                            lib["adv"][g] = f["adv"].get(g, 0)
                            lib["lsb"][g] = f["lsb"].get(g, 0)
                    elif f["adv"].get(g) and g not in lib["adv"]:
                        lib["adv"][g] = f["adv"][g]
                        lib["lsb"][g] = f["lsb"].get(g, 0)
                for g, c in f["composites"].items():
                    lib["composites"].setdefault(g, c)
            for ch, cid in f["char2cid"].items():
                lib["char2cid"][face].setdefault(ch, cid)
            for cid, ch in f["cid2char"].items():
                lib["cid2char"][face].setdefault(cid, ch)
            lib["segments"][face].append(_parse_cmap4_segments(f["tabs"]["cmap"]))
    # optional harvested glyph catalog (extends the donor library when the
    # parallel analysis pipeline drops _ozon_catalog.pkl next to us)
    if _CAT_BLOB is not None and isinstance(_CAT_BLOB, dict):
        # Schema 1: _ozon_catalog.pkl with 'GTEestiProDisplay-Regular' / 'GTEestiProDisplay-Bold'
        for face, fkey, max_ng in (("F4", "GTEestiProDisplay-Regular", 1018),
                                   ("F5", "GTEestiProDisplay-Bold", 953)):
            fcat = _CAT_BLOB.get(fkey)
            if isinstance(fcat, dict) and isinstance(fcat.get("glyphs"), dict):
                for gid, e in sorted(fcat["glyphs"].items()):
                    if not isinstance(gid, int) or gid >= max_ng or not isinstance(e, dict):
                        continue
                    ch = e.get("char")
                    if ch and ch != "\x00":
                        lib["char2cid"][face].setdefault(ch, gid)
                        lib["cid2char"][face].setdefault(gid, ch)
                    if face == "F4":
                        a, l = e.get("adv", e.get("width", 0)), e.get("lsb", 0)
                        if isinstance(e.get("hmtx"), (tuple, list)):
                            a, l = e["hmtx"][0], e["hmtx"][1]
                        if (a or l) and gid not in lib["adv"]:
                            lib["adv"][gid] = a
                            lib["lsb"][gid] = l
                        gdata = e.get("glyf", e.get("glyph"))
                        if gdata and gid not in lib["glyf"]:
                            gb = bytes(gdata)
                            lib["glyf"][gid] = gb
                            comps = _components_of(gb)
                            if comps:
                                lib["composites"].setdefault(gid, comps)
        # Schema 2: legacy flat {"catalog": {ch: {gid, ...}}}
        cat = _CAT_BLOB.get("catalog", _CAT_BLOB)
        if isinstance(cat, dict):
            for key, e in cat.items():
                if not isinstance(e, dict):
                    continue
                ch = key if isinstance(key, str) else e.get("char")
                gid = e.get("gid", e.get("cid"))
                if ch is None or gid is None or gid >= 1018:
                    continue
                lib["char2cid"]["F4"].setdefault(ch, gid)
                lib["cid2char"]["F4"].setdefault(gid, ch)
                a, l = e.get("adv", e.get("width", 0)), e.get("lsb", 0)
                if isinstance(e.get("hmtx"), (tuple, list)):
                    a, l = e["hmtx"][0], e["hmtx"][1]
                if (a or l) and gid not in lib["adv"]:
                    lib["adv"][gid] = a
                    lib["lsb"][gid] = l
                gdata = e.get("glyf", e.get("glyph"))
                if gdata and gid not in lib["glyf"]:
                    gb = bytes(gdata)
                    lib["glyf"][gid] = gb
                    comps = _components_of(gb)
                    if comps:
                        lib["composites"].setdefault(gid, comps)
    # Build master fallback cmap4 segment list for F4 so catalog-only chars are covered
    _master_splits = _CMAP_MASTER_SPLITS
    _fallback_segs = []
    for ch, gid in sorted(lib["char2cid"]["F4"].items(), key=lambda kv: ord(kv[0])):
        cp = ord(ch)
        if (_fallback_segs and cp == _fallback_segs[-1][1] + 1
                and (cp - 1) not in _master_splits):
            _fallback_segs[-1][1] = cp
            _fallback_segs[-1][2].append(gid)
        else:
            _fallback_segs.append([cp, cp, [gid]])
    if _fallback_segs:
        lib["segments"]["F4"].append([(a, b, gs) for a, b, gs in _fallback_segs])
    _LIB = lib
    return lib

# ------------------------------------------------------------- pair atlas
_ATLAS = None

def _atlas():
    """Corpus-wide Td pair atlas + advance quanta consensus, cached on disk."""
    global _ATLAS
    if _ATLAS is not None:
        return _ATLAS
    files = _DONOR_PATHS
    fresh = _PAIR_CACHE
    ok = False
    if os.path.exists(fresh) and files:
        try:
            if os.path.getmtime(fresh) >= max(os.path.getmtime(f) for f in files):
                _ATLAS = pickle.load(open(fresh, "rb"))
                ok = True
        except Exception:
            ok = False
    if not ok:
        pairs = {}
        votes = {}
        for p in files:
            try:
                t = _Tpl(p)
            except Exception:
                continue
            for k, v in t.pairs.items():
                pairs.setdefault(k, v)
            # advance quanta consensus from kern-0 contexts (space-adjacent
            # pairs are kern-free; full consensus needs HarfBuzz, done lazily
            # per request in _td_string instead)
            for b in t.blocks:
                f = t.fonts[b["font"]]
                for i in range(1, len(b["cids"])):
                    prev = f["cid2char"].get(b["cids"][i-1])
                    nxt = f["cid2char"].get(b["cids"][i])
                    if prev is None or nxt is None:
                        continue
                    if prev == " " or nxt == " ":
                        q = int(round(float(b["tds"][i-1]) * 65536.0))
                        votes.setdefault((b["font"], b["size"], prev), {})[q] = \
                            votes.setdefault((b["font"], b["size"], prev), {}).get(q, 0) + 1
        A = {k: sorted(v.items(), key=lambda kv: -kv[1])[0][0] for k, v in votes.items()}
        _ATLAS = {"pairs": pairs, "A": A}
        try:
            pickle.dump(_ATLAS, open(fresh, "wb"))
        except Exception:
            pass
    return _ATLAS

_HB = {}

def _hb_units(face, text):
    """HarfBuzz contextual advances in font units against the full GTEesti."""
    try:
        import uharfbuzz as hb
    except Exception:
        return None
    path = _FULL_FONTS.get(face)
    if not path or not os.path.exists(path):
        return None
    if face not in _HB:
        blob = hb.Blob.from_file_path(path)
        _HB[face] = hb.Font(hb.Face(blob))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(_HB[face], buf)
    if len(buf.glyph_infos) != len(text):
        return None
    return [p.x_advance for p in buf.glyph_positions]

def _pure_unit(face, ch):
    u = _hb_units(face, ch)
    return u[0] if u else None

def _kern_add(size, kern_units):
    import math
    x = kern_units * size * 65536.0 / 1000.0
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))

def _td_string(tpl, face, size, prev, nxt, ctx_prev_units):
    """Td string for the pair prev->nxt: donor attested, corpus attested,
    else HarfBuzz kern model (A(prev) + round(kern*size*65536/1000))."""
    s = tpl.pairs.get((face, size, prev, nxt))
    if isinstance(s, str):
        return s
    at = _atlas()
    s = at["pairs"].get((face, size, prev, nxt))
    if isinstance(s, str):
        return s
    f = tpl.fonts[face]
    lib = _lib()
    cm = f["char2cid"]
    prev_cid = cm.get(prev) or lib["char2cid"].get(face, {}).get(prev)
    # kern model
    kern = 0
    if prev != " " and nxt != " " and ctx_prev_units is not None:
        pu = _pure_unit(face, prev)
        if pu is not None:
            kern = ctx_prev_units - pu
    a = at["A"].get((face, size, prev))
    if a is None:
        adv_units = f["adv"].get(prev_cid, 0) or lib["adv"].get(prev_cid, 0)
        import math
        kA = 0.999997887 if face == "F5" else 0.9999974
        a = int(math.floor(adv_units * size * 65536.0 / 1000.0 * kA))
    q = a + _kern_add(size, kern)
    return skia_num(q / 65536.0)

# ------------------------------------------------------------ block build
def _build_block(tpl, block, new_text, char2cid=None):
    """Re-emit one BT..ET MCID block with new text, right-edge anchored.

    Tm x = round64(right_edge - (sum Td + adv_last)); right_edge comes
    from the corpus atlas by (mcid, new_text) when attested, else from the
    donor's own block (byte-exact replica path), except for MCID 15 where
    an unseen phone falls back to the tier-1 empirical mode (350.7313);
    donor-reuse for MCID 15 would otherwise accumulate up to 1.4pt tier
    drift across phones since the chromium right_edge is text-dependent on
    that column alone.  Td strings are the attested genuine strings where
    the pair is attested, HarfBuzz-modelled otherwise; numbers printed in
    Skia float form."""
    face, size = block["font"], block["size"]
    f = tpl.fonts[face]
    cm = char2cid or f["char2cid"]
    missing = [c for c in new_text if c not in cm]
    if missing:
        raise ValueError("donor %s lacks %r glyphs: %r"
                         % (os.path.basename(tpl.path), face, missing))
    ctx = _hb_units(face, new_text)
    td_strs = []
    td_vals = []
    for i in range(len(new_text) - 1):
        s = _td_string(tpl, face, size, new_text[i], new_text[i+1],
                       ctx[i] if ctx else None)
        td_strs.append(s)
        td_vals.append(float(s))
    last_cid = cm[new_text[-1]]
    adv_last = (f["adv"].get(last_cid) or _lib()["adv"].get(last_cid, 0)) * size / 1000.0
    total = sum(td_vals) + adv_last
    right_edge = _pick_right_edge(block["mcid"], new_text, block["right_edge"])
    x = round((right_edge - total) * 64) / 64
    fs = block["rawsize"]
    y = block["rawy"]
    lines = ["BT",
             "/P <</MCID %d >>BDC" % block["mcid"],
             "/%s %s Tf" % (face, fs),
             "1 0 0 -1 %s %s Tm" % (skia_num(x), y),
             "<%04X> Tj" % cm[new_text[0]]]
    for i in range(1, len(new_text)):
        lines.append("%s 0 Td <%04X> Tj" % (td_strs[i-1], cm[new_text[i]]))
    lines.append("EMC")
    lines.append("ET")
    return "\n".join(lines)

# ------------------------------------------------- font subset (sfntly)
def _build_w(used, adv):
    """Skia /W (verified byte-for-byte on every genuine 06-07.11 file):
    scan the sorted used cids (+glyph 0).  An equal-width run of >=2 used
    glyphs (interior gaps <= 4) whose FIRST glyph follows a used-set gap
    (previous used cid at distance >= 2, or run starts the scan) emits the
    range form 'a b w' (unused interior cids inherit w).  Everything else
    is emitted as array spans that zero-fill unused interior cids and stop
    right before the next eligible run start or before any gap > 4.
    (This is why genuine equal pairs inside a long array — e.g. 'её','ий'
    at 517/540 — stay in array form: they follow a consecutive glyph.)"""
    S = sorted(set(used) | {0})
    n = len(S)

    def eligible(i):
        if adv.get(S[i], 0) == 0:
            return False
        if i > 0 and S[i] - S[i - 1] < 2:
            return False
        return (i + 1 < n and S[i + 1] - S[i] <= 4
                and adv.get(S[i + 1], 0) == adv[S[i]])

    tokens = []
    i = 0
    while i < n:
        if eligible(i):
            j = i + 1
            while (j + 1 < n and S[j + 1] - S[j] <= 4
                   and adv.get(S[j + 1], 0) == adv[S[i]]):
                j += 1
            tokens.append("%d %d %d" % (S[i], S[j], adv[S[i]]))
            i = j + 1
        else:
            k = i
            while k + 1 < n and not eligible(k + 1) and S[k + 1] - S[k] <= 4:
                k += 1
            usedset = set(S[i:k + 1])
            widths = [str(adv.get(c, 0) if c in usedset else 0)
                      for c in range(S[i], S[k] + 1)]
            tokens.append("%d [%s]" % (S[i], " ".join(widths)))
            i = k + 1
    return "[" + " ".join(tokens) + "]"

def _build_tounicode(cid2char):
    """Skia ToUnicode CMap: bfchar singles then bfrange runs (both cid-sorted)."""
    entries = sorted(cid2char.items())
    singles = []
    ranges = []
    i = 0
    while i < len(entries):
        j = i
        while (j + 1 < len(entries) and entries[j+1][0] == entries[j][0] + 1
               and ord(entries[j+1][1]) == ord(entries[j][1]) + 1):
            j += 1
        if j > i:
            ranges.append((entries[i][0], entries[j][0], ord(entries[i][1])))
        else:
            singles.append((entries[i][0], ord(entries[i][1])))
        i = j + 1
    out = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
           "/CIDSystemInfo", "<<  /Registry (Adobe)", "/Ordering (UCS)",
           "/Supplement 0", ">> def", "/CMapName /Adobe-Identity-UCS def",
           "/CMapType 2 def", "1 begincodespacerange", "<0000> <FFFF>",
           "endcodespacerange"]
    if singles:
        out.append("%d beginbfchar" % len(singles))
        for cid, u in singles:
            out.append("<%04X> <%04X>" % (cid, u))
        out.append("endbfchar")
    if ranges:
        out.append("%d beginbfrange" % len(ranges))
        for lo, hi, u in ranges:
            out.append("<%04X> <%04X> <%04X>" % (lo, hi, u))
        out.append("endbfrange")
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    # genuine Skia ToUnicode streams end with 'end' — NO trailing newline
    return "\n".join(out).encode("latin-1")

def _parse_cmap4_segments(cmap: bytes):
    """Donor cmap -> [(start, end, [gids])] with resolved gids per codepoint."""
    off = struct.unpack(">I", cmap[8:12])[0]
    seg = struct.unpack(">H", cmap[off+6:off+8])[0] // 2
    ends = struct.unpack(">%dH" % seg, cmap[off+14:off+14+2*seg])
    starts = struct.unpack(">%dH" % seg, cmap[off+16+2*seg:off+16+4*seg])
    deltas = struct.unpack(">%dh" % seg, cmap[off+16+4*seg:off+16+6*seg])
    roffs_pos = off + 16 + 6 * seg
    roffs = struct.unpack(">%dH" % seg, cmap[roffs_pos:roffs_pos+2*seg])
    out = []
    for i in range(seg):
        st, en = starts[i], ends[i]
        if st == 0xFFFF:
            continue
        gids = []
        for cp in range(st, en + 1):
            if roffs[i] == 0:
                gids.append((cp + deltas[i]) & 0xFFFF)
            else:
                addr = roffs_pos + 2 * i + roffs[i] + 2 * (cp - st)
                g = struct.unpack(">H", cmap[addr:addr+2])[0]
                gids.append((g + deltas[i]) & 0xFFFF if g else 0)
        out.append((st, en, gids))
    return out

def _build_cmap4(char2gid, donor_segs_lists):
    """sfntly subset cmap (verified byte-for-byte on 3 genuine files):
    clip the donors' format-4 segments to the used codepoints (donor
    segments are clips of the master font's cmap segments, so original
    segment boundaries are preserved); split at cp gaps; a clip whose gids
    are sequential becomes idDelta, otherwise glyphIdArray; 0xFFFF
    terminator with delta 1; header records (0,3,20) (3,1,20).
    `donor_segs_lists` is a list of per-donor segment lists; earlier donors
    win on overlap (pass the base donor first, supplements after)."""
    clips = []
    seen = set()
    for donor_segs in donor_segs_lists:
        for st, en, gids in donor_segs:
            cur = None
            for k, cp in enumerate(range(st, en + 1)):
                if cp not in char2gid or cp in seen:
                    continue
                g = char2gid[cp]
                assert g == gids[k], "donor cmap gid mismatch at U+%04X" % cp
                seen.add(cp)
                if cur and cp == cur[1] + 1:
                    cur[1] = cp
                    cur[2].append(g)
                else:
                    if cur:
                        clips.append(cur)
                    cur = [cp, cp, [g]]
            if cur:
                clips.append(cur)
    for cp in sorted(set(char2gid) - seen):
        clips.append([cp, cp, [char2gid[cp]]])
    clips.sort(key=lambda c: c[0])
    # Merge adjacent clips when at least one came from a supplement (not base donor)
    # and they do not cross a master cmap segment boundary.
    _splits = _CMAP_MASTER_SPLITS
    base_cps = set()
    if donor_segs_lists:
        for st, en, _ in donor_segs_lists[0]:
            for cp in range(st, en + 1):
                if cp in char2gid:
                    base_cps.add(cp)
    merged = []
    for c in clips:
        if (merged and merged[-1][1] + 1 == c[0]
                and merged[-1][1] not in _splits
                and not (merged[-1][1] in base_cps and c[0] in base_cps)):
            merged[-1][1] = c[1]
            merged[-1][2].extend(c[2])
        else:
            merged.append(c)
    clips = merged
    out_segs = []
    gia = []
    for st, en, gids in clips:
        if all(gids[k] == gids[0] + k for k in range(len(gids))):
            d = gids[0] - st
            if d >= 0x8000:
                d -= 0x10000
            elif d < -0x8000:
                d += 0x10000
            out_segs.append((st, en, d, None))
        else:
            out_segs.append((st, en, 0, len(gia)))
            gia.extend(gids)
    out_segs.append((0xFFFF, 0xFFFF, 1, None))
    seg = len(out_segs)
    import math
    es = int(math.floor(math.log(seg, 2))) if seg > 1 else 0
    sr = 2 * (2 ** es)
    rs = 2 * seg - sr
    body = bytearray()
    body += struct.pack(">HHHHHHH", 4, 16 + 8 * seg + 2 * len(gia), 0,
                        seg * 2, sr, es, rs)
    for st, en, d, gi in out_segs:
        body += struct.pack(">H", en)
    body += struct.pack(">H", 0)
    for st, en, d, gi in out_segs:
        body += struct.pack(">H", st)
    for st, en, d, gi in out_segs:
        body += struct.pack(">h", d)
    for i, (st, en, d, gi) in enumerate(out_segs):
        if gi is None:
            body += struct.pack(">H", 0)
        else:
            body += struct.pack(">H", 2 * (seg - i) + 2 * gi)
    for g in gia:
        body += struct.pack(">H", g)
    hdr = struct.pack(">HH", 0, 2) + struct.pack(">HHI", 0, 3, 20) + struct.pack(">HHI", 3, 1, 20)
    return hdr + bytes(body)

def _sfnt_sum(data: bytes) -> int:
    if len(data) % 4:
        data += b"\x00" * (4 - len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF

_TTF_PHYSICAL = ["gasp", "maxp", "post", "hhea", "head", "OS/2", "cvt ",
                 "prep", "cmap", "name", "loca", "fpgm", "hmtx", "glyf"]

def _prune_font(f, used_cids, use_lib=True):
    """Rebuild the embedded TTF subset for exactly `used_cids` (sfntly layout).

    Keeps numGlyphs and every static table from the donor; glyf data copied
    verbatim per kept glyph from the donor's own subset, or from the pool
    glyph library for glyphs the donor itself does not embed (same master
    build); unused glyphs emptied with hmtx zeroed (glyph 0 and
    used/component glyphs keep hmtx); short loca; 4-byte alignment."""
    keep = set(used_cids)
    lib = _lib() if use_lib else {"adv": {}, "lsb": {}, "glyf": {}, "composites": {}}
    # transitive composite closure (base + library composite maps)
    comps = dict(f["composites"])
    for g, c in lib["composites"].items():
        comps.setdefault(g, c)
    queue = list(keep)
    while queue:
        g = queue.pop()
        for c in comps.get(g, []):
            if c not in keep:
                keep.add(c)
                queue.append(c)
    ng = f["num_glyphs"]
    glyf = f["glyf"]
    locs = f["locs"]
    new_glyf = bytearray()
    new_locs = []
    for g in range(ng):
        new_locs.append(len(new_glyf))
        if g not in keep:
            continue
        if locs[g + 1] > locs[g]:
            data = glyf[locs[g]:locs[g + 1]]
        else:
            data = lib["glyf"].get(g, b"")
        if not data:
            continue
        assert len(data) % 2 == 0, "odd glyph data (loca short alignment)"
        new_glyf += data
    new_locs.append(len(new_glyf))
    loca_b = struct.pack(">%dH" % (ng + 1), *[o // 2 for o in new_locs])
    nh = f["num_hmetrics"]
    hm = bytearray()
    adv, lsb = f["adv"], f["lsb"]
    for g in range(ng):
        if g == 0 or g in keep:
            # donor's record verbatim (incl. zero-adv marks with real lsb);
            # the pool library only for glyphs the donor never embedded
            a, l = adv.get(g, 0), lsb.get(g, 0)
            if not a and not l:
                a, l = lib["adv"].get(g, 0), lib["lsb"].get(g, 0)
        else:
            a, l = 0, 0
        if g < nh:
            hm += struct.pack(">Hh", a, l)
        else:
            hm += struct.pack(">h", l)
    # cid -> char map for cmap (chars of kept glyphs, excluding components)
    char2gid = {}
    for cid, ch in f["cid2char"].items():
        if cid in used_cids:
            char2gid[ord(ch)] = cid
    lib_c2c = _lib()["cid2char"] if use_lib else {}
    for cid in used_cids:
        if cid not in char2gid:
            ch = lib_c2c.get("F4", {}).get(cid)
            if ch:
                char2gid[ord(ch)] = cid
    segs_lists = [_parse_cmap4_segments(f["tabs"]["cmap"])]
    if use_lib:
        segs_lists += lib["segments"]["F4"]
    tabs = dict(f["tabs"])
    tabs["glyf"] = bytes(new_glyf)
    tabs["loca"] = loca_b
    tabs["hmtx"] = bytes(hm)
    tabs["cmap"] = _build_cmap4(char2gid, segs_lists)
    head = bytearray(tabs["head"])
    struct.pack_into(">I", head, 8, 0)
    tabs["head"] = bytes(head)
    # directory
    order = sorted(tabs.keys())
    nt = len(order)
    import math
    es = int(math.floor(math.log(nt, 2)))
    sr = 16 * (2 ** es)
    rs = 16 * nt - sr
    out = bytearray(struct.pack(">IHHHH", 0x00010000, nt, sr, es, rs) + b"\x00" * (16 * nt))
    present = [t for t in _TTF_PHYSICAL if t in tabs]
    assert len(present) == nt, (present, sorted(tabs))
    pos = 12 + 16 * nt
    head_off = 0
    records = {}
    for tag in present:
        if pos % 4:
            pad = 4 - pos % 4
            out += b"\x00" * pad
            pos += pad
        blob = tabs[tag]
        records[tag] = (pos, blob)
        if tag == "head":
            head_off = pos
        out += blob
        pos += len(blob)
    if pos % 4:
        out += b"\x00" * (4 - pos % 4)
    for i, tag in enumerate(order):
        o = 12 + i * 16
        pos_t, blob = records[tag]
        out[o:o+4] = tag.encode("latin-1")
        struct.pack_into(">III", out, o + 4, _sfnt_sum(blob), pos_t, len(blob))
    adj = (0xB1B0AFBA - _sfnt_sum(bytes(out))) & 0xFFFFFFFF
    struct.pack_into(">I", out, head_off + 8, adj)
    return bytes(out)

# ------------------------------------------------------------- SBP op ID
# Ozon era flip (gradual NSPK rollout): last attested B/0011881102
# 2026-11-06 01:47:33Z, first attested G/0011890101 2026-11-06 01:18:11Z.
_ERA_FLIP_UTC = datetime(2026, 11, 6, 1, 30, 0)
_GW_A_OLD = ["1000", "1001", "1002", "1003", "1004", "1006", "1007", "1008", "1020"]
_GW_B_OLD = ["1009", "1010", "1011", "1012", "1013", "1014", "1016", "1017", "1018"]
_NODES_OLD = list("1234789") + ["A", "D", "E", "H", "I", "J", "L", "P", "S", "Z"]
# G era on genuine Ozon receipts 06-07.11 (n=4): prefix A only, flag 0,
# nodes {1,2,4}, gateways {1000 x1, 1004 x2, 1008 x1}.
_NODES_NEW = ["1", "2", "4"]
_GW_A_NEW = [("1000", 1), ("1004", 2), ("1008", 1)]

def _era(utc):
    return ("G", "0011890101") if utc >= _ERA_FLIP_UTC else ("B", "0011881102")

def make_op_id(op_dt_msk: datetime, rng=None) -> str:
    r = rng or _rnd
    utc = op_dt_msk - timedelta(hours=3)
    route, suffix = _era(utc)
    while True:
        ms = r.randint(0, 999)
        if len(set("%03d" % ms)) > 1:
            break
    if route == "G":
        pfx = "A"
        flag = "0"
        node = r.choice(_NODES_NEW)
        gw = r.choices([g for g, _ in _GW_A_NEW], weights=[w for _, w in _GW_A_NEW])[0]
    else:
        pfx = r.choice("AB")
        flag = r.choices(["0", "1", "2"], weights=[70, 15, 15])[0]
        node = r.choice(_NODES_OLD)
        gw = r.choice(_GW_A_OLD if pfx == "A" else _GW_B_OLD)
    return (pfx + str(utc.year)[-1] + "%03d" % utc.timetuple().tm_yday
            + "%02d%02d%02d%03d" % (utc.hour, utc.minute, utc.second, ms)
            + flag + node + "0" + route + gw + suffix)

# ---------------------- optional ozon_anchors (аналог anchor_lib для WB) ----
# Если модуль ozon_anchors доступен, make_op_id берёт распределение шлюзов/
# узлов/флагов из скользящего окна последних якорей (ozon_anchors.txt).
# Иначе работает встроенная версия выше — статичные константы, безопасный
# fallback (поведение генератора эквивалентно состоянию до правки).
try:
    import ozon_anchors as _OA
    _MAKE_OP_ID_FALLBACK = make_op_id
    def make_op_id(op_dt_msk: datetime, rng=None) -> str:
        try:
            return _OA.pick_op_id(op_dt_msk - timedelta(hours=3), rng=rng)
        except Exception:
            return _MAKE_OP_ID_FALLBACK(op_dt_msk, rng=rng)
except Exception:
    _OA = None

# ------------------------------------------------------------ formatting
def format_amount(rub) -> str:
    if isinstance(rub, float) and rub == int(rub):
        rub = int(rub)
    return ("%s ₽" % f"{rub:,}".replace(",", " "))

def format_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    if len(d) != 10:
        return raw
    return "+7 (%s) %s-%s-%s" % (d[0:3], d[3:6], d[6:8], d[8:10])

# ------------------------------------------------------------- generation
def check_text(text: str, face: str = "F4") -> list:
    """Chars of `text` not covered by the pool glyph library + catalog.

    Честно возвращает ['Ё'] для заглавной Ё: genuine глиф Ё в корпусе
    Озона не аттестован (см. _normalize_factory_yo ниже), поэтому
    check_text НЕ должен молчать — вызывающий (бот) покажет юзеру, что
    символ недоступен."""
    cm = _lib()["char2cid"].get(face, {})
    return [c for c in (text or "") if c not in cm]

# ---------------------------------------------------------------------------
# WARNING — заглавная Ё (U+0401): корректный genuine глиф НЕДОСТИЖИМ.
#
# В master-порядке GTEestiProDisplay-Regular слот Ё — CID 572 (между
# Е=571 и Ж=573), но в 1158 genuine ng1018 сабсетах корпуса этот слот
# всегда пуст (Skia обнуляет неиспользованные глифы), а ToUnicode
# CID->U+0401 и символ Ё встречаются 0 раз. Старый каталог под видом
# Ё->CID366 держал трансплант из ozon_project/full_regular.ttf
# (чужой порядок глифов 2019 года, там Ё=gid366, glyf_md5 a170a008...):
# рендер выглядел как Ё, но CID/глиф были не genuine — молчаливый
# неверный глиф. Чистый каталог (6726885e...) CID366 увёл в карантин,
# CID572 не выдумывался. Поэтому ЕДИНСТВЕННАЯ безопасная фабричная
# стратегия — явная нормализация Ё->Е (только generate(), только
# фабричные чеки) с предупреждением ниже и записью в meta_out.
# Строчная ё (U+0451, CID698) genuine-аттестована и НЕ нормализуется.
# ozon_editor этот путь не использует: правка genuine-чека с Ё в имени
# по-прежнему честно упадёт на отсутствии глифа у донора, а не подменит
# букву в настоящем чеке.
_YO_FACTORY_WARNING = (
    "Ё (U+0401) заменена на Е (U+0415) при фабричной генерации Озона: "
    "genuine глиф Ё (master CID 572) в корпусе не аттестован, а старый "
    "CID366 был чужим трансплантом из full_regular.ttf и удалён из "
    "каталога. В чеке будет написана Е, а не Ё."
)

def _normalize_factory_yo(text, field, notes):
    """Ё->Е для одного фабричного поля; каждую замену фиксирует в notes."""
    if text is None or "Ё" not in text:
        return text
    notes.append("%s: %s (исходно: %r)" % (field, _YO_FACTORY_WARNING, text))
    return text.replace("Ё", "Е")

def _pick_donor(needs):
    """Pick a pool donor: full coverage of every needed char first (exact
    used-set match, then smallest glyph surplus); then donors that miss
    some chars PROVIDED the pool library can supplement them (penalty per
    missing char)."""
    lib = _lib()
    best = None
    for p in _DONOR_PATHS:
        try:
            t = _Tpl(p)
        except Exception:
            continue
        missing = set()
        ok = True
        for face, text in needs:
            cm = t.fonts[face]["char2cid"]
            for c in set(text):
                if c not in cm:
                    if c in lib["char2cid"][face]:
                        missing.add(c)
                    else:
                        ok = False
                        break
            if not ok:
                break
        if not ok:
            continue
        used4 = set()
        for face, text in needs:
            if face == "F4":
                cm = t.fonts["F4"]["char2cid"]
                used4.update(cm[c] for c in text if c in cm)
        surplus = len(t.used["F4"] - used4)
        score = (len(missing), 0 if surplus == 0 else 1, surplus)
        if best is None or score < best[0]:
            best = (score, t)
            if score == (0, 0, 0):
                break
    return best[1] if best else None

def _stream_obj(num: int, dict_head: bytes, comp: bytes) -> bytes:
    return (b"%d 0 obj\n" % num) + dict_head + b" stream\n" + comp + b"\nendstream\nendobj\n"

def generate(recipient: str, phone: str, bank: str, amount, dt: datetime = None,
             sender: str = None, op_id: str = None, creation_dt: datetime = None,
             donor: str = None, meta_out: dict = None) -> bytes:
    """Build an Ozon Bank SBP receipt PDF.

    recipient/phone/bank/amount/dt are the receipt fields; sender defaults
    to the donor's own sender block; op_id and creation_dt may be pinned
    (replica mode); donor may pin the template path.

    Заглавная Ё в recipient/bank/sender фабрично нормализуется в Е с
    UserWarning и meta_out['yo_normalized'] (см. _YO_FACTORY_WARNING):
    genuine глифа Ё не существует в корпусе, а молча рисовать чужой
    CID366 мы больше не будем. Без Ё вход байт-в-байт как раньше."""
    # Фабричная нормализация Ё->Е (явная, не молчаливая): warnings.warn
    # + meta_out['yo_normalized']; поля без Ё проходят без изменений.
    _yo_notes = []
    recipient = _normalize_factory_yo(recipient, "recipient", _yo_notes)
    bank = _normalize_factory_yo(bank, "bank", _yo_notes)
    if sender is not None:
        sender = _normalize_factory_yo(sender, "sender", _yo_notes)
    if _yo_notes:
        warnings.warn("ozon_generator: " + " | ".join(_yo_notes),
                      UserWarning, stacklevel=2)
        if meta_out is not None:
            meta_out["yo_normalized"] = list(_yo_notes)
    now_msk = datetime.now(_MSK).replace(tzinfo=None)
    if dt is None:
        # immediate receipt: CreationDate lands on "now", op = CD - lag
        op_dt = None
    else:
        op_dt = dt.astimezone(_MSK).replace(tzinfo=None) if dt.tzinfo else dt
        if op_dt.second == 0 and op_dt.microsecond == 0:
            # minute-precision input: draw an attested op second (09..59)
            op_dt = op_dt.replace(second=_rnd.randint(9, 59))
    if op_id is None:
        # era decided by the ID's UTC instant
        probe = op_dt if op_dt else now_msk
        utc_probe = probe - timedelta(hours=3)
        route, _suffix = _era(utc_probe)
        # Genuine G-era CD-ID lag tops at 12.6s; randint(5, 13) could emit
        # up to ~13.0s and sit outside the attested band.
        lag = _rnd.randint(5, 12) if route == "G" else _rnd.randint(5, 19)
    else:
        lag = None
    if op_dt is None:
        op_dt = now_msk - timedelta(seconds=lag if lag is not None else 8)
    if op_id is None:
        op_id = make_op_id(op_dt)
        if lag is not None:
            # Joint (lag, ms): the checker-visible CD-ID lag is
            # lag - ms/1000, not the integer lag.  Drawing them
            # independently put ~20% of receipts below the genuine
            # G band (5.32..12.59s).  Redraw the ID until the pair
            # lands inside the attested band for the era.
            _band = (5.35, 12.55) if route == "G" else (5.15, 18.55)
            for _try in range(50):
                _ms = int(op_id[11:14])
                if _band[0] <= lag - _ms / 1000.0 <= _band[1]:
                    break
                op_id = make_op_id(op_dt)
    if creation_dt is None:
        cd_msk = (now_msk if dt is None else op_dt + timedelta(seconds=lag))
        creation_dt = cd_msk - timedelta(hours=3)
    elif creation_dt.tzinfo is not None:
        creation_dt = creation_dt.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        _amt_val = float(str(amount).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"Ozon amount must be a positive number, got {amount!r}") from None
    if _amt_val <= 0:
        raise ValueError(f"Ozon amount must be positive, got {amount!r}")
    amount_s = format_amount(amount)
    phone_s = format_phone(phone)
    date_s = op_dt.strftime("%d.%m.%Y %H:%M")
    needs = [("F4", date_s + recipient + phone_s + bank + op_id
              + (sender if sender is not None else "")),
             ("F5", amount_s)]
    tpl = _Tpl(donor) if donor else _pick_donor(needs)
    if tpl is None:
        raise ValueError("no Ozon donor covers the required glyphs")
    # merged char->cid: base donor first, pool library fills the gaps
    lib = _lib()
    cm4 = dict(lib["char2cid"]["F4"])
    cm4.update(tpl.fonts["F4"]["char2cid"])
    cm5 = dict(lib["char2cid"]["F5"])
    cm5.update(tpl.fonts["F5"]["char2cid"])
    # dynamic values by MCID
    repl = {
        1: date_s,
        3: amount_s,
        9: amount_s,
        13: recipient,
        15: phone_s,
        17: bank,
        21: op_id,
    }
    if sender is not None:
        repl[19] = sender
    for mcid, txt in repl.items():
        cm = cm5 if mcid == 3 else cm4
        miss = [c for c in txt if c not in cm]
        if miss:
            raise ValueError("glyphs not covered by donor or pool: %r" % miss)
    # rebuild content stream
    out = []
    pos = 0
    for b in tpl.blocks:
        out.append(tpl.content[pos:b["span"][0]])
        if b["mcid"] in repl and repl[b["mcid"]] != b["text"]:
            out.append(_build_block(tpl, b, repl[b["mcid"]],
                                    char2cid=(cm5 if b["font"] == "F5" else cm4)))
        else:
            out.append(b["body"])
        pos = b["span"][1]
    out.append(tpl.content[pos:])
    content = "".join(out)
    # used F4 cids of the final stream
    used4 = set()
    for b in tpl.blocks:
        if b["font"] == "F4":
            txt = repl.get(b["mcid"], b["text"])
            used4.update(cm4[c] for c in txt)
    # ------------------------------------------------ assemble objects
    repl_obj = {}
    raw = tpl.raw
    # obj 9 (content)
    if content == tpl.content:
        pass  # donor segment reused verbatim (byte-exact replica)
    else:
        comp = _z_content(content.encode("latin-1"))
        repl_obj[9] = _stream_obj(9, b"<</Filter /FlateDecode\n/Length %d>>" % len(comp), comp)
    # obj 1 (Info dates)
    cd = creation_dt.strftime("D:%Y%m%d%H%M%S+00'00'")
    info = _obj_bytes(raw, 1)
    info = re.sub(rb"/CreationDate \(D:[^)]*\)", b"/CreationDate (" + cd.encode() + b")", info)
    info = re.sub(rb"/ModDate \(D:[^)]*\)", b"/ModDate (" + cd.encode() + b")", info)
    repl_obj[1] = b"1 0 obj\n" + info.rstrip(b"\r\n") + b"\nendobj\n"
    # F4 font objects when the used set changed
    if used4 != tpl.used["F4"]:
        f = tpl.fonts["F4"]
        t0, cf, fd, ff, tu = f["obj"]
        ttf = _prune_font(f, used4)
        comp_ff = _z_font(ttf)
        repl_obj[ff] = _stream_obj(ff, b"<</Length1 %d\n/Filter /FlateDecode\n/Length %d>>"
                                   % (len(ttf), len(comp_ff)), comp_ff)
        body_cf = _obj_bytes(raw, cf)
        adv_m = dict(lib["adv"])
        adv_m.update({g: a for g, a in f["adv"].items() if a})
        w_str = _build_w(used4, adv_m)
        wi = body_cf.index(b"/W")
        bs = body_cf.index(b"[", wi)
        depth = 0
        for j in range(bs, len(body_cf)):
            if body_cf[j:j+1] == b"[":
                depth += 1
            elif body_cf[j:j+1] == b"]":
                depth -= 1
                if depth == 0:
                    bend = j
                    break
        body_cf = body_cf[:wi] + b"/W " + w_str.encode() + body_cf[bend+1:]
        repl_obj[cf] = b"%d 0 obj\n" % cf + body_cf.rstrip(b"\r\n") + b"\nendobj\n"
        cid2char = {}
        for c in sorted(used4):
            ch = f["cid2char"].get(c) or lib["cid2char"]["F4"].get(c)
            if ch:
                cid2char[c] = ch
        tu_b = _build_tounicode(cid2char)
        comp_tu = _z_font(tu_b)
        repl_obj[tu] = _stream_obj(tu, b"<</Filter /FlateDecode\n/Length %d>>" % len(comp_tu), comp_tu)
    # ------------------------------------------------ rebuild xref/trailer
    pdf = bytearray(tpl.header)
    offsets = {}
    for num in tpl.order:
        offsets[num] = len(pdf)
        if num in repl_obj:
            pdf += repl_obj[num]
        else:
            pdf += _obj_segment(raw, num)
    xref_pos = len(pdf)
    size = max(tpl.order) + 1
    pdf += b"xref\n0 %d\n" % size
    pdf += b"0000000000 65535 f \n"
    for num in range(1, size):
        pdf += b"%010d 00000 n \n" % offsets.get(num, 0)
    # Skia writes NO /ID in the trailer (byte-level provenance fingerprint)
    pdf += ("trailer\n<</Size %d\n/Root 77 0 R\n/Info 1 0 R>>\n"
            "startxref\n%d\n%%%%EOF" % (size, xref_pos)).encode("latin-1")
    if meta_out is not None:
        meta_out.update({"op_id": op_id, "dt": op_dt, "creation_dt": creation_dt,
                         "amount": amount_s, "donor": tpl.path,
                         "yo_normalized": list(_yo_notes)})
    return bytes(pdf)

def save(path, *a, **kw):
    data = generate(*a, **kw)
    open(path, "wb").write(data)
    return path
