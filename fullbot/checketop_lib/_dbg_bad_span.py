import sys, fitz
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

gen = Path(r'c:/Users/drozd/Downloads/Tbank_test_2050_Сбер.pdf')
doc = fitz.open(gen)
page = doc[0]
print("=== All spans with font/text ===")
for block in page.get_text('rawdict')['blocks']:
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            fname = span.get('font','')
            txt = ''
            bad_chars = []
            for ch in span.get('chars', []):
                c = ch.get('c','')
                if len(c)==1:
                    cp = ord(c)
                    txt += c
                    if 0x0100 <= cp <= 0x03FF:
                        bad_chars.append(hex(cp))
            if bad_chars:
                print(f'  BAD FONT={fname[-20:]} TEXT=[{txt}] bad={bad_chars}')
doc.close()
