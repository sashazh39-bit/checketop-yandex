#!/usr/bin/env python3
"""Заменить obj 12 (ToUnicode CMap) в receipt на эталон из 13.pdf.

Цель: подставить ToUnicode как в эталоне — убрать след добавленных CID.
ВНИМАНИЕ: После замены Ф, Ч, Ю закодированы в content как новые CID (89,90,91),
но в ToUnicode эталона их нет — отображение будет некорректно.

Использование:
  python3 patch_obj12_from_etalon.py receipt_add.pdf -o receipt_obj12_etalon.pdf
  python3 patch_obj12_from_etalon.py receipt_add.pdf --etalon "/path/to/13.pdf" -o out.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def find_obj_stream_range(data: bytes, obj_num: int) -> tuple[int, int, int, int] | None:
    """Найти obj N: (obj_start, stream_start, stream_len, obj_end)."""
    pat = rf"{obj_num}\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n"
    m = re.search(pat.encode(), data, re.DOTALL)
    if not m:
        return None
    obj_start = m.start()
    stream_len = int(m.group(2))
    stream_start = m.end()
    stream_end = stream_start + stream_len
    endstream = data.find(b"endstream", stream_end)
    endobj = data.find(b"endobj", stream_end)
    if endstream < 0 or endobj < 0:
        return None
    obj_end = endobj + len(b"endobj")
    return obj_start, stream_start, stream_len, obj_end


def replace_obj_stream(
    target_data: bytearray,
    etalon_data: bytes,
    obj_num: int,
    len_key: str = "/Length",
) -> bool:
    """Заменить obj N stream в target на stream из etalon. Возвращает True при успехе."""
    target_info = find_obj_stream_range(bytes(target_data), obj_num)
    etalon_info = find_obj_stream_range(etalon_data, obj_num)
    if not target_info or not etalon_info:
        return False
    t_start, t_str_start, t_str_len, _ = target_info
    _, e_str_start, e_str_len, _ = etalon_info

    e_stream_data = etalon_data[e_str_start : e_str_start + e_str_len]
    new_stream = e_stream_data

    # Обновить /Length в dict (может отличаться разрядностью)
    len_pat = rf"{obj_num}\s+0\s+obj\s*<<.*?/Length\s+(\d+)"
    len_m = re.search(len_pat.encode(), target_data[t_start : t_start + 500], re.DOTALL)
    if len_m:
        len_pos = t_start + len_m.start(1)
        old_len_str = len_m.group(1).decode()
        new_len_str = str(len(new_stream))
        max_len = max(len(old_len_str), len(new_len_str))
        target_data[len_pos : len_pos + len(old_len_str)] = (
            new_len_str.ljust(max_len).encode()[: len(old_len_str)]
        )

    delta = len(new_stream) - t_str_len
    if delta > 0:
        target_data[t_str_start : t_str_start + t_str_len] = new_stream
    else:
        target_data[t_str_start : t_str_start + t_str_len] = new_stream + bytes(-delta)

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
    ap = argparse.ArgumentParser(description="Заменить obj 12 (ToUnicode) на эталон из 13.pdf")
    ap.add_argument("input", help="receipt_add.pdf или аналогичный")
    ap.add_argument("-o", "--output", default="receipt_obj12_etalon.pdf")
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

    if not replace_obj_stream(target_data, etalon_data, 12):
        print("[ERROR] Не удалось заменить obj 12", file=sys.stderr)
        return 1

    out_path = Path(args.output).resolve()
    out_path.write_bytes(target_data)
    print(f"✅ obj 12 (ToUnicode) заменён на эталон из {etalon.name}")
    print(f"   Результат: {out_path}")
    print("   ВНИМАНИЕ: Ф, Ч, Ю закодированы новыми CID — в эталоне их нет, отображение может сломаться")
    return 0


if __name__ == "__main__":
    sys.exit(main())
