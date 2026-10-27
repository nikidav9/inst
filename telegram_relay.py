#!/usr/bin/env python3
"""Страницы Telegram-каналов — снаружи на сервер (27.10.2026).

С сервера jobtoo.ru (Россия) t.me не открывается, поэтому витрины каналов
t.me/s/<канал> скачивает машина GitHub и присылает их сжатыми на
ingest.php?kind=telegram_relay. Дальше всё как раньше: ingest.php → telegram.php
берёт присланную страницу вместо похода в t.me (telegram_source.php, tg_relay_*).

Канал дочитывается назад (?before=), пока посты не станут старше прошлой
присылки (минус сутки) или 30 дней, не больше 15 страниц — как в telegram.php.
Только stdlib. RELAY_KEY (в inst) или ADMIN_TOKEN — из секретов; --dry-run ничего не отправляет.
"""
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

API = 'https://jobtoo.ru/api/ingest.php?kind=telegram_relay'
UA = 'JobToo/1.0 (+https://jobtoo.ru; support@jobtoo.ru)'
KEEP_DAYS = 30
BACK_PAGES = 15
HANDLE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{3,31}$')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Закрытый или переименованный канал t.me уводит на t.me/<канал> без /s/ —
    # это не витрина постов (как CURLOPT_FOLLOWLOCATION => false в telegram.php).
    def redirect_request(self, *a, **k):
        return None


_tme = urllib.request.build_opener(NoRedirect)


def tme_get(url):
    for attempt in range(3):
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html', 'Accept-Language': 'ru'})
        try:
            with _tme.open(req, timeout=30) as r:
                return r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5 * (attempt + 1))
    return None


def oldest_post(html, handle):
    """Самый старый пост страницы (id, unix) или None — как tg_oldest_post."""
    best = None
    for part in re.split(r'(?=<div class="tgme_widget_message_wrap)', html):
        m = re.search(r'data-post="([A-Za-z0-9_]+)/(\d+)"', part)
        if not m or m.group(1).lower() != handle.lower():
            continue
        d = re.search(r'<time[^>]*datetime="([^"]+)"', part)
        try:
            ts = int(datetime.fromisoformat(d.group(1)).timestamp()) if d else 0
        except ValueError:
            ts = 0
        if best is None or int(m.group(2)) < best[0]:
            best = (int(m.group(2)), ts)
    return best


def fetch_channel(handle, since):
    html = tme_get('https://t.me/s/' + handle)
    if html is None or 'tgme_channel_info' not in html:
        return None, 0
    cutoff = max(time.time() - KEEP_DAYS * 86400, since - 86400)
    pages = 1
    for _ in range(BACK_PAGES):
        o = oldest_post(html, handle)
        if o is None or o[1] < cutoff:
            break
        more = tme_get('https://t.me/s/%s?before=%d' % (handle, o[0]))
        n = oldest_post(more, handle) if more else None
        if n is None or n[0] >= o[0]:
            break
        html = more + html
        pages += 1
    return html, pages


def api(url, token, data=None):
    # RELAY_KEY — ключ только для присылки (inst); ADMIN_TOKEN — полный админский.
    auth = {'X-Relay-Key': token} if os.environ.get('RELAY_KEY') else {'X-Admin-Token': token}
    req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET',
                                 headers={**auth, 'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    dry = '--dry-run' in sys.argv
    # strip: секрет, вставленный из файла, приходит с переводом строки на конце.
    token = (os.environ.get('RELAY_KEY') or os.environ.get('ADMIN_TOKEN', '')).strip()
    handles = [a for a in sys.argv[1:] if not a.startswith('--')]
    if handles:
        channels = [{'handle': h, 'since': 0} for h in handles]
    else:
        if not token:
            print('RELAY_KEY (или ADMIN_TOKEN) не задан'); return 1
        channels = api(API, token)['channels']
    channels = [c for c in channels if HANDLE_RE.match(str(c.get('handle', '')))]

    def one(c):
        h = c['handle']
        html, pages = fetch_channel(h, int(c.get('since') or 0))
        if html is None:
            return h, 't.me не отдал витрину'
        if dry:
            return h, 'ок (без отправки): страниц %d, %d КБ' % (pages, len(html) // 1024)
        try:
            api(API + '&handle=' + h, token, gzip.compress(html.encode('utf-8')))
            return h, 'ок: страниц %d' % pages
        except urllib.error.HTTPError as e:
            return h, 'сервер ответил %d: %s' % (e.code, e.read()[:200].decode('utf-8', 'replace'))
        except (urllib.error.URLError, TimeoutError) as e:
            return h, 'сервер недоступен: %s' % e

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, channels))
    bad = [r for r in results if not r[1].startswith('ок')]
    print('Каналов: %d, отправлено: %d, не вышло: %d' % (len(results), len(results) - len(bad), len(bad)))
    for h, s in (results if dry else bad)[:40]:
        print(' ', h, '—', s)
    # Красное, только если не вышло ни одного: единичные сбои t.me доберёт следующий заход.
    return 1 if results and len(bad) == len(results) else 0


if __name__ == '__main__':
    sys.exit(main())
