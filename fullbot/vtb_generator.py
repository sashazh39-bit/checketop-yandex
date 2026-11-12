# -*- coding: utf-8 -*-
"""VTB (openhtmltopdf) SBP receipt generator — donor-template replica.

Template: a genuine VTB receipt whose SFProDisplay subset already covers
every glyph of the new text (alphabet-message donors are richest).
Only obj5 (content) + obj3 (Info dates) are rewritten; the font subset,
CIDToGIDMap, CIDSet, logo form and all other objects stay byte-identical
to the donor. ID generation follows the NSPK grammar verified on 207
genuine SBP IDs: prefix A <-> gateways 1000..1008/1020, B <-> 1009..1018,
    ID time (UTC) = operation time - 1s.  Era (see _ERA_FLIP_UTC): before
2026-11-05 22:00Z route B + suffix 0011881102, from then on route G +
suffix 0011890101.  Re-verified 2026-11-07 on 45 genuine VTB receipts
(14 G-era, 15 B-era, 16 older): immediate lag CD-op = +5..+24s, op second
08..52, G-era flag always 0, nodes {1,2,4,5,6,8,9}, /ID ms offset +5..+948.

Message receipts: the message may contain '\n' (one block per line); the
page grows at the top by _msg_dh(n) points, bottom rows keep their anchors
(verified byte-for-byte on all 11 genuine message receipts 04-07.11,
1/2/3/5 lines).  The '*' account glyph (CID 13, packed as b"\x00\x0d") is
emitted in hex form <000D> exactly like every genuine openhtmltopdf
receipt of the corpus (44/44; raw-CR and '\r'-escape files on disk are
older replicas of this generator, not genuine).
"""
import os, re, zlib, struct, threading, random as _rnd
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_CAT_PATH = os.path.join(_DIR, "_vtb_catalog.pkl")
_CAT_BLOB = None
if os.path.exists(_CAT_PATH):
    import pickle as _pkl
    with open(_CAT_PATH, "rb") as _cat_fh:
        _CAT_BLOB = _pkl.load(_cat_fh)

_TEMPLATES_MSG = [
    os.path.join(_DIR, "vtb_donor_msg2.pdf"),   # richest Cyrillic subset (alphabet msg)
    os.path.join(_DIR, "vtb_donor_msg1.pdf"),
]
_TEMPLATES_PLAIN = [
    # 06.11 genuine donors: the only VTB subsets containing Latin 'G'
    # (route letter of the 0011890101 era); the 04/05.11 donors have no 'G'.
    os.path.join(_DIR, "vtb_donor_new_nikita.pdf"),
    os.path.join(_DIR, "vtb_donor_new_ozon.pdf"),
    os.path.join(_DIR, "vtb_donor_new_mikhail.pdf"),
    os.path.join(_DIR, "vtb_donor_plain_nikita.pdf"),  # no-message, Nikita/T-Bank subset
    os.path.join(_DIR, "vtb_donor_plain.pdf"),
]
_TEMPLATES = _TEMPLATES_MSG + _TEMPLATES_PLAIN

try:
    from mts_build_check15 import jcomp as _jcomp
except Exception:
    _jcomp = None

def _java_deflate(data: bytes) -> bytes:
    """Direct JZComp call (Java Deflater dialect) searching known class dirs."""
    import shutil, subprocess, tempfile, uuid
    dirs = [os.environ.get("JZCOMP_DIR"), _DIR, os.path.join(_DIR, "wb_project"),
            os.path.join(_DIR, "retranslator-mts", "pdf-service"), "/opt/mts-bot"]
    cp = next((d for d in dirs if d and os.path.exists(os.path.join(d, "JZComp.class"))), None)
    if cp is None:
        raise RuntimeError("JZComp.class not found")
    javas = [os.environ.get("JAVA_BIN"),
             r"C:\Users\nemor\AppData\Local\Programs\Microsoft\jdk-17.0.17.10-hotspot\bin\java.exe",
             shutil.which("java")]
    java = next((j for j in javas if j and (os.path.exists(j) or shutil.which(j))), None)
    if java is None:
        raise RuntimeError("java not found")
    suf = uuid.uuid4().hex[:12]
    inn = os.path.join(tempfile.gettempdir(), f"vtb_{suf}_in.bin")
    out = os.path.join(tempfile.gettempdir(), f"vtb_{suf}_out.bin")
    try:
        with open(inn, "wb") as _fh:
            _fh.write(data)
        subprocess.check_call([java, "-cp", cp, "JZComp", inn, out],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out, "rb") as _fh:
            return _fh.read()
    finally:
        for p in (inn, out):
            try:
                os.unlink(p)
            except OSError:
                pass

# ───────────── JZCompFS: persistent Java Deflater with STRATEGY ─────────────
# Genuine VTB (openhtmltopdf) streams are Java Deflater DEFAULT, level 6:
# verified byte-for-byte on 198/198 streams of 33 genuine receipts
# (_vtb_default_lab/REPORT.md).  Java DEFAULT L6 is NOT byte-identical to
# Python zlib L6 in this environment (Python zlib = zlib-ng): they coincide
# only on the trivial 13-byte obj18 CIDSet (137 bytes of mostly 0xFF, raw
# data that compresses identically under DEFAULT/FILTERED/Python), which is
# therefore uninformative for dialect detection.  receipt_validator has no
# is_python_zlib check (only flate_header_789c, decompress and /Length), so
# genuine DEFAULT output is emitted unchanged below.
import shutil as _shutil, subprocess as _sp, struct as _struct2
_JZFS = None
_JZFS_LOCK = threading.Lock()

def _jzfs_dirs():
    for d in (os.environ.get("JZCOMP_DIR"), _DIR,
              os.path.join(_DIR, "retranslator-mts", "pdf-service"), "/opt/mts-bot"):
        if d and os.path.exists(os.path.join(d, "JZCompFS.class")):
            return d
    return None

def _jzfs_java():
    for j in (os.environ.get("JAVA_BIN"),
              r"C:\Users\nemor\AppData\Local\Programs\Microsoft\jdk-17.0.17.10-hotspot\bin\java.exe",
              _shutil.which("java")):
        if j and (os.path.exists(j) or _shutil.which(j)):
            return j
    return None

def _read_exact2(fh, n):
    out = bytearray()
    while len(out) < n:
        chunk = fh.read(n - len(out))
        if not chunk:
            raise EOFError("JZCompFS closed stdout")
        out += chunk
    return bytes(out)

def _jzfs_start(jzdir, java):
    proc = _sp.Popen([java, "-cp", jzdir, "JZCompFS"],
                     stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.DEVNULL)
    try:
        n = _struct2.unpack(">I", _read_exact2(proc.stdout, 4))[0]
        if n != 4 or _read_exact2(proc.stdout, 4) != b"JZFS":
            raise RuntimeError("JZCompFS handshake failed")
    except Exception:
        try: proc.kill()
        except Exception: pass
        raise
    return proc

def _deflate_strategy(data: bytes, level: int = 6, strategy: int = 1) -> bytes:
    """Deflate via the persistent JZCompFS daemon with a chosen strategy.
    strategy: 0=DEFAULT, 1=FILTERED, 2=HUFFMAN_ONLY (Deflater constants)."""
    global _JZFS
    jzdir = _jzfs_dirs()
    if jzdir is None:
        raise RuntimeError("JZCompFS.class not found")
    java = _jzfs_java()
    if java is None:
        raise RuntimeError("java not found for JZCompFS")
    with _JZFS_LOCK:
        if _JZFS is None or _JZFS.poll() is not None:
            _JZFS = _jzfs_start(jzdir, java)
        try:
            _JZFS.stdin.write(_struct2.pack(">III", len(data), level, strategy))
            _JZFS.stdin.write(data)
            _JZFS.stdin.flush()
            n = _struct2.unpack(">I", _read_exact2(_JZFS.stdout, 4))[0]
            return _read_exact2(_JZFS.stdout, n)
        except Exception:
            try: _JZFS.kill()
            except Exception: pass
            _JZFS = None
            raise

def _is_py_zlib(stream_bytes: bytes) -> bool:
    """Diagnostic only: does this stream exactly match Python zlib output at
    any of (6,-1,9,1,7,5) DEFAULT strategy?  Not used by receipt_validator;
    trivial streams (obj18 CIDSet) match under every strategy."""
    import zlib
    try:
        raw = zlib.decompress(stream_bytes)
    except Exception:
        return False
    for lvl in (6, -1, 9, 1, 7, 5):
        try:
            if zlib.compress(raw, lvl) == stream_bytes:
                return True
        except Exception:
            continue
    return False

def _deflate_anti_py(data: bytes) -> bytes:
    """Deflate with genuine Java Deflater DEFAULT level 6 (strategy 0).
    Output carries the standard zlib header 78 9c, satisfying the
    validator's flate_header_789c check.  Name kept for callers; this is
    no longer an anti-Python dialect — genuine openhtmltopdf streams are
    DEFAULT L6 (see header comment and _vtb_default_lab/REPORT.md)."""
    return _deflate_strategy(data, 6, 0)  # DEFAULT (genuine), header 789c

def _deflate(data: bytes) -> bytes:
    # Java Deflater (JZComp) only. NEVER silently fall back to Python zlib:
    # it emits a foreign deflate dialect that fails provenance checks.
    # VTB path: JZCompFS with DEFAULT level 6 (genuine openhtmltopdf
    # dialect).  Fallbacks below (_jcomp/_java_deflate) are also DEFAULT.
    errs = []
    try:
        return _deflate_anti_py(data)
    except Exception as e:
        errs.append(f"jzfs: {e}")
    if _jcomp is not None:
        try:
            return _jcomp(data, "vtbcnt")
        except Exception as e:
            errs.append(f"jcomp: {e}")
    try:
        return _java_deflate(data)
    except Exception as e:
        errs.append(f"_java_deflate: {e}")
    raise RuntimeError(
        "JZComp deflate unavailable for VTB stream (" + "; ".join(errs) +
        "); refusing to fall back to Python zlib")

