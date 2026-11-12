#!/usr/bin/env python3
"""Патч: сумма 10 ₽ -> 100 ₽
zlib level 6, метаданные/xref сохраняются.

Использование: python3 patch_10_to_100.py input.pdf output.pdf
"""
import re
import sys
import zlib
from pathlib import Path

# 10 ₽ -> 100 ₽ (1=0014, 0=0013, space=0003, ₽=04@)
OLD_AMOUNT = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
NEW_AMOUNT = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
OLD_TM = b"1 0 0 1 231.52501 72.37499 Tm"
NEW_TM = b"1 0 0 1 223.34501 72.37499 Tm"  # выравнивание по правому краю (5 глифов)


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_10_to_100.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

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
        if OLD_AMOUNT not in dec:
            continue

        new_dec = dec.replace(OLD_AMOUNT, NEW_AMOUNT)
        if OLD_TM in new_dec:
            new_dec = new_dec.replace(OLD_TM, NEW_TM)

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        data[len_num_start : len_num_start + len(old_len_str)] = new_len_str

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

        print("[OK] 10 ₽ -> 100 ₽")
        break
    else:
        print("[ERROR] Сумма 10 ₽ не найдена")
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
