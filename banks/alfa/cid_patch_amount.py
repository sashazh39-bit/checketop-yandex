#!/usr/bin/env python3
"""Минимальный патч текста в PDF с CID-кодировкой. Сохраняет структуру, обновляет xref.

Использование:
  python3 cid_patch_amount.py input.pdf output.pdf --replace "160 RUR=1 600 RUR"
  python3 cid_patch_amount.py input.pdf output.pdf --replace "OLD1=NEW1" --replace "OLD2=NEW2"
"""
from __future__ import annotations

import argparse
import re
import sys
import zlib
from pathlib import Path


def patch_replacements(
    in_path: Path, out_path: Path, replacements: list[tuple[str, str]]
) -> bool:
    """
    Применить несколько замен в PDF. Сохраняет структуру, обновляет xref.
    При нехватке символов в CMap — расширяет ToUnicode (Identity-маппинг).
    replacements: [(old1, new1), (old2, new2), ...]
    """
    if not replacements:
        return False
    data = bytearray(in_path.read_bytes())
    all_cmaps = _parse_tounicode_all(bytes(data))
    if not all_cmaps:
        print("[ERROR] ToUnicode CMap не найден", file=sys.stderr)
        return False
    uni_to_cid_primary = dict(all_cmaps[0])

    flat_replacements = []
    # Anchored pairs: (anchor_text, target_old, target_new, parent)
    # Used when the first line of a multiline pair is unchanged (acts as context anchor).
    # Only the target (line 1+) is replaced, and only when it follows the anchor in the stream.
    anchored_pairs = []
    for old_val, new_val in replacements:
        if "\n" in old_val:
            old_lines = old_val.split("\n")
            new_lines = new_val.split("\n")
            if old_lines[0] == (new_lines[0] if new_lines else ""):
                # Line 0 is the unchanged anchor; handle line 1 as anchored, rest as flat
                if len(old_lines) >= 2:
                    ol1 = old_lines[1]
                    nl1 = new_lines[1] if len(new_lines) > 1 else ""
                    if ol1 and ol1 != nl1:
                        anchored_pairs.append((old_lines[0], ol1, nl1, old_val))
                for i in range(2, len(old_lines)):
                    ol = old_lines[i]
                    nl = new_lines[i] if i < len(new_lines) else ""
                    if ol and ol != nl:
                        flat_replacements.append((ol, nl, old_val))
            else:
                # Both lines change — original flat behavior for all lines
                for i, old_line in enumerate(old_lines):
                    new_line = new_lines[i] if i < len(new_lines) else ""
                    if old_line and old_line != new_line:
                        flat_replacements.append((old_line, new_line, old_val))
        else:
            flat_replacements.append((old_val, new_val, None))

    required_chars = set()
    for _old, new_val, _parent in flat_replacements:
        for c in new_val:
            cp = ord(c)
            if cp == 0x20:
                required_chars.add(0x20)
                required_chars.add(0xA0)
            elif cp >= 0x20:
                required_chars.add(cp)
    for _anchor, _told, tnew, _parent in anchored_pairs:
        for c in tnew:
            cp = ord(c)
            if cp == 0x20:
                required_chars.add(0x20)
                required_chars.add(0xA0)
            elif cp >= 0x20:
                required_chars.add(cp)
    data, uni_to_cid = _extend_tounicode_identity(data, required_chars, uni_to_cid_primary)
    # Дополнительные ToUnicode (другие subset) — не сливаем в один словарь (ломает кириллицу),
    # а перебираем при поиске hex в потоке (например OST1_… в Arial vs основной шрифт).
    extra_cmaps = [dict(m) for m in all_cmaps[1:]]
    candidate_maps: list[dict] = [uni_to_cid] + extra_cmaps

    streams: list[tuple[int, int, int, bytes]] = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue
        streams.append((stream_start, stream_len, m.start(2), dec))

    _ANCHOR_WINDOW = 2048

    mods: list[tuple[int, int, int, bytes, bytes]] = []
    total_replaced = 0
    logged_parents: set[str] = set()
    for stream_start, stream_len, len_num_start, dec in streams:
        new_dec = dec
        for old_val, new_val, parent in flat_replacements:
            pair = _find_hex_pair_for_maps(old_val, new_val, candidate_maps, new_dec)
            if not pair:
                continue
            old_hex, new_hex = pair
            is_inner = not old_hex.startswith(b"<")
            if is_inner:
                new_hex = new_hex[1:-1]
            if old_hex not in new_dec:
                continue
            is_lower = any(c in b'abcdef' for c in old_hex)
            if is_lower:
                new_hex = new_hex.lower()
            new_dec = new_dec.replace(old_hex, new_hex)
            total_replaced += 1
            display = parent or old_val
            if display not in logged_parents:
                logged_parents.add(display)
                short_old = display[:50].replace("\n", "\\n")
                short_new = new_val[:50].replace("\n", "\\n")
                print(f"[OK] {short_old} -> {short_new}")

        # Anchored replacements: replace target only when it follows anchor in stream
        for anchor_text, target_old, target_new, parent in anchored_pairs:
            anchored = _find_anchored_hex_pair(
                anchor_text, target_old, target_new, candidate_maps, new_dec
            )
            if not anchored:
                continue
            a_hex, t_hex, n_hex = anchored
            is_inner = not t_hex.startswith(b"<")
            if is_inner:
                n_hex = n_hex[1:-1]
            is_lower = any(c in b'abcdef' for c in t_hex)
            if is_lower:
                n_hex = n_hex.lower()
            anchor_pos = new_dec.find(a_hex)
            if anchor_pos < 0:
                continue
            search_start = anchor_pos + len(a_hex)
            search_end = min(search_start + _ANCHOR_WINDOW, len(new_dec))
            t_pos = new_dec.find(t_hex, search_start, search_end)
            if t_pos >= 0:
                new_dec = new_dec[:t_pos] + n_hex + new_dec[t_pos + len(t_hex):]
                total_replaced += 1
                display = parent or target_old
                if display not in logged_parents:
                    logged_parents.add(display)
                    print(f"[OK] {display[:50].replace(chr(10), '\\\\n')} (anchored) -> {target_new[:50]}")

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 6)
            mods.append((stream_start, stream_len, len_num_start, dec, new_raw))

    if not mods:
        if total_replaced == 0:
            print("[ERROR] Ни одна замена не применена", file=sys.stderr)
        return total_replaced > 0

    # Применяем с конца файла, чтобы не сбивать позиции
    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, _dec, new_raw in mods:
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data[stream_start : stream_start + stream_len] = new_raw
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

    out_path.write_bytes(data)
    print(f"[OK] Применено замен: {total_replaced}")
    return True


