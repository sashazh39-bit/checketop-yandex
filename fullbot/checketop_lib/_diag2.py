#!/usr/bin/env python3
import re, zlib
from pathlib import Path
import fitz
from tbank_check_service import _find_page_stream, _parse_keywords

f = Path(r"c:\Users\drozd\Downloads\Tbank_SBP_31052026_2050.pdf")
raw = f.read_bytes()

kw = _parse_keywords(raw)
kw_hash = kw[1] if kw else None

print(f"=== {f.name} ===")
print(f"size={len(raw)}")
print(f"Keywords: {kw}")
print()

# Find donor by Keywords hash
print("=== Matching donor ===")
for d in sorted(Path("TBANK").glob("*.pdf")):
    try:
        dkw = _parse_keywords(d.read_bytes())
        if dkw and dkw[1] == kw_hash:
            print(f"DONOR MATCH: {d.name}  ts={dkw[0]}")
    except Exception:
        pass

print()
# Extract all text with fitz (unicode)
print("=== PDF text (fitz) ===")
doc = fitz.open(f)
page = doc[0]
blocks = page.get_text("dict")["blocks"]
texts = []
for b in blocks:
    for line in b.get("lines", []):
        for span in line.get("spans", []):
            t = span["text"].strip()
            if t:
                texts.append(t)
doc.close()
for t in texts:
    print(f"  {t!r}")

print()
# Structure check
_, st, slen, dec = _find_page_stream(raw)
chunk = raw[st:st+slen]
hdr = chunk[:2].hex()
lvl = next((l for l in range(1,10) if zlib.compress(zlib.decompress(chunk),l)==chunk), None)
trail = len(dec) - len(dec.rstrip(b"\n\r \t"))

# Check font streams
f1_len_m = re.search(rb"Regular[^\n]{0,200}/FontFile2\s+(\d+)\s+0\s+R", raw, re.DOTALL)
f2_len_m = re.search(rb"Medium[^\n]{0,200}/FontFile2\s+(\d+)\s+0\s+R", raw, re.DOTALL)

print("=== Structure ===")
print(f"hdr={hdr} lvl={lvl} trail={trail}")
credate = re.search(rb"/CreationDate\(([^)]+)\)", raw)
print(f"CreDate: {credate.group(1).decode('latin1') if credate else 'NONE'}")
print(f"Keywords ts: {kw[0] if kw else '?'}")
