#!/usr/bin/env python3
"""
Сборка «как в банке» на шаблоне AM_1774146329068.pdf:

  • Одна операция СБП (в шаблоне изначально одна строка в блоке операций).
  • Номер +7 (911) 858-45- и перенос «52» на следующую строку — нативные глифы шаблона,
    без ломаного ToUnicode (в отличие от попытки вставить + на базе 177413).

Суммы без отдельной «комиссии СБП»: расходы = сумма перевода (55 000,03), балансы согласованы.

Если автопроверка сравнивает файл с эталоном AM_1774134591446.pdf — этот PDF не пройдёт
(другой /ID[0] и subset шрифтов). Для проверки по 177413: build_statement_zhukov_sbp_passing.py
(там две строки операций и нет настоящего «+» в шрифте).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_BASE_CANDIDATES = [
    Path.home() / "Downloads" / "AM_1774146329068.pdf",
    ROOT / "AM_1774146329068.pdf",
]
BASE = next((p for p in _BASE_CANDIDATES if p.exists()), None)
OUT = ROOT / "выписка_жуков_177414_СБП_одна_операция.pdf"

# Телефон в описании СБП оставляем как в шаблоне (+7 (911) 858-45- / 52): patch_alfa_statement
# сам подставит C822… вместо C162… в тексте «Перевод C… через СБП…». Отдельно резать
# описание до короткого варианта без телефона не нужно.


def main() -> None:
    if not BASE:
        print(
            "Нет AM_1774146329068.pdf — положите в ~/Downloads/ или в корень проекта.",
            file=sys.stderr,
        )
        sys.exit(1)

    changes: dict[str, str] = {
        "номер_счета": "40817810980480002476",
        "клиент_имя": "Жуков Алексей",
        "клиент_отчество": "Ефимович",
        "адрес_полный": (
            "238340, РОССИЯ, \n"
            "Калининградская область, \n"
            "ОБЛАСТЬ Калининградская, \n"
            "Светлый, УЛИЦА Калининградская, \n"
            "д. 2А, кв. 34"
        ),
        # 56 812,93 − 55 000,03 = 1 812,90 (без строки «комиссии −30»)
        "входящий_остаток": "56 812,93",
        "поступления": "0,00",
        "расходы": "55 000,03",
        "исходящий_остаток": "1 812,90",
        "платежный_лимит": "1 812,90",
        "текущий_баланс": "1 812,90",
        "код_операции_расход": "C822502260006543",
        "сумма_расход": "55 000,03",
        # Другой номер в строке СБП: задать "телефон" и "телефон_окончание" (как в alfa_statement_service).
    }

    from alfa_statement_service import adjust_amount_tm_positions, patch_alfa_statement
    from cid_patch_amount import patch_replacements
    from patch_id import patch_document_id_one_nibble, patch_moddate

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)

    ok, err = patch_alfa_statement(changes, tmp, base_pdf=BASE)
    if not ok:
        print(f"[ERROR] patch_alfa_statement: {err}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        sys.exit(1)

    # Период и дата проводки (в сервисе нет отдельных ключей)
    extra: list[tuple[str, str]] = [
        ("За период с 21.03.2026 по 22.03.2026", "За период с 25.02.2026 по 22.03.2026"),
        ("21.03.2026\nC822502260006543", "25.02.2026\nC822502260006543"),
    ]
    patch_replacements(tmp, tmp, extra)

    try:
        adjust_amount_tm_positions(tmp)
    except Exception as e:
        print(f"[WARN] adjust_amount_tm_positions: {e}")

    raw = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fin:
        fpath = Path(fin.name)
    try:
        fpath.write_bytes(raw)
        patch_moddate(fpath, "22.03.2026")
        # Стабильно как у проходящих выписок 177413 (не случайный ниббл)
        patch_document_id_one_nibble(fpath, which=2, pos_from_end=3)
        OUT.write_bytes(fpath.read_bytes())
    finally:
        fpath.unlink(missing_ok=True)
    print(f"[OK] {BASE.name} → {OUT}")

    try:
        from verify_statement_vs_etalon import analyze, default_etalon

        ok, issues = analyze(OUT, default_etalon())
        if not ok and issues:
            print("\n--- Проверка против эталона AM_1774134591446 (как у верификатора) ---")
            for msg in issues:
                print(" ⚠", msg)
            print(
                "Для прохождения той же проверки запустите:\n"
                "  python3 build_statement_zhukov_sbp_passing.py\n"
                "или см. verify_statement_vs_etalon.py"
            )
    except Exception as e:
        print(f"[WARN] verify: {e}")


if __name__ == "__main__":
    main()
