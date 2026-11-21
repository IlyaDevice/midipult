#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Экспериментальный YouTube Music: поиск и поток через yt-dlp."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import radio  # noqa: E402

DATA_DIR = "/data/mypult"
YTDLP = os.path.join(DATA_DIR, "bin", "yt-dlp")
STATE_PATH = os.path.join(DATA_DIR, "youtube_state.json")
SELF_URL = "http://127.0.0.1:8055/cgi-bin/youtube_stream.py"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


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


def _search(query, limit=12):
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(query)
    raw = _run(["--flat-playlist", "--dump-single-json", url])

    def _collect(node, out):
        if isinstance(node, dict):
            if node.get("_type") in (None, "url") and node.get("id") and node.get("title"):
                entry_id = str(node.get("id"))
                if ID_RE.fullmatch(entry_id) and not any(x["id"] == entry_id for x in out):
                    out.append({
                        "id": entry_id,
                        "title": str(node.get("title") or ""),
                        "artist": str(node.get("channel") or node.get("uploader") or ""),
                        "duration": common.to_int(node.get("duration"), 0),
                        "cover": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % entry_id,
                        "url": "https://music.youtube.com/watch?v=" + entry_id,
                    })
            for value in node.values():
                _collect(value, out)
        elif isinstance(node, list):
            for value in node:
                _collect(value, out)

    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise RuntimeError("yt-dlp вернул не JSON")
    tracks = []
    _collect(data, tracks)
    if not tracks:
        raise RuntimeError("YouTube Music ничего не нашёл")
    return tracks[:limit]


def _stream(track_id):
    if not ID_RE.fullmatch(track_id):
        raise RuntimeError("Некорректный id видео")
    url = "https://music.youtube.com/watch?v=" + track_id
    raw = _run(["--get-url", "-f", "bestaudio[ext=m4a]/bestaudio", url], timeout=60)
    stream_url = next((line.strip() for line in raw.splitlines() if line.startswith("http")), "")
    if not stream_url:
        raise RuntimeError("yt-dlp не вернул поток")
    fmt = "AAC" if ".m4a" in stream_url or "mime=audio%2Fmp4" in stream_url else "OGG"
    return stream_url, fmt


def _state():
    return common.read_json(STATE_PATH, {}) or {}


def _save_state(data):
    common.write_json(STATE_PATH, data, mode=0o600)


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
        tracks = _search(query, common.to_int(data.get("limit"), 12, 1, 25))
        common.ok(tracks=tracks)
        return

    if action == "play":
        track_id = str(data.get("id") or "").strip()
        title = str(data.get("title") or "YouTube Music")
        artist = str(data.get("artist") or "")
        stream_url, fmt = _stream(track_id)
        local = "%s?id=%s" % (SELF_URL, track_id)
        state = {
            "id": track_id, "title": title, "artist": artist,
            "cover": str(data.get("cover") or ""), "url": stream_url,
            "fmt": fmt, "stream": local,
        }
        _save_state(state)
        try:
            radio.play_stream((title + (" — " + artist if artist else "")).strip(),
                              local, fmt=fmt, subtitle=artist)
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
