#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Резервная копия настроек пульта.

    GET  /cgi-bin/backup.py            — JSON с настройками (секреты замаскированы)
    POST /cgi-bin/backup.py            {"full": 1} — то же, но с ключами и подпиской
    POST /cgi-bin/backup.py            {"data": {...}} — восстановить

В копию попадают: профили LLM, VPN-подписка, сценарии, радио, DNS, яркость
кольца, чаты, настройки голоса и автояркости.

Секреты (`key`, `subscription`, токены и uuid серверов) по умолчанию заменяются
на `***`: панель отдаётся по HTTP, и файл копии легко уезжает в мессенджер или
на чужой диск. Полная копия — только осознанным POST с `full=1`. При
восстановлении файлы с маской пропускаются, чтобы `***` не затёрли живые ключи.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

BASE = "/data/mypult"
MASK = "***"
SECRET_KEYS = frozenset({"token", "password", "psk", "secret"})
FILE_SECRET_KEYS = {
    "llm": frozenset({"key", "api_key", "token"}),
    "vpn": frozenset({"url", "subscription", "uuid", "password", "psk", "secret", "token"}),
}
FILES = {
    "llm": "llm.json",
    "vpn": "vpn.json",
    "scenarios": "scenarios.json",
    "radio": "radio.json",
    "dns": "dns.json",
    "ring": "ring.json",
    "chats": "chats.json",
    "voice": "voice.json",
    "auto_brightness": "auto_brightness.json",
    "display": "display.json",
    "display_mode": "display_mode.json",
}


def _mask(value, keys):
    """Копия значения с вырезанными секретами (структура сохраняется)."""
    if isinstance(value, dict):
        return {name: (MASK if name in keys and item not in ("", None)
                       else _mask(item, keys))
                for name, item in value.items()}
    if isinstance(value, list):
        return [_mask(item, keys) for item in value]
    return value


def _masked(value):
    """Есть ли в значении маска — такую копию нельзя заливать обратно."""
    if isinstance(value, dict):
        return any(_masked(item) for item in value.values())
    if isinstance(value, list):
        return any(_masked(item) for item in value)
    return value == MASK


def export(include_secrets=False):
    data = {}
    for key, name in FILES.items():
        path = os.path.join(BASE, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        data[key] = payload if include_secrets else _mask(
            payload, FILE_SECRET_KEYS.get(key, SECRET_KEYS))
    return {"ok": True, "version": 1, "secrets": bool(include_secrets), "data": data}


def restore(payload):
    if not isinstance(payload, dict):
        raise RuntimeError("нужен объект data")
    written, skipped = [], []
    for key, value in payload.items():
        name = FILES.get(key)
        if not name or not isinstance(value, (dict, list)):
            continue
        if _masked(value):
            skipped.append(key)
            continue
        path = os.path.join(BASE, name)
        common.write_json(path, value, indent=2)
        written.append(key)
    return written, skipped


def main():
    data = common.params()
    method = (os.environ.get("REQUEST_METHOD") or "GET").upper()

    if data.get("data") is not None:
        try:
            written, skipped = restore(data.get("data"))
        except Exception as exc:  # noqa: BLE001
            common.fail(400, "Восстановление не удалось: %s" % exc)
            return
        common.ok(restored=written, skipped=skipped)
        return

    # Полная копия — только POST (а POST без same-origin не проходит в common),
    # чтобы ссылка/закладка/чужой GET не вытянули ключи.
    full = common.truthy(data.get("full")) and method == "POST"
    common.respond(200, export(include_secrets=full))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка backup.py: %s" % exc)
        except Exception:
            pass
