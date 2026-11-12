#!/usr/bin/env python3
"""Запуск бота как Web Service для Render.com (бесплатный тариф).

Render требует Web Service (не Worker): этот скрипт поднимает HTTP-сервер
для health check в фоне, а бота запускает в ГЛАВНОМ потоке — это обязательно
для asyncio (python-telegram-bot v20+).

Без входящих запросов Render засыпает через 15 мин — подключите
UptimeRobot (бесплатно) для пинга каждые 5 мин.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# Ensure critical packages are installed (guards against stale pip cache on Render)
def _ensure_packages() -> None:
    _needed = {"fontTools": "fonttools>=4.43.0", "fitz": "PyMuPDF>=1.23.0"}
    for mod, pkg in _needed.items():
        try:
            __import__(mod)
        except ImportError:
            print(f"[render] {mod} not found — installing {pkg} ...", flush=True)
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                    stdout=subprocess.DEVNULL,
                )
                print(f"[render] {pkg} installed OK", flush=True)
            except Exception as exc:
                print(f"[render] WARNING: could not install {pkg}: {exc}", flush=True)

_ensure_packages()

# PORT обязателен для Render: https://render.com/docs/web-services#port-binding
PORT = int(os.environ.get("PORT", 10000))

_STARTED_AT = time.time()
_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "bot_alive": False,
    "last_error": "",
    "uptime_started": _STARTED_AT,
}


def _set(**kw) -> None:
    with _STATE_LOCK:
        _STATE.update(kw)


def _get() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        st = _get()
        alive = st.get("bot_alive", False)
        uptime = int(time.time() - st.get("uptime_started", time.time()))

        if self.path in ("/", "/healthz"):
            status = 200 if alive else 503
            payload = {
                "status": "ok" if alive else "starting",
                "uptime_sec": uptime,
                "bot_alive": alive,
                "last_error": st.get("last_error", ""),
            }
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/metrics":
            body = (
                f"bot_alive {1 if alive else 0}\n"
                f"bot_uptime_seconds {uptime}\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, fmt, *args) -> None:  # silence access log
        pass


def _http_server_thread() -> None:
    class _Server(HTTPServer):
        allow_reuse_address = True

    server = _Server(("0.0.0.0", PORT), HealthHandler)
    print(f"[render] HTTP health-check on 0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception as e:
        print(f"[render] HTTP server error: {e}", flush=True)


def _self_ping_thread() -> None:
    """Ping own health endpoint every 5 min to prevent Render free-tier spin-down."""
    import urllib.request

    # Render exposes the external URL via env var; fall back to localhost
    base_url = (
        os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        or f"http://localhost:{PORT}"
    )
    ping_url = f"{base_url}/healthz"
    print(f"[render] Self-ping enabled → {ping_url} every 5 min", flush=True)

    # Wait for bot to fully start before first ping
    time.sleep(30)
    while True:
        try:
            with urllib.request.urlopen(ping_url, timeout=10) as resp:
                _ = resp.read()
        except Exception as exc:
            print(f"[render] Self-ping failed: {exc}", flush=True)
        time.sleep(300)  # 5 minutes


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[render] TELEGRAM_BOT_TOKEN не задан — выход", flush=True)
        sys.exit(1)

    # Стартуем HTTP-сервер в фоне (для Render health check)
    t = threading.Thread(target=_http_server_thread, daemon=True, name="http-health")
    t.start()

    # Self-ping каждые 5 мин чтобы Render не засыпал (free tier)
    sp = threading.Thread(target=_self_ping_thread, daemon=True, name="self-ping")
    sp.start()

    # Импортируем бот — если упадёт здесь, Render увидит exit(1) и перезапустит
    try:
        from bot_standalone import main as bot_main
    except Exception as exc:
        print(f"[render] Ошибка импорта bot.py: {exc}", flush=True)
        _set(last_error=str(exc))
        sys.exit(1)

    _set(bot_alive=True)
    print("[render] Запускаем бота (polling) в главном потоке...", flush=True)

    try:
        bot_main()  # блокирует; внутри есть restart-loop
    except SystemExit:
        raise
    except Exception as exc:
        _set(bot_alive=False, last_error=str(exc)[:500])
        print(f"[render] Бот упал: {exc}", flush=True)
        sys.exit(1)

    # Если bot_main() вернулся штатно — всё равно выходим,
    # чтобы Render мог перезапустить при необходимости.
    print("[render] bot_main() завершился — выход", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
