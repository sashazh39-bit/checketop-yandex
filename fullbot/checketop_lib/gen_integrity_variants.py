#!/usr/bin/env python3
"""Варианты для прохождения проверки целостности.

Гипотезы почему целостность падает (при том что верификация проходит):
1. add_glyphs — инъекция шрифта (М, Н, Р и др.) меняет CIDToGIDMap/ToUnicode
2. check(3) как донор — полный алфавит без инъекции, только контент-патч
3. 16-03-26 шаблон — возможно актуальнее, целостность по нему
4. Document ID — смена 1 символа может триггерить проверку
5. Минимальный патч — только дата+сумма, ФИО из шаблона

Запуск: python3 gen_integrity_variants.py
Выход: чек_целостность_A.pdf ... чек_целостность_E.pdf
"""
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SBP = BASE / "база_чеков" / "vtb" / "СБП"
TEMPLATE_15 = SBP / "15-03-26_00-00.pdf"
TEMPLATE_16 = SBP / "16-03-26_00-00.pdf"
CHECK3 = SBP / "check (3).pdf"


def run_gen_verified(out: Path, donor: Path, payer: str, recipient: str, amount: int,
                     date_str: str, phone: str | None, bank: str | None, account: str | None,
                     keep_id: bool) -> bool:
    """Вызов gen_verified_receipt (контент-патч, без add_glyphs)."""
    cmd = [
        sys.executable, str(BASE / "gen_verified_receipt.py"), str(out),
        "--donor", str(donor), "--payer", payer, "--recipient", recipient,
        "--amount", str(amount), "--date", date_str,
    ]
    if phone:
        cmd.extend(["--phone", phone])
    if bank:
        cmd.extend(["--bank", bank])
    if account:
        cmd.extend(["--account", account])
    if keep_id:
        cmd.append("--keep-id")
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def run_gen_receipt_15(out: Path, template: Path, payer: str, recipient: str, amount: int,
                       date_str: str, keep_id: bool) -> bool:
    """Вызов gen_receipt_15_03 (или с 16-03-26 через add_glyphs --target)."""
    if template == TEMPLATE_15:
        cmd = [
            sys.executable, str(BASE / "gen_receipt_15_03.py"), str(out),
            "--payer", payer, "--recipient", recipient, "--amount", str(amount),
            "--date", date_str, "--keep-phone", "--keep-bank",
        ]
    else:
        # 16-03-26: используем add_glyphs напрямую
        cmd = [
            sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
            "--target", str(template), "--id-from", str(template),
            "--keep-operation-id", "--replace", "--hybrid-safe",
            "--payer", payer, "--recipient", recipient,
            "--amount", str(amount), "--date", date_str.split(", ")[0],
            "--time", date_str.split(", ")[1] if ", " in date_str else datetime.now().strftime("%H:%M"),
            "-o", str(out),
        ]
    if keep_id:
        cmd.append("--keep-id")
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=90)
    return proc.returncode == 0


def main() -> int:
    if not TEMPLATE_15.exists():
        print(f"[ERROR] Шаблон не найден: {TEMPLATE_15}", file=sys.stderr)
        return 1

    now = datetime.now()
    dt = now + timedelta(minutes=59)
    date_str = dt.strftime("%d.%m.%Y, %H:%M")

    # Данные из 15-03-26
    try:
        from receipt_extractor import extract_from_receipt
        ex = extract_from_receipt(TEMPLATE_15)
        phone_tpl = (ex.get("phone_recipient") or "").strip() or "+7 (999) 000-00-00"
        bank_tpl = (ex.get("bank_recipient") or "").strip()
        payer_tpl = (ex.get("fio_payer") or "").strip() or "Анна Петрова С."
        recipient_tpl = (ex.get("fio_recipient") or "").strip() or "Александр Евгеньевич Ж."
        acc = ex.get("account_last4") or ""
        if not acc and ex.get("account"):
            m = re.search(r"\d{4}", str(ex.get("account", "")))
            if m:
                acc = m.group(0)
    except Exception:
        phone_tpl = "+7 (999) 000-00-00"
        bank_tpl = ""
        payer_tpl = "Анна Петрова С."
        recipient_tpl = "Александр Евгеньевич Ж."
        acc = ""

    # ФИО только из А Б В Д Е Ж И О П С Т (без add_glyphs)
    PAYER_CONTENT = "Ева Адаева В."
    RECIPIENT_CONTENT = "Осип Осипов В."

    variants = [
        ("A", "ФИО только из букв шаблона (контент-патч, БЕЗ add_glyphs)",
         lambda: run_gen_verified(
             BASE / "чек_целостность_A.pdf", TEMPLATE_15,
             PAYER_CONTENT, RECIPIENT_CONTENT, 5000, date_str,
             phone_tpl, bank_tpl, acc[-4:] if len(acc) >= 4 else None, keep_id=False)),
        ("B", "check(3) как донор (полный алфавит, контент-патч)",
         lambda: run_gen_verified(
             BASE / "чек_целостность_B.pdf", CHECK3,
             "Максим Андреевич В.", "Анна Петровна С.", 5000, date_str,
             phone_tpl if CHECK3.exists() else None,
             (extract_from_receipt(CHECK3).get("bank_recipient") or "").strip() if CHECK3.exists() else bank_tpl,
             None, keep_id=False) if CHECK3.exists() else False),
        ("C", "16-03-26 как шаблон (add_glyphs)",
         lambda: run_gen_receipt_15(
             BASE / "чек_целостность_C.pdf", TEMPLATE_16 if TEMPLATE_16.exists() else TEMPLATE_15,
             "Максим Андреевич В.", "Анна Петровна С.", 5000, date_str, keep_id=False)
         if (TEMPLATE_16.exists() or TEMPLATE_15.exists()) else False),
        ("D", "Без изменения Document ID (--keep-id)",
         lambda: run_gen_verified(
             BASE / "чек_целостность_D.pdf", TEMPLATE_15,
             PAYER_CONTENT, RECIPIENT_CONTENT, 5000, date_str,
             phone_tpl, bank_tpl, acc[-4:] if len(acc) >= 4 else None, keep_id=True)),
        ("E", "Минимальный патч: только дата+сумма, ФИО из шаблона",
         lambda: run_gen_verified(
             BASE / "чек_целостность_E.pdf", TEMPLATE_15,
             payer_tpl, recipient_tpl, 5000, date_str,
             phone_tpl, bank_tpl, acc[-4:] if len(acc) >= 4 else None, keep_id=False)),
    ]

    for key, desc, run in variants:
        out = BASE / f"чек_целостность_{key}.pdf"
        try:
            ok = run()
        except Exception as e:
            ok = False
            print(f"[WARN] Вариант {key}: {e}")
        if ok:
            print(f"✅ Вариант {key}: {out.name}")
            print(f"   {desc}")
        else:
            print(f"❌ Вариант {key}: ошибка")

    print("\nГотово. Проверь целостность каждого варианта.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
