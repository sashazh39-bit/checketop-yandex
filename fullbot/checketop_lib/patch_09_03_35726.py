#!/usr/bin/env python3
"""Патч 09-03-26_00-00 2.pdf:
- Сумма: 10 ₽ -> 35 726 ₽
- Плательщик: Александр Евгеньевич Ж. -> Евгений Александрович Е.
- /ID обновляется, структура и метаданные — безупречны

Использование: python3 patch_09_03_35726.py input.pdf output.pdf
"""
import re
import sys
import hashlib
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
        parts.append(s + kern_b + b" ")
    return b"".join(parts)


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
OLD_TM_VARIANTS = [
    b"1 0 0 1 149.8125 227.25 Tm",
    b"1 0 0 1 133.19 227.25 Tm",
    b"1 0 0 1 148.40 227.25 Tm",
    b"1 0 0 1 147.21 227.25 Tm",
]
NEW_TM_PAYER = b"1 0 0 1 147.21 227.25 Tm"

# 10 ₽ -> 35 726 ₽ (3=0016, 5=0018, sp, 7=001A, 2=0015, 6=0019, sp, ₽)
OLD_AMOUNT = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'
NEW_AMOUNT = (
    b'(\x00\x16)-11.11111 (\x00\x18)-11.11111 (\x00\x03)-11.11111 '
    b'(\x00\x1a)-11.11111 (\x00\x15)-11.11111 (\x00\x19)-11.11111 (\x00\x03)-11.11111 (\x04@)'
)
OLD_TM_AMOUNT = b"1 0 0 1 231.52501 72.37499 Tm"
NEW_TM_AMOUNT = b"1 0 0 1 203.91501 72.37499 Tm"  # W=52.71 для 35 726


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
        print("Использование: python3 patch_09_03_35726.py input.pdf output.pdf")
        sys.exit(1)
    inp = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
    content_changed = False

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

        if OLD_TJ in new_dec:
            new_dec = new_dec.replace(OLD_TJ, NEW_TJ)
            changed = True
        if NEW_TJ in new_dec:
            for old_tm in OLD_TM_VARIANTS:
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, NEW_TM_PAYER)
                    changed = True
                    break

        if OLD_AMOUNT in new_dec:
            new_dec = new_dec.replace(OLD_AMOUNT, NEW_AMOUNT)
            changed = True
        if NEW_AMOUNT in new_dec and OLD_TM_AMOUNT in new_dec:
            new_dec = new_dec.replace(OLD_TM_AMOUNT, NEW_TM_AMOUNT)
            changed = True

        # Если уже 35 726 — только Tm и /ID (проверка Tm)
        if NEW_AMOUNT in new_dec and b"1 0 0 1 202." in new_dec and b"72.37499 Tm" in new_dec:
            # Обновить Tm на точное значение
            old_tm_var = re.search(rb"1 0 0 1 [\d.]+ 72\.37499 Tm", new_dec)
            if old_tm_var and NEW_TM_AMOUNT not in new_dec:
                new_dec = new_dec.replace(old_tm_var.group(0), NEW_TM_AMOUNT)
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

        # Всегда обновляем /ID для идеальной структуры
        if update_id(data):
            print("[OK] /ID обновлён")

        if content_changed:
            print("[OK] Евгений Александрович Е., 35 726 ₽")
        break
    else:
        # Stream не найден — только /ID
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
