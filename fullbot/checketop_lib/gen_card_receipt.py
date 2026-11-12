#!/usr/bin/env python3
"""Alfa-Bank card-to-card transfer receipt generator.

Generates a valid Alfa-Bank 'Квитанция о переводе с карты на карту' PDF
with arbitrary input data.  No SBP ID required.

Fields supported:
    amount:           Transfer amount in RUB
    sender_card:      Sender card number mask or last 4 digits
    recipient_card:   Recipient card number mask or last 4 digits
    operation_date:   DD.MM.YYYY or 'auto'
    operation_time:   HH:MM:SS or 'auto'
    commission:       Commission in RUB, default 0

Donor PDFs must be placed in the карта_на_карту/ subfolder.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import string
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.resolve()
CARD_DONORS_DIR = ROOT / "карта_на_карту"

_MSK = timedelta(hours=3)

# Auth code chars that are always present in the CMap (or can be identity-mapped)
_AUTH_CHARS = string.digits + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_rub_card(amount: int) -> str:
    """'10 RUR ' — same as Alfa SBP format with trailing NBSP."""
    s = f"{amount:,}".replace(",", "\xa0")
    return f"{s}\xa0RUR\xa0"


def _fmt_commission_card(amount_kopecks: int) -> str:
    """'49,20 RUR' with kopecks only if non-zero, or '49 RUR' for whole rubles, '0 RUR' for zero."""
    if amount_kopecks == 0:
        return "0\xa0RUR\xa0"
    rubles = amount_kopecks // 100
    kopecks = amount_kopecks % 100
    if kopecks == 0:
        return f"{rubles}\xa0RUR\xa0"
    return f"{rubles},{kopecks:02d}\xa0RUR\xa0"


def _fmt_datetime_card(date_str: str, time_str: str) -> str:
    """'05.03.2026 23:33:27 мск' — card receipt includes seconds."""
    return f"{date_str}\xa0{time_str}\xa0мск"


def _fmt_formed_card(date_str: str, time_str: str) -> str:
    """'05.03.2026 23:33 мск' — no seconds."""
    return f"{date_str}\xa0{time_str[:5]}\xa0мск"


def _fmt_card_mask(card: str) -> str:
    """Convert last 4 digits or full mask to '**** **** **** XXXX' style.

    Accepted inputs:
      - '9136'               → '220432******9136'  (uses random BIN)
      - '220432******9136'   → '220432******9136'
      - '2204320000009136'   → '220432******9136'
    """
    card = card.strip().replace(" ", "").replace("-", "")
    # Already a mask like 220432******9136
    if "*" in card:
        return card.replace("*", "").rjust(4, "0")[:6] + "******" + card[-4:]
    # Full 16-digit number
    if len(card) == 16 and card.isdigit():
        return card[:6] + "******" + card[-4:]
    # Just last 4 digits — pick a random realistic Alfa-Bank BIN
    if len(card) == 4 and card.isdigit():
        bins = ["220015", "220016", "220017", "220030", "220032", "220040"]
        return random.choice(bins) + "******" + card
    # Fallback
    return "220015******" + card[-4:].zfill(4)


def _generate_auth_code() -> str:
    """Generate a random 6-char alphanumeric auth code (card terminal style)."""
    return "".join(random.choices(string.digits + string.ascii_uppercase, k=6))


def _generate_operation_id_card(date_str: str, time_str: str = "00:00:00") -> str:
    """Generate card operation ID: Z09{DDMMYY}{7-digit-seq}.

    Based on observed Alfa-Bank card transfer IDs:
    Z090503260131536 → Z09 + 050326 (05.03.26) + 0131536

    The 7-digit sequence is a daily counter similar to SBP.
    """
    # Normalize separators and split safely
    _normalized = re.sub(r"[:/\-]", ".", date_str.strip())
    parts = _normalized.split(".")
    if len(parts) < 3:
        parts = ["01", "01", "2026"]
    try:
        dd = f"{int(parts[0]):02d}"
        mm = f"{int(parts[1]):02d}"
        yr = parts[2] if parts[2] else "2026"
        yy = yr[-2:] if len(yr) >= 2 else yr.zfill(2)
    except (ValueError, IndexError):
        dd, mm, yy = "01", "01", "26"

    try:
        hh, mi = int(time_str[:2]), int(time_str[3:5])
    except Exception:
        hh, mi = 12, 0
    minutes = hh * 60 + mi

    # Rough daily counter based on time
    # ~100-200k operations/day for card transfers
    base = int(minutes / 1440 * 180_000) + 10_000
    noise = random.uniform(0.8, 1.2)
    seq = max(1000, min(1_999_999, int(base * noise)))
    return f"Z09{dd}{mm}{yy}{seq:07d}"


# ---------------------------------------------------------------------------
# Donor management
# ---------------------------------------------------------------------------

def _cmap_from_pdf(pdf_bytes: bytes) -> dict[int, str]:
    """Extract the first ToUnicode CMap from a PDF."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        raw = pdf_bytes[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
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
                    uni_to_cid[u + i] = f"{s + i:04X}"
            if uni_to_cid:
                return uni_to_cid
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
            if uni_to_cid:
                return uni_to_cid
    return {}


