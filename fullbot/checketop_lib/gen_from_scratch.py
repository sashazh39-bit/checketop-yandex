#!/usr/bin/env python3
"""
Генерация чеков ВТБ СБП с нуля через openhtmltopdf.

Шрифт SF Pro Display Regular встраивается как CIDFontType2 / Identity-H,
PDF генерируется нативно — без модификации шаблонов.

Использование:
    python3 gen_from_scratch.py \
        --payer "Алексей Михайлович Л." \
        --recipient "Никита Алексеевич К." \
        --bank "Т-Банк" \
        --amount 10000 \
        --date "17.03.2026" --time "01:25" \
        --phone "+7 (958) 748-29-95" \
        --account 9426 \
        -o чек_new.pdf
"""

import argparse
import html as html_mod
import os
import random
import re
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATOR_DIR = SCRIPT_DIR / "vtb-generator"
FONT_PATH = GENERATOR_DIR / "SFProDisplay-Regular.ttf"
TEMPLATE_PATH = GENERATOR_DIR / "template.html"
LOGO_SVG = "vtb_logo.svg"
JAVA_HOME = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"


def _format_amount(amount: int) -> str:
    """10000 -> '10 000 ₽'  or  500 -> '500 ₽'"""
    s = f"{amount:,}".replace(",", " ")
    return f"{s} ₽"


def _gen_operation_id(op_date: date, op_time: str, bank: str) -> str:
    """Генерирует ID операции в СБП."""
    try:
        from vtb_cmap import gen_sbp_operation_id
        return gen_sbp_operation_id(
            op_date=op_date,
            op_time_moscow=op_time,
            direction="B",
            recipient_bank=bank,
        )
    except Exception:
        h, m = (op_time.split(":") + ["00", "00"])[:2]
        day = op_date.strftime("%d%m%y")
        return f"B{h}{m}6001{random.randint(100000000, 999999999)}0B{day}"


def _gen_phone() -> str:
    """Генерирует случайный телефон."""
    d = lambda n: "".join(str(random.randint(0, 9)) for _ in range(n))
    return f"+7 ({d(3)}) {d(3)}\u2011{d(2)}\u2011{d(2)}"


def build_html(
    payer: str,
    recipient: str,
    bank: str,
    amount: int,
    op_date: str,
    op_time: str,
    phone: str,
    account: str,
    operation_id: str | None = None,
) -> str:
    """Подставляет значения в HTML-шаблон."""
    tpl = TEMPLATE_PATH.read_text("utf-8")

    dt = datetime.strptime(f"{op_date} {op_time}", "%d.%m.%Y %H:%M")
    opid = operation_id or _gen_operation_id(dt.date(), op_time, bank)

    e = html_mod.escape

    replacements = {
        "{{PAYER}}": e(payer),
        "{{RECIPIENT}}": e(recipient),
        "{{BANK}}": e(bank),
        "{{AMOUNT}}": e(_format_amount(amount)),
        "{{DATE}}": e(op_date),
        "{{TIME}}": e(op_time),
        "{{PHONE}}": e(phone),
        "{{ACCOUNT}}": e(account),
        "{{OPID}}": e(opid),
    }
    for k, v in replacements.items():
        tpl = tpl.replace(k, v)
    return tpl


