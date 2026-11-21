#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Пульт Bluetooth-источника: AVRCP через BlueZ.

Телефон подключается к станции как к обычной колонке (A2DP), а кнопки
и метаданные идут по AVRCP: станция здесь — контроллер, телефон — цель.
BlueZ выставляет плеер телефона в системный D-Bus как
`org.bluez.MediaPlayer1`, поэтому отсюда доступны play/pause, next/prev,
перемотка шагами (FastForward/Rewind) и текущий трек.

CGI:
    GET /cgi-bin/btplayer.py                     — состояние
    GET /cgi-bin/btplayer.py?action=status
    GET /cgi-bin/btplayer.py?action=play|pause|playpause|next|prev|ff|rw|stop
    GET /cgi-bin/btplayer.py?action=probe        — что вообще есть в системе

Ответ status:
    {"ok":true,"avrcp":true,"player":"/org/bluez/hci0/dev_XX/player0",
     "connected":[{"mac":"..","name":".."}],
     "status":"playing","position":12.3,"duration":210.0,
     "track":{"title":"..","artist":"..","album":".."}}

Если AVRCP в прошивке выключен, `avrcp` = false, а `connected` всё равно
показывает, что телефон подключён, — панель честно скажет, что управление
осталось на телефоне.
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DBUS = common.which("dbus-send") or "/usr/bin/dbus-send"
BLUETOOTHCTL = common.which("bluetoothctl") or "/usr/bin/bluetoothctl"
IFACE = "org.bluez.MediaPlayer1"
CONNECTED_PATH = "/data/mypult/bt_connected_cache.json"
CONNECTED_TTL = 15.0

METHODS = {
    "play": "Play",
    "pause": "Pause",
    "stop": "Stop",
    "next": "Next",
    "prev": "Previous",
    "ff": "FastForward",
    "rw": "Rewind",
}

RE_PATH = re.compile(r'object path "(/org/bluez/[^"]+/player\d*)"')
RE_KEY = re.compile(r'\s*string "((?:[^"\\]|\\.)*)"\s*$')
RE_VARIANT = re.compile(
    r'\s*variant\s+(string|uint32|uint64|int32|int64|boolean|object path)\s+(.*?)\s*$')


def _dbus(*args, timeout=8):
    return common.run([DBUS, "--system", "--print-reply", "--dest=org.bluez"] + list(args),
                      timeout=timeout)


def _unescape(text):
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _connected():
    """Подключённые Bluetooth-устройства: имя нужно и без AVRCP.

    `bluetoothctl` — отдельный процесс на сотни миллисекунд, поэтому список
    кэшируется на 10 с: состав устройств так часто не меняется.
    """
    cached = common.read_json(CONNECTED_PATH, {}) or {}
    if isinstance(cached.get("data"), list):
        age = int(time.time() * 1000) - common.to_int(cached.get("at"), 0, 0)
        if 0 <= age < CONNECTED_TTL * 1000:
            return cached["data"]
    devices = _connected_live()
    try:
        common.write_json(CONNECTED_PATH, {"at": int(time.time() * 1000), "data": devices},
                          mode=0o600)
    except OSError:
        pass
    return devices


def _connected_live():
    code, out, err = common.run([BLUETOOTHCTL, "devices", "Connected"], timeout=6)
    if code != 0:
        return []
    devices = []
    for line in (out or "").splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            devices.append({"mac": parts[1], "name": parts[2].strip()})
    return devices


def _player_path():
    """Путь D-Bus до плеера телефона (AVRCP), либо None."""
    code, out, err = _dbus("/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects",
                           timeout=10)
    if code != 0:
        return None, (err or out or "D-Bus недоступен").strip()[:200]
    paths = []
    for path in RE_PATH.findall(out or ""):
        if path not in paths:
            paths.append(path)
    if not paths:
        return None, "AVRCP-плеер не найден"
    return paths[0], ""


