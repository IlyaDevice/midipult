#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Экспериментальный SoundCloud: поиск и поток через yt-dlp."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import radio  # noqa: E402

YTDLP = "/data/mypult/bin/yt-dlp"
STATE_PATH = "/data/mypult/soundcloud_state.json"
SELF_URL = "http://127.0.0.1:8055/cgi-bin/soundcloud_stream.py"
ALLOWED_HOSTS = ("soundcloud.com", "api.soundcloud.com", "m.soundcloud.com")


def _run(args, timeout=45):
    if not os.path.isfile(YTDLP):
        raise RuntimeError("yt-dlp не установлен")
    cmd = [sys.executable, YTDLP, "--no-warnings"] + args
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError("yt-dlp: " + (msg[-1] if msg else "ошибка"))
    return proc.stdout


def _duration(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _search(query, limit=12):
    raw = _run(["--flat-playlist", "--dump-single-json",
                "scsearch%d:%s" % (max(1, min(limit, 25)), query)])
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError("yt-dlp вернул не JSON")
    tracks = []
    for item in data.get("entries") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            continue
        if item.get("ie_key") != "Soundcloud":
            continue
        page = str(item.get("webpage_url") or item.get("url") or "")
        if not page.startswith("http"):
            page = "https://api.soundcloud.com/tracks/" + str(item["id"])
        thumbs = item.get("thumbnails") or []
        cover = ""
        for thumb in thumbs:
            if thumb.get("id") in ("t300x300", "crop", "large"):
                cover = str(thumb.get("url") or "")
        if not cover and thumbs:
            cover = str(thumbs[-1].get("url") or "")
        tracks.append({
            "id": str(item["id"]),
            "title": str(item.get("title") or ""),
            "artist": str(item.get("uploader") or ""),
            "duration": _duration(item.get("duration")),
            "cover": cover,
            "url": page,
        })
    if not tracks:
        raise RuntimeError("SoundCloud ничего не нашёл")
    return tracks[:limit]


def _state():
    return common.read_json(STATE_PATH, {}) or {}


def _save_state(data):
    common.write_json(STATE_PATH, data, mode=0o600)


def _check_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        raise RuntimeError("Некорректная ссылка")
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
        raise RuntimeError("Ссылка не SoundCloud")
    return parsed.geturl()


def _stream(page_url):
    page_url = _check_url(page_url)
    raw = _run(["--get-url", "-f", "bestaudio/best", page_url], timeout=60)
    stream_url = next((line.strip() for line in raw.splitlines() if line.startswith("http")), "")
    if not stream_url:
        raise RuntimeError("yt-dlp не вернул поток")
    return stream_url


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()

    if action == "status":
        common.ok(has_ytdlp=os.path.isfile(YTDLP), track=_state())
        return

    if action == "search":
        query = str(data.get("q") or "").strip()
        if not query:
            raise RuntimeError("Пустой поисковый запрос")
        common.ok(tracks=_search(query, common.to_int(data.get("limit"), 12, 1, 25)))
        return

    if action == "play":
        page_url = _check_url(data.get("url"))
        title = str(data.get("title") or "SoundCloud")
        artist = str(data.get("artist") or "")
        stream_url = _stream(page_url)
        track_id = str(data.get("id") or "")
        local = "%s?id=%s" % (SELF_URL, urllib.parse.quote(track_id))
        state = {
            "id": track_id, "title": title, "artist": artist,
            "cover": str(data.get("cover") or ""), "url": stream_url,
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
