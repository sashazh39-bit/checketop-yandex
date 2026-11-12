#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from gen_tbank_receipt import _all_donors, _verified_donors
from tbank_check_service import donor_keywords_ok, _parse_keywords, detect_receipt_type

all_d = _all_donors(integrity_only=True)
print(f"all_donors count: {len(all_d)}")
print()

# Check k-series
for name in ["tbank_sbp_verified_31052026_k1.pdf", "tbank_sbp_verified_31052026_k2.pdf",
             "tbank_sbp_verified_31052026_k3.pdf", "tbank_sbp_verified_31052026_k4.pdf",
             "tbank_sbp_verified_31052026_k5.pdf", "tbank_sbp_verified_31052026_k6.pdf",
             "tbank_sbp_verified_31052026_k7.pdf"]:
    d = Path("TBANK") / name
    if not d.exists():
        print(f"{name}: NOT FOUND")
        continue
    kw = _parse_keywords(d.read_bytes())
    ok = donor_keywords_ok(d)
    rt = detect_receipt_type(d)
    in_all = d in all_d or d.resolve() in [x.resolve() for x in all_d]
    safe = _verified_donors(all_d)
    in_safe = d in safe or d.resolve() in [x.resolve() for x in safe]
    print(f"{name}: kw_ok={ok}  rt={rt}  in_all={in_all}  in_safe={in_safe}")
    print(f"  kw: {kw}")