def _parse_variants(text):
    """Разобрать вывод dbus-send Properties.GetAll в плоский словарь.

    Словарь Track вложенный, но его ключи (Title/Artist/Album/Duration)
    попадают в общий словарь: на верхнем уровне таких имён нет.
    """
    data = {}
    lines = (text or "").splitlines()
    for index, line in enumerate(lines[:-1]):
        match = RE_KEY.match(line)
        if not match:
            continue
        var = RE_VARIANT.match(lines[index + 1])
        if not var:
            continue
        kind, raw = var.group(1), var.group(2)
        if kind == "string":
            quoted = re.match(r'"(.*)"$', raw)
            value = _unescape(quoted.group(1)) if quoted else raw
        elif kind == "object path":
            value = raw.strip('"')
        elif kind == "boolean":
            value = raw == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                value = raw
        data[match.group(1)] = value
    return data


def _props(path):
    code, out, err = _dbus(path, "org.freedesktop.DBus.Properties.GetAll",
                           "string:" + IFACE)
    if code != 0:
        return {}
    return _parse_variants(out)


def _status(fresh=False):
    """Состояние плеера телефона.

    Без файлового кэша: два вызова `dbus-send` дешевле записи на флешку
    каждые две секунды. Список подключённых устройств кэшируется отдельно и
    запрашивается только когда AVRCP-плеера нет.
    """
    path, error = _player_path()
    data = {
        "ok": True,
        "avrcp": bool(path),
        "player": path or "",
        "connected": [],
        "status": "",
        "position": 0.0,
        "duration": 0.0,
        "track": {"title": "", "artist": "", "album": ""},
    }
    if not path:
        data["hint"] = error
        data["connected"] = _connected()
        return data

    props = _props(path)
    data["status"] = str(props.get("Status") or "")
    # AVRCP отдаёт миллисекунды, панели удобнее секунды.
    data["position"] = round(common.to_int(props.get("Position"), 0, 0) / 1000.0, 1)
    data["duration"] = round(common.to_int(props.get("Duration"), 0, 0) / 1000.0, 1)
    data["track"] = {
        "title": str(props.get("Title") or ""),
        "artist": str(props.get("Artist") or ""),
        "album": str(props.get("Album") or ""),
    }
    if not props:
        data["hint"] = "BlueZ не отдал свойства плеера"
    return data


def _run_action(name):
    path, error = _player_path()
    if not path:
        raise RuntimeError(error or "AVRCP-плеер не найден")
    method = METHODS[name]
    code, out, err = _dbus(path, IFACE + "." + method)
    if code != 0:
        raise RuntimeError((err or out or "BlueZ не принял команду").strip()[:300])


def _probe():
    def exists(binary):
        return os.path.isfile(binary) or bool(common.which(binary))

    path, error = _player_path()
    code, out, _ = _dbus("/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects",
                         timeout=10)
    players = RE_PATH.findall(out or "")
    return {
        "ok": True,
        "dbus_send": exists(DBUS),
        "gdbus": exists("/usr/bin/gdbus") or bool(common.which("gdbus")),
        "busctl": bool(common.which("busctl")),
        "bluetoothctl": bool(common.which("bluetoothctl")),
        "player": path or "",
        "players": players[:5],
        "connected": _connected_live(),
        "dbus_code": code,
        "error": error,
        "raw": (out or "")[:1500],
    }


def main():
    data = common.params()
    action = str(data.get("action") or "status").strip().lower()

    if action == "status":
        common.ok(**_status())
        return

    if action == "probe":
        common.ok(**_probe())
        return

    if action == "playpause":
        current = _status(fresh=True)
        action = "pause" if current.get("status") == "playing" else "play"

    if action in METHODS:
        _run_action(action)
        common.ok(**_status(fresh=True))
        return

    raise RuntimeError("Неизвестное действие")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(502, str(exc))
        except Exception:
            pass
