#!/usr/bin/env python3
"""Патч 20-02-26: замена Илья Станиславович С. на Арман мелсикович Б.

Без замены шрифта — только CIDs из оригинала. Буква М отсутствует в subset,
используется м (0248). copy_font_cmap ломает отображение — не использовать.

Использование: python3 patch_20_02_payer.py input.pdf output.pdf
"""
import re
import sys
import zlib
from pathlib import Path

# OLD: Илья Станиславович С. (256 bytes)
OLD_TJ = (
    b'(\x02$)-16.66667 (\x02G)-16.66667 (\x02X)-16.66667 (\x02[)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02-)-16.66667 (\x02N)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02D)-16.66667 '
    b'(\x02M)-16.66667 (\x02G)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 '
    b'(\x02S)-16.66667 (\x00\x03)-16.66667 (\x02-)-16.66667 (\x00\x11)'
)

# NEW: Арман мелсикович Б. (256 bytes, м=0248 — шрифт 20-02 не содержит заглавную М)
NEW_TJ = (
    b'(\x02\x1c)-16.66667 (\x02L)-16.66667 (\x02H)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02H)-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02F)-16.66667 '
    b'(\x02J)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)'
)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Входной PDF")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--repair", "-r", action="store_true", help="Восстановить PDF (qpdf --linearize)")
    args = parser.parse_args()

    inp = Path(args.input).resolve()
    out = Path(args.output).resolve()
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
    orig_size = len(data)

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

        if OLD_TJ not in dec:
            continue

        new_dec = dec.replace(OLD_TJ, NEW_TJ)
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

    print("[OK] Илья Станиславович С. -> Арман Мелсикович Б.")

    # Сохранение размера файла: дополнение %-комментарием после %%EOF
    if len(data) < orig_size:
        pad_len = orig_size - len(data)
        data = data + (b"\n%" + b" " * (pad_len - 2) if pad_len >= 2 else b" " * pad_len)
    elif len(data) > orig_size:
        print(f"[WARN] Размер вырос на {len(data) - orig_size} байт")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out} ({len(data)} bytes, было {orig_size})")

    if args.repair:
        try:
            import subprocess
            tmp = out.with_suffix(".tmp.pdf")
            r = subprocess.run(["qpdf", "--linearize", str(out), str(tmp)], capture_output=True, text=True, timeout=10)
            if r.returncode in (0, 3) and tmp.exists():
                tmp.replace(out)
                print("[OK] PDF восстановлен (qpdf --linearize)")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"[WARN] qpdf не выполнен: {e}")


if __name__ == "__main__":
    main()
