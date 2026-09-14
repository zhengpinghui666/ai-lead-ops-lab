"""Page the canonical current lead projection; keep classification in analysis_store."""
import math
import re
from datetime import datetime, timedelta, timezone

KEYS = {'lead_page','lead_page_size','lead_tab','lead_query','lead_game','lead_service',
        'lead_published','lead_from','lead_until','lead_sort'}
WINDOWS = {'all','hour','day','week','month','custom','unknown','future'}


def arguments(query):
    if any(k.startswith('lead_') and k not in KEYS | {'lead_id'} for k in query):
        raise ValueError('需求筛选参数无效')
    if not any(k in KEYS for k in query):
        return None
    values = {}
    for key in KEYS:
        entries = query.get(key, [''])
        if len(entries) != 1 or not isinstance(entries[0], str):
            raise ValueError('需求筛选参数不能重复')
        values[key.removeprefix('lead_')] = entries[0]
    for key, default, maximum in [('page',1,1000000),('page_size',30,100)]:
        value = values[key] or str(default)
        if not re.fullmatch(r'[0-9]{1,7}', value) or not 1 <= int(value) <= maximum:
            raise ValueError('需求页码或每页数量无效')
        values[key] = int(value)
    values['tab'] = values['tab'] or 'buyer'
    values['published'] = values['published'] or 'all'
    values['sort'] = values['sort'] or 'published'
    if values['tab'] not in {'buyer','supply','club','uncertain','all'} or values['published'] not in WINDOWS or values['sort'] not in {'published','discovered'}:
        raise ValueError('需求筛选条件无效')
    if len(values['query']) > 300 or len(values['game']) > 100 or len(values['service']) > 100:
        raise ValueError('需求搜索条件过长')
    if values['published'] != 'custom':
        values['from'] = values['until'] = ''
    elif not values['from'] and not values['until']:
        raise ValueError('请至少选择开始或结束日期')
    for key in ('from','until'):
        if values[key]:
            date_bound(values[key])
    if values['from'] and values['until'] and values['from'] > values['until']:
        raise ValueError('开始日期不能晚于结束日期')
    return values


def date_bound(value):
    try:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError()
        return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('日期无效，请重新选择') from None


def stamp(value):
    try:
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        result = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def page(state, options, target_game, *, reference=None):
    """Filtering precedes paging and always uses current human/model/rule results."""
    reference = datetime.now(timezone.utc).timestamp() if reference is None else reference
    focused = lambda game: not game or game == target_game
    summary = dict(day=0,week=0,unknown=0,future=0)
    all_comments = state['comments']
    for comment in all_comments:
        if not focused(comment.get('game')):
            continue
        when = stamp(comment.get('published_at'))
        if when is None:
            summary['unknown'] += 1
        elif when > reference:
            summary['future'] += 1
        else:
            age = reference - when
            summary['day'] += age <= 86400
            summary['week'] += age <= 604800
    cats = None if options['tab']=='all' else {'seller','recruit'} if options['tab']=='supply' else {options['tab']}
    start = date_bound(options['from']) if options['from'] else None
    end = date_bound(options['until']) + 86400 if options['until'] else None
    query = options['query'].lower()
    def matches(lead):
        if cats is not None and lead['category'] not in cats:
            return False
        game, selected = lead.get('game'), options['game']
        if not (bool(game) and game != target_game if selected=='other' else game==selected if selected else focused(game)):
            return False
        row = lead['latest']
        if options['service'] and (row.get('facts') or {}).get('service_type') != options['service']:
            return False
        if query and query not in ' '.join(str(v or '') for v in (lead.get('nickname'),row.get('raw_text'),lead.get('external_id'))).lower():
            return False
        period, when = options['published'], stamp(row.get('published_at'))
        if period == 'all':
            return True
        if period == 'unknown':
            return when is None
        if when is None:
            return False
        if period == 'future':
            return when > reference
        if period == 'custom':
            return (start is None or when >= start) and (end is None or when < end)
        hours = {'hour':1,'day':24,'week':168,'month':720}[period]
        return reference-hours*3600 <= when <= reference
    column = 'discovered_at' if options['sort']=='discovered' else 'published_at'
    def order(lead):
        value = stamp(lead['latest'].get(column))
        return value is not None, value or 0, lead['id']
    items = sorted(filter(matches, state['leads']), key=order, reverse=True)
    total = len(items)
    pages = max(1, math.ceil(total/options['page_size']))
    number = min(options['page'], pages)
    offset = (number-1)*options['page_size']
    state['leads'] = items[offset:offset+options['page_size']]
    state['comments'] = [c for c in all_comments if not c.get('person_id')]
    state['lead_list'] = dict(page=number,page_size=options['page_size'],pages=pages,total=total,
        start=offset+1 if total else 0,end=min(offset+options['page_size'],total),
        comment_count=len(all_comments),unlinked_count=len(state['comments']),publication_summary=summary,
        filters={k:v for k,v in options.items() if k not in ('page','page_size')})
    # These archives belong to demand details and the inbox, not every list page.
    state.update(jobs=[],messages=[],members=[],events=[])
    return state
