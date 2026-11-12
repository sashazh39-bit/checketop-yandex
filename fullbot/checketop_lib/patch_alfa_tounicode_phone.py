#!/usr/bin/env python3
"""
[УСТАРЕЛО / НЕ ИСПОЛЬЗОВАТЬ для отображения]

ToUnicode можно сопоставить U+002B с новым CID, но глифы в embedded subset
остаются «чужими» — на экране получается каша (Т7, р911о…) и съезжает сумма.

Для нормального «+7 (911)…» используйте шаблон AM_1774146329068.pdf
(build_statement_zhukov_177414.py), а не этот патч.
"""
from __future__ import annotations

import re
import tempfile
import zlib
from pathlib import Path


def _update_xref_after_stream_change(data: bytearray, stream_start: int, delta: int) -> None:
    if delta == 0:
        return
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
        data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if startxref_m and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()


def add_sbp_phone_chars_to_pdf(in_path: Path, out_path: Path) -> bool:
    """
    Скопировать PDF, расширить первый ToUnicode Oracle-Identity тремя bfchar.
    """
    data = bytearray(in_path.read_bytes())
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", bytes(data), re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"Oracle-Identity-UCS" not in dec or b"beginbfchar" not in dec:
            continue

        count_m = re.search(rb"(\d+)\s+beginbfchar", dec)
        if not count_m:
            continue
        old_count = int(count_m.group(1))
        new_count = old_count + 3
        dec = dec[: count_m.start(1)] + str(new_count).encode() + dec[count_m.end(1) :]

        insert_pos = dec.find(b"endbfchar")
        if insert_pos < 0:
            continue
        extra = b"\r\n<004D><002B>\r\n<004E><0028>\r\n<004F><0029>\r\n"
        new_dec = dec[:insert_pos] + extra + dec[insert_pos:]

        new_raw = zlib.compress(new_dec, 9)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data[:] = (
            data[:stream_start] + new_raw + data[stream_start + stream_len :]
        )
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str
        _update_xref_after_stream_change(data, stream_start, delta)
        out_path.write_bytes(bytes(data))
        return True
    return False


def materialize_base_with_phone_glyphs(base: Path) -> Path:
    """Временный PDF с расширенным ToUnicode; вызывающий должен удалить файл."""
    tf = Path(tempfile.mktemp(suffix="_alfa_phone_cmap.pdf"))
    if not add_sbp_phone_chars_to_pdf(base, tf):
        tf.unlink(missing_ok=True)
        raise RuntimeError("Не найден Oracle ToUnicode для расширения (+ ( ) )")
    return tf


if __name__ == "__main__":
    import sys

    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ok = add_sbp_phone_chars_to_pdf(a, b)
    print("OK" if ok else "FAIL", b)
