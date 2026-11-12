#!/usr/bin/env python3
"""Сравнение структуры двух PDF: sizes, /ID, Producer, BaseFont, /W array, ToUnicode, content streams.

Использование: python3 compare_pdf_structure.py receipt_custom.pdf "база_чеков/vtb/СБП/29-01-26_18-35.pdf"
"""
import re
import zlib
import hashlib
import sys
from pathlib import Path


def extract_file_size(data: bytes) -> int:
    return len(data)


def extract_id(data: bytes) -> tuple[str | None, str | None]:
    """Извлечь /ID [<id1><id2>]."""
    m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', data)
    if m:
        return m.group(1).decode(), m.group(2).decode()
    return None, None


def extract_producer(data: bytes) -> str | None:
    m = re.search(rb'/Producer\s*\(([^)]*)\)', data)
    return m.group(1).decode("latin-1", errors="replace") if m else None


def extract_basefonts(data: bytes) -> list[str]:
    basefonts = re.findall(rb"/BaseFont\s*/([^\s/]+)", data)
    return [b.decode("latin-1", errors="replace") for b in basefonts]


def extract_w_array(data: bytes) -> dict | None:
    """Извлечь /W массив CIDFontType2: длина, хеш, превью."""
    m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", data, re.DOTALL)
    if m:
        raw = m.group(1)
        return {
            "length": len(raw),
            "hash": hashlib.md5(raw).hexdigest(),
            "preview": raw[:80].decode("latin-1", errors="replace") + ("..." if len(raw) > 80 else ""),
        }
    return None


def extract_tounicode(data: bytes) -> dict:
    """ToUnicode streams: obj_num -> {length, decompressed_len, hash}."""
    result = {}
    for ref in re.findall(rb"/ToUnicode\s+(\d+)\s+0\s+R", data):
        obj_num = ref.decode()
        pat = obj_num.encode() + rb"\s+0\s+obj\s*<<(.*?)>>\s*stream\r?\n"
        mm = re.search(pat, data)
        if mm:
            len_m = re.search(rb"/Length\s+(\d+)", mm.group(1))
            if len_m:
                ln = int(len_m.group(1))
                stream_start = mm.end()
                raw = data[stream_start : stream_start + ln]
                try:
                    dec = zlib.decompress(raw)
                    result[obj_num] = {
                        "compressed": ln,
                        "decompressed": len(dec),
                        "hash": hashlib.md5(dec).hexdigest(),
                    }
                except Exception as e:
                    result[obj_num] = {"compressed": ln, "error": str(e)}
    return result


def extract_content_streams(data: bytes) -> list[dict]:
    """Content streams: length, decompressed length, hash."""
    streams = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        ln = int(m.group(2))
        start = m.end()
        raw = data[start : start + ln]
        try:
            dec = zlib.decompress(raw)
            streams.append({
                "compressed": ln,
                "decompressed": len(dec),
                "hash": hashlib.md5(dec).hexdigest(),
            })
        except Exception as e:
            streams.append({"compressed": ln, "error": str(e)})
    return streams


def extract_all(data: bytes) -> dict:
    return {
        "file_size": extract_file_size(data),
        "id": extract_id(data),
        "producer": extract_producer(data),
        "basefonts": extract_basefonts(data),
        "w_array": extract_w_array(data),
        "tounicode": extract_tounicode(data),
        "content_streams": extract_content_streams(data),
    }


