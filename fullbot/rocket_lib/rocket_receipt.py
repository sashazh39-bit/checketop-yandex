#!/usr/bin/env python3
"""Field-level model of a Rocket Bank receipt.

Fields are addressed by the grey label printed above each value rather
than by font resource number, so the SBP and card-transfer layouts are
handled by the same code even though they number their fonts differently.
"""

from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

MSK = timezone(timedelta(hours=3))

import rocket_font
import rocket_pdf

# Grey labels, in the wording the bank uses, mapped to logical field names.
LABEL_FIELDS: Dict[str, str] = {
    "КОМИССИЯ": "fee",
    "ПЕРЕВОД ЧЕРЕЗ СБП": "direction",
    "ПЕРЕВОД ПО НОМЕРУ КАРТЫ": "direction",
    "ПОЛУЧАТЕЛЬ": "recipient",
    "КАРТА ПОЛУЧАТЕЛЯ": "card",
    "ТЕЛЕФОН ПОЛУЧАТЕЛЯ": "phone_to",
    "БАНК ПОЛУЧАТЕЛЯ": "bank_to",
    "ОТПРАВИТЕЛЬ": "sender",
    "ТЕЛЕФОН ОТПРАВИТЕЛЯ": "phone_from",
    "БАНК ОТПРАВИТЕЛЯ": "bank_from",
    "СЧЁТ ОТПРАВИТЕЛЯ": "account",
    "ИДЕНТИФИКАТОР ОПЕРАЦИИ СБП": "sbp_id",
    "СТАТУС": "status",
    "НОМЕР ДОКУМЕНТА": "doc_number",
}

FORMAT_SBP = "sbp"
FORMAT_CARD = "card"


@dataclass
class TextRun:
    """One `BT ... <hex>Tj ... ET` block in the page content stream."""

    font: str
    size: float
    x: float
    y: float
    is_label: bool
    hex_body: str
    text: str
    span: tuple  # (start, end) of the hex body inside the content stream


@dataclass
class FontSlot:
    """Every PDF object that together makes up one Type0 font resource."""

    resource: str
    type0_obj: int
    cidfont_obj: int
    descriptor_obj: int
    fontfile_obj: int
    tounicode_obj: int
    family: str
    cid_to_char: Dict[int, str] = dataclass_field(default_factory=dict)
    widths: Dict[int, int] = dataclass_field(default_factory=dict)


class ReceiptError(RuntimeError):
    pass


