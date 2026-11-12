#!/usr/bin/env python3
"""Gazprombank SBP receipt generator.

Generates a valid Gazprombank PDF receipt for СБП transfers with arbitrary data.

Fields supported:
    amount:           Transfer amount in RUB
    sender_name:      Sender full name (UPPERCASE, e.g. 'ДАНИЛ АЛЕКСАНДРОВИЧ С.')
    sender_card:      Sender card last 4 digits (e.g. '8527')
    recipient_name:   Recipient full name (e.g. 'Дарья Андреевна М.')
    recipient_phone:  Recipient phone +7(XXX)XXX-XX-XX
    recipient_bank:   Recipient bank name (e.g. 'Т-Банк', 'Сбербанк')
    operation_date:   DD.MM.YYYY or 'auto'
    operation_time:   HH:MM:SS or 'auto'  (only HH:MM shown on receipt)
    sbp_number:       SBP operation ID (32 chars) or 'auto'

Donor PDFs must be placed in the GPB/ subfolder.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.resolve()
GPB_DONORS_DIR = ROOT / "GPB"

_MSK = timedelta(hours=3)

_GPB_SUFFIX_DEFAULT = "0011680301"
_GPB_P23 = "60"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_amount_gpb(amount: int) -> str:
    """'10\xa0000,00\xa0руб.' — GPB comma decimal, NBSP thousands."""
    s = f"{amount:,}".replace(",", "\xa0")
    return f"{s},00\xa0руб."


def _fmt_datetime_gpb(date_str: str, time_str: str) -> str:
    """'08.04.2026\xa0в\xa010:16\xa0(МСК)'."""
    hhmm = time_str[:5]
    return f"{date_str}\xa0в\xa0{hhmm}\xa0(МСК)"


def _fmt_phone_gpb(phone: str) -> str:
    """'+7(XXX)XXX-XX-XX' — GPB uses no spaces around area code."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        d = digits[1:]
        return f"+7({d[:3]}){d[3:6]}-{d[6:8]}-{d[8:10]}"
    return phone


def _fmt_card_mask(last4: str) -> str:
    """'**** **** **** 8527'."""
    last4 = re.sub(r"\D", "", last4)[-4:].zfill(4)
    return f"**** **** **** {last4}"


# ---------------------------------------------------------------------------
# SBP ID generation for Gazprombank
# ---------------------------------------------------------------------------

def _generate_gpb_sbp_id(date_str: str, time_str: str, donor_sbp_id: str = "") -> str:
    """Generate a 32-char Gazprombank SBP ID."""
    parts = date_str.split(".")
    dd, mm, yyyy = int(parts[0]), int(parts[1]), int(parts[2])
    hh, mi, ss = int(time_str[:2]), int(time_str[3:5]), int(time_str[6:8])
    msk_dt = datetime(yyyy, mm, dd, hh, mi, ss, tzinfo=timezone.utc) - _MSK
    utc_dt = msk_dt

    day_of_year = utc_dt.timetuple().tm_yday % 100
    seq = random.randint(1000, 9999)

    p23 = donor_sbp_id[1:3] if len(donor_sbp_id) >= 3 else _GPB_P23
    if donor_sbp_id and len(donor_sbp_id) == 32:
        suffix = donor_sbp_id[16:]
    else:
        counter = f"{random.randint(1, 999):06d}"
        suffix = counter + _GPB_SUFFIX_DEFAULT

    check_char = str(random.randint(0, 9))

    return (
        f"A{p23}{day_of_year:02d}{utc_dt.hour:02d}{utc_dt.minute:02d}{utc_dt.second:02d}"
        f"{seq:04d}{check_char}{suffix}"
    )


# ---------------------------------------------------------------------------
# PDF escape / unescape for parenthesized binary strings
# ---------------------------------------------------------------------------

