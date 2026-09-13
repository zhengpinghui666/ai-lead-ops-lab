"""One game vocabulary for discovery, assets, groups and intent evidence."""
import json
from pathlib import Path
import re

CONFIG = json.loads(Path(__file__).with_name('game_scope.json').read_text('utf-8'))
ALIASES = CONFIG['aliases']
GAME_PATTERN = re.compile('|'.join(map(re.escape, ALIASES))+'|'+CONFIG['isolated_pattern'], re.I)
MOBILE_PATTERN = re.compile(CONFIG['mobile_pattern'], re.I)
DESKTOP_PATTERN = re.compile(CONFIG['desktop_pattern'], re.I)


def exclusion_reason(*contexts):
    """Recognition vocabulary includes mobile names; service eligibility excludes them.

    Ambiguous/mixed version mentions need review, never an automatic paid outreach.
    An unqualified game alias is still usable with the existing intent context gates.
    """
    text=' '.join(str(value or '') for value in contexts)
    if (GAME_PATTERN.search(text) or re.search('源能行[动動]',text)) and MOBILE_PATTERN.search(text):
        return ('端手游信息混杂，需核对端游需求；不自动触达。' if DESKTOP_PATTERN.search(text)
                else '包含手游范围，Mimo 仅承接端游无畏契约；不自动触达。')
    return ''


def in_pc_scope(text):
    return bool(GAME_PATTERN.search(text or '')) and not exclusion_reason(text)


def record_exclusion(c, kind, record_id):
    """Recheck current source and literal message/parent before model and submission.

    Does not modify historical classifications or manufacture model judgments.
    """
    if kind=='comment':
        row=c.execute('''SELECT x.raw_text,v.title,COALESCE(p.raw_text,'') AS parent
          FROM comments x JOIN videos v ON v.id=x.video_id
          LEFT JOIN comments p ON p.source_id=x.source_id AND p.external_id=x.parent_external_id
            AND p.video_id=x.video_id WHERE x.id=?''',(record_id,)).fetchone()
    elif kind=='live':
        row=c.execute('''SELECT m.raw_text,COALESCE(r.title,'') AS current_title,
          CASE WHEN r.source='valorant_category' THEN '无畏契约' ELSE '' END AS category_context
          FROM live_messages m JOIN live_sessions s ON s.id=m.session_id
          LEFT JOIN live_rooms r ON r.room_url=s.room_url WHERE m.id=?''',(record_id,)).fetchone()
    elif kind=='group':
        row=c.execute('''SELECT m.raw_text,m.group_title,g.name,g.description,g.notice
          FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id WHERE m.id=?''',(record_id,)).fetchone()
    else:raise ValueError('原文类型无效')
    return exclusion_reason(*row) if row else '原文不存在，无法核对端游范围。'


def enforce_saved_scope(c):
    """One local migration: retire incompatible sources, retain every evidence row."""
    for table,key,title in [('videos','id','title'),('discovery_works','video_id','title'),('live_rooms','room_url','title')]:
        for row in c.execute(f'SELECT {key},{title} FROM {table}').fetchall():
            if exclusion_reason(row[1]):
                c.execute(f'UPDATE {table} SET enabled=0 WHERE {key}=?',(row[0],))
                if table=='discovery_works':
                    c.execute('UPDATE discovery_works SET relevant=0,next_check_at=NULL WHERE video_id=?',(row[0],))
    for row in c.execute('SELECT id,name,description,notice FROM monitored_groups').fetchall():
        reason=exclusion_reason(*tuple(row)[1:])
        if reason:c.execute("UPDATE monitored_groups SET matched=0,enabled=0,status='paused',detail=?,next_run_at=NULL WHERE id=?",(reason,row[0]))
    for row in c.execute('SELECT account_uid,group_id,name,description FROM public_group_candidates').fetchall():
        if exclusion_reason(row[2],row[3]):
            c.execute('UPDATE public_group_candidates SET matched=0 WHERE account_uid=? AND group_id=?',(row[0],row[1]))


def game_quote(quote, original):
    """The quote must enclose a full game match in its actual source context.

    A standalone 瓦 or #瓦 is valid; a substring cut from 瓦片/瓦工 is not.
    Do not re-run the boundary regex on a stripped single-character quote.
    """
    start = original.find(quote)
    return bool(quote and start >= 0 and any(start <= m.start() and m.end() <= start+len(quote)
                                           for m in GAME_PATTERN.finditer(original)))
