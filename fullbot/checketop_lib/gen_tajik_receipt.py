#!/usr/bin/env python3
"""Tajikistan transgran receipt generator.

Generates a valid Alfa-Bank PDF receipt for cross-border transfers
to Tajikistan (RUR → TJS) with arbitrary input data.

Supported /check command syntax:
    amount: 3000
    sender_name: Kirill S.           (ignored — not in receipt)
    recipient_phone: +992938999964
    recipient_name: Bobomurodov Kh.
    credited_currency: TJS
    amount_int: 354                  (credited amount in TJS)
    operation_date: 25.09.2025       (or "auto")
    operation_time: 13:45:12         (or "auto")
    receipt_number: auto             (auto-generated C-format op ID)

Pipeline:
    1. Calculate all fields (course, commission, amount_with_commission, etc.)
    2. Find the best Tajikistan donor PDF.
    3. Extend the donor's font if any characters are missing.
    4. Apply CID replacements for all fields.
    5. Assign a new Document /ID (ID[0] == ID[1], Oracle BI Publisher style).
    6. Return the final PDF bytes.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import sys
import zlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.resolve()
TAJIK_DONORS_DIR = ROOT / "таджикистан"

# Москвa timezone offset
_MSK = timedelta(hours=3)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_rub(amount: int) -> str:
    """Format integer RUB amount as Oracle BI Publisher style: '3\xa0000\xa0RUR\xa0'."""
    s = f"{amount:,}".replace(",", "\xa0")
    return f"{s}\xa0RUR\xa0"


def _fmt_tjs(value: Decimal, currency: str = "TJS") -> str:
    """Format credited foreign currency amount: '354,00\xa0TJS\xa0' or '2,50\xa0TJS\xa0'.

    Oracle BI Publisher always includes 2 decimal places for foreign currency amounts,
    even for whole numbers (e.g. '354,00 TJS', never '354 TJS').
    """
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cents = int((q * 100).to_integral_value(rounding=ROUND_HALF_UP))
    whole = cents // 100
    frac = cents % 100
    parts: list[str] = []
    s = str(whole)
    while len(s) > 3:
        parts.insert(0, s[-3:])
        s = s[:-3]
    if s:
        parts.insert(0, s)
    int_part = "\xa0".join(parts) if len(parts) > 1 else parts[0] if parts else "0"
    # Always 2 decimal places — Oracle BI Publisher format for all currency amounts
    return f"{int_part},{frac:02d}\xa0{currency}\xa0"


def _fmt_course(amount_rub: int, amount_fx: Decimal, currency: str = "TJS") -> str:
    """Compute and format exchange rate: '1\xa0RUR\xa0=\xa00.118\xa0TJS\xa0'."""
    if amount_rub <= 0:
        raise ValueError("amount_rub must be > 0")
    rate = (amount_fx / Decimal(amount_rub)).quantize(
        Decimal("0.00001"), rounding=ROUND_HALF_UP
    )
    # Drop trailing zeros but keep at least 3 decimal places
    rate_s = f"{rate:.5f}".rstrip("0")
    if "." in rate_s:
        parts = rate_s.split(".")
        parts[1] = parts[1].ljust(3, "0")  # minimum 3 decimals
        rate_s = ".".join(parts)
    return f"1\xa0RUR\xa0=\xa0{rate_s}\xa0{currency}\xa0"


def _fmt_datetime(date_str: str, time_str: str) -> str:
    """Format: '25.09.2025\xa013:45:12\xa0мск'."""
    return f"{date_str}\xa0{time_str}\xa0мск"


def _fmt_formed(date_str: str, time_str: str, offset_minutes: int = 0) -> str:
    """Format: '25.09.2025\xa013:45\xa0мск' (no seconds).

    offset_minutes: add this many minutes to the time (for "Сформирована" timestamp
    which should be realistically after the transfer time).
    """
    if offset_minutes == 0:
        hm = time_str[:5]
        return f"{date_str}\xa0{hm}\xa0мск"

    # Parse and shift time
    h, m = int(time_str[:2]), int(time_str[3:5])
    total_minutes = h * 60 + m + offset_minutes
    # Handle day overflow (rare but keep it clean)
    total_minutes %= 1440
    new_h = total_minutes // 60
    new_m = total_minutes % 60
    return f"{date_str}\xa0{new_h:02d}:{new_m:02d}\xa0мск"


def _generate_operation_id(date_str: str) -> str:
    """Generate a plausible C-format operation ID for the given date.

    Format: C{type:02d}{DD}{MM}{YY}{seq:07d}
    Example: C821302260067521  (type=82, DD=13, MM=02, YY=26, seq=0067521)

    Type code 82 is used for Tajikistan transgran transfers (cross-border by phone number).
    Sequence numbers in real receipts range from ~1 to ~200000 (daily operation counter).
    """
    parts = date_str.split(".")
    dd, mm = parts[0], parts[1]
    yy = parts[2][2:] if len(parts[2]) == 4 else parts[2]
    # Type code 82 = transgran transfer by phone number (Alfa-Bank classification)
    type_code = "82"
    # Plausible daily sequence: max ~200K operations per day at a large bank
    seq = random.randint(1, 199999)
    return f"C{type_code}{dd}{mm}{yy}{seq:07d}"


# ---------------------------------------------------------------------------
# Donor selection
# ---------------------------------------------------------------------------

def _cmap_from_pdf(pdf_bytes: bytes) -> dict[int, str]:
    """Extract ToUnicode CMap from PDF bytes."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        raw = pdf_bytes[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        uni_to_cid: dict[int, str] = {}
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
            if uni_to_cid:
                return uni_to_cid
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
    return {}


