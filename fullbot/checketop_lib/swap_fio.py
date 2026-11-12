#!/usr/bin/env python3
"""Поменять ФИО плательщика и получателя местами."""
import subprocess
import sys
from pathlib import Path

from vtb_patch_from_config import patch_from_values

BASE = Path(__file__).parent
INP = BASE / "12-03-26_00-00 4_10000.pdf"


def extract_fio_from_pdf(pdf_path: Path) -> tuple[str, str] | None:
    """Извлечь payer и recipient через report_last_letter_coords."""
    r = subprocess.run(
        [sys.executable, "report_last_letter_coords.py", str(pdf_path), "--csv"],
        cwd=BASE,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    payer = recipient = None
    for line in r.stdout.strip().split("\n"):
        if "ФИО (отправитель)" in line:
            parts = line.split(",", 4)
            if len(parts) >= 5:
                payer = parts[4].strip('"')
        elif "ФИО (получатель)" in line:
            parts = line.split(",", 4)
            if len(parts) >= 5:
                recipient = parts[4].strip('"')
    return (payer, recipient) if payer and recipient else None


def main():
    inp = INP
    if len(sys.argv) > 1:
        inp = Path(sys.argv[1])
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}")
        return 1

    fio = extract_fio_from_pdf(inp)
    if not fio:
        print("[ERROR] Не удалось извлечь ФИО из PDF (нужен PyMuPDF)")
        return 1
    old_payer, old_recipient = fio
    print(f"Было: плательщик={old_payer}, получатель={old_recipient}")
    print(f"Станет: плательщик={old_recipient}, получатель={old_payer}")

    data = bytearray(inp.read_bytes())
    out_bytes = patch_from_values(
        data,
        inp,
        payer=old_recipient,
        recipient=old_payer,
        keep_metadata=True,
    )
    out_path = inp.parent / f"{inp.stem}_swap.pdf"
    out_path.write_bytes(out_bytes)
    print(f"\n✅ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
