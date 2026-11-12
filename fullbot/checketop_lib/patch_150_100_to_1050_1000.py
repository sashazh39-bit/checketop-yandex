#!/usr/bin/env python3
"""CID-патч для ВТБ PDF.
Замена: 150 ₽ → 1 050 ₽, 100 ₽ → 1 000 ₽.
tm_x вычисляется по wall и ширине TJ (как в vtb_patch_from_config).

Использование:
  python3 patch_150_100_to_1050_1000.py input.pdf [output.pdf]
"""
import re
import sys
import zlib
from pathlib import Path

try:
    from vtb_patch_from_config import _parse_cid_widths, _tj_advance_units
except ImportError:
    _parse_cid_widths = _tj_advance_units = None


def _change_one_char_in_id(data: bytearray) -> None:
    """Изменить ровно 1 символ в /ID."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    hex1 = bytearray(id_m.group(1))
    pos = 0
    c = hex1[pos]  # int (byte value)
    if c in b"0123456789":
        new_c = (c - 48 + 1) % 10 + 48
    elif c in b"ABCDEFabcdef":
        base = 65 if c < 97 else 97
        new_c = (c - base + 1) % 6 + base  # A-F или a-f: цикл по 6 символам
    else:
        return
    hex1[pos] = new_c
    new_enc = bytes(hex1)
    data[id_m.start(1):id_m.end(1)] = new_enc
    data[id_m.start(2):id_m.end(2)] = new_enc
    print("[OK] /ID: 1 символ изменён")


def _get_wall(data: bytes) -> float:
    """Правая граница из layout или MediaBox."""
    try:
        from vtb_sbp_layout import wall_from_fixed_margin, get_layout_values
        w = wall_from_fixed_margin(data)
        if w:
            return w
        return get_layout_values().get("wall", 257.08)
    except Exception:
        return 257.08


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/16-03-26_14-19.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_1050_1000.pdf")

    data = bytearray(inp.read_bytes())

    # CMap: 0=0013, 1=0014, 5=0018, space=0003, ₽=0440 (\x04\x40)
    # 100 ₽ -> 1 000 ₽ (kern -11.11111)
    OLD_100 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
    NEW_1000 = b'(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

    # 150 ₽ -> 1 050 ₽ (kern -11.11111)
    OLD_150 = b'(\x00\x14)-11.11111 (\x00\x18)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
    NEW_1050 = b'(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x18)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

    # Вариант 150 с -16.66667 (если «Сумма с комиссией» в другой зоне)
    OLD_150_16 = b'(\x00\x14)-16.66667 (\x00\x18)-16.66667 (\x00\x13)-16.66667 (\x00\x03)-16.66667 (\x04@)'
    NEW_1050_16 = b'(\x00\x14)-16.66667 (\x00\x03)-16.66667 (\x00\x13)-16.66667 (\x00\x18)-16.66667 (\x00\x13)-16.66667 (\x00\x03)-16.66667 (\x04@)'

    # Время операции 15:19 → 15:33 (kern -16.66667)
    OLD_TIME = b'(\x00\x14)-16.66667 (\x00\x18)-16.66667 (\x00\x1d)-16.66667 (\x00\x14)-16.66667 (\x00\x1c)'
    NEW_TIME = b'(\x00\x14)-16.66667 (\x00\x18)-16.66667 (\x00\x1d)-16.66667 (\x00\x16)-16.66667 (\x00\x16)'

    wall = _get_wall(bytes(data))
    cid_widths = _parse_cid_widths(bytes(data)) if _parse_cid_widths else {}

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

        if OLD_100 in new_dec:
            new_dec = new_dec.replace(OLD_100, NEW_1000)
            print("[OK] 100 ₽ -> 1 000 ₽")
        if OLD_150 in new_dec:
            new_dec = new_dec.replace(OLD_150, NEW_1050)
            print("[OK] 150 ₽ -> 1 050 ₽ (kern -11.11111)")
        if OLD_150_16 in new_dec:
            new_dec = new_dec.replace(OLD_150_16, NEW_1050_16)
            print("[OK] 150 ₽ -> 1 050 ₽ (kern -16.66667)")

        if OLD_TIME in new_dec:
            new_dec = new_dec.replace(OLD_TIME, NEW_TIME)
            print("[OK] Время операции 15:19 -> 15:33")

        # Tm: выравнивание по правой границе (wall) по ширине TJ
        if _tj_advance_units and cid_widths:
            # 100 → 1 000: font 13.5pt, scale=13.5/1000
            if NEW_1000 in new_dec:
                new_units = _tj_advance_units(NEW_1000, cid_widths)
                if new_units > 0:
                    new_x = wall - new_units * (13.5 / 1000.0)
                    pat = rb"(1 0 0 1 )([\d.]+)( 72\.37\d* Tm)"
                    def repl_100(m):
                        return m.group(1) + f"{new_x:.5f}".encode() + m.group(3)
                    new_dec = re.sub(pat, repl_100, new_dec, count=1)
                    print("[OK] Tm 100 → wall")
            # 150 → 1 050: масштаб из старой позиции (150 ₽ уже касалась wall)
            if NEW_1050_16 in new_dec:
                old_units = _tj_advance_units(OLD_150_16, cid_widths)
                new_units = _tj_advance_units(NEW_1050_16, cid_widths)
                if old_units > 0 and new_units > 0:
                    # old_tm_x из PDF (234.45), wall - old_tm_x = old_units * scale
                    old_tm_x = 234.45
                    pts_per_unit = (wall - old_tm_x) / old_units
                    new_x = wall - new_units * pts_per_unit
                    pat = rb"(1 0 0 1 )[\d.]+( 120\.74\d* Tm)"
                    def _repl150(mo):
                        return mo.group(1) + f"{new_x:.5f}".encode() + mo.group(2)
                    new_dec = re.sub(pat, _repl150, new_dec, count=1)
                    print("[OK] Tm 150 → wall")

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

    _change_one_char_in_id(data)

    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
