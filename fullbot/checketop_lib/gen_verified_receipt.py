#!/usr/bin/env python3
"""Генерация чека по критериям прохождения проверки (CHECK_VERIFICATION_RULES.md).

Использует проверенные доноры и применяет только разрешённые изменения:
- дата/время, ФИО, сумма, телефон
- Document ID: меняется ровно 1 символ
- CreationDate синхронизируется с датой в чеке

Использование:
  python3 gen_verified_receipt.py output.pdf
  python3 gen_verified_receipt.py --donor "Андрей Викторович С..pdf" --payer "..." ...
"""
import re
import sys
from datetime import datetime
from pathlib import Path

from vtb_patch_from_config import patch_from_values
from vtb_test_generator import update_creation_date

BASE = Path(__file__).parent

# Доноры только в база_чеков/vtb/СБП
DONORS_DIR = BASE / "база_чеков" / "vtb" / "СБП"
UNIVERSAL_DONOR = BASE / "universal_donor.pdf"  # См. build_universal_donor.py


def _decimal_safe_incs(hex_char: str) -> list[int]:
    """Инкременты 1..15, результат — цифра 0-9. Верификатор VTB требует decimal в pos=0."""
    base = int(hex_char.upper(), 16)
    return [i for i in range(1, 16) if (base + i) % 16 < 10]


