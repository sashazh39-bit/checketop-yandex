#!/usr/bin/env python3
"""Clean Room PDF Generation using PyMuPDF only.

This script rebuilds a PDF by drawing every visual element (images + text spans)
on a brand-new blank document. It does not copy pages as binary objects.

Usage:
    python3 rebuild_pdf.py input.pdf output.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

import fitz  # PyMuPDF


# --- AM CID Encoding (Oracle ToUnicode CMap) ---
# Для режима --native-match: кодируем текст в CIDs шрифта AM (формат <0001><0002>...).
# ToUnicode задаёт CID → Unicode; обратный маппинг Unicode → CID для записи.


def _parse_tounicode_from_pdf(pdf_bytes: bytes) -> Dict[int, str]:
    """
    Извлечь ToUnicode CMap из PDF (beginbfchar).
    Возвращает: Unicode codepoint → CID hex (4 символа, напр. '000B').
    """
    uni_to_cid: Dict[int, str] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_bytes):
            continue
        stream_data = pdf_bytes[stream_start : stream_start + stream_len]
        try:
            dec = zlib.decompress(stream_data)
        except zlib.error:
            continue
        if b"beginbfchar" not in dec:
            continue
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid_hex = mm.group(1).decode("ascii").upper().zfill(4)
            uni_hex = mm.group(2).decode("ascii").upper().zfill(4)
            uni_cp = int(uni_hex, 16)
            uni_to_cid[uni_cp] = cid_hex
        return uni_to_cid
    return uni_to_cid


def _encode_text_to_cid_hex(text: str, uni_to_cid: Dict[int, str]) -> Optional[bytes]:
    """
    Кодировать текст в hex-последовательность CIDs для PDF content stream.
    Пробел U+0020 → U+00A0 (nbsp) если 0020 отсутствует в CMap.
    Возвращает b'<000C000B000A>' или None если символ не в CMap.
    """
    parts: List[str] = []
    for ch in text:
        cp = ord(ch)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        parts.append(uni_to_cid[cp])
    return ("<" + "".join(parts) + ">").encode("ascii")


def _apply_cid_replacements_to_content(
    content_dec: bytes,
    replacements: Dict[str, list[str]],
    uni_to_cid: Dict[int, str],
) -> Tuple[bytes, int]:
    """
    Применить замены в декомпрессированном content stream.
    replacements: {old: [new]} — как в основном пайплайне.
    Возвращает (новый_content, количество_применённых_замен).
    Для сумм типа "10 RUR"=... fallback: "500 RUR" (часто в AM).
    """
    applied = 0
    for old, news in list(replacements.items()):
        if not news:
            continue
        new_val = news[-1]
        # Пробуем exact match
        old_hex = _encode_text_to_cid_hex(old, uni_to_cid)
        if old_hex and old_hex in content_dec:
            new_hex = _encode_text_to_cid_hex(new_val, uni_to_cid)
            if new_hex:
                content_dec = content_dec.replace(old_hex, new_hex)
                applied += 1
            continue
        # Fallback для сумм: AM часто имеет "500 RUR" или "500 RUR " (с trailing nbsp)
        if "RUR" in old.upper() and re.search(r"\d", old):
            for fallback_old in ["500 RUR ", "500 RUR\u00a0", "500 RUR"]:
                fhex = _encode_text_to_cid_hex(fallback_old, uni_to_cid)
                if fhex and fhex in content_dec:
                    new_hex = _encode_text_to_cid_hex(new_val, uni_to_cid)
                    if new_hex:
                        content_dec = content_dec.replace(fhex, new_hex)
                        applied += 1
                    break
    return content_dec, applied


def parse_date_to_pdf_format(date_str: str) -> Optional[str]:
    """
    Преобразовать строку даты в формат PDF (D:YYYYMMDDHHmmSS+HH'mm').
    Поддерживает: "2026-02-24 10:40", "2026-02-24 14:30:15", "24.02.2026 10:40"
    """
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(date_str, fmt)
            return f"D:{dt.strftime('%Y%m%d%H%M%S')}Z00'00'"
        except ValueError:
            continue
    return None

# Keep defaults empty: all replacements should come from CLI/JSON.
REPLACEMENTS: Dict[str, list[str]] = {}
ENABLE_LINE_LEVEL_REPLACEMENT = False

# Prefer Tahoma everywhere (as requested).
TAHOMA_CANDIDATES = [
    "/Library/Fonts/Tahoma.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
]

# Fallback if Tahoma is unavailable on host system.
ARIAL_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]

SUSPICIOUS_OBJECT_TOKENS = [
    b"/JavaScript",
    b"/JS",
    b"/OpenAction",
    b"/AA",
    b"/Launch",
    b"/EmbeddedFile",
    b"/Filespec",
    b"/SubmitForm",
    b"/RichMedia",
    b"/OCG",
    b"/OCProperties",
]

def _apply_native_structure_patch(path: Path, ios_match_input: Optional[Path] = None) -> bool:
    """
    Пост-обработка для варианта C: структура как input ru.pdf / 20251021220717.
    - fzImg0/1/2 -> Im1/2/3
    - forced_tahoma, unicode_fallback -> G1
    - Producer: MuPDF -> iOS Quartz
    - Нейтрализовать /Metadata в Catalog (пробелы той же длины)
    - Корректировать xref/startxref с учётом позиций замен (каждый объект сдвигается по-своему)
    Если ios_match_input задан — BaseFont берётся из эталона (AAAAAB+font...).
    """
    data = bytearray(path.read_bytes())
    modified = False

    # Собираем все замены и их позиции в исходном файле
    replacements: list[tuple[int, bytes, bytes]] = []

    # Content streams: fzImg0/1/2 в сжатом контенте -> Im1/2/3 (иначе картинки не отображаются)
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        dict_before = m.group(1)
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_len <= 0 or stream_start + stream_len > len(data):
            continue
        if b"FlateDecode" not in dict_before and b"FlateDecode" not in m.group(3):
            continue
        stream_data = bytes(data[stream_start : stream_start + stream_len])
        try:
            decoded = zlib.decompress(stream_data)
        except zlib.error:
            continue
        if b"fzImg0" not in decoded and b"fzImg1" not in decoded and b"fzImg2" not in decoded:
            continue
        patched = decoded.replace(b"fzImg0", b"Im1").replace(b"fzImg1", b"Im2").replace(b"fzImg2", b"Im3")
        if patched == decoded:
            continue
        try:
            new_compressed = zlib.compress(patched, 9)
        except Exception:
            continue
        replacements.append((stream_start, stream_data, new_compressed))
        modified = True
        # Обновить /Length в dict (смещение объектов изменится — учтётся в shift_at)
        len_start = m.start(2)
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_compressed)).encode()
        replacements.append((len_start, old_len_str, new_len_str))

    # BaseFont и FontDescriptor FontName из эталона (--ios-match / --match-pdf)
    if ios_match_input:
        input_basefont = get_first_font_basefont_from_pdf(ios_match_input)
        if input_basefont and b"+" in input_basefont:
            # BaseFont: /BaseFont/XXXX+...
            for m in re.finditer(rb"/BaseFont/[A-Za-z0-9+#]+\+[^/\s\[]+", data):
                old_val = m.group(0)[10:]  # после /BaseFont/
                if old_val != input_basefont:
                    replacements.append((m.start() + 10, old_val, input_basefont))
                    modified = True
            # FontDescriptor /FontName/XXXX+... (HOHSFA+Tahoma#20Regular -> AAAAAB+font...)
            for m in re.finditer(rb"/FontName/[A-Za-z0-9+#]+\+[^/\s\[]+", data):
                old_val = m.group(0)[10:]  # после /FontName/
                if old_val != input_basefont:
                    replacements.append((m.start() + 10, old_val, input_basefont))
                    modified = True
    for old, new in [(b"fzImg0", b"Im1"), (b"fzImg1", b"Im2"), (b"fzImg2", b"Im3")]:
        pos = 0
        while True:
            i = data.find(old, pos)
            if i < 0:
                break
            replacements.append((i, old, new))
            pos = i + 1
    for old in [b"forced_tahoma", b"unicode_fallback", b"fallback_arial"]:
        pos = 0
        while True:
            i = data.find(old, pos)
            if i < 0:
                break
            replacements.append((i, old, b"G1"))
            pos = i + 1

    # Catalog: убрать встроенный /Info (как в input ru — только /Pages, без Producer в Catalog)
    for pat in [rb"/Info\s*<<\s*/Producer\(MuPDF 1\.27\.1\)\s*>>\s*", rb"/Info\s*<<\s*/Producer\(iOS Quartz\s+\)\s*>>\s*"]:
        for m in re.finditer(pat, data):
            replacements.append((m.start(), m.group(0), b" "))
            modified = True
            break  # один Catalog

    # Producer: заменить MuPDF/другое на Producer из эталона (--ios-match / --match-pdf)
    match_producer = get_producer_from_pdf(ios_match_input) if ios_match_input else None
    if match_producer:
        producer_m = re.search(rb"Producer\([^)]*\)", data)
        if producer_m:
            replacement = b"Producer(" + match_producer + b")"
            replacements.append((producer_m.start(), producer_m.group(0), replacement))
            modified = True
    else:
        producer_m = re.search(rb"Producer\(MuPDF 1\.27\.1\)", data)
        if producer_m:
            replacements.append((producer_m.start(), producer_m.group(0), b"Producer(iOS Quartz   )"))
            modified = True

    # Сортируем по позиции (с конца, чтобы при замене не сбивались индексы)
    replacements.sort(key=lambda r: r[0], reverse=True)

    # Для каждого исходного offset X: смещение = сумма (len(new)-len(old)) по всем заменам с pos < X
    def shift_at(offset: int) -> int:
        return sum(len(n) - len(o) for p, o, n in replacements if p < offset)

    # Выполняем замены с конца
    for pos, old, new in replacements:
        data[pos : pos + len(old)] = new
        modified = True

    data_bytes = bytes(data)

    # Нейтрализовать /Metadata N M R (пробелы = та же длина)
    metadata_ref = re.search(rb"/Metadata\s+\d+\s+\d+\s+R", data_bytes)
    if metadata_ref:
        pad = b" " * len(metadata_ref.group())
        data = bytearray(data_bytes[: metadata_ref.start()] + pad + data_bytes[metadata_ref.end() :])
        modified = True
        data_bytes = bytes(data)

    # /Type/Metadata в объекте Metadata -> /Type/Obsolete (8 символов, count /Metadata = 0)
    if re.search(rb"/Type/Metadata", data_bytes):
        data_bytes = data_bytes.replace(b"/Type/Metadata", b"/Type/Obsolete")
        modified = True

    # Document ID из эталона (--ios-match) — заменить только hex-значения, сохраняя длину
    if ios_match_input and ios_match_input.exists():
        try:
            input_bytes = ios_match_input.read_bytes()
            id_m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", input_bytes)
            our_m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", data_bytes)
            if id_m and our_m and len(id_m.group(1)) == len(our_m.group(1)) and len(id_m.group(2)) == len(our_m.group(2)):
                # Заменить hex внутри скобок (длина не меняется)
                old1, old2 = our_m.group(1), our_m.group(2)
                new1, new2 = id_m.group(1), id_m.group(2)
                data_bytes = data_bytes.replace(b"<" + old1 + b">", b"<" + new1 + b">", 1)
                data_bytes = data_bytes.replace(b"<" + old2 + b">", b"<" + new2 + b">", 1)
                modified = True
        except Exception:
            pass

    if modified:
        # Корректировать xref: для каждого объекта new_offset = old_offset + shift_at(old_offset)
        def repl_offset(m: re.Match) -> bytes:
            old_off = int(m.group(1))
            delta = shift_at(old_off)
            new_off = old_off + delta
            return str(new_off).zfill(10).encode("ascii") + m.group(2)

        data_bytes = re.sub(rb"(\d{10})(\s+\d{5}\s+n)", repl_offset, data_bytes)

        # startxref: указывает на xref, все замены до него — total_shift
        total_shift = shift_at(len(data_bytes))  # все замены до конца файла
        xref_match = re.search(rb"startxref\r?\n(\d+)\r?\n", data_bytes)
        if xref_match and total_shift != 0:
            old_offset = int(xref_match.group(1))
            new_offset = old_offset + total_shift
            if new_offset >= 0:
                data_bytes = (
                    data_bytes[: xref_match.start(1)]
                    + str(new_offset).encode("ascii")
                    + data_bytes[xref_match.end(1) :]
                )
        path.write_bytes(data_bytes)
    return modified


def _apply_match_metadata_patch(
    path: Path,
    match_pdf: Path,
    use_random_document_id: bool = False,
    producer_from_input: Optional[Path] = None,
) -> bool:
    """
    Лёгкий патч для --match-pdf-metadata-only: структура как эталон AM.
    Producer, Document ID, BaseFont, Catalog без /Info и /Metadata.
    use_random_document_id: заменить /ID на случайный 32 hex (не трогает xref).
    producer_from_input: брать Producer из input (для распознавания Альфа-Банка — iOS Quartz).
    """
    data = bytearray(path.read_bytes())
    modified = False

    # 0. Catalog: убрать /Info<<...>> и /Metadata (как в AM)
    replacements: list[tuple[int, bytes, bytes]] = []
    cat_m = re.search(rb"/Info\s*<<.*?>>\s*/Metadata\s+\d+\s+\d+\s+R", data, re.DOTALL)
    if cat_m:
        replacements.append((cat_m.start(), cat_m.group(0), b" "))
        modified = True
    # 0a. Catalog с встроенным /Info (без /Metadata) — убрать /Info полностью
    cat_info_only = re.search(rb"<</Type/Catalog/Pages\s+(\d+)\s+0\s+R/Info\s*<<[^>]*>>\s*>>", data)
    if cat_info_only:
        pages_ref = cat_info_only.group(1)
        old_cat = cat_info_only.group(0)
        new_cat = b"<<\r\n/Type /Catalog\r\n/Pages " + pages_ref + b" 0 R\r\n>>\r\n"
        replacements.append((cat_info_only.start(), old_cat, new_cat))
        modified = True

    # 1. Producer(MuPDF) -> Producer из эталона или из input (--keep-input-producer для Альфа-Банка)
    producer_source = producer_from_input if producer_from_input else match_pdf
    match_producer = get_producer_from_pdf(producer_source)
    cat_range = (cat_m.start(), cat_m.end()) if cat_m else (0, 0)
    if match_producer:
        for m in re.finditer(rb"Producer\(MuPDF[^)]*\)", data):
            if not (cat_range[0] <= m.start() < cat_range[1]):
                replacements.append((m.start(), m.group(0), b"Producer(" + match_producer + b")"))
                modified = True
                break

    # 2. Document ID: из эталона или случайный 32 hex (длина та же — xref не ломается)
    try:
        our_m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", data)
        if our_m and len(our_m.group(1)) == 32 and len(our_m.group(2)) == 32:
            old1, old2 = our_m.group(1), our_m.group(2)
            if use_random_document_id:
                new_hex = secrets.token_hex(16).encode("ascii")  # 32 символа
                new1 = new2 = new_hex
            else:
                input_bytes = match_pdf.read_bytes()
                id_m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", input_bytes)
                if id_m and len(id_m.group(1)) == 32 and len(id_m.group(2)) == 32:
                    new1, new2 = id_m.group(1), id_m.group(2)
                else:
                    new1 = new2 = None
            if new1 is not None:
                replacements.append((data.find(b"<" + old1 + b">"), b"<" + old1 + b">", b"<" + new1 + b">"))
                replacements.append((data.rfind(b"<" + old2 + b">"), b"<" + old2 + b">", b"<" + new2 + b">"))
                modified = True
    except Exception:
        pass

    # 3. BaseFont/FontName: префикс из эталона (FKWGJK->KKGMOS при той же длине)
    match_basefont = get_first_font_basefont_from_pdf(match_pdf)
    if match_basefont and b"+" in match_basefont:
        match_prefix = match_basefont.split(b"+")[0]  # KKGMOS
        for m in re.finditer(rb"/(BaseFont|FontName)/([A-Za-z0-9]+)\+", data):
            old_prefix = m.group(2)
            if old_prefix != match_prefix and len(old_prefix) == len(match_prefix):
                replacements.append((m.start(2), old_prefix, match_prefix))
                modified = True
    # Tahoma#20Regular -> Tahoma (use match_prefix so we don't overwrite BaseFont fix)
    if match_basefont and b"+" in match_basefont:
        match_prefix = match_basefont.split(b"+")[0]
        for pattern in [rb"/BaseFont/[A-Za-z0-9+#]+\+Tahoma#20Regular", rb"/FontName/[A-Za-z0-9+#]+\+Tahoma#20Regular"]:
            for m in re.finditer(pattern, data):
                old_val = m.group(0)
                # /BaseFont/ or /FontName/ is 10 chars; use match_prefix (KKGMOS) not old
                new_val = old_val[:10] + match_prefix + b"+Tahoma"
                replacements.append((m.start(), old_val, new_val))
                modified = True

    # 4. Catalog format: match AM style <<\r\n/Type /Catalog\r\n/Pages X 0 R\r\n>>\r\n
    cat_fmt = re.search(rb"<</Type/Catalog/Pages\s+(\d+)\s+0\s+R\s*>>", data)
    if cat_fmt:
        pages_ref = cat_fmt.group(1)
        old_cat = cat_fmt.group(0)
        new_cat = b"<<\r\n/Type /Catalog\r\n/Pages " + pages_ref + b" 0 R\r\n>>\r\n"
        replacements.append((cat_fmt.start(), old_cat, new_cat))
        modified = True

    # 4b. Soft hyphen (U+00AD) -> hyphen (U+002D)
    # 4b1. ToUnicode CMap: <00AD> -> <002D> чтобы извлечение давало "-" вместо "\xad"
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        dict_before = m.group(1)
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_len <= 0 or stream_start + stream_len > len(data):
            continue
        stream_data = bytes(data[stream_start : stream_start + stream_len])
        try:
            decoded = zlib.decompress(stream_data)
        except zlib.error:
            continue
        # ToUnicode CMap: beginbfchar/beginbfrange
        if b"beginbfchar" not in decoded and b"beginbfrange" not in decoded:
            # 4b2. Page content: hex <AD> или <00AD> -> <2D>/<002D>
            if b"BT" in decoded and b"ET" in decoded:
                hex_with_ad = [h for h in re.findall(rb"<([0-9A-Fa-f]+)>", decoded) if b"AD" in h.upper()]
                if hex_with_ad:

                    def fix_content_hex(match: re.Match) -> bytes:
                        h = match.group(1)
                        if h.upper() == b"AD":
                            return b"<2D>"
                        if h.upper() == b"00AD":
                            return b"<002D>"
                        if b"AD" in h.upper() and len(h) <= 4:
                            return b"<" + h.replace(b"AD", b"2D").replace(b"ad", b"2d") + b">"
                        return match.group(0)

                    new_dec = re.sub(rb"<([0-9A-Fa-f]+)>", fix_content_hex, decoded)
                    if new_dec != decoded:
                        try:
                            new_compressed = zlib.compress(new_dec, 9)
                            replacements.append((stream_start, stream_data, new_compressed))
                            replacements.append(
                                (m.start(2), str(stream_len).encode(), str(len(new_compressed)).encode())
                            )
                            modified = True
                        except Exception:
                            pass
            continue
        # ToUnicode: замена <00AD> -> <002D>
        new_dec = decoded.replace(b"<00AD>", b"<002D>").replace(b"<00ad>", b"<002d>")
        if new_dec == decoded:
            continue
        try:
            new_compressed = zlib.compress(new_dec, 9)
            replacements.append((stream_start, stream_data, new_compressed))
            replacements.append((m.start(2), str(stream_len).encode(), str(len(new_compressed)).encode()))
            modified = True
        except Exception:
            pass

    # 4c. ICCBased → DeviceRGB (как в AM — без ICC profile)
    icc_cs_ref = re.search(rb"/ColorSpace\s+(\d+)\s+0\s+R", data)
    if icc_cs_ref:
        for m in re.finditer(rb"/ColorSpace\s+" + icc_cs_ref.group(1) + rb"\s+0\s+R", data):
            replacements.append((m.start(), m.group(0), b"/ColorSpace /DeviceRGB"))
            modified = True

    # 5. Info object: compact format like AM <<\r\n/Type /Info\r\n/Producer (...)\r\n>>\r\n
    if match_producer:
        info_m = re.search(
            rb"<</Title null/Author null/Subject null/Keywords null/Creator null/Producer\([^)]+\)/CreationDate null/ModDate null/Trapped null>>",
            data,
        )
        if info_m:
            old_info_dict = info_m.group(0)
            new_info_dict = b"<<\r\n/Type /Info\r\n/Producer (" + match_producer + b")\r\n>>\r\n"
            replacements.append((info_m.start(), old_info_dict, new_info_dict))
            modified = True

    if not modified:
        return False

    replacements.sort(key=lambda r: r[0], reverse=True)

    def shift_at(offset: int) -> int:
        return sum(len(n) - len(o) for p, o, n in replacements if p < offset)

    for pos, old, new in replacements:
        # bytearray slice assignment: если len(new)!=len(old), массив изменит длину
        data[pos : pos + len(old)] = new

    data_bytes = bytes(data)

    # xref и startxref
    def repl_offset(m: re.Match) -> bytes:
        old_off = int(m.group(1))
        delta = shift_at(old_off)
        new_off = old_off + delta
        return str(new_off).zfill(10).encode("ascii") + m.group(2)

    data_bytes = re.sub(rb"(\d{10})(\s+\d{5}\s+n)", repl_offset, data_bytes)

    total_shift = shift_at(len(data_bytes))
    xref_match = re.search(rb"startxref\r?\n(\d+)\r?\n", data_bytes)
    if xref_match and total_shift != 0:
        old_offset = int(xref_match.group(1))
        new_offset = old_offset + total_shift
        if new_offset >= 0:
            data_bytes = (
                data_bytes[: xref_match.start(1)]
                + str(new_offset).encode("ascii")
                + data_bytes[xref_match.end(1) :]
            )

    # 6. Structure renumber: Pages 3, Info 2 (как в эталоне AM)
    match_bytes = match_pdf.read_bytes()
    match_pages = re.search(rb"/Pages\s+(\d+)\s+0\s+R", match_bytes)
    match_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", match_bytes)
    our_pages = re.search(rb"/Pages\s+(\d+)\s+0\s+R", data_bytes)
    our_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", data_bytes)
    if (
        match_pages and match_info and our_pages and our_info
        and match_pages.group(1) == b"3"
        and match_info.group(1) == b"2"
        and our_pages.group(1) == b"2"
        and our_info.group(1) == b"15"
    ):
        # Mapping: 15->2, 2->3, 3->8, 8->15 (Info->2, Pages->3, Page->8, W->15)
        RENUM = {15: 2, 2: 3, 3: 8, 8: 15}

        def _repl(m: re.Match) -> bytes:
            n = int(m.group(1))
            suffix = m.group(2)
            if n in RENUM:
                return str(RENUM[n]).encode() + b" 0 " + suffix
            return m.group(0)

        data_bytes = re.sub(rb"(\d+)\s+0\s+(R|obj)", _repl, data_bytes)

        # Rebuild xref: reorder entries by new object number
        xref_match = re.search(rb"(xref\r?\n\d+\s+\d+\r?\n)((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data_bytes)
        if xref_match:
            header, entries_blob = xref_match.group(1), xref_match.group(2)
            entries = re.findall(rb"(\d{10})\s+(\d{5})\s+([nf])\s*", entries_blob)
            if len(entries) >= 17:  # 0..16
                old_offs: dict[int, tuple[bytes, bytes, bytes]] = {}
                for i, (off, gen, t) in enumerate(entries):
                    old_offs[i] = (off, gen, t)
                new_order = []
                for new_id in range(17):
                    old_id = {2: 15, 3: 2, 8: 3, 15: 8}.get(new_id, new_id)
                    off, gen, t = old_offs[old_id]
                    new_order.append(off + b" " + gen + b" " + t + b"\n")
                new_xref = header + b"".join(new_order)
                data_bytes = data_bytes[: xref_match.start()] + new_xref + data_bytes[xref_match.end() :]
                modified = True

    path.write_bytes(data_bytes)
    return True


def _apply_am_shell_inject(
    output_path: Path,
    match_pdf: Path,
    keep_am_fonts: bool = False,
    replacements: Optional[Dict[str, list[str]]] = None,
) -> bool:
    """
    Использовать AM как шаблон: заменить потоки (images, content, font, tounicode) нашими.
    keep_am_fonts=True: content из AM + CID-патч по replacements; font/ToUnicode из AM.
    Файл будет как «родной» (размер ~59 KB), бот может распознать.
    """
    am_bytes = match_pdf.read_bytes()
    our_bytes = output_path.read_bytes()

    def find_streams(data: bytes) -> list[tuple[int, int, int, bytes]]:
        """(obj_num, len_start, stream_start, stream_data)"""
        result = []
        for m in re.finditer(rb"(\d+)\s+0\s+obj(.*?)>>\s*stream\r?\n", data, re.DOTALL):
            obj_num = int(m.group(1))
            dict_part = m.group(2)
            len_m = re.search(rb"/Length\s+(\d+)", dict_part)
            if not len_m:
                continue
            stream_len = int(len_m.group(1))
            len_start = m.start() + dict_part.find(len_m.group(0).replace(b" ", b" "))
            len_start = m.start(2) + dict_part.find(len_m.group(0))
            stream_start = m.end()
            if stream_start + stream_len > len(data):
                continue
            stream_data = data[stream_start : stream_start + stream_len]
            len_pos = data.find(b"/Length", m.start(), m.end()) + 7
            len_end = data.find(b"/", len_pos) or data.find(b">", len_pos)
            result.append((obj_num, stream_len, stream_start, stream_data))
        return result

    def classify_stream(dict_part: bytes, stream_data: bytes) -> str:
        if b"/Subtype/Image" in dict_part or (b"/Width" in dict_part and b"/Height" in dict_part):
            return "image"
        try:
            dec = zlib.decompress(stream_data)
        except zlib.error:
            return "unknown"
        if b"beginbfchar" in dec or b"beginbfrange" in dec:
            return "tounicode"
        if b"/Length1" in dict_part and len(stream_data) > 1000:
            return "font"
        if b"BT" in dec and b"ET" in dec:
            return "content"
        return "unknown"

    # Собираем потоки AM по типам
    am_streams = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", am_bytes, re.DOTALL):
        obj_num = int(m.group(1))
        dict_part = m.group(2) + b"/Length " + m.group(3) + m.group(4)
        stream_len = int(m.group(3))
        stream_start = m.end()
        stream_data = am_bytes[stream_start : stream_start + stream_len]
        kind = classify_stream(dict_part, stream_data)
        if kind == "image":
            am_streams.setdefault("images", []).append((obj_num, stream_len, stream_start, stream_data, dict_part))
        elif kind == "content":
            am_streams["content"] = (obj_num, stream_len, stream_start, stream_data, dict_part)
        elif kind == "font":
            am_streams["font"] = (obj_num, stream_len, stream_start, stream_data, dict_part)
        elif kind == "tounicode":
            am_streams["tounicode"] = (obj_num, stream_len, stream_start, stream_data, dict_part)

    # Собираем наши потоки
    our_images = []
    our_content = our_font = our_tounicode = None
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", our_bytes, re.DOTALL):
        dict_part = m.group(2) + b"/Length " + m.group(3) + m.group(4)
        stream_len = int(m.group(3))
        stream_start = m.end()
        stream_data = our_bytes[stream_start : stream_start + stream_len]
        kind = classify_stream(dict_part, stream_data)
        if kind == "image":
            our_images.append((stream_len, stream_data))
        elif kind == "content" and our_content is None:
            our_content = stream_data
        elif kind == "font" and our_font is None:
            our_font = stream_data
        elif kind == "tounicode" and our_tounicode is None:
            our_tounicode = stream_data

    if "images" not in am_streams or len(am_streams["images"]) != 3:
        return False
    if "content" not in am_streams:
        return False
    if not keep_am_fonts and (our_content is None or our_font is None or our_tounicode is None):
        return False
    if keep_am_fonts and "font" not in am_streams:
        return False

    # Сортируем изображения по размеру для сопоставления
    am_imgs = sorted(am_streams["images"], key=lambda x: x[1])
    our_imgs = sorted(our_images, key=lambda x: x[0])
    if len(our_imgs) < 3:
        return False

    # Content stream
    final_content: bytes
    if keep_am_fonts:
        # Native mode: берём content из AM, патчим по CID-кодировке
        am_content_data = am_streams["content"][3]
        try:
            content_dec = zlib.decompress(am_content_data)
            if replacements:
                uni_to_cid = _parse_tounicode_from_pdf(am_bytes)
                if uni_to_cid:
                    content_dec, n_applied = _apply_cid_replacements_to_content(
                        content_dec, replacements, uni_to_cid
                    )
                    if n_applied > 0:
                        print(f"[INFO] CID replacements applied: {n_applied}", file=sys.stderr)
            final_content = zlib.compress(content_dec, 9)
        except Exception:
            final_content = am_content_data
    else:
        # Обычный режим: наш content, имена ресурсов -> AM (F1, Im0/1/2)
        try:
            content_dec = zlib.decompress(our_content)
            content_dec = content_dec.replace(b"/unicode_fallback", b"/F1")
            content_dec = content_dec.replace(b"/forced_tahoma", b"/F1")
            for i in range(3):
                content_dec = content_dec.replace(f"/fzImg{i}".encode(), f"/Im{i}".encode())
            final_content = zlib.compress(content_dec, 9)
        except Exception:
            final_content = our_content

    # Собираем все замены (pos, old_bytes, new_bytes) до применения
    all_repls: list[tuple[int, bytes, bytes]] = []
    for i, (am_obj, am_ln, am_start, _, _) in enumerate(am_imgs):
        new_data = our_imgs[i][1]
        old_data = am_bytes[am_start : am_start + am_ln]
        all_repls.append((am_start, old_data, new_data))
    keys_to_replace = ["content"]
    if not keep_am_fonts:
        keys_to_replace.extend(["font", "tounicode"])
    for key in keys_to_replace:
        _, ln, start, _, _ = am_streams[key]
        new_d = final_content if key == "content" else (our_font if key == "font" else our_tounicode)
        old_d = am_bytes[start : start + ln]
        all_repls.append((start, old_d, new_d))

    # Добавляем обновления /Length (точная позиция числа в dict перед потоком)
    for pos, old_data, new_data in all_repls[:]:
        old_len, new_len = len(old_data), len(new_data)
        if old_len == new_len:
            continue
        search_region = am_bytes[max(0, pos - 300) : pos + 50]
        num_m = re.search(rb"/Length\s+(\d+)", search_region)
        if num_m and int(num_m.group(1)) == old_len:
            num_pos = max(0, pos - 300) + num_m.start(1)
            old_str = str(old_len).encode()
            new_str = str(new_len).encode()
            all_repls.append((num_pos, old_str, new_str))

    all_repls.sort(key=lambda r: r[0], reverse=True)

    result = bytearray(am_bytes)
    for pos, old_b, new_b in all_repls:
        if pos < 0 or pos + len(old_b) > len(result):
            continue
        if result[pos : pos + len(old_b)] != old_b:
            continue
        result[pos : pos + len(old_b)] = new_b

    def shift_at(offset: int) -> int:
        return sum(len(n) - len(o) for p, o, n in all_repls if p < offset)

    data_bytes = bytes(result)
    xref_match = re.search(rb"(xref\r?\n\d+\s+\d+\r?\n)((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data_bytes)
    if xref_match:
        header, entries = xref_match.group(1), xref_match.group(2)
        def repl_off(m):
            old = int(m.group(1))
            return str(old + shift_at(old)).zfill(10).encode() + m.group(2)
        new_entries = re.sub(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", repl_off, entries)
        data_bytes = data_bytes[: xref_match.start()] + header + new_entries + data_bytes[xref_match.end() :]

    xref_match2 = re.search(rb"startxref\r?\n(\d+)\r?\n", data_bytes)
    if xref_match2 and shift_at(len(data_bytes)) != 0:
        new_off = int(xref_match2.group(1)) + shift_at(int(xref_match2.group(1)))
        data_bytes = data_bytes[: xref_match2.start(1)] + str(new_off).encode() + data_bytes[xref_match2.end(1) :]

    # Заменяем /W и /DW в CIDFont на наши — AM использует другую метрику.
    our_w_array: Optional[bytes] = None
    our_dw: Optional[bytes] = None
    w_ref = re.search(rb"/W\s+(\d+)\s+0\s+R", our_bytes)
    if w_ref:
        oid = w_ref.group(1).decode()
        obj_start = our_bytes.find(oid.encode() + b" 0 obj")
        if obj_start >= 0:
            arr_start = our_bytes.find(b"[", obj_start)
            if arr_start >= 0 and arr_start - obj_start < 50:
                depth, i = 1, arr_start + 1
                while i < len(our_bytes) and depth > 0:
                    if our_bytes[i : i + 1] == b"[":
                        depth += 1
                    elif our_bytes[i : i + 1] == b"]":
                        depth -= 1
                    i += 1
                our_w_array = b"/W " + our_bytes[arr_start:i]
    dw_m = re.search(rb"/DW\s+(\d+)", our_bytes)
    if dw_m:
        our_dw = dw_m.group(0)

    def _replace_w_dw(cidfont_bytes: bytes) -> bytes:
        out = bytearray(cidfont_bytes)
        if our_w_array:
            w_match = re.search(rb"/W\s+\[", out)
            if not w_match:
                w_match = re.search(rb"/W\s*\[", out)
            if w_match:
                start = w_match.start()
                arr_start = out.find(b"[", start)
                depth, i = 1, arr_start + 1
                while i < len(out) and depth > 0:
                    if out[i : i + 1] == b"[":
                        depth += 1
                    elif out[i : i + 1] == b"]":
                        depth -= 1
                    i += 1
                new_out = out[:start] + our_w_array + out[i:]
                out = new_out
        if our_dw:
            out = re.sub(rb"/DW\s+\d+\s*", our_dw + b" ", bytes(out))
        elif not our_w_array:
            out = re.sub(rb"/DW\s+\d+\s*", b"", bytes(out))
        return bytes(out)

    cid_match = re.search(rb"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R\s*\]", data_bytes)
    if cid_match and not keep_am_fonts:
        cid_id = cid_match.group(1).decode()
        cid_obj = re.search(
            cid_id.encode() + rb"\s+0\s+obj\r?\n(<<.*?>>)\r?\nendobj",
            data_bytes,
            re.DOTALL,
        )
        if cid_obj:
            old_dict = cid_obj.group(1)
            new_dict = _replace_w_dw(old_dict)
            if new_dict != old_dict:
                cid_start, cid_end = cid_obj.start(1), cid_obj.end(1)
                data_bytes = data_bytes[:cid_start] + new_dict + data_bytes[cid_end:]
                delta = len(new_dict) - len(old_dict)
                if delta != 0:
                    xref_match = re.search(
                        rb"(xref\r?\n\d+\s+\d+\r?\n)((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
                        data_bytes,
                    )
                    if xref_match:
                        header, entries = xref_match.group(1), xref_match.group(2)
                        def _adj(match):
                            off = int(match.group(1))
                            return (
                                str(off + (delta if off > cid_end else 0)).zfill(10).encode()
                                + match.group(2)
                            )
                        new_entries = re.sub(
                            rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", _adj, entries
                        )
                        data_bytes = (
                            data_bytes[: xref_match.start()]
                            + header
                            + new_entries
                            + data_bytes[xref_match.end() :]
                        )
                    xref_match2b = re.search(rb"startxref\r?\n(\d+)\r?\n", data_bytes)
                    if xref_match2b and cid_end < int(xref_match2b.group(1)):
                        old_off = int(xref_match2b.group(1))
                        data_bytes = (
                            data_bytes[: xref_match2b.start(1)]
                            + str(old_off + delta).encode()
                            + data_bytes[xref_match2b.end(1) :]
                        )

    output_path.write_bytes(data_bytes)
    return True


EDITOR_FINGERPRINT_TOKENS = [
    b"photoshop",
    b"canva",
    b"ilovepdf",
    b"acrobat distiller",
    b"pdfium",
    b"openhtmltopdf",
]


class SpanStyle(TypedDict):
    fontname: str
    fontsize: float
    color: tuple[float, float, float]


def color_int_to_rgb(color_value: int) -> tuple[float, float, float]:
    """Convert PDF integer color (0xRRGGBB) to PyMuPDF float RGB tuple."""
    r = (color_value >> 16) & 255
    g = (color_value >> 8) & 255
    b = color_value & 255
    return (r / 255.0, g / 255.0, b / 255.0)


def map_font_to_builtin(font_name: str) -> str:
    """Map source font names to builtin PDF base fonts."""
    name = (font_name or "").lower()
    if "courier" in name:
        return "cour"
    if "times" in name:
        return "tiro"
    if "symbol" in name:
        return "symb"
    if "zapfding" in name or "dingbat" in name:
        return "zapf"
    # Default safe mapping for sans-serif / unknown fonts.
    return "helv"


def _span_font_matches_basefont(span_font: str, basefont: str) -> bool:
    """Check if span font name matches get_page_fonts basefont (e.g. with ABCD+ prefix)."""
    if not span_font or not basefont:
        return False
    if span_font == basefont:
        return True
    if span_font in basefont:
        return True
    # basefont often is "PREFIX+FontName"; part after + is the real name
    if "+" in basefont:
        after_plus = basefont.split("+", 1)[1]
        if span_font == after_plus or span_font in after_plus or after_plus in span_font:
            return True
    return False


def _ensure_source_font_on_page(
    src_doc: fitz.Document,
    src_xref: int,
    new_page: fitz.Page,
    font_cache: Dict[int, tuple[str, bytes]],
    inserted_on_page: set,
) -> Optional[str]:
    """
    Extract font from source, insert into new page if needed, return alias.
    Returns None if extraction fails (e.g. builtin font, ext=n/a).
    """
    if src_xref <= 0:
        return None
    cached = font_cache.get(src_xref)
    if cached is None:
        try:
            result = src_doc.extract_font(src_xref)
            if not result or len(result) < 4:
                return None
            basename, ext, _ftype, buffer = result[:4]
            if ext == "n/a" or not buffer or len(buffer) < 50:
                return None
            alias = f"srcfont_{src_xref}"
            font_cache[src_xref] = (alias, bytes(buffer) if buffer else b"")
        except Exception:
            return None
    alias, buffer = font_cache[src_xref]
    if alias not in inserted_on_page and buffer:
        try:
            new_page.insert_font(fontname=alias, fontbuffer=buffer)
            inserted_on_page.add(alias)
        except Exception:
            return None
    return alias


ALNUM_BOUNDARY_CLASS = "0-9A-Za-zА-Яа-яЁё"


def _token_pattern_text(old_value: str) -> str:
    """Build token-like regex text for a replacement key."""
    parts = []
    for ch in old_value:
        if ch.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    escaped = "".join(parts)
    return rf"(?<![{ALNUM_BOUNDARY_CLASS}]){escaped}(?![{ALNUM_BOUNDARY_CLASS}])"


SOFT_HYPHEN = "\u00ad"  # U+00AD -> regular hyphen for match with Oracle PDFs


def _normalize_text_for_match(text: str) -> str:
    """Replace soft hyphen with regular hyphen to match Oracle-style PDFs."""
    return text.replace(SOFT_HYPHEN, "-") if text else text


def replace_text(
    text: str,
    replacements: Dict[str, list[str]],
    occurrence_state: Optional[Dict[str, int]] = None,
) -> str:
    """
    Apply non-cascading token replacements in one regex pass.
    - Replaces only standalone token-like matches.
    - Prevents chain effects and placeholder leakage.
    """
    text = _normalize_text_for_match(text)
    if not replacements:
        return text

    items = [(old, news) for old, news in replacements.items() if old and news]
    if not items:
        return text

    # Longest keys first to prefer specific matches in alternation.
    items.sort(key=lambda pair: len(pair[0]), reverse=True)
    group_to_old: Dict[str, str] = {}
    parts = []
    for idx, (old, _news) in enumerate(items):
        group_name = f"r{idx}"
        group_to_old[group_name] = old
        parts.append(f"(?P<{group_name}>{_token_pattern_text(old)})")

    pattern = re.compile("|".join(parts))

    def repl(match: re.Match[str]) -> str:
        group_name = match.lastgroup
        if not group_name:
            return match.group(0)
        old = group_to_old[group_name]
        news = replacements.get(old) or []
        if not news:
            return match.group(0)
        if occurrence_state is None:
            # Preview mode (no global occurrence mutation):
            # when multiple target variants exist, pick the first.
            return news[0]
        if len(news) == 1:
            return news[0]
        index = occurrence_state.get(old, 0)
        if index < len(news):
            replacement = news[index]
        else:
            replacement = news[-1]
        occurrence_state[old] = index + 1
        return replacement

    return pattern.sub(repl, text)


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_currency_marker(text: str) -> bool:
    upper = text.upper()
    return "RUR" in upper or "TJS" in upper or "UZS" in upper


def _has_non_ascii(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def _is_compact_numeric_field(text: str) -> bool:
    """
    Heuristic for fields where right-edge anchoring is safe:
    short, amount-like snippets (not long descriptive sentences).
    """
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > 28:
        return False
    letters = sum(ch.isalpha() for ch in stripped)
    # Allow short currency labels like RUR/TJS/UZS, but reject narrative text.
    if letters > 6:
        return False
    return _has_digit(stripped) and _has_currency_marker(stripped)


def _to_float(value: object, default: float = 0.0) -> float:
    """Safe float conversion for malformed PDF values."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_diff_index(a: str, b: str) -> int:
    """Return index of first differing character, or common length."""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit


