"""One-shot: generate an SBP receipt with fixed recipient/phone, random rest."""
import sys, random, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from gen_sbp_receipt import (
    generate_sbp_receipt,
    _load_donor_info,
    _is_genuine_pdf,
    _min_p23_for_date,
    SBP_DONORS_DIR,
    random_phone,
    random_account,
)

RECIPIENT = "Роман Андреевич В."
PHONE     = "+79118584552"

now_local = datetime.now()
op_date = now_local.strftime("%d.%m.%Y")
op_time = now_local.strftime("%H:%M:%S")

amount = random.choice([
    random.randint(100, 999),
    random.randint(1000, 9999),
    random.randint(10000, 50000),
])
amount = round(amount / 50) * 50 or 100

account = random_account()

today_min_p23 = _min_p23_for_date(now_local.year, now_local.month, now_local.day)

# Find donors that have the required characters for RECIPIENT
RECIPIENT_CPS = {ord(c) for c in RECIPIENT.replace(" ", "\xa0") if ord(c) >= 0x20}

candidates = [p for p in sorted(SBP_DONORS_DIR.glob("*.pdf")) if _is_genuine_pdf(p)]
print(f"Total donors: {len(candidates)}")

fresh_donors = []
any_donors = []
for p in candidates:
    info = _load_donor_info(p)
    if not info or not info.get("recipient") or not info.get("bank"):
        continue
    if not info.get("operation_id", "").startswith("C16"):
        continue
    # Check p23 era
    try:
        p23_ok = int(info.get("sbp_p23", "0")) >= today_min_p23
    except (ValueError, TypeError):
        p23_ok = False

    avail = info.get("avail_cps", set())
    chars_ok = RECIPIENT_CPS.issubset(avail)

    if p23_ok:
        if chars_ok:
            fresh_donors.append((p, info, True))
        else:
            fresh_donors.append((p, info, False))
    else:
        any_donors.append((p, info, chars_ok))

# Prioritize: fresh + chars_ok > fresh + no chars > stale + chars_ok > rest
def sort_key(t):
    p, info, chars_ok = t
    try:
        p23_ok = int(info.get("sbp_p23", "0")) >= today_min_p23
    except Exception:
        p23_ok = False
    return (0 if (p23_ok and chars_ok) else (1 if p23_ok else (2 if chars_ok else 3)),)

all_sorted = sorted(fresh_donors + any_donors, key=sort_key)

donor_path = None
donor_bank = None
glyph_source = None

for p, info, chars_ok in all_sorted:
    donor_path = p
    donor_bank = info["bank"]
    if not chars_ok:
        # Need font surgery
        font_candidates = [
            Path(__file__).parent / "fonts" / "tahoma.ttf",
            Path(__file__).parent / "fonts" / "Tahoma.ttf",
        ]
        for fc in font_candidates:
            if fc.exists():
                glyph_source = str(fc)
                print(f"Font surgery needed for {p.name}, using {fc.name}")
                break
        if not glyph_source:
            print(f"Skipping {p.name}: missing chars and no glyph_source font found")
            donor_path = None
            continue
    print(f"Selected donor: {p.name} (bank={donor_bank!r}, chars_ok={chars_ok})")
    break

if not donor_path:
    print("ERROR: no suitable donor found!")
    sys.exit(1)

out_path = Path(__file__).parent / "test_generated.pdf"

print(f"Generating: amount={amount}, date={op_date} {op_time}, recipient={RECIPIENT!r}, phone={PHONE!r}")
pdf_bytes, fname = generate_sbp_receipt(
    amount=amount,
    recipient=RECIPIENT,
    phone=PHONE,
    bank=donor_bank,
    operation_date=op_date,
    operation_time=op_time,
    account=account,
    donor_path=donor_path,
    output_path=out_path,
    glyph_source=glyph_source,
)

print(f"Generated: {out_path}  ({len(pdf_bytes):,} bytes)")
print(f"Canonical name: {fname}")
