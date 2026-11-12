#!/usr/bin/env python3
"""Шаблон чека Альфа «перевод с карты на карту»: карта получателя, сумма, комиссия."""
from __future__ import annotations

import re
import zlib
from pathlib import Path

from pdf_patcher import format_amount_display

_BASE_DIR = Path(__file__).resolve().parent
ALFA_KARTA_TEMPLATE = _BASE_DIR / "шаблоны" / "alfa_karta.pdf"


def extract_alfa_karta_fields(pdf_path: str | Path | bytes) -> dict[str, str]:
    """Текстовые поля из квитанции «с карты на карту» (fitz).

    Сохраняет неразрывные пробелы (\xa0) как в PDF — нужно для CID-замен.
    """
    try:
        import fitz
    except ImportError:
        return {}

    if isinstance(pdf_path, bytes):
        doc = fitz.open(stream=pdf_path, filetype="pdf")
    else:
        doc = fitz.open(str(pdf_path))
    if doc.page_count < 1:
        doc.close()
        return {}
    text = doc[0].get_text()
    doc.close()

    lines = [l.strip("\r\n") for l in text.split("\n")]
    fields: dict[str, str] = {}

    def _norm(s: str) -> str:
        return s.replace("\xa0", " ")

    def _next_val(i: int) -> str:
        for j in range(i + 1, min(i + 5, len(lines))):
            v = lines[j].strip()
            if v and v not in ("\xa0", " "):
                return v
        return ""

    for i, line in enumerate(lines):
        nl = _norm(line)
        if "Сумма перевода" in nl:
            raw = _next_val(i)
            if raw:
                fields["amount_raw"] = raw
        elif nl == "Комиссия" or nl.startswith("Комиссия "):
            raw = _next_val(i)
            if raw:
                fields["commission_raw"] = raw
        elif "карты отправителя" in nl:
            raw = _next_val(i)
            if raw:
                fields["card_sender"] = raw
        elif "карты получателя" in nl:
            raw = _next_val(i)
            if raw:
                fields["card_recipient"] = raw
        elif nl == "Сформирована" or nl.startswith("Сформирована "):
            raw = _next_val(i)
            if raw:
                fields["formed_at_raw"] = raw
        elif "Дата" in nl and "время" in nl and "перевода" in nl:
            raw = _next_val(i)
            if raw:
                fields["transfer_dt_raw"] = raw
    return fields


def _normalize_card(s: str) -> str:
    s = s.replace("\xa0", " ").strip().replace(" ", "")
    return s


def commission_user_typed_zero_kopeks(text: str) -> bool:
    """True, если в вводе явно указаны нулевые копейки (…,00 или ….00)."""
    s = (text or "").strip().replace(" ", "").replace("\xa0", "")
    return bool(re.search(r"[.,]00$", s))


def _commission_to_template_str(
    rub: float, nbsp: bool = True, *, show_zero_kopeks: bool = False
) -> str:
    """Целые без «,00», если пользователь не просил ,00; иначе «244,00»; копейки — «62,79»."""
    cents = int(round(rub * 100))
    whole = cents // 100
    frac = cents % 100
    sp = "\xa0" if nbsp else " "
    if frac == 0:
        if show_zero_kopeks:
            return f"{whole},00{sp}RUR{sp}"
        return f"{whole}{sp}RUR{sp}"
    return f"{whole},{frac:02d}{sp}RUR{sp}"


_SKIP_TIME = frozenset(("-", "—", "пропустить", "skip", ""))

_FORMED_LINE_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*$"
)
_TRANSFER_LINE_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*$"
)


def parse_alfa_karta_formed_input(text: str) -> str | None:
    """None — оставить шаблон. Иначе строка для PDF: «ДД.ММ.ГГГГ\\xa0ЧЧ:ММ\\xa0мск»."""
    t = (text or "").strip().replace("\xa0", " ")
    if t in _SKIP_TIME:
        return None
    m = _FORMED_LINE_RE.match(t)
    if not m:
        raise ValueError("Ожидается ДД.ММ.ГГГГ ЧЧ:ММ (например 22.03.2026 06:34)")
    dd, mm, yyyy, hh, mi = m.groups()
    return f"{dd}.{mm}.{yyyy}\xa0{int(hh):02d}:{mi}\xa0мск"


def parse_alfa_karta_transfer_input(text: str) -> str | None:
    """None — оставить шаблон. Иначе «ДД.ММ.ГГГГ\\xa0ЧЧ:ММ:СС\\xa0мск»."""
    t = (text or "").strip().replace("\xa0", " ")
    if t in _SKIP_TIME:
        return None
    m = _TRANSFER_LINE_RE.match(t)
    if not m:
        raise ValueError("Ожидается ДД.ММ.ГГГГ ЧЧ:ММ:СС (например 22.03.2026 06:34:23)")
    dd, mm, yyyy, hh, mi, ss = m.groups()
    return f"{dd}.{mm}.{yyyy}\xa0{int(hh):02d}:{mi}:{ss}\xa0мск"