def change_one_char_in_id(data: bytearray) -> None:
    """Изменить ровно один символ в /ID. pos=0, результат 0-9 (как в add_glyphs — VTB верификатор)."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    hex1 = id_m.group(1).decode().upper()
    pos = 0
    incs = _decimal_safe_incs(hex1[pos])
    if not incs:
        return
    inc = incs[0]
    idx = "0123456789ABCDEF".find(hex1[pos])
    new_c = "0123456789ABCDEF"[(idx + inc) % 16]
    new1 = hex1[:pos] + new_c + hex1[pos + 1:]
    slot_len = id_m.end(1) - id_m.start(1)
    new_enc = new1.encode().ljust(slot_len)[:slot_len]
    data[id_m.start(1) : id_m.end(1)] = new_enc
    data[id_m.start(2) : id_m.end(2)] = new_enc


def get_donors_from_base() -> list[Path]:
    """Все доноры из база_чеков/vtb/СБП."""
    if not DONORS_DIR.exists():
        return []
    return sorted(DONORS_DIR.glob("*.pdf"))


def find_donor_with_chars(required_chars: set[str]) -> Path | None:
    """Найти донора с нужными символами из база_чеков/vtb/СБП."""
    try:
        from receipt_db import get_receipt_chars, _normalize_char
    except ImportError:
        donors = get_donors_from_base()
        return donors[0] if donors else None
    req = {_normalize_char(c) for c in required_chars}
    for p in get_donors_from_base():
        ch = get_receipt_chars(p)
        if not req or req <= ch:
            return p
    return None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Генерация чека по критериям CHECK_VERIFICATION_RULES.md")
    ap.add_argument("output", nargs="?", default="receipt_verified.pdf", help="Выходной PDF")
    ap.add_argument("--donor", "-d", help="Явный путь к донору")
    ap.add_argument("--template", "-t", help="Проходящий чек как шаблон (то же что --donor, приоритет над universal)")
    ap.add_argument("--payer", "-p", default="Алексей Евгеньевич А.", help="ФИО получателя")
    ap.add_argument("--recipient", "-r", default="Роман Алексеевич А.", help="ФИО отправителя")
    ap.add_argument("--phone", default="+7 (992) 494-94-95", help="Телефон")
    ap.add_argument("--amount", "-a", type=int, default=14600, help="Сумма")
    ap.add_argument("--message", "-m", help="Сообщение получателю (заменит тестовый текст)")
    ap.add_argument("--date", default=None, help="Дата DD.MM.YYYY, HH:MM или 'now'")
    ap.add_argument("--operation-id", "-o", help="ID операции СБП (по умолчанию — из шаблона)")
    ap.add_argument("--bank", "-b", help="Банк получателя (напр. Сбербанк, Т-Банк)")
    ap.add_argument("--account", help="4 цифры счёта (напр. 9483)")
    ap.add_argument("--keep-id", action="store_true", help="Не менять Document ID (для теста: оставить как в шаблоне)")
    ap.add_argument("--report", action="store_true", help="Вывести отчёт выравнивания")
    args = ap.parse_args()

    out_path = Path(args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    if args.date and args.date.lower() == "now":
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    elif args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")

    meta_date = datetime.strptime(date_str, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")

    donor_path = None
    if args.template:
        donor_path = Path(args.template).expanduser().resolve()
        if not donor_path.exists():
            print(f"[ERROR] Шаблон не найден: {donor_path}", file=sys.stderr)
            return 1
    elif args.donor:
        donor_path = Path(args.donor).expanduser()
        if not donor_path.is_absolute() and not donor_path.exists():
            donor_path = DONORS_DIR / donor_path.name
        if not donor_path.exists():
            donor_path = BASE / donor_path.name
        if not donor_path.exists():
            print(f"[ERROR] Донор не найден: {donor_path}", file=sys.stderr)
            return 1
    if donor_path is None:
        # Сначала пробуем универсальный донор (если есть)
        if UNIVERSAL_DONOR.exists():
            try:
                from receipt_db import chars_from_text_fields, get_receipt_chars, _normalize_char
                required = chars_from_text_fields(args.payer, args.recipient, args.phone, args.message or "")
                req = {_normalize_char(c) for c in required}
                univ_chars = get_receipt_chars(UNIVERSAL_DONOR)
                if req <= univ_chars:
                    donor_path = UNIVERSAL_DONOR
                else:
                    donor_path = None
            except Exception:
                donor_path = None
        else:
            donor_path = None
        if not donor_path:
            try:
                from receipt_db import chars_from_text_fields
                required = chars_from_text_fields(args.payer, args.recipient, args.phone, args.message or "")
                donor_path = find_donor_with_chars(required)
            except Exception:
                donor_path = None
        if not donor_path:
            donors = get_donors_from_base()
            donor_path = donors[0] if donors else None
        if not donor_path or not donor_path.exists():
            msg = "[ERROR] Нет донора с нужными буквами."
            if not UNIVERSAL_DONOR.exists():
                msg += " Запустите: python3 build_universal_donor.py"
            msg += " Или добавьте --donor путь.pdf"
            print(msg, file=sys.stderr)
            return 1

    # Проверка: донор должен содержать все буквы ФИО
    try:
        from receipt_db import chars_from_text_fields, get_missing_chars_in_receipt
        required = chars_from_text_fields(args.payer, args.recipient, args.phone, args.message or "")
        missing = get_missing_chars_in_receipt(donor_path, required)
        if missing:
            print(f"[ERROR] В доноре нет букв: {''.join(sorted(missing))}", file=sys.stderr)
            print("Используйте ё→е, Ё→Е или другой донор. См. python3 build_universal_donor.py --list", file=sys.stderr)
            return 1
    except Exception:
        pass

    print(f"[INFO] Донор: {donor_path.name}")

    data = bytearray(donor_path.read_bytes())
    try:
        out = patch_from_values(
            data,
            donor_path,
            date_str=date_str,
            payer=args.payer,
            recipient=args.recipient,
            phone=args.phone,
            bank=args.bank,
            amount=args.amount,
            message=args.message,
            operation_id=args.operation_id if args.operation_id else None,
            keep_metadata=True,
            account_last4=args.account if args.account and re.match(r"^\d{4}$", args.account) else None,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)
    if not args.keep_id:
        change_one_char_in_id(out_arr)

    out_path.write_bytes(out_arr)
    print("✅ Сгенерирован:", out_path)
    print(f"   Плательщик (получатель): {args.payer}")
    print(f"   Отправитель: {args.recipient}")
    print(f"   Телефон: {args.phone}")
    print(f"   Сумма: {args.amount:,} ₽".replace(",", " "))
    print(f"   Дата: {date_str}")
    print("   /ID:", "без изменений" if args.keep_id else "1 символ изменён")
    print("   CreationDate: синхронизирован с чеком")

    if args.report:
        import subprocess
        print()
        subprocess.run([sys.executable, "report_last_letter_coords.py", str(out_path)], cwd=BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
