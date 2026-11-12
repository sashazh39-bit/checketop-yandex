#!/usr/bin/env python3
"""Патч ToUnicode: добавить недостающие CID→Unicode для всех CIDs из content.

Анализ check 3a_compact/deepcopy показал: CID 0x005C (backslash) не имеет маппинга →
копирование не работает. Скрипт находит такие CIDs и добавляет Identity-маппинг.

Использование:
  python3 fix_copy_tounicode.py "Лучший чек/check 3a_compact.pdf" -o "Лучший чек/check 3a_compact_fixed.pdf"
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path


def _extract_content_cids(pdf_data: bytes) -> set[int]:
    """Все CIDs из content streams (TJ literal format)."""
    used = set()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"TJ" not in dec:
            continue
        def _unescape(s: bytes) -> bytes:
            out = bytearray()
            i = 0
            while i < len(s):
                if s[i] == 0x5C and i + 1 < len(s):
                    out.append(s[i + 1])
                    i += 2
                    continue
                out.append(s[i])
                i += 1
            return bytes(out)
        for part, kern in re.findall(rb"\((.*?)\)|(-?\d+(?:\.\d+)?)", dec):
            if part:
                vals = list(_unescape(part))
                for j in range(0, len(vals) - 1, 2):
                    cid = (vals[j] << 8) + vals[j + 1]
                    used.add(cid)
    return used


def _extract_cid_to_uni(pdf_data: bytes) -> dict[int, int]:
    """CID → Unicode из ToUnicode."""
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


def _find_tounicode_stream(pdf_data: bytes) -> tuple[int, int, int] | None:
    """ToUnicode stream: (start, len, len_pos)."""
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
            return stream_start, stream_len, m.start(2)
    return None


def fix_pdf(in_path: Path, out_path: Path) -> bool:
    """Добавить в ToUnicode Identity-маппинг для CIDs из content без маппинга."""
    data = bytearray(in_path.read_bytes())
    content_cids = _extract_content_cids(bytes(data))
    cid_to_uni = _extract_cid_to_uni(bytes(data))
    missing = content_cids - set(cid_to_uni.keys())
    if not missing:
        out_path.write_bytes(data)
        print("Все CIDs уже имеют маппинг. Копирование должно работать.")
        return True
    info = _find_tounicode_stream(bytes(data))
    if not info:
        print("[ERROR] ToUnicode stream не найден", file=sys.stderr)
        return False
    stream_start, stream_len, len_pos = info
    dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
    # Identity: CID N → Unicode N (для ASCII/печатаемых)
    add_pairs = [(cid, cid) for cid in sorted(missing) if cid < 0xE000]
    if not add_pairs:
        out_path.write_bytes(data)
        return True
    block = b"\n" + str(len(add_pairs)).encode() + b" beginbfchar\n"
    for cid, uni in add_pairs:
        block += f"<{cid:04X}> <{uni:04X}>\n".encode()
    block += b"endbfchar\n"
    # Вставить перед endcmap — последний блок имеет приоритет (иначе bfrange перезаписывает)
    endcmap = dec.find(b"endcmap")
    if endcmap < 0:
        return False
    new_dec = dec[:endcmap] + block + dec[endcmap:]
    new_raw = zlib.compress(new_dec, 9)
    delta = len(new_raw) - stream_len
    data[stream_start : stream_start + stream_len] = new_raw
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    data[len_pos : len_pos + len(old_len_str)] = new_len_str[: len(old_len_str)].ljust(len(old_len_str))
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and stream_start < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            data[p : p + len(str(old_p))] = str(old_p + delta).encode()
    out_path.write_bytes(data)
    print(f"Добавлено {len(add_pairs)} маппингов: {[f'0x{c:04X}' for c in sorted(missing)]}")
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Добавить недостающие CID→Unicode для копирования")
    ap.add_argument("input", type=Path, help="Входной PDF")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Выходной PDF")
    args = ap.parse_args()
    inp = args.input.resolve()
    out = (args.output or inp).resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1
    return 0 if fix_pdf(inp, out) else 1


if __name__ == "__main__":
    sys.exit(main())
