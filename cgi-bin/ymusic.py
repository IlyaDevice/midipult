#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Плейлисты Яндекс Музыки для необязательного входа.

Токен берётся только из /data/mypult/yandex_auth.json и не возвращается
в браузер. Воспроизведение идёт штатной Алисой по названию плейлиста.
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
import musicapi  # noqa: E402

AUTH_PATH = "/data/mypult/yandex_auth.json"
API = "https://api.music.yandex.net"
UA = "Yandex-Music-API"


def token() -> str:
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("token")
    except (OSError, ValueError, TypeError):
        value = None
    if not value:
        common.fail(401, "Сначала войдите в Яндекс Музыку")
        sys.exit(0)
    return str(value)


def api(path: str, query: dict | None = None, token_value: str | None = None,
        optional: bool = False) -> object:
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={
        "Authorization": "OAuth " + (token_value or token()),
        "User-Agent": UA,
        "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if optional:
            return None
        common.fail(502, "Яндекс Музыка: HTTP %s" % exc.code)
        sys.exit(0)
    except (OSError, ValueError) as exc:
        if optional:
            return None
        common.fail(502, "Яндекс Музыка: %s" % exc)
        sys.exit(0)


def cover(uri) -> str | None:
    return "https://" + str(uri).replace("%%", "200x200") if uri else None


def short_track(item: dict) -> dict:
    track = item.get("track") if isinstance(item.get("track"), dict) else item
    artists = ", ".join(str(a.get("name") or "") for a in (track.get("artists") or []))
    album = (track.get("albums") or [{}])[0]
    return {
        "id": str(track.get("id") or ""),
        "title": str(track.get("title") or "Без названия"),
        "artist": artists,
        "duration": float(track.get("durationMs") or 0) / 1000.0,
        "cover": cover(track.get("coverUri") or album.get("coverUri")),
    }


def playlist_tracks(playlist_id: str) -> list:
    auth_token = token()
    if ":" not in playlist_id:
        common.fail(400, "Неверный плейлист")
        sys.exit(0)
    owner, kind = playlist_id.split(":", 1)
    data = api("/users/%s/playlists/%s" % (owner, kind),
               token_value=auth_token, optional=True) or {}
    return [short_track(item) for item in (data.get("tracks") or [])]


def liked_tracks() -> list:
    auth_token = token()
    account = api("/account/status", token_value=auth_token) or {}
    uid = (account.get("account") or {}).get("uid")
    if not uid:
        return []
    library = (api("/users/%s/likes/tracks" % uid, token_value=auth_token,
                   optional=True) or {}).get("library") or {}
    ids = [str(item.get("id")) for item in (library.get("tracks") or []) if item.get("id")][:80]
    if not ids:
        return []
    result = api("/tracks", {"track-ids": ",".join(ids)},
                 token_value=auth_token, optional=True) or []
    return [short_track(item) for item in result]


def my_playlists() -> list:
    auth_token = token()
    account = api("/account/status", token_value=auth_token) or {}
    uid = (account.get("account") or {}).get("uid")
    if not uid:
        return []
    items = api("/users/%s/likes/playlists" % uid,
                token_value=auth_token, optional=True) or []
    out = []
    for item in items:
        playlist = item.get("playlist") if isinstance(item, dict) else None
        if not playlist or playlist.get("kind") is None:
            continue
        owner = ((playlist.get("owner") or {}).get("uid"))
        if not owner:
            continue
        out.append({
            "id": "%s:%s" % (owner, playlist["kind"]),
            "title": str(playlist.get("title") or "Плейлист"),
            "cover": cover((playlist.get("cover") or {}).get("uri")),
            "tracks": playlist.get("trackCount") or 0,
        })
    return out


def album_tracks(album_id: str) -> list:
    data = api("/albums/%s/with-tracks" % album_id, optional=True) or {}
    out = []
    for volume in (data.get("volumes") or []):
        for track in volume:
            if track.get("id"):
                out.append(short_track(track))
    return out


def _books_html(path: str) -> str:
    request = urllib.request.Request(
        "https://books.yandex.ru" + path,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", "replace")


def _books_items(page: str, limit: int = 12) -> list:
    out = []
    seen = set()
    blocks = re.split(r'(?=data-test-id="CONTENT_PREVIEW")', page)
    for block in blocks:
        link = re.search(r'href="/audiobooks/([A-Za-z0-9]+)"', block)
        if not link:
            continue
        book_id = link.group(1)
        if book_id in seen:
            continue
        seen.add(book_id)
        title_match = re.search(
            r'data-test-id="CONTENT_PREVIEW_TITLE"[^>]*>(.*?)</span>', block, re.S)
        title = re.sub(r"<[^>]+>", "", title_match.group(1)) if title_match else "Аудиокнига"
        author_match = re.findall(
            r'href="/authors/[^"]+"[^>]*>([^<]{2,100})</a>', block)
        author = author_match[-1] if author_match else "Яндекс Книги"
        cover_match = re.search(
            r'<img[^>]+src="(https://api\.bookmate\.ru/assets/(?:audio)?books?-covers/[^"]+)"',
            block)
        out.append({
            "id": book_id,
            "title": str(title).strip()[:160],
            "author": str(author).strip()[:100],
            "cover": cover_match.group(1) if cover_match else None,
            "url": "https://books.yandex.ru/audiobooks/" + book_id,
        })
        if len(out) >= limit:
            break
    return out


def books_search(text: str) -> list:
    try:
        query = urllib.parse.urlencode({"q": str(text or "").strip()[:120]})
        return _books_items(_books_html("/search?" + query))
    except (OSError, ValueError):
        return []


def books_popular() -> list:
    try:
        return _books_items(_books_html("/audiobooks"))
    except (OSError, ValueError):
        return []


def search_all(text: str) -> dict:
    text = str(text or "").strip()[:120]
    if len(text) < 2:
        return {"tracks": [], "albums": [], "playlists": [], "artists": [], "podcasts": []}
    data = api("/search", {"text": text, "type": "all", "page": 0}, optional=True) or {}
    albums = []
    for album in ((data.get("albums") or {}).get("results") or [])[:12]:
        albums.append({
            "id": str(album.get("id") or ""),
            "title": str(album.get("title") or "Альбом"),
            "artist": ", ".join(str(a.get("name") or "") for a in (album.get("artists") or [])),
            "cover": cover(album.get("coverUri")),
        })
    playlists = []
    for item in ((data.get("playlists") or {}).get("results") or [])[:12]:
        if item.get("kind") is None or not item.get("uid"):
            continue
        playlists.append({
            "id": "%s:%s" % (item["uid"], item["kind"]),
            "title": str(item.get("title") or "Плейлист"),
            "cover": cover((item.get("cover") or {}).get("uri")),
            "tracks": item.get("trackCount") or 0,
        })
    artists = []
    for item in ((data.get("artists") or {}).get("results") or [])[:8]:
        artists.append({
            "id": str(item.get("id") or ""),
            "title": str(item.get("name") or "Артист"),
            "cover": cover((item.get("cover") or {}).get("uri")),
        })
    podcasts = []
    for item in ((data.get("podcasts") or {}).get("results") or [])[:8]:
        podcasts.append({
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or "Подкаст"),
            "cover": cover(item.get("coverUri")),
            "description": str(item.get("description") or ""),
        })
    return {
        "tracks": [short_track(t) for t in ((data.get("tracks") or {}).get("results") or [])[:20]],
        "albums": albums,
        "playlists": playlists,
        "artists": artists,
        "podcasts": podcasts,
        "books": books_search(text),
    }


def podcasts() -> list:
    data = api("/landing3", {"blocks": "podcasts"}, optional=True) or {}
    out = []
    for block in (data.get("blocks") or []):
        if block.get("type") != "podcasts":
            continue
        for entity in (block.get("entities") or []):
            podcast = ((entity.get("data") or {}).get("podcast") or {})
            if podcast.get("id"):
                out.append({
                    "id": str(podcast["id"]),
                    "title": str(podcast.get("title") or "Подкаст"),
                    "cover": cover(podcast.get("coverUri")),
                    "description": str(podcast.get("description") or entity.get("description") or ""),
                })
        break
    return out


def wave_data() -> dict:
    auth_token = token()
    station = "user:onyourwave"
    settings = api("/rotor/station/%s/settings" % station,
                   token_value=auth_token, optional=True) or {}
    info = api("/rotor/station/%s/info" % station,
               token_value=auth_token, optional=True) or []
    restrictions = ((info[0] if info else {}).get("station") or {}).get("restrictions") or {}
    return {"settings": settings, "restrictions": restrictions}


def trend_tracks() -> list:
    data = api("/landing3", {"blocks": "chart"}, optional=True) or {}
    out = []
    for block in (data.get("blocks") or []):
        if block.get("type") != "chart":
            continue
        for entity in (block.get("entities") or []):
            track = ((entity.get("data") or {}).get("track") or {})
            if track.get("id"):
                out.append(short_track(track))
        break
    return out[:60]


def playlists() -> list:
    auth_token = token()
    account = api("/account/status", token_value=auth_token) or {}
    uid = ((account.get("account") or {}).get("uid"))
    out = []
    seen = set()
    landing = api("/landing3", {"blocks": "personalPlaylists"}, auth_token) or {}
    for block in landing.get("blocks") or []:
        if block.get("type") != "personal-playlists":
            continue
        for entity in block.get("entities") or []:
            item = ((entity.get("data") or {}).get("data") or {})
            if item.get("uid") is None or item.get("kind") is None:
                continue
            key = "%s:%s" % (item["uid"], item["kind"])
            if key in seen:
                continue
            seen.add(key)
            cover_data = item.get("cover")
            out.append({
                "id": key,
                "title": str(item.get("title") or "Плейлист"),
                "cover": cover(cover_data.get("uri")) if isinstance(cover_data, dict) else None,
                "tracks": item.get("trackCount") or 0,
            })
    if uid:
        for item in (api("/users/%s/playlists" % uid, {"kinds": ""},
                         token_value=auth_token, optional=True) or []):
            if item.get("kind") is None:
                continue
            key = "%s:%s" % (uid, item["kind"])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "id": key,
                "title": str(item.get("title") or "Плейлист"),
                "cover": cover((item.get("cover") or {}).get("uri")),
                "tracks": item.get("trackCount") or 0,
            })
    return out[:30]


