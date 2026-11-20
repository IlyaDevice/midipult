#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""История метрик для графиков.

    GET /cgi-bin/history.py — последние ~24 часа (CPU, температура, память,
    диск, load average) из /data/mypult/history.jsonl.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DATA = "/data/mypult/history.jsonl"


def load():
    points = []
    try:
        with open(DATA, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("t"):
                    points.append(item)
    except OSError:
        return []
    # Для графика достаточно ~600 точек; крупные ряды прореживаем.
    if len(points) > 600:
        step = len(points) / 600.0
        points = [points[int(i * step)] for i in range(600)]
    return points


def main():
    points = load()
    common.respond(200, {
        "ok": True,
        "points": points,
        "count": len(points),
    })


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
