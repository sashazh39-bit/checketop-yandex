#!/usr/bin/env python3
"""Патч 08-03-26_00-00 2.pdf:
- Плательщик: Александр Евгеньевич Ж. -> Евгений Александрович Е.
- Сумма: 10 ₽ -> 5 000 ₽
Метаданные и /ID сохраняются, xref обновляется корректно.

Использование: python3 patch_08_03_2_payer_amount.py input.pdf output.pdf
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
        if l == 0x28:
            s = b'(\x02\\()'
        elif l == 0x29:
            s = b'(\x02\\)'
        else:
            s = bytes([0x28, h, l, 0x29])
        parts.append(s + kern_b + b" ")  # kern уже содержит "-"
    return b"".join(parts)


# --- Плательщик ---
OLD_TJ = (
    b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 '
    b'(\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 '
    b'(\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
)
NEW_CIDS = [
    "0221", "023E", "023F", "0241", "0249", "0244", "0245", "0003",
    "021C", "0247", "0241", "0246", "024D", "023C", "0249", "0240", "024C", "024A", "023E", "0244", "0253", "0003",
    "0221", "0011",
]
NEW_TJ = build_tj(NEW_CIDS)
# Исправление ошибочного kerning (-- вместо -) в уже пропатченном файле
NEW_TJ_WRONG = NEW_TJ.replace(b"-16.66667", b"--16.66667")  # старый баг

OLD_TM_VARIANTS = [
    b"1 0 0 1 149.8125 227.25 Tm",
    b"1 0 0 1 133.19 227.25 Tm",
    b"1 0 0 1 148.40 227.25 Tm",
    b"1 0 0 1 147.21 227.25 Tm",
]
NEW_TM_PAYER = b"1 0 0 1 147.21 227.25 Tm"

# --- Сумма 10 ₽ или 50 ₽ -> 5 000 ₽ ---
OLD_AMOUNT_10 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
OLD_AMOUNT_50 = b'(\x00\x18)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
NEW_AMOUNT = b'(\x00\x18)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
OLD_TM_AMOUNT = b"1 0 0 1 231.52501 72.37499 Tm"
OLD_TM_AMOUNT_50 = b"1 0 0 1 229.6125 72.37499 Tm"
NEW_TM_AMOUNT = b"1 0 0 1 210 72.37499 Tm"


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_08_03_2_payer_amount.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
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

        new_dec = dec
        changed = False

        # Плательщик
        if OLD_TJ in new_dec:
            new_dec = new_dec.replace(OLD_TJ, NEW_TJ)
            changed = True
        elif NEW_TJ_WRONG in new_dec:
            new_dec = new_dec.replace(NEW_TJ_WRONG, NEW_TJ)
            changed = True
        if NEW_TJ in new_dec or NEW_TJ_WRONG in new_dec:
            for old_tm in OLD_TM_VARIANTS:
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, NEW_TM_PAYER)
                    changed = True
                    break

        # Сумма
        if OLD_AMOUNT_10 in new_dec:
            new_dec = new_dec.replace(OLD_AMOUNT_10, NEW_AMOUNT)
            changed = True
        elif OLD_AMOUNT_50 in new_dec:
            new_dec = new_dec.replace(OLD_AMOUNT_50, NEW_AMOUNT)
            changed = True
        if NEW_AMOUNT in new_dec:
            for old_tm in (OLD_TM_AMOUNT, OLD_TM_AMOUNT_50):
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, NEW_TM_AMOUNT)
                    changed = True
                    break

        if changed:
            new_raw = zlib.compress(new_dec, 6)  # level 6 как в оригинале (снижает след правки)
            mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Целевые блоки не найдены")
        sys.exit(1)

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

    print("[OK] Евгений Александрович Е., 5 000 ₽")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out} ({len(data)} bytes, метаданные и /ID сохранены)")


if __name__ == "__main__":
    main()
