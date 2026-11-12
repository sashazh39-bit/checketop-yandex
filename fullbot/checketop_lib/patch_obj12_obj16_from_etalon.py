#!/usr/bin/env python3
"""Заменить obj 12 (ToUnicode) и obj 16 (CIDToGIDMap) в receipt на эталон 13.pdf.

Шрифт ссылается на /ToUnicode 12 0 R и /CIDToGIDMap 16 0 R.
После замены нужно добавить маппинги Ф,Ч,Ю: add_tounicode_cyrillic.py

Использование:
  python3 patch_obj12_obj16_from_etalon.py receipt_add.pdf -o receipt_patched.pdf
  python3 patch_obj12_obj16_from_etalon.py receipt_add.pdf --etalon "/path/to/13.pdf" -o out.pdf
  python3 add_tounicode_cyrillic.py receipt_patched.pdf -o receipt_fixed.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Импортируем из patch_obj12
from patch_obj12_from_etalon import find_obj_stream_range, replace_obj_stream


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Заменить obj 12 и obj 16 на эталон из 13.pdf")
    ap.add_argument("input", help="receipt_add.pdf или аналогичный")
    ap.add_argument("-o", "--output", default="receipt_obj12_obj16_patched.pdf")
    ap.add_argument("--etalon", default=None, help="Эталон 13.pdf")
    args = ap.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1

    etalon_paths = [
        Path(args.etalon).expanduser().resolve() if args.etalon else None,
        Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
        Path(__file__).parent / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
    ]
    etalon = None
    for p in etalon_paths:
        if p and p.exists():
            etalon = p
            break
    if not etalon:
        print("[ERROR] Не найден эталон 13.pdf. Укажите --etalon путь", file=sys.stderr)
        return 1

    target_data = bytearray(inp.read_bytes())
    etalon_data = etalon.read_bytes()

    if not replace_obj_stream(target_data, etalon_data, 12):
        print("[ERROR] Не удалось заменить obj 12 (ToUnicode)", file=sys.stderr)
        return 1
    print("✅ obj 12 (ToUnicode) заменён")

    if not replace_obj_stream(target_data, etalon_data, 16):
        print("[WARN] obj 16 (CIDToGIDMap) не найден или не заменён", file=sys.stderr)
    else:
        print("✅ obj 16 (CIDToGIDMap) заменён")

    out_path = Path(args.output).resolve()
    out_path.write_bytes(target_data)
    print(f"   Результат: {out_path}")
    print("   Далее: python3 add_tounicode_cyrillic.py", out_path.name, "-o receipt_fixed.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