# ---------------------------------------------------------------- parsing
def _parse_tounicode(txt: str) -> dict:
    mp = {}
    sec = None
    for line in txt.splitlines():
        if "beginbfchar" in line:
            sec = "c"; continue
        if "beginbfrange" in line:
            sec = "r"; continue
        if "endbfchar" in line or "endbfrange" in line:
            sec = None; continue
        toks = re.findall(r"<([0-9A-Fa-f]+)>", line)
        if sec == "c" and len(toks) == 2:
            mp[int(toks[0], 16)] = chr(int(toks[1], 16))
        elif sec == "r" and len(toks) >= 3:
            lo, hi = int(toks[0], 16), int(toks[1], 16)
            if "[" in line:
                vals = re.findall(r"<([0-9A-Fa-f]+)>", line[line.index("["):])
                for i, v in enumerate(vals):
                    if lo + i <= hi:
                        mp[lo + i] = chr(int(v, 16))
            else:
                st = int(toks[2], 16)
                for i, cid in enumerate(range(lo, hi + 1)):
                    mp[cid] = chr(st + i)
    return mp

def _parse_w(obj_text: str) -> dict:
    m = re.search(r"/W\s*(\[.*?\]\s*\])", obj_text, re.S)
    if not m:
        return {}
    s = m.group(1)[1:-1]
    out = {}
    for mm in re.finditer(r"(\d+)\s*\[([^\]]*)\]|(\d+)\s+(\d+)\s+(\d+)", s):
        if mm.group(1) is not None:
            st = int(mm.group(1))
            for k, w in enumerate(mm.group(2).split()):
                try:
                    out[st + k] = int(w)
                except ValueError:
                    pass
        else:
            a, b, w = int(mm.group(3)), int(mm.group(4)), int(mm.group(5))
            for c in range(a, b + 1):
                out[c] = w
    return out

