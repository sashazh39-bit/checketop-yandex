#!/usr/bin/env python3
"""Сканирование PDF-чеков в donors, построение базы CID-символов.

Собирает ToUnicode CMap и TJ-блоки из каждого PDF, определяет формат (literal/hex)
и kerning. Сохраняет donors_cid_db.json и опционально шаблон СБП.

Использование:
  python3 scan_donors_cid.py --donors donors --out donors_cid_db.json
  python3 scan_donors_cid.py --donors donors --out donors_cid_db.json --template templates/sbp_cid_template.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zlib
from pathlib import Path

# Импорт из существующих модулей
try:
    from cid_patch_amount import _parse_tounicode
except ImportError:
    _parse_tounicode = None

try:
    from merge_cid_from_sources import extract_tj_blocks
except ImportError:
    extract_tj_blocks = None

X_MIN_RIGHT = 100

# Символы, важные для шаблона СБП (приоритет при выборе донора)
SBP_PRIORITY_CHARS = set(
    "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюяё"
    "0123456789"
    " ₽.,:-"
)


def _parse_tounicode_local(data: bytes) -> dict[int, str]:
    """Локальная копия парсинга ToUnicode если cid_patch_amount недоступен."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end() : m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode().upper(), 16)
                uni_to_cid[uni] = cid
            return uni_to_cid
        if b"beginbfrange" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                src_start = int(mm.group(1).decode().upper(), 16)
                src_end = int(mm.group(2).decode().upper(), 16)
                dest = int(mm.group(3).decode().upper(), 16)
                for i in range(src_end - src_start + 1):
                    uni_to_cid[dest + i] = f"{src_start + i:04X}"
            return uni_to_cid
    return {}


def _extract_tj_blocks_local(pdf_path: Path) -> list[tuple[float, float, bytes]]:
    """Локальная копия извлечения TJ-блоков."""
    data = pdf_path.read_bytes()
    result = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            tj_inner = mm.group(8)
            result.append((x, y, tj_inner))
    return result


def parse_cids_from_tj(tj_inner: bytes) -> tuple[list[str], str | None, str | None]:
    """Извлечь CIDs из содержимого TJ. Возвращает (cids, format, kerning)."""
    cids: list[str] = []
    tj_format: str | None = None
    kerning: str | None = None

    # Literal: (\x02\x1c)-16.66667 — в stream это backslash+x+hex как символы
    # Или бинарные байты: ( \x02 \x1c ) в Python = bytes 0x28 0x02 0x1c 0x29
    kern_pat = rb"\)-([\d.]+)\s"

    # Вариант 1: escaped в stream — \( \x 02 \x 1c \)
    literal_escaped = rb"\\x([0-9a-fA-F]{2})\\x([0-9a-fA-F]{2})"
    # Вариант 2: бинарные байты — ( 0xHH 0xLL ) — один байт после (
    literal_bin = rb"\(([\x00-\xff]{2})\)"
    # Экранированные спецсимволы: \xHH\( или \xHH\) — используем hex для скобок
    literal_esc_paren = rb"\\x([0-9a-fA-F]{2})\x5c\x28"
    literal_esc_rparen = rb"\\x([0-9a-fA-F]{2})\x5c\x29"

    is_literal = (
        b"\\x" in tj_inner
        or (b"(" in tj_inner and b"-" in tj_inner and re.search(kern_pat, tj_inner))
    )

    if is_literal:
        for m in re.finditer(literal_escaped, tj_inner):
            h, l = m.group(1).decode(), m.group(2).decode()
            cid = int(h, 16) * 256 + int(l, 16)
            cids.append(f"{cid:04X}")
        for m in re.finditer(literal_esc_paren, tj_inner):
            h = m.group(1).decode()
            cid = int(h, 16) * 256 + 0x28
            cids.append(f"{cid:04X}")
        for m in re.finditer(literal_esc_rparen, tj_inner):
            h = m.group(1).decode()
            cid = int(h, 16) * 256 + 0x29
            cids.append(f"{cid:04X}")
        # Бинарные 2-байта: ( \xHH \xLL ) — в stream могут быть сырые байты
        for m in re.finditer(literal_bin, tj_inner):
            b1, b2 = m.group(1)[0], m.group(1)[1]
            if b1 >= 0x20 and b1 < 0x80 and b2 >= 0x20 and b2 < 0x80:
                continue  # скорее ASCII пара
            cid = b1 * 256 + b2
            cids.append(f"{cid:04X}")
        # Однобайтовые ASCII: (X)
        for m in re.finditer(rb"\(([\x20-\x7e])\)", tj_inner):
            c = m.group(1)[0]
            if c not in (0x28, 0x29, 0x5C):
                cids.append(f"{c:04X}")
        kern_m = re.search(kern_pat, tj_inner)
        if kern_m:
            kerning = "-" + kern_m.group(1).decode()
        if cids:
            tj_format = "literal"

    # Hex: <021c> или <021c024c>
    if not cids and b"<" in tj_inner:
        hex_pat = rb"<([0-9A-Fa-f]{4})>"
        for m in re.finditer(hex_pat, tj_inner):
            cids.append(m.group(1).decode().upper().zfill(4))
        if not cids:
            hex_block = re.findall(rb"<([0-9A-Fa-f]+)>", tj_inner)
            for block in hex_block:
                s = block.decode().upper()
                for i in range(0, len(s), 4):
                    chunk = s[i : i + 4].zfill(4)
                    if len(chunk) == 4:
                        cids.append(chunk)
        if cids:
            tj_format = "hex"

    return (cids, tj_format, kerning)


