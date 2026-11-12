#!/usr/bin/env python3
"""Сжать PDF до ~150 КБ: рендер страниц в JPEG, новый PDF."""
import io
from pathlib import Path

import fitz
from PIL import Image

INP = Path(__file__).parent / "Выписка_патч_альфа.pdf"
OUT = Path(__file__).parent / "сжатая выписка.pdf"
TARGET_KB = 150

# Если входной файл не найден, попробуем другой
if not INP.exists():
    for p in ["Выписка_патч_альфа.pdf", "Выписка_по_счету 2.pdf"]:
        alt = Path(__file__).parent / p
        if alt.exists():
            INP = alt
            break

def compress_to_target():
    doc = fitz.open(INP)
    target_bytes = TARGET_KB * 1024

    # Подбор DPI: начинаем с 120, качество 80
    for dpi, qual in [(125, 82), (120, 80), (115, 78), (130, 85)]:
        out_doc = fitz.open()
        for i in range(len(doc)):
            page = doc[i]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            buf = io.BytesIO()
            pil_img.save(buf, "JPEG", quality=qual, optimize=True)
            img_bytes = buf.getvalue()

            img_doc = fitz.open(stream=img_bytes, filetype="jpeg")
            rect = page.rect
            out_page = out_doc.new_page(width=rect.width, height=rect.height)
            out_page.insert_image(rect, stream=img_bytes)
            img_doc.close()

        buf_out = io.BytesIO()
        out_doc.save(buf_out, garbage=4, deflate=True)
        out_doc.close()
        result = buf_out.getvalue()

        if len(result) <= target_bytes:
            OUT.write_bytes(result)
            print(f"DPI={dpi} quality={qual}: {len(result)} байт ({len(result)/1024:.1f} КБ) ✓")
            doc.close()
            return len(result)
        print(f"DPI={dpi} quality={qual}: {len(result)} байт ({len(result)/1024:.1f} КБ)")

    # Минимальный вариант если не влезли
    dpi, qual = 110, 72
    out_doc = fitz.open()
    for i in range(len(doc)):
        page = doc[i]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        buf = io.BytesIO()
        pil_img.save(buf, "JPEG", quality=qual, optimize=True)
        img_bytes = buf.getvalue()
        rect = page.rect
        out_page = out_doc.new_page(width=rect.width, height=rect.height)
        out_page.insert_image(rect, stream=img_bytes)
    buf_out = io.BytesIO()
    out_doc.save(buf_out, garbage=4, deflate=True)
    out_doc.close()
    result = buf_out.getvalue()
    OUT.write_bytes(result)
    print(f"DPI={dpi} quality={qual}: {len(result)} байт ({len(result)/1024:.1f} КБ) — итог")
    doc.close()
    return len(result)

if __name__ == "__main__":
    was = INP.stat().st_size
    got = compress_to_target()
    print(f"\nБыло: {was} байт | Стало: {got} байт | Сохранено: {OUT}")
