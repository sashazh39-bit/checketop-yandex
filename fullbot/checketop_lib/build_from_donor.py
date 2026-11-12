#!/usr/bin/env python3
"""
Сборка PDF из донора с настройкой пользователя и корректной расстановкой Tm.

Настройка:
- Дата: 08.03.2026, 23:33
- Плательщик: Андрей Максимович Р.
- Получатель: Жамшут Мадеевич Х.
- Телефон: +7 (993) 214‑39‑57
- Банк: Сбербанк (донор уже с Сбербанк — не трогаем)
- Сумма: 5 000 ₽

Использование: python3 build_from_donor.py [donor.pdf] [output.pdf]
По умолчанию: donors/check (1) (1).pdf -> чеки 07.03/13-02-26_from_donor.pdf
"""
import re
import sys
import zlib
from pathlib import Path

# Конфиг: TJ и Tm для правой колонки (right_edge=257.08)
RIGHT_EDGE = 257.08
KERN_16 = "-16.66667"
KERN_21 = "-21.42857"
KERN_11 = "-11.11111"

# Дата: 08.03.2026, 23:33 (kern -16.66667)
# 0=0013, 1=0014, 2=0015, 3=0016, 4=0017, 5=0018, 6=0019, 7=001a, 8=001b, 9=001c
# . =0011, , =000f, space=0003, : =001d
NEW_DATE = (
    b'[(\x00\x13)-16.66667 (\x00\x1b)-16.66667 (\x00\x11)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 '
    b'(\x00\x11)-16.66667 (\x00\x15)-16.66667 (\x00\x13)-16.66667 (\x00\x15)-16.66667 (\x00\x19)-16.66667 '
    b'(\x00\x0f)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 (\x00\x16)-16.66667 (\x00\x1d)-16.66667 '
    b'(\x00\x16)-16.66667 (\x00\x16)]'
)
TM_DATE_Y = 72.37499  # или 119.25 — дата в шапке

# Плательщик: Андрей Максимович Р. (kern -16.66667)
# А=021c, н=0249, д=0240, р=024c, е=0241, й=0245 | М=0228, а=023c, к=0246, с=024d, и=0244, м=0248, о=024a, в=023e, и=0244, ч=0253 | Р=022c, .=0011
NEW_PAYER = (
    b'(\x02\x1c)-16.66667 (\x02\x49)-16.66667 (\x02\x40)-16.66667 (\x02\x4c)-16.66667 (\x02\x41)-16.66667 (\x02\x45)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02\\()-16.66667 (\x02\x3c)-16.66667 (\x02\x46)-16.66667 (\x02\x4d)-16.66667 (\x02\x44)-16.66667 (\x02\x48)-16.66667 (\x02\x4a)-16.66667 (\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x2c)-16.66667 (\x00\x11)'
)
TM_PAYER_Y = 227.25
TM_PAYER_X = 166  # right_edge - width("Андрей Максимович Р.")

# Получатель: Жамшут Мадеевич Х. (kern -16.66667 для Form)
NEW_RECIPIENT = (
    b'(\x02\x22)-16.66667 (\x02\x3c)-16.66667 (\x02\x48)-16.66667 (\x02\x54)-16.66667 (\x02\x4f)-16.66667 (\x02\x4e)-16.66667 '
    b'(\x00\x03)-16.66667 (\x02\\()-16.66667 (\x02\x3c)-16.66667 (\x02\x40)-16.66667 (\x02\x41)-16.66667 (\x02\x41)-16.66667 '
    b'(\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x31)-16.66667 (\x00\x11)'
)
TM_RECIPIENT_Y = 203.25
TM_RECIPIENT_X = 166  # Жамшут Мадеевич Х. ~91pt

# Телефон: +7 (993) 214‑39‑57
NEW_PHONE = (
    b'[(\x00\x0e)-16.66667 (\x00\x1a)-16.66667 (\x00\x03)-16.66667 (\x00\x0b)-16.66667 (\x00\x1c)-16.66667 '
    b'(\x00\x1c)-16.66667 (\x00\x16)-16.66667 (\x00\x0c)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 '
    b'(\x00\x14)-16.66667 (\x00\x17)-16.66667 (\x00\x10)-16.66667 (\x00\x16)-16.66667 (\x00\x1c)-16.66667 '
    b'(\x00\x10)-16.66667 (\x00\x18)-16.66667 (\x00\x1a)]'
)
TM_PHONE_Y = 179.25

# Сумма: 5 000 ₽
NEW_AMOUNT = b'[(\x00\x18)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
TM_AMOUNT_Y = 155.25
TM_AMOUNT_X = 216  # 5 000 ₽ короче чем 10 ₽ по ширине — подгон под right_edge

