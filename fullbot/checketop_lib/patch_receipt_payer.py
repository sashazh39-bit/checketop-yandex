#!/usr/bin/env python3
"""Патч плательщика в receipt: -> Юлия Константиновна Д.

Использование: python3 patch_receipt_payer.py input.pdf output.pdf
"""
import re
import sys
import zlib
from pathlib import Path


def build_tj(cids: list[str], kern: str = "-16.66667") -> bytes:
    kern_b = kern.encode()
    parts = []
    for cid_hex in cids:
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        s = bytes([0x28, h, l, 0x29])
        parts.append(s + kern_b + b" ")
    return b"".join(parts)


# Текущий плательщик (Анастасия Александрова Р.) — точное совпадение с PDF
OLD_TJ = (
    b'(\x02\x1c)-16.66667 (\x02I)-16.66667 (\x02<)-16.66667 (\x02M)-16.66667 (\x02N)-16.66667 (\x02<)-16.66667 '
    b'(\x02M)-16.66667 (\x02D)-16.66667 (\x02[)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02A)-16.66667 '
    b'(\x02A)-16.66667 (\x02>)-16.66667 (\x02I)-16.66667 (\x02<)-16.66667 (\x00\x03)-16.66667 (\x02R)-16.66667 (\x00\x11)-16.66667 '
)
# Без [ и ] — replace ищет внутри TJ

# Юлия Константиновна Д. (023A=Ю, 0247=л, 0244=и, 025B=я, 0226=К, 0220=Д)
NEW_CIDS = [
    "023A", "0247", "0244", "025B", "0003",
    "0226", "024A", "0249", "024D", "024E", "023C", "0249", "024E", "0244", "0249", "024A", "023E", "0249", "023C", "0003",
    "0220", "0011",
]
NEW_TJ = build_tj(NEW_CIDS)


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_receipt_payer.py input.pdf output.pdf")
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
        if OLD_TJ not in dec:
            continue

        new_dec = dec.replace(OLD_TJ, NEW_TJ)
        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        data[len_num_start : len_num_start + len(old_len_str)] = new_len_str[:len(old_len_str)].ljust(len(old_len_str))

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0:
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

        print("[OK] Плательщик: Юлия Константиновна Д.")
        break
    else:
        print("[ERROR] Блок плательщика не найден")
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
