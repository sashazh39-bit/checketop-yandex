#!/usr/bin/env python3
"""Генератор тестовых PDF ВТБ: полная замена + копии с изменением по одной строке.

Структура координат: см. vtb_structure_scan.py — полное сканирование Tm/TJ.

Создаёт:
1. Полная замена (сумма 10 000, дата 10.03.2025, плательщик Евгений Александрович Е., получатель Анна Петрова С.)
2. Только сумма (1 000 → 10 000)
3. Только дата (09.03.2026 → 10.03.2025)

Каждый PDF получает уникальный /ID. Сохраняет в папку «Тест ВТБ».
В названиях: 09-03-26_03-47_1.pdf, _2.pdf и т.д.

Использование: python3 vtb_test_generator.py input.pdf
             python3 vtb_test_generator.py input.pdf --wall-variants
             python3 vtb_test_generator.py input.pdf --change-metadata  # меняет /ID и CreationDate
"""
import hashlib
import re
import zlib
from datetime import datetime
from pathlib import Path

# CID для даты "DD.MM.YYYY, HH:MM" (font с цифрами)
_CID_DATE = {
    "0": "0013", "1": "0014", "2": "0015", "3": "0016", "4": "0017",
    "5": "0018", "6": "0019", "7": "001A", "8": "001B", "9": "001C",
    ".": "0011", ",": "000F", " ": "0003", ":": "001D",
}


def build_date_tj(dt: datetime) -> bytes:
    """Собрать TJ для даты в формате DD.MM.YYYY, HH:MM."""
    s = dt.strftime("%d.%m.%Y, %H:%M")
    cids = [_CID_DATE.get(c, "0013") for c in s]
    return b"[" + build_tj(cids, kern="-16.66667") + b"]"


def update_id(data: bytearray) -> None:
    """Обновить /ID в trailer для уникальности."""
    id_m = re.search(rb'/ID\s*\[\s*(<[0-9a-fA-F]+>\s*<[0-9a-fA-F]+>)\s*\]', bytes(data))
    if id_m:
        h = hashlib.md5(bytes(data)).hexdigest().upper()
        new_id = f"<{h}> <{h}>".encode()
        data[id_m.start(1) : id_m.end(1)] = new_id.ljust(id_m.end(1) - id_m.start(1))


def update_creation_date(data: bytearray, new_date: str) -> bool:
    """Обновить /CreationDate в Info."""
    m = re.search(rb'/CreationDate\s*\(([^)]+)\)', data)
    if m:
        new_val = new_date.encode()
        data[m.start(1) : m.end(1)] = new_val.ljust(len(m.group(1)))
        return True
    return False


def build_tj(cids: list[str], kern: str = "-16.66667") -> bytes:
    """Собрать TJ из CID hex-кодов. Формат как у донора: керн между глифами, НЕ после последнего."""
    kern_b = kern.encode()
    parts = []

    def _pdf_literal_byte(b: int) -> bytes:
        if b in (0x28, 0x29, 0x5C):  # (, ), backslash
            return b"\\" + bytes([b])
        return bytes([b])

    for i, cid_hex in enumerate(cids):
        cid = int(cid_hex, 16)
        h, l = cid >> 8, cid & 0xFF
        s = b"(" + _pdf_literal_byte(h) + _pdf_literal_byte(l) + b")"
        if i < len(cids) - 1:
            parts.append(s + kern_b + b" ")
        else:
            parts.append(s)
    return b"".join(parts)


def count_tj_glyphs(tj_bytes: bytes) -> int:
    """Подсчёт глифов в TJ: количество кернов + 1 (последний глиф без керна)."""
    n = 0
    for kern in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        n += tj_bytes.count(kern)
    return n + 1


# Логика: WALL = max(x1), pts извлекается для каждого поля из исходного PDF.
try:
    from vtb_sber_reference import get_vtb_per_field_params
