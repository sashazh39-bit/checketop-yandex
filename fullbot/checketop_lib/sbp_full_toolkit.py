#!/usr/bin/env python3
"""Полный набор инструментов для работы с чеками СБП (ВТБ).

1. scan    — сканирует все PDF в donors, собирает объединённую базу CID
2. template — создаёт шаблон СБП из лучшего донора
3. patch   — безопасная замена полей (без крашей, только CID из CMap)
4. merge   — объединяет несколько PDF в один документ

Использование:
  python3 sbp_full_toolkit.py scan
  python3 sbp_full_toolkit.py template
  python3 sbp_full_toolkit.py patch donor.pdf out.pdf --payer "Иванов И.И." --amount "1 600 ₽"
  python3 sbp_full_toolkit.py merge -o combined.pdf a.pdf b.pdf c.pdf
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
DONORS_DIR = ROOT / "donors"
TEMPLATES_DIR = ROOT / "templates"
X_MIN_RIGHT = 100

# Кириллица → латиница для fallback (только если латиница есть в CMap)
_CYRILLIC_FALLBACK = {
    0x0410: 0x0041, 0x0412: 0x0042, 0x0415: 0x0045, 0x041A: 0x004B,
    0x041C: 0x004D, 0x041E: 0x004F, 0x041F: 0x0050, 0x0421: 0x0043,
    0x0422: 0x0054, 0x0425: 0x0058, 0x0430: 0x0061, 0x0435: 0x0065,
    0x043E: 0x006F, 0x043F: 0x0070, 0x0440: 0x0072, 0x0441: 0x0063,
    0x0442: 0x0074, 0x0443: 0x0079, 0x0445: 0x0078,
}

SBP_PRIORITY_CHARS = set(
    "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюяё"
    "0123456789 ₽.,:-"
)


# --- Парсинг ToUnicode ---
def parse_tounicode(data: bytes) -> dict[int, str]:
    """Парсинг ToUnicode CMap из PDF."""
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
        if b"beginbfrange" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                src_start = int(mm.group(1).decode().upper(), 16)
                src_end = int(mm.group(2).decode().upper(), 16)
                dest = int(mm.group(3).decode().upper(), 16)
                for i in range(src_end - src_start + 1):
                    uni_to_cid[dest + i] = f"{src_start + i:04X}"
        if uni_to_cid:
            return uni_to_cid
    return uni_to_cid


def extract_tj_blocks(data: bytes) -> list[tuple[float, float, bytes]]:
    """Извлечь TJ-блоки (x, y, tj_inner) из content streams."""
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
            result.append((x, y, mm.group(8)))
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[([^\]]*)\]\s*TJ)'
        for mm in re.finditer(pat2, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            result.append((x, y, mm.group(7)))
    return result


def parse_cids_from_tj(tj_inner: bytes) -> tuple[list[str], str | None, str | None]:
    """Извлечь CIDs, формат и kerning из TJ."""
    cids: list[str] = []
    tj_format: str | None = None
    kerning: str | None = None
    kern_pat = rb"\)-([\d.]+)\s"
    literal_escaped = rb"\\x([0-9a-fA-F]{2})\\x([0-9a-fA-F]{2})"
    literal_bin = rb"\(([\x00-\xff]{2})\)"
    literal_esc_paren = rb"\\x([0-9a-fA-F]{2})\x5c\x28"
    literal_esc_rparen = rb"\\x([0-9a-fA-F]{2})\x5c\x29"
    is_literal = b"\\x" in tj_inner or (b"(" in tj_inner and b"-" in tj_inner and re.search(kern_pat, tj_inner))
    if is_literal:
        for m in re.finditer(literal_escaped, tj_inner):
            h, l = m.group(1).decode(), m.group(2).decode()
            cids.append(f"{int(h, 16) * 256 + int(l, 16):04X}")
        for m in re.finditer(literal_esc_paren, tj_inner):
            cids.append(f"{int(m.group(1).decode(), 16) * 256 + 0x28:04X}")
        for m in re.finditer(literal_esc_rparen, tj_inner):
            cids.append(f"{int(m.group(1).decode(), 16) * 256 + 0x29:04X}")
        for m in re.finditer(literal_bin, tj_inner):
            b1, b2 = m.group(1)[0], m.group(1)[1]
            if not (0x20 <= b1 < 0x80 and 0x20 <= b2 < 0x80):
                cids.append(f"{b1 * 256 + b2:04X}")
        for m in re.finditer(rb"\(([\x20-\x7e])\)", tj_inner):
            c = m.group(1)[0]
            if c not in (0x28, 0x29, 0x5C):
                cids.append(f"{c:04X}")
        if kern_m := re.search(kern_pat, tj_inner):
            kerning = "-" + kern_m.group(1).decode()
        if cids:
            tj_format = "literal"
    if not cids and b"<" in tj_inner:
        for m in re.finditer(rb"<([0-9A-Fa-f]{4})>", tj_inner):
            cids.append(m.group(1).decode().upper().zfill(4))
        if cids:
            tj_format = "hex"
    return (cids, tj_format, kerning)


# --- Сканирование ---
def scan_pdf(pdf_path: Path, base_dir: Path) -> dict | None:
    """Сканировать один PDF."""
    try:
        data = pdf_path.read_bytes()
    except OSError as e:
        print(f"[WARN] Не удалось прочитать {pdf_path}: {e}", file=sys.stderr)
        return None
    uni_to_cid = parse_tounicode(data)
    if not uni_to_cid:
        print(f"[WARN] ToUnicode не найден в {pdf_path.name}", file=sys.stderr)
        return None
    blocks = extract_tj_blocks(data)
    cid_to_uni = {v: k for k, v in uni_to_cid.items()}
    tj_format, kerning = "literal", "-16.66667"
    for x, y, tj_inner in blocks:
        if x >= X_MIN_RIGHT and tj_inner:
            _, fmt, kern = parse_cids_from_tj(tj_inner)
            if fmt:
                tj_format = fmt
            if kern:
                kerning = kern
            break
    rel = pdf_path.relative_to(base_dir) if pdf_path.is_relative_to(base_dir) else pdf_path.name
    return {
        "path": str(rel),
        "uni_to_cid": {str(k): v for k, v in uni_to_cid.items()},
        "cid_to_uni": cid_to_uni,
        "tj_format": tj_format,
        "kerning": kerning,
        "char_count": len(uni_to_cid),
    }


def build_merged_cid_db(db: dict) -> dict:
    """Объединить uni_to_cid из всех доноров. Для каждого Unicode — список (donor, cid)."""
    merged: dict[int, list[tuple[str, str]]] = {}
    for name, entry in db.items():
        utc = entry.get("uni_to_cid", {})
        for k, v in utc.items():
            cp = int(k)
            if cp not in merged:
                merged[cp] = []
            merged[cp].append((name, v))
    return merged


def cmd_scan(donors_dir: Path, out_path: Path, extra_dirs: list[Path]) -> int:
    """Сканировать donors, сохранить базу и объединённую CID-карту."""
    pdf_paths: list[Path] = []
    for folder in [donors_dir] + extra_dirs:
        if folder.exists():
            pdf_paths.extend(folder.rglob("*.pdf"))
    pdf_paths = sorted(set(p for p in pdf_paths if p.is_file()))
    if not pdf_paths:
        print(f"[WARN] PDF не найдены в {donors_dir}", file=sys.stderr)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"donors": {}, "merged_unicode": {}}, f, ensure_ascii=False, indent=2)
        return 0
    db: dict = {}
    for pdf_path in pdf_paths:
        base_for_rel = pdf_path.parent
        entry = scan_pdf(pdf_path, base_for_rel)
        if entry:
            db[pdf_path.name] = entry
            print(f"[OK] {pdf_path.name}: {entry['char_count']} символов, {entry['tj_format']}")
    merged = build_merged_cid_db(db)
    merged_serializable = {}
    for cp, donors_list in merged.items():
        merged_serializable[str(cp)] = {d: c for d, c in donors_list}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"donors": db, "merged_unicode": merged_serializable}, f, ensure_ascii=False, indent=2)
    print(f"[OK] База сохранена: {out_path} ({len(db)} PDF, {len(merged)} уникальных символов)")
    return 0


# --- Шаблон ---
def score_donor(entry: dict) -> int:
    """Оценка донора для СБП."""
    utc = entry.get("uni_to_cid", {})
    return sum(1 for c in SBP_PRIORITY_CHARS if str(ord(c)) in utc)


def cmd_template(db_path: Path, template_path: Path) -> int:
    """Создать шаблон СБП из лучшего донора."""
    if not db_path.exists():
        print(f"[ERROR] База не найдена: {db_path}. Запустите: sbp_full_toolkit.py scan", file=sys.stderr)
        return 1
    with open(db_path, encoding="utf-8") as f:
        data = json.load(f)
    db = data.get("donors", data) if isinstance(data.get("donors"), dict) else data
    if not db:
        print("[ERROR] База пуста", file=sys.stderr)
        return 1
    best_name = max(db.keys(), key=lambda k: score_donor(db[k]))
    best = db[best_name]
    template = {
        "donor": best["path"],
        "uni_to_cid": best["uni_to_cid"],
        "tj_format": best.get("tj_format", "literal"),
        "kerning": best.get("kerning", "-16.66667"),
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
    return 0


# --- Патч (безопасный) ---
def encode_text_to_cids(text: str, uni_to_cid: dict, use_homoglyph: bool = True) -> list[str] | None:
    """Закодировать текст в CIDs. None если символ отсутствует. uni_to_cid: {str(codepoint): cid_hex}."""
    cids = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and "32" not in uni_to_cid and "160" in uni_to_cid:
            cp = 0xA0
        key = str(cp)
        if key not in uni_to_cid and use_homoglyph and cp in _CYRILLIC_FALLBACK:
            alt = _CYRILLIC_FALLBACK[cp]
            if str(alt) in uni_to_cid:
                key = str(alt)
        if key not in uni_to_cid:
            return None
        cids.append(uni_to_cid[key])
    return cids


def build_tj(cids: list[str], kern: str = "-16.66667", tj_format: str = "literal") -> bytes:
    """Собрать TJ-блок."""
    kern_b = kern.encode()
    if tj_format == "hex":
        return b"".join(b"<" + c.upper().encode() + b">-" + kern_b + b" " for c in cids)
    parts = []
    for cid_hex in cids:
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        if l == 0x28:
            s = b"(\\x%02x\\()" % h
        elif l == 0x29:
            s = b"(\\x%02x\\))" % h
        elif h == 0 and 0x20 <= l <= 0x7E and l not in (0x28, 0x29, 0x5C):
            s = bytes([0x28, l, 0x29])
        else:
            s = b"(\\x%02x\\x%02x)" % (h, l)
        parts.append(s + b"-" + kern_b + b" ")
    return b"".join(parts)


def cmd_patch(
    donor_path: Path,
    out_path: Path,
    replacements: dict[str, str],
    template_path: Path | None,
    patch_id: bool = False,
    repair: bool = False,
) -> int:
    """Безопасный патч полей."""
    data = bytearray(donor_path.read_bytes())
    if template_path and template_path.exists():
        with open(template_path, encoding="utf-8") as f:
            t = json.load(f)
        uni_to_cid = {str(k): str(v) for k, v in t.get("uni_to_cid", {}).items()}
        tj_format = t.get("tj_format", "literal")
        kerning = t.get("kerning", "-16.66667")
        fields_config = t.get("fields", {})
    else:
        uni_to_cid_raw = parse_tounicode(data)
        if not uni_to_cid_raw:
            print("[ERROR] ToUnicode не найден в donor", file=sys.stderr)
            return 1
        uni_to_cid = {str(k): v for k, v in uni_to_cid_raw.items()}
        tj_format = "literal"
        kerning = "-16.66667"
        fields_config = {
            "payer": {"y": 348.75, "ytol": 2, "xmin": X_MIN_RIGHT},
            "recipient": {"y": 330, "ytol": 2, "xmin": X_MIN_RIGHT},
            "payer_alt": {"y": 227.25, "ytol": 2, "xmin": X_MIN_RIGHT},
            "amount": {"y": 313, "ytol": 10, "xmin": X_MIN_RIGHT},
        }
    for fn in replacements:
        if fn not in fields_config:
            fields_config[fn] = {"y": 330, "ytol": 5, "xmin": X_MIN_RIGHT}
    repl_by_pos: dict[tuple[float, float], bytes] = {}
    seen_keys: set[tuple[float, float]] = set()
    for x, y, tj_inner, stream_start, stream_len, len_num_start in _extract_blocks_with_pos(data):
        if x < X_MIN_RIGHT:
            continue
        key = (round(x, 2), round(y, 2))
        if key in seen_keys:
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
                print(f"[WARN] Символы для '{new_text[:30]}...' отсутствуют в CMap, пропуск поля {field_name}", file=sys.stderr)
                continue
            new_tj = build_tj(cids, kerning, tj_format)
            repl_by_pos[key] = new_tj
            seen_keys.add(key)
            print(f"[OK] {field_name} ({x:.1f}, {y:.1f}): -> {new_text[:30]}...")
            break
    if not repl_by_pos:
        print("[ERROR] Не найдены подходящие TJ-блоки для замены", file=sys.stderr)
        return 1
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
            grp = match.lastindex
            inner_start = match.start(grp) - match.start(0)
            inner_end = match.end(grp) - match.start(0)
            return match.group(0)[:inner_start] + repl_by_pos[key] + match.group(0)[inner_end:]

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
        return 1
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
        except Exception as e:
            print(f"[WARN] patch_id: {e}", file=sys.stderr)
    if repair:
        try:
            tmp = out_path.with_suffix(".tmp.pdf")
            subprocess.run(["qpdf", "--linearize", str(out_path), str(tmp)], check=True, capture_output=True)
            tmp.replace(out_path)
            print("[OK] PDF восстановлен (qpdf)")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    print(f"[OK] Сохранено: {out_path} ({len(data)} bytes)")
    return 0


def _extract_blocks_with_pos(data: bytes) -> list[tuple[float, float, bytes, int, int, int]]:
    """Извлечь TJ-блоки с позициями stream."""
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
            result.append((float(mm.group(2)), float(mm.group(4)), mm.group(8), stream_start, stream_len, len_num_start))
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[([^\]]*)\]\s*TJ)'
        for mm in re.finditer(pat2, dec):
            result.append((float(mm.group(2)), float(mm.group(4)), mm.group(7), stream_start, stream_len, len_num_start))
    return result


# --- Merge PDF pages ---
def cmd_merge(output_path: Path, pdf_paths: list[Path]) -> int:
    """Объединить несколько PDF в один (конкатенация страниц)."""
    try:
        import pikepdf
    except ImportError:
        print("[ERROR] Установите pikepdf: pip install pikepdf", file=sys.stderr)
        return 1
    if len(pdf_paths) < 2:
        print("[ERROR] Нужно минимум 2 PDF для объединения", file=sys.stderr)
        return 1
    for p in pdf_paths:
        if not p.exists():
            print(f"[ERROR] Файл не найден: {p}", file=sys.stderr)
            return 1
    out = pikepdf.Pdf.new()
    for pdf_path in pdf_paths:
        src = pikepdf.open(pdf_path)
        out.pages.extend(src.pages)
        src.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path, linearize=False, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    out.close()
    print(f"[OK] Объединено {len(pdf_paths)} PDF в {output_path}")
    return 0


# --- Repair (Acrobat) ---
def cmd_repair(input_path: Path, output_path: Path | None) -> int:
    """Восстановить PDF для корректного отображения в Acrobat (qpdf --linearize)."""
    try:
        out = output_path or input_path.with_stem(input_path.stem + "_repaired")
        if out.resolve() == input_path.resolve():
            tmp = input_path.with_stem(input_path.stem + "_tmp")
            subprocess.run(["qpdf", "--linearize", str(input_path), str(tmp)], check=True, capture_output=True)
            tmp.replace(input_path)
            print(f"[OK] PDF восстановлен (in-place): {input_path}")
        else:
            subprocess.run(["qpdf", "--linearize", str(input_path), str(out)], check=True, capture_output=True)
            print(f"[OK] PDF восстановлен: {out}")
        return 0
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        print(f"[ERROR] qpdf: {err}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("[ERROR] Установите qpdf: brew install qpdf", file=sys.stderr)
        return 1


# --- Main ---
def main() -> int:
    parser = argparse.ArgumentParser(description="Инструменты для чеков СБП (ВТБ)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    # scan
    p_scan = sub.add_parser("scan", help="Сканировать donors, собрать базу CID")
    p_scan.add_argument("--donors", "-d", default="donors", help="Папка donors")
    p_scan.add_argument("--out", "-o", default="donors_cid_db.json", help="Выходной JSON")
    p_scan.add_argument("--extra", "-e", action="append", default=[], help="Доп. папки (СБП и т.д.)")
    # template
    p_tpl = sub.add_parser("template", help="Создать шаблон СБП")
    p_tpl.add_argument("--db", default="donors_cid_db.json", help="База CID")
    p_tpl.add_argument("--out", "-o", default="templates/sbp_cid_template.json", help="Шаблон")
    # patch
    p_patch = sub.add_parser("patch", help="Безопасная замена полей")
    p_patch.add_argument("donor", help="Donor PDF")
    p_patch.add_argument("output", help="Выходной PDF")
    p_patch.add_argument("--template", "-t", default=None, help="Шаблон (иначе из donor)")
    p_patch.add_argument("--payer", "-p", default=None)
    p_patch.add_argument("--amount", "-a", default=None)
    p_patch.add_argument("--recipient", "-r", default=None)
    p_patch.add_argument("--patch-id", action="store_true")
    p_patch.add_argument("--repair", action="store_true")
    # merge
    p_merge = sub.add_parser("merge", help="Объединить несколько PDF в один")
    p_merge.add_argument("-o", "--output", required=True, help="Выходной PDF")
    p_merge.add_argument("pdfs", nargs="+", help="PDF файлы")
    # repair
    p_repair = sub.add_parser("repair", help="Восстановить PDF для Acrobat (qpdf --linearize)")
    p_repair.add_argument("input", help="Входной PDF")
    p_repair.add_argument("-o", "--output", default=None, help="Выходной PDF (по умолчанию: имя_repaired.pdf)")
    args = parser.parse_args()

    if args.cmd == "scan":
        donors = (ROOT / args.donors).resolve()
        out = (ROOT / args.out).resolve()
        extra = [(ROOT / p).resolve() for p in args.extra if p]
        return cmd_scan(donors, out, extra)
    if args.cmd == "template":
        db = (ROOT / args.db).resolve()
        tpl = (ROOT / args.out).resolve()
        return cmd_template(db, tpl)
    if args.cmd == "patch":
        donor = Path(args.donor).expanduser().resolve()
        out = Path(args.output).expanduser().resolve()
        if not donor.exists():
            print(f"[ERROR] Donor не найден: {donor}", file=sys.stderr)
            return 1
        tpl = Path(args.template).resolve() if args.template else (ROOT / "templates" / "sbp_cid_template.json")
        if not tpl.exists():
            tpl = None
        reps = {}
        if args.payer:
            reps["payer"] = args.payer
        if args.amount:
            reps["amount"] = args.amount
        if args.recipient:
            reps["recipient"] = args.recipient
        if not reps:
            print("Укажите --payer, --amount и/или --recipient", file=sys.stderr)
            return 1
        return cmd_patch(donor, out, reps, tpl, args.patch_id, args.repair)
    if args.cmd == "merge":
        out = Path(args.output).expanduser().resolve()
        pdfs = [Path(p).expanduser().resolve() for p in args.pdfs]
        return cmd_merge(out, pdfs)
    if args.cmd == "repair":
        inp = Path(args.input).expanduser().resolve()
        out = Path(args.output).expanduser().resolve() if args.output else None
        if not inp.exists():
            print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
            return 1
        return cmd_repair(inp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
