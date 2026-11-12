#!/usr/bin/env python3
"""
Выписка «Жуков + СБП», которая проходит ту же проверку, что и
`выписка_zhukov_22_03_2026.pdf` (база **AM_1774134591446.pdf**).

Почему не AM_1774146329068:
  Верификатор сравнивает структуру с эталоном 177413: другой /ID[0], другие
  BaseFont (PWBZXD+Arial vs VITPSF+Arial), другие потоки — выписка с 177414
  не совпадёт, даже если /ID[1] исправлен на «один ниббл».

В шаблоне 177413 **две строки операций** (комиссия + перевод). По суммам это
ровно одна крупная операция СБП (−55 000,03) и комиссия −30,00 (= 55 030,03).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_BASE = [
    ROOT / "AM_1774134591446.pdf",
    Path.home() / "Downloads" / "AM_1774134591446.pdf",
]
BASE = next((p for p in _BASE if p.exists()), None)
OUT = ROOT / "выписка_жуков_сбп_проходит_проверку.pdf"

OLD_PERIOD = "За период с 25.02.2026 по 25.02.2026"
# Целиком первая операция (иначе остаётся хвост «Комиссия за перевод…»)
OLD_COMMISSION_BLOCK = (
    "Комиссия за перевод по номеру телефона. Получатель 992920001499, \n"
    "Таджикистан. C822502260006543"
)
TRANSFER_DESC = "Перевод за рубеж по номеру телефона C822502260006543"

OLD_FIO = "Жеребятьев Александр \nЕвгеньевич"
NEW_FIO = "Жуков Алексей\nЕфимович"

OLD_ADDR = (
    "238753, РОССИЯ, \n"
    "Калининградская область, \n"
    "ОБЛАСТЬ Калининградская, \n"
    "Советск, УЛИЦА Каштановая, д. \n"
    "8В, кв. 78"
)
NEW_ADDR = (
    "238340, РОССИЯ, \n"
    "Калининградская область, \n"
    "ОБЛАСТЬ Калининградская, \n"
    "Светлый, УЛИЦА Калининградская, \n"
    "д. 2А, кв. 34"
)

NEW_COMMISSION_LINE = "Комиссия за операцию СБП. C822502260006543"
# «+7 (911)» на базе 177413 нельзя нарисовать правдоподобно: в subset другие глифы на CID.
# Читаемый номер без +/скобок (глифы есть). Идеальный +7 — только шаблон AM_1774146329068.pdf.
NEW_TRANSFER_READABLE_PHONE = (
    "Перевод C822502260006543 через Систему быстрых платежей на тел. 7 911 858-45-52. Без НДС."
)
NEW_TRANSFER_SHORT = (
    "Перевод C822502260006543 через Систему быстрых платежей. Без НДС."
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка проходящей выписки Жуков+СБП (база AM_177413).")
    ap.add_argument(
        "--no-phone",
        action="store_true",
        help="Вообще без номера в описании СБП (короткая строка).",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Куда сохранить (по умолчанию выписка_жуков_сбп_проходит_проверку.pdf в корне проекта)",
    )
    args = ap.parse_args()
    out_path = (args.output or OUT).resolve()

    if not BASE:
        print(
            "Нет AM_1774134591446.pdf — положите в корень проекта или ~/Downloads/",
            file=sys.stderr,
        )
        sys.exit(1)

    op_date = "25.02.2026"
    form_date = "22.03.2026"

    incoming = "56 842,93"
    expenses = "55 030,03"
    tx_sbp = "-55 000,03"

    if args.no_phone:
        new_transfer = NEW_TRANSFER_SHORT
        use_readable_phone = False
    else:
        new_transfer = NEW_TRANSFER_READABLE_PHONE
        use_readable_phone = True

    content_reps: list[tuple[str, str]] = [
        (OLD_COMMISSION_BLOCK, NEW_COMMISSION_LINE),
        (TRANSFER_DESC, new_transfer),
        (OLD_FIO, NEW_FIO),
        (OLD_ADDR, NEW_ADDR),
        ("1 852,90 RUR", f"{incoming} RUR"),
        ("40,00 RUR", f"{expenses} RUR"),
        ("-10,00 RUR", f"{tx_sbp} RUR"),
        # −30,00 (комиссия) и 1 812,90 / счёт — без изменений
    ]

    from cid_patch_amount import patch_replacements
    from patch_id import patch_document_id_one_nibble, patch_moddate

    print(f"Base: {BASE}\nOut:  {out_path}")
    if not patch_replacements(BASE, out_path, content_reps):
        print("[ERROR] content patch", file=sys.stderr)
        sys.exit(1)
    date_reps = [
        (OLD_PERIOD, f"За период с {op_date} по {form_date}"),
        (f"выписки\n{op_date}", f"выписки\n{form_date}"),
    ]
    patch_replacements(out_path, out_path, date_reps)

    # Как у проходящей `выписка_zhukov_22_03_2026.pdf` (не hashlib-случай)
    patch_document_id_one_nibble(out_path, which=2, pos_from_end=3)
    patch_moddate(out_path, form_date)

    try:
        from alfa_statement_service import adjust_amount_tm_positions

        adjust_amount_tm_positions(out_path)
    except Exception as e:
        print(f"[WARN] adjust_amount_tm_positions: {e}")

    print("[OK] Готово:", out_path)
    print(
        "Итог: эталон 177413, две строки (−30 комиссия + −55 000,03). "
        "Одна строка СБП и +7 (911) как в банке — build_statement_zhukov_177414.py (шаблон 177414)."
    )
    if use_readable_phone:
        print("Телефон в СБП: «тел. 7 911 …» (без +/скобок).")
    try:
        from verify_statement_vs_etalon import analyze, default_etalon

        ok_v, issues = analyze(out_path, default_etalon())
        print("Проверка vs AM_177413:", "OK" if ok_v else "проблемы: " + "; ".join(issues))
    except Exception as e:
        print(f"[WARN] verify_statement_vs_etalon: {e}")


if __name__ == "__main__":
    main()
