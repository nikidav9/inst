#!/usr/bin/env python3
"""LinkedIn: публичный гостевой поиск вакансий -> фид в формате docs/feed-format.md.

РЕШЕНИЕ ВЛАДЕЛЬЦА 06.11.2026 (docs/linkedin.md): LinkedIn запрещает автоматический доступ
(robots.txt, User Agreement 8.2); владелец принял риск. Поэтому здесь ограничения:
  * только поля самой вакансии: название, компания, город, дата, ссылка. Данные
    рекрутёров не берём и не храним;
  * честный User-Agent (под браузер не маскируемся), пауза между запросами, потолок
    запросов за запуск; с 12.11.2026 список поисков делится на 8 кусков, каждый запуск (раз в 3 часа)
    берёт свой, за сутки ~96 запросов;
  * первая же блокировка (429/999/403, страница входа) останавливает запуск, прошлый
    фид остаётся как был.
Только стандартная библиотека. Результат: LINKEDIN_FEED (nginx отдаёт его как
https://jobtoo.ru/feeds/linkedin.json, источник `linkedin` в jm_ext_sources).
"""
import gzip
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
# Только латиница: HTTP-заголовок кодируется в latin-1, с русскими буквами сборщик падал на каждом
# запросе (UnicodeEncodeError, видно в /linkedin-status.json 08.11.2026) и ни разу не дошёл до LinkedIn.
UA = "JobTooBot/1.0 (+https://jobtoo.ru; job listings for candidates)"
OUT = os.environ.get("LINKEDIN_FEED", "/var/www/html/linkedin-feed.json")
# Режим присылки (12.11.2026, решение владельца): с московского сервера LinkedIn не открывается
# (сброс соединения), поэтому сборщик запускается в GitHub Actions (nikidav9/inst, адрес вне России)
# и шлёт готовый фид на сервер. Ключ присылки — секрет RELAY_KEY. Без LINKEDIN_RELAY_URL — прежний режим.
RELAY_URL = os.environ.get("LINKEDIN_RELAY_URL", "")
RELAY_KEY = os.environ.get("RELAY_KEY", "").strip()  # секрет из GitHub с \n на конце: http.client отвергает такой заголовок
# Поиски двух групп (решение владельца 12.11.2026): российская лента (region «ru») и раздел «За рубежом»
# (region «abroad»). Окно — месяц: за неделю выдача слишком мала (первый запуск дал ~6 вакансий на фразу).
RU_KEYWORDS = [
    "python developer", "frontend developer", "backend developer", "product manager", "data analyst",
    "marketing manager", "project manager", "qa engineer", "devops engineer", "ux ui designer",
    "business analyst", "hr manager",
]
RU_PLACES = ["Russia", "Moscow"]
ABROAD_KEYWORDS = ["python developer", "frontend developer", "product manager", "data analyst", "qa engineer", "project manager"]
ABROAD_PLACES = [
    "Cyprus", "Tbilisi, Georgia", "Yerevan, Armenia", "Belgrade, Serbia",
    "Almaty, Kazakhstan", "Istanbul, Turkey", "Dubai, United Arab Emirates", "Berlin, Germany",
]
RU_PAGES = 2                 # по 25 карточек на страницу
ABROAD_PAGES = 1
PAUSE_SEC = 8.0              # между запросами; реальная пауза 1,0-1,5 от этого значения
MAX_REQUESTS = 12            # потолок за запуск
SLOTS = 8                    # запусков в сутки (каждые 3 часа): за сутки проходит весь список
MAX_AGE = "r2592000"         # за месяц


def pages() -> list:
    """Весь список страниц поиска: (слова, место, region, start). Российские и зарубежные поиски чередуются
    (на один российский — два зарубежных), чтобы каждый кусок списка, то есть каждый запуск, брал и то и другое."""
    ru = [[(kw, loc, "ru", p * 25) for p in range(RU_PAGES)] for kw in RU_KEYWORDS for loc in RU_PLACES]
    ab = [[(kw, loc, "abroad", p * 25) for p in range(ABROAD_PAGES)] for kw in ABROAD_KEYWORDS for loc in ABROAD_PLACES]
    out = []
    for i, group in enumerate(ru):
        out += group
        for g in ab[2 * i:2 * i + 2]:
            out += g
    for g in ab[2 * len(ru):]:      # если зарубежных больше, чем вмещает чередование
        out += g
    return out


