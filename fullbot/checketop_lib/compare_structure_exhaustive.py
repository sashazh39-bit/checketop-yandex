#!/usr/bin/env python3
"""Доскональное сравнение структуры двух PDF — пообъектно, побайтово.

Сравнивает: объекты, xref, streams, шрифты, /W, ToUnicode, порядок, размеры.
Вывод: детальный отчёт для выявления причин отклонения ботом.
"""
import re
import zlib
import hashlib
import sys
from pathlib import Path
from collections import defaultdict


def parse_objects(data: bytes) -> dict:
    """Парсинг всех объектов PDF. Возвращает {obj_num: {type, offset, raw_slice, refs}}."""
    objects = {}
    for m in re.finditer(rb"^(\d+)\s+0\s+obj\s*(.*?)\s*endobj", data, re.MULTILINE | re.DOTALL):
        obj_num = int(m.group(1))
        body = m.group(2).strip()
        # Определяем тип
        obj_type = "unknown"
        if body.startswith(b"<<"):
            if b"/Length" in body and b"stream" in body:
                obj_type = "stream"
            elif b"/Type" in body:
                type_m = re.search(rb"/Type\s*/(\w+)", body)
                if type_m:
                    obj_type = "dict:" + type_m.group(1).decode()
            else:
                obj_type = "dict"
        elif body.startswith(b"(") or body.startswith(b"["):
            obj_type = "literal"
        elif body.startswith(b"<"):
            obj_type = "hex"

        refs = re.findall(rb"(\d+)\s+0\s+R", body)
        objects[obj_num] = {
            "num": obj_num,
            "type": obj_type,
            "offset": m.start(),
            "len": len(m.group(0)),
            "body_len": len(body),
            "refs": [int(r) for r in refs],
        }
    return objects


def extract_stream_info(data: bytes) -> list[dict]:
    """Извлечь информацию о каждом stream."""
    streams = []
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        obj_num = int(m.group(1))
        header = m.group(2)
        len_m = re.search(rb"/Length\s+(\d+)", header)
        if not len_m:
            continue
        ln = int(len_m.group(1))
        stream_start = m.end()
        raw = data[stream_start : stream_start + ln]

        info = {"obj": obj_num, "compressed_len": ln}
        if b"/Filter" in header:
            filt_m = re.search(rb"/Filter\s*/(\w+)", header)
            info["filter"] = filt_m.group(1).decode() if filt_m else "?"
        if b"/FlateDecode" in header or b"/Filter" not in header:
            try:
                dec = zlib.decompress(raw)
                info["decompressed_len"] = len(dec)
                info["dec_hash"] = hashlib.md5(dec).hexdigest()
                if b"beginbfchar" in dec:
                    info["has_bfchar"] = True
                    info["bfchar_count"] = dec.count(b"beginbfchar")
                if b"BT" in dec:
                    info["has_BT"] = True
                    info["BT_count"] = dec.count(b"BT")
                    info["TJ_count"] = dec.count(b" TJ")
            except Exception as e:
                info["decompress_error"] = str(e)
        streams.append(info)
    return streams


def extract_xref_table(data: bytes) -> dict:
    """Извлечь xref: offset каждого объекта."""
    xref = {}
    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
    if xref_m:
        start = int(xref_m.group(1))
        count = int(xref_m.group(2))
        entries = re.findall(rb"(\d{10})\s+(\d{5})\s+([nf])", xref_m.group(3))
        for i, (offset, gen, typ) in enumerate(entries):
            xref[start + i] = {"offset": int(offset), "gen": int(gen), "type": typ.decode()}
    return xref


def extract_basefonts(data: bytes) -> list[tuple[str, int]]:
    """(BaseFont, obj_num) для каждого шрифта."""
    result = []
    for m in re.finditer(rb"(\d+)\s+0\s+obj.*?/BaseFont\s*/([^\s/]+)", data, re.DOTALL):
        result.append((m.group(2).decode("latin-1", errors="replace"), int(m.group(1))))
    return result


def extract_w_array(data: bytes) -> dict | None:
    """Содержимое /W массива CIDFont."""
    m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", data, re.DOTALL)
    if m:
        return {
            "raw_len": len(m.group(1)),
            "hash": hashlib.md5(m.group(1)).hexdigest(),
            "first_100": m.group(1)[:100],
        }
    return None


