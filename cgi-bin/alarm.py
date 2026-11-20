#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Будильники пульта: список и команды планировщику (service/alarmd.py).

    GET  /cgi-bin/alarm.py                 — {"alarms": [...], "state": {...}}
    POST {"add": {...}}                    — добавить
    POST {"update": {...}}                 — изменить (нужен id)
    POST {"delete": "id"}                  — удалить
    POST {"toggle": "id", "enabled": 0|1}  — включить/выключить
    POST {"test": "id"}                    — проверить сейчас
    POST {"dismiss": 1} / {"snooze": 1}    — выключить / отложить
"""

from __future__ import annotations

import os
import re
import sys
import uuid

try:
    from zoneinfo import ZoneInfo
except ImportError:  # очень старый Python
    ZoneInfo = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DATA = "/data/mypult/alarms.json"
STATE = "/tmp/mypult-alarm-state.json"
CMD = "/tmp/mypult-alarm-cmd.json"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MODES = ("radio", "music", "query")


def load_data():
    data = common.read_json(DATA, {"alarms": []}) or {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("alarms"), list):
        data["alarms"] = []
    if not isinstance(data.get("timezone"), str):
        data["timezone"] = ""
    return data


def load():
    return load_data()["alarms"]


def save(alarms):
    data = load_data()
    data["alarms"] = alarms
    common.write_json(DATA, data)


def timezone_name(value):
    name = str(value or "").strip()
    if not name:
        return ""
    if ZoneInfo is None:
        raise ValueError("на станции нет zoneinfo")
    try:
        ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("неизвестный часовой пояс: %s" % name) from exc
    return name


def _time(value):
    text = str(value or "").strip()
    if not TIME_RE.match(text):
        raise ValueError("время в формате ЧЧ:ММ")
    return text


def _days(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("дни — список")
    days = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7 and day not in days:
            days.append(day)
    return sorted(days)


def _bounds(data, current=None):
    current = current or {}
    volume = common.to_int(data.get("volume", current.get("volume", 40)),
                           default=40, lo=0, hi=100)
    ramp = common.to_int(data.get("ramp", current.get("ramp", 30)),
                         default=30, lo=0, hi=600)
    stop_after = common.to_int(data.get("stop_after", current.get("stop_after", 10)),
                               default=10, lo=0, hi=180)
    snooze = common.to_int(data.get("snooze", current.get("snooze", 5)),
                           default=5, lo=1, hi=60)
    return volume, ramp, stop_after, snooze


def build(data, current=None):
    current = current or {}
    mode = str(data.get("mode", current.get("mode", "radio")) or "radio")
    if mode not in MODES:
        mode = "radio"
    volume, ramp, stop_after, snooze = _bounds(data, current)
    days = _days(data.get("days", current.get("days", [])))
    if data.get("once") is None and current.get("once") is None:
        once = not bool(days)
    else:
        once = common.truthy(data.get("once", current.get("once", False)))
    if once:
        days = []
    elif not days:
        raise ValueError("выберите дни недели или «Один раз»")
    item = {
        "id": current.get("id") or uuid.uuid4().hex[:8],
        "enabled": bool(current.get("enabled", True))
        if data.get("enabled") is None else common.truthy(data.get("enabled")),
        "time": _time(data.get("time", current.get("time", "07:00"))),
        "days": days,
        "once": once,
        "mode": mode,
        "text": str(data.get("text", current.get("text", "")) or "").strip()[:200],
        "label": str(data.get("label", current.get("label", "")) or "").strip()[:60],
        "volume": volume,
        "ramp": ramp,
        "stop_after": stop_after,
        "snooze": snooze,
    }
    if not item["text"]:
        item["text"] = ("Европа Плюс" if mode == "radio"
                        else "мою волну" if mode == "music" else "доброе утро")
    return item


def command(payload):
    common.write_json(CMD, payload)


def main():
    data = common.params()

    if data.get("timezone") is not None:
        try:
            name = timezone_name(data.get("timezone"))
        except ValueError as exc:
            common.fail(400, str(exc))
            return
        stored = load_data()
        stored["timezone"] = name
        common.write_json(DATA, stored)
        common.ok(alarms=load(), timezone=name)
        return

    if data.get("add") is not None:
        item = build(data.get("add") or {})
        alarms = load()
        alarms.append(item)
        save(alarms)
        common.ok(alarms=alarms)
        return

    if data.get("update") is not None:
        raw = data.get("update") or {}
        alarms = load()
        for index, current in enumerate(alarms):
            if current.get("id") == raw.get("id"):
                alarms[index] = build(raw, current)
                save(alarms)
                common.ok(alarms=alarms)
                return
        common.fail(404, "Будильник не найден")
        return

    if data.get("delete") is not None:
        target = str(data.get("delete") or "")
        save([item for item in load() if item.get("id") != target])
        common.ok(alarms=load())
        return

    if data.get("toggle") is not None:
        target = str(data.get("toggle") or "")
        enabled = common.truthy(data.get("enabled"))
        alarms = load()
        for item in alarms:
            if item.get("id") == target:
                item["enabled"] = enabled
        save(alarms)
        common.ok(alarms=alarms)
        return

    if data.get("test") is not None:
        command({"cmd": "test", "id": str(data.get("test") or "")})
        common.ok(queued=True)
        return

    if common.truthy(data.get("dismiss")):
        command({"cmd": "dismiss"})
        common.ok(queued=True)
        return

    if common.truthy(data.get("snooze")):
        command({"cmd": "snooze"})
        common.ok(queued=True)
        return

    common.ok(alarms=load(), state=common.read_json(STATE, {"ringing": False}) or {},
              timezone=load_data().get("timezone") or "")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Будильник: %s" % exc)
        except Exception:
            pass
