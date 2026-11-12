#!/usr/bin/env python3
"""Патч ВТБ 07-03-26_00-00 2.pdf: 10 ₽ → 5 000 ₽, Александр → Алексей (CID-совместимое имя).
Сохранение в чеки 07.03 с тем же именем.
"""
import re
import sys
import zlib
from pathlib import Path


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "07-03-26_00-00 2.pdf"
    out_dir = Path("чеки 07.03")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / inp.name

    data = bytearray(inp.read_bytes())

    # 10 ₽ → 5 000 ₽
    # CMap: 1=0014, 0=0013, space=0003, ₽=04@
    OLD_10 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    NEW_5000 = b'(\x00\x18)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'

    # Александр Евгеньевич Ж. → Алексей Евгеньевич П. (только плательщик, kern -16.66667)
    # CMap: А=021c,л=0247,е=0241,к=0246,с=024d,а=023c,н=0249,д=023e,р=0240,
    # Е=0221,в=023e,г=023f,ь=0258,и=0244,ч=0253, Ж=0222, П=022b, .=0011
    OLD_NAME = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x22)-16.66667 (\x00\x11)'
    )
    # Алексей Евгеньевич П.: 021c,0247,0241,0246,024d,0241,0245 (А л е к с е й)
    NEW_NAME = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02A)-16.66667 (\x02E)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x2b)-16.66667 (\x00\x11)'
    )

    # Tm: 10 ₽ → 5 000 ₽ (длиннее), сдвиг влево
    OLD_TM_10 = b"1 0 0 1 231.52501 72.37499 Tm"
    NEW_TM_5000 = b"1 0 0 1 210 72.37499 Tm"
    # Tm плательщика: Алексей короче Александр — сдвиг вправо для правой границы
    OLD_TM_NAME = b"1 0 0 1 149.8125 227.25 Tm"
    NEW_TM_NAME = b"1 0 0 1 161.6 227.25 Tm"

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

        if OLD_10 in new_dec:
            new_dec = new_dec.replace(OLD_10, NEW_5000)
            print("[OK] 10 ₽ -> 5 000 ₽")
        if OLD_NAME in new_dec:
            new_dec = new_dec.replace(OLD_NAME, NEW_NAME)
            print("[OK] Александр -> Алексей Евгеньевич П.")
        if OLD_TM_10 in new_dec and NEW_5000 in new_dec:
            new_dec = new_dec.replace(OLD_TM_10, NEW_TM_5000)
            print("[OK] Tm суммы сдвинут влево")
        if OLD_TM_NAME in new_dec and NEW_NAME in new_dec:
            new_dec = new_dec.replace(OLD_TM_NAME, NEW_TM_NAME)
            print("[OK] Tm плательщика — правая граница")

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
