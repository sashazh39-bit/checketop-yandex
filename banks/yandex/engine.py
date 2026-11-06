#!/usr/bin/env python3
"""Bot-facing engine for Yandex Bank receipts.

This is the Yandex analogue of ``banks/rocket/engine.py``: one function,
``generate_yandex(...)``, takes the already-collected transfer data and
writes a finished PDF through the existing Yandex patcher
(``patch_yandex.py`` + ``yandex_receipt.py`` + ``yandex_font.py`` in the
repository root).  The patcher's frozen ``@podlinno_bot`` 45/45
configuration (banks/yandex/BANK.md, 04.11.2026) is not re-invented
here:

* save is always ``mode="natural"`` — single pass, one
  ``startxref``/``%%EOF``, no ``/Prev``, no padding, no incremental
  tail; font subsets are pruned to exactly the used glyphs and all
  three subset tags are re-issued on every generation;
* the SBP serial token (positions 12-22) is inherited from the donor,
  only the UTC time header is rebuilt and the tail is moved to the
  current era ``0011881102`` — a random serial is never assembled
  (``patch_yandex.make_sbp_id`` with ``fresh_serial=False``);
* the authorisation code is random from the observed alphabet
  ``ABCFGIJKLMNOPRTVYZ`` + digits with 1-5 digits, chosen so the
  template can type it with its own glyphs, without growing the font;
* ``Исх. №`` is the operation date (MSK) + 7 digits, the ``Дата``
  field is the operation date, ``CreationDate`` is the generation
  moment (not operation + a round number of minutes), the trailer
  ``/ID`` is fresh and unique, and the file mtime is CreationDate + 1s
  (the last two are done by ``Receipt.save(..., reidentify=True)``);
* commission and message are always ``—``, as in the whole donor
  corpus; the output file name is ``<uuid4>.pdf``, as in genuine
  Yandex downloads.

One-time donor tokens
---------------------
The checker remembers an SBP serial token: one genuine token may be
used in a generation only once, a repeat is caught as "this receipt
or operation has been seen before".  The engine therefore keeps a
template pool (``donors/`` next to this file) and a spent-token
registry (``donors/spent_tokens.json``, a state file that survives
bot restarts).  Every generation picks a donor whose token is not
spent yet, marks the token spent in the registry *before* generating
and never returns it — not even when the generation or the sending
fails afterwards.  When no unused token is left, the engine honestly
refuses and asks for a new genuine Yandex receipt instead of reusing
a token or inventing a serial.

The chosen template must also be able to type every supplied string
with its own already-filled glyphs, without growing the font (font
growth through fontTools is exactly the fingerprint the frozen
configuration avoids).  The glyph pool is the union over all genuine
receipts in the pool (including, when the owner puts it there, the
"full font" donor ``6523d7c5-...pdf``); a name/bank character outside
that coverage is an honest refusal, not a foreign-font substitution.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _path in (_REPO_ROOT, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import patch_yandex  # noqa: E402
import yandex_font  # noqa: E402
import yandex_receipt  # noqa: E402
from yandex_receipt import MSK, Receipt, ReceiptError  # noqa: E402

__all__ = [
    "generate_yandex",
    "serial_token",
    "load_spent_tokens",
    "donor_pool_paths",
    "NoUnusedDonorTokenError",
    "GlyphCoverageError",
]

DEFAULT_POOL_DIR = _HERE / "donors"
DEFAULT_REGISTRY_PATH = DEFAULT_POOL_DIR / "spent_tokens.json"

# Tokens already spent in generations before this registry existed
# (banks/yandex/BANK.md, 03-04.11.2026).  Keys are the full serial
# tokens (SBP positions 12-22); the clean donor aea36931 (token
# 33340H...) is deliberately NOT here — it never took part in a
# generation with its token.
SEEDED_SPENT_TOKENS: Dict[str, dict] = {
    "6381I0B1013": {"donor": "c0787b9b-….pdf", "seeded": "2026-11-04"},
    "906260B1008": {"donor": "48a8181b-….pdf", "seeded": "2026-11-04"},
    "3380O0B1017": {"donor": "02e390b1-….pdf", "seeded": "2026-11-04"},
    "1720E0B1007": {"donor": "6523d7c5-….pdf", "seeded": "2026-11-04"},
    "9446L0B1006": {"donor": "390ab9c7-….pdf", "seeded": "2026-11-04"},
}

_SPACE_CHARS = set(" \u00a0\u2009\u202f")
_AUTH_LETTERS = "ABCFGIJKLMNOPRTVYZ"


class NoUnusedDonorTokenError(ReceiptError):
    """All genuine SBP serial tokens in the donor pool are spent."""


class GlyphCoverageError(ReceiptError):
    """No unused template can type the supplied strings as-is."""


# --------------------------------------------------------------------------
# spent-token registry (state file, survives restarts)
# --------------------------------------------------------------------------


def registry_path(path: Optional[Union[str, Path]] = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("YANDEX_TOKEN_REGISTRY")
    return Path(override).expanduser() if override else DEFAULT_REGISTRY_PATH


def _load_registry(path: Path) -> Dict[str, dict]:
    spent: Dict[str, dict] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("spent", raw) if isinstance(raw, dict) else {}
            if isinstance(entries, dict):
                for token, info in entries.items():
                    spent[str(token)] = info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            spent = {}
    for token, info in SEEDED_SPENT_TOKENS.items():
        spent.setdefault(token, dict(info))
    return spent


def _save_registry(path: Path, spent: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "spent": dict(sorted(spent.items()))}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load_spent_tokens(path: Optional[Union[str, Path]] = None) -> set:
    """All spent serial tokens (registry file + the 04.11.2026 seeds)."""
    return set(_load_registry(registry_path(path)))


@contextmanager
def _registry_lock(path: Path):
    """Cross-process lock around select-and-spend (registry is state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    handle = None
    deadline = time.monotonic() + 30
    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise ReceiptError(f"token registry is locked: {lock_path}")
            time.sleep(0.05)
    try:
        os.write(handle, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(handle)
        try:
            lock_path.unlink()
        except OSError:
            pass


def serial_token(sbp_id: str) -> str:
    """The one-time serial token of an SBP id: positions 12-22."""
    return str(sbp_id or "")[11:22]


# --------------------------------------------------------------------------
# normalisation (input side of the contract)
# --------------------------------------------------------------------------


def _parse_operation_dt(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = patch_yandex.parse_datetime(str(value), donor_seconds=0)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def _format_person(value: str) -> str:
    """ФИО -> the printed Yandex form ``Имя Отчество Ф.``.

    Accepts the usual ``Фамилия Имя Отчество`` (or two-word) order and
    a name already in printed form (``ИВАН ИВАНОВИЧ И``).
    """
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]

    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.strip(".").split("-"))

    if not tokens:
        raise ReceiptError(f"cannot parse name {value!r}")
    if len(tokens[-1].strip(".")) == 1 and len(tokens) >= 2:
        parts = [title(t) for t in tokens[:-1]]
        return " ".join(parts) + f" {tokens[-1].strip('.')[0].upper()}."
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{title(given)} {title(patronymic)} {surname.strip('.')[0].upper()}."
    if len(tokens) == 2:
        surname, given = tokens
        return f"{title(given)} {surname.strip('.')[0].upper()}."
    raise ReceiptError(f"cannot parse name {value!r}")


def _clean_bank(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ReceiptError("recipient bank is empty")
    return cleaned


# --------------------------------------------------------------------------
# glyph coverage (template's own filled glyphs, no font growth)
# --------------------------------------------------------------------------


def _filled_chars(receipt: Receipt, slot) -> set:
    chars = set()
    for cid, char in slot.cid_to_char.items():
        if char in _SPACE_CHARS:
            if cid in slot.widths:
                chars.add(char)
        elif cid in slot.widths and receipt._glyph_filled(slot, cid):
            chars.add(char)
    return chars


def _slot_coverage(receipt: Receipt) -> Dict[str, set]:
    return {
        resource: _filled_chars(receipt, slot)
        for resource, slot in receipt.fonts.items()
    }


def _missing_by_field(receipt: Receipt, planned: Dict[str, str]) -> Dict[str, List[str]]:
    coverage = _slot_coverage(receipt)
    missing: Dict[str, List[str]] = {}
    for field, text in planned.items():
        run = receipt._fields.get(field)
        if run is None:
            missing[field] = sorted(set(text))
            continue
        available = coverage.get(run.font, set())
        absent = sorted({c for c in text if c not in available})
        if absent:
            missing[field] = absent
    return missing


def _auth_code_for(receipt: Receipt, donor_auth: str = "") -> Optional[str]:
    """Random auth code the template can type without growing glyphs."""
    run = receipt._fields.get("auth")
    if run is None:
        return None
    available = _filled_chars(receipt, receipt.fonts[run.font])
    letters = [c for c in _AUTH_LETTERS if c in available]
    digits = [c for c in "0123456789" if c in available]
    if not letters or not digits:
        return None
    rng = secrets.SystemRandom()
    while True:
        digit_count = rng.randint(1, 5)
        chars = [rng.choice(digits) for _ in range(digit_count)]
        chars += [rng.choice(letters) for _ in range(6 - digit_count)]
        rng.shuffle(chars)
        code = "".join(chars)
        if code != donor_auth:
            return code


def _iskh_for(receipt: Receipt, op: datetime, donor_iskh: str = "") -> Optional[str]:
    run = receipt._fields.get("iskh")
    if run is None:
        return None
    available = _filled_chars(receipt, receipt.fonts[run.font])
    digits = [c for c in "0123456789" if c in available]
    if not digits:
        return None
    prefix = op.strftime("%Y%m%d")
    while True:
        iskh = prefix + "".join(secrets.choice(digits) for _ in range(7))
        if f"Исх. № {iskh}" != donor_iskh:
            return iskh


# --------------------------------------------------------------------------
# donor pool
# --------------------------------------------------------------------------


def donor_pool_paths(
    donor_path: Optional[Union[str, Path]] = None,
    pool_dir: Optional[Union[str, Path]] = None,
) -> List[Path]:
    if donor_path is not None:
        donor = Path(donor_path).expanduser()
        if donor.is_dir():
            return sorted(donor.glob("*.pdf"))
        return [donor]
    directory = Path(pool_dir).expanduser() if pool_dir else DEFAULT_POOL_DIR
    return sorted(directory.glob("*.pdf"))


class _Candidate:
    def __init__(self, path: Path, receipt: Receipt):
        self.path = path
        self.fields = dict(receipt.fields)
        self.sbp_id = self.fields.get("sbp_id", "")
        self.token = serial_token(self.sbp_id)
        self.moment = patch_yandex.parse_sbp_moment(self.sbp_id)
        self.planned: Dict[str, str] = {}
        self.auth_code: Optional[str] = None
        self.iskh: Optional[str] = None
        self.op: Optional[datetime] = None


def _assess_candidates(paths: Iterable[Path], planned_base: Dict[str, str], op_base: datetime):
    """Parse donors, keep the ones that can type everything as-is."""
    candidates: List[_Candidate] = []
    pool_chars: set = set()
    missing_union: Dict[str, set] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            receipt = Receipt(str(path))
        except Exception:  # noqa: BLE001 - a stray file is not a donor
            continue
        for slot in receipt.fonts.values():
            if slot.family == yandex_receipt.AMOUNT_FONT_FAMILY:
                pool_chars |= _filled_chars(receipt, slot)
        candidate = _Candidate(path, receipt)
        if len(candidate.sbp_id) != 32 or not candidate.token:
            continue
        if candidate.fields.get("message") not in (None, "—"):
            continue  # e.g. the full-font donor: glyph source, not a template
        op = op_base
        if op.second == 0 and op.microsecond == 0:
            op = op.replace(second=receipt.donor_sbp_seconds())
        candidate.op = op
        try:
            rendered_amount = receipt.render_amount(planned_base["amount"])
        except ReceiptError as error:
            if "cannot parse amount" in str(error):
                raise
            continue
        planned = dict(planned_base)
        planned["amount"] = rendered_amount
        planned["sbp_id"] = patch_yandex.make_sbp_id(op, candidate.sbp_id)
        planned["iskh"] = "Исх. № " + op.strftime("%Y%m%d")
        planned["doc_date"] = "Дата " + op.strftime("%d.%m.%Y")
        planned["datetime"] = patch_yandex.format_datetime(op)
        candidate.auth_code = _auth_code_for(receipt, candidate.fields.get("auth", ""))
        candidate.iskh = _iskh_for(receipt, op, candidate.fields.get("iskh", ""))
        if candidate.auth_code is None or candidate.iskh is None:
            continue
        planned["auth"] = candidate.auth_code
        missing = _missing_by_field(receipt, planned)
        if missing:
            for chars in missing.values():
                for char in chars:
                    missing_union.setdefault(char, set()).add(path.name)
            continue
        candidate.planned = planned
        candidates.append(candidate)
    # Freshest genuine operation first (November donors before September).
    candidates.sort(
        key=lambda c: (c.moment or datetime.min.replace(tzinfo=MSK), c.path.name),
        reverse=True,
    )
    return candidates, pool_chars, missing_union


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def generate_yandex(
    amount,
    operation_dt,
    sender_name: str,
    sender_phone: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    output_path: Optional[Union[str, Path]] = None,
    donor_path: Optional[Union[str, Path]] = None,
    *,
    donor_pool_dir: Optional[Union[str, Path]] = None,
    token_registry_path: Optional[Union[str, Path]] = None,
    glyph_donor_paths: Optional[List[Union[str, Path]]] = None,
    generated_at: Optional[datetime] = None,
) -> Path:
    """Generate a Yandex receipt PDF and return its path.

    Contract (PROJECT.md): see the module docstring for the frozen
    generation configuration, the one-time donor-token pool/registry
    and the glyph-coverage rule.  ``donor_path`` may pin one template
    (or a pool directory); its token is still spent exactly once.
    Extra keyword arguments exist for tests and the bot's server:
    a different pool directory / registry file, extra glyph-source
    receipts and a fixed generation moment.
    """
    op_base = _parse_operation_dt(operation_dt)
    planned_base = {
        "phone_from": patch_yandex.format_phone(sender_phone),
        "sender": _format_person(sender_name),
        "phone_to": patch_yandex.format_phone(recipient_phone),
        "recipient": _format_person(recipient_name),
        "bank_to": _clean_bank(recipient_bank),
        "amount": str(amount),
        "fee": "—",
        "message": "—",
    }

    if output_path is None:
        output = _HERE / "out" / f"{uuid.uuid4()}.pdf"
    else:
        output = Path(output_path).expanduser()

    pool_paths = donor_pool_paths(donor_path, donor_pool_dir)
    extra_paths = [Path(p).expanduser() for p in (glyph_donor_paths or [])]
    candidates, pool_chars, missing_union = _assess_candidates(
        list(pool_paths) + extra_paths, planned_base, op_base
    )
    if donor_path is not None and not Path(donor_path).expanduser().is_dir():
        candidates = [c for c in candidates if c.path == Path(donor_path).expanduser()]

    reg_path = registry_path(token_registry_path)
    chosen: Optional[_Candidate] = None
    with _registry_lock(reg_path):
        spent = _load_registry(reg_path)
        free = [c for c in candidates if c.token not in spent]
        if not free:
            if candidates:
                raise NoUnusedDonorTokenError(
                    "Нет свободного токена: все настоящие чеки Яндекса в пуле "
                    "уже использованы по одному разу. Нужен новый настоящий "
                    "чек Яндекса — свежий перевод и скачанная квитанция; "
                    "положите её PDF в banks/yandex/donors/."
                )
            if missing_union:
                absent = "".join(sorted(missing_union))
                outside = "".join(c for c in sorted(missing_union) if c not in pool_chars)
                detail = (
                    f"Буквы вне пула глифов настоящих чеков Яндекса: {outside}. "
                    if outside
                    else ""
                )
                raise GlyphCoverageError(
                    detail
                    + f"Ни один свободный шаблон не набирает введённые данные "
                    f"своими глифами без доращивания шрифта (не хватает: {absent}). "
                    "Нужен настоящий чек Яндекса с этими буквами."
                )
            raise NoUnusedDonorTokenError(
                "В пуле нет настоящих чеков Яндекса. Нужен новый настоящий "
                "чек Яндекса — свежий перевод и скачанная квитанция; "
                "положите её PDF в banks/yandex/donors/."
            )
        chosen = free[0]
        spent[chosen.token] = {
            "donor": chosen.path.name,
            "spent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "operation": chosen.op.isoformat() if chosen.op else None,
        }
        _save_registry(reg_path, spent)

    assert chosen is not None and chosen.op is not None
    template = chosen.path
    if output.resolve() == template.resolve():
        raise ReceiptError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    receipt = Receipt(str(template))
    donor = dict(receipt.fields)
    op = chosen.op
    receipt.operation_dt = op
    gen = generated_at or datetime.now(MSK)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=MSK)
    receipt.creation_delta = gen.astimezone(MSK) - op

    planned = chosen.planned
    receipt.set("iskh", f"Исх. № {chosen.iskh}")
    receipt.set("doc_date", planned["doc_date"])
    receipt.set("datetime", planned["datetime"])
    receipt.set("sbp_id", planned["sbp_id"])
    receipt.set("auth", planned["auth"])
    receipt.set("phone_from", planned["phone_from"])
    receipt.set("sender", planned["sender"])
    receipt.set("phone_to", planned["phone_to"])
    receipt.set("recipient", planned["recipient"])
    receipt.set("bank_to", planned["bank_to"])
    receipt.set_amount(planned_base["amount"])
    receipt.set("fee", "—")
    if "message" in receipt.fields:
        receipt.set("message", "—")

    receipt.save(str(output), reidentify=True, mode="natural")

    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/Prev" in raw
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            "natural save failed: generated file is not a single-pass PDF"
        )
    return output
