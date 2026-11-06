#!/usr/bin/env python3
"""Patch Rocket Bank receipt PDFs field by field.

The receipts store text as CID glyph codes against per-line font subsets,
so plain byte search-and-replace does not reach the values. This tool
decodes each line through its ToUnicode CMap, re-encodes the new value,
and borrows glyph outlines from donor receipts whenever a subset is
missing a character. Everything else in the file is preserved: an
unmodified run reproduces the source byte for byte.

Build the glyph pool once:

    python3 patch_rocket.py prepare ~/Downloads

Inspect a receipt:

    python3 patch_rocket.py show ~/Downloads/receipt_rocket.pdf

Patch one:

    python3 patch_rocket.py patch in.pdf out.pdf --amount "500 ₽" \\
        --account-tail 1234 --date "06.10.2026 15:30"
"""

from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys
from typing import Dict, List, Optional

import rocket_font
import rocket_receipt

DEFAULT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rocket_glyphs.pkl")

# CLI option -> logical field name.
FIELD_OPTIONS = {
    "date": "date",
    "amount": "amount",
    "fee": "fee",
    "direction": "direction",
    "recipient": "recipient",
    "card": "card",
    "phone_to": "phone_to",
    "bank_to": "bank_to",
    "sender": "sender",
    "phone_from": "phone_from",
    "bank_from": "bank_from",
    "account": "account",
    "sbp_id": "sbp_id",
    "status": "status",
    "doc_number": "doc_number",
}


# ------------------------------------------------------------------- prepare


def harvest(paths: List[str], verbose: bool = True) -> rocket_font.GlyphCache:
    """Collect every glyph outline and width found across donor receipts."""
    cache = rocket_font.GlyphCache()
    for path in paths:
        try:
            receipt = rocket_receipt.Receipt(path)
        except Exception as error:  # noqa: BLE001 - donor set is user supplied
            if verbose:
                print(f"  skip {os.path.basename(path)}: {error}", file=sys.stderr)
            continue

        for slot in receipt.fonts.values():
            cff_bytes = receipt.pdf.obj(slot.fontfile_obj).decoded_stream()
            cache.add_font(
                slot.family,
                rocket_font.filled_cids(cff_bytes),
                slot.widths,
                slot.cid_to_char,
            )
        if verbose:
            print(f"  read {os.path.basename(path)} ({len(receipt.fonts)} fonts)")
    return cache


def cmd_prepare(args: argparse.Namespace) -> int:
    patterns = []
    for source in args.sources:
        patterns.extend(
            sorted(glob.glob(os.path.join(source, "*.pdf")))
            if os.path.isdir(source)
            else [source]
        )
    if not patterns:
        print("no PDFs found in the given sources", file=sys.stderr)
        return 1

    print(f"Harvesting glyphs from {len(patterns)} file(s):")
    cache = harvest(patterns)
    if not cache.glyphs:
        print("no fonts harvested", file=sys.stderr)
        return 1

    with open(args.cache, "wb") as handle:
        pickle.dump(cache, handle)
    print()
    print(cache.summary())
    print()
    print(f"Glyph pool written to {args.cache}")
    return 0


def load_cache(path: str) -> Optional[rocket_font.GlyphCache]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return pickle.load(handle)


# ---------------------------------------------------------------------- show


def cmd_show(args: argparse.Namespace) -> int:
    receipt = rocket_receipt.Receipt(args.input, load_cache(args.cache))
    print(f"File:   {args.input}")
    print(f"Layout: {receipt.format}")
    print()
    width = max(len(name) for name in receipt.fields)
    for name, value in receipt.fields.items():
        own = len(receipt.field_charset(name))
        print(f"  {name:<{width}}  {value!r}   [subset: {own} chars]")
    return 0


# --------------------------------------------------------------------- patch