def _pick_anchor_span_by_char_index(spans: list[dict], char_index: int) -> Optional[dict]:
    """
    Choose span that contains the changed character position in concatenated line text.
    Falls back to first non-empty span if index is out of bounds.
    """
    if not spans:
        return None
    pos = 0
    first_non_empty = None
    for span in spans:
        span_text = str(span.get("text", ""))
        if span_text and first_non_empty is None:
            first_non_empty = span
        next_pos = pos + len(span_text)
        if span_text and pos <= char_index < next_pos:
            return span
        pos = next_pos
    return first_non_empty


def _line_has_wide_span_gaps(spans: list[dict]) -> bool:
    """
    Detect table-like lines where text spans are separated by large horizontal gaps.
    In such lines, line-level redraw can merge neighboring columns into one string.
    """
    prev_x1: Optional[float] = None
    for span in spans:
        bbox = span.get("bbox")
        text = str(span.get("text", ""))
        if not bbox or len(bbox) != 4 or not text.strip():
            continue
        x0 = _to_float(bbox[0])
        x1 = _to_float(bbox[2])
        if prev_x1 is not None:
            gap = x0 - prev_x1
            if gap > 12.0:
                return True
        prev_x1 = x1
    return False


def _insert_text_with_style(
    page: fitz.Page,
    point: fitz.Point,
    text: str,
    style: SpanStyle,
    forced_font_available: bool,
    forced_font_alias: str,
    unicode_font_available: bool,
    unicode_font_alias: str,
    source_font_alias: Optional[str] = None,
) -> None:
    """Insert one text run with selected style and font policy."""
    if source_font_alias:
        page.insert_text(
            point,
            text,
            fontsize=style["fontsize"],
            fontname=source_font_alias,
            color=style["color"],
            overlay=True,
        )
    elif _has_non_ascii(text) and unicode_font_available:
        page.insert_text(
            point,
            text,
            fontsize=style["fontsize"],
            fontname=unicode_font_alias,
            color=style["color"],
            overlay=True,
        )
    elif forced_font_available:
        page.insert_text(
            point,
            text,
            fontsize=style["fontsize"],
            fontname=forced_font_alias,
            color=style["color"],
            overlay=True,
        )
    else:
        page.insert_text(
            point,
            text,
            fontsize=style["fontsize"],
            fontname=style["fontname"],
            color=style["color"],
            overlay=True,
        )


