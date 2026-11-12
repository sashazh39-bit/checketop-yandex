#!/usr/bin/env python3
"""Сгенерировать несколько вариантов чека 15-03-26 для проверки «подделан».

Каждый вариант — разная комбинация: телефон, Document ID, банк, счёт из шаблона.
Запуск: python3 gen_variants_test.py
Выход: чек_вариант_A.pdf ... чек_вариант_Е.pdf
"""
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
TEMPLATE_15 = BASE / "база_чеков" / "vtb" / "СБП" / "15-03-26_00-00.pdf"


def extract_template_data() -> dict:
    """Извлечь данные из 15-03-26."""
    try:
        from receipt_extractor import extract_from_receipt
        ex = extract_from_receipt(TEMPLATE_15)
        return {
            "payer": (ex.get("fio_payer") or "").strip() or "Иван Петрович О.",
            "recipient": (ex.get("fio_recipient") or "").strip() or "Алена Сергеевна С.",
            "amount": ex.get("amount") or 5000,
            "phone": (ex.get("phone_recipient") or "").strip() or "+7 (999) 000-00-00",
            "bank": (ex.get("bank_recipient") or "").strip() or "Сбербанк",
        }
    except Exception:
        return {
            "payer": "Иван Петрович О.",
            "recipient": "Алена Сергеевна С.",
            "amount": 5000,
            "phone": "+7 (999) 000-00-00",
            "bank": "Сбербанк",
        }


def run_gen(out: Path, payer: str, recipient: str, amount: int, date: str,
            phone: str, bank: str | None, account: str | None,
            keep_id: bool, keep_phone: bool, keep_bank: bool) -> bool:
    """Вызов gen_receipt_15_03 с нужными флагами."""
    cmd = [
        sys.executable, str(BASE / "gen_receipt_15_03.py"),
        str(out), "--payer", payer, "--recipient", recipient,
        "--amount", str(amount), "--date", date, "--phone", phone,
    ]
    if bank and not keep_bank:
        cmd.extend(["--bank", bank])
    if account:
        cmd.extend(["--account", account])
    if keep_phone:
        cmd.append("--keep-phone")
    if keep_id:
        cmd.append("--keep-id")
    if keep_bank:
        cmd.append("--keep-bank")
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=90)
    return proc.returncode == 0


def main() -> int:
    if not TEMPLATE_15.exists():
        print(f"[ERROR] Шаблон не найден: {TEMPLATE_15}", file=sys.stderr)
        return 1

    data = extract_template_data()
    from datetime import datetime
    date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")

    # ФИО и сумма — наши (буквы есть в 15-03-26)
    payer = "Иван Петрович О."
    recipient = "Алена Сергеевна С."
    amount = 5000
    phone_tpl = data["phone"]
    bank_tpl = data["bank"]

    variants = [
        ("A", "Телефон+ID+банк из шаблона (только ФИО и сумма наши)", True, True, True),
        ("B", "Телефон+ID из шаблона", True, True, False),
        ("C", "Только телефон из шаблона", False, True, False),
        ("D", "Только ID из шаблона", True, False, False),
        ("E", "Всё из шаблона (телефон, банк), меняем ФИО+сумму+1 символ ID", False, True, True),
    ]

    for key, desc, keep_id, keep_phone, keep_bank in variants:
        out = BASE / f"чек_вариант_{key}.pdf"
        bank = None if keep_bank else bank_tpl
        ok = run_gen(out, payer, recipient, amount, date_str,
                     phone_tpl if keep_phone else "+7 (916) 123-45-67",
                     bank, None, keep_id, keep_phone, keep_bank)
        if ok:
            print(f"✅ Вариант {key}: {out.name}")
            print(f"   {desc}")
        else:
            print(f"❌ Вариант {key}: ошибка")

    print("\nГотово. Проверь каждый вариант в системе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
