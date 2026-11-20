#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Что сейчас играет: трек, исполнитель, обложка.

    GET /cgi-bin/player.py

Состояние берётся из локального glagol (`softwareVersion` возвращает
полный `state`), поэтому ничего не опрашивает железо напрямую.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

try:
    import glagol  # noqa: E402
except Exception:  # noqa: BLE001
    glagol = None


def cover_url(extra):
    cover = (extra or {}).get("coverURI")
    if not cover:
        return None
    if "://" not in cover:
        cover = "https://" + cover
    return cover.replace("%%", "400x400")


def snapshot():
    """Снимок плеера. Сначала — state от постоянного watcher (пуш от Glagol),
    иначе один запрос softwareVersion (CGI без демона)."""
    if glagol is None:
        raise RuntimeError("Модуль glagol недоступен")
    state = None
    age = 0.0
    latest = getattr(glagol, "latest", None)
    if callable(latest):
        state = latest(max_age=15)
        if isinstance(state, dict):
            updated = float((glagol.watch_status() or {}).get("updated") or 0)
            if updated:
                age = max(0.0, time.time() - updated)
    if not isinstance(state, dict):
        reply = glagol.send({"command": "softwareVersion"}, timeout=6)
        state = (reply or {}).get("state") or {}
    player = state.get("playerState") or {}
    extra = player.get("extra") or {}
    return {
        "playing": bool(state.get("playing")),
        "alice": state.get("aliceState"),
        "volume": state.get("volume"),
        "title": player.get("title"),
        "artist": player.get("subtitle"),
        "cover": cover_url(extra),
        "duration": player.get("duration"),
        "progress": player.get("progress"),
        "kind": player.get("type"),
        "age": round(age, 2),
    }


def main():
    if glagol is None:
        common.fail(501, "Модуль glagol недоступен")
        return

    try:
        data = snapshot()
    except Exception as exc:  # noqa: BLE001
        common.fail(502, "glagol: %s" % exc, hint="проверьте /data/mypult/glagol.json")
        return

    common.ok(**data)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка player.py: %s" % exc)
        except Exception:
            pass
