#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Необязательный вход в Яндекс Музыку по OAuth-ссылке.

Токен хранится только на станции (0600) и никогда не отдаётся браузеру.
Идея и client_id — из yapanel-web (MIT): dimap/yapanel-web.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AUTH_PATH = "/data/mypult/yandex_auth.json"
# OAuth-клиент Яндекс Музыки, которым пользуется yapanel-web (публичный client_id).
CLIENT_ID = os.environ.get("MYPULT_YM_CLIENT_ID") or "23cabbbdc6cd418abb4b39c32c41195d"
OAUTH_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token&client_id=" + CLIENT_ID
)
UA = "MidiPult/0.5 (+local station panel)"


def load_auth() -> dict:
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_auth(data: dict) -> None:
    folder = os.path.dirname(AUTH_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def token_from(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"access_token=([^&#\s]+)", text)
    token = match.group(1) if match else text
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{20,}", token or ""):
        raise ValueError("в ссылке нет access_token")
    return token


def account_info(token: str) -> dict:
    request = urllib.request.Request(
        "https://login.yandex.ru/info?format=json",
        headers={"Authorization": "OAuth " + token, "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ValueError("Яндекс не принял токен (HTTP %s)" % exc.code) from exc
    except (OSError, ValueError) as exc:
        raise ValueError("не удалось проверить токен: %s" % exc) from exc
    if not isinstance(data, dict) or not data.get("login"):
        raise ValueError("токен не подошёл")
    return data


def main() -> None:
    data = common.params()
    if common.truthy(data.get("logout")):
        try:
            os.unlink(AUTH_PATH)
        except OSError:
            pass
        common.ok(logged_in=False, oauth_url=OAUTH_URL)
        return

    if data.get("token") or data.get("url"):
        try:
            token = token_from(data.get("url") or data.get("token"))
            info = account_info(token)
            save_auth({
                "token": token,
                "login": str(info.get("login") or ""),
                "uid": str(info.get("id") or ""),
                "method": "oauth",
            })
        except ValueError as exc:
            common.fail(400, str(exc))
            return
        common.ok(logged_in=True, login=info.get("login"), oauth_url=OAUTH_URL)
        return

    auth = load_auth()
    common.ok(
        logged_in=bool(auth.get("token")),
        login=auth.get("login"),
        oauth_url=OAUTH_URL,
    )


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        common.fail(500, "Яндекс Музыка: %s" % exc)
