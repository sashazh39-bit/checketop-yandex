#!/usr/bin/env python3
"""Чек с временем +1 час. Минимальное изменение: только дата/время в контенте.

Шаблон 15-03-26_00-00.pdf проходит проверку. Меняем только время на час позже —
остальное (ФИО, сумма, телефон, ID) без изменений. Целостность сохраняется.

Запуск: python3 gen_time_plus1h.py
Выход: чек_15_03_плюс_час.pdf
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
TEMPLATE = BASE / "база_чеков" / "vtb" / "СБП" / "15-03-26_00-00.pdf"


def main() -> int:
    if not TEMPLATE.exists():
        print(f"[ERROR] Шаблон не найден: {TEMPLATE}", file=sys.stderr)
        return 1

    from receipt_extractor import extract_from_receipt
    ex = extract_from_receipt(TEMPLATE)

    payer = (ex.get("fio_payer") or "").strip() or "Анна Петрова С."
    recipient = (ex.get("fio_recipient") or "").strip() or "Олег Дмитриевич В."
    phone = (ex.get("phone_recipient") or "").strip() or "+7 (999) 000-00-00"
    bank = (ex.get("bank_recipient") or "").strip()
    amount = ex.get("amount") or 5000
    account = ex.get("account_last4") or ""
    if not account and ex.get("account"):
        import re
        m = re.search(r"\d{4}", str(ex.get("account", "")))
        if m:
            account = m.group(0)
    date_str_orig = ((ex.get("date") or "") + ", " + (ex.get("time") or "00:00")).strip(", ")

    try:
        dt = datetime.strptime(date_str_orig.strip(), "%d.%m.%Y, %H:%M")
    except ValueError:
        # Fallback: дата из имени файла или сейчас
        dt = datetime.now()

    dt_new = dt + timedelta(hours=1)
    date_str_new = dt_new.strftime("%d.%m.%Y, %H:%M")
    print(f"[INFO] Было: {date_str_orig.strip()}")
    print(f"[INFO] Стало: {date_str_new} (+1 ч)")

    out_path = BASE / "чек_15_03_плюс_час.pdf"

    from gen_verified_receipt import main as gen_main

    sys.argv = [
        "gen_verified_receipt.py", str(out_path),
        "--donor", str(TEMPLATE),
        "--payer", payer, "--recipient", recipient,
        "--phone", phone, "--amount", str(amount),
        "--date", date_str_new,
        # БЕЗ --keep-id: меняем 1 символ в Document ID (иначе «подделка» при том же ID и другом контенте)
    ]
    if bank:
        sys.argv.extend(["--bank", bank])
    if account and len(account) >= 4:
        sys.argv.extend(["--account", account[-4:]])

    return gen_main()


if __name__ == "__main__":
    sys.exit(main())
