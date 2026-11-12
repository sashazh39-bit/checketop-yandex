#!/usr/bin/env python3
"""Патч только ФИО (плательщик, получатель). Дата, сумма, /ID — не трогает."""
import sys
from pathlib import Path

from vtb_patch_from_config import patch_from_values

BASE = Path(__file__).parent


def main():
    if len(sys.argv) < 4:
        print("Использование: python3 patch_fio_only.py input.pdf payer recipient [output.pdf]")
        print("Пример: python3 patch_fio_only.py '12-03-26_00-00 6.pdf' 'Ренан Арменович Р.' 'Евгений Александрович Е.'")
        return 1

    inp = Path(sys.argv[1])
    payer = sys.argv[2]
    recipient = sys.argv[3]
    out_path = Path(sys.argv[4]) if len(sys.argv) > 4 else BASE / f"{inp.stem}_fio.pdf"

    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}")
        return 1

    data = bytearray(inp.read_bytes())
    out_bytes = patch_from_values(
        data,
        inp,
        payer=payer,
        recipient=recipient,
        keep_metadata=True,
        keep_date=True,
    )
    out_path.write_bytes(out_bytes)
    print(f"✅ {out_path}")
    print(f"   Плательщик: {payer}")
    print(f"   Получатель: {recipient}")
    print("   Дата, сумма, /ID: без изменений")
    return 0


if __name__ == "__main__":
    sys.exit(main())
