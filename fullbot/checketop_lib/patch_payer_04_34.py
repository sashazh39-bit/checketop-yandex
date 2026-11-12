#!/usr/bin/env python3
"""Патч donors/07-03-26_04-34 (1).pdf: Александр Евгеньевич Ж. → Арман Мелсикович Б.
Берёт имя из чеки 07.03/13-02-26_20-29*.pdf. Сохранение в чеки 08.03.

Использует замену на уровне сырых байтов (без pikepdf), чтобы сохранить весь контент
и изображения. pikepdf при obj.write() + save() портил структуру PDF.
"""
import re
import sys
import zlib
from pathlib import Path

# Александр Евгеньевич Ж. (kern -16.66667)
OLD_NAME = (
    b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
)

# Арман Мелсикович Б. (kern -16.66667)
# ВАЖНО: CID 0x0228 (М) — 0x28 '(' экранируем как \x02\( (как в build_tj patch_payer_sbp)
NEW_NAME = (
    b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
    b'(\\x02\\()-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02F)-16.66667 (\x02J)-16.66667 (\x02@)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)'
)


def main():
    base = Path(__file__).parent
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else base / "donors" / "07-03-26_04-34 (1).pdf"
    out_dir = base / "чеки 08.03"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "07-03-26_04-34.pdf"

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
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
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        if OLD_NAME not in dec:
            continue
        new_dec = dec.replace(OLD_NAME, NEW_NAME)
        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Не найдено 'Александр Евгеньевич Ж.' в target.")
        sys.exit(1)

    # Применяем с конца, чтобы не сбивать позиции
    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        # Обновить xref: все offset после stream_start сдвинуть на delta
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
    print("[OK] Александр Евгеньевич Ж. -> Арман Мелсикович Б.")
    print(f"[OK] Сохранено: {out}")
