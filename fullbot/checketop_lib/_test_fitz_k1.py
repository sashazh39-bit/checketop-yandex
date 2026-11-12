#!/usr/bin/env python3
"""Test fitz check on k1 to see what chars it finds."""
import fitz
from pathlib import Path

_ALLOWED_EXTRAS = frozenset([
    0x00A9, 0x00AE, 0x00AB, 0x00BB,
    0x2019, 0x201C, 0x201D,
    0x2013, 0x2014,
    0x2022, 0x00B7, 0x0020,
])

for name in ["tbank_sbp_verified_31052026_k1.pdf", "tbank_sbp_verified_12052026.pdf"]:
    d = Path("TBANK") / name
    doc = fitz.open(d)
    bad_chars = []
    for blk in doc[0].get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("text", ""):
                    cp = ord(ch)
                    if cp <= 0x7F:
                        continue
                    if 0x0400 <= cp <= 0x04FF:
                        continue
                    if cp in _ALLOWED_EXTRAS:
                        continue
                    bad_chars.append((ch, hex(cp)))
    doc.close()
    if bad_chars:
        print(f"FAIL {name}: bad chars = {bad_chars}")
    else:
        print(f"PASS {name}")
