#!/usr/bin/env python3
"""Direct trace of _find_donor logic."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

# Manually reproduce _find_donor with verbose output
from gen_tbank_receipt import (
    _all_donors, _verified_donors, _donor_priority, _probe_padding,
    _renderable_chars_for, _renderable_f2_chars_for, _TYPE_CACHE, _FULL_SBP_TYPES,
)
from tbank_check_service import detect_receipt_type as _detect_type
import fitz

changes = {
    "amount_bold": "2 050 ", "amount_small": "2 050 ",
    "sender": "Дмитрий Дроздов", "phone": "+79118570885",
    "receiver": "Степан С.", "bank": "Сбербанк",
    "account": "408178100000****2345",
    "datetime": "31.05.2026  13:40:23",
}

_F1_SKIP_CHARS = frozenset("* ")
needed = set()
for val in changes.values():
    for ch in str(val):
        if ch not in _F1_SKIP_CHARS:
            needed.add(ord(ch))

print(f"Needed chars: {sorted(chr(c) for c in needed)}")
print()

all_donors = _all_donors(integrity_only=True)
safe = _verified_donors(all_donors)
if safe:
    all_donors = safe
full = [d for d in all_donors if _TYPE_CACHE.get(d, _detect_type(d)) in _FULL_SBP_TYPES]
if full:
    all_donors = full

needed_digits = set()
for key in ("amount_bold", "amount_small"):
    for ch in str(changes.get(key, "")):
        if ch.isdigit():
            needed_digits.add(ord(ch))

print(f"needed_digits: {[chr(c) for c in sorted(needed_digits)]}")
print()

eligible_fresh = []
eligible_enriched = []
all_donors_set = set(all_donors)

for d in all_donors:
    chars = _renderable_chars_for(d)
    if chars is not None:
        covered = needed & chars
        if covered != needed:
            miss = [chr(c) for c in sorted(needed - chars)]
            print(f"SKIP_F1 {d.name}: missing={miss}")
            continue

    f2_chars = _renderable_f2_chars_for(d)
    orig_d = d
    if f2_chars is not None and needed_digits and not needed_digits.issubset(f2_chars):
        if "_enriched" not in d.stem:
            ec = d.with_stem(d.stem + "_enriched")
            if ec.exists() or ec in all_donors_set:
                d = ec
            else:
                print(f"SKIP_F2_NO_ENRICH {orig_d.name}")
                continue
        else:
            print(f"SKIP_F2_ALREADY_ENRICHED {orig_d.name}")
            continue

    # fitz check
    fitz_ok = True
    try:
        doc = fitz.open(d)
        for blk in doc[0].get_text("dict").get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    for ch in sp.get("text", ""):
                        cp = ord(ch)
                        if 0x0100 <= cp <= 0x03FF:
                            fitz_ok = False
                            break
                    if not fitz_ok: break
                if not fitz_ok: break
            if not fitz_ok: break
        doc.close()
    except Exception as e:
        pass

    if not fitz_ok:
        print(f"SKIP_FITZ {d.name}")
        continue

    prio = _donor_priority(d)
    print(f"ELIGIBLE {d.name}  prio={prio}")
    if "_enriched" in d.stem:
        eligible_enriched.append(d)
    else:
        eligible_fresh.append(d)

print()
print(f"eligible_fresh: {[d.name for d in eligible_fresh]}")
print(f"eligible_enriched: {[d.name for d in eligible_enriched[:5]]}... ({len(eligible_enriched)} total)")
