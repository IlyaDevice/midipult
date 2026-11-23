#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""LLM: OpenAI-совместимый API и родной Messages API Anthropic; несколько профилей.

    GET  /cgi-bin/llm.py?info=1           — провайдеры, профили, активный
    POST /cgi-bin/llm.py {"enabled":1}    — включить/выключить (без правки профиля)
    POST /cgi-bin/llm.py {"profile_id","name","provider","model","base_url","key"} — сохранить профиль
    POST /cgi-bin/llm.py {"activate":"deepseek"} — сделать профиль активным
    POST /cgi-bin/llm.py {"delete":"deepseek"}   — удалить профиль
    POST /cgi-bin/llm.py {"prompt":"..."}       — вопрос к активному профилю
    POST /cgi-bin/llm.py {"location","timezone"} — место и часовой пояс для модели
    GET  /cgi-bin/llm.py?history=1[&chat=chat-2] — сообщения чата (по умолчанию активного)

Ключи хранятся в /data/mypult/llm.json (600), наружу отдаётся только key_set.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # очень старый Python без zoneinfo
    ZoneInfo = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

try:
    import websearch  # noqa: E402
except Exception:  # noqa: BLE001
    websearch = None

CONFIG_DIR = "/data/mypult"
CONFIG_PATH = os.path.join(CONFIG_DIR, "llm.json")
CHATS_PATH = os.path.join(CONFIG_DIR, "chats.json")

LOCATION_LIMIT = 100
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")

PROVIDERS = {
    "openai": {
        "title": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
    },
    "openrouter": {
        "title": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-2.0-flash-001",
            "deepseek/deepseek-chat",
        ],
    },
    "deepseek": {
        "title": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
    },
    "anthropic": {
        "title": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
    },
    "custom": {
        "title": "Свой сервер (OpenAI-совместимый)",
        "base_url": "",
        "models": [],
    },
}

# Родной Messages API Anthropic (/v1/messages). Сэмплинг не передаётся:
# в версии 2026-10-21 temperature/top_p/top_k дают 400 на любой модели.
ANTHROPIC_VERSION = "2026-10-21"
# Короткие голосовые ответы: низкое усилие заметно быстрее.
ANTHROPIC_EFFORT = "low"
# Думание и текст между вызовами поиска тоже расходуют max_tokens.
ANTHROPIC_THINKING_ROOM = 1500
# Базовая версия поиска: на Haiku 5.5 отвечает за ~4 с против ~10 с у
# web_search_20260209 (с динамической фильтрацией) и тратит меньше токенов.
ANTHROPIC_SEARCH_TOOL = "web_search_20250305"
# Запасной вариант: если основная версия инструмента не поддержана моделью и
# API отвечает 400 — пробуем актуальную версию с динамической фильтрацией.
ANTHROPIC_SEARCH_FALLBACK = "web_search_20260209"
ANTHROPIC_SEARCH_MAX_USES = 3
ANTHROPIC_TOOL_BLOCKS = frozenset({
    "tool_use", "server_tool_use", "web_search_tool_result", "web_fetch_tool_result",
    "code_execution_tool_result", "bash_code_execution_tool_result",
})
ANTHROPIC_SEARCH_NOTE = (
    "У тебя есть поиск в интернете. Используй его для свежих сведений: погода, "
    "новости, курсы валют, расписания, результаты, события. На остальные вопросы "
    "отвечай сам, без поиска. Найденное перескажи коротко — одно-два предложения "
    "с главным; источники, ссылки и расхождения между сайтами не перечисляй. "
    "В истории диалога хранится только текст прошлых ответов, без результатов "
    "поиска: не пересматривай и не исправляй прошлые ответы, если об этом не просят.")