def _patch_stamp_rotation(pdf_path: str) -> None:
    """Добавляет наклон ~2° к штампу (как в оригинале)."""
    data = open(pdf_path, "rb").read()

    # Find content stream
    cs_match = re.search(
        rb'(\d+) 0 obj\s*<<(.*?/Length\s+)(\d+)(.*?)>>\s*stream\r?\n',
        data, re.DOTALL,
    )
    if not cs_match:
        return

    obj_start = cs_match.start()
    length = int(cs_match.group(3))
    stream_start = cs_match.end()
    compressed = data[stream_start:stream_start + length]

    try:
        dec = zlib.decompress(compressed)
    except Exception:
        return

    text = dec.decode("latin-1")

    # Find stamp border (blue color before stamp text)
    stamp_marker = re.search(r"0\.49804 0\.67059 0\.9\d+ rg\n", text)
    if not stamp_marker:
        return

    insert_pos = stamp_marker.start()
    rotation = "q\n0.99939 0.0349 -0.0349 0.99939 1.43366 -4.88421 cm\n"

    # Insert rotation before stamp, and Q before the final Q
    end_pos = text.rfind("\nQ\n")
    if end_pos < 0:
        end_pos = text.rfind("\nQ")

    new_text = text[:insert_pos] + rotation + text[insert_pos:end_pos] + "\nQ\n" + text[end_pos:].lstrip("\nQ")
    # Ensure ends with Q
    if not new_text.rstrip().endswith("Q"):
        new_text = new_text.rstrip() + "\nQ\n"

    new_dec = new_text.encode("latin-1")
    new_compressed = zlib.compress(new_dec, 9)

    # Replace stream
    end_stream = stream_start + length
    endstream_match = re.search(rb'\r?\nendstream', data[end_stream - 2:end_stream + 20])

    before = data[:cs_match.start()]
    obj_header = data[cs_match.start():stream_start]
    after = data[stream_start + length:]

    # Update /Length
    new_header = re.sub(
        rb'/Length\s+\d+',
        f"/Length {len(new_compressed)}".encode(),
        obj_header,
    )

    new_data = before + new_header + new_compressed + after

    # Fix xref
    open(pdf_path, "wb").write(new_data)


def _fix_xref(data: bytes) -> bytes:
    """Пересчитывает xref таблицу и startxref."""
    offsets = {}
    for m in re.finditer(rb'(\d+) 0 obj', data):
        offsets[int(m.group(1))] = m.start()

    # Find real xref (not inside "startxref")
    xref_start = -1
    pos = 0
    while True:
        idx = data.find(b"xref", pos)
        if idx < 0:
            break
        if idx == 0 or data[idx - 1:idx] in (b"\n", b"\r"):
            after = data[idx + 4:idx + 5]
            if after in (b"\n", b"\r"):
                xref_start = idx
                break
        pos = idx + 1

    if xref_start < 0:
        return data

    trailer_start = data.find(b"trailer", xref_start)
    if trailer_start < 0:
        return data

    trailer_end = data.find(b"%%EOF", trailer_start)
    if trailer_end < 0:
        trailer_end = len(data)
    else:
        trailer_end += 5

    trailer_section = data[trailer_start:trailer_end]
    trailer_dict = re.search(rb'trailer\s*(<<.*?>>)', trailer_section, re.DOTALL)
    if not trailer_dict:
        return data

    max_obj = max(offsets.keys()) if offsets else 0
    xref_lines = [f"xref\n0 {max_obj + 1}\n".encode()]
    xref_lines.append(b"0000000000 65535 f \n")
    for i in range(1, max_obj + 1):
        off = offsets.get(i, 0)
        xref_lines.append(f"{off:010d} 00000 n \n".encode())

    before = data[:xref_start]
    new_xref_offset = len(before)
    new_xref_bytes = b"".join(xref_lines)

    tail = (
        new_xref_bytes
        + b"trailer\n"
        + trailer_dict.group(1)
        + b"\nstartxref\n"
        + str(new_xref_offset).encode()
        + b"\n%%EOF\n"
    )

    return before + tail


def _patch_creation_date(pdf_path: str, op_date: str, op_time: str) -> None:
    """Устанавливает CreationDate в соответствии с датой/временем операции (+0..15 мин)."""
    dt = datetime.strptime(f"{op_date} {op_time}", "%d.%m.%Y %H:%M")
    offset_min = random.randint(0, 15)
    dt = dt.replace(second=random.randint(0, 59))
    from datetime import timedelta
    dt += timedelta(minutes=offset_min)
    date_str = dt.strftime("D:%Y%m%d%H%M%S") + "+03'00'"

    data = open(pdf_path, "rb").read()
    new_data = re.sub(
        rb"/CreationDate \([^)]+\)",
        f"/CreationDate ({date_str})".encode(),
        data,
    )
    if new_data != data:
        open(pdf_path, "wb").write(new_data)