def format_value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, tuple):
        return str(v)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return str(v)
    return str(v)


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: python3 compare_pdf_structure.py receipt_custom.pdf \"база_чеков/vtb/СБП/29-01-26_18-35.pdf\"")
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

    info1 = extract_all(d1)
    info2 = extract_all(d2)

    lines = []
    def out(s: str = ""):
        lines.append(s)
        print(s)

    out("# Сравнение структуры PDF")
    out()
    out(f"**Файл A:** {p1.name}")
    out(f"**Файл B:** {p2.name}")
    out()

    # 1. Размеры
    out("## 1. Размеры файлов")
    out()
    out("| | A | B | Совпадает |")
    out("|---|--:|--:|:---:|")
    s1, s2 = info1["file_size"], info2["file_size"]
    out(f"| file_size (bytes) | {s1} | {s2} | {'✓' if s1 == s2 else '✗'} |")
    out()

    # 2. /ID
    out("## 2. /ID")
    out()
    id1, id2 = info1["id"], info2["id"]
    out("| | A | B | Совпадает |")
    out("|---|:---|:---|:---:|")
    out(f"| id1 | {id1[0] or '—'} | {id2[0] or '—'} | {'✓' if id1[0] == id2[0] else '✗'} |")
    out(f"| id2 | {id1[1] or '—'} | {id2[1] or '—'} | {'✓' if id1[1] == id2[1] else '✗'} |")
    out()

    # 3. Producer
    out("## 3. Producer")
    out()
    pr1, pr2 = info1["producer"], info2["producer"]
    out("| | A | B | Совпадает |")
    out("|---|:---|:---|:---:|")
    out(f"| Producer | {pr1 or '—'} | {pr2 or '—'} | {'✓' if pr1 == pr2 else '✗'} |")
    out()

    # 4. BaseFont
    out("## 4. BaseFont")
    out()
    bf1, bf2 = info1["basefonts"], info2["basefonts"]
    out("| | Значение |")
    out("|---|:---|")
    out(f"| A | {bf1} |")
    out(f"| B | {bf2} |")
    out(f"| Совпадают | {'✓' if bf1 == bf2 else '✗'} |")
    out()

    # 5. /W array
    out("## 5. /W array (CIDFontType2)")
    out()
    w1, w2 = info1["w_array"], info2["w_array"]
    out("| | A | B | Совпадает |")
    out("|---|:---|:---|:---:|")
    if w1 and w2:
        out(f"| length | {w1['length']} | {w2['length']} | {'✓' if w1['length'] == w2['length'] else '✗'} |")
        out(f"| hash | {w1['hash']} | {w2['hash']} | {'✓' if w1['hash'] == w2['hash'] else '✗'} |")
    else:
        out(f"| | {format_value(w1)} | {format_value(w2)} |")
    out()

    # 6. ToUnicode
    out("## 6. ToUnicode")
    out()
    tou1, tou2 = info1["tounicode"], info2["tounicode"]
    out("| obj | A compressed | A decompressed | A hash | B compressed | B decompressed | B hash | Совпадает |")
    out("|-----|:---:|:---:|:---|:---:|:---:|:---|:---:|")
    for obj in sorted(set(tou1.keys()) | set(tou2.keys())):
        t1 = tou1.get(obj, {})
        t2 = tou2.get(obj, {})
        h1 = t1.get("hash", "—")
        h2 = t2.get("hash", "—")
        match = "✓" if h1 == h2 else "✗"
        out(f"| {obj} | {t1.get('compressed', '—')} | {t1.get('decompressed', '—')} | {h1} | "
            f"{t2.get('compressed', '—')} | {t2.get('decompressed', '—')} | {h2} | {match} |")
    out()

    # 7. Content streams
    out("## 7. Content streams")
    out()
    cs1, cs2 = info1["content_streams"], info2["content_streams"]
    out("| idx | A compressed | A decompressed | A hash | B compressed | B decompressed | B hash | Совпадает |")
    out("|-----|:---:|:---:|:---|:---:|:---:|:---|:---:|")
    for i in range(max(len(cs1), len(cs2))):
        c1 = cs1[i] if i < len(cs1) else {}
        c2 = cs2[i] if i < len(cs2) else {}
        h1 = c1.get("hash", "—")
        h2 = c2.get("hash", "—")
        match = "✓" if h1 == h2 else "✗"
        out(f"| {i} | {c1.get('compressed', '—')} | {c1.get('decompressed', '—')} | {h1} | "
            f"{c2.get('compressed', '—')} | {c2.get('decompressed', '—')} | {h2} | {match} |")
    out()

    out("---")
    out("_Отчёт сгенерирован compare_pdf_structure.py_")

    # Сохранить в файл
    report_path = Path("СРАВНЕНИЕ_СТРУКТУРЫ_ОТЧЁТ.md")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nОтчёт сохранён: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
