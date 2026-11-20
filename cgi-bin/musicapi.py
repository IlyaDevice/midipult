#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Лайки и дизлайки Яндекс Музыки через API станции.

    GET  /cgi-bin/musicapi.py          — id текущего трека и liked
    POST /cgi-bin/musicapi.py {"like": 1}    — поставить лайк
    POST /cgi-bin/musicapi.py {"like": 0}    — убрать
    POST /cgi-bin/musicapi.py {"dislike": 1} — «не нравится»

Токен не запрашивается у пользователя: берём OAuth из
/data/quasar/account_storage.dat (тот же, что у Glagol), проверяем через
/account/status и кэшируем uid в /data/mypult/music.json (600). Наружу токен
не отдаём. Идея и эндпоинты — из yapanel-web (dimap/glagol.py).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

try:
    import player  # noqa: E402
except Exception:  # noqa: BLE001
    player = None

ACCOUNT = "/data/quasar/account_storage.dat"
CACHE = "/data/mypult/music.json"
LIKES_CACHE = "/tmp/mypult-likes.json"
API = "https://api.music.yandex.net"
UA = "Yandex-Music-API"
CLIENT = "YandexMusicAndroid/24023621"
TOKEN_RE = re.compile(rb"y0_[A-Za-z0-9_\-]{20,}")


def _request(token, path, form=None, timeout=15):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(
        API + path, data=data, method="POST" if data is not None else "GET",
        headers={
            "Authorization": "OAuth " + token,
            "User-Agent": UA,
            "X-Yandex-Music-Client": CLIENT,
            "Content-Type": "application/x-www-form-urlencoded",
        })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def _tokens():
    try:
        with open(ACCOUNT, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [match.group(0).decode("ascii") for match in TOKEN_RE.finditer(raw)]


def _cache_read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def account(force=False):
    """(token, uid) с кэшем; токен наружу не отдаётся."""
    cached = _cache_read(CACHE)
    token = str(cached.get("token") or "")
    uid = str(cached.get("uid") or "")
    if token and uid and not force:
        return token, uid
    for candidate in _tokens():
        try:
            data = _request(candidate, "/account/status", timeout=12)
        except Exception:  # noqa: BLE001
            continue
        account_data = (data.get("result") or {}).get("account") or {}
        found = str(account_data.get("uid") or "")
        if found:
            try:
                common.write_json(CACHE, {"token": candidate, "uid": found})
            except OSError:
                pass
            return candidate, found
    raise RuntimeError("Яндекс Музыка не приняла токен станции")


def likes(force=False):
    cached = _cache_read(LIKES_CACHE)
    if (not force and cached.get("ids") is not None
            and time.time() - float(cached.get("ts") or 0) < 60):
        return set(str(item) for item in cached["ids"])
    token, uid = account()
    data = _request(token, "/users/%s/likes/tracks" % uid)
    library = (data.get("result") or {}).get("library") or {}
    ids = {str(item.get("id")) for item in (library.get("tracks") or [])
           if isinstance(item, dict)}
    try:
        common.write_json(LIKES_CACHE, {"ts": time.time(), "ids": sorted(ids)})
    except OSError:
        pass
    return ids


def set_like(track_id, on):
    token, uid = account()
    action = "add-multiple" if on else "remove"
    _request(token, "/users/%s/likes/tracks/%s" % (uid, action),
             form={"track-ids": str(track_id)})
    return str(track_id) in likes(force=True)


def set_dislike(track_id):
    token, uid = account()
    _request(token, "/users/%s/dislikes/tracks/add-multiple" % uid,
             form={"track-ids": str(track_id)})
    return True


def current_track():
    if player is None:
        return {}
    try:
        snap = player.snapshot()
    except Exception:  # noqa: BLE001
        return {}
    return {
        "id": str(snap.get("id") or ""),
        "title": snap.get("title") or "",
        "artist": snap.get("artist") or "",
        "kind": snap.get("kind"),
    }


def main():
    data = common.params()

    if data.get("like") is not None:
        track = current_track()
        if not track.get("id"):
            common.fail(400, "Нет активного трека")
            return
        on = common.truthy(data.get("like"))
        try:
            liked = set_like(track["id"], on)
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Лайк: %s" % exc)
            return
        common.ok(liked=liked, track=track)
        return

    if common.truthy(data.get("dislike")):
        track = current_track()
        if not track.get("id"):
            common.fail(400, "Нет активного трека")
            return
        try:
            set_dislike(track["id"])
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Дизлайк: %s" % exc)
            return
        common.ok(disliked=True, track=track)
        return

    track = current_track()
    liked = False
    error = ""
    if track.get("id"):
        try:
            liked = track["id"] in likes()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:200]
    common.ok(track=track, liked=liked, error=error)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Музыка: %s" % exc)
        except Exception:
            pass
