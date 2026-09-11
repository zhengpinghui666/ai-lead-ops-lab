"""Explicit HTTP refresh of public counters for 1–10 already registered works."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clubops as app
import collector_http as http
import collector_http_session as sessions
import uid_bootstrap
import video_metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ids',type=int,nargs='+')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output)
    if not 1<=len(args.ids)<=10 or len(set(args.ids))!=len(args.ids) or output.exists() or not output.parent.is_dir():
        parser.error('Use 1–10 distinct work IDs and a new output file')
    with app.db() as c:
        works=[dict(c.execute('SELECT * FROM videos WHERE id=?',(pk,)).fetchone() or {}) for pk in args.ids]
        if any(not row or not http.numeric(row['external_id']) for row in works):parser.error('Every ID must be an existing Douyin work')
    diagnostics=[]
    report=dict(started_at=app.now(),works=[],browser_used=False,comments_collected=0)
    try:
        session=sessions.load()
        client=http.Client(session,request_limit=len(works),diagnostic=diagnostics.append)
        client.check_gate('detail')
        identity=uid_bootstrap.probe({'expected_account':session['account'],
            'cookie':sessions.cookie_header(session,'identity'),'user_agent':session['user_agent']})
        report['identity_status']=identity['status']
        if identity['status']!='identity_verified' or identity.get('sender_uid')!=session.get('sender_uid'):
            sessions.record_identity_status(session,'identity_failed')
            raise http.ReadError('identity_failed')
        sessions.record_identity_status(session,'identity_verified')
        for work in works:
            row=client.page('detail',video=work['external_id'],count=1)['rows'][0]
            with app.db() as c:
                video_metadata.save(c,work['id'],row['metrics'])
                c.execute('UPDATE videos SET title=? WHERE id=? AND title=external_id',(app.clean(row['video_title']),work['id']))
            report['works'].append(dict(id=work['id'],video_id=row['video_id'],title=row['video_title'],metrics=row['metrics']))
        report['status']='completed'
    except http.ReadError as exc:report['status']=exc.status
    except (OSError,ValueError):report['status']='session_unavailable'
    report.update(finished_at=app.now(),diagnostics=diagnostics)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(status=report['status'],updated=len(report['works']),output=str(output))))


if __name__=='__main__':main()
