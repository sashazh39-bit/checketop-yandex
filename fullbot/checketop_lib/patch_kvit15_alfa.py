#!/usr/bin/env python3
"""Патч квитанции Альфа-Банка «Квитанция 15.pdf»:
  10 RUR  → 10 001 RUR
  49,20 RUR → 244,02 RUR
Сохраняет метаданные, /ID, структуру.
"""
import re
import sys
from pathlib import Path

import fitz


NBSP = "\xa0"
REPLACEMENTS = [
    (f"10{NBSP}RUR{NBSP}", f"10\xa0001{NBSP}RUR{NBSP}"),
    (f"49,20{NBSP}RUR{NBSP}", f"244,02{NBSP}RUR{NBSP}"),
]


def _extract_pdf_id(pdf_bytes: bytes) -> bytes | None:
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>", pdf_bytes)
    return m.group(1) if m else None


def _restore_pdf_id(path: Path, original_id: bytes) -> None:
    data = path.read_bytes()
    pat = re.compile(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]")
    def repl(m):
        return m.group(0).replace(m.group(1), original_id).replace(m.group(2), original_id)
    new_data = pat.sub(repl, data, count=1)
    if new_data != data:
        path.write_bytes(new_data)


def patch(inp: Path, out: Path) -> bool:
    raw = inp.read_bytes()
    orig_id = _extract_pdf_id(raw)

    doc = fitz.open(inp)
    page = doc[0]
    orig_meta = dict(doc.metadata or {})

    dt = page.get_text("dict")
    modified = 0

    for old_text, new_text in REPLACEMENTS:
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    st = span.get("text", "")
                    if st != old_text:
                        continue

                    bbox = span.get("bbox")
                    if not bbox or len(bbox) != 4:
                        continue
                    x0, y0, x1, y1 = bbox

                    fontsize = float(span.get("size", 12.0))
                    c = int(span.get("color", 0))
                    color = (
                        ((c >> 16) & 255) / 255.0,
                        ((c >> 8) & 255) / 255.0,
                        (c & 255) / 255.0,
                    )

                    rect = fitz.Rect(x0, y0, x1, y1)
                    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))

                    baseline_y = y1 - fontsize * 0.2
                    page.insert_text(
                        fitz.Point(x0, baseline_y),
                        new_text,
                        fontsize=fontsize,
                        fontname="helv",
                        color=color,
                    )
                    modified += 1
                    print(f"  [{old_text}] → [{new_text}]  bbox={[round(v,1) for v in bbox]}")

    doc.set_metadata(orig_meta)
    doc.save(out, garbage=0, deflate=True)
    doc.close()

    if orig_id and out.exists():
        _restore_pdf_id(out, orig_id)
        print(f"  /ID восстановлен: {orig_id.decode()}")

    return modified > 0


def main():
    inp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/aleksandrzerebatav/Downloads/Квитанция 15.pdf")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "Квитанция 15_patched.pdf"

    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}", file=sys.stderr)
        return 1

    if patch(inp, out):
        print(f"[OK] Сохранено: {out}")
        # Verify
        vdoc = fitz.open(out)
        vpage = vdoc[0]
        text = vpage.get_text()
        vdoc.close()
        print(f"\n--- Текст результата ---\n{text}")
        return 0

    print("[WARN] Замены не применены", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
