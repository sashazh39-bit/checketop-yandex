#!/usr/bin/env python3
"""Патч Unknown 504.pdf: замена 10 ₽ на 5 000 ₽ с выравниванием вправо.

Сохранение структуры, метаданных, /ID.
Суммы растягиваются влево (right-align): правый край остаётся на месте.
"""
import re
import sys
from pathlib import Path

import fitz


def _extract_pdf_id(pdf_bytes: bytes) -> bytes | None:
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", pdf_bytes)
    return m.group(1) if m else None


def _restore_pdf_id(path: Path, original_id: bytes) -> None:
    data = path.read_bytes()
    pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")

    def repl(m):
        return m.group(0).replace(m.group(1), original_id).replace(m.group(2), original_id)

    new_data = pat.sub(repl, data, count=1)
    if new_data != data:
        path.write_bytes(new_data)


# Пары (old_text, new_text) для замены сумм
# "10 " — число с пробелом перед рублём (i = ₽ в ALSRubl)
REPLACEMENTS = [
    ("10 ", "5 000 "),
]


def _get_text_width(text: str, fontname: str, fontsize: float) -> float:
    """Ширина текста в пунктах."""
    try:
        return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    except (ValueError, TypeError):
        return len(text) * fontsize * 0.55


def patch(inp: Path, out: Path) -> bool:
    raw = inp.read_bytes()
    orig_id = _extract_pdf_id(raw)

    doc = fitz.open(inp)
    page = doc[0]
    orig_meta = dict(doc.metadata or {})

    dt = page.get_text("dict")
    modified = False

    # Собираем пары (span_number, span_ruble) для "10 " + "i"
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            for i, span in enumerate(spans):
                st = span.get("text", "")
                if st != "10 ":
                    continue

                # Следующий span — рубль "i" (ALSRubl)?
                next_span = spans[i + 1] if i + 1 < len(spans) else None
                if not next_span or next_span.get("text") != "i":
                    continue

                bbox = span.get("bbox")
                next_bbox = next_span.get("bbox")
                if not bbox or len(bbox) != 4 or not next_bbox or len(next_bbox) != 4:
                    continue

                x0, y0, x1, y1 = bbox
                nx0, ny0, nx1, ny1 = next_bbox

                # Правый край = конец рубля. Рубль "i" (ALSRubl) оставляем на месте.
                # Вставляем только "5 000 " так, чтобы он заканчивался перед рублём (nx0).
                ruble_left = nx0

                fontsize = float(span.get("size", 12.0))
                orig_font = span.get("font", "TinkoffSans-Medium")
                # Верхняя сумма (Итого) — жирная как в оригинале (TinkoffSans-Medium)
                if "Medium" in orig_font or "Bold" in orig_font:
                    fontname = "hebo"  # Helvetica-Bold
                else:
                    fontname = "helv"

                c = int(span.get("color", 0))
                color = (
                    ((c >> 16) & 255) / 255.0,
                    ((c >> 8) & 255) / 255.0,
                    (c & 255) / 255.0,
                )

                new_text = "5 000 "
                tw = _get_text_width(new_text, fontname, fontsize)
                insert_x = ruble_left - tw  # правый край "5 000 " = ruble_left (растягивание влево)

                # Заливаем белым только span с "10 " (рубль оставляем)
                rect1 = fitz.Rect(x0, y0, x1, y1)
                page.draw_rect(rect1, color=(1, 1, 1), fill=(1, 1, 1))

                # Базовая линия по рублю — чтобы "5 000" не уезжало вниз
                ruble_size = float(next_span.get("size", fontsize))
                baseline_y = ny1 - ruble_size * 0.2
                page.insert_text(
                    fitz.Point(insert_x, baseline_y),
                    new_text,
                    fontsize=fontsize,
                    fontname=fontname,
                    color=color,
                )
                modified = True
                print(f"  [10 ₽] → [5 000 ₽]  bbox={[round(v, 1) for v in bbox]}  ruble_left={ruble_left:.1f}  insert_x={insert_x:.1f}")

    doc.set_metadata(orig_meta)
    doc.save(out, garbage=0, deflate=True)
    doc.close()

    if orig_id and out.exists():
        _restore_pdf_id(out, orig_id)
        print(f"  /ID восстановлен")

    return modified


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/Unknown 504.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_5000.pdf")

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    if patch(inp, out):
        print(f"[OK] Сохранено: {out}")
        return 0

    print("[WARN] Замены не применены", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
