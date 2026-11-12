#!/usr/bin/env python3
"""Re-enrich all verified donors with ToUnicode fix."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_enrich_donor import enrich_pdf

TBANK = Path("TBANK")
ok = 0
skip = 0
for d in sorted(TBANK.glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    out = d.with_stem(d.stem + "_enriched")
    print(f"\n--- {d.name} ---")
    try:
        if enrich_pdf(d, out):
            ok += 1
        else:
            skip += 1
    except Exception as e:
        print(f"  ERROR: {e}")
        skip += 1

print(f"\nDone: {ok} enriched, {skip} skipped/errors")
