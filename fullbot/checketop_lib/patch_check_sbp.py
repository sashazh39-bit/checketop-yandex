#!/usr/bin/env python3
"""
CID-патч чека «Исходящий перевод СБП» (check layout).
Замена в content streams — без новых шрифтов, размер ~9 KB.

Использование:
  python3 patch_check_sbp.py "donors/check (1) (1).pdf" "чеки 08.03/чек.pdf" \\
    --payer "Лукс Максер К." --amount "20 000 ₽"
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
X_MIN_RIGHT = 100

# Координаты полей check layout (Исходящий перевод СБП)
FIELDS = {
    "payer": {"y": 227.25, "ytol": 0.5, "kerning": "-16.66667"},
    "recipient": {"y": 203.25, "ytol": 0.5, "kerning": "-16.66667"},
    "amount": {"y": 72.37, "ytol": 0.5, "kerning": "-11.11111"},
}


def parse_tounicode(data: bytes) -> dict[int, str]:
    """Парсинг ToUnicode CMap."""
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
    """Текст → список CID hex. Пробел: 32 или 160."""
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
    """Собрать TJ в literal формате: (\\xHH\\xLL)-kern."""
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


def patch_check(
    donor_path: Path,
    out_path: Path,
    payer: str,
    amount: str,
    cmap_sources: list[Path] | None = None,
) -> bool:
    """Патч payer и amount по y-координатам. cmap_sources: PDF с CMap (Л, М — из разных доноров)."""
    data = bytearray(donor_path.read_bytes())
    uni_to_cid = parse_tounicode(data)
    for src in cmap_sources or []:
        if src.exists():
            for k, v in parse_tounicode(src.read_bytes()).items():
                if k not in uni_to_cid:
                    uni_to_cid[k] = v
    if not uni_to_cid:
        print("[ERROR] ToUnicode не найден", file=sys.stderr)
        return False

    payer_cids = encode_to_cids(payer, uni_to_cid)
    amount_cids = encode_to_cids(amount, uni_to_cid)
    if not payer_cids:
        print(f"[ERROR] Символы для '{payer}' отсутствуют в CMap", file=sys.stderr)
        return False
    if not amount_cids:
        print(f"[ERROR] Символы для '{amount}' отсутствуют в CMap", file=sys.stderr)
        return False

    new_payer_tj = build_tj_literal(payer_cids, FIELDS["payer"]["kerning"])
    new_recipient_tj = build_tj_literal(payer_cids, FIELDS["recipient"]["kerning"])
    new_amount_tj = build_tj_literal(amount_cids, FIELDS["amount"]["kerning"])

    repl_by_y: dict[float, bytes] = {
        FIELDS["payer"]["y"]: new_payer_tj,
        FIELDS["recipient"]["y"]: new_recipient_tj,
        FIELDS["amount"]["y"]: new_amount_tj,
    }

    mods = []
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
            x, y = float(match.group(2)), float(match.group(4))
            if x < X_MIN_RIGHT:
                return match.group(0)
            for target_y, new_tj in repl_by_y.items():
                if abs(y - target_y) <= 0.5:
                    prefix = match.group(1) + match.group(2) + match.group(3) + match.group(4) + match.group(5) + match.group(6)
                    return prefix + new_tj + b" TJ"
            return match.group(0)

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)\[([^\]]*)\]\s*TJ'
        new_dec = re.sub(pat, replacer, dec)
        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Не найдены блоки для замены", file=sys.stderr)
        return False

    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
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

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    print(f"[OK] Сохранено: {out_path} ({len(data)} bytes)")
    return True


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="CID-патч чека СБП (check layout)")
    parser.add_argument("donor", help="Donor PDF (check format)")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--payer", "-p", default="Лукс Максер К.", help="Имя плательщика")
    parser.add_argument("--amount", "-a", default="20 000 ₽", help="Сумма")
    parser.add_argument("--cmap-source", "-c", action="append", default=[], help="PDF с CMap (можно несколько: Л из одного, М из другого)")
    args = parser.parse_args()

    donor = Path(args.donor).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    cmap_sources = [Path(p).expanduser().resolve() for p in (args.cmap_source or [])]
    if not donor.exists():
        print(f"[ERROR] Donor не найден: {donor}", file=sys.stderr)
        return 1

    if patch_check(donor, out, args.payer, args.amount, cmap_sources=cmap_sources):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
