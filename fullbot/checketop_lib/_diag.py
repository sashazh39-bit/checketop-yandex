#!/usr/bin/env python3
import re, zlib
from pathlib import Path
from tbank_check_service import (
    _find_page_stream, _parse_keywords, extract_fields, detect_receipt_type,
)

f = Path(r"c:\Users\drozd\Downloads\Tbank_SBP_31052026_2050.pdf")
raw = f.read_bytes()
rt = detect_receipt_type(f)
flds = extract_fields(f, rt)
kw = _parse_keywords(raw)
_, st, slen, dec = _find_page_stream(raw)
chunk = raw[st:st+slen]
hdr = chunk[:2].hex()
lvl = next((l for l in range(1,10) if zlib.compress(zlib.decompress(chunk),l)==chunk), None)
trailing = len(dec) - len(dec.rstrip(b"\n\r \t"))

credate = re.search(rb"/CreationDate\(([^)]+)\)", raw)
idm = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", raw)

print(f"=== {f.name} ===")
print(f"size={len(raw)}  hdr={hdr}  lvl={lvl}  trail={trailing}")
print(f"Keywords: {kw}")
print(f"CreDate: {credate.group(1).decode('latin1') if credate else 'NONE'}")
print(f"ID same: {idm.group(1)==idm.group(2) if idm else '?'}")
print()
print("--- Extracted fields ---")
for k, v in sorted(flds.items()):
    print(f"  {k}: {v!r}")
print()
print(f"last dec bytes: {dec[-40:]!r}")
