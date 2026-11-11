# -*- coding: utf-8 -*-
r"""Build C:\Users\nemor\OneDrive\Desktop\mts_check_15\receipt_8947.pdf:
Fixes the exact bug that caused 'обнаружил подмену текста' on >=1000 RUB amounts:
- Uses genuine MTS Bank non-breaking space U+00A0 (CID 97 = 0x0061, width 200) as thousands separator
  in '8\xa0947,00 ₽' (matching genuine file(10).pdf '1\xa0150,00 ₽' and Чек(4).pdf '1\xa0023\xa0₽').
- Fixes the bug from mts_check_10 where F2 ToUnicode (56 0 obj) had '56 beginbfrange' not updated to '57 beginbfrange'!
- Changes the last digit of Debit Account ('40914 81070 00021 29508' -> '40914 81070 00021 29509')
  with exact 355.00pt right-edge alignment (Tm X = 210.28) and synced ObjStm /Names & /XYZ.
- Uses fresh, strictly valid SBP ID (...0W0...) and Transaction code (01920994XXXX0).
"""
import os, sys, re, zlib, struct, subprocess, threading
sys.stdout.reconfigure(encoding="utf-8")

DONOR = r"C:\Users\nemor\OneDrive\Desktop\mtsdonornew\file (18).pdf"
F16 = r"C:\Users\nemor\OneDrive\Desktop\mtsdonornew\file (16).pdf"
F17 = r"C:\Users\nemor\OneDrive\Desktop\mtsdonornew\file (17).pdf"
OUTDIR = r"C:\Users\nemor\OneDrive\Desktop\mts_check_15"
OUT = os.path.join(OUTDIR, "receipt_8947.pdf")
def _find_java():
    """Java executable: JAVA_BIN env, known local JDK17, then PATH."""
    import shutil
    for j in (os.environ.get("JAVA_BIN"),
              r"C:\Users\nemor\AppData\Local\Programs\Microsoft\jdk-17.0.17.10-hotspot\bin\java.exe",
              shutil.which("java")):
        if j and (os.path.exists(j) or shutil.which(j)):
            return j
    return None

