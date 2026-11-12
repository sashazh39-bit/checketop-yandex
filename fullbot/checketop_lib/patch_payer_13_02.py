#!/usr/bin/env python3
"""Патч 13-02-26: замена имени плательщика на Арман Мелсиков Б.
Использование: python3 patch_payer_13_02.py input.pdf output.pdf
"""
import re
import sys
import zlib
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_payer_13_02.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
    orig_size = len(data)

    # Арман Мелсикович Б. — CIDs: ... Б=021D, .=0011
    # 0228 (М) экранируем: (\x02\()
    NEW_NAME = (
        b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\\()-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02F)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)'
    )
    # Tm: выравнивание по правому краю (right_edge=257.08, width "Арман Мелсикович Б."=90.24)
    # Tm_x = 257.08 - 90.24 = 166.84
    OLD_TM = b"1 0 0 1 149.8125 227.25 Tm"
    OLD_TM_165 = b"1 0 0 1 165 227.25 Tm"  # уже пропатчен ранее
    NEW_TM = b"1 0 0 1 166.84 227.25 Tm"

    # OLD: Александр Евгеньевич Ж.
    OLD_JE_16 = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
    # OLD: Арман Евгеньевич Ж. (если уже пропатчен)
    OLD_ARMAN_JE = (
        b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
    # OLD: Арман Мелсиков Б (без ич)
    OLD_ARMAN_MELSIKOV = (
        b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\\()-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02F)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667'
    )
    # OLD: Арман Мелсикович Б (с ич, без точки — добавляем точку)
    OLD_ARMAN_MELSIKOVICH = (
        b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\\()-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02F)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667'
    )
    # OLD: Бабаян Арман М (если файл уже пропатчен — заменяем на Арман)
    OLD_BABAYAN = (
        b'(\x02\x1d)-16.66667 (\x02\x3c)-16.66667 (\x02\x3d)-16.66667 (\x02\x3c)-16.66667 (\x02\x5b)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\x1c)-16.66667 (\x02\x4c)-16.66667 (\x02\x48)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 (\x02\x28)-16.66667'
    )
    # Экранированная версия (М=0228 использует \()
    OLD_BABAYAN_ESC = (
        b'(\x02\x1d)-16.66667 (\x02\x3c)-16.66667 (\x02\x3d)-16.66667 (\x02\x3c)-16.66667 (\x02\x5b)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\x1c)-16.66667 (\x02\x4c)-16.66667 (\x02\x48)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 (\x02\\()-16.66667'
    )

    mods = []
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
        if OLD_JE_16 in new_dec:
            new_dec = new_dec.replace(OLD_JE_16, NEW_NAME)
        elif OLD_ARMAN_JE in new_dec:
            new_dec = new_dec.replace(OLD_ARMAN_JE, NEW_NAME)
        elif OLD_ARMAN_MELSIKOV in new_dec:
            new_dec = new_dec.replace(OLD_ARMAN_MELSIKOV, NEW_NAME)
        elif OLD_BABAYAN in new_dec:
            new_dec = new_dec.replace(OLD_BABAYAN, NEW_NAME)
        elif OLD_BABAYAN_ESC in new_dec:
            new_dec = new_dec.replace(OLD_BABAYAN_ESC, NEW_NAME)
        # Tm: выравнивание по правому краю (даже если имя уже заменено)
        if NEW_NAME in new_dec:
            if OLD_TM in new_dec:
                new_dec = new_dec.replace(OLD_TM, NEW_TM)
            elif OLD_TM_165 in new_dec:
                new_dec = new_dec.replace(OLD_TM_165, NEW_TM)
        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Блок плательщика не найден")
        sys.exit(1)

    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
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

    print("[OK] -> Арман Мелсиков Б")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
