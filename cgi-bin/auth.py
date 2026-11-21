#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""PIN-защита пульта (по умолчанию выключена).

    GET  /cgi-bin/auth.py?status=1      — включён ли PIN и авторизован ли браузер
    POST /cgi-bin/auth.py {"login":1,"pin":"1234"}
    POST /cgi-bin/auth.py {"pin":"1234","enabled":1}   — включить/сменить PIN
    POST /cgi-bin/auth.py {"disable":1}                — выключить
    POST /cgi-bin/auth.py {"logout":1}
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows/тесты
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

STORE = os.environ.get("MYPULT_AUTH_PATH") or "/data/mypult/auth.json"
GUARD = os.environ.get("MYPULT_AUTH_GUARD") or "/data/mypult/auth_guard.json"
COOKIE = "mypult_token"
MAX_AGE = 30 * 24 * 3600
MAX_FAILS = 5
FAIL_WINDOW = 10 * 60
LOCK_SECONDS = 5 * 60
MAX_IPS = 50
PIN_ITERATIONS = 120_000
TOKEN_BYTES = 32


def load():
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"enabled": True, "broken": True}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"enabled": True, "broken": True}


def save(data):
    common.write_json(STORE, data)


def _guard_load():
    try:
        with open(GUARD, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _guard_save(data):
    try:
        common.write_json(GUARD, data)
    except OSError:
        pass


def _guard_key():
    return str(os.environ.get("REMOTE_ADDR") or "?")


@contextlib.contextmanager
def _guard_lock():
    """Сериализуем чтение-изменение-запись счётчика попыток.

    Без блокировки параллельные запросы теряли инкремент, и лимит
    «5 ошибок за 10 минут» обходился простым параллельным перебором.
    """
    if fcntl is None:
        yield
        return
    try:
        handle = open(GUARD + ".lock", "a+", encoding="utf-8")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _retry_seconds(data):
    entry = data.get(_guard_key()) or {}
    until = float(entry.get("until") or 0)
    return max(0, int(until - time.time() + 0.999)) if until > time.time() else 0


def guard_retry():
    """Сколько секунд ещё нельзя пробовать PIN (0 — можно)."""
    with _guard_lock():
        return _retry_seconds(_guard_load())


def guard_fail():
    now = time.time()
    with _guard_lock():
        data = _guard_load()
        key = _guard_key()
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        first = float(entry.get("first") or 0)
        count = int(entry.get("count") or 0)
        if not first or now - first > FAIL_WINDOW:
            first = now
            count = 0
        count += 1
        until = now + LOCK_SECONDS if count >= MAX_FAILS else 0
        data[key] = {"first": first, "count": count, "until": until}
        # не даём файлу расти бесконечно
        if len(data) > MAX_IPS:
            data = dict(list(data.items())[-MAX_IPS:])
        _guard_save(data)
        return max(0, int(until - now + 0.999)) if until > now else 0


def guard_success():
    with _guard_lock():
        data = _guard_load()
        if data.pop(_guard_key(), None) is not None:
            _guard_save(data)


def pin_hash(pin, salt, iterations=PIN_ITERATIONS):
    """PBKDF2-SHA256 вместо одного SHA-256: 4–8 цифр перебираются офлайн."""
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"),
                               salt.encode("utf-8"), int(iterations)).hex()


def legacy_hash(pin, salt):
    return hashlib.sha256((salt + ":" + pin).encode("utf-8")).hexdigest()


def verify_pin(pin, config):
    stored = str(config.get("pin_hash") or "")
    salt = str(config.get("salt") or "")
    if not stored:
        return False
    if config.get("algo") == "pbkdf2_sha256":
        iterations = common.to_int(config.get("iterations"), PIN_ITERATIONS, 10_000, 1_000_000)
        return hmac.compare_digest(pin_hash(pin, salt, iterations), stored)
    return hmac.compare_digest(legacy_hash(pin, salt), stored)


