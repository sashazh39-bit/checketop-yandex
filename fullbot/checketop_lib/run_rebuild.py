#!/usr/bin/env python3
"""Run rebuild_pdf with random account and fixed replacements."""
import random
import string
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REBUILD_SCRIPT = SCRIPT_DIR / "rebuild_pdf.py"
INPUT_PDF = SCRIPT_DIR / "input ru.pdf"
OUTPUT_PDF = SCRIPT_DIR / "Квитанция (21).pdf"
DONOR_PDF = SCRIPT_DIR / "donor.pdf"

random_suffix = "".join(random.choice(string.digits) for _ in range(16))
new_account = "4081" + random_suffix

cmd = [
    sys.executable,
    str(REBUILD_SCRIPT),
    str(INPUT_PDF),
    str(OUTPUT_PDF),
    "--donor-pdf", str(DONOR_PDF),
    "--strict-forensic",
    "--font-alias-from-input",
    "--replace", f"40817810980480002476={new_account}",
    "--replace", "C422402260356995=C422402260934672",
    "--replace", "10=5 000",
    "--replace", "79003517080=79097790757",
    "--replace", "ВТБ=Озон Банк (Ozon)",
    "--replace", "Александр Евгеньевич Ж=Владимир Данилович В.",
    "--replace", "24.02.2026 10:38:28=02.03.2026 11:14:32",
    "--replace", "24.02.2026 13:39=02.03.2026 11:19",
    "--replace", "A60550738283070H0000020011700501=A60550738283070H0000020011900653",
]

print("Generated account:", new_account)
print("Input:", INPUT_PDF)
print("Output:", OUTPUT_PDF)
print("Running:", " ".join(cmd))

result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
sys.exit(result.returncode)
