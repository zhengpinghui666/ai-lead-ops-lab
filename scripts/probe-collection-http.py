"""Explicit, one-page read diagnostic. Does not ingest or send messages."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector_http as http
import collector_http_session as sessions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['comments', 'replies', 'search'])
    parser.add_argument('--video', default='')
    parser.add_argument('--parent', default='')
    parser.add_argument('--keyword', default='')
    parser.add_argument('--cursor', type=int, default=0)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('Evidence output already exists; refusing an accidental repeat')
    evidence = {'transport': 'http', 'browser_started': False, 'message_requests': 0, 'requests': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the evidence path before any request; interrupted attempts cannot be
    # silently repeated under the same evidence name.
    with output.open('x', encoding='utf-8') as stream:
        json.dump({**evidence, 'status': 'reserved'}, stream)
    try:
        session = sessions.load()
        client = http.Client(session, diagnostic=evidence['requests'].append, request_limit=1)
        page = client.page(args.operation, video=args.video, parent=args.parent, keyword=args.keyword, cursor=args.cursor, count=5)
        evidence.update(status='valid_page', page=page)
    except http.ReadError as exc:
        evidence.update(status=exc.status, detail=str(exc))
    except Exception as exc:
        evidence.update(status='failed', error_type=type(exc).__name__)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in evidence.items() if k != 'page'}, ensure_ascii=False))
    if 'page' in evidence:
        print(json.dumps({'rows': len(evidence['page']['rows']), 'has_more': evidence['page']['has_more'],
            'reply_targets': len(evidence['page']['reply_targets']), 'skipped': evidence['page']['skipped']}))
    return 0 if evidence['status'] == 'valid_page' else 2


if __name__ == '__main__':
    raise SystemExit(main())
