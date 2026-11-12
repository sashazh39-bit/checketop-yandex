#!/usr/bin/env python3
"""Extract exact TJ block bytes for payer name from PDF."""
import re
import zlib
from pathlib import Path


def main():
    folder = Path("/Users/aleksandrzerebatav/Desktop/чекетоп/чеки 07.03")
    pdf_path = next((f for f in folder.iterdir() if "13-02-26" in f.name and "копия" in f.name), None)
    if not pdf_path:
        pdf_path = folder / "13-02-26_20-29 — копия.pdf"
    if not pdf_path.exists():
        print("PDF не найден:", pdf_path)
        return

    data = pdf_path.read_bytes()
    found = False

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        # Find all TJ blocks with x>100 (right column) and y ≈ 348.75 or 227.25
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            if x > 100 and (abs(y - 348.75) <= 2 or abs(y - 227.25) <= 2):
                tj_block = mm.group(7)
                inner_match = re.match(rb'\[(.+)\]\s*TJ', tj_block, re.DOTALL)
                inner = inner_match.group(1) if inner_match else b""

                print("=== Payer TJ at x={}, y={} ===".format(x, y))
                print("\nInner content (inside brackets []):")
                print("Hex:", inner.hex())
                print("Repr:", repr(inner))
                print("\nFull TJ block:")
                print("Hex:", tj_block.hex())
                print("Repr:", repr(tj_block))
                print()
                found = True

    if not found:
        print("No payer TJ block found at y≈227.25 or 348.75, x>100")


if __name__ == "__main__":
    main()
