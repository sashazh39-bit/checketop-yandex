#!/usr/bin/env python3
"""Глубокое сравнение: метаданные, Info, xref, CreationDate, все следы."""
import re
import sys
from pathlib import Path


def extract_info_dict(data: bytes) -> dict:
    """Все поля из Info словаря."""
    out = {}
    for key in (b"Title", b"Author", b"Subject", b"Keywords", b"Creator", b"Producer",
                b"CreationDate", b"ModDate", b"Trapped"):
        m = re.search(key + rb"\s*\(([^)]*)\)", data)
        if m:
            out[key.decode()] = m.group(1).decode("latin-1", errors="replace")
        else:
            m2 = re.search(key + rb"\s*/(\w+)", data)
            if m2:
                out[key.decode()] = "/" + m2.group(1).decode()
    return out


def extract_trailer(data: bytes) -> dict:
    out = {}
    m = re.search(rb"trailer\s*<<(.*?)>>", data, re.DOTALL)
    if m:
        trailer = m.group(1)
        out["raw_len"] = len(trailer)
        for k in (b"Size", b"Root", b"Info", b"ID"):
            km = re.search(k + rb"\s*([^/)\s]+)", trailer)
            if km:
                out[k.decode()] = km.group(1).decode().strip()
    return out


def extract_xref(data: bytes) -> list:
    """Позиции и значения xref записей."""
    entries = []
    for m in re.finditer(rb"(\d{10})\s+(\d{5})\s+([nf])\s*", data):
        entries.append((int(m.group(1)), int(m.group(2)), m.group(3).decode()))
    return entries


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 diff_pdf_deep.py patched.pdf original.pdf")
        return 1
    p1, p2 = Path(sys.argv[1]), Path(sys.argv[2])
    d1, d2 = p1.read_bytes(), p2.read_bytes()

    print("=== МЕТАДАННЫЕ (Info) ===\n")
    info1 = extract_info_dict(d1)
    info2 = extract_info_dict(d2)
    all_keys = sorted(set(info1.keys()) | set(info2.keys()))
    for k in all_keys:
        v1, v2 = info1.get(k, "—"), info2.get(k, "—")
        eq = "✓" if v1 == v2 else "✗"
        print(f"  {k}: {eq}")
        if v1 != v2:
            print(f"    patched: {repr(v1)[:80]}")
            print(f"    orig:    {repr(v2)[:80]}")
    if not all_keys:
        print("  (Info dict не найден или пуст)")

    print("\n=== TRAILER ===\n")
    t1, t2 = extract_trailer(d1), extract_trailer(d2)
    for k in set(t1.keys()) | set(t2.keys()):
        if k == "raw_len":
            continue
        v1, v2 = t1.get(k), t2.get(k)
        eq = "✓" if v1 == v2 else "✗"
        print(f"  {k}: patched={v1} orig={v2} {eq}")

    print("\n=== XREF (первые 15 записей) ===\n")
    xref1 = re.findall(rb"(\d{10})\s+(\d{5})\s+[nf]", d1)
    xref2 = re.findall(rb"(\d{10})\s+(\d{5})\s+[nf]", d2)
    for i in range(min(15, max(len(xref1), len(xref2)))):
        e1 = (int(xref1[i][0]), int(xref1[i][1])) if i < len(xref1) else (None, None)
        e2 = (int(xref2[i][0]), int(xref2[i][1])) if i < len(xref2) else (None, None)
        eq = "✓" if e1 == e2 else "✗"
        print(f"  [{i}] offset: {e1[0]} vs {e2[0]} {eq}")

    print("\n=== STARTXREF ===\n")
    sx1 = re.search(rb"startxref\r?\n(\d+)", d1)
    sx2 = re.search(rb"startxref\r?\n(\d+)", d2)
    v1 = int(sx1.group(1)) if sx1 else None
    v2 = int(sx2.group(1)) if sx2 else None
    print(f"  patched: {v1}  orig: {v2}  {'✓' if v1 == v2 else '✗'}")

    print("\n=== /Length объектов (streams) ===\n")
    len1 = re.findall(rb"/Length\s+(\d+)", d1)
    len2 = re.findall(rb"/Length\s+(\d+)", d2)
    print(f"  patched: {len1}")
    print(f"  orig:    {len2}")

    # Байтовый diff — сколько байт отличаются
    print("\n=== РАЗМЕР ===\n")
    print(f"  patched: {len(d1)} bytes")
    print(f"  orig:    {len(d2)} bytes")
    print(f"  diff:    {len(d1) - len(d2)} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
