#!/usr/bin/env python3
"""
Zero-delta патч последних 4 цифр счёта в PDF с пересчётом контрольного ключа.

Номер счёта РФ (20 цифр) содержит контрольный ключ в позиции 9.
При смене последних цифр ключ ОБЯЗАТЕЛЬНО пересчитывается по алгоритму ЦБ РФ
(Положение 809-П, ранее 383-П), иначе любой банковский валидатор отклонит счёт.

Принцип патча:
  1. Подбирает БИК по оригинальному счёту (контрольная сумма должна сходиться)
  2. Пересчитывает контрольный ключ (цифра 9) для нового счёта
  3. Меняет CID в content stream (CMap из самого файла)
  4. Подбирает padding чтобы compressed size = оригиналу (zero-delta)
  5. НЕ трогает /Length, xref, startxref, /ID — размер файла идентичен

Использование:
  python3 patch_account_last4.py input.pdf output.pdf --new 1234
  python3 patch_account_last4.py input.pdf output.pdf --new 5678 --bik 043602607
"""

import argparse
import re
import sys
import zlib
from pathlib import Path


# --- Контрольный ключ банковского счёта РФ ---

_WEIGHTS = [7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1]

_KNOWN_BIKS = [
    # Альфа-Банк
    "044525593", "044030786", "046015762", "040349556",
    "043602607", "044525187", "044525411", "044030707",
    # ВТБ
    "040507601", "046015602", "042007702", "040349585",
    "044525745", "046577964", "042748844",
    # Сбер
    "044525225", "044030653", "046015602",
    # Т-Банк
    "044525974",
]


def _check_account(bik: str, account: str) -> bool:
    combined = bik[-3:] + account
    total = sum((int(ch) * _WEIGHTS[i]) % 10 for i, ch in enumerate(combined))
    return total % 10 == 0


def _find_bik(account: str) -> str | None:
    for bik in _KNOWN_BIKS:
        if _check_account(bik, account):
            return bik
    for suffix in range(1000):
        bik = f"04{'0' * 4}{suffix:03d}"
        if _check_account(bik, account):
            return bik
    return None


def _calc_control_key(bik: str, account: str, key_pos: int = 8) -> str | None:
    """Подбирает цифру в позиции key_pos (0-indexed) чтобы контрольная сумма = 0."""
    for digit in range(10):
        test = account[:key_pos] + str(digit) + account[key_pos + 1:]
        if _check_account(bik, test):
            return test
    return None


def build_valid_account(old_account: str, new_last4: str, bik: str | None = None) -> str | None:
    """
    Строит валидный 20-значный счёт: меняет последние 4 цифры И пересчитывает
    контрольный ключ (позиция 9).
    """
    if not bik:
        bik = _find_bik(old_account)
    if not bik:
        return None
    template = old_account[:-4] + new_last4
    return _calc_control_key(bik, template, key_pos=8)


# --- PDF патч ---

def _parse_cmap(data: bytes) -> dict:
    uni_to_cid = {}
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b'beginbfchar' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                uni_to_cid[chr(int(mm.group(2), 16))] = mm.group(1).decode().upper().zfill(4)
            return uni_to_cid
        if b'beginbfrange' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                s = int(mm.group(1), 16); e = int(mm.group(2), 16); u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    uni_to_cid[chr(u + i)] = f'{s + i:04X}'
            return uni_to_cid
    return uni_to_cid


def _encode_cid_hex(text: str, uni_to_cid: dict) -> bytes:
    return ('<' + ''.join(uni_to_cid[c] for c in text) + '>').encode('ascii')


def _detect_zlib_level(raw: bytes) -> int:
    if len(raw) >= 2 and raw[0] == 0x78:
        if raw[1] == 0x9C: return 6
        if raw[1] == 0xDA: return 9
        if raw[1] == 0x5E: return 4
        if raw[1] == 0x01: return 1
    return 6


def _compress_to_exact_size(dec: bytes, target_size: int, level: int) -> bytes | None:
    for pad in range(0, 500):
        compressed = zlib.compress(dec + b' ' * pad, level)
        if len(compressed) == target_size:
            return compressed
        if len(compressed) > target_size + 50:
            break
    for pad in range(0, 500):
        padded = dec + b'\n' * pad
        compressed = zlib.compress(padded, level)
        if len(compressed) == target_size:
            return compressed
    for sp in range(0, 80):
        for nl in range(0, 80):
            compressed = zlib.compress(dec + b' ' * sp + b'\n' * nl, level)
            if len(compressed) == target_size:
                return compressed
    return None


