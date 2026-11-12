#!/usr/bin/env python3
"""Заменить ToUnicode в PDF: убрать bfrange, исправить 0222/023F/0240→ФЧЮ.

Проблема: beginbfrange перезаписывает наши маппинги, копирование даёт ȿɜɝ вместо ФЧЮ.
Решение: взять cid_to_uni из target (или эталона), override 0222/023F/0240→ФЧЮ,
собрать один beginbfchar (без bfrange) и заменить ВСЕ ToUnicode streams.

Использование:
  python3 fix_tounicode_replace.py check_3a_deepcopy.pdf -o fixed.pdf
  python3 fix_tounicode_replace.py check.pdf --etalon 13.pdf -o fixed.pdf
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path


def _extract_cid_to_uni(pdf_data: bytes) -> dict[int, int]:
    """CID → Unicode из всех ToUnicode streams."""
    cid_to_uni: dict[int, int] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"beginbfchar" not in dec and b"beginbfrange" not in dec:
            continue
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = int(mm.group(1).decode(), 16)
            uni = int(mm.group(2).decode(), 16)
            cid_to_uni[cid] = uni
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
            if s2 >= s1:
                for i in range(s2 - s1 + 1):
                    cid_to_uni[s1 + i] = d + i
    return cid_to_uni


def _find_all_tounicode_streams(pdf_data: bytes) -> list[tuple[int, int, int]]:
    """Список (stream_start, stream_len, len_pos) для всех ToUnicode."""
    result: list[tuple[int, int, int]] = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"begincmap" in dec and (b"beginbfchar" in dec or b"beginbfrange" in dec):
            result.append((stream_start, stream_len, m.start(2)))
    return result


def fix(in_path: Path, out_path: Path, etalon_path: Path | None = None) -> bool:
    """Заменить все ToUnicode: base из target (или etalon) + override Ф,Ч,Ю."""
    target_data = bytearray(in_path.read_bytes())
    streams = _find_all_tounicode_streams(bytes(target_data))
    if not streams:
        print("[ERROR] ToUnicode не найден в target", file=sys.stderr)
        return False

    # База: target или etalon
    base_data = etalon_path.read_bytes() if etalon_path and etalon_path.exists() else bytes(target_data)
    cid_to_uni = _extract_cid_to_uni(base_data)
    if not cid_to_uni:
        print("[ERROR] ToUnicode не найден в base", file=sys.stderr)
        return False

    cid_to_uni[0x0222] = 0x0424
    cid_to_uni[0x023F] = 0x0427
    cid_to_uni[0x0240] = 0x042E
    entries = [f"<{cid:04X}> <{uni:04X}>" for cid, uni in sorted(cid_to_uni.items())]
    body = f"{len(entries)} beginbfchar\n" + "\n".join(entries) + "\nendbfchar\n"
    cmap = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n" + body.encode("latin-1") + b"\nendcmap\nend\nend\n"
    new_raw = zlib.compress(cmap, 9)

    # Заменить каждый ToUnicode stream (с конца, чтобы смещения не сбивались)
    streams_desc = sorted(streams, key=lambda x: x[0], reverse=True)
    total_delta = 0
    for stream_start, stream_len, len_pos in streams_desc:
        delta = len(new_raw) - stream_len
        target_data[stream_start : stream_start + stream_len] = new_raw
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        slice_len = min(len(old_len_str), len(new_len_str))
        target_data[len_pos : len_pos + slice_len] = new_len_str[:slice_len]
        if len(new_len_str) > len(old_len_str):
            # Вставить лишние цифры (редкий случай)
            target_data[len_pos + slice_len : len_pos + len(old_len_str)] = new_len_str[slice_len:].ljust(len(old_len_str) - slice_len)
        total_delta += delta

    if total_delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", target_data)
        if xref_m:
            min_start = min(s[0] for s in streams)
            entries_bytes = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries_bytes):
                offset = int(em.group(1))
                if offset > min_start:
                    entries_bytes[em.start(1) : em.start(1) + 10] = f"{offset + total_delta:010d}".encode()
            target_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries_bytes)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", target_data)
        if startxref_m and min(streams, key=lambda x: x[0])[0] < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            target_data[p : p + len(str(old_p))] = str(old_p + total_delta).encode()

    out_path.write_bytes(target_data)
    print(f"ToUnicode заменён во всех {len(streams)} stream(s): {len(cid_to_uni)} маппингов, 0222→Ф 023F→Ч 0240→Ю")
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Заменить ToUnicode: убрать bfrange, 0222/023F/0240→ФЧЮ")
    ap.add_argument("input", type=Path, help="PDF для исправления")
    ap.add_argument("--etalon", type=Path, default=None, help="Эталон (опционально, по умолчанию target)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Выходной PDF")
    args = ap.parse_args()
    inp = args.input.resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1
    etalon = args.etalon.resolve() if args.etalon else None
    out = (args.output or inp).resolve()
    return 0 if fix(inp, out, etalon) else 1


if __name__ == "__main__":
    sys.exit(main())