class _Tpl:
    def __init__(self, path):
        import fitz
        self.path = path
        with open(path, "rb") as _fh:
            self.raw = _fh.read()
        d = fitz.open(path)
        try:
            self.cid2char = _parse_tounicode(d.xref_stream(12).decode("latin-1"))
            self.char2cid = {}
            for cid, ch in self.cid2char.items():
                self.char2cid.setdefault(ch, cid)
            self.W = _parse_w(d.xref_object(11))
            self.content = d.xref_stream(5).decode("latin-1")
            _c2g = d.xref_stream(16)
            self.cid2gid_raw = _c2g
            self.cid2gid = {}
            for _i in range(0, len(_c2g) - 1, 2):
                _g = struct.unpack(">H", _c2g[_i:_i + 2])[0]
                if _g:
                    self.cid2gid[_i // 2] = _g
            self.ttf = d.xref_stream(17)
            self.tounicode_text = d.xref_stream(12).decode("latin-1")
            self.page_h = float(d[0].rect.height)
        finally:
            # fitz.Document leaks native memory if __init__ raises mid-parse.
            d.close()
        self.donor_cids = set(self.cid2char.keys())
        self._adv = _ttf_adv_units(self.ttf)  # hmtx advance units per gid
        self.blocks = []
        for m in re.finditer(r"BT(.*?)ET", self.content, re.S):
            b = m.group(0)
            tm = re.search(r"1 0 0 1 (\S+) (\S+) Tm", b)
            tf = re.search(r"/F1 (\S+) Tf", b)
            if not tm:
                continue
            text = self._decode_block(b)
            blk = {
                "span": m.span(), "body": b,
                "x": float(tm.group(1)), "y": float(tm.group(2)),
                "rawx": tm.group(1), "rawy": tm.group(2),
                "rawsize": tf.group(1) if tf else "9",
                "size": float(tf.group(1)) if tf else 9.0,
                "text": text,
            }
            blk["w"] = self.text_width(text, blk["size"], self.tracking(blk))
            blk["rawadj"] = (re.search(r"\)\s*(-[\d.]+)", b).group(1)
                             if re.search(r"\)\s*(-[\d.]+)", b) else None)
            self.blocks.append(blk)
        # Augment with the 144-char harvested catalog so ANY Russian/Latin
        # character can be measured and rendered on any template.
        if _CAT_BLOB is not None:
            for cid, entry in _CAT_BLOB["catalog"].items():
                if cid > 0:
                    ch = entry["char"]
                    self.cid2char.setdefault(cid, ch)
                    self.char2cid.setdefault(ch, cid)
                    self.W.setdefault(cid, entry["width"])

    def adv_units(self, cid):
        if _CAT_BLOB is not None and cid in _CAT_BLOB["catalog"]:
            return struct.unpack(">H", _CAT_BLOB["catalog"][cid]["hmtx"][:2])[0]
        g = self.cid2gid.get(cid, 0)
        return self._adv[g] if g < len(self._adv) else 2048

    def width_dots(self, text, size):
        """openhtmltopdf layout width in int dots: integer /W widths summed,
        scaled by size dots in float32, ceil-with-0.01-tolerance, plus
        n x letter-spacing (float dots). Verified: gives R=6860.00 exactly
        on every value row of 15 genuine receipts."""
        import math
        acc = sum(self.W.get(self.char2cid.get(c, 3), 1000) for c in text)
        res = float(_f32(_f32(float(acc)) / _f32(1000.0)) * _f32(_f32(float(size)) * _DPP))
        fl = math.floor(res)
        w_int = int(fl) if (res - fl) < 0.009999999776482582 else int(math.ceil(res))
        ls = _LS_DOTS[size]
        return w_int + int(float(_f32(float(len(text))) * _f32(ls)))

    def width_hmtx_dots(self, text, size):
        """openhtmltopdf float32 hmtx width in int dots (exact for 06.11 date)."""
        import math
        acc = _f32(0.0)
        for c in text:
            acc = _f32(acc + _f32(_f32(float(self.adv_units(self.char2cid.get(c, 3)))) * _F32_SCALE))
        res = float(_f32(_f32(acc / _f32(1000.0)) * _f32(_f32(float(size)) * _DPP)))
        fl = math.floor(res)
        w_int = int(fl) if (res - fl) < 0.009999999776482582 else int(math.ceil(res))
        ls = _LS_DOTS[size]
        return w_int + int(float(_f32(float(len(text))) * _f32(ls)))

    def _inline_width_dots(self, text, size):
        """Breaker.getTextWidthWithLetterSpacing for ONE InlineText:
        PdfBoxTextRenderer.getWidth (f32( SigmaW/1000 x sizeDots ), floor if
        frac<0.01 else ceil) + int(float32(len x letterSpacing)).  This is the
        exact decompiled openhtmltopdf rule (TEXT_MEASURING_DELTA = 0.01f)."""
        import math
        acc = sum(self.W.get(self.char2cid.get(c, 3), 1000) for c in text)
        res = float(_f32(_f32(float(acc)) / _f32(1000.0)) * _f32(_f32(float(size)) * _DPP))
        fl = math.floor(res)
        w_int = int(fl) if (res - fl) < 0.009999999776482582 else int(math.ceil(res))
        ls = _LS_DOTS[size]
        return w_int + int(float(_f32(float(len(text))) * _f32(ls)))

    def width_date_dots(self, text, size=9.0):
        """Layout width of the date row.  The date is split at the space into
        TWO InlineTexts ('DD.MM.YYYY,' + ' HH:MM') at the BreakIterator break
        after the comma; each part is measured by _inline_width_dots.
        Verified against 22 genuine 04-07.11 dates: exact on 07.11 (both new
        receipts) and 13/22 overall; 9/22 receipts (a per-receipt InlineText
        merge variant) sit 1 dot further left.  Donor calibration cancels the
        residual whenever donor and target share the variant."""
        d, t = text.split(" ", 1)
        return self._inline_width_dots(d, size) + self._inline_width_dots(" " + t, size)

    def dots_x(self, rawx):
        return int(round(float(rawx) / _S))

    def _block_cids(self, body):
        cids = []
        # scan TJ arrays element by element: literal strings "(..)" (each a
        # 2-byte glyph code) or hex strings "<..>", skipping the numeric
        # tracking adjustments between them.
        for arr in re.findall(r"\[(.*?)\]\s*TJ", body, re.S):
            i = 0
            while i < len(arr):
                ch = arr[i]
                if ch == "(":
                    j = i + 1
                    buf = bytearray()
                    while j < len(arr):
                        if arr[j] == "\\" and j + 1 < len(arr):
                            e = arr[j + 1]
                            # PDF literal-string escapes (PDF 1.7 7.3.4.2):
                            # \r \n \t \b \f \( \) \\ and \ddd (octal).  Raw
                            # CR/LF would be normalized to LF, so emitted
                            # escapes must be decoded back to the real byte.
                            if e == "r":
                                buf.append(0x0d)
                            elif e == "n":
                                buf.append(0x0a)
                            elif e == "t":
                                buf.append(0x09)
                            elif e == "b":
                                buf.append(0x08)
                            elif e == "f":
                                buf.append(0x0c)
                            elif e == "\\" or e == "(" or e == ")":
                                buf.append(ord(e))
                            elif e in "01234567":
                                # octal escape \ddd (1-3 octal digits)
                                oct_s = e
                                k = j + 2
                                while k < len(arr) and arr[k] in "01234567" and len(oct_s) < 3:
                                    oct_s += arr[k]; k += 1
                                buf.append(int(oct_s, 8) & 0xff)
                                j = k; continue
                            else:
                                buf.append(ord(e))
                            j += 2; continue
                        if arr[j] == ")":
                            break
                        buf.append(ord(arr[j])); j += 1
                    for k in range(0, len(buf) - 1, 2):
                        cids.append(struct.unpack(">H", bytes(buf)[k:k+2])[0])
                    i = j + 1
                elif ch == "<":
                    j = arr.index(">", i)
                    bb = bytes.fromhex(arr[i+1:j])
                    for k in range(0, len(bb) - 1, 2):
                        cids.append(struct.unpack(">H", bb[k:k+2])[0])
                    i = j + 1
                else:
                    i += 1
        return cids

    def _decode_block(self, body):
        return "".join(self.cid2char.get(c, "?") for c in self._block_cids(body))

    def width(self, text, size):
        # advance = sum((glyph width - TJ adjustment) ) ; TJ uses a constant
        # tracking adjustment per size, recovered from the block itself.
        return sum(self.W.get(self.char2cid.get(c, 3), 1000) for c in text) * size / 1000.0

    def tracking(self, block):
        adjs = [float(a) for a in re.findall(r"\)\s*(-[\d.]+)", block["body"])]
        return adjs[0] if adjs else 0.0

    def text_width(self, text, size, tracking):
        # Layout width (CSS letter-spacing) counts one tracking step per
        # glyph, including the last — that is how openhtmltopdf positions
        # right/center-aligned blocks (the TJ stream itself has n-1 steps).
        n = len(text)
        return (sum(self.W.get(self.char2cid.get(c, 3), 1000) for c in text)
                - tracking * n) * size / 1000.0

    def covers(self, *texts):
        return all(c in self.char2cid for t in texts for c in t)

# Парсинг донора тяжёлый (fitz + TTF + regex по всему контенту), а шаблонов
# всего 7 и они неизменны на диске: кэшируем _Tpl по пути. Раньше каждый
# generate() пересоздавал до 5 шаблонов — лишние ~сотни мс на квитанцию
# и повторные fitz.open одних и тех же файлов. Кэш ограничен числом
# файлов-шаблонов, утечкой не является; _Tpl после постройки read-only.
_TPL_CACHE: dict = {}
_TPL_LOCK = threading.Lock()

def _get_tpl(path: str) -> "_Tpl":
    t = _TPL_CACHE.get(path)
    if t is None:
        with _TPL_LOCK:
            t = _TPL_CACHE.get(path)
            if t is None:
                t = _Tpl(path)
                _TPL_CACHE[path] = t
    return t

# ---------------------------------------------------------------- SBP ID
_GW_A = ["1001", "1002", "1003", "1004"]
_GW_B = ["1010", "1016", "1017", "1018"]
_GW_W = {"A": [("1001", 1), ("1002", 1), ("1003", 4), ("1004", 2)],
         "B": [("1010", 1), ("1016", 2), ("1017", 2), ("1018", 1)]}
_NODES = ["5", "6", "7", "8", "A", "E", "H", "N", "P", "T", "U", "W"]
_NODE_W = {"7": 2, "W": 2, "5": 1, "6": 1, "8": 1, "A": 1, "E": 1,
           "H": 1, "N": 1, "P": 1, "T": 1, "U": 1}
# genuine (ms, flag) joint bootstrap: flag is NOT independent of ms
# (Spearman +0.896 on 14 genuine IDs: flag0 ms<=756, flag1 771..977, flag2 966+)
_MS_FLAG = [(294, "0"), (966, "2"), (993, "2"), (924, "1"), (262, "0"),
            (428, "0"), (170, "0"), (924, "1"), (756, "0"), (977, "1"),
            (485, "0"), (850, "1"), (142, "0"), (771, "1")]

# NSPK era flip (genuine VTB+WB, both sender banks): last old-era ID
# 2026-11-05 18:14:27Z (B/0011881102), first new-era ID 2026-11-05
# 22:11:07Z (G/0011890101, gateways 1011/1017/1000, nodes 9/1/9/4/2,
# all flag=0).  Keyed by the ID's UTC instant at the round hour inside
# that attested interval, NOT by DOY (both eras occur on DOY 309).
_ERA_FLIP_UTC = datetime(2026, 11, 5, 22, 0, 0)
# A-route gateways attested in the new era (n=4, weights = counts):
# 1006 x2, 1000 x1, 1003 x1 (07-11-26_02-18).  Sampled with those weights —
# only genuine-attested values are emitted; the wider NSPK A range is
# grammatically valid but never observed in this era (final op_id audit 07.11).
_GW_A_NEW = [("1000", 1), ("1006", 2), ("1003", 1)]
# B-route gateways attested on genuine 06-07.11 VTB receipts (weights = counts):
# 1011 x7, 1017 x2, 1014 x1 (07.11).  All inside the NSPK B range 1009..1018.
_GW_B_NEW = [("1011", 7), ("1017", 2), ("1014", 1)]
# nodes attested in the new era on 14 genuine G/0011890101 IDs (weights):
# 9 x5, 5 x3, 1 x2, 2/4/6/8 x1
_NODES_NEW = ["1", "2", "4", "5", "6", "8", "9"]
_NODE_W_NEW = {"9": 5, "5": 3, "1": 2, "2": 1, "4": 1, "6": 1, "8": 1}

def _era(utc: datetime):
    return ("G", "0011890101") if utc >= _ERA_FLIP_UTC else ("B", "0011881102")

def make_op_id(op_dt_msk: datetime, rng: _rnd.Random = None,
               allowed_nodes=None, required_digits=None) -> str:
    r = rng or _rnd
    utc = op_dt_msk - timedelta(hours=3) - timedelta(seconds=1)
    route, suffix = _era(utc)
    if route == "G":
        # New era (G/0011890101): 14/14 genuine VTB IDs have flag='0'
        # (06-07.11; flag 1/2 not attested in this era).  Prefix skews B:
        # 4 A : 10 B on genuine 06-07.11 receipts.
        pfx = r.choices(["A", "B"], weights=[2, 5])[0]
    else:
        pfx = r.choices(["A", "B"], weights=[57, 43])[0]
    if allowed_nodes is not None and pfx not in allowed_nodes:
        pfx = "B" if "B" in allowed_nodes else "A"
    if route == "G":
        flag = "0"
        nodes = [n for n in _NODES_NEW if allowed_nodes is None or n in allowed_nodes]
        if not nodes:
            nodes = _NODES_NEW
        node = r.choices(nodes, weights=[_NODE_W_NEW[n] for n in nodes])[0]
        gws = _GW_A_NEW if pfx == "A" else _GW_B_NEW
        gw = r.choices([g for g, _ in gws], weights=[w for _, w in gws])[0]
        for _ in range(500):
            ms = r.randint(50, 999)
            ms_s = f"{ms:03d}"
            if len(set(ms_s)) == 1:
                continue
            cand = (pfx + str(utc.year)[-1] + f"{utc.timetuple().tm_yday:03d}"
                    + f"{utc.hour:02d}{utc.minute:02d}{utc.second:02d}{ms_s}"
                    + flag + node + "0" + route + gw + suffix)
            if not required_digits or all(d in cand for d in required_digits):
                return cand
    else:
        ms, flag = r.choice(_MS_FLAG)
        nodes = [n for n in _NODES if allowed_nodes is None or n in allowed_nodes]
        if not nodes:
            nodes = _NODES
        node = r.choices(nodes, weights=[_NODE_W[n] for n in nodes])[0]
        gws = _GW_W[pfx]
        gw = r.choices([g for g, _ in gws], weights=[w for _, w in gws])[0]
    return (pfx + str(utc.year)[-1] + f"{utc.timetuple().tm_yday:03d}"
            + f"{utc.hour:02d}{utc.minute:02d}{utc.second:02d}{ms:03d}"
            + flag + node + "0" + route + gw + suffix)

def format_amount(rub) -> str:
    if isinstance(rub, float) and rub == int(rub):
        rub = int(rub)
    return f"{rub:,} ₽".replace(",", " ")

def _fmt(v: float) -> str:
    s = f"{v:.5f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"

# ---- openhtmltopdf exact arithmetic (dots = 1/20 CSS px) ------------------
def _f32(v):
    return struct.unpack("f", struct.pack("f", float(v)))[0]

_DPP = _f32(_f32(20.0) * _f32(4.0) / _f32(3.0))   # 26.6666660308837890625
_S = 1.0 / _DPP                                    # double 0.0375000008940697
_F32_SCALE = _f32(_f32(1000.0) / _f32(2048.0))     # set per-upem in _ttf_adv_units
_LS_DOTS = {9.0: 4.0, 10.5: 6.0, 13.5: 4.0}

def _ttf_adv_units(ttf: bytes):
    n = struct.unpack(">H", ttf[4:6])[0]
    tabs = {}
    for i in range(n):
        o = 12 + i * 16
        tabs[ttf[o:o + 4].decode("latin-1")] = struct.unpack(">II", ttf[o + 8:o + 16])
    mo, _ = tabs["maxp"]
    ng = struct.unpack(">H", ttf[mo + 4:mo + 6])[0]
    hao, _ = tabs["hhea"]
    nh = struct.unpack(">H", ttf[hao + 34:hao + 36])[0]
    hmo, _ = tabs["hmtx"]
    return [struct.unpack(">H", ttf[hmo + min(g, nh - 1) * 4:hmo + min(g, nh - 1) * 4 + 2])[0]
            for g in range(ng)]

def fmt5(v) -> str:
    """PDFBox NumberFormatUtil.formatFloatFast(v, 5): round half away from
    zero at 5 dp on the float32 value, trailing zeros stripped."""
    import math
    neg = v < 0
    av = abs(float(v))
    ip = int(av)
    frac = int((av - ip) * 100000 + 0.5)
    if frac >= 100000:
        ip += 1
        frac = 0
    if frac == 0:
        return ("-" if neg else "") + str(ip)
    s = str(frac).rjust(5, "0").rstrip("0")
    return ("-" if neg else "") + f"{ip}.{s}"

def tm_x(dots: int) -> str:
    """dots -> Tm x string: one double multiply by S, one float32 cast, 5dp."""
    return fmt5(_f32(dots * _S))

def _esc(cid_bytes: bytes) -> bytes:
    # Escape backslash and parens.  CR/LF bytes are NOT escaped here; they are
    # handled by _emit_item, which switches the whole string element to hex
    # form — that is what genuine openhtmltopdf/PDFBox receipts do for CID 13
    # ('*' in the account row): 44/49 genuine corpus files carry `<000D>`,
    # none carries the '\r' literal escape.
    return (cid_bytes.replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(").replace(b")", b"\\)"))

# openhtmltopdf forces HEX form for string elements containing an EOL byte
# (CR would otherwise be normalized to LF and corrupt the glyph code) OR a
# non-ASCII byte.  Empirical over the 100-file genuine corpus: CID 0x000D
# ('*') is <000D> in 95/100 receipts, CID 0x020D ('Ё') is <020D> in 1/1,
# CIDs 0x02F2 ('֏') and 0x03FF ('•') are hex in 7/7 receipts using them;
# CIDs 0x0009/0x000C stay RAW literal bytes, and '(' stays the escaped
# literal sequence '\\(' (CID 0x0228 literal in 100/100 receipts).  NOTE:
# 5/100 genuine receipts (06-11 x3, 07-11 02-18/02-46 — a second emission
# dialect) write cid 13 as literal '(\x00\r)'; every donor here is the hex
# dialect, so generated files emit <000D> like 95/100 genuine receipts.
_FORCE_HEX = frozenset([0x0A, 0x0D] + list(range(0x80, 0x100)))

def _glyph_item(cid: int) -> str:
    """One TJ-array string item for a glyph code, in PDFBox emission style:
    hex string when any byte is in PDFBox's force-hex set, literal else."""
    b2 = struct.pack(">H", cid)
    if any(b in _FORCE_HEX for b in b2):
        return "<" + b2.hex().upper() + ">"
    return "(" + _esc(b2).decode("latin-1") + ")"

def _build_block(tpl: _Tpl, block, new_text: str, align: str, y_off: float = 0.0,
                 y_str: str = None) -> str:
    """Re-emit one BT..ET block in donor style, with openhtmltopdf-exact
    dots arithmetic for the Tm x coordinate (calibrated on the donor block,
    so model errors cancel for similar texts).  y_off shifts the Tm y
    (message-line page growth); y_str pins the exact y token (ulp-level
    bottom-anchor variants of genuine receipts)."""
    if new_text == block["text"] and not y_off and y_str is None:
        return block["body"]
    if new_text == block["text"] and (y_off or y_str is not None):
        # pure vertical shift: clone the donor block, rewrite only the Tm y
        new_y = y_str or fmt5(float(block["rawy"]) + y_off)
        return re.sub(r"(1 0 0 1 \S+ )\S+ Tm", r"\g<1>%s Tm" % new_y,
                      block["body"])
    size = block["size"]
    e_donor = tpl.dots_x(block["rawx"])
    if align == "r" and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}", new_text):
        # Date row: openhtmltopdf measures each inline box of the date line
        # separately (see _date_width_dots); the harvested table is exact for
        # every attested genuine date, the structural model otherwise.
        w_new = _date_width_dots(tpl, new_text)
        w_donor = _date_width_dots(tpl, block["text"])
        dots = e_donor + w_donor - w_new
    elif align == "r":
        w_new = tpl.width_dots(new_text, size)
        w_donor = tpl.width_dots(block["text"], size)
        dots = e_donor + w_donor - w_new
    elif align == "c":
        w_new = tpl.width_dots(new_text, size)
        w_donor = tpl.width_dots(block["text"], size)
        midpoint = e_donor + w_donor // 2
        dots = midpoint - w_new // 2
    else:  # keep (left-aligned, labels)
        dots = e_donor
    x_s = tm_x(dots) if align != "keep" else block["rawx"]
    adj = block["rawadj"] or _fmt_adj(tpl.tracking(block))
    items = []
    for i, ch in enumerate(new_text):
        cid = tpl.char2cid[ch]
        item = _glyph_item(cid)
        if i < len(new_text) - 1:
            item += adj
        items.append(item)
    y_s = y_str or (block["rawy"] if not y_off
                    else fmt5(float(block["rawy"]) + y_off))
    body = ("BT\n/F1 %s Tf\n1 0 0 1 %s %s Tm\n[%s] TJ\nET"
            % (block["rawsize"], x_s, y_s, " ".join(items)))
    return body

