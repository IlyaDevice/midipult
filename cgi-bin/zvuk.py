#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Сбер Звук: токен, поиск, «Мой поток» и запуск через штатный плеер станции."""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import radio  # noqa: E402
import zvuk_api  # noqa: E402

DATA_DIR = "/data/mypult"
AUTH_PATH = os.path.join(DATA_DIR, "zvuk.json")
ANON_PATH = os.path.join(DATA_DIR, "zvuk_anon.json")
ANON_TTL = 3600
CACHE_DIR = os.path.join(DATA_DIR, "zvuk-cache")
STATE_PATH = os.path.join(DATA_DIR, "zvuk_state.json")
SELF_URL = "http://127.0.0.1:8055/cgi-bin/zvuk_stream.py"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _auth():
    return zvuk_api.Auth(AUTH_PATH)


def _api():
    token = _auth().token
    if not token:
        cached = common.read_json(ANON_PATH, {}) or {}
        if cached.get("token") and time.time() - float(cached.get("ts") or 0) < ANON_TTL:
            token = str(cached["token"])
        else:
            try:
                token = zvuk_api.anonymous_token()
                common.write_json(ANON_PATH, {"token": token, "ts": time.time()}, mode=0o600)
            except zvuk_api.Error:
                token = ""
    if not token:
        raise zvuk_api.AuthError("Не удалось получить доступ к Сбер Звуку")
    return zvuk_api.Api(token)


def _state():
    return common.read_json(STATE_PATH, {}) or {}


def _save_state(data):
    common.write_json(STATE_PATH, data, mode=0o600)


def _track_id(value):
    track_id = str(value or "").strip()
    if not ID_RE.fullmatch(track_id):
        raise zvuk_api.Error("Некорректный id трека")
    return track_id


def _play(data):
    if not _auth().token:
        raise zvuk_api.AuthError(
            "Без аккаунта Сбер Звук даёт только 30 секунд. Войдите на zvuk.com, "
            "скопируйте token со страницы профиля API и сохраните его здесь")
    track_id = _track_id(data.get("id"))
    api = _api()
    url = api.stream_url(track_id)
    os.makedirs(CACHE_DIR, exist_ok=True)
    target = os.path.join(CACHE_DIR, track_id + ".mp3")
    api.download(url, target)
    local = "%s?id=%s" % (SELF_URL, track_id)
    title = str(data.get("title") or "Сбер Звук")
    artist = str(data.get("artist") or "")
    name = (title + (" — " + artist if artist else "")).strip()
    try:
        radio.play_stream(name, local)
    except Exception as exc:  # noqa: BLE001
        raise zvuk_api.Error("Не удалось запустить на станции: %s" % exc)
    state = {
        "id": track_id,
        "title": title,
        "artist": artist,
        "cover": str(data.get("cover") or ""),
        "duration": common.to_int(data.get("duration"), 0),
        "stream": local,
    }
    _save_state(state)
    return {"playing": True, "track": state}


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()
    auth = _auth()

    if action == "status":
        common.ok(has_token=bool(auth.token), track=_state())
        return

    if action == "token":
        token = str(data.get("token") or "").strip()
        if not token:
            raise zvuk_api.AuthError("Токен пустой")
        try:
            zvuk_api.Api(token).personal_wave(1)
        except zvuk_api.Error as exc:
            raise zvuk_api.AuthError("Токен не принят: %s" % exc)
        auth.save(token)
        common.ok(has_token=True)
        return

    if action == "clear":
        auth.clear()
        try:
            os.unlink(STATE_PATH)
        except OSError:
            pass
        common.ok(has_token=False)
        return

    if action == "search":
        query = str(data.get("q") or "").strip()
        if not query:
            raise zvuk_api.Error("Пустой поисковый запрос")
        result = _api().search(query, common.to_int(data.get("limit"), 12, 1, 30))
        common.ok(**result)
        return

    if action in ("wave", "my_wave"):
        if not auth.token:
            raise zvuk_api.AuthError("«Мой поток» требует аккаунт Сбер Звука — поиск работает анонимно")
        count = common.to_int(data.get("count"), 12, 1, 30)
        tracks = _api().personal_wave(count)
        common.ok(tracks=tracks)
        return

    if action == "play":
        common.ok(**_play(data))
        return

    raise zvuk_api.Error("Неизвестное действие")


if __name__ == "__main__":
    try:
        main()
    except zvuk_api.AuthError as exc:
        common.fail(401, str(exc))
    except zvuk_api.Error as exc:
        common.fail(502, str(exc))
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка zvuk.py: %s" % exc)
        except Exception:
            pass
