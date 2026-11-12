#!/usr/bin/env python3
"""Поиск CID-слотов в 13.pdf для переиспользования (Ф, Ч, Ю).

Цель: не добавлять новые глифы (меняет font, ToUnicode, /W, CIDToGIDMap),
а перезаписать 3 существующих CID, которые НЕ используются в контенте.

Использование:
  python3 find_reusable_cids.py [path/to/13.pdf]
"""
from __future__ import annotations

import re
import zlib
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGET_13 = [
    Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
]


def _extract_tounicode_cids(pdf_data: bytes) -> dict[int, int]:
    """CID -> Unicode из ToUnicode (beginbfchar + beginbfrange)."""
    from copy_font_cmap import _find_font_and_tounicode, _parse_tounicode_full
    _, tu_data, _ = _find_font_and_tounicode(pdf_data)
    if not tu_data:
        return {}
    uni_to_cid = _parse_tounicode_full(tu_data)
    return {int(cid, 16): uni for uni, cid in uni_to_cid.items()}


def _unescape_pdf(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == 0x5C and i + 1 < len(s):
            out.append(s[i + 1]); i += 2; continue
        out.append(s[i]); i += 1
    return bytes(out)


def _extract_cids_from_content(pdf_data: bytes) -> set[int]:
    """Все CIDs, используемые в content streams (TJ)."""
    used = set()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"TJ" not in dec:
            continue
        for part, _kern in re.findall(rb"\((.*?)\)|(-?\d+(?:\.\d+)?)", dec):
            if part:
                vals = list(_unescape_pdf(part))
                for j in range(0, len(vals) - 1, 2):
                    cid = (vals[j] << 8) + vals[j + 1]
                    used.add(cid)
    return used


def _extract_fio_only_cids(pdf_data: bytes, fio_y: tuple[float, ...] = (327.1, 227.25, 203.25), tol: float = 2.0) -> set[int]:
    """CIDs, которые встречаются ТОЛЬКО в позициях ФИО (Y ≈ fio_y), не в статическом контенте.

    Статический контент — все TJ-блоки вне FIO-строк.
    Это позволяет определить, какие CIDs безопасно переиспользовать.
    """
    fio_cids: set[int] = set()
    static_cids: set[int] = set()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue
        tm_pat = rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm"
        for tm_m in re.finditer(tm_pat, dec):
            y = float(tm_m.group(2))
            pos = tm_m.end()
            tj_m = re.search(rb"\[([^\]]+)\]\s*TJ", dec[pos:pos + 600])
            if not tj_m:
                continue
            is_fio = any(abs(y - fy) < tol for fy in fio_y)
            cids_here: set[int] = set()
            for part, _kern in re.findall(rb"\((.*?)\)|(-?\d+(?:\.\d+)?)", tj_m.group(1)):
                if part:
                    vals = list(_unescape_pdf(part))
                    for j in range(0, len(vals) - 1, 2):
                        cids_here.add((vals[j] << 8) | vals[j + 1])
            if is_fio:
                fio_cids |= cids_here
            else:
                static_cids |= cids_here
    return fio_cids - static_cids


# Символы, которые НЕ нужны в «Филипп Юсаев Ч.» — их CID можно переиспользовать.
# ВАЖНО: Б и В исключены — они используются в «Банк ВТБ(ПАО)» в футере.
_CHARS_WE_DONT_NEED = frozenset("МГДЖЗЙКНПРСТФХЦЧШЩЪЫЬЭЯмгджзйкнпрстфхцчшщъыьэя*F")
# Ф, Ч, Ю — нужны, их CIDs должны быть в итоге
_NEED_UNI = {0x0424: "Ф", 0x0427: "Ч", 0x042E: "Ю"}

# Unicode символов, которые ИСПОЛЬЗУЮТСЯ в статическом контенте чека — их CID нельзя красть.
# Иначе «Дата»→«Юата», «*9426»→«Ф9426», «50B10»→«50Ч10», «+7 (995)»→«+7 ,995�», «ВТБ»→«ВЧБ».
_STEAL_BLACKLIST_UNI = frozenset({
    0x002A,  # * (asterisk) — счёт
    0x0028, 0x0029,  # ( ) — телефон
    0x002E,  # . (period) — дата, сумма
    0x0414,  # Д — «Дата»
    0x0411, 0x0412, 0x0422,  # Б, В, Т — «Банк ВТБ(ПАО)» (кража → ВЧБ)
    0x0041, 0x0042, 0x0043, 0x0044, 0x0045, 0x0046,  # A-F — ID операции
    0x0410, 0x0421, 0x0415, 0x0424,  # А,С,Е,Ф — homoglyph для ID
})

# Символы, нужные для «Филипп Юсаев Ч.» — нельзя красть (п/П общий CID 022B; при краже п→Ч).
_FIO_NEEDED_UNI = frozenset({
    0x041F, 0x043F,  # П, п — «Филипп»
    0x042E, 0x044E,  # Ю, ю — «Юсаев»
})

# Символы из старого ФИО (Роман Андреевич В / Александр Евгеньевич Ж) — можно красть, т.к. заменяем.
# НО исключаем п, П, ю, Ю — они нужны в целевом ФИО.
_STALE_FIO_STEALABLE = frozenset("РмндрчЖ") - frozenset("пПюЮ")


def _get_cidtogid_map(pdf_data: bytes) -> dict[int, int] | None:
    """CID->GID из CIDToGIDMap stream."""
    ctg_ref = re.search(rb"/CIDToGIDMap\s+(\d+)\s+0\s+R", pdf_data)
    if not ctg_ref:
        return None
    oid = int(ctg_ref.group(1))
    pat = rf"{oid}\s+0\s+obj".encode()
    m = re.search(pat, pdf_data)
    if not m:
        return None
    chunk = pdf_data[m.start() : m.start() + 300]
    stream_m = re.search(rb">>\s*stream\r?\n", chunk)
    len_m = re.search(rb"/Length\s+(\d+)", chunk)
    if not stream_m or not len_m:
        return None
    stream_len = int(len_m.group(1))
    stream_start = m.start() + stream_m.end()
    if stream_start + stream_len > len(pdf_data):
        return None
    raw = pdf_data[stream_start : stream_start + stream_len]
    try:
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        elif raw.startswith(b"\n"):
            raw = raw[1:]
        dec = zlib.decompress(raw)
    except zlib.error:
        return None
    out: dict[int, int] = {}
    for i in range(0, len(dec), 2):
        if i + 2 <= len(dec):
            out[i // 2] = int.from_bytes(dec[i : i + 2], "big")
    return out


def find_reusable(
    base_path: Path,
    base_ctg: dict[int, int] | None = None,
    target_unis: list[int] | None = None,
    need_uni: frozenset[int] | None = None,
) -> tuple[list[tuple[int, int]], dict[int, int]]:
    """
    (unused_cids, reuse_map).
    unused_cids — [(cid, uni)] не в контенте.
    reuse_map   — {target_uni: cid} для target_unis (по умолч. Ф/Ч/Ю).

    target_unis — список Unicode кодов букв, для которых нужен safe-slot.
    need_uni    — буквы нового ФИО, которые должны быть доступны (не красть их CID).
                  По умолчанию = frozenset(target_unis) если задан target_unis.
    """
    if target_unis is None:
        target_unis = [0x0424, 0x0427, 0x042E]  # Ф, Ч, Ю — обратная совместимость
    if need_uni is None:
        need_uni = frozenset(target_unis)

    data = base_path.read_bytes()
    cid_to_uni = _extract_tounicode_cids(data)
    used_cids = _extract_cids_from_content(data)
    ctg = base_ctg if base_ctg is not None else _get_cidtogid_map(data)

    # GID collision: при REPLACE мы перезаписываем глиф по GID. Если * и Ф используют один GID,
    # то * в номере счёта покажет Ф. Нужно исключить слоты, чей GID совпадает с GID blacklist-символов.
    blacklist_gids: set[int] = set()
    if ctg:
        for cid, uni in cid_to_uni.items():
            if uni in _STEAL_BLACKLIST_UNI:
                g = ctg.get(cid)
                if g is not None:
                    blacklist_gids.add(g)

    def _gid_safe(cid: int) -> bool:
        """Слот безопасен: его GID не используется blacklist-символами (*, (, ), и т.д.)."""
        if not ctg or not blacklist_gids:
            return True
        g = ctg.get(cid)
        return g is None or g not in blacklist_gids

    unused = [(c, u) for c, u in sorted(cid_to_uni.items())
              if c not in used_cids and _gid_safe(c)]

    # FIO-only CIDs: встречаются ТОЛЬКО в позициях ФИО (Y≈327/227/203), не в статике.
    # Это надёжнее, чем unicode-фильтр, т.к. не зависит от ToUnicode (PUA или real).
    fio_only_cids = _extract_fio_only_cids(data)

    if fio_only_cids:
        # Используем content-stream анализ: только CIDs из FIO-only позиций.
        # _gid_safe не применяем — FIO-only CIDs безопасны по определению,
        # т.к. заменяем всё ФИО целиком, и их GID не влияет на статику.
        stealable = [(c, u) for c, u in sorted(cid_to_uni.items())
                     if c in fio_only_cids]
    else:
        # Fallback: стандартный юникод-фильтр (для PDF с реальным ToUnicode).
        stealable = [(c, u) for c, u in sorted(cid_to_uni.items())
                     if c in used_cids and 0 < u < 0xE000
                     and u not in _STEAL_BLACKLIST_UNI
                     and u not in need_uni
                     and (chr(u) in _CHARS_WE_DONT_NEED or chr(u) in _STALE_FIO_STEALABLE)
                     and _gid_safe(c)]

    reuse_map: dict[int, int] = {}
    for target_uni in target_unis:
        if unused:
            cid, _ = unused.pop(0)
            reuse_map[target_uni] = cid
        elif stealable:
            cid, _ = stealable.pop(0)
            reuse_map[target_uni] = cid
        else:
            break
    return unused, reuse_map


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else []
    if not paths:
        for p in TARGET_13:
            if p.exists():
                paths = [p]
                break
    if not paths:
        print("[ERROR] Укажите 13.pdf: python3 find_reusable_cids.py path/to/13.pdf", file=sys.stderr)
        return 1
    pdf_path = paths[0]
    if not pdf_path.exists():
        print(f"[ERROR] Не найден: {pdf_path}", file=sys.stderr)
        return 1
    unused, reuse_map = find_reusable(pdf_path)
    print(f"13.pdf: {pdf_path.name}")
    print(f"CID в ToUnicode, НЕ используемые в контенте: {len(unused)}")
    for cid, uni in unused[:30]:
        ch = chr(uni) if 0x20 <= uni < 0xE000 else f"U+{uni:04X}"
        print(f"  CID 0x{cid:04X} -> U+{uni:04X} ({ch})")
    if len(unused) > 30:
        print(f"  ... и ещё {len(unused) - 30}")
    if len(reuse_map) >= 3:
        print("\n✅ Выбрано 3 слота для REPLACE (структура без изменений):")
        uni_map = {0x0424: "Ф", 0x0427: "Ч", 0x042E: "Ю"}
        for target_uni in [0x0424, 0x0427, 0x042E]:
            cid = reuse_map.get(target_uni)
            if cid is not None:
                print(f"  CID 0x{cid:04X} -> {uni_map[target_uni]} (U+{target_uni:04X})")
    else:
        print(f"\n[WARN] Мало слотов ({len(reuse_map)} < 3). REPLACE недоступен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
