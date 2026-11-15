#!/usr/bin/env python3
"""Читатель Telegram-каналов: новый пост — вакансия у нас через секунды (06.11.2026).

Работает постоянно на машине ВНЕ России (t.me и Telegram оттуда доступны; с сервера сайта —
нет), под отдельным аккаунтом Telegram. Слушает обновления каналов, пачкой присылает новые
посты в ingest.php?kind=telegram_push (ключ присылки — тот же, что у telegram_relay).
Дальше всё как у обычного круга: разбор, нейросеть, фильтры — на сервере.

    python3 telegram-listener.py login   # один раз: выдаёт TG_SESSION
    python3 telegram-listener.py join    # подписаться на включённые каналы (медленно, по одному)
    python3 telegram-listener.py run     # слушать (его запускает systemd)
    python3 telegram-listener.py poll    # один проход без подписки: новые посты и выход (GitHub Actions по cron)

Окружение: TG_API_ID, TG_API_HASH (my.telegram.org), TG_SESSION, JT_RELAY_KEY, JT_BASE
(по умолчанию https://jobtoo.ru). Подробности и риски — docs/telegram-push.md.
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get('JT_BASE', 'https://jobtoo.ru').rstrip('/')
FLUSH_S = 15            # посты канала копим столько секунд и шлём одной пачкой
MAX_BATCH = 20
REFRESH_S = 1800        # список каналов перечитываем раз в полчаса
BACKFILL_LIMIT = 40     # после простоя дочитываем назад не больше стольких постов канала
BACKFILL_SLACK_S = 86400


def message_to_post(msg):
    """Сообщение Telethon → пост для kind=telegram_push; None — поста нет (фото без текста)."""
    text = (getattr(msg, 'raw_text', None) or getattr(msg, 'message', None) or '').strip()
    if not text:
        return None
    html = ''
    try:
        from telethon.extensions import html as tl_html
        html = tl_html.unparse(msg.raw_text, msg.entities or [])
    except Exception:
        html = ''
    buttons = []
    for row in (getattr(msg, 'buttons', None) or []):
        for b in row:
            url = getattr(b, 'url', None)
            if url:
                buttons.append({'url': url, 'text': getattr(b, 'text', '') or ''})
    date = getattr(msg, 'date', None)
    return {
        'id': int(msg.id),
        'date': date.isoformat() if date else None,
        'text': text,
        'html': html,
        'buttons': buttons,
    }


class Batcher:
    """Копит посты по каналам; канал готов к отправке, когда накопилась пачка или прошло FLUSH_S."""

    def __init__(self, flush_s=FLUSH_S, max_batch=MAX_BATCH, clock=time.monotonic):
        self.flush_s, self.max_batch, self.clock = flush_s, max_batch, clock
        self.items = {}    # handle -> {id: post}
        self.first = {}    # handle -> время первого поста в пачке

    def add(self, handle, post):
        self.items.setdefault(handle, {})[post['id']] = post   # правка поста заменяет прежний
        self.first.setdefault(handle, self.clock())

    def due(self):
        now = self.clock()
        out = []
        for h, posts in list(self.items.items()):
            if len(posts) >= self.max_batch or now - self.first[h] >= self.flush_s:
                out.append((h, sorted(posts.values(), key=lambda p: p['id'])))
                del self.items[h], self.first[h]
        return out


def _request(path, data=None, timeout=60):
    headers = {'X-Relay-Key': os.environ['JT_RELAY_KEY'], 'Content-Type': 'application/json'}
    req = urllib.request.Request(BASE + path, headers=headers,
                                 data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or '{}')


def server_channels():
    """Включённые каналы: {handle_lower: since}."""
    got = _request('/api/ingest.php?kind=telegram_relay', timeout=30)
    return {c['handle'].lower(): int(c.get('since') or 0) for c in got.get('channels', [])}


def push(handle, posts, tries=3):
    for i in range(tries):
        try:
            res = _request('/api/ingest.php?kind=telegram_push', {'handle': handle, 'posts': posts}, timeout=120)
            return res
        except urllib.error.HTTPError as e:
            if e.code == 422:      # канал не включён / нет годных постов — повтор не поможет
                print(f'[{handle}] отказ сервера: {e.read().decode()[:200]}', file=sys.stderr)
                return None
            err = e
        except Exception as e:     # сеть, таймаут, 5xx
            err = e
        print(f'[{handle}] не отправлено (попытка {i + 1}/{tries}): {err}', file=sys.stderr)
        time.sleep(5 * (2 ** i))
    return None


async def _flusher(batcher, loop):
    while True:
        await asyncio.sleep(2)
        for handle, posts in batcher.due():
            res = await loop.run_in_executor(None, push, handle, posts)
            print(f'[{handle}] отправлено постов: {len(posts)}; ответ: {str(res)[:160]}')


async def run():
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(os.environ['TG_SESSION']), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    await client.start()
    loop = asyncio.get_running_loop()
    state = {'channels': await loop.run_in_executor(None, server_channels), 'at': time.monotonic()}
    batcher = Batcher()

    async def handler(event):
        chat = await event.get_chat()
        handle = (getattr(chat, 'username', None) or '').lower()
        if handle not in state['channels']:
            return
        post = message_to_post(event.message)
        if post:
            batcher.add(handle, post)

    client.add_event_handler(handler, events.NewMessage(incoming=True))
    client.add_event_handler(handler, events.MessageEdited(incoming=True))

    # После простоя дочитываем назад: с прошлой присылки минус сутки, не больше BACKFILL_LIMIT постов.
    for handle, since in list(state['channels'].items()):
        cut = (since - BACKFILL_SLACK_S) if since else time.time() - 3 * 86400
        try:
            async for msg in client.iter_messages(handle, limit=BACKFILL_LIMIT):
                if msg.date.timestamp() < cut:
                    break
                post = message_to_post(msg)
                if post:
                    batcher.add(handle, post)
        except Exception as e:
            print(f'[{handle}] дочитывание: {e}', file=sys.stderr)
        await asyncio.sleep(1)

    flusher = asyncio.create_task(_flusher(batcher, loop))
    print(f'слушаю каналов: {len(state["channels"])}')
    while True:
        await asyncio.sleep(60)
        if time.monotonic() - state['at'] > REFRESH_S:
            try:
                state['channels'] = await loop.run_in_executor(None, server_channels)
                state['at'] = time.monotonic()
            except Exception as e:
                print(f'список каналов не обновлён: {e}', file=sys.stderr)
        if flusher.done():
            flusher.result()


POLL_OVERLAP_S = 600    # проход берёт посты с прошлой присылки минус 10 минут: пересечение безопасно


def fresh_posts(messages, cut_ts):
    """Посты новее cut_ts (unix) из сообщений от новых к старым; пустые пропускаются."""
    out = []
    for msg in messages:
        if msg.date.timestamp() < cut_ts:
            break
        post = message_to_post(msg)
        if post:
            out.append(post)
    return sorted(out, key=lambda p: p['id'])


async def poll():
    """Один проход: публичные каналы читаются без подписки (iter_messages), новое уходит на сервер.
    Для машины, которая не живёт постоянно (GitHub Actions в открытом inst): свежесть — период cron."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(os.environ['TG_SESSION']), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    await client.start()
    loop = asyncio.get_running_loop()
    channels = await loop.run_in_executor(None, server_channels)
    sent = 0
    for handle, since in channels.items():
        cut = (since - POLL_OVERLAP_S) if since else time.time() - 3 * 86400
        try:
            msgs = [m async for m in client.iter_messages(handle, limit=BACKFILL_LIMIT)]
        except Exception as e:
            print(f'[{handle}] чтение: {e}', file=sys.stderr)
            continue
        posts = fresh_posts(msgs, cut)
        if posts:
            res = await loop.run_in_executor(None, push, handle, posts)
            sent += len(posts)
            print(f'[{handle}] постов: {len(posts)}; ответ: {str(res)[:120]}')
        await asyncio.sleep(0.5)
    await client.disconnect()
    print(f'каналов: {len(channels)}, отправлено постов: {sent}')


async def login():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    await client.start()
    print('TG_SESSION=' + client.session.save())


async def join():
    """Подписка на включённые каналы: живые обновления приходят только из тех, где состоишь."""
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import JoinChannelRequest
    client = TelegramClient(StringSession(os.environ['TG_SESSION']), int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    await client.start()
    have = set()
    async for d in client.iter_dialogs():
        u = getattr(d.entity, 'username', None)
        if u:
            have.add(u.lower())
    for handle in server_channels():
        if handle in have:
            continue
        try:
            await client(JoinChannelRequest(handle))
            print(f'[{handle}] подписался')
        except errors.FloodWaitError as e:
            print(f'FloodWait {e.seconds} с — жду')
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            print(f'[{handle}] {e}', file=sys.stderr)
        await asyncio.sleep(45)   # медленно: быстрые подписки Telegram считает спамом


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    fn = {'run': run, 'login': login, 'join': join, 'poll': poll}.get(cmd)
    if fn is None:
        sys.exit(__doc__)
    asyncio.run(fn())
