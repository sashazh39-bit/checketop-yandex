#!/usr/bin/env python3
"""Вариант C: база = compact-чек (random_receipt_2), шрифт/CMap из check(3).

Структура и размер файла сохраняются от compact-чека:
- MediaBox 281×437 pt
- «Имя плательщика», «Телефон получателя» в одну строку
- Без «Сообщение»
Размер ~9–10 KB (зависит от base).

Использование:
  python3 gen_variant_c.py output.pdf
  python3 gen_variant_c.py -o receipt_c.pdf --payer "..." --recipient "..." --amount 5000
"""
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
# База: компактный формат 281×437. Бот распознаёт только чеки с известной структурой (BaseFont, /W).
# 29-01-26_18-35 — эталон по БОТ_ОТКЛОНЯЕТ_ЧЕК.md. 13-03-26 из Downloads имеет другую структуру (AAWONC) → бот не распознаёт.
COMPACT_BASE = BASE / "база_чеков" / "vtb" / "СБП" / "29-01-26_18-35.pdf"
FONT_DONOR = BASE / "база_чеков" / "vtb" / "СБП" / "check (3).pdf"  # шрифт с полным алфавитом


def main() -> int:
    import argparse
    from copy_font_cmap import copy_font_cmap
    from vtb_patch_from_config import patch_from_values
    from vtb_test_generator import update_creation_date

    ap = argparse.ArgumentParser(description="Вариант C: compact-формат + шрифт из check(3)")
    ap.add_argument("output", nargs="?", default="receipt_variant_c.pdf", help="Выходной PDF")
    ap.add_argument("-o", "--output-file", dest="output_alt", default=None, help="Выходной PDF (alias)")
    ap.add_argument("--payer", "-p", default="Алексей Евгеньевич А.", help="ФИО получателя")
    ap.add_argument("--recipient", "-r", default="Роман Алексеевич А.", help="ФИО отправителя")
    ap.add_argument("--phone", default="+7 (992) 494-94-95", help="Телефон")
    ap.add_argument("--amount", "-a", type=int, default=8700, help="Сумма")
    ap.add_argument("--bank", default=None, help="Банк получателя (из базы если не указан)")
    ap.add_argument("--date", default=None, help="Дата DD.MM.YYYY, HH:MM или 'now'")
    ap.add_argument("--base", default=None, help="Базовый чек (по умол. 29-01-26 — эталон для бота). Другая база → бот может не распознать.")
    ap.add_argument("--donor-font", default=None, help="Донор шрифта (по умол. check (3).pdf)")
    ap.add_argument("--no-font-copy", action="store_true", help="Не копировать шрифт — только патч (если в базе есть все буквы)")
    args = ap.parse_args()

    out_path = Path(args.output_alt or args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    base_pdf = Path(args.base or str(COMPACT_BASE)).expanduser().resolve()
    font_donor = Path(args.donor_font or str(FONT_DONOR)).expanduser().resolve()

    if not base_pdf.exists():
        print(f"[ERROR] Базовый чек не найден: {base_pdf}", file=sys.stderr)
        return 1
    if not font_donor.exists():
        print(f"[ERROR] Донор шрифта не найден: {font_donor}", file=sys.stderr)
        return 1

    if args.date and args.date.lower() == "now":
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    elif args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")

    meta_date = datetime.strptime(date_str, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")

    # Шаг 1: копируем шрифт (или используем базу как есть при --no-font-copy)
    temp_with_font = BASE / ".temp_variant_c_with_font.pdf"
    if args.no_font_copy:
        import shutil
        shutil.copy2(base_pdf, temp_with_font)
    elif not copy_font_cmap(font_donor, base_pdf, temp_with_font):
        return 1

    # Шаг 2: патчим значения
    data = bytearray(temp_with_font.read_bytes())
    try:
        out = patch_from_values(
            data,
            temp_with_font,
            date_str=date_str,
            payer=args.payer,
            recipient=args.recipient,
            phone=args.phone,
            amount=args.amount,
            bank=args.bank,
            keep_metadata=True,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        temp_with_font.unlink(missing_ok=True)
        return 1

    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)

    # Document ID: 1 символ (для прохождения проверки)
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', out_arr)
    if id_m:
        hex1 = id_m.group(1).decode()
        c = hex1[-1]
        chars = "0123456789ABCDEF"
        idx = chars.find(c.upper())
        new_c = chars[(idx + 1) % 16]
        new1 = hex1[:-1] + new_c
        out_arr[id_m.start(1) : id_m.end(1)] = new1.encode()
        out_arr[id_m.start(2) : id_m.end(2)] = new1.encode()

    # Пересохранение через PyMuPDF для исправления структуры (исправляет ошибки открытия)
    try:
        import fitz
        doc = fitz.open(stream=bytes(out_arr), filetype="pdf")
        doc.save(str(out_path), garbage=4, deflate=True, pretty=False)
        doc.close()
        size_kb = out_path.stat().st_size / 1024
    except Exception:
        out_path.write_bytes(out_arr)
        size_kb = len(out_arr) / 1024
    temp_with_font.unlink(missing_ok=True)

    print("✅ Вариант C — сгенерирован:", out_path)
    print(f"   Размер: {size_kb:.1f} KB (цель 8–10 KB)")
    print(f"   База: {base_pdf.name}, шрифт: {font_donor.name}")
    print(f"   Плательщик: {args.payer}, Получатель: {args.recipient}")
    print(f"   Сумма: {args.amount:,} ₽, Дата: {date_str}".replace(",", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