def _measure_text_width(
    text: str,
    style: SpanStyle,
    forced_font_available: bool,
    forced_font_alias: str,
    unicode_font_available: bool,
    unicode_font_alias: str,
    source_font_alias: Optional[str] = None,
) -> float:
    """Estimate text width for right-edge anchoring."""
    if source_font_alias:
        font_for_measure = source_font_alias
    elif _has_non_ascii(text) and unicode_font_available:
        font_for_measure = unicode_font_alias
    elif forced_font_available:
        font_for_measure = forced_font_alias
    else:
        font_for_measure = style["fontname"]
    try:
        return fitz.get_text_length(
            text,
            fontname=font_for_measure,
            fontsize=style["fontsize"],
        )
    except Exception:
        return max(8.0, style["fontsize"] * len(text) * 0.62)


def _span_point(span: dict, offset_x: float, offset_y: float) -> fitz.Point:
    """
    Use original text baseline (origin) when available.
    This minimizes vertical drift after replacement.
    """
    origin = span.get("origin")
    if origin and len(origin) >= 2 and origin[0] is not None and origin[1] is not None:
        return fitz.Point(_to_float(origin[0]) - offset_x, _to_float(origin[1]) - offset_y)

    bbox = span.get("bbox")
    if bbox and len(bbox) == 4:
        return fitz.Point(_to_float(bbox[0]) - offset_x, _to_float(bbox[3]) - offset_y)

    return fitz.Point(0.0, 0.0)


