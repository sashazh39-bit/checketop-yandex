#!/usr/bin/env python3
"""Test generated receipt - check all chars decode correctly."""
import sys, zlib
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import fitz
from tbank_check_service import _find_page_stream, _parse_keywords, extract_fields, detect_receipt_type
from gen_tbank_receipt import generate_tbank_receipt

for amount, bank in [(2050, "Сбербанк"), (11300, "Альфа-Банк"), (4000, "Газпромбанк")]:
    print(f"\n{'='*60}")
    print(f"Generating amount={amount}, bank={bank}")
    
    try:
        pdf_bytes, fname = generate_tbank_receipt(
            amount=amount,
            sender_name="Дмитрий Дроздов",
            sender_account="408178100000****2345",
            recipient_name="Степан С.",
            recipient_phone="+79118570885",
            recipient_bank=bank,
            operation_date="31.05.2026",
            operation_time="13:40:23",
            preserve_integrity=True,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        continue
    
    out = Path(f"c:/Users/drozd/Downloads/Tbank_test_{amount}_{bank[:4]}.pdf")
    out.write_bytes(pdf_bytes)
    
    kw = _parse_keywords(pdf_bytes)
    _, st, slen, dec = _find_page_stream(pdf_bytes)
    chunk = pdf_bytes[st:st+slen]
    hdr = chunk[:2].hex()
    lvl = next((l for l in range(1,10) if zlib.compress(zlib.decompress(chunk),l)==chunk), None)
    trail = len(dec) - len(dec.rstrip(b"\n\r \t"))
    
    print(f"  size={len(pdf_bytes)} hdr={hdr} lvl={lvl} trail={trail}")
    print(f"  Keywords: {kw}")
    
    # Check fitz decodes correctly
    doc = fitz.open(out)
    bad = []
    texts = []
    for block in doc[0].get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = ""
                for ch_info in span.get("chars", []):
                    c = ch_info.get("c", "")
                    t += c
                    if c and len(c) == 1:
                        cp = ord(c)
                        if 0x0100 <= cp <= 0x03FF:
                            bad.append((c, hex(cp)))
                if t.strip():
                    texts.append(t.strip())
    doc.close()
    
    if bad:
        print(f"  BAD chars: {bad[:5]}")
    else:
        print(f"  fitz chars: OK")
    
    print(f"  fields:")
    for t in texts[:15]:
        print(f"    {t!r}")
