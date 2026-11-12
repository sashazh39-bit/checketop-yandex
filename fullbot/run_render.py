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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# checketop_lib нужен адаптеру уже к моменту импорта mts_bot
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "checketop_lib"))

_BASE = os.path.dirname(os.path.abspath(__file__))
_JRE = os.path.join(os.path.dirname(_BASE), "jre", "bin", "java")
if not os.environ.get("JAVA_BIN"):
    os.environ["JAVA_BIN"] = _JRE if os.path.exists(_JRE) else "/usr/bin/java"
os.environ.setdefault("JZCOMP_DIR", _BASE)
os.environ.setdefault("MTS_DONOR_SBP", os.path.join(_BASE, "donors", "file25.pdf"))
os.environ.setdefault("TEMP", "/tmp")
if not os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("BOT_TOKEN"):
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["BOT_TOKEN"]

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
            self.send_response(200 if _STATE["bot_alive"] else 503)
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
