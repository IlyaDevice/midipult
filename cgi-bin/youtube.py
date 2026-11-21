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
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import radio  # noqa: E402

DATA_DIR = "/data/mypult"
YTDLP = os.path.join(DATA_DIR, "bin", "yt-dlp")
FFMPEG = "/opt/bin/ffmpeg"
MEDIA_DIR = os.path.join(DATA_DIR, "media")
STATE_PATH = os.path.join(DATA_DIR, "youtube_state.json")
SELF_URL = "http://127.0.0.1:8055/cgi-bin/youtube_stream.py"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHUNK = 1024 * 1024


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
    """Скачать аудио чанками и собрать локальный MP3.

    Штатный плеер станции не открывает AAC/M4A и не тянет длинный
    googlevideo-поток через CGI. Зато MP3 с локального прокси играет
    стабильно. Чанки по 1 МБ качаются быстро: большой поток одним
    соединением CDN заметно тормозит.
    """
    if not ID_RE.fullmatch(track_id):
        raise RuntimeError("Некорректный id видео")
    if not os.path.isfile(FFMPEG):
        raise RuntimeError("ffmpeg не установлен — нужен для MP3")
    os.makedirs(MEDIA_DIR, mode=0o755, exist_ok=True)
    mp3_path = os.path.join(MEDIA_DIR, "yt-%s.mp3" % track_id)
    if os.path.isfile(mp3_path) and os.path.getsize(mp3_path) > 1024:
        return mp3_path

    url = "https://music.youtube.com/watch?v=" + track_id
    raw = _run(["--get-url", "-f", "bestaudio[ext=m4a]/bestaudio", url], timeout=60)
    stream_url = next((line.strip() for line in raw.splitlines() if line.startswith("http")), "")
    if not stream_url:
        raise RuntimeError("yt-dlp не вернул поток")

    tmp_m4a = mp3_path + ".m4a.part"
    tmp_mp3 = mp3_path + ".tmp.mp3"
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
            "Referer": "https://music.youtube.com/",
            "Origin": "https://music.youtube.com",
        }
        offset = 0
        total = 0
        with open(tmp_m4a, "wb") as out:
            while True:
                req_headers = dict(headers)
                req_headers["Range"] = "bytes=%d-%d" % (offset, offset + CHUNK - 1)
                request = urllib.request.Request(stream_url, headers=req_headers)
                with urllib.request.urlopen(request, timeout=25) as response:
                    data = response.read()
                    status = getattr(response, "status", 200)
                    content_range = response.headers.get("Content-Range") or ""
                if not data:
                    break
                out.write(data)
                offset += len(data)
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = 0
                if status == 200 or (total and offset >= total):
                    break
        if offset < 1024:
            raise RuntimeError("не удалось скачать аудио YouTube")
        proc = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", tmp_m4a,
             "-vn", "-c:a", "libshine", "-b:a", "128k", tmp_mp3],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, text=True)
        if proc.returncode != 0 or not os.path.isfile(tmp_mp3):
            msg = (proc.stderr or "").strip().splitlines()
            raise RuntimeError("ffmpeg: " + (msg[-1] if msg else "не удалось собрать MP3"))
        os.replace(tmp_mp3, mp3_path)
        os.chmod(mp3_path, 0o644)
    finally:
        for path in (tmp_m4a, tmp_mp3):
            try:
                os.unlink(path)
            except OSError:
                pass
    _prune_media()
    return mp3_path


def _prune_media():
    """Держим последние 12 треков, чтобы кэш не рос бесконечно."""
    try:
        files = [os.path.join(MEDIA_DIR, name) for name in os.listdir(MEDIA_DIR)
                 if name.startswith("yt-") and name.endswith(".mp3")]
        files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        for path in files[12:]:
            os.unlink(path)
    except OSError:
        pass


def _state():
    return common.read_json(STATE_PATH, {}) or {}


def _save_state(data):
    common.write_json(STATE_PATH, data, mode=0o600)


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()

    if action == "status":
        common.ok(has_ytdlp=os.path.isfile(YTDLP), has_ffmpeg=os.path.isfile(FFMPEG),
                  track=_state())
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
        mp3_path = _stream(track_id)
        local = "%s?id=%s" % (SELF_URL, track_id)
        state = {
            "id": track_id, "title": title, "artist": artist,
            "cover": str(data.get("cover") or ""), "file": mp3_path,
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
