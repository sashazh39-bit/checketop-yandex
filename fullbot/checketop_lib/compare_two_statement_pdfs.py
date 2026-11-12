#!/usr/bin/env python3
"""Сравнить два PDF-выписки: размер, /ID, ModDate, совпадение распакованных потоков, текст СБП."""
from __future__ import annotations

import argparse
import re
import sys
import zlib
from pathlib import Path


def _streams(raw: bytes) -> list[bytes]:
    out: list[bytes] = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", raw, re.DOTALL):
        ln = int(m.group(2))
        start = m.end()
        chunk = raw[start : start + ln]
        try:
            out.append(zlib.decompress(chunk))
        except zlib.error:
            out.append(chunk)
    return out


def _ids(raw: bytes) -> tuple[str | None, str | None]:
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw)
    if not m:
        return None, None
    return m.group(1).decode(), m.group(2).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", type=Path, help="PDF A (например проходящий)")
    ap.add_argument("b", type=Path, help="PDF B (например не проходящий)")
    args = ap.parse_args()
    pa, pb = args.a.expanduser().resolve(), args.b.expanduser().resolve()
    if not pa.exists() or not pb.exists():
        print("Файл не найден", file=sys.stderr)
        return 1
    ra, rb = pa.read_bytes(), pb.read_bytes()
    print(f"A: {pa} ({len(ra)} bytes)")
    print(f"B: {pb} ({len(rb)} bytes)")
    i0a, i1a = _ids(ra)
    i0b, i1b = _ids(rb)
    print(f"/ID A: {i0a}  {i1a}")
    print(f"/ID B: {i0b}  {i1b}")
    print(f"ID[0] совпадают: {i0a == i0b}   ID[1] совпадают: {i1a == i1b}")
    sa, sb = _streams(ra), _streams(rb)
    print(f"Число потоков: {len(sa)} vs {len(sb)}")
    diff_streams = [i for i, (x, y) in enumerate(zip(sa, sb)) if x != y]
    if len(sa) != len(sb):
        print("Разное число потоков!")
    elif diff_streams:
        print(f"Распакованное содержимое отличается в потоках (индексы 0..n): {diff_streams}")
        i = diff_streams[0]
        print(f"  первый отличие в потоке {i}: len {len(sa[i])} vs {len(sb[i])}")
    else:
        print("Все распакованные потоки побайтно совпадают.")

    try:
        import fitz

        for label, p in ("A", pa), ("B", pb):
            d = fitz.open(str(p))
            t = d[0].get_text()
            d.close()
            k = t.find("Перевод C822")
            line = t[k : k + 160].split("\n")[0] if k >= 0 else "(нет)"
            print(f"Текст СБП ({label}): {line!r}")
    except Exception as e:
        print("(fitz для текста недоступен)", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
