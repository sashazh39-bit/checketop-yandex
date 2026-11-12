#!/usr/bin/env python3
"""
Прямой патчер PDF-донора: меняет только текст в TJ-массивах,
сохраняя ВСЮ структуру, drawing-команды, font-объекты, метаданные.
"""

import re
import zlib
import random
import string
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent / "база_чеков" / "vtb" / "СБП"
DEFAULT_DONOR = BASE_DIR / "15-03-26_00-00.pdf"


def _build_cid_maps(data: bytes):
    """Строит CID↔Unicode маппинг из ToUnicode stream."""
    cid2uni = {}
    uni2cid = {}
    for m in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.DOTALL):
        try:
            dec = zlib.decompress(m.group(1)[:6000])
        except Exception:
            continue
        if b'beginbfrange' not in dec:
            continue
        tu = dec.decode('latin-1')
        for rm in re.finditer(
            r'<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>', tu
        ):
            s = int(rm.group(1), 16)
            e = int(rm.group(2), 16)
            u = int(rm.group(3), 16)
            for i in range(e - s + 1):
                cid2uni[s + i] = chr(u + i)
                uni2cid[chr(u + i)] = s + i
    return cid2uni, uni2cid


def _build_widths(data: bytes):
    """Парсит /W массив из CIDFont для расчёта ширин символов."""
    m = re.search(rb'/W\s+\[([\s\S]*?)\]\s*/', data)
    if not m:
        return {}
    w_text = m.group(1).decode('latin-1')
    widths = {}
    for start_s, vals_s in re.findall(r'(\d+)\s+\[([^\]]+)\]', w_text):
        start = int(start_s)
        vals = [float(v) for v in vals_s.split()]
        for i, w in enumerate(vals):
            widths[start + i] = w
    return widths


def _encode_text_to_tj(text: str, uni2cid: dict, kern: float) -> bytes:
    """Кодирует текст в формат TJ-массива с 2-байтовыми CID в parenthesized strings.
    
    Формат: [(\x02$)-21.42857 (\x02M)-21.42857 ...(\x02+)] TJ
    Kern ставится между каждой парой символов, последний без kern.
    """
    parts = []
    chars = list(text)
    for i, ch in enumerate(chars):
        cid = uni2cid.get(ch)
        if cid is None:
            raise ValueError(f"Character {ch!r} (U+{ord(ch):04X}) not in donor font")
        hi = (cid >> 8) & 0xFF
        lo = cid & 0xFF
        # Escape special bytes for PDF parenthesized strings
        s = b''
        for byte_val in [hi, lo]:
            if byte_val == 0x28:       # (
                s += b'\\('
            elif byte_val == 0x29:     # )
                s += b'\\)'
            elif byte_val == 0x5C:     # backslash
                s += b'\\\\'
            elif byte_val == 0x0A:     # newline
                s += b'\\n'
            elif byte_val == 0x0D:     # carriage return
                s += b'\\r'
            else:
                s += bytes([byte_val])
        
        if i < len(chars) - 1:
            parts.append(b'(' + s + b')' + f'{kern:.5f}'.rstrip('0').rstrip('.').encode())
        else:
            parts.append(b'(' + s + b')')
    
    return b'[' + b' '.join(parts) + b'] TJ'


def _calc_advance(text: str, uni2cid: dict, widths: dict, font_size: float, kern: float) -> float:
    """Рассчитывает горизонтальное продвижение текста (в points)."""
    total = 0.0
    chars = list(text)
    for i, ch in enumerate(chars):
        cid = uni2cid.get(ch, 0)
        w = widths.get(cid, 500)
        total += w * font_size / 1000.0
        if i < len(chars) - 1:
            total -= kern * font_size / 1000.0
    return total


def _day_of_year(date_str: str) -> int:
    """Возвращает номер дня в году для даты ДД.ММ.ГГГГ."""
    import datetime
    try:
        d = datetime.datetime.strptime(date_str, '%d.%m.%Y')
        return d.timetuple().tm_yday
    except Exception:
        return 76


