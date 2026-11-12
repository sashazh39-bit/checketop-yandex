#!/usr/bin/env python3
"""Audit ToUnicode CMap for each verified donor - find donors where our CIDs map correctly."""
import re
from pathlib import Path
from io import BytesIO

CYRILLIC_CHARS = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"

def get_tounicode_cmap(pdf_bytes: bytes, font_name: str = "Regular") -> dict[int, int]:
    """Extract ToUnicode CMap from a PDF font: CID -> Unicode codepoint."""
    # Find font stream for name_fragment
    cid_to_uni = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj", pdf_bytes):
        chunk = pdf_bytes[m.start(): m.start() + 2000]
        if b"ToUnicode" not in chunk:
            continue
        # Find ToUnicode stream reference
        tou_m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", chunk)
        if not tou_m:
            continue
        # Check font name is nearby
        fn_m = re.search(rb"/BaseFont\s*/([^\s/\]>]+)", chunk)
        if fn_m and font_name.encode() not in fn_m.group(1):
            continue
        tou_obj = int(tou_m.group(1))
        # Find the ToUnicode stream object
        pat = rb"(" + str(tou_obj).encode() + rb")\s+0\s+obj\s*<<"
        for sm in re.finditer(pat, pdf_bytes):
            if int(sm.group(1)) != tou_obj:
                continue
            schunk = pdf_bytes[sm.end(): sm.end() + 500]
            len_m = re.search(rb"/Length\s+(\d+)", schunk)
            end_m = re.search(rb">>\s*stream\r?\n", schunk)
            if not len_m or not end_m:
                continue
            slen = int(len_m.group(1))
            sstart = sm.end() + end_m.end()
            cmap_bytes = pdf_bytes[sstart: sstart + slen]
            # Parse: <XXXX> <YYYY> pairs in beginbfchar..endbfchar
            for pair in re.finditer(rb"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{4})>", cmap_bytes):
                cid = int(pair.group(1), 16)
                uni = int(pair.group(2), 16)
                cid_to_uni[cid] = uni
            return cid_to_uni
    return cid_to_uni

from tbank_cmap import REGULAR_UNI_TO_CID

# Build reverse map
cid_to_expected_uni = {cid: uni for uni, cid in REGULAR_UNI_TO_CID.items()}

checked = 0
good = 0
donors_by_coverage = []

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    raw = d.read_bytes()
    cmap = get_tounicode_cmap(raw, "Regular")
    if not cmap:
        continue
    checked += 1
    # Check key Cyrillic chars
    bad_cids = []
    for ch in CYRILLIC_CHARS[:30]:  # test first 30 Cyrillic chars
        cid = REGULAR_UNI_TO_CID.get(ord(ch))
        if cid is None:
            continue
        actual_uni = cmap.get(cid)
        if actual_uni is not None and actual_uni != ord(ch):
            bad_cids.append((ch, cid, actual_uni))
    if bad_cids:
        print(f"BAD  {d.name}: {len(bad_cids)} mismatches, e.g. {bad_cids[:2]}")
    else:
        print(f"GOOD {d.name}: CMap matches our table")
        good += 1
        donors_by_coverage.append(d.name)

print()
print(f"Checked {checked} donors, {good} have correct ToUnicode CMap")
print("Good donors:", donors_by_coverage)
