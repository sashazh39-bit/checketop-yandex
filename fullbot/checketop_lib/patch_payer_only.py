#!/usr/bin/env python3
"""
Замена только имени плательщика в чеке.
Берёт имя из source.pdf, подставляет в target.pdf. Остальное не трогает.

Использование: python3 patch_payer_only.py source.pdf target.pdf [output.pdf]
По умолчанию: чеки 07.03/13-02-26_20-29.pdf -> donors/07-03-26_04-34 (1).pdf -> чеки 08.03/07-03-26_04-34.pdf
"""
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

try:
    import pikepdf
except ImportError:
    pikepdf = None

PAYER_Y_SOURCE = 227.25  # 13-02-26 (Арман Мелсикович Б.)
PAYER_Y_TARGET = 348.75  # 07-03-26_04-34 (Имя плательщика)
Y_TOL = 1.5

# Когда в правой колонке ошибочно стоит "Имя плательщика" вместо имени — заменяем на Арман Мелсикович Б.
OLD_IMYA_PLATELSHIKA = (
    b'[(\x02\x1d)-8.33333 (\x02<)-8.33333 (\x02I)-8.33333 (\x02F)-8.33333 (\x00\x03)-8.33333 (\x02\x1e)-8.33333 (\x02.)-8.33333 (\x02\x1d)-8.33333 (\x00\x03)-8.33333 (\x00\x0b)-8.33333 (\x02+)-8.33333 (\x02\x1c)-8.33333 (\x02*)-8.33333 (\x00\x0c)] TJ'
)
# Арман Мелсикович Б. (kern 8.33333): А=021c, р=024c, м=0248, а=023c, н=0249, space, М=0228, е=0241, л=0247, с=024d, и=0244, к=0246, о=024a, в=023e, и=0244, ч=0253, space, Б=021d, .=0011
NEW_ARMAN_MELSIKOVICH = (
    b'[(\x02\x1c)-8.33333 (\x02\x4c)-8.33333 (\x02\x48)-8.33333 (\x02\x3c)-8.33333 (\x02\x49)-8.33333 (\x00\x03)-8.33333 (\x02\x28)-8.33333 (\x02\x41)-8.33333 (\x02\x47)-8.33333 (\x02\x4d)-8.33333 (\x02\x44)-8.33333 (\x02\x46)-8.33333 (\x02\x4a)-8.33333 (\x02\x3e)-8.33333 (\x02\x44)-8.33333 (\x02\x53)-8.33333 (\x00\x03)-8.33333 (\x02\x1d)-8.33333 (\x00\x11)] TJ'
)

# Когда в правой колонке ошибочно "Имя плательщика" вместо имени — заменяем на Арман Мелсикович Б.
# (kern 8.33333, формат «Перевод по номеру телефона в другую страну»)
OLD_IMYA_IN_VALUE = (
    b'[(\x02\x1d)-8.33333 (\x02<)-8.33333 (\x02I)-8.33333 (\x02F)-8.33333 (\x00\x03)-8.33333 '
    b'(\x02\x1e)-8.33333 (\x02.)-8.33333 (\x02\x1d)-8.33333 (\x00\x03)-8.33333 (\x00\x0b)-8.33333 '
    b'(\x02+)-8.33333 (\x02\x1c)-8.33333 (\x02*)-8.33333 (\x00\x0c)] TJ'
)
# Арман Мелсикович Б.: А=021c, р=024c, м=0248, а=023c, н=0249, space, М=0228, е=0241, л=0247, с=024d, и=0244, к=0246, о=024a, в=023e, и=0244, ч=0253, space, Б=021d, .=0011
NEW_ARMAN_KERN8 = (
    b'[(\x02\x1c)-8.33333 (\x02\x4c)-8.33333 (\x02\x48)-8.33333 (\x02\x3c)-8.33333 (\x02\x49)-8.33333 (\x00\x03)-8.33333 '
    b'(\x02\x28)-8.33333 (\x02\x41)-8.33333 (\x02\x47)-8.33333 (\x02\x4d)-8.33333 (\x02\x44)-8.33333 (\x02\x46)-8.33333 '
    b'(\x02\x4a)-8.33333 (\x02\x3e)-8.33333 (\x02\x44)-8.33333 (\x02\x53)-8.33333 (\x00\x03)-8.33333 (\x02\x1d)-8.33333 (\x00\x11)] TJ'
)

