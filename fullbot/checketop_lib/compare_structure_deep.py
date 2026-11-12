#!/usr/bin/env python3
"""Доскональное сравнение структуры двух PDF.

Сравнивает: версию, trailer, объекты, content stream, шрифты, /W, ToUnicode,
Resources, Info, xref — всё что может влиять на проверку бота.

Использование: python3 compare_structure_deep.py receipt_custom.pdf база_чеков/vtb/СБП/29-01-26_18-35.pdf
"""
import re
import zlib
import hashlib
import sys
from pathlib import Path
from collections import defaultdict


def extract_patterns(data: bytes, name: str) -> dict:
    """Извлечь ключевые структурные элементы."""
    out = {}
    
    # Версия
    m = re.search(rb"%PDF-(\d\.\d)", data[:30])
    out["pdf_version"] = m.group(1).decode() if m else None
    
    out["file_size"] = len(data)
    
    # trailer
    out["has_trailer"] = b"trailer" in data
    out["startxref_count"] = data.count(b"startxref")
    out["eof_count"] = data.count(b"%%EOF")
    
    # /ID
    id_m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', data)
    if id_m:
        out["id1"] = id_m.group(1).decode()
        out["id2"] = id_m.group(2).decode()
    
    # /Info, /Root
    info_m = re.search(rb'/Info\s+(\d+)\s+0\s+R', data)
    out["info_ref"] = info_m.group(1).decode() if info_m else None
    root_m = re.search(rb'/Root\s+(\d+)\s+0\s+R', data)
    out["root_ref"] = root_m.group(1).decode() if root_m else None
    
    # CreationDate, Producer, Creator
    for key in (b"CreationDate", b"Producer", b"Creator", b"ModDate"):
        pat = key + rb"\s*\(([^)]*)\)"
        m = re.search(pat, data)
        if m:
            out[key.decode().lower()] = m.group(1).decode("latin-1", errors="replace")
    
    # Количество объектов в xref
    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)", data)
    if xref_m:
        out["xref_start"] = int(xref_m.group(1))
        out["xref_count"] = int(xref_m.group(2))
    
    # Шрифты (имена из /Font)
    fonts = re.findall(rb"/F(\d+)\s+(\d+)\s+0\s+R", data)
    out["font_refs"] = [(f[0].decode(), f[1].decode()) for f in fonts]
    
    # Имена шрифтов (BaseFont) — из объектов
    basefonts = re.findall(rb"/BaseFont\s*/([^\s/]+)", data)
    out["basefonts"] = [b.decode("latin-1", errors="replace") for b in basefonts]
    
    # Subtype шрифтов (Type0, CIDFontType0/2)
    subtype_m = re.findall(rb"/Subtype\s*/(\w+)", data)
    out["font_subtypes"] = [s.decode() for s in subtype_m]
    
    # /W массив (CIDFontType2) — важный для рендеринга
    w_m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", data, re.DOTALL)
    if w_m:
        w_content = w_m.group(1)
        out["w_array_len"] = len(w_content)
        out["w_array_hash"] = hashlib.md5(w_content).hexdigest()
    else:
        out["w_array"] = "not found"
    
    # ToUnicode stream
    tounicode_refs = re.findall(rb"/ToUnicode\s+(\d+)\s+0\s+R", data)
    out["tounicode_refs"] = [r.decode() for r in tounicode_refs]
    
    # Content streams: ищем << ... /Length N ... >> stream
    stream_matches = list(re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL))
    out["content_stream_count"] = len(stream_matches)
    
    stream_infos = []
    for i, m in enumerate(stream_matches):
        ln = int(m.group(2))
        start = m.end()
        raw = data[start : start + ln]
        try:
            dec = zlib.decompress(raw)
            info = {
                "idx": i,
                "length_compressed": ln,
                "length_decompressed": len(dec),
                "dec_hash": hashlib.md5(dec).hexdigest(),
                "has_BT": b"BT" in dec,
                "BT_count": dec.count(b"BT"),
                "TJ_count": dec.count(b" TJ"),
                "Tm_count": dec.count(b" Tm"),
            }
            # Первые/последние операторы
            ops = re.findall(rb"[A-Za-z]{1,3}\b", dec)
            info["op_sequence_sample"] = " ".join(ops[:30].decode() if isinstance(ops[0], bytes) else ops[:30]) if ops else ""
            stream_infos.append(info)
        except Exception as e:
            stream_infos.append({"idx": i, "length": ln, "decompress_error": str(e)})
    out["streams"] = stream_infos
    
    # Проверка: есть ли /Encoding
    out["has_cidtogidmap"] = b"/CIDToGIDMap" in data
    out["has_cidfont"] = b"/CIDFontType2" in data or b"/CIDFontType0" in data
    
    # Объекты: подсчёт по номерам
    obj_nums = re.findall(rb"(\d+)\s+0\s+obj", data)
    out["object_numbers"] = len(set(obj_nums))
    
    # MediaBox
    mediabox = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s*\]", data)
    if mediabox:
        out["mediabox"] = [float(x) for x in mediabox.groups()]
    
    return out


