#!/usr/bin/env python3
"""Недельный отчёт по SEO-страницам jobtoo.ru (только чтение, публичные адреса, секретов нет).

Берёт карту сайта (/sitemaps/index.xml), проверяет ВСЕ страницы из main.xml и выборку из остальных частей
(по умолчанию 300 адресов на часть, детерминированно по дню — каждый запуск смотрит другой срез) и считает:
  • страницы не с кодом 200;
  • дубли <title> (одинаковый title у разных адресов);
  • canonical не равен самому адресу;
  • description длиннее 160 знаков, короче 100, оканчивающийся «…» / «….» или без точки в конце;
  • нет <h1> или их больше одного;
  • title без «| JobToo».
Печатает сводку и до 15 примеров по каждой проблеме; код выхода 0 (отчёт, а не сторож), 2 — если не удалось прочитать карту.
Запуск: python3 scripts/seo-weekly-report.py [--sample 300] [--base https://jobtoo.ru] [--json out.json]
"""
import argparse, collections, concurrent.futures as cf, datetime, hashlib, html, json, re, sys, urllib.request

UA = 'Mozilla/5.0 (compatible; jobtoo-seo-weekly/1.0)'


def get(url, timeout=40):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, ''
    except Exception as e:  # сеть: считаем отдельной проблемой, а не падаем
        return 0, ''


def locs(xml):
    return [html.unescape(u) for u in re.findall(r'<loc>([^<]+)</loc>', xml)]


def tag_attr(body, tag, key, val, want):
    """Значение атрибута want у первого тега <tag>, где key="val" (порядок атрибутов и data-rh не важны)."""
    for m in re.finditer(r'<%s\b[^>]*>' % tag, body):
        t = m.group(0)
        if re.search(r'\b%s="%s"' % (key, re.escape(val)), t):
            w = re.search(r'\b%s="([^"]*)"' % want, t)
            if w:
                return w.group(1)
    return ''


def analyze(url, status, body):
    """Проблемы одной страницы: список кодов. Чистая функция — проверяется тестом без сети."""
    probs = []
    if status != 200:
        return ['код_%s' % status], {}
    t = re.search(r'<title[^>]*>(.*?)</title>', body, re.S)
    title = html.unescape(t.group(1).strip()) if t else ''
    desc = html.unescape(tag_attr(body, 'meta', 'name', 'description', 'content').strip())
    canon = html.unescape(tag_attr(body, 'link', 'rel', 'canonical', 'href'))
    h1 = re.findall(r'<h1[^>]*>', body)
    if not title:
        probs.append('нет_title')
    elif not title.endswith('| JobToo') and 'JobToo' not in title:
        probs.append('title_без_бренда')
    if canon.rstrip('/') != url.rstrip('/'):
        probs.append('canonical_не_сам')
    if not desc:
        probs.append('нет_description')
    else:
        if len(desc) > 160:
            probs.append('description_длиннее_160')
        if len(desc) < 100:
            probs.append('description_короче_100')
        if desc.endswith('…') or desc.endswith('….') or '…' in desc[:-1]:
            probs.append('description_с_многоточием')
        elif not desc.endswith(('.', '!', '?')):
            probs.append('description_без_точки')
    if len(h1) == 0:
        probs.append('нет_h1')
    elif len(h1) > 1:
        probs.append('несколько_h1')
    return probs, {'title': title, 'desc': desc, 'canon': canon}


def pick(urls, n, seed):
    """Детерминированная выборка: порядок по хэшу (адрес + неделя), берём первые n."""
    if len(urls) <= n:
        return list(urls)
    return sorted(urls, key=lambda u: hashlib.sha1((seed + u).encode()).hexdigest())[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='https://jobtoo.ru')
    ap.add_argument('--sample', type=int, default=300)
    ap.add_argument('--json')
    a = ap.parse_args()
    code, idx = get(a.base + '/sitemaps/index.xml')
    if code != 200:
        print('не удалось прочитать карту сайта: HTTP', code)
        return 2
    week = datetime.date.today().strftime('%G-W%V')
    todo = []
    for part in locs(idx):
        c, x = get(part)
        urls = locs(x) if c == 200 else []
        full = part.endswith('/main.xml') or part.endswith('/companies.xml')
        todo += urls if full else pick(urls, a.sample, week)
        print(f'часть {part.rsplit("/", 1)[-1]}: адресов {len(urls)}, проверяем {len(urls) if full else min(len(urls), a.sample)}')
    problems = collections.defaultdict(list)
    titles = collections.defaultdict(list)

    def one(u):
        st, body = get(u)
        return u, analyze(u, st, body)

    with cf.ThreadPoolExecutor(4) as ex:
        for u, res in ex.map(one, todo):
            probs, info = res if isinstance(res, tuple) else (res, {})
            for p in probs:
                problems[p].append(u)
            if info.get('title'):
                titles[info['title']].append(u)
    dups = {t: us for t, us in titles.items() if len(us) > 1}
    print(f'\n== SEO-отчёт {week}: проверено {len(todo)} страниц ==')
    print(f'дубли title: {len(dups)} групп, {sum(len(v) for v in dups.values())} страниц')
    for k in sorted(problems):
        print(f'{k}: {len(problems[k])}')
    for k in sorted(problems):
        print(f'\n-- {k} (примеры) --')
        for u in problems[k][:15]:
            print(u)
    if dups:
        print('\n-- дубли title (примеры) --')
        for t, us in list(dups.items())[:10]:
            print(f'«{t}» ×{len(us)}: {us[0]} …')
    if a.json:
        json.dump({'week': week, 'checked': len(todo), 'problems': {k: v[:200] for k, v in problems.items()},
                   'dup_titles': {t: us[:20] for t, us in list(dups.items())[:100]}}, open(a.json, 'w'), ensure_ascii=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
