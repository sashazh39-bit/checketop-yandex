#!/usr/bin/env python3
"""Patch Yandex Bank receipt PDFs field by field.

The receipt stores every value as 2-byte CID codes against the
``YSText-Regular`` Type0 subset (see ``yandex_receipt.py``), so plain
byte search-and-replace does not reach the values.  This tool decodes
each value through its ToUnicode CMap, re-encodes the new value,
recomputes its right-aligned ``Tm`` x from the ``/W`` widths, and —
when the subset lacks a needed character — grows the native YSText
font with glyphs harvested from other genuine Yandex receipts.
Everything else in the file is preserved: the donor bytes stay
verbatim and the changed objects are appended as an incremental
update, exactly like ``patch_rocket.py``.

Every generated receipt is a *new* operation, so the identifiers that
donors tie to the operation moment are regenerated with it, in the
formats measured on 39 Sep-2026 donors plus the Nov-2026 originals
(banks/yandex/BANK.md, дополнение 03.11.2026):

* ``Исх. №``       -- YYYYMMDD of the operation (MSK) + 7 opaque digits
* ``Дата``         -- operation date (MSK)
* SBP id           -- [A|B] + last year digit (UTC) + day-of-year (UTC)
                      + HHMMSS (UTC) + opaque segments in donor formats;
                      tail segment is ``0011881102`` in Nov-2026 originals
* auth code        -- 6 chars from the observed alphabet
                      ``ABCFGIJKLMNOPRTYZ`` + digits, 1-5 of them digits
* PDF CreationDate -- operation + 5 minutes, in UTC, ``D:...Z``
* trailer /ID      -- fresh unique 32-hex (``rocket_pdf.reidentify``)
* output file name -- ``<uuid4>.pdf`` when not given explicitly

Usage (all fields in a single run):

    python3 patch_yandex.py yandex_fresh.pdf \\
        --datetime "03.11.2026 14:25" \\
        --sender-phone "+7 916 123-45-67" --sender "Алексей Николаевич С." \\
        --recipient-phone "+7 925 765-43-21" --recipient "Мария Сергеевна К." \\
        --bank-to "Т-Банк" --amount "17 350,00 ₽" --fee "—"

An explicit output path may be given as the second positional
argument.  Optional glyph donors (only consulted when a glyph is
missing; by default other Yandex-looking PDFs next to the donor are
used):

    python3 patch_yandex.py donor.pdf out.pdf --amount "10 000,00 ₽" \\
        --glyph-donor yandex_10000.pdf --glyph-donor ~/Downloads/another.pdf

Inspect a receipt without patching:

    python3 patch_yandex.py show donor.pdf
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yandex_font
import yandex_receipt
from yandex_receipt import MSK, Receipt, ReceiptError

AMOUNT_FAMILY = yandex_receipt.AMOUNT_FONT_FAMILY

# ------------------------------------------------------ linked identifiers
# Formats measured on the donor set; see banks/yandex/BANK.md for the
# evidence.  Only the segments donors prove to be derived from the
# operation moment are recomputed from it; the opaque segments are
# regenerated inside the exact alphabets/lengths the donors use.

_SBP_TAILS = ("0011881102",)  # 03.11.2026; 06.11.2026 moved to 0011890101
_SBP_TAIL_ERAS = (
    (datetime(2026, 11, 3, tzinfo=MSK), "0011881102"),
    (datetime(2026, 11, 6, tzinfo=MSK), "0011890101"),
)
_SBP_NOV_SEGMENTS = ("1007", "1008", "1010", "1013", "1017")  # segments of the Nov-2026 originals
_SBP_MID_ALPHABET = "0123456789ABEHILMNOPRT"  # observed alphabet, segment 16
_AUTH_ALPHABET = "ABCFGIJKLMNOPRTVYZ0123456789"  # observed letters + digits


def _sbp_era_tail(op_msk: datetime) -> str:
    """Tail of the SBP id for the operation day. Empty before Nov 2026."""
    tail = ""
    for start, value in _SBP_TAIL_ERAS:
        if op_msk >= start:
            tail = value
    return tail


def make_sbp_id(
    op_msk: datetime, donor_sbp: str = "", *, fresh_serial: bool = False
) -> str:
    """Build a 32-char SBP operation id for the given operation moment.

    The serial token after the time header (positions 12-32) is issued
    by the ОПКЦ.  Two strategies, because the checkers differ:

    * inherited (default): only the UTC time header is rebuilt, the
      donor's token is kept (the tail is moved to the current era,
      ``0011881102``).  This is the configuration that passed
      ``@podlinno_bot`` 45/45 — its operation check evidently accepts
      a token a genuine receipt was issued with, and rejects an
      invented one (a fresh token on a clean donor failed 44/45 with
      only «номер операции» red), even though no checksum over the
      token could be found on 119 genuine IDs.
    * fresh (``fresh_serial=True``): a new token in the November era
      format (random serial, a segment observed on the November
      originals — the donor's own when it is already a November
      operation — and the ``0011881102`` tail).

    The fully random fallback is kept for callers without a donor ID.
    """
    utc = op_msk.astimezone(timezone.utc)
    stamp = (
        str(utc.year % 10)
        + f"{utc.timetuple().tm_yday:03d}"
        + utc.strftime("%H%M%S")
    )
    if len(donor_sbp) == 32 and donor_sbp[1:11].isdigit():
        if op_msk >= datetime(2026, 11, 3, tzinfo=MSK) and fresh_serial:
            segment = (
                donor_sbp[18:22]
                if donor_sbp[22:] == _SBP_TAILS[-1]
                else secrets.choice(_SBP_NOV_SEGMENTS)
            )
            while True:
                candidate = (
                    donor_sbp[0]
                    + stamp
                    + f"{secrets.randbelow(10000):04d}"
                    + secrets.choice(_SBP_MID_ALPHABET)
                    + ("0G" if op_msk >= datetime(2026, 11, 6, tzinfo=MSK) else "0B")
                    + segment
                    + _sbp_era_tail(op_msk)
                )
                if candidate != donor_sbp:
                    return candidate
        if op_msk >= datetime(2026, 11, 3, tzinfo=MSK):
            candidate = donor_sbp[0] + stamp + donor_sbp[11:22] + _sbp_era_tail(op_msk)
        else:
            candidate = donor_sbp[0] + stamp + donor_sbp[11:]
        if candidate != donor_sbp:
            return candidate
    while True:
        sbp = (
            secrets.choice("AB")
            + stamp
            + f"{secrets.randbelow(10000):04d}"
            + secrets.choice(_SBP_MID_ALPHABET)
            + ("0G" if datetime.now(MSK) >= datetime(2026, 11, 6, tzinfo=MSK) else "0B")
            + f"10{secrets.randbelow(20) + 1:02d}"
            + (_sbp_era_tail(datetime.now(MSK)) or _SBP_TAILS[-1])
        )
        if sbp != donor_sbp:
            return sbp


def make_auth_code(donor_auth: str = "") -> str:
    """6-char authorisation code, 1-5 digits (Sep donors: 1-4, Nov: up to 5)."""
    while True:
        code = "".join(secrets.choice(_AUTH_ALPHABET) for _ in range(6))
        digits = sum(char.isdigit() for char in code)
        if 1 <= digits <= 5 and code != donor_auth:
            return code


def make_iskh(op_msk: datetime, donor_iskh: str = "") -> str:
    """Исх. №: operation date (MSK) + 7 opaque digits (zeros allowed)."""
    while True:
        iskh = op_msk.strftime("%Y%m%d") + f"{secrets.randbelow(10_000_000):07d}"
        if iskh != donor_iskh:
            return iskh


def parse_sbp_moment(sbp: str) -> Optional[datetime]:
    """Operation moment (MSK) encoded in an SBP id, for verification."""
    if len(sbp) != 32 or not sbp[1:11].isdigit():
        return None
    year_digit = int(sbp[1])
    day_of_year = int(sbp[2:5])
    now_year = datetime.now(timezone.utc).year
    year = now_year - (now_year % 10) + year_digit
    if year > now_year:
        year -= 10
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=day_of_year - 1
    )
    return base.replace(
        hour=int(sbp[5:7]), minute=int(sbp[7:9]), second=int(sbp[9:11])
    ).astimezone(MSK)


# -------------------------------------------------------------- normalisation


def format_phone(value: str) -> str:
    """Any Russian phone spelling -> '+7 900 351-70-80'."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ReceiptError(f"cannot parse phone {value!r}")
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def parse_datetime(value: str, donor_seconds: int = 0) -> datetime:
    """Parse the operation moment; seconds default to the donor's SBP ones."""
    text = value.strip().replace(" в ", " ")
    has_seconds = False
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            has_seconds = fmt.endswith("%S")
            break
        except ValueError:
            continue
    else:
        raise ReceiptError(f"cannot parse datetime {value!r}")
    if not has_seconds:
        parsed = parsed.replace(second=donor_seconds)
    return parsed.replace(tzinfo=MSK)