except ImportError:
    get_vtb_per_field_params = lambda _: {
        "wall": 257.1,
        "pts_date": 4.55, "pts_payer": 4.66, "pts_recipient": 4.66,
        "pts_amount": 6.447,
    }


def tm_x_touch_wall(wall: float, n_glyphs: int, pts: float) -> float:
    """Tm_x: последняя буква касается стенки."""
    return wall - n_glyphs * pts


# Эталон: дата и телефон идеальны. Все последние символы на одной вертикали wall.
PTS_CALIBRATION = {
    "date": 4.12,
    "payer": 4.08,
    "recipient": 4.28,   # 4.20 уезжало вправо (столбец) и по центру
    "phone": 4.34,
    "bank": 4.60,
    "amount": 5.92,      # 6.05 слишком вправо (50 000 ₽)
    "opid": 5.25,
    "account": 5.25,
}


def patch_stream(
    data: bytes | bytearray,
    *,
    amount: bool = False,
    date: bool = False,
    payer: bool = False,
    recipient: bool = False,
    creation_date: str | None = None,
    pdf_path: str | Path | None = None,
    keep_metadata: bool = True,
) -> bytes:
    """Патчит content stream. Возвращает изменённые байты PDF.
    pdf_path: путь к PDF для извлечения WALL и pts.
    keep_metadata: True = не менять /ID и CreationDate (для прохождения проверки бота).
    """
    data = bytearray(data)
    params = (
        get_vtb_per_field_params(pdf_path)
        if pdf_path
        else {"wall": 257.1, "pts_date": 4.55, "pts_payer": 4.66, "pts_recipient": 4.66, "pts_amount": 6.447}
    )
    wall = params["wall"]
    pts_date = PTS_CALIBRATION["date"]
    pts_payer = PTS_CALIBRATION["payer"]
    pts_recipient = PTS_CALIBRATION["recipient"]
    pts_amount = PTS_CALIBRATION["amount"]
    # 1 000 ₽ → 10 000 ₽ (kern -11.11111)
    OLD_AMOUNT = b'[(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    NEW_AMOUNT = b'[(\x00\x14)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    OLD_TM_AMOUNT = b"1 0 0 1 211.95001 72.37499 Tm"
    new_x_amt = tm_x_touch_wall(wall, 8, pts_amount)
    NEW_TM_AMOUNT = f"1 0 0 1 {new_x_amt:.5f} 72.37499 Tm".encode()

    # 09.03.2026, 04:47 → текущие дата и время
    OLD_DATE = (
        b'[(\x00\x13)-16.66667 (\x00\x1c)-16.66667 (\x00\x11)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 (\x00\x11)-16.66667 '
        b'(\x00\x15)-16.66667 (\x00\x13)-16.66667 (\x00\x15)-16.66667 (\x00\x19)-16.66667 (\x00\x0f)-16.66667 (\x00\x03)-16.66667 '
        b'(\x00\x13)-16.66667 (\x00\x17)-16.66667 (\x00\x1d)-16.66667 (\x00\x17)-16.66667 (\x00\x1a)]'
    )
    now = datetime.now()
    NEW_DATE = build_date_tj(now)

    # Александр Евгеньевич Ж. → Евгений Александрович Е. (из patch_09_03)
    OLD_PAYER = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 '
        b'(\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 '
        b'(\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
    NEW_PAYER = build_tj(
        ["0221", "023E", "023F", "0241", "0249", "0244", "0245", "0003",
         "021C", "0247", "0241", "0246", "024D", "023C", "0249", "0240", "024C", "024A", "023E", "0244", "0253", "0003",
         "0221", "0011"]
    )

    # Ефим Антонович Б. → Анна Петрова С. (оба вхождения: -16.66667 и -21.42857)
    OLD_RECIPIENT_16 = (
        b'[(\x02!)-16.66667 (\x02P)-16.66667 (\x02D)-16.66667 (\x02H)-16.66667 (\x00\x03)-16.66667 (\x02\x1c)-16.66667 '
        b'(\x02I)-16.66667 (\x02N)-16.66667 (\x02J)-16.66667 (\x02I)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 '
        b'(\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)]'
    )
    OLD_RECIPIENT_21 = (
        b'[(\x02!)-21.42857 (\x02P)-21.42857 (\x02D)-21.42857 (\x02H)-21.42857 (\x00\x03)-21.42857 (\x02\x1c)-21.42857 '
        b'(\x02I)-21.42857 (\x02N)-21.42857 (\x02J)-21.42857 (\x02I)-21.42857 (\x02J)-21.42857 (\x02>)-21.42857 '
        b'(\x02D)-21.42857 (\x02S)-21.42857 (\x00\x03)-21.42857 (\x02\x1d)-21.42857 (\x00\x11)]'
    )
    # Анна Петрова С.
    NEW_RECIPIENT_16 = b"[" + build_tj(
        ["021C", "0249", "0249", "023C", "0003", "022B", "0241", "024E", "024C", "024A", "023E", "023C", "0003", "022D", "0011"],
        kern="-16.66667"
    ) + b"]"
    NEW_RECIPIENT_21 = b"[" + build_tj(
        ["021C", "0249", "0249", "023C", "0003", "022B", "0241", "024E", "024C", "024A", "023E", "023C", "0003", "022D", "0011"],
        kern="-21.42857"
    ) + b"]"

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

        if amount:
            if OLD_AMOUNT in new_dec:
                new_dec = new_dec.replace(OLD_AMOUNT, NEW_AMOUNT)
            if NEW_AMOUNT in new_dec and OLD_TM_AMOUNT in new_dec:
                new_dec = new_dec.replace(OLD_TM_AMOUNT, NEW_TM_AMOUNT)

        if date:
            if OLD_DATE in new_dec:
                new_dec = new_dec.replace(OLD_DATE, NEW_DATE)
            new_x_date = tm_x_touch_wall(wall, 17, pts_date)
            tm_date_m = re.search(rb"(1 0 0 1 )([\d.]+)( 275\.25 Tm)", new_dec)
            if tm_date_m and float(tm_date_m.group(2)) > 100:
                new_dec = new_dec.replace(tm_date_m.group(0), tm_date_m.group(1) + f"{new_x_date:.5f}".encode() + tm_date_m.group(3))

        if payer and OLD_PAYER in new_dec:
            new_dec = new_dec.replace(OLD_PAYER, NEW_PAYER)
            new_x = tm_x_touch_wall(wall, 24, pts_payer)
            for old_tm in (b"1 0 0 1 149.8125 227.25 Tm", b"1 0 0 1 133.19 227.25 Tm", b"1 0 0 1 148.40 227.25 Tm", b"1 0 0 1 147.21 227.25 Tm"):
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, f"1 0 0 1 {new_x:.5f} 227.25 Tm".encode())
                    break

        if recipient:
            if OLD_RECIPIENT_16 in new_dec:
                new_dec = new_dec.replace(OLD_RECIPIENT_16, NEW_RECIPIENT_16)
                new_x = tm_x_touch_wall(wall, 15, pts_recipient)
                tm_m = re.search(rb"(1 0 0 1 )([\d.]+)( 203\.25 Tm)", new_dec)
                if tm_m and float(tm_m.group(2)) > 100:
                    new_dec = new_dec.replace(tm_m.group(0), tm_m.group(1) + f"{new_x:.5f}".encode() + tm_m.group(3))
            if OLD_RECIPIENT_21 in new_dec:
                new_dec = new_dec.replace(OLD_RECIPIENT_21, NEW_RECIPIENT_21)
                pts_h = pts_recipient * (10.5 / 9)
                shift = (17 - 15) * pts_h / 2  # сдвиг вправо при укорочении
                tm_m = re.search(rb"(1 0 0 1 )([\d.]+)( 327\.11249 Tm)", new_dec)
                if tm_m and 90 < float(tm_m.group(2)) < 120:
                    old_x = float(tm_m.group(2))
                    new_x = old_x + shift
                    new_dec = new_dec.replace(tm_m.group(0), tm_m.group(1) + f"{new_x:.5f}".encode() + tm_m.group(3))

        # Дата 17→17 глифов — Tm НЕ МЕНЯТЬ (сохраняем 179.74)
        # if date: ... — убрано, т.к. число глифов одинаково

        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)

            data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str.ljust(len(old_len_str))[: len(old_len_str)]

            xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
            if xref_m:
                entries = bytearray(xref_m.group(3))
                for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                    offset = int(em.group(1))
                    if offset > stream_start:
                        entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

            startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
            if startxref_m and delta != 0:
                pos = startxref_m.start(1)
                old_pos = int(startxref_m.group(1))
                data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

            break

    if not keep_metadata:
        if creation_date:
            update_creation_date(data, creation_date)
        update_id(data)
    return bytes(data)


