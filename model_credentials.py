"""Endpoint-bound API keys protected by the current Windows user's DPAPI."""
import json
import os
import uuid
from urllib.parse import urlsplit

import clubops as app
from uid_session import crypt

MAGIC = b'CLUBOPS-MODEL-KEY-1\n'


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError('API 地址无效')
    parsed = urlsplit(value.strip().rstrip('/'))
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.port not in (None, 443) or
            parsed.path not in ('', '/v1') or not parsed.hostname.isascii() or
            any(c.isspace() for c in value)):
        raise ValueError('API 地址须为 HTTPS 主机地址或 /v1 地址，不接受凭证、查询参数或重定向')
    return 'https://' + parsed.hostname.lower() + '/v1'


def key_path():
    return app.DATA_DIR / 'private' / 'model-api' / 'api-key.dpapi'


def save(base_url, key):
    base_url = normalize_url(base_url)
    if not isinstance(key, str) or not 8 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() or ord(c) < 32 for c in key):
        raise ValueError('API 密钥格式无效')
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('密钥路径无效')
    encrypted = MAGIC + crypt(json.dumps(dict(base_url=base_url, key=key)).encode())
    temporary = path.with_name('.key-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load(base_url):
    path = key_path()
    if path.is_symlink():
        raise ValueError('密钥路径无效')
    with path.open('rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384 or not raw.startswith(MAGIC):
        raise ValueError('密钥文件无效')
    data = json.loads(crypt(raw[len(MAGIC):], decrypt=True))
    if data['base_url'] != normalize_url(base_url):
        raise ValueError('当前密钥未绑定到这个 API 地址，请重新填写密钥')
    return data['key']


def ready(base_url):
    try:
        return bool(load(base_url))
    except (OSError, ValueError, KeyError, TypeError):
        return False
