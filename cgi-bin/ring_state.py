#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Текущее состояние LED-кольца станции.

На Миди (Cucumber) кольцо — 24 диода в sysfs, и панель показывает штатные
анимации прошивки, которые идут мимо CGI.

На Station 2 sysfs-светодиодов нет：кольцо — это строка 89 пикселей в
`/dev/fb0` (Gowin), которой владеет прошивка. Читать её можно (и панель так
делает), а писать нельзя: Luna отвечает 400 «framebuffer belongs to firmware»,
да и лезть в чужой кадр опасно. Поэтому отдаём цвета с `readonly: true`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LED_ROOT = "/sys/class/leds"
COLORS = ("red", "green", "blue")
FB = "/dev/fb0"
FB_VIRTUAL = "/sys/class/graphics/fb0/virtual_size"
FB_BPP = "/sys/class/graphics/fb0/bits_per_pixel"


def channel(name: str) -> int:
    try:
        with open(os.path.join(LED_ROOT, name, "brightness"), "r", encoding="ascii") as fh:
            return max(0, min(255, int(fh.read().strip() or "0")))
    except (OSError, ValueError):
        return 0


def parse_virtual_size(value):
    """'89,1' -> (89, 1); None, если формат не тот."""
    parts = str(value or "").replace(" ", "").split(",")
    if len(parts) != 2:
        return None
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def fb_geometry():
    """(число пикселей, байт на пиксель) для LED-панели Station 2, иначе None."""
    try:
        with open(FB_VIRTUAL, "r", encoding="ascii") as fh:
            size = parse_virtual_size(fh.read())
        with open(FB_BPP, "r", encoding="ascii") as fh:
            bpp = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if not size or bpp not in (24, 32) or not os.path.exists(FB):
        return None
    width, height = size
    return width * height, bpp // 8


def fb_colors(count, step):
    """Прочитать кадр панели. Порядок каналов у Station 2 — R,G,B,W."""
    with open(FB, "rb", buffering=0) as fh:
        data = fh.read(count * step)
    colors = []
    for index in range(count):
        chunk = data[index * step:(index + 1) * step]
        if len(chunk) < 3:
            break
        colors.append("%02X%02X%02X" % (chunk[0], chunk[1], chunk[2]))
    return colors


def sysfs_colors():
    colors = []
    for index in range(24):
        values = [channel("cucumber:%s:ring%d" % (color, index)) for color in COLORS]
        colors.append("%02X%02X%02X" % tuple(values))
    return colors


def sysfs_present():
    """Есть ли кольцо в sysfs (у Миди — `cucumber:red:ringN` и т.д.).

    У Миди тоже есть `/dev/fb0` с именем `ring` (24×1), но штатный и точный
    путь там — sysfs, поэтому framebuffer используем только когда sysfs пуст.
    """
    try:
        for name in os.listdir(LED_ROOT):
            if ":ring" in name:
                return True
    except OSError:
        return False
    return False


def main() -> None:
    if not sysfs_present():
        geometry = fb_geometry()
        if geometry:
            count, step = geometry
            try:
                colors = fb_colors(count, step)
            except OSError as exc:
                common.fail(500, "fb0 недоступен: %s" % exc)
                return
            common.ok(colors=colors, via="framebuffer", count=count,
                      supported=True, readonly=True,
                      note="панель читает кадр, но не управляет им: им владеет прошивка")
            return

    colors = sysfs_colors()
    common.ok(colors=colors, via="sysfs", count=len(colors),
              supported=True, readonly=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        common.fail(500, "Не удалось прочитать кольцо: %s" % exc)