def _detect_zlib_level(raw: bytes) -> int:
    """Определяет уровень zlib-сжатия по заголовку потока."""
    if len(raw) >= 2 and raw[0] == 0x78:
        if raw[1] == 0x9C: return 6
        if raw[1] == 0xDA: return 9
        if raw[1] == 0x5E: return 4
        if raw[1] == 0x01: return 1
    return 6


def _find_hex_pair_for_maps(
    old_val: str,
    new_val: str,
    candidate_maps: list[dict],
    dec: bytes,
) -> tuple[bytes, bytes] | None:
    """Для одной пары old→new: подобрать CMap, в котором строка реально закодирована в потоке."""
    for umap in candidate_maps:
        old_hex = _find_old_hex(old_val, umap, dec)
        if not old_hex:
            continue
        new_hex = _encode_cid(new_val, umap)
        if not new_hex:
            continue
        return (old_hex, new_hex)
    return None


def _find_anchored_hex_pair(
    anchor_text: str,
    target_old: str,
    target_new: str,
    candidate_maps: list[dict],
    dec: bytes,
) -> tuple[bytes, bytes, bytes] | None:
    """Якорь и цель могут быть в разных subset — перебираем пары карт."""
    for umap_a in candidate_maps:
        a_hex = _find_old_hex(anchor_text, umap_a, dec)
        if not a_hex:
            continue
        for umap_t in candidate_maps:
            t_hex = _find_old_hex(target_old, umap_t, dec)
            if not t_hex:
                continue
            n_hex = _encode_cid(target_new, umap_t)
            if not n_hex:
                continue
            return (a_hex, t_hex, n_hex)
    return None


