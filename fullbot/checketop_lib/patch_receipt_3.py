#!/usr/bin/env python3
"""Патч 12-03-26_00-00 3.pdf: получатель, отправитель, сумма. ID не менять."""
import sys
from pathlib import Path

from vtb_patch_from_config import patch_from_values

BASE = Path(__file__).parent
INP = Path("/Users/aleksandrzerebatav/Downloads/12-03-26_00-00 3.pdf")


def main():
    inp = INP
    if len(sys.argv) > 1:
        inp = Path(sys.argv[1])
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}")
        return 1

    data = bytearray(inp.read_bytes())
    out_bytes = patch_from_values(
        data,
        inp,
        payer="Александр Евгеньевич Е.",
        recipient="Евгений Евгеньевич Ж.",
        amount=10000,
        keep_metadata=True,
    )
    out_path = BASE / f"{inp.stem}_patched.pdf"
    out_path.write_bytes(out_bytes)
    print(f"✅ {out_path}")
    print("   Получатель: Евгений Евгеньевич Ж.")
    print("   Отправитель: Александр Евгеньевич Е.")
    print("   Сумма: 10 000 ₽")
    print("   /ID: сохранён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