# "Имя плательщика" в правой колонке (ошибочно подставлен label) -> "Арман Мелсикович Б." (kern 8.33333)
OLD_LABEL_AS_VALUE = (
    b'[(\x02\x1d)-8.33333 (\x02<)-8.33333 (\x02I)-8.33333 (\x02F)-8.33333 (\x00\x03)-8.33333 (\x02\x1e)-8.33333 (\x02.)-8.33333 (\x02\x1d)-8.33333 (\x00\x03)-8.33333 (\x00\x0b)-8.33333 (\x02+)-8.33333 (\x02\x1c)-8.33333 (\x02*)-8.33333 (\x00\x0c)] TJ'
)
# "Имя плательщика" с kern -16.66667 (формат 07-03-26_18-10, 16-46 и др.) — полный TJ-блок
OLD_LABEL_IMYA_16 = (
    b'[(\x02\x1d)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02F)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02K)-16.66667 (\x02J)-16.66667 (\x02G)-16.66667 (\x02O)-16.66667 (\x02S)-16.66667 (\x02<)-16.66667 (\x02N)-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02[)] TJ'
)
# Арман Мелсикович Б.: А=021c, р=024c, м=0248, а=023c, н=0249, space, М=0228, е=0241, л=0247, с=024d, и=0244, к=0246, о=024a, в=023e, и=0244, ч=0253, space, Б=021d, .=0011
NEW_PAYER_KERN8 = (
    b'[(\x02\x1c)-8.33333 (\x02\x4c)-8.33333 (\x02\x48)-8.33333 (\x02\x3c)-8.33333 (\x02\x49)-8.33333 (\x00\x03)-8.33333 (\x02\x28)-8.33333 (\x02\x41)-8.33333 (\x02\x47)-8.33333 (\x02\x4d)-8.33333 (\x02\x44)-8.33333 (\x02\x46)-8.33333 (\x02\x4a)-8.33333 (\x02\x3e)-8.33333 (\x02\x44)-8.33333 (\x02\x53)-8.33333 (\x00\x03)-8.33333 (\x02\x1d)-8.33333 (\x00\x11)] TJ'
)
# Арман Мелсикович Б. с kerning -16.66667 (для target 07-03-26_04-34)
NEW_PAYER_KERN16 = (
    b'[(\x02\x1c)-16.66667 (\x02\x4c)-16.66667 (\x02\x48)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 (\x02\x28)-16.66667 (\x02\x41)-16.66667 (\x02\x47)-16.66667 (\x02\x4d)-16.66667 (\x02\x44)-16.66667 (\x02\x46)-16.66667 (\x02\x4a)-16.66667 (\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)] TJ'
)


def extract_payer_tj(pdf_path: Path, payer_y: float = PAYER_Y_SOURCE) -> bytes | None:
    """Извлечь TJ плательщика из PDF. Возвращает tj_content или None."""
    data = pdf_path.read_bytes()
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue
        pat = rb'(1\s+0\s+0\s+1\s+[\d.]+\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        for mm in re.finditer(pat, dec):
            y = float(mm.group(2))
            if abs(y - payer_y) <= Y_TOL:
                return mm.group(5)
        pat2 = rb'(1\s+0\s+0\s+1\s+[\d.]+\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
        for mm in re.finditer(pat2, dec):
            y = float(mm.group(2))
            if abs(y - payer_y) <= Y_TOL:
                return mm.group(4)
    return None


def adapt_tj_kerning(tj: bytes, target_tj: bytes) -> bytes:
    """Подогнать kerning в tj под формат target (напр. -8.33333 -> -16.66667)."""
    m = re.search(rb"\)-([\d.]+)\s", target_tj)
    if not m:
        return tj
    target_kern = m.group(1)
    # source обычно -8.33333
    for old in (b"8.33333", b"16.66667"):
        if old in tj and old != target_kern:
            return tj.replace(b"-" + old, b"-" + target_kern)
    return tj