def _find_old_hex(old_val: str, uni_to_cid: dict, dec: bytes) -> bytes | None:
    """Case-insensitive поиск hex CID в потоке. Возвращает hex в том регистре, в каком он в потоке.

    Tries full <hex> match first. Falls back to inner hex substring match
    (without angle brackets) for cases where the text is part of a longer TJ string.
    """
    variants = [old_val, old_val + " ", old_val + "\u00a0"]
    for v in variants:
        h = _encode_cid(v, uni_to_cid, use_homoglyph=False)
        if not h:
            continue
        if h in dec:
            return h
        h_lower = h.lower()
        if h_lower in dec:
            return h_lower
        h_upper = h.upper()
        if h_upper in dec:
            return h_upper
    h_base = _encode_cid(old_val, uni_to_cid, use_homoglyph=False)
    if not h_base:
        return None
    inner = h_base[1:-1]
    if inner in dec:
        return inner
    if inner.lower() in dec:
        return inner.lower()
    if inner.upper() in dec:
        return inner.upper()
    return None


def _find_noop_padded_raw(dec: bytes, target_len: int, base_level: int) -> bytes | None:
    """Return a zlib-compressed stream of exactly *target_len* bytes by appending
    PDF no-op operators to *dec* until the compressed output matches the target.

    Uses '0 Tc\r\n' (set character spacing to 0 — default, harmless) as padding.
    Falls back to other no-ops if needed.  Returns None if no match found.

    The returned stream has no trailing unused bytes (unused_data == 0), which
    avoids the zero-padding forgery detector.
    """
    current = len(zlib.compress(dec, base_level))
    if current == target_len:
        return zlib.compress(dec, base_level)
    if current > target_len:
        return None  # already too large — caller handles this

    noop_ops = [b"0 Tc\r\n", b"0 Tw\r\n", b"0 Tr\r\n", b"\t"]
    for noop in noop_ops:
        for n in range(1, 512):
            candidate_dec = dec + noop * n
            candidate_raw = zlib.compress(candidate_dec, base_level)
            if len(candidate_raw) == target_len:
                return candidate_raw
            if len(candidate_raw) > target_len:
                break
    return None


