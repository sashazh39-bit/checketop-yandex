#!/usr/bin/env python3
"""Extract ToUnicode CMap stream from a T-Bank PDF."""
import sys, zlib, re
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

d = Path("TBANK/tbank_sbp_verified_31052026_k4.pdf")
raw = d.read_bytes()

# Find all objects with CMapName or beginbfchar
for m in re.finditer(rb"(\d+)\s+0\s+obj\s*\n?<<", raw):
    obj_num = int(m.group(1))
    header_end = raw.find(b">>", m.end())
    header = raw[m.end():min(header_end+2, m.end()+500)]
    
    if b"ToUnicode" not in header and b"CMapName" not in header:
        continue
    
    # Check if this obj has stream
    end_m = re.search(rb">>\s*stream\r?\n", raw[m.end():m.end()+600])
    if not end_m:
        continue
    
    len_m = re.search(rb"/Length\s+(\d+)", header)
    if not len_m:
        continue
    
    slen = int(len_m.group(1))
    sstart = m.end() + end_m.end()
    stream_bytes = raw[sstart:sstart+slen]
    
    # Try decompress
    try:
        data = zlib.decompress(stream_bytes)
        compressed = True
    except Exception:
        data = stream_bytes
        compressed = False
    
    print(f"=== Object {obj_num} (compressed={compressed}) ===")
    if b"beginbfchar" in data or b"CMapName" in data:
        text = data.decode("latin-1")
        print(text[:2000])
        print("...")
        break
