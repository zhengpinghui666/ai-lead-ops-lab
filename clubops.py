from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from intent_rules import RULESET_VERSION, classify_comment

DATA_DIR = Path(os.environ.get('CLUBOPS_DATA_DIR') or Path(__file__).resolve().parent / 'data').expanduser().resolve()
LOCKS = {'live': threading.RLock(), 'demo': threading.RLock()}
TARGET_GAME = '无畏契约'
SERVICE_TYPES = ('娱乐开黑', '排位组队', '新手陪练', '对局复盘')
REVIEW_FIELDS = ('game', 'service_type', 'region', 'rank_label', 'time', 'budget', 'party_size')
from game_scope import ALIASES as VALORANT_ALIASES
GAMES = {'三角洲行动': ['三角洲', 'delta'], '无畏契约': list(VALORANT_ALIASES), '王者荣耀': ['王者荣耀', '王者'], '英雄联盟': ['英雄联盟', 'lol'], '和平精英': ['和平精英']}
LABELS = {'buyer': '客户需求', 'seller': '陪玩接单', 'recruit': '招募需求', 'social': '免费组队', 'noise': '无关讨论', 'uncertain': '待判断'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def timestamp(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, timezone.utc).isoformat(timespec='seconds')
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        raise ValueError('时间无效，请使用 ISO 时间或 Unix 时间戳')


def clean(value, limit=5000):
    return str(value or '').strip()[:limit]


def link(value):
    value = clean(value, 2000)
    if value and (urlparse(value).scheme not in ('https', 'http') or not urlparse(value).hostname):
        raise ValueError('来源链接必须是有效的 http 或 https 地址')
    return value


@contextmanager
def db(mode='live'):
    if mode not in LOCKS:
        raise ValueError('未知工作区')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    filename = 'clubops-demo-valorant.db' if mode == 'demo' else 'clubops-live.db'
    c = sqlite3.connect(DATA_DIR / filename, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        with c:
            yield c
    finally:
        c.close()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'manual',notes TEXT NOT NULL DEFAULT '',last_received TEXT);
CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES sources(id),external_id TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL DEFAULT '',game TEXT NOT NULL DEFAULT '',priority TEXT NOT NULL DEFAULT 'normal',interval_seconds INTEGER NOT NULL DEFAULT 300,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,last_received TEXT,UNIQUE(source_id,external_id));
CREATE TABLE IF NOT EXISTS video_metadata (video_id INTEGER PRIMARY KEY REFERENCES videos(id),payload_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES sources(id),external_id TEXT NOT NULL,nickname TEXT NOT NULL,contact_basis TEXT NOT NULL DEFAULT '',contact_note TEXT NOT NULL DEFAULT '',do_not_contact INTEGER NOT NULL DEFAULT 0,UNIQUE(source_id,external_id));
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES sources(id),external_id TEXT NOT NULL,video_id INTEGER NOT NULL REFERENCES videos(id),person_id INTEGER REFERENCES people(id),parent_external_id TEXT NOT NULL DEFAULT '',raw_text TEXT NOT NULL,published_at TEXT,discovered_at TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'uncertain',game TEXT NOT NULL DEFAULT '',confidence REAL,reason TEXT NOT NULL DEFAULT '',facts TEXT NOT NULL DEFAULT '{}',analysis_method TEXT NOT NULL DEFAULT 'pending',analyzed_at TEXT,UNIQUE(source_id,external_id));
CREATE TABLE IF NOT EXISTS comment_revisions (id INTEGER PRIMARY KEY,comment_id INTEGER NOT NULL REFERENCES comments(id),raw_text TEXT NOT NULL,replaced_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comment_reviews (id INTEGER PRIMARY KEY,comment_id INTEGER NOT NULL REFERENCES comments(id),snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_comment_reviews_comment ON comment_reviews(comment_id,id);
CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY,name TEXT NOT NULL,game TEXT NOT NULL,region TEXT NOT NULL DEFAULT '',skill TEXT NOT NULL DEFAULT '',price REAL,availability TEXT NOT NULL DEFAULT '',available INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY,person_id INTEGER NOT NULL UNIQUE REFERENCES people(id),stage TEXT NOT NULL DEFAULT 'new',owner TEXT NOT NULL DEFAULT '',assigned_member INTEGER REFERENCES members(id),outcome_note TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_jobs (id INTEGER PRIMARY KEY,lead_id INTEGER NOT NULL REFERENCES leads(id),request_id TEXT NOT NULL UNIQUE,content TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY,lead_id INTEGER NOT NULL REFERENCES leads(id),job_id INTEGER UNIQUE REFERENCES message_jobs(id),direction TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,kind TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_comments_video_time ON comments(video_id,published_at);
CREATE INDEX IF NOT EXISTS idx_comments_person ON comments(person_id,id);
CREATE INDEX IF NOT EXISTS idx_comments_analysis ON comments(analysis_method);
CREATE INDEX IF NOT EXISTS idx_videos_source_url ON videos(source_id,url);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(source_id,parent_external_id,video_id);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id,id);
CREATE TABLE IF NOT EXISTS collection_tasks (id INTEGER PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,target TEXT NOT NULL,video_limit INTEGER NOT NULL,comment_limit INTEGER NOT NULL,interactive INTEGER NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',videos INTEGER NOT NULL DEFAULT 0,comments INTEGER NOT NULL DEFAULT 0,inserted INTEGER NOT NULL DEFAULT 0,duplicate INTEGER NOT NULL DEFAULT 0,revised INTEGER NOT NULL DEFAULT 0,skipped INTEGER NOT NULL DEFAULT 0,page_url TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished_at TEXT);
CREATE TABLE IF NOT EXISTS collection_observations (task_id INTEGER NOT NULL REFERENCES collection_tasks(id),kind TEXT NOT NULL,external_id TEXT NOT NULL,page_url TEXT NOT NULL,observed_at TEXT NOT NULL,payload_hash TEXT NOT NULL,PRIMARY KEY(task_id,kind,external_id));
CREATE TABLE IF NOT EXISTS collection_diagnostics (id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL REFERENCES collection_tasks(id),stage TEXT NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_checkpoints (task_id INTEGER NOT NULL REFERENCES collection_tasks(id),video_id TEXT NOT NULL,video_title TEXT NOT NULL,video_url TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',detail TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,PRIMARY KEY(task_id,video_id));
CREATE TABLE IF NOT EXISTS collection_resumes (task_id INTEGER PRIMARY KEY REFERENCES collection_tasks(id),parent_task_id INTEGER NOT NULL REFERENCES collection_tasks(id));
CREATE TABLE IF NOT EXISTS collection_plans (id INTEGER PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,video_limit INTEGER NOT NULL,comment_limit INTEGER NOT NULL,interval_seconds INTEGER NOT NULL,priority INTEGER NOT NULL,run_limit INTEGER NOT NULL,run_count INTEGER NOT NULL DEFAULT 0,settled_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'paused',next_run_at TEXT,last_task_id INTEGER REFERENCES collection_tasks(id),settled_task_id INTEGER REFERENCES collection_tasks(id),activated_at TEXT,detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
'''


def init(mode='live'):
    # Before introducing the HTTP ledger, back up the existing DB using SQLite's
    # backup API (including committed WAL data). No browser profile is moved.
    filename = 'clubops-demo-valorant.db' if mode == 'demo' else 'clubops-live.db'
    existing_path = DATA_DIR / filename
    if existing_path.exists():
        with db(mode) as source:
            known_tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            verticality_missing = not {'asset_verticality','asset_references','asset_keywords'} <= known_tables
            keyword_sources_missing = 'comment_keyword_sources' not in known_tables
            paging_missing = 'collection_page_progress' not in known_tables
            inbox_missing = not {'uid_inbox_conversations','uid_inbox_messages','uid_inbox_reads','uid_reply_links','uid_inbox_sync'} <= known_tables
            group_missing = not {'monitored_groups','group_messages','group_reads'} <= known_tables
            live_columns = {r[1] for r in source.execute('PRAGMA table_info(live_messages)')}
            collection_columns = {r[1] for r in source.execute('PRAGMA table_info(collection_tasks)')}
            plan_columns = {r[1] for r in source.execute('PRAGMA table_info(collection_plans)')}
            observation_columns = {r[1] for r in source.execute('PRAGMA table_info(collection_observations)')}
            activity_index_missing = not source.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_observation_published_activity'").fetchone()
            if not {'uid_message_attempts', 'live_sessions', 'live_links', 'live_judgments', 'live_reviews', 'intent_results', 'semantic_jobs', 'video_metadata', 'live_tracks', 'live_rooms', 'live_pool_checks', 'collection_candidates', 'collection_candidate_reads', 'discovery_authors', 'discovery_works', 'discovery_jobs', 'discovery_queries'} <= known_tables or not {'outer_message_id', 'game'} <= live_columns or 'transport' not in collection_columns or 'intent_version' not in plan_columns or 'ingest_disposition' not in observation_columns or activity_index_missing or verticality_missing or inbox_missing or keyword_sources_missing or paging_missing or group_missing:
                backup_dir = DATA_DIR / 'backups'
                backup_dir.mkdir(parents=True, exist_ok=True)
                label = ('before-uid-http-' if 'uid_message_attempts' not in known_tables else
                         'before-live-stream-' if 'outer_message_id' not in live_columns else
                         'before-live-workflow-' if 'live_links' not in known_tables else 'before-http-collection-' if 'transport' not in collection_columns else 'before-intent-results-' if 'intent_results' not in known_tables else 'before-semantic-queue-' if 'semantic_jobs' not in known_tables else 'before-monitor-recovery-' if 'intent_version' not in plan_columns else 'before-collection-timing-' if 'ingest_disposition' not in observation_columns else 'before-video-metadata-' if 'video_metadata' not in known_tables else 'before-live-tracking-' if 'live_tracks' not in known_tables else 'before-live-room-pool-' if not {'live_rooms', 'live_pool_checks'} <= known_tables else 'before-candidate-pool-')
                if label=='before-candidate-pool-' and activity_index_missing:label='before-work-activity-'
                if label=='before-candidate-pool-' and verticality_missing:label='before-asset-verticality-'
                if label=='before-candidate-pool-' and inbox_missing:label='before-uid-inbox-'
                if label=='before-candidate-pool-' and keyword_sources_missing:label='before-comment-keywords-'
                if label=='before-candidate-pool-' and paging_missing:label='before-comment-paging-'
                if label=='before-candidate-pool-' and group_missing:label='before-group-monitor-'
                backup = sqlite3.connect(backup_dir / (filename + '.' + label + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.bak'))
                try:
                    source.backup(backup)
                finally:
                    backup.close()
    with LOCKS[mode], db(mode) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        import candidate_pool
        c.executescript(candidate_pool.SCHEMA)
        import discovery_tracking
        c.executescript(discovery_tracking.SCHEMA)
        import uid_messaging
        c.executescript(uid_messaging.SCHEMA)
        import uid_inbox_store
        c.executescript(uid_inbox_store.SCHEMA)
        import uid_inbox_sync
        c.executescript(uid_inbox_sync.SCHEMA)
        import group_monitor
        c.executescript(group_monitor.SCHEMA)
        import group_discovery
        c.executescript(group_discovery.SCHEMA)
        import live_monitor
        c.executescript(live_monitor.SCHEMA)
        import live_tracking
        c.executescript(live_tracking.SCHEMA)
        import live_room_pool
        c.executescript(live_room_pool.SCHEMA)
        import asset_verticality
        import asset_references
        import asset_keywords
        c.executescript(asset_references.SCHEMA)
        c.executescript(asset_keywords.SCHEMA)
        import comment_keywords
        c.executescript(comment_keywords.SCHEMA)
        import comment_paging
        c.executescript(comment_paging.SCHEMA)
        c.executescript(asset_verticality.SCHEMA)
        if 'outer_message_id' not in {r[1] for r in c.execute('PRAGMA table_info(live_messages)')}:
            c.execute('ALTER TABLE live_messages ADD COLUMN outer_message_id TEXT')
        import live_workflow
        live_workflow.migrate(c)
        defaults = {'club_name': '无畏契约陪玩俱乐部', 'keywords': '无畏契约陪玩,瓦陪玩,VALORANT陪玩,无畏契约开黑,无畏契约陪练,无畏契约复盘', 'excluded': '', 'analysis_workers': 4, 'auto_reply': False, 'focus_game': TARGET_GAME}
        for key, value in defaults.items():
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
        # Upgrade stock defaults only; preserve any club-specific configuration.
        if not c.execute("SELECT 1 FROM settings WHERE key='valorant_profile_version'").fetchone():
            stock = {'club_name': '我的陪玩俱乐部', 'keywords': '三角洲陪玩,无畏契约陪练,游戏陪玩'}
            for key, old in stock.items():
                stored = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
                if json.loads(stored) == old:
                    c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(defaults[key], ensure_ascii=False), key))
            c.execute("INSERT INTO settings VALUES('valorant_profile_version','1')")
        columns = {r['name'] for r in c.execute('PRAGMA table_info(members)')}
        for column, definition in {'service_types': "TEXT NOT NULL DEFAULT '[]'", 'rank_label': "TEXT NOT NULL DEFAULT ''"}.items():
            if column not in columns:
                c.execute(f'ALTER TABLE members ADD COLUMN {column} {definition}')
        comment_columns = {r['name'] for r in c.execute('PRAGMA table_info(comments)')}
        if 'observed_nickname' not in comment_columns:
            c.execute("ALTER TABLE comments ADD COLUMN observed_nickname TEXT NOT NULL DEFAULT ''")
        if 'manual_fields' not in comment_columns:
            c.execute("ALTER TABLE comments ADD COLUMN manual_fields TEXT NOT NULL DEFAULT '{}'")
        import analysis_store
        analysis_store.migrate(c)
        import semantic_queue
        c.executescript(semantic_queue.SCHEMA)
        for table, additions in {
            'collection_tasks': {'page_concurrency': 'INTEGER NOT NULL DEFAULT 1', 'active_pages': 'INTEGER NOT NULL DEFAULT 0', 'peak_pages': 'INTEGER NOT NULL DEFAULT 0',
                'transport': "TEXT NOT NULL DEFAULT 'local_browser'",
                'lookback_hours': 'INTEGER', 'comment_since': 'TEXT', 'filtered_old': 'INTEGER NOT NULL DEFAULT 0',
                'filtered_unknown': 'INTEGER NOT NULL DEFAULT 0', 'filtered_future': 'INTEGER NOT NULL DEFAULT 0',
                'include_keywords': "TEXT NOT NULL DEFAULT ''", 'exclude_keywords': "TEXT NOT NULL DEFAULT ''",
                'filtered_keyword': 'INTEGER NOT NULL DEFAULT 0', 'filtered_blocked': 'INTEGER NOT NULL DEFAULT 0'},
            'collection_plans': {'page_concurrency': 'INTEGER NOT NULL DEFAULT 1', 'continuous': 'INTEGER NOT NULL DEFAULT 0', 'lookback_hours': 'INTEGER',
                'intent_version': 'INTEGER NOT NULL DEFAULT 0',
                'transport': "TEXT NOT NULL DEFAULT 'local_browser'",
                'include_keywords': "TEXT NOT NULL DEFAULT ''", 'exclude_keywords': "TEXT NOT NULL DEFAULT ''"},
            'collection_observations': {'filter_reason': "TEXT NOT NULL DEFAULT ''",
                'ingest_disposition': "TEXT NOT NULL DEFAULT ''",
                'comment_text': "TEXT NOT NULL DEFAULT ''", 'published_at': 'TEXT',
                'nickname': "TEXT NOT NULL DEFAULT ''", 'user_identifier': "TEXT NOT NULL DEFAULT ''"},
        }.items():
            existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            for column, definition in additions.items():
                if column not in existing:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        c.execute(discovery_tracking.ACTIVITY_INDEX)
        # Classify saved assets locally, without re-queuing any old comments.
        if not c.execute("SELECT 1 FROM settings WHERE key='asset_keyword_corpus_v1'").fetchone():
            asset_keywords.backfill(c)
            c.execute("INSERT INTO settings VALUES('asset_keyword_corpus_v1','true')")
        if not c.execute("SELECT 1 FROM settings WHERE key='comment_keyword_corpus_v1'").fetchone():
            comment_keywords.backfill(c)
            c.execute("INSERT INTO settings VALUES('comment_keyword_corpus_v1','true')")
        for query in asset_keywords.queries(c):
            c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(query,now()))
        if not c.execute("SELECT 1 FROM settings WHERE key='valorant_alias_scope_v1'").fetchone():
            discovery_tracking.refresh_game_scope(c)
            c.execute("INSERT INTO settings VALUES('valorant_alias_scope_v1','true')")
        if not c.execute("SELECT 1 FROM settings WHERE key='valorant_pc_only_v1'").fetchone():
            import game_scope
            game_scope.enforce_saved_scope(c)
            c.execute("INSERT INTO settings VALUES('valorant_pc_only_v1','true')")
        if not c.execute("SELECT 1 FROM settings WHERE key='valorant_cn_pc_only_v1'").fetchone():
            import game_scope
            game_scope.enforce_saved_scope(c)
            c.execute("INSERT INTO settings VALUES('valorant_cn_pc_only_v1','true')")
        if not c.execute('SELECT 1 FROM asset_verticality LIMIT 1').fetchone() or c.execute("SELECT 1 FROM asset_verticality WHERE json_extract(result,'$.version')!=? LIMIT 1",(asset_verticality.VERSION,)).fetchone():
            asset_verticality.backfill(c)
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_single_monitor ON collection_plans(continuous) WHERE continuous=1')
        if not c.execute('SELECT 1 FROM sources LIMIT 1').fetchone():
            c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('人工导入','import','manual','接收 CSV / JSON；不主动访问抖音')")
        if not c.execute("SELECT 1 FROM settings WHERE key='parent_context_version'").fetchone():
            # Legacy results keep their original context/version. New pending
            # records use today's context rules; startup never clears history.
            c.execute("INSERT INTO settings VALUES('parent_context_version','1')")
        stored_rules = c.execute("SELECT value FROM settings WHERE key='ruleset_version'").fetchone()
        if not stored_rules or json.loads(stored_rules['value']) != RULESET_VERSION:
            c.execute("INSERT INTO settings VALUES('ruleset_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(RULESET_VERSION),))
        c.execute('PRAGMA optimize')


def event(c, kind, detail):
    c.execute('INSERT INTO events(kind,detail,created_at) VALUES(?,?,?)', (kind, detail, now()))


def classify(raw, context='', *, parent_context=''):
    return classify_comment(raw, context, parent_context, GAMES, TARGET_GAME)


def review_token(row):
    """Detect stale review forms without exposing or modifying source records."""
    keys = ('id', 'raw_text', 'category', 'game', 'facts', 'manual_fields', 'reason', 'analysis_method', 'analyzed_at')
    payload = json.dumps({key: row[key] for key in keys}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def validate_review_fields(value):
    if not isinstance(value, dict) or set(value) - set(REVIEW_FIELDS):
        raise ValueError('人工字段仅支持游戏、服务、区服、段位、时间、预算和人数')
    if any(not isinstance(v, str) or len(v) > 120 for v in value.values()):
        raise ValueError('每个人工字段须为不超过 120 字的文本；未知请留空')
    fields = {key: val.strip() for key, val in value.items()}
    if fields.get('game') and fields['game'] not in GAMES:
        raise ValueError('请选择支持的游戏，无法确认时留空')
    if fields.get('service_type') and fields['service_type'] not in SERVICE_TYPES:
        raise ValueError('请选择支持的服务方向，无法确认时留空')
    return fields


def reset_rule_context(c, source_id, external, video_id, *, include_self=False):
    c.execute('''UPDATE comment_keyword_sources SET current=0 WHERE comment_id IN
        (SELECT id FROM comments WHERE source_id=? AND video_id=? AND
         (parent_external_id=? OR (? AND external_id=?)))''', (source_id, video_id, external, include_self, external))
    c.execute("""UPDATE comments SET analysis_method='pending',category='uncertain',game='',
        confidence=NULL,facts='{}',reason='',analyzed_at=NULL
        WHERE source_id=? AND video_id=? AND analysis_method='rules'
        AND (parent_external_id=? OR (? AND external_id=?))""",
        (source_id, video_id, external, include_self, external))


def reset_video_rules(c, video_id):
    """A changed video title changes context, never overwrite a human decision."""
    c.execute('UPDATE comment_keyword_sources SET current=0 WHERE comment_id IN (SELECT id FROM comments WHERE video_id=?)', (video_id,))
    c.execute("UPDATE comments SET analysis_method='pending',category='uncertain',game='',confidence=NULL,facts='{}',reason='',analyzed_at=NULL WHERE video_id=? AND analysis_method='rules'", (video_id,))


def validate_comment_relations(c, source_id, external):
    row = c.execute('SELECT * FROM comments WHERE source_id=? AND external_id=?', (source_id, external)).fetchone()
    parent = c.execute('SELECT video_id FROM comments WHERE source_id=? AND external_id=?', (source_id, row['parent_external_id'])).fetchone()
    child_conflict = c.execute('SELECT 1 FROM comments WHERE source_id=? AND parent_external_id=? AND video_id!=? LIMIT 1', (source_id, external, row['video_id'])).fetchone()
    if (parent and parent['video_id'] != row['video_id']) or child_conflict:
        raise ValueError(f'评论 {external} 的回复关系跨越不同视频；本批次未导入')
    # UNION terminates even for a malformed existing cycle. Source IDs never cross.
    cycle = c.execute('''WITH RECURSIVE ancestors(external_id,parent_external_id) AS (
        SELECT external_id,parent_external_id FROM comments WHERE source_id=? AND external_id=?
        UNION
        SELECT p.external_id,p.parent_external_id FROM comments p JOIN ancestors a
        ON p.external_id=a.parent_external_id WHERE p.source_id=?
    ) SELECT 1 FROM ancestors WHERE parent_external_id=? LIMIT 1''',
        (source_id, external, source_id, external)).fetchone()
    if cycle:
        raise ValueError(f'评论 {external} 的回复关系存在循环；本批次未导入')


def ingest(payload, mode='live', *, allow_browser_source=False, connection=None):
    records = payload.get('records')
    if records is None and 'csv' in payload:
        records = list(csv.DictReader(io.StringIO(payload['csv'].lstrip('\ufeff'))))
    if not isinstance(records, list) or not records or len(records) > 5000:
        raise ValueError('请提交 1–5000 条评论记录')
    source_id = int(payload.get('source_id', 1))
    normalized = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('每条记录必须是对象')
        raw = clean(row.get('text') or row.get('comment') or row.get('评论'))
        external, video = clean(row.get('comment_id'), 300), clean(row.get('video_id'), 300)
        if not raw or not external or not video:
            raise ValueError('每条记录必须包含 comment_id、video_id、text；不能用用户 ID 代替评论 ID')
        normalized.append((row, raw, external, video, timestamp(row.get('published_at') or row.get('create_time')), link(row.get('video_url'))))
    result = {'inserted': 0, 'duplicate': 0, 'revised': 0}
    with LOCKS[mode], (nullcontext(connection) if connection is not None else db(mode)) as c:
        source = required(c, 'sources', source_id)
        if source['kind'] == 'browser' and not allow_browser_source:
            raise ValueError('浏览器采集来源只能由采集器写入；文件请使用人工导入来源')
        for row, raw, external, vid, published, url in normalized:
            t = now()
            parent_id = clean(row.get('parent_comment_id'), 300)
            observed_nickname = clean(row.get('nickname'), 120)
            user_id = clean(row.get('user_id') or row.get('uid') or row.get('open_id'), 300)
            existing = c.execute('SELECT x.*,v.external_id AS external_video,p.external_id AS external_person FROM comments x JOIN videos v ON x.video_id=v.id LEFT JOIN people p ON x.person_id=p.id WHERE x.source_id=? AND x.external_id=?', (source_id, external)).fetchone()
            if existing and (existing['external_video'] != vid or (user_id and existing['external_person'] and user_id != existing['external_person'])):
                raise ValueError(f'评论 {external} 的视频或用户 ID 与已有记录冲突；本批次未导入')
            if parent_id == external or (existing and parent_id and existing['parent_external_id'] and existing['parent_external_id'] != parent_id):
                raise ValueError(f'评论 {external} 的上级评论 ID 与自身或已有记录冲突；本批次未导入')
            prior_video = c.execute('SELECT id,title FROM videos WHERE source_id=? AND external_id=?', (source_id, vid)).fetchone()
            c.execute("INSERT INTO videos(source_id,external_id,title,url,created_at,last_received) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,external_id) DO UPDATE SET last_received=excluded.last_received,title=CASE WHEN videos.title=videos.external_id AND excluded.title!=excluded.external_id THEN excluded.title ELSE videos.title END,url=CASE WHEN videos.url='' THEN excluded.url ELSE videos.url END", (source_id, vid, clean(row.get('video_title')) or vid, url, t, t))
            video_id = c.execute('SELECT id FROM videos WHERE source_id=? AND external_id=?', (source_id, vid)).fetchone()[0]
            if prior_video and prior_video['title'] == vid and clean(row.get('video_title')) not in ('', vid):
                reset_video_rules(c, video_id)
            person_id = None
            if user_id:
                c.execute("INSERT INTO people(source_id,external_id,nickname) VALUES(?,?,?) ON CONFLICT(source_id,external_id) DO UPDATE SET nickname=CASE WHEN excluded.nickname='未提供昵称' THEN people.nickname ELSE excluded.nickname END", (source_id, user_id, clean(row.get('nickname')) or '未提供昵称'))
                person_id = c.execute('SELECT id FROM people WHERE source_id=? AND external_id=?', (source_id, user_id)).fetchone()[0]
                c.execute('INSERT OR IGNORE INTO leads(person_id,updated_at) VALUES(?,?)', (person_id, t))
            if existing:
                c.execute("UPDATE comments SET person_id=COALESCE(person_id,?),published_at=COALESCE(published_at,?),parent_external_id=CASE WHEN parent_external_id='' THEN ? ELSE parent_external_id END WHERE id=?", (person_id, published, parent_id, existing['id']))
                c.execute("UPDATE comments SET observed_nickname=? WHERE id=? AND observed_nickname=''", (observed_nickname, existing['id']))
                if parent_id and not existing['parent_external_id']:
                    reset_rule_context(c, source_id, external, video_id, include_self=True)
                if existing['raw_text'] == raw:
                    result['duplicate'] += 1
                else:
                    c.execute('INSERT INTO comment_revisions(comment_id,raw_text,replaced_at) VALUES(?,?,?)', (existing['id'], existing['raw_text'], t))
                    import comment_keywords
                    comment_keywords.invalidate(c, existing['id'])
                    c.execute("UPDATE comments SET raw_text=?,analysis_method='pending',confidence=NULL,category='uncertain',game='',facts='{}',manual_fields='{}',reason='',analyzed_at=NULL WHERE id=?", (raw, existing['id']))
                    reset_rule_context(c, source_id, external, video_id)
                    result['revised'] += 1
                continue
            c.execute('INSERT INTO comments(source_id,external_id,video_id,person_id,parent_external_id,raw_text,published_at,discovered_at,observed_nickname) VALUES(?,?,?,?,?,?,?,?,?)', (source_id, external, video_id, person_id, parent_id, raw, published, t, observed_nickname))
            reset_rule_context(c, source_id, external, video_id)
            result['inserted'] += 1
        for external in dict.fromkeys(r[2] for r in normalized):
            validate_comment_relations(c, source_id, external)
        c.execute('UPDATE sources SET last_received=? WHERE id=?', (now(), source_id))
        event(c, 'import', f"接收 {len(records)} 条：新增 {result['inserted']}，重复 {result['duplicate']}，修订 {result['revised']}")
    return result


def analyze(mode='live', *, comment_ids=None):
    with LOCKS[mode]:
        with db(mode) as c:
            workers = json.loads(c.execute("SELECT value FROM settings WHERE key='analysis_workers'").fetchone()[0])
            scope, args = '', []
            if comment_ids is not None:
                if not isinstance(comment_ids, list) or len(comment_ids) > 1000 or any(type(i) is not int for i in comment_ids):
                    raise ValueError('分析范围须为最多 1000 个本地评论 ID')
                if not comment_ids:
                    return {'analyzed': 0, 'method': 'rules'}
                scope, args = ' AND x.id IN (' + ','.join('?' for _ in comment_ids) + ')', comment_ids
            rows = c.execute("SELECT x.*,v.title,(SELECT p.raw_text FROM comments p WHERE p.source_id=x.source_id AND p.video_id=x.video_id AND p.id!=x.id AND p.external_id=x.parent_external_id) AS parent_text FROM comments x JOIN videos v ON v.id=x.video_id WHERE analysis_method='pending'" + scope + ' LIMIT 1000', args).fetchall()
        classify_row = lambda r: classify(r['raw_text'], r['title'], parent_context=r['parent_text'] or '')
        if comment_ids is not None or len(rows) < 16:
            # Incremental collection usually contributes one record. Avoid a
            # new thread pool for every pipe event; external inference is queued.
            results = [classify_row(row) for row in rows]
        else:
            with ThreadPoolExecutor(max_workers=min(16, max(1, int(workers)))) as pool:
                results = list(pool.map(classify_row, rows))
        with db(mode) as c:
            for row, r in zip(rows, results):
                c.execute("UPDATE comments SET category=?,game=?,confidence=?,reason=?,facts=?,analysis_method=?,analyzed_at=? WHERE id=? AND analysis_method='pending'", (r['category'], r['game'], r['confidence'], r['reason'], json.dumps(r['facts'], ensure_ascii=False), r['analysis_method'], now(), row['id']))
                import analysis_store
                analysis_store.capture_rule(c, 'comment', row['id'])
                if mode == 'live':
                    import comment_keywords
                    comment_keywords.observe(c, row['id'])
            event(c, 'analysis', f'完成 {len(rows)} 条规则初筛；本次未调用语义模型')
        import semantic_queue
        queued = semantic_queue.enqueue('comment', [r['id'] for r in rows], mode)
        return {'analyzed': len(rows), 'method': 'rules', 'model_queue': queued}


def shell_state(mode='live'):
    """Configuration for monitoring views, without unrelated lead histories.

    These pages read their records through their own paginated endpoints. Full
    lead and analysis state stays available when opening the corresponding view.
    """
    import semantic
    import semantic_queue
    model = semantic.state()
    model['queue'] = semantic_queue.state(mode)
    with db(mode) as c:
        settings = {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
        sources = [dict(r) for r in c.execute('SELECT * FROM sources')]
        videos = [dict(r) for r in c.execute('''SELECT v.*,s.name AS source_name,
            (SELECT COUNT(*) FROM comments x WHERE x.video_id=v.id) AS comment_count
            FROM videos v JOIN sources s ON s.id=v.source_id ORDER BY v.id DESC''')]
        stats = {name: c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('videos','comments')}
        stats['pending'] = c.execute("SELECT COUNT(*) FROM comments WHERE analysis_method='pending'").fetchone()[0]
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 60')]
    return dict(mode=mode,profile={'game':TARGET_GAME,'services':list(SERVICE_TYPES)},settings=settings,
        sources=sources,videos=videos,comments=[],live_messages=[],leads=[],members=[],jobs=[],messages=[],
        events=events,stats=stats,semantic=model,
        connections={'collector':'not_connected','messaging':'not_connected','ai':model['mode'] if mode=='live' else 'rules'})


def state(mode='live'):
    import semantic
    import analysis_store
    model_state = semantic.state()
    import semantic_queue
    model_state['queue'] = semantic_queue.state(mode)
    model_engine = model_state['engine'] if mode == 'live' else None
    with db(mode) as c:
        settings = {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
        sources = [dict(r) for r in c.execute('SELECT * FROM sources')]
        videos = [dict(r) for r in c.execute("SELECT v.*,s.name AS source_name,(SELECT COUNT(*) FROM comments x WHERE x.video_id=v.id) AS comment_count,(SELECT COUNT(*) FROM comments x WHERE x.video_id=v.id AND x.category='buyer') AS demand_count FROM videos v JOIN sources s ON s.id=v.source_id ORDER BY v.id DESC")]
        comments = [dict(r) for r in c.execute("SELECT x.*,v.title AS video_title,v.url AS video_url,COALESCE(NULLIF(x.observed_nickname,''),p.nickname) AS nickname,l.id AS lead_id,s.name AS source_name FROM comments x JOIN videos v ON v.id=x.video_id JOIN sources s ON s.id=x.source_id LEFT JOIN people p ON p.id=x.person_id LEFT JOIN leads l ON l.person_id=p.id ORDER BY COALESCE(julianday(x.published_at),julianday(x.discovered_at)) DESC,x.id DESC")]
        by_person = defaultdict(list)
        reviews = defaultdict(list)
        for review in c.execute('SELECT * FROM (SELECT r.*,ROW_NUMBER() OVER(PARTITION BY comment_id ORDER BY id DESC) AS recent FROM comment_reviews r) WHERE recent<=10 ORDER BY id DESC'):
            reviews[review['comment_id']].append({'id': review['id'], 'created_at': review['created_at'], **json.loads(review['snapshot'])})
        by_external = {(row['source_id'], row['external_id']): row for row in comments}
        for row in comments:
            row.update(evidence_type='comment', source_url=row['video_url'], source_title=row['video_title'])
            row['review_token'] = review_token(row)
            parent = by_external.get((row['source_id'], row['parent_external_id']))
            status = 'none' if not row['parent_external_id'] else 'missing' if parent is None else 'conflict' if parent['video_id'] != row['video_id'] or parent['id'] == row['id'] else 'available'
            row['parent_context'] = {'status': status, 'external_id': row['parent_external_id']}
            if status == 'available':
                row['parent_context'].update({key: parent[key] for key in ('raw_text', 'nickname', 'published_at', 'discovered_at', 'video_url', 'source_name')})
            row['rule_facts'], row['rule_game'] = json.loads(row['facts']), row['game']
            row['manual_fields'] = json.loads(row['manual_fields'])
            row['facts'] = {**row['rule_facts'], **{k: v for k, v in row['manual_fields'].items() if k != 'game'}}
            row['game'] = row['manual_fields'].get('game', row['game'])
            row['review_history'] = reviews[row['id']]
            analysis_store.project(c, row, model_engine=model_engine)
            by_person[row['person_id']].append(row)
        import live_workflow
        live_messages, live_counts = live_workflow.latest_by_person(c)
        for row in live_messages:
            by_person[row['person_id']].append(row)
        import group_monitor
        group_counts=dict(c.execute('SELECT person_id,COUNT(*) FROM group_messages WHERE person_id IS NOT NULL GROUP BY person_id').fetchall())
        group_messages=[group_monitor.project(c,r,engine=model_engine) for r in c.execute(group_monitor.SELECT+''' WHERE m.id IN
          (SELECT MAX(id) FROM group_messages WHERE person_id IS NOT NULL GROUP BY person_id)''')]
        for row in group_messages:by_person[row['person_id']].append(row)
        leads = [dict(r) for r in c.execute('SELECT l.*,p.nickname,p.external_id,p.contact_basis,p.contact_note,p.do_not_contact,s.kind AS source_kind FROM leads l JOIN people p ON p.id=l.person_id JOIN sources s ON s.id=p.source_id ORDER BY l.id DESC')]
        for lead in leads:
            related = by_person[lead['person_id']]
            related.sort(key=lambda r: (datetime.fromisoformat((r['published_at'] or r['discovered_at']).replace('Z', '+00:00')).timestamp(), r['evidence_type'], r['id']), reverse=True)
            latest = related[0] if related else {}
            comment_count = sum(r['evidence_type'] == 'comment' for r in related)
            live_count = live_counts.get(lead['person_id'], 0)
            group_count=group_counts.get(lead['person_id'],0)
            lead.update({'latest': latest, 'comment_count': comment_count, 'live_count': live_count, 'group_count':group_count, 'evidence_count': comment_count + live_count + group_count,
                         'category': latest.get('category', 'uncertain'), 'game': latest.get('game', '')})
        members = [dict(r) for r in c.execute('SELECT * FROM members ORDER BY available DESC,id')]
        for member in members:
            member['service_types'] = json.loads(member['service_types'])
        jobs = [dict(r) for r in c.execute('SELECT j.*,p.nickname,s.kind AS source_kind FROM message_jobs j JOIN leads l ON l.id=j.lead_id JOIN people p ON p.id=l.person_id JOIN sources s ON s.id=p.source_id ORDER BY j.id DESC')]
        attempts = {r['job_id']: dict(r) for r in c.execute('SELECT job_id,status,phase,detail,evidence FROM uid_message_attempts')}
        for job in jobs:
            if job['id'] in attempts:
                job['uid_http'] = attempts[job['id']]
                job['uid_http']['evidence'] = json.loads(job['uid_http']['evidence'])
        messages = [dict(r) for r in c.execute('SELECT * FROM messages ORDER BY id')]
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 60')]
        counts, demands = defaultdict(int), defaultdict(int)
        for comment in comments:
            counts[comment['category']] += 1
            if comment['category'] == 'buyer':
                demands[comment['video_id']] += 1
        counts = dict(counts)
        for video in videos:
            video['demand_count'] = demands[video['id']]
        stats = {'videos': len(videos), 'comments': len(comments), 'pending': sum(x['analysis_method'] == 'pending' for x in comments), 'buyers': sum(x['category'] == 'buyer' for x in leads), 'sellers': sum(x['category'] == 'seller' for x in leads), 'available': sum(x['available'] for x in members), 'won': sum(x['stage'] == 'won' for x in leads), 'referred': sum(x['stage'] == 'referred' for x in leads), 'submitted': sum(x['status'] in ('accepted', 'delivered', 'replied') and x['source_kind'] != 'uid_test' for x in jobs), 'drafts': sum(x['status'] == 'draft' for x in jobs), 'category_counts': counts}
        stats['live_messages'] = c.execute('SELECT COUNT(*) FROM live_messages').fetchone()[0]
    return {'mode': mode, 'profile': {'game': TARGET_GAME, 'services': list(SERVICE_TYPES)}, 'settings': settings, 'sources': sources, 'videos': videos, 'comments': comments, 'live_messages': live_messages, 'leads': leads, 'members': members, 'jobs': jobs, 'messages': messages, 'events': events, 'stats': stats, 'semantic': model_state, 'connections': {'collector': 'not_connected', 'messaging': 'demo' if mode == 'demo' else 'not_connected', 'ai': model_state['mode'] if mode == 'live' else 'rules'}}


def required(c, table, row_id):
    if table not in ('sources', 'videos', 'people', 'comments', 'leads', 'members', 'message_jobs'):
        raise ValueError('记录类型无效')
    row = c.execute(f'SELECT * FROM {table} WHERE id=?', (int(row_id),)).fetchone()
    if row is None:
        raise ValueError('记录不存在')
    return row


def mutate(action, body, mode='live'):
    if action == 'import':
        return ingest(body, mode)
    if action == 'analyze':
        return analyze(mode)
    with LOCKS[mode], db(mode) as c:
        if action == 'video':
            source = required(c, 'sources', body.get('source_id', 1))
            url = link(body.get('url'))
            match = re.search(r'/video/(\d+)', url)
            external = clean(body.get('external_id'), 300) or (match.group(1) if match else '')
            if not external:
                raise ValueError('请填写视频 ID 或包含视频 ID 的链接')
            priority = body.get('priority', 'normal')
            if priority not in ('high', 'normal', 'low'):
                raise ValueError('优先级无效')
            interval = max(10, min(86400, int(body.get('interval_seconds', 300))))
            c.execute('INSERT INTO videos(source_id,external_id,title,url,game,priority,interval_seconds,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,external_id) DO UPDATE SET title=excluded.title,url=excluded.url,priority=excluded.priority,interval_seconds=excluded.interval_seconds', (source['id'], external, clean(body.get('title')) or external, url, clean(body.get('game')), priority, interval, now()))
            event(c, 'monitor', f'保存监控目标 {external}；等待采集通道接入')
        elif action == 'video-toggle':
            row = required(c, 'videos', body['id'])
            c.execute('UPDATE videos SET enabled=? WHERE id=?', (not row['enabled'], row['id']))
        elif action == 'source':
            name, kind = clean(body.get('name'), 120), body.get('kind', 'import')
            if not name or kind not in ('import', 'official', 'provider'):
                raise ValueError('请填写数据源名称和类型')
            c.execute('INSERT INTO sources(name,kind,status,notes) VALUES(?,?,?,?)', (name, kind, 'manual' if kind == 'import' else 'not_connected', clean(body.get('notes'))))
            event(c, 'source', f'添加数据源：{name}')
        elif action == 'member':
            name, game = clean(body.get('name'), 120), clean(body.get('game'), 120)
            if not name or not game:
                raise ValueError('姓名与游戏不能为空')
            price = None if body.get('price') in ('', None) else float(body['price'])
            if price is not None and (not math.isfinite(price) or price < 0 or price > 100000):
                raise ValueError('价格范围无效')
            existing_member = required(c, 'members', body['id']) if body.get('id') else None
            services = body.get('service_types', json.loads(existing_member['service_types']) if existing_member else [])
            if not isinstance(services, list) or any(s not in SERVICE_TYPES for s in services):
                raise ValueError('请选择支持的服务类型')
            services = list(dict.fromkeys(services))
            rank_label = clean(body.get('rank_label', existing_member['rank_label'] if existing_member else ''), 80)
            values = (name, game, clean(body.get('region')), clean(body.get('skill')), price, clean(body.get('availability')), int(bool(body.get('available', True))))
            if body.get('id'):
                required(c, 'members', body['id'])
                c.execute('UPDATE members SET name=?,game=?,region=?,skill=?,price=?,availability=?,available=? WHERE id=?', (*values, int(body['id'])))
                member_id = int(body['id'])
            else:
                member_id = c.execute('INSERT INTO members(name,game,region,skill,price,availability,available) VALUES(?,?,?,?,?,?,?)', values).lastrowid
            c.execute('UPDATE members SET service_types=?,rank_label=? WHERE id=?', (json.dumps(services, ensure_ascii=False), rank_label, member_id))
        elif action == 'member-toggle':
            row = required(c, 'members', body['id'])
            c.execute('UPDATE members SET available=? WHERE id=?', (not row['available'], row['id']))
        elif action == 'review':
            row = required(c, 'comments', body['id'])
            category = body.get('category')
            if category not in LABELS:
                raise ValueError('分类无效')
            if 'review_token' in body and body['review_token'] != review_token(row):
                raise ValueError('这条评论或判断已更新，请关闭弹窗、刷新并重新核对；本次未覆盖')
            reason = clean(body.get('reason'))
            previous_fields = json.loads(row['manual_fields'])
            fields = previous_fields
            if 'manual_fields' in body:
                if not body.get('review_token'):
                    raise ValueError('纠正或撤销字段前，请刷新并重新打开核对表单')
                if not reason:
                    raise ValueError('请填写纠正或撤销字段的人工判断依据')
                fields = validate_review_fields(body['manual_fields'])
            reason = reason or '人工核对原文后调整分类'
            import analysis_store
            analysis_store.capture_rule(c, 'comment', row['id'])
            reviewed_at = now()
            snapshot = {'raw_text': row['raw_text'], 'category': category, 'reason': reason,
                'manual_fields': fields, 'previous_fields': previous_fields,
                'previous_category': row['category'], 'rule_game': row['game'], 'rule_facts': json.loads(row['facts'])}
            c.execute('INSERT INTO comment_reviews(comment_id,snapshot,created_at) VALUES(?,?,?)', (row['id'], json.dumps(snapshot, ensure_ascii=False), reviewed_at))
            c.execute("UPDATE comments SET category=?,analysis_method='human',reason=?,manual_fields=?,confidence=NULL,analyzed_at=? WHERE id=?", (category, reason, json.dumps(fields, ensure_ascii=False), reviewed_at, row['id']))
            event(c, 'review', f"人工确认评论 {body['id']} 为{LABELS[category]}")
            if mode == 'live':
                import comment_keywords
                comment_keywords.observe(c, row['id'])
        elif action == 'lead':
            row = required(c, 'leads', body['id'])
            stage = body.get('stage', row['stage'])
            if stage not in ('new', 'reviewed', 'following', 'referred', 'won', 'lost'):
                raise ValueError('跟进阶段无效')
            member = body.get('assigned_member', row['assigned_member']) or None
            if member:
                required(c, 'members', member)
            note = clean(body.get('outcome_note', row['outcome_note']))
            if stage == 'referred' and not note:
                raise ValueError('请填写导流确认记录，例如用户确认咨询的时间和承接方；不要只凭已发送判断导流成功')
            c.execute('UPDATE leads SET stage=?,owner=?,assigned_member=?,outcome_note=?,updated_at=? WHERE id=?', (stage, clean(body.get('owner', row['owner']), 120), member, note, now(), row['id']))
            event(c, 'lead', f"更新线索 {row['id']} 的跟进记录")
        elif action == 'contact':
            row = required(c, 'leads', body['lead_id'])
            basis = body.get('contact_basis', '')
            if basis not in ('', 'inbound', 'opt_in', 'test') or (basis == 'test' and mode != 'demo'):
                raise ValueError('联系依据无效')
            note = clean(body.get('contact_note'))
            if basis and not note:
                raise ValueError('请记录用户咨询或同意联系的依据')
            c.execute('UPDATE people SET contact_basis=?,contact_note=?,do_not_contact=? WHERE id=?', (basis, note, int(bool(body.get('do_not_contact'))), row['person_id']))
            event(c, 'contact', f"更新线索 {row['id']} 的联系依据；这不会授予平台发送权限")
        elif action == 'draft':
            lead = required(c, 'leads', body['lead_id'])
            content, request_id = clean(body.get('content'), 2000), clean(body.get('request_id'), 150)
            if not content or not request_id:
                raise ValueError('消息内容和请求标识不能为空')
            old = c.execute('SELECT * FROM message_jobs WHERE request_id=?', (request_id,)).fetchone()
            if old and (old['lead_id'] != lead['id'] or old['content'] != content):
                raise ValueError('请求标识已被其他草稿使用')
            c.execute("INSERT OR IGNORE INTO message_jobs(lead_id,request_id,content,status,created_at,updated_at) VALUES(?,?,?,'draft',?,?)", (lead['id'], request_id, content, now(), now()))
            return dict(c.execute('SELECT * FROM message_jobs WHERE request_id=?', (request_id,)).fetchone())
        elif action == 'send':
            job = required(c, 'message_jobs', body['id'])
            lead = required(c, 'leads', job['lead_id'])
            person = required(c, 'people', lead['person_id'])
            if job['status'] == 'demo_sent':
                return {'status': 'demo_sent', 'detail': '该演示任务已经执行，未重复投递'}
            if c.execute('SELECT 1 FROM uid_message_attempts WHERE job_id=?', (job['id'],)).fetchone():
                return {'status': job['status'], 'detail': job['detail']}
            if person['do_not_contact'] or not person['contact_basis']:
                status, detail = 'blocked', '缺少联系依据或用户已拒绝联系；未发送'
            elif mode == 'live':
                status, detail = 'not_connected', '真实私信通道未接通；未向抖音提交请求'
            else:
                status, detail = 'demo_sent', '仅在隔离演示工作区生成消息记录；无真实收件人'
                c.execute("INSERT OR IGNORE INTO messages(lead_id,job_id,direction,content,status,created_at) VALUES(?,?,'outbound',?,'demo',?)", (lead['id'], job['id'], job['content'], now()))
            c.execute('UPDATE message_jobs SET status=?,detail=?,updated_at=? WHERE id=?', (status, detail, now(), job['id']))
            event(c, 'message', detail)
            return {'status': status, 'detail': detail}
        elif action == 'settings':
            for key in {'club_name', 'keywords', 'excluded', 'analysis_workers'} & body.keys():
                value = max(1, min(16, int(body[key]))) if key == 'analysis_workers' else clean(body[key], 2000)
                c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value, ensure_ascii=False), key))
        else:
            raise ValueError('不支持的操作')
        return {'saved': True}


def seed_demo():
    with LOCKS['demo']:
        init('demo')
        with db('demo') as c:
            if c.execute('SELECT 1 FROM comments LIMIT 1').fetchone():
                return
            c.execute("UPDATE sources SET name='演示数据 · 非真实抖音数据' WHERE id=1")
            c.execute("UPDATE settings SET value=? WHERE key='club_name'", (json.dumps('无畏契约 · 演示俱乐部', ensure_ascii=False),))
        samples = [
            ('演示用户 01', '无畏契约娱乐开黑', '今晚国服找个无畏契约陪玩，娱乐开黑，预算100', 'demo-u1', 'demo-v1', 3),
            ('演示用户 02', '无畏契约新手陪练', '无畏契约新手想找个陪练，练枪怎么收费？国服', 'demo-u2', 'demo-v2', 9),
            ('演示用户 03', '无畏契约娱乐开黑', '瓦陪玩接单，国服娱乐开黑，今晚有空', 'demo-u3', 'demo-v1', 12),
            ('演示用户 04', '无畏契约排位组队', '无畏契约今晚只找队友，不找收费的', 'demo-u4', 'demo-v3', 22),
            ('演示用户 05', '无畏契约对局复盘', '无畏契约复盘陪练接单，有老板吗', 'demo-u5', 'demo-v4', 36),
            ('演示用户 06', '无畏契约对局复盘', '国服无畏契约复盘多少钱，周末可以吗？', 'demo-u6', 'demo-v4', 41),
            ('演示用户 01', '无畏契约排位组队', '无畏契约国服，想预约陪玩，排位双排', 'demo-u1', 'demo-v3', 48),
            ('演示用户 07', '无畏契约排位组队', '哈哈太厉害了', 'demo-u7', 'demo-v3', 55),
            ('演示用户 08', '无畏契约陪玩俱乐部', '无畏契约俱乐部招陪玩，时间自由', 'demo-u8', 'demo-v5', 72),
            ('演示用户 09', '无畏契约排位组队', '无畏契约国服找个排位陪玩，段位白银，明天晚上', 'demo-u9', 'demo-v3', 91),
            ('演示用户 10', '无畏契约娱乐开黑', 'VALORANT找搭子开黑，有人吗', 'demo-u10', 'demo-v1', 102),
        ]
        records = [{'comment_id': f'demo-c{i}', 'video_id': vid, 'video_title': title, 'text': text, 'nickname': name, 'user_id': user, 'published_at': (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()} for i, (name, title, text, user, vid, minutes) in enumerate(samples, 1)]
        ingest({'records': records}, 'demo')
        analyze('demo')
        for name, region, skill, price, availability, available, services in [
            ('演示陪玩 A', '国服', '轻松聊天 / 娱乐组队', 40, '今晚 19:00–24:00', True, ['娱乐开黑']),
            ('演示陪玩 B', '国服', '基础练枪 / 地图讲解', 60, '今晚 20:00–23:00', True, ['新手陪练', '对局复盘']),
            ('演示陪玩 C', '亚服', '对局复盘 / 配合沟通', 75, '周末 14:00–22:00', True, ['排位组队', '对局复盘']),
            ('演示陪玩 D', '国服', '双排组队 / 沟通配合', 50, '明晚 18:00–23:00', False, ['排位组队']),
        ]:
            mutate('member', dict(name=name, game=TARGET_GAME, region=region, skill=skill, price=price, availability=availability, available=available, service_types=services), 'demo')
        with db('demo') as c:
            c.execute("UPDATE people SET contact_basis='test',contact_note='仅演示会话，不对应真实账号' WHERE external_id IN ('demo-u1','demo-u2')")
            first = c.execute("SELECT l.id FROM leads l JOIN people p ON l.person_id=p.id WHERE p.external_id='demo-u1'").fetchone()[0]
            c.execute("INSERT INTO messages(lead_id,direction,content,status,created_at) VALUES(?,'inbound','这是演示消息：想了解今晚无畏契约国服娱乐陪玩的安排。','demo',?)", (first, now()))