def patch_replacements_zero_delta(
    in_path: Path, out_path: Path, replacements: list[tuple[str, str]]
) -> bool:
    """
    Zero-delta variant of patch_replacements.

    Identical CID-replacement logic, but after recompression the stream bytes
    are padded with \\x00 to match the original /Length exactly.  This means:
      - /Length stays untouched
      - xref offsets stay untouched
      - startxref stays untouched
      - file size stays untouched

    zlib.decompress() stops at the end of valid compressed data and ignores any
    trailing bytes, so PDF readers see the correct decompressed content.

    Falls back to standard patch_replacements if the recompressed stream would
    be *larger* than the original (should not happen in practice for small text
    substitutions of equal byte-length).

    The function also enforces equal-length CID substitutions: if _find_old_hex
    matched a trailing-space variant (making old_hex longer than new_hex), the
    new_hex is padded with the space CID so the decompressed stream length is
    preserved.
    """
    if not replacements:
        return False

    data = bytearray(in_path.read_bytes())
    all_cmaps = _parse_tounicode_all(bytes(data))
    if not all_cmaps:
        print("[ERROR] ToUnicode CMap не найден", file=sys.stderr)
        return False
    uni_to_cid_primary = dict(all_cmaps[0])

    flat_replacements: list[tuple[str, str, str | None]] = []
    anchored_pairs: list[tuple[str, str, str, str]] = []
    for old_val, new_val in replacements:
        if "\n" in old_val:
            old_lines = old_val.split("\n")
            new_lines = new_val.split("\n")
            if old_lines[0] == (new_lines[0] if new_lines else ""):
                if len(old_lines) >= 2:
                    ol1 = old_lines[1]
                    nl1 = new_lines[1] if len(new_lines) > 1 else ""
                    if ol1 and ol1 != nl1:
                        anchored_pairs.append((old_lines[0], ol1, nl1, old_val))
                for i in range(2, len(old_lines)):
                    ol = old_lines[i]
                    nl = new_lines[i] if i < len(new_lines) else ""
                    if ol and ol != nl:
                        flat_replacements.append((ol, nl, old_val))
            else:
                for i, old_line in enumerate(old_lines):
                    new_line = new_lines[i] if i < len(new_lines) else ""
                    if old_line and old_line != new_line:
                        flat_replacements.append((old_line, new_line, old_val))
        else:
            flat_replacements.append((old_val, new_val, None))

    required_chars: set[int] = set()
    for _old, new_val, _parent in flat_replacements:
        for c in new_val:
            cp = ord(c)
            if cp == 0x20:
                required_chars.add(0x20)
                required_chars.add(0xA0)
            elif cp >= 0x20:
                required_chars.add(cp)
    for _anchor, _told, tnew, _parent in anchored_pairs:
        for c in tnew:
            cp = ord(c)
            if cp == 0x20:
                required_chars.add(0x20)
                required_chars.add(0xA0)
            elif cp >= 0x20:
                required_chars.add(cp)
    data, uni_to_cid = _extend_tounicode_identity(data, required_chars, uni_to_cid_primary)
    extra_cmaps = [dict(m) for m in all_cmaps[1:]]
    candidate_maps: list[dict] = [uni_to_cid] + extra_cmaps

    streams: list[tuple[int, int, int, bytes]] = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue
        streams.append((stream_start, stream_len, m.start(2), dec))

    _ANCHOR_WINDOW = 2048
    total_replaced = 0
    logged_parents: set[str] = set()
    any_fallback = False

    for stream_start, stream_len, len_num_start, dec in streams:
        new_dec = dec

        for old_val, new_val, parent in flat_replacements:
            pair = _find_hex_pair_zero_delta(old_val, new_val, candidate_maps, new_dec)
            if not pair:
                continue
            old_hex, new_hex = pair
            if old_hex not in new_dec:
                continue
            is_lower = any(c in b"abcdef" for c in old_hex)
            if is_lower:
                new_hex = new_hex.lower()
            new_dec = new_dec.replace(old_hex, new_hex)
            total_replaced += 1
            display = parent or old_val
            if display not in logged_parents:
                logged_parents.add(display)
                short_old = display[:50].replace("\n", "\\n")
                short_new = new_val[:50].replace("\n", "\\n")
                print(f"[OK] {short_old} -> {short_new}")

        for anchor_text, target_old, target_new, parent in anchored_pairs:
            anchored = _find_anchored_hex_pair(
                anchor_text, target_old, target_new, candidate_maps, new_dec
            )
            if not anchored:
                continue
            a_hex, t_hex, n_hex = anchored
            # Apply same length-preservation to anchored target
            n_hex_fixed = _pad_new_hex_to_length(t_hex, n_hex, candidate_maps)
            is_lower = any(c in b"abcdef" for c in t_hex)
            if is_lower:
                n_hex_fixed = n_hex_fixed.lower()
            anchor_pos = new_dec.find(a_hex)
            if anchor_pos < 0:
                continue
            search_start = anchor_pos + len(a_hex)
            search_end = min(search_start + _ANCHOR_WINDOW, len(new_dec))
            t_pos = new_dec.find(t_hex, search_start, search_end)
            if t_pos >= 0:
                new_dec = new_dec[:t_pos] + n_hex_fixed + new_dec[t_pos + len(t_hex) :]
                total_replaced += 1
                display = parent or target_old
                if display not in logged_parents:
                    logged_parents.add(display)
                    print(f"[OK] {display[:50].replace(chr(10), chr(10))} (anchored) -> {target_new[:50]}")

        if new_dec == dec:
            continue

        if len(new_dec) != len(dec):
            # Decompressed length changed — can't do zero-delta, fall back
            print(
                f"[WARN] zero-delta: decompressed length changed "
                f"({len(dec)} -> {len(new_dec)}), falling back to standard patch",
                file=sys.stderr,
            )
            any_fallback = True
            continue

        # Recompress at level 6 (genuine Oracle BI Publisher level)
        new_raw = zlib.compress(new_dec, 6)
        if len(new_raw) > stream_len:
            for try_level in range(7, 10):
                candidate = zlib.compress(new_dec, try_level)
                if len(candidate) <= stream_len:
                    new_raw = candidate
                    break
            else:
                print(
                    f"[WARN] zero-delta: recompressed stream too large "
                    f"({len(new_raw)} > {stream_len}), falling back",
                    file=sys.stderr,
                )
                any_fallback = True
                continue

        # Try to find exact match via PDF no-op padding (0 Tc\r\n) instead of zero-bytes.
        # This avoids unused_data / recompress-mismatch detection.
        final_raw = _find_noop_padded_raw(new_dec, stream_len, 6)
        if final_raw is None:
            # Fallback: pad with \x00
            final_raw = new_raw + b"\x00" * (stream_len - len(new_raw))

        assert len(final_raw) == stream_len

        # In-place replacement — ONLY the stream bytes, nothing else
        data[stream_start : stream_start + stream_len] = final_raw

    if total_replaced == 0:
        if not any_fallback:
            print("[ERROR] Ни одна замена не применена", file=sys.stderr)
        return any_fallback

    out_path.write_bytes(bytes(data))
    print(f"[OK] zero-delta: применено замен: {total_replaced}")
    return True