def _pdf_unescape(raw: bytes) -> bytes:
    """Remove PDF string escape sequences → actual bytes."""
    result = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            if nxt == 0x6E:    result.append(0x0A); i += 2   # \n
            elif nxt == 0x72:  result.append(0x0D); i += 2   # \r
            elif nxt == 0x74:  result.append(0x09); i += 2   # \t
            elif nxt == 0x62:  result.append(0x08); i += 2   # \b
            elif nxt == 0x66:  result.append(0x0C); i += 2   # \f
            elif nxt == 0x5C:  result.append(0x5C); i += 2   # \\
            elif nxt == 0x28:  result.append(0x28); i += 2   # \(
            elif nxt == 0x29:  result.append(0x29); i += 2   # \)
            elif 0x30 <= nxt <= 0x37:                          # \ddd octal
                octal = chr(nxt)
                j = i + 2
                while j < i + 4 and j < len(raw) and 0x30 <= raw[j] <= 0x37:
                    octal += chr(raw[j])
                    j += 1
                result.append(int(octal, 8))
                i = j
            else:
                result.append(nxt)
                i += 2
        else:
            result.append(raw[i])
            i += 1
    return bytes(result)


def _pdf_escape(actual: bytes) -> bytes:
    """Apply PDF string escaping → suitable for use inside (...)."""
    result = bytearray()
    for byte in actual:
        if byte == 0x28:   result += b"\\("
        elif byte == 0x29: result += b"\\)"
        elif byte == 0x5C: result += b"\\\\"
        elif byte == 0x0A: result += b"\\n"
        elif byte == 0x0D: result += b"\\r"
        elif byte == 0x09: result += b"\\t"
        elif byte == 0x08: result += b"\\b"
        elif byte == 0x0C: result += b"\\f"
        else:
            result.append(byte)
    return bytes(result)


# ---------------------------------------------------------------------------
# CMap parsing — merge ALL CMaps from the PDF
# ---------------------------------------------------------------------------

def _build_all_cmaps(pdf_bytes: bytes) -> tuple[dict[int, str], list[dict[int, str]]]:
    """Return (cid_to_char, list_of_uni_to_cid_dicts) from ALL CMaps in the PDF.

    cid_to_char:  CID_int → unicode char  (merged from all fonts)
    list_of_dicts: each dict is uni_int → cid_str for one font
    """
    uni_to_cid_list: list[dict[int, str]] = []
    cid_to_char: dict[int, str] = {}

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        raw = pdf_bytes[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"beginbfrange" not in dec and b"beginbfchar" not in dec:
            continue

        uni_to_cid: dict[int, str] = {}
        if b"beginbfrange" in dec:
            for mm in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec
            ):
                s = int(mm.group(1), 16)
                e = int(mm.group(2), 16)
                u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    cid_int = s + i
                    uni_int = u + i
                    uni_to_cid[uni_int] = f"{cid_int:04X}"
                    cid_to_char[cid_int] = chr(uni_int)
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid_str = mm.group(1).decode().upper().zfill(4)
                uni_int = int(mm.group(2).decode(), 16)
                cid_int = int(cid_str, 16)
                uni_to_cid[uni_int] = cid_str
                cid_to_char[cid_int] = chr(uni_int)
        if uni_to_cid:
            uni_to_cid_list.append(uni_to_cid)

    return cid_to_char, uni_to_cid_list


# ---------------------------------------------------------------------------
# Binary (...)Tj text extraction
# ---------------------------------------------------------------------------

def _iter_paren_strings(dec: bytes):
    """Yield (raw_content, is_tj) for each (...) string in dec.

    raw_content: raw bytes between the outer parentheses (not unescaped yet)
    is_tj:       True if this string is immediately followed by Tj operator
    """
    i = 0
    while i < len(dec):
        paren_pos = dec.find(b"(", i)
        if paren_pos < 0:
            break
        # Scan for matching close-paren (handle nested parens and escapes)
        j = paren_pos + 1
        depth = 1
        content = bytearray()
        while j < len(dec) and depth > 0:
            b = dec[j]
            if b == 0x5C and j + 1 < len(dec):  # backslash escape
                content.append(b)
                content.append(dec[j + 1])
                j += 2
            elif b == 0x28:  # (
                depth += 1
                content.append(b)
                j += 1
            elif b == 0x29:  # )
                depth -= 1
                if depth > 0:
                    content.append(b)
                j += 1
            else:
                content.append(b)
                j += 1
        # Check if Tj follows
        after = dec[j:].lstrip(b" \t\r\n")
        is_tj = after.startswith(b"Tj")
        yield paren_pos, j, bytes(content), is_tj
        i = j


