#!/usr/bin/env python3
"""Копирование метаданных (ID, CreationDate, Producer) из одного PDF в другой.

Использование:
  python3 copy_metadata.py --from check_3a_compact.pdf --to check_3a_add.pdf -o check_3a_add_meta.pdf
  python3 copy_metadata.py --from src.pdf --to dst.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def _copy_field(data: bytearray, pattern: bytes, src_bytes: bytes, group: int = 1) -> bool:
    """Заменить группу в pattern на src_bytes. group=1 — первая группа."""
    m = re.search(pattern, data)
    if not m or group > m.lastindex:
        return False
    start, end = m.start(group), m.end(group)
    new_val = src_bytes.ljust(end - start)[: end - start]
    data[start:end] = new_val
    return True


def copy_metadata(from_pdf: Path, to_pdf: Path, out: Path | None = None) -> bool:
    """Скопировать ID, CreationDate, Producer из from_pdf в to_pdf."""
    src = from_pdf.read_bytes()
    dst = bytearray(to_pdf.read_bytes())
    changed = False

    # 1. Document ID
    id_src = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', src)
    id_dst = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', dst)
    if id_src and id_dst and len(id_src.group(1)) == 32 and len(id_dst.group(1)) == 32:
        slot_len = id_dst.end(1) - id_dst.start(1)
        new1 = id_src.group(1).ljust(slot_len)[:slot_len]
        dst[id_dst.start(1) : id_dst.end(1)] = new1
        dst[id_dst.start(2) : id_dst.end(2)] = new1
        changed = True

    # 2. CreationDate
    cd_src = re.search(rb'/CreationDate\s*\(([^)]+)\)', src)
    cd_dst = re.search(rb'/CreationDate\s*\(([^)]+)\)', dst)
    if cd_src and cd_dst:
        new_cd = cd_src.group(1).ljust(len(cd_dst.group(1)))[: len(cd_dst.group(1))]
        dst[cd_dst.start(1) : cd_dst.end(1)] = new_cd
        changed = True

    # 3. Producer
    prod_src = re.search(rb'/Producer\s*\(([^)]*)\)', src)
    prod_dst = re.search(rb'/Producer\s*\(([^)]*)\)', dst)
    if prod_src and prod_dst:
        new_prod = prod_src.group(1).ljust(len(prod_dst.group(1)))[: len(prod_dst.group(1))]
        dst[prod_dst.start(1) : prod_dst.end(1)] = new_prod
        changed = True

    out_path = out or to_pdf
    out_path.write_bytes(dst)
    return changed


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Копирование метаданных между PDF")
    ap.add_argument("--from", "-f", dest="src", type=Path, required=True, help="Источник метаданных")
    ap.add_argument("--to", "-t", dest="dst", type=Path, required=True, help="Целевой PDF")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Выход (по умол. = --to)")
    args = ap.parse_args()
    if not args.src.exists():
        print(f"[ERROR] Не найден: {args.src}", file=sys.stderr)
        return 1
    if not args.dst.exists():
        print(f"[ERROR] Не найден: {args.dst}", file=sys.stderr)
        return 1
    copy_metadata(args.src, args.dst, args.output)
    print(f"✅ Метаданные из {args.src.name} → {args.output or args.dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