def _insert_right_anchored(
    page: fitz.Page,
    line_bbox: fitz.Rect,
    baseline_y: float,
    text: str,
    style: SpanStyle,
    forced_font_available: bool,
    forced_font_alias: str,
    unicode_font_available: bool,
    unicode_font_alias: str,
    source_font_alias: Optional[str] = None,
) -> None:
    """
    Insert text so right edge stays in place:
    if replacement is longer, it grows to the left.
    """
    text_width = _measure_text_width(
        text,
        style,
        forced_font_available,
        forced_font_alias,
        unicode_font_available,
        unicode_font_alias,
        source_font_alias,
    )
    x = max(0.0, line_bbox.x1 - text_width)
    point = fitz.Point(x, baseline_y)
    _insert_text_with_style(
        page,
        point,
        text,
        style,
        forced_font_available,
        forced_font_alias,
        unicode_font_available,
        unicode_font_alias,
        source_font_alias,
    )


def resolve_fontfile(candidates: list[str]) -> Optional[str]:
    """Return first available font path from candidates."""
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def get_first_font_name_from_pdf(pdf_path: Path) -> Optional[str]:
    """
    Get the first font's name from the first page of a PDF.
    Prefers the symbolic 'name' (e.g. font000000002f4db77f) when meaningful,
    else the part after '+' in basefont.
    Used for --font-alias-from-input.
    """
    if not pdf_path.exists():
        return None
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return None
            fonts = doc.get_page_fonts(0, full=False)
            if not fonts:
                return None
            item = fonts[0]
            if len(item) < 4:
                return None
            basefont = str(item[3])
            name = str(item[4]) if len(item) >= 5 else ""
            if name and len(name) > 3 and not re.match(r"^[A-Z]\d+$", name):
                return name
            if "+" in basefont:
                return basefont.split("+", 1)[1]
            return basefont
    except Exception:
        return None


