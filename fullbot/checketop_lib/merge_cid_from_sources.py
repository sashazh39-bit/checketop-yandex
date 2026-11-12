#!/usr/bin/env python3
"""Объединение CID из нескольких PDF в один шаблон.

Берёте 3 (или N) PDF-источника и «приклеиваете» из них по кусочкам CID-блоки
в целевой PDF. Каждое поле (плательщик, сумма, счёт и т.д.) можно взять из
разного источника — чтобы собрать шаблон с «правильными» CID для каждого поля.

Использование:
  python3 merge_cid_from_sources.py target.pdf out.pdf source1.pdf source2.pdf source3.pdf
  python3 merge_cid_from_sources.py target.pdf out.pdf s1.pdf s2.pdf s3.pdf --config merge_config.json

Конфиг (JSON) задаёт, из какого источника брать каждое поле по y-координате:
  [
    {"y": 348.75, "ytol": 2, "xmin": 100, "source": 0},
    {"y": 330, "ytol": 2, "xmin": 100, "source": 1},
    {"y": 227.25, "ytol": 2, "xmin": 100, "source": 2}
  ]
  y — координата Tm, ytol — допуск, xmin — только правая колонка (x >= xmin), source — индекс (0,1,2...)
"""
from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path

Y_TOL_DEFAULT = 2.0
X_MIN_RIGHT = 100


def extract_tj_blocks(pdf_path: Path) -> list[tuple[float, float, bytes]]:
    """Извлечь все TJ-блоки (x, y, tj_inner) из PDF. tj_inner — содержимое [...], без скобок."""
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
        # Один паттерн: prefix между Tm и TJ опционален, inner = group 8
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            tj_inner = mm.group(8)  # содержимое [...]
            result.append((x, y, tj_inner))
    return result


def adapt_kerning(tj_inner: bytes, target_kern: bytes | None = None) -> bytes:
    """Привести kerning к целевому. target_kern=None — оставить как есть."""
    if target_kern is None:
        return tj_inner
    # Заменить любой kerning в tj_inner на target_kern
    return re.sub(rb"\)-[\d.]+", b")-" + target_kern, tj_inner)


def find_best_match(
    target_x: float, target_y: float,
    source_blocks: list[tuple[float, float, bytes]],
    y_tol: float, x_min: float
) -> bytes | None:
    """Найти в source_blocks блок с ближайшим y при x >= x_min. Возвращает tj_inner или None."""
    candidates = [(x, y, tj) for x, y, tj in source_blocks if x >= x_min and abs(y - target_y) <= y_tol]
    if not candidates:
        return None
    # Берём с минимальной разницей по y
    best = min(candidates, key=lambda c: abs(c[1] - target_y))
    return best[2]


def build_default_config(sources: list[Path]) -> list[dict]:
    """Дефолтный конфиг: все поля из source[0]."""
    return [
        {"y": 348.75, "ytol": Y_TOL_DEFAULT, "xmin": X_MIN_RIGHT, "source": 0},
        {"y": 330, "ytol": Y_TOL_DEFAULT, "xmin": X_MIN_RIGHT, "source": 0},
        {"y": 227.25, "ytol": Y_TOL_DEFAULT, "xmin": X_MIN_RIGHT, "source": 0},
    ]


def merge_cid(
    target_path: Path,
    out_path: Path,
    source_paths: list[Path],
    config: list[dict] | None = None,
    adapt_kern: bool = True,
) -> bool:
    """
    Объединить CID из source_paths в target. Результат в out_path.
    config: список {y, ytol, xmin, source}. Если None — все из source[0].
    """
    if not source_paths:
        print("[ERROR] Нужен хотя бы один source PDF", file=sys.stderr)
        return False

    target_blocks = extract_tj_blocks(target_path)
    source_blocks_list = [extract_tj_blocks(p) for p in source_paths]

    if config is None:
        config = build_default_config(source_paths)

    # Строим замены: (round(x,2), round(y,2)) -> new_tj (округление для надёжного совпадения)
    def _key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 2), round(y, 2))

    replacements: dict[tuple[float, float], bytes] = {}
    for tx, ty, t_tj in target_blocks:
        for cfg in config:
            if ty >= cfg["y"] - cfg["ytol"] and ty <= cfg["y"] + cfg["ytol"] and tx >= cfg.get("xmin", 0):
                src_idx = cfg["source"]
                if src_idx >= len(source_blocks_list):
                    continue
                # source_y: брать из source по этой y (если target имеет другую раскладку)
                src_y = cfg.get("source_y", ty)
                new_tj = find_best_match(tx, src_y, source_blocks_list[src_idx], cfg["ytol"], cfg.get("xmin", 0))
                if new_tj and new_tj != t_tj:
                    replacements[_key(tx, ty)] = new_tj
                break

    if not replacements:
        print("[WARN] Не найдено совпадений для замены. Проверьте конфиг и координаты.", file=sys.stderr)
        return False

    # Патчим target
    data = bytearray(target_path.read_bytes())
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
            key = _key(x, y)
            if key not in replacements:
                return match.group(0)
            new_inner = replacements[key]
            if adapt_kern:
                orig_inner = match.group(match.lastindex)
                m_kern = re.search(rb"\)-([\d.]+)\s", orig_inner)
                target_kern = m_kern.group(1) if m_kern else b"16.66667"
                new_inner = adapt_kerning(new_inner, target_kern)
            # Заменяем только содержимое [...], inner = последняя группа (7 или 8)
            grp = match.lastindex
            inner_start = match.start(grp) - match.start(0)
            inner_end = match.end(grp) - match.start(0)
            return match.group(0)[:inner_start] + new_inner + match.group(0)[inner_end:]

        # Паттерн с group 8 = inner содержимое [...]
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
    print(f"[OK] Объединено {len(replacements)} полей из {len(source_paths)} источников")
    print(f"[OK] Сохранено: {out_path}")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Объединение CID из нескольких PDF в один шаблон"
    )
    parser.add_argument("target", help="Целевой PDF (база)")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument("sources", nargs="+", help="PDF-источники (2–3 или больше)")
    parser.add_argument("--config", "-c", help="JSON-конфиг полей (y, ytol, xmin, source)")
    parser.add_argument("--no-adapt-kern", action="store_true", help="Не адаптировать kerning")
    parser.add_argument("--scan", action="store_true", help="Только сканировать target и вывести (x,y) для создания конфига")
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    sources = [Path(p).expanduser().resolve() for p in args.sources]

    if args.scan:
        if not target.exists():
            print(f"[ERROR] Target не найден: {target}", file=sys.stderr)
            return 1
        blocks = extract_tj_blocks(target)
        # Сортируем по y (сверху вниз), затем по x
        for x, y, _ in sorted(blocks, key=lambda b: (-b[1], b[0])):
            if x >= X_MIN_RIGHT:
                print(f"  {{\"y\": {y}, \"ytol\": 2, \"xmin\": 100, \"source\": 0}},")
        print("\n# Добавьте source: 0/1/2 для каждого поля. Сохраните в JSON и передайте --config")
        return 0

    if not target.exists():
        print(f"[ERROR] Target не найден: {target}", file=sys.stderr)
        return 1
    for s in sources:
        if not s.exists():
            print(f"[ERROR] Source не найден: {s}", file=sys.stderr)
            return 1

    config = None
    if args.config:
        cfg_path = Path(args.config).expanduser().resolve()
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                config = json.load(f)
        else:
            print(f"[WARN] Конфиг не найден: {cfg_path}, используем дефолт", file=sys.stderr)

    ok = merge_cid(target, out, sources, config=config, adapt_kern=not args.no_adapt_kern)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
