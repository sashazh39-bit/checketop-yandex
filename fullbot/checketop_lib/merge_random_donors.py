#!/usr/bin/env python3
"""Собрать рандомный PDF из donors (только < 13 KB), объединяя CID из разных источников.

Использование:
  python3 merge_random_donors.py
  python3 merge_random_donors.py --out чеки 08.03/random_merged.pdf
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

MAX_SIZE_KB = 13
DONORS_DIR = Path(__file__).parent / "donors"
OUT_DIR = Path(__file__).parent / "чеки 08.03"


def main() -> int:
    donors = DONORS_DIR
    if not donors.exists():
        print(f"[ERROR] Папка donors не найдена: {donors}", file=sys.stderr)
        return 1

    max_bytes = MAX_SIZE_KB * 1024
    pdfs = [p for p in donors.glob("*.pdf") if p.stat().st_size < max_bytes]
    if len(pdfs) < 2:
        print(f"[ERROR] Нужно минимум 2 PDF < {MAX_SIZE_KB} KB в donors. Найдено: {len(pdfs)}", file=sys.stderr)
        return 1

    # Случайный target и 3 источника (с повторами допустимы)
    random.shuffle(pdfs)
    target = pdfs[0]
    sources = random.choices(pdfs, k=3)  # может повторяться — ок

    # Конфиг по координатам target (правая колонка x>=100)
    from merge_cid_from_sources import extract_tj_blocks

    blocks = extract_tj_blocks(target)
    right_blocks = [(x, y, _) for x, y, _ in blocks if x >= 100]
    right_blocks.sort(key=lambda b: (-b[1], b[0]))  # сверху вниз

    config = [
        {"y": y, "ytol": 2, "xmin": 100, "source": random.randint(0, 2)}
        for x, y, _ in right_blocks
    ]

    out_path = Path(__file__).parent / "чеки 08.03" / "random_merged.pdf"
    if len(sys.argv) >= 2:
        out_path = Path(sys.argv[1]).expanduser().resolve()

    out_path.parent.mkdir(parents=True, exist_ok=True)

    from merge_cid_from_sources import merge_cid

    print(f"Target: {target.name}")
    print(f"Sources: {[s.name for s in sources]}")
    print(f"Config: {config}")
    print(f"Out: {out_path}")

    ok = merge_cid(target, out_path, sources, config=config, adapt_kern=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
