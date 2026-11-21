#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Отдать скачанный трек Сбер Звука штатному плееру станции."""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

CACHE_DIR = "/data/mypult/zvuk-cache"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def main():
    common.require_auth()
    query = common.params() if (os.environ.get("REQUEST_METHOD") or "GET").upper() == "POST" else {}
    # params() читает stdin; для GET берём query напрямую.
    if not query:
        import urllib.parse
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(
            os.environ.get("QUERY_STRING") or "", keep_blank_values=True).items()}
    track_id = str(query.get("id") or "").strip()
    if not ID_RE.fullmatch(track_id):
        common.fail(404, "Трек не найден")
        return
    path = os.path.join(CACHE_DIR, track_id + ".mp3")
    if not os.path.isfile(path):
        common.fail(404, "Трек не найден")
        return
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, "200 OK"
    rng = os.environ.get("HTTP_RANGE") or ""
    if rng.startswith("bytes="):
        part = rng[6:].split(",", 1)[0].strip()
        left, _, right = part.partition("-")
        try:
            if left:
                start = max(0, int(left))
                end = min(size - 1, int(right)) if right else size - 1
            elif right:
                start = max(0, size - int(right))
            if start > end or start >= size:
                raise ValueError
            status = "206 Partial Content"
        except ValueError:
            start, end, status = 0, size - 1, "200 OK"
    length = max(0, end - start + 1)
    headers = [
        "Status: " + status,
        "Content-Type: audio/mpeg",
        "Content-Length: " + str(length),
        "Accept-Ranges: bytes",
        "Cache-Control: no-store, max-age=0",
        "X-Content-Type-Options: nosniff",
    ]
    if status.startswith("206"):
        headers.append("Content-Range: bytes %d-%d/%d" % (start, end, size))
    out = sys.stdout.buffer
    out.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
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
            common.fail(500, "Внутренняя ошибка zvuk_stream.py: %s" % exc)
        except Exception:
            pass
