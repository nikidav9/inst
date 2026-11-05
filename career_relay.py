#!/usr/bin/env python3
"""Страницы карьерных сайтов работодателей — снаружи на сервер (ноябрь 2026).

С сервера jobtoo.ru (Россия) запросы к чужим сайтам создают многочасовую
нагрузку, а часть зарубежных систем найма (Greenhouse, Ashby, Lever)
блокирует российские IP. В открытом репозитории nikidav9/inst машины
GitHub бесплатны и имеют быстрый международный канал: здесь страницы
карьерных сайтов скачиваются и отправляются на jobtoo.ru сжатыми (gzip)
на ingest.php?kind=career_relay.
Дальше career.php и ingest.php берут уже скачанные страницы из локального
кэша диска за 0 мс без ожидания внешних HTTP-запросов.

Только stdlib. RELAY_KEY (в inst) или ADMIN_TOKEN — из секретов;
--dry-run ничего не отправляет.
"""
import gzip
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = 'https://jobtoo.ru/api/ingest.php?kind=career_relay'
UA = 'JobToo/1.0 (+https://jobtoo.ru; support@jobtoo.ru)'

# Сайты с сертификатом Минцифры РФ: стандартный системный Python без этих
# корней выдаёт CERTIFICATE_VERIFY_FAILED. Для них используем unverified SSL context.
RU_CA_DOMAINS = ('tbank.ru', 'alfabank.ru', 'tochka.com', 'nspk.ru', 'ptsecurity.com', 'rosatom-career.ru')

_default_ctx = ssl.create_default_context()
_unverified_ctx = ssl._create_unverified_context()


def get_ssl_context(url: str) -> ssl.SSLContext:
    host = urllib.parse.urlsplit(url).hostname or ''
    host = host.lower()
    for dom in RU_CA_DOMAINS:
        if host == dom or host.endswith('.' + dom):
            return _unverified_ctx
    return _default_ctx


def http_get(url: str, post: bool = False, body: str = None, headers: list = None, xhr: bool = False):
    """Делает запрос к внешнему сайту с повторными попытками."""
    ctx = get_ssl_context(url)
    req_headers = {
        'User-Agent': UA,
        'Accept': 'application/json' if not post else 'application/json, text/plain, */*',
        'Accept-Language': 'ru,en;q=0.9',
    }
    if xhr:
        req_headers['X-Requested-With'] = 'XMLHttpRequest'
    if headers:
        for h in headers:
            if isinstance(h, str) and ': ' in h:
                k, v = h.split(': ', 1)
                req_headers[k] = v

    data = None
    if post:
        req_headers['Content-Type'] = 'application/json'
        data = (body or '{}').encode('utf-8')

    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers=req_headers, method='POST' if post else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                content = resp.read()
                return content
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            if e.code != 429 and e.code < 500:
                return None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(3 * (attempt + 1))
    return None


def api(url: str, token: str, data: bytes = None):
    auth = {'X-Relay-Key': token} if os.environ.get('RELAY_KEY') else {'X-Admin-Token': token}
    headers = {**auth, 'Content-Type': 'application/octet-stream'}
    req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET', headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def with_query_param(url: str, param: str, val) -> str:
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query)
    q = [(k, v) for k, v in q if k != param]
    q.append((param, str(val)))
    new_query = urllib.parse.urlencode(q)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def fetch_and_send_unit(u: dict, token: str, dry: bool = False):
    base_url = u['url']
    post = bool(u.get('post'))
    body = u.get('body')
    headers = u.get('headers')
    xhr = bool(u.get('xhr'))
    paging = u.get('paging') or {}

    pages_sent = 0
    pages_to_fetch = [base_url]

    ptype = paging.get('type')
    param = paging.get('param')
    start = int(paging.get('start') or 1)
    limit = int(paging.get('limit') or 50)
    max_pages = min(int(paging.get('max_pages') or 20), 30)

    # Если есть пагинация типа page:
    if ptype == 'page' and param:
        # Для начала берем первые max_pages страниц
        for p in range(start + 1, start + max_pages):
            pages_to_fetch.append(with_query_param(base_url, param, p))
    elif ptype == 'offset' and param and limit:
        for offset in range(limit, limit * max_pages, limit):
            pages_to_fetch.append(with_query_param(base_url, param, offset))

    host = urllib.parse.urlsplit(base_url).hostname or 'unknown'

    for idx, page_url in enumerate(pages_to_fetch):
        content = http_get(page_url, post=post, body=body, headers=headers, xhr=xhr)
        if not content:
            # Если страница 0 не ответила — ошибка, если последующие страницы — конец списка
            if idx == 0:
                return host, 'сайт не ответил на первый запрос', pages_sent
            break

        # Проверка на пустой ответ для последующих страниц
        if idx > 0 and len(content) < 50:
            break

        if dry:
            pages_sent += 1
            continue

        gz = gzip.compress(content)
        try:
            target_url = API + '&url=' + urllib.parse.quote_plus(page_url)
            if post and body:
                target_url += '&post_body=' + urllib.parse.quote_plus(body)
            res = api(target_url, token, gz)
            if res.get('ok'):
                pages_sent += 1
            else:
                return host, f'ошибка сервера: {res.get("error")}', pages_sent
        except Exception as e:
            return host, f'сбой отправки: {e}', pages_sent

    return host, f'ок: страниц {pages_sent}', pages_sent


def main():
    dry = '--dry-run' in sys.argv
    token = (os.environ.get('RELAY_KEY') or os.environ.get('ADMIN_TOKEN', '')).strip()
    if not token and not dry:
        print('RELAY_KEY (или ADMIN_TOKEN) не задан')
        return 1

    try:
        data = api(API, token)
        endpoints = data.get('endpoints') or []
    except Exception as e:
        print(f'Не удалось получить список endpoints от сервера: {e}')
        return 1

    if not endpoints:
        print('Список карьерных endpoints пуст')
        return 0

    print(f'Получено карьерных эндпоинтов: {len(endpoints)}')

    def worker(u):
        h, status, pages = fetch_and_send_unit(u, token, dry=dry)
        return h, status, pages

    total_pages = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, endpoints))

    bad = [r for r in results if not r[1].startswith('ок')]
    total_pages = sum(r[2] for r in results)

    print('Эндпоинтов: %d, страниц отправлено: %d, сбоев: %d' % (len(results), total_pages, len(bad)))
    for h, s, _ in (results if dry else bad)[:40]:
        print(' ', h, '—', s)

    return 1 if results and len(bad) == len(results) else 0


if __name__ == '__main__':
    sys.exit(main())
