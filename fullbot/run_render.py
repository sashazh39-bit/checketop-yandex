#!/usr/bin/env python3
"""Render Web Service entrypoint для merged-бота (mts-bot + чекетоп).

Render Web Service требует открытый PORT и ответы на HTTP.
Скрипт поднимает health-сервер в фоновом потоке, а бота (aiogram polling)
запускает в главном потоке — как в исходном run_render.py чекетопа.

Env:
  TELEGRAM_BOT_TOKEN — токен preview-бота (иначе зашитый prod mts-бота)
  INTERNAL_TOKEN     — для api_call в ретранслятор (на Render может отсутствовать)
  JAVA_BIN / JZCOMP_DIR — путь к Java и JZComp (deflate законов, ставится в Dockerfile)
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# checketop_lib нужен адаптеру уже к моменту импорта mts_bot
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "checketop_lib"))

_BASE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_BASE)
os.environ.setdefault("JZCOMP_DIR", _BASE)
os.environ.setdefault("MTS_DONOR_SBP", os.path.join(_BASE, "donors", "file25.pdf"))
os.environ.setdefault("ANCHOR_STATE", os.path.join(_BASE, "anchor_state.json"))
os.environ.setdefault("CHECKER_ANCHORS_PATH", os.path.join(_BASE, "anchor_state.json"))
_seed = os.path.join(_BASE, "anchor_state.seed.json")
_state = os.path.join(_BASE, "anchor_state.json")
if os.path.isfile(_seed) and not os.path.isfile(_state):
    import shutil
    shutil.copyfile(_seed, _state)
os.environ.setdefault("TEMP", "/tmp")
if not os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("BOT_TOKEN"):
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["BOT_TOKEN"]


def _bind_java() -> str | None:
    """Найти java или скачать JRE до импорта генераторов.

    Render startCommand часто ставит JAVA_BIN=$PWD/jre/bin/java, хотя
    файла ещё нет. Не оставляем этот путь — JZComp иначе падает с
    «java executable not found».
    """
    scripts = os.path.join(_REPO, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import ensure_jre
    except Exception as exc:  # noqa: BLE001
        print("ensure_jre import failed:", exc, flush=True)
        return None
    java = ensure_jre.ensure_java(Path(_REPO))
    ensure_jre.bind_env(java)
    print("JAVA_BIN", os.environ.get("JAVA_BIN") or "(unset)", flush=True)
    return java

PORT = int(os.environ.get("PORT", 10000))
_STARTED = time.time()
_STATE = {"bot_alive": False, "last_error": ""}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/healthz"):
            payload = {
                "status": "ok" if _STATE["bot_alive"] else "starting",
                "bot_alive": _STATE["bot_alive"],
                "uptime_sec": int(time.time() - _STARTED),
                "last_error": _STATE["last_error"],
            }
            body = json.dumps(payload).encode()
            # 200 и на starting: иначе Render убивает сервис, пока качается JRE.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # тишина в логах Render
        pass


def _serve() -> None:
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


def _ensure_packages() -> None:
    import subprocess
    for mod, pkg in {"fitz": "PyMuPDF>=1.23.0", "fontTools": "fonttools>=4.43.0"}.items():
        try:
            __import__(mod)
        except ImportError:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                                      stdout=subprocess.DEVNULL)
            except Exception:
                pass


def main() -> None:
    _ensure_packages()
    threading.Thread(target=_serve, daemon=True).start()
    java = _bind_java()
    if not java:
        print("WARNING: java not found; WB/RSHB/Ozon/Yandex PDF deflate will fail", flush=True)
    import mts_bot

    orig_main = mts_bot.main

    async def wrapped() -> None:
        _STATE["bot_alive"] = True
        await orig_main()

    try:
        asyncio.run(wrapped())
    except Exception as e:  # noqa: BLE001
        _STATE["bot_alive"] = False
        _STATE["last_error"] = f"{type(e).__name__}: {e}"[:300]
        raise


if __name__ == "__main__":
    main()
