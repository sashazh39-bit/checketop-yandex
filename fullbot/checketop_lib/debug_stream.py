#!/usr/bin/env python3
"""Проверить формат текста в content stream выписки."""
import re
import zlib
from pathlib import Path

p = Path(__file__).parent / "Выписка_по_счёту.pdf"
if not p.exists():
    print("File not found:", p)
    exit(1)
data = p.read_bytes()
print("File size:", len(data))

# hex для "40817" = 4,0,8,1,7 -> CID <0034><0030><0038><0031><0037>
needle_hex = b"00340030003800310037"
needle_ascii = b"40817"

for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
    stream_len = int(m.group(2))
    stream_start = m.end()
    try:
        dec = zlib.decompress(data[stream_start : stream_start + stream_len])
    except zlib.error:
        continue
    if b"BT" not in dec:
        continue
    if needle_hex in dec:
        idx = dec.find(needle_hex)
        start = max(0, idx - 80)
        end = min(len(dec), idx + 120)
        print("Found hex format, context:")
        print(repr(dec[start:end]))
        print()
    if needle_ascii in dec:
        print("Found ASCII format!")
    # Показать первые Tj
    for tj in re.finditer(rb"\(([^)]*)\)\s*Tj", dec):
        s = tj.group(1)
        if len(s) > 10:
            print("Tj sample:", repr(s[:80]))
            break
    break
else:
    print("No matching stream found")
