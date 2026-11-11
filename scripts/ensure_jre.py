#!/usr/bin/env python3
"""Скачать Temurin JRE 17 на Linux, если java ещё нет.

Россельхозбанк и правка чека Озона жмут поток только через Java Deflater
(JZComp). На Render это обычный Python-сервис, системной Java там нет.
Ошибка скачивания не валит сборку: чеки без Java просто скажут об этом в боте.
"""
from __future__ import annotations

import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "jre"
URL = (
    "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/"
    "hotspot/normal/eclipse?project=jdk"
)


def main() -> int:
    if shutil.which("java") or (DEST / "bin" / "java").exists():
        print("java already available")
        return 0
    if sys.platform != "linux":
        print("skip jre download: not linux and no system java")
        return 0
    try:
        DEST.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "jre.tar.gz"
            print("downloading temurin jre 17")
            urllib.request.urlretrieve(URL, archive)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp)
            java = next(Path(tmp).rglob("bin/java"))
            home = java.parent.parent
            if DEST.exists():
                shutil.rmtree(DEST)
            shutil.move(str(home), str(DEST))
        print("jre ready", DEST / "bin" / "java")
    except Exception as error:
        print("jre download skipped:", error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
