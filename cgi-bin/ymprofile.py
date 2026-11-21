#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Профиль Яндекс-аккаунта для шапки Яндекс Музыки.

    GET /cgi-bin/ymprofile.py

Токен берётся из `/data/mypult/yandex_auth.json` (тот же, что у Яндекс Музыки)
и наружу не отдаётся: панель получает только имя и ссылку на аватар.
Ответ кэшируется в `/tmp/mypult-ymprofile.json` на час.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AUTH_PATH = "/data/mypult/yandex_auth.json"
CACHE = "/tmp/mypult-ymprofile.json"
TTL = 3600
INFO_URL = "https://login.yandex.ru/info?format=json"
AVATAR = "https://avatars.yandex.net/get-yapic/%s/islands-200"


def token():
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("token")
    except (OSError, ValueError, TypeError):
        value = None
    return str(value).strip() if value else ""


def fetch(access_token):
    request = urllib.request.Request(INFO_URL, headers={
        "Authorization": "OAuth " + access_token,
        "User-Agent": "MidiPult",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    avatar_id = str(data.get("default_avatar_id") or "").strip()
    empty = bool(data.get("is_avatar_empty"))
    return {
        "ok": True,
        "login": data.get("login") or "",
        "name": data.get("display_name") or data.get("real_name") or "",
        "avatar": (AVATAR % avatar_id) if avatar_id and not empty else None,
        "checked_at": int(time.time()),
    }


def cached():
    try:
        with open(CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and time.time() - int(data.get("checked_at") or 0) < TTL:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def main():
    data = common.params()

    old = cached()
    if old and not common.truthy(data.get("refresh")):
        common.ok(**old)
        return

    access_token = token()
    if not access_token:
        common.ok(ok=False, reason="нет входа в Яндекс Музыку")
        return

    try:
        profile = fetch(access_token)
    except Exception as exc:  # noqa: BLE001
        if old:
            common.ok(**old)
            return
        common.ok(ok=False, reason="профиль недоступен: %s" % exc)
        return

    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False)
    except OSError:
        pass
    common.ok(**profile)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка ymprofile.py: %s" % exc)
        except Exception:
            pass
