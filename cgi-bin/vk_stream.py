#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Прокси потока VK Музыки для штатного плеера станции."""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

STATE_PATH = "/data/mypult/vk_state.json"
UA = ("Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")


def main():
    common.require_auth()
    qs = {key: values[-1] for key, values in urllib.parse.parse_qs(
        os.environ.get("QUERY_STRING") or "", keep_blank_values=True).items()}
    state = common.read_json(STATE_PATH, {}) or {}
    if not state.get("url") or str(qs.get("id") or "") != str(state.get("id") or ""):
        common.fail(404, "Поток не найден")
        return
    headers = {"User-Agent": UA, "Referer": "https://vk.com/"}
    rng = os.environ.get("HTTP_RANGE") or ""
    if rng:
        headers["Range"] = rng
    try:
        upstream = urllib.request.urlopen(
            urllib.request.Request(state["url"], headers=headers), timeout=25)
    except Exception as exc:  # noqa: BLE001
        common.fail(502, "VK: %s" % exc)
        return
    status = getattr(upstream, "status", 200)
    length = upstream.headers.get("Content-Length")
    ctype = upstream.headers.get("Content-Type") or "audio/mpeg"
    out_headers = [
        "Status: %d %s" % (status, "Partial Content" if status == 206 else "OK"),
        "Content-Type: " + ctype,
        "Accept-Ranges: bytes",
        "Cache-Control: no-store, max-age=0",
        "X-Content-Type-Options: nosniff",
    ]
    if length:
        out_headers.append("Content-Length: " + str(length))
    content_range = upstream.headers.get("Content-Range")
    if content_range:
        out_headers.append("Content-Range: " + content_range)
    out = sys.stdout.buffer
    out.write(("\r\n".join(out_headers) + "\r\n\r\n").encode("latin-1"))
    try:
        while True:
            chunk = upstream.read(128 * 1024)
            if not chunk:
                break
            out.write(chunk)
        out.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            upstream.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка vk_stream.py: %s" % exc)
        except Exception:
            pass
