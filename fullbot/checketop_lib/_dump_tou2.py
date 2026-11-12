#!/usr/bin/env python3
"""Find ToUnicode stream via ToUnicode reference."""
import sys, zlib, re
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import fitz

d = Path("TBANK/tbank_sbp_verified_31052026_k4.pdf")
raw = d.read_bytes()

# Find ToUnicode reference: /ToUnicode N 0 R
for m in re.finditer(rb"/ToUnicode\s+(\d+)\s+0\s+R", raw):
    tou_num = int(m.group(1))
    print(f"Found /ToUnicode {tou_num} 0 R at offset {m.start()}")
    
    # Find the stream object
    pat = re.compile(rb"(\b" + str(tou_num).encode() + rb")\s+0\s+obj\s*\n?")
    for sm in re.finditer(pat, raw):
        if int(sm.group(1)) == tou_num:
            chunk = raw[sm.end():sm.end()+500]
            len_m = re.search(rb"/Length\s+(\d+)", chunk)
            end_m = re.search(rb">>\s*stream\r?\n", chunk)
            if len_m and end_m:
                slen = int(len_m.group(1))
                sstart = sm.end() + end_m.end()
                stream_bytes = raw[sstart:sstart+slen]
                
                compressed_m = re.search(rb"/Filter\s*/FlateDecode", chunk)
                try:
                    data = zlib.decompress(stream_bytes)
                except Exception:
                    data = stream_bytes
                
                print(f"Object {tou_num}: length={slen}, actual_len={len(stream_bytes)}")
                print(data.decode("latin-1")[:2000])
                break

print()
print("=== Using fitz font info ===")
doc = fitz.open(d)
page = doc[0]
for font in page.get_fonts():
    print(f"Font: {font}")
doc.close()
