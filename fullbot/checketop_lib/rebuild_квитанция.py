#!/usr/bin/env python3
"""Скрипт пересборки квитанции с заменой реквизитов.
Использует все новые опции rebuild_pdf.py (font-alias, metadata по умолчанию и т.д.).
"""
import random
import string
import subprocess

random_suffix = "".join(random.choice(string.digits) for _ in range(16))
new_account = "4081" + random_suffix

cmd = [
    "python3",
    "rebuild_pdf.py",
    "input ru.pdf",
    "Квитанция (20).pdf",
    "--donor-pdf",
    "donor.pdf",
    "--strict-forensic",
    "--font-alias-from-input",
    "--replace",
    f"40817810980480002476={new_account}",
    "--replace",
    "C422402260356995=C422402260934672",
    "--replace",
    "10=5 000",
    "--replace",
    "79003517080=79097790757",
    "--replace",
    "ВТБ=Озон Банк (Ozon)",
    "--replace",
    "Александр Евгеньевич Ж=Владимир Данилович В.",
    "--replace",
    "24.02.2026 10:38:28=02.03.2026 11:14:32",
    "--replace",
    "24.02.2026 13:39=02.03.2026 11:19",
    "--replace",
    "A60550738283070H0000020011700501=A60550738283070H0000020011900653",
]

print("Generated account:", new_account)
print("Running:", " ".join(cmd))
subprocess.run(cmd, check=True)
