#!/usr/bin/env python3
"""База чеков-доноров: индексация символов и поиск подходящего чека.

Использование:
  python3 receipt_db.py build          # построить индекс
  python3 receipt_db.py add path.pdf vtb  # добавить чек
"""
import json
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None

BASE_DIR = Path(__file__).parent
RECEIPT_BASE = BASE_DIR / "база_чеков"
INDEX_PATH = BASE_DIR / "receipt_index.json"

# Нормализация ё→е для проверки (vtb_cmap FALLBACK)
_CHAR_NORMALIZE = {"ё": "е", "Ё": "Е", "‑": "-"}


def _normalize_char(c: str) -> str:
    return _CHAR_NORMALIZE.get(c, c)


def _extract_amount_from_text(text: str) -> int | None:
    """Извлечь сумму из текста вида '1 000 ₽' или '10000 RUR'."""
    import re
    m = re.search(r"[\d\s]+(?:\s*[₽]|RUR|р\.)", text)
    if not m:
        return None
    nums = re.sub(r"\s", "", m.group(0))
    nums = re.sub(r"[^\d]", "", nums)
    if nums:
        return int(nums)
    return None


def get_receipt_amount(pdf_path: str | Path) -> int | None:
    """Извлечь сумму из чека (первая найденная в тексте)."""
    if fitz is None:
        return None
    path = Path(pdf_path)
    if not path.exists():
        return None
    try:
        doc = fitz.open(path)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
        return _extract_amount_from_text(full_text)
    except Exception:
        return None


def get_operation_id_from_pdf(pdf_path: str | Path | bytes) -> str | None:
    """Извлечь ID операции (B606.../A606...) из чека. Для отображения при копировании."""
    import re
    text = ""
    if isinstance(pdf_path, bytes):
        if fitz is None:
            return None
        try:
            doc = fitz.open(stream=pdf_path, filetype="pdf")
            text = "".join(page.get_text() for page in doc)
            doc.close()
        except Exception:
            return None
    else:
        path = Path(pdf_path)
        if not path.exists() or fitz is None:
            return None
        try:
            doc = fitz.open(path)
            text = "".join(page.get_text() for page in doc)
            doc.close()
        except Exception:
            return None
    m = re.search(r"[AB]606[\dA-Fa-f]{18,30}", text)
    return m.group(0) if m else None


def get_receipt_chars(pdf_path: str | Path) -> set[str]:
    """Извлечь множество символов, которые есть в чеке (подмножество шрифта).
    Возвращает пустой set если PyMuPDF недоступен или ошибка."""
    if fitz is None:
        return set()
    path = Path(pdf_path)
    if not path.exists():
        return set()
    chars = set()
    try:
        doc = fitz.open(path)
        for page in doc:
            dt = page.get_text("dict")
            for block in dt.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        for c in text:
                            chars.add(_normalize_char(c))
        doc.close()
    except Exception:
        pass
    return chars


def index_receipt(pdf_path: str | Path, bank: str) -> set[str]:
    """Индексировать один чек. bank: 'vtb' | 'alfa'.
    Возвращает set символов."""
    return get_receipt_chars(pdf_path)


VTB_SUBTYPES = {"vtb_sbp": "СБП", "vtb_vtb_vtb": "ВТБ на ВТБ"}


def load_index() -> dict:
    """Загрузить индекс из JSON."""
    default = {"vtb_sbp": {}, "vtb_vtb_vtb": {}, "vtb": {}, "alfa": {}}
    if not INDEX_PATH.exists():
        return default
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        for k in default:
            if k not in data:
                data[k] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return default