def cmd_patch(args: argparse.Namespace) -> int:
    cache = load_cache(args.cache)
    if cache is None:
        print(
            f"warning: no glyph pool at {args.cache}; new characters will be "
            "rejected. Run the prepare step to enable them.",
            file=sys.stderr,
        )

    receipt = rocket_receipt.Receipt(args.input, cache)
    # In incremental mode we byte-match iText's reference (full donor glyph set);
    # in same-size modes (inplace/auto) we add only the glyphs we actually need.
    receipt.minimal_glyphs = getattr(args, "mode", "auto") != "incremental"
    changes: Dict[str, str] = {}
    for option, field in FIELD_OPTIONS.items():
        value = getattr(args, option, None)
        if value is not None:
            changes[field] = value

    if not changes and args.account_tail is None:
        print("nothing to change", file=sys.stderr)
        return 1

    for field, value in changes.items():
        before = receipt.fields.get(field)
        receipt.set(field, value)
        print(f"  {field}: {before!r} -> {value!r}")

    if args.account_tail is not None:
        before = receipt.fields.get("account")
        receipt.set_account_tail(args.account_tail)
        print(f"  account: {before!r} -> {receipt.fields['account']!r}")

    mode = getattr(args, "mode", "auto")
    receipt.save(args.output, mode=mode)
    size = os.path.getsize(args.output)
    orig_size = os.path.getsize(args.input)
    size_note = f" (оригинал {orig_size})" if size != orig_size else " (тот же размер!)"
    print()
    print(f"Written {args.output} ({size} bytes){size_note}")
    created = receipt._created_dt()
    print(
        f"PDF CreationDate {receipt._pdf_timestamp()}  "
        f"(платёж МСК +5 мин → {created.isoformat()})"
    )
    return 0


# ----------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch_rocket.py",
        description="Inspect and patch Rocket Bank receipt PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cache",
        default=DEFAULT_CACHE,
        help=f"glyph pool file (default: {DEFAULT_CACHE})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="harvest glyph outlines from donor receipts"
    )
    prepare.add_argument("sources", nargs="+", help="PDF files or directories")
    prepare.set_defaults(func=cmd_prepare)

    show = subparsers.add_parser("show", help="list the fields of a receipt")
    show.add_argument("input", help="receipt PDF")
    show.set_defaults(func=cmd_show)

    patch = subparsers.add_parser("patch", help="write a receipt with new values")
    patch.add_argument("input", help="template receipt PDF")
    patch.add_argument("output", help="destination PDF")
    patch.add_argument("--date", help='e.g. "06.10.2026 15:30 ПО МСК"')
    patch.add_argument("--amount", help='e.g. "500 ₽"')
    patch.add_argument("--fee", help='e.g. "БЕЗ КОМИССИИ"')
    patch.add_argument("--direction", help='e.g. "ИСХОДЯЩИЙ"')
    patch.add_argument("--recipient", help="recipient name (SBP layout)")
    patch.add_argument("--card", help="recipient card (card layout)")
    patch.add_argument("--phone-to", dest="phone_to", help="recipient phone")
    patch.add_argument("--bank-to", dest="bank_to", help="recipient bank")
    patch.add_argument("--sender", help="sender name")
    patch.add_argument("--phone-from", dest="phone_from", help="sender phone")
    patch.add_argument("--bank-from", dest="bank_from", help="sender bank")
    patch.add_argument("--account", help="full sender account number")
    patch.add_argument(
        "--account-tail", help="rewrite only the trailing digits of the account"
    )
    patch.add_argument("--sbp-id", dest="sbp_id", help="SBP operation id")
    patch.add_argument("--status", help='e.g. "УСПЕШНО"')
    patch.add_argument("--doc-number", dest="doc_number", help="document number")
    patch.add_argument(
        "--mode",
        choices=["auto", "inplace", "incremental"],
        default="auto",
        help=(
            "auto: in-place if possible, else incremental (default); "
            "inplace: same byte size as original (checker-facing); "
            "incremental: iText-style append (debugging only)"
        ),
    )
    patch.set_defaults(func=cmd_patch)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (rocket_receipt.ReceiptError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
