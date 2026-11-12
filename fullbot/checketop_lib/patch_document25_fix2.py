#!/usr/bin/env python3
"""
Два фикса для document25.04.26 20_52_53.905_patched.pdf:
  1. Выравнивание суммы -19 000,00 RUR: Tm x=525.812 → 510.252 (как у -23 000,00)
  2. Буква М в имени: fitz-оверлей через системный Arial (глифа М нет в embedded font)
"""
import re
import sys
import zlib
import shutil
import tempfile
from pathlib import Path

PDF_IN  = Path("/Users/aleksandrzerebatav/Desktop/document25.04.26 20_52_53.905_patched.pdf")
ARIAL   = "/System/Library/Fonts/Supplemental/Arial.ttf"


# ── Part 1: Fix Tm alignment (stream-level patch) ─────────────────────────

def fix_tm_alignment(in_path: Path, out_path: Path) -> bool:
    """Replace Tm x=525.812 (2nd tx) → 510.252 (same as 1st tx) at y=403.006."""
    OLD_TM = b"525.812 403.006 Tm"
    NEW_TM = b"510.252 403.006 Tm"
    assert len(OLD_TM) == len(NEW_TM), "Tm byte length must match"

    data = bytearray(in_path.read_bytes())
    replaced = 0

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        slen       = int(m.group(2))
        sstart     = m.end()
        len_pos    = m.start(2)
        if sstart + slen > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[sstart:sstart + slen]))
        except zlib.error:
            continue
        if OLD_TM not in dec:
            continue

        new_dec = dec.replace(OLD_TM, NEW_TM)
        new_raw = zlib.compress(new_dec, 6)
        delta   = len(new_raw) - slen

        old_len_str = str(slen).encode()
        new_len_str = str(len(new_raw)).encode()

        data = bytearray(
            bytes(data[:sstart]) + new_raw + bytes(data[sstart + slen:])
        )
        data[len_pos: len_pos + len(old_len_str)] = new_len_str
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        # Update xref offsets
        xref_m = re.search(
            rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
        )
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                if int(em.group(1)) > sstart:
                    entries[em.start(1): em.start(1) + 10] = \
                        f"{int(em.group(1)) + delta:010d}".encode()
            data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

        sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if sxref_m and delta != 0 and sstart < int(sxref_m.group(1)):
            pos = sxref_m.start(1)
            old_pos = int(sxref_m.group(1))
            data[pos: pos + len(str(old_pos))] = str(old_pos + delta).encode()

        replaced += 1
        print(f"[OK] Tm alignment fixed: {OLD_TM.decode()} → {NEW_TM.decode()}")

    out_path.write_bytes(data)
    if replaced == 0:
        print("[WARN] Tm 525.812 not found — already aligned?")
    return True


# ── Part 2: Fix М glyph via fitz overlay ─────────────────────────────────

def fix_M_glyph(in_path: Path, out_path: Path) -> bool:
    """White-out the broken М and redraw 'Алексеев Максим' using system Arial."""
    try:
        import fitz
    except ImportError:
        print("[ERROR] PyMuPDF (fitz) not installed", file=sys.stderr)
        return False

    arial_path = Path(ARIAL)
    if not arial_path.exists():
        print(f"[ERROR] Arial not found: {ARIAL}", file=sys.stderr)
        return False

    # Preserve original /ID
    raw = in_path.read_bytes()
    id_m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", raw)
    orig_id = id_m.group(1) if id_m else None

    doc  = fitz.open(in_path)
    page = doc[0]
    orig_meta = dict(doc.metadata or {})

    # Register Arial (full Cyrillic support)
    try:
        page.insert_font(fontname="arial_fix", fontfile=str(arial_path))
    except Exception as e:
        print(f"[WARN] insert_font: {e}")

    dt = page.get_text("dict")
    fixed = 0
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                # The span with the client name contains "Алексеев" and "аксим"
                if "Алексеев" in text and ("аксим" in text or "?" in text):
                    bbox     = fitz.Rect(span["bbox"])
                    fontsize = float(span.get("size", 8.0))
                    c = int(span.get("color", 0))
                    color = (((c >> 16) & 255)/255.0,
                             ((c >>  8) & 255)/255.0,
                             ( c        & 255)/255.0)

                    # White out the span
                    page.draw_rect(bbox, color=(1,1,1), fill=(1,1,1))

                    # Baseline: bottom of bbox minus small descender offset
                    baseline_y = bbox.y1 - fontsize * 0.18

                    try:
                        page.insert_text(
                            fitz.Point(bbox.x0, baseline_y),
                            "Алексеев Максим",
                            fontname="arial_fix",
                            fontsize=fontsize,
                            color=color,
                        )
                        print(f"[OK] Overlaid 'Алексеев Максим' at {bbox} size={fontsize}")
                        fixed += 1
                    except Exception as e:
                        print(f"[WARN] insert_text failed: {e}", file=sys.stderr)

    doc.set_metadata(orig_meta)
    doc.save(out_path, garbage=0, deflate=True)
    doc.close()

    # Restore original /ID
    if orig_id:
        data = out_path.read_bytes()
        pat  = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")
        def repl(m):
            return m.group(0).replace(m.group(2), orig_id)
        out_path.write_bytes(pat.sub(repl, data, count=1))

    if fixed == 0:
        print("[WARN] Name span not found by fitz — check manually")
    return True


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else PDF_IN
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp  # overwrite in-place

    if not inp.exists():
        print(f"[ERROR] Not found: {inp}", file=sys.stderr)
        return 1

    tmp1 = Path(tempfile.mktemp(suffix=".pdf"))
    tmp2 = Path(tempfile.mktemp(suffix=".pdf"))

    try:
        # Step 1: Tm alignment fix
        print("=== Step 1: Fix Tm alignment ===")
        fix_tm_alignment(inp, tmp1)

        # Step 2: М glyph fix via fitz
        print("\n=== Step 2: Fix М glyph (fitz overlay) ===")
        fix_M_glyph(tmp1, tmp2)

        shutil.move(str(tmp2), str(out))
        print(f"\n[OK] Saved: {out}")
        return 0
    finally:
        for t in (tmp1, tmp2):
            if t.exists():
                try: t.unlink()
                except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
