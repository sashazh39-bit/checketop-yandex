#!/usr/bin/env python3
"""Патч ВТБ: замена ФИО плательщика на Бабаян Арман М.
Расширяет ToUnicode для б и М (если нет), затем заменяет в content stream.
"""
import re
import sys
import zlib
from pathlib import Path


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("чеки 07.03/07-03-26_00-00 2.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp

    data = bytearray(inp.read_bytes())

    # 1. Найти и расширить ToUnicode (объект 12 0 R)
    data = _extend_tounicode(data, {0x0431: "0431", 0x041C: "041C"})  # б, М

    # 2. Замена: Алексей Евгеньевич П. -> Бабаян Арман М.
    # CIDs: Б=021D, а=023C, б=0431, я=025B, н=0249, А=021C, р=024C, м=0248, М=041C
    OLD_NAME = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02A)-16.66667 (\x02E)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x2b)-16.66667 (\x00\x11)'
    )
    # Бабаян Арман М: Б  а  б  а  я  н  space  А  р  м  а  н  space  М
    NEW_NAME = (
        b'(\x02\x1d)-16.66667 (\x02\x3c)-16.66667 (\x04\x31)-16.66667 (\x02\x3c)-16.66667 (\x02\x5b)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\x1c)-16.66667 (\x02\x4c)-16.66667 (\x02\x48)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 (\x04\x1c)-16.66667'
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
        if OLD_NAME in new_dec:
            new_dec = new_dec.replace(OLD_NAME, NEW_NAME)
            print("[OK] Алексей Евгеньевич П. -> Бабаян Арман М.")
        else:
            continue

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


def _extend_tounicode(data: bytearray, additions: dict[int, str]) -> bytearray:
    """Добавить uni->cid в CMap (Identity). additions: {uni_cp: "CIDhex"}"""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"beginbfrange" not in dec:
            continue

        # Проверить существующие маппинги (dest Unicode)
        existing_uni = set()
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            src_s, src_e, dest = int(mm.group(1), 16), int(mm.group(2), 16), int(mm.group(3), 16)
            for i in range(src_e - src_s + 1):
                existing_uni.add(dest + i)

        to_add = [(cp, cid) for cp, cid in additions.items() if cp not in existing_uni]
        if not to_add:
            return data

        # beginbfrange: <src> <src> <dest> для одиночных
        new_entries = b"\r\n".join(
            f"<{cid}><{cid}><{cp:04X}>".encode() for cp, cid in sorted(to_add)
        )
        insert_pos = dec.find(b"endbfrange")
        if insert_pos < 0:
            return data
        new_block = new_entries + b"\r\n"
        new_dec = dec[:insert_pos] + new_block + dec[insert_pos:]

        count_m = re.search(rb"(\d+)\s+beginbfrange", new_dec)
        if count_m:
            old_n = int(count_m.group(1))
            new_n = old_n + len(to_add)
            new_dec = new_dec[: count_m.start(1)] + str(new_n).encode() + new_dec[count_m.end(1) :]

        new_raw = zlib.compress(new_dec, 9)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        len_num_start = m.start(2)
        num_end = len_num_start + len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
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

        print("[OK] CMap: добавлены б, М")
        break
    return data


if __name__ == "__main__":
    main()
