import sys, fitz, zlib, re
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from gen_tbank_receipt import generate_tbank_receipt
from tbank_enrich_donor import _find_f2_tounicode_obj

# Generate 4000/Газпромбанк
out = Path(r'c:/Users/drozd/Downloads/_test_4000_gazp.pdf')
generate_tbank_receipt(
    amount=4000,
    bank='Газпромбанк',
    receiver='Тест Тестов',
    sender='Дмитрий Дроздов',
    phone='+7 (999) 123-45-67',
    date='24.05.2026',
    time_str='12:00:00',
    output_path=out,
    preserve_integrity=True,
)

print(f"Generated: {out}")
data = out.read_bytes()

# Check F2 ToUnicode in generated file
obj_num = _find_f2_tounicode_obj(data)
print(f'F2 ToUnicode obj: {obj_num}')
pat = re.compile(rb'(\d+)\s+0\s+obj\s*\n?<<')
for sm in re.finditer(pat, data):
    if int(sm.group(1)) != obj_num: continue
    chunk = data[sm.end():sm.end()+600]
    len_m = re.search(rb'/Length\s+(\d+)', chunk)
    end_m = re.search(rb'>>\s*stream\r?\n', chunk)
    if not len_m or not end_m: break
    old_len = int(len_m.group(1))
    stream_start = sm.end() + end_m.end()
    raw = data[stream_start:stream_start+old_len]
    compressed = b'/Filter' in chunk[:500]
    try:
        stream = zlib.decompress(raw) if compressed else raw
    except:
        stream = raw
    print(f'F2 TOU compressed={compressed}, len={len(stream)}')
    print('has 0131-013a range:', b'<0131><013a>' in stream)
    print('stream tail:', stream[-200:].decode('latin-1', errors='replace'))
    break

# Check fitz decoding
doc = fitz.open(out)
page = doc[0]
bad = []
for block in page.get_text('rawdict')['blocks']:
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            for ch in span.get('chars', []):
                c = ch.get('c','')
                if len(c)==1 and 0x0100 <= ord(c) <= 0x03FF:
                    bad.append((c, hex(ord(c)), span.get('font','')[-5:]))
doc.close()
if bad:
    print(f'BAD chars: {bad}')
else:
    print('fitz: OK')
