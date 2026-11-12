#!/usr/bin/env python3
"""
Генерация PDF-квитанции по шаблону и значениям полей.
Использует rebuild_pdf с donor, применяет замены, присваивает уникальный Document ID.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
TEMPLATES_DIR = ROOT / "templates"
DONORS_DIR = ROOT / "donors"

# Папки категорий (для поиска donor)
CATEGORY_FOLDERS = {
    "sbp": "СБП",
    "karta": "карта_на_карту",
    "tadzhikistan": "таджикистан",
    "uzbekistan": "узбекистан",
    "alfa": "альфа",
}

# Для СБП при замене account — приоритетный donor с «чистым» CMap (бот не детектит подделку)
# AM_1772658254522 имеет счёт 40817810980480002476 и распознаётся ботом
SBP_PREFERRED_ACCOUNT_DONOR = "AM_1772658254522.pdf"


def load_template(category: str) -> dict:
    """Загрузить шаблон категории."""
    path = TEMPLATES_DIR / f"{category}.json"
    if not path.exists():
        raise FileNotFoundError(f"Шаблон не найден: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_required_chars(values: dict) -> set[int]:
    """Собрать Unicode-коды всех символов из values."""
    chars = set()
    for v in values.values():
        if v:
            for c in str(v):
                chars.add(ord(c))
    return chars


def _cmap_covers_chars(uni_to_cid: dict, required: set[int]) -> bool:
    """Проверить, что CMap содержит все нужные символы (пробел → nbsp)."""
    for cp in required:
        if cp in uni_to_cid:
            continue
        if cp == 0x20 and (0xA0 in uni_to_cid or 0x20 in uni_to_cid):
            continue
        return False
    return True


def find_best_donor(
    category: str,
    required_chars: set[int],
    template: dict,
    values: dict,
) -> tuple[Path | None, dict]:
    """
    Найти donor, у которого CMap нативно содержит все нужные символы.
    Исключает extended/patched — у них неверное соответствие глифов.
    Для СБП при замене account — приоритет donor с «чистым» CMap (AM_1772658254522).
    Возвращает (donor_path, donor_fields) или (None, {}).
    """
    folder_name = CATEGORY_FOLDERS.get(category)
    if not folder_name:
        return None, {}
    folder = ROOT / folder_name
    if not folder.exists():
        return None, {}

    # Для СБП при замене account — сначала пробуем preferred donor (чистый CMap)
    pdfs_to_try: list[Path] = []
    if category == "sbp" and "account" in values:
        preferred = folder / SBP_PREFERRED_ACCOUNT_DONOR
        if preferred.exists():
            pdfs_to_try.append(preferred)
    # Остальные PDF — как раньше
    for p in sorted(folder.glob("*.pdf")):
        if p not in pdfs_to_try:
            pdfs_to_try.append(p)

    for pdf_path in pdfs_to_try:
        name = pdf_path.name.lower()
        if "extended" in name or "patched" in name:
            continue
        try:
            sys.path.insert(0, str(ROOT))
            from cid_patch_amount import _parse_tounicode

            data = pdf_path.read_bytes()
            uni_to_cid = _parse_tounicode(data)
            if not uni_to_cid:
                continue
            if not _cmap_covers_chars(uni_to_cid, required_chars):
                continue
            donor_fields = extract_donor_fields(pdf_path)
            reps = build_replacements(template, values, donor_fields)
            if reps:
                return pdf_path, donor_fields
        except Exception:
            continue
    return None, {}


def get_donor_path(category: str) -> Path:
    """Путь к donor PDF (fallback). Берёт не-extended из папки категории."""
    folder_name = CATEGORY_FOLDERS.get(category)
    if folder_name:
        folder = ROOT / folder_name
        if folder.exists():
            for p in sorted(folder.glob("*.pdf")):
                if "extended" not in p.name.lower() and "patched" not in p.name.lower():
                    return p
    path = DONORS_DIR / f"{category}.pdf"
    if path.exists():
        return path
    raise FileNotFoundError(f"Donor не найден. Запустите scan_templates.py")


def extract_donor_fields(donor_path: Path) -> dict[str, str]:
    """Извлечь поля из donor PDF для использования как OLD в заменах."""
    try:
        import fitz
    except ImportError:
        return {}
    try:
        doc = fitz.open(donor_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return {}
    # Используем тот же парсер, что и в scan_templates
    from scan_templates import extract_fields_simple, fields_to_template_spec

    raw = extract_fields_simple(text.replace("\xa0", " ").replace("\u00a0", " "))
    spec = fields_to_template_spec(raw, "temp")
    result = {}
    for key, fd in spec.get("fields", {}).items():
        if "example" in fd:
            result[key] = fd["example"]
    return result


def build_replacements(
    template: dict,
    values: dict[str, str],
    donor_fields: dict[str, str],
) -> list[tuple[str, str]]:
    """
    Построить список замен OLD=NEW.
    OLD берётся из donor (чтобы точно было в PDF), NEW — из values.
    """
    replacements = []
    for key, new_val in values.items():
        if not new_val:
            continue
        # Пробуем donor, потом template example
        old_val = donor_fields.get(key) or (
            template.get("fields", {}).get(key, {}).get("example")
        )
        if not old_val:
            continue
        if str(old_val).strip() != str(new_val).strip():
            replacements.append((str(old_val), str(new_val)))
    return replacements


def run_cid_patch(
    donor_path: Path,
    output_path: Path,
    replacements: list[tuple[str, str]],
) -> bool:
    """CID-патч: точечная замена, сохраняет структуру и размер (~57 KB)."""
    try:
        sys.path.insert(0, str(ROOT))
        from cid_patch_amount import _parse_tounicode, patch_replacements
    except ImportError:
        return False
    if not _parse_tounicode(donor_path.read_bytes()):
        return False
    return patch_replacements(donor_path, output_path, replacements)


def run_rebuild(
    donor_path: Path,
    output_path: Path,
    replacements: list[tuple[str, str]],
) -> bool:
    """Полная пересборка через rebuild_pdf.py (больший размер, полный Unicode)."""
    cmd = [
        sys.executable,
        str(ROOT / "rebuild_pdf.py"),
        str(donor_path),
        str(output_path),
        "--donor-pdf",
        str(donor_path),
        "--font-alias-from-input",
    ]
    for old_val, new_val in replacements:
        # Экранировать = в значениях
        if "=" in new_val and old_val + "=" not in new_val:
            pass
        cmd.extend(["--replace", f"{old_val}={new_val}"])
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(result.stderr or result.stdout, file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[ERROR] rebuild_pdf timeout", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return False


def patch_unique_id(pdf_path: Path) -> bool:
    """Присвоить PDF уникальный Document ID."""
    try:
        sys.path.insert(0, str(ROOT))
        from patch_id import patch_document_id

        return patch_document_id(pdf_path, None)
    except Exception as e:
        print(f"[WARN] patch_id: {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Генерация PDF-квитанции по категории и значениям полей."
    )
    parser.add_argument(
        "--category",
        "-c",
        default=None,
        choices=["sbp", "karta", "tadzhikistan", "uzbekistan", "alfa"],
        help="Категория чека (или укажите в JSON)",
    )
    parser.add_argument("--output", "-o", default="receipt.pdf", help="Выходной PDF")
    parser.add_argument(
        "--json",
        "-j",
        metavar="FILE",
        help="JSON-файл с полями (переопределяет CLI)",
    )
    parser.add_argument(
        "--field",
        "-f",
        action="append",
        metavar="KEY=VALUE",
        help="Поле: key=value (можно повторять)",
    )
    # Частые поля как отдельные аргументы
    parser.add_argument("--amount", help="Сумма перевода (напр. 5 000 RUR)")
    parser.add_argument("--commission", help="Комиссия")
    parser.add_argument("--recipient", help="Получатель")
    parser.add_argument("--phone", help="Номер телефона")
    parser.add_argument("--account", help="Счёт списания")
    parser.add_argument("--operation-id", dest="operation_id", help="Номер операции")
    parser.add_argument("--date", dest="date_time", help="Дата и время перевода")
    parser.add_argument("--date-formed", dest="date_formed", help="Дата формирования")
    parser.add_argument("--amount-credited", dest="amount_credited", help="Сумма зачисления (TJS/UZS)")
    parser.add_argument("--course", help="Курс конвертации")
    parser.add_argument("--bank", help="Банк получателя")
    parser.add_argument("--sbp-id", dest="sbp_id", help="Идентификатор СБП")
    parser.add_argument(
        "--donor",
        "-d",
        metavar="PDF",
        help="Явно указать donor-PDF (напр. AM_1772740244320.pdf). Иначе выбирается автоматически.",
    )
    parser.add_argument(
        "--keep-donor-id",
        action="store_true",
        help="Не менять Document ID (оставить как у donor). Для «чистых» чеков.",
    )
    parser.add_argument("--card-from", dest="card_from", help="Номер карты отправителя")
    parser.add_argument("--card-to", dest="card_to", help="Номер карты получателя")
    parser.add_argument("--auth-code", dest="auth_code", help="Код авторизации")
    parser.add_argument("--terminal-code", dest="terminal_code", help="Код терминала")
    args = parser.parse_args()

    # Собрать values из args или JSON
    values = {}
    category_from_json = None
    output_from_json = None
    keep_donor_id = args.keep_donor_id
    if args.json:
        jpath = Path(args.json).expanduser().resolve()
        if not jpath.exists():
            print(f"[ERROR] Файл не найден: {jpath}", file=sys.stderr)
            return 1
        with open(jpath, encoding="utf-8") as f:
            data = json.load(f)
        if "category" in data:
            category_from_json = data["category"]
        if "output" in data:
            output_from_json = data["output"]
        if data.get("keep_donor_id") is True:
            keep_donor_id = True
        values = {k: str(v) for k, v in data.items()
                  if k not in ("category", "output", "keep_donor_id", "donor") and not k.startswith("_") and v is not None}
    else:
        # Из CLI
        for attr in ["amount", "commission", "recipient", "phone", "account", "operation_id",
                     "date_time", "date_formed", "amount_credited", "course", "bank", "sbp_id",
                     "card_from", "card_to", "auth_code", "terminal_code"]:
            v = getattr(args, attr, None)
            if v:
                values[attr] = v
        for kv in args.field or []:
            if "=" in kv:
                k, v = kv.split("=", 1)
                values[k.strip()] = v.strip()

    if not values:
        print("[ERROR] Укажите поля (--amount, --recipient и т.д.) или --json FILE")
        return 1

    category = category_from_json or args.category
    if not category:
        print("[ERROR] Укажите --category или category в JSON")
        return 1
    if category not in ("sbp", "karta", "tadzhikistan", "uzbekistan", "alfa"):
        print(f"[ERROR] Неизвестная категория: {category}")
        return 1

    output_path = Path(output_from_json or args.output).expanduser().resolve()

    try:
        template = load_template(category)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    donor_from_json = None
    if args.json:
        jpath = Path(args.json).expanduser().resolve()
        if jpath.exists():
            with open(jpath, encoding="utf-8") as f:
                data = json.load(f)
            donor_from_json = data.get("donor")

    donor_specified = args.donor or donor_from_json
    if donor_specified:
        # Явный donor: ищем в корне и в папках категорий
        d = Path(donor_specified).expanduser()
        if not d.is_absolute():
            for base in [ROOT, ROOT / CATEGORY_FOLDERS.get(category, "")]:
                if (base / d.name).exists():
                    d = base / d.name
                    break
            if not d.exists():
                d = ROOT / donor_specified
        if not d.exists():
            print(f"[ERROR] Donor не найден: {donor_specified}", file=sys.stderr)
            return 1
        donor_path = d.resolve()
        donor_fields = extract_donor_fields(donor_path)
        print(f"[INFO] Donor: {donor_path.name} (явно указан)")
    else:
        required_chars = get_required_chars(values)
        donor_path, donor_fields = find_best_donor(category, required_chars, template, values)
        if donor_path is None:
            try:
                donor_path = get_donor_path(category)
                donor_fields = extract_donor_fields(donor_path)
            except FileNotFoundError as e:
                print(f"[ERROR] {e}", file=sys.stderr)
                return 1
        else:
            print(f"[INFO] Donor: {donor_path.name} (все символы в CMap)")
            if (category == "sbp" and "account" in values
                    and donor_path.name != SBP_PREFERRED_ACCOUNT_DONOR):
                print("[INFO] Для «чистого» счёта (минуя детекцию) используйте receipt_sbp_clean.json")

    replacements = build_replacements(template, values, donor_fields)
    if not replacements:
        print("[ERROR] Не удалось построить замены. Проверьте, что значения полей заданы.")
        return 1

    print(f"Генерация {output_path}...")
    if run_cid_patch(donor_path, output_path, replacements):
        print("[INFO] CID-патч: структура и размер сохранены (~57 KB)")
    elif not run_rebuild(donor_path, output_path, replacements):
        return 2

    if not keep_donor_id:
        if patch_unique_id(output_path):
            print("[OK] Уникальный Document ID присвоен")
    else:
        print("[INFO] Document ID оставлен от donor")

    print(f"[OK] Готово: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
