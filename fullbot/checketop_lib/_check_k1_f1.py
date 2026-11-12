#!/usr/bin/env python3
"""Find which char makes k1 fail F1 coverage check."""
from pathlib import Path
from gen_tbank_receipt import _renderable_chars_for, _renderable_f2_chars_for

changes = {
    "amount_bold": "2 050 ", "amount_small": "2 050 ",
    "sender": "Дмитрий Дроздов", "phone": "+79118570885",
    "receiver": "Степан С.", "bank": "Сбербанк",
    "account": "408178100000****2345",
    "datetime": "31.05.2026  13:40:23",
}

needed = set()
for val in changes.values():
    for ch in str(val):
        if ch != " ":
            needed.add(ord(ch))

print("Needed chars:", sorted(chr(c) for c in needed))
print()

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_3*.pdf")):
    if "_enriched" in d.stem:
        continue
    chars = _renderable_chars_for(d)
    if chars is None:
        print(f"{d.name}: F1=None (all renderable)")
        continue
    missing = needed - chars
    if missing:
        print(f"{d.name}: MISSING F1 chars = {[chr(c) for c in sorted(missing)]}")
    else:
        f2 = _renderable_f2_chars_for(d)
        f2_digits = sorted(chr(c) for c in f2 if chr(c).isdigit()) if f2 else None
        print(f"{d.name}: F1 OK, F2_digits={f2_digits}")
