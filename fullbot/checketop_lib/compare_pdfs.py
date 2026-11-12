#!/usr/bin/env python3
"""
Сравнение двух PDF: оригинал vs результат замены счёта.
Показывает точные отличия в content stream и Document ID.
"""
import re
import zlib
import sys
from pathlib import Path


def get_id(data: bytes) -> str | None:
    m = re.search(rb'/ID\s*\[<([0-9A-Fa-f]+)>', data)
    return m.group(1).decode() if m else None


def get_content_streams(data: bytes) -> list[bytes]:
    streams = []
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        ln = int(m.group(2))
        start = m.end()
        try:
            dec = zlib.decompress(data[start : start + ln])
            if b"BT" in dec:
                streams.append(dec)
        except Exception:
            pass
    return streams


def find_diff_offset(a: bytes, b: bytes) -> int | None:
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return len(a) if len(a) != len(b) else None


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: python3 compare_pdfs.py ОРИГИНАЛ.pdf ЗАМЕНЁННЫЙ.pdf")
        return 1
    orig_path = Path(sys.argv[1]).resolve()
    mod_path = Path(sys.argv[2]).resolve()
    if not orig_path.exists():
        print(f"[ERROR] Не найден: {orig_path}")
        return 1
    if not mod_path.exists():
        print(f"[ERROR] Не найден: {mod_path}")
        return 1

    orig_data = orig_path.read_bytes()
    mod_data = mod_path.read_bytes()

    print("=" * 60)
    print("СРАВНЕНИЕ PDF")
    print("=" * 60)
    print(f"Оригинал:    {orig_path.name} ({len(orig_data)} bytes)")
    print(f"Заменённый:  {mod_path.name} ({len(mod_data)} bytes)")
    print()

    # Document ID
    orig_id = get_id(orig_data)
    mod_id = get_id(mod_data)
    print("Document ID:")
    print(f"  Оригинал:   {orig_id}")
    print(f"  Заменённый: {mod_id}")
    print(f"  Совпадают:  {'Да' if orig_id == mod_id else 'НЕТ'}")
    print()

    # Content streams
    orig_streams = get_content_streams(orig_data)
    mod_streams = get_content_streams(mod_data)
    print(f"Content streams: {len(orig_streams)} vs {len(mod_streams)}")

    for i, (oa, ob) in enumerate(zip(orig_streams, mod_streams)):
        if oa == ob:
            print(f"  Stream {i}: идентичны")
        else:
            pos = find_diff_offset(oa, ob)
            print(f"  Stream {i}: ОТЛИЧАЮТСЯ (первое отличие на позиции {pos})")
            if pos is not None:
                ctx = 80
                start = max(0, pos - 40)
                end = min(len(oa), len(ob), pos + 40)
                print(f"    Оригинал [{start}:{end}]:")
                print(f"      {oa[start:end]!r}")
                print(f"    Заменённый [{start}:{end}]:")
                print(f"      {ob[start:end]!r}")
                # Попытка найти строку с account
                for chunk in [oa, ob]:
                    if b"408178" in chunk or b"<" in chunk:
                        for j in range(0, len(chunk) - 60, 4):
                            seg = chunk[j : j + 60]
                            if b"408178" in seg or (b">" in seg and b"<" in seg):
                                pass  # could extract more context

    if len(orig_streams) != len(mod_streams):
        print(f"  [WARN] Разное количество streams!")
    print()
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
