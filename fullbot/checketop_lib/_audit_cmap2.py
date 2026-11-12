#!/usr/bin/env python3
"""Find ToUnicode CMap in T-Bank PDFs."""
import re, zlib
from pathlib import Path

def extract_cmap_from_pdf(pdf_bytes: bytes) -> dict[int, int]:
    """Find and decode ToUnicode CMap from the first font that has one."""
    # Find all objects with ToUnicode
    results = {}
    # Search for CMap streams directly
    for m in re.finditer(rb"/CMapName", pdf_bytes):
        # find the stream around here
        # look backwards for stream start
        chunk_start = max(0, m.start() - 200)
        chunk = pdf_bytes[chunk_start: m.start() + 5000]
        # parse beginbfchar..endbfchar
        for pair in re.finditer(rb"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{4})>", chunk):
            cid = int(pair.group(1), 16)
            uni = int(pair.group(2), 16)
            results[cid] = uni
    if results:
        return results
    
    # Try uncompressed ToUnicode streams
    for m in re.finditer(rb"beginbfchar", pdf_bytes):
        chunk = pdf_bytes[m.start(): m.start() + 5000]
        for pair in re.finditer(rb"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{4})>", chunk):
            cid = int(pair.group(1), 16)
            uni = int(pair.group(2), 16)
            results[cid] = uni
    if results:
        return results
    
    # Try decompressed streams
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<", pdf_bytes):
        chunk = pdf_bytes[m.end(): m.end() + 300]
        if b"ToUnicode" not in chunk and b"CMap" not in chunk:
            continue
        len_m = re.search(rb"/Length\s+(\d+)", chunk)
        end_m = re.search(rb">>\s*stream\r?\n", chunk)
        if not len_m or not end_m:
            continue
        slen = int(len_m.group(1))
        sstart = m.end() + end_m.end()
        raw = pdf_bytes[sstart: sstart + slen]
        try:
            data = zlib.decompress(raw)
        except Exception:
            data = raw
        if b"beginbfchar" in data or b"beginbfrange" in data:
            for pair in re.finditer(rb"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{4})>", data):
                cid = int(pair.group(1), 16)
                uni = int(pair.group(2), 16)
                results[cid] = uni
    return results

from tbank_cmap import REGULAR_UNI_TO_CID
cid_to_expected = {cid: uni for uni, cid in REGULAR_UNI_TO_CID.items()}

TEST_CHARS = "АБВГДЕЖЗИКЛМНОПРСТУФабвгдежзийклмнопрстуфхцчб"

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    raw = d.read_bytes()
    cmap = extract_cmap_from_pdf(raw)
    if not cmap:
        print(f"NO_CMAP {d.name}")
        continue
    bad = []
    for ch in TEST_CHARS:
        cid = REGULAR_UNI_TO_CID.get(ord(ch))
        if cid is None:
            continue
        actual = cmap.get(cid)
        if actual is not None and actual != ord(ch):
            bad.append(f"'{ch}'(U+{ord(ch):04X})@CID{cid}->U+{actual:04X}")
    status = "BAD " if bad else "GOOD"
    print(f"{status} {d.name}: {bad[:3] if bad else 'ok'}")