def format_datetime(op_msk: datetime) -> str:
    return op_msk.strftime("%d.%m.%Y в %H:%M")


# ------------------------------------------------------------------- harvest


def harvest(paths: List[str], verbose: bool = True) -> yandex_font.GlyphPool:
    """Collect every YSText glyph outline and width across donor receipts."""
    pool = yandex_font.GlyphPool()
    for path in paths:
        try:
            receipt = yandex_receipt.Receipt(path)
        except Exception as error:  # noqa: BLE001 - donor set is user supplied
            if verbose:
                print(f"  skip {os.path.basename(path)}: {error}", file=sys.stderr)
            continue
        for slot in receipt.fonts.values():
            if slot.family != AMOUNT_FAMILY:
                continue
            pool.add_font(
                slot.family,
                receipt.pdf.obj(slot.fontfile_obj).decoded_stream(),
                slot.cid_to_char,
                slot.widths,
            )
        if verbose:
            print(f"  read {os.path.basename(path)} ({len(receipt.fonts)} fonts)")
    return pool


def default_glyph_donors(donor_path: str, output_path: str) -> List[str]:
    """Other PDFs next to the donor, Yandex-named ones first.

    Only files that actually parse as Yandex receipts with a
    YSText-Regular subset contribute glyphs (``harvest`` skips the
    rest), so stray PDFs in the same folder do no harm.
    """
    directory = os.path.dirname(os.path.abspath(donor_path))
    donor_abs = os.path.abspath(donor_path)
    output_abs = os.path.abspath(output_path)
    candidates = []
    for path in sorted(glob.glob(os.path.join(directory, "*.pdf"))):
        absolute = os.path.abspath(path)
        if absolute in (donor_abs, output_abs):
            continue
        candidates.append(path)
    candidates.sort(
        key=lambda p: (not os.path.basename(p).startswith("yandex_"),
                       os.path.basename(p))
    )
    return candidates


