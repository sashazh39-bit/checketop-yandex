#!/usr/bin/env python3
"""Check F2 ToUnicode in enriched donors."""
import sys, zlib, re
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_enrich_donor import _find_f2_tounicode_obj

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_3*_enriched.pdf")):
    raw = d.read_bytes()
    tou_num = _find_f2_tounicode_obj(raw)
    if tou_num is None:
        print(f"NO_TOU  {d.name}")
        continue
    
    # Find stream
    pat = re.compile(rb"(\d+)\s+0\s+obj\s*\n?<<")
    found = False
    for sm in re.finditer(pat, raw):
        if int(sm.group(1)) != tou_num:
            continue
        chunk = raw[sm.end():sm.end()+500]
        len_m = re.search(rb"/Length\s+(\d+)", chunk)
        end_m = re.search(rb">>\s*stream\r?\n", chunk)
        if len_m and end_m:
            old_len = int(len_m.group(1))
            sstart = sm.end() + end_m.end()
            stream_bytes = raw[sstart:sstart+old_len]
            compressed = b"/Filter" in chunk
            try:
                data = zlib.decompress(stream_bytes) if compressed else stream_bytes
            except Exception:
                data = stream_bytes
            has_range = b"<013a>" in data or b"<0131><013a>" in data
            digit_count = sum(1 for cid in range(0x0131, 0x013B)
                              if f"<{cid:04x}>".encode() in data)
            print(f"{'OK  ' if has_range else 'MISS'} {d.name}: obj={tou_num} len={old_len} has_range={has_range} digit_cids={digit_count}")
            found = True
        break
    
    if not found:
        print(f"NOOBJ  {d.name}: tou_obj={tou_num}")
