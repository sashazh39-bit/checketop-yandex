#!/usr/bin/env python3
"""Build a Rocket account statement PDF from the retranslator history JSON on stdin."""
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rocket_statement import generate_rocket_statement  # noqa: E402

MSK = timedelta(hours=3)


def main() -> int:
    data = json.load(sys.stdin)
    holder = str(data.get("holder") or "Александр Евгеньевич Жеребятьев").strip()
    phone = str(data.get("phone") or "")
    account = str(data.get("account") or "")
    closing = Decimal(str(data.get("closing") or "0"))
    raw_ops = list(data.get("operations") or [])[:10]
    today = datetime.now().replace(second=0, microsecond=0)
    ops = []
    net = Decimal("0")
    for i, op in enumerate(raw_ops):
        amount = Decimal(str(op.get("amount") or "0"))
        if amount == 0:
            continue
        income = str(op.get("direction") or "").upper() == "INCOME"
        if income:
            net += amount
        else:
            net -= amount
        moment = today.replace(hour=9, minute=0) + timedelta(minutes=7 * i)
        ops.append({
            "kind": "sbp_in" if income else "sbp_out",
            "datetime": moment.strftime("%Y-%m-%d %H:%M"),
            "amount": str(amount),
            "phone": str(op.get("phone") or phone or "9001234567"),
        })
    opening = closing - net
    if opening < 0:
        opening = Decimal("0")
    end = today.date()
    start = end - timedelta(days=6)
    out = Path(data.get("output") or "/tmp/rocket-statement.pdf")
    generate_rocket_statement(
        period_start=start.strftime("%d.%m.%Y"),
        period_end=end.strftime("%d.%m.%Y"),
        issue_date=end.strftime("%d.%m.%Y"),
        holder_name=holder,
        account=account or None,
        random_account_tail=not account,
        opening_balance=str(opening),
        operations=ops,
        operation_count=len(ops),
        fill_random=False,
        phone=phone or None,
        output_path=out,
    )
    sys.stdout.buffer.write(out.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
