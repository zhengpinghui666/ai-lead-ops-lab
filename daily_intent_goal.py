"""Daily acquisition target using current supported evidence, never raw model hits."""
from collections import Counter
from datetime import datetime,timezone
import math
import json
import analysis_store
import demand_freshness
import game_scope
import service_roles
import author_roles

TARGET = 100  # User's daily acceptance metric, confirmed 2026-09-15.


def summary(c, start, end, *, model_engine=None, games=('无畏契约',)):
    # Keep the historical first-hit dashboard unchanged. This separate metric
    # rechecks today's qualifying user against the current evidence/role guards.
    candidates=c.execute("""SELECT r.*,COALESCE(p.external_id,l.uid,g.uid) AS uid
      FROM intent_results r
      LEFT JOIN comments x ON r.evidence_type='comment' AND x.id=r.record_id
      LEFT JOIN people p ON p.id=x.person_id
      LEFT JOIN live_messages l ON r.evidence_type='live' AND l.id=r.record_id
      LEFT JOIN group_messages g ON r.evidence_type='group' AND g.id=r.record_id
      WHERE r.method='model' AND r.status='completed' AND json_valid(r.result_json)
        AND json_extract(r.result_json,'$.category')='buyer'
        AND julianday(r.finished_at)<=julianday(?)
      ORDER BY julianday(r.finished_at),r.id""",(end.isoformat(),)).fetchall()
    cache={};users={};excluded=Counter();today_raw=set()
    for result in candidates:
        try:at=datetime.fromisoformat(result['finished_at'].replace('Z','+00:00'))
        except (ValueError,TypeError):continue
        if at.tzinfo is None:continue
        uid=str(result['uid'] or '').strip();today=start<=at<=end
        if today and uid:today_raw.add(uid)
        key=(result['evidence_type'],result['record_id'])
        if key not in cache:
            reason='';source=None;archive=None;fingerprint=None;current=None
            try:
                source,archive=analysis_store.inputs(c,*key)
                fingerprint=analysis_store.digest(source)
                current=analysis_store.latest(c,*key,'model',fingerprint)
                if not current or current['status']!='completed' or not analysis_store.compatible_engine(current['engine'],model_engine):
                    reason='unconfirmed_current_evidence'
                else:
                    row=dict(current['result'],id=key[1],uid=uid,evidence_type=key[0],analysis_method='model')
                    if archive['analysis_method']=='human':row.update(category=archive['category'],game=archive['game'],analysis_method='human')
                    if key[0]=='live':
                        manual=c.execute('SELECT category,manual_fields FROM live_judgments WHERE message_id=?',(key[1],)).fetchone()
                        if manual:row.update(category=manual['category'],game=json.loads(manual['manual_fields']).get('game',row.get('game')),analysis_method='human')
                    # An explicit rejection of a service is not a paying lead,
                    # even if a historical model snapshot still says buyer.
                    service_roles.project(row,source);author_roles.project(c,row,source)
                    if row.get('category')!='buyer' or row.get('analysis_method') not in ('model','human'):reason='role_reclassified'
                    elif row.get('game') not in games:reason='game_unconfirmed_or_unsupported'
                    elif ('filter_reason' in archive.keys() and archive['filter_reason']) or game_scope.record_exclusion(c,*key):reason='outside_service_scope'
            except (ValueError,KeyError,TypeError):reason='unconfirmed_current_evidence'
            cache[key]=(reason,fingerprint)
        reason,fingerprint=cache[key]
        if not reason and result['input_hash']!=fingerprint:reason='changed_evidence'
        if not reason and not demand_freshness.record(c,*key,now=at)['eligible']:reason='not_fresh_when_identified'
        if not uid:reason='missing_user'
        if reason:
            if today:excluded[reason]+=1
            continue
        if uid not in users:users[uid]=(at,key[0])
    achieved=sum(start<=at<=end for at,_ in users.values())
    elapsed=max(0,min(86400,(end-start).total_seconds()))
    expected=min(TARGET,math.floor(TARGET*elapsed/86400))
    daily=Counter(at.astimezone(start.tzinfo).date().isoformat() for at,_ in users.values())
    return dict(target=TARGET,achieved=achieved,remaining=max(0,TARGET-achieved),
        progress_percent=round(achieved/TARGET*100,1),expected_by_now=expected,
        behind_by=max(0,expected-achieved),status='met' if achieved>=TARGET else 'behind' if achieved<expected else 'in_progress',
        raw_model_users_today=len(today_raw),excluded_records=dict(excluded),daily=dict(daily),
        definition='当天首次有效识别的独立用户；按当前原文与角色复核，识别时需求在24小时内；跨评论、直播、群聊去重',
        accuracy='模型与规则确认，不代表已同意付费；人工复核纠正后重新计算',
        source_counts=dict(Counter(kind for at,kind in users.values() if start<=at<=end)))
