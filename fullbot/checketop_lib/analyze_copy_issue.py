#!/usr/bin/env python3
"""Глубокий анализ: почему не копируется текст в check 3a_compact/deepcopy.

Извлекает:
1. Все CIDs из content streams (TJ — literal и hex)
2. CID→Unicode из ToUnicode (beginbfchar + beginbfrange)
3. CIDs без маппинга (проблема копирования)
"""
from __future__ import annotations

import re
import zlib
import sys
from pathlib import Path


def _extract_cids_from_content(pdf_data: bytes) -> set[int]:
    """Все CIDs из content streams. Поддержка literal (\\xHH\\xLL) и hex <HHHH>."""
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

        def _unescape(s: bytes) -> bytes:
            out = bytearray()
            i = 0
            while i < len(s):
                if s[i] == 0x5C and i + 1 < len(s):
                    out.append(s[i + 1])
                    i += 2
                    continue
                out.append(s[i])
                i += 1
            return bytes(out)

        # Literal: (byte1)(byte2) = CID
        for part, kern in re.findall(rb"\((.*?)\)|(-?\d+(?:\.\d+)?)", dec):
            if part:
                vals = list(_unescape(part))
                for j in range(0, len(vals) - 1, 2):
                    cid = (vals[j] << 8) + vals[j + 1]
                    used.add(cid)

        # Hex: <HHHH> или <HH><HH>
        for mm in re.finditer(rb"<([0-9A-Fa-f]{2,4})>", dec):
            h = mm.group(1).decode("ascii")
            if len(h) == 4:
                used.add(int(h, 16))
            elif len(h) == 2:
                # Может быть пара <HH><LL> для одного CID
                pass  # обработаем в контексте
    return used


def _extract_cid_to_uni(pdf_data: bytes) -> dict[int, int]:
    """CID → Unicode из ToUnicode (beginbfchar + beginbfrange)."""
    cid_to_uni: dict[int, int] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_data):
            continue
        try:
            dec = zlib.decompress(bytes(pdf_data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"begincmap" not in dec and b"beginbfchar" not in dec and b"beginbfrange" not in dec:
            continue
        # beginbfchar
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid = int(mm.group(1).decode(), 16)
            uni = int(mm.group(2).decode(), 16)
            cid_to_uni[cid] = uni
        # beginbfrange: <srcStart><srcEnd><destStart>
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
            if s2 >= s1:
                for i in range(s2 - s1 + 1):
                    cid_to_uni[s1 + i] = d + i
    return cid_to_uni


def analyze(pdf_path: Path) -> dict:
    """Полный анализ PDF: content CIDs vs ToUnicode."""
    data = pdf_path.read_bytes()
    content_cids = _extract_cids_from_content(data)
    cid_to_uni = _extract_cid_to_uni(data)
    missing = content_cids - set(cid_to_uni.keys())
    mapped = content_cids & set(cid_to_uni.keys())
    return {
        "content_cids": content_cids,
        "cid_to_uni": cid_to_uni,
        "missing": missing,
        "mapped": mapped,
        "all_mapped": len(missing) == 0,
    }


def main():
    base = Path(__file__).parent
    checks = [
        base / "Лучший чек" / "check 3a_compact.pdf",
        base / "Лучший чек" / "check 3a_deepcopy.pdf",
        base / "Лучший чек" / "check 3a_add.pdf",
    ]
    for p in checks:
        if not p.exists():
            continue
        print(f"\n=== {p.name} ===")
        r = analyze(p)
        print(f"CIDs в content: {len(r['content_cids'])}")
        print(f"CIDs в ToUnicode: {len(r['cid_to_uni'])}")
        print(f"CIDs БЕЗ маппинга (не копируются): {len(r['missing'])}")
        if r["missing"]:
            for cid in sorted(r["missing"])[:50]:
                print(f"  CID 0x{cid:04X} ({cid}) — нет Unicode")
            if len(r["missing"]) > 50:
                print(f"  ... и ещё {len(r['missing'])-50}")
        if r["all_mapped"]:
            print("✅ Все CIDs имеют маппинг — копирование должно работать")
        else:
            print("❌ Есть CIDs без маппинга — текст не копируется")
        # Примеры маппленных
        sample = sorted(r["mapped"])[:10]
        if sample:
            print("Примеры маппленных:", [(f"0x{c:04X}", chr(r["cid_to_uni"][c]) if 0x20 <= r["cid_to_uni"][c] < 0xE000 else f"U+{r['cid_to_uni'][c]:04X}") for c in sample])
    return 0


if __name__ == "__main__":
    sys.exit(main())
