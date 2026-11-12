#!/usr/bin/env python3
"""
Сканирование PDF-квитанций по категориям и формирование шаблонов полей.
Извлекает поля (label + value) и сохраняет в YAML для каждой категории.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from collections import defaultdict

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Установите PyMuPDF: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

import json

ROOT = Path(__file__).parent.resolve()

# Маппинг: ключ категории -> папка
CATEGORIES = {
    "sbp": "СБП",
    "karta": "карта_на_карту",
    "tadzhikistan": "таджикистан",
    "uzbekistan": "узбекистан",
    "alfa": "альфа",
}

# Заголовки документов — не сохраняем как поля, только сдвигаем индекс
SKIP_TITLES = [
    "Квитанция о переводе по СБП",
    "Квитанция о переводе с карты на карту",
    "Квитанция о переводе клиенту Альфа-Банка",
    "Квитанция о переводе за рубеж по номеру телефона",
]

# Известные метки полей (для парсинга). Значения — это то, что идёт после метки.
FIELD_LABELS = [
    "Сформирована",
    "Квитанция о переводе по СБП",
    "Квитанция о переводе с карты на карту",
    "Квитанция о переводе клиенту Альфа-Банка",
    "Квитанция о переводе за рубеж по номеру телефона",
    "Сумма перевода",
    "Сумма списания, включая все комиссии",
    "Комиссия",
    "Списано с учётом комиссии",
    "Курс конвертации",
    "Сумма зачисления",
    "Сумма зачисления банком получателя",
    "Дата и время перевода",
    "Номер операции",
    "Получатель",
    "Номер телефона получателя",
    "Банк получателя",
    "Счёт списания",
    "Идентификатор операции в СБП",
    "Идентификатор операции",
    "Сообщение получателю",
    "Номер карты отправителя",
    "Номер карты получателя",
    "Код авторизации",
    "Код терминала",
    "Номер операции в банке",
]


def normalize_text(text: str) -> str:
    """Заменить неразрывные пробелы на обычные."""
    return text.replace("\xa0", " ").replace("\u00a0", " ").strip()


def extract_fields_from_text(text: str) -> dict[str, str]:
    """
    Извлечь поля из текста квитанции.
    Паттерн: метка (из FIELD_LABELS) на одной строке, значение на следующей.
    """
    text = normalize_text(text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        # Проверяем, совпадает ли строка с какой-то меткой (или начинается с неё)
        matched_label = None
        for label in FIELD_LABELS:
            if label in line or line == label:
                matched_label = label
                break
        if matched_label:
            # Значение обычно на следующей строке
            val_parts = []
            j = i + 1
            while j < len(lines) and lines[j] not in FIELD_LABELS:
                # Не берём строки, которые выглядят как новые метки
                next_line = lines[j]
                is_label = any(l in next_line for l in FIELD_LABELS if l != next_line)
                if is_label and len(next_line) > 30:
                    break
                val_parts.append(next_line)
                j += 1
                # Обычно значение — одна строка
                if val_parts and not any(
                    re.match(r"^[А-Яа-яA-Za-z].*[.:]$", p) for p in val_parts
                ):
                    if len(val_parts) >= 1 and (
                        "RUR" in val_parts[0]
                        or "TJS" in val_parts[0]
                        or "UZS" in val_parts[0]
                        or re.match(r"^\d", val_parts[0])
                        or "+" in val_parts[0]
                        or "****" in val_parts[0]
                    ):
                        break
            value = " ".join(val_parts).strip() if val_parts else ""
            if value and matched_label not in result:
                result[matched_label] = value
            i = j if val_parts else i + 1
        else:
            # Специальный случай: "Сформирована" — значение на этой же строке или следующей
            if "Сформирована" in " ".join(lines[max(0, i - 1) : i + 2]):
                # Ищем дату рядом
                for k in range(i, min(i + 3, len(lines))):
                    if re.search(r"\d{2}\.\d{2}\.\d{4}", lines[k]):
                        result["date_formed"] = lines[k]
                        break
            i += 1
    return result


def _is_value_line(line: str) -> bool:
    """Строка выглядит как значение (сумма, телефон, номер и т.д.)."""
    return (
        re.match(r"^\d", line)
        or " RUR" in line
        or "RUR " in line
        or " TJS" in line
        or "TJS " in line
        or " UZS" in line
        or "UZS " in line
        or (line.startswith("+") and len(line) > 5)
        or "****" in line
        or re.match(r"^[A-Z]\d{6}\d*$", line)
        or re.match(r"^[A-Z]\d+[A-Z0-9]+$", line)
        or re.match(r"^Z\d+$", line)
        or re.match(r"^\d[\d\s,.]*$", line)
    )


def extract_fields_simple(text: str) -> dict[str, str]:
    """
    Упрощённый парсер: чередование метка-значение.
    Метки — строки без цифр в начале, без RUR/TJS/UZS, без + и т.д.
    """
    text = normalize_text(text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    result = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_value_line(line):
            i += 1
            continue
        if any(skip in line for skip in SKIP_TITLES):
            i += 1
            continue
        if len(line) > 2 and not _is_value_line(line):
            # Строка — метка, ищем значение. Не брать как значение известные метки полей
            for j in range(i + 1, min(i + 4, len(lines))):
                next_ln = lines[j]
                if next_ln in FIELD_LABELS:
                    continue  # следующая строка — тоже метка, не значение
                if _is_value_line(next_ln) or (next_ln and next_ln != line):
                    result[line] = next_ln
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1
    return result


def extract_from_pdf(pdf_path: Path) -> tuple[str, dict[str, str]]:
    """Извлечь текст и поля из одного PDF."""
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception as e:
        print(f"  [Ошибка {pdf_path.name}]: {e}", file=sys.stderr)
        return "", {}
    fields = extract_fields_simple(text)
    # Добавить date_formed если есть паттерн "Сформирована ... DD.MM.YYYY"
    if not fields.get("date_formed") or "Сформирована" not in str(fields):
        m = re.search(r"Сформирована\s+[\s\S]*?(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}\s*мск)", text)
        if m:
            fields["Сформирована"] = m.group(1).strip()
    return text, fields


def fields_to_template_spec(fields: dict[str, str], category: str) -> dict:
    """Преобразовать сырые поля в структуру шаблона."""
    # Маппинг русских меток на ключи шаблона
    LABEL_TO_KEY = {
        "Сформирована": "date_formed",
        "Квитанция о переводе по СБП": "_title_sbp",
        "Квитанция о переводе с карты на карту": "_title_karta",
        "Квитанция о переводе клиенту Альфа-Банка": "_title_alfa",
        "Квитанция о переводе за рубеж по номеру телефона": "_title_abroad",
        "Сумма перевода": "amount",
        "Сумма списания, включая все комиссии": "amount_total",
        "Комиссия": "commission",
        "Списано с учётом комиссии": "amount_with_commission",
        "Курс конвертации": "course",
        "Сумма зачисления": "amount_credited",
        "Сумма зачисления банком получателя": "amount_credited",
        "Дата и время перевода": "date_time",
        "Номер операции": "operation_id",
        "Номер операции в банке": "operation_id",
        "Получатель": "recipient",
        "Номер телефона получателя": "phone",
        "Банк получателя": "bank",
        "Счёт списания": "account",
        "Идентификатор операции в СБП": "sbp_id",
        "Идентификатор операции": "operation_identifier",
        "Сообщение получателю": "message",
        "Номер карты отправителя": "card_from",
        "Номер карты получателя": "card_to",
        "Код авторизации": "auth_code",
        "Код терминала": "terminal_code",
    }
    spec = {"category": category, "fields": {}}
    for label, value in fields.items():
        key = LABEL_TO_KEY.get(label, label.replace(" ", "_")[:30])
        if key.startswith("_"):
            continue  # заголовки не включаем как поля для замены
        if key not in spec["fields"] or not spec["fields"][key].get("example"):
            spec["fields"][key] = {"label": label, "example": value}
    return spec


def merge_template_specs(specs: list[dict]) -> dict:
    """Объединить несколько спецификаций (разные форматы в одной категории)."""
    if not specs:
        return {}
    merged = {"category": specs[0]["category"], "fields": {}, "format_variants": []}
    seen_examples = defaultdict(set)
    for s in specs:
        for key, fd in s["fields"].items():
            ex = fd.get("example", "")
            if key not in merged["fields"]:
                merged["fields"][key] = {"label": fd.get("label", key), "example": ex}
                seen_examples[key].add(ex)
            elif ex and ex not in seen_examples[key] and len(seen_examples[key]) < 3:
                seen_examples[key].add(ex)
                # Добавить альтернативный пример
                if "examples" not in merged["fields"][key]:
                    merged["fields"][key]["examples"] = []
                merged["fields"][key]["examples"].append(ex)
    return merged


def scan_category(category_key: str, category_dir: str) -> dict:
    """Сканировать все PDF в папке категории и собрать шаблон."""
    folder = ROOT / category_dir
    if not folder.exists():
        print(f"[WARN] Папка {category_dir} не найдена")
        return {"category": category_key, "fields": {}}
    pdfs = list(folder.glob("*.pdf"))[:30]  # ограничить для скорости
    if not pdfs:
        print(f"[WARN] Нет PDF в {category_dir}")
        return {"category": category_key, "fields": {}}
    all_specs = []
    for p in pdfs:
        _, fields = extract_from_pdf(p)
        if fields:
            all_specs.append(fields_to_template_spec(fields, category_key))
    merged = merge_template_specs(all_specs)
    merged["source_pdfs"] = [p.name for p in pdfs[:5]]
    return merged


def main():
    import os

    os.chdir(ROOT)
    templates_dir = ROOT / "templates"
    templates_dir.mkdir(exist_ok=True)
    donors_dir = ROOT / "donors"
    donors_dir.mkdir(exist_ok=True)

    for key, folder in CATEGORIES.items():
        print(f"Сканирование {folder}...")
        spec = scan_category(key, folder)
        out_file = templates_dir / f"{key}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
        print(f"  -> {out_file}")

        # Скопировать первый PDF как donor для категории
        donor_src = ROOT / folder
        if donor_src.exists():
            pdfs = list(donor_src.glob("*.pdf"))
            if pdfs:
                donor_dst = donors_dir / f"{key}.pdf"
                import shutil

                shutil.copy2(pdfs[0], donor_dst)
                print(f"  donor: {donor_dst}")

    print("\nГотово. Шаблоны в templates/ (JSON)")


if __name__ == "__main__":
    main()
