#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""VK Музыка (бета): поиск по токену VK и поток через штатный плеер.

Токен нужен с доступом к аудио и хранится только на станции (0600).
VK-API неофициальный и может меняться — провайдер помечен как бета.
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
import radio  # noqa: E402

TOKEN_PATH = "/data/mypult/vk_token.json"
STATE_PATH = "/data/mypult/vk_state.json"
SELF_URL = "http://127.0.0.1:8055/cgi-bin/vk_stream.py"
API_URL = "https://api.vk.com/method/"
API_VERSION = "5.199"
UA = "MidiPult/0.5 (+local station panel)"


def _token_from(value):
    """Принять сам токен или полную ссылку после OAuth VK."""
    text = str(value or "").strip()
    match = re.search(r"access_token=([^&#\s]+)", text)
    token = match.group(1) if match else text
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{20,}", token or ""):
        raise RuntimeError("В ссылке нет access_token VK")
    return token


def _read_token():
    data = common.read_json(TOKEN_PATH, {}) or {}
    token = str(data.get("token") or "").strip()
    return token or None


def _save_token(token):
    common.write_json(TOKEN_PATH, {"token": token}, mode=0o600)


def _state():
    return common.read_json(STATE_PATH, {}) or {}


def _save_state(data):
    common.write_json(STATE_PATH, data, mode=0o600)


def _api(method, params, token=None):
    token = token or _read_token()
    if not token:
        raise RuntimeError("Сначала сохраните токен VK")
    query = dict(params)
    query["access_token"] = token
    query["v"] = API_VERSION
    url = API_URL + method + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("VK API недоступен: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("VK API вернул неожиданный ответ")
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("error_code")
        message = str(error.get("error_msg") or "ошибка VK")
        if code == 5:
            raise RuntimeError("VK не принял токен — сохраните новый")
        if code == 15:
            raise RuntimeError("токену VK не разрешён доступ к аудио")
        raise RuntimeError("VK: %s" % message)
    return payload.get("response")


def _cover(item):
    album = item.get("album") or {}
    thumb = album.get("thumb") or {}
    if isinstance(thumb, dict):
        for key in ("photo_600", "photo_300", "photo_270", "photo_1200"):
            if thumb.get(key):
                return str(thumb[key])
    for key in ("photo_600", "photo_300", "photo_270"):
        if item.get(key):
            return str(item[key])
    return ""


def _track(item):
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "")
    title = str(item.get("title") or "").strip()
    if not url or not title:
        return None
    owner_id = item.get("owner_id")
    track_id = item.get("id")
    return {
        "id": ("%s_%s" % (owner_id, track_id)) if owner_id is not None else str(track_id or ""),
        "title": title,
        "artist": str(item.get("artist") or ""),
        "duration": common.to_int(item.get("duration"), 0),
        "cover": _cover(item),
        "url": url,
    }


def _search(query, limit=15):
    response = _api("audio.search", {
        "q": query, "count": max(1, min(limit, 30)), "sort": 0, "autoplay": 1,
    })
    items = response.get("items") if isinstance(response, dict) else response
    tracks = [track for track in (_track(item) for item in (items or [])) if track]
    if not tracks:
        raise RuntimeError("VK Музыка ничего не нашла")
    return tracks[:limit]


def _mine(limit=30):
    response = _api("audio.get", {"count": max(1, min(limit, 50))})
    items = response.get("items") if isinstance(response, dict) else response
    tracks = [track for track in (_track(item) for item in (items or [])) if track]
    if not tracks:
        raise RuntimeError("В VK Музыке нет доступных треков")
    return tracks[:limit]


def _safe_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        raise RuntimeError("Некорректная ссылка на поток")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
            host.endswith("userapi.com") or host.endswith("vk.com")
            or host.endswith("vk-cdn.net")):
        raise RuntimeError("VK вернул неожиданный адрес потока")
    return parsed.geturl()


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()

    if action == "status":
        common.ok(has_token=bool(_read_token()), track=_state())
        return

    if action == "token":
        token = _token_from(data.get("token"))
        if len(token) < 20:
            raise RuntimeError("Токен VK слишком короткий")
        # users.get проверяет сам токен; доступ к аудио проверится при поиске.
        profile = _api("users.get", {"fields": "screen_name"}, token=token)
        user = profile[0] if isinstance(profile, list) and profile else {}
        _save_token(token)
        common.ok(has_token=True, login=str(user.get("screen_name") or user.get("first_name") or ""))
        return

    if action == "clear":
        try:
            os.unlink(TOKEN_PATH)
        except OSError:
            pass
        common.ok(has_token=False)
        return

    if action == "search":
        query = str(data.get("q") or "").strip()
        if not query:
            raise RuntimeError("Пустой поисковый запрос")
        common.ok(tracks=_search(query, common.to_int(data.get("limit"), 15, 1, 30)))
        return

    if action == "mine":
        common.ok(tracks=_mine(common.to_int(data.get("limit"), 30, 1, 50)))
        return

    if action == "play":
        source_url = _safe_url(data.get("url"))
        title = str(data.get("title") or "VK Музыка")
        artist = str(data.get("artist") or "")
        track_id = str(data.get("id") or "")
        local = "%s?id=%s" % (SELF_URL, urllib.parse.quote(track_id))
        state = {
            "id": track_id, "title": title, "artist": artist,
            "cover": str(data.get("cover") or ""), "url": source_url,
            "fmt": "MP3", "stream": local,
        }
        _save_state(state)
        try:
            radio.play_stream((title + (" — " + artist if artist else "")).strip(),
                              local, fmt="MP3", subtitle=artist)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Поток не запустился: %s" % exc)
        common.ok(playing=True, track=state)
        return

    if action == "stop":
        _save_state({})
        common.ok(playing=False)
        return

    raise RuntimeError("Неизвестное действие")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(502, str(exc))
        except Exception:
            pass
