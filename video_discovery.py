"""Bounded public-video discovery. Candidates are not proof of relevance/coverage.

Uses the existing HTTP client and encrypted session; never starts a browser,
follows arbitrary URLs, or retries an upstream rejection.
"""
import re
import time
import video_metadata

from collector_http import ReadError, numeric
from collector_http_session import ORIGIN

from game_scope import GAME_PATTERN, in_pc_scope, exclusion_reason

PRIVATE_WORK_DETAIL = '平台明确返回该作品受作者隐私设置限制；已停止跟踪该作品，其他公开作品继续采集'
WORK_RESTRICTION_DETAILS = {
    'author_secret': PRIVATE_WORK_DETAIL,
    'status_self_see': '平台明确返回该作品因权限或已被删除而无法观看；已停止跟踪该作品，其他公开作品继续采集',
    'status_audit_self_see': '平台返回该作品当前仅作者可见（status_audit_self_see）；已停止跟踪该作品，其他公开作品继续采集',
}


def work_restriction(error, video):
    reason = error.evidence.get('reason')
    return reason if (error.status == 'access_denied' and reason in WORK_RESTRICTION_DETAILS
        and error.evidence.get('restriction_scope') == 'work' and error.evidence.get('video_id') == video) else None


def is_private_work(error, video):
    return (error.status == 'access_denied' and error.evidence.get('reason') == 'author_secret'
            and error.evidence.get('restriction_scope') == 'work' and error.evidence.get('video_id') == video)


def in_search_scope(row, keyword):
    """Game evidence for discovery only; never a paid-intent classification."""
    title=str(row.get('video_title') or '')
    return not exclusion_reason(title,keyword) and (not GAME_PATTERN.search(keyword) or in_pc_scope(title))


