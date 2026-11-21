#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Возможности этой станции для панели.

    GET /cgi-bin/caps.py            — модель, дисплей, кольцо (кэш 5 минут)
    GET /cgi-bin/caps.py?refresh=1  — пересчитать заново
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import device  # noqa: E402


def main():
    data = common.params()
    refresh = common.truthy(data.get("refresh"))
    common.respond(200, device.caps(ttl=0 if refresh else 300))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка caps.py: %s" % exc)
        except Exception:
            pass
