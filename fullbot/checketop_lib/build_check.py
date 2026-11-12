#!/usr/bin/env python3
"""
Сборка CID-чека «Исходящий перевод СБП» из донора с настройкой и корректной Tm.

Настройка:
- Дата: 08.03.2026, 23:33
- Плательщик: Андрей Максимович Р.
- Получатель: Жамшут Мадеевич Х.
- Телефон: +7 (993) 214‑39‑57
- Банк: Сбербанк
- Сумма: 5 000 ₽
- Document ID: случайный
- Остальное (Счет, Статус, ID операции): из донора

Использование: python3 build_check.py [base.pdf] [output.pdf]
По умолчанию: чеки 07.03/13-02-26_20-29.pdf или donors/check (1) (1).pdf
"""
import re
import secrets
import sys
import zlib
from pathlib import Path

# CMap: 0=0013, 1=0014, 2=0015, 3=0016, 4=0017, 5=0018, 6=0019, 7=001a, 8=001b, 9=001c
# . =0011, , =000f, space=0003, : =001d, ( =000b, ) =000c, + =000e, ‑ =0010
# А=021c, Ж=0222, М=0228, Р=022c, С=022d, Х=0231
# а=023c, б=023d, в=023e, д=0240, е=0241, и=0244, к=0246, м=0248, н=0249, о=024a
# р=024c, с=024d, т=024e, у=024f, ч=0253, ш=0254

NEW_DATE = (
    b'[(\x00\x13)-16.66667 (\x00\x1b)-16.66667 (\x00\x11)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 '
    b'(\x00\x11)-16.66667 (\x00\x15)-16.66667 (\x00\x13)-16.66667 (\x00\x15)-16.66667 (\x00\x19)-16.66667 '
    b'(\x00\x0f)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 (\x00\x16)-16.66667 (\x00\x1d)-16.66667 '
    b'(\x00\x16)-16.66667 (\x00\x16)]'
)
NEW_PAYER = (
    b'[(\x02\x1c)-16.66667 (\x02\x49)-16.66667 (\x02\x40)-16.66667 (\x02\x4c)-16.66667 (\x02\x41)-16.66667 (\x02\x45)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02\\()-16.66667 (\x02\x3c)-16.66667 (\x02\x46)-16.66667 (\x02\x4d)-16.66667 (\x02\x44)-16.66667 (\x02\x48)-16.66667 (\x02\x4a)-16.66667 (\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x2c)-16.66667 (\x00\x11)]'
)
NEW_RECIPIENT = (
    b'[(\x02\x22)-16.66667 (\x02\x3c)-16.66667 (\x02\x48)-16.66667 (\x02\x54)-16.66667 (\x02\x4f)-16.66667 (\x02\x4e)-16.66667 '
    b'(\x00\x03)-16.66667 (\x02\\()-16.66667 (\x02\x3c)-16.66667 (\x02\x40)-16.66667 (\x02\x41)-16.66667 (\x02\x41)-16.66667 '
    b'(\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x31)-16.66667 (\x00\x11)]'
)
NEW_PHONE = (
    b'[(\x00\x0e)-16.66667 (\x00\x1a)-16.66667 (\x00\x03)-16.66667 (\x00\x0b)-16.66667 (\x00\x1c)-16.66667 '
    b'(\x00\x1c)-16.66667 (\x00\x16)-16.66667 (\x00\x0c)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 '
    b'(\x00\x14)-16.66667 (\x00\x17)-16.66667 (\x00\x10)-16.66667 (\x00\x16)-16.66667 (\x00\x1c)-16.66667 '
    b'(\x00\x10)-16.66667 (\x00\x18)-16.66667 (\x00\x1a)]'
)
NEW_AMOUNT = b'[(\x00\x18)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
NEW_BANK = (
    b'[(\x02\x2d)-16.66667 (\x02\x3d)-16.66667 (\x02\x41)-16.66667 (\x02\x4c)-16.66667 '
    b'(\x02\x3d)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x02\x46)-16.66667]'
)
OLD_KB_SOLID = (
    b'(\x02\x26)-16.66667 (\x02\x1d)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02\x2d)-16.66667 (\x02\x4a)-16.66667 (\x02\x47)-16.66667 (\x02\x44)-16.66667 (\x02\x40)-16.66667 '
    b'(\x02\x3c)-16.66667 (\x02\x4c)-16.66667 (\x02\x49)-16.66667 (\x02\x4a)-16.66667 (\x02\x4d)-16.66667 '
    b'(\x02\x4e)-16.66667 (\x02\x58)-16.66667'
)

