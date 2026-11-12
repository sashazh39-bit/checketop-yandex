#!/usr/bin/env python3
"""Генерация чека с заданными полями.

Координаты: одна формула в vtb_patch_from_config — tm_x = wall - n×pts.
pts из блока донора: (wall - tm_x_старого) / n. Счёт не меняется (account=None).
Метаданные: уникальный /ID для загрузки в ТГ-бота.

Использование:
  python3 gen_custom_receipt.py
  python3 gen_custom_receipt.py output.pdf
  python3 gen_custom_receipt.py output.pdf --report   # + отчёт выравнивания
"""
import hashlib
import sys
from datetime import datetime
from pathlib import Path

from receipt_db import get_receipt_amount, receipt_supports_chars, chars_from_text_fields, find_all_donors
from pdf_patcher import patch_amount as pdf_patch_amount, format_amount_display
from vtb_patch_from_config import patch_from_values

BASE = Path(__file__).parent
IDEAL_DONOR = BASE / "база_чеков" / "vtb" / "СБП" / "Александр Валерьевич М..pdf"  # приоритет, если поддержан


def unique_operation_id() -> str:
    """Уникальный ID операции для метаданных."""
    h = hashlib.sha256(f"{datetime.now().isoformat()}{id(object())}".encode()).hexdigest().upper()
    return "A606" + h[:20]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    do_report = "--report" in sys.argv
    out_path = Path(args[0]) if args else BASE / "receipt_10000.pdf"
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    payer = "Алла Олеговна А."
    recipient = "Александр Олегович О."
    phone = "+7 (916) 784-52-19"
    bank = "Сбербанк"
    amount = 10000
    date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    operation_id = unique_operation_id()

    required = chars_from_text_fields(payer, recipient, phone, bank)
    donors = list(find_all_donors(required, "vtb_sbp"))
    if not donors:
        from receipt_db import get_bank_report
        missing, _, _ = get_bank_report(required, "vtb_sbp")
        print(f"❌ Нет донора. Не хватает: {sorted(missing)}")
        return 1

    if IDEAL_DONOR.exists() and receipt_supports_chars(IDEAL_DONOR, required):
        donor_path = IDEAL_DONOR
    else:
        donor_path = Path(donors[0][0])
    amount_from = get_receipt_amount(donor_path) or donors[0][1] or 180
    if amount_from is None:
        amount_from = 180

    data = bytearray(donor_path.read_bytes())
    ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount, bank="vtb")
    if not ok_sum or new_data is None:
        print(f"❌ Ошибка суммы: {err_sum}")
        return 1

    data = bytearray(new_data)
    try:
        out_bytes = patch_from_values(
            data,
            donor_path,
            date_str=date_str,
            payer=payer,
            recipient=recipient,
            phone=phone,
            bank=bank,
            amount=amount,
            operation_id=operation_id,
            account=None,
            keep_metadata=False,
        )
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    out_path.write_bytes(out_bytes)
    print("✅ Сгенерирован:", out_path)
    print(f"   Плательщик: {payer}")
    print(f"   Получатель: {recipient}")
    print(f"   Телефон: {phone}")
    print(f"   Банк: {bank}")
    print(f"   Сумма: {format_amount_display(amount)} ₽")
    print(f"   Дата: {date_str}")
    print(f"   ID операции: {operation_id}")
    print(f"   /ID в метаданных: уникальный")
    if do_report:
        import subprocess
        print()
        subprocess.run([sys.executable, "report_last_letter_coords.py", str(out_path)], cwd=BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
