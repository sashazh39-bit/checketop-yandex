#!/usr/bin/env python3
"""Перенос глифов Ф,Ч,Ю из ADD-чека (визуал ок) в REPLACE-чек (проходит проверку).

Структура base (compact) сохраняется, глифы берутся из add (где deepcopy даёт правильный визуал).

Использование:
  python3 transplant_glyphs.py --base check_3a_compact.pdf --glyphs check_3a_add.pdf -o check_3a_transplant.pdf
"""
from __future__ import annotations

import re
import zlib
import sys
from pathlib import Path
from io import BytesIO

BASE = Path(__file__).parent
TARGET_UNI = [0x0424, 0x0427, 0x042E]  # Ф, Ч, Ю


def _decompress_stream(raw: bytes) -> bytes:
    if raw.startswith(b"\r\n"):
        raw = raw[2:]
    elif raw.startswith(b"\n"):
        raw = raw[1:]
    return zlib.decompress(raw)


def _compress_stream(data: bytes) -> bytes:
    return zlib.compress(data, 9)


def _find_font_stream(data: bytes) -> tuple[int, int, int, bytes] | None:
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_num_start = m.start(3)
        dict_part = m.group(2) + m.group(4)
        if b"/Length1" not in dict_part or stream_len < 500:
            continue
        raw = data[stream_start : stream_start + stream_len]
        try:
            dec = _decompress_stream(raw)
        except zlib.error:
            continue
        if dec[:4] in (b"\x00\x01\x00\x00", b"OTTO"):
            return stream_start, stream_len, len_num_start, dec
    return None


def _get_cidtogid_map(pdf_data: bytes) -> dict[int, int] | None:
    from add_glyphs_to_13_03 import _get_cidtogid_map as _get
    return _get(pdf_data)


def _get_uni_to_cid(pdf_data: bytes) -> dict[int, int]:
    """Unicode -> CID из ToUnicode."""
    from copy_font_cmap import _find_font_and_tounicode, _parse_tounicode_full
    _, tu_data, _ = _find_font_and_tounicode(pdf_data)
    if not tu_data:
        return {}
    uni_to_cid = _parse_tounicode_full(tu_data)
    return {u: int(c, 16) for u, c in uni_to_cid.items()}


def _sync_w_widths(base_data: bytearray, glyphs_data: bytes, base_cids: dict[int, int], glyphs_uni_cid: dict[int, int]) -> bool:
    """Синхронизировать /W: скопировать ширины Ф,Ч,Ю из glyphs в base."""
    try:
        from vtb_patch_from_config import _parse_cid_widths
    except ImportError:
        return False
    glyphs_w = _parse_cid_widths(glyphs_data)
    base_w = _parse_cid_widths(bytes(base_data))
    cid_widths: dict[int, tuple[int, int]] = {}
    for uni, base_cid in base_cids.items():
        glyphs_cid = glyphs_uni_cid.get(uni) or glyphs_uni_cid.get(uni + 0x20)
        if glyphs_cid is None or glyphs_cid not in glyphs_w:
            continue
        old_w = base_w.get(base_cid)
        new_w = glyphs_w[glyphs_cid]
        if old_w is not None and old_w != new_w:
            cid_widths[base_cid] = (old_w, new_w)
    if not cid_widths:
        return True
    from add_glyphs_to_13_03 import _patch_w_in_place
    return _patch_w_in_place(base_data, cid_widths)


