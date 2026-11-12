#!/usr/bin/env python3
"""Создание универсального донора с максимальным набором букв.

Объединяет шрифт и CMap из нескольких чеков — один PDF для любых ФИО без
поиска донора по буквам.

Использование:
  python3 build_universal_donor.py [output.pdf]
  python3 build_universal_donor.py universal.pdf --base "13-03-26_00-00 14.pdf"

Затем:
  python3 gen_verified_receipt.py out.pdf --donor universal.pdf --payer "Фуя Ян У." ...
"""
import sys
from pathlib import Path

from copy_font_cmap import copy_font_cmap

BASE = Path(__file__).parent
DONORS_DIR = BASE / "база_чеков" / "vtb" / "СБП"

# Алфавит для ФИО (ё заменяем на е через vtb_cmap)
FIO_ALPHABET = frozenset("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя")


def get_donor_chars(p: Path) -> set[str]:
    try:
        from receipt_db import get_receipt_chars, _normalize_char
    except ImportError:
        return set()
    ch = get_receipt_chars(p)
    return {_normalize_char(c) for c in ch}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Создать универсальный донор с макс. набором букв (без поиска по символам)"
    )
    ap.add_argument("output", nargs="?", default="universal_donor.pdf", help="Выходной PDF")
    ap.add_argument("--base", "-b", help="Базовый чек (проходящий) — структура сохраняется")
    ap.add_argument("--list", action="store_true", help="Только показать покрытие символов")
    args = ap.parse_args()

    if not DONORS_DIR.exists():
        print("[ERROR] Папка база_чеков/vtb/СБП не найдена.", file=sys.stderr)
        return 1

    donors = sorted(DONORS_DIR.glob("*.pdf"))
    if not donors:
        print("[ERROR] Нет доноров в базе.", file=sys.stderr)
        return 1

    # Донор с макс. покрытием алфавита ФИО (не просто кол-во символов)
    by_chars = [(p, get_donor_chars(p)) for p in donors]
    by_chars.sort(key=lambda x: (-len(FIO_ALPHABET & x[1]), -len(x[1])))
    best_donor, best_chars = by_chars[0]

    if args.list:
        fio_cover = FIO_ALPHABET & best_chars
        print(f"Донор с макс. буквами: {best_donor.name} ({len(fio_cover)}/66 для ФИО, всего {len(best_chars)})")
        print("Есть:", "".join(sorted(fio_cover)))
        missing = FIO_ALPHABET - best_chars
        if missing:
            print(f"Нет: {''.join(sorted(missing))} — используйте ё→е для ё/Ё")
        return 0

    # Базовый чек — проходящий или лучший донор
    if args.base:
        base_path = Path(args.base).expanduser().resolve()
        if not base_path.exists():
            print(f"[ERROR] Базовый файл не найден: {base_path}", file=sys.stderr)
            return 1
        target = base_path
    else:
        target = best_donor

    # Merge CMap из доноров с редкими буквами (шрифт — subset, новые глифы не добавятся,
    # но ToUnicode можно дополнить для символов, которые используют тот же CID в других чеках)
    merge_paths = []
    for p, ch in by_chars[1:6]:  # ещё до 5 доноров для максимального покрытия
        if ch - best_chars:
            merge_paths.append(p)

    out_path = Path(args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    ok = copy_font_cmap(
        best_donor,
        target,
        out_path,
        merge_cmap_paths=merge_paths if merge_paths else None,
    )
    if not ok:
        return 1

    print(f"✅ Универсальный донор: {out_path}")
    print(f"   Источник шрифта: {best_donor.name} ({len(best_chars)} симв.)")
    if merge_paths:
        print(f"   Merge CMap из: {[p.name for p in merge_paths[:3]]}{'...' if len(merge_paths) > 3 else ''}")
    print()
    print("Использование:")
    print(f'  python3 gen_verified_receipt.py out.pdf --donor "{out_path.name}" --payer "ФИО" --recipient "ФИО" ...')
    return 0


if __name__ == "__main__":
    sys.exit(main())
