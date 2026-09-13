"""Shared publication-time window for proactive outreach, not historical labels."""
from datetime import datetime, timezone

MAX_AGE_SECONDS = 86400
TABLES = {'comment': 'comments', 'live': 'live_messages', 'group': 'group_messages'}
DETAILS = {
    'current': '原文需求在一天内',
    'expired': '原文需求已超过一天，不自动联系',
    'unknown': '缺少可核对的原文发布时间，暂不自动联系',
    'future': '原文发布时间晚于当前时间，需要核对',
    'missing': '原文记录不存在，不能据此联系',
}


def assess(published_at, *, now=None):
    """Never substitute ingestion, model or retry time for publication time."""
    current = now if now is not None else datetime.now(timezone.utc)
    result = dict(status='unknown', eligible=False, max_age_seconds=MAX_AGE_SECONDS)
    try:
        if not isinstance(published_at, str) or not 1 <= len(published_at) <= 64:
            raise ValueError()
        published = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        if published.tzinfo is None or published.utcoffset() is None or current.tzinfo is None:
            raise ValueError()
        age = (current - published).total_seconds()
        status = 'future' if age < 0 else 'expired' if age > MAX_AGE_SECONDS else 'current'
        result.update(status=status, eligible=status == 'current', published_at=published_at, age_seconds=round(age, 3))
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    result['detail'] = DETAILS[result['status']]
    return result


def record(c, kind, record_id, *, now=None):
    if kind not in TABLES or type(record_id) is not int or not 0 < record_id < 2**63:
        raise ValueError('需求来源无效')
    columns = 'published_at,observed_at' if kind == 'live' else 'published_at'
    row = c.execute('SELECT ' + columns + ' FROM ' + TABLES[kind] + ' WHERE id=?', (record_id,)).fetchone()
    result = assess(row['published_at'], now=now) if row else dict(status='missing', eligible=False,
        max_age_seconds=MAX_AGE_SECONDS, detail=DETAILS['missing'])
    result['time_basis'] = 'published_at'
    # User confirmed on 2026-09-13: use this live reception when the platform
    # omits publication time. Keep it distinct; never rewrite published_at.
    if kind == 'live' and row and row['published_at'] in (None, ''):
        result = assess(row['observed_at'], now=now)
        received = result.pop('published_at', None)
        result.update(time_basis='received_at', detail=result['detail'].replace('原文发布时间', '本机接收时间').replace('原文需求', '直播弹幕'))
        if received:
            result['received_at'] = received
    return dict(result, evidence_type=kind, record_id=record_id)


class FreshnessError(ValueError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__(evidence['detail'])


def require(c, kind, record_id):
    result = record(c, kind, record_id)
    if not result['eligible']:
        raise FreshnessError(result)
    return result
