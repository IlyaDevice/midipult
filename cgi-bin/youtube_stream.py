#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Локальный MP3-поток YouTube Music для штатного плеера станции."""

from __future__ import annotations

import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MEDIA_DIR = "/data/mypult/media"
STATE_PATH = "/data/mypult/youtube_state.json"
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _headers(status, length, content_range=None):
    lines = [
        "Status: %d %s" % (status, "Partial Content" if status == 206 else "OK"),
        "Content-Type: audio/mpeg",
        "Accept-Ranges: bytes",
        "Cache-Control: no-store, max-age=0",
        "X-Content-Type-Options: nosniff",
        "Content-Length: " + str(length),
    ]
    if content_range:
        lines.append("Content-Range: " + content_range)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def main():
    common.require_auth()
    qs = {k: v[-1] for k, v in urllib.parse.parse_qs(
        os.environ.get("QUERY_STRING") or "", keep_blank_values=True).items()}
    state = common.read_json(STATE_PATH, {}) or {}
    if str(qs.get("id") or "") != str(state.get("id") or ""):
        common.fail(404, "Поток не найден")
        return
    path = os.path.realpath(str(state.get("file") or ""))
    if not path.startswith(MEDIA_DIR + os.sep) or not os.path.isfile(path):
        common.fail(404, "Файл потока не найден")
        return
    size = os.path.getsize(path)
    if size <= 0:
        common.fail(404, "Пустой файл потока")
        return

    start = 0
    end = size - 1
    status = 200
    rng = os.environ.get("HTTP_RANGE") or ""
    match = RANGE_RE.match(rng.strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), size - 1)
        else:
            suffix = int(match.group(2) or "0")
            start = max(0, size - suffix)
            end = size - 1
        if start > end or start >= size:
            common.fail(416, "Некорректный Range")
            return
        status = 206

    length = end - start + 1
    content_range = "bytes %d-%d/%d" % (start, end, size) if status == 206 else None
    out = sys.stdout.buffer
    out.write(_headers(status, length, content_range))
    with open(path, "rb") as fh:
        fh.seek(start)
        left = length
        try:
            while left > 0:
                chunk = fh.read(min(128 * 1024, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
            out.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка youtube_stream.py: %s" % exc)
        except Exception:
            pass
