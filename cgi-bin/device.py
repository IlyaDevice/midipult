#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Какая это станция и что на ней вообще есть.

Модели:
    midi      — розничная «Яндекс Станция Миди» (Cucumber, Amlogic A5):
                сегментный дисплей `cucumber:white:clock-*` и 24 диода sysfs;
    station2  — розничная «Яндекс Станция 2» (внутри device-tree тоже «Midi»!):
                LED-панель на 88 диодов через Gowin и `/dev/fb0`, сегментов нет;
    unknown   — всё остальное.

Возможности нужны, чтобы панель не показывала карточки, которых на железе нет.
Определяем по Luna (`/api/device`) и sysfs, с запасным вариантом из
`/data/mypult/device.json`, который пишет установщик.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.request

LUNA = "http://127.0.0.1:8099"
CACHE = "/tmp/mypult-device.json"
STORE = "/data/mypult/device.json"
TTL = 300

MIDI_MODELS = ("yandexmidi",)
STATION2_MODELS = ("yandexstation_2", "yandexstation2")
RING_GLOB = "/sys/class/leds/*:ring*"
SEGMENT_GLOB = "/sys/class/leds/cucumber:white:clock-*"


def _json(path, timeout=3):
    try:
        with urllib.request.urlopen(LUNA + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def device_tree_model():
    try:
        with open("/proc/device-tree/model", "rb") as fh:
            return fh.read().replace(b"\x00", b"").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _count(pattern):
    try:
        return len(glob.glob(pattern))
    except OSError:
        return 0


def _ring_diodes():
    """Сколько диодов в кольце: sysfs держит по три канала на диод."""
    indexes = set()
    try:
        for path in glob.glob(RING_GLOB):
            match = re.search(r":ring(\d+)$", os.path.basename(path))
            if match:
                indexes.add(int(match.group(1)))
    except OSError:
        return 0
    return len(indexes)


def _fb_pixels():
    """Ширина LED-панели Station 2 из /sys/class/graphics/fb0 (89 у Station 2)."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size", "r", encoding="ascii") as fh:
            width = int(fh.read().split(",")[0])
        return width if width > 0 and os.path.exists("/dev/fb0") else 0
    except (OSError, ValueError):
        return 0


BACKLIGHT = "/sys/class/backlight/fb0"


def _read_int(path):
    try:
        with open(path, "r", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def panel_backlight():
    """Подсветка всей LED-панели (Station 2): 0 — панель погашена.

    Диоды поштучно недоступны, зато backlight реально управляет панелью:
    прошивка не перебивает выставленное значение.
    """
    current = _read_int(os.path.join(BACKLIGHT, "brightness"))
    maximum = _read_int(os.path.join(BACKLIGHT, "max_brightness"))
    if current is None or not maximum:
        return None
    return {"current": current, "max": maximum, "off": current == 0}


def read_store():
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_store(data):
    try:
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)
    except OSError:
        pass


def classify(model, tree_model, features):
    """Вернуть kind по данным Luna и device-tree.

    Важно: у розничной Станции 2 в device-tree тоже написано «Yandex Station
    Midi» — поэтому сначала смотрим на Cucumber (это и есть Миди), а уже потом
    на «station-midi»/`yandexstation_2`.
    """
    model = str(model or "").strip().lower()
    tree = str(tree_model or "").strip().lower()
    if "cucumber" in tree or model in MIDI_MODELS:
        return "midi"
    if ("station-midi" in tree or "station midi" in tree
            or model in STATION2_MODELS or model.startswith("yandexstation")):
        return "station2"
    features = [str(item) for item in (features or [])]
    if any(item.startswith("display.clock.segment") for item in features):
        return "midi"
    return "unknown"


def detect():
    device = _json("/api/device")
    features = [str(item) for item in (device.get("features") or [])]
    model = str(device.get("model") or "").strip()
    tree = device_tree_model()
    kind = classify(model, tree, features)

    segments = _count(SEGMENT_GLOB)
    ring = _ring_diodes()
    fb_pixels = _fb_pixels()
    has_fb = os.path.exists("/dev/fb0")
    has_segment_feature = any(item.startswith("display.clock.segment") for item in features)

    display = bool(segments or has_segment_feature)
    support_ring = bool(ring)
    if support_ring:
        ring_kind = "sysfs"
    elif has_fb:
        ring_kind = "framebuffer"
    else:
        ring_kind = "none"

    note = ""
    if kind == "station2":
        note = ("Station 2: панель ведёт 88 диодов через Gowin/SPI, кадр /dev/fb0 "
                "не читается и диоды поштучно недоступны — но панель целиком можно "
                "приглушить или погасить через подсветку.")

    backlight = None
    if ring_kind == "framebuffer" and not support_ring:
        backlight = panel_backlight()

    caps = {
        "ok": True,
        "kind": kind,
        "model": model,
        "tree_model": tree,
        "display": display,
        "ring": support_ring,
        "ring_count": ring or fb_pixels,
        "ring_kind": ring_kind,
        "ring_read": bool(support_ring),
        "panel_backlight": backlight,
        "leds": {"segments": segments, "ring": ring, "fb_pixels": fb_pixels},
        "note": note,
        "detected_at": int(time.time()),
    }

    # Для следующего раза, когда Luna ещё не поднялась: kind из установщика.
    store = read_store()
    if store.get("kind") and not model:
        caps["kind"] = store["kind"]

    try:
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(caps, fh, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    return caps


def caps(ttl=TTL):
    try:
        with open(CACHE, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and time.time() - int(cached.get("detected_at") or 0) < ttl:
            return cached
    except (OSError, ValueError, TypeError):
        pass
    try:
        return detect()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "kind": "unknown", "model": "",
                "display": False, "ring": False, "ring_count": 0,
                "ring_kind": "none", "note": ""}


if __name__ == "__main__":
    print(json.dumps(caps(ttl=0), ensure_ascii=False, indent=2))
