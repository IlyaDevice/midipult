#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Текущее состояние LED-кольца станции из sysfs.

Нужно, чтобы панель показывала штатные анимации прошивки (например, музыку),
которые не идут через CGI, а пишутся напрямую в LED-контроллеры.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LED_ROOT = "/sys/class/leds"
COLORS = ("red", "green", "blue")


def channel(name: str) -> int:
    try:
        with open(os.path.join(LED_ROOT, name, "brightness"), "r", encoding="ascii") as fh:
            return max(0, min(255, int(fh.read().strip() or "0")))
    except (OSError, ValueError):
        return 0


def main() -> None:
    colors = []
    for index in range(24):
        values = [channel("cucumber:%s:ring%d" % (color, index)) for color in COLORS]
        colors.append("%02X%02X%02X" % tuple(values))
    common.ok(colors=colors)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        common.fail(500, "Не удалось прочитать кольцо: %s" % exc)