def diff_tounicode_sample(a_dec: bytes, b_dec: bytes) -> list[str]:
    """Сравнить ToUnicode CMap — какие маппинги отличаются."""
    diffs = []
    # Парсим beginbfchar
    def parse_bfchar(dec):
        out = {}
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid, uni = m.group(1).decode(), m.group(2).decode()
            out[cid] = uni
        return out

    ma, mb = parse_bfchar(a_dec), parse_bfchar(b_dec)
    only_a = set(ma.keys()) - set(mb.keys())
    only_b = set(mb.keys()) - set(ma.keys())
    common_diff = [k for k in set(ma.keys()) & set(mb.keys()) if ma[k] != mb[k]]
    if only_a:
        diffs.append(f"CID только в A: {len(only_a)} шт")
    if only_b:
        diffs.append(f"CID только в B: {len(only_b)} шт")
    if common_diff:
        diffs.append(f"Разный маппинг: {len(common_diff)} CID")
    return diffs


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: python3 compare_structure_exhaustive.py receipt_custom.pdf база_чеков/vtb/СБП/29-01-26_18-35.pdf")
        return 1

    p1 = Path(sys.argv[1]).expanduser().resolve()
    p2 = Path(sys.argv[2]).expanduser().resolve()
    if not p1.exists() or not p2.exists():
        print("[ERROR] Файл не найден")
        return 1

    d1, d2 = p1.read_bytes(), p2.read_bytes()

    out_lines = []
    def log(s=""):
        out_lines.append(s)
        print(s)

    log("=" * 90)
    log("ДОСКОНАЛЬНОЕ СРАВНЕНИЕ СТРУКТУРЫ PDF")
    log("=" * 90)
    log(f"Файл A (наш):     {p1.name} ({len(d1)} bytes)")
    log(f"Файл B (эталон):  {p2.name} ({len(d2)} bytes)")
    log()

    # 1. Объекты
    objs1, objs2 = parse_objects(d1), parse_objects(d2)
    log("1. СТРУКТУРА ОБЪЕКТОВ")
    log("-" * 90)
    nums1, nums2 = set(objs1.keys()), set(objs2.keys())
    if nums1 != nums2:
        log(f"  Номера объектов: A={sorted(nums1)}, B={sorted(nums2)}")
        log(f"  Только в A: {nums1 - nums2}")
        log(f"  Только в B: {nums2 - nums1}")
    else:
        log(f"  Номера объектов: одинаковые {sorted(nums1)}")

    type_diff = []
    for n in sorted(nums1 & nums2):
        t1, t2 = objs1[n]["type"], objs2[n]["type"]
        if t1 != t2:
            type_diff.append((n, t1, t2))
    if type_diff:
        log(f"  Разные типы объектов: {type_diff}")
    else:
        log("  Типы объектов: совпадают")
    log()

    # 2. XRef
    xref1, xref2 = extract_xref_table(d1), extract_xref_table(d2)
    log("2. XREF ТАБЛИЦА")
    log("-" * 90)
    log(f"  A: {len(xref1)} записей")
    log(f"  B: {len(xref2)} записей")
    offset_diff = [(n, xref1[n]["offset"], xref2[n]["offset"]) for n in xref1 if n in xref2 and xref1[n]["offset"] != xref2[n]["offset"]]
    if offset_diff:
        log(f"  Разные offset: {len(offset_diff)} объектов (ожидаемо при разном содержимом)")
    log()

    # 3. Шрифты BaseFont
    fonts1 = extract_basefonts(d1)
    fonts2 = extract_basefonts(d2)
    log("3. ШРИФТЫ (BaseFont)")
    log("-" * 90)
    log(f"  A: {fonts1}")
    log(f"  B: {fonts2}")
    subset1 = [f.split("+")[0] for f, _ in fonts1 if "+" in f]
    subset2 = [f.split("+")[0] for f, _ in fonts2 if "+" in f]
    if subset1 != subset2:
        log(f"  ★ ОТЛИЧИЕ: subset-теги A={subset1} B={subset2}")
        log("  Разные доноры → разный subset шрифта при эмбеддинге")
    log()

    # 4. /W массив
    w1, w2 = extract_w_array(d1), extract_w_array(d2)
    log("4. /W МАССИВ (CIDFontType2)")
    log("-" * 90)
    if w1 and w2:
        log(f"  A: len={w1['raw_len']} hash={w1['hash']}")
        log(f"  B: len={w2['raw_len']} hash={w2['hash']}")
        if w1["hash"] != w2["hash"]:
            log("  ★ ОТЛИЧИЕ: разный набор ширин глифов (разный font subset)")
    else:
        log("  Не найден в одном из файлов")
    log()

    # 5. Streams
    streams1 = extract_stream_info(d1)
    streams2 = extract_stream_info(d2)
    log("5. STREAMS (по объектам)")
    log("-" * 90)
    stream_by_obj = {s["obj"]: s for s in streams1}
    stream_by_obj2 = {s["obj"]: s for s in streams2}
    for obj in sorted(set(s["obj"] for s in streams1 + streams2)):
        s1 = stream_by_obj.get(obj, {})
        s2 = stream_by_obj2.get(obj, {})
        h1 = s1.get("dec_hash", "—")
        h2 = s2.get("dec_hash", "—")
        match = "✓" if h1 == h2 else "✗"
        log(f"  obj {obj}: A_hash={h1} B_hash={h2} {match}")
        if h1 != h2 and "compressed_len" in s1 and "compressed_len" in s2:
            log(f"         A: compressed={s1.get('compressed_len')} dec={s1.get('decompressed_len', '—')}")
            log(f"         B: compressed={s2.get('compressed_len')} dec={s2.get('decompressed_len', '—')}")
    log()

    # 6. ToUnicode — детальное сравнение
    log("6. TOUNICODE CMap — детали")
    log("-" * 90)
    def get_tounicode_streams(data):
        result = {}
        for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<[^>]*/Length\s+(\d+).*?>>\s*stream\r?\n", data, re.DOTALL):
            obj_num, ln = int(m.group(1)), int(m.group(2))
            pos = m.end()
            try:
                dec = zlib.decompress(data[pos : pos + ln])
                if b"beginbfchar" in dec or b"beginbfrange" in dec:
                    result[obj_num] = dec
            except Exception:
                pass
        return result

    tou1 = get_tounicode_streams(d1)
    tou2 = get_tounicode_streams(d2)
    for obj in sorted(set(tou1.keys()) | set(tou2.keys())):
        dec1 = tou1.get(obj, b"")
        dec2 = tou2.get(obj, b"")
        if dec1 and dec2:
            diffs = diff_tounicode_sample(dec1, dec2)
            log(f"  obj {obj}: len_A={len(dec1)} len_B={len(dec2)}")
            if diffs:
                log(f"         ★ {diffs}")
    log()

    # 7. Метаданные
    log("7. МЕТАДАННЫЕ")
    log("-" * 90)
    for key, pat in [
        ("/ID", rb'/ID\s*\[\s*<([0-9a-fA-F]+)>'),
        ("CreationDate", rb'/CreationDate\s*\(([^)]+)\)'),
        ("Producer", rb'/Producer\s*\(([^)]+)\)'),
        ("Creator", rb'/Creator\s*\(([^)]+)\)'),
    ]:
        m1, m2 = re.search(pat, d1), re.search(pat, d2)
        v1 = m1.group(1).decode("latin-1", errors="replace")[:30] if m1 else "—"
        v2 = m2.group(1).decode("latin-1", errors="replace")[:30] if m2 else "—"
        match = "✓" if v1 == v2 else "✗"
        log(f"  {key}: A={v1} | B={v2} {match}")
    log()

    # 8. Итоговые критические отличия
    log("8. КРИТИЧЕСКИЕ ОТЛИЧИЯ (могут влиять на бота)")
    log("-" * 90)
    critical = []
    if subset1 != subset2:
        critical.append("BaseFont subset-тег (AANHPC vs AAWJPC) — разный донор")
    if w1 and w2 and w1["hash"] != w2["hash"]:
        critical.append("/W массив — разный набор ширин глифов")
    tou_refs = re.findall(rb"/ToUnicode\s+(\d+)\s+0\s+R", d1)
    for ref in tou_refs:
        obj = int(ref)
        if obj in tou1 and obj in tou2:
            if tou1[obj] != tou2[obj]:
                critical.append(f"ToUnicode obj {obj} — разный CMap")
                break
    id_m1 = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>', d1)
    id_m2 = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>', d2)
    if id_m1 and id_m2 and id_m1.group(1) != id_m2.group(1):
        critical.append("Document /ID — генерируется заново при патче")
    for c in critical:
        log(f"  • {c}")
    log()
    log("=" * 90)

    # Сохранить отчёт
    report_path = p1.parent / "ОТЧЁТ_СРАВНЕНИЕ_СТРУКТУРЫ.md"
    report_path.write_text("\n".join(out_lines), encoding="utf-8")
    log(f"\nОтчёт сохранён: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