def _fmt_adj(tr: float) -> str:
    s = f"{tr:.5f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"

# ---- date-line layout width ---------------------------------------------
# openhtmltopdf splits the date line into inline boxes ("DD.MM.YYYY," /
# " " / "HH:MM") and rounds each box separately, so the plain aggregate
# /W model underestimates by 0..2 dots depending on the exact digits.
# Ground truth below was harvested from 63 genuine receipts (right edge
# R=6860 dots on every genuine file): text -> exact width in int dots.
_DATE_WIDTH_KNOWN = {
    '04.03.2026, 20:45': 2069,
    '04.11.2026, 18:11': 1890,
    '04.11.2026, 18:12': 1920,
    '05.11.2026, 21:09': 1954,
    '05.11.2026, 21:10': 1916,
    '05.11.2026, 21:11': 1877,
    '05.11.2026, 21:12': 1907,
    '05.11.2026, 21:13': 1913,
    '05.11.2026, 21:14': 1916,
    '06.11.2026, 01:11': 1889,
    '06.11.2026, 01:12': 1919,
    '06.11.2026, 03:34': 1999,
    '06.11.2026, 04:00': 2006,
    '06.11.2026, 04:23': 1993,
    '06.11.2026, 12:58': 1951,
    '06.11.2026, 16:41': 1926,
    '07.03.2026, 18:52': 2015,
    '07.03.2026, 19:10': 1990,
    '07.11.2026, 01:52': 1941,
    '08.03.2026, 14:36': 2037,
    '08.03.2026, 14:42': 2033,
    '08.03.2026, 14:48': 2040,
    '08.03.2026, 14:55': 2032,
    '08.03.2026, 15:06': 2036,
    '08.03.2026, 15:39': 2032,
    '13.03.2026, 04:41': 2001,
    '13.03.2026, 04:49': 2038,
    '13.03.2026, 05:18': 1995,
    '13.03.2026, 05:26': 2024,
    '13.03.2026, 06:00': 2039,
    '16.03.2026, 00:58': 2036,
    '19.03.2026, 15:19': 1958,
    '21.09.2026, 15:07': 1979,
    '24.03.2026, 07:04': 2059,
    '27.03.2026, 03:15': 2012,
    '27.03.2026, 03:20': 2047,
    '27.03.2026, 03:25': 2042,
    '28.02.2026, 12:32': 2004,
    '29.03.2026, 02:47': 2049,
    '29.03.2026, 16:28': 2021,
    '29.03.2026, 16:47': 2018,
}

def _date_width_dots(tpl, text: str) -> int:
    """Layout width of the date line in int dots. Exact for every date text
    attested in genuine receipts; the 3-box structural model
    (ceil/ceil/round, letter-spacing per box) otherwise (26/41 exact,
    +-1 dot otherwise -- subpixel)."""
    w = _DATE_WIDTH_KNOWN.get(text)
    if w is not None:
        return w
    import math
    d, t = text.split(" ")
    def _part(chars, rounding):
        acc = sum(tpl.W.get(tpl.char2cid.get(c, 3), 1000) for c in chars)
        res = float(_f32(_f32(float(acc)) / _f32(1000.0)) * _f32(_f32(9.0) * _DPP))
        fl = math.floor(res)
        if rounding == "c":
            v = int(fl) if (res - fl) < 0.009999999776482582 else int(math.ceil(res))
        else:  # round half up
            v = int(math.floor(res + 0.5))
        return v + len(chars) * 4
    return _part(d, "c") + _part(" ", "c") + _part(t, "r")

def esc_char(cid):
    return struct.pack(">H", cid)

_TJ_NUM = re.compile(r"\)\s*(-?[\d.]+)")

def _block_texts(tpl: _Tpl):
    return [b["text"] for b in tpl.blocks]

VTB_BANKS = [
    "Т-Банк", "Сбербанк", "Альфа-Банк", "Озон Банк (Ozon)",
    "Совкомбанк", "ВТБ", "Яндекс Банк", "Райффайзен Банк", "Газпромбанк",
]

_PAYERS_MIKHAIL = [
    "Максим Андреевич Х.", "Матвей Викторович Х.", "Тимофей Максимович Х.",
    "Вадим Михайлович Х.", "Денис Максимович Х.", "Павел Михайлович Х.",
    "Антон Максимович Х.", "Даниил Матвеевич Х.",
    "Сергей Александрович К.", "Дмитрий Евгеньевич Б.", "Алексей Николаевич Р.",
]
_PAYERS_NIKITA = [
    "Николай Ульянович Х.", "Никита Ульянович Х.",
]
_PAYERS_OZON = [
    "Антон Викторович Х.", "Денис Андреевич Х.", "Павел Дмитриевич Х.",
    "Вадим Алексеевич Х.", "Даниил Павлович Х.", "Степан Викторович Х.",
]

