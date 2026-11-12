#!/usr/bin/env python3
"""Заменить obj 17 (font stream) в receipt на эталон из 13.pdf.

Цель: подставить font stream как в эталоне — убрать след добавленных глифов.
ВНИМАНИЕ: После замены Ф, Ч, Ю отобразятся как оригинальные глифы в тех слотах
(в 13.pdf их нет — будут *, F, Д или мусор). Нужен для теста: влияет ли размер
font stream на проверку бота.

Использование:
  python3 patch_obj17_from_etalon.py receipt_add.pdf -o receipt_obj17_etalon.pdf
  python3 patch_obj17_from_etalon.py receipt_add.pdf --etalon "/path/to/13.pdf" -o out.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def find_obj17_range(data: bytes) -> tuple[int, int, int, int] | None:
    """Найти obj 17: (obj_start, stream_start, stream_len, obj_end)."""
    m = re.search(rb"17\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL)
    if not m:
        return None
    obj_start = m.start()
    stream_len = int(m.group(2))
    stream_start = m.end()
    stream_end = stream_start + stream_len
    # endstream и endobj
    endstream = data.find(b"endstream", stream_end)
    endobj = data.find(b"endobj", stream_end)
    if endstream < 0 or endobj < 0:
        return None
    obj_end = endobj + len(b"endobj")
    return obj_start, stream_start, stream_len, obj_end


def replace_obj17(target_data: bytearray, etalon_data: bytes) -> bool:
    """Заменить obj 17 в target на obj 17 из etalon. Возвращает True при успехе."""
    target_info = find_obj17_range(bytes(target_data))
    etalon_info = find_obj17_range(etalon_data)
    if not target_info or not etalon_info:
        return False
    t_start, t_str_start, t_str_len, t_end = target_info
    e_start, e_str_start, e_str_len, e_end = etalon_info

    # Извлечь obj 17 из etalon (всё от "17 0 obj" до "endobj")
    etalon_obj17 = etalon_data[e_start:e_end]
    # Из etalon нужен stream + заголовок. Проверим: stream идёт после ">> stream\n"
    e_stream_header_end = etalon_data.find(b">>", e_start) + 2
    e_stream_m = re.search(rb"stream\r?\n", etalon_data[e_stream_header_end : e_stream_header_end + 20])
    if not e_stream_m:
        return False
    e_stream_data_start = e_stream_header_end + e_stream_m.end()
    e_stream_data = etalon_data[e_stream_data_start : e_stream_data_start + e_str_len]

    # Заменить в target: от stream_start до stream_start + t_str_len
    old_stream = bytes(target_data[t_str_start : t_str_start + t_str_len])
    new_stream = e_stream_data
    if len(new_stream) != e_str_len:
        return False  # проверка

    # Длина stream может отличаться — обновить /Length в dict
    len_in_dict = re.search(rb"17\s+0\s+obj\s*<<.*?/Length\s+(\d+)", target_data[t_start : t_start + 500], re.DOTALL)
    if len_in_dict:
        len_pos = t_start + len_in_dict.start(1)
        old_len_str = len_in_dict.group(1).decode()
        new_len_str = str(len(new_stream))
        if len(new_len_str) <= len(old_len_str):
            # Замена на месте
            target_data[len_pos : len_pos + len(old_len_str)] = new_len_str.ljust(len(old_len_str)).encode()[: len(old_len_str)]

    delta = len(new_stream) - t_str_len
    target_data[t_str_start : t_str_start + t_str_len] = new_stream

    # Обновить xref для объектов с offset > t_str_start
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", target_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > t_str_start:
                    new_offset = offset + delta
                    entries[em.start(1) : em.start(1) + 10] = f"{new_offset:010d}".encode()
            target_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", target_data)
        if startxref_m:
            sx_pos = int(startxref_m.group(1))
            if sx_pos > t_str_start:
                p = startxref_m.start(1)
                target_data[p : p + len(str(sx_pos))] = str(sx_pos + delta).encode()

    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Заменить obj 17 (font) на эталон из 13.pdf")
    ap.add_argument("input", help="receipt_add.pdf или аналогичный")
    ap.add_argument("-o", "--output", default="receipt_obj17_etalon.pdf")
    ap.add_argument("--etalon", default=None, help="Эталон 13.pdf (по умолчанию Downloads)")
    args = ap.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1

    etalon_paths = [
        Path(args.etalon).expanduser().resolve() if args.etalon else None,
        Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
        Path(__file__).parent / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
    ]
    etalon = None
    for p in etalon_paths:
        if p and p.exists():
            etalon = p
            break
    if not etalon:
        print("[ERROR] Не найден эталон 13.pdf. Укажите --etalon путь", file=sys.stderr)
        return 1

    target_data = bytearray(inp.read_bytes())
    etalon_data = etalon.read_bytes()

    if not replace_obj17(target_data, etalon_data):
        print("[ERROR] Не удалось заменить obj 17", file=sys.stderr)
        return 1

    out_path = Path(args.output).resolve()
    out_path.write_bytes(target_data)
    print(f"✅ obj 17 заменён на эталон из {etalon.name}")
    print(f"   Результат: {out_path}")
    print("   ВНИМАНИЕ: Ф, Ч, Ю могут отображаться некорректно (шрифт эталона без этих глифов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
