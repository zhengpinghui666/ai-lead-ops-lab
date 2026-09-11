"""Minimal, independently implemented IM wire codec; no captured auth templates.

Field layout is a research reference, not a current platform compatibility claim.
Unknown fields are skipped, malformed or ambiguous responses are never successes.
"""
import json


def varint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('Invalid unsigned protobuf integer')
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value):
    if type(number) is not int or not 1 <= number < 2**29:
        raise ValueError('Invalid protobuf field number')
    if type(value) is int:
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    if not isinstance(value, bytes):
        raise ValueError('Invalid protobuf field value')
    return varint(number << 3 | 2) + varint(len(value)) + value


def decode(raw):
    if not isinstance(raw, bytes) or len(raw) > 262144:
        raise ValueError('Invalid protobuf size')
    pos, result = 0, {}

    def read_int():
        nonlocal pos
        value = 0
        for i in range(10):
            if pos >= len(raw):
                raise ValueError('Truncated protobuf')
            b = raw[pos]
            pos += 1
            if i == 9 and b > 1:
                raise ValueError('Protobuf integer overflow')
            value |= (b & 127) << (7 * i)
            if not b & 128:
                return value
        raise ValueError('Invalid varint')

    while pos < len(raw):
        tag = read_int()
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 2**29:
            raise ValueError('Invalid field tag')
        if wire == 0:
            value = read_int()
        elif wire in (1, 2, 5):
            size = read_int() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(raw) - pos:
                raise ValueError('Truncated field')
            value = raw[pos:pos + size]
            pos += size
        else:
            raise ValueError('Unsupported wire type')
        result.setdefault(number, []).append((wire, value))
    return result


def one(fields, number, wire, default=None):
    values = fields.get(number, [])
    if not values:
        return default
    if len(values) != 1 or values[0][0] != wire:
        raise ValueError('Ambiguous field')
    return values[0][1]


def text(fields, number, default=''):
    return one(fields, number, 2, default.encode()).decode('utf-8')


def numeric_uid(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith('0') or len(value) > 19 or int(value) >= 2**63:
        raise ValueError('请提供数字 UID 字符串，不接受抖音号、sec_uid 或 OpenID')
    return value


def create_body(sender, receiver):
    # Current Douyin web encoder writes repeated int64 participants unpacked.
    users = field(2, int(numeric_uid(receiver))) + field(2, int(numeric_uid(sender)))
    return field(609, field(1, 1) + users)


def conversation_matches(conversation_id, sender, receiver):
    parts = conversation_id.split(':')
    return len(parts) == 4 and parts[:2] == ['0', '1'] and sorted(parts[2:]) == sorted([sender, receiver]) and sender != receiver


def send_body(conversation, client_id, message, ticket):
    if not isinstance(ticket, str) or not ticket:
        raise ValueError('缺少当前会话的有效 ticket')
    content = json.dumps(dict(mention_users=[], aweType=700, richTextInfos=[], text=message), ensure_ascii=False, separators=(',', ':'))
    body = (field(1, conversation['conversation_id']) + field(2, 1)
            + field(3, int(conversation['conversation_short_id'])) + field(4, content)
            + field(6, 7) + field(7, ticket) + field(8, client_id))
    body += field(5, field(1, 's:client_message_id') + field(2, client_id))
    return field(100, body)


def result(raw, command, sender, receiver, client_id=None, *, sequence=None):
    """Return accepted only for correlated, explicit server evidence."""
    unknown = dict(status='unknown', detail='协议响应不能确认结果；不会自动重发')
    try:
        root = decode(raw)
        if sequence is not None and one(root, 2, 0) != sequence:
            return unknown
        if one(root, 1, 0) != command or one(root, 13, 0) != int(sender):
            return unknown
        code = one(root, 3, 0, 0)
        if code != 0:
            return dict(status='failed', detail='平台拒绝请求', platform_code=str(code))
        if text(root, 4) != 'OK' or one(root, 5, 0, 0) != 0:
            return unknown
        body = decode(one(root, 6, 2, b''))
        if command == 609:
            created = decode(one(body, 609, 2, b''))
            if one(created, 2, 0, 0) != 0 or one(created, 5, 0, 0) != 0:
                return dict(status='failed', detail='平台未接受会话创建')
            info = decode(one(created, 1, 2, b''))
            cid, short = text(info, 1), one(info, 2, 0, 0)
            if not conversation_matches(cid, sender, receiver) or not 0 < short < 2**63 or one(info, 3, 0, 1) != 1 or one(info, 9, 0, 0) != 0:
                return unknown
            # ConversationInfoV2 field 4 is the server-issued ticket. It stays
            # in memory; messaging evidence never persists this private field.
            ticket = text(info, 4)
            result = dict(status='conversation_created', conversation_id=cid, conversation_short_id=str(short))
            if ticket and len(ticket) <= 4096 and not any(ord(c) < 32 or ord(c) == 127 for c in ticket):
                result['_ticket'] = ticket
            return result
        info = decode(one(body, 100, 2, b''))
        if text(info, 4) != client_id:
            return unknown
        # Current web schema: field 2 is extra_info; field 6 is a plain
        # check_message, not JSON. Never persist that server-supplied text.
        text(info, 6)
        if one(info, 3, 0, 0) != 0 or one(info, 5, 0, 0) != 0:
            return dict(status='failed', detail='平台未接受消息；不会重发')
        extra = json.loads(text(info, 2) or '{}')
        if not isinstance(extra, dict):
            return unknown
        if 'status_code' in extra and str(extra['status_code']) != '0':
            return dict(status='failed', detail='平台返回消息业务错误；不会重发')
        message_id = one(info, 1, 0, 0)
        if not 0 < message_id < 2**63:
            return unknown
        return dict(status='accepted', detail='服务端接受；尚无送达或已读证据', server_message_id=str(message_id))
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return unknown
