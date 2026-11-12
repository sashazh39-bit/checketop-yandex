#!/usr/bin/env python3
import sys, zlib
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_check_service import _find_page_stream, _parse_keywords

# Compare generated file vs its donor
gen = Path(r"c:\Users\drozd\Downloads\Tbank_debug_2050.pdf")
gen_raw = gen.read_bytes()
gen_kw = _parse_keywords(gen_raw)

# Find donor by hash
donor = None
for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem: continue
    try:
        dkw = _parse_keywords(d.read_bytes())
        if dkw and dkw[1] == gen_kw[1]:
            donor = d
            break
    except: pass

if not donor:
    print("Donor not found!")
else:
    print(f"Donor: {donor.name}")
    donor_raw = donor.read_bytes()
    
    # Compare sizes
    print(f"Donor size: {len(donor_raw)}")
    print(f"Gen size:   {len(gen_raw)}")
    print()
    
    # Compare content streams
    _, d_st, d_slen, d_dec = _find_page_stream(donor_raw)
    _, g_st, g_slen, g_dec = _find_page_stream(gen_raw)
    
    d_chunk = donor_raw[d_st:d_st+d_slen]
    g_chunk = gen_raw[g_st:g_st+g_slen]
    
    print(f"Donor stream: compressed={d_slen} hdr={d_chunk[:2].hex()} trail={len(d_dec)-len(d_dec.rstrip())}")
    print(f"Gen stream:   compressed={g_slen} hdr={g_chunk[:2].hex()} trail={len(g_dec)-len(g_dec.rstrip())}")
    print(f"Stream bytes same: {d_chunk == g_chunk}")
    print()
    
    # Compare file sections OUTSIDE content stream
    # Before stream
    before_same = donor_raw[:d_st] == gen_raw[:g_st]
    after_donor = donor_raw[d_st+d_slen:]
    after_gen = gen_raw[g_st+g_slen:]
    print(f"Before stream identical: {before_same} (donor_start={d_st} gen_start={g_st})")
    print(f"After stream identical: {after_donor == after_gen} (donor_after={len(after_donor)} gen_after={len(after_gen)})")
    
    # Find keyword/metadata diffs
    import re
    for pattern in [rb"/Keywords", rb"/CreationDate", rb"/ModDate", rb"/ID"]:
        dm = re.search(pattern + rb"\s*[(\[]([^\n]{0,100})", donor_raw)
        gm = re.search(pattern + rb"\s*[(\[]([^\n]{0,100})", gen_raw)
        dv = dm.group(1)[:60] if dm else b"NOT_FOUND"
        gv = gm.group(1)[:60] if gm else b"NOT_FOUND"
        if dv != gv:
            print(f"DIFF {pattern.decode()}: donor={dv!r}")
            print(f"          gen  ={gv!r}")
        else:
            print(f"SAME {pattern.decode()}")
