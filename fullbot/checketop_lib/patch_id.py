#!/usr/bin/env python3
"""
Замена Document ID в PDF на случайный (32 hex символа).
Не меняет структуру файла — только hex-значения внутри /ID (длина та же).
"""
import argparse
import re
import secrets
import sys
from pathlib import Path


def patch_document_id_one_nibble(
    pdf_path: Path,
    *,
    which: int = 2,
    pos_from_end: int = 0,
) -> bool:
    """
    Изменить ровно один hex-символ в одной из строк /ID (без полной замены).

    Нужно для проверок, которые по CHECK_VERIFICATION_RULES.md флагают **полную**
    подмену /ID как подделку; для чеков разрешён только один символ.

    which: 1 = первый hex (ID[0]), 2 = второй (ID[1]) — по умолчанию второй,
           как «instance id» при неизменном «документном» ID[0].
    pos_from_end: 0 = последний символ, 1 = предпоследний, …
    """
    if which not in (1, 2):
        raise ValueError("which must be 1 or 2")

    data = bytearray(pdf_path.read_bytes())
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", bytes(data))
    if not m:
        return False

    h1 = m.group(1).decode()
    h2 = m.group(2).decode()
    if len(h1) != len(h2):
        return False

    if which == 1:
        s, other = h1, h2
        start, end = m.start(1), m.end(1)
    else:
        s, other = h2, h1
        start, end = m.start(2), m.end(2)

    idx = len(s) - 1 - pos_from_end
    if idx < 0 or idx >= len(s):
        return False

    def flip_at(s: str, i: int, delta: int) -> str:
        c = s[i]
        lo = c.lower()
        if lo not in "0123456789abcdef":
            lo = "0"
        j = int(lo, 16)
        nl = f"{(j + delta) % 16:x}"
        nc = nl.upper() if c.isupper() else nl
        return s[:i] + nc + s[i + 1 :]

    new_s = None
    for delta in range(1, 17):
        cand = flip_at(s, idx, delta)
        if cand != other:
            new_s = cand
            break

    if new_s is None:
        return False

    data[start:end] = new_s.encode("ascii")
    pdf_path.write_bytes(bytes(data))
    return True


def patch_document_id(pdf_path: Path, new_id_hex: str | None = None) -> bool:
    """
    Заменить /ID [ <hex1> <hex2> ] на два РАЗНЫХ новых ID той же длины.

    Oracle BI Publisher всегда генерирует два разных ID:
      ID[0] = постоянный идентификатор документа (creation ID)
      ID[1] = идентификатор экземпляра/модификации (instance ID, меняется при сохранении)
    Установка ID[0] == ID[1] — явный форензик-флаг подделки.

    new_id_hex: если задан, используется как ID[0]; ID[1] генерируется независимо.
    """
    data = pdf_path.read_bytes()
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", data)
    if not m:
        return False

    old1, old2 = m.group(1), m.group(2)
    target_len = len(old1)
    if len(old2) != target_len:
        return False

    # Generate ID[0]
    if new_id_hex is None:
        new_id1 = secrets.token_hex(target_len // 2).lower()
    else:
        new_id1 = new_id_hex.lower()
    if len(new_id1) != target_len:
        raise ValueError(f"ID должен быть ровно {target_len} hex-символов, получено {len(new_id1)}")
    if not re.match(r"^[0-9a-f]+$", new_id1):
        raise ValueError("ID должен содержать только 0-9 и a-f")

    # Generate ID[1] independently — must differ from ID[0]
    new_id2 = secrets.token_hex(target_len // 2).lower()
    while new_id2 == new_id1:
        new_id2 = secrets.token_hex(target_len // 2).lower()

    new_b1 = new_id1.encode("ascii")
    new_b2 = new_id2.encode("ascii")
    data = data.replace(b"<" + old1 + b">", b"<" + new_b1 + b">", 1)
    data = data.replace(b"<" + old2 + b">", b"<" + new_b2 + b">", 1)
    pdf_path.write_bytes(data)
    return True


def patch_moddate(pdf_path: Path, date_str: str) -> bool:
    """
    Обновить /ModDate в Info-словаре PDF.

    date_str: дата в формате ДД.ММ.ГГГГ (например '10.03.2026').
    Новое значение: D:YYYYMMDD120000+03'00' (полдень, московское время).

    Заменяет в raw-байтах без изменения длины файла (подстрока фиксированной длины).
    Если длина меняется — обновляет xref и startxref.
    """
    parts = date_str.split(".")
    if len(parts) != 3:
        return False
    dd, mm, yyyy = parts
    new_moddate = f"D:{yyyy}{mm}{dd}120000+03'00'"

    data = bytearray(pdf_path.read_bytes())
    # Match /ModDate (D:YYYYMMDDHHmmSS+HH'mm') in PDF Info dict
    m = re.search(rb"/ModDate\s*\(([^)]+)\)", data)
    if not m:
        return False

    old_val = m.group(1)
    new_val = new_moddate.encode("ascii")
    old_full = m.group(0)
    # No space after /ModDate — matches iText / Oracle BI Publisher output (strict validators compare raw bytes)
    new_full = b"/ModDate(" + new_val + b")"

    if old_full == new_full:
        return True  # already correct

    if len(new_full) == len(old_full):
        data[m.start():m.end()] = new_full
    else:
        # Size change — replace and update xref if needed
        delta = len(new_full) - len(old_full)
        change_pos = m.start()
        data = bytearray(bytes(data[:change_pos]) + new_full + bytes(data[m.end():]))

        # Update /Length for the containing stream/object if present (Info is usually uncompressed)
        # Update xref offsets for objects after change_pos
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > change_pos:
                    entries[em.start(1):em.start(1)+10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3):xref_m.end(3)] = bytes(entries)
        sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if sxref_m and change_pos < int(sxref_m.group(1)):
            pos = sxref_m.start(1)
            old_pos = int(sxref_m.group(1))
            data[pos:pos+len(str(old_pos))] = str(old_pos + delta).encode()

    pdf_path.write_bytes(bytes(data))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Заменить Document ID в PDF на случайный (32 hex).")
    parser.add_argument("pdf", nargs="?", default="output.pdf", help="Путь к PDF (по умолчанию output.pdf)")
    parser.add_argument("--id", dest="custom_id", default=None, help="Свой 32-символьный hex (иначе генерируется случайный)")
    args = parser.parse_args()

    path = Path(args.pdf).expanduser().resolve()
    if not path.exists():
        print(f"[ERROR] Файл не найден: {path}", file=sys.stderr)
        return 1

    try:
        ok = patch_document_id(path, args.custom_id)
        if not ok:
            print("[ERROR] /ID не найден в PDF.", file=sys.stderr)
            return 1
        new_hex = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", path.read_bytes())
        if new_hex:
            print(f"[OK] Document ID заменён на: {new_hex.group(1).decode()}")
        return 0
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
