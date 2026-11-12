#!/usr/bin/env python3
"""
Добавление печати (круглой) на PDF-чек.
Создаёт изображение печати ВТБ и накладывает его на страницу.

Использование:
  python3 add_stamp.py input.pdf output.pdf
  python3 add_stamp.py input.pdf output.pdf --x 100 --y 50 --size 80
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("[ERROR] Установите PyMuPDF: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[ERROR] Установите Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)


def create_stamp_image(size: int = 90) -> bytes:
    """Создать PNG-изображение круглой печати (красная обводка)."""
    img = Image.new("RGBA", (size * 2, size * 2), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size, size
    r_outer = size - 4
    r_inner = size - 12
    # Красный круг (обводка печати)
    for w in range(3):
        draw.ellipse(
            [cx - r_outer + w, cy - r_outer + w, cx + r_outer - w, cy + r_outer - w],
            outline=(200, 0, 0, 240),
        )
    draw.ellipse(
        [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
        outline=(200, 0, 0, 200),
        width=2,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def add_stamp(
    input_path: Path,
    output_path: Path,
    x: float = 80,
    y: float = 30,
    size: float = 45,
) -> bool:
    """Добавить печать на первую страницу PDF."""
    doc = fitz.open(input_path)
    if doc.page_count == 0:
        print("[ERROR] PDF не содержит страниц", file=sys.stderr)
        doc.close()
        return False

    page = doc[0]
    stamp_png = create_stamp_image(int(size * 2))

    # rect: левый верхний угол (x, y), размер (size x size)
    # fitz использует координаты от левого верхнего угла
    rect = fitz.Rect(x, y, x + size, y + size)
    page.insert_image(rect, stream=stamp_png)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # PyMuPDF не позволяет сохранять в тот же файл, что открыт
    if input_path.resolve() == output_path.resolve():
        import tempfile
        tmp = output_path.with_suffix(".tmp.pdf")
        doc.save(tmp)
        doc.close()
        tmp.replace(output_path)
    else:
        doc.save(output_path)
        doc.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Добавить печать на PDF-чек")
    parser.add_argument("input", help="Входной PDF")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--x", type=float, default=80, help="X позиция (по умолчанию 80)")
    parser.add_argument("--y", type=float, default=30, help="Y позиция от верха (по умолчанию 30)")
    parser.add_argument("--size", type=float, default=45, help="Размер печати в pt (по умолчанию 45)")
    args = parser.parse_args()

    inp = Path(args.input).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1
    if add_stamp(inp, out, args.x, args.y, args.size):
        print(f"[OK] Печать добавлена: {out}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
