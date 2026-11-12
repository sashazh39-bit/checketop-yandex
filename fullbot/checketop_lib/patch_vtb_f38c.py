#!/usr/bin/env python3
"""CID-патч для ВТБ PDF (формат (bytes)-kern в content stream).
Замена: 100→1 000, 130→1 030, 449.65→4 496,50, ФИО→Николай Евгеньевич П.
С сохранением правой границы (Tm сдвиг влево).
Использование: python3 patch_vtb_f38c.py [input.pdf] [output.pdf]
"""
import re
import sys
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("f38c7775-7677-4678-9b4b-70e64be3815a.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_cid.pdf")
    data = bytearray(inp.read_bytes())

    # CMap: 0=13, 1=14, 2=15, 3=16, 4=17, 5=18, 6=19, 7=1a, 8=1b, 9=1c, space=03, .=11, ,=0f
    # 100 ₽ -> 1 000 ₽
    OLD_100 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
    NEW_1000 = b'(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

    # 130 ₽ -> 1 030 ₽
    OLD_130 = b'(\x00\x14)-16.66667 (\x00\x16)-16.66667 (\x00\x13)-16.66667 (\x00\x03)-16.66667 (\x04@)'
    NEW_1030 = b'(\x00\x14)-16.66667 (\x00\x03)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 (\x00\x13)-16.66667 (\x00\x03)-16.66667 (\x04@)'

    # 449.65 AMD -> 4 496,50 AMD (comma=0f, period=11, A=24,M=30,D=27)
    OLD_44965 = (
        b'(\x00\x17)-16.66667 (\x00\x17)-16.66667 (\x00\x1c)-16.66667 (\x00\x11)-16.66667 '
        b'(\x00\x19)-16.66667 (\x00\x18)-16.66667 (\x00\x03)-16.66667 (\x00\x24)-16.66667 '
        b'(\x00\x30)-16.66667 (\x00\x27)'
    )
    NEW_449650 = (
        b'(\x00\x17)-16.66667 (\x00\x03)-16.66667 (\x00\x17)-16.66667 (\x00\x1c)-16.66667 '
        b'(\x00\x19)-16.66667 (\x00\x0f)-16.66667 (\x00\x18)-16.66667 (\x00\x13)-16.66667 '
        b'(\x00\x03)-16.66667 (\x00\x24)-16.66667 (\x00\x30)-16.66667 (\x00\x27)'
    )

    # Александр Евгеньевич Ж. -> Николай Евгеньевич П.
    # CMap: Н=0229,и=0244,к=0246,о=024a,л=0247,а=023c,й=0245, space=0003,
    #       Е=0221,в=023e,г=023f,е=0241,н=0249,ь=0258,и=0244,ч=0253, П=022b, .=0011
    OLD_NAME = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 '
        b'(\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 '
        b'(\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 '
        b'(\x00\x03)-16.66667 (\x02\x22)-16.66667 (\x00\x11)'
    )
    # CID 0229: \x29 is ")" which must be escaped as \) in PDF literal strings
    NEW_NAME = (
        b'(\x02\\))-16.66667 (\x02\x44)-16.66667 (\x02\x46)-16.66667 (\x02\x4a)-16.66667 (\x02\x47)-16.66667 '
        b'(\x02\x3c)-16.66667 (\x02\x45)-16.66667 (\x00\x03)-16.66667 (\x02\x21)-16.66667 (\x02\x3e)-16.66667 '
        b'(\x02\x3f)-16.66667 (\x02\x41)-16.66667 (\x02\x49)-16.66667 (\x02\x58)-16.66667 (\x02\x41)-16.66667 '
        b'(\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x2b)-16.66667 (\x00\x11)'
    )

    # Tm: правая граница 24 pt от края (page 281.2 - 24 - text_width)
    # 1 000 ₽: width=45.1 → Tm_x=212.0
    OLD_TM_100 = b"1 0 0 1 223.20001 72.37498 Tm"
    NEW_TM_100 = b"1 0 0 1 212.0 72.37498 Tm"
    # 1 030 ₽: width=30.3 → Tm_x=226.9
    OLD_TM_130 = b"1 0 0 1 234.375 120.74999 Tm"
    NEW_TM_130 = b"1 0 0 1 226.9 120.74999 Tm"
    # 4 496,50 AMD: width=59.3 → Tm_x=197.8
    OLD_TM_44965 = b"1 0 0 1 205.38751 168.74998 Tm"
    NEW_TM_44965 = b"1 0 0 1 197.8 168.74998 Tm"
    # Николай Евгеньевич П.: width=95.5 → Tm_x=161.6
    OLD_TM_NAME = b"1 0 0 1 149.8125"
    NEW_TM_NAME = b"1 0 0 1 161.6"

    # Время: 05:34 -> MSK+1min. CMap: 0=13, 5=18, 6=19, 3=16, 4=17, :=1d
    # 05:34 = (\x00\x13)(\x00\x18)(\x00\x1d)(\x00\x16)(\x00\x17)
    OLD_TIME = b'(\x00\x13)-16.66667 (\x00\x18)-16.66667 (\x00\x1d)-16.66667 (\x00\x16)-16.66667 (\x00\x17)'
    _now = (datetime.now(MOSCOW) + timedelta(minutes=1)).strftime("%H:%M")
    _cmap = {'0': b'\x13', '1': b'\x14', '2': b'\x15', '3': b'\x16', '4': b'\x17', '5': b'\x18', '6': b'\x19', '7': b'\x1a', '8': b'\x1b', '9': b'\x1c'}
    NEW_TIME = (
        b'(\x00' + _cmap[_now[0]] + b')-16.66667 (\x00' + _cmap[_now[1]] + b')-16.66667 '
        b'(\x00\x1d)-16.66667 (\x00' + _cmap[_now[3]] + b')-16.66667 (\x00' + _cmap[_now[4]] + b')'
    )

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
            print("[OK] 100 -> 1 000")
        if OLD_130 in new_dec:
            new_dec = new_dec.replace(OLD_130, NEW_1030)
            print("[OK] 130 -> 1 030")
        if OLD_44965 in new_dec:
            new_dec = new_dec.replace(OLD_44965, NEW_449650)
            print("[OK] 449.65 AMD -> 4 496,50 AMD")

        # Name - need exact bytes, try without OLD_NAME first to verify amounts
        if OLD_NAME in new_dec:
            new_dec = new_dec.replace(OLD_NAME, NEW_NAME)
            print("[OK] ФИО -> Николай Евгеньевич П.")
        if OLD_TIME in new_dec:
            new_dec = new_dec.replace(OLD_TIME, NEW_TIME)
            print(f"[OK] Время -> {_now} (МСК+1 мин)")

        if OLD_TM_100 in new_dec and NEW_1000 in new_dec:
            new_dec = new_dec.replace(OLD_TM_100, NEW_TM_100)
            print("[OK] Tm 100 сдвинут влево")
        if OLD_TM_130 in new_dec and NEW_1030 in new_dec:
            new_dec = new_dec.replace(OLD_TM_130, NEW_TM_130)
            print("[OK] Tm 130 сдвинут влево")
        if OLD_TM_44965 in new_dec and NEW_449650 in new_dec:
            new_dec = new_dec.replace(OLD_TM_44965, NEW_TM_44965)
            print("[OK] Tm 449.65 сдвинут влево")
        if OLD_TM_NAME in new_dec and NEW_NAME in new_dec:
            new_dec = new_dec.replace(OLD_TM_NAME, NEW_TM_NAME)
            print("[OK] Tm ФИО — правая граница")

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

    if "--random-id" in sys.argv:
        try:
            from patch_id import patch_document_id
            if patch_document_id(out):
                print("[OK] Document ID заменён на случайный.")
        except ImportError:
            print("[WARN] patch_id не найден. Document ID не изменён.")


if __name__ == "__main__":
    main()
