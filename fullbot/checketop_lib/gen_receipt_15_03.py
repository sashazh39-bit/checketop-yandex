#!/usr/bin/env python3
"""Генерация чека на шаблоне 15-03-26_00-00.pdf с максимумом целостности.

Два режима:
1. Контент-патч (без инъекции глифов) — если все буквы ФИО есть в 15-03-26.
   → Целостность сохраняется, operation_id из шаблона.
2. add_glyphs (с инъекцией) — если нужны буквы М, Ф, Э и др.
   → operation_id сохраняется из 15-03-26 (--keep-operation-id).

Буквы, которых НЕТ в 15-03-26: Г З Й К Л М Н Р У Ф Х Ц Ч Ш Щ Ъ Ы Ь Э Ю Я

Использование:
  python3 gen_receipt_15_03.py чек.pdf --payer "Максим Андреевич В." --recipient "Алан Петрович Е." --amount 15000
  python3 gen_receipt_15_03.py чек.pdf --payer "Иван Петрович О." --amount 5000  # без М → контент-патч
"""
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
TEMPLATE_15 = BASE / "база_чеков" / "vtb" / "СБП" / "15-03-26_00-00.pdf"
ADD_GLYPHS = BASE / "add_glyphs_to_13_03.py"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Чек на шаблоне 15-03-26 с целостностью")
    ap.add_argument("output", nargs="?", default="чек_15_03.pdf")
    ap.add_argument("--payer", "-p", default="Максим Андреевич В.")
    ap.add_argument("--recipient", "-r", default="Алан Петрович Е.")
    ap.add_argument("--amount", "-a", type=int, default=15000)
    ap.add_argument("--bank", "-b", default="Сбербанк")
    ap.add_argument("--phone", default="+7 (916) 123-45-67")
    ap.add_argument("--account", default=None, help="4 цифры счёта")
    ap.add_argument("--date", default="now")
    ap.add_argument("--keep-phone", action="store_true", help="Оставить телефон из шаблона 15-03-26")
    ap.add_argument("--keep-id", action="store_true", help="Не менять Document ID (оставить как в шаблоне)")
    ap.add_argument("--keep-bank", action="store_true", help="Оставить банк и счёт из шаблона")
    args = ap.parse_args()

    # --keep-phone: взять телефон из шаблона (может помочь против «подделан»)
    phone = args.phone
    if args.keep_phone and TEMPLATE_15.exists():
        try:
            from receipt_extractor import extract_from_receipt
            ex = extract_from_receipt(TEMPLATE_15)
            phone = (ex.get("phone_recipient") or "").strip() or phone
            if phone != args.phone:
                print("[INFO] --keep-phone: телефон из шаблона:", phone)
        except Exception:
            pass

    out_path = Path(args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    if not TEMPLATE_15.exists():
        print(f"[ERROR] Шаблон не найден: {TEMPLATE_15}", file=sys.stderr)
        return 1

    if args.date.lower() == "now":
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    else:
        date_str = args.date

    # Проверка: все ли буквы есть в 15-03-26?
    try:
        from receipt_db import chars_from_text_fields, get_missing_chars_in_receipt
        required = chars_from_text_fields(args.payer, args.recipient, phone)
        missing = get_missing_chars_in_receipt(TEMPLATE_15, required)
    except Exception:
        missing = {"?"}  # при ошибке — идём в add_glyphs

    if not missing:
        # Режим 1: контент-патч (без инъекции) → целостность сохраняется
        print("[INFO] Все буквы в 15-03-26 — контент-патч (без инъекции глифов)")
        try:
            from gen_verified_receipt import main as gen_verified_main
            # Запускаем gen_verified_receipt с нужными аргументами
            sys.argv = [
                "gen_verified_receipt.py", str(out_path),
                "--donor", str(TEMPLATE_15),
                "--payer", args.payer, "--recipient", args.recipient,
                "--amount", str(args.amount), "--phone", phone,
                "--date", date_str,
            ]
            if not args.keep_bank:
                sys.argv.extend(["--bank", args.bank])
            if args.account and re.match(r"^\d{4}$", args.account) and not args.keep_bank:
                sys.argv.extend(["--account", args.account])
            if args.keep_id:
                sys.argv.append("--keep-id")
            return gen_verified_main()
        except Exception as e:
            print(f"[ERROR] gen_verified_receipt: {e}", file=sys.stderr)
            return 1

    # Режим 2: add_glyphs с --keep-operation-id (operation_id из 15-03-26)
    print(f"[INFO] Нет в 15-03-26: {''.join(sorted(missing))} — add_glyphs с --keep-operation-id")
    date_part, _, time_part = date_str.partition(", ")
    if not time_part:
        time_part = datetime.now().strftime("%H:%M")

    # --keep-bank: взять банк из шаблона (для варианта A и др.)
    bank = args.bank
    account = args.account
    if args.keep_bank and TEMPLATE_15.exists():
        try:
            from receipt_extractor import extract_from_receipt
            ex = extract_from_receipt(TEMPLATE_15)
            bank = (ex.get("bank_recipient") or "").strip() or bank
            acc = (ex.get("account_recipient") or "").strip()
            if re.search(r"\d{4}", acc or ""):
                account = re.search(r"\d{4}", acc).group(0)
            if bank != args.bank:
                print("[INFO] --keep-bank: банк из шаблона:", bank)
        except Exception:
            pass

    cmd = [
        sys.executable, str(ADD_GLYPHS),
        "--target", str(TEMPLATE_15),
        "--id-from", str(TEMPLATE_15),
        "--keep-operation-id",
        "--replace", "--hybrid-safe",
        "--payer", args.payer, "--recipient", args.recipient,
        "--bank", bank, "--amount", str(args.amount),
        "--phone", phone,
        "--date", date_part, "--time", time_part,
        "-o", str(out_path),
    ]
    if account and re.match(r"^\d{4}$", account):
        cmd.extend(["--account", account])
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:600]
        print(f"[ERROR] add_glyphs: {err}", file=sys.stderr)
        return 1
    print("✅ Готово:", out_path)
    print("   Шаблон: 15-03-26_00-00.pdf")
    print("   operation_id: из шаблона (--keep-operation-id)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