def _gen_operation_id(available_latin: set = None, date_str: str = None):
    """Генерирует operation ID похожий на оригинал.

    15-03 стиль (K): B60[day][10 random digits]K0B1014001  — K на позиции 15
    17-03 стиль (G): B60[day][12 random digits]G1011001    — G на позиции 17
    Форматы установлены путём обратной разработки реальных VTB чеков.
    """
    use_g = available_latin and 'G' in available_latin
    use_k = available_latin and 'K' in available_latin

    day = _day_of_year(date_str) if date_str else 76
    day_str = f'{day:02d}'

    rand_digits = lambda n: ''.join(random.choice('0123456789') for _ in range(n))

    if use_g and not use_k:
        # 17-03 стиль: G на позиции 17, суффикс G1011001
        middle = rand_digits(12)
        op = f'B60{day_str}{middle}G1011001'
    else:
        # 15-03 стиль: K на позиции 15, суффикс K0B1014001
        middle = rand_digits(10)
        op = f'B60{day_str}{middle}K0B1014001'

    assert len(op) == 25, f"Operation ID length error: {len(op)} != 25 for {op!r}"
    return op


def patch(donor_path: str, output_path: str, *,
          payer: str = None,
          recipient: str = None,
          date: str = None,
          time: str = None,
          phone: str = None,
          bank: str = None,
          amount: int = None,
          account: str = None,
          op_id: str = None,
          keep_op_id: bool = False,
          doc_id_changes: int = 1):
    """
    Патчит PDF-донор, меняя только текст в TJ-массивах.
    Сохраняет ВСЮ структуру: drawing-команды, font-объекты, метаданные.
    """
    data = bytearray(open(donor_path, 'rb').read())
    cid2uni, uni2cid = _build_cid_maps(bytes(data))
    widths = _build_widths(bytes(data))

    # --- 1. Decompress content stream ---
    cs_m = re.search(rb'5 0 obj\s*<<.*?/Length\s+(\d+).*?>>\s*stream\r?\n', bytes(data), re.DOTALL)
    cs_len = int(cs_m.group(1))
    cs_start = cs_m.end()
    cs_raw = bytes(data[cs_start:cs_start + cs_len])
    dec = zlib.decompress(cs_raw)

    # --- 2. Parse BT..ET blocks ---
    blocks = []
    pos = 0
    while True:
        bt = dec.find(b'BT', pos)
        if bt < 0:
            break
        et = dec.find(b'ET', bt)
        block_bytes = dec[bt:et + 2]
        
        tm_m = re.search(rb'1 0 0 1 ([\d.]+) ([\d.]+) Tm', block_bytes)
        tf_m = re.search(rb'/F1 ([\d.]+) Tf', block_bytes)
        kern_m = re.search(rb'\)([-\d.]+)\s', block_bytes)
        tj_m = re.search(rb'\[.*?\]\s*TJ', block_bytes, re.DOTALL)
        
        # Decode text
        text = ''
        if tj_m:
            inner = tj_m.group(0)
            i = 0
            while i < len(inner):
                if inner[i:i+1] == b'(':
                    j = i + 1
                    raw_bytes = b''
                    while j < len(inner):
                        if inner[j:j+1] == b'\\':
                            esc = inner[j+1:j+2]
                            if esc == b'(': raw_bytes += b'('; j += 2
                            elif esc == b')': raw_bytes += b')'; j += 2
                            elif esc == b'\\': raw_bytes += b'\\'; j += 2
                            elif esc == b'n': raw_bytes += b'\n'; j += 2
                            elif esc == b'r': raw_bytes += b'\r'; j += 2
                            else:
                                oct_s = b''
                                k = j + 1
                                while k < len(inner) and k < j + 4 and 0x30 <= inner[k] <= 0x37:
                                    oct_s += inner[k:k+1]
                                    k += 1
                                raw_bytes += bytes([int(oct_s, 8)]) if oct_s else b''
                                j = k
                        elif inner[j:j+1] == b')':
                            break
                        else:
                            raw_bytes += inner[j:j+1]
                            j += 1
                    for k in range(0, len(raw_bytes) - 1, 2):
                        cid = (raw_bytes[k] << 8) | raw_bytes[k + 1]
                        text += cid2uni.get(cid, '?')
                    i = j + 1
                else:
                    i += 1
        
        blocks.append({
            'start': bt, 'end': et + 2,
            'x': float(tm_m.group(1)) if tm_m else 0,
            'y': float(tm_m.group(2)) if tm_m else 0,
            'x_str': tm_m.group(1).decode() if tm_m else '0',
            'y_str': tm_m.group(2).decode() if tm_m else '0',
            'size': float(tf_m.group(1)) if tf_m else 0,
            'kern': float(kern_m.group(1)) if kern_m else 0,
            'text': text,
            'tj_span': (bt + tj_m.start(), bt + tj_m.end()) if tj_m else None,
            'tm_match': tm_m,
            'raw': block_bytes,
        })
        pos = et + 2

    # --- 3. Build replacement map ---
    # Block assignments (from donor analysis):
    # 0: title "Исходящий перевод СБП" - keep
    # 1: payer name in title
    # 2: label "Статус" - keep
    # 3: value "Выполнено" - keep
    # 4: label "Дата операции" - keep
    # 5: value date+time
    # 6: label "Счет списания" - keep
    # 7: value account
    # 8: label "Имя плательщика" - keep
    # 9: value payer FIO
    # 10: label "Получатель" - keep
    # 11: value recipient FIO
    # 12: label "Телефон получателя" - keep
    # 13: value phone
    # 14: label "Банк получателя" - keep
    # 15: value bank name
    # 16: label "ID операции в СБП" - keep
    # 17: value op ID part 1
    # 18: value op ID part 2
    # 19: label "Сумма операции" - keep (large font)
    # 20: value amount - keep format
    # 21: stamp line 1 - keep
    # 22: stamp line 2 - keep
    
    available_latin = {c for c in uni2cid if c.isalpha() and ord(c) < 256}

    replacements = {}
    
    if payer:
        replacements[1] = payer
        replacements[9] = payer
    
    if date and time:
        replacements[5] = f"{date}, {time}"
    elif date:
        replacements[5] = f"{date}, {blocks[5]['text'].split(', ')[1]}"
    elif time:
        parts = blocks[5]['text'].split(', ')
        replacements[5] = f"{parts[0]}, {time}"
    
    if account:
        # Preserve the * prefix that's part of the account display
        account_str = account.lstrip('*')
        orig_account = blocks[7]['text'] if len(blocks) > 7 else ''
        if orig_account.startswith('*'):
            account_str = '*' + account_str
        replacements[7] = account_str
    
    if recipient:
        replacements[11] = recipient
    
    if phone:
        phone_fmt = phone.replace('-', '\u2011')
        replacements[13] = phone_fmt
    
    if bank:
        bank_fmt = bank.replace('-', '\u2011')
        replacements[15] = bank_fmt
    
    # Operation ID handling
    available_latin = {ch for ch in uni2cid if 'A' <= ch <= 'Z'}
    if op_id:
        new_op_id = op_id
        if len(new_op_id) > 25:
            replacements[17] = new_op_id[:25]
            replacements[18] = new_op_id[25:]
        else:
            replacements[17] = new_op_id
        print(f"  Операция ID: {new_op_id} (явный)")
    elif keep_op_id:
        # Leave blocks 17/18 unchanged — donor's real operation ID stays
        print(f"  Операция ID: сохранён из донора (реальный VTB)")
    else:
        new_op_id = _gen_operation_id(available_latin=available_latin, date_str=date)
        if len(new_op_id) > 25:
            replacements[17] = new_op_id[:25]
            replacements[18] = new_op_id[25:]
        else:
            replacements[17] = new_op_id
        print(f"  Операция ID: {new_op_id} (свежий, стиль: {'G@17' if 'G' in available_latin else 'K@15'})")
    
    if amount is not None:
        amt_str = f"{amount:,}".replace(',', ' ')
        replacements[20] = f"{amt_str} ₽"

    # --- 4. Validate all characters exist in donor font ---
    for idx, new_text in replacements.items():
        for ch in new_text:
            if ch not in uni2cid:
                raise ValueError(
                    f"Block {idx}: character {ch!r} (U+{ord(ch):04X}) not in donor font. "
                    f"Available: {''.join(sorted(uni2cid.keys()))}"
                )

    # --- 5. Apply replacements to decompressed content stream ---
    # Work backwards to preserve offsets
    patches = []  # (start_in_dec, end_in_dec, new_bytes)
    
    for idx in sorted(replacements.keys(), reverse=True):
        if idx >= len(blocks):
            continue
        b = blocks[idx]
        new_text = replacements[idx]
        kern = b['kern']
        font_size = b['size']
        
        # Calculate new x position (right-aligned)
        orig_advance = _calc_advance(b['text'], uni2cid, widths, font_size, kern)
        new_advance = _calc_advance(new_text, uni2cid, widths, font_size, kern)
        right_edge = b['x'] + orig_advance
        new_x = right_edge - new_advance
        
        # For block 1 (title payer), it's center-aligned
        if idx == 1:
            page_width = 281.2125
            new_x = (page_width - new_advance) / 2
        
        # Build new TJ
        new_tj = _encode_text_to_tj(new_text, uni2cid, kern)
        
        # Build new Tm — preserve original Y format exactly, format X like patch_from_values
        def _fmt_coord(val, orig_str):
            """Format coordinate preserving original decimal style."""
            s = f'{val:.5f}'
            # Strip trailing zeros to match original format (e.g. 227.25 not 227.25000)
            if '.' in orig_str:
                orig_decimals = len(orig_str.split('.')[1])
                s = f'{val:.{orig_decimals}f}'
            elif '.' not in orig_str:
                s = str(int(round(val)))
            return s
        x_str = _fmt_coord(new_x, b['x_str'])
        new_tm = f'1 0 0 1 {x_str} {b["y_str"]} Tm'.encode()
        
        # Build complete new BT..ET block
        old_block = b['raw']
        
        # Replace Tm
        tm_in_block = re.search(rb'1 0 0 1 [\d.]+ [\d.]+ Tm', old_block)
        new_block = old_block[:tm_in_block.start()] + new_tm + old_block[tm_in_block.end():]
        
        # Replace TJ — use SPACE before [ (matching add_glyphs / patch_from_values format)
        # Original donor uses Tm\n[ but VTB generator produces Tm [ (space)
        tj_in_block = re.search(rb'\s*\[.*?\]\s*TJ', new_block, re.DOTALL)
        new_block = new_block[:tj_in_block.start()] + b' ' + new_tj + new_block[tj_in_block.end():]
        
        patches.append((b['start'], b['end'], new_block))

    # Apply patches (already sorted reverse by start position)
    new_dec = bytearray(dec)
    for start, end, replacement in patches:
        new_dec[start:end] = replacement

    # --- 6. Recompress ---
    # IMPORTANT: original VTB PDFs use level 6 (header 78 9C), not level 9 (78 DA)
    new_compressed = zlib.compress(bytes(new_dec), 6)

    # --- 7. Replace in PDF ---
    # Only replace /Length value and stream content, keeping exact header format
    length_m = re.search(rb'(/Length\s+)\d+', bytes(data)[cs_m.start():cs_m.end()])
    length_abs_start = cs_m.start() + length_m.start(0)
    length_abs_end = cs_m.start() + length_m.end(0)
    
    endstream_pos = bytes(data).find(b'\r\nendstream', cs_start)
    if endstream_pos < 0:
        endstream_pos = bytes(data).find(b'\nendstream', cs_start)
    
    # Replace /Length value
    new_length_str = length_m.group(1) + str(len(new_compressed)).encode()
    data[length_abs_start:length_abs_end] = new_length_str
    
    # Recalculate stream start after length change
    cs_m2 = re.search(rb'5 0 obj\s*<<.*?>>\s*stream\r?\n', bytes(data), re.DOTALL)
    cs_start2 = cs_m2.end()
    endstream_pos2 = bytes(data).find(b'\r\nendstream', cs_start2)
    if endstream_pos2 < 0:
        endstream_pos2 = bytes(data).find(b'\nendstream', cs_start2)
    
    # Replace stream content only
    data[cs_start2:endstream_pos2] = new_compressed

    # --- 8. Patch Document ID ---
    # CRITICAL rule (from CHECK_VERIFICATION_RULES.md):
    # - Change EXACTLY position 0 in the hex ID
    # - Result MUST be a decimal digit (0-9) — VTB verifier requires this
    if doc_id_changes > 0:
        id_m = re.search(rb'/ID\s*\[<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\]', bytes(data))
        if id_m:
            old_id = id_m.group(1).decode().upper()
            pos = 0
            old_ch = old_id[pos]
            base = int(old_ch, 16)
            # Find increments that result in a decimal digit (0-9)
            valid_incs = [i for i in range(1, 16) if (base + i) % 16 < 10]
            if valid_incs:
                inc = valid_incs[0]  # Use first valid increment (deterministic)
                new_ch = '0123456789ABCDEF'[(base + inc) % 16]
                new_id = new_ch + old_id[1:]
                data[id_m.start(1):id_m.end(1)] = new_id.encode()
                data[id_m.start(2):id_m.end(2)] = new_id.encode()

    # --- 9. Patch creation date ---
    if date and time:
        d_parts = date.split('.')
        t_parts = time.split(':')
        new_creation = f"D:{d_parts[2]}{d_parts[1]}{d_parts[0]}{t_parts[0]}{t_parts[1]}00+03'00'"
        cd_m = re.search(rb'/CreationDate\s*\(([^)]+)\)', bytes(data))
        if cd_m:
            data[cd_m.start(1):cd_m.end(1)] = new_creation.encode()

    # --- 10. Fix xref table ---
    data = bytearray(_fix_xref(bytes(data)))

    # --- Write output ---
    with open(output_path, 'wb') as f:
        f.write(data)
    
    return output_path