def scan_pdf(pdf_path: Path, base_dir: Path) -> dict | None:
    """Сканировать один PDF, вернуть запись для базы или None."""
    parse_tounicode = _parse_tounicode if _parse_tounicode else _parse_tounicode_local
    extract_tj = extract_tj_blocks if extract_tj_blocks else _extract_tj_blocks_local

    try:
        data = pdf_path.read_bytes()
    except OSError as e:
        print(f"[WARN] Не удалось прочитать {pdf_path}: {e}", file=sys.stderr)
        return None

    uni_to_cid = parse_tounicode(data)
    if not uni_to_cid:
        print(f"[WARN] ToUnicode не найден в {pdf_path.name}", file=sys.stderr)
        return None

    blocks = extract_tj(pdf_path)
    if not blocks:
        print(f"[WARN] TJ-блоки не найдены в {pdf_path.name}", file=sys.stderr)

    # cid_to_uni: обратный маппинг (CID hex -> unicode codepoint)
    cid_to_uni: dict[str, int] = {v: k for k, v in uni_to_cid.items()}

    # Определяем формат и kerning по первому блоку справа
    tj_format = "literal"
    kerning = "-16.66667"
    for x, y, tj_inner in blocks:
        if x >= X_MIN_RIGHT and tj_inner:
            _, fmt, kern = parse_cids_from_tj(tj_inner)
            if fmt:
                tj_format = fmt
            if kern:
                kerning = kern
            break

    # Блоки с подсказками по y (типичные координаты СБП)
    blocks_info = []
    for x, y, tj_inner in blocks:
        if x < X_MIN_RIGHT:
            continue
        _, _, _ = parse_cids_from_tj(tj_inner)
        hint = _guess_block_hint(y)
        blocks_info.append({"x": round(x, 2), "y": round(y, 2), "hint": hint})

    rel_path = pdf_path.relative_to(base_dir) if pdf_path.is_relative_to(base_dir) else pdf_path.name

    return {
        "path": str(rel_path),
        "uni_to_cid": {str(k): v for k, v in uni_to_cid.items()},
        "cid_to_uni": {k: v for k, v in cid_to_uni.items()},
        "tj_format": tj_format,
        "kerning": kerning,
        "blocks": blocks_info,
        "char_count": len(uni_to_cid),
    }