def format_phone_vtb(phone: str) -> str:
    """VTB receipts ALWAYS display the recipient phone as +7 (XXX) XXX-XX-XX
    with non-breaking hyphens (U+2011).  Normalize any raw input (79677399849,
    89677399849, +7 967 739-98-49, already-formatted) to that exact shape;
    non-Russian / unparseable input passes through with hyphens mapped to
    U+2011 (idempotent for pre-formatted strings)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return ("+7 (%s) %s\u2011%s\u2011%s"
                % (digits[1:4], digits[4:7], digits[7:9], digits[9:11]))
    return (phone or "").replace("-", "\u2011")

def check_text(text: str, bank: str = None) -> list:
    """Return list of characters in text not covered by the 144-char VTB catalog."""
    t = (text or "").replace("-", "\u2011")
    if _CAT_BLOB is not None:
        avail = set(_CAT_BLOB["char2cid"].keys())
        return [c for c in t if c not in avail]
    b = (bank or "").replace("-", "\u2011")
    new_donors = _TEMPLATES_PLAIN[:3]
    union_chars = set()
    for path in new_donors:
        if os.path.exists(path):
            cand = _get_tpl(path)
            if not b or cand.covers(b):
                union_chars |= set(cand.char2cid.keys())
    return [c for c in t if c not in union_chars]

def pick_payer_for(recipient: str = "", bank: str = "Т-Банк") -> str:
    """Pick a fresh payer name."""
    b = (bank or "Т-Банк").replace("-", "\u2011")
    r = (recipient or "").replace("-", "\u2011")
    if "Ozon" in b or "Озон" in b:
        return _rnd.choice(_PAYERS_OZON)
    if "Н" in r or "У" in r:
        return _rnd.choice(_PAYERS_NIKITA)
    return _rnd.choice(_PAYERS_MIKHAIL)

def generate(payer: str, recipient: str, phone: str, bank: str, amount_rub,
             message: str = None, dt: datetime = None,
             op_id: str = None, template: str = None,
             account: str = None, meta_out: dict = None,
             created_dt: datetime = None, doc_id: str = None) -> bytes:
    """message=None/'' -> receipt WITHOUT a message (plain donor only).

    created_dt/doc_id pin /CreationDate and the trailer /ID exactly
    (byte-replica mode for a known genuine receipt); when omitted both
    follow the usual immediate-receipt model.

    Fields that stay byte-identical to the donor: header recipient block
    (block 0 mirrors the recipient and is handled below), labels,
    logo form, fonts. Only the value blocks + Info dates are rewritten.
    """
    acct_digits = re.sub(r"\D", "", str(account or ""))
    account_s = f"*{acct_digits[-4:]}" if len(acct_digits) >= 4 else "*0298"
    now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    lag_s = None
    if dt is None:
        # 28/28 immediate genuine VTB receipts (incl. 07.11): op second in
        # 08..52, CreationDate = op_time + 5..24s (== ID_time + 6..25s), same
        # minute as display; for a freshly generated receipt CreationDate IS
        # the generation instant (genuine immediate CDs never sit in the
        # future), so the chosen lag is threaded into _assemble.
        if now_msk.second >= 13:
            cd_sec = _rnd.randint(max(13, now_msk.second - 10), now_msk.second)
            # clamp keeps op_sec = cd_sec - lag + 1 inside the attested 08..52
            lag = _rnd.randint(max(6, cd_sec - 51), min(25, cd_sec - 7))
            # op = ID_time + 1s (the -1s rule), so op_sec = cd_sec - lag + 1
            op_dt = now_msk.replace(second=cd_sec - lag + 1, microsecond=0)
            lag_s = lag - 1  # CreationDate lands exactly on "now"
        else:
            prev = now_msk - timedelta(minutes=1)
            op_sec = _rnd.randint(8, 52)
            op_dt = prev.replace(second=op_sec, microsecond=0)
    elif dt.tzinfo is not None:
        op_dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)
    else:
        op_dt = dt
    if not 8 <= op_dt.second <= 52:
        # op seconds outside the attested 08..52 envelope are redrawn: they
        # are invisible on the receipt (display is minute-precision) and
        # 53..59 would force a CreationDate lag below the genuine +5s floor
        # or push CreationDate into the next minute (never seen on 28/28
        # immediate genuine receipts).
        op_dt = op_dt.replace(second=_rnd.randint(8, 52))
    display = op_dt.strftime("%d.%m.%Y, %H:%M")
    amount_s = format_amount(amount_rub)
    # VTB receipts typeset hyphens as U+2011 (non-breaking hyphen); the
    # recipient phone is ALWAYS shown as +7 (XXX) XXX-XX-XX on genuine
    # receipts, so normalize raw digit input here as well (the bot's own
    # format_phone already produces this shape -- the call is idempotent).
    phone = format_phone_vtb(phone)
    bank = (bank or "").replace("-", "\u2011")
    # Names too: check_text() normalizes '-' -> U+2011 before checking,
    # so hyphenated names passed the bot's gate and then died here with
    # "no VTB template covers the required glyphs" (Анна-Мария ...).
    recipient = (recipient or "").replace("-", "\u2011")
    payer = (payer or "").replace("-", "\u2011")
    if message:
        message = message.replace("-", "\u2011")

    # choose a template covering every glyph we need; without a message use
    # ONLY no-message donors, preferring a donor with zero unused non-digit
    # glyphs so the font subset stays 100% byte-identical to the donor.
    tpl = None
    best_unused = None
    candidates = [template] if template else (_TEMPLATES_MSG if message else _TEMPLATES_PLAIN)
    static_s = "Исходящий перевод СБПСтатусВыполненоДата операцииСчет списанияИмя плательщикаПолучательТелефон получателяБанк получателяID операции в СБПСумма операцииБанк ВТБ (ПАО)Операция выполнена"
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        cand = _get_tpl(path)
        probe = [recipient, payer, phone, bank, display, amount_s, account_s]
        if message:
            probe.append(message.replace("\n", ""))
        if cand.covers(*probe):
            used_non_id = set("".join(probe) + static_s)
            extra = len(set(cand.donor_cids) - {cand.char2cid[c] for c in used_non_id if c in cand.char2cid} - {cand.char2cid[c] for c in "0123456789BG" if c in cand.char2cid})
            if tpl is None or extra < best_unused:
                tpl = cand
                best_unused = extra
                if extra == 0:
                    break
    if tpl is None:
        raise ValueError("no VTB template covers the required glyphs")
    if op_id is None:
        # Ensure all digits present in the donor subset remain used in the
        # receipt so no digit glyph is ever dropped from the font subset.
        donor_digits = {tpl.cid2char[c] for c in tpl.donor_cids if c in tpl.cid2char} & set("0123456789")
        used_digits = set(display + phone + amount_s + account_s) & set("0123456789")
        req_digits = donor_digits - used_digits
        for _ in range(500):
            op_id = make_op_id(op_dt, allowed_nodes=set(tpl.char2cid),
                               required_digits=req_digits)
            if tpl.covers(op_id):
                break
        else:
            raise ValueError("template does not cover the operation ID glyphs")
    if not tpl.covers(op_id):
        raise ValueError("template does not cover the operation ID glyphs")

    # Build the replacement plan: block 1 (10.5pt under the title) mirrors
    # the recipient name, centered; every other value maps via label
    # adjacency (_discover_fields).  (The old edits/label_idx/_FIELD_OF
    # scaffolding computed maps that were never read.)
    plan = {}
    if len(tpl.blocks) > 1 and tpl.blocks[1]["size"] == 10.5:
        plan[1] = (recipient, "c")
    fields = _discover_fields(tpl)
    newvals = {
        "date": (display, "r"),
        "account": (account_s, "r"),
        "payer": (payer, "r"),
        "recipient": (recipient, "r"),
        "phone": (phone, "r"),
        "bank": (bank, "r"),
        "amount": (amount_s, "r"),
    }
    for role, idx in fields.items():
        if role in newvals and idx is not None:
            plan[idx] = newvals[role]
    # ID: two blocks (25 + 7 chars)
    if fields.get("id1") is not None:
        plan[fields["id1"]] = (op_id[:25], "r")
    if fields.get("id2") is not None:
        plan[fields["id2"]] = (op_id[25:], "r")

    # ---- multi-line message ----------------------------------------------
    # The message is rendered as N right-aligned lines (newline-separated in
    # the input).  A receipt with N message lines grows the page by
    # _msg_dh(N) points at the TOP: every block from the title through the
    # 'Сообщение' label (and the clip path, header rule and logo) shifts up
    # by the delta vs the donor's own line count; 'Получатель' and everything
    # below is anchored to the bottom and never moves (verified on all 11
    # genuine message receipts of the corpus: 461.25/471.75/483.75/507.75 pt
    # for 1/2/3/5 lines).
    msg_lines = message.split("\n") if message else []
    donor_msg_idx = [fields[r] for r in ("msg1", "msg2", "msg3")
                     if fields.get(r) is not None]
    for i in donor_msg_idx:
        plan.pop(i, None)
    y_off = 0.0
    if msg_lines:
        if not donor_msg_idx:
            raise ValueError("no VTB message donor available")
        y_off = _msg_dh(len(msg_lines)) - _msg_dh(len(donor_msg_idx))
    msg1_idx = fields.get("msg1") if msg_lines else None
    # bottom-anchor y-token variants (float32 accumulation noise at the ulp
    # level in genuine receipts; harvested per message-line count from all 11
    # genuine message receipts): 1-2 lines -> Банк rows '154.5'/'155.25';
    # 5 lines -> Телефон rows '178.49998'/'179.24998'; everything else keeps
    # the (3-line) donor token.
    y_over = {}
    if msg_lines:
        n_lines = len(msg_lines)
        if n_lines <= 2:
            y_over[fields["bank"] - 1] = "154.5"
            y_over[fields["bank"]] = "155.25"
        if n_lines >= 5:
            y_over[fields["phone"] - 1] = "178.49998"
            y_over[fields["phone"]] = "179.24998"

    content = tpl.content
    # replace from last to first to keep spans valid
    repls = []  # (start, end, new_block_text)
    for idx, b in enumerate(tpl.blocks):
        if idx in plan:
            new_text, align = plan[idx]
            off = y_off if (msg_lines and idx < msg1_idx) else 0.0
            repls.append((b["span"][0], b["span"][1],
                          _build_block(tpl, b, new_text, align, y_off=off,
                                       y_str=y_over.get(idx))))
        elif msg_lines and idx < msg1_idx:
            repls.append((b["span"][0], b["span"][1],
                          _build_block(tpl, b, b["text"], "keep", y_off=y_off)))
        elif idx in y_over:
            repls.append((b["span"][0], b["span"][1],
                          _build_block(tpl, b, b["text"], "keep",
                                       y_str=y_over[idx])))
    if msg_lines:
        first = tpl.blocks[msg1_idx]
        last = tpl.blocks[donor_msg_idx[-1]]
        label_y = float(tpl.blocks[msg1_idx - 1]["rawy"]) + y_off
        bodies = []
        for k, line in enumerate(msg_lines):
            bodies.append(_msg_line_block(tpl, first, line,
                                          label_y + 0.75 - 12.0 * k))
        repls.append((first["span"][0], last["span"][1], "\n".join(bodies)))
    for s, e, new_block in sorted(repls, reverse=True):
        # span refers to the BT..ET match in the decoded stream text
        content = content[:s] + new_block + content[e:]
    if msg_lines:
        # chrome reflow comes AFTER the span surgery: the replacement may
        # change the stream length (e.g. '418.5' -> '396.75' for a 1-line
        # message), which would invalidate every BT..ET span.
        content = _reflow_page_chrome(tpl, content, y_off)
    return _assemble(tpl, content, op_dt, meta_out, {
        "recipient": recipient, "payer": payer, "phone": phone,
        "bank": bank, "amount": amount_s, "op_id": op_id, "display": display,
        "account": account_s,
    }, lag_s=lag_s, page_h=(tpl.page_h + y_off) if msg_lines else None,
        created_dt=created_dt, doc_id=doc_id)

# Page-height growth per message line count (genuine corpus):
# 0 lines -> 437.25 pt, 1 -> 461.25, 2 -> 471.75, 3 -> 483.75, 5 -> 507.75.
def _msg_dh(n: int) -> float:
    if n <= 1:
        return {0: 0.0, 1: 24.0}[n]
    return 34.5 + 12.0 * (n - 2)

def _msg_line_block(tpl: _Tpl, donor_block, line: str, y: float) -> str:
    """One right-aligned message line, donor-styled; x by the int-dots model
    (verified 17/17 exact on the genuine message lines of the corpus)."""
    size = donor_block["size"]
    e_donor = tpl.dots_x(donor_block["rawx"]) + tpl.width_dots(donor_block["text"], size)
    dots = e_donor - tpl.width_dots(line, size)
    adj = donor_block["rawadj"] or _fmt_adj(tpl.tracking(donor_block))
    items = []
    for i, ch in enumerate(line):
        item = _glyph_item(tpl.char2cid[ch])
        if i < len(line) - 1:
            item += adj
        items.append(item)
    return ("BT\n/F1 %s Tf\n1 0 0 1 %s %s Tm\n[%s] TJ\nET"
            % (donor_block["rawsize"], tm_x(dots), fmt5(y), " ".join(items)))

def _reflow_page_chrome(tpl: _Tpl, content: str, y_off: float) -> str:
    """Shift the page-height-dependent vector chrome of a message receipt:
    the clip path (0 H m / 0 H l), the blue header rule at H-64.5/H-64.75
    and the logo form cm at H-48."""
    H = tpl.page_h
    Hn = H + y_off
    old = (f"0 {fmt5(H)} m\n281.21249 {fmt5(H)} l\n281.21249 0.03748 l\n"
           f"0 0.03748 l\n0 {fmt5(H)} l")
    new = (f"0 {fmt5(Hn)} m\n281.21249 {fmt5(Hn)} l\n281.21249 0.03748 l\n"
           f"0 0.03748 l\n0 {fmt5(Hn)} l")
    # assert под python -O выключается: промах replace стал бы молчаливой
    # порчей PDF, поэтому здесь явные проверки.
    if old not in content:
        raise ValueError("clip path not found in VTB content stream")
    content = content.replace(old, new)
    a, b = H - 64.5, H - 65.25
    old = (f"24 {fmt5(a)} m\n257.25 {fmt5(a)} l\n257.25 {fmt5(b)} l\n"
           f"24 {fmt5(b)} l")
    new = (f"24 {fmt5(a + y_off)} m\n257.25 {fmt5(a + y_off)} l\n"
           f"257.25 {fmt5(b + y_off)} l\n24 {fmt5(b + y_off)} l")
    if old not in content:
        raise ValueError("header rule not found in VTB content stream")
    content = content.replace(old, new)
    old = f"1 0 0 1 91.125 {fmt5(H - 48.0)} cm"
    new = f"1 0 0 1 91.125 {fmt5(H - 48.0 + y_off)} cm"
    if old not in content:
        raise ValueError("logo cm not found in VTB content stream")
    content = content.replace(old, new)
    return content

def _discover_fields(tpl: _Tpl) -> dict:
    """Map layout roles to block indices using label adjacency."""
    blocks = tpl.blocks
    fields = {"date": None, "account": None, "payer": None, "recipient": None,
              "phone": None, "bank": None, "amount": None, "id1": None,
              "id2": None, "msg1": None, "msg2": None, "msg3": None}
    labels = {
        "Дата операции": "date", "Счет списания": "account",
        "Имя плательщика": "payer",
        "Получатель": "recipient", "Телефон получателя": "phone",
        "Банк получателя": "bank", "ID операции в СБП": "id1",
        "Сумма операции": "amount", "Сообщение": "msg1",
    }
    for i, b in enumerate(blocks):
        role = labels.get(b["text"])
        if role and i + 1 < len(blocks):
            fields[role] = i + 1
            if role == "id1" and i + 2 < len(blocks):
                fields["id2"] = i + 2
            if role == "msg1":
                # following value blocks until the next label
                nxt = i + 2
                k = 2
                while nxt < len(blocks) and blocks[nxt]["text"] not in (
                        "Получатель", "Телефон получателя", "Банк получателя"):
                    if k <= 3:
                        fields[f"msg{k}"] = nxt
                    nxt += 1; k += 1
    return fields

# ------------------------------------------------------- font re-subset
_BASE25 = "BCDEFGHIJKLMNOPQRSTUVWXYZ"  # PDFBox TrueTypeEmbedder.getTag()

def _subset_tag(num: int) -> str:
    """PDFBox subset tag: base-25 of the gidToCid HashMap hashCode
    (sum of newGID^oldGID), least-significant digit first, left-padded
    with 'A' to 6 chars. Deterministic per glyph inventory."""
    sb = []
    n = num
    while n != 0 and len(sb) < 6:
        n, mod = divmod(n, 25)
        sb.append(_BASE25[mod])
    while len(sb) < 6:
        sb.insert(0, "A")
    return "".join(sb)

def _trailer_id(raw: bytes, created: str, created_dt: datetime = None) -> str:
    """PDFBox COSWriter document ID: MD5(decimalASCII(epochMillis) +
    COSString.toString() of every Info-dict value, in dictionary order).
    Verified 21/21 on genuine VTB PDFs: COSString.toString() wraps each
    value in 'COSString{...}' and idTime is +5..+948ms inside created_dt."""
    import hashlib
    body = _obj_bytes(raw, 3)
    prod = re.search(rb"/Producer \(([^)]*)\)", body).group(1).decode()
    vp = re.search(rb"/viewport \(([^)]*)\)", body).group(1).decode()
    th = re.search(rb"/Title <([0-9A-Fa-f]+)>", body).group(1).decode()
    tb = bytes.fromhex(th)
    title = tb[2:].decode("utf-16-be") if tb.startswith(b"\xfe\xff") else tb.decode("latin-1")
    msk = timezone(timedelta(hours=3))
    if created_dt is None:
        created_dt = datetime.strptime(created[2:16], "%Y%m%d%H%M%S")
    base_ms = int(created_dt.replace(tzinfo=msk).timestamp() * 1000)
    # Genuine /ID ms offset inside the CreationDate second: +5..+948 ms
    # (42/42 genuine VTB receipts: old 120..745, B-era 5..948, G-era 234..896).
    ms = base_ms + _rnd.randint(5, 948)
    vals = [created, prod, vp, title]
    suffix = "".join(f"COSString{{{v}}}" for v in vals).encode("latin-1", "replace")
    return hashlib.md5(str(ms).encode("latin-1") + suffix).hexdigest().upper()

def _sfnt_sum(data: bytes) -> int:
    if len(data) % 4:
        data += b"\0" * (4 - len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF

def _ttf_drop_glyphs(ttf: bytes, drop: set) -> bytes:
    """Remove glyphs from a PDFBox-style TTF subset, preserving the table
    layout (physical table order, long loca, all other bytes) exactly."""
    n = struct.unpack(">H", ttf[4:6])[0]
    entries = []  # [tag, checksum, offset, data]
    for i in range(n):
        o = 12 + i * 16
        tag = ttf[o:o + 4].decode("latin-1")
        ck, off, ln = struct.unpack(">III", ttf[o + 4:o + 16])
        entries.append([tag, ck, off, bytearray(ttf[off:off + ln])])
    tab = {e[0]: e[3] for e in entries}
    head, maxp, hhea = tab["head"], tab["maxp"], tab["hhea"]
    loc_fmt = struct.unpack(">h", head[50:52])[0]
    ng = struct.unpack(">H", maxp[4:6])[0]
    loca_raw = bytes(tab["loca"])
    if loc_fmt == 1:
        locs = list(struct.unpack(">%dI" % (len(loca_raw) // 4), loca_raw))
    else:
        locs = [v * 2 for v in struct.unpack(">%dH" % (len(loca_raw) // 2), loca_raw)]
    glyf = bytes(tab["glyf"])
    kept = [g for g in range(ng) if g not in drop]
    new_glyf = bytearray(); new_locs = []
    for g in kept:
        new_locs.append(len(new_glyf))
        new_glyf += glyf[locs[g]:locs[g + 1]]
    new_locs.append(len(new_glyf))
    tab["glyf"] = bytearray(new_glyf)
    tab["loca"] = bytearray(struct.pack(">%dI" % len(new_locs), *new_locs))
    nh = struct.unpack(">H", hhea[34:36])[0]
    if nh == ng:  # one hmtx record per glyph
        hm = bytes(tab["hmtx"])
        tab["hmtx"] = bytearray(b"".join(hm[g * 4:g * 4 + 4] for g in kept))
        struct.pack_into(">H", hhea, 34, len(kept))
    struct.pack_into(">H", maxp, 4, len(kept))
    struct.pack_into(">I", head, 8, 0)  # checkSumAdjustment zeroed for calc
    for e in entries:  # refresh checksums of (possibly) changed tables
        e[1] = _sfnt_sum(bytes(tab[e[0]]))
    out = bytearray(ttf[:12 + 16 * n])
    phys = sorted(entries, key=lambda e: e[2])
    pos = 12 + 16 * n
    for e in phys:
        data = bytes(tab[e[0]])
        e[2] = pos
        if len(out) < pos:
            out += b"\0" * (pos - len(out))
        out += data
        pos += (len(data) + 3) & ~3
        out += b"\0" * (pos - len(out))
    for i, e in enumerate(entries):  # directory, original order
        struct.pack_into(">III", out, 12 + i * 16 + 4, e[1], e[2], len(tab[e[0]]))
    head_off = next(e[2] for e in entries if e[0] == "head")
    adj = (0xB1B0AFBA - _sfnt_sum(bytes(out))) & 0xFFFFFFFF
    struct.pack_into(">I", out, head_off + 8, adj)
    return bytes(out)

def _stream_body(dec: bytes, extra: bytes = b"") -> bytes:
    comp = _deflate(dec)
    return (b"<<\n/Length %d\n/Filter /FlateDecode\n" % len(comp) + extra + b">>\nstream\r\n"
            + comp + b"\r\nendstream")

def _font_replacements(tpl: _Tpl, used: set) -> dict:
    """Re-subset the font to exactly the glyphs used by the new text.

    Verified 24/24 (168/168 objects) byte-for-byte identical against all
    genuine VTB PDFs when built from the harvested 144-CID catalog."""
    if used == getattr(tpl, "donor_cids", set()):
        return {}
    if _CAT_BLOB is not None:
        catalog = _CAT_BLOB["catalog"]
        base_tables = _CAT_BLOB["base_tables"]
        cids = sorted(c for c in used if c > 0)
        cid2gid = {cid: i + 1 for i, cid in enumerate(cids)}
        num = sum(gid ^ cid for cid, gid in cid2gid.items())
        tag = _subset_tag(num)

        o9 = (f"<<\n/Type /Font\n/BaseFont /{tag}+SFProDisplay-Regular\n"
              f"/Subtype /Type0\n/Encoding /Identity-H\n/DescendantFonts [11 0 R]\n"
              f"/ToUnicode 12 0 R\n>>").encode("latin-1")

        o15 = (f"<<\n/Type /FontDescriptor\n/FontName /{tag}+SFProDisplay-Regular\n"
               f"/Flags 4\n/FontWeight 400.0\n/ItalicAngle 0.0\n"
               f"/FontBBox [-266.11328 -241.21094 1251.9531 1074.2188]\n"
               f"/Ascent 952.14844\n/Descent -241.21094\n/CapHeight 704.58984\n"
               f"/XHeight 507.8125\n/StemV 197.34863\n/FontFile2 17 0 R\n"
               f"/CIDSet 18 0 R\n>>").encode("latin-1")

        wgroups = []
        for cid in cids:
            w = catalog[cid]["width"]
            if w == 1000:
                # PDFBox omits /W entries equal to the CIDFont default width
                # (1000, /DW absent).  Attested on 5 genuine Armenia-transfer
                # receipts: cid 754 (U+058F, adv 2048) is in the font,
                # CIDToGIDMap and ToUnicode but absent from /W.
                continue
            if wgroups and cid == wgroups[-1][1] + 1:
                wgroups[-1][1] = cid
                wgroups[-1][2].append(w)
            else:
                wgroups.append([cid, cid, [w]])
        w_parts = []
        for g_idx, (st, _, ws) in enumerate(wgroups):
            inner = ""
            for i, w in enumerate(ws):
                if i and i % 10 == 0:
                    inner += "\n"
                elif i:
                    inner += " "
                inner += str(w)
            prefix = "" if g_idx % 5 == 0 else " "
            w_parts.append(f"{prefix}{st} [{inner}]")
        wtxt = "/W [" + "\n".join(w_parts) + "\n]"
        o11 = (f"<<\n/Type /Font\n/Subtype /CIDFontType2\n"
               f"/BaseFont /{tag}+SFProDisplay-Regular\n/CIDSystemInfo 14 0 R\n"
               f"/FontDescriptor 15 0 R\n{wtxt}\n/CIDToGIDMap 16 0 R\n>>").encode("latin-1")

        tu_ranges = []
        for cid in cids:
            uni = ord(catalog[cid]["char"])
            if (tu_ranges and cid < 256 and uni < 256
                    and tu_ranges[-1][1] < 256 and tu_ranges[-1][2] + (tu_ranges[-1][1] - tu_ranges[-1][0]) < 256
                    and cid == tu_ranges[-1][1] + 1
                    and uni == tu_ranges[-1][2] + (tu_ranges[-1][1] - tu_ranges[-1][0]) + 1):
                tu_ranges[-1][1] = cid
            else:
                tu_ranges.append([cid, cid, uni])
        tu_lines = "".join(f"<{lo:04X}> <{hi:04X}> <{st:04X}>\n" for lo, hi, st in tu_ranges)
        tu_str = (
            "/CIDInit /ProcSet findresource begin\n"
            "12 dict begin\n\n"
            "begincmap\n"
            "/CIDSystemInfo\n"
            "<< /Registry (Adobe)\n"
            "/Ordering (UCS)\n"
            "/Supplement 0\n"
            ">> def\n\n"
            "/CMapName /Adobe-Identity-UCS def\n"
            "/CMapType 2 def\n\n"
            "1 begincodespacerange\n"
            "<0000> <FFFF>\n"
            "endcodespacerange\n\n"
            f"{len(tu_ranges)} beginbfrange\n"
            f"{tu_lines}"
            "endbfrange\n\n"
            "endcmap\n"
            "CMapName currentdict /CMap defineresource pop\n"
            "end\n"
            "end\n"
        )
        o12 = _stream_body(tu_str.encode("latin-1"))

        max_cid = max(cids)
        c2g = bytearray((max_cid + 1) * 2)
        for cid, gid in cid2gid.items():
            struct.pack_into(">H", c2g, cid * 2, gid)
        o16 = _stream_body(bytes(c2g))

        all_gids_cids = [0] + cids
        ng = len(all_gids_cids)
        new_glyf = bytearray()
        new_locs = []
        new_hmtx = bytearray()
        for cid in all_gids_cids:
            new_locs.append(len(new_glyf))
            new_glyf += catalog[cid]["glyf"]
            new_hmtx += catalog[cid]["hmtx"]
        new_locs.append(len(new_glyf))
        loca_b = struct.pack(f">{ng+1}I", *new_locs)
        head_b = bytearray(base_tables["head"])
        struct.pack_into(">I", head_b, 8, 0)
        hhea_b = bytearray(base_tables["hhea"])
        struct.pack_into(">H", hhea_b, 34, ng)
        maxp_b = bytearray(base_tables["maxp"])
        struct.pack_into(">H", maxp_b, 4, ng)
        gasp_b = base_tables["gasp"]

        tab_order = [
            ("gasp", bytes(gasp_b)),
            ("glyf", bytes(new_glyf)),
            ("head", bytes(head_b)),
            ("hhea", bytes(hhea_b)),
            ("hmtx", bytes(new_hmtx)),
            ("loca", bytes(loca_b)),
            ("maxp", bytes(maxp_b)),
        ]
        nt = 7
        ttf_out = bytearray(struct.pack(">IHHHH", 0x00010000, nt, 64, 2, 48) + b"\0" * (16 * nt))
        pos = 12 + 16 * nt
        head_off = 0
        for i, (tname, tbytes) in enumerate(tab_order):
            ck = _sfnt_sum(tbytes)
            ln = len(tbytes)
            if tname == "head":
                head_off = pos
            struct.pack_into(">4sIII", ttf_out, 12 + i * 16, tname.encode("latin-1"), ck, pos, ln)
            ttf_out += tbytes
            pad = (4 - (ln % 4)) % 4
            if pad:
                ttf_out += b"\0" * pad
            pos += ln + pad
        adj = (0xB1B0AFBA - _sfnt_sum(bytes(ttf_out))) & 0xFFFFFFFF
        struct.pack_into(">I", ttf_out, head_off + 8, adj)
        o17 = _stream_body(bytes(ttf_out), b"/Length1 %d\n" % len(ttf_out))
        return {9: o9, 11: o11, 12: o12, 15: o15, 16: o16, 17: o17}

    unused = sorted(set(tpl.cid2char) - set(used))
    if not unused:
        return {}
    drop_gids = sorted(tpl.cid2gid[c] for c in unused)
    kept_gids = sorted(set(tpl.cid2gid.values()) - set(drop_gids))
    old2new = {g: i + 1 for i, g in enumerate(kept_gids)}
    repl = {}
    # obj16 CIDToGIDMap
    c2g = bytearray(tpl.cid2gid_raw)
    for cid in range(len(c2g) // 2):
        g = struct.unpack(">H", c2g[cid * 2:cid * 2 + 2])[0]
        struct.pack_into(">H", c2g, cid * 2, old2new.get(g, 0) if g else 0)
    repl[16] = _stream_body(bytes(c2g))
    # obj12 ToUnicode: surgically drop unused CIDs from the donor's own
    # bfrange lines (trim/split ranges, keep every other line byte-exact)
    unused_set = set(unused)
    rng = []
    for lo, hi, st in re.findall(r"<([0-9A-F]{4})> <([0-9A-F]{4})> <([0-9A-F]{4})>",
                                 tpl.tounicode_text):
        lo, hi, st = int(lo, 16), int(hi, 16), int(st, 16)
        cur = None
        for cid in range(lo, hi + 1):
            if cid in unused_set:
                if cur:
                    rng.append(cur); cur = None
                continue
            if cur and cid == cur[1] + 1:
                cur[1] = cid
            else:
                if cur:
                    rng.append(cur)
                cur = [cid, cid, st + (cid - lo)]
        if cur:
            rng.append(cur)
    lines = "".join(f"<{lo:04X}> <{hi:04X}> <{st:04X}>\n" for lo, hi, st in rng)
    tu = re.sub(r"\d+ beginbfrange\n.*?endbfrange",
                f"{len(rng)} beginbfrange\n{lines}endbfrange",
                tpl.tounicode_text, flags=re.S)
    repl[12] = _stream_body(tu.encode("latin-1"))
    # obj11 /W (regroup remaining widths)
    remw = sorted((c, tpl.W[c]) for c in tpl.W if c not in set(unused))
    wgroups = []
    for cid, w in remw:
        if wgroups and cid == wgroups[-1][1] + 1:
            wgroups[-1][1] = cid; wgroups[-1][2].append(w)
        else:
            wgroups.append([cid, cid, [w]])
    def fmt_group(st, ws):
        inner = ""
        for i, w in enumerate(ws):
            if i and i % 10 == 0:
                inner += "\n"
            elif i:
                inner += " "
            inner += str(w)
        return f"{st} [{inner}]"
    wtxt = "/W [" + "\n ".join(fmt_group(s, ws) for s, _, ws in wgroups) + "\n]"
    body11 = _obj_bytes(tpl.raw, 11).decode("latin-1")
    body11 = re.sub(r"/W \[.*?\n\]", wtxt, body11, flags=re.S)
    repl[11] = body11.encode("latin-1")
    # obj17 FontFile2
    new_ttf = _ttf_drop_glyphs(tpl.ttf, set(drop_gids))
    repl[17] = _stream_body(new_ttf, b"/Length1 %d\n" % len(new_ttf))
    # subset tag must match the NEW glyph inventory (checker recomputes it):
    # num = sum over kept CIDs of (newGID XOR CID) — PDFBox gidToCid hashCode
    num = 0
    for cid in tpl.cid2char:
        if cid in unused_set:
            continue
        g_old = tpl.cid2gid.get(cid)
        if g_old is None:
            continue
        num += old2new[g_old] ^ cid
    new_tag = _subset_tag(num).encode()
    old_tag = re.search(rb"/BaseFont /([A-Z]{6})\+", tpl.raw).group(1)
    if new_tag != old_tag:
        for n in (9, 11, 15):
            body = repl.get(n) or _obj_bytes(tpl.raw, n)
            repl[n] = body.replace(b"/" + old_tag + b"+", b"/" + new_tag + b"+")
    return repl

def _assemble(tpl: _Tpl, content: str, op_dt: datetime, meta_out, info,
              lag_s: int = None, page_h: float = None,
              created_dt: datetime = None, doc_id: str = None):
    raw = tpl.raw
    # object bodies by fitz xrefs: content=5, info=3 (donor layout)
    comp = _deflate(content.encode("latin-1"))
    # 28/28 immediate genuine VTB receipts (incl. both 07.11): CreationDate
    # is +5..+24s after the operation time (== ID_time+1s, i.e. +6..+25s after
    # ID_time) and stays in the SAME minute as the displayed operation time.
    # lag_s may be threaded in by generate() so an immediate receipt's
    # CreationDate equals the actual generation instant.  created_dt pins the
    # instant outright (byte-replica of a known genuine receipt).
    if created_dt is None:
        if lag_s is None:
            max_lag = min(24, 59 - op_dt.second)
            lag_s = _rnd.randint(5, max_lag) if max_lag >= 5 else max(1, 59 - op_dt.second)
        created_dt = op_dt.replace(microsecond=0) + timedelta(seconds=lag_s)
    else:
        created_dt = created_dt.replace(microsecond=0)
    created = created_dt.strftime("D:%Y%m%d%H%M%S+03'00'")
    repl = {}
    repl[5] = ("<<\n/Length %d\n/Filter /FlateDecode\n>>\nstream\r\n" % len(comp)).encode() \
        + comp + b"\r\nendstream"
    repl[3] = re.sub(rb"/CreationDate \(D:[^)]*\)",
                     f"/CreationDate ({created})".encode(),
                     _obj_bytes(raw, 3))
    if page_h is not None and abs(page_h - tpl.page_h) > 1e-9:
        repl[4] = re.sub(rb"/MediaBox \[0\.0 0\.0 281\.2125 [\d.]+\]",
                         f"/MediaBox [0.0 0.0 281.2125 {fmt5(page_h)}]".encode(),
                         _obj_bytes(raw, 4))
    repl.update(_font_replacements(tpl, set(tpl._block_cids(content))))
    pdf = _rebuild(raw, repl, doc_id=(doc_id if doc_id is not None
                                      else _trailer_id(raw, created, created_dt)))
    if meta_out is not None:
        meta_out.update(info)
        meta_out["dt"] = op_dt
        meta_out["created_dt"] = created_dt
    return pdf

def _obj_bytes(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^%d 0 obj\r?\n" % num, raw)
    start = m.end()
    end = raw.index(b"endobj", start)
    return raw[start:end].rstrip(b"\r\n")

def _obj_segment(raw: bytes, num: int) -> bytes:
    m = re.search(rb"(?m)^%d 0 obj\r?\n" % num, raw)
    start = m.start()
    end = raw.index(b"endobj", start) + len(b"endobj")
    return raw[start:end]

def _anti_py_zlib_pass(pdf: bytes) -> bytes:
    """Re-compress every FlateDecode stream in the assembled PDF with
    genuine Java Deflater DEFAULT level 6.  For donor-genuine streams this
    is byte-identical to the original, so the `new == data` branch below
    leaves them untouched.  Name kept for callers.  Each FlateDecode object
    is rebuilt wholesale (new /Length + new stream body) and spliced back,
    last-to-first so earlier offsets stay valid."""
    import zlib
    out = bytearray(pdf)
    # collect (object_start, object_end, new_object_bytes) for every
    # FlateDecode stream that needs rewriting.
    edits = []
    for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", pdf):
        n = int(m.group(1))
        o_start = m.start()
        o_end = pdf.find(b"endobj", m.end())
        if o_end < 0:
            continue
        o_end += len(b"endobj")
        body = pdf[m.end():o_end - len(b"endobj")]
        if b"/FlateDecode" not in body:
            continue
        si = body.find(b"stream")
        if si < 0:
            continue
        s = si + 6
        if body[s:s+2] == b"\r\n":
            s += 2
        elif body[s:s+1] == b"\n":
            s += 1
        e = body.rfind(b"endstream")
        if e < 0:
            continue
        data = body[s:e]
        if data.endswith(b"\n"):
            data = data[:-1]
        if data.endswith(b"\r"):
            data = data[:-1]
        try:
            raw = zlib.decompress(data)
        except Exception:
            continue
        # Use the full fallback chain (JZCompFS -> JZComp -> JZComp CLI),
        # not _deflate_anti_py directly: without JZCompFS.class the old
        # code killed every VTB generation even though the other Java
        # deflate paths were available and working.
        new = _deflate(raw)
        if new == data:
            continue
        # rebuild the object: header dict (with new /Length) + stream + endobj
        hdr = body[:si]
        # detect the EOL between 'stream' and the data
        if body[si + 6:si + 8] == b"\r\n":
            stream_eol = b"\r\n"
        elif body[si + 6:si + 7] == b"\n":
            stream_eol = b"\n"
        else:
            stream_eol = b"\r\n"
        # detect EOL before 'endstream'
        if body[e-2:e] == b"\r\n":
            end_eol = b"\r\n"
        elif body[e-1:e] == b"\n":
            end_eol = b"\n"
        else:
            end_eol = b"\r\n"
        # rewrite /Length in the header
        new_hdr = re.sub(rb"/Length\s+\d+", b"/Length " + str(len(new)).encode(), hdr, count=1)
        new_obj = (f"{n} 0 obj\n".encode() + new_hdr + b"stream" + stream_eol
                   + new + end_eol + b"endstream\nendobj\n")
        edits.append((o_start, o_end, new_obj))
    for o_start, o_end, new_obj in reversed(edits):
        out[o_start:o_end] = new_obj
    return bytes(out)

def _rebuild(raw: bytes, repl: dict, doc_id: str = None) -> bytes:
    order = [int(m.group(1)) for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", raw)]
    # header = everything before the first object
    first = re.search(rb"(?m)^\d+ 0 obj\r?\n", raw).start()
    pdf = bytearray(raw[:first])
    for num in order:
        if num in repl:
            pdf += f"{num} 0 obj\n".encode() + repl[num] + b"\nendobj\n"
        else:
            pdf += _obj_segment(raw, num) + b"\n"
    # Genuine-deflate pass: re-compress EVERY FlateDecode stream (including
    # donor-kept ones like the logo form XObject and CIDSet) with Java
    # Deflater DEFAULT level 6 — the genuine openhtmltopdf dialect.
    pdf = _anti_py_zlib_pass(bytes(pdf))
    # xref offsets MUST be measured on the post-pass bytes: the pass
    # re-compresses streams (donor logo obj8: 840 -> 835 B), shifting
    # every later object; offsets taken before it were 4 B too large
    # for obj9..18 (MuPDF: "syntax error: expected object number").
    offsets = {int(m.group(1)): m.start()
               for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?\n", pdf)}
    xref_pos = len(pdf)
    size = max(order) + 1
    pdf += b"xref\n" + f"0 {size}\n".encode()
    pdf += b"0000000000 65535 f\r\n"
    for num in range(1, size):
        pdf += f"{offsets.get(num, 0):010d} 00000 n\r\n".encode()
    if doc_id is None:
        doc_id = _rnd.randbytes(16).hex().upper()
    pdf += ("trailer\n<<\n/Root 1 0 R\n/Info 3 0 R\n/ID [<{}> <{}>]\n/Size {}\n>>\n"
            "startxref\n{}\n%%EOF\n".format(doc_id, doc_id, size, xref_pos)).encode()
    return bytes(pdf)