def transplant(base_path: Path, glyphs_path: Path, out_path: Path) -> bool:
    """Перенести глифы Ф,Ч,Ю из glyphs_path в base_path."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("[ERROR] pip install fonttools", file=sys.stderr)
        return False

    base_data = bytearray(base_path.read_bytes())
    glyphs_data = glyphs_path.read_bytes()

    base_info = _find_font_stream(bytes(base_data))
    glyphs_info = _find_font_stream(glyphs_data)
    if not base_info or not glyphs_info:
        print("[ERROR] Font stream не найден", file=sys.stderr)
        return False

    b_start, b_len, b_len_pos, b_font_bytes = base_info
    _, _, _, g_font_bytes = glyphs_info

    base_font = TTFont(BytesIO(b_font_bytes))
    glyphs_font = TTFont(BytesIO(g_font_bytes))
    base_ctg = _get_cidtogid_map(bytes(base_data))
    glyphs_ctg = _get_cidtogid_map(glyphs_data)
    base_uni_cid = _get_uni_to_cid(bytes(base_data))
    glyphs_uni_cid = _get_uni_to_cid(glyphs_data)

    if not base_ctg:
        base_ctg = {}
    if not glyphs_ctg:
        glyphs_ctg = {}

    # CIDs для Ф, Ч, Ю из ToUnicode базы (не find_reusable — base уже модифицирован)
    reuse_map = {u: base_uni_cid[u] for u in TARGET_UNI if u in base_uni_cid}
    if len(reuse_map) < 3:
        # Fallback: эталон для find_reusable (13.pdf)
        etalon = BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf"
        if etalon.exists():
            from find_reusable_cids import find_reusable
            _, reuse_map = find_reusable(etalon, base_ctg)
        if len(reuse_map) < 3:
            print("[ERROR] REPLACE-слоты не найдены в base (Ф,Ч,Ю)", file=sys.stderr)
            return False

    base_glyf = base_font.get("glyf")
    glyphs_glyf = glyphs_font.get("glyf")
    glyphs_order = glyphs_font.getGlyphOrder()
    base_order = base_font.getGlyphOrder()
    if not base_glyf or not glyphs_glyf:
        print("[ERROR] glyf table не найден", file=sys.stderr)
        return False

    import copy
    for target_uni in TARGET_UNI:
        base_cid = reuse_map.get(target_uni)
        if base_cid is None:
            continue
        donor_cid = glyphs_uni_cid.get(target_uni)
        if donor_cid is None:
            donor_cid = glyphs_uni_cid.get(target_uni + 0x20)  # строчная
        if donor_cid is None:
            print(f"[WARN] CID для U+{target_uni:04X} не найден в glyphs PDF", file=sys.stderr)
            continue
        donor_gid = glyphs_ctg.get(donor_cid, donor_cid)
        base_gid = base_ctg.get(base_cid)
        if base_gid is None or donor_gid >= len(glyphs_order):
            continue
        donor_gname = glyphs_order[donor_gid]
        base_gname = base_order[base_gid]
        if donor_gname not in glyphs_glyf:
            continue
        base_glyf[base_gname] = copy.deepcopy(glyphs_glyf[donor_gname])
        glyphs_hmtx = glyphs_font.get("hmtx")
        base_hmtx = base_font.get("hmtx")
        if glyphs_hmtx and donor_gname in glyphs_hmtx.metrics:
            base_hmtx.metrics[base_gname] = glyphs_hmtx.metrics[donor_gname]

    out_buf = BytesIO()
    base_font.save(out_buf)
    new_font_bytes = out_buf.getvalue()
    base_font.close()
    glyphs_font.close()

    # Синхронизировать /W: ширины Ф,Ч,Ю из glyphs (точная метрика под скопированные глифы)
    _sync_w_widths(base_data, glyphs_data, reuse_map, glyphs_uni_cid)

    new_compressed = _compress_stream(new_font_bytes)
    delta = len(new_compressed) - b_len
    base_data[b_start : b_start + b_len] = new_compressed
    base_data[b_len_pos : b_len_pos + len(str(b_len))] = str(len(new_compressed)).encode()
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", base_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > b_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            base_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", base_data)
        if startxref_m and b_start < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            base_data[p : p + len(str(old_p))] = str(old_p + delta).encode()

    out_path.write_bytes(base_data)
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Перенос глифов Ф,Ч,Ю из ADD в REPLACE")
    ap.add_argument("--base", "-b", type=Path, required=True, help="Base PDF (REPLACE, проходит проверку)")
    ap.add_argument("--glyphs", "-g", type=Path, required=True, help="Glyphs PDF (ADD, визуал ок)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Выходной PDF")
    args = ap.parse_args()
    if not args.base.exists():
        print(f"[ERROR] Не найден: {args.base}", file=sys.stderr)
        return 1
    if not args.glyphs.exists():
        print(f"[ERROR] Не найден: {args.glyphs}", file=sys.stderr)
        return 1
    if transplant(args.base, args.glyphs, args.output):
        print(f"✅ {args.output.name} ({args.output.stat().st_size} bytes)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
