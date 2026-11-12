#!/usr/bin/env python3
"""Сумма 10→10 000 + поменять /ID. Остальное не трогать."""
import sys
from pathlib import Path

from vtb_patch_from_config import patch_amount_only
from vtb_test_generator import update_id

BASE = Path(__file__).parent


def main():
    inp = Path("/Users/aleksandrzerebatav/Downloads/12-03-26_00-00 5.pdf")
    if len(sys.argv) > 1:
        inp = Path(sys.argv[1])
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}")
        return 1

    data = bytearray(inp.read_bytes())
    out_bytes = patch_amount_only(data, inp, 10000)
    data = bytearray(out_bytes)
    update_id(data)

    out_path = BASE / f"{inp.stem}_10000.pdf"
    out_path.write_bytes(data)
    print(f"✅ {out_path}")
    print("   Сумма: 10 ₽ → 10 000 ₽")
    print("   /ID: обновлён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
