#!/usr/bin/env python3
"""
Проверка выписки Альфа относительно эталона AM_1774134591446.pdf
(те же критерии, что в task2_statements / compare_structure_deep).

Запуск:
  python3 verify_statement_vs_etalon.py путь/к/выписке.pdf
  python3 verify_statement_vs_etalon.py путь/к/выписке.pdf --etalon AM_1774134591446.pdf
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def default_etalon() -> Path:
    cands = [ROOT / "AM_1774134591446.pdf", Path.home() / "Downloads" / "AM_1774134591446.pdf"]
    for p in cands:
        if p.exists():
            return p
    return ROOT / "AM_1774134591446.pdf"


def analyze(pdf_path: Path, etalon_path: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not pdf_path.exists():
        return False, [f"Файл не найден: {pdf_path}"]
    if not etalon_path.exists():
        return False, [f"Эталон не найден: {etalon_path}"]

    raw = pdf_path.read_bytes()
    e_raw = etalon_path.read_bytes()

    # 1) Маркер шрифта эталона (другой шаблон — другой subset name)
    if b"PWBZXD+Arial" not in raw:
        if b"VITPSF+Arial" in raw or b"ZAVLBB+Arial" in raw:
            issues.append(
                "Шрифт не как у эталона 177413 (нет PWBZXD+Arial). "
                "Обычно это выписка с шаблона AM_177414… / AM_177410… — такой PDF "
                "не совпадёт с верификатором, настроенным на AM_1774134591446.pdf."
            )
        else:
            issues.append(
                "В PDF не найден BaseFont PWBZXD+Arial — структура может не совпадать с эталоном 177413."
            )

    id_p = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw)
    id_e = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", e_raw)
    if not id_p:
        issues.append("Не удалось прочитать /ID из выписки.")
    if not id_e:
        issues.append("Не удалось прочитать /ID из эталона.")
    if id_p and id_e:
        g1 = id_p.group(1).decode().lower()
        g2 = id_p.group(2).decode().lower()
        e1 = id_e.group(1).decode().lower()
        e2 = id_e.group(2).decode().lower()
        if g1 != e1:
            issues.append(
                "ID[0] не совпадает с эталоном: верификатор ожидает тот же «постоянный» "
                "идентификатор документа, что у AM_1774134591446.pdf. "
                f"У выписки: …{g1[-8:]}, у эталона: …{e1[-8:]}."
            )
        diff2 = sum(1 for a, b in zip(g2, e2) if a != b) + abs(len(g2) - len(e2))
        if diff2 != 1:
            issues.append(
                f"Во второй строке /ID отличий от эталона: {diff2} (нужно ровно 1 hex-символ, "
                "как после patch_document_id_one_nibble)."
            )
        if g1 == g2:
            issues.append("ID[0] и ID[1] совпадают — типичный признак полной подмены /ID.")

    md = re.search(rb"/ModDate(\s*)\(", raw)
    if md and md.group(1) != b"":
        issues.append(
            "Между /ModDate и '(' есть пробел — у Oracle BI Publisher его нет; строгие проверки могут отклонить."
        )

    return len(issues) == 0, issues


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка выписки против эталона 177413.")
    ap.add_argument("pdf", type=Path, help="Проверяемый PDF")
    ap.add_argument("--etalon", type=Path, default=None, help="Эталон (по умолчанию AM_177413 в проекте/Downloads)")
    args = ap.parse_args()
    etalon = args.etalon.expanduser().resolve() if args.etalon else default_etalon()
    pdf = args.pdf.expanduser().resolve()

    ok, issues = analyze(pdf, etalon)
    print(f"Файл:   {pdf.name}")
    print(f"Эталон: {etalon}")
    if ok:
        print("Результат: OK (критерии как у task2 для 177413)")
        return 0
    print("Результат: НЕ ПРОШЛА")
    for i, msg in enumerate(issues, 1):
        print(f"  {i}. {msg}")
    print(
        "\nИсправление: соберите выписку с базы AM_1774134591446.pdf, например:\n"
        "  python3 build_statement_zhukov_sbp_passing.py"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