# ---------------------------------------------------------------------- show


def cmd_show(args: argparse.Namespace) -> int:
    receipt = Receipt(args.input)
    print(f"File: {args.input}")
    print()
    width = max(len(name) for name in receipt.fields)
    for name, value in receipt.fields.items():
        print(f"  {name:<{width}}  {value!r}")
    print()
    moment = parse_sbp_moment(receipt.fields.get("sbp_id", ""))
    if moment is not None:
        print(f"  SBP encodes (MSK): {moment.strftime('%d.%m.%Y %H:%M:%S')}")
    info_obj = receipt.pdf.resolve_ref(receipt.pdf.trailer_dict, b"/Info")
    if info_obj is not None:
        info = receipt.pdf.obj(info_obj).dict_bytes
        print(f"  Info dict: {info.decode('latin-1').strip()}")
    trailer_id = re.search(rb"/ID\s*\[<([0-9a-fA-F]+)>", receipt.pdf.trailer_dict)
    if trailer_id:
        print(f"  Trailer /ID: {trailer_id.group(1).decode()}")
    return 0


# --------------------------------------------------------------------- patch


def cmd_patch(args: argparse.Namespace) -> int:
    output = args.output
    if output is None:
        os.makedirs("out", exist_ok=True)
        output = os.path.join("out", f"{uuid.uuid4()}.pdf")
    if os.path.abspath(args.input) == os.path.abspath(output):
        print("error: output must not overwrite the donor", file=sys.stderr)
        return 2

    donor_paths = list(args.glyph_donor or [])
    if not donor_paths and not args.no_auto_glyph_donors:
        donor_paths = default_glyph_donors(args.input, output)

    pool_box: List[yandex_font.GlyphPool] = []

    def load_pool() -> yandex_font.GlyphPool:
        """Harvest glyph donors lazily — only when a glyph is missing."""
        if not pool_box:
            print(
                "Font lacks a needed character; harvesting glyphs from "
                "other Yandex receipts next to the donor:",
                file=sys.stderr,
            )
            pool_box.append(harvest(donor_paths, verbose=False))
        return pool_box[0]

    receipt = Receipt(
        args.input, glyph_pool_loader=load_pool if donor_paths else None
    )
    donor = dict(receipt.fields)
    receipt.creation_delta = timedelta(minutes=args.creation_delta_minutes)

    # The operation moment first: every linked identifier hangs off it.
    if args.datetime is not None:
        op = parse_datetime(args.datetime, receipt.donor_sbp_seconds())
    else:
        op = receipt._payment_dt()
        if op is None:
            raise ReceiptError("donor has no operation datetime and none was given")
    receipt.operation_dt = op

    changes: Dict[str, str] = {}

    def apply(field: str, new: Optional[str]) -> None:
        if new is None:
            return
        receipt.set(field, new)
        changes[field] = receipt.fields[field]

    # Linked identifiers are always regenerated for the new operation,
    # so a patched receipt never reuses the donor's Исх. № / SBP id /
    # auth code (rules in banks/yandex/BANK.md).
    apply("iskh", f"Исх. № {make_iskh(op, donor.get('iskh', ''))}")
    apply("doc_date", "Дата " + op.strftime("%d.%m.%Y"))
    apply("datetime", format_datetime(op))
    receipt.set(
        "sbp_id",
        make_sbp_id(op, donor.get("sbp_id", ""), fresh_serial=args.fresh_sbp_serial),
    )
    changes["sbp_id"] = receipt.fields["sbp_id"]
    receipt.set("auth", make_auth_code(donor.get("auth", "")))
    changes["auth"] = receipt.fields["auth"]

    apply("phone_from", format_phone(args.sender_phone) if args.sender_phone else None)
    apply("sender", args.sender.strip() if args.sender else None)
    apply("phone_to", format_phone(args.recipient_phone) if args.recipient_phone else None)
    apply("recipient", args.recipient.strip() if args.recipient else None)
    apply("bank_to", args.bank_to.strip() if args.bank_to else None)
    if args.amount is not None:
        receipt.set_amount(args.amount)
        changes["amount"] = receipt.fields["amount"]
    if args.fee is not None:
        fee = args.fee.strip()
        receipt.set("fee", "—" if fee in ("—", "-") else receipt.render_amount(fee))
        changes["fee"] = receipt.fields["fee"]

    for field in (
        "iskh", "doc_date", "datetime", "phone_from", "sender", "phone_to",
        "recipient", "bank_to", "amount", "fee", "auth", "sbp_id",
    ):
        if field in changes:
            print(f"  {field}: {donor.get(field)!r} -> {changes[field]!r}")
    if receipt.grown_chars:
        grown = "".join(receipt.grown_chars)
        print(f"  grown glyphs in {AMOUNT_FAMILY}: {grown!r} (from donor receipts)")

    receipt.save(output, reidentify=True, mode=args.save_mode)
    print()
    print(f"Written {output} ({os.path.getsize(output)} bytes, donor {os.path.getsize(args.input)} bytes)")
    print(f"PDF CreationDate {receipt._pdf_timestamp()} (операция МСК +{args.creation_delta_minutes:g} мин, в UTC)")
    print(f"SBP encodes (MSK): {parse_sbp_moment(receipt.fields['sbp_id']).strftime('%d.%m.%Y %H:%M:%S')}")
    return 0


