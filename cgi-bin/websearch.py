#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Веб-поиск для нейросети: короткая выжимка из интернета.

Kлючи не нужны: по умолчанию разбирается выдача Bing (HTML), при неудаче —
DuckDuckGo Instant Answer. Если в настройках указан свой SearXNG, используется
он (JSON API). Результаты — только заголовок, ссылка и сниппет, чтобы не
раздувать контекст и не тянуть в модель чужие инструкции.

Модуль не CGI: его импортирует llm.py. Сетевые запросы идут через
urllib с таймаутом; ошибки не валят ответ LLM — поиск просто пропускается.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MAX_RESULTS = 5
MAX_PAGE = 600_000


def _fetch(url, timeout=8):
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_PAGE).decode("utf-8", "replace")


def _clean(text, limit=240):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_mod.unescape(text)
    text = " ".join(text.split())
    # Тег внутри предложения оставлял пробел перед знаком препинания.
    text = re.sub(r"\s+([.,;:!?)»])", r"\1", text)
    return text[:limit]


def _decode_bing(href):
    """Bing отдаёт ссылки через /ck/a?u=a1<base64> — достаём настоящий адрес."""
    if "bing.com/ck/a" not in href:
        return href
    query = urllib.parse.urlsplit(href).query
    value = (urllib.parse.parse_qs(query).get("u") or [""])[-1]
    if not value:
        return href
    payload = value[2:] if value.startswith("a1") else value
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return href
    return decoded if decoded.startswith(("http://", "https://")) else href


def _bing_rss(query, limit, timeout):
    """Bing умеет отдавать выдачу как RSS: чище и без редиректов."""
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "format": "rss", "setlang": "ru", "cc": "ru"})
    page = _fetch(url, timeout)
    results = []
    for item in re.findall(r"<item>(.*?)</item>", page, re.S):
        title = re.search(r"<title>(.*?)</title>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        desc = re.search(r"<description>(.*?)</description>", item, re.S)
        url_value = html_mod.unescape(link.group(1).strip()) if link else ""
        if not title or not url_value.startswith(("http://", "https://")):
            continue
        results.append({
            "title": _clean(title.group(1), 120),
            "url": url_value,
            "snippet": _clean(desc.group(1) if desc else "", 240),
        })
        if len(results) >= limit:
            break
    return results


def _bing_html(query, limit, timeout):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "ru", "cc": "ru"})
    page = _fetch(url, timeout)
    results = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        match = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                          block, re.S)
        if not match:
            continue
        link = _decode_bing(html_mod.unescape(match.group(1)))
        title = _clean(match.group(2), 120)
        if not title or not link.startswith(("http://", "https://")):
            continue
        snippet = ""
        paragraph = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if paragraph:
            snippet = _clean(paragraph.group(1), 240)
        if not snippet:
            # У части выдачи сниппет лежит не в <p>, а в div'ах. Берём текст
            # блока после заголовка и обрезаем служебное «домен › путь».
            text = _clean(block, 900)
            position = text.find(title[:24]) if title else -1
            rest = text[position + len(title):] if position >= 0 else text
            snippet = _clean(rest, 240).lstrip(" —–-·|")
        results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _bing(query, limit, timeout):
    """RSS-выдача точнее HTML; HTML остаётся запасным вариантом."""
    try:
        results = _bing_rss(query, limit, timeout)
    except Exception:  # noqa: BLE001 - попробуем HTML
        results = []
    if results:
        return results
    return _bing_html(query, limit, timeout)


def _ddg(query, limit, timeout):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
        "q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    data = json.loads(_fetch(url, timeout))
    results = []
    if data.get("AbstractText"):
        results.append({
            "title": _clean(data.get("Heading") or query, 120),
            "url": str(data.get("AbstractURL") or ""),
            "snippet": _clean(data.get("AbstractText"), 300),
        })
    for item in (data.get("RelatedTopics") or []):
        if not isinstance(item, dict) or item.get("Topics"):
            continue
        text = str(item.get("Text") or "")
        results.append({
            "title": _clean(text, 90),
            "url": str(item.get("FirstURL") or ""),
            "snippet": _clean(text, 240),
        })
        if len(results) >= limit:
            break
    return [item for item in results
            if item["url"].startswith(("http://", "https://"))][:limit]