def slug(text):
    text = re.sub(r"[^a-zа-я0-9]+", "-", (text or "").lower()).strip("-")
    return text[:40] or "profile"


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    if "profiles" not in data:
        profiles = []
        if data.get("provider") or data.get("key"):
            provider = data.get("provider") or "openai"
            profiles.append({
                "id": "default",
                "name": data.get("name") or PROVIDERS.get(provider, {}).get("title") or "Профиль",
                "provider": provider,
                "model": data.get("model") or "",
                "base_url": data.get("base_url") or PROVIDERS.get(provider, {}).get("base_url", ""),
                "key": data.get("key") or "",
            })
        data = {
            "enabled": bool(data.get("enabled")),
            "active": profiles[0]["id"] if profiles else None,
            "profiles": profiles,
        }

    if not isinstance(data.get("profiles"), list):
        data["profiles"] = []
    if not isinstance(data.get("max_tokens"), int):
        data["max_tokens"] = 260
    if not isinstance(data.get("system_prompt"), str):
        data["system_prompt"] = ""
    if not isinstance(data.get("web_search"), bool):
        data["web_search"] = False
    if not isinstance(data.get("search_url"), str):
        data["search_url"] = ""
    if not isinstance(data.get("follow_up"), bool):
        data["follow_up"] = False
    if not isinstance(data.get("location"), str):
        data["location"] = ""
    if not isinstance(data.get("timezone"), str):
        data["timezone"] = ""
    return data


