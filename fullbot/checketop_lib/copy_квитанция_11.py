#!/usr/bin/env python3
"""Полная копия Квитанция 11.pdf с заменой 200 на 100.
Использует точечную замену (content stream / redaction) — метаданные, шрифт, структура сохраняются.
"""
import subprocess
from pathlib import Path

INPUT = "Квитанция 11.pdf"
OUTPUT = "Квитанция 11_copy.pdf"
REPLACE = "200=100"

d = Path(__file__).parent.resolve()

cmd = [
    "python3",
    str(d / "content_stream_replace.py"),
    INPUT,
    OUTPUT,
    "--replace",
    REPLACE,
]

print("Input:", INPUT)
print("Output:", OUTPUT)
print("Replace:", REPLACE, "(точечная замена в content stream)")
print("Running:", " ".join(cmd))
subprocess.run(cmd, cwd=str(d), check=True)
