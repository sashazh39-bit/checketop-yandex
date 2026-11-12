#!/usr/bin/env python3
"""Debug donor selection for amount 2050."""
import fitz, zlib
from pathlib import Path
from tbank_check_service import (
    _find_page_stream, _parse_keywords,
    get_renderable_chars, detect_receipt_type,
)
from gen_tbank_receipt import _find_donor, _probe_padding, _renderable_f2_chars_for

amount = 2050
amt_str = f"{amount:,}".replace(",", " ") + " "
needed_digits = {ord(c) for c in amt_str if c.isdigit()}
print(f"Amount: {amount}  amt_str={amt_str!r}  needed_digit_cps={sorted(needed_digits)} = {[chr(c) for c in sorted(needed_digits)]}")
print()

changes = {
    "amount_bold": amt_str, "amount_small": amt_str,
    "sender": "Дмитрий Дроздов", "phone": "+79118570885",
    "receiver": "Степан С.", "bank": "Сбербанк",
    "account": "408178100000****2345",
    "datetime": "31.05.2026  13:40:23",
}

print("Checking each verified donor F2 digits:")
for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    f2 = _renderable_f2_chars_for(d)
    if f2 is None:
        print(f"  {d.name}: F2=None (skip)")
        continue
    f2_digits = [chr(c) for c in f2 if chr(c).isdigit()]
    covered = needed_digits.issubset(f2)
    print(f"  {d.name}: F2_digits={f2_digits}  covers_amount={covered}")

print()
donor = _find_donor(text_fields=changes, preserve_integrity=True)
print(f"Selected donor: {donor.name}")
pad = _probe_padding(donor, changes)
print(f"Padding: {pad}")

# Also generate and check fitz
from gen_tbank_receipt import generate_tbank_receipt
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
out = Path(r"c:\Users\drozd\Downloads\Tbank_debug_2050.pdf")
out.write_bytes(pdf_bytes)

kw = _parse_keywords(pdf_bytes)
print(f"\nGenerated: size={len(pdf_bytes)} kw={kw}")

doc = fitz.open(out)
print("fitz text spans:")
bad_chars = []
for block in doc[0].get_text("rawdict")["blocks"]:
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            t = ""
            for ch in span.get("chars", []):
                c = ch.get("c", "")
                cp = ord(c) if c else 0
                if 0x0100 <= cp <= 0x03FF:
                    bad_chars.append((c, hex(cp)))
                t += c
            if t.strip():
                safe = t.encode("utf8", errors="replace").decode("utf8")
                print(f"  {safe!r}")
doc.close()
if bad_chars:
    print(f"BAD chars (non-cyrillic non-ascii): {bad_chars[:5]}")
else:
    print("All chars OK!")