def _decode_binary_cids(raw_content: bytes, cid_to_char: dict[int, str]) -> str:
    """Decode a (already unescaped) sequence of 2-byte CIDs to text."""
    actual = _pdf_unescape(raw_content)
    text = ""
    for k in range(0, len(actual) - 1, 2):
        cid = (actual[k] << 8) | actual[k + 1]
        text += cid_to_char.get(cid, f"[{cid:04X}]")
    return text


def _extract_gpb_fields(pdf_bytes: bytes, cid_to_char: dict[int, str]) -> dict[str, str]:
    """Extract variable fields from a GPB receipt.

    GPB content streams render some label/value pairs in reverse order
    (value before label in stream), so we use regex pattern matching on
    the decoded text values rather than relying on stream position.
    """
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        slen = int(m.group(2))
        start = m.end()
        raw = pdf_bytes[start: start + slen]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in dec or b"q\nBT\n" not in dec:
            continue

        # Decode all Tj strings
        texts: list[str] = []
        for _, _, content, is_tj in _iter_paren_strings(dec):
            if not is_tj:
                continue
            t = _decode_binary_cids(content, cid_to_char)
            texts.append(t)

        fields: dict[str, str] = {}
        for t in texts:
            tn = t.replace("\xa0", " ").strip()
            if not tn:
                continue
            # Date/time: "08.04.2026 в 10:16 (МСК)"
            if "date_time" not in fields and re.fullmatch(
                r"\d{2}\.\d{2}\.\d{4}\s+в\s+\d{2}:\d{2}\s+\(МСК\)", tn
            ):
                fields["date_time"] = t
            # Sender card: "**** **** **** XXXX"
            elif "sender_card" not in fields and re.fullmatch(
                r"\*+\s+\*+\s+\*+\s+\d{4}", tn
            ):
                fields["sender_card"] = t
            # Recipient phone: "+7(XXX)XXX-XX-XX" or "+7(XXX)XXX XX XX"
            elif "recipient_phone" not in fields and re.match(r"\+7[\(\d]", tn) and re.search(r"\d{3}.?\d{2}.?\d{2}", tn):
                fields["recipient_phone"] = t
            # SBP ID: 32-char alphanumeric starting with A or B
            elif "sbp_id" not in fields and re.fullmatch(r"[A-Za-z0-9]{32}", tn):
                fields["sbp_id"] = t
            # Amount "10 000,00 руб." — the largest amount (main transfer)
            elif re.search(r"\d[\d\s]*,\d{2}\s*руб\.", tn):
                # First match → "amount", second → "commission", third → "total"
                # But GPB stream order might vary; use largest as amount and total
                if "amount" not in fields:
                    fields["amount"] = t
                elif "commission" not in fields:
                    fields["commission"] = t
                elif "total" not in fields:
                    fields["total"] = t
            # Names: Cyrillic with a dot and capital first letter
            # Recipient name: "Дарья Андреевна М." (mixed case initial + dot)
            # Sender name: "ДАНИЛ АЛЕКСАНДРОВИЧ С." (all caps)

        # Second pass: identify names by looking between known labels
        # Use a label→next_value approach but aware of stream order issues
        labels = {t.replace("\xa0", " ").strip(): t for t in texts}
        all_norms = [t.replace("\xa0", " ").strip() for t in texts]

        def _adjacent_value(label_text: str, look_after: bool = True) -> str:
            """Find the value adjacent to a label in the stream."""
            try:
                idx = all_norms.index(label_text)
            except ValueError:
                return ""
            # Try looking after (idx+1)
            if look_after:
                for j in range(idx + 1, min(idx + 3, len(texts))):
                    v = all_norms[j].strip()
                    if v and v not in (
                        "Дата и время операции:", "Тип операции:", "Статус операции:",
                        "Реквизиты карты списания:", "Номер телефона получателя:",
                        "Ф.И.О. получателя:", "Банк получателя:", "Ф.И.О. отправителя:",
                        "Сумма:", "Сумма комиссии:", "Сумма с учётом комиссии:",
                        "Номер операции СБП:", "Тип операции СБП:", "Перевод по номеру телефона",
                        "Операция выполнена", "Чек по операции",
                    ):
                        return texts[j]
            # Try looking before (idx-1)
            for j in range(idx - 1, max(idx - 3, -1), -1):
                v = all_norms[j].strip()
                if v and v not in (
                    "Дата и время операции:", "Тип операции:", "Статус операции:",
                    "Реквизиты карты списания:", "Номер телефона получателя:",
                    "Ф.И.О. получателя:", "Банк получателя:", "Ф.И.О. отправителя:",
                    "Сумма:", "Сумма комиссии:", "Сумма с учётом комиссии:",
                    "Номер операции СБП:", "Тип операции СБП:", "Перевод по номеру телефона",
                    "Операция выполнена", "Чек по операции",
                ) and not re.search(r"\d[\d\s]*,\d{2}\s*руб\.", v):
                    return texts[j]
            return ""

        for label, field_key in [
            ("Ф.И.О. получателя:", "recipient_name"),
            ("Банк получателя:", "recipient_bank"),
            ("Ф.И.О. отправителя:", "sender_name"),
        ]:
            if field_key not in fields:
                val = _adjacent_value(label)
                if val:
                    fields[field_key] = val

        # Fix amounts: identify commission (= 0,00) vs amount (largest)
        amount_texts = [t for t in texts if re.search(r"\d[\d\s]*,\d{2}\s*руб\.", t.replace("\xa0", " "))]
        if len(amount_texts) >= 3:
            # Parse and sort by numeric value
            def _parse_amt(t: str) -> float:
                m2 = re.search(r"([\d\xa0\s]+),(\d{2})", t)
                if m2:
                    return float(re.sub(r"[\xa0\s]", "", m2.group(1)) + "." + m2.group(2))
                return 0.0
            sorted_amts = sorted(amount_texts, key=_parse_amt)
            fields["commission"] = sorted_amts[0]   # smallest = commission (0,00)
            fields["amount"] = sorted_amts[-1]       # largest = transfer amount
            fields["total"] = sorted_amts[-1]        # total = same as amount when commission=0

        return fields
    return {}


