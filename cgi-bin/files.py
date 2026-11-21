#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Файловый менеджер MidiPult.

Работает только через PIN-куку (common.require_auth). Чтение разрешено в
/data, /opt, /tmp, /root, /etc и /var/log; запись — только в /data/mypult и
/tmp, чтобы нельзя было случайно переписать систему.

    GET  /cgi-bin/files.py?action=list&path=/data/mypult
    GET  /cgi-bin/files.py?action=read&path=/data/mypult/ring.json
    GET  /cgi-bin/files.py?action=raw&path=/data/mypult/ring.json
    POST /cgi-bin/files.py  {"action":"write","path":"...","text":"..."}
    POST /cgi-bin/files.py  {"action":"mkdir","path":"...","name":"..."}
    POST /cgi-bin/files.py  {"action":"rename","path":"...","name":"..."}
    POST /cgi-bin/files.py  {"action":"delete","path":"..."}
    POST /cgi-bin/files.py?action=upload&path=/tmp&name=file.bin
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import stat
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MAX_TEXT = 2 * 1024 * 1024
MAX_UPLOAD = 64 * 1024 * 1024
READ_ROOTS = ("/data", "/opt", "/tmp", "/root", "/etc", "/var/log")
# Содержимое (не листинг) отдаём только из этих каталогов: там нет системных
# секретов вроде /etc/shadow, wpa_supplicant.conf, ключей SSH и токенов
# Яндекс-аккаунта. Остальные корни можно листать, но не читать.
CONTENT_ROOTS = ("/data/mypult", "/data/myweb", "/tmp", "/var/log")
WRITE_ROOTS = ("/data/mypult", "/tmp")
FORBIDDEN_DELETE = ("/", "/data", "/data/mypult", "/data/myweb", "/opt",
                    "/tmp", "/root", "/etc", "/var/log", "/var")
ROOT_CHOICES = (
    {"path": "/data", "label": "Данные"},
    {"path": "/opt", "label": "Entware"},
    {"path": "/tmp", "label": "Временные"},
    {"path": "/root", "label": "Root"},
    {"path": "/etc", "label": "Конфиги"},
    {"path": "/var/log", "label": "Логи"},
)
SENSITIVE_NAMES = {
    "auth.json", "yandex_auth.json", "llm.json", "vpn.json",
    "zvuk.json", ".zvuk_auth.json", "glagol.conf", "glagol.json",
    "spotify.json", "youtube_state.json", "soundcloud_state.json",
    # история чатов с LLM и старый кэш Glagol-токена: это не для файлового менеджера
    "chats.json", "secrets.json",
}
SENSITIVE_EXT = (".pem", ".key", ".secret")

# Код, который выполняется от root, нельзя перезаписывать через файловый
# менеджер: случайная загрузка voice_daemon.py ломает пульт до перезагрузки.
PROTECTED_DIRS = ("/data/mypult/bin",)
PROTECTED_EXT = (".py", ".sh", ".pyc", ".so", ".pid")


class FileError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _inside(path, roots):
    for root in roots:
        root = os.path.realpath(root)
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _resolve(value, write=False):
    raw = str(value or "").strip()
    if not raw:
        raw = "/data/mypult"
    if "\x00" in raw:
        raise FileError("Недопустимый путь")
    if not raw.startswith("/"):
        raw = "/data/mypult/" + raw
    path = os.path.realpath(os.path.abspath(raw))
    if not _inside(path, WRITE_ROOTS if write else READ_ROOTS):
        raise FileError("Этот путь недоступен" if not write else
                        "Запись разрешена только в /data/mypult и /tmp", 403)
    return path


def _sensitive(path):
    name = os.path.basename(path)
    return (name in SENSITIVE_NAMES or name.endswith(SENSITIVE_EXT)
            or name.endswith("_auth.json"))


def _protected(path):
    """Исполняемый код и pid-файлы внутри /data/mypult: только чтение."""
    real = os.path.realpath(path)
    root = os.path.realpath("/data/mypult")
    if not (real == root or real.startswith(root + os.sep)):
        return False
    if real.endswith(PROTECTED_EXT):
        return True
    for folder in PROTECTED_DIRS:
        folder = os.path.realpath(folder)
        if real == folder or real.startswith(folder + os.sep):
            return True
    return False


def _can_read(path):
    """Содержимое разрешено только в CONTENT_ROOTS и не для секретов."""
    return _inside(os.path.realpath(path), CONTENT_ROOTS) and not _sensitive(path)