def _chars_missing(cmap: dict[int, str], text: str) -> set[str]:
    """Return characters in text that are not covered by the CMap."""
    return {ch for ch in text if ch != "\xa0" and ord(ch) not in cmap}


def _strip_unused_cmap_entries(pdf_bytes: bytes) -> bytes:
    """Remove CMap bfchar entries whose CIDs are not referenced in any Tj command.

    Oracle BI Publisher generates a perfectly minimal CMap.  After donor-patching
    some characters may no longer be used; this function removes them so the CMap
    stays minimal and the unused-entry checker passes.
    """
    # Collect every CID actually referenced in Tj commands
    used_cids: set[int] = set()
    stream_starts = [m.end() for m in re.finditer(rb"stream\r\n", pdf_bytes)]
    for sstart in stream_starts:
        lm = None
        for m in re.finditer(rb"/Length\s+(\d+)", pdf_bytes[max(0, sstart - 300):sstart]):
            lm = m
        if not lm:
            continue
        slen = int(lm.group(1))
        chunk = pdf_bytes[sstart: sstart + slen]
        try:
            dec = zlib.decompress(chunk)
        except zlib.error:
            continue
        if b"Tj" not in dec or b"BT" not in dec:
            continue
        for tj in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", dec):
            hs = tj.group(1).decode()
            for i in range(0, len(hs), 4):
                used_cids.add(int(hs[i: i + 4], 16))

    if not used_cids:
        return pdf_bytes

    # Find and patch the CMap stream (contains beginbfchar)
    pdf_ba = bytearray(pdf_bytes)
    for sstart in stream_starts:
        lm = None
        for m in re.finditer(rb"/Length\s+(\d+)", pdf_bytes[max(0, sstart - 300):sstart]):
            lm = m
        if not lm:
            continue
        len_match_abs = sstart - 300 + max(0, sstart - 300 - (sstart - 300)) + lm.start(1)
        # Locate absolute position of /Length value in the original bytes
        search_start = max(0, sstart - 300)
        abs_lm = None
        for m in re.finditer(rb"/Length\s+(\d+)", pdf_bytes[search_start:sstart]):
            abs_lm = m
        if not abs_lm:
            continue
        abs_len_pos = search_start + abs_lm.start(1)

        slen = int(abs_lm.group(1))
        chunk = pdf_bytes[sstart: sstart + slen]
        try:
            dec = zlib.decompress(chunk)
        except zlib.error:
            continue
        if b"beginbfchar" not in dec:
            continue

        # Parse bfchar section
        bfc_m = re.search(rb"(\d+)\s+beginbfchar(.*?)endbfchar", dec, re.DOTALL)
        if not bfc_m:
            continue

        old_count = int(bfc_m.group(1))
        entries_raw = bfc_m.group(2)
        # Keep only entries whose CID is in used_cids (plus CID 0 = .notdef)
        kept = []
        for em in re.finditer(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4})>", entries_raw):
            cid = int(em.group(1), 16)
            if cid == 0 or cid in used_cids:
                kept.append(em.group(0))

        if len(kept) == old_count:
            continue  # nothing to strip

        new_entries = b"\r\n" + b"\r\n".join(kept) + b"\r\n"
        new_section = str(len(kept)).encode() + b" beginbfchar" + new_entries + b"endbfchar"
        new_dec = dec[: bfc_m.start()] + new_section + dec[bfc_m.end():]

        new_raw = zlib.compress(new_dec, 6)
        delta = len(new_raw) - slen

        # Patch Length value
        old_len_str = str(slen).encode()
        new_len_str = str(len(new_raw)).encode()
        pdf_ba[abs_len_pos: abs_len_pos + len(old_len_str)] = new_len_str
        len_delta_chars = len(new_len_str) - len(old_len_str)

        # Replace stream content
        real_sstart = sstart + len_delta_chars
        pdf_ba = bytearray(bytes(pdf_ba)[:real_sstart] + new_raw + bytes(pdf_ba)[real_sstart + slen:])

        # Update xref offsets
        total_delta = delta + len_delta_chars
        xref_m = re.search(rb"xref\r\n\d+ \d+\r\n((?:\d{10} \d{5} [nf]\s*\r?\n)+)", bytes(pdf_ba))
        if xref_m and total_delta != 0:
            entries = bytearray(xref_m.group(1))
            for em in re.finditer(rb"(\d{10})( \d{5} [nf]\s*\r?\n)", entries):
                off = int(em.group(1))
                if off > sstart:
                    entries[em.start(1): em.start(1) + 10] = f"{off + total_delta:010d}".encode()
            pdf_ba[xref_m.start(1): xref_m.end(1)] = bytes(entries)

        # Update startxref
        sxref_m = re.search(rb"startxref\r\n(\d+)\r\n", bytes(pdf_ba))
        if sxref_m and total_delta != 0:
            old_sxref = int(sxref_m.group(1))
            if old_sxref > sstart:
                new_sxref = str(old_sxref + total_delta).encode()
                pdf_ba[sxref_m.start(1): sxref_m.end(1)] = new_sxref

        print(f"[CARD] CMap stripped: {old_count} -> {len(kept)} entries")
        return bytes(pdf_ba)

    return pdf_bytes