# ---------------------------------------------------------------------------
# Binary CID stream patching
# ---------------------------------------------------------------------------

def _encode_text_binary(text: str, uni_to_cid: dict[int, str]) -> bytes | None:
    """Encode text as a sequence of 2-byte big-endian CID values.

    Returns None if any character is missing from the CMap.
    """
    result = bytearray()
    for ch in text:
        cp = ord(ch)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        cid_int = int(uni_to_cid[cp], 16)
        result.append((cid_int >> 8) & 0xFF)
        result.append(cid_int & 0xFF)
    return bytes(result)


def _patch_gpb_stream(
    dec: bytes,
    replacements: list[tuple[str, str]],
    uni_to_cid_list: list[dict[int, str]],
    cid_to_char: dict[int, str],
) -> bytes:
    """Apply text replacements to a GPB binary CID content stream.

    For each (old_text, new_text) pair:
      1. Encode old_text as binary CID bytes using each available font's CMap
      2. Apply PDF escaping to get the escaped form as it appears in the stream
      3. Search in the (...)Tj string content
      4. Replace with escaped form of new_text
    """
    new_dec = bytearray(dec)

    # Build NBSP fallback: treat space as NBSP for fonts that only have NBSP
    def _get_cid(cp: int, umap: dict[int, str]) -> int | None:
        if cp == 0x20 and 0x20 not in umap and 0xA0 in umap:
            cp = 0xA0
        if cp in umap:
            return int(umap[cp], 16)
        return None

    total_replaced = 0
    for old_text, new_text in replacements:
        if old_text == new_text:
            continue

        # Try each font CMap
        replaced = False
        for umap in uni_to_cid_list:
            # Encode old text
            old_actual = bytearray()
            ok = True
            for ch in old_text.replace("\xa0", " "):
                cp = ord(ch)
                cid = _get_cid(cp, umap)
                if cid is None:
                    ok = False
                    break
                old_actual.append((cid >> 8) & 0xFF)
                old_actual.append(cid & 0xFF)
            if not ok:
                continue

            old_escaped = _pdf_escape(bytes(old_actual))

            # Find in stream (within parenthesized strings)
            pos = bytes(new_dec).find(old_escaped)
            if pos < 0:
                # Try with trailing NBSP
                old_text_nbsp = old_text.rstrip() + "\xa0"
                old_actual2 = bytearray()
                ok2 = True
                for ch in old_text_nbsp.replace("\xa0", " "):
                    cp = ord(ch)
                    cid = _get_cid(cp, umap)
                    if cid is None:
                        ok2 = False
                        break
                    old_actual2.append((cid >> 8) & 0xFF)
                    old_actual2.append(cid & 0xFF)
                if ok2:
                    old_escaped_nbsp = _pdf_escape(bytes(old_actual2))
                    pos = bytes(new_dec).find(old_escaped_nbsp)
                    if pos >= 0:
                        old_escaped = old_escaped_nbsp
                        new_text = new_text.rstrip() + " "  # keep trailing space

            if pos < 0:
                continue

            # Encode new text
            new_actual = bytearray()
            ok_new = True
            for ch in new_text.replace("\xa0", " "):
                cp = ord(ch)
                cid = _get_cid(cp, umap)
                if cid is None:
                    ok_new = False
                    break
                new_actual.append((cid >> 8) & 0xFF)
                new_actual.append(cid & 0xFF)
            if not ok_new:
                continue

            new_escaped = _pdf_escape(bytes(new_actual))
            new_dec = bytearray(bytes(new_dec).replace(old_escaped, new_escaped, 1))
            total_replaced += 1
            replaced = True
            print(f"[GPB] Replaced: {old_text!r} → {new_text!r}")
            break

        if not replaced:
            print(f"[GPB] WARN: could not find {old_text!r} in stream")

    print(f"[GPB] Total replaced: {total_replaced}/{len(replacements)}")
    return bytes(new_dec)