def _can_write(path):
    return (_inside(os.path.realpath(path), WRITE_ROOTS)
            and not _sensitive(path) and not _protected(path))


def _entry(path, name):
    full = os.path.join(path, name)
    try:
        st = os.lstat(full)
    except OSError:
        return None
    is_link = stat.S_ISLNK(st.st_mode)
    real = os.path.realpath(full)
    if not _inside(real, READ_ROOTS):
        return None
    try:
        target = os.stat(full)
    except OSError:
        target = st
    is_dir = stat.S_ISDIR(target.st_mode)
    locked = _sensitive(full)
    return {
        "name": name,
        "path": full,
        "kind": "dir" if is_dir else "file",
        "size": 0 if is_dir else int(target.st_size),
        "mtime": int(target.st_mtime),
        "link": is_link,
        "locked": locked,
        "readable": _can_read(full),
        "writable": _can_write(full),
    }


def _list(path):
    if not os.path.isdir(path):
        raise FileError("Каталог не найден", 404)
    items = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                item = _entry(path, entry.name)
                if item:
                    items.append(item)
    except OSError as exc:
        raise FileError("Не удалось прочитать каталог: %s" % exc, 500)
    items.sort(key=lambda x: (x["kind"] != "dir", x["name"].casefold()))
    parent = os.path.dirname(path)
    if parent == path or not _inside(parent, READ_ROOTS):
        parent = ""
    return {
        "path": path,
        "parent": parent,
        "can_write": _can_write(path),
        "items": items,
        "roots": [dict(x, can_write=_can_write(x["path"])) for x in ROOT_CHOICES],
    }


def _read(path):
    if not os.path.isfile(path):
        raise FileError("Файл не найден", 404)
    if _sensitive(path):
        raise FileError("Файл с секретами нельзя открыть здесь", 403)
    if not _can_read(path):
        raise FileError("Просмотр файлов вне /data/mypult, /data/myweb, "
                        "/tmp и /var/log отключён", 403)
    size = os.path.getsize(path)
    if size > MAX_TEXT:
        raise FileError("Файл больше 2 МБ — скачайте его", 413)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise FileError("Не удалось прочитать: %s" % exc, 500)
    if b"\x00" in data:
        return {"binary": True, "size": size,
                "raw": "/cgi-bin/files.py?action=raw&path=" + urllib.parse.quote(path)}
    return {"binary": False, "text": data.decode("utf-8", "replace"),
            "size": size,
            "raw": "/cgi-bin/files.py?action=raw&path=" + urllib.parse.quote(path)}


def _write(path, text):
    if not _can_write(path):
        raise FileError("Запись сюда запрещена", 403)
    data = str(text or "").encode("utf-8")
    if len(data) > MAX_TEXT:
        raise FileError("Текст больше 2 МБ", 413)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise FileError("Каталог не найден", 404)
    tmp = path + ".mypult-tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise FileError("Не удалось сохранить: %s" % exc, 500)


def _mkdir(path, name):
    name = _new_name(name)
    target = os.path.join(path, name)
    if not _can_write(target):
        raise FileError("Создавать здесь нельзя", 403)
    try:
        os.mkdir(target, 0o755)
    except FileExistsError:
        raise FileError("Уже существует")
    except OSError as exc:
        raise FileError("Не удалось создать: %s" % exc, 500)


def _new_name(value):
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise FileError("Недопустимое имя")
    return name


def _rename(path, name):
    name = _new_name(name)
    if os.path.realpath(path) in FORBIDDEN_DELETE:
        raise FileError("Корень переименовывать нельзя", 403)
    target = os.path.join(os.path.dirname(path), name)
    if not _can_write(path) or not _can_write(target):
        raise FileError("Переименовывать здесь нельзя", 403)
    if os.path.exists(target):
        raise FileError("Такое имя уже есть")
    try:
        os.rename(path, target)
    except OSError as exc:
        raise FileError("Не удалось переименовать: %s" % exc, 500)


def _delete(path):
    real = os.path.realpath(path)
    if real in FORBIDDEN_DELETE:
        raise FileError("Корень удалять нельзя", 403)
    if not _can_write(real):
        raise FileError("Удалять здесь нельзя", 403)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError as exc:
        raise FileError("Не удалось удалить: %s" % exc, 500)


