#!/usr/bin/env python3
"""Универсальный патчер трансграничных чеков ВТБ.

Работает на уровне CID-байтов: парсит ToUnicode CMap и /W из PDF,
кодирует новый текст, заменяет в content stream с правильным
выравниванием (right-align к wall). Автоматически считает «Сумма
зачисления» = сумма × курс обмена.

Поля:
  amount      — «Сумма операции»       (100 ₽ → 10 000 ₽)
  credited    — «Сумма зачисления»     (13 796 UZS → 1 379 600 UZS)
  rate        — «Курс обмена»          (1 ₽ = 137,96 UZS)
  name        — имя получателя         (SULUKHAN MUYDINOVA)
  phone       — телефон получателя     (998388960287)
  account     — счет списания          (*9481)
  date        — дата операции          (18.03.2026, 02:11)
"""
from __future__ import annotations

import hashlib
import re
import zlib
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path


# ─── PDF parsing helpers ──────────────────────────────────────────────


def _parse_tounicode(data: bytes) -> dict[int, str]:
    """ToUnicode CMap: unicode_codepoint → CID hex (4-char uppercase)."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b'beginbfrange' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                src_start = int(mm.group(1), 16)
                src_end = int(mm.group(2), 16)
                dest = int(mm.group(3), 16)
                for i in range(src_end - src_start + 1):
                    uni_to_cid[dest + i] = f'{src_start + i:04X}'
        if b'beginbfchar' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
    return uni_to_cid


def _parse_cid_widths(data: bytes) -> dict[int, int]:
    """/W массива CIDFontType2: CID int → width."""
    m = re.search(rb'/W\s*\[(.*?)\]\s*/CIDToGIDMap', data, re.DOTALL)
    if not m:
        return {}
    tokens = re.findall(rb'\[|\]|\d+', m.group(1))
    widths: dict[int, int] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (b'[', b']'):
            i += 1
            continue
        start = int(tok)
        if i + 1 < len(tokens) and tokens[i + 1] == b'[':
            i += 2
            cid = start
            while i < len(tokens) and tokens[i] != b']':
                widths[cid] = int(tokens[i])
                cid += 1
                i += 1
            i += 1
            continue
        i += 1
    return widths


# ─── CID encoding ────────────────────────────────────────────────────


def _text_to_cid_bytes(text: str, uni_to_cid: dict[int, str]) -> bytes | None:
    """Текст → последовательность 2-byte CID (raw bytes для TJ literal string)."""
    result = bytearray()
    for c in text:
        cp = ord(c)
        cid_hex = uni_to_cid.get(cp)
        if cid_hex is None:
            return None
        cid_int = int(cid_hex, 16)
        result.append((cid_int >> 8) & 0xFF)
        result.append(cid_int & 0xFF)
    return bytes(result)


def _escape_pdf_literal(raw: bytes) -> bytes:
    """Escape bytes for PDF literal string ()."""
    out = bytearray()
    for b in raw:
        if b in (0x28, 0x29, 0x5C):  # ( ) backslash
            out.append(0x5C)
        out.append(b)
    return bytes(out)


def _build_tj_array(text: str, uni_to_cid: dict[int, str], kern: str) -> bytes | None:
    """Текст → TJ-массив: [(\x00\x14) kern (\x00\x13) kern ...] TJ."""
    parts = []
    for c in text:
        cp = ord(c)
        cid_hex = uni_to_cid.get(cp)
        if cid_hex is None:
            return None
        cid_int = int(cid_hex, 16)
        raw = bytes([(cid_int >> 8) & 0xFF, cid_int & 0xFF])
        parts.append(b'(' + _escape_pdf_literal(raw) + b')')
    return b'[' + (b' ' + kern.encode() + b' ').join(parts) + b']'


def _tj_advance_units(tj_content: bytes, cid_widths: dict[int, int]) -> float:
    """Ширина TJ в font units: sum(widths) - sum(kern)."""
    total = 0.0

    def _unescape(raw: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] == 0x5C and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
            else:
                out.append(raw[i])
                i += 1
        return bytes(out)

    for string_part, kern_part in re.findall(rb'\(([^)]*)\)|(-?\d+(?:\.\d+)?)', tj_content):
        if string_part or (not string_part and not kern_part):
            vals = list(_unescape(string_part))
            if len(vals) % 2 != 0:
                continue
            for i in range(0, len(vals), 2):
                cid = (vals[i] << 8) + vals[i + 1]
                total += cid_widths.get(cid, 0)
        if kern_part:
            total -= float(kern_part)
    return total


def _compute_wall(dec: bytes, cid_widths: dict[int, int]) -> float:
    """Определяет wall (правый край) из content stream.

    Берёт только правый столбец (tm_x > 150, right > 250), отбрасывает
    центрированный текст и короткие поля типа *9481.
    """
    walls = []
    pat = rb'1 0 0 1 ([\d.]+) ([\d.]+) Tm\s*\[([^\]]*)\]\s*TJ'
    for m in re.finditer(pat, dec):
        tm_x = float(m.group(1))
        if tm_x < 150:
            continue
        tj_content = m.group(3)
        kern_m = re.search(rb'-?\d+\.\d+', tj_content)
        if not kern_m:
            continue
        before = dec[:m.start()]
        tf_m = list(re.finditer(rb'/F\d+\s+([\d.]+)\s+Tf', before))
        if not tf_m:
            continue
        font_size = float(tf_m[-1].group(1))
        units = _tj_advance_units(tj_content, cid_widths)
        right = tm_x + units * (font_size / 1000.0)
        if right > 254:
            walls.append(right)
    if walls:
        walls.sort()
        mid = len(walls) // 2
        return walls[mid]
    return 257.08


# ─── Field extraction ────────────────────────────────────────────────


def extract_fields(pdf_data: bytes) -> dict[str, str]:
    """Извлекает поля из трансграничного чека ВТБ через fitz."""
    try:
        import fitz
    except ImportError:
        return {}

    doc = fitz.open(stream=pdf_data, filetype='pdf')
    if doc.page_count == 0:
        return {}
    page = doc[0]
    text = page.get_text()
    doc.close()

    fields: dict[str, str] = {}
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    FIELD_MAP = [
        ('Курс обмена', 'rate'),
        ('Сумма зачисления', 'credited'),
        ('Сумма операции', 'amount'),
        ('Дата операции', 'date'),
        ('Счет списания', 'account'),
        ('Счёт списания', 'account'),
        ('Получатель', 'name'),
        ('Телефон получателя', 'phone'),
        ('Банк получателя', 'bank'),
        ('Страна получателя', 'country'),
    ]

    for i, line in enumerate(lines):
        for label, key in FIELD_MAP:
            if label.lower() in line.lower().replace('\xa0', ' '):
                remainder = line[line.lower().replace('\xa0', ' ').index(label.lower()) + len(label):].strip()
                if remainder:
                    fields[key] = remainder.replace('\xa0', ' ')
                else:
                    for j in range(i + 1, min(i + 4, len(lines))):
                        val = lines[j].replace('\xa0', ' ').strip()
                        if val:
                            fields[key] = val
                            break
                break
    return fields


def parse_rate(rate_str: str) -> tuple[Decimal, str] | None:
    """Парсит '1 ₽ = 137,96 UZS' или '1 RUR = 0.1130 TJS' → (Decimal, currency)."""
    s = rate_str.replace('\xa0', ' ')
    m = re.match(r'1\s*(?:₽|RUR|RUB|руб\.?)\s*=\s*([\d\s,.]+)\s+(\w+)', s)
    if not m:
        return None
    rate_val = m.group(1).replace(' ', '').replace(',', '.')
    currency = m.group(2)
    return Decimal(rate_val), currency


def format_credited(value: Decimal, currency: str) -> str:
    """1379600.00 → '1 379 600 UZS' (без копеек если целое)."""
    if value == value.to_integral_value():
        int_val = int(value)
    else:
        int_val = None

    if int_val is not None:
        s = str(int_val)
    else:
        s = str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)).replace('.', ',')

    if int_val is not None:
        parts = []
        for i, c in enumerate(reversed(s)):
            if i and i % 3 == 0:
                parts.append(' ')
            parts.append(c)
        s = ''.join(reversed(parts))
    else:
        int_part, dec_part = s.split(',')
        formatted = []
        for i, c in enumerate(reversed(int_part)):
            if i and i % 3 == 0:
                formatted.append(' ')
            formatted.append(c)
        s = ''.join(reversed(formatted)) + ',' + dec_part

    return f'{s} {currency}'


def format_amount_rub(amount: int) -> str:
    """10000 → '10 000 ₽'."""
    s = str(amount)
    parts = []
    for i, c in enumerate(reversed(s)):
        if i and i % 3 == 0:
            parts.append(' ')
        parts.append(c)
    return ''.join(reversed(parts)) + ' ₽'


# ─── Content stream patching ─────────────────────────────────────────


def _find_tj_block_by_text(
    dec: bytes,
    text: str,
    uni_to_cid: dict[int, str],
    min_x: float = 0,
) -> re.Match | None:
    """Ищет TJ-блок содержащий `text` (по CID hex). Возвращает Match для Tm+TJ."""
    cid_bytes = _text_to_cid_bytes(text, uni_to_cid)
    if cid_bytes is None:
        return None

    pat = rb'(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*(\[([^\]]*)\]\s*TJ)'
    for m in re.finditer(pat, dec):
        tm_x = float(m.group(2))
        if tm_x < min_x:
            continue
        tj_content = m.group(6)
        # Собираем все raw bytes из literal strings в TJ
        raw_bytes = bytearray()
        for sm in re.finditer(rb'\(([^)]*)\)', tj_content):
            inner = sm.group(1)
            i = 0
            while i < len(inner):
                if inner[i:i + 1] == b'\\' and i + 1 < len(inner):
                    raw_bytes.append(inner[i + 1])
                    i += 2
                else:
                    raw_bytes.append(inner[i])
                    i += 1
        if cid_bytes in bytes(raw_bytes):
            return m
    return None


def _replace_tj_block(
    dec: bytes,
    old_match: re.Match,
    new_text: str,
    uni_to_cid: dict[int, str],
    cid_widths: dict[int, int],
    wall: float,
    kern: str | None = None,
    font_size: float | None = None,
) -> bytes:
    """Заменяет TJ-блок на новый текст с правильным выравниванием."""
    tj_content = old_match.group(6)

    if kern is None:
        kern_m = re.search(rb'(-?\d+\.\d+)', tj_content)
        kern = kern_m.group(1).decode() if kern_m else '-16.66667'

    if font_size is None:
        before = dec[:old_match.start()]
        tf_matches = list(re.finditer(rb'/F\d+\s+([\d.]+)\s+Tf', before))
        font_size = float(tf_matches[-1].group(1)) if tf_matches else 9.0

    new_tj = _build_tj_array(new_text, uni_to_cid, kern)
    if new_tj is None:
        return dec

    new_content = new_tj[1:-1]  # strip [ ]
    new_units = _tj_advance_units(new_content, cid_widths)
    new_x = wall - new_units * (font_size / 1000.0)

    repl = (
        old_match.group(1) +
        f'{new_x:.5f}'.encode() +
        old_match.group(3) +
        b' ' + new_tj + b' TJ'
    )
    return dec[:old_match.start()] + repl + dec[old_match.end():]


# ─── Main patch function ─────────────────────────────────────────────


def _replace_name_fitz(
    pdf_data: bytes,
    old_name: str,
    new_name: str,
) -> bytes:
    """Заменяет имя получателя через fitz: white rect + insert_text.

    Имя может встречаться в двух местах:
    1) Шапка (крупный шрифт, центрирование)
    2) Поле «Получатель» (правый столбец)
    """
    import fitz

    doc = fitz.open(stream=pdf_data, filetype='pdf')
    page = doc[0]
    blocks = page.get_text('dict')['blocks']

    for block in blocks:
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                span_text = span['text'].strip()
                if old_name.lower() not in span_text.lower():
                    continue
                bbox = fitz.Rect(span['bbox'])
                font_size = span['size']
                color = tuple(c / 255.0 for c in bytes.fromhex(
                    f'{span["color"]:06x}'
                )) if isinstance(span['color'], int) else (0.133, 0.133, 0.133)

                page.draw_rect(bbox, fill=(1, 1, 1), color=(1, 1, 1), width=0)

                old_width = fitz.get_text_length(old_name, fontsize=font_size, fontname='helv')
                new_width = fitz.get_text_length(new_name, fontsize=font_size, fontname='helv')

                if span['origin'][0] < 100:
                    # Centered header: keep centered
                    center = (bbox.x0 + bbox.x1) / 2
                    insert_x = center - new_width / 2
                else:
                    # Right-aligned field: align to right edge
                    insert_x = bbox.x1 - new_width

                page.insert_text(
                    (insert_x, span['origin'][1]),
                    new_name,
                    fontsize=font_size,
                    fontname='helv',
                    color=color,
                )

    result = doc.tobytes(garbage=0, deflate=True)
    doc.close()
    return result


def patch_vtb_transgran(
    pdf_data: bytes,
    *,
    amount: int | None = None,
    credited: str | None = None,
    auto_calc: bool = True,
    phone: str | None = None,
    date: str | None = None,
    name: str | None = None,
) -> tuple[bool, str | None, bytes | None]:
    """Патчит трансграничный чек ВТБ.

    amount   — новая сумма в рублях (int, например 10000)
    credited — «Сумма зачисления» вручную (если не auto_calc)
    auto_calc — автоматически считать credited = amount × rate
    phone    — новый номер телефона (только цифры)
    date     — новая дата/время ("18.03.2026, 02:44")
    name     — новое имя получателя (fitz overlay, любые символы)

    Возвращает (ok, error_msg, new_data).
    """
    data = bytearray(pdf_data)
    uni_to_cid = _parse_tounicode(data)
    if not uni_to_cid:
        return False, 'ToUnicode CMap не найден в PDF', None

    cid_widths = _parse_cid_widths(data)
    if not cid_widths:
        return False, '/W (ширины глифов) не найден в PDF', None

    fields = extract_fields(pdf_data)
    if not fields:
        return False, 'Не удалось извлечь поля из PDF', None

    old_amount_str = fields.get('amount', '')
    old_credited_str = fields.get('credited', '')
    old_phone_str = fields.get('phone', '')
    old_date_str = fields.get('date', '')
    old_name_str = fields.get('name', '')
    rate_str = fields.get('rate', '')

    # Подготовка новых значений
    new_amount_str = format_amount_rub(amount) if amount else None
    new_credited_str = None

    if amount and auto_calc and rate_str:
        parsed = parse_rate(rate_str)
        if parsed:
            rate_val, currency = parsed
            credited_val = Decimal(amount) * rate_val
            new_credited_str = format_credited(credited_val, currency)
    elif credited:
        new_credited_str = credited

    # Проверяем CID-доступность для всех CID-полей
    cid_replacements: list[tuple[str, str, str]] = []  # (old, new, label)

    if new_amount_str:
        for c in new_amount_str:
            if ord(c) not in uni_to_cid:
                return False, f'Сумма: символ «{c}» отсутствует в шрифте', None
        cid_replacements.append((old_amount_str.replace('\xa0', ' '), new_amount_str, 'Сумма'))

    if new_credited_str:
        for c in new_credited_str:
            if ord(c) not in uni_to_cid:
                return False, f'Зачисление: символ «{c}» отсутствует в шрифте', None
        cid_replacements.append((old_credited_str.replace('\xa0', ' '), new_credited_str, 'Зачисление'))

    if phone:
        for c in phone:
            if ord(c) not in uni_to_cid:
                return False, f'Телефон: символ «{c}» отсутствует в шрифте', None
        cid_replacements.append((old_phone_str.replace('\xa0', ' '), phone, 'Телефон'))

    if date:
        for c in date:
            if ord(c) not in uni_to_cid:
                return False, f'Дата: символ «{c}» отсутствует в шрифте', None
        cid_replacements.append((old_date_str.replace('\xa0', ' '), date, 'Дата'))

    # CID-level патч (amount, credited, phone, date)
    if cid_replacements:
        for m in re.finditer(
            rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL
        ):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(data):
                continue
            try:
                dec = zlib.decompress(bytes(data[stream_start: stream_start + stream_len]))
            except zlib.error:
                continue
            if b'BT' not in dec:
                continue

            wall = _compute_wall(dec, cid_widths)
            replaced = 0

            for old_val, new_val, label in cid_replacements:
                match = _find_tj_block_by_text(dec, old_val, uni_to_cid, min_x=100)
                if match:
                    dec = _replace_tj_block(dec, match, new_val, uni_to_cid, cid_widths, wall)
                    replaced += 1

            if replaced == 0:
                continue

            new_raw = zlib.compress(dec, 9)
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            delta = len(new_raw) - stream_len + (len(new_len_str) - len(old_len_str))

            data = bytearray(
                data[:stream_start] + new_raw + data[stream_start + stream_len:]
            )
            data[len_num_start: len_num_start + len(old_len_str)] = new_len_str

            _update_xref(data, stream_start, delta)
            _mutate_doc_id(data)
            break

    result_data = bytes(data)

    # Fitz overlay для имени (может содержать символы, отсутствующие в CMap)
    if name and old_name_str:
        result_data = _replace_name_fitz(result_data, old_name_str, name)

    # Формируем инфо-строку
    parts = []
    if new_amount_str:
        parts.append(f'Сумма: {old_amount_str} → {new_amount_str}')
    if new_credited_str:
        parts.append(f'Зачисление: {old_credited_str} → {new_credited_str}')
    if phone:
        parts.append(f'Телефон: {old_phone_str} → {phone}')
    if date:
        parts.append(f'Дата: {old_date_str} → {date}')
    if name:
        parts.append(f'Имя: {old_name_str} → {name}')
    info = ', '.join(parts) if parts else 'Нет замен'

    return True, info, result_data


def _update_xref(data: bytearray, stream_start: int, delta: int) -> None:
    xref_m = re.search(
        rb'xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)',
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb'(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)', entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1): em.start(1) + 10] = f'{offset + delta:010d}'.encode()
        data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

    startxref_m = re.search(rb'startxref\r?\n(\d+)\r?\n', data)
    if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        new_val = str(old_pos + delta).encode()
        data[pos: pos + len(str(old_pos))] = new_val


def _mutate_doc_id(data: bytearray) -> bool:
    id_m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', bytes(data))
    if not id_m:
        return False
    h = hashlib.md5(bytes(data)).hexdigest().upper()
    new_id1 = h.encode()
    new_id2 = h.encode()
    old1 = id_m.group(1)
    old2 = id_m.group(2)
    full_old = id_m.group(0)
    full_new = full_old.replace(old1, new_id1[:len(old1)].ljust(len(old1), b'0'), 1)
    full_new = full_new.replace(old2, new_id2[:len(old2)].ljust(len(old2), b'0'), 1)
    data[id_m.start(): id_m.end()] = full_new
    return True


# ─── CLI ──────────────────────────────────────────────────────────────


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print('Использование:')
        print('  python3 vtb_transgran_patch.py input.pdf --amount 10000')
        print('  python3 vtb_transgran_patch.py input.pdf --extract')
        sys.exit(1)

    inp = Path(sys.argv[1])
    pdf_data = inp.read_bytes()

    if '--extract' in sys.argv:
        fields = extract_fields(pdf_data)
        for k, v in fields.items():
            print(f'  {k}: {v}')
        rate_str = fields.get('rate', '')
        if rate_str:
            parsed = parse_rate(rate_str)
            if parsed:
                print(f'  rate_parsed: {parsed[0]} {parsed[1]}')
        sys.exit(0)

    amount = None
    for i, a in enumerate(sys.argv):
        if a == '--amount' and i + 1 < len(sys.argv):
            amount = int(sys.argv[i + 1])

    if amount is None:
        print('Укажите --amount <сумма>')
        sys.exit(1)

    ok, info, new_data = patch_vtb_transgran(pdf_data, amount=amount)
    if not ok:
        print(f'ОШИБКА: {info}')
        sys.exit(1)

    out = inp.with_stem(inp.stem + '_patched')
    out.write_bytes(new_data)
    print(f'[OK] {info}')
    print(f'Сохранено: {out}')