def save_index(index: dict) -> None:
    """Сохранить индекс в JSON."""
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def build_index(base_folder: str | Path | None = None) -> dict:
    """Построить индекс: vtb/СБП, vtb/ВТБ на ВТБ, alfa."""
    base = Path(base_folder or RECEIPT_BASE)
    index = {"vtb_sbp": {}, "vtb_vtb_vtb": {}, "vtb": {}, "alfa": {}}
    if not base.exists():
        return index
    for subtype, subfolder in [("vtb_sbp", "СБП"), ("vtb_vtb_vtb", "ВТБ на ВТБ")]:
        folder = base / "vtb" / subfolder
        if not folder.is_dir():
            continue
        for path in folder.glob("**/*.pdf"):
            chars = get_receipt_chars(path)
            if chars:
                key = str(path.relative_to(base))
                amount = get_receipt_amount(path)
                index[subtype][key] = {"chars": sorted(chars), "amount": amount}
    for path in (base / "vtb").glob("*.pdf"):
        chars = get_receipt_chars(path)
        if chars:
            key = str(path.relative_to(base))
            index["vtb"][key] = {"chars": sorted(chars), "amount": get_receipt_amount(path)}
    folder = base / "alfa"
    if folder.is_dir():
        for path in folder.glob("*.pdf"):
            chars = get_receipt_chars(path)
            if chars:
                key = str(path.relative_to(base))
                index["alfa"][key] = {"chars": sorted(chars), "amount": get_receipt_amount(path)}
    return index


def chars_from_text_fields(*texts: str) -> set[str]:
    """Собрать множество символов из текстовых полей (payer, recipient, phone, bank)."""
    out = set()
    for t in texts:
        if t:
            for c in t:
                out.add(_normalize_char(c))
    return out


def receipt_supports_chars(pdf_path: str | Path, required_chars: set[str]) -> bool:
    """Проверить, что чек содержит все нужные символы."""
    rc = {_normalize_char(c) for c in required_chars}
    if not rc:
        return True
    receipt_chars = get_receipt_chars(pdf_path)
    return rc <= receipt_chars


def get_missing_chars_in_receipt(pdf_path: str | Path, required_chars: set[str]) -> set[str]:
    """Каких букв не хватает в конкретном чеке. Пустой set = всё есть."""
    rc = {_normalize_char(c) for c in required_chars}
    receipt_chars = get_receipt_chars(pdf_path)
    return rc - receipt_chars


COMMON_AMOUNTS = [10, 50, 100, 500, 1000, 5000, 10000, 50000, 100000]


def get_bank_counts(index: dict | None = None) -> dict[str, int]:
    """Число чеков по банкам (с fallback на старый индекс vtb)."""
    idx = index or load_index()
    return {
        "vtb_sbp": len(_get_bank_data(idx, "vtb_sbp")),
        "vtb_vtb_vtb": len(_get_bank_data(idx, "vtb_vtb_vtb")),
        "alfa": len(idx.get("alfa", {})),
    }


def _get_bank_data(idx: dict, bank: str) -> dict:
    """Получить данные банка. Fallback на старый индекс vtb с подпапками."""
    data = idx.get(bank, {})
    if data:
        return data
    if bank in ("vtb_sbp", "vtb_vtb_vtb"):
        vtb_data = idx.get("vtb", {})
        prefix = "vtb/СБП/" if bank == "vtb_sbp" else "vtb/ВТБ на ВТБ/"
        return {k: v for k, v in vtb_data.items() if isinstance(k, str) and k.startswith(prefix)}
    return {}


def get_bank_report(required_chars: set[str], bank: str, index: dict | None = None) -> tuple[set[str], int, set[str]]:
    """Проверить, каких букв не хватает в базе.
    Возвращает (missing_chars, receipts_count, all_chars_in_db)."""
    idx = index or load_index()
    bank_data = _get_bank_data(idx, bank)
    required = {_normalize_char(c) for c in required_chars}
    all_chars = set()
    for key, val in bank_data.items():
        if isinstance(val, dict):
            chars_list = val.get("chars", [])
        else:
            chars_list = val if isinstance(val, list) else []
        all_chars.update(chars_list)
    missing = required - all_chars
    return missing, len(bank_data), all_chars


