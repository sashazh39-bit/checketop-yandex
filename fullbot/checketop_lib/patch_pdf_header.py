#!/usr/bin/env python3
"""Вариант А: байтовый патч заголовка PDF — убрать «Written by MuPDF».

Заменяет блок комментариев после %PDF-1.3 на нейтральный (в стиле iOS),
удаляя явный признак обработчика MuPDF/PyMuPDF.

Использование:
    python3 patch_pdf_header.py input.pdf output.pdf
    python3 patch_pdf_header.py "Квитанция (3).pdf" "Квитанция (3)_patched.pdf"

Для варианта Б: после rebuild_pdf.py вызывать этот скрипт для post-processing.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# Комментарий в стиле iOS (из input ru.pdf) — нейтральные байты после %
IOS_STYLE_COMMENT = bytes([
    0xC4, 0xE5, 0xF2, 0xE5, 0xEB, 0xA7, 0xF3, 0xA0, 0xD0, 0xC4, 0xC6,
])

# Паттерны для поиска MuPDF-блока
MUPDF_PATTERNS = [
    # Стандартный: %<any>% Written by MuPDF 1.x.x
    re.compile(
        rb"(%PDF-1\.\d\r?\n)"  # заголовок
        rb"(%[^\r\n]*\r?\n)?"   # опционально первая строка комментария
        rb"(%\s*Written by MuPDF[^\r\n]*\r?\n\r?\n)",
        re.DOTALL,
    ),
    # Упрощённый: только MuPDF-строка
    re.compile(rb"(%\s*Written by MuPDF[^\r\n]*\r?\n)(\r?\n)?", re.MULTILINE),
]


def patch_header(data: bytes) -> tuple[bytes, bool]:
    """
    Заменить MuPDF-комментарий на нейтральный.
    Замена сохраняет длину блока — offset'ы xref не меняются.
    Не перезаписывает первый объект (N 0 obj) — ограничивает блок до него.
    """
    modified = False

    # Не трогать байты после первого "N 0 obj", чтобы не сломать Catalog
    first_obj = re.search(rb"\d+ 0 obj", data[:512])
    safe_len = first_obj.start() if first_obj else 512

    full_match = re.search(
        rb"(%PDF-1\.\d\r?\n)"
        rb"(%[^\r\n]*\r?\n)?"
        rb"(%\s*Written by MuPDF[^\r\n]*\r?\n\r?\n)",
        data[:safe_len],
    )
    if full_match:
        prefix = full_match.group(1)
        old_block = full_match.group(0)
        tail_len = len(old_block) - len(prefix)
        neutral1 = b"%" + IOS_STYLE_COMMENT + b"\n"
        remain = tail_len - len(neutral1)
        neutral2 = (b"%" + b" " * (remain - 2) + b"\n") if remain >= 2 else b""
        new_tail = (neutral1 + neutral2)[:tail_len]
        if len(new_tail) < tail_len:
            new_tail += b" " * (tail_len - len(new_tail))
        new_block = prefix + new_tail
        data = data.replace(old_block, new_block, 1)
        modified = True
    else:
        m = re.search(rb"%\s*Written by MuPDF[^\r\n]*\r?\n", data[:safe_len])
        if m and m.end() <= safe_len:
            old_slice = m.group(0)
            repl = b"%" + IOS_STYLE_COMMENT
            repl = (repl + b" " * (len(old_slice) - len(repl) - 1))[: len(old_slice) - 1] + b"\n"
            data = data[: m.start()] + repl + data[m.end() :]
            modified = True

    return data, modified


def patch_file(path: Path) -> bool:
    """Патч файла по месту. Возвращает True, если было изменение."""
    data = path.read_bytes()
    if b"Written by MuPDF" not in data[:512]:
        return False
    patched, modified = patch_header(data)
    if modified:
        path.write_bytes(patched)
    return modified


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Убрать «Written by MuPDF» из заголовка PDF (вариант А)."
    )
    parser.add_argument("input_pdf", help="Входной PDF")
    parser.add_argument("output_pdf", help="Выходной PDF")
    args = parser.parse_args()

    input_path = Path(args.input_pdf).expanduser().resolve()
    output_path = Path(args.output_pdf).expanduser().resolve()

    if not input_path.exists():
        print(f"[ERROR] Файл не найден: {input_path}", file=sys.stderr)
        return 1

    data = input_path.read_bytes()
    if b"Written by MuPDF" not in data[:512]:
        print("[INFO] MuPDF-комментарий не найден, файл копируется без изменений.")
        output_path.write_bytes(data)
        return 0

    patched, modified = patch_header(data)
    if not modified:
        print("[WARN] Паттерн не сработал.", file=sys.stderr)
        output_path.write_bytes(data)
        return 0

    output_path.write_bytes(patched)
    print(f"[OK] Патч применён: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
