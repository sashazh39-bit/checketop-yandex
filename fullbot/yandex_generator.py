# -*- coding: utf-8 -*-
"""Yandex Bank statement generator — public entry point.

Bot/pdf-service signature ``generate(recipient, phone, bank, amount, dt,
meta_out)``: one SBP transfer on an EDS-account statement. The render is
delegated to ``yandex_statement_v2.generate_statement`` (correct genuine
geometry/anchors, pagination, Decimal money with NBSP thousands, equal
lowercase trailer /ID actually written to the file, CreationDate =
generation moment). The previous implementation (``yandex_gen_stable``)
mirrored the page vertically, let PyMuPDF write unequal UPPERCASE /ID
halves while ``meta_out["id"]`` claimed an equal lowercase pair, put
CreationDate *before* the operation and never grouped thousands — every
statement failed genuine-corpus forensic checks for a different visible
reason depending on name length / amount, which read as random
green/red flapping. ``yandex_gen_stable`` stays importable for compat
(re-exports below) but is no longer the render path.

Honest limitation (also in meta_out): synthetic PyMuPDF render is NOT
byte-faithful — font is Arial/DejaVu instead of YSText Type0 subsets,
``byte_faithful=False, engine="PyMuPDF"``. Text, amounts, anchors, /ID
and CreationDate laws match the genuine corpus; font-program forensics
do not. For byte-faithful statements use
``yandex_statement_v2.generate_from_donor`` (donor text unchanged).
"""
import os as _os, pickle as _pkl
from datetime import datetime as _datetime, timezone as _timezone
from decimal import Decimal as _Decimal

from yandex_gen_stable import (  # noqa: F401  (compat re-exports)
    _PROD, _CREATOR, _MSK,
    _fmt_amount, _fmt_phone, _make_isx_no, _make_id, _creation_date,
)

__file__ = _os.path.abspath(__file__)
_DIR = _os.path.dirname(__file__)
_CATALOG_PATH = _os.path.join(_DIR, "_yandex_catalog.pkl")
_CHARS = None


def _load_chars():
    """Lazily load the supported-char set from the catalog (built by
    build_yandex_catalog.py). Falls back to a permissive Cyrillic+Latin
    +digits+punctuation baseline if the catalog is missing or has no
    `chars` field — the renderer's DejaVu Sans fallback covers these."""
    global _CHARS
    if _CHARS is not None:
        return _CHARS
    s = set()
    try:
        with open(_CATALOG_PATH, "rb") as f:
            cat = _pkl.load(f)
        s |= set(cat.get("chars", ""))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    if not s:
        ru = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяё"
        lat = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        s = set(ru + lat + "0123456789" + " ()+-.,:;«»—–№₽/%&")
    _CHARS = s
    return s


def check_text(text, face=None):
    """Chars of `text` not covered by the Yandex font catalog.

    Mirrors ozon_generator.check_text / vtb_generator.check_text so the bot
    can validate user input (name, bank) before generation. `face` is
    accepted for signature compatibility with the other banks and ignored
    (Yandex uses one text face for all dynamic fields)."""
    cov = _load_chars()
    return [c for c in (text or "") if c not in cov]


def generate(recipient, phone, bank, amount, dt=None, meta_out=None):
    """One-transfer EDS statement (bot signature) via yandex_statement_v2.

    ``amount`` >= 0 — incoming SBP transfer, < 0 — outgoing (module of
    the amount is the operation sum, sign selects the description, as in
    the genuine corpus). ``meta_out`` keeps every legacy key
    (isx_no/creation/op_dt/op_id/amount_fmt/phone_fmt/period_from/
    period_to/stmt_date/id/donor) with values read from the actual file
    (id == trailer /ID, both halves equal lowercase), plus v2 keys."""
    import yandex_statement_v2 as _v2

    if dt is None:
        op_dt = _datetime.now(_MSK)
    elif dt.tzinfo is None:
        op_dt = dt.replace(tzinfo=_MSK)
    else:
        op_dt = dt.astimezone(_MSK)
    signed = _Decimal(str(amount))
    # Keep the sign on the amount itself: v2 totals/directions are driven
    # by the signed amount; abs() here silently turned an outgoing
    # transfer into an incoming one in the printed statement.
    op = _v2.Operation(
        op_dt, signed, sender=str(recipient or ""),
        phone=str(phone or ""), bank=str(bank or ""),
        outgoing=signed < 0)
    v2_meta = {}
    pdf = _v2.generate_statement(
        [op], opening_balance=0,
        period_from=op_dt.date(), period_to=op_dt.date(),
        statement_date=_datetime.now(_MSK).date(),
        meta_out=v2_meta)
    if meta_out is not None:
        meta_out.clear()
        meta_out.update(v2_meta)
        meta_out.update({
            "op_dt": op_dt,
            "op_id": v2_meta["isx_no"],  # legacy alias (statement has no SBP id)
            "amount_fmt": _v2.fmt_signed(signed),
            "phone_fmt": _fmt_phone(phone),
            "period_from": op_dt.strftime("%d.%m.%Y"),
            "period_to": op_dt.strftime("%d.%m.%Y"),
            "stmt_date": _datetime.now(_MSK).strftime("%d.%m.%Y"),
            "id": (v2_meta["doc_id"], v2_meta["doc_id"]),
            "donor": "yandex",
        })
    return pdf
