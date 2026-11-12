#!/usr/bin/env python3
"""
Замена плательщика в чеке «Входящий перевод СБП» (07-03-26_16-46).

ВАЖНО: copy_font_cmap ломает отображение (CID→глиф не совпадает между чеками).
Используется content_stream_replace (PyMuPDF + Arial) — корректное отображение,
но размер файла ~450–800 KB вместо ~9 KB.

Использование:
  python3 patch_07_03_16_46.py
  python3 patch_07_03_16_46.py donors/07-03-26_16-46.pdf out.pdf
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Замена плательщика 07-03-26_16-46 (через content_stream_replace)")
    parser.add_argument("target", nargs="?", default="donors/07-03-26_16-46.pdf", help="Чек для патча")
    parser.add_argument("output", nargs="?", default="чеки 08.03/07-03-26_16-46.pdf", help="Выходной PDF")
    parser.add_argument("--payer", "-p", default="Арман Мелсикович Б.", help="Имя плательщика")
    args = parser.parse_args()

    tgt = Path(args.target).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()

    if not tgt.exists():
        print(f"[ERROR] Target не найден: {tgt}", file=sys.stderr)
        return 1

    script = ROOT / "content_stream_replace.py"
    if not script.exists():
        print(f"[ERROR] content_stream_replace.py не найден", file=sys.stderr)
        return 1

    # Замены: шапка "Александр Константинович Д" и таблица "Александр Константинович" / "Д"
    payer = args.payer
    cmd = [
        sys.executable,
        str(script),
        str(tgt),
        str(out),
        "--replace", f"Александр Константинович Д={payer}",
        "--replace", f"Александр Константинович={payer}",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
