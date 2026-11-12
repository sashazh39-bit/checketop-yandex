#!/usr/bin/env python3
"""Патч СБП: замена имени плательщика на Бабаян Арман М.
Использует CIDs из CMap целевого PDF (без расширения CMap).
Сохраняет метаданные, обновляет только content stream.
Использование: python3 patch_payer_sbp.py input.pdf output.pdf
"""
import re
import sys
import zlib
from pathlib import Path


def build_tj(cids: list[str], kern: str = "-16.66667") -> bytes:
    """Собрать TJ-блок из списка CIDs в формате (bytes)-kern.
    Экранирует 0x28 '(' и 0x29 ')' для корректного PDF."""
    parts = []
    kern_b = kern.encode()
    for cid_hex in cids:
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        # PDF: \( = 0x28, \) = 0x29, \\ = 0x5c
        if l == 0x28:
            s = b"(\\x%02x\\()" % h
        elif l == 0x29:
            s = b"(\\x%02x\\))" % h
        elif h == 0 and l < 0x80 and 0x20 <= l <= 0x7E and l not in (0x28, 0x29, 0x5C):
            s = bytes([0x28, l, 0x29])
        else:
            s = b"(\\x%02x\\x%02x)" % (h, l)
        parts.append(s + b"-" + kern_b + b" ")
    return b"".join(parts)


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_payer_sbp.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
    orig_size = len(data)

    # CIDs из CMap целевого PDF (СБП 07-03-26): Б=021D, а=023C, б=023D, я=025B, н=0249,
    # А=021C, р=024C, м=0248, М=0228, space=0003
    NEW_CIDS = ["021D", "023C", "023D", "023C", "025B", "0249", "0003",
                "021C", "024C", "0248", "023C", "0249", "0003", "0228"]
    NEW_NAME = build_tj(NEW_CIDS)

    # OLD: Александр Владимирович Г. — два варианта (разный kern)
    OLD_16 = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02\x1e)-16.66667 (\x02G)-16.66667 (\x02<)-16.66667 (\x02@)-16.66667 (\x02D)-16.66667 (\x02H)-16.66667 (\x02D)-16.66667 (\x02L)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1f)-16.66667 (\x00\x11)'
    )
    OLD_21 = (
        b'(\x02\x1c)-21.42857 (\x02G)-21.42857 (\x02A)-21.42857 (\x02F)-21.42857 (\x02M)-21.42857 (\x02<)-21.42857 (\x02I)-21.42857 (\x02@)-21.42857 (\x02L)-21.42857 (\x00\x03)-21.42857 '
        b'(\x02\x1e)-21.42857 (\x02G)-21.42857 (\x02<)-21.42857 (\x02@)-21.42857 (\x02D)-21.42857 (\x02H)-21.42857 (\x02D)-21.42857 (\x02L)-21.42857 (\x02J)-21.42857 (\x02>)-21.42857 (\x02D)-21.42857 (\x02S)-21.42857 (\x00\x03)-21.42857 (\x02\x1f)-21.42857 (\x00\x11)'
    )
    # OLD: Александр Евгеньевич Ж. (13-02-26 и др.)
    OLD_JE_16 = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
    NEW_NAME_21 = build_tj(NEW_CIDS, "-21.42857")

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
        if OLD_16 in new_dec:
            new_dec = new_dec.replace(OLD_16, NEW_NAME)
        if OLD_21 in new_dec:
            new_dec = new_dec.replace(OLD_21, NEW_NAME_21)
        if OLD_JE_16 in new_dec:
            new_dec = new_dec.replace(OLD_JE_16, NEW_NAME)
        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Блок плательщика не найден")
        sys.exit(1)

    # Применяем с конца, чтобы не сбивать позиции
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

    print("[OK] Александр Владимирович Г. -> Бабаян Арман М")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out} ({len(data)} bytes, было {orig_size})")


if __name__ == "__main__":
    main()
