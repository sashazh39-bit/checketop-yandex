#!/usr/bin/env python3
"""Clean CID patcher for Alfa-Bank SBP receipts.

Pure CID hex replacement with field consistency enforcement.
No structural injections, no /ID modification, no CMap/font changes.

Usage:
  from alfa_sbp_clean_patch import patch_alfa_sbp
  patch_alfa_sbp(
      donor_pdf="donor.pdf",
      output_pdf="out.pdf",
      amount=5000,
      date_time="30.03.2026 12:30:45 мск",
      recipient="Денис Алексеевич К",
      phone="+7 (903) 712-34-56",
      bank="Сбербанк",
      account_last4="1234",
  )
"""
from __future__ import annotations

import random
import re
import sys
import zlib
from pathlib import Path


# ---------------------------------------------------------------------------
# CMap helpers
# ---------------------------------------------------------------------------

def _parse_cmap(data: bytes) -> dict[int, str]:
    """Parse first ToUnicode CMap -> {unicode_int: cid_hex_str}."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        uni_to_cid: dict[int, str] = {}
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid_hex = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid_hex
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
            return uni_to_cid
    return {}


def _available_chars(uni_to_cid: dict[int, str]) -> set[str]:
    """Set of unicode characters available in the CMap."""
    return {chr(k) for k in uni_to_cid if k < 0xFFFF}


def _validate_chars(text: str, available: set[str], field_name: str) -> None:
    """Raise ValueError if text contains characters not in font."""
    for ch in text:
        if ch == " ":
            if "\xa0" not in available:
                raise ValueError(
                    f"[{field_name}] NBSP (\\xa0) not in font, cannot replace space"
                )
            continue
        if ch not in available:
            raise ValueError(
                f"[{field_name}] Character {ch!r} (U+{ord(ch):04X}) not in font. "
                f"Available: {''.join(sorted(available))}"
            )


# ---------------------------------------------------------------------------
# Field extraction from donor
# ---------------------------------------------------------------------------

def _extract_fields(data: bytes, uni_to_cid: dict[int, str]) -> dict[str, str]:
    """Extract all text fields from the donor PDF content stream."""
    cid_to_chr: dict[str, str] = {}
    for uni_int, cid_hex in uni_to_cid.items():
        cid_to_chr[cid_hex] = chr(uni_int)

    texts: list[str] = []
    for m in re.finditer(rb"/Length\s+(\d+).*?stream\r?\n", data, re.DOTALL):
        slen = int(m.group(1))
        start = m.end()
        raw = data[start : start + slen]
        try:
            dec = zlib.decompress(raw)
            if b"Tj" not in dec:
                continue
        except Exception:
            continue
        for tj in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", dec):
            hexdata = tj.group(1).decode().upper()
            chars = []
            for i in range(0, len(hexdata), 4):
                code = hexdata[i : i + 4]
                chars.append(cid_to_chr.get(code, "?"))
            texts.append("".join(chars))
        break

    labels = [t.replace("\xa0", " ").strip() for t in texts]

    def _next_value(idx: int) -> str | None:
        j = idx + 1
        while j < len(labels) and labels[j] == "":
            j += 1
        return texts[j] if j < len(texts) else None

    fields: dict[str, str] = {}
    for i, lbl in enumerate(labels):
        if re.search(r"\d+\.\d+\.\d{4}\s+\d{2}:\d{2}\s+мск", lbl):
            fields["formed_at"] = texts[i]
        elif re.search(r"\d+\s*RUR", lbl) and "Сумма" not in lbl and "Комиссия" not in lbl:
            pass
        elif lbl == "Сумма перевода":
            fields["amount"] = _next_value(i) or ""
        elif lbl == "Комиссия":
            fields["commission"] = _next_value(i) or ""
        elif lbl == "Дата и время перевода":
            fields["date_time"] = _next_value(i) or ""
        elif lbl == "Номер операции":
            fields["operation_id"] = _next_value(i) or ""
        elif lbl == "Получатель":
            fields["recipient"] = _next_value(i) or ""
        elif "телефона получателя" in lbl:
            fields["phone"] = _next_value(i) or ""
        elif lbl == "Банк получателя":
            fields["bank"] = _next_value(i) or ""
        elif "Счёт списания" in lbl or "Счет списания" in lbl:
            fields["account"] = _next_value(i) or ""
        elif "Идентификатор операции" in lbl:
            fields["sbp_id"] = _next_value(i) or ""
        elif "Сообщение получателю" in lbl:
            fields["message"] = _next_value(i) or ""

    # Capture first amount-like text that follows "Сумма перевода" label
    if "amount" not in fields:
        for t in texts:
            clean = t.replace("\xa0", " ").strip()
            if re.match(r"\d[\d\s]*RUR", clean):
                fields["amount"] = t
                break

    return fields


# ---------------------------------------------------------------------------
# Operation ID generation
# ---------------------------------------------------------------------------

def generate_operation_id(donor_op_id: str, new_date: str) -> str:
    """Generate a new operation ID preserving type code and encoding the new date.

    donor_op_id: e.g. "C163003260096636"
    new_date: "DD.MM.YYYY" e.g. "25.03.2026"
    Returns: e.g. "C162503260123456"
    """
    prefix = donor_op_id[0]  # "C"
    type_code = donor_op_id[1:3]  # "16"

    parts = new_date.split(".")
    dd, mm = parts[0], parts[1]
    yy = parts[2][2:] if len(parts[2]) == 4 else parts[2]
    date_block = f"{dd}{mm}{yy}"

    seq_len = len(donor_op_id) - 3 - 6  # total - prefix(1) - type(2) - date(6)
    seq = "".join(str(random.randint(0, 9)) for _ in range(seq_len))

    return f"{prefix}{type_code}{date_block}{seq}"


# ---------------------------------------------------------------------------
# Amount formatting
# ---------------------------------------------------------------------------

def format_amount(value: int | float) -> str:
    """Format amount as 'N RUR ' with NBSP thousands separators, matching Oracle BI Publisher."""
    if isinstance(value, float):
        value = int(value)
    s = f"{value:,}".replace(",", "\xa0")
    return f"{s}\xa0RUR\xa0"


# ---------------------------------------------------------------------------
# Main patcher
# ---------------------------------------------------------------------------

def patch_alfa_sbp(
    donor_pdf: str | Path,
    output_pdf: str | Path,
    *,
    amount: int | None = None,
    date_time: str | None = None,
    recipient: str | None = None,
    phone: str | None = None,
    bank: str | None = None,
    account_last4: str | None = None,
    message: str | None = None,
    commission: int | None = None,
    operation_id: str | None = None,
    bik: str | None = None,
) -> bool:
    """Apply clean CID replacements to an Alfa-Bank SBP receipt PDF.

    Returns True on success.
    """
    donor_pdf = Path(donor_pdf)
    output_pdf = Path(output_pdf)
    data = donor_pdf.read_bytes()

    uni_to_cid = _parse_cmap(data)
    if not uni_to_cid:
        print("[ERROR] CMap not found in donor PDF", file=sys.stderr)
        return False

    available = _available_chars(uni_to_cid)
    current = _extract_fields(data, uni_to_cid)

    print(f"[INFO] Donor fields extracted: {list(current.keys())}")
    for k, v in current.items():
        print(f"  {k}: {v.replace(chr(0xa0), ' ').strip()!r}")

    replacements: list[tuple[str, str]] = []

    # --- Amount ---
    if amount is not None and "amount" in current:
        old_amt = current["amount"].replace("\xa0", " ").strip()
        new_amt = format_amount(amount)
        if old_amt != new_amt.replace("\xa0", " ").strip():
            replacements.append((old_amt, new_amt))
            print(f"[FIELD] amount: {old_amt!r} -> {new_amt.replace(chr(0xa0), ' ')!r}")

    # --- Commission ---
    if commission is not None and "commission" in current:
        old_comm = current["commission"].replace("\xa0", " ").strip()
        new_comm = format_amount(commission)
        if old_comm != new_comm.replace("\xa0", " ").strip():
            replacements.append((old_comm, new_comm))

    # --- Date/time + formed_at ---
    new_date_str = None
    if date_time is not None and "date_time" in current:
        old_dt = current["date_time"].replace("\xa0", " ").strip()
        new_dt = date_time.replace(" ", "\xa0")
        if not new_dt.endswith("\xa0"):
            new_dt += "\xa0"
        _validate_chars(new_dt.replace("\xa0", " "), available, "date_time")
        if old_dt != new_dt.replace("\xa0", " ").strip():
            replacements.append((old_dt, new_dt))
            print(f"[FIELD] date_time: {old_dt!r} -> {date_time!r}")

        dt_match = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})", date_time)
        if dt_match:
            new_date_str = dt_match.group(1)
            new_time_hm = dt_match.group(2)

            if "formed_at" in current:
                old_formed = current["formed_at"].replace("\xa0", " ").strip()
                new_formed = f"{new_date_str}\xa0{new_time_hm}\xa0мск"
                if old_formed != new_formed.replace("\xa0", " "):
                    replacements.append((old_formed, new_formed))
                    print(f"[FIELD] formed_at: {old_formed!r} -> {new_formed.replace(chr(0xa0), ' ')!r}")

    # --- Operation ID ---
    if "operation_id" in current:
        old_op = current["operation_id"].replace("\xa0", " ").strip()

        if operation_id is not None:
            new_op = operation_id
        elif new_date_str is not None:
            new_op = generate_operation_id(old_op, new_date_str)
        else:
            new_op = None

        if new_op and old_op != new_op:
            _validate_chars(new_op, available, "operation_id")
            new_op_padded = new_op + "\xa0" if old_op.endswith(" ") or current["operation_id"].endswith("\xa0") else new_op
            replacements.append((old_op, new_op_padded))
            print(f"[FIELD] operation_id: {old_op!r} -> {new_op!r}")

    # --- Recipient ---
    if recipient is not None and "recipient" in current:
        old_recip = current["recipient"].replace("\xa0", " ").strip()
        new_recip = recipient.replace(" ", "\xa0")
        _validate_chars(recipient, available, "recipient")
        if old_recip != new_recip.replace("\xa0", " ").strip():
            replacements.append((old_recip, new_recip))
            print(f"[FIELD] recipient: {old_recip!r} -> {recipient!r}")

    # --- Phone ---
    if phone is not None and "phone" in current:
        old_phone = current["phone"].replace("\xa0", " ").strip()
        new_phone = phone.replace(" ", "\xa0")
        _validate_chars(phone, available, "phone")
        if old_phone != new_phone.replace("\xa0", " ").strip():
            replacements.append((old_phone, new_phone))
            print(f"[FIELD] phone: {old_phone!r} -> {phone!r}")

    # --- Bank ---
    if bank is not None and "bank" in current:
        old_bank = current["bank"].replace("\xa0", " ").strip()
        new_bank = bank.replace(" ", "\xa0")
        _validate_chars(bank, available, "bank")
        if old_bank != new_bank.replace("\xa0", " ").strip():
            replacements.append((old_bank, new_bank))
            print(f"[FIELD] bank: {old_bank!r} -> {bank!r}")

    # --- Message ---
    if message is not None and "message" in current:
        old_msg = current["message"].replace("\xa0", " ").strip()
        new_msg = message.replace(" ", "\xa0")
        _validate_chars(message, available, "message")
        if old_msg != new_msg.replace("\xa0", " ").strip():
            replacements.append((old_msg, new_msg))
            print(f"[FIELD] message: {old_msg!r} -> {message!r}")

    if not replacements:
        print("[INFO] No replacements needed")
        import shutil
        shutil.copy2(str(donor_pdf), str(output_pdf))
        return True

    # Apply CID replacements via existing module
    from cid_patch_amount import patch_replacements

    text_reps = [
        (old.replace("\xa0", " "), new.replace("\xa0", " "))
        for old, new in replacements
    ]
    ok = patch_replacements(donor_pdf, output_pdf, text_reps)
    if not ok:
        print("[ERROR] patch_replacements failed", file=sys.stderr)
        return False

    # Account (post-patch, separate pass)
    if account_last4 is not None and "account" in current:
        old_acct = current["account"].replace("\xa0", "").strip()
        digits = re.findall(r"\d+", old_acct)
        full_acct = "".join(digits)
        if len(full_acct) == 20:
            from patch_account_last4 import patch_account_last4 as do_patch_account

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name

            acct_ok = do_patch_account(
                input_pdf=str(output_pdf),
                output_pdf=tmp_path,
                old_account=full_acct,
                new_last4=account_last4,
                bik=bik,
            )
            if acct_ok:
                import shutil
                shutil.move(tmp_path, str(output_pdf))
                print(f"[FIELD] account: ****{full_acct[-4:]} -> ****{account_last4}")
            else:
                try:
                    import os
                    os.unlink(tmp_path)
                except OSError:
                    pass
                print("[WARN] Account patch failed", file=sys.stderr)

    print(f"[OK] Output: {output_pdf}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Clean Alfa-Bank SBP receipt patcher")
    parser.add_argument("donor", help="Donor PDF path")
    parser.add_argument("output", help="Output PDF path")
    parser.add_argument("--amount", type=int, help="New amount (integer RUR)")
    parser.add_argument("--date-time", help="New date/time, e.g. '25.03.2026 14:30:00 мск'")
    parser.add_argument("--recipient", help="New recipient name")
    parser.add_argument("--phone", help="New phone, e.g. '+7 (903) 712-34-56'")
    parser.add_argument("--bank", help="New bank name")
    parser.add_argument("--account-last4", help="New last 4 digits of account")
    parser.add_argument("--message", help="New message")
    parser.add_argument("--operation-id", help="Explicit operation ID (auto-generated if omitted)")
    parser.add_argument("--bik", help="BIK for account control key (auto-detected)")
    args = parser.parse_args()

    ok = patch_alfa_sbp(
        donor_pdf=args.donor,
        output_pdf=args.output,
        amount=args.amount,
        date_time=args.date_time,
        recipient=args.recipient,
        phone=args.phone,
        bank=args.bank,
        account_last4=args.account_last4,
        message=args.message,
        operation_id=args.operation_id,
        bik=args.bik,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