def main() -> None:
    data = common.params()
    if data.get("wave"):
        common.ok(**wave_data())
        return
    if data.get("trends"):
        common.ok(tracks=trend_tracks())
        return
    if data.get("wave_play"):
        settings = {
            "mood": max(1, min(4, common.to_int(data.get("mood"), 2, 1, 4))),
            "energy": max(1, min(4, common.to_int(data.get("energy"), 2, 1, 4))),
            "language": str(data.get("language") or "any")[:20],
            "diversity": str(data.get("diversity") or "default")[:20],
        }
        try:
            import glagol  # noqa: PLC0415
            glagol.send({
                "command": "playMusic", "type": "wave",
                "id": "user:onyourwave", "settings": settings,
            }, timeout=15)
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Не удалось включить волну: %s" % exc)
            return
        common.ok(playing="Моя волна", settings=settings)
        return
    if data.get("my_playlists"):
        common.ok(playlists=my_playlists())
        return
    if data.get("search"):
        common.ok(**search_all(str(data.get("search") or "")))
        return
    if data.get("album"):
        common.ok(tracks=album_tracks(str(data.get("album") or "")))
        return
    if data.get("podcasts"):
        common.ok(podcasts=podcasts())
        return
    if data.get("books"):
        common.ok(books=books_popular())
        return
    if data.get("playlist"):
        common.ok(tracks=playlist_tracks(str(data.get("playlist") or "")))
        return
    if data.get("liked"):
        common.ok(tracks=liked_tracks())
        return
    if data.get("play"):
        title = str(data.get("play") or "").strip()[:160]
        item_id = str(data.get("id") or "").strip()[:120]
        item_type = "track" if str(data.get("type") or "") == "track" else "playlist"
        if not title:
            common.fail(400, "Пустое название")
            return
        try:
            if item_id:
                import glagol  # noqa: PLC0415
                glagol.send({"command": "playMusic", "id": item_id, "type": item_type}, timeout=15)
            else:
                musicapi.play_phrase("плейлист " + title)
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Не удалось включить: %s" % exc)
            return
        common.ok(playing=title)
        return
    common.ok(playlists=playlists())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        common.fail(500, "Яндекс Музыка: %s" % exc)
