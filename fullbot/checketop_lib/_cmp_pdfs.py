#!/usr/bin/env python3
import re
from pathlib import Path
from tbank_check_service import extract_fields, detect_receipt_type, _parse_keywords, donor_keywords_ok

FILES = [
    r"c:\Users\drozd\Downloads\receipt_05.05.2026.pdf",
    r"c:\Users\drozd\Downloads\Tbank_SBP_24052026_11300.pdf",
    r"c:\Users\drozd\Downloads\receipt_12.05.2026.pdf",
]

for fp in FILES:
    p = Path(fp)
    if not p.exists():
        print(f"MISSING: {fp}\n")
        continue
    raw = p.read_bytes()
    rt = detect_receipt_type(p)
    fields = extract_fields(p, rt)
    kw = _parse_keywords(raw)
    print("=" * 70)
    print(p.name, f"({len(raw)} bytes)", f"type={rt}")
    print("donor_ok:", donor_keywords_ok(p))
    if kw:
        print(f"Keywords: {' | '.join(kw)}")
        print(f"  fmt: {'uuid' if '-' in kw[1] else 'md5'} suffix={kw[2]}")
    for tag in (b"/CreationDate", b"/ModDate"):
        m = re.search(tag + rb"\s*\(([^)]+)\)", raw)
        if m:
            print(f"  {tag.decode()}: {m.group(1).decode('latin-1')}")
    idm = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]", raw)
    if idm:
        print(f"  ID[0]={idm.group(1).decode()} ID[1]={idm.group(2).decode()} same={idm.group(1)==idm.group(2)}")
    print(f"  content datetime: {fields.get('datetime')}")
    print(f"  amount_bold: {repr(fields.get('amount_bold'))}")
    print(f"  amount_small: {repr(fields.get('amount_small'))}")
    print(f"  sender: {fields.get('sender')}")
    print(f"  receiver: {fields.get('receiver')}")
    if kw and fields.get("datetime"):
        kw_ts = re.sub(r"\s+", " ", kw[0].strip())
        cnt_ts = re.sub(r"\s+", " ", fields["datetime"].strip())
        print(f"  kw_ts==content: {kw_ts == cnt_ts} (kw={kw_ts!r} content={cnt_ts!r})")
    print()
