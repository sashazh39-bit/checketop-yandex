#!/usr/bin/env python3
"""Исправление отображения «Евгений Александрович Е.» в 08-03-26_00-00 2.pdf.
Копирует шрифт, ToUnicode и CIDToGIDMap из рабочего 08-03-26_00-00.pdf.
Без CIDToGIDMap PDF ломается (ошибка 109).

Использование: python3 fix_08_03_2_font.py
"""
import re
import zlib
from pathlib import Path


def find_streams(data: bytes) -> dict:
    """Найти font, ToUnicode, CIDToGIDMap. {type: (start, len, len_pos)}."""
    result = {}
    ctg_ref = re.search(rb'/CIDToGIDMap\s+(\d+)\s+0\s+R', data)
    ctg_oid = int(ctg_ref.group(1)) if ctg_ref else None

    for m in re.finditer(rb'(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        oid = int(m.group(1))
        dict_part = m.group(2) + m.group(4)
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_pos = m.start(3)
        if stream_start + stream_len > len(data):
            continue
        stream_data = data[stream_start : stream_start + stream_len]
        try:
            dec = zlib.decompress(stream_data)
        except zlib.error:
            dec = b""

        if ctg_oid is not None and oid == ctg_oid:
            result["cidtogid"] = (stream_start, stream_len, len_pos)
        elif b"beginbfchar" in dec or b"beginbfrange" in dec:
            result["tounicode"] = (stream_start, stream_len, len_pos)
        elif b"/Length1" in dict_part and stream_len > 1000:
            result["font"] = (stream_start, stream_len, len_pos)

    return result


def main():
    src = Path("чеки 08.03/08-03-26_00-00.pdf")
    tgt = Path("чеки 08.03/08-03-26_00-00 2.pdf")
    if not src.exists() or not tgt.exists():
        print("[ERROR] Файлы не найдены")
        return 1

    src_data = src.read_bytes()
    tgt_data = bytearray(tgt.read_bytes())
    src_s = find_streams(src_data)
    tgt_s = find_streams(tgt_data)

    # Порядок: от конца к началу, чтобы позиции не сбивались
    order = ["cidtogid", "tounicode", "font"]
    items = [(k, (tgt_s[k][0], tgt_s[k][1], tgt_s[k][2])) for k in order if k in src_s and k in tgt_s]
    items.sort(key=lambda x: x[1][0], reverse=True)

    total_delta = 0
    min_pos = min(v[0] for v in tgt_s.values()) if tgt_s else 0

    for key, (tgt_start, tgt_len, tgt_len_pos) in items:
        # Замены от конца к началу — позиции младших не сдвигаются
        tgt_start_adj = tgt_start
        tgt_len_pos_adj = tgt_len_pos
        tgt_len_pos_adj = tgt_len_pos

        src_start, src_len = src_s[key][0], src_s[key][1]
        new_data = src_data[src_start : src_start + src_len]
        delta = len(new_data) - tgt_len
        total_delta += delta

        tgt_data = tgt_data[:tgt_start_adj] + new_data + tgt_data[tgt_start_adj + tgt_len :]

        old_str = str(tgt_len).encode()
        new_str = str(len(new_data)).encode()
        pad = new_str.ljust(len(old_str))[: len(old_str)]
        tgt_data[tgt_len_pos_adj : tgt_len_pos_adj + len(old_str)] = pad

    if total_delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", tgt_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > min_pos:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + total_delta:010d}".encode()
            tgt_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", tgt_data)
        if startxref_m:
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            tgt_data[pos : pos + len(str(old_pos))] = str(old_pos + total_delta).encode()

    tgt.write_bytes(tgt_data)
    print("[OK] Шрифт, ToUnicode и CIDToGIDMap скопированы")

    # Починка xref через qpdf (на всякий случай)
    import subprocess
    r = subprocess.run(["qpdf", "--replace-input", str(tgt)], capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        print(f"[OK] Сохранено и исправлено: {tgt}")
    else:
        print(f"[OK] Сохранено: {tgt}")
    return 0


if __name__ == "__main__":
    exit(main())
