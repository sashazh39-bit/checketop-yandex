#!/usr/bin/env python3
"""Сканирует content stream: все Tm+TJ блоки с -11.11111 (сумма) и соседние."""
import re
import zlib
import sys
from pathlib import Path


def scan(pdf_path: Path):
    data = pdf_path.read_bytes()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        ln = int(m.group(2))
        start = m.end()
        if start + ln > len(data):
            continue
        try:
            dec = zlib.decompress(data[start : start + ln])
        except Exception:
            continue
        if b"BT" not in dec:
            continue

        # Все Tm ... TJ с -11.11111 (сумма)
        pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)(\s*\r?\n[^\[]*)?\[([^\]]+)\]\s*TJ"
        blocks_11 = []
        for mt in re.finditer(pat, dec):
            tj = mt.group(6)
            if b"-11.11111" in tj:
                blocks_11.append({
                    "tm_x": float(mt.group(2)),
                    "y": float(mt.group(4)),
                    "tj_preview": tj[:120].decode("latin-1", errors="replace"),
                    "full": mt.group(0)[:200].decode("latin-1", errors="replace"),
                })

        # Все Tm+TJ с -16.66667 около y 70-80 (подпись "Сумма операции")
        blocks_16 = []
        for mt in re.finditer(pat, dec):
            tj = mt.group(6)
            y = float(mt.group(4))
            if b"-16.66667" in tj and 60 < y < 90:
                blocks_16.append({
                    "tm_x": float(mt.group(2)),
                    "y": y,
                    "tj_preview": tj[:120].decode("latin-1", errors="replace"),
                })

        if blocks_11 or blocks_16:
            print(f"=== Stream (dec len={len(dec)}) ===\n")
            print("Блоки с -11.11111 (сумма):")
            for i, b in enumerate(blocks_11):
                print(f"  [{i}] tm_x={b['tm_x']:.2f} y={b['y']:.2f}")
                print(f"      tj: {b['tj_preview'][:80]}...")
            print("\nБлоки с -16.66667 при y 60-90 (подпись?):")
            for i, b in enumerate(blocks_16):
                print(f"  [{i}] tm_x={b['tm_x']:.2f} y={b['y']:.2f}")
                print(f"      tj: {b['tj_preview'][:80]}...")
            print()
            # Контекст: что идёт перед/после первого блока -11.11111
            if blocks_11:
                first_y = blocks_11[0]["y"]
                # Найти все блоки в порядке появления, с y близким к first_y
                all_near = []
                for mt in re.finditer(pat, dec):
                    y = float(mt.group(4))
                    if abs(y - first_y) < 5 or abs(y - 72) < 15:
                        all_near.append((y, mt.group(2).decode(), mt.group(6)[:60]))
                print("Блоки около y суммы (72±15):")
                for y, tm_x, tj in sorted(all_near, key=lambda x: -x[0]):
                    k = "-11.11111" if b"-11.11111" in (tj if isinstance(tj, bytes) else tj.encode()) else "-16"
                    print(f"  y={y:.2f} tm_x={tm_x} kern~{k} tj={str(tj)[:50]}")
        break  # первый stream с BT


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/12-03-26_00-00 4.pdf")
    scan(p)
