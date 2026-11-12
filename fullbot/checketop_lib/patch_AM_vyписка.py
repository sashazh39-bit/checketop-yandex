#!/usr/bin/env python3
"""Patch AM_1773797576813.pdf — pure CID patching, no font overlay."""
import sys
import re
import zlib
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cid_patch_amount import patch_replacements, _parse_tounicode, _extend_tounicode_identity, _encode_cid


def main():
    inp = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "/Users/aleksandrzerebatav/Downloads/AM_1773797576813.pdf"
    )
    out = Path(
        sys.argv[2]
        if len(sys.argv) > 2
        else inp.parent / (inp.stem + "_patched.pdf")
    )

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mktemp(suffix=".pdf"))

    # === Phase 1: full hex string CID replacements ===
    print("=== Phase 1: CID patching ===")
    replacements = [
        ("94,82 RUR", "3 094,82 RUR"),
        ("4,82 RUR", "94,82 RUR"),
        ("100,00 RUR", "10 000,00 RUR"),
        ("10,00 RUR", "7 000,00 RUR"),
        ("-10,00 RUR", "-7 000,00 RUR"),
        ("40817810280480002477", "40817810280480002476"),
        ("C161803260059362", "C161803260033848"),
        ("Жеребятьев Александр", "Жарков Ефим"),
        ("Евгеньевич", "Ееннадьевич"),
        ("238753, РОССИЯ,", "238401, РОССИЯ,"),
        ("Советск, УЛИЦА Каштановая, д.", "Славск, УЛИЦА Каштановая, д."),
        ("8В, кв. 78", "12В, кв. 56"),
    ]
    patch_replacements(inp, tmp, replacements)

    # === Phase 2: substring CID replacements ===
    print("\n=== Phase 2: Substring CID + font switch ===")
    data = bytearray(tmp.read_bytes())
    uni_to_cid = _parse_tounicode(data)
    if not uni_to_cid:
        print("[ERROR] ToUnicode не найден", file=sys.stderr)
        shutil.move(str(tmp), str(out))
        return 1

    required = set()
    for text in ["C161803260033848", "Жарков Е. Е.", "10 000,00 RUR", "3 094,82 RUR"]:
        for c in text:
            cp = ord(c)
            required.add(0xA0 if cp == 0x20 else cp)
            if cp == 0x20:
                required.add(0x20)
    data, uni_to_cid = _extend_tounicode_identity(data, required, uni_to_cid)

    substring_replacements = [
        ("C161803260059362", "C161803260033848"),
        ("Жеребятьев А. Е.", "Жарков Е. Е."),
    ]

    # Bold transaction: "100,00 RUR"(F3) → "10 000,00 RUR"(F3) — all chars in F3
    old_bold_hex = b"0011001200120013001200120010001400150014"
    new_bold_hex = b"0011001200100012001200120013001200120010001400150014"

    sub_count = 0
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
        if b"BT" not in dec:
            continue

        new_dec = dec

        for old_val, new_val in substring_replacements:
            old_cid = _encode_cid(old_val, uni_to_cid)
            new_cid = _encode_cid(new_val, uni_to_cid)
            if not old_cid or not new_cid:
                continue
            old_inner = old_cid[1:-1]
            new_inner = new_cid[1:-1]
            if old_inner in new_dec:
                new_dec = new_dec.replace(old_inner, new_inner)
                sub_count += 1
                print(f"[OK-sub] {old_val} → {new_val}")

        # Bold amount: replace hex in F3 context (no font switch needed)
        if old_bold_hex in new_dec:
            old_tag = b"<" + old_bold_hex + b">"
            new_tag = b"<" + new_bold_hex + b">"
            if old_tag in new_dec:
                new_dec = new_dec.replace(old_tag, new_tag)
                sub_count += 1
                print("[OK-sub] 100,00 RUR Bold(F3) → 10 000,00 RUR Bold(F3)")

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)

            data = bytearray(
                bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len :])
            )
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str

            xref_m = re.search(
                rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
            )
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

    if sub_count > 0:
        tmp.write_bytes(data)
        print(f"[OK] Substring замен: {sub_count}")

    # === Phase 3: Tm position adjustments (right-align amounts) ===
    print("\n=== Phase 3: Tm position adjustments ===")
    data = bytearray(tmp.read_bytes())

    # Font F1 (Arial) char widths in /1000 units:
    #   digits 0-9: 556, space: 277, comma: 277, minus: 333, R: 722, U: 722
    # Text widths at size 8 = sum(widths) * 8 / 1000
    # Original right edges (from fitz bbox): header ~566.979, transaction ~568.028
    #
    # Each tuple: (old_x_y_tm_bytes, new_x_y_tm_bytes)
    tm_adjustments = [
        # Header amounts (right edge ≈ 566.979)
        (b"531.875 662.497 Tm", b"527.427 662.497 Tm"),  # "4,82 RUR" → "94,82 RUR"
        (b"522.977 645.447 Tm", b"511.865 645.447 Tm"),  # "100,00 RUR" → "10 000,00 RUR"
        (b"527.426 628.397 Tm", b"516.314 628.397 Tm"),  # "10,00 RUR" → "7 000,00 RUR"
        (b"527.426 611.347 Tm", b"516.314 611.347 Tm"),  # "94,82 RUR" → "3 094,82 RUR"
        (b"527.426 594.297 Tm", b"516.314 594.297 Tm"),  # "94,82 RUR" → "3 094,82 RUR"
        (b"527.426 537.497 Tm", b"516.314 537.497 Tm"),  # "94,82 RUR" → "3 094,82 RUR"
        # Transaction amounts (right edge ≈ 568.028)
        (b"525.812 427.104 Tm", b"514.700 427.104 Tm"),  # "-10,00 RUR" → "-7 000,00 RUR"
        (b"524.026 403.006 Tm", b"512.914 403.006 Tm"),  # "100,00 RUR" Bold → "10 000,00 RUR"
    ]

    tm_count = 0
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
        if b"BT" not in dec:
            continue

        new_dec = dec
        for old_tm, new_tm in tm_adjustments:
            if old_tm in new_dec:
                new_dec = new_dec.replace(old_tm, new_tm)
                tm_count += 1

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)

            data = bytearray(
                bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len :])
            )
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str

            xref_m = re.search(
                rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
            )
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

    if tm_count > 0:
        tmp.write_bytes(data)
        print(f"[OK] Tm позиций скорректировано: {tm_count}")
    else:
        print("[WARN] Tm позиции не найдены")

    shutil.move(str(tmp), str(out))

    orig_size = inp.stat().st_size
    new_size = out.stat().st_size
    print(f"\nРазмер: {orig_size} → {new_size} байт (delta: {new_size - orig_size:+d})")
    print(f"[OK] Сохранено: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
