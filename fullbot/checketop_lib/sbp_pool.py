#!/usr/bin/env python3
"""SBP ID pool management.

Pool file: sbp_pool.json
Format:
  [
    {
      "id": "A61051018121260A0B10040011740901",
      "used": false,
      "bank": "Альфа",
      "amount": 250,
      "date": "15.04.2026",
      "time": "13:18:11",
      "added_at": "2026-04-15T10:00:00Z"
    },
    ...
  ]

Usage from code:
    from sbp_pool import SBPPool
    pool = SBPPool()
    entry = pool.consume()   # returns dict or None
    pool.add("A61...", bank="Альфа", amount=500, date="15.04.2026", time="13:20:00")
    pool.status()            # returns (total, used, available)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

POOL_FILE = Path(__file__).parent / "sbp_pool.json"


class SBPPool:
    def __init__(self, pool_file: "str | Path | None" = None) -> None:
        self._path = Path(pool_file) if pool_file else POOL_FILE
        self._data: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        sbp_id: str,
        *,
        bank: str = "",
        amount: int = 0,
        date: str = "",
        time: str = "",
    ) -> bool:
        """Add an SBP ID to the pool.  Returns False if already present."""
        sbp_id = sbp_id.strip()
        if len(sbp_id) != 32:
            return False
        # Validate: only alphanumeric
        if not re.fullmatch(r"[A-Za-z0-9]{32}", sbp_id):
            return False
        # Check duplicate
        for entry in self._data:
            if entry.get("id") == sbp_id:
                return False
        self._data.append({
            "id": sbp_id,
            "used": False,
            "bank": bank,
            "amount": amount,
            "date": date,
            "time": time,
            "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        self._save()
        return True

    def add_bulk(self, ids_text: str, *, bank: str = "") -> tuple[int, int]:
        """Parse a newline/comma/space-separated string of SBP IDs and add them.

        Returns (added_count, skipped_count).
        """
        candidates = re.findall(r"[A-Za-z0-9]{32}", ids_text)
        added = 0
        skipped = 0
        for sid in candidates:
            if self.add(sid, bank=bank):
                added += 1
            else:
                skipped += 1
        return added, skipped

    def consume(self) -> Optional[dict]:
        """Return the first unused entry and mark it as used.

        Returns None if pool is empty.
        """
        for entry in self._data:
            if not entry.get("used", False):
                entry["used"] = True
                self._save()
                return entry
        return None

    def peek(self) -> Optional[dict]:
        """Return the first unused entry without marking it as used."""
        for entry in self._data:
            if not entry.get("used", False):
                return entry
        return None

    def status(self) -> tuple[int, int, int]:
        """Returns (total, used, available)."""
        total = len(self._data)
        used = sum(1 for e in self._data if e.get("used", False))
        return total, used, total - used

    def list_available(self) -> list[dict]:
        """Return all unused entries."""
        return [e for e in self._data if not e.get("used", False)]

    def list_all(self) -> list[dict]:
        return list(self._data)

    def clear_used(self) -> int:
        """Remove all used entries. Returns count removed."""
        before = len(self._data)
        self._data = [e for e in self._data if not e.get("used", False)]
        self._save()
        return before - len(self._data)


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="SBP ID pool manager")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="Add SBP IDs")
    p_add.add_argument("ids", nargs="+", help="32-char SBP IDs")
    p_add.add_argument("--bank", default="")

    sub.add_parser("status", help="Show pool status")
    sub.add_parser("list", help="List all entries")
    sub.add_parser("consume", help="Consume next available entry")

    p_clear = sub.add_parser("clear-used", help="Remove used entries")

    args = parser.parse_args()
    pool = SBPPool()

    if args.cmd == "add":
        for sid in args.ids:
            ok = pool.add(sid, bank=args.bank)
            print(f"{'Added' if ok else 'Skipped (duplicate/invalid)'}: {sid}")
    elif args.cmd == "status":
        total, used, avail = pool.status()
        print(f"Total: {total} | Used: {used} | Available: {avail}")
    elif args.cmd == "list":
        for e in pool.list_all():
            mark = "✓" if e.get("used") else "○"
            print(f"  {mark} {e['id']}  bank={e.get('bank','')}  amount={e.get('amount','')}  date={e.get('date','')}")
    elif args.cmd == "consume":
        entry = pool.consume()
        if entry:
            print(f"Consumed: {entry['id']}")
        else:
            print("Pool is empty.")
    elif args.cmd == "clear-used":
        n = pool.clear_used()
        print(f"Removed {n} used entries.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