def _find_jzdir():
    """Directory holding JZComp.class. The bank's stack compresses with Java
    Deflater; Python zlib emits a different deflate dialect, so jcomp MUST
    locate the class — there is no zlib fallback by design."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("JZCOMP_DIR"),
              here,
              os.getcwd(),
              os.path.join(here, "wb_project"),
              os.path.join(here, "retranslator-mts", "pdf-service"),
              os.path.join(os.path.dirname(here), "wb_project"),
              "/opt/mts-bot",
              TMP):
        if d and os.path.exists(os.path.join(d, "JZComp.class")):
            return d
    return None

JAVA = _find_java()
TMP = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"
_JZDIR = _find_jzdir()

# ───────────── persistent JZCompServer (same bytes, no per-call JVM spawn) ─────────────
# Every deflate here must come out of java.util.zip.Deflater.  Starting a JVM
# per stream costs ~80-400 ms on Windows and dominated receipt generation
# (WB: 11 streams/receipt).  JZCompServer runs one JVM per Python process and
# serves length-prefixed requests; each request deflates with a fresh
# `new Deflater(level)` written in a single DeflaterOutputStream.write()
# followed by close() — the exact sequence the JZComp CLI performs, so the
# emitted bytes are identical.  Any server hiccup falls back to the CLI
# subprocess path below (still Java Deflater, still the same bytes).
_JZSRV = None
_JZSRV_LOCK = threading.RLock()

def _read_exact(fh, n):
    out = bytearray()
    while len(out) < n:
        chunk = fh.read(n - len(out))
        if not chunk:
            raise EOFError("JZCompServer closed stdout")
        out += chunk
    return bytes(out)

def _find_javac():
    """javac for auto-building JZCompServer.class: JAVA_BIN sibling, then PATH."""
    import shutil
    if JAVA:
        sib = os.path.join(os.path.dirname(JAVA), "javac.exe" if JAVA.lower().endswith(".exe") else "javac")
        if os.path.exists(sib):
            return sib
    return shutil.which("javac")

def _ensure_server_class(jzdir):
    """True if JZCompServer.class is usable in jzdir (compiled on demand)."""
    cls = os.path.join(jzdir, "JZCompServer.class")
    if os.path.exists(cls):
        return True
    src = os.path.join(jzdir, "JZCompServer.java")
    javac = _find_javac()
    if not (os.path.exists(src) and javac):
        return False
    try:
        subprocess.check_call([javac, "-d", jzdir, src],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return os.path.exists(cls)

def _jz_server_start(jzdir):
    proc = subprocess.Popen([JAVA, "-cp", jzdir, "JZCompServer"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        n = struct.unpack(">I", _read_exact(proc.stdout, 4))[0]
        if n != 4 or _read_exact(proc.stdout, 4) != b"JZOK":
            raise RuntimeError("JZCompServer handshake failed")
    except Exception:
        try: proc.kill()
        except Exception: pass
        raise
    return proc

def _jcomp_via_server(data, level=6):
    """Deflate through the persistent daemon; raises (caller falls back to CLI)."""
    jzdir = _JZDIR or _find_jzdir()
    if jzdir is None:
        raise RuntimeError("JZComp.class not found for server mode")
    if JAVA is None:
        raise RuntimeError("java executable not found for server mode")
    if not _ensure_server_class(jzdir):
        raise RuntimeError("JZCompServer.class unavailable")
    global _JZSRV
    with _JZSRV_LOCK:
        if _JZSRV is None or _JZSRV.poll() is not None:
            _JZSRV = _jz_server_start(jzdir)
        try:
            _JZSRV.stdin.write(struct.pack(">II", len(data), level))
            _JZSRV.stdin.write(data)
            _JZSRV.stdin.flush()
            n = struct.unpack(">I", _read_exact(_JZSRV.stdout, 4))[0]
            return _read_exact(_JZSRV.stdout, n)
        except Exception:
            # Daemon died mid-request: drop it so the next call respawns; the
            # caller's CLI fallback still produces the required bytes.
            try: _JZSRV.kill()
            except Exception: pass
            _JZSRV = None
            raise

def jcomp(data, tag="tmp"):
    # Fast path: persistent JVM daemon (byte-identical to the CLI below).
    try:
        return _jcomp_via_server(data, 6)
    except Exception:
        pass
    # Unique temp files per call: fixed names race when generations run
    # concurrently (bot executor threads) and silently swap stream contents.
    jzdir = _find_jzdir()
    if jzdir is None:
        raise RuntimeError(
            "JZComp.class not found (searched JZCOMP_DIR, script dir, cwd, "
            "wb_project/, retranslator-mts/pdf-service/, /opt/mts-bot, TMP); "
            "Java Deflater is mandatory, no zlib fallback")
    if JAVA is None:
        raise RuntimeError("java executable not found (set JAVA_BIN); "
                           "Java Deflater is mandatory, no zlib fallback")
    import uuid
    suf = uuid.uuid4().hex[:12]
    inn = os.path.join(TMP, f"c15_{tag}_{suf}_in.bin")
    out = os.path.join(TMP, f"c15_{tag}_{suf}_out.bin")
    try:
        open(inn, "wb").write(data)
        subprocess.check_call([JAVA, "-cp", jzdir, "JZComp", inn, out], stdout=subprocess.DEVNULL)
        return open(out, "rb").read()
    finally:
        for p in (inn, out):
            try: os.unlink(p)
            except OSError: pass

def cksum(data):
    if len(data) % 4:
        data += b"\x00" * (4 - len(data) % 4)
    return sum(struct.unpack(f">{len(data)//4}I", data)) & 0xFFFFFFFF

def font_tables(ttf):
    n = struct.unpack(">H", ttf[4:6])[0]
    tabs = {}
    for i in range(n):
        o = 12 + i * 16
        tag = ttf[o:o+4]
        ck, off, ln = struct.unpack(">III", ttf[o+4:o+16])
        tabs[tag] = (ck, off, ln, o)
    return tabs

def get_glyph(ttf, gid):
    tabs = font_tables(ttf)
    loca_off = tabs[b"loca"][1]
    glyf_off = tabs[b"glyf"][1]
    hmtx_off = tabs[b"hmtx"][1]
    a = struct.unpack_from(">H", ttf, loca_off + gid * 2)[0] * 2
    b = struct.unpack_from(">H", ttf, loca_off + (gid + 1) * 2)[0] * 2
    return ttf[glyf_off + a : glyf_off + b], ttf[hmtx_off + gid * 4 : hmtx_off + gid * 4 + 4]

def smaller_font(path):
    blob = open(path, "rb").read()
    fonts = []
    for m in re.finditer(rb"(\d+) 0 obj\s*<<([^>]*?/Length1[^>]*?)>>\s*stream\r?\n", blob, re.S):
        L = int(re.search(rb"/Length\s+(\d+)", m.group(2)).group(1))
        fonts.append(zlib.decompress(blob[m.end():m.end()+L]))
    return min(fonts, key=len)

def build_f3_font(base, g4, g7):
    tabs = font_tables(base)
    loca_off = tabs[b"loca"][1]
    glyf_off = tabs[b"glyf"][1]
    maxp_off = tabs[b"maxp"][1]
    old_n = struct.unpack(">H", base[maxp_off + 4 : maxp_off + 6])[0]
    loca = [struct.unpack_from(">H", base, loca_off + g * 2)[0] * 2 for g in range(old_n + 1)]
    glyf = base[glyf_off : glyf_off + loca[-1]]

    out_glyf = bytearray()
    new_loca = []
    inj = {23: g4, 26: g7}
    for gid in range(old_n):
        new_loca.append(len(out_glyf))
        chunk = inj[gid] if gid in inj else glyf[loca[gid]:loca[gid + 1]]
        if len(chunk) % 2:
            chunk += b"\x00"
        out_glyf += chunk
    new_loca.append(len(out_glyf))
    loca_b = b"".join(struct.pack(">H", off // 2) for off in new_loca)

    head_b = base[tabs[b"head"][1] : tabs[b"head"][1] + 54]
    hhea_b = base[tabs[b"hhea"][1] : tabs[b"hhea"][1] + 36]
    hmtx_b = base[tabs[b"hmtx"][1] : tabs[b"hmtx"][1] + 1404]
    maxp_b = base[tabs[b"maxp"][1] : tabs[b"maxp"][1] + 32]

    entries = [
        (b"glyf", bytes(out_glyf), cksum(bytes(out_glyf))),
        (b"head", head_b, 0x15B3C133),
        (b"hhea", hhea_b, 0x0847051A),
        (b"hmtx", hmtx_b, 0xFB9D3E4B),
        (b"loca", loca_b, cksum(loca_b)),
        (b"maxp", maxp_b, 0x01A80052),
    ]
    pos = 108
    built = []
    for tag, blob, ck in entries:
        if pos % 4:
            pos += 4 - (pos % 4)
        built.append((tag, pos, blob, ck))
        pos += len(blob)

    font = bytearray(pos)
    font[:12] = base[:12]
    for i, (tag, off, blob, ck) in enumerate(built):
        o = 12 + i * 16
        font[o:o+4] = tag
        struct.pack_into(">III", font, o + 4, ck, off, len(blob))
        font[off:off+len(blob)] = blob
    return bytes(font)

CID = {
    " ": 3, "\xa0": 0x61, ",": 0x0F,
    "0": 0x13, "1": 0x14, "2": 0x15, "3": 0x16, "4": 0x17,
    "5": 0x18, "6": 0x19, "7": 0x1A, "8": 0x1B, "9": 0x1C,
    "A": 0x24, "G": 0x2A, "W": 0x3A, "₽": 0xF3,
}
def enc(s):
    return b"".join(CID[c].to_bytes(2, "big") for c in s)

W_F2 = {str(d): [642, 347, 541, 584, 598, 575, 600, 496, 626, 600][d] for d in range(10)}
def tw(s): return sum(W_F2[c] for c in s)

def parse_xref(blob):
    rows = []
    for i in range(len(blob) // 5):
        t = blob[i * 5]
        a = int.from_bytes(blob[i * 5 + 1 : i * 5 + 3], "big")
        b = int.from_bytes(blob[i * 5 + 3 : i * 5 + 5], "big")
        rows.append((t, a, b))
    return rows

def build_xref(rows):
    out = bytearray()
    for t, a, b in rows:
        out.append(t)
        out += int(a).to_bytes(2, "big")
        out += int(b).to_bytes(2, "big")
    return bytes(out)

def find_stream(data, objnum):
    m = re.search(f"{objnum} 0 obj\\n<<(.*?)>>stream\\n".encode(), data)
    hdr = m.group(1)
    lm = re.search(rb"/Length\s+(\d+)", hdr)
    L = int(lm.group(1))
    start = m.end()
    dec = zlib.decompress(bytes(data[start:start + L]))
    return {"num": objnum, "len": L, "start": start, "hdr_m": m, "dec": dec, "len_span": lm.span(1)}

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    data = bytearray(open(DONOR, "rb").read())
    cs = find_stream(data, 29)
    ob = find_stream(data, 31)
    f2_tu = find_stream(data, 56)
    f3_st = find_stream(data, 57)
    f3_tu = find_stream(data, 60)
    xr = find_stream(data, 65)
    rows = parse_xref(xr["dec"])

    g4, _ = get_glyph(smaller_font(F16), 23)
    g7, _ = get_glyph(smaller_font(F17), 26)
    f3_new = build_f3_font(f3_st["dec"], g4, g7)
    f3_comp = jcomp(f3_new, "f3")

    # 1. Update F2 ToUnicode (56 0 obj): 56 beginbfrange -> 57 beginbfrange, add <0061><0061><00a0>
    assert b"56 beginbfrange\n" in f2_tu["dec"]
    tu56_raw = f2_tu["dec"].replace(b"56 beginbfrange\n", b"57 beginbfrange\n", 1)
    anchor56 = b"<003a><003a><0057>\n"
    at56 = tu56_raw.find(anchor56) + len(anchor56)
    tu56_raw = tu56_raw[:at56] + b"<0061><0061><00a0>\n" + tu56_raw[at56:]
    # Verify count of lines between beginbfrange and endbfrange is 57!
    bfr56 = re.search(rb"57 beginbfrange\n(.*?)endbfrange", tu56_raw, re.S).group(1).strip().splitlines()
    assert len(bfr56) == 57
    f2_tu_comp = jcomp(tu56_raw, "tu56")

    # 2. Update F3 ToUnicode (60 0 obj): 10 beginbfrange -> 13 beginbfrange, add 4, 7, and 0x61 (nbsp)
    assert b"10 beginbfrange\n" in f3_tu["dec"]
    tu60_raw = f3_tu["dec"].replace(b"10 beginbfrange\n", b"13 beginbfrange\n", 1)
    anchor60_1 = b"<0013><0013><0030>\n"
    at60_1 = tu60_raw.find(anchor60_1) + len(anchor60_1)
    tu60_raw = tu60_raw[:at60_1] + b"<0017><0017><0034>\n<001a><001a><0037>\n" + tu60_raw[at60_1:]
    anchor60_2 = b"<001c><001c><0039>\n"
    at60_2 = tu60_raw.find(anchor60_2) + len(anchor60_2)
    tu60_raw = tu60_raw[:at60_2] + b"<0061><0061><00a0>\n" + tu60_raw[at60_2:]
    bfr60 = re.search(rb"13 beginbfrange\n(.*?)endbfrange", tu60_raw, re.S).group(1).strip().splitlines()
    assert len(bfr60) == 13
    f3_tu_comp = jcomp(tu60_raw, "tu60")

    # 3. Clean pools for SBP ID (...0W0) and Transaction code (01920994XXXX0)
    w7 = tw("3861300")
    w12 = tw("019209948490")
    used_s7 = {"3861300", "3189300", "3003310", "3380610", "3330010", "3013300", "3031300", "3033100", "3136800", "3368100"}
    used_s12 = {"019209948490", "019209948460", "019209949840", "019209948640", "019209944980", "019209946840", "019209948940"}

    valid_s7 = []
    for ss in range(25, 43):
        ss_s = f"{ss:02d}"
        if "00" in ss_s: continue
        for d2 in range(1, 10):
            for d3 in range(1, 10):
                for d4 in range(1, 10):
                    cand = f"{ss_s}{d2}{d3}{d4}00"
                    if tw(cand) == w7 and cand not in used_s7:
                        valid_s7.append(cand)

    valid_s12 = []
    for x in range(100000):
        s5 = f"{x:05d}"
        if "00" in s5: continue
        cand = f"019209{s5}0"
        if tw(cand) == w12 and cand not in used_s12:
            valid_s12.append(cand)
    valid_s12.sort(key=lambda t: (0 if t.startswith("01920994") else 1, abs(int(t) - 19209948490)))

    old_acc_cid = enc("40914 81070 00021 29508")
    new_acc_cid = enc("40914 81070 00021 29509")
    old_amt_cid = enc("89,00 ₽")
    new_amt_cid = enc("8\xa0947,00 ₽")

    for s7 in valid_s7:
        for s12 in valid_s12[:20]:
            # Content stream (29 0 obj)
            p = bytearray(cs["dec"])
            p = bytearray(
                bytes(p)
                .replace(old_acc_cid, new_acc_cid, 1)
                .replace(b"1 0 0 1 209.97 469.35 Tm", b"1 0 0 1 210.28 469.35 Tm", 1)
                .replace(old_amt_cid, new_amt_cid)
                .replace(b"1 0 0 1 311.72 437.35 Tm", b"1 0 0 1 296.19 437.35 Tm", 1)
                .replace(b"1 0 0 1 280.08 260.58 Tm", b"1 0 0 1 253.22 260.58 Tm", 1)
            )
            aa = p.find(b"\x00\x24")
            for ch, off in zip(s7, (9, 10, 11, 12, 13, 14, 16)):
                p[aa + off * 2] = 0
                p[aa + off * 2 + 1] = 0x13 + int(ch)
            tpos = p.find(enc("019209948490"))
            p[tpos:tpos+24] = enc(s12)
            content_comp = jcomp(bytes(p), "cs")

            # ObjStm (31 0 obj)
            first = 228
            orig_dec = ob["dec"]
            full_sbp = b"A63000612386130W0G10000011871001"
            chars = bytearray(full_sbp)
            for ch, off in zip(s7, (9, 10, 11, 12, 13, 14, 16)):
                chars[off] = ord(ch)

            q = bytearray(
                orig_dec
                .replace(b"[1 0 R/XYZ 209.97 481.35 0]", b"[1 0 R/XYZ 210.28 481.35 0]", 1)
                .replace(b"[1 0 R/XYZ 311.72 449.35 0]", b"[1 0 R/XYZ 296.19 449.35 0]", 1)
                .replace(b"(40914 81070 00021 29508)", b"(40914 81070 00021 29509)", 1)
                .replace(full_sbp, bytes(chars), 1)
                .replace(b"019209948490", s12.encode(), 1)
            )
            assert len(q) == len(orig_dec)

            w2_needle = b"58[901]137["
            w2_insert = b"58[901]97[200]137["
            w3_needle = b"19[655]27[645 618]"
            w3_insert = b"19[655]23[631]26[512 645 618]97[200]"
            old_name_b = b"89,00 \xbd"
            new_name_b = b"8\xa0947,00 \xbd"

            grew = [
                (orig_dec.find(w2_needle), len(w2_insert) - len(w2_needle)),
                (orig_dec.find(w3_needle), len(w3_insert) - len(w3_needle)),
                (orig_dec.find(old_name_b), len(new_name_b) - len(old_name_b)),
            ]
            q = bytearray(
                bytes(q)
                .replace(old_name_b, new_name_b, 1)
                .replace(w3_needle, w3_insert, 1)
                .replace(w2_needle, w2_insert, 1)
            )

            hdr_toks = [x for x in orig_dec[:first].decode("latin1").split(" ") if x]
            pairs = [(int(hdr_toks[i]), int(hdr_toks[i+1])) for i in range(0, len(hdr_toks), 2)]
            fixed = []
            for onum, off in pairs:
                abs_start = first + off
                add = sum(delta for thr, delta in grew if abs_start > thr)
                fixed.append((onum, off + add))
            rebuilt_hdr = (" ".join(f"{a} {b}" for a, b in fixed) + " ").encode("latin1")
            assert len(rebuilt_hdr) == first
            q[:first] = rebuilt_hdr
            os_comp = jcomp(bytes(q), "os")

            out = bytearray(data)
            m1 = re.search(rb"/Length1\s+(\d+)", f3_st["hdr_m"].group(1))
            p1 = f3_st["hdr_m"].start(1) + m1.start(1)
            out[p1:p1+4] = str(len(f3_new)).encode()

            stream_edits = [
                (cs, content_comp),
                (f2_tu, f2_tu_comp),
                (f3_st, f3_comp),
                (f3_tu, f3_tu_comp),
                (ob, os_comp),
            ]
            for info, comp in sorted(stream_edits, key=lambda x: x[0]["start"], reverse=True):
                hs = info["hdr_m"].start(1)
                a, b_sp = info["len_span"]
                out[hs+a : hs+b_sp] = str(len(comp)).encode()
                out = out[:info["start"]] + comp + out[info["start"]+info["len"]:]

            found = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", out)}
            new_rows = [(1, found[num], b_val) if t == 1 else (t, a_val, b_val) for num, (t, a_val, b_val) in enumerate(rows)]
            xref_new = jcomp(build_xref(new_rows), "xr")
            if len(xref_new) == 194:
                m65 = re.search(rb"65 0 obj\n<<(.*?)>>stream\n", out)
                hdr65 = re.sub(rb"/Length\s+\d+", b"/Length 194", m65.group(1), count=1)
                prefix = bytes(out[m65.start():m65.start(1)])
                new_tail = prefix + hdr65 + b">>stream\n" + xref_new + b"\nendstream\nendobj\nstartxref\n" + str(found[65]).encode() + b"\n%%EOF\n"
                out = out[:m65.start()] + new_tail
                open(OUT, "wb").write(out)
                print(f"SAVED {OUT} ({len(out)}B) SBP={bytes(chars).decode()} Trans={s12} L65={len(xref_new)}")
                return

if __name__ == "__main__":
    main()
