#!/usr/bin/env python3
"""
Патч чека Альфа-Банка PDF Document 18.pdf:
  10 RUR  → 3 036 RUR
  1,13 TJS → 343,06 TJS
Сохраняет метаданные, структуру, xref.
"""
import re
import zlib
from pathlib import Path

INP = Path('/Users/aleksandrzerebatav/Desktop/чекетоп/PDF Document 18_patched.pdf')
OUT = Path('/Users/aleksandrzerebatav/Desktop/чекетоп/PDF Document 18_patched2.pdf')

# CID mappings (lowercase, as used in this PDF's content streams):
# Digits: 0→000e, 1→000b, 2→0010, 3→000f, 4→0024, 5→0033,
#         6→0011, 7→0032, 8→000c, 9→0031
# NBSP→000a, comma→0029, .→000d, R→0021, U→0022, T→0025, J→0026, S→0027
# Кириллица: Ш→002d, М→002e, у→001d, к→0014, р→0004, л→0020, о→0003
# C→0035

# ── Replacement 1: "10\u00a0RUR\u00a0" (Tj) ──────────────────────────
OLD_RUR = b'<000b000e000a002100220021000a>'
NEW_RUR = b'<000f000a000e000f0011000a002100220021000a>'

# ── Replacement 2: "1,13\u00a0TJS\u00a0" (TJ array) ──────────────────
OLD_TJS = (
    b'[ <000b> 1 <0029> 1 <000b> 1 <000f>\n'
    b'1 <000a> 1 <0025> 1 <0026> 1 <0027000a> ] TJ'
)
NEW_TJS = (
    b'[ <000f> 1 <0024> 1 <000f> 1 <0029> 1 <000e> 1 <0011> 1 <000a> 1 <0025> 1 <0026> 1 <0027000a> ] TJ'
)

# ── Replacement 3: телефон +992000332753 → +992000332793 ──────────────
# Меняется только позиция 12: 5(0033) → 9(0031)
OLD_PHONE = b'<0030003100310010000e000e000e000f000f001000320033000f>'
NEW_PHONE = b'<0030003100310010000e000e000e000f000f001000320031000f>'

# ── Replacement 4: имя "Шукрулло М." → "Шукрууо Ш." ─────────────────
# TJ: [ <002d001d00140004>\n1 <001d002000200003> 1 <000a> 1 <002e> 1 <000d> ] TJ
# Шукр=002d001d00140004, улло→ууо: 001d002000200003→001d001d0003, М→Ш: 002e→002d
OLD_NAME = (
    b'[ <002d001d00140004>\n'
    b'1 <001d002000200003> 1 <000a> 1 <002e> 1 <000d> ] TJ'
)
NEW_NAME = (
    b'[ <002d001d00140004>\n'
    b'1 <001d001d0003> 1 <000a> 1 <002d> 1 <000d> ] TJ'
)

# ── Replacement 5: номер операции C821803260001144 → C821803260001193 ─
# Меняются последние 4 символа: 1144→1193
# 1=000b, 4=0024, 9=0031, 3=000f
OLD_OPID = b'<0035000c0010000b000c000e000f00100011000e000e000e000b000b00240024>'
NEW_OPID = b'<0035000c0010000b000c000e000f00100011000e000e000e000b000b0031000f>'


def _update_xref_and_startxref(data: bytearray, stream_start: int, delta: int) -> None:
    xref_m = re.search(
        rb'xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)',
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb'(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)', entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1): em.start(1) + 10] = f'{offset + delta:010d}'.encode()
        data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

    startxref_m = re.search(rb'startxref\r?\n(\d+)\r?\n', data)
    if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        data[pos: pos + len(str(old_pos))] = str(old_pos + delta).encode()


def patch() -> bool:
    raw_file = INP.read_bytes()
    data = bytearray(raw_file)

    total_replaced = 0

    for m in re.finditer(
        rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL
    ):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start: stream_start + stream_len]))
        except zlib.error:
            continue
        if b'BT' not in dec:
            continue

        new_dec = dec
        replacements_map = [
            (OLD_RUR,   NEW_RUR,   '10 RUR → 3 036 RUR'),
            (OLD_TJS,   NEW_TJS,   '1,13 TJS → 343,06 TJS'),
            (OLD_PHONE, NEW_PHONE, '+992000332753 → +992000332793'),
            (OLD_NAME,  NEW_NAME,  'Шукрулло М. → Шукрууо Ш.'),
            (OLD_OPID,  NEW_OPID,  'C821803260001144 → C821803260001193'),
        ]
        for old_b, new_b, label in replacements_map:
            if old_b in new_dec:
                new_dec = new_dec.replace(old_b, new_b)
                print(f'  [OK] {label}')
                total_replaced += 1

        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        delta = len(new_raw) - stream_len + (len(new_len_str) - len(old_len_str))

        # Replace stream content
        data = bytearray(
            data[:stream_start] + new_raw + data[stream_start + stream_len:]
        )
        # Update /Length
        data[len_num_start: len_num_start + len(old_len_str)] = new_len_str

        _update_xref_and_startxref(data, stream_start, delta)

    if total_replaced == 0:
        print('[ERROR] Паттерны не найдены')
        return False

    OUT.write_bytes(data)
    print(f'[OK] Сохранено: {OUT}')
    return True


if __name__ == '__main__':
    patch()
