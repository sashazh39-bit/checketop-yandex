import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_check_service import extract_fields, get_renderable_chars

TBANK = Path('TBANK')

# Bank names we need to support
test_banks = ['Сбербанк', 'Газпромбанк', 'Альфа-Банк', 'ВТБ', 'Тинькофф']
# Chars needed for each bank
bank_chars = {}
for bank in test_banks:
    bank_chars[bank] = set(bank)

print("=== Donor bank names and F1 glyph coverage ===\n")
for d in sorted(TBANK.glob('tbank_sbp_verified_*_enriched.pdf')):
    try:
        fields = extract_fields(d, 'sbp_normal')
        orig_bank = fields.get('bank', '?')
    except Exception as e:
        orig_bank = f"ERR:{e}"
    
    try:
        renderable = get_renderable_chars(d)
    except Exception as e:
        renderable = set()
        print(f"  {d.name}: get_renderable_chars error: {e}")
    
    # Check which bank names can be rendered
    can_render = []
    for bank, chars in bank_chars.items():
        missing = chars - {chr(c) for c in renderable}
        if not missing:
            can_render.append(bank)
        else:
            pass  # has missing chars
    
    missing_sber = {chr(c) for c in (set(ord(c) for c in 'Сбербанк') - renderable)}
    missing_gazp = {chr(c) for c in (set(ord(c) for c in 'Газпромбанк') - renderable)}
    
    print(f"{d.name}:")
    print(f"  orig_bank={repr(orig_bank)}")
    print(f"  can_render: {can_render}")
    if missing_sber:
        print(f"  missing for Сбербанк: {missing_sber}")
    if missing_gazp:
        print(f"  missing for Газпромбанк: {missing_gazp}")
    print()
