"""Bounded own-account HTTP reads for selected UIDs. No sends or read markers.

Only matched conversation metadata is returned. Missing from a scan does not
mean stranger: this is neither a contacts query nor proof of deleted history.
Message content is returned only for a selected, participant-checked thread.
"""
import argparse
import hashlib
import json

import uid_protocol as wire
import uid_session
import uid_transport

OPERATIONS = {'conversations': (2006, 2006), 'stranger_conversations': (1001, 1000),
              'messages': (301, 301), 'stranger_messages': (1002, 1001)}


class ReadError(ValueError):
    def __init__(self, reason):
        allowed = {'invalid_number', 'invalid_page_size_or_type', 'conversation_identity_mismatch',
                   'unsupported_conversation_type', 'http_response_rejected', 'response_identity_or_sequence_mismatch',
                   'platform_response_rejected', 'missing_or_wrong_response_body', 'configured_identity_mismatch',
                   'identity_check_failed', 'duplicate_conversations_in_page', 'nonadvancing_cursor',
                   'message_conversation_mismatch', 'message_author_mismatch', 'duplicate_message_id'}
        super().__init__(reason if reason in allowed else 'unrecognized_read_failure')


def _number(value, minimum=0, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReadError('invalid_number')
    return value


def _flag(fields, number):
    return bool(_number(wire.one(fields, number, 0, 0), 0, 1))


def _items(fields, number, limit):
    items = fields.get(number, [])
    if len(items) > limit or any(kind != 2 for kind, _ in items):
        raise ReadError('invalid_page_size_or_type')
    return [wire.decode(raw) for _, raw in items]


def _conversation(fields, sender, stranger=False):
    cid = wire.text(fields, 4 if stranger else 1)
    short = wire.one(fields, 1 if stranger else 2, 0)
    parts = cid.split(':')
    if len(parts) != 4 or parts[:2] != ['0', '1'] or parts[2:].count(sender) != 1:
        raise ReadError('conversation_identity_mismatch')
    peer = next(part for part in parts[2:] if part != sender)
    wire.numeric_uid(peer)
    _number(short, 1)
    if not stranger and (wire.one(fields, 3, 0) != 1 or wire.one(fields, 9, 0, 0) != 0):
        raise ReadError('unsupported_conversation_type')
    return {'conversation_id': cid, 'conversation_short_id': str(short),
            'peer_uid': peer, 'inbox': 1 if stranger else 0}


def _read(operation, body, sender, provider, exchange, evidence):
    command, field = OPERATIONS[operation]
    prepared = provider.prepare(operation, body, {'sender_uid': sender, 'command': command})
    sequence = uid_transport.validate_envelope(prepared, command, body)
    requested_inbox = wire.one(wire.decode(prepared['payload']), 6, 0, 0)
    proof = {'operation': operation, 'command': command}
    evidence.append(proof)
    status, mime, raw = exchange(operation, prepared)
    proof.update(http_status=status, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
    if status != 200 or 'protobuf' not in mime.lower():
        raise ReadError('http_response_rejected')
    root = wire.decode(raw)
    code = wire.one(root, 3, 0, 0)
    proof.update(platform_code=str(_number(code)),
                 command_matches=wire.one(root, 1, 0) == command,
                 sequence_matches=wire.one(root, 2, 0) == sequence,
                 sender_matches=wire.one(root, 13, 0) == int(sender),
                 inbox_matches=wire.one(root, 5, 0, 0) == requested_inbox,
                 platform_message_ok=wire.text(root, 4) == 'OK')
    if wire.one(root, 1, 0) != command or wire.one(root, 2, 0) != sequence or wire.one(root, 13, 0) != int(sender):
        raise ReadError('response_identity_or_sequence_mismatch')
    # Response field 5 is inbox_type, not an additional business error code.
    if code != 0 or wire.text(root, 4) != 'OK' or wire.one(root, 5, 0, 0) != requested_inbox:
        raise ReadError('platform_response_rejected')
    outer = wire.decode(wire.one(root, 6, 2, b''))
    if set(outer) != {field}:
        raise ReadError('missing_or_wrong_response_body')
    return wire.decode(wire.one(outer, field, 2))


def _identity(sender, provider, exchange):
    wire.numeric_uid(sender)
    if provider.current()['sender_uid'] != sender:
        raise ReadError('configured_identity_mismatch')
    identity = uid_transport.verify_identity({'sender_uid': sender}, provider=provider, exchange=exchange)
    if identity['status'] != 'identity_verified':
        raise ReadError('identity_check_failed')


def scan(sender, targets, *, provider=None, exchange=None, max_pages=3, page_size=20):
    """Scan bounded regular and stranger inbox pages; keep only target matches."""
    wire.numeric_uid(sender)
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError('请提供 1–20 个待核对的数字 UID')
    for uid in targets:
        wire.numeric_uid(uid)
    if sender in targets or len(set(targets)) != len(targets):
        raise ValueError('核对对象不能重复或包含当前账号')
    _number(max_pages, 1, 5)
    _number(page_size, 1, 20)
    provider = provider or uid_session.Provider()
    exchange = exchange or uid_transport.request
    result = {'status': 'read_failed', 'can_send': False, 'browser_started': False,
              'targets': {uid: {'status': 'unknown', 'conversations': []} for uid in targets},
              'scopes': [], 'evidence': [], 'contacts_checked': False, 'history_exhaustive': False}
    try:
        _identity(sender, provider, exchange)
        for operation in ('conversations', 'stranger_conversations'):
            stranger = operation == 'stranger_conversations'
            scope = {'inbox': 1 if stranger else 0, 'pages': 0, 'observed': 0, 'complete': False}
            result['scopes'].append(scope)
            cursor, seen = 0, {0}
            for _ in range(max_pages):
                body = (wire.field(1000, wire.field(1, cursor) + wire.field(2, page_size) + wire.field(3, 1)) if stranger
                        else wire.field(2006, wire.field(1, 1) + wire.field(2, cursor) + wire.field(3, 1) + wire.field(4, page_size)))
                info = _read(operation, body, sender, provider, exchange, result['evidence'])
                rows = [_conversation(row, sender, stranger) for row in _items(info, 4 if stranger else 1, page_size)]
                if len({row['conversation_id'] for row in rows}) != len(rows):
                    raise ReadError('duplicate_conversations_in_page')
                more = _flag(info, 2)
                next_cursor = _number(wire.one(info, 1 if stranger else 3, 0, 0))
                for row in rows:
                    if row['peer_uid'] in result['targets']:
                        target = result['targets'][row['peer_uid']]
                        if row not in target['conversations']:
                            target['conversations'].append(row)
                        target['status'] = 'existing_conversation'
                scope['pages'] += 1
                scope['observed'] += len(rows)
                scope['next_cursor'] = str(next_cursor)
                scope['has_more'] = more
                if not more:
                    scope['complete'] = True
                    break
                if next_cursor in seen:
                    raise ReadError('nonadvancing_cursor')
                seen.add(next_cursor)
                cursor = next_cursor
        complete = all(scope['complete'] for scope in result['scopes'])
        result['status'] = 'checked' if complete else 'partial'
        for target in result['targets'].values():
            if target['status'] == 'unknown':
                target['status'] = 'not_observed'
        # A complete list response still cannot prove never-contacted status.
    except uid_transport.TransportError as exc:
        result['error'] = 'transport_failed'
        result['transport'] = exc.evidence
    except ReadError as exc:
        result['error'] = str(exc)  # Only fixed literals raised in this module.
    except Exception:
        result['error'] = 'unrecognized_response_or_session'
    return result


def messages(sender, conversation, *, provider=None, exchange=None, limit=20, cursor=0):
    """Read one selected conversation page. Never reset unread or mark read."""
    wire.numeric_uid(sender)
    _number(limit, 1, 20)
    _number(cursor)
    if (not isinstance(conversation, dict) or set(conversation) != {'conversation_id', 'conversation_short_id', 'peer_uid', 'inbox'}
            or type(conversation['inbox']) is not int or conversation['inbox'] not in (0, 1)
            or not wire.conversation_matches(conversation['conversation_id'], sender, conversation['peer_uid'])):
        raise ValueError('需要已核对双方的会话')
    wire.numeric_uid(conversation['peer_uid'])
    short = int(wire.numeric_uid(conversation['conversation_short_id']))
    stranger = conversation['inbox'] == 1
    if stranger and cursor:
        raise ValueError('陌生人消息接口不支持该游标')
    provider, exchange = provider or uid_session.Provider(), exchange or uid_transport.request
    result = {'status': 'read_failed', 'can_send': False, 'browser_started': False, 'messages': [], 'evidence': [],
              'unread_reset_requested': False, 'read_marker_requested': False, 'history_exhaustive': False}
    try:
        _identity(sender, provider, exchange)
        operation = 'stranger_messages' if stranger else 'messages'
        body = (wire.field(1001, wire.field(1, short) + wire.field(2, 0)) if stranger else
                wire.field(301, wire.field(1, conversation['conversation_id']) + wire.field(2, 1) + wire.field(3, short)
                           + wire.field(4, 1 if cursor else 3) + wire.field(5, cursor) + wire.field(6, limit)))
        info = _read(operation, body, sender, provider, exchange, result['evidence'])
        # Stranger API has no request limit; retain a strict response bound.
        rows = _items(info, 3 if stranger else 1, 100 if stranger else limit)
        output, seen, skipped = [], set(), 0
        for row in rows:
            if (wire.text(row, 1) != conversation['conversation_id'] or wire.one(row, 2, 0) != 1
                    or wire.one(row, 5, 0) != short):
                raise ReadError('message_conversation_mismatch')
            author = str(_number(wire.one(row, 7, 0), 1))
            if author not in (sender, conversation['peer_uid']):
                raise ReadError('message_author_mismatch')
            message_id = str(_number(wire.one(row, 3, 0), 1))
            if message_id in seen:
                raise ReadError('duplicate_message_id')
            seen.add(message_id)
            if wire.one(row, 6, 0) != 7 or wire.one(row, 12, 0, 0) != 0:
                skipped += 1
                continue
            try:
                content = json.loads(wire.text(row, 8))
                text = content.get('text') if isinstance(content, dict) else None
                if not isinstance(text, str) or not text.strip() or len(text) > 5000:
                    raise ValueError()
            except (ValueError, TypeError):
                skipped += 1
                continue
            output.append({'server_message_id': message_id, 'sender_uid': author,
                           'direction': 'outbound' if author == sender else 'inbound', 'content': text,
                           'index': str(_number(wire.one(row, 4, 0, 0))),
                           'created_at_raw': str(_number(wire.one(row, 10, 0, 0)))})
        result.update(status='messages_observed', messages=output[:limit], returned_count=len(rows), skipped_count=skipped,
                      output_truncated=len(output) > limit,
                      has_more=None if stranger else _flag(info, 3),
                      next_cursor=None if stranger else str(_number(wire.one(info, 2, 0, 0))))
    except uid_transport.TransportError as exc:
        result['error'], result['transport'] = 'transport_failed', exc.evidence
    except ReadError as exc:
        result['error'] = str(exc)
    except Exception:
        result['error'] = 'unrecognized_response_or_session'
    return result


def main():
    parser = argparse.ArgumentParser(description='Read-only conversation check for selected numeric UIDs')
    parser.add_argument('--recipient', action='append', required=True)
    parser.add_argument('--pages', type=int, default=3)
    args = parser.parse_args()
    provider = uid_session.Provider()
    result = scan(provider.current()['sender_uid'], args.recipient, provider=provider, max_pages=args.pages)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('checked', 'partial') else 2


if __name__ == '__main__':
    raise SystemExit(main())