def get_first_font_basefont_from_pdf(pdf_path: Path) -> Optional[bytes]:
    """
    Получить BaseFont первого шрифта из PDF (как в сыром виде, для патча).
    Пример: AAAAAB+font000000002f4db77f (iOS CID), KKGMOS+Tahoma (Oracle).
    """
    if not pdf_path.exists():
        return None
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return None
            fonts = doc.get_page_fonts(0, full=False)
            if not fonts or len(fonts[0]) < 4:
                return None
            basefont = str(fonts[0][3])
            # В PDF пробелы экранируются как #20
            encoded = basefont.replace(" ", "#20").encode("ascii", errors="replace")
            return encoded
    except Exception:
        return None


def get_producer_from_pdf(pdf_path: Path) -> Optional[bytes]:
    """Получить Producer из PDF (для патча метаданных)."""
    if not pdf_path.exists():
        return None
    try:
        with fitz.open(pdf_path) as doc:
            prod = (doc.metadata or {}).get("producer")
            if prod:
                return prod.encode("ascii", errors="replace")
    except Exception:
        pass
    try:
        data = pdf_path.read_bytes()
        m = re.search(rb"/Producer\s*\(([^)]*)\)", data)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def extract_donor_style(donor_pdf: Path, donor_text: str) -> Optional[SpanStyle]:
    """Find style of donor_text in donor PDF and return mapped font/size/color."""
    if not donor_text:
        return None
    if not donor_pdf.exists():
        raise FileNotFoundError(f"Donor PDF not found: {donor_pdf}")

    with fitz.open(donor_pdf) as donor_doc:
        for donor_page in donor_doc:
            text_dict = donor_page.get_text("dict")
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_text = str(span.get("text", ""))
                        if not span_text:
                            continue
                        if donor_text not in span_text:
                            continue
                        return {
                            "fontname": map_font_to_builtin(str(span.get("font", ""))),
                            "fontsize": float(span.get("size", 11.0)),
                            "color": color_int_to_rgb(int(span.get("color", 0))),
                        }
    return None


def draw_page_images_from_xrefs(
    src_doc: fitz.Document,
    src_page: fitz.Page,
    new_page: fitz.Page,
    offset_x: float,
    offset_y: float,
    image_cache: Dict[int, int],
) -> None:
    """
    Copy images by original xref streams (no re-encoding from text blocks).
    This usually keeps file size much closer to source.
    """
    for image_info in src_page.get_images(full=True):
        src_xref = int(image_info[0])
        if src_xref <= 0:
            continue

        rects = src_page.get_image_rects(src_xref)
        if not rects:
            continue

        dst_xref = image_cache.get(src_xref)
        if dst_xref is None:
            extracted = src_doc.extract_image(src_xref)
            image_bytes = extracted.get("image")
            if not image_bytes:
                continue
            first_rect = fitz.Rect(
                rects[0].x0 - offset_x,
                rects[0].y0 - offset_y,
                rects[0].x1 - offset_x,
                rects[0].y1 - offset_y,
            )
            if first_rect.is_empty:
                continue
            dst_xref = int(
                new_page.insert_image(
                    rect=first_rect,
                    stream=image_bytes,
                    keep_proportion=False,
                )
            )
            image_cache[src_xref] = dst_xref
            remaining_rects = rects[1:]
        else:
            remaining_rects = rects

        for rect in remaining_rects:
            target_rect = fitz.Rect(
                rect.x0 - offset_x,
                rect.y0 - offset_y,
                rect.x1 - offset_x,
                rect.y1 - offset_y,
            )
            if target_rect.is_empty:
                continue
            new_page.insert_image(
                rect=target_rect,
                xref=dst_xref,
                keep_proportion=False,
            )


def draw_page_vector_paths(
    src_page: fitz.Page,
    new_page: fitz.Page,
    offset_x: float,
    offset_y: float,
) -> None:
    """
    Copy vector drawings (lines, rectangles, curves, fills).
    This restores visual elements like separators/bars that are not images.
    """
    drawings = src_page.get_drawings()
    for path in drawings:
        shape = new_page.new_shape()
        for item in path.get("items", []):
            op = item[0]
            try:
                if op == "l":
                    p1 = fitz.Point(_to_float(item[1].x) - offset_x, _to_float(item[1].y) - offset_y)
                    p2 = fitz.Point(_to_float(item[2].x) - offset_x, _to_float(item[2].y) - offset_y)
                    shape.draw_line(p1, p2)
                elif op == "re":
                    r = item[1]
                    rect = fitz.Rect(
                        _to_float(r.x0) - offset_x,
                        _to_float(r.y0) - offset_y,
                        _to_float(r.x1) - offset_x,
                        _to_float(r.y1) - offset_y,
                    )
                    shape.draw_rect(rect)
                elif op == "c":
                    p1 = fitz.Point(_to_float(item[1].x) - offset_x, _to_float(item[1].y) - offset_y)
                    p2 = fitz.Point(_to_float(item[2].x) - offset_x, _to_float(item[2].y) - offset_y)
                    p3 = fitz.Point(_to_float(item[3].x) - offset_x, _to_float(item[3].y) - offset_y)
                    p4 = fitz.Point(_to_float(item[4].x) - offset_x, _to_float(item[4].y) - offset_y)
                    shape.draw_bezier(p1, p2, p3, p4)
                elif op == "qu":
                    q = item[1]
                    pts = [
                        fitz.Point(_to_float(p.x) - offset_x, _to_float(p.y) - offset_y)
                        for p in q
                    ]
                    if len(pts) >= 4:
                        shape.draw_quad(fitz.Quad(pts[0], pts[1], pts[2], pts[3]))
            except Exception:
                # Skip malformed path entries but keep processing the page.
                continue

        color = path.get("color")
        fill = path.get("fill")
        width = _to_float(path.get("width"), 1.0)
        line_cap = path.get("lineCap") or 0
        line_join = int(path.get("lineJoin") or 0)
        dashes = path.get("dashes")
        close_path = bool(path.get("closePath", False))
        even_odd = bool(path.get("even_odd", False))

        shape.finish(
            color=color,
            fill=fill,
            width=width,
            lineCap=line_cap,
            lineJoin=line_join,
            dashes=dashes,
            closePath=close_path,
            even_odd=even_odd,
        )
        shape.commit(overlay=True)