# Маппинг: (y, tolerance) -> (new_tj, new_tm_x или None=оставить донорский)
FIELD_MAP = [
    (TM_PAYER_Y, 0.5, NEW_PAYER, TM_PAYER_X),
    (TM_RECIPIENT_Y, 0.5, NEW_RECIPIENT, TM_RECIPIENT_X),
    (TM_PHONE_Y, 0.5, NEW_PHONE, None),  # телефон — оставляем Tm донора
    (TM_AMOUNT_Y, 0.5, NEW_AMOUNT, TM_AMOUNT_X),
]


def replace_by_position(dec: bytes) -> bytes:
    """Замена TJ по y-координате Tm. Ищем '1 0 0 1 X Y Tm' и следующий '[...] TJ'."""
    # Паттерн: Tm с координатами, затем TJ
    # 1 0 0 1 184.575 227.25 Tm\n[...] TJ
    pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\n)(\[[^\]]*\]\s*TJ)'
    new_dec = dec

    def repl(m):
        prefix = m.group(1)
        x = float(m.group(2))
        sp = m.group(3)
        y = float(m.group(4))
        suffix = m.group(5)
        tj = m.group(6)
        for target_y, tol, new_tj, new_x in FIELD_MAP:
            if abs(y - target_y) <= tol:
                if new_x is not None:
                    return prefix + f"{new_x:.2f}".encode() + sp + m.group(4) + suffix + new_tj
                return prefix + m.group(2) + sp + m.group(4) + suffix + new_tj
        return m.group(0)

    new_dec = re.sub(pat, repl, dec)
    return new_dec


def replace_date(dec: bytes) -> bytes:
    """Замена даты: ищем Tm с y~72 или ~119 (дата в шапке), затем TJ."""
    # Ищем Tm с y около 72 или 119, затем TJ (дата в правой колонке)
    pat = rb'(1\s+0\s+0\s+1\s+[\d.]+\s+)(72\.\d+|119\.\d+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
    def repl_date(m):
        y = float(m.group(2))
        if 71 < y < 74 or 118 < y < 121:  # дата
            return m.group(1) + m.group(2) + m.group(3) + NEW_DATE
        return m.group(0)
    return re.sub(pat, repl_date, dec)


def main():
    base = Path(__file__).parent
    donor_dir = base / "donors"
    default_donor = donor_dir / "check (1) (1).pdf"
    default_out = base / "чеки 07.03" / "13-02-26_from_donor.pdf"

    if len(sys.argv) >= 3:
        inp = Path(sys.argv[1])
        out = Path(sys.argv[2])
    elif len(sys.argv) == 2:
        inp = Path(sys.argv[1])
        out = default_out
    else:
        inp = default_donor
        out = default_out

    if not inp.exists():
        print(f"[ERROR] Донор не найден: {inp}")
        sys.exit(1)

    data = bytearray(inp.read_bytes())
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

        new_dec = dec
        new_dec = replace_by_position(new_dec)
        new_dec = replace_date(new_dec)

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            mods.append((stream_start, stream_len, len_num_start, new_raw))
            print(f"[OK] Stream {stream_len} -> {len(new_raw)} (замены по Tm)")

    if not mods:
        print("[WARN] Ни один stream не изменён. Пробуем альтернативный паттерн...")
        # Fallback: может донор использует другой формат Tm (без пробелов)
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
            # Паттерн с \r\n и разными пробелами
            pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
            new_dec = dec
            for target_y, tol, new_tj, new_x in FIELD_MAP:
                def repl_y(ma, ty=target_y, tt=tol, nt=new_tj, nx=new_x):
                    y = float(ma.group(4))
                    if abs(y - ty) <= tt:
                        if nx is not None:
                            return ma.group(1) + f"{nx:.2f}".encode() + ma.group(3) + ma.group(4) + ma.group(5) + nt
                        return ma.group(1) + ma.group(2) + ma.group(3) + ma.group(4) + ma.group(5) + nt
                    return ma.group(0)
                new_dec = re.sub(pat, repl_y, new_dec)
            if new_dec != dec:
                new_raw = zlib.compress(new_dec, 9)
                mods.append((stream_start, stream_len, len_num_start, new_raw))
                print(f"[OK] Stream {stream_len} (fallback)")
        if not mods:
            print("[ERROR] Не удалось применить замены. Проверьте формат донора.")
            sys.exit(1)

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

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[OK] Сохранено: {out}")

    try:
        from patch_id import patch_document_id
        if patch_document_id(out):
            print("[OK] Document ID заменён.")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