def extract_tounicode(data: bytes) -> dict:
    """Извлечь ToUnicode stream целиком для сравнения."""
    result = {}
    # Найти stream по ссылке ToUnicode
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        obj_num = m.group(1).decode()
        header = m.group(2)
        if b"/ToUnicode" in header:
            continue  # это не ToUnicode, а ссылка на него
        # Ищем stream после >> 
        pos = m.end()
        len_m = re.search(rb"/Length\s+(\d+)", header)
        if len_m:
            ln = int(len_m.group(1))
            stream_raw = data[pos : pos + ln]
            try:
                dec = zlib.decompress(stream_raw)
                result[obj_num] = {
                    "length": ln,
                    "decompressed_len": len(dec),
                    "hash": hashlib.md5(dec).hexdigest(),
                    "beginbfchar_count": dec.count(b"beginbfchar"),
                    "beginbfrange_count": dec.count(b"beginbfrange"),
                }
            except Exception:
                result[obj_num] = {"error": "decompress failed"}
    
    # Ищем /ToUnicode N 0 R и затем объект N
    for ref in re.findall(rb"/ToUnicode\s+(\d+)\s+0\s+R", data):
        obj_num = ref.decode()
        pat = obj_num.encode() + rb"\s+0\s+obj\s*<<(.*?)>>\s*stream\r?\n([\s\S]{0,200})"
        mm = re.search(pat, data)
        if mm:
            len_m = re.search(rb"/Length\s+(\d+)", mm.group(1))
            if len_m:
                ln = int(len_m.group(1))
                stream_start = mm.end()
                stream_raw = data[stream_start : stream_start + ln]
                try:
                    dec = zlib.decompress(stream_raw)
                    result[obj_num] = {
                        "length": ln,
                        "decompressed_len": len(dec),
                        "hash": hashlib.md5(dec).hexdigest(),
                        "sample": dec[:200].decode("latin-1", errors="replace"),
                    }
                except Exception as e:
                    result[obj_num] = {"error": str(e)}
    return result


