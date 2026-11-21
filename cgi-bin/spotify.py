#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Экспериментальный Spotify: поиск по Web API.

Spotify Web API не отдаёт аудиопоток. Карточка ищет треки и открывает их в
Spotify; для звука на станции нужен отдельный Spotify Connect (librespot).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DATA_PATH = "/data/mypult/spotify.json"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
UA = "MidiPult/1.0"


def _read():
    return common.read_json(DATA_PATH, {}) or {}


def _creds():
    data = _read()
    return str(data.get("client_id") or ""), str(data.get("secret") or "")


def _token(client_id, secret, force=False):
    data = _read()
    now = int(time.time())
    if not force and data.get("access_token") and int(data.get("expires") or 0) > now + 30:
        return data["access_token"]
    auth = base64.b64encode((client_id + ":" + secret).encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": "Basic " + auth,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Spotify token: %s" % exc)
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Spotify не выдал access token")
    data.update({
        "access_token": token,
        "expires": now + int(payload.get("expires_in") or 3600),
    })
    common.write_json(DATA_PATH, data, mode=0o600)
    return token


def _api(path, params, token):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Spotify API: %s" % exc)


def _track(item):
    if not isinstance(item, dict) or not item.get("id"):
        return None
    images = ((item.get("album") or {}).get("images") or [])
    artists = ", ".join(str(a.get("name") or "") for a in item.get("artists") or [])
    return {
        "id": item.get("id"),
        "title": item.get("name") or "",
        "artist": artists,
        "album": (item.get("album") or {}).get("name") or "",
        "cover": (images[0].get("url") if images else ""),
        "duration": round((item.get("duration_ms") or 0) / 1000),
        "url": ((item.get("external_urls") or {}).get("spotify") or ""),
    }


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()
    client_id, secret = _creds()

    if action == "status":
        common.ok(has_credentials=bool(client_id and secret))
        return

    if action == "credentials":
        cid = str(data.get("client_id") or "").strip()
        sec = str(data.get("secret") or "").strip()
        if not cid or not sec:
            raise RuntimeError("Нужны Client ID и Client Secret")
        _token(cid, sec, force=True)
        common.write_json(DATA_PATH, {"client_id": cid, "secret": sec}, mode=0o600)
        common.ok(has_credentials=True)
        return

    if action == "clear":
        try:
            os.unlink(DATA_PATH)
        except OSError:
            pass
        common.ok(has_credentials=False)
        return

    if action == "search":
        query = str(data.get("q") or "").strip()
        if not query:
            raise RuntimeError("Пустой поисковый запрос")
        if not client_id or not secret:
            raise RuntimeError("Сначала сохраните Client ID и Client Secret")
        token = _token(client_id, secret)
        payload = _api("/search", {
            "q": query, "type": "track", "limit": common.to_int(data.get("limit"), 12, 1, 25),
        }, token)
        items = ((payload.get("tracks") or {}).get("items") or [])
        tracks = [t for t in (_track(item) for item in items) if t]
        common.ok(tracks=tracks)
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
