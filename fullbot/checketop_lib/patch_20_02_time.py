#!/usr/bin/env python3
"""Патч времени в 20-02-26_final.pdf: 13:26 -> 13:27.

Замена последней цифры минут (6 -> 7) в content stream.
Паттерн: ... : 2 6 — уникален для времени (двоеточие только в HH:MM).
CID: 6=0x0019, 7=0x001A.
"""
import re
import zlib
from pathlib import Path


# Паттерн ":" (0x001d) "2" (0x0015) "6" (0x0019) — конец времени 13:26
# Заменяем (\x00\x19) на (\x00\x1a) для 6 -> 7
OLD_26 = b'(\x00\x1d)-16.66667 (\x00\x15)-16.66667 (\x00\x19)'
NEW_27 = b'(\x00\x1d)-16.66667 (\x00\x15)-16.66667 (\x00\x1a)'


def main():
    inp = Path("20-02-26_final.pdf")
    out = Path("20-02-26_final.pdf")  # перезапись
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        return 1

    data = bytearray(inp.read_bytes())
    patched = False

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

        if OLD_26 not in dec:
            continue

        new_dec = dec.replace(OLD_26, NEW_27, 1)
        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        # Обновить /Length (длина числа может измениться)
        len_num_end = len_num_start + len(old_len_str)
        data[len_num_start : len_num_end] = new_len_str[:len(old_len_str)].ljust(len(old_len_str))
        patched = True
        break

    if not patched:
        # Попробуем альтернативный кернинг
        for kern in (b"-21.42857", b"-11.11111", b"-8.33333"):
            old = kern.join(OLD_26.split(b"-16.66667"))
            if not old.startswith(b'(\x00\x1d)'):
                old = b'(\x00\x1d)' + kern + b' (\x00\x15)' + kern + b' (\x00\x19)'
            new = kern.join(NEW_27.split(b"-16.66667"))
            if not new.startswith(b'(\x00\x1d)'):
                new = b'(\x00\x1d)' + kern + b' (\x00\x15)' + kern + b' (\x00\x1a)'
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
                if old in dec:
                    new_dec = dec.replace(old, new, 1)
                    new_raw = zlib.compress(new_dec, 6)
                    delta = len(new_raw) - stream_len
                    old_len_str = str(stream_len).encode()
                    new_len_str = str(len(new_raw)).encode()
                    data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
                    data[len_num_start : len_num_start + len(old_len_str)] = new_len_str[:len(old_len_str)].ljust(len(old_len_str))
                    patched = True
                    break
            if patched:
                break

    if patched:
        out.write_bytes(data)
        print(f"[OK] Время изменено: 13:26 -> 13:27 в {out}")
    else:
        print("[ERROR] Паттерн времени не найден в PDF")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
