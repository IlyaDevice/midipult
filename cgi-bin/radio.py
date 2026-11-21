#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Интернет-радио: штатный плеер станции и свои потоки по прямой ссылке.

    GET  /cgi-bin/radio.py                       — станции и свои каналы
    POST /cgi-bin/radio.py {"index":3}           — включить станцию
    POST /cgi-bin/radio.py {"stop":1}            — выключить
    POST /cgi-bin/radio.py {"add":"Радио X"}     — добавить свой канал по названию
    POST /cgi-bin/radio.py {"add":"X","url":"http://…"} — свой поток по ссылке
    POST /cgi-bin/radio.py {"remove":"Радио X"}  — удалить свой канал

Встроенные станции играет Алиса по названию (Glagol `sendText`). Свой поток
отдаётся станции директивой Glagol `audio_play` (protobuf через
`externalCommandBypass`): играет **штатный плеер** колонки, поэтому Алиса,
громкость и пауза остаются штатными, а ALSA не занимается вторым процессом.
Так делает yapanel-web (dimap) — за идею спасибо. HTTPS-потоки штатный плеер
на этой прошивке не открывает, нужен http://.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

try:
    import glagol  # noqa: E402
except Exception:  # noqa: BLE001
    glagol = None

CUSTOM_FILE = "/data/mypult/radio.json"
NAME_RE = r"^[^\x00-\x1f]{1,40}$"
URL_RE = r"^https?://[^\s\"'!]+$"

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
    import re
    if not (2 <= len(name) <= 40) or not re.match(NAME_RE, name):
        raise ValueError("Название канала: 2–40 символов без управляющих символов")
    return name


def check_url(url):
    import re
    if not re.match(URL_RE, url):
        raise ValueError("Ссылка должна начинаться с http:// или https:// "
                         "и не содержать пробелов, кавычек и «!»")
    return url


# --------------------------------------------------------------------------- #
#  Свой поток через штатный плеер (Glagol audio_play)
# --------------------------------------------------------------------------- #

def _pb_string(out, field, value):
    """protobuf: поле-строка (wire type 2)."""
    out.append((field << 3) | 2)
    data = value.encode("utf-8")
    size = len(data)
    while size >= 0x80:
        out.append((size & 0x7F) | 0x80)
        size >>= 7
    out.append(size)
    out.extend(data)


def audio_play_data(url, title="", subtitle="", fmt="MP3"):
    """base64(protobuf {1:"audio_play", 2: JSON{stream:{…},set_pause}})."""
    stream = {"url": url, "format": fmt or "MP3", "type": "Track", "offset_ms": 0}
    if title:
        stream["title"] = title
    if subtitle:
        stream["subtitle"] = subtitle
    payload = json.dumps({"stream": stream, "set_pause": False}, ensure_ascii=False)
    raw = bytearray()
    _pb_string(raw, 1, "audio_play")
    _pb_string(raw, 2, payload)
    return base64.b64encode(bytes(raw)).decode("ascii")


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


def play_stream(name, url, fmt="MP3", subtitle="", wait=2.5):
    """Отдать ссылку штатному плееру. Возвращает краткое состояние."""
    reply = glagol.send({"command": "externalCommandBypass",
                         "data": audio_play_data(url, title=name, subtitle=subtitle, fmt=fmt)},
                        timeout=12)
    status = (reply or {}).get("status")
    if status not in (None, "SUCCESS"):
        raise RuntimeError("станция отклонила ссылку: %s" % status)
    time.sleep(max(0.5, float(wait or 2.5)))
    brief = state_brief()
    if not brief.get("playing"):
        hint = ("HTTPS-потоки штатный плеер на этой прошивке не открывает — "
                "нужен http://" if url.startswith("https://") else
                "станция не начала воспроизведение — проверьте ссылку и формат")
        raise RuntimeError(hint)
    return brief


def main():
    if glagol is None:
        common.fail(501, "Модуль glagol недоступен")
        return

    data = common.params()

    if common.truthy(data.get("stop")):
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
                brief = play_stream(station["name"], station["url"])
            except (ValueError, RuntimeError) as exc:
                common.fail(502, "Поток не играет: %s" % exc,
                            station={"index": index, "name": station["name"]},
                            url=station["url"])
                return
            brief["stream"] = True
            brief["station"] = {"index": index, "name": station["name"]}
            brief["url"] = station["url"]
            common.respond(200, brief)
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

    common.respond(200, {"ok": True, "stations": all_stations()})


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