def _extend_bfrange_for_unicode(data: bytearray, unicode_cp: int) -> bytearray:
    """Добавить в beginbfrange строку <CID><CID><U+cp>, CID = следующий свободный 002e.."""
    import re as _re

    for m in _re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", bytes(data), _re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"beginbfrange" not in dec or b"beginbfchar" in dec:
            continue
        marker = f"<{unicode_cp:04x}>".encode().upper()
        if marker in dec.upper():
            return data
        cm = _re.search(rb"(\d+)\s+beginbfrange", dec)
        if not cm:
            continue
        old_n = int(cm.group(1))
        new_n = old_n + 1
        dec2 = dec[: cm.start(1)] + str(new_n).encode() + dec[cm.end(1) :]
        used: set[int] = set()
        for mm in _re.finditer(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4})>", dec2):
            for g in (1, 2):
                used.add(int(mm.group(g), 16))
        free_cid = 0x2E
        while free_cid <= 0xFFFF and free_cid in used:
            free_cid += 1
        if free_cid > 0xFFFF:
            return data
        insert = f"\r\n<{free_cid:04X}><{free_cid:04X}><{unicode_cp:04X}>".encode()
        end_pos = dec2.rfind(b"endbfrange")
        if end_pos < 0:
            continue
        new_dec = dec2[:end_pos] + insert + dec2[end_pos:]
        new_raw = zlib.compress(new_dec, 9)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)
        data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str
        xref_m = _re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in _re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = _re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()
        return data
    return data


def _ensure_pdf_chars(data: bytearray, text: str) -> bytearray:
    from cid_patch_amount import _parse_tounicode, _encode_cid

    uni = _parse_tounicode(bytes(data))
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni and 0xA0 in uni:
            continue
        if cp not in uni and cp >= 0x20:
            data = _extend_bfrange_for_unicode(data, cp)
            uni = _parse_tounicode(bytes(data))
    return data


def patch_alfa_karta(
    *,
    new_recipient_card: str,
    new_amount_rub: int,
    new_commission_rub: float | None,
    commission_show_zero_kopeks: bool = False,
    new_formed_at: str | None = None,
    new_transfer_at: str | None = None,
    template_path: Path | None = None,
) -> tuple[bool, str | None, bytes | None]:
    """Патч шаблона: карта, сумма, комиссия; опционально «Сформирована» и дата/время перевода."""
    from cid_patch_amount import patch_replacements

    tpl = template_path or ALFA_KARTA_TEMPLATE
    if not tpl.exists():
        return False, f"Шаблон не найден: {tpl}", None

    raw = bytearray(tpl.read_bytes())
    fields = extract_alfa_karta_fields(bytes(raw))
    if not fields.get("card_recipient") or not fields.get("amount_raw"):
        return False, "Не удалось прочитать поля шаблона (карта/сумма).", None

    old_card = fields["card_recipient"]
    new_card = _normalize_card(new_recipient_card)
    if not new_card.endswith("\xa0"):
        new_card_disp = new_card + "\xa0"
    else:
        new_card_disp = new_card
    if not old_card.endswith("\xa0"):
        old_card_key = old_card + "\xa0"
    else:
        old_card_key = old_card

    old_amt = fields["amount_raw"]
    new_amt = f"{format_amount_display(int(new_amount_rub))}\xa0RUR\xa0"

    pairs: list[tuple[str, str]] = []

    pairs.append((old_card_key, new_card_disp))

    if old_amt != new_amt:
        pairs.append((old_amt, new_amt))

    if new_commission_rub is not None and fields.get("commission_raw"):
        old_c = fields["commission_raw"]
        new_c = _commission_to_template_str(
            float(new_commission_rub), show_zero_kopeks=commission_show_zero_kopeks
        )
        if old_c != new_c:
            pairs.append((old_c, new_c))

    if new_formed_at is not None and fields.get("formed_at_raw"):
        old_f = fields["formed_at_raw"]
        if old_f != new_formed_at:
            pairs.append((old_f, new_formed_at))

    if new_transfer_at is not None and fields.get("transfer_dt_raw"):
        old_t = fields["transfer_dt_raw"]
        if old_t != new_transfer_at:
            pairs.append((old_t, new_transfer_at))

    for _old, _new in pairs:
        raw = _ensure_pdf_chars(raw, _new)

    tmp_in = _BASE_DIR / "_alfa_karta_work.pdf"
    tmp_in.write_bytes(bytes(raw))
    tmp = _BASE_DIR / "_alfa_karta_out.pdf"
    ok = patch_replacements(tmp_in, tmp, pairs)
    tmp_in.unlink(missing_ok=True)
    if not ok:
        tmp.unlink(missing_ok=True)
        return False, "Не удалось применить замены в PDF.", None

    out = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return True, None, out


def template_exists() -> bool:
    return ALFA_KARTA_TEMPLATE.exists()