def _chars_missing(cmap: dict[int, str], text: str) -> set[str]:
    missing: set[str] = set()
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp not in cmap and cp != 0xFFFF:
            missing.add(ch)
    return missing


def _find_best_donor(all_new_text: str) -> Optional[Path]:
    """Find the donor with maximum native character overlap, excluding patched ones."""
    if not TAJIK_DONORS_DIR.exists():
        return None

    required = set()
    for ch in all_new_text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp != 0xFFFF:
            required.add(cp)

    best_path: Optional[Path] = None
    best_covered = -1

    for pdf_path in sorted(TAJIK_DONORS_DIR.glob("*.pdf")):
        name = pdf_path.name.lower()
        if "extended" in name or "patched" in name or name.endswith("_1.pdf"):
            continue
        try:
            raw = pdf_path.read_bytes()
        except OSError:
            continue
        cmap = _cmap_from_pdf(raw)
        if not cmap:
            continue
        covered = len(required & set(cmap.keys()))
        if covered > best_covered:
            best_covered = covered
            best_path = pdf_path

    return best_path


# ---------------------------------------------------------------------------
# Field extraction from donor
# ---------------------------------------------------------------------------

def _decode_stream_texts(pdf_bytes: bytes, cmap: dict[int, str]) -> list[str]:
    """Decode all <hex> Tj strings from the first BT content stream."""
    cid_to_chr: dict[str, str] = {cid: chr(uni) for uni, cid in cmap.items()}
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


def _extract_donor_fields(pdf_bytes: bytes, cmap: dict[int, str]) -> dict[str, str]:
    """Extract labelled field values from donor PDF."""
    texts = _decode_stream_texts(pdf_bytes, cmap)
    labels = [t.replace("\xa0", " ").strip() for t in texts]

    def _next_val(i: int) -> str:
        j = i + 1
        while j < len(labels) and labels[j] == "":
            j += 1
        return texts[j] if j < len(texts) else ""

    fields: dict[str, str] = {}
    for i, lbl in enumerate(labels):
        if re.search(r"\d+\.\d+\.\d{4}\s+\d{2}:\d{2}\s+мск", lbl):
            fields["date_formed"] = texts[i]
        elif lbl == "Сумма перевода":
            fields["amount"] = _next_val(i)
        elif lbl == "Комиссия":
            fields["commission"] = _next_val(i)
        elif lbl == "Списано с учётом комиссии":
            fields["amount_with_commission"] = _next_val(i)
        elif lbl == "Курс конвертации":
            fields["course"] = _next_val(i)
        elif lbl == "Сумма зачисления банком получателя":
            fields["amount_credited"] = _next_val(i)
        elif lbl == "Дата и время перевода":
            fields["date_time"] = _next_val(i)
        elif lbl == "Получатель":
            fields["recipient"] = _next_val(i)
        elif "телефона получателя" in lbl:
            fields["phone"] = _next_val(i)
        elif "Счёт списания" in lbl or "Счет списания" in lbl:
            fields["account"] = _next_val(i)
        elif "операции" in lbl.lower():
            fields["operation_id"] = _next_val(i)

    return fields


