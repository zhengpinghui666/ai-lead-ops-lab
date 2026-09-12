"""Read the public VALORANT category once; candidates are not verified streams."""
from html.parser import HTMLParser
from datetime import datetime, timezone
import json
import re
import threading
import urllib.error
import urllib.request

import clubops as app

SOURCE_URL = 'https://live.douyin.com/category/1_1_1_1010017'
LIMIT = 3 * 1024 * 1024
GUARD = threading.Lock()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = None
        self.rows = []

    def finish_link(self):
        if self.active:
            parts = self.active.pop('parts')
            if parts:
                self.active.update(title=' '.join(parts[1:] if len(parts) > 1 else parts)[:300],
                                   popularity_text=parts[0][:30] if len(parts) > 1 else '')
                self.rows.append(self.active)
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.finish_link()
            url = dict(attrs).get('href', '')
            if re.fullmatch(r'https://live\.douyin\.com/[1-9][0-9]{2,29}', url):
                self.active = dict(room_url=url, parts=[])

    def handle_endtag(self, tag):
        if tag == 'a':
            self.finish_link()

    def handle_data(self, value):
        if self.active and value.strip() and len(self.active['parts']) < 20:
            self.active['parts'].append(value.strip()[:300])


def parse(html):
    parser = Links()
    parser.feed(html)
    parser.close()
    parser.finish_link()
    unique = {}
    for row in parser.rows:
        if row['title'] and row['room_url'] not in unique:
            unique[row['room_url']] = row
    return list(unique.values())[:20]


def state(mode='live'):
    with app.db(mode) as c:
        row = c.execute("SELECT value FROM settings WHERE key='live_discovery'").fetchone()
        result = json.loads(row[0]) if row else dict(status='idle', rows=[], fetched_at=None,
                source_url=SOURCE_URL, transport='http_public_page', detail='尚未读取公开直播分类')
        urls = [r['room_url'] for r in result['rows']]
        result['observations'] = {}
        if urls:
            placeholders = ','.join('?' for _ in urls)
            for record in c.execute('SELECT id,room_url,status,detail,inserted,frames,started_at,finished_at FROM live_sessions WHERE id IN '
                    '(SELECT MAX(id) FROM live_sessions WHERE room_url IN (' + placeholders + ') GROUP BY room_url)', urls):
                result['observations'][record['room_url']] = dict(record)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover(mode='live', *, opener=None, _catalog=True):
    if mode != 'live':
        raise ValueError('演示区不发现真实直播间')
    if not GUARD.acquire(blocking=False):
        raise ValueError('正在读取直播分类，请等待本次完成')
    try:
        previous = state(mode)
        stamp = previous.get('attempted_at')
        if stamp and (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() < 60:
            return {**previous, 'cached': True}
        result = dict(status='failed', rows=previous.get('rows', []), fetched_at=previous.get('fetched_at'),
                      attempted_at=app.now(), source_url=SOURCE_URL, transport='http_public_page', cached=False)
        opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'ClubOps-LiveDiscovery/1.0', 'Accept': 'text/html'})
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise ValueError('response_status')
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError('response_too_large')
                html = raw.decode('utf-8')
                rows = parse(html)
                if not rows:
                    raise ValueError('no_candidates')
            result.update(status='ready', rows=rows, fetched_at=app.now(),
                          detail='来自公开无畏契约分类的候选房间；选择后保存配置，读取时再确认是否开播', response_bytes=len(raw))
        except urllib.error.HTTPError as exc:
            result.update(error='http_'+str(exc.code), detail='公开分类返回访问异常，未重试；保留上次候选及其时间')
            exc.close()
        except (OSError, ValueError) as exc:
            result.update(error=str(exc) if isinstance(exc, ValueError) and str(exc) in ('response_status', 'response_too_large', 'no_candidates') else 'read_failed',
                          detail='本次未取得可确认的公开房间列表；保留上次候选及其时间')
        with app.LOCKS[mode], app.db(mode) as c:
            c.execute("INSERT INTO settings VALUES('live_discovery',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(result, ensure_ascii=False),))
            if _catalog and result['status'] == 'ready':
                import live_room_pool
                live_room_pool.ingest(c, result['rows'], 'valorant_category', result['fetched_at'])
        return state(mode)
    finally:
        GUARD.release()