def new_token():
    return secrets.token_hex(TOKEN_BYTES)


def make_token(salt, hashed):
    """Старый детерминированный токен — только для совместимости."""
    return hashlib.sha256(("mypult:" + salt + ":" + hashed).encode("utf-8")).hexdigest()


def cookie_header(token, age=MAX_AGE):
    return ("Set-Cookie",
            "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (COOKIE, token, age))


def status():
    config = load()
    enabled = bool(config.get("enabled"))
    return {
        "ok": True,
        "enabled": enabled,
        "broken": bool(config.get("broken")),
        "authed": bool(enabled and common.auth_cookie_ok(config)),
        "has_pin": bool(config.get("pin_hash")),
    }


def can_manage(config):
    """Менять PIN/отключать защиту можно только с localhost или после входа."""
    if not config.get("enabled"):
        return True
    if os.environ.get("REMOTE_ADDR") in ("127.0.0.1", "::1"):
        return True
    return common.auth_cookie_ok(config)


def main():
    data = common.params()

    if common.truthy(data.get("logout_all")):
        config = load()
        if not can_manage(config):
            common.fail(403, "Сначала введите PIN")
            return
        if config.get("enabled"):
            config["token"] = secrets.token_hex(32)
            save(config)
        common.respond(200, {"ok": True, "authed": False},
                       [cookie_header("", 0)])
        return

    if common.truthy(data.get("logout")):
        common.respond(200, {"ok": True, "authed": False},
                       [cookie_header("", 0)])
        return

    if common.truthy(data.get("login")):
        config = load()
        if config.get("broken"):
            common.fail(503, "auth.json повреждён — восстановите локально")
            return
        pin = str(data.get("pin") or "").strip()
        if not config.get("enabled"):
            common.fail(400, "PIN не включён")
            return
        retry = guard_retry()
        if retry:
            common.fail(429, "Слишком много попыток. Подождите %d с" % retry)
            return
        if not pin or not verify_pin(pin, config):
            retry = guard_fail()
            common.fail(403, "Неверный PIN" + (". Блокировка на %d с" % retry if retry else ""))
            return
        guard_success()
        # Старые установки хранили токен, выведенный из хеша PIN. После
        # первого удачного входа меняем его на случайный: утечка auth.json
        # больше не даёт вечную куку.
        token = str(config.get("token") or "")
        if not token or not config.get("token_random"):
            token = new_token()
            config["token"] = token
            config["token_random"] = True
            save(config)
        common.respond(200, {"ok": True, "authed": True}, [cookie_header(token)])
        return

    if common.truthy(data.get("disable")):
        config = load()
        if not can_manage(config):
            common.fail(403, "Сначала введите PIN")
            return
        if config.get("broken"):
            config = {"enabled": False}
        else:
            config["enabled"] = False
        save(config)
        common.respond(200, {"ok": True, "enabled": False},
                       [cookie_header("", 0)])
        return

    if data.get("pin") is not None and common.truthy(data.get("enabled")):
        current = load()
        if not can_manage(current):
            common.fail(403, "Сначала введите текущий PIN")
            return
        pin = str(data.get("pin") or "").strip()
        if not (4 <= len(pin) <= 8) or not pin.isdigit():
            common.fail(400, "PIN: 4–8 цифр")
            return
        salt = secrets.token_hex(8)
        hashed = pin_hash(pin, salt)
        config = {"enabled": True, "salt": salt, "pin_hash": hashed,
                  "algo": "pbkdf2_sha256", "iterations": PIN_ITERATIONS,
                  "token": new_token(), "token_random": True}
        save(config)
        # Не оставляем текущий браузер «уже вошедшим»: новая соль
        # инвалидирует старые куки, а эту — гасим.
        common.respond(200, {"ok": True, "enabled": True, "authed": False},
                       [cookie_header("", 0)])
        return

    common.respond(200, status())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка auth.py: %s" % exc)
        except Exception:
            pass
