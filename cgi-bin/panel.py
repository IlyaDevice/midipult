#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Общая яркость LED-панели (Station 2, у которой нет sysfs-диодов).

    GET  /cgi-bin/panel.py                       — состояние
    POST /cgi-bin/panel.py {"brightness": 0..255}
    POST /cgi-bin/panel.py {"off": 1}            — погасить (запомнить яркость)
    POST /cgi-bin/panel.py {"restore": 1}        — вернуть запомненную

Панелью Station 2 владеет прошивка: кадр `/dev/fb0` не читается, диоды
поштучно недоступны, но `/sys/class/backlight/fb0` реально гасит и возвращает
всю панель. На Миди кольцом управляет `display.py` через sysfs, поэтому здесь
`supported: false` — чтобы не было двух конкурирующих регуляторов.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

BACKLIGHT = "/sys/class/backlight/fb0"
LED_ROOT = "/sys/class/leds"
STORE = "/data/mypult/panel.json"
DEFAULT = 83


def _read_int(path):
    try:
        with open(path, "r", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def has_sysfs_ring():
    try:
        return any(":ring" in name for name in os.listdir(LED_ROOT))
    except OSError:
        return False


def support():
    """Только там, где кольцо не управляется по диодам (Station 2)."""
    return bool(_read_int(BACKLIGHT + "/max_brightness")) and not has_sysfs_ring()


def clamp(value, maximum):
    """Яркость в 0…maximum; None, если это не число."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(maximum, number))


def load_saved():
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = int(data.get("restore"))
        return value if value > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def save_saved(value):
    common.write_json(STORE, {"restore": int(value)})


def state():
    maximum = _read_int(BACKLIGHT + "/max_brightness") or 0
    current = _read_int(BACKLIGHT + "/brightness")
    return {
        "ok": True,
        "supported": support(),
        "current": current,
        "max": maximum,
        "off": current == 0,
        "restore": load_saved() or DEFAULT,
    }


def write_brightness(value):
    with open(BACKLIGHT + "/brightness", "w", encoding="ascii") as fh:
        fh.write(str(int(value)))


def main():
    data = common.params()

    if not support():
        common.ok(supported=False,
                  reason="на этой модели панелью управляет другое (sysfs-кольцо)")
        return

    if data.get("brightness") is not None:
        maximum = _read_int(BACKLIGHT + "/max_brightness") or 255
        value = clamp(data.get("brightness"), maximum)
        if value is None:
            common.fail(400, "яркость: нужно число 0…%d" % maximum)
            return
        if value > 0:
            save_saved(value)
        write_brightness(value)
        common.ok(**state())
        return

    if common.truthy(data.get("off")):
        current = _read_int(BACKLIGHT + "/brightness") or 0
        if current > 0:
            save_saved(current)
        write_brightness(0)
        common.ok(**state())
        return

    if common.truthy(data.get("restore")):
        maximum = _read_int(BACKLIGHT + "/max_brightness") or 255
        write_brightness(min(load_saved() or DEFAULT, maximum))
        common.ok(**state())
        return

    common.ok(**state())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка panel.py: %s" % exc)
        except Exception:
            pass