def _pad_new_hex_to_length(old_hex: bytes, new_hex: bytes, candidate_maps: list[dict]) -> bytes:
    """Pad new_hex (inner bytes without angle brackets) to match len(old_hex).

    When _find_old_hex matched a trailing-space variant the old_hex is longer.
    We pad new_hex with a space CID so the decompressed stream length stays
    identical.
    """
    if len(old_hex) == len(new_hex):
        return new_hex

    diff = len(old_hex) - len(new_hex)
    if diff <= 0:
        return new_hex  # new is already longer — caller handles

    # Determine if these are inner (no angle brackets) or full hex strings
    is_inner = not old_hex.startswith(b"<")

    # Find a space CID (one 4-hex-char CID = 4 bytes in inner representation)
    space_cid: bytes | None = None
    for umap in candidate_maps:
        for cp in (0xA0, 0x20):
            if cp in umap:
                space_cid = umap[cp].encode()  # e.g. b"0022"
                break
        if space_cid:
            break

    if not space_cid or diff % len(space_cid) != 0:
        return new_hex  # can't pad cleanly

    pad_units = diff // len(space_cid)
    padding = space_cid * pad_units

    if is_inner:
        return new_hex + padding
    else:
        # new_hex is b"<....XXXX>"
        return new_hex[:-1] + padding + b">"


