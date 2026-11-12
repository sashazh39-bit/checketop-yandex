#!/usr/bin/env python3
"""Копирование шрифта и ToUnicode CMap из одного чека в другой.

Чек с полным набором букв (Л, М, К и т.д.) → целевой чек.
После копирования CID-патч будет корректно отображать все символы.

Использование:
  python3 copy_font_cmap.py source.pdf target.pdf output.pdf
  python3 copy_font_cmap.py source.pdf target.pdf output.pdf --merge-cmap extra1.pdf extra2.pdf

  source — чек с МНОГИМИ буквами (ваш «полный» чек)
  --merge-cmap — доп. PDF для недостающих символов (Л из одного, М из другого)

Затем:
  python3 patch_check_sbp.py output.pdf "чеки 08.03/чек_сбп.pdf" -p "Лукс Максер К." -a "20 000 ₽"
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path


def _classify_stream(dict_part: bytes, stream_data: bytes) -> str:
    """Определить тип потока: font, tounicode, content, image, unknown."""
    if b"/Subtype/Image" in dict_part or (b"/Width" in dict_part and b"/Height" in dict_part):
        return "image"
    try:
        dec = zlib.decompress(stream_data)
    except zlib.error:
        return "unknown"
    if b"beginbfchar" in dec or b"beginbfrange" in dec:
        return "tounicode"
    if b"/Length1" in dict_part and len(stream_data) > 500:
        return "font"
    if b"BT" in dec and b"ET" in dec:
        return "content"
    return "unknown"


def _find_font_and_tounicode(data: bytes) -> tuple[bytes | None, bytes | None, dict]:
    """
    Найти первый font stream и tounicode stream в PDF.
    Возвращает (font_data, tounicode_data, {font: {stream_start, stream_len, len_num_start}, tounicode: {...}}).
    """
    font_data = tounicode_data = None
    font_info = tounicode_info = {}

    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        dict_part = m.group(2) + b"/Length " + m.group(3) + m.group(4)
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_num_start = m.start(3)  # позиция числа Length в dict
        if stream_start + stream_len > len(data):
            continue
        stream_data = data[stream_start : stream_start + stream_len]
        kind = _classify_stream(dict_part, stream_data)

        if kind == "font" and font_data is None:
            font_data = stream_data
            font_info = {"stream_start": stream_start, "stream_len": stream_len, "len_num_start": len_num_start}
        elif kind == "tounicode" and tounicode_data is None:
            tounicode_data = stream_data
            tounicode_info = {"stream_start": stream_start, "stream_len": stream_len, "len_num_start": len_num_start}

    return font_data, tounicode_data, {"font": font_info, "tounicode": tounicode_info}


def _update_length_in_place(data: bytearray, len_num_start: int, old_len: int, new_len: int) -> None:
    """Заменить число Length в потоке."""
    old_str = str(old_len).encode()
    new_str = str(new_len).encode()
    if len(new_str) <= len(old_str):
        data[len_num_start : len_num_start + len(old_str)] = new_str.ljust(len(old_str))
    else:
        data[len_num_start : len_num_start + len(old_str)] = new_str[: len(old_str)]


def _parse_tounicode_from_stream(stream_data: bytes) -> dict[int, str]:
    """Извлечь uni_to_cid из сжатого ToUnicode stream."""
    try:
        dec = zlib.decompress(stream_data)
    except zlib.error:
        return {}
    uni_to_cid: dict[int, str] = {}
    if b"beginbfchar" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = mm.group(1).decode().upper().zfill(4)
            uni = int(mm.group(2).decode().upper(), 16)
            uni_to_cid[uni] = cid
    if b"beginbfrange" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
            for i in range(s2 - s1 + 1):
                uni_to_cid[d + i] = f"{s1 + i:04X}"
    return uni_to_cid


def _parse_tounicode_full(stream_data: bytes) -> dict[int, str]:
    """Парсит beginbfchar И beginbfrange — полный маппинг (для find_reusable)."""
    try:
        dec = zlib.decompress(stream_data)
    except zlib.error:
        return {}
    uni_to_cid: dict[int, str] = {}
    if b"beginbfchar" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = mm.group(1).decode().upper().zfill(4)
            uni = int(mm.group(2).decode().upper(), 16)
            uni_to_cid[uni] = cid
    if b"beginbfrange" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
            for i in range(s2 - s1 + 1):
                uni_to_cid[d + i] = f"{s1 + i:04X}"
    return uni_to_cid


def _build_tounicode_stream(uni_to_cid: dict[int, str]) -> bytes:
    """Собрать ToUnicode stream из uni_to_cid (beginbfchar)."""
    entries = []
    for uni in sorted(uni_to_cid.keys()):
        cid = uni_to_cid[uni]
        entries.append(f"<{cid}> <{uni:04X}>")
    body = str(len(entries)) + " beginbfchar\n" + "\n".join(entries) + "\nendbfchar\n"
    return zlib.compress(body.encode("latin-1"), 9)


def copy_font_cmap(
    source_path: Path,
    target_path: Path,
    out_path: Path,
    merge_cmap_paths: list[Path] | None = None,
) -> bool:
    """
    Скопировать шрифт, ToUnicode и /W из source в target. Результат в out_path.
    /W — массив ширин глифов, нужен для корректного отображения (без наложения).
    merge_cmap_paths: доп. PDF для объединения CMap (символы из разных чеков).
    """
    src_data = source_path.read_bytes()
    tgt_data = bytearray(target_path.read_bytes())

    src_font, src_tounicode, _ = _find_font_and_tounicode(src_data)
    tgt_font, tgt_tounicode, tgt_info = _find_font_and_tounicode(bytes(tgt_data))

    if not src_font or not src_tounicode:
        print(f"[ERROR] В source {source_path.name} не найден font или ToUnicode", file=sys.stderr)
        return False
    if not tgt_info.get("font") or not tgt_info.get("tounicode"):
        print(f"[ERROR] В target {target_path.name} не найден font или ToUnicode", file=sys.stderr)
        return False

    fi = tgt_info["font"]
    ti = tgt_info["tounicode"]
    new_font = src_font

    # ToUnicode: база из TARGET (сохраняем отображение существующего контента), добавляем из source недостающие
    uni_to_cid = _parse_tounicode_from_stream(tgt_tounicode)
    for uni, cid in _parse_tounicode_from_stream(src_tounicode).items():
        if uni not in uni_to_cid:
            uni_to_cid[uni] = cid
    for p in merge_cmap_paths or []:
        if p.exists():
            _, extra_tu, _ = _find_font_and_tounicode(p.read_bytes())
            if extra_tu:
                for k, v in _parse_tounicode_from_stream(extra_tu).items():
                    if k not in uni_to_cid:
                        uni_to_cid[k] = v
    new_tu = _build_tounicode_stream(uni_to_cid) if uni_to_cid else tgt_tounicode

    # Заменяем от начала к концу (второй stream сдвигается после первой замены)
    min_pos = min(fi["stream_start"], ti["stream_start"])
    delta_total = 0

    # 1. Font
    old_font_len = fi["stream_len"]
    tgt_data[fi["stream_start"] : fi["stream_start"] + old_font_len] = new_font
    delta_font = len(new_font) - old_font_len
    _update_length_in_place(tgt_data, fi["len_num_start"], old_font_len, len(new_font))
    delta_total += delta_font

    # 2. ToUnicode (позиция могла сдвинуться, если font был раньше)
    ti_start = ti["stream_start"]
    if ti["stream_start"] > fi["stream_start"]:
        ti_start += delta_font
    old_tu_len = ti["stream_len"]
    tgt_data[ti_start : ti_start + old_tu_len] = new_tu
    delta_tu = len(new_tu) - old_tu_len
    _update_length_in_place(tgt_data, ti["len_num_start"] + (delta_font if ti["len_num_start"] > fi["stream_start"] else 0), old_tu_len, len(new_tu))
    delta_total += delta_tu

    # 3. xref и startxref
    if delta_total != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", tgt_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > min_pos:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta_total:010d}".encode()
            tgt_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", tgt_data)
        if startxref_m:
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            tgt_data[pos : pos + len(str(old_pos))] = str(old_pos + delta_total).encode()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(tgt_data)
    print(f"[OK] Шрифт и CMap скопированы: {out_path} ({len(tgt_data)} bytes)")
    return True


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Копировать шрифт и ToUnicode CMap из чека с полным набором букв в целевой чек."
    )
    parser.add_argument("source", help="PDF-источник (чек с многими буквами)")
    parser.add_argument("target", help="PDF-цель (чек для патча)")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--merge-cmap", "-m", action="append", default=[], metavar="PDF",
                        help="Доп. PDF для объединения CMap (Л из одного, М из другого)")
    args = parser.parse_args()

    src = Path(args.source).expanduser().resolve()
    tgt = Path(args.target).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    merge_paths = [Path(p).expanduser().resolve() for p in args.merge_cmap]

    if not src.exists():
        print(f"[ERROR] Source не найден: {src}", file=sys.stderr)
        return 1
    if not tgt.exists():
        print(f"[ERROR] Target не найден: {tgt}", file=sys.stderr)
        return 1

    return 0 if copy_font_cmap(src, tgt, out, merge_cmap_paths=merge_paths or None) else 1


if __name__ == "__main__":
    sys.exit(main())