def _patch_gpb_pdf(
    pdf_bytes: bytes,
    replacements: list[tuple[str, str]],
    uni_to_cid_list: list[dict[int, str]],
    cid_to_char: dict[int, str],
) -> bytes:
    """Apply replacements to the GPB PDF, update /Length and xref."""
    data = bytearray(pdf_bytes)

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        slen = int(m.group(2))
        len_num_start = m.start(2)
        stream_start = m.end()
        raw = data[stream_start: stream_start + slen]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in dec or b"q\nBT\n" not in dec:
            continue

        new_dec = _patch_gpb_stream(dec, replacements, uni_to_cid_list, cid_to_char)
        if new_dec == dec:
            break

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - slen

        old_len_str = str(slen).encode()
        new_len_str = str(len(new_raw)).encode()
        delta += len(new_len_str) - len(old_len_str)

        # Replace stream bytes
        data = bytearray(bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + slen:]))
        # Update /Length
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        # Update xref
        xref_m = re.search(
            rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
        )
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1): em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

        # Update startxref
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos: pos + len(str(old_pos))] = str(old_pos + delta).encode()

        break  # only one content stream to patch

    return bytes(data)


# ---------------------------------------------------------------------------
# Metadata helpers (JasperReports / iText style)
# ---------------------------------------------------------------------------

def _set_doc_id_jasper(pdf_bytes: bytes) -> bytes:
    """Randomize both /ID hex strings (JasperReports style: ID[0] != ID[1]).

    JasperReports uses two different random hex IDs (unlike Oracle BI Publisher
    which uses ID[0] == ID[1]).  Keeping them different avoids the checker
    recognising the original donor IDs.
    """
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf_bytes)
    if not m:
        return pdf_bytes
    old1, old2 = m.group(1), m.group(2)
    hex_len = len(old1) // 2  # number of bytes → token length in hex chars
    new1 = secrets.token_hex(hex_len).encode()
    # Ensure the two IDs are different
    new2 = secrets.token_hex(hex_len).encode()
    while new2 == new1:
        new2 = secrets.token_hex(hex_len).encode()
    pdf_bytes = pdf_bytes.replace(b"<" + old1 + b">", b"<" + new1 + b">", 1)
    pdf_bytes = pdf_bytes.replace(b"<" + old2 + b">", b"<" + new2 + b">", 1)
    return pdf_bytes


