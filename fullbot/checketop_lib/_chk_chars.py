#!/usr/bin/env python3
"""Find donors that have 'б' in original text (=> CID 269 in ToUnicode)."""
import fitz
from pathlib import Path

from tbank_check_service import _parse_keywords, get_renderable_chars

TARGET_CHARS = set("Сбербанк Альфа-Банк ВТБ Тинькофф Газпром Раифф Дмитрий Дроздов Степан")

for d in sorted(Path("TBANK").glob("tbank_sbp_verified_*.pdf")):
    if "_enriched" in d.stem:
        continue
    doc = fitz.open(d)
    page = doc[0]
    # extract all chars via rawdict
    chars_in_doc = set()
    texts = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = ""
                for ch_info in span.get("chars", []):
                    c = ch_info.get("c", "")
                    chars_in_doc.add(c)
                    t += c
                if t.strip():
                    texts.append(t.strip())
    doc.close()
    
    # Check key chars
    has_b_lower = "б" in chars_in_doc
    has_A_upper = "А" in chars_in_doc
    has_D_upper = "Д" in chars_in_doc
    has_soft = "ь" in chars_in_doc
    
    kw = _parse_keywords(d.read_bytes())
    print(f"{d.name}:")
    print(f"  б={has_b_lower} А={has_A_upper} Д={has_D_upper} ь={has_soft}")
    print(f"  sample: {texts[1:4]}")
    print()
