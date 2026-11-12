#!/usr/bin/env python3
"""Пересчёт tm_x во всех TJ после замены шрифта.

Когда в PDF подставлен шрифт с другим /W, позиции (tm_x) становятся неверными.
Эта функция пересчитывает tm_x для каждого TJ в content stream по формуле:
  new_tm_x = wall - advance_units * (font_scale)
где advance_units берётся из нового /W.
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path

from vtb_patch_from_config import _parse_cid_widths, _tj_advance_units
from vtb_sber_reference import get_vtb_per_field_params
from vtb_sbp_layout import get_layout_values, WALL_RATIO


def _get_wall(pdf_bytes: bytes, pdf_path: Path) -> float:
    """wall из PDF или layout."""
    params = get_vtb_per_field_params(pdf_path)
    layout = get_layout_values()
    return params.get("wall") or layout.get("wall", 257.08)


def reflow_content_stream(dec: bytes, cid_widths: dict[int, int], wall: float) -> bytes:
    """Пересчитать tm_x для всех TJ с x>50 (правая колонка).

    advance_pt = advance_units * (font_size/1000).
    Керн -11.11111 = сумма (font 13.5pt), иначе 9pt.
    """
    pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
    result = dec

    for mt in list(re.finditer(pat, dec)):
        tm_x = float(mt.group(2))
        if tm_x < 50:
            continue
        content = mt.group(5)
        advance_units = _tj_advance_units(content, cid_widths)
        if advance_units <= 0:
            continue
        scale = 0.0135 if b"-11.11111" in content else 0.009  # сумма 13.5pt, остальное 9pt
        advance_pt = advance_units * scale
        new_x = wall - advance_pt
        if new_x < 20 or new_x > wall - 10:
            continue
        repl = mt.group(1) + f"{new_x:.5f}".encode() + mt.group(3) + mt.group(5) + mt.group(6)
        result = result.replace(mt.group(0), repl, 1)
    return result


def reflow_pdf(data: bytearray, pdf_path: Path, cid_widths: dict[int, int] | None = None) -> bytes:
    """Применить reflow ко всем content streams в PDF."""
    wall = _get_wall(bytes(data), Path(pdf_path))
    if cid_widths is None:
        cid_widths = _parse_cid_widths(bytes(data))

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue

        new_dec = reflow_content_stream(dec, cid_widths, wall)
        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            data[len_num_start : len_num_start + len(old_len_str)] = new_len_str.ljust(len(old_len_str))
            if delta != 0:
                xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
                if xref_m:
                    entries = bytearray(xref_m.group(3))
                    for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                        offset = int(em.group(1))
                        if offset > stream_start:
                            entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                    data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        break
    return bytes(data)
