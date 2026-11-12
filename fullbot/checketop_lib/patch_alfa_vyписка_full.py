#!/usr/bin/env python3
"""Патч выписки Альфа-Банка: все замены с сохранением структуры и размера.

Замены:
1. 40817810280480002477 → 40817810280480002476
2. Жеребятьев Александр → Николаев Дмитрий
3. За период с 16.03.2026 по 16.03.2026 → За период с 07.03.2026 по 08.03.2026
4. 14,82 RUR → 5 004,82 RUR
5. 10,00 RUR → 5 000,00 RUR (Расходы)
6. -10,00 RUR → 5 000,00 RUR (операция)
7. 16.03.2026,19-12-24 → 07.03.2026,14-17-24
8. 16.03.2026 A011603260469228 → 07.03.2026 A011603260469228 (только в таблице операций)
   Дата формирования выписки 16.03.2026 — НЕ меняем
9. д. 8В, кв. 78 → д. X, кв. Y (случайные)
"""
import random
import subprocess
import sys
from pathlib import Path


def main():
    inp = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else Path(__file__).parent / "Выписка_по_счёту.pdf"
    )
    out = Path(
        sys.argv[2]
        if len(sys.argv) > 2
        else inp.parent / (inp.stem + "_patched.pdf")
    )

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    house = random.randint(1, 50)
    apt = random.randint(1, 150)
    house_str = str(house)
    apt_str = str(apt)

    replacements = [
        ("40817810280480002477", "40817810280480002476"),
        ("Жеребятьев Александр", "Николаев Дмитрий"),
        ("За период с 16.03.2026 по 16.03.2026", "За период с 07.03.2026 по 08.03.2026"),
        ("14,82 RUR", "5 004,82 RUR"),
        ("10,00 RUR", "5 000,00 RUR"),
        ("-10,00 RUR", "5 000,00 RUR"),
        ("16.03.2026,19-12-24", "07.03.2026,14-17-24"),
        ("16.03.2026 A011603260469228", "07.03.2026 A011603260469228"),
        ("8В, кв. 78", f"{house_str}, кв. {apt_str}"),
    ]

    args = [
        sys.executable,
        str(Path(__file__).parent / "cid_patch_amount.py"),
        str(inp),
        str(out),
    ]
    for old, new in replacements:
        args.extend(["--replace", f"{old}={new}"])

    orig_size = inp.stat().st_size
    result = subprocess.run(args)
    if result.returncode != 0:
        return result.returncode

    if out.exists():
        new_size = out.stat().st_size
        print(f"\nРазмер: {orig_size} → {new_size} байт")
        print(f"Дом: {house_str}, Квартира: {apt_str}")
        print(f"[OK] Сохранено: {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