def _raw(path, download=False):
    if not os.path.isfile(path):
        raise FileError("Файл не найден", 404)
    if _sensitive(path):
        raise FileError("Файл с секретами нельзя открыть здесь", 403)
    if not _can_read(path):
        raise FileError("Скачивание файлов вне /data/mypult, /data/myweb, "
                        "/tmp и /var/log отключено", 403)
    size = os.path.getsize(path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith(".opus"):
        ctype = "audio/ogg"
    start, end = 0, size - 1
    status = "200 OK"
    rng = os.environ.get("HTTP_RANGE") or ""
    if rng.startswith("bytes="):
        part = rng[6:].split(",", 1)[0].strip()
        left, _, right = part.partition("-")
        try:
            if left:
                start = max(0, int(left))
                end = min(size - 1, int(right)) if right else size - 1
            elif right:
                start = max(0, size - int(right))
            if start > end or start >= size:
                raise ValueError
            status = "206 Partial Content"
        except ValueError:
            start, end, status = 0, size - 1, "200 OK"
    length = max(0, end - start + 1)
    headers = [
        "Status: " + status,
        "Content-Type: " + ctype,
        "Content-Length: " + str(length),
        "Accept-Ranges: bytes",
        "Cache-Control: no-store, max-age=0",
        "X-Content-Type-Options: nosniff",
    ]
    if status.startswith("206"):
        headers.append("Content-Range: bytes %d-%d/%d" % (start, end, size))
    if download:
        fname = os.path.basename(path).replace('"', "")
        headers.append("Content-Disposition: attachment; filename=\"%s\"" % fname)
    out = sys.stdout.buffer
    out.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(128 * 1024, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        out.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _upload(path, name):
    if not _can_write(os.path.join(path, name)):
        raise FileError("Загрузка сюда запрещена", 403)
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        raise FileError("Пустой файл")
    if length > MAX_UPLOAD:
        raise FileError("Файл больше 64 МБ", 413)
    name = _new_name(name)
    target = os.path.join(path, name)
    tmp = target + ".mypult-upload"
    try:
        with open(tmp, "wb") as fh:
            left = length
            while left > 0:
                chunk = sys.stdin.buffer.read(min(128 * 1024, left))
                if not chunk:
                    raise FileError("Файл оборвался", 400)
                fh.write(chunk)
                left -= len(chunk)
        os.replace(tmp, target)
        os.chmod(target, 0o644)
    except FileError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise FileError("Не удалось загрузить: %s" % exc, 500)


def main():
    method = (os.environ.get("REQUEST_METHOD") or "GET").upper()
    query = os.environ.get("QUERY_STRING") or ""
    qs = {k: v[-1] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
    action = str(qs.get("action") or "").strip().lower()

    # Загрузка — сырое тело, common.params() его обрезает до 1 МБ.
    if action == "upload":
        common.require_auth()
        if method not in ("POST", "PUT"):
            raise FileError("Нужен POST", 405)
        if not common.same_origin_ok():
            raise FileError("Запрос с другого сайта запрещён", 403)
        folder = _resolve(qs.get("path") or "/data/mypult", write=True)
        if not os.path.isdir(folder):
            raise FileError("Каталог не найден", 404)
        _upload(folder, qs.get("name") or "")
        common.ok(path=folder)
        return

    data = common.params()
    action = str(data.get("action") or action or "list").strip().lower()
    explicit = data.get("path") or qs.get("path")
    if action in ("write", "mkdir", "rename", "delete") and not explicit:
        raise FileError("Укажите путь явно", 400)
    path = _resolve(explicit or "/data/mypult",
                    write=action in ("write", "mkdir", "rename", "delete"))

    if action in ("write", "mkdir", "rename", "delete") and method not in ("POST", "PUT"):
        raise FileError("Нужен POST", 405)

    if action in ("", "list"):
        result = _list(path)
        result["read_only"] = not result["can_write"]
        common.ok(**result)
    elif action == "read":
        common.ok(**_read(path))
    elif action == "raw":
        _raw(path, download=common.truthy(data.get("download")))
    elif action == "write":
        _write(path, data.get("text") or "")
        common.ok(path=path)
    elif action == "mkdir":
        _mkdir(path, data.get("name") or "")
        common.ok(path=path)
    elif action == "rename":
        _rename(path, data.get("name") or "")
        common.ok()
    elif action == "delete":
        _delete(path)
        common.ok()
    else:
        raise FileError("Неизвестное действие", 400)


if __name__ == "__main__":
    try:
        main()
    except FileError as exc:
        try:
            common.fail(exc.status, str(exc))
        except BrokenPipeError:
            pass
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка files.py: %s" % exc)
        except Exception:
            pass
