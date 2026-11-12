import sys, fitz
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_check_service import extract_fields

# Check what k4 donor contains
d = Path('TBANK/tbank_sbp_verified_31052026_k4_enriched.pdf')
fields = extract_fields(d, 'sbp_normal')
print("=== k4 donor fields (via extract_fields) ===")
for k, v in fields.items():
    print(f"  {k}: {repr(v)}")

# Check fitz decoding of k4 donor
doc = fitz.open(d)
page = doc[0]
print("\n=== k4 fitz all text spans ===")
for block in page.get_text('dict')['blocks']:
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            txt = span.get('text', '')
            if txt.strip():
                print(f"  [{txt}]")
doc.close()