# Минимальный x для правой колонки (левая — метки, не трогаем)
XMIN_RIGHT = 100

# x из оригинала СБП/07-03-26_00-00.pdf (просканировано)
X_BY_Y = {
    227.25: 149.81,   # Имя плательщика
    203.25: 139.12,   # Получатель
    179.25: 174.30,   # Телефон
    155.25: 227.81,   # Банк
    72.37: 231.53,    # Сумма
    72.37499: 231.53,
    39.82: 108.15,    # Дата в печати
}

# Позиции полей SBP (y, tol, new_tj, new_x). new_x из оригинала — не двигаем
FIELD_MAP = [
    (227.25, 0.5, NEW_PAYER, X_BY_Y[227.25]),
    (203.25, 0.5, NEW_RECIPIENT, X_BY_Y[203.25]),
    (179.25, 0.5, NEW_PHONE, X_BY_Y[179.25]),
    (155.25, 0.5, NEW_BANK, X_BY_Y[155.25]),
    (131.25, 0.5, None, None),       # ID операции — из донора
    (72.37499, 0.5, NEW_AMOUNT, X_BY_Y[72.37499]),
    (72.37, 0.5, NEW_AMOUNT, X_BY_Y[72.37]),
    (39.82, 0.5, NEW_DATE, X_BY_Y[39.82]),
]


def replace_by_position(dec: bytes) -> bytes:
    """Замена TJ по y. Паттерн допускает Tf между Tm и TJ."""
    pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
    new_dec = dec

    def repl(m):
        prefix, x, sp, y_b, suffix, between, tj = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7)
        if float(x) < XMIN_RIGHT:
            return m.group(0)
        y = float(y_b)
        for target_y, tol, new_tj, new_x in FIELD_MAP:
            if new_tj is None:
                continue
            if abs(y - target_y) <= tol:
                tj_out = new_tj if new_tj.endswith(b" TJ") else new_tj + b" TJ"
                if new_x is not None:
                    return prefix + f"{new_x:.2f}".encode() + sp + y_b + suffix + between + tj_out
                return prefix + x + sp + y_b + suffix + between + tj_out
        return m.group(0)

    new_dec = re.sub(pat, repl, dec)
    if new_dec == dec:
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
        def repl2(m):
            prefix, x, sp, y_b, suffix, tj = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
            if float(x) < XMIN_RIGHT:
                return m.group(0)
            y = float(y_b)
            for target_y, tol, new_tj, new_x in FIELD_MAP:
                if new_tj is None:
                    continue
                if abs(y - target_y) <= tol:
                    tj_out = new_tj if new_tj.endswith(b" TJ") else new_tj + b" TJ"
                    if new_x is not None:
                        return prefix + f"{new_x:.2f}".encode() + sp + y_b + suffix + tj_out
                    return prefix + x + sp + y_b + suffix + tj_out
            return m.group(0)
        new_dec = re.sub(pat2, repl2, dec)
    return new_dec


