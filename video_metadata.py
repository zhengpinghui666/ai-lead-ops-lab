"""Public work statistics snapshots; unknown counters stay unknown."""
from datetime import datetime, timezone
import json

COUNTERS = ('likes','comments','shares','favorites')


def extract(item):
    stats = item.get('statistics') if isinstance(item.get('statistics'),dict) else {}
    values = {}
    for name,key in zip(COUNTERS,('digg_count','comment_count','share_count','collect_count')):
        value=stats.get(key)
        values[name]=value if type(value) is int and 0<=value<2**53 else None
    return {**values,'updated_at':datetime.now(timezone.utc).isoformat()}


def stale(metrics):
    try:
        stamp=datetime.fromisoformat(metrics['updated_at'])
        return stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() < 300
    except (ValueError,TypeError,KeyError):
        return True


def save(c, video_id, metrics):
    if not isinstance(metrics,dict):return
    stamp=metrics.get('updated_at')
    try:
        if datetime.fromisoformat(stamp).tzinfo is None:return
    except (ValueError,TypeError):return
    payload={k:metrics[k] if type(metrics.get(k)) is int and 0<=metrics[k]<2**53 else None for k in COUNTERS}
    c.execute("""INSERT INTO video_metadata(video_id,payload_json,updated_at) VALUES(?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at
        WHERE julianday(excluded.updated_at)>=julianday(video_metadata.updated_at)""",
        (video_id,json.dumps(payload),stamp))


def project(c, video_id):
    row=c.execute('SELECT payload_json,updated_at FROM video_metadata WHERE video_id=?',(video_id,)).fetchone()
    return {**json.loads(row['payload_json']),'updated_at':row['updated_at']} if row else None
