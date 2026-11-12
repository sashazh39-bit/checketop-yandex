#!/usr/bin/env python3
"""Extract text dict from Выписка_по_счёту.pdf using fitz (PyMuPDF)."""

import fitz  # PyMuPDF
from pathlib import Path

PDF_PATH = Path(__file__).parent / "Выписка_по_счёту.pdf"


def main():
    doc = fitz.open(PDF_PATH)
    text_dict = doc[0].get_text("dict")  # page 0

    # 1. All spans with bbox (x0,y0,x1,y1) and text
    print("=" * 80)
    print("1. ALL SPANS (bbox + text)")
    print("=" * 80)
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox", (0, 0, 0, 0))
                text = span.get("text", "")
                print(f"  bbox=({bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f})  text='{text}'")

    # 2. Identify y-ranges: "Дата формирования" (top) and "Операции по счету" (bottom)
    print("\n" + "=" * 80)
    print("2. Y-RANGES: 'Дата формирования' vs 'Операции по счету'")
    print("=" * 80)
    date_form_range = None
    operacii_range = None
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            line_text = "".join(s.get("text", "") for s in line.get("spans", []))
            if "Дата формирования" in line_text:
                y0 = min(s["bbox"][1] for s in line["spans"])
                y1 = max(s["bbox"][3] for s in line["spans"])
                date_form_range = (y0, y1)
                print(f"  'Дата формирования' block: y_range = ({y0:.2f}, {y1:.2f})")
            if "Операции по счету" in line_text:
                y0 = min(s["bbox"][1] for s in line["spans"])
                y1 = max(s["bbox"][3] for s in line["spans"])
                operacii_range = (y0, y1)
                print(f"  'Операции по счету' block: y_range = ({y0:.2f}, {y1:.2f})")
    if not date_form_range:
        print("  (no 'Дата формирования' found)")
    if not operacii_range:
        print("  (no 'Операции по счету' found)")

    # 3. All occurrences of span "16.03.2026" with bbox
    print("\n" + "=" * 80)
    print("3. ALL OCCURRENCES OF '16.03.2026' (bbox)")
    print("=" * 80)
    matches = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if "16.03.2026" in text:
                    bbox = span.get("bbox", (0, 0, 0, 0))
                    matches.append((bbox, text))
                    print(f"  bbox=({bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f})  text='{text}'")
    if not matches:
        print("  (no matches)")

    # Raw structure dump for layout understanding
    print("\n" + "=" * 80)
    print("4. RAW DICT STRUCTURE (blocks/lines/spans)")
    print("=" * 80)
    for bi, block in enumerate(text_dict.get("blocks", [])):
        print(f"  Block {bi}: bbox={block.get('bbox')}")
        for li, line in enumerate(block.get("lines", [])):
            line_text = "".join(s.get("text", "") for s in line.get("spans", []))
            print(f"    Line {li}: bbox={line.get('bbox')}  text='{line_text[:60]}...' " if len(line_text) > 60 else f"    Line {li}: bbox={line.get('bbox')}  text='{line_text}'")
            for si, span in enumerate(line.get("spans", [])):
                t = span.get("text", "")
                print(f"      Span {si}: bbox={span.get('bbox')}  text='{t}'")

    doc.close()


if __name__ == "__main__":
    main()
