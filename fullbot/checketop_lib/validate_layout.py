#!/usr/bin/env python3
"""Валидация выравнивания чека ВТБ СБП.

1. Визуальная линия: рисует вертикальную линию на x=wall в PDF.
2. Авто-проверка: проверяет, что x1 полей правой колонки в пределах wall ± tol.

Использование:
  python3 validate_layout.py receipt.pdf
  python3 validate_layout.py receipt.pdf --visual-out receipt_with_line.pdf
  python3 validate_layout.py receipt.pdf --check-only
"""
import argparse
import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None


def get_right_column_x1(pdf_path: Path) -> list[tuple[float, float, str]]:
    """Извлечь (x1, y, text) полей правой колонки (100 < x0 < 270)."""
    if fitz is None:
        return []
    out = []
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]
        dt = page.get_text("dict")
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if not bb or bb[1] < 50 or bb[1] > 400:
                        continue
                    if 100 <= bb[0] <= 270:
                        text = (span.get("text", "") or "").strip()
                        if text:
                            out.append((bb[2], (bb[1] + bb[3]) / 2, text[:30]))
        doc.close()
    except Exception:
        pass
    return out


def check_alignment(pdf_path: Path, wall: float = 257.08, tol: float = 1.5) -> tuple[bool, list[str]]:
    """Проверить, что все поля в пределах wall ± tol. Возвращает (ok, messages)."""
    fields = get_right_column_x1(pdf_path)
    if not fields:
        return False, ["Не удалось извлечь поля"]
    wall_max = wall + tol
    wall_min = wall - tol
    ok = True
    msgs = []
    for x1, y, text in fields:
        if x1 > wall_max or x1 < wall_min:
            ok = False
            msgs.append(f"  x1={x1:.1f} вне [{wall_min:.1f}, {wall_max:.1f}]: {text!r}")
    if ok:
        msgs.append("Все поля в пределах wall ± {:.1f}".format(tol))
    return ok, msgs


def draw_wall_line(pdf_path: Path, out_path: Path, wall: float = 257.08, color: tuple = (1, 0, 0)) -> bool:
    """Нарисовать вертикальную линию на x=wall. Возвращает True при успехе."""
    if fitz is None:
        return False
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]
        rect = page.rect
        # Линия от верха до низа страницы
        shape = page.new_shape()
        shape.draw_line(
            fitz.Point(wall, 0),
            fitz.Point(wall, rect.y1),
        )
        shape.finish(color=color, width=0.5)
        shape.commit()
        doc.save(str(out_path))
        doc.close()
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path, help="PDF чек")
    ap.add_argument("--visual-out", type=Path, help="Сохранить PDF с линией wall")
    ap.add_argument("--check-only", action="store_true", help="Только авто-проверка")
    ap.add_argument("--wall", type=float, default=257.08, help="Ожидаемый wall")
    ap.add_argument("--tol", type=float, default=1.5, help="Допуск ± для проверки")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"Файл не найден: {args.pdf}")
        return 1

    from vtb_sbp_layout import get_layout_values
    layout = get_layout_values()
    wall = layout.get("wall") or args.wall

    print(f"Wall = {wall:.2f}")
    ok, msgs = check_alignment(args.pdf, wall=wall, tol=args.tol)
    for m in msgs:
        print(m)
    if not ok:
        print("Проверка не пройдена.")
    else:
        print("Проверка пройдена.")

    if args.visual_out and not args.check_only:
        if draw_wall_line(args.pdf, args.visual_out, wall=wall):
            print(f"С линией сохранено: {args.visual_out}")
        else:
            print("Не удалось нарисовать линию (нужен PyMuPDF)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
