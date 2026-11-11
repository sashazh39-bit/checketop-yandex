#!/usr/bin/env python3
"""Read a genuine Rocket receipt PDF and print its fields as JSON for the admin.

Usage: parse_receipt.py <path-to.pdf>
Prints {"found": [...], "amount", "direction", "date", "recipientName",
"recipientPhone", "recipientBank", "description", "category", "status"}.
On any failure prints {"found": []} so the upload still attaches the PDF.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/rocket")

MSK = timezone(timedelta(hours=3))


def clean_amount(raw):
    s = str(raw or "").replace("\u00a0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    s = re.sub(r"[^0-9.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def iso_date(raw):
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", str(raw or ""))
    if not m:
        m2 = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", str(raw or ""))
        if not m2:
            return None
        d, mo, y = (int(x) for x in m2.groups())
        return datetime(y, mo, d, 12, 0, tzinfo=MSK).isoformat()
    d, mo, y, hh, mm = (int(x) for x in m.groups())
    return datetime(y, mo, d, hh, mm, tzinfo=MSK).isoformat()


def main():
    out = {"found": [], "amount": None, "direction": None, "date": None,
           "recipientName": None, "recipientPhone": None, "recipientBank": None,
           "description": None, "category": "transfer", "status": None}
    try:
        import rocket_receipt
        receipt = rocket_receipt.Receipt(sys.argv[1])
        f = receipt.fields
    except Exception as exc:  # noqa: BLE001 — any parse failure → empty result
        print(json.dumps({**out, "error": str(exc)[:200]}, ensure_ascii=False))
        return 0

    found = []
    amount = clean_amount(f.get("amount"))
    if amount:
        out["amount"] = amount
        found.append("amount")

    date = iso_date(f.get("date"))
    if date:
        out["date"] = date
        found.append("date")

    # Outgoing receipt lists the recipient; incoming lists the sender.
    has_to = any(f.get(k) for k in ("recipient", "phone_to", "bank_to"))
    has_from = any(f.get(k) for k in ("sender", "phone_from", "bank_from"))
    if has_to and not has_from:
        out["direction"] = "OUTCOME"
        name, phone, bank = f.get("recipient"), f.get("phone_to"), f.get("bank_to")
    elif has_from:
        out["direction"] = "INCOME"
        name, phone, bank = f.get("sender"), f.get("phone_from"), f.get("bank_from")
    else:
        out["direction"] = "OUTCOME"
        name = phone = bank = None
    if out["direction"]:
        found.append("direction")
    if name:
        out["recipientName"] = name.strip()
        found.append("recipientName")
    if phone:
        out["recipientPhone"] = phone.strip()
        found.append("recipientPhone")
    if bank:
        out["recipientBank"] = bank.strip()
        found.append("recipientBank")

    status = str(f.get("status") or "")
    if re.search(r"выполн", status, re.I):
        out["status"] = "COMPLETED"
        found.append("status")

    out["found"] = found
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
