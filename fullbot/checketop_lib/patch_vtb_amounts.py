#!/usr/bin/env python3
"""CID-патч для PDF ВТБ с форматом (cid)-kern в content stream.
Замена: 14 391 → 431 730, 100 → 3 000
"""
import re
import zlib
from pathlib import Path


def main():
    inp = Path("07-03-26_01-55.pdf")
    out = Path("07-03-26_01-55_vtb.pdf")
    data = inp.read_bytes()

    # CMap: 0=0013, 1=0014, 2=0015, 3=0016, 4=0017, 5=0018, 6=0019, 7=001A, 8=001B, 9=001C, space=0003
    # 14 391 = 1,4,space,3,9,1
    OLD_14391 = b'(\x00\x14)-16.66667 (\x00\x17)-16.66667 (\x00\x03)-16.66667 (\x00\x16)-16.66667 (\x00\x1c)-16.66667 (\x00\x14)'
    # 431 730 = 4,3,1,space,7,3,0  (7=001A). Оригинальный кернинг как в PDF
    NEW_431730 = b'(\x00\x17)-16.66667 (\x00\x16)-16.66667 (\x00\x14)-16.66667 (\x00\x03)-16.66667 (\x00\x1a)-16.66667 (\x00\x16)-16.66667 (\x00\x13)'

    # 100 = 1,0,0
    OLD_100 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)'
    # 3 000 = 3,space,0,0,0. Оригинальный кернинг
    NEW_3000 = b'(\x00\x16)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)'

    # Сдвиг Tm влево: "431 730" (7 цифр) и "3 000" (5 цифр) длиннее оригинала
    OLD_TM_14391 = b"1 0 0 1 210.75 120.74999 Tm"
    NEW_TM_14391 = b"1 0 0 1 198 120.74999 Tm"  # -12.75 pts влево
    OLD_TM_100 = b"1 0 0 1 223.20001 72.37499 Tm"
    NEW_TM_100 = b"1 0 0 1 205 72.37499 Tm"  # -18 pts влево

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue

        new_dec = dec
        if OLD_14391 in new_dec:
            new_dec = new_dec.replace(OLD_14391, NEW_431730)
            print("[OK] 14 391 -> 431 730")
        if OLD_100 in new_dec:
            new_dec = new_dec.replace(OLD_100, NEW_3000)
            print("[OK] 100 -> 3 000")
        # Сдвиг позиции (Tm) влево для компенсации более длинного текста
        if OLD_TM_14391 in new_dec and NEW_431730 in new_dec:
            new_dec = new_dec.replace(OLD_TM_14391, NEW_TM_14391)
            print("[OK] Tm 14 391 сдвинут влево")
        if OLD_TM_100 in new_dec and NEW_3000 in new_dec:
            new_dec = new_dec.replace(OLD_TM_100, NEW_TM_100)
            print("[OK] Tm 100 сдвинут влево")

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)
            # Rebuild data with new stream (handles size change)
            data = (
                data[:stream_start]
                + new_raw
                + data[stream_start + stream_len :]
            )
            len_num_start = m.start(2)
            num_end = len_num_start + len(old_len_str)
            data = data[:len_num_start] + new_len_str + data[num_end:]
            # Update xref
            xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
            if xref_m:
                entries = bytearray(xref_m.group(3))
                for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                    offset = int(em.group(1))
                    if offset > stream_start:
                        entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                data = data[: xref_m.start(3)] + bytes(entries) + data[xref_m.end(3) :]
            startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
            if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
                pos = startxref_m.start(1)
                old_pos = int(startxref_m.group(1))
                new_pos_str = str(old_pos + delta).encode()
                data = data[:pos] + new_pos_str + data[pos + len(str(old_pos)) :]

    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
