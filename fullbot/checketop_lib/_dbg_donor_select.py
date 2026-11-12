import sys, random
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from tbank_check_service import get_renderable_chars

# Monkey-patch _find_donor to add debug
import gen_tbank_receipt as gnr

_orig_find = gnr._find_donor

def _debug_find_donor(*args, **kwargs):
    receipt_type = args[0] if args else kwargs.get('receipt_type', 'sbp_normal')
    text_fields = args[1] if len(args) > 1 else kwargs.get('text_fields', {})
    preserve_integrity = kwargs.get('preserve_integrity', False)
    
    # Build the text_fields for 4000/Газпромбанк
    print(f"\nDEBUG _find_donor: type={receipt_type} fields={text_fields}")
    
    result = _orig_find(*args, **kwargs)
    print(f"SELECTED: {result.name if result else None}")
    return result

gnr._find_donor = _debug_find_donor

from gen_tbank_receipt import generate_tbank_receipt

pdf_bytes, fname = generate_tbank_receipt(
    amount=4000,
    sender_name="Дмитрий Дроздов",
    sender_account="408178100000****2345",
    recipient_name="Степан С.",
    recipient_phone="+79118570885",
    recipient_bank="Газпромбанк",
    operation_date="31.05.2026",
    operation_time="13:40:23",
    preserve_integrity=True,
)
print(f"\nGenerated size: {len(pdf_bytes)}")
