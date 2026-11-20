#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Фоновое проигрывание кадров анимации дисплея (для сценариев).

Запускается из display.py по действию сценария. Читает JSON с кадрами
(4 строки сегментов `abcdefg`) и шлёт их в Luna через /api/clock.
Останавливается сигналом SIGTERM.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

LUNA = "http://127.0.0.1:8099/api/clock"


def segments(frame):
    result = {}
    for digit in range(4):
        lit = str((frame or ['', '', '', ''])[digit] or '')
        for seg in 'abcdefg':
            result[seg.upper() + str(digit + 1)] = 255 if seg in lit else 0
    return result


def main():
    if len(sys.argv) < 2:
        return 1
    with open(sys.argv[1], 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    frames = data.get('frames') or []
    if not frames:
        return 1
    delay = max(0.05, min(5.0, float(data.get('duration') or 300) / 1000.0))
    while True:
        for frame in frames:
            body = json.dumps({'segments': segments(frame)}).encode('utf-8')
            req = urllib.request.Request(
                LUNA, data=body,
                headers={'Content-Type': 'application/json'},
            )
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
            time.sleep(delay)


if __name__ == '__main__':
    sys.exit(main())
