"""Explicit, bounded backend candidate discovery from existing video seeds."""
import argparse
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector_http as http
import collector_http_session as sessions
import uid_bootstrap
import video_discovery


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('seeds',nargs='+',help='1–3 original video IDs')
    parser.add_argument('--output',required=True,help='New private evidence JSON file; never overwritten')
    parser.add_argument('--authors-only',action='store_true',help='Read author works only; do not request related feed')
    parser.add_argument('--author-pages',type=int,choices=range(1,4),default=1,help='Maximum pages per author (1–3)')
    parser.add_argument('--page-size',type=int,choices=range(1,21),default=10,help='Maximum works per page (1–20)')
    args=parser.parse_args()
    if not 1<=len(args.seeds)<=3 or any(not http.numeric(v) for v in args.seeds):
        parser.error('Provide 1–3 numeric video IDs')
    output=Path(args.output)
    if output.exists() or not output.parent.is_dir():
        parser.error('Output must be a new file in an existing directory')
    diagnostics=[]
    result={'started_at':datetime.now(timezone.utc).isoformat(), 'browser_used':False}
    try:
        session=sessions.load()
        budget=len(set(args.seeds))*(1+args.author_pages+int(not args.authors_only))
        client=http.Client(session,request_limit=budget,diagnostic=diagnostics.append)
        client.check_gate('detail')
        identity=uid_bootstrap.probe({'expected_account':session['account'],
            'cookie':sessions.cookie_header(session,'identity'),'user_agent':session['user_agent']})
        result['identity']={k:identity[k] for k in ('status','http_status','business_code',
            'response_bytes','response_sha256','user_present') if k in identity}
        if identity['status']!='identity_verified' or identity.get('sender_uid')!=session.get('sender_uid'):
            sessions.record_identity_status(session,'identity_failed')
            raise http.ReadError('identity_failed')
        sessions.record_identity_status(session,'identity_verified')
        result.update(video_discovery.discover(client,args.seeds,author_pages=args.author_pages,
            page_size=args.page_size,include_related=not args.authors_only))
        result['budget']={'author_pages':args.author_pages,'page_size':args.page_size,'requests':budget}
    except http.ReadError as exc:
        result.update(status=exc.status)
    except (OSError, ValueError):
        result.update(status='session_unavailable')
    result.update(diagnostics=diagnostics,finished_at=datetime.now(timezone.utc).isoformat())
    with output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'status':result['status'],'candidate_count':len(result.get('candidates',[])),
        'failures':result.get('failures',[]),'diagnostics':diagnostics},ensure_ascii=False))


if __name__=='__main__':main()
