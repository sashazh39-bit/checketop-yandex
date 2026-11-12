#!/usr/bin/env python3
"""Патч Ozon Bank PDF: 50 ₽ → 5 000 ₽.
CID-замена с коррекцией Tm (сдвиг влево для выравнивания по правому краю)."""
import re
import sys
import zlib
from pathlib import Path


def main():
    args = sys.argv[1:]
    inp = Path(args[0]) if args else Path("ozonbank_document_20260308153137.pdf")
    out = Path(args[1]) if len(args) >= 2 else inp.parent / "vega08.03" / f"{inp.stem}_5000.pdf"

    out.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(inp.read_bytes())

    # Block 1: Итого 50 ₽ (font 24, F5) - Tm 302.40625 134
    # Block 2: Сумма 50 ₽ (font 14, F4) - Tm 323.23438 257
    # 50 ₽ → 5 000 ₽: добавляем space+0+0, сдвигаем Tm влево

    OLD_BLOCK_1 = (
        b"1 0 0 -1 302.40625 134 Tm\n"
        b"<013E> Tj\n"
        b"12.6719666 0 Td <0139> Tj\n"
        b"14.9039612 0 Td <0003> Tj\n"
        b"6.3119812 0 Td <03B8> Tj\n"
        b"EMC\n"
        b"ET"
    )
    # 5 [sp] 0 0 0 [sp] ₽ — пробел перед ₽
    NEW_BLOCK_1 = (
        b"1 0 0 -1 259.99 134 Tm\n"
        b"<013E> Tj\n"
        b"12.6719666 0 Td <0003> Tj\n"
        b"6.3119812 0 Td <0139> Tj\n"
        b"14.9039612 0 Td <0139> Tj\n"
        b"14.9039612 0 Td <0139> Tj\n"
        b"6.3119812 0 Td <0003> Tj\n"
        b"6.3119812 0 Td <0003> Tj\n"
        b"6.3119812 0 Td <03B8> Tj\n"
        b"EMC\n"
        b"ET"
    )

    OLD_BLOCK_2 = (
        b"1 0 0 -1 323.23438 257 Tm\n"
        b"<013E> Tj\n"
        b"7.1959686 0 Td <0139> Tj\n"
        b"7.9239655 0 Td <0003> Tj\n"
        b"4.0599823 0 Td <03B8> Tj\n"
        b"EMC\n"
        b"ET"
    )
    # 5 [sp] 0 0 0 [sp] ₽ — пробел перед ₽
    NEW_BLOCK_2 = (
        b"1 0 0 -1 299.28 257 Tm\n"
        b"<013E> Tj\n"
        b"7.1959686 0 Td <0003> Tj\n"
        b"4.0599823 0 Td <0139> Tj\n"
        b"7.9239655 0 Td <0139> Tj\n"
        b"7.9239655 0 Td <0139> Tj\n"
        b"4.0599823 0 Td <0003> Tj\n"
        b"4.0599823 0 Td <0003> Tj\n"
        b"4.0599823 0 Td <03B8> Tj\n"
        b"EMC\n"
        b"ET"
    )

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
        if b"BT" not in dec:
            continue

        new_dec = dec
        if OLD_BLOCK_1 in new_dec:
            new_dec = new_dec.replace(OLD_BLOCK_1, NEW_BLOCK_1)
            print("[OK] Итого: 50 ₽ -> 5 000 ₽ (Tm скорректирован)")
        if OLD_BLOCK_2 in new_dec:
            new_dec = new_dec.replace(OLD_BLOCK_2, NEW_BLOCK_2)
            print("[OK] Сумма: 50 ₽ -> 5 000 ₽ (Tm скорректирован)")

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)

            data = (
                data[:stream_start]
                + new_raw
                + data[stream_start + stream_len :]
            )
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

    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
