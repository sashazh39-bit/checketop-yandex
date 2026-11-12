#!/usr/bin/env python3
"""Точечная замена текста в PDF через content stream / redaction.
Минимальные изменения: только целевой текст, метаданные и структура сохраняются.

Использование:
    python3 content_stream_replace.py input.pdf output.pdf --replace "200=100"
    python3 content_stream_replace.py input.pdf output.pdf --replace "200=100" --preserve-id
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

SYSTEM_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _resolve_system_font() -> str | None:
    """Путь к системному шрифту с полным набором символов (для замены subset)."""
    for p in SYSTEM_FONTS:
        if Path(p).exists():
            return p
    return None


def _extract_pdf_id(pdf_bytes: bytes) -> bytes | None:
    """Извлечь первый ID из trailer (/ID [ <...><...> ])."""
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", pdf_bytes)
    if m:
        return m.group(1)
    return None


def _restore_pdf_id(output_path: Path, original_id_hex: bytes) -> None:
    """Заменить второй ID в trailer на original (чтобы файл выглядел неотредактированным)."""
    data = output_path.read_bytes()
    pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")

    def repl(m: re.Match[bytes]) -> bytes:
        return m.group(0).replace(m.group(2), original_id_hex)

    new_data = pat.sub(repl, data, count=1)
    if new_data != data:
        output_path.write_bytes(new_data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Точечная замена текста в PDF (redaction)."
    )
    parser.add_argument("input_pdf", help="Входной PDF")
    parser.add_argument("output_pdf", help="Выходной PDF")
    parser.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Замена (можно несколько).",
    )
    parser.add_argument(
        "--preserve-id",
        action="store_true",
        help="Восстановить оригинальный Document ID в trailer (для прохождения проверок).",
    )
    parser.add_argument(
        "--x-min",
        type=float,
        default=None,
        metavar="X",
        help="Заменять только вхождения с rect.x0 >= X (для правой колонки таблицы).",
    )
    parser.add_argument(
        "--deflate",
        action="store_true",
        help="Сжатие потоков (уменьшает размер файла).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Вывод отладочной информации.",
    )
    args = parser.parse_args()

    if not args.replace:
        print("[ERROR] Укажите хотя бы одно --replace OLD=NEW", file=sys.stderr)
        return 1

    replacements = []
    for r in args.replace:
        if "=" not in r:
            print(f"[ERROR] Неверный формат: {r}", file=sys.stderr)
            return 1
        old, new = r.split("=", 1)
        replacements.append((old.strip(), new.strip()))

    input_path = Path(args.input_pdf).expanduser().resolve()
    output_path = Path(args.output_pdf).expanduser().resolve()

    if not input_path.exists():
        print(f"[ERROR] Файл не найден: {input_path}", file=sys.stderr)
        return 1

    original_id: bytes | None = None
    if args.preserve_id:
        orig_bytes = input_path.read_bytes()
        original_id = _extract_pdf_id(orig_bytes)

    doc = fitz.open(input_path)
    try:
        orig_meta = dict(doc.metadata or {})

        source_font_name: str | None = None
        font_path = _resolve_system_font()
        if font_path:
            try:
                for p in doc:
                    try:
                        p.insert_font(fontname="repl_arial", fontfile=font_path)
                        source_font_name = "repl_arial"
                        if args.verbose:
                            print(f"[DEBUG] Шрифт Arial вставлен: {font_path}", file=sys.stderr)
                        break
                    except Exception as e:
                        if args.verbose:
                            print(f"[DEBUG] insert_font на стр. {p.number}: {e}", file=sys.stderr)
            except Exception:
                pass

        needs_cyrillic = any(
            any(ord(c) > 127 for c in new)
            for _, new in replacements
        )
        if not source_font_name and needs_cyrillic:
            print(
                "[ERROR] Для замены кириллицы нужен шрифт Arial, но вставка не удалась. "
                "Стандартный helv не поддерживает кириллицу.",
                file=sys.stderr,
            )
            return 1

        if args.verbose and not source_font_name:
            print("[DEBUG] Используется helv (только латиница)", file=sys.stderr)

        modified = False
        if source_font_name:
            for page in doc:
                for old_text, new_text in replacements:
                    instances = page.search_for(old_text)
                    if not instances:
                        continue
                    text_dict = page.get_text("dict")
                    fontsize = 11.0
                    text_color = (0, 0, 0)
                    for block in text_dict.get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                if old_text in span.get("text", ""):
                                    fontsize = float(span.get("size", 11.0))
                                    c = int(span.get("color", 0))
                                    text_color = (
                                        ((c >> 16) & 255) / 255.0,
                                        ((c >> 8) & 255) / 255.0,
                                        (c & 255) / 255.0,
                                    )
                                    break
                    for rect in instances:
                        if args.x_min is not None and rect.x0 < args.x_min:
                            continue
                        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))
                        baseline_y = rect.y1 - fontsize * 0.25
                        page.insert_text(
                            fitz.Point(rect.x0, baseline_y),
                            new_text,
                            fontsize=fontsize,
                            fontname=source_font_name,
                            color=text_color,
                        )
                        modified = True
        else:
            fontname = "helv"
            for page in doc:
                for old_text, new_text in replacements:
                    instances = page.search_for(old_text)
                    if not instances:
                        continue
                    text_dict = page.get_text("dict")
                    fontsize = 11.0
                    text_color = (0, 0, 0)
                    for block in text_dict.get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                if old_text in span.get("text", ""):
                                    fontsize = float(span.get("size", 11.0))
                                    c = int(span.get("color", 0))
                                    text_color = (
                                        ((c >> 16) & 255) / 255.0,
                                        ((c >> 8) & 255) / 255.0,
                                        (c & 255) / 255.0,
                                    )
                                    break
                    for rect in instances:
                        if args.x_min is not None and rect.x0 < args.x_min:
                            continue
                        page.add_redact_annot(
                            rect,
                            text=new_text,
                            fontname=fontname,
                            fontsize=fontsize,
                            fill=(1, 1, 1),
                            text_color=text_color,
                            cross_out=False,
                        )
                        modified = True

            if modified:
                for page in doc:
                    page.apply_redactions()

        if not modified:
            print("[WARN] Текст для замены не найден.", file=sys.stderr)

        doc.set_metadata(orig_meta)

        doc.save(output_path, garbage=0, deflate=args.deflate)

        if args.preserve_id and original_id and output_path.exists():
            _restore_pdf_id(output_path, original_id)

        print(f"[OK] Сохранено: {output_path}")
    finally:
        doc.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
