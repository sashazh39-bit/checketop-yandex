#!/usr/bin/env python3
"""Патч выписки ВТБ 13.03: замены сумм с выравниванием вправо.

-10.00 RUB → -1,500.00 RUB
-50.00 RUB → -1,000.00 RUB
10.00 → 1,500.00 (колонка Расход)
50.00 → 1,000.00 (колонка Расход)
54,532.65 RUB → 48,242.65 RUB
710.00 RUB → 7,000.00 RUB
Разделитель тысяч: запятая. Фамилия без изменений.
"""
from pathlib import Path
import re
import fitz


def _extract_pdf_id(pdf_bytes: bytes) -> bytes | None:
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", pdf_bytes)
    return m.group(1) if m else None


def _restore_pdf_id(path: Path, original_id: bytes) -> None:
    data = path.read_bytes()
    pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")
    def repl(m): return m.group(0).replace(m.group(2), original_id)
    new_data = pat.sub(repl, data, count=1)
    if new_data != data:
        path.write_bytes(new_data)

# Все замены — ASCII (цифры, запятая, точка, RUB), шрифт helv

# Замены: (старый_текст, новый_текст, right_align)
# right_align=True: вставлять так, чтобы правый край совпадал (растягивание влево)
# Разделитель тысяч: запятая (1,000 / 1,500)
REPLACEMENTS = [
    ("-10.00 RUB", "-1,500.00 RUB", True),
    ("-50.00 RUB", "-1,000.00 RUB", True),
    ("10.00", "1,500.00", True),
    ("50.00", "1,000.00", True),
    ("54,532.65 RUB", "48,242.65 RUB", True),
    ("710.00 RUB", "7,000.00 RUB", True),
]

# Для сумм с RUB — правый край (B) сохраняем на месте x1 оригинального текста


def patch_with_replacements(
    inp: Path,
    out: Path,
    replacements: list[tuple[str, str, bool]],
) -> bool:
    """
    Применить замены к выписке.
    replacements: [(old_text, new_text, right_align), ...]
    Возвращает True если были изменения.
    """
    orig_id = _extract_pdf_id(inp.read_bytes()) if inp.exists() else None

    doc = fitz.open(inp)
    page = doc[0]
    orig_meta = dict(doc.metadata or {})

    dt = page.get_text("dict")
    modified = False

    for old_text, new_text, right_align in replacements:
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    st = span.get("text", "")
                    if st != old_text:
                        continue
                    bbox = span.get("bbox")
                    if not bbox or len(bbox) != 4:
                        continue
                    x0, y0, x1, y1 = bbox
                    fontsize = float(span.get("size", 7.0))
                    c = int(span.get("color", 0))
                    color = (
                        ((c >> 16) & 255) / 255.0,
                        ((c >> 8) & 255) / 255.0,
                        (c & 255) / 255.0,
                    )

                    if old_text == "50.00" and x0 < 250:
                        continue
                    if old_text == "10.00" and x0 < 250:
                        continue

                    rect = fitz.Rect(x0, y0, x1, y1)
                    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))

                    if right_align:
                        try:
                            tw = fitz.get_text_length(new_text, fontname="helv", fontsize=fontsize)
                        except (ValueError, TypeError):
                            tw = len(new_text) * fontsize * 0.55
                        insert_x = x1 - tw
                    else:
                        insert_x = x0

                    baseline_y = y1 - fontsize * 0.2
                    page.insert_text(
                        fitz.Point(insert_x, baseline_y),
                        new_text,
                        fontsize=fontsize,
                        fontname="helv",
                        color=color,
                    )
                    modified = True

    doc.set_metadata(orig_meta)
    doc.save(out, garbage=0, deflate=True)
    doc.close()

    if orig_id and out.exists():
        _restore_pdf_id(out, orig_id)

    return modified


def main():
    import sys
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/Users/aleksandrzerebatav/Downloads/Выписка_по_счёту_№408178**********9414_с_12_03_2026_по_13_03_2026.pdf"
    )
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_patched.pdf")

    modified = patch_with_replacements(inp, out, REPLACEMENTS)

    if modified:
        print(f"[OK] Сохранено: {out}")
    else:
        print("[WARN] Замены не применены")


if __name__ == "__main__":
    main()
