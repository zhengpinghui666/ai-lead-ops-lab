"""JSON-line worker for the existing collector ledger; never launches a browser."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import threading
import time
import traceback
import video_metadata
import video_discovery
import candidate_pool
import comment_paging
import collection_session_refresh
import clubops as app
from pathlib import Path

import collector_http as http
import collector_http_session as sessions
import uid_bootstrap
import captcha_runtime
from collector import video_targets


def collect(config, emit, cancel, *, client=None, session=None, identity_probe=None):
    owned_client = client is None
    stopped = threading.Event()
    failure = []
    lock = threading.Lock()
    counts = {'active': 0, 'peak': 0, 'skipped': 0, 'unsupported':0}
    budget_reached = threading.Event()
    comment_response_seen = threading.Event()
    limit = min(int(config['page_concurrency']), int(config['video_limit']))
    def diagnostic(value):
        emit({'type': 'diagnostic', 'stage': 'http_read', 'snapshot': {'responses': [value]}})
    def progress(change):
        with lock:
            counts['active'] += change
            counts['peak'] = max(counts['peak'], counts['active'])
            emit({'type': 'parallel', 'active_pages': counts['active'], 'peak_pages': counts['peak'], 'page_concurrency': limit})
    try:
        if cancel.is_set():
            raise http.ReadError('cancelled')
        if client is None:
            saved_session=session is None
            try:
                # Validate structure first; the original account gate and a
                # bounded identity revalidation precede any data request.
                session = session or sessions.load(check_age=False)
            except FileNotFoundError:
                raise http.ReadError('needs_login') from None
            except Exception:
                raise http.ReadError('session_expired') from None
            assigned=config.get('collection_account')
            if assigned and (session['account']!=assigned['account_id'] or session['sender_uid']!=assigned['sender_uid']):
                raise http.ReadError('identity_failed',{'reason':'collection_account_mismatch'})
            client = http.Client(session, cancelled=lambda: cancel.is_set() or stopped.is_set(), diagnostic=diagnostic,
                                 comment_since=config.get('comment_since'))
            operation = {'search': 'search', 'author': 'detail'}.get(config['kind'], 'comments') if not config.get('resume_targets') else 'comments'
            client.check_gate(operation)
            identity=None
            if saved_session:
                proof=[]
                def verify_original_account(payload):
                    result=(identity_probe or uid_bootstrap.probe)(payload)
                    proof.append(result)
                    return result
                try:
                    session,identity=collection_session_refresh.ensure(None,
                        assigned or dict(account_id=session['account'],sender_uid=session['sender_uid']),
                        probe=verify_original_account,cancelled=lambda:cancel.is_set() or stopped.is_set(),
                        minimum_valid_seconds=300)
                    client.session=session
                    client.check_gate(operation)
                except collection_session_refresh.RefreshError as exc:
                    if exc.reason=='cancelled':raise http.ReadError('cancelled') from None
                    if exc.reason=='identity_unverified' and proof:
                        # Reuse the actual single identity response below so
                        # network errors and platform gates retain their normal
                        # diagnostic/status and bounded-recovery semantics.
                        identity=proof[0]
                    else:
                        raise http.ReadError('session_expired',{'reason':exc.reason}) from None
            identity = identity or (identity_probe or uid_bootstrap.probe)({'expected_account': session['account'],
                'cookie': sessions.cookie_header(session, 'identity'), 'user_agent': session['user_agent']})
            diagnostic({'operation': 'identity', 'transport': 'http', 'status': identity['status'],
                'identity_check_version': 'identity-check-v2', 'collection_account':session['account'],
                'collection_sender_uid':identity.get('sender_uid'),
                **{k: identity[k] for k in ('http_status', 'response_bytes', 'response_sha256', 'business_code',
                    'user_present', 'verification_indicated', 'transport_error', 'transport_phase', 'http_attempts') if k in identity}})
            if cancel.is_set():
                raise http.ReadError('cancelled')
            if identity.get('http_status') in (401,403,429):
                if identity['http_status'] == 401:
                    sessions.record_identity_status(session, 'identity_failed')
                raise http.ReadError({401:'needs_login',403:'access_denied',429:'rate_limited'}[identity['http_status']])
            if identity['status'] == 'http_failed' and not identity.get('verification_indicated'):
                # An incomplete request is not evidence that the saved account
                # is wrong. Every subsequent batch must still verify it anew.
                raise http.ReadError('network_error', {'reason': 'identity_transport_incomplete'})
            if identity['status'] != 'identity_verified' or identity.get('sender_uid') != session.get('sender_uid'):
                sessions.record_identity_status(session, 'identity_failed')
                raise http.ReadError('identity_failed')
            if identity.get('verification_indicated'):
                sessions.record_identity_status(session, 'identity_failed')
                raise http.ReadError('needs_verification')
            sessions.record_identity_status(session, 'identity_verified')
        emit({'type': 'status', 'status': 'running', 'detail': '正在通过后端 HTTP 读取；本批不启动浏览器'})
        targets = config.get('resume_targets') or []
        if not targets and config['kind'] == 'video':
            targets = video_targets(config['target'])
        if not targets and config['kind'] == 'author':
            seeds = [row['video_id'] for row in video_targets(config['target'])]
            discovery_job=config.get('discovery_job') or {}
            result = video_discovery.discover(client, seeds, author_pages=discovery_job.get('author_pages',1), page_size=10, include_related=False)
            for restriction in result.get('restricted_seeds',[]):
                if cancel.is_set():raise http.ReadError('cancelled')
                emit({'type':'discovery_restriction',**restriction})
                diagnostic({'operation':'author_seed_restriction',**restriction,'status':'skipped_restricted_work'})
            for source,records in [('author_seed',result['seed_details']),('author',result['candidates'])]:
                for at in range(0,len(records),10):
                    emit({'type':'discovery_catalog','source':source,'records':[{**r,'video_title':r['video_title'][:500]} for r in records[at:at+10]]})
            targets, audit = video_discovery.select_author_targets(result, config['video_limit'])
            if config.get('candidate_policy') and not result['failures']:
                candidates=video_discovery.author_candidates(result)
                targets=candidate_pool.select(candidates,config['video_limit'],config['candidate_policy'])
                audit['selection']=config['candidate_policy']['version']
                audit.update(candidate_pool.selection_evidence(candidates,targets,config['candidate_policy']))
                selected_ids={r['video_id'] for r in targets}
                for item in audit['candidates']:item['selected']=item['video_id'] in selected_ids
            emit({'type': 'diagnostic', 'stage': 'author_discovery', 'snapshot': {
                'title': '作者作品发现', 'responses': [audit],
                'visible_text': f"限定作者候选 {audit['candidate_count']} 个，文案匹配无畏契约 {audit['relevant_count']} 个，选择 {len(targets)} 个。仅在本次返回候选中选择；不代表全站或作者全部作品。"}})
            if result['failures']:
                raise http.ReadError(result['failures'][0]['status'])
            if config.get('candidate_policy'):
                if cancel.is_set():raise http.ReadError('cancelled')
                if candidates:emit({'type':'candidates','records':[{**r,'video_title':r['video_title'][:300]} for r in candidates]})
                diagnostic({'operation':'candidate_selection','policy':config['candidate_policy']['version'],'scope':'current_author_response',
                            'candidate_count':len(candidates),'selected':[r['video_id'] for r in targets],
                            **candidate_pool.selection_evidence(candidates,targets,config['candidate_policy'])})
        if not targets and config['kind'] == 'search':
            cursor, search_id, seen_cursors, excluded = 0, '', set(), set()
            while len(targets) < config['video_limit']:
                page = client.page('search', keyword=config['target'], cursor=cursor, search_id=search_id,
                                   count=10 if config.get('candidate_policy') else min(10, config['video_limit']))
                with lock:
                    counts['skipped'] += page['skipped']
                    counts['unsupported'] += page['skipped']
                known = {r['video_id'] for r in targets}
                for row in page['rows']:
                    if not video_discovery.in_search_scope(row,config['target']):
                        excluded.add(row['video_id'])
                        continue
                    if row['video_id'] not in known and len(targets) < (50 if config.get('candidate_policy') else config['video_limit']):
                        targets.append(row)
                        known.add(row['video_id'])
                if not page['has_more'] or len(targets) >= config['video_limit']:
                    break
                if page['cursor'] == cursor or page['cursor'] in seen_cursors:
                    raise http.ReadError('schema_changed')
                seen_cursors.add(cursor)
                cursor, search_id = page['cursor'], page['search_id']
            if excluded:
                diagnostic({'operation':'search_scope','policy':'game-title-scope-v1','excluded_candidates':len(excluded),
                            'eligible_candidates':len(targets),'reason':'no_game_evidence_in_title','all_douyin':False})
            if targets and config.get('candidate_policy'):
                candidates=targets
                targets=candidate_pool.select(candidates,config['video_limit'],config['candidate_policy'])
                if cancel.is_set():raise http.ReadError('cancelled')
                emit({'type':'candidates','records':[{**r,'video_title':r['video_title'][:300]} for r in candidates]})
                diagnostic({'operation':'candidate_selection','policy':config['candidate_policy']['version'],'scope':'current_search_response',
                            'candidate_count':len(candidates),'selected':[r['video_id'] for r in targets],
                            **candidate_pool.selection_evidence(candidates,targets,config['candidate_policy'])})
        if not targets:
            restricted_empty=config['kind']=='author' and bool(result.get('restricted_seeds')) and result['status']=='completed'
            healthy_empty=config['kind']=='author' and bool(config.get('discovery_job')) and result['status']=='completed'
            emit({'type': 'status', 'status': 'completed' if healthy_empty or restricted_empty else 'no_data', 'detail': '作者入口检查完成，受限作品已停止跟踪；本次未读取评论，其他公开作品继续' if restricted_empty else '作者作品检查完成，本次没有文案匹配的作品，等待下次检查' if healthy_empty else '本次有限发现未找到可读取的相关视频；未扩大范围或切换入口'})
            return
        emit({'type': 'targets', 'records': targets})
        lane=(config.get('discovery_job') or {}).get('read_lane','combined')
        if lane not in ('front','history','combined'):raise http.ReadError('schema_changed')

        def read_video(target):
            if cancel.is_set() or stopped.is_set():
                return
            vid, title = target['video_id'], target['video_title']
            progress(1)
            active = True
            active_started=time.monotonic();active_seconds=0.0;read_outcome='partial'
            seen = set()
            skipped = unsupported = 0
            read_comment_page = False
            head_pending = True
            def remaining():
                return max(0, config['comment_limit'] - len(seen) - skipped)
            try:
                metadata_failed = False
                if config.get('resolve_video_titles') and (not title or title == vid):
                    title = config.get('known_video_titles', {}).get(vid) or title
                    target = {**target, 'video_title': title or vid}
                if config.get('refresh_video_metrics'):
                    metrics=target.get('metrics') or config.get('known_video_metrics',{}).get(vid)
                    # Known works can inspect their front comments immediately.
                    # Optional statistics still refresh in history/combined reads;
                    # unknown titles still require detail before scope checking.
                    defer_metrics = lane == 'front' and bool(title and title != vid)
                    if video_metadata.stale(metrics) and not defer_metrics:
                        try:
                            target={**target,**client.page('detail',video=vid,count=1)['rows'][0]}
                            title=target['video_title']
                        except http.ReadError as exc:
                            if exc.status not in ('empty_response','schema_changed','no_data','network_error','timeout'):
                                raise
                            metadata_failed = True
                            target={**target,'metrics':metrics}
                            diagnostic({'operation':'work_metadata','video_id':vid,'status':exc.status,
                                        'detail':'作品参数未更新；保留原快照，继续本批评论读取'})
                    else:
                        target={**target,'metrics':metrics}
                if config.get('resolve_video_titles') and (not title or title == vid):
                    title = config.get('known_video_titles', {}).get(vid)
                    if not title and not metadata_failed:
                        metadata = client.page('detail', video=vid, count=1)
                        title = metadata['rows'][0]['video_title']
                    target = {**target, 'video_title': title or vid}
                emit({'type': 'video', 'record': target})
                from game_scope import exclusion_reason
                if exclusion_reason(title):
                    emit({'type':'checkpoint','video_id':vid,'status':'unavailable','reason':'outside_pc_scope',
                          'detail':exclusion_reason(title)+' 未请求其评论'})
                    return
                emit({'type': 'checkpoint', 'video_id': vid, 'status': 'reading', 'detail': 'HTTP 评论／回复读取中'})
                def deliver(page):
                    nonlocal skipped, unsupported
                    omitted = min(page['skipped'], remaining())
                    nontext = min(omitted, page.get('skipped_reasons', {}).get('non_text', 0))
                    skipped += omitted
                    unsupported += omitted-nontext
                    for row in page['rows']:
                        if cancel.is_set() or stopped.is_set():
                            raise http.ReadError('cancelled')
                        if row['comment_id'] not in seen and remaining():
                            emit({'type': 'comment', 'record': row})
                            seen.add(row['comment_id'])
                revision,state,reset=0,None,'disabled'
                tag=comment_paging.session_tag(session) if session else None
                durable=bool(tag and config.get('paging_session_tag')==tag and type(config.get('paging_source_id')) is int)
                if durable:
                    with app.db() as connection:
                        revision,state,reset=comment_paging.load(connection,config['paging_source_id'],vid,tag,app.now())
                # A history job may only use its own still-valid cursor. A new
                # session starts with a real front read instead of foreign offsets.
                history_only=lane=='history' and durable and reset=='continued'
                rotation=comment_paging.Rotation(state,head=not history_only)
                while remaining() and (request:=rotation.next()) is not None:
                    operation,parent,cursor=request
                    if operation=='replies':
                        page = client.page('replies', video=vid, parent=parent, title=title,
                            cursor=cursor, count=min(10,remaining()))
                    else:
                        try:
                            page = client.page('comments', video=vid, title=title, cursor=cursor,
                                count=min(10,remaining()))
                        except http.ReadError as exc:
                            if cursor==0 and exc.status=='schema_changed' and exc.evidence.get('work_visibility_probe') is True:
                                # Only explicit, same-video work restrictions may
                                # retire a work. A valid detail leaves the original
                                # schema failure intact; account gates still stop.
                                client.page('detail',video=vid,count=1)
                            raise
                    read_comment_page = True
                    comment_response_seen.set()
                    complete=len(page['rows'])+page['skipped']<=remaining() and not page.get('skipped_reasons',{}).get('invalid_record',0)
                    deliver(page)
                    try:receipt=rotation.accept(request,page,complete)
                    except ValueError:raise http.ReadError('schema_changed',{'reason':'comment_cursor_not_advanced'}) from None
                    if durable and not cancel.is_set() and not stopped.is_set():
                        revision+=1
                        emit(dict(type='comment_paging',video_id=vid,session_tag=tag,revision=revision,
                                  state=rotation.snapshot(),page=receipt))
                    if lane=='front':break
                    if head_pending and operation=='comments' and cursor==0:
                        head_pending=False
                        # Release this slot before any history/reply read so
                        # the next work can inspect its own front page first.
                        active_seconds+=time.monotonic()-active_started
                        progress(-1);active=False
                        yield
                        if cancel.is_set() or stopped.is_set():
                            raise http.ReadError('cancelled')
                        progress(1);active=True;active_started=time.monotonic()
                    if not remaining() or not complete:break
                emit({'type': 'checkpoint', 'video_id': vid, 'status': 'partial' if unsupported else 'done',
                      'detail': '达到本批观察预算或已读取响应可见末页；无文字内容计入跳过，不代表全量评论' if not unsupported else '部分记录结构不支持，保留已读取数据'})
                read_outcome='partial' if unsupported else 'done'
            except http.ReadError as exc:
                restriction = video_discovery.work_restriction(exc, vid)
                if restriction:
                    emit({'type': 'checkpoint', 'video_id': vid, 'status': 'unavailable',
                          'reason': restriction, 'detail': video_discovery.WORK_RESTRICTION_DETAILS[restriction]})
                    return
                if exc.status == 'resource_limited' and exc.evidence.get('reason') == 'request_budget':
                    budget_reached.set()
                    # A batch budget is not a full-history completion promise.
                    # Only works with a valid comment response finish this batch;
                    # untouched works keep a partial checkpoint and remain due.
                    emit({'type':'checkpoint','video_id':vid,
                          'status':'done' if read_comment_page and not unsupported else 'partial',
                          'detail':'达到本批请求预算，保留已读评论；后续继续轮询，未读完全部评论' if read_comment_page else '本批请求预算已用完，该作品尚未读取评论'})
                    return
                with lock:
                    if not failure:
                        failure.append(exc)
                stopped.set()
                emit({'type': 'checkpoint', 'video_id': vid, 'status': 'partial', 'detail': str(exc)})
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                diagnostic({'operation':'processing', 'video_id':vid, 'status':'schema_changed',
                    'error_type':type(exc).__name__, 'frames':[
                        {'file':Path(f.filename).name, 'function':f.name, 'line':f.lineno}
                        for f in frames[-4:]]})
                with lock:
                    if not failure:
                        failure.append(http.ReadError('schema_changed'))
                stopped.set()
                emit({'type': 'checkpoint', 'video_id': vid, 'status': 'partial', 'detail': 'HTTP 响应处理异常；已停止整批并保留已有数据'})
            finally:
                if active:active_seconds+=time.monotonic()-active_started
                emit({'type':'diagnostic','stage':'work_timing','snapshot':{'processing':dict(
                    version='work-timing-v1',video_id=vid,lane=lane,duration_ms=max(0,round(active_seconds*1000)),outcome=read_outcome)}})
                with lock:
                    counts['skipped'] += skipped
                    counts['unsupported'] += unsupported
                if skipped:
                    emit({'type': 'skipped', 'count': skipped})
                if active:progress(-1)
        readers=[read_video(row) for row in targets]
        def queue_phase(phase):
            emit({'type':'diagnostic','stage':'work_read_queue','snapshot':{'processing':dict(
                version='front-page-priority-v1' if lane=='combined' else 'comment-lanes-v1',phase=phase,works=len(targets),page_concurrency=limit,
                newest_order_verified=False,**({'lane':lane} if lane!='combined' else {}))}})
        def drain(reader):
            for _ in reader:pass
        try:
            with ThreadPoolExecutor(max_workers=limit) as pool:
                queue_phase('history_and_replies' if lane=='history' else 'front_pages')
                futures=[pool.submit(drain if lane=='history' else lambda r:next(r,None),reader) for reader in readers]
                for future in as_completed(futures):future.result()
                if lane=='combined' and not cancel.is_set() and not stopped.is_set():
                    queue_phase('history_and_replies')
                    futures=[pool.submit(drain,reader) for reader in readers]
                    for future in as_completed(futures):future.result()
        finally:
            # Executor exit joins every in-flight reader before closing any
            # suspended generator, including on cancellation or protocol gates.
            for reader in readers:reader.close()
        if cancel.is_set():
            raise http.ReadError('cancelled')
        if failure:
            raise failure[0]
        if budget_reached.is_set():
            if not comment_response_seen.is_set():
                raise http.ReadError('resource_limited', {'reason':'request_budget'})
            emit({'type':'status','status':'partial' if counts['unsupported'] else 'completed',
                  'detail':'已达到本批请求预算，已读结果保留；未读作品和后续评论继续按计划轮询，不代表全量读完'})
            return
        emit({'type': 'status', 'status': 'partial' if counts['unsupported'] else 'completed',
              'detail': 'HTTP 批次已结束；已保存来源和本批观察结果' + ('，部分结构不支持' if counts['unsupported'] else f"；跳过 {counts['skipped']} 条无文字内容" if counts['skipped'] else '')})
    except http.ReadError as exc:
        if exc.status == 'needs_verification':
            captcha_runtime.http_unavailable(emit)
        detail = ('账号身份核对请求未完成；保留已有会话，本批未读取评论。'
                  if exc.evidence.get('reason') == 'identity_transport_incomplete' else str(exc))
        emit({'type': 'status', 'status': exc.status, 'detail': detail})
    except Exception as exc:
        emit({'type': 'status', 'status': 'failed', 'detail': 'HTTP 采集器异常，已保留数据；错误类型：' + type(exc).__name__})
    finally:
        if owned_client and client is not None and callable(getattr(client,'close',None)):client.close()


MAX_CONFIG_BYTES = 1024*1024


def read_configuration(stream):
    # The supported candidate snapshot can contain 10,000 IDs and 500 history
    # rows. Read a complete bounded frame, not a truncated 30 KB JSON prefix.
    raw = stream.readline(MAX_CONFIG_BYTES+1)
    if len(raw)>MAX_CONFIG_BYTES or not raw.endswith(b'\n'):
        raise ValueError('Invalid configuration frame')
    config = json.loads(raw.decode('utf-8'))
    if not isinstance(config,dict):
        raise ValueError('Invalid configuration object')
    return config


def main():
    output_lock = threading.Lock()
    def emit(message):
        with output_lock:
            print(json.dumps(message, ensure_ascii=False), flush=True)
    try:
        config = read_configuration(sys.stdin.buffer)
    except (ValueError, UnicodeError):
        emit({'type':'status','status':'failed',
              'detail':'采集配置无效、未完整传入或超过 1 MiB 上限；未开始读取平台数据。'})
        return
    cancel = threading.Event()
    def commands():
        for raw in sys.stdin:
            try:
                if json.loads(raw).get('command') == 'cancel':
                    cancel.set()
            except ValueError:
                cancel.set()
    threading.Thread(target=commands, daemon=True).start()
    collect(config, emit, cancel)


if __name__ == '__main__':
    main()