def _searx(query, limit, base, timeout):
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "language": "ru"})
    data = json.loads(_fetch(url, timeout))
    results = []
    for item in (data.get("results") or []):
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or "")
        if not link.startswith(("http://", "https://")):
            continue
        results.append({
            "title": _clean(item.get("title"), 120),
            "url": link,
            "snippet": _clean(item.get("content"), 240),
        })
        if len(results) >= limit:
            break
    return results


FILLER = frozenset("такой такая такое такие нового новое новые новый "
                   "сегодня сейчас пожалуйста".split())


def query_from_prompt(prompt):
    """Из вопроса «Какая сегодня погода в Екатеринбурге? Ответь кратко» —
    поисковый запрос. Предлоги и падежи не трогаем: Bing на «погода в …»
    отвечает верно, а на «погода …бурге» путается."""
    text = " ".join(str(prompt or "").split())
    text = re.sub(r"^(алиса|алеса)[,!.\s]+", "", text, flags=re.I)
    # Хвосты-инструкции отрезаем до первого знака конца предложения.
    text = re.split(r"\b(?:ответь|скажи|напиши|кратко|подробно|"
                    r"одной фразой|одним предложением)\b", text, maxsplit=1,
                    flags=re.I)[0]
    text = re.split(r"[?.!]\s|$", text)[0].strip(" ?.!,")
    text = re.sub(r"^(?:что|какие|какой)\s+нового(?:\s+в)?\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:(?:какой|какая|какое|какие|что|кто|где|когда|сколько|"
                  r"почему|зачем|как|расскажи|покажи|подскажи)\s+)+", "",
                  text, flags=re.I)
    text = re.sub(r"\b(поищи|найди|посмотри|погугли|в интернете|в сети)\b",
                  " ", text, flags=re.I)
    words = [word.strip(",;:—-–") for word in text.split()]
    words = [word for word in words
             if word and word.lower() not in FILLER]
    return " ".join(words[:10]).strip(" ,;-—–")[:120]


def search(query, limit=MAX_RESULTS, searx_url="", timeout=8):
    """Найти до `limit` результатов. Возвращает {"results": [...], "source": ...}."""
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return {"results": [], "source": None, "error": "пустой запрос"}

    errors = []
    attempts = []
    if searx_url:
        attempts.append(("searx", lambda: _searx(query, limit, searx_url, timeout)))
    attempts.extend([
        ("bing", lambda: _bing(query, limit, timeout)),
        ("ddg", lambda: _ddg(query, limit, timeout)),
    ])

    for name, fetch in attempts:
        try:
            results = fetch()
        except Exception as exc:  # noqa: BLE001 - сеть и разбор не должны валить LLM
            errors.append("%s: %s" % (name, str(exc)[:120]))
            continue
        if results:
            return {"results": results, "source": name, "error": ""}
        errors.append("%s: пусто" % name)

    return {"results": [], "source": None, "error": "; ".join(errors)[:300]}


def context(results, query):
    """Текст для system-промпта: короткая выжимка с источниками."""
    if not results:
        return ""
    lines = ["Свежие данные из интернета по запросу «%s» "
             "(могут быть неполными или устаревшими):" % query]
    for index, item in enumerate(results[:MAX_RESULTS], 1):
        snippet = item.get("snippet") or ""
        lines.append("%d. %s — %s [%s]" % (index, item.get("title") or "—",
                                           snippet or "без описания", item.get("url") or ""))
    lines.append("Ответь по этим данным. Если данных мало или они противоречат "
                 "вопросу — скажи об этом прямо, не выдумывай факты и ссылки.")
    return "\n".join(lines)
