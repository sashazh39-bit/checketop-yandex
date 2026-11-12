import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from gen_tbank_receipt import _renderable_f2_chars_for

d = Path('TBANK/tbank_sbp_verified_05052026.pdf')
f2_chars = _renderable_f2_chars_for(d)
print(f'F2 chars type: {type(f2_chars)}')
print(f'F2 chars: {f2_chars}')

needed_digits = {0x34, 0x30}  # '4' and '0'
print(f'needed_digits: {needed_digits}')
print(f'f2_chars is not None: {f2_chars is not None}')
print(f'needed_digits not empty: {bool(needed_digits)}')
if f2_chars is not None:
    print(f'not issubset: {not needed_digits.issubset(f2_chars)}')
    print(f'0x34 in f2_chars: {0x34 in f2_chars}')
    print(f'0x30 in f2_chars: {0x30 in f2_chars}')
    
# The condition: f2_chars is not None AND needed_digits AND not needed_digits.issubset(f2_chars)
condition = f2_chars is not None and needed_digits and not needed_digits.issubset(f2_chars)
print(f'\nSwap condition: {condition}')
print('Should swap to enriched:', condition)

# Now test with amount_bold='4 000 ' - extract only digits
text_fields = {'amount_bold': '4 000 ', 'amount_small': '4 000 '}
needed_digits2 = set()
for key in ("amount_bold", "amount_small"):
    for ch in str(text_fields.get(key, "")):
        if ch.isdigit():
            needed_digits2.add(ord(ch))
print(f'\nneeded_digits from fields: {needed_digits2} (chars: {[chr(c) for c in needed_digits2]})')
condition2 = f2_chars is not None and needed_digits2 and not needed_digits2.issubset(f2_chars)
print(f'Swap condition2: {condition2}')
