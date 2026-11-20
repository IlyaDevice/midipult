#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Интернет-радио: штатный плеер станции и свои потоки по прямой ссылке.

    GET  /cgi-bin/radio.py                       — станции, свои каналы, статус потока
    POST /cgi-bin/radio.py {"index":3}           — включить станцию
    POST /cgi-bin/radio.py {"stop":1}            — выключить (Glagol и свой поток)
    POST /cgi-bin/radio.py {"add":"Радио X"}     — добавить свой канал по названию
    POST /cgi-bin/radio.py {"add":"X","url":"http://…"} — свой поток по ссылке
    POST /cgi-bin/radio.py {"remove":"Радио X"}  — удалить свой канал

Встроенные станции играет Алиса по названию (Glagol `sendText`). Свои каналы
с URL пускаются напрямую: GStreamer из прошивки (`maind --gst-launch`) играет
поток в ALSA тем же маршрутом, что и «свои звуки». Это обход ограничения
«прямые URL не поддерживаются»: Luna `audio.play.url` на Миди требует backend
`aplay` и отдаёт 501, а HTTP(S)-поток через GStreamer работает (HTTP; HTTPS
в этой сборке не поднимается — нет рабочих сертификатов у souphttpsrc).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

try:
    import glagol  # noqa: E402
except Exception:  # noqa: BLE001
    glagol = None

CUSTOM_FILE = "/data/mypult/radio.json"
QUASAR = "/system/vendor/quasar"
MAIND = os.path.join(QUASAR, "maind")
STREAM_PID = "/data/mypult/stream.pid"
STREAM_LOG = "/data/mypult/stream.log"
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,40}$")
URL_RE = re.compile(r"^https?://[^\s\"'!]+$")

# (id, подпись в пульте, фраза для Алисы)
BUILTIN = [
    ("europa", "Европа Плюс", "Европа Плюс"),
    ("record", "Радио Рекорд", "Радио Рекорд"),
    ("dfm", "DFM", "DFM"),
    ("russian", "Русское радио", "Русское радио"),
    ("mayak", "Маяк", "Маяк"),
    ("retro", "Ретро FM", "Ретро FM"),
    ("avtoradio", "Авторадио", "Авторадио"),
    ("humor", "Юмор FM", "Юмор FM"),
    ("love", "Love Radio", "Love Radio"),
    ("dacha", "Радио Дача", "Радио Дача"),
    ("nashe", "Наше радио", "Наше радио"),
    ("relax", "Relax FM", "Relax FM"),
    ("vesti", "Вести FM", "Вести FM"),
    ("dorozhnoe", "Дорожное радио", "Дорожное радио"),
    ("shanson", "Радио Шансон", "Радио Шансон"),
    ("radio_si", "Радио Си", "Радио Си"),
    ("silver", "Серебряный дождь", "Серебряный дождь"),
    ("business", "Business FM", "Business FM"),
    ("kids", "Детское радио", "Детское радио"),
    ("rock", "ROCK FM", "Rock FM"),
    ("militia", "Милицейская волна", "Милицейская волна"),
    ("vera", "Радио Вера", "Радио Вера"),
    ("zvezda", "Радио Звезда", "Радио Звезда"),
    ("comedy", "Comedy Radio", "Comedy Radio"),
    ("energy", "Радио ENERGY", "Радио ENERGY"),
    ("monte", "Монте-Карло", "Монте-Карло"),
    ("jazz", "Радио JAZZ", "Джаз FM"),
    ("culture", "Радио Культура", "Радио Культура"),
    ("orfeus", "Орфей", "Орфей"),
]


# --------------------------------------------------------------------------- #
#  Свои каналы
# --------------------------------------------------------------------------- #

def normalize_custom(items):
    """Старый формат (строки) и новый ({"name","url"}) → список словарей."""
    result = []
    for item in items or []:
        if isinstance(item, str):
            name, url = item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
        else:
            continue
        if not name:
            continue
        result.append({"name": name, "url": url})
    return result


