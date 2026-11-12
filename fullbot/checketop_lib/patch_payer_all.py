#!/usr/bin/env python3
"""Замена «Имя плательщика» на «Арман Мелсикович Б.» во всех PDF < 13 KB в donors.

Работает по КООРДИНАТАМ: берёт байты из source (13-02-26 с Арманом) и подставляет
в target на той же позиции. Никаких жёстких паттернов — всё извлекается из файлов.

Использование:
  python3 patch_payer_all.py                    # все PDF < 13 KB из donors
  python3 patch_payer_all.py donors/07-03.pdf  # один файл
  python3 patch_payer_all.py --source "чеки 07.03/13-02-26.pdf"
"""
from __future__ import annotations

import sys
from pathlib import Path

MAX_SIZE_KB = 13
DONORS_DIR = Path(__file__).parent / "donors"
OUT_DIR = Path(__file__).parent / "чеки 08.03"
SOURCE_DIR = Path(__file__).parent / "чеки 07.03"

# Конфиг: только поле плательщика. source_y=227.25 — в эталоне имя на этой y.
# target может иметь y=348.75 (04-34) или 227.25 (18-10) — подставляем из source.
PAYER_CONFIG = [
    {"y": 348.75, "ytol": 2, "xmin": 100, "source": 0, "source_y": 227.25},
    {"y": 227.25, "ytol": 2, "xmin": 100, "source": 0},
]


def _find_source() -> Path | None:
    """Найти PDF с Арманом (13-02-26)."""
    for name in ("13-02-26_20-29 — копия.pdf", "13-02-26_20-29.pdf"):
        p = SOURCE_DIR / name
        if p.exists():
            return p
    for f in SOURCE_DIR.glob("13-02-26*.pdf"):
        return f
    return None


def main() -> int:
    base = Path(__file__).parent
    source = _find_source()
    if not source or not source.exists():
        print(f"[ERROR] Source не найден в {SOURCE_DIR}", file=sys.stderr)
        return 1

    donors = base / "donors"
    if not donors.exists():
        print(f"[ERROR] Папка donors не найдена: {donors}", file=sys.stderr)
        return 1

    max_bytes = MAX_SIZE_KB * 1024

    if len(sys.argv) >= 2 and not sys.argv[1].startswith("-"):
        targets = [Path(sys.argv[1]).expanduser().resolve()]
        if not targets[0].exists():
            print(f"[ERROR] Файл не найден: {targets[0]}", file=sys.stderr)
            return 1
    else:
        targets = [p for p in donors.glob("*.pdf") if p.stat().st_size < max_bytes]

    if not targets:
        print(f"[WARN] Нет PDF < {MAX_SIZE_KB} KB в {donors}", file=sys.stderr)
        return 0

    from merge_cid_from_sources import merge_cid

    out_dir = base / "чеки 08.03"
    out_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    for target in targets:
        out = out_dir / target.name.replace(" (1)", "").replace(" (2)", "")
        print(f"\n[→] {target.name}")
        if merge_cid(target, out, [source], config=PAYER_CONFIG, adapt_kern=True):
            ok_count += 1

    print(f"\n[OK] Обработано {ok_count}/{len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