def _set_doc_id_equal(pdf_bytes: bytes) -> bytes:
    """Randomize Document /ID with ID[0] == ID[1] (Oracle BI Publisher style)."""
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf_bytes)
    if not m:
        return pdf_bytes
    old1, old2 = m.group(1), m.group(2)
    new_hex = secrets.token_hex(len(old1) // 2).lower().encode()
    pdf_bytes = pdf_bytes.replace(b"<" + old1 + b">", b"<" + new_hex + b">", 1)
    pdf_bytes = pdf_bytes.replace(b"<" + old2 + b">", b"<" + new_hex + b">", 1)
    return pdf_bytes


def _decode_texts_from_pdf(pdf_bytes: bytes, cmap: dict[int, str]) -> list[str]:
    """Decode all Tj strings from the content stream."""
    cid_to_chr = {cid: chr(uni) for uni, cid in cmap.items()}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        slen = int(m.group(2))
        start = m.end()
        raw = pdf_bytes[start: start + slen]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tj" not in dec:
            continue
        texts: list[str] = []
        for tj in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", dec):
            hexdata = tj.group(1).decode().upper()
            chars = []
            for i in range(0, len(hexdata), 4):
                code = hexdata[i: i + 4]
                chars.append(cid_to_chr.get(code, "?"))
            texts.append("".join(chars))
        return texts
    return []


def _extract_card_fields(pdf_bytes: bytes, cmap: dict[int, str]) -> dict[str, str]:
    """Extract variable fields from a card transfer receipt."""
    texts = _decode_texts_from_pdf(pdf_bytes, cmap)
    labels = [t.replace("\xa0", " ").strip() for t in texts]

    def _next_val(i: int) -> str:
        j = i + 1
        while j < len(labels) and labels[j] == "":
            j += 1
        return texts[j] if j < len(texts) else ""

    fields: dict[str, str] = {}
    for i, lbl in enumerate(labels):
        # "Сформирована" date field
        if re.search(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+мск", lbl) and "date_formed" not in fields:
            fields["date_formed"] = texts[i]
        elif lbl == "Сумма перевода":
            fields["amount"] = _next_val(i)
        elif lbl == "Комиссия":
            fields["commission"] = _next_val(i)
        elif lbl == "Номер карты отправителя":
            fields["sender_card"] = _next_val(i)
        elif lbl == "Номер карты получателя":
            fields["recipient_card"] = _next_val(i)
        elif lbl == "Дата и время перевода":
            fields["date_time"] = _next_val(i)
        elif lbl == "Код авторизации":
            fields["auth_code"] = _next_val(i)
        elif lbl == "Код терминала":
            fields["terminal_code"] = _next_val(i)
        elif lbl == "Номер операции в банке":
            fields["operation_id"] = _next_val(i)
    return fields


def _find_best_card_donor(required_text: str) -> Optional[Path]:
    """Return the card donor whose CMap best covers required_text."""
    if not CARD_DONORS_DIR.exists():
        return None
    required_cps = set()
    for ch in required_text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        required_cps.add(cp)

    best_path: Optional[Path] = None
    best_missing = len(required_cps) + 1

    candidates = sorted(CARD_DONORS_DIR.glob("*.pdf"))
    if not candidates:
        return None

    for pdf_path in candidates:
        try:
            cmap = _cmap_from_pdf(pdf_path.read_bytes())
        except OSError:
            continue
        if not cmap:
            continue
        missing = len(required_cps - set(cmap.keys()))
        if missing < best_missing:
            best_missing = missing
            best_path = pdf_path
            if missing == 0:
                break

    return best_path


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_card_receipt(
    *,
    amount: int,
    sender_card: str = "9999",
    recipient_card: str = "1234",
    operation_date: str = "auto",
    operation_time: str = "auto",
    commission: "int | float" = 0,
    formed_time: "str | None" = None,
    donor_path: "str | Path | None" = None,
    output_path: "str | Path | None" = None,
) -> tuple[bytes, str]:
    """Generate an Alfa-Bank card-to-card transfer receipt PDF.

    Parameters
    ----------
    amount:           Transfer amount in RUB.
    sender_card:      Sender card last 4 digits or full mask.
    recipient_card:   Recipient card last 4 digits or full mask.
    operation_date:   'DD.MM.YYYY' or 'auto'.
    operation_time:   'HH:MM:SS' or 'auto'.
    commission:       Commission in RUB (default 0).
    donor_path:       Explicit donor PDF. Auto-selected if None.
    output_path:      Output file path or None.

    Returns
    -------
    (pdf_bytes, canonical_filename)
    """
    # --- Resolve date / time ---
    if operation_date in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_date = now_msk.strftime("%d.%m.%Y")
    else:
        # Normalize separators: user may enter "24:05:2026" or "24/05/2026"
        operation_date = re.sub(r"[:/]", ".", operation_date.strip())
        # Fix doubled separator e.g. "24.025.2026" → try to parse and reformat
        _dp = operation_date.split(".")
        if len(_dp) == 3:
            try:
                _dd, _mm, _yyyy = int(_dp[0]), int(_dp[1]), int(_dp[2])
                if _yyyy < 100:
                    _yyyy += 2000
                operation_date = f"{_dd:02d}.{_mm:02d}.{_yyyy:04d}"
            except (ValueError, IndexError):
                operation_date = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        elif len(_dp) < 3:
            operation_date = (datetime.now(timezone.utc) + _MSK).strftime("%d.%m.%Y")
    if operation_time in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_time = now_msk.strftime("%H:%M:%S")
    else:
        # Normalize time: "22.53.43" → "22:53:43"
        operation_time = re.sub(r"[./]", ":", operation_time.strip())
        _tp = operation_time.split(":")
        if len(_tp) >= 2:
            try:
                operation_time = f"{int(_tp[0]):02d}:{int(_tp[1]):02d}:{int(_tp[2]) if len(_tp) > 2 else 0:02d}"
            except (ValueError, IndexError):
                pass

    # --- Format fields ---
    new_amount = _fmt_rub_card(amount)
    new_commission = _fmt_commission_card(int(round(commission * 100)))
    new_sender_card = _fmt_card_mask(sender_card)
    new_recipient_card = _fmt_card_mask(recipient_card)
    new_date_time = _fmt_datetime_card(operation_date, operation_time)
    if formed_time and formed_time not in ("auto", "авто", "-", ""):
        _ft = formed_time.strip()
        if re.match(r"^\d{1,2}:\d{2}$", _ft):
            _ft_date = operation_date
            _hh, _mi = _ft.split(":")
            _ft = f"{_ft_date}\xa0{int(_hh):02d}:{_mi}\xa0мск"
        elif re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}$", _ft):
            _parts = _ft.split()
            _ft = f"{_parts[0]}\xa0{_parts[1]}\xa0мск"
        new_date_formed = _ft
    else:
        new_date_formed = _fmt_formed_card(operation_date, operation_time)
    new_auth_code = _generate_auth_code()
    new_operation_id = _generate_operation_id_card(operation_date, operation_time)

    # --- Collect all text ---
    all_text = "".join([
        new_amount, new_commission,
        new_sender_card, new_recipient_card,
        new_date_time, new_date_formed,
        new_auth_code, new_operation_id,
    ])

    # --- Find donor ---
    if donor_path is not None:
        donor_file = Path(donor_path)
    else:
        donor_file = _find_best_card_donor(all_text)
        if donor_file is None:
            raise FileNotFoundError(
                f"No card transfer donor PDFs found in {CARD_DONORS_DIR}. "
                "Add real Alfa-Bank card transfer receipt PDFs to карта_на_карту/."
            )

    pdf_bytes = donor_file.read_bytes()
    print(f"[CARD] Donor: {donor_file.name}")

    # --- Font surgery if needed (e.g. auth code chars not in donor font) ---
    cmap = _cmap_from_pdf(pdf_bytes)
    missing = _chars_missing(cmap, all_text)
    if missing:
        print(f"[CARD] Missing chars: {sorted(missing)} — running font surgery...")
        from font_extend import extend_font_in_pdf
        pdf_bytes, cmap = extend_font_in_pdf(pdf_bytes, all_text)
        still_missing = _chars_missing(cmap, all_text)
        if still_missing:
            print(f"[CARD] WARNING: font surgery could not add: {sorted(still_missing)}")
        else:
            print("[CARD] Font surgery complete.")
    else:
        print("[CARD] All characters already in font.")

    # --- Extract donor fields ---
    donor_fields = _extract_card_fields(pdf_bytes, cmap)
    print(f"[CARD] Donor fields: {list(donor_fields.keys())}")

    # --- Build replacement pairs ---
    replacements: list[tuple[str, str]] = []

    def _add(old_key: str, new_val: str) -> None:
        old = donor_fields.get(old_key, "")
        old_clean = old.replace("\xa0", " ").strip()
        new_clean = new_val.replace("\xa0", " ").strip()
        if old and old_clean != new_clean:
            if old.endswith("\xa0"):
                new_clean += "\xa0"  # preserve trailing NBSP (genuine Oracle pattern)
            replacements.append((old_clean, new_clean))

    _add("amount", new_amount)
    _add("commission", new_commission)
    _add("sender_card", new_sender_card)
    _add("recipient_card", new_recipient_card)
    _add("date_time", new_date_time)
    _add("date_formed", new_date_formed)
    _add("auth_code", new_auth_code)
    _add("operation_id", new_operation_id)

    print(f"[CARD] Replacements ({len(replacements)}):")
    for old, new in replacements:
        print(f"  {old!r} -> {new!r}")

    if replacements:
        from cid_patch_amount import patch_replacements
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            in_path = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            out_path_tmp = Path(f.name)

        try:
            ok = patch_replacements(in_path, out_path_tmp, replacements)
            if not ok:
                print("[CARD] WARNING: Some replacements failed, using donor as-is")
                pdf_bytes = in_path.read_bytes()
            else:
                pdf_bytes = out_path_tmp.read_bytes()
        finally:
            for p in (in_path, out_path_tmp):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # --- Randomize Document /ID (Oracle BI Publisher: ID[0] == ID[1]) ---
    pdf_bytes = _set_doc_id_equal(pdf_bytes)

    # --- Post-generation validation ---
    # We check every editable field (not just amount + 2 cards) so the bot
    # refuses to send a card receipt that doesn't really contain what the
    # user asked for. Structural errors (xref / length / /ID / producer)
    # also trigger a hard failure — they always indicate a broken PDF.
    from pdf_validate import validate_pdf
    expected = [
        new_amount.replace("\xa0", " ").strip(),
        new_commission.replace("\xa0", " ").strip(),
        new_sender_card.replace("\xa0", " ").strip(),
        new_recipient_card.replace("\xa0", " ").strip(),
        new_date_time.replace("\xa0", " ").strip(),
        new_date_formed.replace("\xa0", " ").strip(),
        new_auth_code,
        new_operation_id,
    ]
    result = validate_pdf(pdf_bytes, expected)
    for line in result.info:
        print(f"[CARD VALIDATE] {line}")
    for line in result.warnings:
        print(f"[CARD VALIDATE WARN] {line}")
    if result.errors:
        for line in result.errors:
            print(f"[CARD VALIDATE ERROR] {line}")
        raise RuntimeError(
            "Generated card PDF is internally inconsistent:\n  - "
            + "\n  - ".join(result.errors)
        )

    # --- Strip unused CMap entries ---
    # Genuine Oracle BI Publisher PDFs have a minimal CMap with exactly the
    # characters used in content.  After donor-patching some may be unused.
    pdf_bytes = _strip_unused_cmap_entries(pdf_bytes)

    # --- Canonical filename ---
    # Genuine Alfa-Bank card filenames: AM_{13-digit-ms}.pdf
    # The epoch is 225-301 seconds BEFORE date_formed (= operation time rounded to minutes).
    # date_formed strips seconds, so: formed_ts = op_ts - seconds_part
    # filename_ts must satisfy: formed_ts - filename_ts in [180, 360]
    # => filename_ts = formed_ts - offset  where offset in [225, 295]
    try:
        if formed_time and formed_time not in ("auto", "авто", "-", ""):
            # Parse explicit formed_time for filename timestamp
            _ft2 = formed_time.strip()
            if re.match(r"^\d{1,2}:\d{2}$", _ft2):
                _ft2 = f"{operation_date} {_ft2}"
            _fm = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})", _ft2)
            if _fm:
                _dd2, _mm2, _yyyy2, _hh2, _mi2 = (int(x) for x in _fm.groups())
                formed_utc = datetime(_yyyy2, _mm2, _dd2, _hh2, _mi2, 0, tzinfo=timezone.utc) - _MSK
            else:
                dd, mm, yyyy = (int(x) for x in operation_date.split("."))
                hh, mi = int(operation_time[:2]), int(operation_time[3:5])
                formed_utc = datetime(yyyy, mm, dd, hh, mi, 0, tzinfo=timezone.utc) - _MSK
        else:
            dd, mm, yyyy = (int(x) for x in operation_date.split("."))
            hh, mi = int(operation_time[:2]), int(operation_time[3:5])
            formed_utc = datetime(yyyy, mm, dd, hh, mi, 0, tzinfo=timezone.utc) - _MSK
        offset_sec = random.randint(225, 295)
        ts_ms = int((formed_utc.timestamp() - offset_sec) * 1000) + random.randint(0, 999)
    except Exception:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 250_000
    canonical_filename = f"AM_{ts_ms}.pdf"

    if output_path is not None:
        out = Path(output_path)
        if out.is_dir():
            out = out / canonical_filename
        out.write_bytes(pdf_bytes)
        print(f"[CARD] Written: {out} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes, canonical_filename


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alfa-Bank карта-на-карту чек-генератор")
    parser.add_argument("--amount", type=int, required=True, help="Сумма (руб)")
    parser.add_argument("--sender-card", default="9999", help="Карта отправителя (последние 4 цифры)")
    parser.add_argument("--recipient-card", default="1234", help="Карта получателя (последние 4 цифры)")
    parser.add_argument("--date", default="auto", help="DD.MM.YYYY или auto")
    parser.add_argument("--time", default="auto", help="HH:MM:SS или auto")
    parser.add_argument("--commission", type=int, default=0, help="Комиссия (руб)")
    parser.add_argument("--output", "-o", help="Выходной файл или папка")
    parser.add_argument("--donor", help="Конкретный донор PDF")
    args = parser.parse_args()

    pdf_bytes, filename = generate_card_receipt(
        amount=args.amount,
        sender_card=args.sender_card,
        recipient_card=args.recipient_card,
        operation_date=args.date,
        operation_time=args.time,
        commission=args.commission,
        donor_path=args.donor,
        output_path=args.output or ".",
    )
    if args.output is None:
        out = Path(filename)
        out.write_bytes(pdf_bytes)
        print(f"[CARD] Saved: {out}")


if __name__ == "__main__":
    main()
