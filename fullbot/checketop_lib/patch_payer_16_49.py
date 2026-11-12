#!/usr/bin/env python3
"""
Замена ТОЛЬКО имени плательщика в donors/07-03-26_16-49.pdf.
Остальные поля (получатель, сумма) не трогаются.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
X_MIN_RIGHT = 100
PAYER_Y = 227.25
PAYER_YTOL = 0.5
KERNING = "-16.66667"


def parse_tounicode(data: bytes) -> dict[int, str]:
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
            return uni_to_cid
        if b"beginbfrange" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
                for i in range(s2 - s1 + 1):
                    uni_to_cid[d + i] = f"{s1 + i:04X}"
            return uni_to_cid
    return {}


def encode_to_cids(text: str, uni_to_cid: dict) -> list[str] | None:
    cids = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        cids.append(uni_to_cid[cp])
    return cids


def build_tj_literal(cids: list[str], kern: str) -> bytes:
    kern_b = kern.encode()
    parts = []
    for cid_hex in cids:
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        if l == 0x28:
            s = b"(\\x%02x\\()" % h
        elif l == 0x29:
            s = b"(\\x%02x\\))" % h
        elif h == 0 and 0x20 <= l <= 0x7E and l not in (0x28, 0x29, 0x5C):
            s = bytes([0x28, l, 0x29])
        else:
            s = b"(\\x%02x\\x%02x)" % (h, l)
        parts.append(s + b"-" + kern_b + b" ")
    return b"[" + b"".join(parts) + b"]"


def patch_payer_only(donor_path: Path, out_path: Path, payer: str) -> bool:
    """Замена только поля плательщика на y=227.25."""
    data = bytearray(donor_path.read_bytes())
    uni_to_cid = parse_tounicode(data)
    if not uni_to_cid:
        print("[ERROR] ToUnicode не найден", file=sys.stderr)
        return False

    payer_cids = encode_to_cids(payer, uni_to_cid)
    if not payer_cids:
        print(f"[ERROR] Символы для '{payer}' отсутствуют в CMap", file=sys.stderr)
        return False

    new_payer_tj = build_tj_literal(payer_cids, KERNING)
    modified = False

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
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        def replacer(match):
            nonlocal modified
            x, y = float(match.group(2)), float(match.group(4))
            if x < X_MIN_RIGHT:
                return match.group(0)
            if abs(y - PAYER_Y) <= PAYER_YTOL:
                modified = True
                prefix = match.group(1) + match.group(2) + match.group(3) + match.group(4) + match.group(5) + match.group(6)
                return prefix + new_payer_tj + b" TJ"
            return match.group(0)

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)\[([^\]]*)\]\s*TJ'
        new_dec = re.sub(pat, replacer, dec)
        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)

            data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
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

    if not modified:
        print("[ERROR] Блок плательщика не найден", file=sys.stderr)
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    print(f"[OK] Сохранено: {out_path} ({len(data)} bytes)")
    return True


def main() -> int:
    donor = ROOT / "donors" / "07-03-26_16-49.pdf"
    out = donor.parent / "07-03-26_16-49.pdf"  # перезапись оригинала
    payer = "Евгений Александрович E."
    if len(sys.argv) >= 2:
        payer = sys.argv[1]
    if len(sys.argv) >= 3:
        out = Path(sys.argv[2]).expanduser().resolve()

    if not donor.exists():
        print(f"[ERROR] Файл не найден: {donor}", file=sys.stderr)
        return 1
    return 0 if patch_payer_only(donor, out, payer) else 1


if __name__ == "__main__":
    sys.exit(main())
