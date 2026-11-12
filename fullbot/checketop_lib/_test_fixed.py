#!/usr/bin/env python3
import zlib
from pathlib import Path
from tbank_check_service import _find_page_stream, _parse_keywords, extract_fields, detect_receipt_type
from gen_tbank_receipt import generate_tbank_receipt, _find_donor, _probe_padding

for amount, digits_desc in [(2050, "2050 -> needs {0,2,5}"), (11300, "11300 -> needs {0,1,3}")]:
    amt_str = f"{amount:,}".replace(",", " ") + " "
    changes = {
        "amount_bold": amt_str, "amount_small": amt_str,
        "sender": "Дмитрий Дроздов", "phone": "+79118570885",
        "receiver": "Степан С.", "bank": "Сбербанк",
        "account": "408178100000****2345",
        "datetime": f"31.05.2026  13:40:23",
    }
    donor = _find_donor(text_fields=changes, preserve_integrity=True)
    pad = _probe_padding(donor, changes)
    print(f"Amount {amount} ({digits_desc})")
    print(f"  Donor: {donor.name}  padding={pad}")
    print()

# Full generate for 2050
pdf_bytes, fname = generate_tbank_receipt(
    amount=2050,
    sender_name="Дмитрий Дроздов",
    sender_account="408178100000****2345",
    recipient_name="Степан С.",
    recipient_phone="+79118570885",
    recipient_bank="Сбербанк",
    operation_date="31.05.2026",
    operation_time="13:40:23",
    preserve_integrity=True,
)
out = Path(r"c:\Users\drozd\Downloads\Tbank_fixed_31052026_2050.pdf")
out.write_bytes(pdf_bytes)

_, st, slen, dec = _find_page_stream(pdf_bytes)
chunk = pdf_bytes[st:st+slen]
hdr = chunk[:2].hex()
lvl = next((l for l in range(1,10) if zlib.compress(zlib.decompress(chunk),l)==chunk), None)
trail = len(dec) - len(dec.rstrip(b"\n\r \t"))
kw = _parse_keywords(pdf_bytes)
flds = extract_fields(out, detect_receipt_type(out))

print("=== Generated 2050 fixed ===")
print(f"  size={len(pdf_bytes)} hdr={hdr} lvl={lvl} trail={trail}")
print(f"  Keywords: {kw}")
print(f"  amount_bold={flds.get('amount_bold')!r}  bank={flds.get('bank')!r}")
print(f"  sender={flds.get('sender')!r}")

# Check with fitz
import fitz
doc = fitz.open(out)
texts = []
for b in doc[0].get_text("dict")["blocks"]:
    for ln in b.get("lines", []):
        for sp in ln.get("spans", []):
            t = sp["text"].strip()
            if t: texts.append(t)
doc.close()
print("  fitz text OK:", all(all(0x20 <= ord(c) <= 0x7E or 0x0400 <= ord(c) <= 0x04FF for c in t) for t in texts))
for t in texts[:12]:
    print(f"    {t!r}")