def _update_gpb_dates(pdf_bytes: bytes, operation_date: str, operation_time: str) -> bytes:
    """Update /CreationDate and /ModDate in the PDF /Info dict.

    Format: D:YYYYMMDDHHmmssZ  (UTC, JasperReports convention)
    The operation date/time is given in Moscow time; we convert to UTC.
    """
    parts = operation_date.split(".")
    dd, mm, yyyy = int(parts[0]), int(parts[1]), int(parts[2])
    hh, mi, ss = int(operation_time[:2]), int(operation_time[3:5]), int(operation_time[6:8])
    msk_dt = datetime(yyyy, mm, dd, hh, mi, ss, tzinfo=timezone.utc) - _MSK
    date_str = msk_dt.strftime("D:%Y%m%d%H%M%SZ").encode()
    for field in (b"CreationDate", b"ModDate"):
        m = re.search(rb"/" + field + rb"\s*\(([^)]+)\)", pdf_bytes)
        if m:
            old_val = m.group(1)
            new_val = date_str[:len(old_val)].ljust(len(old_val), b"Z")
            # Replace the full field entry to avoid hitting same-valued fields
            old_entry = m.group(0)
            # Reconstruct new entry preserving whitespace between field name and (
            new_entry = old_entry.replace(b"(" + old_val + b")", b"(" + new_val + b")")
            pdf_bytes = pdf_bytes.replace(old_entry, new_entry, 1)
    return pdf_bytes


# ---------------------------------------------------------------------------
# Donor management
# ---------------------------------------------------------------------------

def _is_genuine_pdf(pdf_path: Path) -> bool:
    """Quick check: all compressed streams reproduce at zlib level 6."""
    try:
        raw = pdf_path.read_bytes()
    except OSError:
        return False
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", raw, re.DOTALL):
        slen = int(m.group(2))
        data = raw[m.end(): m.end() + slen]
        if len(data) < 2 or data[0] != 0x78:
            continue
        try:
            dec = zlib.decompress(data)
            if zlib.compress(dec, 6) != data:
                return False
        except zlib.error:
            return False
    return True


