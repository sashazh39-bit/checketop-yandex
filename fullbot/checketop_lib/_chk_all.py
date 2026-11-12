import fitz
from pathlib import Path

def check_donor(d):
    doc = fitz.open(d)
    page = doc[0]
    bad = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch_info in span.get("chars", []):
                    c = ch_info.get("c", "")
                    if len(c) == 1:
                        cp = ord(c)
                        if 0x0080 <= cp <= 0x00FF or (0x0100 <= cp <= 0x03FF):
                            bad.append(hex(cp))
    doc.close()
    return bad[:3]

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    bad = check_donor(d)
    s = "BAD " if bad else "GOOD"
    print(f"{s} {d.name}: {bad if bad else 'ok'}")