def _fix_xref(data: bytes) -> bytes:
    """Пересчитывает xref таблицу и startxref."""
    offsets = {}
    for m in re.finditer(rb'(\d+) 0 obj', data):
        offsets[int(m.group(1))] = m.start()

    # Find the real xref keyword (not inside "startxref")
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
    xref_lines.append(b"0000000000 65535 f\r\n")
    for i in range(1, max_obj + 1):
        off = offsets.get(i, 0)
        xref_lines.append(f"{off:010d} 00000 n\r\n".encode())

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


def main():
    parser = argparse.ArgumentParser(description='Прямой патчер PDF-донора')
    parser.add_argument('--donor', default=str(DEFAULT_DONOR), help='Путь к PDF-донору (по умолчанию: 15-03-26_00-00.pdf)')
    parser.add_argument('--payer', help='ФИО плательщика')
    parser.add_argument('--recipient', help='ФИО получателя')
    parser.add_argument('--date', help='Дата (ДД.ММ.ГГГГ)')
    parser.add_argument('--time', help='Время (ЧЧ:ММ)')
    parser.add_argument('--phone', help='Телефон получателя')
    parser.add_argument('--bank', help='Банк получателя')
    parser.add_argument('--amount', type=int, help='Сумма')
    parser.add_argument('--account', help='Номер счёта (4 цифры)')
    parser.add_argument('--op-id', help='ID операции (25+ символов)')
    parser.add_argument('--keep-op-id', action='store_true', help='Сохранить реальный op_id донора (не генерировать)')
    parser.add_argument('--doc-id-changes', type=int, default=1, help='Сколько символов менять в Document ID')
    parser.add_argument('-o', '--output', default='чек_patched.pdf', help='Выходной файл')
    args = parser.parse_args()
    
    donor_path = args.donor
    # Auto-select 17-03 donor when date is 17.03 and default donor is still 15-03
    if args.donor == str(DEFAULT_DONOR) and args.date and '17.03' in args.date:
        donor17 = BASE_DIR / '17-03-26_00-00.pdf'
        if donor17.exists():
            donor_path = str(donor17)
            print(f"  Авто-донор: 17-03-26_00-00.pdf (дата 17.03)")

    result = patch(
        donor_path=donor_path,
        output_path=args.output,
        payer=args.payer,
        recipient=args.recipient,
        date=args.date,
        time=args.time,
        phone=args.phone,
        bank=args.bank,
        amount=args.amount,
        account=args.account,
        op_id=args.op_id,
        keep_op_id=args.keep_op_id,
        doc_id_changes=args.doc_id_changes,
    )
    print(f"✓ Создан: {result}")


if __name__ == '__main__':
    main()
