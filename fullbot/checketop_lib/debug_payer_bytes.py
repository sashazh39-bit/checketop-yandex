#!/usr/bin/env python3
"""Отладка: вывести точные байты «Имя плательщика» в правой колонке target PDF."""
import re
import zlib
from pathlib import Path

def main():
    base = Path(__file__).parent
    target = base / "donors" / "07-03-26_04-34 (1).pdf"
    if not target.exists():
        target = base / "donors" / "07-03-26_04-34.pdf"
    if not target.exists():
        print("Target не найден")
        return

    data = target.read_bytes()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        # Ищем все TJ блоки с x>100 (правая колонка) и y около 348.75 или 227.25
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            if x > 100 and (abs(y - 348.75) <= 2 or abs(y - 227.25) <= 2):
                tj_block = mm.group(7)
                # Проверяем, содержит ли «Имя плательщика» (первые глифы: И=\x02\x1d, м=\x02<)
                if b'\x02\x1d' in tj_block and b'\x02<' in tj_block:
                    print(f"=== Найден TJ при x={x}, y={y} (правая колонка) ===")
                    print("Hex dump (первые 200 байт):")
                    print(tj_block[:200].hex())
                    print("\nRepr:")
                    print(repr(tj_block[:300]))
                    print("\nПолный TJ block repr:")
                    print(repr(tj_block))
                    print()

if __name__ == "__main__":
    main()
