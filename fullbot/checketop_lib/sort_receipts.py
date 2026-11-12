#!/usr/bin/env python3
"""
Скрипт для сортировки чеков по категориям.
Сканирует всю папку (включая чеки и вложенные). Перемещает (не копирует) файлы.
Обрабатывает только PDF размером 49-59 КБ. Остальные → папка "паль".
В итоге в корне только папки категорий (+ скрипт).
"""
import os
import shutil
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Установите PyMuPDF: pip install pymupdf")
    exit(1)

# Диапазон размера в байтах: 49-59 КБ
MIN_SIZE = 49 * 1024   # 50176
MAX_SIZE = 59 * 1024   # 60416

# Папки категорий
FOLDERS = {
    "sbp": "СБП",
    "karta": "карта_на_карту",
    "tadzhikistan": "таджикистан",
    "uzbekistan": "узбекистан",
    "alfa": "альфа",
    "pal": "паль",
}

ROOT = Path(__file__).parent.resolve()


def extract_text(pdf_path: str) -> str:
    """Извлечь текст из PDF."""
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:
        print(f"  [Ошибка чтения {pdf_path}]: {e}")
        return ""


def normalize_text(text: str) -> str:
    """Заменить неразрывные пробелы и прочие спецсимволы на обычные."""
    return text.replace("\xa0", " ").replace("\u00a0", " ")


def categorize(text: str) -> str:
    """
    Определить категорию чека по тексту.
    Возвращает ключ из FOLDERS или 'pal' если не распознано.
    """
    if not text:
        return "pal"

    text = normalize_text(text)

    # Порядок важен: сначала специфичные фразы
    if "Квитанция о переводе по СБП" in text:
        return "sbp"
    if "Квитанция о переводе с карты на карту" in text:
        return "karta"
    if "Квитанция о переводе клиенту Альфа-Банка" in text:
        return "alfa"
    if "Альфа-Банка" in text and "клиенту" in text:
        return "alfa"
    # TJS и UZS — переводы за рубеж
    if "TJS" in text:
        return "tadzhikistan"
    if "UZS" in text:
        return "uzbekistan"

    return "pal"


def collect_pdfs(root: Path) -> list[tuple[Path, int]]:
    """Собрать все PDF с путями и размерами."""
    result = []
    for p in root.rglob("*.pdf"):
        if p.is_file():
            try:
                size = p.stat().st_size
                result.append((p, size))
            except OSError:
                pass
    return result


def main():
    os.chdir(ROOT)

    # Создать папки категорий
    for key, folder in FOLDERS.items():
        (ROOT / folder).mkdir(parents=True, exist_ok=True)

    pdfs = collect_pdfs(ROOT)

    # Исключить PDF из папок категорий (чтобы не обрабатывать уже рассортированные)
    exclude_dirs = set(FOLDERS.values())
    to_process = []
    for p, size in pdfs:
        try:
            rel = p.relative_to(ROOT)
            if rel.parts and rel.parts[0] in exclude_dirs:
                continue  # уже в папке категории
        except ValueError:
            pass
        to_process.append((p, size))

    stats = {k: 0 for k in FOLDERS}
    in_range = []
    out_range = []

    for path, size in to_process:
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
        if MIN_SIZE <= size <= MAX_SIZE:
            in_range.append((path, size))
        else:
            out_range.append((path, size))

    print(f"Всего PDF: {len(to_process)}")
    print(f"  В диапазоне 49–59 КБ (для категоризации): {len(in_range)}")
    print(f"  Вне диапазона (→ паль): {len(out_range)}")

    # Категоризация PDF 49–59 КБ
    for path, size in in_range:
        text = extract_text(str(path))
        cat = categorize(text)
        dest_dir = ROOT / FOLDERS[cat]
        dest = dest_dir / path.name

        # Уникальное имя при конфликте
        if dest.exists() and dest.resolve() != path.resolve():
            stem, suf = path.stem, path.suffix
            n = 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{n}{suf}"
                n += 1

        try:
            shutil.move(str(path), str(dest))
            stats[cat] += 1
            print(f"  [49–59KB] {path.name} → {FOLDERS[cat]}")
        except Exception as e:
            print(f"  [Ошибка перемещения {path}]: {e}")

    # Перемещение остальных в "паль"
    for path, size in out_range:
        dest_dir = ROOT / "паль"
        dest = dest_dir / path.name

        if dest.exists() and dest.resolve() != path.resolve():
            stem, suf = path.stem, path.suffix
            n = 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{n}{suf}"
                n += 1

        try:
            shutil.move(str(path), str(dest))
            stats["pal"] += 1
            print(f"  [паль] {path.name}")
        except Exception as e:
            print(f"  [Ошибка перемещения {path}]: {e}")

    # Удалить пустые папки (чеки и др.), считая .DS_Store за «пустоту»
    def is_effectively_empty(p: Path) -> bool:
        for x in p.iterdir():
            if x.name != ".DS_Store":
                return False
        return True

    category_dirs = {ROOT / f for f in FOLDERS.values()}
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=False):
        for d in dirnames:
            full = Path(dirpath) / d
            if full.resolve() in category_dirs or full.resolve() == ROOT:
                continue
            try:
                if full.exists() and is_effectively_empty(full):
                    for x in full.iterdir():
                        try:
                            x.unlink()
                        except OSError:
                            pass
                    full.rmdir()
                    print(f"  [удалена пустая] {full.relative_to(ROOT)}")
            except OSError:
                pass

    print("\n--- Итог ---")
    for key, folder in FOLDERS.items():
        print(f"  {folder}: {stats[key]}")


if __name__ == "__main__":
    main()
