#!/usr/bin/env python3
"""
Пересадка font-объектов из оригинального PDF в сгенерированный.
Ремаппинг CID в content stream для совпадения с оригинальной нумерацией.

Результат: PDF со структурой openhtmltopdf, но font subset идентичен оригиналу.
"""

import re
import zlib
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent / "база_чеков" / "vtb" / "СБП"


def _extract_tounicode(data: bytes) -> dict[int, str]:
    """CID→Unicode из ToUnicode stream."""
    cid2uni = {}
    for m in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.DOTALL):
        try:
            dec = zlib.decompress(m.group(1)[:6000])
            if b'beginbfrange' not in dec:
                continue
            tu = dec.decode('latin-1')
            for rm in re.finditer(
                r'<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>', tu
            ):
                s, e, u = int(rm.group(1), 16), int(rm.group(2), 16), int(rm.group(3), 16)
                for c in range(s, e + 1):
                    cid2uni[c] = chr(u + (c - s))
            for bfc in re.finditer(r'beginbfchar\s*(.*?)endbfchar', tu, re.DOTALL):
                for em in re.finditer(r'<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>', bfc.group(1)):
                    cid2uni[int(em.group(1), 16)] = chr(int(em.group(2), 16))
            break
        except Exception:
            pass
    return cid2uni


def _extract_obj_raw(data: bytes, obj_n: int) -> bytes:
    """Извлекает полные байты объекта N 0 obj ... endobj."""
    pattern = re.compile(
        rb'(?:^|\n|\r)(' + str(obj_n).encode() + rb' 0 obj\b.*?endobj)',
        re.DOTALL,
    )
    m = pattern.search(data)
    if m:
        return m.group(1)
    return b''


def _find_obj_nums(data: bytes) -> dict:
    """Находит номера объектов по типу."""
    result = {}
    for m in re.finditer(rb'(\d+) 0 obj\s*<<(.*?)>>', data, re.DOTALL):
        n = int(m.group(1))
        h = m.group(2)
        if b'/Subtype /Type0' in h:
            result['type0'] = n
        elif b'/Subtype /CIDFontType2' in h:
            result['cidfont'] = n
        elif b'/Type /FontDescriptor' in h:
            result['fontdesc'] = n
        # ToUnicode stream
        tu_m = re.search(rb'/ToUnicode\s+(\d+)', h)
        if tu_m and b'/Subtype /Type0' in h:
            result['tounicode'] = int(tu_m.group(1))
        # CIDToGIDMap
        cid_m = re.search(rb'/CIDToGIDMap\s+(\d+)', h)
        if cid_m:
            result['cidtogidmap'] = int(cid_m.group(1))
        # FontFile2
        ff_m = re.search(rb'/FontFile2\s+(\d+)', h)
        if ff_m:
            result['fontfile'] = int(ff_m.group(1))
    return result


def _parse_pdf_string(raw_bytes: bytes) -> list[int]:
    """Парсит PDF-строку (parenthesized) с учётом escape-последовательностей."""
    result = []
    i = 0
    while i < len(raw_bytes):
        b = raw_bytes[i]
        if b == 0x5C:  # backslash
            i += 1
            if i < len(raw_bytes):
                nb = raw_bytes[i]
                if nb == 0x6E:  # \n
                    result.append(0x0A)
                elif nb == 0x72:  # \r
                    result.append(0x0D)
                elif nb == 0x74:  # \t
                    result.append(0x09)
                else:
                    result.append(nb)
        else:
            result.append(b)
        i += 1
    return result


