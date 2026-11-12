#!/usr/bin/env python3
"""Патч полей в чеке СБП по шаблону CID.

Заменяет текст в полях (плательщик, сумма и т.д.) с использованием только CIDs
из CMap донора. Сохраняет структуру PDF, метаданные, обновляет xref/Length.
При замене — только символы из CMap (homoglyph fallback), без Identity.

Использование:
  python3 patch_sbp_template.py donor.pdf out.pdf --payer "Бабаян Арман М." --amount "1 600 ₽"
  python3 patch_sbp_template.py donor.pdf out.pdf --payer "Иванов И.И." --patch-id
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
X_MIN_RIGHT = 100

# Кириллица → латиница для fallback (только если латиница есть в CMap)
_CYRILLIC_FALLBACK = {
    0x0410: 0x0041,  # А → A
    0x0412: 0x0042,  # В → B
    0x0415: 0x0045,  # Е → E
    0x041A: 0x004B,  # К → K
    0x041C: 0x004D,  # М → M
    0x041E: 0x004F,  # О → O
    0x041F: 0x0050,  # П → P
    0x0421: 0x0043,  # С → C
    0x0422: 0x0054,  # Т → T
    0x0425: 0x0058,  # Х → X
    0x0430: 0x0061,  # а → a
    0x0435: 0x0065,  # е → e
    0x043E: 0x006F,  # о → o
    0x043F: 0x0070,  # п → p
    0x0440: 0x0072,  # р → r
    0x0441: 0x0063,  # с → c
    0x0442: 0x0074,  # т → t
    0x0443: 0x0079,  # у → y
    0x0445: 0x0078,  # х → x
}


def build_tj(cids: list[str], kern: str = "-16.66667") -> bytes:
    """Собрать TJ-блок из списка CIDs в формате (bytes)-kern.
    Экранирует 0x28 '(' и 0x29 ')' для корректного PDF."""
    parts = []
    kern_b = kern.encode()
    for cid_hex in cids:
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        if l == 0x28:
            s = b"(\\x%02x\\()" % h
        elif l == 0x29:
            s = b"(\\x%02x\\))" % h
        elif h == 0 and l < 0x80 and 0x20 <= l <= 0x7E and l not in (0x28, 0x29, 0x5C):
            s = bytes([0x28, l, 0x29])
        else:
            s = b"(\\x%02x\\x%02x)" % (h, l)
        parts.append(s + b"-" + kern_b + b" ")
    return b"".join(parts)


def build_tj_hex(cids: list[str], kern: str = "-16.66667") -> bytes:
    """Собрать TJ в hex-формате: <021c><024c>-kern ..."""
    kern_b = kern.encode()
    parts = []
    for cid_hex in cids:
        parts.append(b"<" + cid_hex.upper().encode() + b">-" + kern_b + b" ")
    return b"".join(parts)


def encode_text_to_cids(text: str, uni_to_cid: dict[int, str], use_homoglyph: bool = True) -> list[str] | None:
    """Закодировать текст в список CID hex. Возвращает None если символ отсутствует."""
    cids = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid and use_homoglyph and cp in _CYRILLIC_FALLBACK:
            alt = _CYRILLIC_FALLBACK[cp]
            if alt in uni_to_cid:
                cp = alt
        if cp not in uni_to_cid:
            return None
        cids.append(uni_to_cid[cp])
    return cids


def load_template(template_path: Path | None) -> dict | None:
    """Загрузить шаблон СБП. uni_to_cid с int-ключами."""
    if not template_path or not template_path.exists():
        return None
    with open(template_path, encoding="utf-8") as f:
        t = json.load(f)
    # Конвертируем ключи uni_to_cid в int
    utc = t.get("uni_to_cid", {})
    t["uni_to_cid"] = {int(k): v for k, v in utc.items()}
    return t


def parse_tounicode(data: bytes) -> dict[int, str]:
    """Парсинг ToUnicode из PDF."""
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


def extract_tj_blocks(data: bytes) -> list[tuple[float, float, bytes, int, int, int]]:
    """Извлечь TJ-блоки: (x, y, tj_inner, stream_start, stream_len, len_num_start)."""
    result = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
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
            result.append((x, y, tj_inner, stream_start, stream_len, len_num_start))
    return result


def patch_sbp(
    donor_path: Path,
    out_path: Path,
    replacements: dict[str, str],
    template_path: Path | None = None,
    patch_id: bool = False,
    repair: bool = False,
) -> bool:
    """
    Применить замены к donor PDF. replacements: {"payer": "Текст", "amount": "1 600 ₽", ...}
    """
    data = bytearray(donor_path.read_bytes())
    orig_size = len(data)

    # Загружаем CMap
    template = load_template(template_path) if template_path else None
    if template:
        uni_to_cid = template["uni_to_cid"]
        tj_format = template.get("tj_format", "literal")
        kerning = template.get("kerning", "-16.66667")
        fields_config = template.get("fields", {})
    else:
        uni_to_cid = parse_tounicode(data)
        if not uni_to_cid:
            print("[ERROR] ToUnicode CMap не найден в donor", file=sys.stderr)
            return False
        tj_format = "literal"
        kerning = "-16.66667"
        fields_config = {
            "payer": {"y": 348.75, "ytol": 2, "xmin": X_MIN_RIGHT},
            "recipient": {"y": 330, "ytol": 2, "xmin": X_MIN_RIGHT},
            "payer_alt": {"y": 227.25, "ytol": 2, "xmin": X_MIN_RIGHT},
            "amount": {"y": 313, "ytol": 5, "xmin": X_MIN_RIGHT},
        }

    # Добавляем недостающие поля в конфиг (напр. amount)
    for fn in replacements:
        if fn not in fields_config:
            fields_config[fn] = {"y": 330, "ytol": 5, "xmin": X_MIN_RIGHT}

    # Добавляем amount в fields_config если нужно (шаблон может не содержать)
    DEFAULT_FIELDS = {
        "amount": {"y": 313, "ytol": 10, "xmin": X_MIN_RIGHT},
    }
    for k, v in DEFAULT_FIELDS.items():
        if k in replacements and k not in fields_config:
            fields_config[k] = v

    # Собираем блоки и определяем замены по (x,y)
    blocks = extract_tj_blocks(data)
    repl_by_pos: dict[tuple[float, float], bytes] = {}
    for x, y, tj_inner, stream_start, stream_len, _ in blocks:
        if x < X_MIN_RIGHT:
            continue
        for field_name, new_text in replacements.items():
            if not new_text:
                continue
            cfg = fields_config.get(field_name)
            if not cfg:
                continue
            if abs(y - cfg["y"]) > cfg.get("ytol", 2):
                continue
            if x < cfg.get("xmin", X_MIN_RIGHT):
                continue
            cids = encode_text_to_cids(new_text, uni_to_cid)
            if not cids:
                continue
            if tj_format == "literal":
                new_tj = build_tj(cids, kerning)
            else:
                new_tj = build_tj_hex(cids, kerning)
            repl_by_pos[(round(x, 2), round(y, 2))] = new_tj
            print(f"[OK] {field_name} ({x:.1f}, {y:.1f}): -> {new_text[:30]}...")
            break

    if not repl_by_pos:
        print("[ERROR] Не найдены подходящие TJ-блоки для замены", file=sys.stderr)
        return False

    # Патчим streams
    mods = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        def replacer(match):
            x, y = float(match.group(2)), float(match.group(4))
            key = (round(x, 2), round(y, 2))
            if key not in repl_by_pos:
                return match.group(0)
            new_inner = repl_by_pos[key]
            grp = match.lastindex
            inner_start = match.start(grp) - match.start(0)
            inner_end = match.end(grp) - match.start(0)
            return match.group(0)[:inner_start] + new_inner + match.group(0)[inner_end:]

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\]\s*TJ)'
        new_dec = re.sub(pat, replacer, dec)
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[([^\]]*)\]\s*TJ)'
        if new_dec == dec:
            new_dec = re.sub(pat2, replacer, dec)

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Не удалось применить замены", file=sys.stderr)
        return False

    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)

    if patch_id:
        try:
            from patch_id import patch_document_id
            if patch_document_id(out_path):
                print("[OK] Document ID заменён")
        except ImportError:
            print("[WARN] patch_id не найден", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] patch_id: {e}", file=sys.stderr)

    if repair:
        try:
            tmp = out_path.with_suffix(".tmp.pdf")
            subprocess.run(
                ["qpdf", "--linearize", str(out_path), str(tmp)],
                check=True, capture_output=True
            )
            tmp.replace(out_path)
            print("[OK] PDF восстановлен (qpdf --linearize) для совместимости с Acrobat")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[WARN] qpdf не выполнен: {e}", file=sys.stderr)

    print(f"[OK] Сохранено: {out_path} ({len(data)} bytes)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Патч полей в чеке СБП по CID-шаблону"
    )
    parser.add_argument("donor", help="Donor PDF")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("--template", "-t", default=None, help="Путь к sbp_cid_template.json")
    parser.add_argument("--payer", "-p", default=None, help="Имя плательщика")
    parser.add_argument("--amount", "-a", default=None, help="Сумма (напр. 1 600 ₽)")
    parser.add_argument("--recipient", "-r", default=None, help="Получатель")
    parser.add_argument("--patch-id", action="store_true", help="Заменить Document ID")
    parser.add_argument("--repair", action="store_true", help="Восстановить PDF (qpdf --linearize) для Acrobat")
    args = parser.parse_args()

    donor = Path(args.donor).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    if not donor.exists():
        print(f"[ERROR] Donor не найден: {donor}", file=sys.stderr)
        return 1

    template_path = None
    if args.template:
        template_path = Path(args.template).expanduser().resolve()
    else:
        default_tpl = ROOT / "templates" / "sbp_cid_template.json"
        if default_tpl.exists():
            template_path = default_tpl

    replacements = {}
    if args.payer:
        replacements["payer"] = args.payer
    if args.amount:
        replacements["amount"] = args.amount
    if args.recipient:
        replacements["recipient"] = args.recipient

    if not replacements:
        print("Укажите хотя бы одно поле: --payer, --amount, --recipient", file=sys.stderr)
        return 1

    ok = patch_sbp(donor, out, replacements, template_path=template_path, patch_id=args.patch_id, repair=args.repair)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
