#!/usr/bin/env python3
"""Патч Unknown 504.pdf на уровне stream: 10 → 5 000.

Без добавления шрифтов — только замена байт в content stream.
Сохранение структуры, /ID, метаданных. Минимальный прирост размера.
Чек должен распознаваться (нет новых объектов).
"""
import re
import sys
import zlib
from pathlib import Path

# CID: 0131=0, 0132=1, 0136=5, 0003=space
# Верхняя сумма (Итого): /F2 — subset без "5". Меняем на /F1 (есть в номере карты).
BLOCK1_OLD = b"1 0 0 1 217.3 304.39 Tm\n/F2 16 Tf\n0.2 0.2 0.2 rg\n(\x01\x32\x01\x31\x00\x03)Tj"
BLOCK1_NEW = b"1 0 0 1 193.3 304.39 Tm\n/F1 16 Tf\n0.2 0.2 0.2 rg\n(\x01\x36\x00\x03\x01\x31\x01\x31\x01\x31\x00\x03)Tj"

# Нижняя сумма (Сумма): /F1 уже есть "5"
BLOCK2_OLD = b"1 0 0 1 232.76 228.78 Tm\n/F1 9 Tf\n0.2 0.2 0.2 rg\n(\x01\x32\x01\x31\x00\x03)Tj"
BLOCK2_NEW = b"1 0 0 1 218.7 228.78 Tm\n/F1 9 Tf\n0.2 0.2 0.2 rg\n(\x01\x36\x00\x03\x01\x31\x01\x31\x01\x31\x00\x03)Tj"


def patch(inp: Path, out: Path) -> bool:
    data = bytearray(inp.read_bytes())

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if BLOCK1_OLD not in dec or BLOCK2_OLD not in dec:
            continue

        new_dec = dec.replace(BLOCK1_OLD, BLOCK1_NEW).replace(BLOCK2_OLD, BLOCK2_NEW)

        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

        print("[OK] 10 → 5 000 (stream-level, без новых шрифтов)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return True

    return False


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/Unknown 504.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_5000.pdf")

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    if patch(inp, out):
        orig_sz = inp.stat().st_size
        new_sz = out.stat().st_size
        print(f"[OK] Сохранено: {out} ({orig_sz} → {new_sz} байт, Δ{new_sz - orig_sz:+d})")
        return 0

    print("[ERROR] Блок 10 не найден", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
