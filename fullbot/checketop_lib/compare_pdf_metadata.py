#!/usr/bin/env python3
"""Полное сравнение метаданных и структуры двух PDF.
Выводит все поля, XMP, шрифты, паттерны.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import fitz


def dump_pdf_full(pdf_path: Path) -> dict:
    """Извлечь всё: метаданные, XMP, шрифты, структура."""
    path = Path(pdf_path).expanduser().resolve()
    if not path.exists():
        return {"error": f"File not found: {path}"}

    result = {"file": str(path.name), "path": str(path)}

    with fitz.open(path) as doc:
        # 1. Все метаданные Info (включая нестандартные ключи)
        meta = doc.metadata or {}
        result["metadata"] = {k: (v or "") for k, v in meta.items()}
        result["metadata_keys"] = sorted(meta.keys())

        # 2. XMP (сырой XML если есть)
        try:
            xmp = doc.get_xml_metadata() or ""
            result["xmp_length"] = len(xmp)
            result["xmp_preview"] = xmp[:500] + "..." if len(xmp) > 500 else xmp
        except Exception as e:
            result["xmp_error"] = str(e)

        # 3. Шрифты (полная структура)
        fonts_all = []
        for pno in range(len(doc)):
            try:
                items = doc.get_page_fonts(pno, full=False)
                for item in items:
                    fonts_all.append({
                        "page": pno + 1,
                        "raw": list(item) if hasattr(item, "__iter__") else str(item),
                        "xref": item[0] if len(item) > 0 else None,
                        "ext": item[1] if len(item) > 1 else None,
                        "type": item[2] if len(item) > 2 else None,
                        "basefont": item[3] if len(item) > 3 else None,
                        "name": item[4] if len(item) > 4 else None,
                        "encoding": item[5] if len(item) > 5 else None,
                    })
            except Exception as e:
                fonts_all.append({"page": pno + 1, "error": str(e)})
        result["fonts"] = fonts_all

        # 4. PDF trailer / xref counts
        result["page_count"] = len(doc)
        try:
            raw = path.read_bytes()
            result["file_size"] = len(raw)
            result["startxref_count"] = raw.count(b"startxref")
            result["eof_count"] = raw.count(b"%%EOF")
            result["pdf_version"] = (re.search(rb"%PDF-(\d\.\d)", raw[:20]) or [None, b"?"])[1].decode("ascii", errors="ignore")
            # Паттерны: упоминания шрифтов, producer и т.п.
            result["has_ocg"] = b"/OCG" in raw or b"/OCProperties" in raw
            result["trailer_preview"] = raw[-400:].decode("latin-1", errors="replace")[-200:]
        except Exception as e:
            result["raw_error"] = str(e)

        # 5. Trailer/xref структура — из сырых байтов

    return result


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 compare_pdf_metadata.py 'input ru.pdf' 'Квитанция (21).pdf'")
        print("       Или: compare_pdf_metadata.py file1.pdf file2.pdf")
        sys.exit(1)

    file1 = Path(sys.argv[1]).expanduser().resolve()
    file2 = Path(sys.argv[2]).expanduser().resolve()

    d1 = dump_pdf_full(file1)
    d2 = dump_pdf_full(file2)

    print("=" * 70)
    print(f"ФАЙЛ 1: {d1.get('file', '?')}")
    print("=" * 70)
    print(json.dumps(d1, ensure_ascii=False, indent=2, default=str))

    print("\n")
    print("=" * 70)
    print(f"ФАЙЛ 2: {d2.get('file', '?')}")
    print("=" * 70)
    print(json.dumps(d2, ensure_ascii=False, indent=2, default=str))

    print("\n")
    print("=" * 70)
    print("СРАВНЕНИЕ МЕТАДАННЫХ (diff)")
    print("=" * 70)
    m1 = d1.get("metadata", {})
    m2 = d2.get("metadata", {})
    all_keys = sorted(set(m1.keys()) | set(m2.keys()))
    for k in all_keys:
        v1 = m1.get(k, "<нет>")
        v2 = m2.get(k, "<нет>")
        if v1 != v2:
            print(f"  {k}:")
            print(f"    F1: {repr(v1)}")
            print(f"    F2: {repr(v2)}")
        else:
            print(f"  {k}: (одинаково) {repr(v1)[:60]}...")

    print("\n")
    print("=" * 70)
    print("СРАВНЕНИЕ ШРИФТОВ")
    print("=" * 70)
    print("F1 fonts:", json.dumps(d1.get("fonts", []), ensure_ascii=False, indent=2, default=str)[:800])
    print("-" * 40)
    print("F2 fonts:", json.dumps(d2.get("fonts", []), ensure_ascii=False, indent=2, default=str)[:800])


if __name__ == "__main__":
    main()
