#!/usr/bin/env python3
"""
Patch Справка о движении средств-2026-05-18 21_58_12.pdf
Changes: +3 000.00 → +30.00, Пополнения 3 000,00 → 30,00
"""
import re, zlib

SRC = "/Users/aleksandrzerebatav/Downloads/Справка о движении средств-2026-05-18 21_58_12.pdf"
DST = "/Users/aleksandrzerebatav/Downloads/Справка о движении средств-2026-05-18 21_58_12_patched.pdf"

UNI_TO_CID = {
    0x0020: 0x0003, 0x002B: 0x0186, 0x002C: 0x0157,
    0x002D: 0x016B, 0x002E: 0x0156,
    **{0x30 + i: 0x0131 + i for i in range(10)},
}
def encode(t): return b"".join(UNI_TO_CID[ord(c)].to_bytes(2, "big") for c in t)

# Amount in transaction (Tj format, both x=199 and x=294 columns)
AMT_OLD = b"(" + encode("+3 000.00 ") + b")Tj"
AMT_NEW = b"(" + encode("+30.00 ")    + b")Tj"

# Total in Пополнения row (TJ array format)
TOT_OLD = b"[(" + encode("3 000,00 ") + b")]TJ"
TOT_NEW = b"[(" + encode("30,00 ")    + b")]TJ"

# ── PDF helpers ──────────────────────────────────────────────────────────────
def get_xref_pos(data: bytes) -> int:
    return int(re.search(rb"startxref\s+(\d+)\s+%%EOF", data).group(1))

def get_obj_offset(data: bytes, xref_pos: int, obj_num: int) -> int:
    block = data[xref_pos:]
    lines = block.split(b"\n"); idx = 1
    while idx < len(lines):
        hm = re.match(rb"(\d+)\s+(\d+)", lines[idx].strip())
        if hm:
            start = int(hm.group(1)); count = int(hm.group(2)); idx += 1
            for i in range(count):
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", lines[idx+i].strip()) if idx+i<len(lines) else None
                if em and start+i == obj_num: return int(em.group(1))
            idx += count
        else: idx += 1
    raise ValueError(f"obj {obj_num} not found")

def decompress_stream(data: bytes, obj_num: int, xref_pos: int) -> bytes:
    off = get_obj_offset(data, xref_pos, obj_num)
    obj = data[off:]
    sm = re.search(rb"stream\r?\n", obj)
    em = re.search(rb"\nendstream", obj)
    raw = obj[sm.end():em.start()]
    return zlib.decompress(raw) if b"FlateDecode" in obj[:sm.start()] else raw

def rebuild_stream(data: bytearray, obj_num: int, xref_pos: int,
                   new_plain: bytes) -> tuple[bytearray, int]:
    off = get_obj_offset(bytes(data), xref_pos, obj_num)
    obj = bytes(data[off:])
    sm = re.search(rb"stream\r?\n", obj)
    em = re.search(rb"\nendstream", obj)
    old_len = em.start() - sm.end()
    new_comp = zlib.compress(new_plain, 9)
    delta = len(new_comp) - old_len
    hdr_new = re.sub(rb"/Length\s+\d+", b"/Length " + str(len(new_comp)).encode(), obj[:sm.start()])
    abs_end = off + em.start()
    data = bytearray(bytes(data[:off]) + hdr_new + b"stream\n" + new_comp + bytes(data[abs_end:]))
    return data, delta

def shift_xref(data: bytearray, xref_pos: int, above: int, delta: int) -> bytearray:
    block = bytes(data[xref_pos:]); lines = block.split(b"\n")
    out = []; idx = 0; cur_start = 0
    while idx < len(lines):
        ln = lines[idx]
        hm = re.match(rb"(\d+)\s+(\d+)\s*$", ln.strip())
        if hm:
            cur_start = int(hm.group(1)); cnt = int(hm.group(2))
            out.append(ln); idx += 1
            for i in range(cnt):
                if idx >= len(lines): break
                el = lines[idx]; idx += 1
                em = re.match(rb"(\d{10})\s+(\d{5})\s+([fn])", el.strip())
                if em and em.group(3) != b"f" and int(em.group(1)) > above:
                    out.append(f"{int(em.group(1))+delta:010d} {em.group(2).decode()} {em.group(3).decode()} ".encode())
                else:
                    out.append(el)
        else:
            out.append(ln); idx += 1
    return bytearray(bytes(data[:xref_pos]) + b"\n".join(out))

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    import fitz, os

    with open(SRC, "rb") as f:
        data = bytearray(f.read())

    xref_pos = get_xref_pos(bytes(data))
    doc = fitz.open(SRC)
    p1_xref = doc[0].get_contents()[0]
    doc.close()

    obj_off = get_obj_offset(bytes(data), xref_pos, p1_xref)
    plain = decompress_stream(bytes(data), p1_xref, xref_pos)

    # Apply replacements
    new_plain = plain
    new_plain = new_plain.replace(AMT_OLD, AMT_NEW)
    new_plain = new_plain.replace(TOT_OLD, TOT_NEW)

    amt_replaced = new_plain.count(AMT_NEW)
    tot_replaced = TOT_NEW in new_plain
    print(f"Amount replacements: {amt_replaced} (expected 2 — x=199 and x=294)")
    print(f"Total replaced: {tot_replaced}")

    data, delta = rebuild_stream(data, p1_xref, xref_pos, new_plain)
    print(f"Stream delta: {delta:+d} bytes")

    if delta != 0:
        xref_pos += delta
        data = shift_xref(data, xref_pos, obj_off, delta)
        data = bytearray(re.sub(
            rb"startxref\s+\d+\s+%%EOF",
            b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF",
            bytes(data)
        ))

    with open(DST, "wb") as f:
        f.write(data)

    src_sz = os.path.getsize(SRC)
    dst_sz = os.path.getsize(DST)
    print(f"\nWrote {dst_sz} bytes (delta {dst_sz-src_sz:+d}) → {DST}")

    # Verify
    doc2 = fitz.open(DST)
    txt = doc2[0].get_text()
    doc2.close()

    checks = [
        ("no +3 000.00",   "+3 000.00" not in txt),
        ("+30.00 present", "+30.00" in txt),
        ("no 3 000,00",    "3 000,00" not in txt),
        ("30,00 present",  "30,00" in txt),
        ("-29 500.00 ok",  "-29 500.00" in txt),
    ]
    print("\nVerification:")
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    if all(ok for _, ok in checks):
        print("\nAll checks passed ✓")

if __name__ == "__main__":
    main()
