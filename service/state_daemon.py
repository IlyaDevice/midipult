#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Постоянный процесс состояния пульта на 127.0.0.1:8057.

lighttpd проксирует /state сюда, поэтому панель получает статус и плеер
без запуска нового Python-процесса на каждый запрос.

Данные обновляются в фоне раз в 2 секунды. Если процесс недоступен,
панель автоматически возвращается к CGI /cgi-bin/state.py.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Используем системное время станции, отдельных настроек нет.
sys.path.insert(0, "/data/myweb/cgi-bin")
import common  # noqa: E402
import glagol  # noqa: E402
import player  # noqa: E402
import sounds  # noqa: E402
import status  # noqa: E402
import system  # noqa: E402

try:
    import discovery  # noqa: E402
except ImportError:
    discovery = None

PORT = int(os.environ.get("MYPULT_STATE_PORT") or "8057")
LOCK = threading.Lock()
PLAYER_LOCK = threading.Lock()
STATE = {"ok": True, "status": {}, "player": {}}
SYSTEM_STATE = {}
SOUNDS_STATE = {}
PLAYER_STATE = {}
PLAYER_INTERVAL = 10.0


def player_now():
    """Свежий снимок Glagol, не ожидая 10-секундного фонового цикла."""
    with PLAYER_LOCK:
        try:
            return player.snapshot()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}


def cookie_ok(headers):
    config = common.auth_config()
    if not config.get("enabled"):
        return True
    token = str(config.get("token") or "")
    if not token:
        return False
    for part in (headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "mypult_token" and hmac.compare_digest(value.strip(), token):
            return True
    return False


def ring_forward(colors):
    """Кадр кольца в Luna без запуска CGI: один HTTP-запрос на кадр.

    Прямая запись в /dev/fb0 не подходит: прошивка непрерывно перерисовывает
    кольцо в свой framebuffer и затирает кадр (проверено на колонке).
    Luna обновляет кольцо с частотой ~25–27 fps — это и есть потолок.
    """
    body = json.dumps({"colors": colors}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", 8099, timeout=2.0)
    try:
        connection.request("POST", "/api/led/ring", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read(2048)
        return 200 <= response.status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        try:
            connection.close()
        except OSError:
            pass


def refresh_loop():
    global STATE, PLAYER_STATE
    while True:
        payload = {"ok": True, "status": {}, "player": {}}
        try:
            payload["status"] = status.build()
        except Exception as exc:  # noqa: BLE001
            payload["status"] = {"ok": False, "error": str(exc)[:200]}
        # Watcher держит WS к Glagol и обновляет state пушем, поэтому снимок
        # дешёвый: берём его каждые 2 с, а не раз в 10.
        PLAYER_STATE = player_now()
        payload["player"] = PLAYER_STATE
        with LOCK:
            payload["system"] = SYSTEM_STATE
            payload["sounds"] = SOUNDS_STATE
            STATE = payload
        time.sleep(2)


def system_loop():
    global SYSTEM_STATE
    while True:
        try:
            value = system.snapshot()
        except Exception as exc:  # noqa: BLE001
            value = {"error": str(exc)[:200]}
        with LOCK:
            SYSTEM_STATE = value
        time.sleep(10)


def sounds_loop():
    global SOUNDS_STATE
    while True:
        try:
            value = sounds.snapshot()
        except Exception as exc:  # noqa: BLE001
            value = {"error": str(exc)[:200]}
        with LOCK:
            SOUNDS_STATE = value
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "mypult-state/1.0"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/state", "/state/"):
            self.send_error(404)
            return
        if not cookie_ok(self.headers):
            body = b'{"ok":false,"error":"\xd0\x9d\xd1\x83\xd0\xb6\xd0\xb5\xd0\xbd PIN","auth":true}'
            self.send_response(401)
        else:
            query = urllib.parse.parse_qs(parsed.query)
            full = "full" in query
            if "player" in query:
                fresh = player_now()
                with LOCK:
                    PLAYER_STATE = fresh
                    STATE["player"] = fresh
            with LOCK:
                value = STATE if full else {
                    "ok": STATE.get("ok", True),
                    "status": STATE.get("status", {}),
                    "player": STATE.get("player", {}),
                }
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/ring", "/ring/"):
            self.send_error(404)
            return
        if not cookie_ok(self.headers):
            self._json(401, {"ok": False, "error": "Нужен PIN", "auth": True})
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        colors = data.get("colors") if isinstance(data, dict) else None
        if (not isinstance(colors, list) or not 1 <= len(colors) <= 24
                or not all(isinstance(item, str) and 1 <= len(item) <= 9
                           for item in colors)):
            self._json(400, {"ok": False, "error": "colors"})
            return
        sent = ring_forward(colors[:24])
        self._json(200 if sent else 502, {"ok": sent})

    def log_message(self, *args):  # noqa: D401
        return


def main():
    try:
        glagol.start_watcher()
    except Exception:  # noqa: BLE001 - без watcher плеер работает через запросы
        pass
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=system_loop, daemon=True).start()
    threading.Thread(target=sounds_loop, daemon=True).start()
    if discovery is not None:
        try:
            discovery.start()
        except OSError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