def _guess_block_hint(y: float) -> str:
    """Угадать назначение блока по y-координате (типичные значения СБП)."""
    if abs(y - 348.75) < 3:
        return "payer"
    if abs(y - 330) < 3:
        return "recipient_or_phone"
    if abs(y - 227.25) < 3:
        return "payer_alt"
    if 300 < y < 360:
        return "value_col"
    if 200 < y < 250:
        return "value_col_alt"
    return "unknown"


def score_donor_for_sbp(entry: dict) -> int:
    """Оценка донора для шаблона СБП: больше покрытие приоритетных символов — лучше."""
    uni_to_cid = entry.get("uni_to_cid", {})
    if not uni_to_cid:
        return 0
    score = 0
    for c in SBP_PRIORITY_CHARS:
        cp = ord(c)
        if str(cp) in uni_to_cid:
            score += 1
    return score


def build_sbp_template(db: dict, base_dir: Path, template_path: Path) -> bool:
    """Выбрать лучшего донора и сохранить шаблон СБП."""
    if not db:
        print("[WARN] База пуста, шаблон не создан", file=sys.stderr)
        return False

    best_name = max(db.keys(), key=lambda k: score_donor_for_sbp(db[k]))
    best = db[best_name]
    donor_full = base_dir / best["path"]
    if not donor_full.is_absolute():
        donor_full = (base_dir / best["path"]).resolve()

    template = {
        "donor": best["path"],
        "uni_to_cid": best["uni_to_cid"],
        "tj_format": best["tj_format"],
        "kerning": best["kerning"],
        "fields": {
            "payer": {"y": 348.75, "ytol": 2, "xmin": X_MIN_RIGHT},
            "recipient": {"y": 330, "ytol": 2, "xmin": X_MIN_RIGHT},
            "payer_alt": {"y": 227.25, "ytol": 2, "xmin": X_MIN_RIGHT},
            "amount": {"y": 313, "ytol": 10, "xmin": X_MIN_RIGHT},
        },
    }

    template_path.parent.mkdir(parents=True, exist_ok=True)
    with open(template_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)

    print(f"[OK] Шаблон СБП: {template_path} (донор: {best_name})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сканирование donors, построение базы CID и шаблона СБП"
    )
    parser.add_argument(
        "--donors",
        "-d",
        default="donors",
        help="Папка с донорами (рекурсивно)",
    )
    parser.add_argument(
        "--extra",
        "-e",
        action="append",
        default=[],
        help="Доп. папки (СБП, карта_на_карту и т.д.)",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="donors_cid_db.json",
        help="Выходной JSON базы",
    )
    parser.add_argument(
        "--template",
        "-t",
        default=None,
        help="Путь к шаблону СБП (templates/sbp_cid_template.json)",
    )
    args = parser.parse_args()

    base = Path(__file__).parent.resolve()
    donors_dir = (base / args.donors).resolve()
    if not donors_dir.exists():
        print(f"[ERROR] Папка не найдена: {donors_dir}", file=sys.stderr)
        return 1

    # Собираем все PDF
    pdf_paths: list[Path] = []
    for folder in [donors_dir] + [(base / p).resolve() for p in args.extra if p]:
        if folder.exists():
            pdf_paths.extend(folder.rglob("*.pdf"))

    pdf_paths = sorted(set(p for p in pdf_paths if p.is_file()))
    if not pdf_paths:
        print(f"[WARN] PDF не найдены в {donors_dir}", file=sys.stderr)
        out_path = base / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
        return 0

    db: dict = {}
    for pdf_path in pdf_paths:
        base_for_rel = pdf_path.parent
        entry = scan_pdf(pdf_path, base_for_rel)
        if entry:
            name = pdf_path.name
            db[name] = entry
            print(f"[OK] {name}: {entry['char_count']} символов, {entry['tj_format']}")

    out_path = base / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"[OK] База сохранена: {out_path} ({len(db)} PDF)")

    if args.template:
        template_path = base / args.template
        build_sbp_template(db, base, template_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