def diff_dict(a: dict, b: dict, path: str = "") -> list:
    """Рекурсивный diff двух dict. Возвращает список отличий."""
    diffs = []
    all_keys = set(a.keys()) | set(b.keys())
    for k in sorted(all_keys):
        new_path = f"{path}.{k}" if path else k
        va, vb = a.get(k), b.get(k)
        if k == "streams":
            # Сравнение streams отдельно
            for i, (sa, sb) in enumerate(zip(a.get("streams", []), b.get("streams", []))):
                if sa != sb:
                    for sk in set(sa.keys()) | set(sb.keys()):
                        if sa.get(sk) != sb.get(sk):
                            diffs.append((f"streams[{i}].{sk}", sa.get(sk), sb.get(sk)))
            if len(a.get("streams", [])) != len(b.get("streams", [])):
                diffs.append((f"streams.count", len(a.get("streams", [])), len(b.get("streams", []))))
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            diffs.extend(diff_dict(va, vb, new_path))
        elif va != vb:
            diffs.append((new_path, va, vb))
    return diffs


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: python3 compare_structure_deep.py receipt_custom.pdf база_чеков/vtb/СБП/29-01-26_18-35.pdf")
        return 1
    
    p1 = Path(sys.argv[1]).expanduser().resolve()
    p2 = Path(sys.argv[2]).expanduser().resolve()
    
    if not p1.exists():
        print(f"[ERROR] Не найден: {p1}")
        return 1
    if not p2.exists():
        print(f"[ERROR] Не найден: {p2}")
        return 1
    
    d1 = p1.read_bytes()
    d2 = p2.read_bytes()
    
    info1 = extract_patterns(d1, p1.name)
    info2 = extract_patterns(d2, p2.name)
    
    tou1 = extract_tounicode(d1)
    tou2 = extract_tounicode(d2)
    
    # Отчёт
    print("=" * 80)
    print("ДОСКОНАЛЬНОЕ СРАВНЕНИЕ СТРУКТУРЫ PDF")
    print("=" * 80)
    print(f"\nФайл A (наш):      {p1.name} ({info1['file_size']} bytes)")
    print(f"Файл B (эталон):   {p2.name} ({info2['file_size']} bytes)")
    print()
    
    # Таблица сравнения
    compare_keys = [
        "pdf_version", "file_size", "startxref_count", "eof_count",
        "xref_start", "xref_count", "object_numbers", "content_stream_count",
        "id1", "id2", "info_ref", "root_ref",
        "creationdate", "producer", "creator", "moddate",
        "w_array_len", "w_array_hash", "has_cidtogidmap", "has_cidfont",
        "mediabox",
    ]
    
    print("ОСНОВНЫЕ ПАРАМЕТРЫ")
    print("-" * 80)
    print(f"{'Параметр':<25} {'A (наш)':<25} {'B (эталон)':<25} {'Совпадает'}")
    print("-" * 80)
    
    for key in compare_keys:
        v1 = info1.get(key, "—")
        v2 = info2.get(key, "—")
        match = "✓" if v1 == v2 else "✗ ОТЛИЧИЕ"
        v1s = str(v1)[:23] if v1 is not None else "—"
        v2s = str(v2)[:23] if v2 is not None else "—"
        print(f"{key:<25} {v1s:<25} {v2s:<25} {match}")
    
    print()
    print("ШРИФТЫ (BaseFont)")
    print("-" * 80)
    print(f"A: {info1.get('basefonts', [])}")
    print(f"B: {info2.get('basefonts', [])}")
    same_fonts = set(info1.get("basefonts", [])) == set(info2.get("basefonts", []))
    print(f"Совпадают: {'✓' if same_fonts else '✗ ОТЛИЧИЕ'}")
    
    print()
    print("CONTENT STREAMS")
    print("-" * 80)
    s1, s2 = info1.get("streams", []), info2.get("streams", [])
    for i in range(max(len(s1), len(s2))):
        a_s = s1[i] if i < len(s1) else {}
        b_s = s2[i] if i < len(s2) else {}
        dec_match = a_s.get("dec_hash") == b_s.get("dec_hash")
        print(f"Stream {i}: A_len={a_s.get('length_compressed', '—')} B_len={b_s.get('length_compressed', '—')} "
              f"dec_hash: {'✓' if dec_match else '✗ ОТЛИЧИЕ'}")
        if not dec_match:
            print(f"    A dec_hash: {a_s.get('dec_hash')}")
            print(f"    B dec_hash: {b_s.get('dec_hash')}")
    
    print()
    print("TOUNICODE CMap")
    print("-" * 80)
    print(f"A ToUnicode refs: {info1.get('tounicode_refs')}")
    print(f"B ToUnicode refs: {info2.get('tounicode_refs')}")
    for obj_num in set(tou1.keys()) | set(tou2.keys()):
        t1 = tou1.get(obj_num, {})
        t2 = tou2.get(obj_num, {})
        h1 = t1.get("hash", "—")
        h2 = t2.get("hash", "—")
        match = "✓" if h1 == h2 else "✗ ОТЛИЧИЕ"
        print(f"  obj {obj_num}: A_hash={h1} B_hash={h2} {match}")
        if h1 != h2:
            print(f"    A len: {t1.get('length')} dec: {t1.get('decompressed_len')}")
            print(f"    B len: {t2.get('length')} dec: {t2.get('decompressed_len')}")
    
    print()
    print("КЛЮЧЕВЫЕ ОТЛИЧИЯ (для проверки бота)")
    print("-" * 80)
    
    critical_diffs = []
    if info1.get("id1") != info2.get("id1"):
        critical_diffs.append("Document /ID — отличается (мы генерируем новый)")
    if info1.get("w_array_hash") != info2.get("w_array_hash"):
        critical_diffs.append("/W массив CIDFontType2 — отличается (ширины глифов)")
    if info1.get("creationdate") != info2.get("creationdate"):
        critical_diffs.append("CreationDate — отличается")
    if info1.get("producer") != info2.get("producer"):
        critical_diffs.append("Producer — отличается")
    if s1 and s2 and s1[0].get("dec_hash") != s2[0].get("dec_hash"):
        critical_diffs.append("Content stream — отличается (патч текста)")
    
    for d in critical_diffs:
        print(f"  • {d}")
    
    if tou1 and tou2:
        for k in set(tou1.keys()) | set(tou2.keys()):
            if tou1.get(k, {}).get("hash") != tou2.get(k, {}).get("hash"):
                critical_diffs.append(f"ToUnicode CMap (obj {k}) — отличается")
    
    print()
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
