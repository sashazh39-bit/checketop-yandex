#!/usr/bin/env python3
"""Extract font and layer (OCG) information from a PDF.

Usage:
    python3 pdf_info.py input.pdf
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show fonts and layers (Optional Content) used in a PDF."
    )
    parser.add_argument("input_pdf", help="Input PDF path")
    parser.add_argument(
        "--fonts-from-text",
        action="store_true",
        help="Also show fonts per text span (with sample text).",
    )
    args = parser.parse_args()

    input_path = Path(args.input_pdf).expanduser().resolve()
    if not input_path.exists():
        print(f"[ERROR] File not found: {input_path}", file=sys.stderr)
        return 1

    try:
        doc = fitz.open(input_path)
    except Exception as e:
        print(f"[ERROR] Cannot open as PDF: {e}", file=sys.stderr)
        return 1

    with doc:
        # --- Metadata (creator / producer) ---
        meta = doc.metadata or {}
        print("=" * 60)
        print("METADATA (как было создано)")
        print("=" * 60)
        for key in ("title", "author", "subject", "creator", "producer", "creationDate", "modDate"):
            val = meta.get(key, "")
            if val:
                print(f"  {key}: {val}")
        meta_keys = ("title", "author", "subject", "creator", "producer", "creationDate", "modDate")
        if not any(meta.get(k) for k in meta_keys):
            print("  (метаданные пусты или отсутствуют)")
        print()

        # --- Fonts (from page resources) ---
        all_fonts: dict[tuple, dict] = {}  # (xref, basefont) -> info
        for pno in range(len(doc)):
            try:
                fonts = doc.get_page_fonts(pno, full=False)
            except Exception:
                fonts = []
            for item in fonts:
                # (xref, ext, type, basefont, name, encoding)
                if len(item) >= 5:
                    xref, ext, ftype, basefont, name = item[:5]
                    key = (xref, basefont)
                    if key not in all_fonts:
                        all_fonts[key] = {
                            "xref": xref,
                            "ext": ext,
                            "type": ftype,
                            "basefont": basefont,
                            "name": name,
                            "pages": [],
                        }
                    if pno + 1 not in all_fonts[key]["pages"]:
                        all_fonts[key]["pages"].append(pno + 1)

        print("=" * 60)
        print("ШРИФТЫ (из ресурсов страниц)")
        print("=" * 60)
        if not all_fonts:
            print("  (шрифты не найдены)")
        else:
            for info in sorted(all_fonts.values(), key=lambda x: (x["basefont"], x["xref"])):
                pages_str = ",".join(str(p) for p in sorted(info["pages"])[:10])
                if len(info["pages"]) > 10:
                    pages_str += f"... (всего {len(info['pages'])} стр.)"
                print(f"  Шрифт: {info['basefont']}")
                print(f"    тип: {info['type']}, файл: .{info['ext']}, xref: {info['xref']}")
                print(f"    страницы: {pages_str}")
                print()
        print()

        # --- Fonts from text spans (which font for which text) ---
        if args.fonts_from_text:
            font_to_samples: dict[str, list[str]] = defaultdict(list)
            max_samples = 3
            for page in doc:
                text_dict = page.get_text("dict")
                for block in text_dict.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            font = str(span.get("font", ""))
                            text = (span.get("text") or "").strip()
                            if font and text and len(font_to_samples[font]) < max_samples:
                                sample = text[:50] + "..." if len(text) > 50 else text
                                if sample not in font_to_samples[font]:
                                    font_to_samples[font].append(sample)

            print("=" * 60)
            print("ШРИФТЫ ПО ТЕКСТУ (образцы)")
            print("=" * 60)
            for font, samples in sorted(font_to_samples.items()):
                print(f"  {font}:")
                for s in samples:
                    print(f"    «{s}»")
                print()
            print()

        # --- Layers (Optional Content) ---
        print("=" * 60)
        print("СЛОИ (Optional Content Groups)")
        print("=" * 60)

        try:
            layers = doc.get_layers()
        except Exception:
            layers = []

        if layers:
            print("  Конфигурации слоёв:")
            for item in layers:
                print(f"    {item}")
            print()
        else:
            print("  Конфигурации слоёв: нет (или только стандартная)")
            print()

        try:
            ocgs = doc.get_ocgs()
        except Exception:
            ocgs = {}

        if ocgs:
            print("  OCG (Optional Content Groups):")
            for xref, info in sorted(ocgs.items()):
                name = info.get("name", "?")
                on = info.get("on", "?")
                intent = info.get("intent", "")
                usage = info.get("usage", "")
                print(f"    xref={xref}: «{name}» (on={on}, intent={intent}, usage={usage})")
            print()
        else:
            print("  OCG: нет (документ без слоёв)")
            print()

        try:
            ui_configs = doc.layer_ui_configs()
        except Exception:
            ui_configs = ()

        if ui_configs:
            print("  UI-слои (видимость в просмотрщиках):")
            for item in ui_configs:
                num = item.get("number", "?")
                text = item.get("text", "?")
                on = item.get("on", "?")
                locked = item.get("locked", False)
                dtype = item.get("type", "?")
                print(f"    #{num}: «{text}» on={on} locked={locked} type={dtype}")
        else:
            print("  UI-слои: нет (документ не использует Optional Content)")
        print()

        # --- Raw byte check for OCG/OCProperties ---
        raw = input_path.read_bytes()
        has_ocg = b"/OCG" in raw or b"/OCProperties" in raw
        print("=" * 60)
        print("ПРИЗНАКИ СЛОЁВ В СЫРОМ PDF")
        print("=" * 60)
        print(f"  /OCG или /OCProperties в файле: {'да' if has_ocg else 'нет'}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
