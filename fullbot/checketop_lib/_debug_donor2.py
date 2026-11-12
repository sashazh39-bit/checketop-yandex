#!/usr/bin/env python3
"""Patch _find_donor to print debug info."""
import gen_tbank_receipt as g
from pathlib import Path
from tbank_check_service import _parse_keywords

orig_find = g._find_donor

def debug_find(prefer_enriched=False, text_fields=None, require_full_sbp=True, preserve_integrity=False):
    from tbank_check_service import detect_receipt_type as _detect_type
    import random, zlib
    
    all_donors = g._all_donors(integrity_only=preserve_integrity)
    safe_donors = g._verified_donors(all_donors)
    if safe_donors:
        all_donors = safe_donors
    
    if require_full_sbp:
        full = [d for d in all_donors if g._TYPE_CACHE.get(d, _detect_type(d)) in g._FULL_SBP_TYPES]
        if full:
            all_donors = full
    
    needed = set()
    if text_fields:
        for val in text_fields.values():
            for ch in str(val):
                if ch != " ":
                    needed.add(ord(ch))
    
    eligible_fresh = []
    eligible_enriched = []
    
    for d in all_donors:
        chars = g._renderable_chars_for(d)
        if chars is not None:
            covered = needed & chars
            if covered != needed:
                miss = [chr(c) for c in (needed - chars)]
                continue
        
        needed_digits = set()
        if text_fields:
            for key in ("amount_bold", "amount_small"):
                for ch in str(text_fields.get(key, "")):
                    if ch.isdigit():
                        needed_digits.add(ord(ch))
        
        f2 = g._renderable_f2_chars_for(d)
        original_d = d
        if f2 is not None and needed_digits and not needed_digits.issubset(f2):
            if "_enriched" not in d.stem:
                ec = d.with_stem(d.stem + "_enriched")
                if ec.exists():
                    d = ec
                else:
                    continue
            else:
                continue
        
        # fitz check
        import fitz
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
        except Exception:
            pass
        
        if not fitz_ok:
            print(f"FITZ_EXCLUDE: {d.name}")
            continue
        
        prio = g._donor_priority(d)
        print(f"ELIGIBLE: {d.name}  prio={prio}  F2_was={g._renderable_f2_chars_for(original_d)!r}")
        
        if "_enriched" in d.stem:
            eligible_enriched.append(d)
        else:
            eligible_fresh.append(d)
    
    pool = eligible_fresh + eligible_enriched
    if not pool:
        print("POOL IS EMPTY - falling back")
        return orig_find(prefer_enriched, text_fields, require_full_sbp, preserve_integrity)
    
    top_score = min(g._donor_priority(p)[0] for p in pool)
    best = [p for p in pool if g._donor_priority(p)[0] == top_score]
    print(f"\ntop_score={top_score} best={[p.name for p in best]}")
    return best[0]

changes = {
    "amount_bold": "2 050 ", "amount_small": "2 050 ",
    "sender": "Дмитрий Дроздов", "phone": "+79118570885",
    "receiver": "Степан С.", "bank": "Сбербанк",
    "account": "408178100000****2345",
    "datetime": "31.05.2026  13:40:23",
}

print("=== Finding donor for 2050 ===")
result = debug_find(text_fields=changes, preserve_integrity=True)
print(f"\nFinal: {result.name}")
