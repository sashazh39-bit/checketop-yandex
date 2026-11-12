"""Create a universal donor PDF with ALL common Cyrillic+Latin chars via font surgery.
Run locally where fonttools is installed. Outputs СБП/_universal_donor.pdf
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from gen_sbp_receipt import _cmap_from_pdf, _chars_missing, SBP_DONORS_DIR, _is_genuine_pdf
from font_extend import extend_font_in_pdf

# All chars we want covered
TARGET_CHARS = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " .,()-+:\xa0"
)

TAHOMA = Path(__file__).parent / "fonts" / "tahoma.ttf"
OUT = SBP_DONORS_DIR / "_universal_donor.pdf"

# Pick the best base donor (fewest missing chars)
donors = [p for p in sorted(SBP_DONORS_DIR.glob("*.pdf"))
          if _is_genuine_pdf(p) and p.name != "_universal_donor.pdf"]

best_donor = None
best_missing = 9999
for p in donors:
    try:
        raw = p.read_bytes()
        cmap = _cmap_from_pdf(raw)
        m = _chars_missing(cmap, TARGET_CHARS)
        if len(m) < best_missing:
            best_missing = len(m)
            best_donor = p
    except Exception:
        pass

print(f"Base donor: {best_donor.name} (missing {best_missing} chars)")

# Run font surgery to add all missing chars
pdf_bytes = best_donor.read_bytes()
missing = _chars_missing(_cmap_from_pdf(pdf_bytes), TARGET_CHARS)
print(f"Adding {len(missing)} chars: {sorted(missing)[:20]}...")

pdf_out, cmap_out = extend_font_in_pdf(pdf_bytes, TARGET_CHARS, glyph_source=str(TAHOMA))
still_missing = _chars_missing(cmap_out, TARGET_CHARS)
if still_missing:
    print(f"WARNING: still missing after surgery: {sorted(still_missing)}")
else:
    print("All chars covered!")

OUT.write_bytes(pdf_out)
print(f"Saved: {OUT} ({len(pdf_out):,} bytes)")