def zone(name):
    """ZoneInfo по имени IANA или None, если имя пустое/неизвестное."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError и т.п.
        return None


def local_now(config, now=None):
    """Время в поясе из настроек; без настройки — местное время станции."""
    tz = zone(config.get("timezone"))
    if now is None:
        moment = datetime.datetime.now().astimezone()
    elif tz is None:
        moment = now.astimezone()
    else:
        moment = now
    return moment.astimezone(tz) if tz is not None else moment


def context_note(config, now=None):
    """Строка о дате, времени и месте — модель сама их не знает."""
    moment = local_now(config, now)
    offset = moment.strftime("%z")
    offset = "UTC%s:%s" % (offset[:3], offset[3:]) if offset else "UTC"
    tz_name = config.get("timezone") if zone(config.get("timezone")) else (
        moment.tzname() or "местное")
    note = "Сейчас %s, %d %s %d года, %s (часовой пояс %s, %s)." % (
        WEEKDAYS[moment.weekday()], moment.day, MONTHS[moment.month - 1], moment.year,
        moment.strftime("%H:%M"), tz_name, offset)
    location = str(config.get("location") or "").strip()
    if location:
        note += " Колонка находится здесь: %s." % location
    return note


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    common.write_json(CONFIG_PATH, config, indent=2)


def public_profile(profile):
    if not profile:
        return None
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "provider": profile.get("provider") or "openai",
        "model": profile.get("model") or "",
        "base_url": profile.get("base_url") or "",
        "key_set": bool(profile.get("key")),
    }


def active_profile(config):
    profiles = config.get("profiles") or []
    active = config.get("active")
    for profile in profiles:
        if profile.get("id") == active:
            return profile
    return profiles[0] if profiles else None


def info(config=None):
    config = config or load_config()
    profiles = config.get("profiles") or []
    active = active_profile(config)
    chats = load_chats()
    chat = active_chat(chats)
    return {
        "ok": True,
        "providers": [
            {"id": key, "title": value["title"], "base_url": value["base_url"], "models": value["models"]}
            for key, value in PROVIDERS.items()
        ],
        "profiles": [public_profile(item) for item in profiles],
        "active": active.get("id") if active else None,
        "active_name": active.get("name") if active else None,
        "voice_enabled": voice_enabled(config),
        # Совместимость со старым API: enabled теперь означает голосовой тумблер.
        "enabled": voice_enabled(config),
        "system_prompt": str(config.get("system_prompt") or ""),
        "web_search": bool(config.get("web_search")),
        "search_url": str(config.get("search_url") or ""),
        "search_mode": search_mode(active.get("provider") if active else "",
                                   config),
        "follow_up": bool(config.get("follow_up")),
        "location": str(config.get("location") or ""),
        "timezone": str(config.get("timezone") or ""),
        "context": context_note(config),
        "config": public_profile(active),
        "max_tokens": int(config.get("max_tokens") or 260),
        "chats": [chat_public(item) for item in chats.get("chats") or []],
        "active_chat": chat.get("id") if chat else None,
        "active_chat_name": chat.get("name") if chat else None,
    }


def load_chats():
    try:
        with open(CHATS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, list) or not chats:
        chats = [{"id": "chat-1", "name": "Чат 1", "messages": []}]
        data = {"active": "chat-1", "chats": chats}
        save_chats(data)
    if not data.get("active") or not any(c.get("id") == data["active"] for c in chats):
        data["active"] = chats[0].get("id")
    return data


def save_chats(data):
    try:
        common.write_json(CHATS_PATH, data, indent=2)
    except OSError:
        pass


def active_chat(data):
    for chat in data.get("chats") or []:
        if chat.get("id") == data.get("active"):
            return chat
    return (data.get("chats") or [None])[0]


def chat_public(chat):
    return {
        "id": chat.get("id"),
        "name": chat.get("name"),
        "messages": len(chat.get("messages") or []),
    }


def voice_enabled(config):
    """Тумблер относится только к голосовым вопросам, не к текстовому API."""
    if "voice_enabled" in config:
        return bool(config.get("voice_enabled"))
    return bool(config.get("enabled"))


NATIVE_SEARCH = ("openrouter", "openai", "anthropic")


def native_search(provider):
    """У провайдера есть встроенный поиск? OpenRouter — плагин web,
    OpenAI — инструмент web_search в Responses API, Anthropic — серверный
    инструмент web_search в Messages API."""
    return (provider or "") in NATIVE_SEARCH


def search_mode(provider, config):
    """Чем реально будет искаться: провайдер, searx или ничего."""
    if native_search(provider):
        return provider
    if str(config.get("search_url") or "").strip():
        return "searx"
    return None


def _post_chat(base_url, payload, key, timeout=90):
    """Один POST в /chat/completions; HTTPError отдаётся вызывающему."""
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_detail(exc, limit=600):
    """Тело HTTP-ошибки для лога и ответа панели (читается один раз)."""
    try:
        return exc.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _post_responses(base_url, payload, key, timeout=120):
    """POST в OpenAI Responses API (родной веб-поиск)."""
    request = urllib.request.Request(
        base_url + "/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _responses_answer(body):
    """Текст и аннотации из ответа Responses API."""
    text = body.get("output_text")
    annotations = []
    if isinstance(text, str) and text.strip():
        return text.strip(), annotations
    parts = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                parts.append(str(part.get("text") or ""))
                annotations.extend(part.get("annotations") or [])
    return "".join(parts).strip(), annotations


def dialog_messages(items, limit=10):
    """Сообщения чата для API: только role/content, без служебных полей.

    В истории теперь хранится `ts`, но провайдеры не любят лишние поля в
    messages, поэтому в запрос уходит чистый диалог.
    """
    clean = []
    for item in (items or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        if item.get("role") not in ("user", "assistant"):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            continue
        clean.append({"role": item["role"], "content": content})
    return clean


def _openai_web_answer(base_url, profile, messages, max_tokens, timeout=120):
    """Родной поиск OpenAI: Responses API + инструмент web_search."""
    payload = {
        "model": profile.get("model") or "gpt-4o-mini",
        "input": messages,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": int(max_tokens),
    }
    body = _post_responses(base_url, payload, profile["key"], timeout)
    text, annotations = _responses_answer(body)
    if not text:
        raise RuntimeError("пустой ответ Responses API")
    return text, websearch.annotations(annotations)


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def post_json(url, payload, headers, timeout=90):
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=dict({"Content-Type": "application/json"}, **headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(502, "API ответил HTTP %s" % exc.code, detail=_http_detail(exc)[:400])
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(502, "Нет связи с API: %s" % exc)
    except ValueError:
        raise ApiError(502, "API вернул не JSON")


def anthropic_text(content):
    """Текст ответа Messages API.

    С поиском ответ состоит из нескольких блоков: рассуждения, вызовы поиска,
    результаты и текст. Модель может сказать «сейчас посмотрю» до поиска —
    колонке нужен только итог, поэтому берём текст после последнего блока
    инструмента (без инструментов — весь текст).
    """
    blocks = [block for block in content or [] if isinstance(block, dict)]
    start = 0
    for index, block in enumerate(blocks):
        if block.get("type") in ANTHROPIC_TOOL_BLOCKS:
            start = index + 1
    tail = "".join(block.get("text") or "" for block in blocks[start:] if block.get("type") == "text")
    if tail.strip():
        return tail
    return "".join(block.get("text") or "" for block in blocks if block.get("type") == "text")


def anthropic_sources(content):
    """Источники поиска: сначала цитаты из текста ответа, потом выдача поиска."""
    if websearch is None:
        return []
    cited = []
    found = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            for citation in block.get("citations") or []:
                if isinstance(citation, dict):
                    cited.append({"url": citation.get("url"), "title": citation.get("title"),
                                  "snippet": citation.get("cited_text")})
        elif block.get("type") == "web_search_tool_result":
            results = block.get("content")
            # При ошибке поиска content — объект с error_code, а не список.
            for result in results if isinstance(results, list) else []:
                if isinstance(result, dict) and result.get("type") == "web_search_result":
                    found.append({"url": result.get("url"), "title": result.get("title")})
    return websearch.annotations(cited + found)


def anthropic_messages(base_url, key, model, system, dialog, limit, search):
    """Родной Messages API Anthropic (/v1/messages) с необязательным веб-поиском.

    Возвращает (текст, usage, источники)."""
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    tools = [ANTHROPIC_SEARCH_TOOL, ANTHROPIC_SEARCH_FALLBACK] if search else [None]
    result = None
    last_error = None
    for tool in tools:
        payload = {
            "model": model,
            "messages": list(dialog),
            "max_tokens": limit + ANTHROPIC_THINKING_ROOM,
            "output_config": {"effort": ANTHROPIC_EFFORT},
        }
        if system:
            payload["system"] = system
        if tool:
            payload["tools"] = [{"type": tool, "name": "web_search",
                                 "max_uses": ANTHROPIC_SEARCH_MAX_USES}]
        usage = {}
        content = []
        stop = None
        body = {}
        try:
            for _ in range(3):
                body = post_json(base_url + "/messages", payload, headers)
                content.extend(body.get("content") or [])
                for name, value in (body.get("usage") or {}).items():
                    if isinstance(value, int):
                        usage[name] = usage.get(name, 0) + value
                    elif name == "server_tool_use" and isinstance(value, dict):
                        counters = usage.setdefault(name, {})
                        for tool_name, count in value.items():
                            if isinstance(count, int):
                                counters[tool_name] = counters.get(tool_name, 0) + count
                stop = body.get("stop_reason")
                if stop != "pause_turn":
                    break
                # Серверный цикл поиска упёрся в лимит итераций: отправляем
                # ответ обратно как есть, API продолжит с того же места.
                payload["messages"] = list(dialog) + [
                    {"role": "assistant", "content": content}]
        except ApiError as exc:
            # Старая версия инструмента могла стать несовместимой с моделью:
            # API отвечает 400 — один раз пробуем актуальную версию.
            if tool and tool != tools[-1] and "HTTP 400" in exc.message:
                last_error = exc
                continue
            raise
        if stop == "pause_turn":
            raise ApiError(502, "Модель не закончила поиск за 3 шага",
                           detail="stop_reason=pause_turn")
        result = (content, usage, stop, body)
        break
    if result is None:
        raise last_error or ApiError(502, "Не удалось выполнить запрос")
    content, usage, stop, body = result
    if stop == "refusal":
        details = body.get("stop_details") or {}
        raise ApiError(502, "Модель отказалась отвечать на этот вопрос",
                       detail=str(details.get("explanation") or details.get("category") or "")[:300])
    answer = anthropic_text(content)
    if not answer.strip():
        raise ApiError(502, "Пустой ответ модели", detail="stop_reason=%s" % stop)
    usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return answer, usage, anthropic_sources(content)


def ask(prompt, system=None, search=None):
    config = load_config()
    profile = active_profile(config)
    if not profile:
        common.fail(400, "Нет профиля LLM", hint="Добавьте профиль и ключ")
        return
    if not profile.get("key"):
        common.fail(400, "Не задан API-ключ", hint="Вставьте ключ и нажмите «Сохранить»")
        return

    provider = profile.get("provider") or "openai"
    base_url = (profile.get("base_url") or PROVIDERS.get(provider, {}).get("base_url") or "").rstrip("/")
    if not base_url:
        common.fail(400, "Не задан base_url для своего провайдера")
        return
    try:
        base_url = common.url_guard(base_url, allow_http=True, allow_private=True,
                                    allow_loopback=True)
    except ValueError as exc:
        common.fail(400, "Некорректный base_url: %s" % exc)
        return

    chats = load_chats()
    chat = active_chat(chats)

    messages = []
    if not system:
        system = str(config.get("system_prompt") or "").strip()
    # Дату, время и место добавляем к любому промпту — и голосовому, и текстовому.
    system = (str(system or "").strip() + "\n\n" + context_note(config)).strip()

    # Веб-поиск: сначала родной у провайдера, потом свой SearXNG.
    # `search` из запроса важнее настройки (голосовые триггеры вроде «поищи»).
    use_search = bool(config.get("web_search")) if search is None else bool(search)
    sources = []
    search_source = ""
    search_note = ""
    answer = None
    body = None

    if use_search:
        mode = search_mode(provider, config)
        if mode == "searx":
            searx_url = str(config.get("search_url") or "").strip()
            query = websearch.query_from_prompt(prompt) or prompt
            try:
                found = websearch.search_searx(query, searx_url)
            except Exception as exc:  # noqa: BLE001 - поиск не должен валить ответ
                found = []
                search_note = "SearXNG не ответил: %s" % str(exc)[:160]
            if found:
                extra = websearch.context(found, query)
                system = (system + "\n\n" + extra) if system else extra
                sources = found
                search_source = "searx"
            elif not search_note:
                search_note = "SearXNG ничего не нашёл"
        elif mode is None:
            search_note = ("У провайдера «%s» нет встроенного поиска: "
                           "добавьте профиль OpenRouter/OpenAI/Anthropic или укажите SearXNG"
                           % provider)

    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(dialog_messages(chat.get("messages"), 10))
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": profile.get("model") or (
            PROVIDERS["anthropic"]["models"][0] if provider == "anthropic" else "gpt-4o-mini"),
        "messages": messages,
        "temperature": 0.7,
        # Длина ответа настраивается в панели (260 / 700 / 1400).
        "max_tokens": max(50, min(int(config.get("max_tokens") or 260), 2000)),
    }
    if provider == "deepseek":
        # Без «размышлений» deepseek-flash отвечает ~1.5 с вместо 10–15 с.
        payload["thinking"] = {"type": "disabled"}
    if use_search and provider == "openrouter":
        # Родной поиск OpenRouter: плагин web, источники приходят аннотациями.
        payload["plugins"] = [{"id": "web"}]
        search_source = "openrouter"

    if use_search and provider == "openai":
        try:
            answer, response_sources = _openai_web_answer(
                base_url, profile, messages, payload["max_tokens"])
            sources = response_sources
            search_source = "openai"
        except Exception as exc:  # noqa: BLE001 - упадёт, ответим без поиска
            search_note = "Родной поиск OpenAI не сработал: %s" % str(exc)[:160]
            answer = None

    if provider == "anthropic":
        # Messages API: system отдельным полем, в диалоге только user/assistant.
        dialog = [item for item in messages
                  if isinstance(item, dict) and item.get("role") in ("user", "assistant")]
        try:
            answer, usage, found = anthropic_messages(
                base_url, profile["key"], payload["model"],
                (system + "\n\n" + ANTHROPIC_SEARCH_NOTE).strip() if use_search else system,
                dialog, payload["max_tokens"], use_search)
        except ApiError as exc:
            common.fail(exc.status, exc.message, **exc.extra)
            return
        body = {"usage": usage}
        if use_search:
            # Модель ищет только когда нужно: без поиска источников нет, и это не ошибка.
            sources = found
            search_source = "anthropic"

    temperature_dropped = False
    if answer is None:
        try:
            body = _post_chat(base_url, payload, profile["key"])
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            # Новые модели (Claude 5.x и подобные) отвечают 400 на temperature.
            # Правило — исправлять payload по ошибке провайдера, а не по имени
            # модели: повторяем один раз без temperature, остальное не трогаем.
            if (exc.code == 400 and "temperature" in payload
                    and "temperature" in detail.lower()):
                payload.pop("temperature", None)
                temperature_dropped = True
                try:
                    body = _post_chat(base_url, payload, profile["key"])
                except urllib.error.HTTPError as retry_exc:
                    common.fail(502, "API ответил HTTP %s" % retry_exc.code,
                                detail=_http_detail(retry_exc)[:400])
                    return
                except (urllib.error.URLError, OSError) as retry_exc:
                    common.fail(502, "Нет связи с API: %s" % retry_exc)
                    return
            else:
                common.fail(502, "API ответил HTTP %s" % exc.code, detail=detail[:400])
                return
        except (urllib.error.URLError, OSError) as exc:
            common.fail(502, "Нет связи с API: %s" % exc)
            return

    if answer is None:
        try:
            message = body["choices"][0]["message"]
            answer = message["content"]
        except (KeyError, IndexError, TypeError):
            common.fail(502, "Неожиданный ответ API", detail=str(body)[:400])
            return
        if use_search and provider == "openrouter":
            sources = websearch.annotations(message.get("annotations"))
            if not sources and not search_note:
                search_note = "OpenRouter не вернул источники"

    answer = answer.strip()
    history = chat.setdefault("messages", [])
    stamp = int(time.time())
    history.append({"role": "user", "content": prompt, "ts": stamp})
    history.append({"role": "assistant", "content": answer, "ts": stamp})
    chat["messages"] = history[-20:]
    save_chats(chats)

    usage = (body or {}).get("usage")
    common.ok(answer=answer, model=payload["model"], provider=provider,
              profile=profile.get("name"), chat=chat.get("name"),
              chat_id=chat.get("id"), usage=usage,
              temperature_dropped=temperature_dropped,
              sources=sources, search_source=search_source,
              search_note=search_note)


def save_profile(config, data):
    profiles = config.get("profiles") or []
    pid = data.get("profile_id")
    profile = next((item for item in profiles if item.get("id") == pid), None) if pid else None

    name = str(data.get("name") or "").strip()
    provider = str(data.get("provider") or "").strip()
    if provider and provider not in PROVIDERS:
        common.fail(400, "Неизвестный провайдер", providers=list(PROVIDERS))
        return None

    if profile is None:
        base_name = name or (PROVIDERS.get(provider, {}).get("title") if provider else "Профиль")
        pid = slug(base_name)
        existing = {item.get("id") for item in profiles}
        suffix = 2
        candidate = pid
        while candidate in existing:
            candidate = "%s-%d" % (pid, suffix)
            suffix += 1
        profile = {"id": candidate, "name": base_name, "provider": provider or "openai",
                   "model": "", "base_url": "", "key": ""}
        profiles.append(profile)

    if name:
        profile["name"] = name
    if provider:
        profile["provider"] = provider
        if not data.get("base_url") and provider != "custom":
            profile["base_url"] = PROVIDERS[provider]["base_url"]

    model = str(data.get("model") or "").strip()
    if model:
        profile["model"] = model

    base_url = str(data.get("base_url") or "").strip()
    if base_url:
        if not base_url.startswith(("http://", "https://")):
            common.fail(400, "Base URL должен начинаться с http:// или https://")
            return None
        profile["base_url"] = base_url

    key = str(data.get("key") or "").strip()
    if key:
        profile["key"] = key

    config["profiles"] = profiles
    config["active"] = profile["id"]
    if "voice_enabled" in data or "enabled" in data:
        value = common.truthy(data.get("voice_enabled", data.get("enabled")))
        config["voice_enabled"] = value
        config["enabled"] = value
    return profile


CHAT_NAME_LIMIT = 40


def clean_chat_name(raw):
    """Имя чата: без лишних пробелов, кавычек и управляющих символов."""
    name = " ".join(str(raw or "").split()).strip()
    return name.strip("\"'«»“”").strip()


def unique_chat_name(items, name):
    """Не давать двум чатам одинаковое имя: «Работа», «Работа 2», …"""
    base = name
    taken = {clean_chat_name(item.get("name")).casefold() for item in items}
    index = 2
    while name.casefold() in taken:
        name = "%s %d" % (base, index)
        index += 1
    return name


def chat_switch(data):
    chats = load_chats()
    items = chats.get("chats") or []
    current = chats.get("active")
    index = next((i for i, item in enumerate(items) if item.get("id") == current), 0)

    if common.truthy(data.get("chat_new")):
        name = clean_chat_name(data.get("chat_name"))
        if len(name) > CHAT_NAME_LIMIT:
            common.fail(413, "Имя чата длиннее %d символов" % CHAT_NAME_LIMIT)
            return None
        number = len(items) + 1
        chat_id = "chat-%d" % number
        while any(item.get("id") == chat_id for item in items):
            number += 1
            chat_id = "chat-%d" % number
        chat = {"id": chat_id, "name": name or ("Чат %d" % number), "messages": []}
        chat["name"] = unique_chat_name(items, chat["name"])
        items.append(chat)
        chats["active"] = chat["id"]
    elif data.get("chat_rename") is not None:
        target = str(data.get("chat_rename") or current)
        chat = next((item for item in items if item.get("id") == target), None)
        if chat is None:
            common.fail(404, "Чат не найден: %s" % target)
            return None
        name = clean_chat_name(data.get("chat_name"))
        if not name:
            common.fail(400, "Пустое имя чата")
            return None
        if len(name) > CHAT_NAME_LIMIT:
            common.fail(413, "Имя чата длиннее %d символов" % CHAT_NAME_LIMIT)
            return None
        others = [item for item in items if item.get("id") != target]
        chat["name"] = unique_chat_name(others, name)
    elif data.get("chat_delete") is not None:
        target = str(data.get("chat_delete") or current)
        items = [item for item in items if item.get("id") != target]
        if not items:
            items = [{"id": "chat-1", "name": "Чат 1", "messages": []}]
        if chats.get("active") == target:
            chats["active"] = items[min(index, len(items) - 1)]["id"]
    elif data.get("chat_select") is not None:
        target = str(data.get("chat_select"))
        if not any(item.get("id") == target for item in items):
            common.fail(404, "Чат не найден: %s" % target)
            return None
        chats["active"] = target
    else:
        delta = 1 if common.truthy(data.get("chat_next")) else -1
        chats["active"] = items[(index + delta) % len(items)]["id"]

    chats["chats"] = items
    save_chats(chats)
    return chats


def chat_history(chat_id=None):
    """Сообщения чата (по умолчанию активного) для просмотра в панели. Только чтение."""
    chats = load_chats()
    chat = None
    if chat_id:
        chat = next((item for item in chats.get("chats") or [] if item.get("id") == chat_id), None)
        if chat is None:
            common.fail(404, "Чат не найден: %s" % chat_id)
            return
    chat = chat or active_chat(chats)
    messages = [
        {"role": "user" if item.get("role") == "user" else "assistant",
         "content": str(item.get("content") or "")}
        for item in chat.get("messages") or []
        if isinstance(item, dict)
    ][-20:]
    common.ok(chat_id=chat.get("id"), chat_name=chat.get("name"), messages=messages,
              limit=20)


def main():
    data = common.params()
    if common.truthy(data.get("history")):
        chat_history(str(data.get("chat") or "").strip() or None)
        return
    action_keys = ("prompt", "enabled", "voice_enabled", "activate", "delete", "provider", "name", "profile_id",
                   "max_tokens", "system_prompt", "chat_next", "chat_prev", "chat_new", "chat_select",
                   "chat_delete", "chat_rename", "web_search", "search_url", "follow_up",
                   "location", "timezone")
    if common.truthy(data.get("info")) or not any(key in data for key in action_keys):
        common.respond(200, info())
        return

    config = load_config()

    if data.get("max_tokens") is not None:
        config["max_tokens"] = common.to_int(data.get("max_tokens"), 260, 50, 2000)
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("system_prompt") is not None:
        prompt = str(data.get("system_prompt") or "").strip()
        if len(prompt) > 4000:
            common.fail(413, "Промпт длиннее 4000 символов")
            return
        config["system_prompt"] = prompt
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("web_search") is not None:
        config["web_search"] = common.truthy(data.get("web_search"))
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("search_url") is not None:
        url = str(data.get("search_url") or "").strip()
        if url:
            try:
                common.url_guard(url, allow_http=True, allow_private=True,
                                 allow_loopback=False)
            except ValueError as exc:
                common.fail(400, "Некорректный адрес SearXNG: %s" % exc)
                return
            url = url.rstrip("/")
        config["search_url"] = url
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("follow_up") is not None:
        config["follow_up"] = common.truthy(data.get("follow_up"))
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("location") is not None or data.get("timezone") is not None:
        if (os.environ.get("REQUEST_METHOD") or "GET").upper() != "POST":
            common.fail(405, "Действие требует POST")
            return
        if data.get("location") is not None:
            location = " ".join(str(data.get("location") or "").split())
            if len(location) > LOCATION_LIMIT:
                common.fail(413, "Местоположение длиннее %d символов" % LOCATION_LIMIT)
                return
            config["location"] = location
        if data.get("timezone") is not None:
            tz_name = str(data.get("timezone") or "").strip()
            if tz_name and zone(tz_name) is None:
                common.fail(400, "Неизвестный часовой пояс: %s" % tz_name,
                            hint="Формат IANA, например Europe/Moscow или Asia/Yekaterinburg")
                return
            config["timezone"] = tz_name
        save_config(config)
        common.respond(200, info(config))
        return

    if any(key in data for key in ("chat_next", "chat_prev", "chat_new", "chat_select",
                                   "chat_delete", "chat_rename")):
        if chat_switch(data) is None:
            return
        common.respond(200, info(config))
        return

    if data.get("activate") is not None:
        pid = str(data.get("activate") or "").strip()
        if not any(item.get("id") == pid for item in config.get("profiles") or []):
            common.fail(404, "Профиль не найден: %s" % pid)
            return
        config["active"] = pid
        if "voice_enabled" in data or "enabled" in data:
            value = common.truthy(data.get("voice_enabled", data.get("enabled")))
            config["voice_enabled"] = value
            config["enabled"] = value
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("delete") is not None:
        pid = str(data.get("delete") or "").strip()
        config["profiles"] = [item for item in config.get("profiles") or [] if item.get("id") != pid]
        if config.get("active") == pid:
            config["active"] = config["profiles"][0]["id"] if config["profiles"] else None
        save_config(config)
        common.respond(200, info(config))
        return

    if data.get("prompt") is not None:
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            common.fail(400, "Пустой запрос")
            return
        if len(prompt) > 4000:
            common.fail(413, "Запрос длиннее 4000 символов")
            return
        search = None
        if data.get("search") is not None:
            search = common.truthy(data.get("search"))
        ask(prompt, data.get("system"), search)
        return

    # только тумблер голосовых вопросов, без правки профиля
    if ("voice_enabled" in data or "enabled" in data) and not any(
            key in data for key in ("provider", "name", "profile_id")):
        value = common.truthy(data.get("voice_enabled", data.get("enabled")))
        config["voice_enabled"] = value
        config["enabled"] = value
        save_config(config)
        common.respond(200, info(config))
        return

    profile = save_profile(config, data)
    if profile is None:
        return
    save_config(config)
    common.respond(200, info(config))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            common.fail(500, "Внутренняя ошибка llm.py: %s" % exc)
        except Exception:
            pass