def main():
    import sys
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf")
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    # Папка 10.03 для полной замены с обновлённым ID
    out_base = Path(__file__).parent
    out_dir = out_base / "10.03"
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = inp.stem
    today = datetime.now().strftime("%Y%m%d")
    creation_date = f"D:{today}120000+03'00'"

    # Полная замена: все поля правого столбца кроме ID операции, обновить /ID
    keep_meta = False
    opts_full = {
        "amount": True, "date": True, "payer": True, "recipient": True,
        "creation_date": creation_date,
    }
    data = inp.read_bytes()
    data = patch_stream(data, pdf_path=inp, keep_metadata=keep_meta, **opts_full)
    out_path = out_dir / f"{base_name}_full.pdf"
    out_path.write_bytes(data)
    print(f"[OK] {out_path} — все поля (кроме ID), /ID обновлён")

    out_dir = out_base / "Тест ВТБ"
    out_dir.mkdir(parents=True, exist_ok=True)
    variants = [
        (1, "Полная замена", {"amount": True, "date": True, "payer": True, "recipient": True, "creation_date": creation_date}),
        (2, "Только сумма", {"amount": True}),
        (3, "Только дата", {"date": True, "creation_date": creation_date}),
        (4, "Только плательщик", {"payer": True}),
        (5, "Только получатель", {"recipient": True}),
    ]

    # Варианты стенки для выравнивания правого столбца (см. --wall-variants)
    wall_variants = [
        ("sber", "Сбербанк"),
        ("phone", "Телефон"),
        ("done", "Выполнено"),
        ("star", "*9426"),
        ("max", "Макс из всех"),
    ]

    do_wall_variants = "--wall-variants" in sys.argv or "-w" in sys.argv
    keep_meta = "--change-metadata" in sys.argv or "-m" in sys.argv

    for num, desc, opts in variants:
        data = inp.read_bytes()
        data = patch_stream(data, pdf_path=inp, keep_metadata=keep_meta, **opts)
        out_path = out_dir / f"{base_name}_{num}.pdf"
        out_path.write_bytes(data)
        print(f"[OK] {out_path.name} — {desc}")

    if do_wall_variants:
        opts_full = {
            "amount": True, "date": True, "payer": True, "recipient": True,
            "creation_date": creation_date,
        }
        print("\n--- Варианты выравнивания (стенка) ---")
        for src, name in wall_variants:
            data = inp.read_bytes()
            data = patch_stream(data, pdf_path=inp, keep_metadata=keep_meta, **opts_full)
            out_path = out_dir / f"{base_name}_wall_{src}.pdf"
            out_path.write_bytes(data)
            print(f"[OK] {out_path.name} — стенка: {name}")

    print(f"\n[OK] Сохранено в: {out_dir}")


if __name__ == "__main__":
    main()
