#!/usr/bin/env python3
"""Патч: 07-03-26_04-34 — все строчки из target, «Имя плательщика» из 13-02-26 — копия.

Target: чеки 08.03/07-03-26_04-34.pdf (база)
Source: чеки 07.03/13-02-26_20-29 — копия.pdf (только строка плательщика)

Использование:
  python3 patch_04_34_payer_from_13_02.py
  python3 patch_04_34_payer_from_13_02.py target.pdf source.pdf out.pdf
  python3 patch_04_34_payer_from_13_02.py target.pdf source.pdf out.pdf 227.25  # y плательщика в target
"""
import re
import sys
import zlib
from pathlib import Path

PAYER_Y_SOURCE = 227.25  # y в 13-02-26
PAYER_Y_TARGET = 348.75  # y в 07-03-26
Y_TOL = 2.0


def extract_payer_tj(pdf_path: Path, payer_y: float) -> bytes | None:
    """Извлечь TJ плательщика (правая колонка, x>100). Возвращает tj_inner без скобок."""
    data = pdf_path.read_bytes()
    pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\]\s*TJ)'
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
        for mm in re.finditer(pat, dec):
            x, y = float(mm.group(2)), float(mm.group(4))
            if abs(y - payer_y) <= Y_TOL and x > 100:
                return mm.group(8)
    return None


def adapt_kerning(tj_inner: bytes) -> bytes:
    """-8.33333 / -21.42857 -> -16.66667."""
    for old in (b"8.33333", b"21.42857"):
        if b"-" + old in tj_inner:
            return tj_inner.replace(b"-" + old, b"-16.66667")
    return tj_inner


def main():
    base = Path(__file__).parent
    target = base / "чеки 08.03" / "07-03-26_04-34.pdf"
    source = base / "чеки 07.03" / "13-02-26_20-29 — копия.pdf"
    out = base / "чеки 08.03" / "07-03-26_04-34.pdf"

    payer_y_target = PAYER_Y_TARGET
    if len(sys.argv) >= 5:
        target = Path(sys.argv[1])
        source = Path(sys.argv[2])
        out = Path(sys.argv[3])
        payer_y_target = float(sys.argv[4])
    elif len(sys.argv) >= 4:
        target = Path(sys.argv[1])
        source = Path(sys.argv[2])
        out = Path(sys.argv[3])
    elif len(sys.argv) >= 3:
        target = Path(sys.argv[1])
        source = Path(sys.argv[2])
        out = target
        # 07-03-26_18-10 и подобные: плательщик на y=227.25
        if "18-10" in target.name or "16-46" in target.name or "16-49" in target.name:
            payer_y_target = 227.25

    # Ищем копию по маске
    if not source.exists() and base.joinpath("чеки 07.03").exists():
        for f in (base / "чеки 07.03").glob("13-02-26*копия*"):
            source = f
            break

    if not target.exists():
        print(f"[ERROR] Target не найден: {target}")
        sys.exit(1)
    if not source.exists():
        print(f"[ERROR] Source не найден: {source}")
        sys.exit(1)

    new_tj = extract_payer_tj(source, PAYER_Y_SOURCE)
    if not new_tj:
        print("[ERROR] Не удалось извлечь имя плательщика из source")
        sys.exit(1)
    new_tj = adapt_kerning(new_tj)
    new_tj = new_tj.replace(b"(\x02\x28)", b"(\x02M)").replace(b"(\x02\\(", b"(\x02M)")

    data = bytearray(target.read_bytes())
    mods = []
    pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[([^\]]*)\]\s*TJ)'
    pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[([^\]]*)\]\s*TJ)'

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
            if abs(y - payer_y_target) > Y_TOL or x < 100:
                return match.group(0)
            grp = match.lastindex  # inner group
            prefix_end = match.start(grp) - match.start(0)  # до inner, "[" уже в prefix
            return match.group(0)[:prefix_end] + new_tj + b"] TJ"

        new_dec = re.sub(pat, replacer, dec)
        if new_dec == dec:
            new_dec = re.sub(pat2, replacer, dec)
        if new_dec != dec:
            mods.append((stream_start, stream_len, len_num_start, zlib.compress(new_dec, 9)))

    if not mods:
        print("[ERROR] Не найдено поле плательщика в target (y≈348.75, x>100)")
        sys.exit(1)

    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        data[len_num_start : len_num_start + len(old_len_str)] = new_len_str

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

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print("[OK] Имя плательщика взято из 13-02-26 — копия.pdf")
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