def draw_text_block(
    new_page: fitz.Page,
    block: dict,
    offset_x: float,
    offset_y: float,
    replacements: Dict[str, list[str]],
    occurrence_state: Dict[str, int],
    donor_style: Optional[SpanStyle],
    forced_font_available: bool,
    forced_font_alias: str,
    unicode_font_available: bool,
    unicode_font_alias: str,
    numbers_grow_left: bool,
    use_source_fonts: bool = False,
    src_doc: Optional[fitz.Document] = None,
    src_page_num: int = 0,
    font_cache: Optional[Dict[int, tuple[str, bytes]]] = None,
    font_inserted_on_page: Optional[set] = None,
    page_font_map: Optional[Dict[str, int]] = None,
) -> None:
    """Insert text spans preserving coordinates, size and color."""

    def _resolve_source_font(span: dict) -> Optional[str]:
        if not use_source_fonts or not src_doc or font_cache is None or font_inserted_on_page is None or page_font_map is None:
            return None
        span_font = str(span.get("font", ""))
        if not span_font:
            return None
        src_xref = page_font_map.get(span_font)
        if src_xref is None:
            for basefont, xref in page_font_map.items():
                if _span_font_matches_basefont(span_font, basefont):
                    src_xref = xref
                    break
        if src_xref is None:
            return None
        return _ensure_source_font_on_page(src_doc, src_xref, new_page, font_cache, font_inserted_on_page)

    for line in block.get("lines", []):
        spans = line.get("spans", [])
        if not spans:
            continue

        # Try replacement on whole line text to catch values split across spans.
        line_text = "".join(str(span.get("text", "")) for span in spans)
        replaced_line_text = replace_text(line_text, replacements)
        span_level_changed = any(
            replace_text(str(span.get("text", "")), replacements)
            != str(span.get("text", ""))
            for span in spans
        )

        # Use line-level replacement only if needed (split across spans).
        # Disabled by default because merging spans into one draw operation can
        # break table layout and cause cross-column overlaps.
        if (
            ENABLE_LINE_LEVEL_REPLACEMENT
            and
            replaced_line_text != line_text
            and replaced_line_text
            and not span_level_changed
            and not _line_has_wide_span_gaps(spans)
        ):
            changed_at = _first_diff_index(line_text, replaced_line_text)
            anchor_span = _pick_anchor_span_by_char_index(spans, changed_at)
            line_x0 = float("inf")
            line_y0 = float("inf")
            line_x1 = float("-inf")
            line_y1 = float("-inf")

            for span in spans:
                span_text = str(span.get("text", ""))
                bbox = span.get("bbox")
                if bbox and len(bbox) == 4:
                    line_x0 = min(line_x0, float(bbox[0]))
                    line_y0 = min(line_y0, float(bbox[1]))
                    line_x1 = max(line_x1, float(bbox[2]))
                    line_y1 = max(line_y1, float(bbox[3]))

            if anchor_span is not None and line_x0 != float("inf"):
                replaced_line_text = replace_text(line_text, replacements, occurrence_state)
                if not replaced_line_text:
                    continue
                point = _span_point(anchor_span, offset_x, offset_y)
                baseline_y = point.y
                line_rect = fitz.Rect(
                    line_x0 - offset_x,
                    line_y0 - offset_y,
                    line_x1 - offset_x,
                    line_y1 - offset_y,
                )

                src_style: SpanStyle = {
                    "fontname": map_font_to_builtin(str(anchor_span.get("font", ""))),
                    "fontsize": _to_float(anchor_span.get("size"), 11.0),
                    "color": color_int_to_rgb(int(anchor_span.get("color", 0))),
                }
                style = donor_style if donor_style is not None else src_style

                src_font = _resolve_source_font(anchor_span)
                try:
                    # Do not apply "grow left" on line-level replacements:
                    # line bbox often covers unrelated text, so right anchoring by line x1
                    # can shift replaced numbers too far to the left.
                    _insert_text_with_style(
                        new_page,
                        point,
                        replaced_line_text,
                        style,
                        forced_font_available,
                        forced_font_alias,
                        unicode_font_available,
                        unicode_font_alias,
                        src_font,
                    )
                except Exception:
                    _insert_text_with_style(
                        new_page,
                        point,
                        replaced_line_text,
                        {
                            "fontname": "helv",
                            "fontsize": style["fontsize"],
                            "color": style["color"],
                        },
                        forced_font_available=False,
                        forced_font_alias=forced_font_alias,
                        unicode_font_available=unicode_font_available,
                        unicode_font_alias=unicode_font_alias,
                        source_font_alias=src_font,
                    )
                continue

        # Fallback to span-level drawing when no line-level replacement happened.
        for span in spans:
            src_text = span.get("text", "")
            if not src_text:
                continue

            out_text = replace_text(src_text, replacements, occurrence_state)
            if not out_text:
                continue

            bbox = span.get("bbox")
            if not bbox or len(bbox) != 4:
                continue

            x = float(bbox[0]) - offset_x
            point = _span_point(span, offset_x, offset_y)
            baseline_y = point.y
            span_rect = fitz.Rect(
                float(bbox[0]) - offset_x,
                float(bbox[1]) - offset_y,
                float(bbox[2]) - offset_x,
                float(bbox[3]) - offset_y,
            )

            src_style: SpanStyle = {
                "fontname": map_font_to_builtin(str(span.get("font", ""))),
                "fontsize": _to_float(span.get("size"), 11.0),
                "color": color_int_to_rgb(int(span.get("color", 0))),
            }
            style = donor_style if out_text != src_text and donor_style is not None else src_style
            src_font = _resolve_source_font(span)

            try:
                if (
                    numbers_grow_left
                    and out_text != src_text
                    and _has_digit(src_text)
                    and _has_digit(out_text)
                    and (_has_currency_marker(src_text) or _has_currency_marker(out_text))
                    and (_is_compact_numeric_field(src_text) or _is_compact_numeric_field(out_text))
                ):
                    _insert_right_anchored(
                        new_page,
                        span_rect,
                        baseline_y,
                        out_text,
                        style,
                        forced_font_available,
                        forced_font_alias,
                        unicode_font_available,
                        unicode_font_alias,
                        src_font,
                    )
                else:
                    _insert_text_with_style(
                        new_page,
                        point,
                        out_text,
                        style,
                        forced_font_available,
                        forced_font_alias,
                        unicode_font_available,
                        unicode_font_alias,
                        src_font,
                    )
            except Exception:
                _insert_text_with_style(
                    new_page,
                    point,
                    out_text,
                    {
                        "fontname": "helv",
                        "fontsize": style["fontsize"],
                        "color": style["color"],
                    },
                    forced_font_available=False,
                    forced_font_alias=forced_font_alias,
                    unicode_font_available=unicode_font_available,
                    unicode_font_alias=unicode_font_alias,
                    source_font_alias=src_font,
                )