def find_all_donors(required_chars: set[str], bank: str, index: dict | None = None) -> list[tuple[Path, int | None]]:
    """Найти все чеки, где required_chars ⊆ chars чека.
    Возвращает список (Path, amount_from)."""
    idx = index or load_index()
    bank_data = _get_bank_data(idx, bank)
    required = {_normalize_char(c) for c in required_chars}
    result = []
    for key, val in bank_data.items():
        if isinstance(val, dict):
            chars_list = val.get("chars", [])
            amount = val.get("amount")
        else:
            chars_list = val if isinstance(val, list) else []
            amount = None
        receipt_chars = set(chars_list)
        if required and required <= receipt_chars:
            path = RECEIPT_BASE / key
            if not path.exists():
                path = BASE_DIR / key
            if path.exists():
                am = amount if amount is not None else get_receipt_amount(path)
                result.append((path, am))
    return result


def find_donor(required_chars: set[str], bank: str, index: dict | None = None) -> tuple[Path | None, int | None]:
    """Найти чек, где required_chars ⊆ chars чека.
    Возвращает (Path к PDF, amount_from в чеке) или (None, None)."""
    donors = find_all_donors(required_chars, bank, index)
    return (donors[0][0], donors[0][1]) if donors else (None, None)


def add_receipt_to_index(pdf_path: str | Path, bank: str, subtype: str | None = None) -> bool:
    """Добавить чек в индекс. bank: vtb_sbp | vtb_vtb_vtb | vtb | alfa."""
    path = Path(pdf_path)
    if not path.exists():
        return False
    chars = get_receipt_chars(path)
    if not chars:
        return False
    index = load_index()
    if bank in ("vtb_sbp", "vtb_vtb_vtb"):
        subfolder = VTB_SUBTYPES[bank]
        target_dir = RECEIPT_BASE / "vtb" / subfolder
    else:
        target_dir = RECEIPT_BASE / bank
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    if path.resolve() != dest.resolve():
        import shutil
        shutil.copy2(path, dest)
    if bank in ("vtb_sbp", "vtb_vtb_vtb"):
        key = f"vtb/{VTB_SUBTYPES[bank]}/{path.name}"
    else:
        key = f"{bank}/{path.name}"
    amount = get_receipt_amount(dest if dest.exists() else path)
    index.setdefault(bank, {})[key] = {"chars": sorted(chars), "amount": amount}
    save_index(index)
    return True


def build_and_save(base_folder: str | Path | None = None) -> dict:
    """Построить индекс и сохранить в receipt_index.json."""
    index = build_index(base_folder or RECEIPT_BASE)
    save_index(index)
    return index


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python3 receipt_db.py build [папка]")
        print("              python3 receipt_db.py add путь.pdf vtb|alfa")
        sys.exit(0)
    cmd = sys.argv[1].lower()
    if cmd == "build":
        base = sys.argv[2] if len(sys.argv) > 2 else RECEIPT_BASE
        idx = build_and_save(base)
        print(f"[OK] Индекс: СБП {len(idx.get('vtb_sbp', {}))}, ВТБ на ВТБ {len(idx.get('vtb_vtb_vtb', {}))}, Альфа {len(idx.get('alfa', {}))}")
    elif cmd == "add" and len(sys.argv) >= 4:
        path, bank = sys.argv[2], sys.argv[3].lower()
        if bank not in ("vtb_sbp", "vtb_vtb_vtb", "vtb", "alfa"):
            print("[ERROR] bank: vtb_sbp | vtb_vtb_vtb | vtb | alfa")
            sys.exit(1)
        if add_receipt_to_index(path, bank):
            print(f"[OK] Добавлен {path} в {bank}")
        else:
            print("[ERROR] Не удалось добавить")
            sys.exit(1)
    else:
        print("[ERROR] Неизвестная команда")
        sys.exit(1)