# ---------------------------------------------------------------------------
# Document /ID helper
# ---------------------------------------------------------------------------

def _set_doc_id_equal(pdf_bytes: bytes) -> bytes:
    """Replace /ID with a fresh random pair where ID[0] == ID[1]."""
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf_bytes)
    if not m:
        return pdf_bytes
    old1, old2 = m.group(1), m.group(2)
    target_len = len(old1)
    new_hex = secrets.token_hex(target_len // 2).lower().encode()
    pdf_bytes = pdf_bytes.replace(b"<" + old1 + b">", b"<" + new_hex + b">", 1)
    pdf_bytes = pdf_bytes.replace(b"<" + old2 + b">", b"<" + new_hex + b">", 1)
    return pdf_bytes


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_tajik_receipt(
    *,
    amount: int,
    recipient_name: str,
    recipient_phone: str,
    credited_currency: str = "TJS",
    amount_credited: int | None = None,
    operation_date: str = "auto",
    operation_time: str = "auto",
    receipt_number: str = "auto",
    commission: int = 0,
    account: str | None = None,
    donor_path: str | Path | None = None,
    output_path: str | Path | None = None,
    glyph_source: str | None = None,
) -> bytes:
    """Generate a Tajikistan transgran receipt PDF with the given parameters.

    Parameters
    ----------
    amount:             Transfer amount in RUB (integer).
    recipient_name:     Recipient's name (any Latin or Cyrillic characters).
    recipient_phone:    Recipient's phone, e.g. '+992938999964'.
    credited_currency:  Foreign currency code, default 'TJS'.
    amount_credited:    Credited amount in foreign currency (integer, e.g. 354).
                        If None, not replaced.
    operation_date:     'DD.MM.YYYY' or 'auto' (uses current Moscow date).
    operation_time:     'HH:MM:SS' or 'auto' (uses current Moscow time).
    receipt_number:     Operation ID override, or 'auto' to generate.
    commission:         Commission in RUB, default 0.
    account:            Sender account (20-digit string). Keeps donor value if None.
    donor_path:         Explicit donor PDF path. Auto-selected if None.
    output_path:        Path to write the output PDF. If None, returns bytes only.
    glyph_source:       Path to full Tahoma TTF for font surgery. Uses system default.

    Returns
    -------
    PDF bytes of the generated receipt.
    """
    # --- Resolve date / time ---
    if operation_date == "auto" or not operation_date:
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_date = now_msk.strftime("%d.%m.%Y")
    if operation_time == "auto" or not operation_time:
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_time = now_msk.strftime("%H:%M:%S")

    # --- Derived fields ---
    amount_with_commission = amount + commission

    if amount_credited is not None:
        credited_decimal = Decimal(amount_credited)
    else:
        credited_decimal = None

    new_date_time = _fmt_datetime(operation_date, operation_time)
    # "Сформирована" = PDF generation time, which is realistically 3-15 minutes
    # after the actual transfer time (user navigates to download the receipt)
    formed_offset = random.randint(3, 15)
    new_date_formed = _fmt_formed(operation_date, operation_time, offset_minutes=formed_offset)
    new_amount = _fmt_rub(amount)
    new_commission = _fmt_rub(commission)
    new_amount_with_commission = _fmt_rub(amount_with_commission)

    if credited_decimal is not None:
        new_amount_credited = _fmt_tjs(credited_decimal, credited_currency.upper())
        new_course = _fmt_course(amount, credited_decimal, credited_currency.upper())
    else:
        new_amount_credited = None
        new_course = None

    # receipt_number → operation_id
    if receipt_number in ("auto", "", None):
        new_operation_id = _generate_operation_id(operation_date)
    else:
        # If it contains hyphens, generate a proper C-format ID instead
        if "-" in receipt_number:
            new_operation_id = _generate_operation_id(operation_date)
        else:
            new_operation_id = receipt_number

    # Normalise phone (strip internal spaces, keep + prefix)
    new_phone = recipient_phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not new_phone.startswith("+"):
        new_phone = "+" + new_phone

    # --- Collect all text needed for font coverage check ---
    all_new_text = "".join(filter(None, [
        new_amount, new_commission, new_amount_with_commission,
        new_date_time, new_date_formed,
        new_course, new_amount_credited,
        recipient_name, new_phone,
        new_operation_id,
        account or "",
    ]))

    # --- Find / load donor ---
    if donor_path is not None:
        donor_file = Path(donor_path)
    else:
        donor_file = _find_best_donor(all_new_text)
        if donor_file is None:
            raise FileNotFoundError(
                f"No Tajikistan donor PDFs found in {TAJIK_DONORS_DIR}. "
                "Add real receipts to that folder."
            )

    pdf_bytes = donor_file.read_bytes()
    print(f"[INFO] Donor: {donor_file.name}")

    # --- Font surgery (if needed) ---
    cmap = _cmap_from_pdf(pdf_bytes)
    missing = _chars_missing(cmap, all_new_text)
    if missing:
        print(f"[INFO] Missing chars: {sorted(missing)} — running font surgery...")
        from font_extend import extend_font_in_pdf
        pdf_bytes, cmap = extend_font_in_pdf(
            pdf_bytes, all_new_text, glyph_source=glyph_source
        )
        still_missing = _chars_missing(cmap, all_new_text)
        if still_missing:
            raise RuntimeError(
                f"Font surgery failed — still missing: {sorted(still_missing)}"
            )
        print("[INFO] Font surgery complete.")
    else:
        print("[INFO] All characters already in font.")

    # --- Extract donor field values (as in PDF) ---
    donor_fields = _extract_donor_fields(pdf_bytes, cmap)
    print(f"[INFO] Donor fields: {list(donor_fields.keys())}")

    # --- Build replacements: old (from donor) → new ---
    replacements: list[tuple[str, str]] = []

    def _add(old_key: str, new_val: str) -> None:
        old = donor_fields.get(old_key, "")
        old_clean = old.replace("\xa0", " ").strip()
        new_clean = new_val.replace("\xa0", " ").strip()
        if old and old_clean != new_clean:
            replacements.append((old_clean, new_clean))

    _add("amount", new_amount)
    _add("commission", new_commission)
    _add("amount_with_commission", new_amount_with_commission)
    _add("date_time", new_date_time)
    _add("date_formed", new_date_formed)
    if new_course is not None:
        _add("course", new_course)
    if new_amount_credited is not None:
        _add("amount_credited", new_amount_credited)
    _add("recipient", recipient_name)
    _add("phone", new_phone)
    _add("operation_id", new_operation_id)
    if account is not None:
        _add("account", account)

    print(f"[INFO] Replacements ({len(replacements)}):")
    for old, new in replacements:
        print(f"  {old!r} → {new!r}")

    if not replacements:
        print("[WARN] No replacements needed — donor already matches target values.")
    else:
        # Apply CID replacements
        from cid_patch_amount import patch_replacements
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as in_tmp:
            in_tmp.write(pdf_bytes)
            in_tmp_path = Path(in_tmp.name)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_tmp:
            out_tmp_path = Path(out_tmp.name)

        try:
            ok = patch_replacements(in_tmp_path, out_tmp_path, replacements)
            if not ok:
                raise RuntimeError("CID replacement failed — check donor compatibility.")
            pdf_bytes = out_tmp_path.read_bytes()
        finally:
            try:
                os.unlink(in_tmp_path)
            except OSError:
                pass
            try:
                os.unlink(out_tmp_path)
            except OSError:
                pass

    # --- Set Document /ID (Oracle BI Publisher: ID[0] == ID[1]) ---
    pdf_bytes = _set_doc_id_equal(pdf_bytes)

    # --- Post-generation validation ---
    from pdf_validate import validate_pdf
    expected = [
        recipient_name,
        new_phone,
        new_amount.replace("\xa0", " ").strip(),
    ]
    if new_amount_credited:
        expected.append(new_amount_credited.replace("\xa0", " ").strip())
    result = validate_pdf(pdf_bytes, expected)
    for line in result.info:
        print(f"[VALIDATE] {line}")
    for line in result.warnings:
        print(f"[VALIDATE WARN] {line}")
    if result.errors:
        for line in result.errors:
            print(f"[VALIDATE ERROR] {line}")
        raise RuntimeError(
            f"Generated PDF failed validation: {result.errors[0]}"
        )

    # --- Write output if requested ---
    if output_path is not None:
        Path(output_path).write_bytes(pdf_bytes)
        print(f"[OK] Written: {output_path} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes


# ---------------------------------------------------------------------------
# /check command parser
# ---------------------------------------------------------------------------

def parse_check_command(text: str) -> dict:
    """Parse /check command parameters into a dict.

    Expected format (each param on its own line after '/check'):
        /check
        amount: 3000
        sender_name: Kirill S.
        recipient_phone: +992938999964
        recipient_name: Bobomurodov Kh.
        credited_currency: TJS
        amount_int: 354
        operation_date: auto // 25.09.2025
        operation_time: auto // 13:45:12
        receipt_number: auto // 1-112-138-138-512

    The '// comment' part is stripped from values.
    'auto' is converted to None (caller decides).
    """
    params: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("/check") or not line:
            continue
        if ":" not in line:
            continue
        key, _, raw_val = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        # Strip inline comments
        val = raw_val.split("//")[0].strip()
        if val.lower() == "auto":
            val = "auto"
        params[key] = val
    return params


def generate_from_check_command(text: str, output_path: str | Path | None = None) -> bytes:
    """Generate a receipt from a raw /check command string."""
    params = parse_check_command(text)

    amount_str = params.get("amount", "")
    if not amount_str:
        raise ValueError("amount is required")
    amount = int(re.sub(r"\D", "", amount_str))

    recipient_name = params.get("recipient_name", "")
    if not recipient_name:
        raise ValueError("recipient_name is required")

    recipient_phone = params.get("recipient_phone", "")
    if not recipient_phone:
        raise ValueError("recipient_phone is required")

    credited_currency = params.get("credited_currency", "TJS").strip().upper()

    amount_int_str = params.get("amount_int", "")
    amount_credited: int | None = None
    if amount_int_str and amount_int_str != "auto":
        amount_credited = int(re.sub(r"\D", "", amount_int_str))

    operation_date = params.get("operation_date", "auto")
    operation_time = params.get("operation_time", "auto")
    receipt_number = params.get("receipt_number", "auto")

    return generate_tajik_receipt(
        amount=amount,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        credited_currency=credited_currency,
        amount_credited=amount_credited,
        operation_date=operation_date,
        operation_time=operation_time,
        receipt_number=receipt_number,
        output_path=output_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate an Alfa-Bank Tajikistan transgran receipt PDF."
    )
    parser.add_argument("--amount", type=int, required=True, help="Transfer amount in RUB")
    parser.add_argument("--recipient-name", required=True, help="Recipient name")
    parser.add_argument("--recipient-phone", required=True, help="Recipient phone")
    parser.add_argument("--credited-currency", default="TJS", help="Foreign currency (TJS)")
    parser.add_argument("--amount-credited", type=int, default=None,
                        help="Credited amount in foreign currency (integer)")
    parser.add_argument("--operation-date", default="auto", help="DD.MM.YYYY or auto")
    parser.add_argument("--operation-time", default="auto", help="HH:MM:SS or auto")
    parser.add_argument("--receipt-number", default="auto", help="Operation ID or auto")
    parser.add_argument("--commission", type=int, default=0, help="Commission in RUB")
    parser.add_argument("--account", default=None, help="Sender account (20 digits)")
    parser.add_argument("--donor", default=None, help="Explicit donor PDF path")
    parser.add_argument("--output", "-o", default="receipt_tajik.pdf", help="Output PDF")
    args = parser.parse_args()

    try:
        generate_tajik_receipt(
            amount=args.amount,
            recipient_name=args.recipient_name,
            recipient_phone=args.recipient_phone,
            credited_currency=args.credited_currency,
            amount_credited=args.amount_credited,
            operation_date=args.operation_date,
            operation_time=args.operation_time,
            receipt_number=args.receipt_number,
            commission=args.commission,
            account=args.account,
            donor_path=args.donor,
            output_path=args.output,
        )
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
