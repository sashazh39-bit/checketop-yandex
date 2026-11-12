#!/usr/bin/env python3
"""
Одноразовая сборка выписки под заданные поля (эталон AM_1774134591446.pdf).
Суммы в операциях: -10 000,30 и -0,09 (всего расходы 10 000,39).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_BASE = [
    Path(__file__).parent / "AM_1774134591446.pdf",
    Path.home() / "Downloads" / "AM_1774134591446.pdf",
]
BASE = next((p for p in _BASE if p.exists()), None)
OUT = Path(__file__).parent / "выписка_zhukov_22_03_2026.pdf"

OLD_ACCOUNT = "40817810980480002476"
OLD_DATE = "25.02.2026"
OLD_PERIOD = "За период с 25.02.2026 по 25.02.2026"
OLD_OP_C = "C822502260006543"
OLD_OST = "OST1_5KSH0001I0M"

# Однострочные замены (якорная многострочная не попадает в окно между Tj в потоке).
OLD_TADZH = "Таджикистан. C822502260006543"
TRANSFER_DESC = "Перевод за рубеж по номеру телефона C822502260006543"

OLD_FIO = "Жеребятьев Александр \nЕвгеньевич"
NEW_FIO = "Жуков Артем\nЕгорович"

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

# 16 символов; слот OST1_ — только символы из суффикса эталона (см. make_ost_code в task2)
NEW_OST = "OST1_5H501K0SIMK"


def main() -> None:
    if not BASE:
        print("Нет AM_1774134591446.pdf (положите в папку проекта или ~/Downloads/)", file=sys.stderr)
        sys.exit(1)

    new_account = "40817810280480002477"
    new_op_c = "C162103261572782"
    incoming = "10 065,21"
    expenses = "10 000,39"
    outgoing = "0 064,82"  # 8 символов как «1 812,90» для выравнивания
    tx1 = "-10 000,30"
    tx2 = "-00,09"  # 6 символов как у «-10,00» в шаблоне

    # Без + ( ) — этих глифов нет в CMap эталона; номер как 79118584552 = +7 911 …
    new_tadzh = f"Тел 79118584552 C822502260006543"
    new_transfer = f"Доп. списание округления C822502260006543"

    # Не включать OLD_OP_C в этот проход — сначала строки с C822502260006543.
    content_reps: list[tuple[str, str]] = [
        (OLD_TADZH, new_tadzh),
        (TRANSFER_DESC, new_transfer),
        (OLD_FIO, NEW_FIO),
        (OLD_ADDR, NEW_ADDR),
        (OLD_ACCOUNT, new_account),
        ("1 852,90 RUR", f"{incoming} RUR"),
        ("1 812,90 RUR", f"{outgoing} RUR"),
        ("40,00 RUR", f"{expenses} RUR"),
        ("-30,00 RUR", f"{tx1} RUR"),
        ("-10,00 RUR", f"{tx2} RUR"),
        (OLD_OST, NEW_OST),
    ]

    op_date = "21.03.2026"
    form_date = "22.03.2026"

    from cid_patch_amount import patch_replacements
    from patch_id import patch_document_id_one_nibble, patch_moddate

    print(f"Base: {BASE}\nOut:  {OUT}")
    ok = patch_replacements(BASE, OUT, content_reps)
    if not ok:
        print("[ERROR] content patch", file=sys.stderr)
        sys.exit(1)

    # Код C822… встречается в нескольких Tj/картах — иногда нужно >1 прохода.
    for _ in range(8):
        if not patch_replacements(OUT, OUT, [(OLD_OP_C, new_op_c)]):
            break

    date_reps = [
        (OLD_PERIOD, f"За период с {op_date} по {form_date}"),
        (OLD_DATE, op_date),
        (f"выписки\n{op_date}", f"выписки\n{form_date}"),
    ]
    patch_replacements(OUT, OUT, date_reps)

    patch_document_id_one_nibble(OUT, which=2, pos_from_end=3)
    patch_moddate(OUT, form_date)

    try:
        from alfa_statement_service import adjust_amount_tm_positions

        adjust_amount_tm_positions(OUT)
    except Exception as e:
        print(f"[WARN] adjust_amount_tm_positions: {e}")

    print("[OK] Готово:", OUT)


if __name__ == "__main__":
    main()