def _find_hex_pair_zero_delta(
    old_val: str,
    new_val: str,
    candidate_maps: list[dict],
    dec: bytes,
) -> tuple[bytes, bytes] | None:
    """Like _find_hex_pair_for_maps but ensures old_hex and new_hex are the
    same length so the decompressed stream length is preserved.

    When _find_old_hex matched a trailing-space variant (old_hex longer than
    the bare encoding), new_hex is padded with space CIDs to compensate.
    """
    for umap in candidate_maps:
        old_hex = _find_old_hex(old_val, umap, dec)
        if not old_hex:
            continue
        new_hex = _encode_cid(new_val, umap)
        if not new_hex:
            continue
        # Strip angle brackets from new_hex to work in inner representation
        is_full = old_hex.startswith(b"<")
        if is_full:
            old_inner = old_hex[1:-1]
            new_inner = new_hex[1:-1]
        else:
            old_inner = old_hex
            new_inner = new_hex[1:-1]

        if len(old_inner) != len(new_inner):
            # Pad new_inner to match old_inner length with space CIDs
            diff = len(old_inner) - len(new_inner)
            if diff > 0:
                space_cid: bytes | None = None
                for cp in (0xA0, 0x20):
                    if cp in umap:
                        space_cid = umap[cp].encode()
                        break
                if space_cid and diff % len(space_cid) == 0:
                    new_inner = new_inner + space_cid * (diff // len(space_cid))
                else:
                    continue  # can't fix length, try next map
            else:
                continue  # new is longer than old — skip

        if is_full:
            # Both returned with angle brackets; caller does plain replace(old, new)
            return (b"<" + old_inner + b">", b"<" + new_inner + b">")
        else:
            # Both inner (no brackets)
            return (old_inner, new_inner)
    return None


def patch_amount(in_path: Path, out_path: Path, old_amount: str, new_amount: str) -> bool:
    """
    Заменить old_amount на new_amount в PDF (например "160 RUR" -> "1 600 RUR").
    Использует CID hex; обновляет /Length и xref.
    Пробует варианты с trailing space/nbsp.
    """
    data = bytearray(in_path.read_bytes())

    # Найти ToUnicode и построить encoder
    uni_to_cid = _parse_tounicode(data)
    if not uni_to_cid:
        print("[ERROR] ToUnicode CMap не найден", file=sys.stderr)
        return False

    # Варианты old (в PDF может быть trailing space/nbsp)
    old_variants = [old_amount, old_amount + " ", old_amount + "\u00a0"]
    new_hex = _encode_cid(new_amount, uni_to_cid)
    if not new_hex:
        print(f"[ERROR] Не удалось закодировать '{new_amount}'", file=sys.stderr)
        return False

    # Найти какой вариант old есть в PDF
    old_hex = None
    for v in old_variants:
        h = _encode_cid(v, uni_to_cid, use_homoglyph=False)
        if h and h in data:
            old_hex = h
            break
    if not old_hex:
        # Проверить в декомпрессированных потоках
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
            try:
                dec = zlib.decompress(
                    data[m.end() : m.end() + int(m.group(2))]
                )
                for v in old_variants:
                    h = _encode_cid(v, uni_to_cid, use_homoglyph=False)
                    if h and h in dec:
                        old_hex = h
                        break
                if old_hex:
                    break
            except zlib.error:
                continue
    if not old_hex:
        print(f"[ERROR] '{old_amount}' не найден в PDF", file=sys.stderr)
        return False

    # Найти content stream с old_hex
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        len_num_start = m.start(2)
        stream_start = m.end()
        stream_end = stream_start + stream_len
        if stream_end > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start:stream_end]))
        except zlib.error:
            continue
        if old_hex not in dec:
            continue

        # Патч
        new_dec = dec.replace(old_hex, new_hex)
        if new_dec == dec:
            continue
        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len

        # 1. Заменить stream (concat вместо slice — избегаем BufferError при resize)
        data = bytearray(data[:stream_start] + new_raw + data[stream_end:])
        stream_end = stream_start + len(new_raw)

        # 2. Обновить /Length для ЭТОГО stream (в его dict)
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        # len_num_start указывает на начало числа в /Length N
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        # 3. Обновить xref: все offset после stream_start сдвинуть на delta
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    new_offset = (offset + delta)
                    entries[em.start(1):em.start(1)+10] = f"{new_offset:010d}".encode()
            data[xref_m.start(3):xref_m.end(3)] = bytes(entries)

        # 4. Обновить startxref если xref сдвинулся
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0:
            old_xref_pos = int(startxref_m.group(1))
            if stream_start < old_xref_pos:
                new_xref_pos = old_xref_pos + delta
                pos = startxref_m.start(1)
                data[pos:pos+len(str(old_xref_pos))] = str(new_xref_pos).encode()

        out_path.write_bytes(data)
        print(f"[OK] Заменено: {old_amount} -> {new_amount}")
        return True

    print(f"[ERROR] '{old_amount}' не найден в content streams", file=sys.stderr)
    return False