class Receipt:
    """A receipt opened for inspection and field-level editing."""

    def __init__(self, path: str, cache: Optional[rocket_font.GlyphCache] = None):
        self.path = path
        self.pdf = rocket_pdf.load(path)
        self.cache = cache
        self._page = self._find_page()
        self._content_obj = self.pdf.resolve_ref(self._page.dict_bytes, b"/Contents")
        if self._content_obj is None:
            raise ReceiptError("page has no /Contents reference")
        self._content = self.pdf.obj(self._content_obj).decoded_stream()
        self._content_original = self._content
        self.fonts = self._load_fonts()
        self.runs = self._parse_runs()
        self._fields = self._map_fields()
        self._touched_fonts: set = set()  # resource names whose text was changed
        # Operation moment with seconds, when the caller knows it.  The
        # printed date field carries only minutes; the SBP id and the PDF
        # CreationDate need the full moment (see _created_dt).
        self.operation_dt: Optional[datetime] = None
        self.creation_delta: timedelta = timedelta(minutes=5)
        self.rotate_tags: bool = True
        # When True, font extension adds only the glyphs actually needed (keeps
        # the file as small as possible — required for same-size / refit saves).
        # When False, RocketSans-Wide pulls in the full donor set (0-9 + ₽) to
        # byte-match iText's reference output for the checker (incremental mode).
        self.minimal_glyphs: bool = True

    # ------------------------------------------------------------- structure

    def _find_page(self) -> rocket_pdf.PdfObject:
        for obj in self.pdf.find_objects(b"/Type/Page"):
            if b"/Contents" in obj.dict_bytes:
                return obj
        raise ReceiptError("no page object with contents found")

    def _load_fonts(self) -> Dict[str, FontSlot]:
        resources = re.search(rb"/Font\s*<<(.+?)>>", self._page.dict_bytes, re.DOTALL)
        if not resources:
            raise ReceiptError("page resources carry no /Font dictionary")

        slots: Dict[str, FontSlot] = {}
        for item in re.finditer(rb"/(F\d+)\s+(\d+)\s+0\s+R", resources.group(1)):
            resource = item.group(1).decode("ascii")
            type0 = int(item.group(2))
            type0_dict = self.pdf.obj(type0).dict_bytes

            descendants = re.search(rb"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", type0_dict)
            tounicode = self.pdf.resolve_ref(type0_dict, b"/ToUnicode")
            if not descendants or tounicode is None:
                continue
            cidfont = int(descendants.group(1))
            cidfont_dict = self.pdf.obj(cidfont).dict_bytes

            descriptor = self.pdf.resolve_ref(cidfont_dict, b"/FontDescriptor")
            if descriptor is None:
                continue
            fontfile = self.pdf.resolve_ref(self.pdf.obj(descriptor).dict_bytes, b"/FontFile3")
            if fontfile is None:
                continue

            cff_bytes = self.pdf.obj(fontfile).decoded_stream()
            slots[resource] = FontSlot(
                resource=resource,
                type0_obj=type0,
                cidfont_obj=cidfont,
                descriptor_obj=descriptor,
                fontfile_obj=fontfile,
                tounicode_obj=tounicode,
                family=rocket_font.family_of(cff_bytes),
                cid_to_char=rocket_font.parse_tounicode(
                    self.pdf.obj(tounicode).decoded_stream()
                ),
                widths=rocket_font.parse_widths(cidfont_dict),
            )
        if not slots:
            raise ReceiptError("no usable Type0 fonts on the page")
        return slots

    def _parse_runs(self) -> List[TextRun]:
        text = self._content.decode("latin-1")
        runs: List[TextRun] = []
        font = ""
        size = 0.0
        x = y = 0.0
        is_label = False

        for match in re.finditer(
            r"/(F\d+)\s+([\d.]+)\s+Tf"
            r"|([\d.]+)\s+([\d.]+)\s+Td"
            r"|/Gs\d+\s+gs"
            r"|<([0-9a-fA-F]+)>\s*Tj"
            r"|\bET\b",
            text,
        ):
            token = match.group(0)
            if match.group(1):
                font, size = match.group(1), float(match.group(2))
            elif match.group(3):
                x, y = float(match.group(3)), float(match.group(4))
            elif token.endswith("gs"):
                is_label = True
            elif match.group(5):
                body = match.group(5)
                decoded = self._decode(font, body)
                if decoded.strip():
                    runs.append(
                        TextRun(
                            font=font,
                            size=size,
                            x=x,
                            y=y,
                            is_label=is_label,
                            hex_body=body,
                            text=decoded,
                            span=(match.start(5), match.end(5)),
                        )
                    )
            elif token == "ET":
                is_label = False
        return runs

    def _decode(self, resource: str, hex_body: str) -> str:
        slot = self.fonts.get(resource)
        if slot is None:
            return ""
        out = []
        for index in range(0, len(hex_body), 4):
            cid = int(hex_body[index : index + 4], 16)
            out.append(slot.cid_to_char.get(cid, "\ufffd"))
        return "".join(out)

    def _map_fields(self) -> Dict[str, TextRun]:
        """Pair each grey label with the value run that follows it."""
        mapping: Dict[str, TextRun] = {}
        values = [run for run in self.runs if run.text.strip()]

        for index, run in enumerate(values):
            if index == 0 and run.is_label:
                mapping["date"] = run
                continue
            if index == 1 and not run.is_label:
                mapping["amount"] = run
                continue
            if not run.is_label:
                continue
            name = LABEL_FIELDS.get(run.text.strip())
            if not name:
                continue
            for candidate in values[index + 1 :]:
                if not candidate.is_label:
                    mapping[name] = candidate
                    break
        return mapping

    # ------------------------------------------------------------- accessors

    @property
    def format(self) -> str:
        return FORMAT_CARD if "card" in self._fields else FORMAT_SBP

    @property
    def fields(self) -> Dict[str, str]:
        return {name: run.text for name, run in self._fields.items()}

    def field_charset(self, name: str) -> Dict[str, int]:
        """Characters the field's own font subset can already render."""
        run = self._require(name)
        return rocket_font.invert_cmap(self.fonts[run.font].cid_to_char)

    def _require(self, name: str) -> TextRun:
        if name not in self._fields:
            available = ", ".join(sorted(self._fields))
            raise ReceiptError(f"unknown field {name!r}; available: {available}")
        return self._fields[name]

    # -------------------------------------------------------------- mutation

    def set(self, name: str, value: str) -> None:
        """Set a field, pulling in donor glyphs when the subset lacks them."""
        run = self._require(name)
        slot = self.fonts[run.font]
        char_to_cid = rocket_font.invert_cmap(slot.cid_to_char)
        missing = sorted({char for char in value if char not in char_to_cid})

        if missing:
            self._extend_font(slot, missing)
            char_to_cid = rocket_font.invert_cmap(slot.cid_to_char)

        # A genuine subset carries exactly the glyphs of its one line.
        # Drop glyphs the new value no longer uses (stub them, prune /W
        # and ToUnicode) — but only when this font serves no other run.
        keep = set(value)
        extra_cids = [
            cid for cid, char in slot.cid_to_char.items() if char not in keep
        ]
        runs_with_font = [other for other in self.runs if other.font == run.font]
        if extra_cids and len(runs_with_font) == 1:
            self._prune_font(slot, extra_cids)
            char_to_cid = rocket_font.invert_cmap(slot.cid_to_char)

        hex_body = rocket_font.encode_text(value, char_to_cid).decode("ascii")
        if hex_body == run.hex_body:
            return  # value encodes identically — nothing to change
        self._replace_run(run, hex_body, value)
        self._touched_fonts.add(run.font)

    def _payment_dt(self) -> Optional[datetime]:
        """Parse the receipt date as Moscow time."""
        if self.operation_dt is not None:
            return self.operation_dt.astimezone(MSK)
        raw = self.fields.get("date") or ""
        match = re.match(
            r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", raw
        )
        if not match:
            return None
        day, month, year, hour, minute = (int(p) for p in match.groups())
        return datetime(year, month, day, hour, minute, tzinfo=MSK)

    def _created_dt(self) -> datetime:
        """PDF/file creation time: a few minutes after the payment, in UTC.

        Real Rocket receipts are emitted ~5 minutes after the operation
        (payment 03:03 MSK → CreationDate 00:08Z, filesystem 02:08 +0200).
        """
        payment = self._payment_dt()
        if payment is None:
            return datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return (payment + self.creation_delta).astimezone(timezone.utc).replace(
            microsecond=0
        )

    def _pdf_timestamp(self) -> str:
        """Build a PDF date ``D:YYYYMMDDHHmmssZ`` from the receipt's date field."""
        return self._created_dt().strftime("D:%Y%m%d%H%M%SZ")

    def _stamp_filesystem(self, path: str) -> None:
        """Set mtime/atime (and macOS birthtime) to the PDF creation moment."""
        created = self._created_dt() + timedelta(seconds=1)  # original is :01
        ts = created.timestamp()
        os.utime(path, (ts, ts))
        local = created.astimezone()
        stamp = local.strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(
            ["SetFile", "-d", stamp, "-m", stamp, path],
            check=False,
            capture_output=True,
        )

    def set_account_tail(self, digits: str) -> None:
        """Rewrite only the trailing digits of the sender account number."""
        if not digits.isdigit():
            raise ReceiptError("account tail must be digits only")
        current = self._require("account").text
        tail = current[len(current) - len(digits) :]
        if len(digits) > len(current) or " " in tail:
            raise ReceiptError(
                f"tail of {len(digits)} digits does not fit the last group "
                f"of {current!r}"
            )
        self.set("account", current[: len(current) - len(digits)] + digits)

    def _extend_font(self, slot: FontSlot, missing: List[str]) -> None:
        if self.cache is None:
            raise ReceiptError(
                f"font {slot.family} lacks {''.join(missing)!r} and no glyph cache "
                "was supplied; run the prepare step first"
            )
        donor_chars = self.cache.available_chars(slot.family)
        unavailable = [char for char in missing if char not in donor_chars]
        if unavailable:
            raise ReceiptError(
                f"no donor glyph for {''.join(unavailable)!r} in {slot.family}; "
                "add a receipt containing those characters to the donor set"
            )

        pool = self.cache.glyphs.get(slot.family, {})
        donor_widths = self.cache.widths.get(slot.family, {})

        # For the amount font (RocketSans-Wide), iText re-subsets with the full
        # donor set (all 10 digits + ₽), not just the minimum needed.  We mirror
        # that only when byte-matching the checker (minimal_glyphs=False); for
        # same-size / refit saves we add just the missing glyphs to stay small.
        if slot.family == "RocketSans-Wide" and not self.minimal_glyphs:
            to_add = [c for c in sorted(donor_chars.keys()) if c not in slot.cid_to_char]
        else:
            to_add = list(missing)

        new_glyphs: Dict[int, bytes] = {}
        for char in to_add:
            cid = donor_chars[char]
            slot.cid_to_char[cid] = char
            if char != " " and cid in pool:
                new_glyphs[cid] = pool[cid]
            if cid in donor_widths:
                slot.widths[cid] = donor_widths[cid]

        if new_glyphs:
            fontfile = self.pdf.obj(slot.fontfile_obj)
            merged = rocket_font.inject_cids(fontfile.decoded_stream(), new_glyphs)
            fontfile.set_decoded_stream(merged)
            self._set_length1(fontfile, len(merged))

        cidfont = self.pdf.obj(slot.cidfont_obj)
        cidfont.dict_bytes = rocket_font.replace_widths(cidfont.dict_bytes, slot.widths)

        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(rocket_font.build_tounicode(slot.cid_to_char))

    def _prune_font(self, slot: FontSlot, cids: List[int]) -> None:
        """Stub out glyphs the field no longer uses (exact-line subsets).

        Unused slots in genuine subsets hold the single byte 0x0e
        (endchar); the CID disappears from /W and ToUnicode alike.
        """
        stubbed = {cid: b"\x0e" for cid in cids if cid > 0}
        for cid in cids:
            slot.cid_to_char.pop(cid, None)
            slot.widths.pop(cid, None)

        if stubbed:
            fontfile = self.pdf.obj(slot.fontfile_obj)
            merged = rocket_font.inject_cids(fontfile.decoded_stream(), stubbed)
            fontfile.set_decoded_stream(merged)
            self._set_length1(fontfile, len(merged))

        cidfont = self.pdf.obj(slot.cidfont_obj)
        cidfont.dict_bytes = rocket_font.replace_widths(cidfont.dict_bytes, slot.widths)

        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(rocket_font.build_tounicode(slot.cid_to_char))

    @staticmethod
    def _set_length1(obj: rocket_pdf.PdfObject, value: int) -> None:
        """Keep /Length1 (the inflated font size) consistent after a merge."""
        obj.dict_bytes = re.sub(
            rb"/Length1\s+\d+",
            b"/Length1 " + str(value).encode("ascii"),
            obj.dict_bytes,
            count=1,
        )

    def _replace_run(self, run: TextRun, hex_body: str, text: str) -> None:
        start, end = run.span
        shift = len(hex_body) - (end - start)
        self._content = (
            self._content[:start] + hex_body.encode("ascii") + self._content[end:]
        )
        run.hex_body = hex_body
        run.text = text
        run.span = (start, start + len(hex_body))

        for other in self.runs:
            if other is run or other.span[0] <= start:
                continue
            other.span = (other.span[0] + shift, other.span[1] + shift)

    def _rotate_subset_tags(self) -> None:
        """Give every font slot a fresh 6-letter subset tag.

        The tag appears in the Type0 and CIDFont ``/BaseFont`` and in
        the FontDescriptor ``/FontName``; all three are rewritten
        together.  Tags are arbitrary identifiers in genuine exports,
        so fresh ones cannot be wrong — but the donor's old ones on
        changed subsets are a tell.
        """
        used = set()
        for slot in self.fonts.values():
            type0 = self.pdf.obj(slot.type0_obj)
            match = re.search(rb"/BaseFont/([A-Z]{6})\+", type0.dict_bytes)
            if not match:
                continue
            old = match.group(1)
            while True:
                new = "".join(
                    secrets.choice(string.ascii_uppercase) for _ in range(6)
                ).encode("ascii")
                if new != old and new not in used:
                    break
            used.add(new)
            for obj_num, key in (
                (slot.type0_obj, b"/BaseFont/"),
                (slot.cidfont_obj, b"/BaseFont/"),
                (slot.descriptor_obj, b"/FontName/"),
            ):
                obj = self.pdf.obj(obj_num)
                obj.dict_bytes = obj.dict_bytes.replace(
                    key + old + b"+", key + new + b"+", 1
                )

    # ------------------------------------------------------------------ save

    def save(self, path: str, *, mode: str = "auto", reidentify: bool = True) -> None:
        """Write the receipt with all applied field changes.

        mode="inplace"     – overwrite objects at their original offsets;
                             the output file has the same byte-count as the
                             original.  Falls back to incremental automatically
                             if any object cannot be fitted (e.g. CFF grew).
        mode="incremental" – always append an incremental-update section
                             (iText-compatible, expected by the checker).
        mode="auto"        – try in-place first; fall back to incremental.
        mode="natural"     – checker-facing: surgical in-place when it
                             fits, else a single-pass classic-xref refit
                             at its natural size (no size padding, no
                             incremental tail).  Genuine exports vary in
                             size with their text; a forced byte count
                             needs detectable padding.
        """
        if self._content != self._content_original:
            self.pdf.obj(self._content_obj).set_decoded_stream(self._content)

        # A patched file must not share the original's trailer /ID or Info
        # timestamps — checkers treat those as a fingerprint of a known PDF.
        # Exception: zero-surgery patches keep /ID (genuine iText exports do
        # not rotate it when only the content stream changes).
        if reidentify and (
            self._content != self._content_original or self._touched_fonts
        ):
            self.pdf.reidentify(self._pdf_timestamp())

        # Collect font objects for every touched font resource.
        force: set = set()
        for res in self._touched_fonts:
            slot = self.fonts[res]
            force.add(slot.fontfile_obj)
            force.add(slot.cidfont_obj)
            force.add(slot.tounicode_obj)

        # A freshly exported receipt gets fresh subset tags from its
        # own subsetting run.  Keeping the donor's tags (e.g.
        # HIHAXQ+RocketMono-Regular) on subsets whose glyphs changed is
        # a fingerprint of an edited donor file, so rotate every tag
        # whenever the receipt was actually modified.
        if self.rotate_tags and reidentify and (
            self._content != self._content_original or self._touched_fonts
        ):
            self._rotate_subset_tags()

        want_samesize = mode in ("inplace", "auto")
        written = False

        if mode == "exact":
            # Same byte count as the donor, classic xref only: surgical
            # in-place first, else an exact-size refit (never an xref
            # stream, never an incremental tail).
            if self.pdf.inplace_save(path, content_stream_objs={self._content_obj}):
                written = True
            elif self.pdf.refit_save(path, allow_xref_stream=False, exact_size=True):
                written = True
            else:
                raise ReceiptError(
                    "exact-size classic-xref save could not fit the donor size"
                )
        elif mode == "surgical":
            # Only the in-place writer: every object stays at the donor's
            # offset and byte length (changed streams are re-compressed
            # to their original /Length with inert decompressed padding).
            # If anything cannot fit, fail loudly instead of producing a
            # file with shifted offsets.
            if not self.pdf.inplace_save(path, content_stream_objs={self._content_obj}):
                raise ReceiptError(
                    "surgical in-place save could not fit all changed "
                    "objects at their original sizes"
                )
            written = True
        elif mode == "natural":
            if self.pdf.inplace_save(path, content_stream_objs={self._content_obj}):
                written = True
            else:
                written = self.pdf.refit_save(path, exact_size=False)
        elif want_samesize:
            # Step 1 — surgical in-place: patch stream data at the original file
            # positions.  Fastest and keeps the file structurally identical, but
            # only works when nothing grew (e.g. account-tail change).
            if self.pdf.inplace_save(path, content_stream_objs={self._content_obj}):
                written = True
            # Step 2 — classic refit: rewrite the whole file, re-deflating every
            # stream at level 9 and padding to the original byte count with a
            # classic xref table (which our own parser can round-trip).
            elif self.pdf.refit_save(path, allow_xref_stream=(mode == "inplace")):
                written = True
            elif mode == "inplace":
                import sys
                print(
                    "[warn] change too large to fit the original size "
                    "— falling back to incremental update",
                    file=sys.stderr,
                )

        if not written:
            self.pdf.incremental_save(path, force_include=force)

        if reidentify and (
            self._content != self._content_original or self._touched_fonts
        ):
            self._stamp_filesystem(path)
