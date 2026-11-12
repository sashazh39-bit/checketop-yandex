#!/usr/bin/env python3
"""Патч ФИО в выписке через замену в content stream — без добавления шрифтов.

Заменяет текст напрямую в сжатом content stream (CID-кодировка). Шрифты не
добавляются — используется существующий в PDF. Размер файла меняется минимально
(на несколько байт из‑за пересжатия потока).

Использование:
  python3 patch_vyписка_fio_stream.py input.pdf output.pdf
  python3 patch_vyписка_fio_stream.py input.pdf output.pdf "Вислоусов Демид Андреевич" "Вислосусов Демид Андреевич"
"""
from pathlib import Path
import re
import zlib


def _parse_cid_to_uni(pdf_bytes: bytes) -> dict[int, int]:
    """Извлечь CID→Unicode из ToUnicode (beginbfrange)."""
    cid_to_uni = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_bytes):
            continue
        try:
            dec = zlib.decompress(pdf_bytes[stream_start : stream_start + stream_len])
        except zlib.error:
            continue
        if b"beginbfrange" not in dec:
            continue
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid_s, cid_e, uni_s = int(mm.group(1), 16), int(mm.group(2), 16), int(mm.group(3), 16)
            for i in range(cid_e - cid_s + 1):
                cid_to_uni[cid_s + i] = uni_s + i
        return cid_to_uni
    return cid_to_uni


def _encode_name_cid_bytes(name: str, cid_to_uni: dict[int, int], escape_5c: bool = False) -> bytes | None:
    """Кодировать имя в байты CID (2 байта на символ, big-endian).
    escape_5c: если True, байт 0x5c удваивается (как в некоторых PDF).
    """
    uni_to_cid = {v: k for k, v in cid_to_uni.items()}
    result = bytearray()
    for c in name:
        cp = ord(c)
        if cp not in uni_to_cid:
            return None
        cid = uni_to_cid[cp]
        hi, lo = (cid >> 8) & 0xFF, cid & 0xFF
        result.append(hi)
        result.append(lo)
        if escape_5c and lo == 0x5C:
            result.append(0x5C)
    return bytes(result)


def patch_fio_in_stream(
    pdf_path: Path,
    old_name: str,
    new_name: str,
    out_path: Path | None = None,
    escape_5c: bool = True,
    allow_len_mismatch: bool = False,
) -> bool:
    """
    Заменить ФИО в content stream.
    При одинаковой длине — размер файла почти не меняется.
    allow_len_mismatch: разрешить разную длину (размер изменится на ~2 байта на символ разницы).
    escape_5c: в выписках ВТБ байт 0x5c (backslash) в CID удваивается.
    """
    data = bytearray(pdf_path.read_bytes())
    cid_to_uni = _parse_cid_to_uni(data)
    if not cid_to_uni:
        return False

    old_bytes = _encode_name_cid_bytes(old_name, cid_to_uni, escape_5c=escape_5c)
    new_bytes = _encode_name_cid_bytes(new_name, cid_to_uni, escape_5c=escape_5c)
    if not old_bytes or not new_bytes:
        return False
    if not allow_len_mismatch and len(old_bytes) != len(new_bytes):
        return False

    modified = False
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or old_bytes not in dec:
            continue

        new_dec = dec.replace(old_bytes, new_bytes, 1)
        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        delta = len(new_raw) - stream_len

        data = bytearray(data[:stream_start]) + bytearray(new_raw) + bytearray(data[stream_start + stream_len :])
        len_start = m.start(2)
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) <= len(old_len_str):
            data[len_start : len_start + len(old_len_str)] = new_len_str.ljust(len(old_len_str))
        else:
            data[len_start : len_start + len(old_len_str)] = new_len_str[: len(old_len_str)]
        delta += len(new_len_str) - len(old_len_str)

        # Update xref
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data = data[: xref_m.start(3)] + bytes(entries) + data[xref_m.end(3) :]

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            new_pos_str = str(old_pos + delta).encode()
            data = data[:pos] + new_pos_str + data[pos + len(str(old_pos)) :]

        modified = True
        break

    if modified and out_path:
        out_path.write_bytes(data)
    return modified


def main():
    import sys
    inp = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "/Users/aleksandrzerebatav/Downloads/Выписка_по_счёту_№408178**********9414_с_12_03_2026_по_13_03_2026.pdf"
    )
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_fio.pdf")
    # По умолчанию: полная замена (размер изменится на ~2 байта)
    old_name = sys.argv[3] if len(sys.argv) > 3 else "Вислоусов Демид Андреевич"
    new_name = sys.argv[4] if len(sys.argv) > 4 else "Вислосусов Демид Андреевич"

    if patch_fio_in_stream(inp, old_name, new_name, out, allow_len_mismatch=True):
        print(f"[OK] Сохранено: {out}")
        orig_sz = inp.stat().st_size
        new_sz = out.stat().st_size
        print(f"     Размер: {orig_sz} → {new_sz} байт")
    else:
        print("[WARN] Замена не применена (текст не найден или CMap не поддерживает символы)")
        sys.exit(1)


if __name__ == "__main__":
    main()
