#!/usr/bin/env python3
"""Генерация нового чека. Максим/Анна, время +59 мин, сумма 5000 ₽.

Режимы:
- --integrity: 02-03-26 (полный алфавит) + gen_verified (БЕЗ add_glyphs) → целостность OK
- по умолч.: 16-03-26 + add_glyphs → верификация OK
"""
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SBP = BASE / "база_чеков" / "vtb" / "СБП"
TEMPLATE_16 = SBP / "16-03-26_00-00.pdf"
TEMPLATE_15 = SBP / "15-03-26_00-00.pdf"
TEMPLATE_02 = SBP / "02-03-26_00-47.pdf"  # полный алфавит → content-only, целостность

DEFAULT_PAYER = "Максим Андреевич В."
DEFAULT_RECIPIENT = "Анна Петровна С."


def _decimal_safe_incs(hex_char: str) -> list[int]:
    """Инкременты 1..15, результат — цифра 0-9."""
    base = int(hex_char.upper(), 16)
    return [i for i in range(1, 16) if (base + i) % 16 < 10]


def replace_id_from_pdf(target_path: Path, id_source_path: Path) -> bool:
    """Заменить Document ID в target на ID из id_source (1 символ в pos=0)."""
    src = id_source_path.read_bytes()
    dst = bytearray(target_path.read_bytes())
    id_src = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', src)
    id_dst = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', dst)
    if not id_src or not id_dst:
        return False
    hex1 = id_src.group(1).decode().upper()
    pos = 0
    incs = _decimal_safe_incs(hex1[pos])
    if not incs:
        return False
    inc = incs[0]
    idx = "0123456789ABCDEF".find(hex1[pos])
    new_c = "0123456789ABCDEF"[(idx + inc) % 16]
    new1 = hex1[:pos] + new_c + hex1[pos + 1:]
    slot_len = id_dst.end(1) - id_dst.start(1)
    new_enc = new1.encode().ljust(slot_len)[:slot_len]
    dst[id_dst.start(1):id_dst.end(1)] = new_enc
    dst[id_dst.start(2):id_dst.end(2)] = new_enc
    target_path.write_bytes(dst)
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Новый чек для прохождения всех проверок")
    ap.add_argument("output", nargs="?", default="чек_новый.pdf")
    ap.add_argument("--payer", "-p", default=DEFAULT_PAYER, help="ФИО получателя")
    ap.add_argument("--recipient", "-r", default=DEFAULT_RECIPIENT, help="ФИО отправителя")
    ap.add_argument("--amount", "-a", type=int, default=5000, help="Сумма")
    ap.add_argument("--keep-id", action="store_true", help="НЕ менять Document ID")
    ap.add_argument("--integrity", "-i", action="store_true",
                    help="Режим целостности: 02-03-26 (полный алфавит) + gen_verified, без add_glyphs")
    ap.add_argument("--id-from", "-I", type=Path, default=None,
                    help="Document ID взять из этого PDF (1 символ изменён). Для прохождения верификации.")
    args = ap.parse_args()

    now = datetime.now()
    dt = now + timedelta(minutes=59)
    date_str = dt.strftime("%d.%m.%Y, %H:%M")
    out_path = Path(args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    # Режим целостности: 02-03-26 + gen_verified (content-only, без инъекции шрифта)
    if args.integrity and TEMPLATE_02.exists():
        try:
            from receipt_extractor import extract_from_receipt
            ex = extract_from_receipt(TEMPLATE_02)
            phone = (ex.get("phone_recipient") or "").strip() or "+7 (999) 000-00-00"
            bank = (ex.get("bank_recipient") or "").strip()
        except Exception:
            phone, bank = "+7 (999) 000-00-00", ""
        cmd = [
            sys.executable, str(BASE / "gen_verified_receipt.py"), str(out_path),
            "--donor", str(TEMPLATE_02),
            "--payer", args.payer, "--recipient", args.recipient,
            "--phone", phone, "--amount", str(args.amount), "--date", date_str,
        ]
        if bank:
            cmd.extend(["--bank", bank])
        if args.keep_id:
            cmd.append("--keep-id")
        print(f"[INFO] gen_verified (02-03-26, content-only — целостность OK)")
        print(f"[INFO] ФИО: {args.payer} → {args.recipient}, сумма {args.amount} ₽, время +59 мин")
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=60)
    else:
        # По умолчанию: 16-03-26 + add_glyphs (верификация OK)
        template = TEMPLATE_16 if TEMPLATE_16.exists() else TEMPLATE_15
        if not template.exists():
            print(f"[ERROR] Шаблон не найден: {template}", file=sys.stderr)
            return 1
        date_part, _, time_part = date_str.partition(", ")
        if not time_part:
            time_part = "00:00"
        try:
            from receipt_extractor import extract_from_receipt
            ex = extract_from_receipt(template)
            phone = (ex.get("phone_recipient") or "").strip()
            bank = (ex.get("bank_recipient") or "").strip()
        except Exception:
            phone, bank = "", ""
        cmd = [
            sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
            "--target", str(template),
            "--keep-operation-id", "--replace", "--hybrid-safe",
            "--payer", args.payer, "--recipient", args.recipient,
            "--amount", str(args.amount), "--date", date_part, "--time", time_part,
            "-o", str(out_path),
        ]
        if phone:
            cmd.extend(["--phone", phone])
        if bank:
            cmd.extend(["--bank", bank])
        if args.keep_id:
            cmd.extend(["--id-from", str(template)])
        print(f"[INFO] add_glyphs, Document ID: ротация шаблонов (свежий слот)")
        print(f"[INFO] ФИО: {args.payer} → {args.recipient}, сумма {args.amount} ₽, время +59 мин")
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:600]
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1

    # В режиме целостности: заменить ID на ID из 16-03-26 серии (для верификации)
    id_src = args.id_from
    if id_src is None and args.integrity:
        for candidate in [
            Path.home() / "Downloads" / "16-03-26_00-00 2.pdf",
            SBP / "16-03-26_00-00 2.pdf",
            BASE / "16-03-26_00-00 2.pdf",
        ]:
            if candidate.exists():
                id_src = candidate
                break
    if args.integrity and id_src and Path(id_src).exists():
        if replace_id_from_pdf(out_path, Path(id_src)):
            print(f"[INFO] Document ID: из {Path(id_src).name} (1 символ изменён)")

    print("✅ Готово:", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
