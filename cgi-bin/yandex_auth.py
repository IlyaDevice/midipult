#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Необязательный вход в Яндекс Музыку по OAuth-ссылке.

Токен хранится только на станции (0600) и никогда не отдаётся браузеру.
Идея и client_id — из yapanel-web (MIT): dimap/yapanel-web.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AUTH_PATH = "/data/mypult/yandex_auth.json"
# OAuth-клиент Яндекс Музыки, которым пользуется yapanel-web (публичный client_id).
CLIENT_ID = os.environ.get("MYPULT_YM_CLIENT_ID") or "23cabbbdc6cd418abb4b39c32c41195d"
CLIENT_SECRET = os.environ.get("MYPULT_YM_CLIENT_SECRET") or "53bc75238f0c4d08a118e51fe9203300"
PASSPORT_CLIENT = ("c0ebe342af7d48fbbbfcf2d2eedb8f9e", "ad0a908f0aa341a182a37ecd75bc319e")
OAUTH_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token&client_id=" + CLIENT_ID
)
UA = "MidiPult/0.5 (+local station panel)"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
QR_STATE = "/tmp/mypult-yandex-qr.json"
QR_COOKIES = "/tmp/mypult-yandex-qr.cookies"
QR_TTL = 300


class AuthError(RuntimeError):
    pass


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


# --------------------------------------------------------------------------- #
#  Вход по QR: ссылка подтверждается на телефоне, панель сама забирает токен
# --------------------------------------------------------------------------- #

def _request(opener, url, data=None, headers=None, json_body=None, timeout=20):
    head = {"User-Agent": BROWSER_UA}
    head.update(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        head["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        head["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=head,
                                     method="POST" if body is not None else "GET")
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise AuthError("%s: HTTP %s" % (urllib.parse.urlsplit(url).netloc, exc.code)) from exc
    except OSError as exc:
        raise AuthError("%s: %s" % (urllib.parse.urlsplit(url).netloc, exc)) from exc


def _cleanup_qr():
    for path in (QR_STATE, QR_COOKIES):
        try:
            os.unlink(path)
        except OSError:
            pass


def _save_qr(state):
    tmp = QR_STATE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, QR_STATE)


def _load_qr():
    try:
        with open(QR_STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _qr_opener():
    jar = http.cookiejar.MozillaCookieJar(QR_COOKIES)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        pass
    return jar, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _x_token_to_music(x_token):
    raw = _request(None, "https://oauth.mobile.yandex.net/1/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "x-token", "access_token": x_token,
    })
    try:
        token = json.loads(raw or "{}").get("access_token")
    except ValueError:
        token = None
    if not token:
        raise AuthError("Яндекс не выдал токен Музыки")
    return token


def qr_start():
    """Создать QR-сессию и вернуть ссылку для подтверждения на телефоне."""
    _cleanup_qr()
    jar = http.cookiejar.MozillaCookieJar(QR_COOKIES)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = _request(opener, "https://passport.yandex.ru/pwl-yandex")
    match = re.search(r'__CSRF__\s*=\s*"([^"]+)', page)
    if not match:
        raise AuthError("Яндекс изменил страницу входа — войдите по ссылке")
    headers = {"X-CSRF-Token": match.group(1),
               "Referer": "https://passport.yandex.ru/pwl-yandex"}
    try:
        submit = json.loads(_request(
            opener,
            "https://passport.yandex.ru/pwl-yandex/api/passport/auth/password/submit",
            json_body={"retpath": "https://passport.yandex.ru/"}, headers=headers))
        magic = json.loads(_request(
            opener,
            "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code",
            data={"location_id": "0", "magic_track_id": submit["track_id"],
                  "track_id": ""}, headers=headers))
    except (KeyError, ValueError) as exc:
        raise AuthError("Яндекс вернул неожиданный ответ при входе") from exc
    link = str(magic.get("link") or "")
    if not link.startswith("https://"):
        raise AuthError("Яндекс не выдал ссылку для входа")
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(QR_COOKIES, 0o600)
    except OSError:
        pass
    _save_qr({"headers": headers, "submit": submit, "started": time.time()})
    return {"state": "waiting", "link": link, "expires_in": QR_TTL}


def qr_status():
    """Проверить подтверждение и при успехе сохранить токен Музыки."""
    state = _load_qr()
    if not state:
        return {"state": "none"}
    if time.time() - float(state.get("started") or 0) > QR_TTL:
        _cleanup_qr()
        return {"state": "expired"}
    jar, opener = _qr_opener()
    result = json.loads(_request(
        opener,
        "https://passport.yandex.ru/pwl-yandex/api/passport/auth/magic/code/status",
        json_body=state.get("submit") or {}, headers=state.get("headers") or {}))
    if result.get("state") != "otp_auth_finished":
        return {"state": "waiting"}
    _request(opener,
             "https://passport.yandex.ru/pwl-yandex/api/passport/sessions/get_session",
             data={"track_id": result.get("trackId") or ""},
             headers=state.get("headers") or {})
    cookies = "; ".join("%s=%s" % (cookie.name, cookie.value)
                        for cookie in jar if cookie.domain.endswith("yandex.ru"))
    raw = _request(None, "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
                   data={"client_id": PASSPORT_CLIENT[0],
                         "client_secret": PASSPORT_CLIENT[1]},
                   headers={"Ya-Client-Host": "passport.yandex.ru",
                            "Ya-Client-Cookie": cookies})
    try:
        x_token = json.loads(raw or "{}").get("access_token")
    except ValueError:
        x_token = None
    if not x_token:
        raise AuthError("не удалось получить токен по сессии")
    music_token = _x_token_to_music(x_token)
    info = account_info(music_token)
    save_auth({"token": music_token, "login": str(info.get("login") or ""),
               "uid": str(info.get("id") or ""), "method": "qr"})
    _cleanup_qr()
    return {"state": "done", "logged_in": True, "login": info.get("login")}


def main() -> None:
    data = common.params()
    if common.truthy(data.get("logout")):
        try:
            os.unlink(AUTH_PATH)
        except OSError:
            pass
        _cleanup_qr()
        common.ok(logged_in=False, oauth_url=OAUTH_URL)
        return

    if common.truthy(data.get("qr_start")):
        try:
            common.ok(**qr_start())
        except AuthError as exc:
            common.fail(502, str(exc))
        return

    if common.truthy(data.get("qr_status")):
        try:
            common.ok(**qr_status())
        except (AuthError, ValueError) as exc:
            common.fail(502, str(exc))
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
