#!/usr/bin/env python3
"""Генерация чека ВТБ из конфига vtb_config.json.

Меняй значения в vtb_config.json — выравнивание сохранится.

СИСТЕМА КООРДИНАТ (зафиксирована):
- Правый столбец: последняя буква на wall. tm_x = wall - real_text_width.
- real_text_width — из /W CIDFontType2 + кернинга TJ.
- Шапка (ФИО под «Исходящий перевод СБП»): центрирование по center_heading.
- Сумма: font-size 13.5 → new_x = wall - new_units * (13.5/1000).
- Fallback: tm_x_touch_wall(wall, n_glyphs, pts).

Использование:
  python3 vtb_patch_from_config.py [input.pdf]
  python3 vtb_patch_from_config.py --config vtb_config.json input.pdf
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from vtb_test_generator import build_tj, build_date_tj, tm_x_touch_wall
from vtb_sber_reference import get_vtb_per_field_params, get_field_align_raw
from vtb_cmap import text_to_cids, format_amount, operation_id_to_cids, _FALLBACK
from vtb_sbp_layout import get_layout_values


def _parse_cid_widths(pdf_bytes: bytes) -> dict[int, int]:
    """Парсит /W массива CIDFontType2: CID -> width."""
    m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", pdf_bytes, re.DOTALL)
    if not m:
        return {}
    tokens = re.findall(rb"\[|\]|\d+", m.group(1))
    widths: dict[int, int] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in (b"[", b"]"):
            i += 1
            continue
        start = int(tok)
        if i + 1 < len(tokens) and tokens[i + 1] == b"[":
            i += 2
            cid = start
            while i < len(tokens) and tokens[i] != b"]":
                widths[cid] = int(tokens[i])
                cid += 1
                i += 1
            i += 1
            continue
        i += 1
    return widths


def _tj_advance_units(tj_content: bytes, cid_widths: dict[int, int]) -> float:
    """Ширина TJ в units: sum(widths[cid]) - sum(kern)."""
    total = 0.0

    def _unescape_pdf_literal(raw: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(raw):
            b = raw[i]
            if b == 0x5C and i + 1 < len(raw):  # backslash
                out.append(raw[i + 1])
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    for string_part, kern_part in re.findall(rb"\((.*?)\)|(-?\d+(?:\.\d+)?)", tj_content):
        if string_part:
            vals = list(_unescape_pdf_literal(string_part))
            if len(vals) % 2 != 0:
                continue
            for i in range(0, len(vals), 2):
                cid = (vals[i] << 8) + vals[i + 1]
                total += cid_widths.get(cid, 0)
        elif kern_part:
            total -= float(kern_part)
    return total


def _fallback_pts(tj: bytes) -> float:
    """Fallback pts по керну: один параметр на тип кернинга."""
    if b"-11.11111" in tj:
        return 6.75
    if b"-21.42857" in tj:
        return 4.5   # центрированный получатель: было 4.2, съезжал вправо
    return 4.5       # ФИО и др.: было 4.6, съезжали влево


def build_amount_tj(amount: int) -> bytes:
    """Сумма → TJ (kern -11.11111). 10000 → '10 000 ₽'."""
    s = format_amount(amount)
    cids = text_to_cids(s)
    if not cids:
        raise ValueError(f"Не удалось закодировать сумму: {s}")
    return b"[" + build_tj(cids, kern="-11.11111") + b"]"


def patch_amount_only(data: bytearray, pdf_path: Path, amount: int) -> bytes:
    """Замена только суммы. Остальное не трогает, /ID не меняет."""
    params = get_vtb_per_field_params(pdf_path)
    layout = get_layout_values()
    raw_y = layout.get("y", {}).get("amount")
    y_amount = raw_y if isinstance(raw_y, (int, float)) else (raw_y[0] if raw_y else 72.375)
    y_tol = max(layout.get("y_tolerance", 0.15), 1.0)
    wall = params.get("wall") or layout.get("wall", 257.08)
    cid_widths = _parse_cid_widths(pdf_path.read_bytes())
    new_amount_tj = build_amount_tj(amount)
    new_content = new_amount_tj[1:-1]

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = __import__("zlib").decompress(bytes(data[stream_start : stream_start + stream_len]))
        except Exception:
            continue
        if b"BT" not in dec:
            continue

        amt_pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)(\s*\r?\n[^\[]*)?\[([^\]]+)\]\s*TJ"
        for amt_m in re.finditer(amt_pat, dec):
            if abs(float(amt_m.group(4)) - y_amount) > y_tol:
                continue
            tm_x = float(amt_m.group(2))
            if tm_x < 100:
                continue
            tj_content = amt_m.group(6)
            if b"-11.11111" not in tj_content:
                continue

            new_units = _tj_advance_units(new_content, cid_widths)
            if new_units > 0:
                new_x_amt = wall - new_units * (13.5 / 1000.0)
            else:
                n_amt = 1 + tj_content.count(b"-11.11111")
                pts_amt = (wall - float(amt_m.group(2))) / n_amt if n_amt > 0 else 6.75
                if pts_amt <= 0 or pts_amt > 15:
                    pts_amt = 6.75
                new_x_amt = tm_x_touch_wall(wall, n_amt, pts_amt)

            between = amt_m.group(5) or b"\n"
            repl = amt_m.group(1) + f"{new_x_amt:.5f}".encode() + amt_m.group(3) + between + b"[" + new_content + b"] TJ"
            new_dec = dec.replace(amt_m.group(0), repl)
            new_raw = __import__("zlib").compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            num_end = len_num_start + len(old_len_str)
            data = data[:len_num_start] + new_len_str + data[num_end:]
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)
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
            return bytes(data)
    raise ValueError("Блок суммы (kern -11.11111) не найден")


def _parse_donor_tounicode(pdf_bytes: bytes) -> dict[int, str] | None:
    """ToUnicode CMap из PDF: Unicode codepoint → CID hex."""
    try:
        from sbp_full_toolkit import parse_tounicode
        return parse_tounicode(pdf_bytes)
    except Exception:
        return None


def _text_to_cids_donor(text: str, uni_to_cid: dict[int, str]) -> list[str] | None:
    """Текст → CID через ToUnicode донора (сохраняет регистр: И≠и, О≠о)."""
    result = []
    for c in text:
        c = _FALLBACK.get(c, c)
        ucp = ord(c)
        cid = uni_to_cid.get(ucp)
        if cid is None:
            fallback = text_to_cids(c)
            cid = fallback[0] if fallback else None
        if cid is None:
            return None
        result.append(cid)
    return result


def build_text_tj(text: str, kern: str = "-16.66667", wrap: bool = True, uni_to_cid: dict[int, str] | None = None) -> bytes:
    """Текст (ФИО) → TJ. wrap=True для recipient. uni_to_cid — ToUnicode донора (для И/О в верх. регистре)."""
    if uni_to_cid:
        cids = _text_to_cids_donor(text, uni_to_cid)
    else:
        cids = text_to_cids(text)
    if not cids:
        raise ValueError(f"Символ не найден в CMap: {text}")
    tj = build_tj(cids, kern=kern)
    return b"[" + tj + b"]" if wrap else tj


def patch_from_values(
    data: bytearray,
    pdf_path: Path,
    *,
    date_str: str | datetime | None = None,
    payer: str | None = None,
    recipient: str | None = None,
    phone: str | None = None,
    bank: str | None = None,
    amount: int | None = None,
    operation_id: str | None = None,
    account: str | None = None,
    account_last4: str | None = None,
    message: str | None = None,
    keep_metadata: bool = False,
    keep_date: bool = False,
    override_uni_to_cid: dict[int, str] | None = None,
    original_cid_widths: dict[int, int] | None = None,
) -> bytes:
    """Патч: wall и pts из донора; pts из блока при замене по Y.

    original_cid_widths — /W до замены глифов (для корректного расчёта scale по старому ФИО).
    Если не передан, используется текущий /W (возможна неточность при hybrid-safe замене).
    """
    pdf_bytes = pdf_path.read_bytes()
    params = get_vtb_per_field_params(pdf_path)
    layout = get_layout_values()
    raw = get_field_align_raw(pdf_path)
    cid_widths = _parse_cid_widths(pdf_bytes)
    # Для вычисления old_units (scale по старому ФИО) используем ИСХОДНЫЕ ширины,
    # чтобы замена глифов не сдвигала scale.
    old_cid_widths = original_cid_widths if original_cid_widths is not None else cid_widths
    uni_to_cid = override_uni_to_cid or _parse_donor_tounicode(pdf_bytes)
    wall = params.get("wall") or layout["wall"]
    center_heading = params.get("center_heading") or layout["center_heading"]
    ly = layout["y"]
    y_tol = layout["y_tolerance"]

    def _y_list(field: str) -> tuple[list[float], float]:
        """Y для сопоставления: из донора если есть, иначе layout. tol: 0.5 для донора, 2.0 для layout."""
        raw_y = raw.get("y", {}).get(field)
        v = ly.get(field)
        layout_ys = list(v) if isinstance(v, (list, tuple)) else [v]
        if raw_y is not None:
            return [raw_y], 0.5
        return layout_ys, 2.0

    # Парсим дату (keep_date=True — не трогать дату, минимум изменений)
    if keep_date:
        new_date_tj = None
    else:
        if date_str == "now" or date_str is None:
            dt = datetime.now()
        elif isinstance(date_str, datetime):
            dt = date_str
        else:
            try:
                dt = datetime.strptime(date_str.strip(), "%d.%m.%Y, %H:%M")
            except ValueError:
                dt = datetime.now()
        new_date_tj = build_date_tj(dt)
    def _build(text: str, wrap: bool = True, kern: str = "-16.66667") -> bytes:
        return build_text_tj(text, kern=kern, wrap=wrap, uni_to_cid=uni_to_cid)
    new_payer_tj = _build(payer, wrap=False) if payer else None
    new_recipient_tj = _build(recipient) if recipient else None
    new_recipient_21 = _build(recipient, kern="-21.42857") if recipient else None
    new_phone_tj = _build(phone) if phone else None
    new_bank_tj = _build(bank) if bank else None
    if account_last4 is not None and account is None:
        account = account_last4  # только цифры; * берём из оригинального шаблона
    # Счёт: сохраняем оригинальный токен * из шаблона, заменяем только цифры.
    # CID 0x000D содержит \r (carriage return) — в literal string () он ломается.
    # Поэтому берём оригинальный <000D>-... префикс из шаблона как есть.
    new_account_tj = None
    if account:
        _digits = account.lstrip("*")  # берём только цифры
        _digit_cids = text_to_cids(_digits)
        if _digit_cids:
            _digit_tj = build_tj(_digit_cids)
            # Ищем оригинальный asterisk-токен в контент-стриме шаблона
            _orig_star_tok: bytes | None = None
            try:
                import zlib as _z
                _raw_pdf = pdf_path.read_bytes()
                for _om in re.finditer(rb'\d+ 0 obj', _raw_pdf):
                    _chunk = _raw_pdf[_om.end():_om.end()+3000]
                    _sm = re.search(rb'stream\r?\n', _chunk)
                    if not _sm: continue
                    _lm = re.search(rb'/Length\s+(\d+)', _chunk[:_sm.start()])
                    if not _lm: continue
                    _sl = int(_lm.group(1))
                    _ss = _om.end() + _sm.end()
                    try:
                        _dec = _z.decompress(_raw_pdf[_ss:_ss+_sl])
                    except Exception:
                        continue
                    # Ищем TJ с <000D> (hex) или (\x00\x0d) (literal) на Y≈251
                    _acc_m = re.search(
                        rb'[\d.]+\s+251\.25\s+Tm\s*\[(<000[Dd]>|[^]]*?\x00[\x0d\r])[^]]*\]',
                        _dec
                    )
                    if _acc_m:
                        # Извлечь первый токен (до первого kern)
                        _tj_body = re.search(rb'\[(.+?)\]\s*TJ', _dec[_acc_m.start():_acc_m.start()+300])
                        if _tj_body:
                            _first = re.match(rb'(<[0-9A-Fa-f]+>|\([^)]*\))(-[\d.]+\s+)?', _tj_body.group(1))
                            if _first:
                                _orig_star_tok = _first.group(1)  # e.g. b'<000D>'
                                break
            except Exception:
                pass
            if _orig_star_tok:
                # Собираем TJ: <оригинальная звёздочка>-kern <цифры>
                _kern = b"-16.66667 "
                new_account_tj = b"[" + _orig_star_tok + _kern + _digit_tj + b"]"
            else:
                # Fallback: собираем весь TJ включая *
                new_account_tj = _build("*" + _digits)
    new_message_tj = _build(message, wrap=True) if message else None
    new_amount_tj = build_amount_tj(amount) if amount else None
    new_opid_tj = None
    if operation_id and operation_id_to_cids(operation_id.replace(" ", "").replace("\n", "")):
        cids = operation_id_to_cids(operation_id.replace(" ", "").replace("\n", ""))
        new_opid_tj = b"[" + build_tj(cids, kern="-16.66667") + b"]"

    # OLD patterns (из 09-03-26_03-47)
    OLD_DATE = (
        b'[(\x00\x13)-16.66667 (\x00\x1c)-16.66667 (\x00\x11)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 (\x00\x11)-16.66667 '
        b'(\x00\x15)-16.66667 (\x00\x13)-16.66667 (\x00\x15)-16.66667 (\x00\x19)-16.66667 (\x00\x0f)-16.66667 (\x00\x03)-16.66667 '
        b'(\x00\x13)-16.66667 (\x00\x17)-16.66667 (\x00\x1d)-16.66667 (\x00\x17)-16.66667 (\x00\x1a)]'
    )
    OLD_PAYER = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 '
        b'(\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 '
        b'(\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
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
    OLD_AMOUNT = b'[(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    OLD_TM_AMOUNT = b"1 0 0 1 211.95001 72.37499 Tm"
    OLD_PHONE = (
        b'[(\x00\x0e)-16.66667 (\x00\x1a)-16.66667 (\x00\x03)-16.66667 (\x00\x0b)-16.66667 (\x00\x1c)-16.66667 (\x00\x13)-16.66667 '
        b'(\x00\x19)-16.66667 (\x00\x0c)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 (\x00\x16)-16.66667 (\x00\x19)-16.66667 '
        b'(\x00\x10)-16.66667 (\x00\x1b)-16.66667 (\x00\x19)-16.66667 (\x00\x10)-16.66667 (\x00\x14)-16.66667 (\x00\x16)]'
    )
    OLD_BANK = b'[(\x02-)-16.66667 (\x02=)-16.66667 (\x02A)-16.66667 (\x02L)-16.66667 (\x02=)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02F)]'
    OLD_TM_PHONE = b"1 0 0 1 174.825 179.25 Tm"
    OLD_TM_BANK = b"1 0 0 1 216.33751 155.25 Tm"

    def n_glyphs(tj: bytes) -> int:
        """Количество глифов в TJ: кернов + 1."""
        return sum(tj.count(k) for k in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333")) + 1

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = __import__("zlib").decompress(bytes(data[stream_start : stream_start + stream_len]))
        except Exception:
            continue
        if b"BT" not in dec:
            continue

        new_dec = dec

        def replace_field_by_y(
            dec: bytes, y_list: list[float], new_tj: bytes,
            n_min: int = 0, n_max: int = 999, tol: float = 0.15,
            exclude_y_list: list[float] | None = None,
            font_pts_per_unit: float | None = None,
        ) -> bytes:
            """Последний символ = wall. Ширина TJ считается по /W и кернингу.

            font_pts_per_unit: если задан, new_x = wall - new_units * font_pts_per_unit
              (точное выравнивание по шрифтовым метрикам, игнорирует tm_x как опорную точку).
              Например: font_size=9pt / unitsPerEm=1000 → 0.009.
            """
            exclude_y = exclude_y_list or []
            pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
            for mt in re.finditer(pat, dec):
                tm_x = float(mt.group(2))
                y = float(mt.group(4))
                if tm_x < 50:
                    continue
                if exclude_y and any(abs(y - ey) < 2.0 for ey in exclude_y):
                    continue
                if not any(abs(y - vy) < tol for vy in y_list):
                    continue
                old_content = mt.group(5)
                n_old = n_glyphs(b"[" + old_content + b"]")
                if n_old <= 0 or not (n_min <= n_old <= n_max):
                    continue
                new_content = new_tj[1:-1] if new_tj.startswith(b"[") and new_tj.endswith(b"]") else new_tj
                new_units = _tj_advance_units(new_content, cid_widths)
                if font_pts_per_unit is not None and new_units > 0:
                    # Точное выравнивание: new_x = wall - ширина_в_пт (не зависит от tm_x)
                    new_x = wall - new_units * font_pts_per_unit
                else:
                    # old_units: используем ИСХОДНЫЕ ширины (до замены глифов), чтобы scale = правильный
                    old_units = _tj_advance_units(old_content, old_cid_widths)
                    if old_units > 0 and new_units > 0:
                        scale = (wall - tm_x) / old_units
                        new_x = wall - new_units * scale
                    else:
                        p = (wall - tm_x) / n_old
                        if p <= 0 or p > 15:
                            p = _fallback_pts(new_tj)
                        n_new = n_glyphs(b"[" + new_content + b"]")
                        new_x = tm_x_touch_wall(wall, n_new, p)
                repl = mt.group(1) + f"{new_x:.5f}".encode() + mt.group(3) + b" [" + new_content + b"]" + mt.group(6)
                return dec.replace(mt.group(0), repl)
            return dec

        def find_tm_by_y(dec: bytes, y_list: list[float], tol: float):
            """Найти Tm+TJ по Y с допуском. Возвращает re.Match или None."""
            pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
            for m in re.finditer(pat, dec):
                y = float(m.group(4))
                if any(abs(y - vy) < tol for vy in y_list):
                    return m
            return None

        def replace_field_keep_tm(dec: bytes, y_list: list[float], new_tj: bytes, n_min: int = 0, n_max: int = 999, tol: float = 0.15) -> bytes:
            """Заменить только TJ, Tm не трогать (для телефона: формат +7 (XXX) XXX-XX-XX фиксирован)."""
            new_content = new_tj[1:-1] if new_tj.startswith(b"[") and new_tj.endswith(b"]") else new_tj
            n_new = n_glyphs(b"[" + new_content + b"]")
            if not (n_min <= n_new <= n_max):
                return dec
            pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
            for mt in re.finditer(pat, dec):
                y = float(mt.group(4))
                if not any(abs(y - vy) < tol for vy in y_list):
                    continue
                old_content = mt.group(5)
                n_old = n_glyphs(b"[" + old_content + b"]")
                if n_old <= 0 or not (n_min <= n_old <= n_max):
                    continue
                repl = mt.group(0).replace(b"[" + old_content + b"]", b"[" + new_content + b"]")
                return dec.replace(mt.group(0), repl)
            return dec

        def replace_field_by_y_centered(dec: bytes, y_list: list[float], new_tj: bytes) -> bytes:
            """Центрирование под «Исходящий перевод СБП» по реальной ширине TJ."""
            pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
            for mt in re.finditer(pat, dec):
                tm_x = float(mt.group(2))
                y = float(mt.group(4))
                if tm_x < 50 or tm_x > 180:
                    continue
                if not any(abs(y - vy) < y_tol for vy in y_list):
                    continue
                old_content = mt.group(5)
                n_old = n_glyphs(b"[" + old_content + b"]")
                if n_old <= 0:
                    continue
                new_content = new_tj[1:-1] if new_tj.startswith(b"[") and new_tj.endswith(b"]") else new_tj
                old_units = _tj_advance_units(old_content, old_cid_widths)
                new_units = _tj_advance_units(new_content, cid_widths)
                if old_units > 0 and new_units > 0:
                    scale = 2 * (center_heading - tm_x) / old_units
                    new_x = center_heading - (new_units * scale) / 2
                else:
                    p = 2 * (center_heading - tm_x) / n_old if n_old > 0 else _fallback_pts(new_tj)
                    if p <= 0 or p > 15:
                        p = _fallback_pts(new_tj)
                    n_new = n_glyphs(b"[" + new_content + b"]")
                    new_x = center_heading - (n_new * p) / 2
                repl = mt.group(1) + f"{new_x:.5f}".encode() + mt.group(3) + b" [" + new_content + b"]" + mt.group(6)
                return dec.replace(mt.group(0), repl)
            return dec

        if new_date_tj:
            ys_date, tol_date = _y_list("date")
            out = replace_field_by_y(new_dec, ys_date, new_date_tj, tol=max(y_tol, tol_date))
            if out != new_dec:
                new_dec = out
            elif OLD_DATE in new_dec:
                tm_date_m = find_tm_by_y(new_dec, ys_date, max(y_tol, tol_date))
                new_dec = new_dec.replace(OLD_DATE, new_date_tj)
                n = n_glyphs(new_date_tj)
                if n != n_glyphs(OLD_DATE) and tm_date_m and float(tm_date_m.group(2)) > 100:
                    old_units = _tj_advance_units(tm_date_m.group(5), cid_widths)
                    new_units = _tj_advance_units(new_date_tj[1:-1], cid_widths)
                    if old_units > 0 and new_units > 0:
                        scale = (wall - float(tm_date_m.group(2))) / old_units
                        new_x = wall - new_units * scale
                    else:
                        n_old = n_glyphs(b"[" + tm_date_m.group(5) + b"]")
                        pts = (wall - float(tm_date_m.group(2))) / n_old if n_old > 0 else _fallback_pts(new_date_tj)
                        if pts <= 0 or pts > 15:
                            pts = _fallback_pts(new_date_tj)
                        new_x = tm_x_touch_wall(wall, n, pts)
                    tm_date_m2 = find_tm_by_y(new_dec, ys_date, max(y_tol, tol_date))
                    if tm_date_m2:
                        repl = tm_date_m2.group(1) + f"{new_x:.5f}".encode() + tm_date_m2.group(3) + b" [" + tm_date_m2.group(5) + b"]" + tm_date_m2.group(6)
                        new_dec = new_dec.replace(tm_date_m2.group(0), repl)

        if new_payer_tj:
            ys_payer, tol_payer = _y_list("payer")
            exclude_rec = _y_list("recipient")[0]
            out = replace_field_by_y(new_dec, ys_payer, new_payer_tj, tol=max(y_tol, tol_payer), exclude_y_list=exclude_rec)
            if out != new_dec:
                new_dec = out
            elif OLD_PAYER in new_dec:
                tm_payer_m = find_tm_by_y(new_dec, ys_payer, max(y_tol, tol_payer))
                new_dec = new_dec.replace(OLD_PAYER, new_payer_tj)
                n = n_glyphs(new_payer_tj)
                if tm_payer_m and float(tm_payer_m.group(2)) > 50:
                    old_units = _tj_advance_units(tm_payer_m.group(5), cid_widths)
                    new_units = _tj_advance_units(new_payer_tj, cid_widths)
                    if old_units > 0 and new_units > 0:
                        scale = (wall - float(tm_payer_m.group(2))) / old_units
                        new_x = wall - new_units * scale
                    else:
                        n_old = n_glyphs(b"[" + tm_payer_m.group(5) + b"]")
                        pts = (wall - float(tm_payer_m.group(2))) / n_old if n_old > 0 else _fallback_pts(new_payer_tj)
                        if pts <= 0 or pts > 15:
                            pts = _fallback_pts(new_payer_tj)
                        new_x = tm_x_touch_wall(wall, n, pts)
                    tm_payer_m2 = find_tm_by_y(new_dec, ys_payer, max(y_tol, tol_payer))
                    if tm_payer_m2:
                        repl = tm_payer_m2.group(1) + f"{new_x:.5f}".encode() + tm_payer_m2.group(3) + b" [" + tm_payer_m2.group(5) + b"]" + tm_payer_m2.group(6)
                        new_dec = new_dec.replace(tm_payer_m2.group(0), repl)

        if new_recipient_tj:
            ys_recipient, tol_recipient = _y_list("recipient")
            exclude_pay = _y_list("payer")[0]
            out = replace_field_by_y(new_dec, ys_recipient, new_recipient_tj, tol=max(y_tol, tol_recipient), exclude_y_list=exclude_pay)
            if out != new_dec:
                new_dec = out
            elif OLD_RECIPIENT_16 in new_dec:
                tm_rec_m = find_tm_by_y(new_dec, ys_recipient, max(y_tol, tol_recipient))
                new_dec = new_dec.replace(OLD_RECIPIENT_16, new_recipient_tj)
                n = n_glyphs(new_recipient_tj)
                if tm_rec_m and float(tm_rec_m.group(2)) > 100:
                    old_units = _tj_advance_units(tm_rec_m.group(5), cid_widths)
                    new_units = _tj_advance_units(new_recipient_tj[1:-1], cid_widths)
                    if old_units > 0 and new_units > 0:
                        scale = (wall - float(tm_rec_m.group(2))) / old_units
                        new_x = wall - new_units * scale
                    else:
                        n_old = n_glyphs(b"[" + tm_rec_m.group(5) + b"]")
                        pts = (wall - float(tm_rec_m.group(2))) / n_old if n_old > 0 else _fallback_pts(new_recipient_tj)
                        if pts <= 0 or pts > 15:
                            pts = _fallback_pts(new_recipient_tj)
                        new_x = tm_x_touch_wall(wall, n, pts)
                    tm_rec_m2 = find_tm_by_y(new_dec, ys_recipient, max(y_tol, tol_recipient))
                    if tm_rec_m2:
                        repl = tm_rec_m2.group(1) + f"{new_x:.5f}".encode() + tm_rec_m2.group(3) + b" [" + tm_rec_m2.group(5) + b"]" + tm_rec_m2.group(6)
                        new_dec = new_dec.replace(tm_rec_m2.group(0), repl)
            if new_recipient_21:
                ys_centered, tol_centered = _y_list("centered")
                out327 = replace_field_by_y_centered(new_dec, ys_centered, new_recipient_21)
                if out327 != new_dec:
                    new_dec = out327

        if new_amount_tj:
            pass

        # Сумма: реальная ширина TJ из /W и кернинга. Только правый столбец (tm_x>100), не подпись «Сумма операции».
        y_amount = _y_list("amount")[0][0]
        amt_pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)(\s*\r?\n[^\[]*)?\[([^\]]+)\]\s*TJ"
        for amt_m in re.finditer(amt_pat, new_dec):
            if abs(float(amt_m.group(4)) - y_amount) > 0.1:
                continue
            if float(amt_m.group(2)) < 100:
                continue
            between = amt_m.group(5) or b"\n"
            tj_content = amt_m.group(6)
            if b"-11.11111" in tj_content:
                new_content = new_amount_tj[1:-1] if new_amount_tj else tj_content
                new_units = _tj_advance_units(new_content, cid_widths)
                if new_units > 0:
                    # Сумма в ВТБ всегда в font-size 13.5pt, поэтому можно считать напрямую.
                    new_x_amt = wall - new_units * (13.5 / 1000.0)
                else:
                    n_amt = 1 + tj_content.count(b"-11.11111")
                    pts_amt = (wall - float(amt_m.group(2))) / n_amt if n_amt > 0 else 6.75
                    if pts_amt <= 0 or pts_amt > 15:
                        pts_amt = 6.75
                    new_x_amt = tm_x_touch_wall(wall, n_amt, pts_amt)
                repl = amt_m.group(1) + f"{new_x_amt:.5f}".encode() + amt_m.group(3) + between + b"[" + new_content + b"] TJ"
                new_dec = new_dec.replace(amt_m.group(0), repl)
                break

        if new_message_tj:
            # Всегда использовать полный список Y из layout (не raw), чтобы заменить обе строки
            ys_message = list(ly.get("message", ())) if isinstance(ly.get("message"), (list, tuple)) else []
            tol_message = 2.0
            if ys_message:
                # Найти все TJ-блоки в области сообщения, отсортировать по Y (сверху вниз)
                tol_m = max(y_tol, tol_message)
                pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
                matches = []
                for mt in re.finditer(pat, new_dec):
                    tm_x = float(mt.group(2))
                    y = float(mt.group(4))
                    if tm_x < 50:
                        continue
                    if not any(abs(y - vy) < tol_m for vy in ys_message):
                        continue
                    n_old = n_glyphs(b"[" + mt.group(5) + b"]")
                    if n_old <= 0 or not (5 <= n_old <= 60):
                        continue
                    matches.append((y, mt))
                matches.sort(key=lambda t: -t[0])  # Y по убыванию (первая строка сверху)
                for i, (y, mt) in enumerate(matches):
                    new_tj = new_message_tj if i == 0 else _build(" ", wrap=True)
                    new_content = new_tj[1:-1] if new_tj.startswith(b"[") and new_tj.endswith(b"]") else new_tj
                    old_content = mt.group(5)
                    old_units = _tj_advance_units(old_content, cid_widths)
                    new_units = _tj_advance_units(new_content, cid_widths)
                    tm_x = float(mt.group(2))
                    if old_units > 0 and new_units > 0:
                        scale = (wall - tm_x) / old_units
                        new_x = wall - new_units * scale
                    else:
                        n_new = n_glyphs(b"[" + new_content + b"]")
                        new_x = tm_x_touch_wall(wall, n_new, _fallback_pts(new_tj))
                    repl = mt.group(1) + f"{new_x:.5f}".encode() + mt.group(3) + b" [" + new_content + b"]" + mt.group(6)
                    new_dec = new_dec.replace(mt.group(0), repl)

        if new_account_tj:
            ys_account, tol_account = _y_list("account")
            exclude_done = [raw["y"]["done"]] if raw.get("y", {}).get("done") is not None else []
            out = replace_field_by_y(new_dec, ys_account, new_account_tj, n_min=4, n_max=6, tol=max(y_tol, tol_account), exclude_y_list=exclude_done)
            if out != new_dec:
                new_dec = out

        if new_phone_tj:
            ys_phone, tol_phone = _y_list("phone")
            out = replace_field_by_y(new_dec, ys_phone, new_phone_tj, n_min=10, n_max=28, tol=max(y_tol, tol_phone))
            if out != new_dec:
                new_dec = out
            elif OLD_PHONE in new_dec:
                tm_phone_m = find_tm_by_y(new_dec, ys_phone, max(y_tol, tol_phone))
                new_dec = new_dec.replace(OLD_PHONE, new_phone_tj)
                if tm_phone_m and float(tm_phone_m.group(2)) > 50:
                    old_units = _tj_advance_units(tm_phone_m.group(5), cid_widths)
                    new_units = _tj_advance_units(new_phone_tj[1:-1], cid_widths)
                    if old_units > 0 and new_units > 0:
                        scale = (wall - float(tm_phone_m.group(2))) / old_units
                        new_x = wall - new_units * scale
                    else:
                        n_old = n_glyphs(b"[" + tm_phone_m.group(5) + b"]")
                        n = n_glyphs(new_phone_tj)
                        pts = (wall - float(tm_phone_m.group(2))) / n_old if n_old > 0 else _fallback_pts(new_phone_tj)
                        if pts <= 0 or pts > 15:
                            pts = _fallback_pts(new_phone_tj)
                        new_x = tm_x_touch_wall(wall, n, pts)
                    tm_phone_m2 = find_tm_by_y(new_dec, ys_phone, max(y_tol, tol_phone))
                    if tm_phone_m2:
                        repl = tm_phone_m2.group(1) + f"{new_x:.5f}".encode() + tm_phone_m2.group(3) + b" [" + tm_phone_m2.group(5) + b"]" + tm_phone_m2.group(6)
                        new_dec = new_dec.replace(tm_phone_m2.group(0), repl)

        if new_bank_tj:
            ys_bank, tol_bank = _y_list("bank")
            out = replace_field_by_y(new_dec, ys_bank, new_bank_tj, n_min=7, n_max=16, tol=max(y_tol, tol_bank))
            if out != new_dec:
                new_dec = out
            elif OLD_BANK in new_dec:
                tm_bank_m = find_tm_by_y(new_dec, ys_bank, max(y_tol, tol_bank))
                new_dec = new_dec.replace(OLD_BANK, new_bank_tj)
                n = n_glyphs(new_bank_tj)
                if tm_bank_m and float(tm_bank_m.group(2)) > 50:
                    n_old = n_glyphs(b"[" + tm_bank_m.group(5) + b"]")
                    pts = (wall - float(tm_bank_m.group(2))) / n_old if n_old > 0 else _fallback_pts(new_bank_tj)
                    if pts <= 0 or pts > 15:
                        pts = _fallback_pts(new_bank_tj)
                    new_x = tm_x_touch_wall(wall, n, pts)
                    tm_bank_m2 = find_tm_by_y(new_dec, ys_bank, max(y_tol, tol_bank))
                    if tm_bank_m2:
                        repl = tm_bank_m2.group(1) + f"{new_x:.5f}".encode() + tm_bank_m2.group(3) + b" [" + tm_bank_m2.group(5) + b"]" + tm_bank_m2.group(6)
                        new_dec = new_dec.replace(tm_bank_m2.group(0), repl)

        if new_opid_tj:
            ys_opid, tol_opid = _y_list("opid")
            exclude_done = [raw["y"]["done"]] if raw.get("y", {}).get("done") is not None else []
            # font_pts_per_unit=0.009 (font_size=9pt / unitsPerEm=1000) для точного выравнивания
            # к правому краю независимо от tm_x в deepcopy-генерируемом PDF
            out = replace_field_by_y(new_dec, ys_opid, new_opid_tj, n_min=15, tol=max(y_tol, tol_opid),
                                     exclude_y_list=exclude_done, font_pts_per_unit=0.009)
            if out != new_dec:
                new_dec = out

        if new_dec != dec:
            new_raw = __import__("zlib").compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)
            data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
            num_end = len_num_start + len(old_len_str)
            data = data[:len_num_start] + new_len_str + data[num_end:]
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
        from vtb_test_generator import update_id, update_creation_date
        update_creation_date(data, f"D:{datetime.now().strftime('%Y%m%d%H%M%S')}+03'00'")
        update_id(data)
    return bytes(data)


def main():
    base = Path(__file__).parent
    config_path = base / "vtb_config.json"
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            config_path = Path(sys.argv[i + 1])
            break

    if not config_path.exists():
        print(f"[ERROR] Конфиг не найден: {config_path}")
        print("Создай vtb_config.json (см. vtb_config.json)")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    pdf_args = [a for a in sys.argv[1:] if not a.startswith("-") and a != "--config" and not a.endswith(".json") and a.lower().endswith(".pdf")]
    inp = Path(pdf_args[-1]) if pdf_args and Path(pdf_args[-1]).exists() else None
    for fallback in [
        Path("/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf"),
        base / "Тест ВТБ" / "09-03-26_03-47_1.pdf",
        *(list((base / "база_чеков" / "vtb" / "СБП").glob("*.pdf")) if (base / "база_чеков" / "vtb" / "СБП").exists() else []),
    ]:
        if not inp and fallback.exists() and str(fallback).lower().endswith(".pdf"):
            inp = fallback
            break
    if not inp or not inp.exists():
        print("[ERROR] Файл не найден. Укажите PDF: python3 vtb_patch_from_config.py donor.pdf")
        sys.exit(1)

    out_dir = base / cfg.get("output_folder", "10.03")
    out_dir.mkdir(parents=True, exist_ok=True)

    data = bytearray(inp.read_bytes())
    try:
        data = patch_from_values(
            data,
            inp,
            date_str=cfg.get("date", "now"),
            payer=cfg.get("payer"),
            recipient=cfg.get("recipient"),
            phone=cfg.get("phone"),
            bank=cfg.get("bank"),
            amount=cfg.get("amount"),
            operation_id=cfg.get("operation_id"),
            account=cfg.get("account", "*9483"),
            keep_metadata=not cfg.get("update_id", True),
        )
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    out_name = cfg.get("output_name") or f"{inp.stem}_config"
    if not out_name.endswith(".pdf"):
        out_name += ".pdf"
    out_path = out_dir / out_name
    out_path.write_bytes(data)
    print(f"[OK] {out_path}")
    print(f"  date={cfg.get('date')}, payer={cfg.get('payer')}, recipient={cfg.get('recipient')}, amount={cfg.get('amount')}")


if __name__ == "__main__":
    main()