def main():
    base = Path(__file__).parent
    out_dir = base / "чеки 08.03"
    default_source = base / "чеки 07.03" / "13-02-26_20-29.pdf"
    source_copy = base / "чеки 07.03" / "13-02-26_20-29 — копия.pdf"
    default_target = base / "donors" / "07-03-26_04-34 (1).pdf"

    payer_y_target = PAYER_Y_TARGET
    if len(sys.argv) >= 5:
        source = Path(sys.argv[1])
        target = Path(sys.argv[2])
        out = out_dir / Path(sys.argv[3]).name
        payer_y_target = float(sys.argv[4])
    elif len(sys.argv) >= 4:
        source = Path(sys.argv[1])
        target = Path(sys.argv[2])
        out = out_dir / Path(sys.argv[3]).name
        if "18-10" in target.name or "16-46" in target.name or "16-49" in target.name:
            payer_y_target = 227.25
    elif len(sys.argv) >= 2:
        target = Path(sys.argv[1])
        # Для 18-10, 16-46, 16-49 используем копию как source (Арман Мелсикович Б.)
        if ("18-10" in target.name or "16-46" in target.name or "16-49" in target.name) and source_copy.exists():
            source = source_copy
        else:
            source = default_source
        out = out_dir / (target.stem.replace(" (1)", "") + target.suffix)
        if "18-10" in target.name or "16-46" in target.name or "16-49" in target.name:
            payer_y_target = 227.25
    else:
        source = default_source
        target = default_target
        out = out_dir / "07-03-26_04-34.pdf"

    if not source.exists() and source_copy.exists() and source == default_source:
        source = source_copy
        print(f"[INFO] Используем source: {source.name}")
    if not source.exists():
        print(f"[ERROR] Source не найден: {source}")
        sys.exit(1)
    if not target.exists():
        print(f"[ERROR] Target не найден: {target}")
        sys.exit(1)

    # 07-03-26_18-10, 16-46, 16-49: плательщик на y=227.25; 07-03-26_04-34: y=348.75
    payer_y_target = PAYER_Y_TARGET
    if "18-10" in target.name or "16-46" in target.name or "16-49" in target.name:
        payer_y_target = 227.25

    source_tj = extract_payer_tj(source, PAYER_Y_SOURCE)
    if not source_tj:
        print("[ERROR] Не удалось извлечь имя плательщика из source.")
        sys.exit(1)
    print(f"[OK] Извлечено имя плательщика из {source.name}")

    if pikepdf is None:
        print("[ERROR] Установите pikepdf: pip install pikepdf")
        sys.exit(1)

    pdf = pikepdf.open(target)
    modified = False

    def iter_streams(obj, seen=None):
        """Рекурсивно обойти все stream-объекты."""
        if seen is None:
            seen = set()
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, pikepdf.Stream):
            yield obj
        elif isinstance(obj, pikepdf.Array):
            for item in obj:
                yield from iter_streams(item, seen)
        elif isinstance(obj, pikepdf.Dictionary):
            for v in obj.values():
                yield from iter_streams(v, seen)

    streams_to_check = []
    for page in pdf.pages:
        if "/Contents" in page:
            streams_to_check.extend(iter_streams(page.Contents))
    if not streams_to_check:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                streams_to_check.append(obj)

    for obj in streams_to_check:
        if not hasattr(obj, "read_raw_bytes") or not hasattr(obj, "write"):
            continue
        try:
            raw = obj.read_raw_bytes()
        except Exception:
            continue
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        new_dec = dec
        # Target может использовать -16.66667, source -8.33333; адаптируем source_tj под kerning target
        def adapt_kerning(tj_bytes: bytes, target_tj: bytes) -> bytes:
            m = re.search(rb"\)-([\d.]+)\s", target_tj)
            if m:
                target_kern = m.group(1)
                return tj_bytes.replace(b"-8.33333", b"-" + target_kern)
            return tj_bytes

        # 1. Сначала заменяем «Имя плательщика» в правой колонке (kern -8.33333) — приоритет
        if OLD_LABEL_AS_VALUE in new_dec:
            new_dec = new_dec.replace(OLD_LABEL_AS_VALUE, NEW_PAYER_KERN8)

        # 2. Позиционная замена по y (если блок на payer_y_target в правой колонке)
        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        def repl(mm):
            x, y = float(mm.group(2)), float(mm.group(4))
            if x > 100 and abs(y - payer_y_target) <= Y_TOL:
                orig_tj = mm.group(6)
                adapted = adapt_kerning(source_tj, orig_tj)
                return mm.group(1) + mm.group(2) + mm.group(3) + mm.group(4) + mm.group(5) + adapted
            return mm.group(0)
        before_sub = new_dec
        new_dec = re.sub(pat, repl, new_dec)
        if new_dec == before_sub:
            pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
            def repl2(mm):
                x, y = float(mm.group(2)), float(mm.group(4))
                if x > 100 and abs(y - payer_y_target) <= Y_TOL:
                    orig_tj = mm.group(5)
                    adapted = adapt_kerning(source_tj, orig_tj)
                    return mm.group(1) + mm.group(2) + mm.group(3) + mm.group(4) + mm.group(5) + adapted
                return mm.group(0)
            new_dec = re.sub(pat2, repl2, new_dec)

        # 3. Повторная замена «Имя плательщика» (на случай если позиционная не сработала)
        if OLD_LABEL_AS_VALUE in new_dec:
            new_dec = new_dec.replace(OLD_LABEL_AS_VALUE, NEW_PAYER_KERN8)

        # «Имя плательщика» с kern -16.66667 в правой колонке (x>100) -> Арман из source
        OLD_LABEL_IMYA_16_FULL = b'[' + OLD_LABEL_IMYA_16 + b'] TJ'
        if OLD_LABEL_IMYA_16_FULL in new_dec:
            pat_imya = (
                rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n[^\[]*?)\['
                + re.escape(OLD_LABEL_IMYA_16) + rb'\]\s*TJ'
            )
            def repl_imya(m):
                if float(m.group(2)) > 100:
                    adapted = adapt_kerning(source_tj, OLD_LABEL_IMYA_16_FULL)
                    adapted = adapted.replace(b"(\x02\x28)", b"(\x02M)").replace(b"(\x02\\(", b"(\x02M)")
                    return m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5) + adapted
                return m.group(0)
            new_dec = re.sub(pat_imya, repl_imya, new_dec)

        if new_dec != dec:
            # Передаём несжатые данные — pikepdf применит /FlateDecode при сохранении
            obj.write(new_dec)
            modified = True

    if not modified:
        pdf.close()
        print("[ERROR] Не удалось найти поле плательщика в target.")
        sys.exit(1)

    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(out, linearize=False, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    pdf.close()
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
