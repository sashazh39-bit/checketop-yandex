#!/usr/bin/env python3
"""Полное сканирование структуры PDF ВТБ: координаты Tm, TJ, глифы, логика выравнивания.

Извлекает все блоки Tm+TJ из content stream, считает глифы, определяет y-строки
и вычисляет implied right_edge для правой колонки.

Использование: python3 vtb_structure_scan.py input.pdf

Вывод:
- MediaBox, страницы
- Все Tm (x, y) + TJ (N глифов, kern) с x >= 100 (правая колонка)
- implied right_edge = x + N*pts_per_glyph
- Таблица y -> (label, x, N, right_edge)
"""
import re
import sys
import zlib
from pathlib import Path


def count_tj_glyphs(tj_bytes: bytes) -> int:
    """Подсчёт глифов в TJ по количеству кернов + 1."""
    for kern in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        if kern in tj_bytes:
            return tj_bytes.count(kern) + 1
    return 1


def get_font_size_before_tj(dec: bytes, tj_end: int) -> float | None:
    """Найти размер шрифта (Tf) перед этим TJ."""
    # Ищем последний " N Tf" перед позицией tj_end
    head = dec[:tj_end]
    m = list(re.finditer(rb"(\d+(?:\.\d+)?)\s+0\s+0\s+(\d+(?:\.\d+)?)\s+\d+\s+\d+\s+Tf", head))
    if m:
        return float(m[-1].group(2))  # второй размер (height = font size)
    m = list(re.finditer(rb"/F\d+\s+(\d+(?:\.\d+)?)\s+Tf", head))
    if m:
        return float(m[-1].group(1))
    return None


def scan_pdf(pdf_path: Path) -> dict:
    """Сканирует PDF и возвращает структуру координат."""
    data = pdf_path.read_bytes()
    result = {
        "path": str(pdf_path),
        "mediabox": None,
        "pages": 0,
        "blocks": [],
        "by_y": {},
        "right_edges": [],
    }

    # MediaBox
    m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", data)
    if m:
        result["mediabox"] = {
            "x0": float(m.group(1)), "y0": float(m.group(2)),
            "x1": float(m.group(3)), "y1": float(m.group(4)),
        }

    X_MIN = 50  # включая заголовок (получатель ~105)
    PTS_PER_GLYPH = 4.7  # приблизительно для font 9

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

        # Паттерн: 1 0 0 1 X Y Tm ... [TJ] TJ
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)\[([^\]]*)\]\s*TJ'
        for m in re.finditer(pat, dec):
            x, y = float(m.group(2)), float(m.group(4))
            if x < X_MIN:
                continue
            tj_inner = m.group(7)
            n = count_tj_glyphs(tj_inner)
            # kern из TJ
            kern = None
            for k in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
                if k in tj_inner:
                    kern = k.decode()
                    break
            right_edge = x + n * PTS_PER_GLYPH
            blk = {"x": x, "y": y, "n": n, "kern": kern, "right_edge": round(right_edge, 2)}
            result["blocks"].append(blk)
            result["by_y"].setdefault(round(y, 2), []).append(blk)
            result["right_edges"].append(right_edge)
        break  # один content stream

    return result


def infer_labels(blocks: list) -> dict:
    """Сопоставить блоки с известными полями по y."""
    # Типичные y для ВТБ чека (сверху вниз)
    labels = {
        327.11: "Заголовок (получатель)",
        299.25: "Выполнено",
        275.25: "Дата",
        251.25: "*9426",
        227.25: "Плательщик",
        203.25: "Получатель",
        179.25: "Телефон",
        155.25: "Сбербанк",
        131.25: "ID",
        119.25: "1700501",
        72.37: "Сумма",
    }
    return labels


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf")
    if not inp.exists():
        inp = Path("/Users/aleksandrzerebatav/Desktop/чекетоп/Тест ВТБ/09-03-26_03-47_1.pdf")
    if not inp.exists():
        print(f"[ERROR] Файл не найден. Укажите: python3 vtb_structure_scan.py <путь.pdf>")
        sys.exit(1)

    r = scan_pdf(inp)
    labels = infer_labels(r["blocks"])

    print("=" * 70)
    print(f"СТРУКТУРА PDF: {r['path']}")
    print("=" * 70)
    if r["mediabox"]:
        mb = r["mediabox"]
        print(f"\nMediaBox: {mb['x0']} x {mb['y0']} .. {mb['x1']} x {mb['y1']}")
        print(f"         ширина={mb['x1']-mb['x0']:.1f}, высота={mb['y1']-mb['y0']:.1f}")

    print("\n" + "-" * 70)
    print("ПРАВАЯ КОЛОНКА (x >= 100): Tm_x, y, N глифов, implied right_edge (x + N*4.7)")
    print("-" * 70)
    print(f"{'y':>10} | {'Tm_x':>10} | {'N':>4} | {'right':>8} | label")
    print("-" * 70)

    by_y = sorted(r["by_y"].items(), key=lambda p: -p[0])
    for y, blks in by_y:
        for b in blks:
            lb = labels.get(round(y, 2), "")
            print(f"{y:>10.2f} | {b['x']:>10.2f} | {b['n']:>4} | {b['right_edge']:>8.2f} | {lb}")

    if r["right_edges"]:
        lo, hi = min(r["right_edges"]), max(r["right_edges"])
        avg = sum(r["right_edges"]) / len(r["right_edges"])
        print("\n" + "-" * 70)
        print("ЛОГИКА ВЫРАВНИВАНИЯ:")
        print(f"  implied right_edge: min={lo:.1f}, max={hi:.1f}, avg={avg:.1f}")
        print("  (поля правой колонки НЕ используют единый right_edge!)")
        print("  При замене текста: new_x = orig_x + (orig_n - new_n) * pts_per_glyph")
        print("  Дата 17→17 глифов: Tm НЕ МЕНЯТЬ")
        print("-" * 70)

    return r


if __name__ == "__main__":
    main()
