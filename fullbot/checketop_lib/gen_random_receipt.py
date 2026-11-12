#!/usr/bin/env python3
"""Генерация случайного PDF-чека с изменёнными полями (по технике бота).

Использование: python3 gen_random_receipt.py [output.pdf]
"""
import random
from pathlib import Path
from datetime import datetime, timedelta

from receipt_db import (
    chars_from_text_fields,
    find_all_donors,
    receipt_supports_chars,
    load_index,
    COMMON_AMOUNTS,
)
from pdf_patcher import patch_amount as pdf_patch_amount, format_amount_display
from vtb_patch_from_config import patch_from_values

# ФИО, которые точно есть в базе (по скану)
KNOWN_NAMES = [
    "Евгений Александрович Е.",
    "Арман Мелсикович Б.",
    "Юлия Константиновна Д.",
    "Елена Ивановна В.",
    "Даниил Алексеевич Т.",
]

BANKS = ["Сбербанк", "ВТБ", "Альфа-Банк"]

IDEAL_DONOR = Path(__file__).parent / "база_чеков" / "vtb" / "СБП" / "Александр Валерьевич М..pdf"


def rand_phone() -> str:
    return f"+7 ({random.randint(900, 999)}) {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}"


def rand_operation_id() -> str:
    prefix = random.choice(["A606", "B606"])
    hex_part = "".join(random.choices("0123456789ABCDEF", k=random.randint(18, 22)))
    return prefix + hex_part


def rand_date() -> str:
    dt = datetime.now() - timedelta(days=random.randint(0, 30))
    return dt.strftime("%d.%m.%Y, %H:%M")


def main():
    import sys
    import subprocess
    args = sys.argv[1:]
    do_report = "--report" in args
    args = [a for a in args if a != "--report"]
    out_path = Path(args[0]) if args else Path("random_receipt.pdf")
    if not out_path.suffix:
        out_path = Path("random_receipt.pdf")

    payer = random.choice(KNOWN_NAMES)
    recipient = random.choice(KNOWN_NAMES)
    phone = rand_phone()
    bank = random.choice(BANKS)
    amount_to = random.choice(COMMON_AMOUNTS)
    date_str = rand_date()
    operation_id = rand_operation_id()

    required = chars_from_text_fields(payer, recipient, phone, bank)
    donors = list(find_all_donors(required, "vtb_sbp"))
    if not donors:
        from receipt_db import get_bank_report
        missing, scanned, _ = get_bank_report(required, "vtb_sbp")
        print(f"❌ Нет донора. Не хватает: {sorted(missing)}")
        return 1

    if IDEAL_DONOR.exists() and receipt_supports_chars(IDEAL_DONOR, required):
        from receipt_db import get_receipt_amount
        ideal_amt = get_receipt_amount(IDEAL_DONOR)
        rest = [d for d in donors if Path(d[0]) != IDEAL_DONOR]
        donors = [(IDEAL_DONOR, ideal_amt)] + rest
    else:
        random.shuffle(donors)
    amounts_to_try = [amount_to] + [a for a in COMMON_AMOUNTS if a != amount_to]
    out_bytes = None
    last_err = None

    for donor_path, amount_from in donors:
        full_path = Path(donor_path)
        data = bytearray(full_path.read_bytes())

        am_list = [amount_from] if amount_from else []
        am_list += [a for a in amounts_to_try if a not in am_list]

        ok_sum, err_sum, new_data = False, None, None
        for am in am_list:
            ok_sum, err_sum, new_data = pdf_patch_amount(data, am, amount_to, bank="vtb")
            if ok_sum and new_data is not None:
                break

        if not ok_sum or new_data is None:
            last_err = err_sum
            continue

        data = bytearray(new_data)
        try:
            out_bytes = patch_from_values(
                data,
                full_path,
                date_str=date_str,
                payer=payer,
                recipient=recipient,
                phone=phone,
                bank=bank,
                amount=None,
                operation_id=operation_id,
                account=None,
            )
        except Exception as e:
            last_err = str(e)
            continue

        # Валидация: PDF должен открываться без syntax error
        try:
            import fitz
            p = fitz.open(stream=bytes(out_bytes), filetype="pdf")
            t = p[0].get_text()
            p.close()
            if len(t) > 200:
                break
        except Exception:
            pass
        out_bytes = None

    if out_bytes is None:
        print(f"❌ Все доноры дали повреждённый PDF. {last_err or 'syntax error in array'}")
        return 1

    Path(out_path).write_bytes(out_bytes)

    print("✅ Сгенерирован:", out_path)
    print(f"   Плательщик: {payer}")
    print(f"   Получатель: {recipient}")
    print(f"   Телефон: {phone}")
    print(f"   Банк: {bank}")
    print(f"   Сумма: {format_amount_display(amount_to)} ₽")
    print(f"   Дата: {date_str}")
    print(f"   ID операции: {operation_id}")

    if do_report:
        print()
        subprocess.run(
            [sys.executable, "report_last_letter_coords.py", str(out_path)],
            cwd=Path(__file__).parent,
        )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
