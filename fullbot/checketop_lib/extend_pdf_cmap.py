#!/usr/bin/env python3
"""
Расширить ToUnicode CMap PDF — добавить недостающие русские буквы в CMap.
Требует: pip install fonttools

ВНИМАНИЕ: Скрипт обновляет только ToUnicode. Шрифт в PDF — subset, в нём
нет контуров для новых глифов. Без замены потока шрифта новые символы
отобразятся как пустые/заглушки. Полное расширение (добавление глифов в шрифт)
требует замены font stream — это сложнее, см. rebuild_pdf.py для полной пересборки.

Использование:
  python3 extend_pdf_cmap.py input.pdf output.pdf
  python3 extend_pdf_cmap.py input.pdf output.pdf --chars "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

# Полный набор русских букв для добавления
RUSSIAN_FULL = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюяё"

TAHOMA_PATHS = [
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    "/Library/Fonts/Tahoma.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]


def _find_tahoma() -> Path | None:
    for p in TAHOMA_PATHS:
        if Path(p).exists():
            return Path(p)
    return None


def extend_cmap(pdf_path: Path, out_path: Path, extra_chars: str = RUSSIAN_FULL) -> bool:
    """Добавить глифы для extra_chars в PDF. Возвращает True при успехе."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools.subset import Subsetter
    except ImportError:
        print("[ERROR] Требуется fonttools. Установите: pip install fonttools", file=sys.stderr)
        return False

    tahoma = _find_tahoma()
    if not tahoma:
        print("[ERROR] Tahoma не найден.", file=sys.stderr)
        return False

    data = bytearray(pdf_path.read_bytes())

    # 1. Парсим текущий ToUnicode
    uni_to_cid: dict[int, str] = {}
    tounicode_stream_start = None
    tounicode_stream_len = None
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end() : m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"beginbfchar" not in dec:
            continue
        tounicode_stream_start = m.end()
        tounicode_stream_len = int(m.group(2))
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = mm.group(1).decode().upper().zfill(4)
            uni = int(mm.group(2).decode().upper(), 16)
            uni_to_cid[uni] = cid
        break

    if not uni_to_cid:
        print("[ERROR] ToUnicode не найден.", file=sys.stderr)
        return False

    # 2. Какие символы добавить
    existing = set(uni_to_cid.keys())
    to_add = [ord(c) for c in extra_chars if ord(c) not in existing]
    if not to_add:
        print("[INFO] Все символы уже есть в CMap.")
        out_path.write_bytes(data)
        return True

    # 3. Максимальный CID + 1
    max_cid = max(int(cid, 16) for cid in uni_to_cid.values())
    next_cid = max_cid + 1

    # 4. Добавляем в ToUnicode (beginbfchar)
    new_entries = []
    for cp in sorted(set(to_add)):
        cid_hex = f"{next_cid:04X}"
        uni_hex = f"{cp:04X}"
        new_entries.append(f"<{cid_hex}> <{uni_hex}>")
        uni_to_cid[cp] = cid_hex
        next_cid += 1

    # 5. Читаем ToUnicode stream и добавляем записи
    dec = zlib.decompress(bytes(data[tounicode_stream_start : tounicode_stream_start + tounicode_stream_len]))
    # Ищем endbfchar и вставляем перед ним
    insert_text = "\n".join(new_entries) + "\n"
    if b"endbfchar" in dec:
        # Меняем число в beginbfchar (65 -> 65+N) если нужно
        dec_str = dec.decode("latin-1", errors="replace")
        bfchar_m = re.search(r"(\d+)\s+beginbfchar", dec_str)
        if bfchar_m:
            old_n = int(bfchar_m.group(1))
            new_n = old_n + len(new_entries)
            dec_str = dec_str[: bfchar_m.start(1)] + str(new_n) + dec_str[bfchar_m.end(1) :]
        else:
            dec_str = dec.decode("latin-1", errors="replace")
        # Вставляем перед endbfchar
        end_pos = dec_str.find("endbfchar")
        dec_str = dec_str[:end_pos] + insert_text + dec_str[end_pos:]
        new_dec = dec_str.encode("latin-1", errors="replace")
    else:
        new_dec = dec + b"\n" + insert_text.encode("latin-1")

    new_raw = zlib.compress(new_dec, 9)
    delta_stream = len(new_raw) - tounicode_stream_len

    # 6–7. Собираем новый буфер через конкатенацию (без resize bytearray)
    before = data[:tounicode_stream_start]
    after = data[tounicode_stream_start + tounicode_stream_len :]
    delta_length = 0
    len_pattern = rb"/Length\s+" + str(tounicode_stream_len).encode()
    for m in re.finditer(len_pattern, before):
        if m.start() > tounicode_stream_start - 500:
            old_str = m.group(0)
            new_str = b"/Length " + str(len(new_raw)).encode()
            before = before[: m.start()] + new_str + before[m.end() :]
            delta_length = len(new_str) - len(old_str)
            break
    delta_total = delta_length + delta_stream
    result = before + new_raw + after

    # 8. Обновляем xref
    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", result)
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > tounicode_stream_start:
                entries[em.start(1) : em.start(1) + 10] = f"{offset + delta_total:010d}".encode()
        result = result[: xref_m.start(3)] + bytes(entries) + result[xref_m.end(3) :]

    # 9. Обновляем startxref
    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", result)
    if startxref_m and delta_total != 0 and tounicode_stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        end_pos = startxref_m.end(1)
        old_pos = int(startxref_m.group(1))
        result = result[:pos] + str(old_pos + delta_total).encode() + result[end_pos:]

    out_path.write_bytes(result)
    added = "".join(chr(c) for c in sorted(set(to_add)))
    print(f"[OK] Добавлено {len(new_entries)} символов в ToUnicode CMap.")
    print("[WARN] Шрифт не заменён — новые символы могут отображаться пусто. Для полной поддержки нужна замена font stream.")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Расширить CMap PDF русскими буквами.")
    parser.add_argument("input", help="Входной PDF")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--chars", default=RUSSIAN_FULL, help="Символы для добавления (по умолчанию все русские)")
    args = parser.parse_args()

    in_p = Path(args.input).expanduser().resolve()
    out_p = Path(args.output).expanduser().resolve()
    if not in_p.exists():
        print(f"[ERROR] Файл не найден: {in_p}", file=sys.stderr)
        return 1
    return 0 if extend_cmap(in_p, out_p, args.chars) else 1


if __name__ == "__main__":
    sys.exit(main())
