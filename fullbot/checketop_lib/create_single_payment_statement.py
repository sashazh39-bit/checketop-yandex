#!/usr/bin/env python3
"""Создать базовую выписку с одним платежом из эталона.

Оставляет только последнюю операцию (-500.00 RUB), затирает остальные 5.
Обновляет Расходные операции (710→500) и Баланс на конец (54,532.65→54,742.65).
"""
from pathlib import Path
import fitz

BASE = Path(__file__).parent
REFERENCE = Path("/Users/aleksandrzerebatav/Downloads/Выписка_по_счёту_№408178**********9414_с_12_03_2026_по_13_03_2026.pdf")
for alt in [BASE / "Выписка_патч_13_03.pdf", BASE / "Выписка_патч_13_03_итог.pdf"]:
    if alt.exists():
        REFERENCE = alt
        break
OUT_DIR = BASE / "база_выписок"
OUT_PATH = OUT_DIR / "vtb_template.pdf"


def main():
    if not REFERENCE.exists():
        print(f"[ERROR] Эталон не найден: {REFERENCE}")
        return 1

    doc = fitz.open(REFERENCE)
    page = doc[0]
    dt = page.get_text("dict")

    # Собираем bbox блоков: блок = все spans с общим bbox
    blocks_to_erase = []
    for block in dt.get("blocks", []):
        block_text = ""
        x0_min, y0_min, x1_max, y1_max = 9999, 9999, 0, 0
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "")
                block_text += t
                bbox = span.get("bbox")
                if bbox:
                    x0_min = min(x0_min, bbox[0])
                    y0_min = min(y0_min, bbox[1])
                    x1_max = max(x1_max, bbox[2])
                    y1_max = max(y1_max, bbox[3])
        if block_text and ("-10.00" in block_text or "-50.00" in block_text) and "-500.00" not in block_text:
            blocks_to_erase.append(((x0_min, y0_min, x1_max, y1_max), block_text))

    blocks_to_erase.sort(key=lambda x: x[0][1])

    for bbox, _ in blocks_to_erase[:5]:
        rect = fitz.Rect(bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2)
        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))

    # Замена 710.00 RUB → 500.00 RUB и 54,532.65 RUB → 54,742.65 RUB
    for old_t, new_t in [("710.00 RUB", "500.00 RUB"), ("54,532.65 RUB", "54,742.65 RUB")]:
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text") != old_t:
                        continue
                    bbox = span.get("bbox")
                    if not bbox:
                        continue
                    x0, y0, x1, y1 = bbox
                    fs = float(span.get("size", 9))
                    c = int(span.get("color", 0))
                    color = ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255
                    page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=(1, 1, 1), fill=(1, 1, 1))
                    tw = fitz.get_text_length(new_t, fontname="helv", fontsize=fs)
                    page.insert_text(fitz.Point(x1 - tw, y1 - fs * 0.2), new_t, fontsize=fs, fontname="helv", color=color)
                    break

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    doc.save(OUT_PATH, garbage=0, deflate=True)
    doc.close()
    print(f"[OK] Сохранено: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    exit(main())