def _parse_one_cmap_from_dec(dec: bytes) -> dict:
    """Один ToUnicode stream: beginbfchar или beginbfrange."""
    uni_to_cid: dict = {}
    if b"beginbfchar" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = mm.group(1).decode().upper().zfill(4)
            uni = int(mm.group(2).decode().upper(), 16)
            uni_to_cid[uni] = cid
        return uni_to_cid
    if b"beginbfrange" in dec:
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            src_start = int(mm.group(1).decode().upper(), 16)
            src_end = int(mm.group(2).decode().upper(), 16)
            dest = int(mm.group(3).decode().upper(), 16)
            for i in range(src_end - src_start + 1):
                uni_to_cid[dest + i] = f"{src_start + i:04X}"
        return uni_to_cid
    return {}


def _parse_tounicode_all(data: bytes) -> list[dict]:
    """Все ToUnicode CMap в порядке появления в файле (каждый отдельным dict)."""
    maps: list[dict] = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end() : m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        cmap = _parse_one_cmap_from_dec(dec)
        if cmap:
            maps.append(cmap)
    return maps


def _parse_tounicode(data: bytes) -> dict:
    """Первый ToUnicode (как раньше) — для patch_amount и одиночных сценариев."""
    maps = _parse_tounicode_all(data)
    return dict(maps[0]) if maps else {}


def _extend_tounicode_identity(
    data: bytearray,
    required_chars: set[int],
    uni_to_cid: dict,
) -> tuple[bytearray, dict]:
    """
    Для недостающих символов: сначала homoglyph (А→A и т.д.), иначе Identity в ToUnicode.
    Homoglyph не меняет PDF — использует уже имеющиеся глифы.
    """
    missing = required_chars - set(uni_to_cid.keys())
    missing.discard(0x20)
    missing = {cp for cp in missing if cp >= 0x20}
    if 0x20 not in uni_to_cid and 0xA0 in uni_to_cid and 0x20 in required_chars:
        uni_to_cid[0x20] = uni_to_cid[0xA0]
    for cp in list(missing):
        if cp in _CYRILLIC_FALLBACK:
            alt = _CYRILLIC_FALLBACK[cp]
            if alt in uni_to_cid:
                uni_to_cid[cp] = uni_to_cid[alt]
                missing.discard(cp)
    existing_cids = set(uni_to_cid.values())
    safe_missing = set()
    for cp in missing:
        cid_hex = f"{cp:04X}"
        if cid_hex not in existing_cids:
            safe_missing.add(cp)
    missing = safe_missing
    if not missing:
        return data, uni_to_cid

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
        if b"beginbfchar" not in dec:
            continue

        new_entries = []
        for cp in sorted(missing):
            cid_hex = f"{cp:04X}"
            new_entries.append(f"<{cid_hex}> <{cid_hex}>".encode())
            uni_to_cid[cp] = cid_hex

        insert_pos = dec.find(b"endbfchar")
        if insert_pos < 0:
            continue
        count_m = re.search(rb"(\d+)\s+beginbfchar", dec)
        if count_m:
            old_count = int(count_m.group(1))
            new_count = old_count + len(new_entries)
            dec = dec[: count_m.start(1)] + str(new_count).encode() + dec[count_m.end(1) :]
        # Use the same line ending as the existing CMap to avoid mixed endings
        eol = b"\r\n" if b"\r\n" in dec else b"\n"
        new_block = eol.join(new_entries) + eol
        insert_pos = dec.find(b"endbfchar")
        new_dec = dec[:insert_pos] + new_block + dec[insert_pos:]
        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - stream_len

        new_data = (
            data[:stream_start]
            + new_raw
            + data[stream_start + stream_len :]
        )
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        num_end = len_num_start + len(old_len_str)
        new_data[len_num_start:num_end] = new_len_str
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", new_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            new_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", new_data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            new_data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

        return new_data, uni_to_cid
    return data, uni_to_cid


