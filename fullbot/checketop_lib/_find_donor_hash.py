import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_check_service import _parse_keywords

TBANK = Path('TBANK')

target_hash = 'e99795a0ff8fa5c892e512f513cc37fa'

print(f"Looking for hash: {target_hash}\n")
for d in sorted(TBANK.glob('*.pdf')):
    try:
        res = _parse_keywords(d.read_bytes())
        if res and target_hash in res[1]:
            print(f"FOUND: {d.name}")
            print(f"  parsed: {res}")
    except Exception:
        pass

print("\nAll verified donors and hashes:")
for d in sorted(TBANK.glob('tbank_sbp_verified_*.pdf')):
    if '_enriched' in d.stem:
        continue
    try:
        res = _parse_keywords(d.read_bytes())
        print(f"  {d.name}: {res}")
    except Exception as e:
        print(f"  {d.name}: ERR {e}")
