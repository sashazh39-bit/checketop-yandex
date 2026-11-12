#!/usr/bin/env python3
"""Сравнение метаданных и структуры двух PDF."""
import re
import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None


def extract_raw_meta(path: Path) -> dict:
    """Извлечь сырые метаданные и структуру из bytes."""
    data = path.read_bytes()
    result = {}

    # Info dict
    info_m = re.search(rb'/Info\s+(\d+)\s+(\d+)\s+R', data)
    if info_m:
        result["info_ref"] = f"{info_m.group(1).decode()} {info_m.group(2).decode()} R"

    # Trailer
    trailer_m = re.search(rb'trailer\s*<<(.*?)>>', data, re.DOTALL)
    if trailer_m:
        trailer = trailer_m.group(1).decode("utf-8", errors="replace")
        result["trailer"] = trailer[:500]

    # /ID
    id_m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', data)
    if id_m:
        result["id_0"] = id_m.group(1).decode()
        result["id_1"] = id_m.group(2).decode()

    # Root / Catalog
    cat_m = re.search(rb'/Type\s*/Catalog', data)
    result["has_catalog"] = bool(cat_m)

    # xref
    xref_m = re.search(rb'xref\r?\n(\d+)\s+(\d+)', data)
    if xref_m:
        result["xref_count"] = xref_m.group(2).decode()

    # Metadata stream ref
    meta_m = re.search(rb'/Metadata\s+(\d+)\s+(\d+)\s+R', data)
    result["has_metadata_stream"] = bool(meta_m)

    return result


def extract_fitz_meta(path: Path) -> dict:
    """Метаданные через PyMuPDF."""
    if not fitz:
        return {}
    doc = fitz.open(path)
    m = doc.metadata
    doc.close()
    return dict(m)


def main():
    orig = Path("/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf")
    gen = Path("/Users/aleksandrzerebatav/Desktop/чекетоп/Тест ВТБ/09-03-26_03-47_1.pdf")

    if len(sys.argv) > 1:
        gen = Path(sys.argv[1])

    print("=" * 70)
    print("СРАВНЕНИЕ МЕТАДАННЫХ")
    print("=" * 70)
    print(f"\nИсходник: {orig}")
    print(f"Сгенерированный: {gen}")

    print("\n--- PyMuPDF metadata ---")
    m_orig = extract_fitz_meta(orig)
    m_gen = extract_fitz_meta(gen)
    all_keys = sorted(set(m_orig.keys()) | set(m_gen.keys()))
    for k in all_keys:
        v1 = m_orig.get(k, "(нет)")
        v2 = m_gen.get(k, "(нет)")
        diff = " << ИЗМЕНЕНО" if v1 != v2 else ""
        print(f"  {k}:")
        print(f"    orig: {v1}")
        print(f"    gen:  {v2}{diff}")

    print("\n--- Raw structure ---")
    r_orig = extract_raw_meta(orig)
    r_gen = extract_raw_meta(gen)
    for k in sorted(set(r_orig.keys()) | set(r_gen.keys())):
        v1 = r_orig.get(k, "(нет)")
        v2 = r_gen.get(k, "(нет)")
        diff = " << ИЗМЕНЕНО" if v1 != v2 else ""
        print(f"  {k}: orig={v1} | gen={v2}{diff}")

    # Info object content (if dereferenced)
    print("\n--- Info dict (из объектов) ---")
    for name, p in [("orig", orig), ("gen", gen)]:
        data = p.read_bytes()
        # Найти объект с CreationDate
        cd = re.search(rb'/CreationDate\s*\(([^)]+)\)', data)
        md = re.search(rb'/ModDate\s*\(([^)]+)\)', data)
        cr = re.search(rb'/Creator\s*\(([^)]+)\)', data)
        pr = re.search(rb'/Producer\s*\(([^)]+)\)', data)
        print(f"  [{name}] CreationDate={cd.group(1).decode() if cd else '?'}")
        print(f"  [{name}] ModDate={md.group(1).decode() if md else '?'}")
        print(f"  [{name}] Creator={cr.group(1).decode() if cr else '?'}")
        print(f"  [{name}] Producer={pr.group(1).decode() if pr else '?'}")


if __name__ == "__main__":
    main()
