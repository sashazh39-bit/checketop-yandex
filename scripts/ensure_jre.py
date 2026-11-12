#!/usr/bin/env python3
"""Найти или скачать Temurin JRE 17 для Java Deflater (JZComp).

На Render нет системной Java, а startCommand часто указывает на
ещё не существующий jre/bin/java. Этот скрипт:
  • не считает JAVA_BIN валидным, если файла нет;
  • качает JRE в <repo>/jre при старте/сборке на Linux;
  • возвращает абсолютный путь к java.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "jre"
ADOPTIUM = (
    "https://api.adoptium.net/v3/binary/latest/17/ga/linux/{arch}/jre/"
    "hotspot/normal/eclipse?project=jdk"
)
PINNED = {
    "x64": (
        "https://github.com/adoptium/temurin17-binaries/releases/download/"
        "jdk-17.0.21%2B9/OpenJDK17U-jre_x64_linux_hotspot_17.0.21_9.tar.gz"
    ),
    "aarch64": (
        "https://github.com/adoptium/temurin17-binaries/releases/download/"
        "jdk-17.0.21%2B9/OpenJDK17U-jre_aarch64_linux_hotspot_17.0.21_9.tar.gz"
    ),
}


def _arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return "x64"


def is_java(path: str | None) -> bool:
    if not path:
        return False
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_java(root: Path | None = None) -> str | None:
    root = root or ROOT
    cands = [
        os.environ.get("JAVA_BIN"),
        str(root / "jre" / "bin" / "java"),
        str(Path(__file__).resolve().parent.parent / "fullbot" / "jre" / "bin" / "java"),
        shutil.which("java"),
        "/usr/bin/java",
        "/opt/homebrew/opt/openjdk@17/bin/java",
        "/opt/homebrew/opt/openjdk/bin/java",
    ]
    seen: set[str] = set()
    for raw in cands:
        if not raw:
            continue
        path = os.path.realpath(raw)
        if path in seen:
            continue
        seen.add(path)
        if is_java(path):
            return path
    return None


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "checketop-jre"})
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def download_jre(root: Path | None = None) -> str:
    root = root or ROOT
    dest = root / "jre"
    arch = _arch()
    urls = [ADOPTIUM.format(arch=arch), PINNED.get(arch, PINNED["x64"])]
    last_error: Exception | None = None
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "jre.tar.gz"
        for url in urls:
            try:
                print("downloading temurin jre 17 from", url.split("?")[0], flush=True)
                _download(url, archive)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print("jre download failed:", exc, flush=True)
        if last_error and not archive.exists():
            raise last_error
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp)
        java = next(Path(tmp).rglob("bin/java"))
        home = java.parent.parent
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(home), str(dest))
    java_bin = dest / "bin" / "java"
    os.chmod(java_bin, 0o755)
    print("jre ready", java_bin, flush=True)
    return str(java_bin)


def ensure_java(root: Path | None = None) -> str | None:
    root = root or ROOT
    found = find_java(root)
    if found:
        print("java already available", found, flush=True)
        return found
    if not sys.platform.startswith("linux"):
        print("skip jre download: not linux and no usable java", flush=True)
        return None
    try:
        return download_jre(root)
    except Exception as error:  # noqa: BLE001
        print("jre download skipped:", error, flush=True)
        return None


def bind_env(java: str | None) -> None:
    if not java:
        # Не оставляем JAVA_BIN на несуществующий путь — генераторы
        # иначе останавливаются на первой же кандидатуре.
        if os.environ.get("JAVA_BIN") and not is_java(os.environ["JAVA_BIN"]):
            os.environ.pop("JAVA_BIN", None)
        return
    os.environ["JAVA_BIN"] = java
    bindir = os.path.dirname(java)
    path = os.environ.get("PATH", "")
    if bindir and bindir not in path.split(os.pathsep):
        os.environ["PATH"] = bindir + os.pathsep + path


def main() -> int:
    java = ensure_java()
    bind_env(java)
    return 0 if java else 0


if __name__ == "__main__":
    raise SystemExit(main())
