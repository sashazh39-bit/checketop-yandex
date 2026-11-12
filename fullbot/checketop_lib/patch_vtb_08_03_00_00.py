#!/usr/bin/env python3
"""Патч ВТБ 08-03-26_00-00.pdf: 50 ₽ → 5 000 ₽.
Сдвиг Tm влево, чтобы правый край суммы не съехал (ничего не смещается)."""
import re
import sys
import zlib
from pathlib import Path


def main():
    args = sys.argv[1:]
    keep_metadata = "--keep-metadata" in args or "-k" in args
    args = [a for a in args if a not in ("--keep-metadata", "-k")]
    inp = Path(args[0]) if args else Path("08-03-26_00-00.pdf")
    out = Path(args[1]) if len(args) >= 2 else inp.parent / f"{inp.stem}_5000.pdf"

    data = bytearray(inp.read_bytes())

    # 50 ₽ → 5 000 ₽ (kern -11.11111)
    # CMap: 5=0018, 0=0013, space=0003, ₽=04@
    OLD_50 = b'[(\x00\x18)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    NEW_5000 = b'[(\x00\x18)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'

    # Tm: 50 ₽ → 5 000 ₽ (длиннее), сдвиг влево, чтобы правый край не съехал
    OLD_TM_50 = b"1 0 0 1 229.6125 72.37499 Tm"
    NEW_TM_5000 = b"1 0 0 1 210 72.37499 Tm"

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

        if OLD_50 in new_dec:
            new_dec = new_dec.replace(OLD_50, NEW_5000)
            print("[OK] 50 ₽ -> 5 000 ₽")
        if OLD_TM_50 in new_dec and NEW_5000 in new_dec:
            new_dec = new_dec.replace(OLD_TM_50, NEW_TM_5000)
            print("[OK] Tm суммы сдвинут влево (ничего не съехало)")

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

    # Метаданные: по умолчанию openhtmltopdf → PDF Generator; с --keep-metadata оставляем как в исходнике
    if not keep_metadata:
        OLD_PRODUCER = b"/Producer (openhtmltopdf.com)"
        NEW_PRODUCER = b"/Producer (PDF Generator    )"  # та же длина
        if OLD_PRODUCER in data:
            data = data.replace(OLD_PRODUCER, NEW_PRODUCER)
            print("[OK] Producer: openhtmltopdf.com -> PDF Generator")

    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
