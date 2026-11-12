#!/usr/bin/env python3
"""Простой патч check (3).pdf: payer, recipient, phone — без копирования шрифта.

Чек отображается корректно, т.к. меняются только значения в родном шрифте донора.

Использование:
  python3 patch_check3_simple.py -o receipt.pdf
  python3 patch_check3_simple.py -o receipt.pdf --phone "+7 (999) 123-45-67"
"""
import random
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DONOR = BASE / "база_чеков" / "vtb" / "СБП" / "check (3).pdf"


def _random_phone() -> str:
    return f"+7 ({random.randint(900, 999)}) {random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}"


def main() -> int:
    import argparse
    from vtb_patch_from_config import patch_from_values
    from vtb_test_generator import update_creation_date

    ap = argparse.ArgumentParser(description="Патч check(3) — ФИО и телефон, без замены шрифта")
    ap.add_argument("-o", "--output", default="receipt_check3_simple.pdf", help="Выходной PDF")
    ap.add_argument("--payer", "-p", default="Филипп Юсаев Ч.", help="Плательщик")
    ap.add_argument("--recipient", "-r", default="Филипп Юсаев Ч.", help="Получатель")
    ap.add_argument("--phone", default=None, help="Телефон (рандом если не указан)")
    ap.add_argument("--amount", "-a", type=int, default=None, help="Сумма (из донора если не указана)")
    ap.add_argument("--message", "-m", default="", help="Сообщение (пусто = оставить как в доноре)")
    args = ap.parse_args()

    donor_path = DONOR.resolve()
    if not donor_path.exists():
        print(f"[ERROR] Донор не найден: {donor_path}", file=sys.stderr)
        return 1

    phone = args.phone or _random_phone()
    date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    meta_date = datetime.now().strftime("D:%Y%m%d%H%M00+03'00'")

    data = bytearray(donor_path.read_bytes())
    try:
        out = patch_from_values(
            data,
            donor_path,
            date_str=date_str,
            payer=args.payer,
            recipient=args.recipient,
            phone=phone,
            amount=args.amount,
            message=args.message if args.message else None,
            keep_metadata=True,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)

    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', out_arr)
    if id_m:
        hex1 = id_m.group(1).decode()
        c = hex1[-1]
        idx = "0123456789ABCDEF".find(c.upper())
        new_c = "0123456789ABCDEF"[(idx + 1) % 16]
        new1 = hex1[:-1] + new_c
        out_arr[id_m.start(1) : id_m.end(1)] = new1.encode()
        out_arr[id_m.start(2) : id_m.end(2)] = new1.encode()

    out_path = Path(args.output).resolve()
    try:
        import fitz
        doc = fitz.open(stream=bytes(out_arr), filetype="pdf")
        doc.save(str(out_path), garbage=4, deflate=True, pretty=False)
        doc.close()
    except Exception:
        out_path.write_bytes(out_arr)

    print("✅ Готово:", out_path)
    print(f"   Плательщик: {args.payer}")
    print(f"   Получатель: {args.recipient}")
    print(f"   Телефон: {phone}")
    print(f"   Шрифт: родной check(3), без подмены — отображение корректное")
    return 0


if __name__ == "__main__":
    sys.exit(main())