def load_custom():
    try:
        with open(CUSTOM_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("stations") or []
        return normalize_custom(data)
    except (OSError, ValueError):
        return []


def save_custom(items):
    common.write_json(CUSTOM_FILE, {"stations": items}, indent=2)


def all_stations():
    result = [
        {"index": i, "id": item[0], "name": item[1], "query": item[2],
         "custom": False, "url": ""}
        for i, item in enumerate(BUILTIN)
    ]
    for item in load_custom():
        if any(entry["name"] == item["name"] for entry in result):
            continue
        result.append({
            "index": len(result), "id": "custom", "name": item["name"],
            "query": item["name"], "custom": True, "url": item["url"],
        })
    return result


def check_name(name):
    if not (2 <= len(name) <= 40) or not NAME_RE.match(name):
        raise ValueError("Название канала: 2–40 символов без управляющих символов")
    return name


def check_url(url):
    if not URL_RE.match(url):
        raise ValueError("Ссылка должна начинаться с http:// или https:// "
                         "и не содержать пробелов, кавычек и «!»")
    return url


# --------------------------------------------------------------------------- #
#  Свой поток через GStreamer
# --------------------------------------------------------------------------- #

def stream_pid():
    try:
        with open(STREAM_PID, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stream_status():
    pid = stream_pid()
    alive = pid_alive(pid)
    if pid and not alive:
        try:
            os.remove(STREAM_PID)
        except OSError:
            pass
    return {"playing": alive, "pid": pid if alive else None}


def stream_stop():
    pid = stream_pid()
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        os.remove(STREAM_PID)
    except OSError:
        pass
    return True


def log_tail(lines=6):
    try:
        with open(STREAM_LOG, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])[-1200:]
    except OSError:
        return ""


def stream_play(name, url):
    """Запустить поток. Возвращает pid; при неудаче — RuntimeError."""
    check_url(url)
    stream_stop()
    if not os.path.isfile(MAIND):
        raise RuntimeError("нет %s — прошивка без GStreamer" % MAIND)
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = QUASAR + ":libs"
    # URL проверен выше: без пробелов, кавычек и «!», поэтому в pipeline
    # он не может разорвать описание.
    pipeline = ("souphttpsrc location=%s ! decodebin ! audioconvert ! "
                "audioresample ! alsasink" % url)
    try:
        with open(STREAM_LOG, "ab") as log_file:
            proc = subprocess.Popen(
                [MAIND, "--gst-launch", pipeline],
                cwd=QUASAR, env=env,
                stdin=subprocess.DEVNULL, stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
    except OSError as exc:
        raise RuntimeError("не удалось запустить GStreamer: %s" % exc) from exc
    with open(STREAM_PID, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    time.sleep(2.0)
    if proc.poll() is not None:
        stream_stop()
        raise RuntimeError("поток не запустился. %s" % (log_tail(3) or "см. %s" % STREAM_LOG))
    return proc.pid


# --------------------------------------------------------------------------- #
#  Встроенные станции через Алису
# --------------------------------------------------------------------------- #

def state_brief():
    reply = glagol.send({"command": "softwareVersion"}, timeout=12)
    state = (reply or {}).get("state") or {}
    player = state.get("playerState") or {}
    return {
        "playing": bool(state.get("playing")),
        "alice": state.get("aliceState"),
        "title": player.get("title"),
        "kind": player.get("type"),
        "volume": state.get("volume"),
    }


def main():
    if glagol is None:
        common.fail(501, "Модуль glagol недоступен")
        return

    data = common.params()

    if common.truthy(data.get("stop")):
        stream_stop()
        try:
            glagol.send({"command": "stop"}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Не удалось остановить: %s" % exc)
            return
        common.ok(playing=False, stream=False)
        return

    if data.get("add") is not None:
        try:
            name = check_name(str(data.get("add") or "").strip())
            url = str(data.get("url") or "").strip()
            if url:
                check_url(url)
        except ValueError as exc:
            common.fail(400, str(exc))
            return
        custom = load_custom()
        custom = [item for item in custom if item["name"] != name]
        custom.append({"name": name, "url": url})
        save_custom(custom)
        common.ok(added=name, url=url or None, stations=all_stations())
        return

    if data.get("remove") is not None:
        name = str(data.get("remove") or "").strip()
        custom = [item for item in load_custom() if item["name"] != name]
        save_custom(custom)
        common.ok(removed=name, stations=all_stations())
        return

    if data.get("index") is not None:
        stations = all_stations()
        index = common.to_int(data.get("index"), 0, 0, len(stations) - 1)
        station = stations[index]
        if station.get("url"):
            try:
                pid = stream_play(station["name"], station["url"])
            except (ValueError, RuntimeError) as exc:
                common.fail(502, "Поток не играет: %s" % exc,
                            station={"index": index, "name": station["name"]},
                            log=log_tail(6))
                return
            common.ok(playing=True, stream=True, pid=pid,
                      station={"index": index, "name": station["name"]},
                      url=station["url"])
            return
        try:
            glagol.send({"command": "sendText",
                        "text": "включи радио " + station["query"]}, timeout=20)
            time.sleep(2.5)
            brief = state_brief()
        except Exception as exc:  # noqa: BLE001
            common.fail(502, "Радио не включилось: %s" % exc)
            return
        brief["stream"] = False
        brief["station"] = {"index": index, "name": station["name"]}
        common.respond(200, brief)
        return

    common.respond(200, {"ok": True, "stations": all_stations(),
                         "stream": stream_status(), "log": log_tail(3)})


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка radio.py: %s" % exc)
        except Exception:
            pass
