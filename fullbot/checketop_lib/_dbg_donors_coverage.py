import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from gen_tbank_receipt import _renderable_chars_for
from tbank_check_service import extract_fields

TBANK = Path('TBANK')

# Which chars do we need for each bank?
banks = {
    'Сбербанк': set('Сбербанк'),
    'Газпромбанк': set('Газпромбанк'),
    'Альфа-Банк': set('АльфаБнк-'),
    'ВТБ': set('ВТБ'),
    'Тинькофф': set('Тинькофф'),
}

print("=== Donor F1 coverage for bank names ===\n")

# Check only verified enriched donors
for d in sorted(TBANK.glob('tbank_sbp_verified_*_enriched.pdf')):
    f1 = _renderable_chars_for(d)
    if f1 is None:
        print(f"{d.name}: F1=None (skip check)")
        continue
    
    try:
        fields = extract_fields(d, 'sbp_normal')
        orig_bank = fields.get('bank', '?')
    except:
        orig_bank = '?'
    
    can_render = []
    missing_info = {}
    for bank_name, bank_chars in banks.items():
        missing = bank_chars - {chr(c) for c in f1}
        if not missing:
            can_render.append(bank_name)
        else:
            missing_info[bank_name] = sorted(missing)
    
    if 'Газпромбанк' not in can_render:
        gazp_miss = missing_info.get('Газпромбанк', [])
        print(f"  {d.name}: orig_bank={repr(orig_bank)}, MISSING for Газпромбанк: {gazp_miss}")
    else:
        print(f"  {d.name}: CAN render Газпромбанк ✓ (orig_bank={repr(orig_bank)})")