# ----------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch_yandex.py",
        description="Patch Yandex Bank receipt PDFs field by field.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # The documented form has no subcommand: donor.pdf [out.pdf] --amount ...
    parser.add_argument("input", nargs="?", help="template (donor) receipt PDF")
    parser.add_argument(
        "output",
        nargs="?",
        help="destination PDF (default: out/<uuid4>.pdf, as Yandex names them)",
    )
    parser.add_argument("--datetime", help='operation moment MSK, e.g. "03.11.2026 14:25"')
    parser.add_argument("--sender-phone", help="sender phone")
    parser.add_argument("--sender", help="sender full name")
    parser.add_argument("--recipient-phone", help="recipient phone")
    parser.add_argument("--recipient", help="recipient full name")
    parser.add_argument("--bank-to", dest="bank_to", help="recipient bank")
    parser.add_argument("--amount", help='e.g. "17 350,00 ₽"')
    parser.add_argument("--fee", help='e.g. "—" or "50,00 ₽"')
    parser.add_argument(
        "--creation-delta-minutes",
        type=float,
        default=5,
        help="CreationDate lag after the operation, minutes (default: 5)",
    )
    parser.add_argument(
        "--glyph-donor",
        action="append",
        default=[],
        metavar="PDF",
        help="genuine Yandex receipt to harvest missing glyphs from "
        "(repeatable; default: other PDFs next to the donor)",
    )
    parser.add_argument(
        "--no-auto-glyph-donors",
        action="store_true",
        help="do not auto-discover glyph donors next to the donor",
    )
    parser.add_argument(
        "--save-mode",
        choices=("natural", "incremental"),
        default="natural",
        help="natural: single-pass Rocket-style save with exact font "
        "subsets (default); incremental: donor + incremental update",
    )
    parser.add_argument(
        "--fresh-sbp-serial",
        action="store_true",
        help="build a fresh November-era SBP serial token instead of "
        "inheriting the donor's genuine token (default: inherit)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["show"]:
        show_parser = argparse.ArgumentParser(
            prog="patch_yandex.py show",
            description="Print the receipt's fields.",
        )
        show_parser.add_argument("input", help="receipt PDF")
        args = show_parser.parse_args(argv[1:])
        try:
            return cmd_show(args)
        except (ReceiptError, KeyError, FileNotFoundError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.input:
        parser.print_usage(sys.stderr)
        print("error: donor.pdf is required", file=sys.stderr)
        return 2
    try:
        return cmd_patch(args)
    except (ReceiptError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
