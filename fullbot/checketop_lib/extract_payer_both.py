#!/usr/bin/env python3
"""Extract payer TJ block bytes from two PDFs and report inner content length."""
import re
import zlib
from pathlib import Path


def extract_payer_tj(pdf_path: Path, label: str) -> bytes | None:
    """Extract inner TJ content (inside [...]) for payer at y≈227.25 or 348.75, x>100."""
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return None

    data = pdf_path.read_bytes()

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

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            if x > 100 and (abs(y - 348.75) <= 2 or abs(y - 227.25) <= 2):
                tj_block = mm.group(7)
                inner_match = re.match(rb'\[(.+)\]\s*TJ', tj_block, re.DOTALL)
                inner = inner_match.group(1) if inner_match else b""
                return inner

    return None


def main():
    base = Path("/Users/aleksandrzerebatav/Desktop/чекетоп")
    pdf1 = base / "20-02-26_12-26.pdf"   # Илья Станиславович С. (OLD)
    folder2 = base / "чеки 07.03"
    pdf2 = next((f for f in folder2.iterdir() if "13-02-26" in f.name and "копия" in f.name), None)
    if pdf2 is None:
        pdf2 = folder2 / "13-02-26_20-29 — копия.pdf"  # fallback

    results = []

    for pdf_path, label, payer_name in [
        (pdf1, "OLD", "Илья Станиславович С."),
        (pdf2, "NEW", "Арман Мелсикович Б."),
    ]:
        print(f"\n{'='*60}")
        print(f"PDF: {pdf_path.name}")
        print(f"Payer: {payer_name} ({label})")
        print("=" * 60)

        inner = extract_payer_tj(pdf_path, label)
        if inner is None:
            print("No payer TJ block found at y≈227.25 or 348.75, x>100")
            results.append((label, None))
            continue

        length_bytes = len(inner)
        results.append((label, length_bytes))

        print("\nInner content (inside brackets []):")
        print("Hex:", inner.hex())
        print("Repr:", repr(inner))
        print(f"\nLENGTH IN BYTES: {length_bytes}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for label, length in results:
        if length is not None:
            print(f"{label}: {length} bytes")
        else:
            print(f"{label}: NOT FOUND")


if __name__ == "__main__":
    main()