def video_row(item):
    if not isinstance(item, dict) or not numeric(item.get('aweme_id')):
        return None
    vid = numeric(item['aweme_id'])
    author = item.get('author') if isinstance(item.get('author'), dict) else {}
    sec_uid = author.get('sec_uid', '')
    if not isinstance(sec_uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', sec_uid):
        sec_uid = ''
    published = item.get('create_time')
    stats = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
    comments = stats.get('comment_count')
    tags=[r['hashtag_name'] for r in (item.get('text_extra') or []) if isinstance(r,dict) and isinstance(r.get('hashtag_name'),str)] if isinstance(item.get('text_extra'),list) else []
    return {'video_id': vid, 'video_title': str(item.get('desc') or vid)[:5000], 'tags':tags[:30],
            'video_url': ORIGIN + '/video/' + vid, 'author_sec_uid': sec_uid,
            'author_nickname': str(author.get('nickname') or '')[:120],
            'published_at': published if type(published) is int and 0 < published < 32503680000 else None,
            'comment_count': comments if type(comments) is int and comments >= 0 else None,
            'metrics': video_metadata.extract(item)}


def discovery_shape(body):
    """Whitelist structure and paging evidence; no content or identity fields."""
    result = {'version': 'http-discovery-shape-v1', 'body_type': type(body).__name__}
    if not isinstance(body, dict):
        return result
    for key in ('status_code', 'has_more', 'max_cursor', 'aweme_list', 'aweme_detail'):
        value = body.get(key)
        result[key + '_type'] = type(value).__name__ if key in body else 'missing'
        if type(value) is int and -(2**63) <= value < 2**63:
            result[key] = value
    result['verification_indicated'] = bool(body.get('verify_type') or body.get('verify_data'))
    if isinstance(body.get('aweme_list'), list):
        result['item_count'] = len(body['aweme_list'])
    restriction = body.get('filter_detail')
    if isinstance(restriction,dict):
        reason = restriction.get('filter_reason')
        if isinstance(reason,str) and re.fullmatch(r'[a-z_]{1,60}',reason):result['filter_reason']=reason
        result['filter_video_id_valid']=bool(numeric(restriction.get('aweme_id')))
    return result


def parse_discovery(body, operation, *, video='', sec_uid='', requested_cursor=0):
    if not isinstance(body, dict):
        raise ReadError('schema_changed', {'reason': 'invalid_discovery_body'})
    if body.get('verify_type') or body.get('verify_data'):
        raise ReadError('needs_verification')
    if type(body.get('status_code')) is not int:
        raise ReadError('schema_changed', {'reason': 'invalid_status_code'})
    if body['status_code']:
        raise ReadError('upstream_rejected', {'business_code': body['status_code']})
    if operation == 'detail':
        restriction = body.get('filter_detail')
        if (body.get('aweme_detail') is None and isinstance(restriction, dict)
                and restriction.get('filter_reason') in WORK_RESTRICTION_DETAILS
                and numeric(restriction.get('aweme_id')) == video and numeric(video)):
            raise ReadError('access_denied', {'reason': restriction['filter_reason'], 'restriction_scope': 'work', 'video_id': video})
        row = video_row(body.get('aweme_detail'))
        if row is None or row['video_id'] != video:
            raise ReadError('schema_changed', {'reason': 'invalid_video_detail'})
        rows, skipped, more, cursor = [row], 0, 0, 0
    elif operation in ('author', 'related'):
        items = body.get('aweme_list')
        more = body.get('has_more', 0 if operation == 'related' else None)
        cursor = body.get('max_cursor', 0 if operation == 'related' or more == 0 else None)
        if not isinstance(items, list) or len(items) > 1000:
            raise ReadError('schema_changed', {'reason': 'invalid_discovery_container'})
        if type(more) is not int or more not in (0, 1):
            raise ReadError('schema_changed', {'reason': 'invalid_has_more'})
        if type(cursor) is not int or not 0 <= cursor < 2**63:
            raise ReadError('schema_changed', {'reason': 'invalid_cursor'})
        # Author max_cursor walks backwards through time. A valid empty page
        # can still have a next page; consume a page budget and keep paging.
        if operation == 'author' and more and (cursor == 0 or requested_cursor > 0 and cursor >= requested_cursor):
            raise ReadError('schema_changed', {'reason': 'non_advancing_author_cursor'})
        if operation != 'author' and not items and more:
            raise ReadError('schema_changed', {'reason': 'empty_page_with_more'})
        rows, seen, skipped = [], set(), 0
        for item in items:
            row = video_row(item)
            if row is None or operation == 'author' and row['author_sec_uid'] != sec_uid:
                skipped += 1
                continue
            if row['video_id'] not in seen:
                seen.add(row['video_id'])
                rows.append(row)
        if items and not rows:
            raise ReadError('schema_changed', {'reason': 'invalid_discovery_records'})
    else:
        raise ValueError('发现入口无效')
    result = {'rows': rows, 'cursor': cursor, 'has_more': bool(more), 'skipped': skipped,
            'skipped_reasons': {'invalid_record': skipped}, 'reply_targets': [], 'search_id': ''}
    if operation == 'author' and not rows and more:
        result['page_visibility'] = {'state': 'empty_page_with_more', 'returned_rows': 0}
    return result


def discover(client, seeds, *, author_pages=1, page_size=10, include_related=True):
    """One-hop expansion, at most 3 seeds × (detail + author pages + related).

    Stops the whole expansion on a failed read; exposes already accepted
    candidates with provenance. Caller decides which candidates to monitor.
    """
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 3 or any(not numeric(v) for v in seeds)
            or type(author_pages) is not int or not 1 <= author_pages <= 3
            or type(page_size) is not int or not 1 <= page_size <= 20 or type(include_related) is not bool):
        raise ValueError('发现预算无效')
    candidates, authors, details, failures = {}, set(), [], []
    operation, seed = 'detail', ''
    def accept(page, source, seed_id):
        stamp = time.time()
        for row in page['rows'][:page_size]:
            entry = candidates.setdefault(row['video_id'], {**row, 'first_discovered_at': stamp, 'provenance': []})
            provenance = {'kind': source, 'seed_video_id': seed_id}
            if provenance not in entry['provenance']:
                entry['provenance'].append(provenance)
    try:
        for seed in dict.fromkeys(map(str, seeds)):
            operation = 'detail'
            detail = client.page(operation, video=seed, count=1)
            details.extend(detail['rows'])
            sec = detail['rows'][0]['author_sec_uid']
            if sec and sec not in authors:
                authors.add(sec)
                cursor, visited = 0, set()
                for _ in range(author_pages):
                    operation = 'author'
                    page = client.page(operation, sec_uid=sec, cursor=cursor, count=page_size)
                    accept(page, operation, seed)
                    if not page['has_more']:
                        break
                    if page['cursor'] == cursor or page['cursor'] in visited:
                        raise ReadError('schema_changed')
                    visited.add(cursor)
                    cursor = page['cursor']
            if include_related:
                operation = 'related'
                page = client.page(operation, video=seed, count=page_size)
                accept(page, operation, seed)
    except ReadError as exc:
        failures.append({'operation': operation, 'seed_video_id': seed, 'status': exc.status})
    return {'status': 'partial' if failures else 'completed', 'candidates': list(candidates.values()),
            'seed_details': details, 'failures': failures, 'browser_used': False,
            'scope': 'bounded_author_and_related_candidates' if include_related else 'bounded_author_candidates', 'all_douyin': False,
            'comment_freshness_verified': False}


def author_candidates(result):
    relevant = [row for row in result['candidates'] if in_pc_scope(row['video_title'])]
    relevant.sort(key=lambda row: (row.get('published_at') or 0, row['video_id']), reverse=True)
    return relevant[:50]


def select_author_targets(result, limit):
    """Select this product's game scope, newest first within the bounded response.

    This is a discovery filter, not a paid-intent judgement. An old work can
    still receive fresh comments; publication time only prioritizes candidates.
    """
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('作者发现每批最多读取 1–5 个作品')
    relevant = author_candidates(result)
    targets = relevant[:limit]
    # Persist only public, bounded provenance. Do not include the author sec_uid
    # or raw upstream data in the user-visible diagnostic.
    selected = {row['video_id'] for row in targets}
    audit = {'scope': result['scope'], 'candidate_count': len(result['candidates']),
        'relevant_count': len(relevant), 'selected_count': len(targets), 'all_douyin': False,
        'selection': 'title_game_match_then_newest_within_response',
        'failures': result['failures'], 'candidates': [
            {k: row.get(k) for k in ('video_id', 'published_at', 'provenance')} | {'selected': row['video_id'] in selected}
            for row in result['candidates']]}
    return targets, audit