def _find_best_gpb_donor(required_text: str) -> Optional[Path]:
    """Return the GPB donor whose combined CMap best covers required_text."""
    if not GPB_DONORS_DIR.exists():
        return None

    required_cps = set()
    for ch in required_text.replace("\xa0", " "):
        cp = ord(ch)
        required_cps.add(cp)

    best_path: Optional[Path] = None
    best_missing = len(required_cps) + 1

    for pdf_path in sorted(GPB_DONORS_DIR.glob("*.pdf")):
        try:
            cid_to_char, _ = _build_all_cmaps(pdf_path.read_bytes())
        except OSError:
            continue
        if not cid_to_char:
            continue
        # Build unicode coverage from cid_to_char
        covered = {ord(ch) for ch in cid_to_char.values()}
        missing = len(required_cps - covered)
        if missing < best_missing:
            best_missing = missing
            best_path = pdf_path
            if missing == 0:
                break

    return best_path


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_gpb_receipt(
    *,
    amount: int,
    sender_name: str,
    sender_card: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    operation_date: str = "auto",
    operation_time: str = "auto",
    sbp_number: str = "auto",
    donor_path: "str | Path | None" = None,
    output_path: "str | Path | None" = None,
) -> tuple[bytes, str]:
    """Generate a Gazprombank SBP receipt PDF."""
    # Resolve date / time
    if operation_date in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_date = now_msk.strftime("%d.%m.%Y")
    if operation_time in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_time = now_msk.strftime("%H:%M:%S")

    # Format fields
    new_date_time = _fmt_datetime_gpb(operation_date, operation_time)
    new_amount = _fmt_amount_gpb(amount)
    new_commission = "0,00\xa0руб."
    new_total = new_amount
    new_sender_card = _fmt_card_mask(sender_card)
    new_recipient_phone = _fmt_phone_gpb(recipient_phone)
    new_recipient_name = recipient_name.replace(" ", "\xa0")
    new_recipient_bank = recipient_bank.replace(" ", "\xa0")
    new_sender_name = sender_name.replace(" ", "\xa0")

    # SBP ID
    if sbp_number not in ("auto", "", None) and len(sbp_number.strip()) == 32:
        new_sbp_id = sbp_number.strip()
    else:
        new_sbp_id = None  # resolve after loading donor

    all_text = " ".join([
        new_date_time, new_amount, new_sender_card,
        new_recipient_phone, new_recipient_name, new_recipient_bank, new_sender_name,
    ])

    # Find donor
    if donor_path is not None:
        donor_file = Path(donor_path)
    else:
        donor_file = _find_best_gpb_donor(all_text)
        if donor_file is None:
            raise FileNotFoundError(
                f"No GPB donor PDFs found in {GPB_DONORS_DIR}. "
                "Add real Gazprombank SBP receipt PDFs to the GPB/ folder."
            )

    pdf_bytes = donor_file.read_bytes()
    print(f"[GPB] Donor: {donor_file.name}")

    # Build CMap structures
    cid_to_char, uni_to_cid_list = _build_all_cmaps(pdf_bytes)
    print(f"[GPB] CMap entries: {len(cid_to_char)} total, {len(uni_to_cid_list)} fonts")

    # Extract donor fields
    donor_fields = _extract_gpb_fields(pdf_bytes, cid_to_char)
    print(f"[GPB] Donor fields found: {list(donor_fields.keys())}")

    # Resolve SBP ID using donor suffix
    donor_sbp_id = donor_fields.get("sbp_id", "").replace("\xa0", " ").strip()
    if new_sbp_id is None:
        new_sbp_id = _generate_gpb_sbp_id(operation_date, operation_time, donor_sbp_id)
        print(f"[GPB] Generated SBP ID: {new_sbp_id}")

    # Build replacement pairs
    replacements: list[tuple[str, str]] = []

    def _add(old_key: str, new_val: str) -> None:
        old = donor_fields.get(old_key, "")
        old_clean = old.replace("\xa0", " ").strip()
        new_clean = new_val.replace("\xa0", " ").strip()
        if old and old_clean != new_clean:
            trailing_space = old.endswith("\xa0") or old.endswith(" ")
            if trailing_space:
                new_clean += " "
            replacements.append((old_clean, new_clean))

    _add("date_time", new_date_time)
    _add("sender_card", new_sender_card)
    _add("recipient_phone", new_recipient_phone)
    _add("recipient_name", new_recipient_name)
    _add("recipient_bank", new_recipient_bank)
    _add("sender_name", new_sender_name)
    _add("amount", new_amount)
    _add("commission", new_commission)
    _add("total", new_total)
    _add("sbp_id", new_sbp_id)

    print(f"[GPB] Replacements ({len(replacements)}):")
    for old, new in replacements:
        print(f"  {old!r} → {new!r}")

    if replacements:
        pdf_bytes = _patch_gpb_pdf(pdf_bytes, replacements, uni_to_cid_list, cid_to_char)

    # Randomize Document /ID (JasperReports: two different hex strings)
    pdf_bytes = _set_doc_id_jasper(pdf_bytes)
    # Update CreationDate / ModDate to operation datetime
    pdf_bytes = _update_gpb_dates(pdf_bytes, operation_date, operation_time)

    canonical_filename = f"receipt_{operation_date}.pdf"

    if output_path is not None:
        out = Path(output_path)
        if out.is_dir():
            out = out / canonical_filename
        out.write_bytes(pdf_bytes)
        print(f"[GPB] Written: {out} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes, canonical_filename


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Газпромбанк СБП чек-генератор")
    parser.add_argument("--amount", type=int, required=True)
    parser.add_argument("--sender-name", required=True)
    parser.add_argument("--sender-card", required=True)
    parser.add_argument("--recipient-name", required=True)
    parser.add_argument("--recipient-phone", required=True)
    parser.add_argument("--recipient-bank", required=True)
    parser.add_argument("--date", default="auto")
    parser.add_argument("--time", default="auto")
    parser.add_argument("--sbp-number", default="auto")
    parser.add_argument("--output", "-o", default=".")
    parser.add_argument("--donor")
    args = parser.parse_args()

    generate_gpb_receipt(
        amount=args.amount,
        sender_name=args.sender_name,
        sender_card=args.sender_card,
        recipient_name=args.recipient_name,
        recipient_phone=args.recipient_phone,
        recipient_bank=args.recipient_bank,
        operation_date=args.date,
        operation_time=args.time,
        sbp_number=args.sbp_number,
        donor_path=args.donor,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
