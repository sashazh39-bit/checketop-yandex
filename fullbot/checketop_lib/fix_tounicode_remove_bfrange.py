#!/usr/bin/env python3
"""Удалить beginbfrange для CIDs 0222, 023F, 0240 — чтобы наш beginbfchar работал.

Проблема: bfrange перезаписывает Ф,Ч,Ю. Решение: заменить однокомпонентные 
range <0222><0222><0416> и т.д. на пустые или удалить эти строки, оставив только
наш beginbfchar для 0222, 023F, 0240.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path


def _find_tounicode_stream(pdf_data: bytes) -> tuple[int, int, int, bytes] | None:
    """(stream_start, stream_len, len_pos, decompressed)."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_pos = m.start(2)
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"begincmap" in dec and (b"beginbfchar" in dec or b"beginbfrange" in dec):
            return stream_start, stream_len, len_pos, dec
    return None


# CIDs которые должны маппиться через наш bfchar, а не через bfrange
OVERRIDE_CIDS = {0x0222, 0x023F, 0x0240}


def _remove_bfrange_entries_for_cids(dec: bytes) -> bytes:
    """Удалить из beginbfrange строки, затрагивающие OVERRIDE_CIDS."""
    result = bytearray()
    i = 0
    in_bfrange = False
    bfrange_count = 0
    while i < len(dec):
        if dec[i:i+14] == b"beginbfrange":
            in_bfrange = True
            result.extend(dec[i:i+14])
            i += 14
            continue
        if dec[i:i+12] == b"endbfrange":
            in_bfrange = False
            result.extend(dec[i:i+12])
            i += 12
            continue
        if in_bfrange:
            m = re.match(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec[i:])
            if m:
                s1, s2 = int(m.group(1).decode(), 16), int(m.group(2).decode(), 16)
                skip = False
                for cid in OVERRIDE_CIDS:
                    if s1 <= cid <= s2:
                        skip = True
                        break
                if not skip:
                    result.extend(dec[i:i+m.end()])
                i += m.end()
                continue
        result.append(dec[i])
        i += 1
    return bytes(result)


def fix(in_path: Path, out_path: Path) -> bool:
    target_data = bytearray(in_path.read_bytes())
    info = _find_tounicode_stream(bytes(target_data))
    if not info:
        print("[ERROR] ToUnicode не найден", file=sys.stderr)
        return False
    stream_start, stream_len, len_pos, dec = info
    new_dec = _remove_bfrange_entries_for_cids(dec)
    new_raw = zlib.compress(new_dec, 9)
    delta = len(new_raw) - stream_len
    target_data[stream_start : stream_start + stream_len] = new_raw
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    target_data[len_pos : len_pos + len(old_len_str)] = new_len_str.ljust(len(old_len_str))[:len(old_len_str)]
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", target_data)
        if xref_m:
            ent = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", ent):
                if int(em.group(1)) > stream_start:
                    ent[em.start(1):em.start(1)+10] = f"{int(em.group(1))+delta:010d}".encode()
            target_data[xref_m.start(3):xref_m.end(3)] = bytes(ent)
        sx = re.search(rb"startxref\r?\n(\d+)\r?\n", target_data)
        if sx and stream_start < int(sx.group(1)):
            p = sx.start(1)
            target_data[p:p+len(str(int(sx.group(1))))] = str(int(sx.group(1))+delta).encode()
    out_path.write_bytes(target_data)
    print("Удалены bfrange-записи для 0222, 023F, 0240")
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()
    out = (args.output or args.input).resolve()
    if not args.input.exists():
        print("[ERROR] Не найден:", args.input)
        return 1
    return 0 if fix(args.input.resolve(), out) else 1


if __name__ == "__main__":
    sys.exit(main())
