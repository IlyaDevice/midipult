#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Настройки голоса: потолок громкости ответов LLM и режим прослушивания.

    GET  /cgi-bin/voicecfg.py
    POST /cgi-bin/voicecfg.py {"max_volume": 20}
    POST /cgi-bin/voicecfg.py {"enabled": false}   — пользователь выключил пульт
    POST /cgi-bin/voicecfg.py {"idle": "auto"}     — не слушать вхолостую

`idle`:
    always — держать сессию распознавания постоянно (поведение Миди);
    auto   — открывать её только когда есть что обрабатывать: есть сценарии
             или включена нейросеть. На не-Миди (например Station 2) это
             значение по умолчанию: там каждая сессия подсвечивает кольцо.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

STORE = "/data/mypult/voice.json"
LUNA = "http://127.0.0.1:8099"
IDLE_MODES = ("always", "auto")
MIDI_MODELS = ("yandexmidi",)


def load():
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    common.write_json(STORE, data)


def device_model():
    """Модель станции по Luna. Пусто — Luna не ответила."""
    try:
        with urllib.request.urlopen(LUNA + "/api/device", timeout=4) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return str(data.get("model") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def default_idle(model=None):
    """На Миди сессия распознавания кольцо не подсвечивает, на других — да.

    Неизвестная модель считается Миди: лучше сохранить привычное поведение,
    чем молча выключить голосовые команды.
    """
    if model is None:
        model = device_model()
    return "always" if model in ("",) + MIDI_MODELS else "auto"


def resolve_idle(config, model=None):
    mode = str((config or {}).get("idle") or "").strip().lower()
    if mode in IDLE_MODES:
        return mode
    return default_idle(model)


def is_enabled(config):
    """Пользовательский тумблер. По умолчанию включён (как раньше)."""
    return (config or {}).get("enabled") is not False


def state(config=None):
    config = load() if config is None else config
    model = device_model()
    return {
        "enabled": is_enabled(config),
        "idle": resolve_idle(config, model),
        "device": model,
        "max_volume": common.to_int(config.get("max_volume"), 45, 1, 100),
    }


def main():
    data = common.params()

    if data.get("max_volume") is not None:
        config = load()
        config["max_volume"] = common.to_int(data.get("max_volume"), 45, 1, 100)
        save(config)
        common.ok(config=config, **state(config))
        return

    if data.get("enabled") is not None:
        config = load()
        config["enabled"] = common.truthy(data.get("enabled"))
        save(config)
        common.ok(config=config, **state(config))
        return

    if data.get("idle") is not None:
        mode = str(data.get("idle") or "").strip().lower()
        if mode not in IDLE_MODES:
            common.fail(400, "режим простоя: %s" % ", ".join(IDLE_MODES))
            return
        config = load()
        config["idle"] = mode
        save(config)
        common.ok(config=config, **state(config))
        return

    common.ok(config=load(), **state())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка voicecfg.py: %s" % exc)
        except Exception:
            pass
