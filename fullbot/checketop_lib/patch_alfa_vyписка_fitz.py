#!/usr/bin/env python3
"""Патч выписки Альфа-Банка через PyMuPDF: замена текста с сохранением структуры.

Замены:
1. 40817810280480002477 → 40817810280480002476
2. Жеребятьев Александр → Николаев Дмитрий
3. За период с 16.03.2026 по 16.03.2026 → За период с 07.03.2026 по 08.03.2026
4. 14,82 RUR → 5 004,82 RUR
5. 10,00 RUR → 5 000,00 RUR (Расходы)
6. -10,00 RUR → 5 000,00 RUR (операция)
7. 16.03.2026,19-12-24 → 07.03.2026,14-17-24 (в описании операции)
8. 16.03.2026 в таблице операций → 07.03.2026 (НЕ трогаем "Дата формирования")
9. 8В, кв. 78 → случайные дом, квартира
"""
import random
import re
import sys
from pathlib import Path

import fitz


ARIAL_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
]


def _resolve_arial() -> str | None:
    for p in ARIAL_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _has_cyrillic(s: str) -> bool:
    return any(ord(c) > 127 for c in s)


def _extract_pdf_id(pdf_bytes: bytes) -> bytes | None:
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", pdf_bytes)
    return m.group(1) if m else None


def _restore_pdf_id(path: Path, original_id: bytes) -> None:
    data = path.read_bytes()
    pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")

    def repl(m):
        return m.group(0).replace(m.group(2), original_id)

    new_data = pat.sub(repl, data, count=1)
    if new_data != data:
        path.write_bytes(new_data)


def _make_replacement(
    dt: dict,
    replacements: list[tuple],
    page: fitz.Page,
    date_form_y_max: float = 250.0,
    cyrillic_font: str | None = None,
) -> int:
    modified = 0
    for item in replacements:
        if len(item) == 3:
            old_text, new_text, right_align = item
            skip_if_below = 0
        else:
            old_text, new_text, right_align, skip_if_below = item

        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    st = span.get("text", "")
                    if st.strip() != old_text.strip():
                        if old_text not in st:
                            continue
                        if old_text == "16.03.2026,19-12-24":
                            new_st = st.replace(old_text, "07.03.2026,14-17-24")
                        else:
                            continue
                    else:
                        new_st = new_text

                    bbox = span.get("bbox")
                    if not bbox or len(bbox) != 4:
                        continue
                    x0, y0, x1, y1 = bbox

                    if skip_if_below and y0 < skip_if_below:
                        continue

                    fontsize = float(span.get("size", 8.0))
                    c = int(span.get("color", 0))
                    color = (
                        ((c >> 16) & 255) / 255.0,
                        ((c >> 8) & 255) / 255.0,
                        (c & 255) / 255.0,
                    )
                    fontname = cyrillic_font if (cyrillic_font and _has_cyrillic(new_st)) else "helv"

                    rect = fitz.Rect(x0, y0, x1, y1)
                    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))

                    if right_align:
                        try:
                            tw = fitz.get_text_length(new_st, fontname=fontname, fontsize=fontsize)
                        except (ValueError, TypeError):
                            tw = len(new_st) * fontsize * 0.5
                        insert_x = min(x0, x1 - tw)
                    else:
                        insert_x = x0

                    baseline_y = y1 - fontsize * 0.2
                    try:
                        page.insert_text(
                            fitz.Point(insert_x, baseline_y),
                            new_st,
                            fontsize=fontsize,
                            fontname=fontname,
                            color=color,
                        )
                    except Exception:
                        try:
                            page.insert_text(
                                fitz.Point(insert_x, baseline_y),
                                new_st,
                                fontsize=fontsize,
                                fontname="helv",
                                color=color,
                            )
                        except Exception:
                            page.insert_text(
                                fitz.Point(insert_x, baseline_y),
                                new_st,
                                fontsize=fontsize,
                                color=color,
                            )
                    modified += 1
    return modified


def patch_alfa_statement(
    inp: Path,
    out: Path,
    house: int | None = None,
    apt: int | None = None,
) -> bool:
    house = house if house is not None else random.randint(1, 50)
    apt = apt if apt is not None else random.randint(1, 150)
    house_str = str(house)
    apt_str = str(apt)

    orig_id = _extract_pdf_id(inp.read_bytes()) if inp.exists() else None
    doc = fitz.open(inp)
    page = doc[0]
    orig_meta = dict(doc.metadata or {})

    cyrillic_font = None
    arial_path = _resolve_arial()
    if arial_path:
        try:
            page.insert_font(fontname="repl_arial", fontfile=arial_path)
            cyrillic_font = "repl_arial"
        except Exception:
            pass

    dt = page.get_text("dict")
    DATE_FORM_Y = 250.0

    replacements = [
        ("40817810280480002477", "40817810280480002476", False),
        ("Жеребятьев Александр ", "Николаев Дмитрий ", False),
        ("За период с 16.03.2026 по 16.03.2026", "За период с 07.03.2026 по 08.03.2026", False),
        ("14,82 RUR", "5 004,82 RUR", True),
        ("10,00 RUR", "5 000,00 RUR", True),
        ("-10,00 RUR", "5 000,00 RUR", True),
        ("16.03.2026", "07.03.2026", False, DATE_FORM_Y),
        ("8В, кв. 78", f"{house_str}, кв. {apt_str}", False),
    ]

    total = 0
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                st = span.get("text", "")
                if "16.03.2026,19-12-24" in st:
                    new_st = st.replace("16.03.2026,19-12-24", "07.03.2026,14-17-24")
                    bbox = span.get("bbox")
                    if bbox and len(bbox) == 4:
                        x0, y0, x1, y1 = bbox
                        fontsize = float(span.get("size", 8.0))
                        c = int(span.get("color", 0))
                        color = (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)
                        fn = cyrillic_font if cyrillic_font and _has_cyrillic(new_st) else "helv"
                        page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=(1, 1, 1), fill=(1, 1, 1))
                        try:
                            page.insert_text(
                                fitz.Point(x0, y1 - fontsize * 0.2),
                                new_st,
                                fontsize=fontsize,
                                fontname=fn,
                                color=color,
                            )
                        except Exception:
                            page.insert_text(
                                fitz.Point(x0, y1 - fontsize * 0.2),
                                new_st,
                                fontsize=fontsize,
                                color=color,
                            )
                        total += 1
                    break

    repl_filtered = [
        r for r in replacements
        if r[0] not in ("4,82 RUR",) and "19-12-24" not in str(r[0])
    ]
    total += _make_replacement(dt, repl_filtered, page, DATE_FORM_Y, cyrillic_font)

    doc.set_metadata(orig_meta)
    doc.save(out, garbage=0, deflate=True)
    doc.close()

    if orig_id and out.exists():
        _restore_pdf_id(out, orig_id)

    print(f"Дом: {house_str}, Квартира: {apt_str}")
    return total > 0


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "Выписка_по_счёту.pdf"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else inp.parent / (inp.stem + "_patched.pdf")

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    if patch_alfa_statement(inp, out):
        print(f"[OK] Сохранено: {out}")
        return 0
    print("[WARN] Замены не применены", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
