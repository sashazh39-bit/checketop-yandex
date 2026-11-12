#!/usr/bin/env python3
"""Глубокое сканирование PDF: фактические bbox (x1) через PyMuPDF + Tm/TJ из stream.

Цель: понять реальные координаты правого края каждого поля и рассчитать 
точные pts_per_glyph для идеального выравнивания.
"""
import re
import zlib
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None


def count_tj_glyphs(tj_bytes: bytes) -> int:
    for kern in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        if kern in tj_bytes:
            return tj_bytes.count(kern) + 1
    return 1


def get_font_size_before(dec: bytes, pos: int) -> float | None:
    head = dec[:pos]
    m = list(re.finditer(rb"(\d+(?:\.\d+)?)\s+0\s+0\s+(\d+(?:\.\d+)?)\s+\d+\s+\d+\s+Tf", head))
    if m:
        return float(m[-1].group(2))
    return None


def scan_stream_blocks(data: bytes) -> list[dict]:
    """Извлечь Tm+TJ блоки из content stream."""
    blocks = []
    for stream_m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(stream_m.group(2))
        stream_start = stream_m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)\[([^\]]*)\]\s*TJ'
        for m in re.finditer(pat, dec):
            x, y = float(m.group(2)), float(m.group(4))
            if x < 50:
                continue
            tj_inner = m.group(7)
            n = count_tj_glyphs(tj_inner)
            font_size = get_font_size_before(dec, m.start())
            blocks.append({"tm_x": x, "y": y, "n": n, "font_size": font_size})
        break
    return blocks


def main():
    import sys
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf")
    if not path.exists():
        print("Файл не найден:", path)
        return

    data = path.read_bytes()
    stream_blocks = scan_stream_blocks(data)

    print("=" * 80)
    print("ГЛУБОКОЕ СКАНИРОВАНИЕ:", path.name)
    print("=" * 80)

    if not fitz:
        print("\nPyMuPDF не установлен. Установите: pip install pymupdf")
        return

    doc = fitz.open(path)
    page = doc[0]
    dt = page.get_text("dict")

    # Собираем spans с bbox из правой части (x0 > 100, y в диапазоне чеков)
    spans_info = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bb = span.get("bbox")
                if not bb:
                    continue
                x0, y0, x1, y1 = bb
                # Правая колонка: x0 между 100 и 270
                if 100 < x0 < 270 and 50 < y0 < 400:
                    text = span.get("text", "").strip()
                    if text:
                        spans_info.append({
                            "text": text[:40],
                            "x0": round(x0, 2), "x1": round(x1, 2),
                            "y": round((y0 + y1) / 2, 2),
                            "font_size": span.get("size"),
                        })

    # Сортируем по y (сверху вниз)
    spans_info.sort(key=lambda s: -s["y"])

    print("\n--- Фактические bbox (PyMuPDF) — правая колонка ---")
    print(f"{'y':>8} | {'x0':>8} | {'x1':>8} | {'font':>6} | text")
    print("-" * 80)
    for s in spans_info:
        print(f"{s['y']:>8.1f} | {s['x0']:>8.2f} | {s['x1']:>8.2f} | {s['font_size'] or 0:>6.1f} | {s['text']!r}")

    max_x1 = max(s["x1"] for s in spans_info)
    print(f"\n>>> МАКС x1 (правый край): {max_x1:.4f}")
    rightmost = [s for s in spans_info if s["x1"] >= max_x1 - 0.5]
    print(f">>> Самый правый текст: {[s['text'] for s in rightmost]}")

    # Сопоставление по тексту (словарь текст->x1)
    text_to_x1 = {s["text"]: s["x1"] for s in spans_info}

    # Поля для замены и их ключевые слова
    field_map = [
        (299.25, 210.98, 9, "Выполнено"),
        (275.25, 179.74, 17, "04:47"),   # дата
        (251.25, 230.81, 5, "9426"),
        (227.25, 149.81, 23, "Александр"),
        (203.25, 177.82, 17, "Ефим"),
        (179.25, 174.82, 18, "236"),
        (155.25, 216.34, 8, "Сбербанк"),
        (72.37, 211.95, 7, "000"),
        (360.1, 211.95, 7, "1 000"),
    ]

    print("\n--- Фактический pts для каждого поля (x1 из bbox) ---")
    print(f"{'Поле':20} | {'tm_x':>10} | {'N':>4} | {'x1':>8} | pts")
    print("-" * 65)

    pts_by_field = {}
    for y, tm_x, n, key in field_map:
        best = None
        for sp in spans_info:
            if key in sp["text"]:
                best = sp
                break
        if best:
            x1 = best["x1"]
            pts = (x1 - tm_x) / n if n > 0 else 0
            pts_by_field[key] = pts
            print(f"{key:20} | {tm_x:>10.2f} | {n:>4} | {x1:>8.2f} | {pts:.4f}")

    # Для НОВОГО текста — сколько глифов и какой pts
    new_fields = [
        ("Дата 10.03.2025, 12:00", 17, "04:47"),  # тот же pts что дата
        ("Плательщик Евгений...Е.", 24, "Александр"),
        ("Получатель Анна...С.", 15, "Ефим"),
        ("Сумма 10 000 ₽", 8, "000"),  # font 13.5
    ]
    print("\n--- Новые поля: tm_x = WALL - n * pts ---")
    WALL = max_x1
    print(f"WALL = {WALL:.4f}\n")
    for name, n_new, orig_key in new_fields:
        pts = pts_by_field.get(orig_key)
        if pts is None:
            pts = 5.09  # fallback
        new_tm_x = WALL - n_new * pts
        print(f"{name:30} n={n_new}, pts={pts:.4f} -> tm_x={new_tm_x:.4f}")

    doc.close()

    # Рекомендация для WALL
    print("\n" + "=" * 80)
    print("РЕКОМЕНДАЦИЯ:")
    print(f"  WALL = max(x1) = {max_x1:.4f}")
    print("  Для заменяемых полей использовать pts из соответствующей строки выше.")
    print("=" * 80)


if __name__ == "__main__":
    main()
