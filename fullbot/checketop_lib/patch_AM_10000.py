#!/usr/bin/env python3
"""Патч AM_1773090400215.pdf:
- Сумма: 10 -> 10 000
Использование: python3 patch_AM_10000.py input.pdf output.pdf
"""
import re
import sys
import hashlib
import zlib
from pathlib import Path


# 10 (один глиф 000A) -> 10 000 (0012=1, 000B=0, 000A=space, 000B=0, 000B=0, 000B=0)
OLD_AMOUNT = b'<000A> Tj'
NEW_AMOUNT = b'<0012000B000A000B000B000B> Tj'


def update_id(data: bytearray) -> bool:
    """Обновить /ID в trailer."""
    id_m = re.search(rb'/ID\s*\[\s*(<[0-9a-fA-F]+>\s*<[0-9a-fA-F]+>)\s*\]', bytes(data))
    if id_m:
        old_id = id_m.group(1)
        h = hashlib.md5(bytes(data)).hexdigest().upper()
        new_id = f"<{h}> <{h}>".encode()
        data[id_m.start(1):id_m.end(1)] = new_id[:len(old_id)].ljust(len(old_id))
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_AM_10000.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
    content_changed = False

    # Собираем все потоки и обрабатываем с конца — чтобы не ломать смещения при модификации
    matches = list(re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL))

    for m in reversed(matches):
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
        changed = False

        if OLD_AMOUNT in new_dec:
            new_dec = new_dec.replace(OLD_AMOUNT, NEW_AMOUNT)
            changed = True

        if changed:
            content_changed = True
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

    if update_id(data):
        print("[OK] /ID обновлён")

    if content_changed:
        print("[OK] Сумма: 10 -> 10 000")
    if not content_changed:
        if update_id(data):
            print("[OK] /ID обновлён (refresh)")
        else:
            print("[ERROR] Целевые блоки не найдены")
            sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
