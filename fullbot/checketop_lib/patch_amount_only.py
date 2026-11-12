#!/usr/bin/env python3
"""Патч только суммы: 10 → 10 000. Больше ничего не трогает, /ID не меняет."""
import sys
from pathlib import Path

from vtb_patch_from_config import patch_amount_only

def main():
    inp = Path("/Users/aleksandrzerebatav/Downloads/12-03-26_00-00 4.pdf")
    if len(sys.argv) > 1:
        inp = Path(sys.argv[1])
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}")
        return 1

    data = bytearray(inp.read_bytes())
    out_bytes = patch_amount_only(data, inp, 10000)
    base = Path(__file__).parent
    out_path = base / f"{inp.stem}_10000.pdf"
    out_path.write_bytes(out_bytes)
    print(f"✅ {out_path}")
    print("   10 ₽ → 10 000 ₽ (координаты по правилам)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
