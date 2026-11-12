#!/usr/bin/env python3
"""Сгенерировать check_21.pdf и check_11.pdf для теста.

- check_21: донор 21-04-25_23-38.pdf, обычный /ID (1 символ от донора)
- check_11: донор 11-02-26_09-10.pdf, /ID из 13-03-26_00-00 15.pdf (1 символ заменён)
- Оба: текущее время, ФИО с правильной капитализацией (Александр, не александр)
"""
import re
from datetime import datetime
from pathlib import Path

from vtb_patch_from_config import patch_from_values
from vtb_test_generator import update_creation_date

BASE = Path(__file__).parent
DONORS_DIR = BASE / "база_чеков" / "vtb" / "СБП"

# Доноры: check 21 = 21-04-25_23-38, check 11 = 11-02-26_09-10
DONOR_21 = DONORS_DIR / "21-04-25_23-38.pdf"
DONOR_11 = DONORS_DIR / "11-02-26_09-10.pdf"
ID_SOURCE = Path("/Users/aleksandrzerebatav/Downloads/13-03-26_00-00 15.pdf")

# ФИО с правильной капитализацией: заглавные А,П — отдельные глифы от строчных
PAYER = "Александр Александрович А."
RECIPIENT = "Анна Петровна П."
PHONE = "+7 (916) 555-12-34"
AMOUNT = 5700


def change_one_char_in_id(data: bytearray, use_id_from: bytes | None = None) -> None:
    """Изменить 1 символ в /ID. Если use_id_from — подставить тот ID (с 1 символом заменённым)."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    if use_id_from:
        # Берём hex из use_id_from, меняем последний символ
        hex_str = use_id_from.decode()
        c = hex_str[-1]
        chars = "0123456789ABCDEF"
        idx = chars.find(c.upper())
        new_c = chars[(idx + 1) % 16]
        new1 = hex_str[:-1] + new_c
    else:
        hex1 = id_m.group(1).decode()
        c = hex1[-1]
        chars = "0123456789ABCDEF"
        idx = chars.find(c.upper())
        new_c = chars[(idx + 1) % 16]
        new1 = hex1[:-1] + new_c
    data[id_m.start(1) : id_m.end(1)] = new1.encode()
    data[id_m.start(2) : id_m.end(2)] = new1.encode()


def main():
    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y, %H:%M")
    meta_date = f"D:{now.strftime('%Y%m%d%H%M%S')}+03'00'"

    # /ID из файла 15 для check_11
    id_from_15 = None
    if ID_SOURCE.exists():
        raw = ID_SOURCE.read_bytes()
        m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>', raw)
        if m:
            id_from_15 = m.group(1)
    else:
        print(f"[WARN] {ID_SOURCE} не найден, check_11 получит обычный /ID")

    def gen_one(donor_path: Path, out_name: str, use_external_id: bool = False) -> bool:
        if not donor_path.exists():
            print(f"[ERROR] Донор не найден: {donor_path}")
            return False
        data = bytearray(donor_path.read_bytes())
        try:
            out = patch_from_values(
                data,
                donor_path,
                date_str=date_str,
                payer=PAYER,
                recipient=RECIPIENT,
                phone=PHONE,
                amount=AMOUNT,
                keep_metadata=True,
            )
        except ValueError as e:
            print(f"[ERROR] {out_name}: {e}")
            return False
        out_arr = bytearray(out)
        update_creation_date(out_arr, meta_date)
        if use_external_id and id_from_15:
            change_one_char_in_id(out_arr, use_id_from=id_from_15)
        else:
            change_one_char_in_id(out_arr)
        out_path = BASE / out_name
        out_path.write_bytes(out_arr)
        print(f"✅ {out_name}")
        return True

    print("[INFO] Время:", date_str)
    print("[INFO] ФИО: получатель", PAYER, "| отправитель", RECIPIENT)
    print("[INFO] Сумма:", AMOUNT, "₽ | Телефон:", PHONE)
    print()
    gen_one(DONOR_21, "check_21.pdf", use_external_id=False)
    gen_one(DONOR_11, "check_11.pdf", use_external_id=True)
    print()
    print("check_21: /ID от донора (1 символ изменён)")
    print("check_11: /ID из 13-03-26_00-00 15.pdf (1 символ изменён)")


if __name__ == "__main__":
    main()