def build_clean_room_pdf(
    input_path: Path,
    output_path: Path,
    replacements: Dict[str, list[str]],
    donor_style: Optional[SpanStyle],
    forced_fontfile: Optional[str],
    forced_font_alias: str,
    unicode_fontfile: Optional[str],
    unicode_font_alias: str,
    optimize_size: bool,
    numbers_grow_left: bool,
    match_input_pdf_version: bool,
    copy_input_metadata: bool,
    use_source_fonts: bool = True,
    font_alias_note: Optional[str] = None,
    native_structure: bool = False,
    creation_date_override: Optional[str] = None,
    mod_date_override: Optional[str] = None,
    metadata_source_path: Optional[Path] = None,
    version_source_path: Optional[Path] = None,
    metadata_only_will_patch: bool = False,
    allow_subset_when_metadata_patch: bool = False,
) -> None:
    """
    Create a fully rebuilt PDF from visual primitives only.

    Forensic rationale:
    - No page-level binary copying from source into target.
    - New file body is generated from scratch.
    - save(..., garbage=4, clean=True) compacts and rewrites xref/object structure.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    # Capture PDF header version (e.g. "1.3", "1.6") for matching output.
    version_src = version_source_path or input_path
    input_header_version = None
    try:
        header = version_src.read_bytes()[:16]
        m = re.match(br"%PDF-(\d\.\d)", header)
        if m:
            input_header_version = m.group(1).decode("ascii", errors="ignore")
    except Exception:
        input_header_version = None

    metadata_src = metadata_source_path or input_path
    with fitz.open(input_path) as src_doc:
        if src_doc.page_count == 0:
            raise ValueError("Input PDF has zero pages.")

        new_doc = fitz.Document()
        image_cache: Dict[int, int] = {}
        occurrence_state: Dict[str, int] = {}
        font_cache: Dict[int, tuple[str, bytes]] = {}
        try:
            for pno, src_page in enumerate(src_doc):
                page_rect = src_page.cropbox if not src_page.cropbox.is_empty else src_page.rect
                page_width = float(page_rect.width)
                page_height = float(page_rect.height)
                offset_x = float(page_rect.x0)
                offset_y = float(page_rect.y0)

                new_page = new_doc.new_page(width=page_width, height=page_height)
                forced_font_available = False
                if forced_fontfile:
                    try:
                        new_page.insert_font(
                            fontname=forced_font_alias,
                            fontfile=forced_fontfile,
                        )
                        forced_font_available = True
                    except Exception:
                        forced_font_available = False

                unicode_font_available = False
                if unicode_fontfile:
                    # When alias is same as forced (e.g. --font-alias-from-input), skip second insert.
                    if unicode_font_alias != forced_font_alias or not forced_font_available:
                        try:
                            new_page.insert_font(
                                fontname=unicode_font_alias,
                                fontfile=unicode_fontfile,
                            )
                            unicode_font_available = True
                        except Exception:
                            unicode_font_available = False
                    else:
                        unicode_font_available = forced_font_available

                draw_page_images_from_xrefs(
                    src_doc=src_doc,
                    src_page=src_page,
                    new_page=new_page,
                    offset_x=offset_x,
                    offset_y=offset_y,
                    image_cache=image_cache,
                )
                draw_page_vector_paths(
                    src_page=src_page,
                    new_page=new_page,
                    offset_x=offset_x,
                    offset_y=offset_y,
                )

                content = src_page.get_text("dict")
                # Normalize soft hyphens to match Oracle PDFs
                for block in content.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if "text" in span:
                                span["text"] = _normalize_text_for_match(span["text"])

                # Build page font map: basefont (or name) -> xref for source font lookup.
                # Skip Type0/Identity-H: CID fonts often break when re-embedded (ToUnicode loss).
                page_font_map: Dict[str, int] = {}
                if use_source_fonts:
                    try:
                        for item in src_doc.get_page_fonts(pno, full=False):
                            if len(item) < 4:
                                continue
                            xref, ext, ftype, basefont = item[:4]
                            encoding = item[5] if len(item) >= 6 else ""
                            if ext == "n/a" or ftype == "Type0" or "Identity" in str(encoding):
                                continue
                            page_font_map[basefont] = xref
                            if "+" in basefont:
                                page_font_map[basefont.split("+", 1)[1]] = xref
                    except Exception:
                        pass
                font_inserted_on_page: set = set()

                # Text is redrawn from spans with optional replacements.
                for block in content.get("blocks", []):
                    block_type = block.get("type")
                    if block_type == 0:
                        draw_text_block(
                            new_page,
                            block,
                            offset_x,
                            offset_y,
                            replacements,
                            occurrence_state,
                            donor_style,
                            forced_font_available,
                            forced_font_alias,
                            unicode_font_available,
                            unicode_font_alias,
                            numbers_grow_left,
                            use_source_fonts=use_source_fonts,
                            src_doc=src_doc if use_source_fonts else None,
                            src_page_num=pno,
                            font_cache=font_cache if use_source_fonts else None,
                            font_inserted_on_page=font_inserted_on_page if use_source_fonts else None,
                            page_font_map=page_font_map if use_source_fonts else None,
                        )

            if copy_input_metadata:
                # Copy document info from metadata_source (input or --match-pdf).
                try:
                    meta_doc = fitz.open(metadata_src) if metadata_src != input_path else src_doc
                    meta = dict(meta_doc.metadata or {})
                    if meta_doc != src_doc:
                        meta_doc.close()
                except Exception:
                    meta = dict(src_doc.metadata or {})
                if font_alias_note:
                    kw = meta.get("keywords", "")
                    meta["keywords"] = f"{kw}; {font_alias_note}".strip("; ").strip() if kw else font_alias_note
                # Переопределение дат (--creation-date, --mod-date)
                if creation_date_override:
                    pdf_creation = parse_date_to_pdf_format(creation_date_override)
                    if pdf_creation:
                        meta["creationDate"] = pdf_creation
                if mod_date_override:
                    pdf_mod = parse_date_to_pdf_format(mod_date_override)
                    if pdf_mod:
                        meta["modDate"] = pdf_mod
                elif creation_date_override:
                    # modDate = creationDate если mod-date не задан
                    pdf_creation = parse_date_to_pdf_format(creation_date_override)
                    if pdf_creation:
                        meta["modDate"] = pdf_creation
                new_doc.set_metadata(meta)
            else:
                # Neutral metadata for clean forensic profile.
                meta = {
                    "title": "Document",
                    "author": "",
                    "subject": "",
                    "keywords": font_alias_note or "",
                    "creator": "",
                    "producer": "PDF Generator",
                    "creationDate": "",
                    "modDate": "",
                }
                if creation_date_override:
                    pdf_creation = parse_date_to_pdf_format(creation_date_override)
                    if pdf_creation:
                        meta["creationDate"] = meta["modDate"] = pdf_creation
                if mod_date_override:
                    pdf_mod = parse_date_to_pdf_format(mod_date_override)
                    if pdf_mod:
                        meta["modDate"] = pdf_mod
                new_doc.set_metadata(meta)
            # Remove XMP metadata packet when available.
            try:
                if native_structure:
                    new_doc.set_xml_metadata("")
                elif copy_input_metadata:
                    xmp_doc = fitz.open(metadata_src) if metadata_src != input_path else src_doc
                    xmp = xmp_doc.get_xml_metadata() or ""
                    new_doc.set_xml_metadata(xmp)
                    if xmp_doc != src_doc:
                        xmp_doc.close()
                else:
                    new_doc.set_xml_metadata("")
            except Exception:
                pass

            if optimize_size and (not metadata_only_will_patch or allow_subset_when_metadata_patch):
                # Subset embedded fonts to used glyphs only (usually biggest win).
                # Пропускаем при metadata_only: subset ломает ToUnicode (TJS→TÃS).
                # allow_subset_when_metadata_patch: для СБП (RUR) уменьшает файл с 437KB до ~60KB.
                try:
                    new_doc.subset_fonts()
                except Exception:
                    pass

            if optimize_size:
                save_kwargs = {
                    "garbage": 4,
                    "clean": True,
                    "deflate": True,
                    "deflate_images": True,
                    "deflate_fonts": not metadata_only_will_patch,
                    # Без object streams, если: 1.3/1.4, native_structure, или metadata_only патч.
                    "use_objstms": 0
                    if (
                        native_structure
                        or metadata_only_will_patch
                        or (match_input_pdf_version and input_header_version in ("1.3", "1.4"))
                    )
                    else 1,
                }
            else:
                # No compression, no compaction: file is neither shrunk nor adjusted.
                save_kwargs = {
                    "garbage": 0,
                    "clean": False,
                    "deflate": False,
                    "deflate_images": False,
                    "deflate_fonts": False,
                    "use_objstms": 0,
                }
            if metadata_only_will_patch:
                try:
                    new_doc.del_xml_metadata()
                except Exception:
                    pass
            try:
                new_doc.save(output_path, **save_kwargs)
            except TypeError:
                # Compatibility for older PyMuPDF versions.
                save_kwargs.pop("deflate_images", None)
                save_kwargs.pop("deflate_fonts", None)
                save_kwargs.pop("use_objstms", None)
                new_doc.save(output_path, **save_kwargs)

            # PyMuPDF does not expose a "save as PDF-1.3" option, but most tools
            # (incl. qpdf) report the version from the header line. If you need the
            # output to *report* the same version as input (for lab checks), we can
            # safely rewrite "%PDF-1.x" (same-length edit).
            if match_input_pdf_version and input_header_version:
                try:
                    with output_path.open("r+b") as f:
                        first = f.read(16)
                        m2 = re.match(br"%PDF-(\d\.\d)", first)
                        if m2:
                            f.seek(0)
                            f.write(f"%PDF-{input_header_version}".encode("ascii"))
                except Exception:
                    pass
        finally:
            new_doc.close()


def parse_replacements_arg(values: list[str]) -> Dict[str, list[str]]:
    """Parse CLI replacements in form old=new (keeps duplicate keys in order)."""
    result: Dict[str, list[str]] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid --replace value (expected old=new): {item}")
        old, new = item.split("=", 1)
        if old not in result:
            result[old] = []
        result[old].append(new)
    return result


def forensic_self_check(pdf_path: Path) -> dict:
    """
    Basic forensic-oriented checks:
    - xref / EOF marker counts (incremental update signal)
    - suspicious object tokens
    - common editor fingerprint tokens
    - metadata sanity
    """
    raw = pdf_path.read_bytes()
    raw_lower = raw.lower()

    suspicious_hits: Dict[str, int] = {}
    for token in SUSPICIOUS_OBJECT_TOKENS:
        # /AA в именах шрифтов (AAGQMC, AAFYPC) — не считается; нужен отдельный токен /AA
        if token == b"/AA":
            count = len(re.findall(rb"/AA(?![a-zA-Z0-9+_\-\.])", raw))
        else:
            count = raw.count(token)
        if count > 0:
            suspicious_hits[token.decode("ascii", errors="ignore")] = count

    fingerprint_hits: Dict[str, int] = {}
    for token in EDITOR_FINGERPRINT_TOKENS:
        count = raw_lower.count(token)
        if count > 0:
            fingerprint_hits[token.decode("ascii", errors="ignore")] = count

    metadata = {}
    try:
        with fitz.open(pdf_path) as doc:
            metadata = doc.metadata or {}
    except Exception:
        metadata = {}

    creator = (metadata.get("creator") or "").lower()
    producer = (metadata.get("producer") or "").lower()
    metadata_warnings = []
    for marker in ("photoshop", "canva", "ilovepdf", "openhtmltopdf"):
        if marker in creator or marker in producer:
            metadata_warnings.append(marker)

    startxref_count = raw.count(b"startxref")
    eof_count = raw.count(b"%%EOF")
    has_incremental_chain = startxref_count > 1 or eof_count > 1

    passed = (
        not has_incremental_chain
        and not suspicious_hits
        and not fingerprint_hits
        and not metadata_warnings
    )

    return {
        "passed": passed,
        "startxref_count": startxref_count,
        "eof_count": eof_count,
        "has_incremental_chain": has_incremental_chain,
        "suspicious_hits": suspicious_hits,
        "fingerprint_hits": fingerprint_hits,
        "metadata_warnings": metadata_warnings,
        "metadata": metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean Room PDF rebuild: images+text redrawn on a blank PDF."
    )
    parser.add_argument("input_pdf", help="Source PDF path (e.g. input.pdf)")
    parser.add_argument("output_pdf", help="Output PDF path (e.g. output.pdf)")
    parser.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Text replacement pair. Can be repeated.",
    )
    parser.add_argument(
        "--replacements-json",
        default="",
        help="Optional JSON file with replacements object.",
    )
    parser.add_argument(
        "--donor-pdf",
        default="",
        help="Optional donor PDF path to copy text style from.",
    )
    parser.add_argument(
        "--donor-text",
        default="1000 RUR",
        help="Text sample to find in donor PDF (default: '1000 RUR').",
    )
    parser.add_argument(
        "--strict-forensic",
        action="store_true",
        help="Return non-zero exit code if forensic self-check reports findings.",
    )
    parser.add_argument(
        "--no-size-optimize",
        action="store_true",
        help=(
            "Disable size optimizations: no font subsetting, no deflate compression, "
            "no garbage collection, no object streams. File is neither compressed nor adjusted."
        ),
    )
    parser.add_argument(
        "--numbers-grow-left",
        action="store_true",
        help=(
            "For numeric replacements, keep right edge fixed and expand to the left "
            "(recommended for input2)."
        ),
    )
    parser.add_argument(
        "--preserve-font-metrics",
        action="store_true",
        help=(
            "Do not force Tahoma/Arial for all text. "
            "Use source mapped fonts to reduce layout drift."
        ),
    )
    parser.add_argument(
        "--match-input-pdf-version",
        action="store_true",
        dest="match_input_pdf_version",
        default=True,
        help="Rewrite output header to match input PDF version (default: on).",
    )
    parser.add_argument(
        "--no-match-pdf-version",
        action="store_false",
        dest="match_input_pdf_version",
        help="Do not match input PDF version in output header.",
    )
    parser.add_argument(
        "--copy-input-metadata",
        action="store_true",
        dest="copy_input_metadata",
        default=True,
        help="Copy input PDF metadata (Producer/Creator/etc) into output (default: on).",
    )
    parser.add_argument(
        "--no-copy-metadata",
        action="store_false",
        dest="copy_input_metadata",
        help="Do not copy input metadata; use neutral producer/title.",
    )
    parser.add_argument(
        "--no-preserve-source-fonts",
        action="store_false",
        dest="use_source_fonts",
        default=True,
        help="Use Tahoma/fallback instead of original fonts (default: preserve source fonts).",
    )
    parser.add_argument(
        "--font-alias",
        default="",
        metavar="NAME",
        help=(
            "Use this name for the font in PDF resources (e.g. font000000002f4db77f). "
            "Glyphs still come from Tahoma; only the displayed name changes."
        ),
    )
    parser.add_argument(
        "--font-alias-from-input",
        action="store_true",
        help="Use the first font name from input PDF as font alias (for Type0 fallback).",
    )
    parser.add_argument(
        "--no-font-keywords-note",
        action="store_true",
        dest="no_font_keywords_note",
        help="Do not add font substitution note to keywords (exact metadata copy).",
    )
    parser.add_argument(
        "--patch-header",
        action="store_true",
        help=(
            "После сохранения убрать «Written by MuPDF» из заголовка (вариант Б). "
            "Требует patch_pdf_header.py в том же каталоге."
        ),
    )
    parser.add_argument(
        "--native-structure",
        action="store_true",
        help=(
            "Вариант C: структура как input ru.pdf / 20251021220717 — "
            "Im1/Im2/Im3, G1, без Metadata Stream."
        ),
    )
    parser.add_argument(
        "--ios-match",
        action="store_true",
        help=(
            "Более полное совпадение с эталоном iOS: Producer, BaseFont из input, "
            "подразумевает --native-structure и --patch-header."
        ),
    )
    parser.add_argument(
        "--match-pdf",
        default="",
        metavar="PATH",
        help=(
            "Эталлон для forensic-совпадения (Oracle/AM и др.): метаданные, Producer, "
            "BaseFont, Document ID, версия PDF берутся из указанного файла. "
            "Подразумевает --native-structure и --patch-header. Пример: AM_1772641049914.pdf"
        ),
    )
    parser.add_argument(
        "--match-pdf-metadata-only",
        action="store_true",
        help=(
            "С --match-pdf: копировать только метаданные и версию, без native-structure патча. "
            "Используйте если полный патч даёт повреждённый PDF."
        ),
    )
    parser.add_argument(
        "--random-document-id",
        action="store_true",
        help=(
            "С --match-pdf-metadata-only: заменить Document ID на случайный 32 hex вместо копирования из эталона. "
            "Длина не меняется — xref и структура файла не затрагиваются."
        ),
    )
    parser.add_argument(
        "--native-match",
        action="store_true",
        help=(
            "С --match-pdf-metadata-only: content из AM + CID-патч замен; font/ToUnicode от AM. "
            "Файл ~59 KB как родной, бот может распознать. Замены: old=new, old должен быть в AM."
        ),
    )
    parser.add_argument(
        "--no-am-shell-inject",
        action="store_true",
        help=(
            "Не применять AM shell inject. Текст (TJS и др.) извлекается корректно, "
            "но структура PDF может отличаться от эталона. Используйте если бот не распознаёт банк/валюту."
        ),
    )
    parser.add_argument(
        "--subset-fonts",
        action="store_true",
        help=(
            "При --match-pdf-metadata-only: включить subset шрифтов. Уменьшает файл с ~437KB до ~60KB. "
            "Может исказить TJS в переводах за рубеж — используйте только для СБП (RUR)."
        ),
    )
    parser.add_argument(
        "--keep-input-producer",
        action="store_true",
        help=(
            "С --match-pdf-metadata-only: брать Producer из input (iOS Quartz), а не из AM (Oracle). "
            "Нужно для распознавания чеков Альфа-Банка вторым ботом."
        ),
    )
    parser.add_argument(
        "--creation-date",
        default="",
        metavar="DATE",
        help=(
            "Дата создания PDF в метаданных. Форматы: YYYY-MM-DD HH:MM, DD.MM.YYYY HH:MM. "
            "Рекомендуется указывать близко к дате операции в чеке."
        ),
    )
    parser.add_argument(
        "--mod-date",
        default="",
        metavar="DATE",
        help=(
            "Дата изменения PDF (modDate). Если не задана — используется creation-date."
        ),
    )
    args = parser.parse_args()

    if args.ios_match:
        args.native_structure = True
        args.patch_header = True
    match_pdf_path: Optional[Path] = None
    match_metadata_only: bool = bool(getattr(args, "match_pdf_metadata_only", False))
    if args.match_pdf and args.match_pdf.strip():
        match_pdf_path = Path(args.match_pdf.strip()).expanduser().resolve()
        if not match_pdf_path.exists():
            print(f"[ERROR] --match-pdf file not found: {match_pdf_path}", file=sys.stderr)
            return 1
        if not match_metadata_only:
            args.native_structure = True
            args.patch_header = True
        else:
            args.patch_header = True  # убрать MuPDF из заголовка

    input_path = Path(args.input_pdf).expanduser().resolve()
    output_path = Path(args.output_pdf).expanduser().resolve()

    replacements = {k: list(v) for k, v in REPLACEMENTS.items()}
    # Мягкий перенос -> обычный дефис для совпадения с Oracle/AM
    if match_pdf_path:
        replacements.setdefault("\u00ad", []).insert(0, "-")
    if args.replace:
        cli_replacements = parse_replacements_arg(args.replace)
        for old, news in cli_replacements.items():
            replacements.setdefault(old, []).extend(news)
    if args.replacements_json:
        repl_path = Path(args.replacements_json).expanduser().resolve()
        if not repl_path.exists():
            raise FileNotFoundError(f"Replacements JSON not found: {repl_path}")
        loaded = json.loads(repl_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Replacements JSON must be an object: {\"old\": \"new\"}")
        for old_raw, new_raw in loaded.items():
            old = str(old_raw)
            if isinstance(new_raw, list):
                replacements.setdefault(old, []).extend(str(v) for v in new_raw)
            else:
                replacements.setdefault(old, []).append(str(new_raw))

    donor_style: Optional[SpanStyle] = None
    if args.donor_pdf:
        donor_path = Path(args.donor_pdf).expanduser().resolve()
        donor_style = extract_donor_style(donor_path, args.donor_text)
        if donor_style is None:
            print(
                "[WARN] Donor style not found. Falling back to source span styles.",
                file=sys.stderr,
            )

    forced_font_alias = "forced_tahoma"
    forced_fontfile: Optional[str] = None
    if args.preserve_font_metrics:
        print(
            "[INFO] preserve-font-metrics is on: no global forced font.",
            file=sys.stderr,
        )
    else:
        forced_fontfile = resolve_fontfile(TAHOMA_CANDIDATES)
        if not forced_fontfile:
            forced_fontfile = resolve_fontfile(ARIAL_CANDIDATES)
            forced_font_alias = "fallback_arial"
            if forced_fontfile:
                print(
                    "[WARN] Tahoma not found. Using Arial fallback for all text.",
                    file=sys.stderr,
                )
            else:
                print(
                    "[WARN] Neither Tahoma nor Arial found. Falling back to builtin fonts.",
                    file=sys.stderr,
                )

    # Font alias: name to show in PDF resources (when using Tahoma fallback).
    font_alias_override: Optional[str] = None
    if args.font_alias:
        font_alias_override = args.font_alias.strip()
        if font_alias_override:
            forced_font_alias = font_alias_override
    elif args.font_alias_from_input:
        detected = get_first_font_name_from_pdf(input_path)
        if detected:
            font_alias_override = detected
            forced_font_alias = detected
            print(f"[INFO] Font alias from input: {detected}", file=sys.stderr)
        else:
            print("[WARN] Could not detect font name from input, using default.", file=sys.stderr)

    # Unicode fallback is always prepared to keep Cyrillic visible,
    # even when preserve-font-metrics is enabled.
    unicode_font_alias = forced_font_alias if font_alias_override else "unicode_fallback"
    if args.native_structure:
        forced_font_alias = "G1"
        unicode_font_alias = "G1"
    unicode_fontfile = resolve_fontfile(TAHOMA_CANDIDATES) or resolve_fontfile(
        ARIAL_CANDIDATES
    )
    if not unicode_fontfile:
        print(
            "[WARN] Unicode fallback font (Tahoma/Arial) not found. "
            "Non-ASCII text may be missing.",
            file=sys.stderr,
        )

    try:
        font_note = None
        if (
            not args.no_font_keywords_note
            and font_alias_override
            and (forced_fontfile or unicode_fontfile)
        ):
            font_note = f"Font display name: {font_alias_override} (glyphs from Tahoma)"

        build_clean_room_pdf(
            input_path,
            output_path,
            replacements,
            donor_style,
            forced_fontfile,
            forced_font_alias,
            unicode_fontfile,
            unicode_font_alias,
            optimize_size=not args.no_size_optimize,
            numbers_grow_left=args.numbers_grow_left,
            match_input_pdf_version=args.match_input_pdf_version,
            copy_input_metadata=args.copy_input_metadata,
            use_source_fonts=args.use_source_fonts,
            font_alias_note=font_note,
            native_structure=args.native_structure,
            creation_date_override=args.creation_date.strip() or None,
            mod_date_override=args.mod_date.strip() or None,
            metadata_source_path=match_pdf_path,
            version_source_path=match_pdf_path,
            metadata_only_will_patch=bool(match_pdf_path and match_metadata_only),
            allow_subset_when_metadata_patch=bool(getattr(args, "subset_fonts", False)),
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print(f"[OK] Clean PDF created: {output_path}")

    if args.native_structure:
        patch_input = match_pdf_path if match_pdf_path else (input_path if getattr(args, "ios_match", False) else None)
        if _apply_native_structure_patch(output_path, ios_match_input=patch_input):
            msg = "[OK] Native structure patch applied (Im1/2/3, G1, Producer, no Metadata)."
            if patch_input:
                msg += f" BaseFont/Producer/DocID from {'match PDF' if match_pdf_path else 'input'}."
            print(msg)

    # Metadata patch first, then shell inject (AM structure), then header, then metadata again
    if match_pdf_path and match_metadata_only:
        use_random_id = bool(getattr(args, "random_document_id", False))
        producer_from = input_path if getattr(args, "keep_input_producer", False) else None
        if _apply_match_metadata_patch(
            output_path,
            match_pdf_path,
            use_random_document_id=use_random_id,
            producer_from_input=producer_from,
        ):
            print("[OK] Match metadata patch: Producer, Document ID, BaseFont Tahoma.")
        keep_am_fonts = bool(getattr(args, "native_match", False))
        if not getattr(args, "no_am_shell_inject", False) and _apply_am_shell_inject(
            output_path,
            match_pdf_path,
            keep_am_fonts=keep_am_fonts,
            replacements=replacements if keep_am_fonts else None,
        ):
            msg = "[OK] AM shell inject: structure matched (16 obj, 6 streams)."
            if keep_am_fonts:
                msg += " Native mode: AM font/ToUnicode kept."
            print(msg)

    if args.patch_header:
        try:
            script_dir = Path(__file__).resolve().parent
            if str(script_dir) not in sys.path:
                sys.path.insert(0, str(script_dir))
            from patch_pdf_header import patch_file

            if patch_file(output_path):
                print("[OK] Header patched: MuPDF comment removed.")
            else:
                print("[INFO] No MuPDF header found (skip patch).")
        except ImportError as e:
            print(f"[WARN] --patch-header: cannot import patch_pdf_header: {e}", file=sys.stderr)

    # Re-apply metadata patch after header (header can overwrite Catalog region in tight PDFs)
    if match_pdf_path and match_metadata_only:
        _apply_match_metadata_patch(
            output_path,
            match_pdf_path,
            use_random_document_id=use_random_id,
            producer_from_input=producer_from,
        )

    if replacements:
        print("[INFO] Text replacements applied during reconstruction.")
    if donor_style:
        print("[INFO] Donor style was applied to replaced text.")
    else:
        print("[INFO] No donor style applied.")
    if forced_fontfile:
        print(f"[INFO] Forced font file: {forced_fontfile}")
    else:
        print("[INFO] Forced font file: none (preserve metrics mode)")
    print(f"[INFO] Preserve source fonts: {'on' if args.use_source_fonts else 'off'}")
    if font_alias_override:
        print(f"[INFO] Font alias in PDF: {font_alias_override}")
    if unicode_fontfile:
        print(f"[INFO] Unicode fallback font file: {unicode_fontfile}")
    print(f"[INFO] Size optimization: {'on' if not args.no_size_optimize else 'off'}")
    print(f"[INFO] Numbers grow left: {'on' if args.numbers_grow_left else 'off'}")
    check = forensic_self_check(output_path)
    print(
        f"[INFO] Forensic self-check: startxref={check['startxref_count']}, "
        f"%%EOF={check['eof_count']}"
    )
    if check["passed"]:
        print("[INFO] Forensic self-check status: PASS")
    else:
        print("[WARN] Forensic self-check status: WARN")
        if check["has_incremental_chain"]:
            print("[WARN] Incremental chain markers detected (>1 startxref or %%EOF).")
        if check["suspicious_hits"]:
            print(f"[WARN] Suspicious object tokens: {check['suspicious_hits']}")
        if check["fingerprint_hits"]:
            print(f"[WARN] Editor fingerprint tokens: {check['fingerprint_hits']}")
        if check["metadata_warnings"]:
            print(f"[WARN] Metadata warnings: {check['metadata_warnings']}")
        if args.strict_forensic:
            return 3
    print("[INFO] External forensic check examples:")
    print(f"       qpdf --check \"{output_path}\"")
    print(
        "       python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"b = Path({output_path.name!r}).read_bytes()\n"
        "print('%%EOF:', b.count(b'%%EOF'))\n"
        "print('startxref:', b.count(b'startxref'))\n"
        "PY"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