def _remap_content_stream(
    data: bytes, gen_cid2uni: dict, donor_uni2cid: dict
) -> bytes:
    """Ремаппит CIDs в content stream: наш CID → Unicode → донорский CID.
    Все строки конвертируются в hex-формат <...> для избежания проблем с escaping."""
    for m in re.finditer(rb'(\d+) 0 obj\s*<<(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        header = m.group(2)
        if b'/Length' not in header:
            continue
        length = int(re.search(rb'/Length\s+(\d+)', header).group(1))
        stream_start = m.end()
        compressed = data[stream_start:stream_start + length]
        try:
            dec = zlib.decompress(compressed)
        except Exception:
            continue
        if b'Tm' not in dec:
            continue

        raw_stream = bytearray(dec)
        text = dec.decode('latin-1')

        # Find all TJ arrays and remap CIDs
        # Work on bytes level to handle escaping correctly
        result_parts = []
        last_end = 0

        # Match [...] TJ patterns (non-empty arrays only, no DOTALL)
        for tj_m in re.finditer(rb'\[([^\]]+)\]\s*TJ', dec):
            tj_inner = tj_m.group(1)
            tj_start = tj_m.start(1)
            tj_end = tj_m.end(1)

            new_tokens = []
            pos = 0
            while pos < len(tj_inner):
                b = tj_inner[pos]

                if b == 0x28:  # '(' start of string
                    # Find matching ')'
                    depth = 1
                    end = pos + 1
                    while end < len(tj_inner) and depth > 0:
                        if tj_inner[end] == 0x5C:  # backslash
                            end += 2
                            continue
                        if tj_inner[end] == 0x28:
                            depth += 1
                        elif tj_inner[end] == 0x29:
                            depth -= 1
                        end += 1
                    string_bytes = tj_inner[pos + 1:end - 1]
                    parsed = _parse_pdf_string(string_bytes)

                    # Remap CID pairs → hex format
                    hex_parts = []
                    for i in range(0, len(parsed) - 1, 2):
                        cid = (parsed[i] << 8) | parsed[i + 1]
                        uni_ch = gen_cid2uni.get(cid)
                        if uni_ch and uni_ch in donor_uni2cid:
                            new_cid = donor_uni2cid[uni_ch]
                        else:
                            new_cid = cid
                        hex_parts.append(f"{new_cid:04X}")

                    new_tokens.append(b"<" + "".join(hex_parts).encode() + b">")
                    pos = end

                elif b == 0x3C:  # '<' start of hex string
                    end = tj_inner.index(0x3E, pos) + 1  # find '>'
                    hex_str = tj_inner[pos + 1:end - 1].decode('ascii')
                    hex_parts = []
                    for i in range(0, len(hex_str) - 3, 4):
                        cid = int(hex_str[i:i + 4], 16)
                        uni_ch = gen_cid2uni.get(cid)
                        if uni_ch and uni_ch in donor_uni2cid:
                            new_cid = donor_uni2cid[uni_ch]
                        else:
                            new_cid = cid
                        hex_parts.append(f"{new_cid:04X}")
                    new_tokens.append(b"<" + "".join(hex_parts).encode() + b">")
                    pos = end

                elif b in (0x2D, 0x2E) or (0x30 <= b <= 0x39):
                    # Number (kern value)
                    num_start = pos
                    while pos < len(tj_inner) and (
                        tj_inner[pos] in (0x2D, 0x2E) or 0x30 <= tj_inner[pos] <= 0x39
                    ):
                        pos += 1
                    new_tokens.append(tj_inner[num_start:pos])

                elif b in (0x20, 0x0A, 0x0D, 0x09):
                    pos += 1
                else:
                    pos += 1

            # Rebuild TJ array
            new_inner = b" ".join(new_tokens)
            result_parts.append(dec[last_end:tj_start])
            result_parts.append(new_inner)
            last_end = tj_end

        result_parts.append(dec[last_end:])
        new_dec = b"".join(result_parts)
        new_compressed = zlib.compress(new_dec, 9)

        new_header = re.sub(
            rb'/Length\s+\d+',
            f"/Length {len(new_compressed)}".encode(),
            data[m.start():stream_start],
        )
        new_data = (
            data[:m.start()]
            + new_header
            + new_compressed
            + data[stream_start + length:]
        )
        return new_data

    return data


def _replace_obj(data: bytes, obj_n: int, new_obj_bytes: bytes) -> bytes:
    """Заменяет объект obj_n на new_obj_bytes."""
    pattern = re.compile(
        rb'(?:^|\r?\n)' + str(obj_n).encode() + rb' 0 obj\b.*?endobj',
        re.DOTALL,
    )
    m = pattern.search(data)
    if not m:
        return data

    prefix_nl = b'\n' if m.start() > 0 and data[m.start():m.start()+1] in (b'\n', b'\r') else b''
    start = m.start()
    if data[start:start+1] in (b'\n', b'\r'):
        start += 1
        if data[start:start+1] == b'\n' and data[start-1:start] == b'\r':
            start += 1

    return data[:start] + new_obj_bytes + data[m.end():]


def _adjust_obj_refs(obj_bytes: bytes, ref_map: dict) -> bytes:
    """Меняет ссылки на объекты внутри obj_bytes по ref_map.
    Использует placeholder для избежания двойной замены."""
    result = obj_bytes
    # Pass 1: replace with unique placeholders
    for old_n in ref_map:
        result = re.sub(
            rf'(?<!\d){old_n} 0 R'.encode(),
            f'__XREF{old_n}__ 0 R'.encode(),
            result,
        )
    # Pass 2: replace placeholders with final values
    for old_n, new_n in ref_map.items():
        result = result.replace(
            f'__XREF{old_n}__ 0 R'.encode(),
            f'{new_n} 0 R'.encode(),
        )
    return result


def _fix_xref(data: bytes) -> bytes:
    """Пересчитывает xref и startxref."""
    offsets = {}
    for m in re.finditer(rb'(\d+) 0 obj', data):
        offsets[int(m.group(1))] = m.start()

    xref_start = -1
    pos = 0
    while True:
        idx = data.find(b"xref", pos)
        if idx < 0:
            break
        if (idx == 0 or data[idx - 1:idx] in (b"\n", b"\r")):
            after = data[idx + 4:idx + 5]
            if after in (b"\n", b"\r"):
                xref_start = idx
                break
        pos = idx + 1

    if xref_start < 0:
        return data

    trailer_start = data.find(b"trailer", xref_start)
    if trailer_start < 0:
        return data

    trailer_end = data.find(b"%%EOF", trailer_start)
    if trailer_end < 0:
        trailer_end = len(data)
    else:
        trailer_end += 5

    trailer_dict = re.search(
        rb'trailer\s*(<<.*?>>)', data[trailer_start:trailer_end], re.DOTALL
    )
    if not trailer_dict:
        return data

    max_obj = max(offsets.keys()) if offsets else 0
    xref_lines = [f"xref\n0 {max_obj + 1}\n".encode()]
    xref_lines.append(b"0000000000 65535 f \n")
    for i in range(1, max_obj + 1):
        off = offsets.get(i, 0)
        xref_lines.append(f"{off:010d} 00000 n \n".encode())

    before = data[:xref_start]
    new_xref_offset = len(before)
    new_xref_bytes = b"".join(xref_lines)

    tail = (
        new_xref_bytes
        + b"trailer\n"
        + trailer_dict.group(1)
        + b"\nstartxref\n"
        + str(new_xref_offset).encode()
        + b"\n%%EOF\n"
    )

    return before + tail


def find_best_donor(needed_chars: set[str]) -> tuple[str, int]:
    """Находит донора с максимальным покрытием нужных символов."""
    best_path = None
    best_missing = 999
    best_size = 999999

    for f in os.listdir(BASE_DIR):
        if not f.endswith('.pdf'):
            continue
        path = str(BASE_DIR / f)
        try:
            data = open(path, 'rb').read()
            cid2uni = _extract_tounicode(data)
            avail = set(cid2uni.values())
            missing = needed_chars - avail - {' '}
            missing_count = len([c for c in missing if c.strip()])
            if missing_count < best_missing or (
                missing_count == best_missing and len(data) < best_size
            ):
                best_missing = missing_count
                best_size = len(data)
                best_path = path
        except Exception:
            pass

    return best_path, best_missing


def transplant(generated_path: str, donor_path: str, output_path: str) -> dict:
    """
    Трансплантация: берём сгенерированный PDF (content stream, metadata),
    заменяем font-объекты на донорские, ремаппим CID.
    """
    gen_data = open(generated_path, 'rb').read()
    donor_data = open(donor_path, 'rb').read()

    # 1. Extract CID mappings
    gen_cid2uni = _extract_tounicode(gen_data)
    donor_cid2uni = _extract_tounicode(donor_data)
    donor_uni2cid = {v: k for k, v in donor_cid2uni.items()}

    # Check coverage
    gen_chars = set(gen_cid2uni.values())
    donor_chars = set(donor_cid2uni.values())
    missing = gen_chars - donor_chars
    missing_printable = [c for c in missing if c.strip()]

    # 2. Remap CIDs in content stream
    result = _remap_content_stream(gen_data, gen_cid2uni, donor_uni2cid)

    # 3. Find object numbers in both PDFs
    gen_objs = _find_obj_nums(gen_data)
    donor_objs = _find_obj_nums(donor_data)

    # 4. Extract donor's font objects
    # Objects to transplant: Type0, CIDFont, FontDescriptor, ToUnicode, CIDToGIDMap, FontFile2
    transplant_map = {
        'type0': ('type0', gen_objs.get('type0'), donor_objs.get('type0')),
        'cidfont': ('cidfont', gen_objs.get('cidfont'), donor_objs.get('cidfont')),
        'fontdesc': ('fontdesc', gen_objs.get('fontdesc'), donor_objs.get('fontdesc')),
        'tounicode': ('tounicode', gen_objs.get('tounicode'), donor_objs.get('tounicode')),
        'cidtogidmap': ('cidtogidmap', gen_objs.get('cidtogidmap'), donor_objs.get('cidtogidmap')),
        'fontfile': ('fontfile', gen_objs.get('fontfile'), donor_objs.get('fontfile')),
    }

    # Build reference map: donor_obj_n → gen_obj_n
    ref_map = {}
    for key, (_, gen_n, donor_n) in transplant_map.items():
        if gen_n is not None and donor_n is not None and gen_n != donor_n:
            ref_map[donor_n] = gen_n

    # 5. Replace each font object
    for key, (_, gen_n, donor_n) in transplant_map.items():
        if gen_n is None or donor_n is None:
            continue

        donor_obj = _extract_obj_raw(donor_data, donor_n)
        if not donor_obj:
            continue

        # Adjust object number and internal references
        new_obj = re.sub(
            rb'^\d+ 0 obj',
            f'{gen_n} 0 obj'.encode(),
            donor_obj,
        )
        new_obj = _adjust_obj_refs(new_obj, ref_map)
        result = _replace_obj(result, gen_n, new_obj)

    # 6. Fix xref
    result = _fix_xref(result)

    # 7. Write
    open(output_path, 'wb').write(result)

    return {
        'size': len(result),
        'missing_chars': missing_printable,
        'gen_chars': len(gen_chars),
        'donor_chars': len(donor_chars),
        'donor': donor_path,
    }


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 4:
        print("Usage: font_transplant.py <generated.pdf> <donor.pdf> <output.pdf>")
        sys.exit(1)
    info = transplant(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"Done: {info['size']} bytes ({info['size']/1024:.1f} KB)")
    if info['missing_chars']:
        print(f"WARNING: Missing chars: {info['missing_chars']}")
