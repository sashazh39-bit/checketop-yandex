#!/usr/bin/env python3
"""Добавить ToUnicode маппинги для Ф, Ч, Ю (CID 62,63,64 = 0x003E, 0x003F, 0x0040).

Чек использует эти CIDs в content stream. Эталонный ToUnicode их не содержит —
при копировании получаются символы >, @, ? вместо Ф, Ю, Ч.

Использование:
  python3 add_tounicode_cyrillic.py receipt_w_patched.pdf -o receipt_fixed.pdf
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

# CID → Unicode для Ф, Ч, Ю
# ADD mode (check 3,4): CIDs 62,63,64 в content
CID_TO_UNI_ADD = [
    (0x003E, 0x0424),  # Ф
    (0x003F, 0x0427),  # Ч
    (0x0040, 0x042E),  # Ю
]
# REPLACE mode: CIDs из find_reusable + безопасный слот для Ю.
# 0221=Е встречается только в ФИО-позициях (не в «Исходящий перевод» и др.),
# поэтому подмена 0221→Ю не ломает статичный текст.
CID_TO_UNI_REPLACE_DEFAULT = [
    (0x0222, 0x0424),  # Ф → слот Ж
    (0x023F, 0x0427),  # Ч → слот г
    (0x0221, 0x042E),  # Ю → слот Е (безопасно: не в статичных строках)
]


def _parse_cmap_to_cid_uni(dec: bytes) -> dict[int, int]:
    """Извлечь cid -> uni из декомпрессированного CMap."""
    cid_to_uni: dict[int, int] = {}
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
        cid, uni = int(m.group(1).decode(), 16), int(m.group(2).decode(), 16)
        cid_to_uni[cid] = uni
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
        s1, s2, d = int(m.group(1).decode(), 16), int(m.group(2).decode(), 16), int(m.group(3).decode(), 16)
        for i in range(s2 - s1 + 1):
            cid_to_uni[s1 + i] = d + i
    return cid_to_uni


# Явные identity-маппинги для копирования: * (счёт), ( ) (телефон)
COPY_FIX_PAIRS = [
    (0x002A, 0x002A),  # *
    (0x0028, 0x0028),  # (
    (0x0029, 0x0029),  # )
]


def find_tounicode_stream(data: bytes) -> tuple[int, int, int] | None:
    """Найти ToUnicode stream: (stream_start, stream_len, len_num_pos)."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_start = m.end()
        stream_len = int(m.group(2))
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            raw = data[stream_start : stream_start + stream_len]
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"begincmap" in dec and (b"beginbfchar" in dec or b"beginbfrange" in dec):
            return stream_start, stream_len, len_num_start
    return None


def _parse_cmap_uni_mapped(dec: bytes) -> set[int]:
    """Unicode коды, для которых уже есть CID→Unicode в cmap (beginbfchar + beginbfrange)."""
    mapped: set[int] = set()
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
        cid_val = int(m.group(1).decode(), 16)
        uni_val = int(m.group(2).decode(), 16)
        mapped.add(uni_val)
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
        s1, s2, d = int(m.group(1).decode(), 16), int(m.group(2).decode(), 16), int(m.group(3).decode(), 16)
        for i in range(s2 - s1 + 1):
            mapped.add(d + i)
    return mapped


def add_cyrillic_mappings(data: bytearray, use_replace_cids: bool = False, add_copy_fix: bool = True) -> bool:
    """Добавить beginbfchar блок с Ф,Ч,Ю и явные *, (, ) перед endcmap. Возвращает True при успехе."""
    cid_uni = CID_TO_UNI_REPLACE_DEFAULT if use_replace_cids else CID_TO_UNI_ADD
    info = find_tounicode_stream(bytes(data))
    if not info:
        return False
    stream_start, stream_len, len_num_start = info

    dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
    cid_to_uni = _parse_cmap_to_cid_uni(dec)

    # REPLACE: ВСЕГДА добавлять override 0222,023F,0240→Ф,Ч,Ю — beginbfrange перезаписывает
    # и даёт 0222→ȿ(023F) при копировании. Override в конце CMap имеет приоритет.
    # ADD: только если нет маппинга
    need_cyrillic_add = not use_replace_cids and {0x0424, 0x0427, 0x042E} <= set(cid_to_uni.values())
    need_cyrillic_replace = use_replace_cids or not need_cyrillic_add  # REPLACE — всегда

    # Явные *, (, ), \ для надёжного копирования
    copy_fix = [(0x002A, 0x002A), (0x0028, 0x0028), (0x0029, 0x0029), (0x005C, 0x005C)] if add_copy_fix else []

    cid_uni_pairs = (cid_uni if need_cyrillic_replace else (cid_uni if not need_cyrillic_add else [])) + copy_fix
    if not cid_uni_pairs:
        return True

    block = b"\n" + str(len(cid_uni_pairs)).encode() + b" beginbfchar\n"
    for cid, uni in cid_uni_pairs:
        block += f"<{cid:04X}> <{uni:04X}>\n".encode()
    block += b"endbfchar\n"

    # Вставить ПЕРЕД endcmap — последний блок имеет приоритет при copy/paste.
    # Раньше вставляли перед beginbfrange, и range перезаписывал Ф→ȿ, Ч→?, Ю→?.
    endcmap_pos = dec.find(b"endcmap")
    if endcmap_pos < 0:
        return False
    new_dec = dec[:endcmap_pos] + block + dec[endcmap_pos:]

    new_raw = zlib.compress(new_dec, 9)
    delta_stream = len(new_raw) - stream_len

    new_data = bytearray(
        data[:stream_start]
        + new_raw
        + data[stream_start + stream_len :]
    )

    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    num_end = len_num_start + len(old_len_str)
    new_data[len_num_start:num_end] = new_len_str
    delta_len = len(new_len_str) - len(old_len_str)
    delta_total = delta_stream + delta_len

    # Обновить xref
    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", new_data)
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = f"{offset + delta_total:010d}".encode()
        new_data = new_data[: xref_m.start(3)] + bytes(entries) + new_data[xref_m.end(3) :]

    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", new_data)
    if startxref_m and delta_total != 0 and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        new_data = new_data[:pos] + str(old_pos + delta_total).encode() + new_data[pos + len(str(old_pos)) :]

    data.clear()
    data.extend(new_data)
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Добавить ToUnicode для Ф, Ч, Ю")
    ap.add_argument("input", help="receipt_w_patched.pdf")
    ap.add_argument("-o", "--output", default=None, help="Выходной файл (по умолчанию перезапись)")
    ap.add_argument("--replace", action="store_true", help="REPLACE: CIDs 0222,023F,0240 (из find_reusable). Не 0029=) — ломает копирование телефона.")
    args = ap.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1

    out = Path(args.output).resolve() if args.output else inp
    data = bytearray(inp.read_bytes())

    if not add_cyrillic_mappings(data, use_replace_cids=args.replace):
        print("[ERROR] Не удалось добавить ToUnicode маппинги", file=sys.stderr)
        return 1

    out.write_bytes(data)
    print(f"✅ ToUnicode: добавлены Ф, Ч, Ю → CIDs 0x003E, 0x003F, 0x0040")
    print(f"   Результат: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
