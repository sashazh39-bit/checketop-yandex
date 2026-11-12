#!/usr/bin/env python3
"""
Чистый патч имени: убираем все fitz-наслоения, меняем CID-имя на нижний регистр.

Изменения:
  - Удаляем fitz-добавленные потоки (белый rect + Arial-текст)
  - В оригинальном CID-потоке: А(002E)→а(000A) и М(041C)→м(0008)
  - Итог: "алексеев максим" — чистый CID без оверлеев, глифы есть в шрифте
"""
import re
import sys
import fitz
from pathlib import Path

PDF = Path("/Users/aleksandrzerebatav/Desktop/document25.04.26 20_52_53.905_patched.pdf")

# Имя в CID-потоке: "Алексеев Максим" → "алексеев максим"
#   А(002E)→а(000A), остальные CIDs те же, М(041C)→м(0008)
OLD_NAME = b"<002E000D0010001C00130010001000090003041C000A001C001300070008>"
NEW_NAME = b"<000A000D0010001C001300100010000900030008000A001C001300070008>"
assert len(OLD_NAME) == len(NEW_NAME)


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else PDF
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src

    if not src.exists():
        print(f"[ERROR] Not found: {src}", file=sys.stderr)
        return 1

    # Preserve original /ID
    raw = src.read_bytes()
    id_m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", raw)
    orig_id = id_m.group(1) if id_m else None

    doc = fitz.open(src)
    page = doc[0]
    xrefs = page.get_contents()
    print(f"Content streams: {len(xrefs)} → {xrefs}")

    # Identify original Oracle stream vs fitz-added overlay streams
    orig_xrefs = []
    fitz_xrefs = []
    for xref in xrefs:
        s = doc.xref_stream(xref)  # fitz returns decompressed
        # Oracle BI Publisher streams contain BT…ET text blocks with CID pairs
        if b"BT" in s and (b"002E000D" in s or b"000A000D" in s or b"041C" in s
                           or b"0010001C" in s):
            orig_xrefs.append(xref)
        elif b"arial_fix" in s or b"1 1 1 rg" in s or b"1 1 1 RG" in s:
            # fitz drawing/text overlay (white rect or Arial text)
            fitz_xrefs.append(xref)
        else:
            orig_xrefs.append(xref)  # keep any other original streams

    print(f"  Original streams: {orig_xrefs}")
    print(f"  Fitz overlay streams (to remove): {fitz_xrefs}")

    # ── Step 1: Remove fitz overlay streams from /Contents ────────────────
    if fitz_xrefs:
        new_contents = " ".join(f"{x} 0 R" for x in orig_xrefs)
        doc.xref_set_key(page.xref, "Contents", f"[{new_contents}]")
        print(f"[OK] Removed {len(fitz_xrefs)} fitz stream(s) from /Contents")
    else:
        print("[INFO] No fitz overlay streams found")

    # ── Step 2: Fix name CIDs in original Oracle stream ──────────────────
    oracle_xref = next((x for x in orig_xrefs
                        if b"BT" in doc.xref_stream(x)), None)
    if oracle_xref is None:
        print("[ERROR] Oracle content stream not found", file=sys.stderr)
        return 1

    stream = doc.xref_stream(oracle_xref)
    if OLD_NAME in stream:
        new_stream = stream.replace(OLD_NAME, NEW_NAME)
        doc.update_stream(oracle_xref, new_stream)
        print(f"[OK] CID name fixed in xref={oracle_xref}: Алексеев Максим → алексеев максим")
    elif NEW_NAME in stream:
        print("[INFO] Name already lowercase in CID stream")
    else:
        print(f"[WARN] Name CID sequence not found in xref={oracle_xref}")
        # Debug: look for partial match
        if b"002E000D" in stream:
            print("      Found start of name (002E=А present)")
        if b"041C" in stream:
            print("      Found broken М (CID 041C present)")

    # ── Step 3: Save with garbage collection (cleans unused Arial font etc) ─
    import tempfile, shutil
    tmp = Path(tempfile.mktemp(suffix=".pdf"))
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    shutil.move(str(tmp), str(dst))

    # Restore original /ID
    if orig_id:
        data = dst.read_bytes()
        pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")
        def repl(m):
            return m.group(0).replace(m.group(2), orig_id)
        dst.write_bytes(pat.sub(repl, data, count=1))

    print(f"\n[OK] Saved: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
