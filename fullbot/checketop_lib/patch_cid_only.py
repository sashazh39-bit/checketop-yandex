#!/usr/bin/env python3
"""Патч ТОЛЬКО CID суммы. Без изменения метаданных, /ID, xref, startxref.

Секрет: добавляем padding (пробел) в конец decompressed stream, чтобы
сжатая длина совпадала с оригиналом. Тогда заменяем только байты потока —
/Length, xref, startxref не трогаем.

Использование:
  python3 patch_cid_only.py input.pdf output.pdf [10->100|10->1000|10->10000]
По умолчанию: 10->100
"""
import re
import sys
import zlib
from pathlib import Path

# Варианты замен (старый блок -> новый блок)
# 10 ₽ (1,0,space,₽) — 4 глифа
OLD_10 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

# 100₽ (1,0,0,₽) — 4 глифа, ТА ЖЕ ДЛИНА 46 байт → можно сохранить stream length
NEW_100 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x04@)'

# 1000 ₽ (1,0,0,0,space,₽) — 6 глифов
NEW_1000 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

# 10000 ₽ (1,0,0,0,0,space,₽) — 7 глифов
NEW_10000 = b'(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)'

# Tm для выравнивания по правому краю (ширины из /Widths: 1=443, 0=606, sp=205, ₽=605)
def pt(w): return 13.5 * w / 1000
w1, w0, wsp, wr = pt(443), pt(606), pt(205), pt(605)
orig_X = 231.52501

def tm_for_glyphs(n):
    # W4=25.1, W5=33.28, W6=41.46, W7=49.64, W8=52.41
    W = {4: 25.1, 5: 33.28, 6: 41.46, 7: 49.64, 8: 52.41}
    return orig_X + 25.1 - W.get(n, 25.1)

# (old_blk, new_blk, n_glyphs для Tm, preserve_length?)
PRESETS = {
    "10->100": (OLD_10, NEW_100, 4, True),   # 100₽ — 4 глифа, длина 46=46
    "10->1000": (OLD_10, NEW_1000, 6, False),
    "10->10000": (OLD_10, NEW_10000, 7, False),
}


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 patch_cid_only.py input.pdf output.pdf [10->100|10->1000|10->10000]")
        sys.exit(1)
    inp = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    preset = sys.argv[3] if len(sys.argv) > 3 else "10->100"
    if preset not in PRESETS:
        preset = "10->100"
    old_blk, new_blk, n_glyphs, try_preserve_len = PRESETS[preset]
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())

    for m in re.finditer(rb"<<[^>]*/Length\s+(\d+)[^>]*>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(1))
        stream_start = m.end()
        len_num_start = m.start(1)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if old_blk not in dec:
            continue

        new_dec = dec.replace(old_blk, new_blk)
        # Tm для суммы — только если число глифов изменилось (не для 10->100)
        if n_glyphs != 4:
            old_tm = b"1 0 0 1 231.52501 72.37499 Tm"
            new_x = tm_for_glyphs(n_glyphs)
            new_tm = f"1 0 0 1 {new_x:.5f} 72.37499 Tm".encode()
            if old_tm in new_dec:
                new_dec = new_dec.replace(old_tm, new_tm)

        # Padding: при try_preserve_len добиваем до той же сжатой длины
        target_len = stream_len
        new_raw = None
        if try_preserve_len:
            for pad in range(51):
                raw = zlib.compress(new_dec + b" " * pad, 6)
                if len(raw) == target_len:
                    new_raw = raw
                    break
        if new_raw is None:
            new_raw = zlib.compress(new_dec, 6)

        if len(new_raw) == target_len:
            # Идеально: только замена байтов потока — /Length, xref, startxref, /ID без изменений
            data[stream_start : stream_start + stream_len] = new_raw
            print("[OK] Длина потока сохранена — метаданные, /ID, xref без изменений")
        else:
            # Fallback: обновляем /Length, xref, startxref
            delta = len(new_raw) - stream_len
            data = data[:stream_start] + new_raw + data[stream_start + stream_len:]
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            # /Length: заменяем только если укладываемся в те же цифры
            data[len_num_start : len_num_start + len(old_len_str)] = new_len_str[:len(old_len_str)].ljust(len(old_len_str))

            xref_m = re.search(rb"xref\r?\n\d+\s+\d+\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
            if xref_m:
                entries = bytearray(xref_m.group(1))
                for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                    offset = int(em.group(1))
                    if offset > stream_start:
                        entries[em.start(1):em.start(1)+10] = f"{offset + delta:010d}".encode()
                data[xref_m.start(1):xref_m.end(1)] = bytes(entries)
            startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
            if startxref_m and delta != 0:
                pos = startxref_m.start(1)
                op = int(startxref_m.group(1))
                data[pos:pos+len(str(op))] = str(op + delta).encode()
            print("[!] Длина изменилась, обновлены /Length, xref, startxref")

        print(f"[OK] {preset}")
        break
    else:
        print("[ERROR] Сумма 10 ₽ не найдена")
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