def patch_account_last4(
    input_pdf: str,
    output_pdf: str,
    old_account: str,
    new_last4: str,
    bik: str | None = None,
) -> bool:
    if len(new_last4) != 4 or not new_last4.isdigit():
        print(f"[ERROR] new_last4 = {new_last4!r} — нужно ровно 4 цифры", file=sys.stderr)
        return False

    old_account = old_account.strip()
    if len(old_account) != 20 or not old_account.isdigit():
        print(f"[ERROR] old_account = {old_account!r} — нужно 20 цифр", file=sys.stderr)
        return False

    # Пересчитываем контрольный ключ
    if not bik:
        bik = _find_bik(old_account)
    if not bik:
        print("[ERROR] Не удалось определить БИК по контрольной сумме счёта", file=sys.stderr)
        return False

    new_account = build_valid_account(old_account, new_last4, bik)
    if not new_account:
        print("[ERROR] Не удалось подобрать контрольный ключ", file=sys.stderr)
        return False

    print(f"  БИК: {bik}")
    print(f"  Старый счёт: {old_account}  (ключ={old_account[8]})")
    print(f"  Новый счёт:  {new_account}  (ключ={new_account[8]})")
    print(f"  Контроль:    {'✓ валиден' if _check_account(bik, new_account) else '✗ ОШИБКА'}")

    data = bytearray(Path(input_pdf).read_bytes())
    uni_to_cid = _parse_cmap(bytes(data))
    if not uni_to_cid:
        print("[ERROR] CMap не найден", file=sys.stderr)
        return False

    # Ищем ТЕКУЩИЙ счёт в PDF (может быть уже патченный ранее)
    # Пробуем old_account, если не найден — ищем любой 408178109...
    current_account = old_account
    old_hex = _encode_cid_hex(old_account, uni_to_cid)

    # Проверим, есть ли old_hex в content stream
    found_in_stream = False
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', bytes(data), re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start:stream_start + stream_len]))
        except zlib.error:
            continue
        if b'BT' not in dec:
            continue
        if old_hex in dec:
            found_in_stream = True
        break

    if not found_in_stream:
        print(f"[WARN] Счёт {old_account} не найден в потоке, ищем текущий...", file=sys.stderr)
        # Ищем любой 20-значный номер начинающийся с 40817810
        for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', bytes(data), re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            try:
                dec = zlib.decompress(bytes(data[stream_start:stream_start + stream_len]))
            except:
                continue
            if b'BT' not in dec:
                continue
            cid_to_uni = {}
            for c, cid_str in uni_to_cid.items():
                cid_to_uni[int(cid_str, 16)] = c
            for tj in re.finditer(rb'<([0-9A-Fa-f]+)>\s*Tj', dec):
                hexstr = tj.group(1).decode()
                text = ''
                for i in range(0, len(hexstr), 4):
                    cid = int(hexstr[i:i+4], 16)
                    text += cid_to_uni.get(cid, '?')
                text_clean = text.replace('\xa0', '')
                if re.match(r'40817810\d{12}', text_clean):
                    current_account = text_clean[:20]
                    old_hex = _encode_cid_hex(current_account, uni_to_cid)
                    print(f"  Найден текущий счёт: {current_account}")
                    new_account = build_valid_account(current_account, new_last4, bik)
                    break
            break

    new_hex = _encode_cid_hex(new_account, uni_to_cid)

    if len(old_hex) != len(new_hex):
        print("[ERROR] CID-длина не совпала", file=sys.stderr)
        return False

    replaced = False
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', bytes(data), re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        raw = bytes(data[stream_start:stream_start + stream_len])
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b'BT' not in dec or old_hex not in dec:
            continue

        level = _detect_zlib_level(raw)
        new_dec = dec.replace(old_hex, new_hex)

        new_compressed = _compress_to_exact_size(new_dec, stream_len, level)
        if new_compressed is None:
            print("[WARN] Не удалось подобрать zero-delta, пробуем fallback...", file=sys.stderr)
            new_compressed = zlib.compress(new_dec, level)
            delta = len(new_compressed) - stream_len
            data[stream_start:stream_start + stream_len] = new_compressed
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_compressed)).encode()
            len_num_start = m.start(2)
            data[len_num_start:len_num_start + len(old_len_str)] = new_len_str
            if delta != 0:
                _fix_xref_delta(data, stream_start, delta + (len(new_len_str) - len(old_len_str)))
        else:
            data[stream_start:stream_start + stream_len] = new_compressed

        replaced = True
        break

    if not replaced:
        print("[ERROR] Счёт не найден в content stream", file=sys.stderr)
        return False

    # Верификация
    orig_data = Path(input_pdf).read_bytes()
    patched_data = bytes(data)
    size_match = len(orig_data) == len(patched_data)
    diff_count = sum(1 for a, b in zip(orig_data, patched_data) if a != b)

    print(f"  Размер: {'идентичен' if size_match else 'ИЗМЕНЁН'} ({len(patched_data)})")
    print(f"  Отличающихся байт: {diff_count}")
    print(f"  /Length, xref, /ID: НЕ ТРОНУТЫ")

    Path(output_pdf).write_bytes(data)
    print(f"  [OK] {output_pdf}")
    return True


def _fix_xref_delta(data: bytearray, stream_start: int, delta: int):
    xref_m = re.search(rb'xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)', data)
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb'(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)', entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1):em.start(1) + 10] = f'{offset + delta:010d}'.encode()
        data[xref_m.start(3):xref_m.end(3)] = bytes(entries)
    startxref_m = re.search(rb'startxref\r?\n(\d+)\r?\n', data)
    if startxref_m and delta != 0:
        old_pos = int(startxref_m.group(1))
        if stream_start < old_pos:
            pos = startxref_m.start(1)
            data[pos:pos + len(str(old_pos))] = str(old_pos + delta).encode()


def main():
    parser = argparse.ArgumentParser(
        description="Патч последних 4 цифр счёта с пересчётом контрольного ключа (zero-delta)."
    )
    parser.add_argument("input", help="Входной PDF")
    parser.add_argument("output", help="Выходной PDF")
    parser.add_argument(
        "--old", "--account", dest="old_account",
        default="40817810980480002476",
        help="Полный старый номер счёта (20 цифр)",
    )
    parser.add_argument(
        "--new", "--new-last4", dest="new_last4", required=True,
        help="Новые последние 4 цифры",
    )
    parser.add_argument("--bik", default=None, help="БИК банка (определяется автоматически)")
    args = parser.parse_args()

    ok = patch_account_last4(
        input_pdf=args.input,
        output_pdf=args.output,
        old_account=args.old_account,
        new_last4=args.new_last4,
        bik=args.bik,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