def replace_date(dec: bytes) -> bytes:
    """Замена даты только в нижней строке (y~39.82). НЕ трогаем y=72 (Сумма) и y=119 (ID)."""
    pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
    def repl(m):
        x, y = float(m.group(2)), float(m.group(4))
        if x < XMIN_RIGHT:
            return m.group(0)
        if 38 < y < 42:  # только нижняя дата (39.82)
            dt = NEW_DATE if NEW_DATE.endswith(b" TJ") else NEW_DATE + b" TJ"
            return m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5) + m.group(6) + dt
        return m.group(0)
    out = re.sub(pat, repl, dec)
    if out == dec:
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
        def repl2(m):
            x, y = float(m.group(2)), float(m.group(4))
            if x < XMIN_RIGHT:
                return m.group(0)
            if 38 < y < 42:
                date_tj = NEW_DATE if NEW_DATE.endswith(b" TJ") else NEW_DATE + b" TJ"
                return m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5) + date_tj
            return m.group(0)
        out = re.sub(pat2, repl2, dec)
    return out


def replace_bank(dec: bytes) -> bytes:
    if OLD_KB_SOLID in dec:
        return dec.replace(OLD_KB_SOLID, NEW_BANK)
    return dec


def patch_document_id(path: Path) -> bool:
    try:
        from patch_id import patch_document_id as do_patch
        return do_patch(path)
    except ImportError:
        data = bytearray(path.read_bytes())
        m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", data)
        if not m:
            return False
        old1, old2 = m.group(1), m.group(2)
        target_len = len(old1)
        new_hex = secrets.token_hex(target_len // 2).lower()[:target_len].ljust(target_len, "0")
        new_b = new_hex.encode("ascii")
        data = data.replace(b"<" + old1 + b">", b"<" + new_b + b">", 1)
        data = data.replace(b"<" + old2 + b">", b"<" + new_b + b">", 1)
        path.write_bytes(data)
        return True


def main():
    base = Path(__file__).parent
    target = base / "чеки 07.03" / "13-02-26_20-29.pdf"
    donor = base / "donors" / "check (1) (1).pdf"
    default_out = base / "чеки 07.03" / "13-02-26_built.pdf"

    if len(sys.argv) >= 3:
        inp = Path(sys.argv[1])
        out = Path(sys.argv[2])
    elif len(sys.argv) == 2:
        inp = Path(sys.argv[1])
        out = default_out
    else:
        inp = target if target.exists() else donor
        out = default_out

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
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
        # replace_date отключён: дата в SBP на y=39.82 в FIELD_MAP; старый replace_date перезаписывал Сумму (y=72)
        # new_dec = replace_date(new_dec)
        new_dec = replace_bank(new_dec)

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 9)
            mods.append((stream_start, stream_len, len_num_start, new_raw))
            print(f"[OK] Stream {stream_len} -> {len(new_raw)}")

    if not mods:
        print("[WARN] Пробуем паттерн без Tf между Tm и TJ...")
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
            pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
            new_dec = dec
            for target_y, tol, new_tj, new_x in FIELD_MAP:
                if new_tj is None:
                    continue
                def repl(ma, ty=target_y, tt=tol, nt=new_tj, nx=new_x):
                    if float(ma.group(2)) < XMIN_RIGHT:
                        return ma.group(0)
                    y = float(ma.group(4))
                    if abs(y - ty) <= tt:
                        tj_out = nt if nt.endswith(b" TJ") else nt + b" TJ"
                        if nx is not None:
                            return ma.group(1) + f"{nx:.2f}".encode() + ma.group(3) + ma.group(4) + ma.group(5) + tj_out
                        return ma.group(1) + ma.group(2) + ma.group(3) + ma.group(4) + ma.group(5) + tj_out
                    return ma.group(0)
                new_dec = re.sub(pat, repl, new_dec)
            # replace_date отключён — дата в FIELD_MAP (y=39.82)
            new_dec = replace_bank(new_dec)
            if new_dec != dec:
                new_raw = zlib.compress(new_dec, 9)
                mods.append((stream_start, stream_len, len_num_start, new_raw))
                print(f"[OK] Stream {stream_len} (fallback)")
        if not mods:
            print("[ERROR] Не удалось применить замены.")
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

    if patch_document_id(out):
        print("[OK] Document ID заменён.")


if __name__ == "__main__":
    main()
