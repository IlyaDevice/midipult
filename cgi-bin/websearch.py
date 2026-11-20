#!/opt/bin/python3
# -*- coding: utf-8 -*-
"""Вспомогательное для веб-поиска LLM: очистка вопроса, свой SearXNG, контекст.

Родной поиск делают сами провайдеры:
- OpenRouter — плагин `web` (в ответе приходят аннотации url_citation);
- OpenAI — Responses API с инструментом `web_search`.
Этот модуль нужен только для провайдеров без встроенного поиска: если в
настройках указан свой SearXNG, запрос уходит туда (JSON API). Никакого
скрейпинга поисковой выдачи: если поиска нет — панель честно сообщает об этом.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
import urllib.request

UA = "MidiPult/1.0 (+https://github.com/RoBiN19666/midipult)"
MAX_RESULTS = 5
MAX_PAGE = 400_000

FILLER = frozenset("такой такая такое такие нового новое новые новый "
                   "сегодня сейчас пожалуйста".split())


def _fetch(url, timeout=8, accept="application/json"):
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
        "Accept": accept,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_PAGE).decode("utf-8", "replace")


def _clean(text, limit=240):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = html_mod.unescape(text)
    text = " ".join(text.split())
    text = re.sub(r"\s+([.,;:!?)»])", r"\1", text)
    return text[:limit]


def query_from_prompt(prompt):
    """Из вопроса «Какая сегодня погода в Екатеринбурге? Ответь кратко» —
    поисковый запрос. Предлоги и падежи не трогаем."""
    text = " ".join(str(prompt or "").split())
    text = re.sub(r"^(алиса|алеса)[,!.\s]+", "", text, flags=re.I)
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


def search_searx(query, base, limit=MAX_RESULTS, timeout=8):
    """Свой SearXNG (JSON API). Возвращает список {title,url,snippet}."""
    url = str(base or "").rstrip("/") + "/search?" + urllib.parse.urlencode(
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


def annotations(items, limit=8):
    """Аннотации провайдера (url_citation) → список источников."""
    from_provider = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        citation = item.get("url_citation")
        source = citation if isinstance(citation, dict) else item
        url = str(source.get("url") or source.get("uri") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        from_provider.append({
            "title": _clean(source.get("title"), 120) or url,
            "url": url,
            "snippet": _clean(source.get("content") or source.get("snippet"), 240),
        })
    seen = set()
    unique = []
    for item in from_provider:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def context(results, query):
    """Текст для system-промпта: короткая выжимка со ссылками (SearXNG)."""
    if not results:
        return ""
    lines = ["Свежие данные из интернета по запросу «%s» "
             "(могут быть неполными или устаревшими):" % query]
    for index, item in enumerate(results[:MAX_RESULTS], 1):
        lines.append("%d. %s — %s [%s]" % (
            index, item.get("title") or "—",
            item.get("snippet") or "без описания", item.get("url") or ""))
    lines.append("Ответь по этим данным. Если данных мало или они противоречат "
                 "вопросу — скажи об этом прямо, не выдумывай факты и ссылки.")
    return "\n".join(lines)
