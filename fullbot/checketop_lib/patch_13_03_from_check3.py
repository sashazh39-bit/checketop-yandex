#!/usr/bin/env python3
"""Внедрить буквы из check (3).pdf в 13-03-26, заменить оба ФИО на «Филипп Юсаев Ч.», телефон — рандом.

Сохранены: структура 13-03-26, метаданные, mediabox.
Подмена: font + ToUnicode + /W из check(3), перекодирование content → CIDs check(3).

Использование:
  python3 patch_13_03_from_check3.py
  python3 patch_13_03_from_check3.py -o receipt.pdf
"""
from __future__ import annotations

import re
import random
import sys
import zlib
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
TARGET_PDF = Path("/Users/aleksandrzerebatav/Downloads/13-03-26_00-00 16.pdf")
DONOR_PDF = BASE / "база_чеков" / "vtb" / "СБП" / "check (3).pdf"

PAYER = RECIPIENT = "Филипп Юсаев Ч."


def _parse_tounicode(data: bytes) -> tuple[dict[int, str], dict[str, int]]:
    """(uni→cid, cid→uni)."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end() : m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode().upper(), 16)
                uni_to_cid[uni] = cid
            break
        if b"beginbfrange" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
                for i in range(s2 - s1 + 1):
                    uni_to_cid[d + i] = f"{s1 + i:04X}"
            break
    cid_to_uni = {v: k for k, v in uni_to_cid.items()}
    return uni_to_cid, cid_to_uni


def _parse_cids_from_tj(tj_inner: bytes) -> tuple[list[str], str | None]:
    """Извлечь CIDs и kerning из TJ. Использует sbp_full_toolkit."""
    from sbp_full_toolkit import parse_cids_from_tj as _parse
    cids, _, kerning = _parse(tj_inner)
    return cids, kerning


def _build_tj_from_cids(cids: list[str], kern: str = "-16.66667") -> bytes:
    """CID hex → TJ bytes."""
    kern_b = kern.encode()

    def esc(b: int) -> bytes:
        if b in (0x28, 0x29, 0x5C):
            return b"\\" + bytes([b])
        return bytes([b])

    parts = []
    for i, cid_hex in enumerate(cids):
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        parts.append(b"(" + esc(h) + esc(l) + b")")
        if i < len(cids) - 1:
            parts.append(kern_b + b" ")
    return b"[" + b"".join(parts) + b"]"


def _reencode_tj(tj_inner: bytes, cid_to_uni: dict[str, int], uni_to_cid: dict[int, str]) -> bytes | None:
    """Перекодировать TJ: старые CIDs → новые CIDs через Unicode."""
    cids_old, kern = _parse_cids_from_tj(tj_inner)
    if not cids_old:
        return None
    kern = kern or "-16.66667"
    new_cids = []
    for cid in cids_old:
        uni = cid_to_uni.get(cid)
        if uni is None:
            return None
        new_cid = uni_to_cid.get(uni)
        if new_cid is None:
            return None
        new_cids.append(new_cid)
    return _build_tj_from_cids(new_cids, kern)


def _copy_w_array(tgt_data: bytearray, src_data: bytes) -> tuple[int, int]:
    """Заменить /W массив в target на /W из source. Возвращает (delta, edit_pos)."""
    w_m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", src_data, re.DOTALL)
    if not w_m:
        return 0, 0
    src_w = w_m.group(0)
    tgt_m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", tgt_data, re.DOTALL)
    if not tgt_m:
        return 0, 0
    pos, end = tgt_m.start(0), tgt_m.end(0)
    delta = len(src_w) - (end - pos)
    tgt_data[pos:end] = src_w
    return delta, pos


def _random_phone() -> str:
    """+7 (9XX) XXX-XX-XX."""
    a = random.randint(900, 999)
    b = random.randint(100, 999)
    c = random.randint(10, 99)
    d = random.randint(10, 99)
    return f"+7 ({a}) {b}-{c}-{d}"


def main() -> int:
    import argparse
    from copy_font_cmap import copy_font_cmap
    from vtb_patch_from_config import patch_from_values, _parse_cid_widths
    from vtb_test_generator import update_creation_date
    from reflow_after_font_swap import reflow_pdf

    ap = argparse.ArgumentParser(description="ФИО из check(3) в 13-03-26, структура сохранена")
    ap.add_argument("-o", "--output", default="receipt_filipp_yusaev.pdf", help="Выходной PDF")
    ap.add_argument("--target", default=None, help="База PDF (по умолчанию 13.pdf или 16.pdf)")
    args = ap.parse_args()

    target_path = Path(args.target or TARGET_PDF).expanduser().resolve()
    donor_path = Path(DONOR_PDF).expanduser().resolve()
    out_path = Path(args.output).resolve()

    if not target_path.exists():
        print(f"[ERROR] Не найден: {target_path}", file=sys.stderr)
        return 1
    if not donor_path.exists():
        print(f"[ERROR] Не найден: {donor_path}", file=sys.stderr)
        return 1

    tgt_data = target_path.read_bytes()
    src_data = donor_path.read_bytes()

    _, cid_to_uni_tgt = _parse_tounicode(tgt_data)
    uni_to_cid_src, _ = _parse_tounicode(src_data)

    if not cid_to_uni_tgt or not uni_to_cid_src:
        print("[ERROR] ToUnicode не найден в одном из PDF", file=sys.stderr)
        return 1

    # 1. Копируем font + ToUnicode из check(3)
    temp = BASE / ".temp_patch_13_03.pdf"
    if not copy_font_cmap(donor_path, target_path, temp):
        return 1

    data = bytearray(temp.read_bytes())

    # 2. Копируем /W (inline в CIDFont)
    w_delta, w_pos = _copy_w_array(data, src_data)
    if w_delta != 0 and w_pos > 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > w_pos:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + w_delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and w_pos < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            data[p : p + len(str(old_p))] = str(old_p + w_delta).encode()

    # 3. Перекодируем content streams — с конца, чтобы не сбивать offset
    stream_matches = list(re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL))
    for m in reversed(stream_matches):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        pat = rb"(1\s+0\s+0\s+1\s+[\d.]+\s+[\d.]+\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\])\s*TJ"
        pat2 = rb"(1\s+0\s+0\s+1\s+[\d.]+\s+[\d.]+\s+Tm\s*\r?\n)(\[([^\]]*)\])\s*TJ"
        new_dec = dec
        for pat_use in (pat, pat2):
            for mt in list(re.finditer(pat_use, dec)):
                prefix = mt.group(1)
                between = mt.group(2) if mt.lastindex >= 2 else b""
                tj_inner = mt.group(4) if mt.lastindex >= 4 else mt.group(3)
                reencoded = _reencode_tj(tj_inner, cid_to_uni_tgt, uni_to_cid_src)
                if reencoded and mt.group(0) in new_dec:
                    repl = prefix + between + reencoded + b" TJ"
                    new_dec = new_dec.replace(mt.group(0), repl, 1)

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            data = data[:stream_start] + bytearray(new_raw) + data[stream_start + stream_len :]
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
                startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
                if startxref_m and stream_start < int(startxref_m.group(1)):
                    pos = startxref_m.start(1)
                    old_pos = int(startxref_m.group(1))
                    data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

    temp.write_bytes(data)

    # 4. Reflow tm_x под новый /W
    cid_widths = _parse_cid_widths(bytes(data))
    data = bytearray(reflow_pdf(data, temp, cid_widths))
    temp.write_bytes(data)

    # 5. Патч: payer, recipient, phone
    phone = _random_phone()
    date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
    meta_date = datetime.now().strftime("D:%Y%m%d%H%M00+03'00'")

    try:
        out_bytes = patch_from_values(
            data,
            temp,
            date_str=date_str,
            payer=PAYER,
            recipient=RECIPIENT,
            phone=phone,
            amount=None,
            keep_metadata=True,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        temp.unlink(missing_ok=True)
        return 1

    out_arr = bytearray(out_bytes)

    # Дозамена получателя и телефона: заменяем по точному совпадению TJ-последовательности
    OLD_RECIPIENT = "Роман Андреевич В."
    old_phone = None
    try:
        import fitz as _fitz
        doc = _fitz.open(str(target_path))
        text = doc[0].get_text()
        doc.close()
        import re as _re
        m = _re.search(r"\+7\s*\([0-9]{3}\)\s*[0-9]{3}[‑\-][0-9]{2}[‑\-][0-9]{2}", text)
        if m:
            old_phone = m.group(0).replace("\u2011", "-").replace("‑", "-")
    except Exception:
        pass

    try:
        from vtb_patch_from_config import build_text_tj
        uni_to_cid = {k: v for k, v in uni_to_cid_src.items()}
        new_rec_tj = build_text_tj(RECIPIENT, wrap=True, uni_to_cid=uni_to_cid)
        new_rec_tj_21 = build_text_tj(RECIPIENT, kern="-21.42857", wrap=True, uni_to_cid=uni_to_cid)
        old_rec_tj = build_text_tj(OLD_RECIPIENT, wrap=True, uni_to_cid=uni_to_cid)
        old_rec_tj_21 = build_text_tj(OLD_RECIPIENT, kern="-21.42857", wrap=True, uni_to_cid=uni_to_cid)
        new_phone_tj = build_text_tj(phone, wrap=True, uni_to_cid=uni_to_cid) if phone else None
        old_phone_tj = build_text_tj(old_phone, wrap=True, uni_to_cid=uni_to_cid) if old_phone else None
    except Exception:
        new_rec_tj = old_rec_tj = new_rec_tj_21 = old_rec_tj_21 = new_phone_tj = old_phone_tj = None

    if new_rec_tj and old_rec_tj and new_rec_tj_21 and old_rec_tj_21:
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", out_arr, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(out_arr):
                continue
            try:
                dec = zlib.decompress(bytes(out_arr[stream_start : stream_start + stream_len]))
            except zlib.error:
                continue
            if b"BT" not in dec:
                continue
            old_rec_inner = old_rec_tj[1:-1] if old_rec_tj.startswith(b"[") else old_rec_tj
            new_rec_inner = new_rec_tj[1:-1] if new_rec_tj.startswith(b"[") else new_rec_tj
            new_dec = dec
            if old_rec_inner in new_dec:
                new_dec = new_dec.replace(b"[" + old_rec_inner + b"] TJ", b"[" + new_rec_inner + b"] TJ")
            old_rec_21_inner = old_rec_tj_21[1:-1] if old_rec_tj_21.startswith(b"[") else old_rec_tj_21
            new_rec_21_inner = new_rec_tj_21[1:-1] if new_rec_tj_21.startswith(b"[") else new_rec_tj_21
            if old_rec_21_inner in new_dec:
                new_dec = new_dec.replace(b"[" + old_rec_21_inner + b"] TJ", b"[" + new_rec_21_inner + b"] TJ")
            if new_dec != dec:
                new_raw = zlib.compress(new_dec, 9)
                delta = len(new_raw) - stream_len
                out_arr = out_arr[:stream_start] + bytearray(new_raw) + out_arr[stream_start + stream_len :]
                out_arr[len_num_start : len_num_start + len(str(stream_len))] = str(len(new_raw)).encode()
                if delta != 0:
                    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", out_arr)
                    if xref_m:
                        entries = bytearray(xref_m.group(3))
                        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                            offset = int(em.group(1))
                            if offset > stream_start:
                                entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                        out_arr[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
                break

    if new_phone_tj and old_phone_tj:
        old_ph_inner = old_phone_tj[1:-1] if old_phone_tj.startswith(b"[") else old_phone_tj
        new_ph_inner = new_phone_tj[1:-1] if new_phone_tj.startswith(b"[") else new_phone_tj
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", out_arr, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(out_arr):
                continue
            try:
                dec = zlib.decompress(bytes(out_arr[stream_start : stream_start + stream_len]))
            except zlib.error:
                continue
            if old_ph_inner in dec:
                new_dec = dec.replace(b"[" + old_ph_inner + b"] TJ", b"[" + new_ph_inner + b"] TJ")
                if new_dec != dec:
                    new_raw = zlib.compress(new_dec, 9)
                    delta = len(new_raw) - stream_len
                    out_arr = out_arr[:stream_start] + bytearray(new_raw) + out_arr[stream_start + stream_len :]
                    out_arr[len_num_start : len_num_start + len(str(stream_len))] = str(len(new_raw)).encode()
                    if delta != 0:
                        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", out_arr)
                        if xref_m:
                            entries = bytearray(xref_m.group(3))
                            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                                offset = int(em.group(1))
                                if offset > stream_start:
                                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                            out_arr[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
                    break
    update_creation_date(out_arr, meta_date)

    # Document ID: 1 символ
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', out_arr)
    if id_m:
        hex1 = id_m.group(1).decode()
        c = hex1[-1]
        chars = "0123456789ABCDEF"
        idx = chars.find(c.upper())
        new_c = chars[(idx + 1) % 16]
        new1 = hex1[:-1] + new_c
        out_arr[id_m.start(1) : id_m.end(1)] = new1.encode()
        out_arr[id_m.start(2) : id_m.end(2)] = new1.encode()

    try:
        import fitz
        doc = fitz.open(stream=bytes(out_arr), filetype="pdf")
        doc.save(str(out_path), garbage=4, deflate=True, pretty=False)
        doc.close()
    except Exception:
        out_path.write_bytes(out_arr)

    temp.unlink(missing_ok=True)

    print("✅ Готово:", out_path)
    print(f"   Плательщик / Получатель: {PAYER}")
    print(f"   Телефон: {phone}")
    print(f"   Дата: {date_str}")
    print(f"   Структура: font+ToUnicode+/W из check(3), layout — 13-03-26")
    return 0


if __name__ == "__main__":
    sys.exit(main())