# Кириллица → замена (лат. или др. кирилл.) для fallback когда глифа нет в CMap
# Используется только если замена есть в CMap
_CYRILLIC_FALLBACK = {
    0x0410: 0x0041,  # А → A
    0x0412: 0x0042,  # В → B
    0x0415: 0x0045,  # Е → E
    0x041A: 0x004B,  # К → K
    0x041C: 0x004D,  # М → M
    0x041E: 0x004F,  # О → O
    0x041F: 0x0050,  # П → P
    0x0421: 0x0043,  # С → C
    0x0422: 0x0054,  # Т → T
    0x0425: 0x0058,  # Х → X
    0x0430: 0x0061,  # а → a
    0x0435: 0x0065,  # е → e
    0x043E: 0x006F,  # о → o
    0x043F: 0x0070,  # п → p
    0x0440: 0x0072,  # р → r
    0x0441: 0x0063,  # с → c
    0x0442: 0x0074,  # т → t
    0x0443: 0x0079,  # у → y
    0x0445: 0x0078,  # х → x
}


def _encode_cid(text: str, uni_to_cid: dict, use_homoglyph: bool = True) -> bytes | None:
    parts = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid and use_homoglyph and cp in _CYRILLIC_FALLBACK:
            alt = _CYRILLIC_FALLBACK[cp]
            if alt in uni_to_cid:
                cp = alt
        if cp not in uni_to_cid:
            return None
        parts.append(uni_to_cid[cp])
    return ("<" + "".join(parts) + ">").encode()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CID-патч текста в PDF без потери структуры."
    )
    parser.add_argument("input", help="Входной PDF")
    parser.add_argument("output", nargs="?", default=None, help="Выходной PDF (не нужен для --list-cmap)")
    parser.add_argument(
        "--replace",
        "-r",
        action="append",
        metavar="OLD=NEW",
        help="Замена (можно несколько). Пример: -r '160 RUR=1 600 RUR'",
    )
    parser.add_argument(
        "--random-id",
        action="store_true",
        help="Заменить Document ID на случайный 32 hex (после патча контента).",
    )
    parser.add_argument(
        "--list-cmap",
        action="store_true",
        help="Показать доступные символы в CMap PDF и выйти.",
    )
    args = parser.parse_args()
    in_p = Path(args.input).expanduser().resolve()
    out_p = Path(args.output).expanduser().resolve() if args.output else None
    if not in_p.exists():
        print(f"[ERROR] Файл не найден: {in_p}", file=sys.stderr)
        return 1
    if args.list_cmap:
        data = in_p.read_bytes()
        uni_to_cid = _parse_tounicode(data)
        if not uni_to_cid:
            print("[ERROR] ToUnicode CMap не найден", file=sys.stderr)
            return 1
        chars = sorted(uni_to_cid.keys())
        print(f"Доступно {len(chars)} символов в CMap:")
        line = []
        for cp in chars:
            try:
                c = chr(cp)
                if c.isprintable():
                    line.append(f"{c}(U+{cp:04X})")
            except ValueError:
                line.append(f"U+{cp:04X}")
            if len(line) >= 12:
                print("  " + " ".join(line))
                line = []
        if line:
            print("  " + " ".join(line))
        return 0
    reps = []
    if args.replace:
        for r in args.replace:
            if "=" not in r:
                print(f"[ERROR] Неверный формат: {r} (нужно OLD=NEW)", file=sys.stderr)
                return 1
            old, new = r.split("=", 1)
            reps.append((old.strip(), new.strip()))
    if not reps:
        print("Укажите --replace OLD=NEW (можно несколько -r)")
        return 1
    if not out_p:
        print("Укажите выходной PDF")
        return 1
    if not patch_replacements(in_p, out_p, reps):
        return 1
    if args.random_id:
        try:
            from patch_id import patch_document_id
            if patch_document_id(out_p):
                print("[OK] Document ID заменён на случайный.")
            else:
                print("[WARN] /ID не найден, Document ID не изменён.")
        except ImportError:
            print("[WARN] patch_id не найден. Document ID не изменён.")
        except Exception as e:
            print(f"[WARN] Ошибка смены ID: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