def generate_pdf(html_content: str, output_path: str) -> bool:
    """Вызывает Java-генератор openhtmltopdf."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", dir=str(GENERATOR_DIR), delete=False, encoding="utf-8"
    ) as f:
        f.write(html_content)
        tmp_html = f.name

    try:
        env = os.environ.copy()
        env["JAVA_HOME"] = JAVA_HOME
        env["PATH"] = f"{JAVA_HOME}/bin:{env.get('PATH', '')}"

        classpath = f"build:{GENERATOR_DIR / 'lib'}/*"

        result = subprocess.run(
            [
                f"{JAVA_HOME}/bin/java",
                "-cp", classpath,
                "VtbGenerator",
                tmp_html,
                output_path,
                str(FONT_PATH),
            ],
            capture_output=True,
            text=True,
            cwd=str(GENERATOR_DIR),
            env=env,
            timeout=30,
        )

        if result.returncode != 0:
            print(f"ERROR: {result.stderr}", file=sys.stderr)
            return False

        out_lines = result.stdout.strip().split("\n")
        for line in out_lines:
            if line.startswith("OK:"):
                return True

        print(f"Unexpected output: {result.stdout}", file=sys.stderr)
        return False

    finally:
        os.unlink(tmp_html)


def main():
    parser = argparse.ArgumentParser(description="Генерация чеков ВТБ СБП с нуля")
    parser.add_argument("--payer", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--bank", default="Т-Банк")
    parser.add_argument("--amount", type=int, required=True)
    parser.add_argument("--date", required=True, help="ДД.ММ.ГГГГ")
    parser.add_argument("--time", required=True, help="ЧЧ:ММ")
    parser.add_argument("--phone", default=None)
    parser.add_argument("--account", default="9426")
    parser.add_argument("--operation-id", default=None)
    parser.add_argument("-o", "--output", default="check_output.pdf")
    parser.add_argument("--donor", default=None, help="Путь к PDF-донору для пересадки шрифтов")

    args = parser.parse_args()

    phone = args.phone or _gen_phone()
    bank = args.bank.replace('-', '\u2011')

    print(f"Генерация чека...")
    print(f"  Плательщик: {args.payer}")
    print(f"  Получатель: {args.recipient}")
    print(f"  Сумма: {_format_amount(args.amount)}")
    print(f"  Дата: {args.date}, {args.time}")
    print(f"  Телефон: {phone}")
    print(f"  Банк: {args.bank}")
    print(f"  Счёт: *{args.account}")

    html = build_html(
        payer=args.payer,
        recipient=args.recipient,
        bank=bank,
        amount=args.amount,
        op_date=args.date,
        op_time=args.time,
        phone=phone,
        account=args.account,
        operation_id=args.operation_id,
    )

    output = str(Path(args.output).resolve())

    if generate_pdf(html, output):
        _patch_stamp_rotation(output)
        _patch_creation_date(output, args.date, args.time)
        # Fix xref after all patches
        final_data = _fix_xref(open(output, "rb").read())
        open(output, "wb").write(final_data)

        # Font transplant from donor
        if args.donor:
            from font_transplant import transplant
            donor = str(Path(args.donor).resolve())
            info = transplant(output, donor, output)
            print(f"  Font transplant: donor={Path(donor).name}")
            if info['missing_chars']:
                print(f"  ⚠ Missing chars: {info['missing_chars']}")

        size = os.path.getsize(output)
        print(f"\n✓ Чек создан: {output} ({size} байт, {size/1024:.1f} КБ)")
    else:
        print("\n✗ Ошибка генерации", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
