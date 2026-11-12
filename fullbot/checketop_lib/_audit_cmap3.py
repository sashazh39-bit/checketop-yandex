#!/usr/bin/env python3
"""Use fitz to extract actual character mapping from font."""
import fitz
from pathlib import Path
from tbank_cmap import REGULAR_UNI_TO_CID

TEST_CHARS = "АБВГДЕЁЖЗИКЛМНОПРСТУФабвгдежзийклмнопрстуфхцчшб"

def check_donor(d: Path) -> tuple[bool, list]:
    """Return (is_good, list_of_bad_chars). Good if our CIDs decode to correct unicode."""
    import fitz
    doc = fitz.open(d)
    page = doc[0]
    # Insert test chars via annotation (won't modify file), just check font decoding
    # Better: look at existing text and see if it decodes correctly
    # Extract char-level info
    ok = True
    bad = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch_info in span.get("chars", []):
                    c = ch_info.get("c", "")
                    if len(c) == 1:
                        cp = ord(c)
                        # Cyrillic range should be 0x0400-0x04FF
                        # If we see anything from 0x80-0x3FF or 0x0500+, that's a mapping issue
                        if 0x0080 <= cp <= 0x00FF or (0x0100 <= cp <= 0x03FF):
                            bad.append((cp, hex(cp), ch_info.get("origin")))
                            ok = False
    doc.close()
    return ok, bad[:5]

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_3*.pdf")):
    if "_enriched" in d.stem:
        continue
    try:
        good, bad = check_donor(d)
        status = "GOOD" if good else "BAD "
        print(f"{status} {d.name}: {bad if bad else 'ok'}")
    except Exception as e:
        print(f"ERR  {d.name}: {e}")