def current_slot(now: float = None) -> int:
    """Номер куска списка для этого запуска. LINKEDIN_SLOT задаёт вручную; иначе по часу UTC (запуски в 0, 3, 6 ... ч)."""
    env = os.environ.get("LINKEDIN_SLOT", "")
    if env.strip().isdigit():
        return int(env) % SLOTS
    return int((now if now is not None else time.time()) // 3600 // (24 // SLOTS)) % SLOTS


def todays_slice(slot: int) -> list:
    allp = pages()
    size = -(-len(allp) // SLOTS)
    return allp[slot * size:(slot + 1) * size][:MAX_REQUESTS]


class Blocked(Exception):
    pass


_CARD = re.compile(r"<li\b.*?</li>", re.S)


def _text(pattern, chunk):
    m = re.search(pattern, chunk, re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()) if m else ""


def parse_cards(page: str) -> list:
    """Карточки гостевого поиска -> вакансии фида. Битую карточку пропускает."""
    out = []
    for chunk in _CARD.findall(page or ""):
        m = re.search(r"jobPosting:(\d{6,})", chunk) or re.search(r"/jobs/view/[^\"?]*?-(\d{6,})", chunk)
        title = _text(r'base-search-card__title[^>]*>(.*?)</h3>', chunk)
        if not m or not title:
            continue
        job_id = m.group(1)
        company = _text(r'base-search-card__subtitle[^>]*>(.*?)</h4>', chunk)
        place = _text(r'job-search-card__location[^>]*>(.*?)</span>', chunk)
        d = re.search(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})', chunk)
        item = {
            "id": "li-" + job_id, "title": title[:200], "kind": "permanent",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}", "active": True,
        }
        if company:
            item["company"] = company[:200]
        if place:
            item["address"] = place[:200]
        if d:
            item["date"] = d.group(1)
        out.append(item)
    return out


def fetch(keywords: str, location: str, start: int) -> str:
    qs = urllib.parse.urlencode({"keywords": keywords, "location": location, "f_TPR": MAX_AGE, "start": start})
    req = urllib.request.Request(SEARCH + "?" + qs, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 999) or e.code >= 500:
            raise Blocked(f"HTTP {e.code}")
        return ""
    except (urllib.error.URLError, OSError) as e:
        # 09.11.2026: «Connection reset by peer» — LinkedIn рвёт соединение с адреса сервера, то есть
        # блокировка. Это тоже остановка запуска, а не падение со стеком; обходить её (прокси, маскировка)
        # нельзя — решение владельца 06.11.2026.
        raise Blocked(f"сеть: {type(e).__name__} {getattr(e, 'reason', e)}")
    if "authwall" in body[:3000].lower() or "/login" in body[:500].lower():
        raise Blocked("страница входа")
    return body


def send(feed: dict, requests: int) -> int:
    """Прислать фид на сервер (ingest.php?kind=linkedin_relay), gzip, ключ присылки."""
    if len(RELAY_KEY) < 32:
        print("нет RELAY_KEY: присылать нечем", file=sys.stderr)
        return 1
    body = gzip.compress(json.dumps(feed, ensure_ascii=False).encode("utf-8"))
    req = urllib.request.Request(RELAY_URL, data=body, method="POST", headers={
        "User-Agent": UA, "X-Relay-Key": RELAY_KEY, "Content-Type": "application/gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        print(f"сервер отказал: HTTP {e.code} {e.read(300).decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"сервер недоступен: {e}", file=sys.stderr)
        return 1
    print(f"фид прислан: {len(feed['items'])} вакансий, запросов к LinkedIn: {requests}, ответ сервера {code}")
    return 0


def run() -> int:
    seen, requests, blocked = {}, 0, None
    dead = set()                 # поиски, у которых страница вышла короткой: дальше страниц нет
    try:
        for kw, loc, region, start in todays_slice(current_slot()):
            if (kw, loc) in dead:
                continue
            if requests >= MAX_REQUESTS:
                break
            if requests:
                time.sleep(PAUSE_SEC * random.uniform(1.0, 1.5))
            requests += 1
            cards = parse_cards(fetch(kw, loc, start))
            for c in cards:
                seen.setdefault(c["id"], dict(c, region=region))
            if len(cards) < 10:
                dead.add((kw, loc))
    except Blocked as e:
        blocked = str(e)
    if blocked:
        print(f"остановлено: {blocked}; прежний фид не тронут", file=sys.stderr)
        # Блокировка — ожидаемая остановка по решению владельца, а не поломка: служба не «failed».
        # В Actions (режим присылки) её видно красным запуском: молча зелёный прогон ничего не принёс.
        return 3 if RELAY_URL else 0
    if not seen:
        print("ничего не нашли; прежний фид не тронут", file=sys.stderr)
        return 1
    feed = {"items": list(seen.values()), "has_more": False}
    if RELAY_URL:
        return send(feed, requests)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"фид: {len(seen)} вакансий, запросов: {requests}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
