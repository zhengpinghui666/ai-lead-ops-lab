"""Local solver configuration and bounded, credential-free workflow events."""
import os
from pathlib import Path
import re
import sys

BASE = Path(__file__).resolve().parent
PHASES = {'detected', 'capturing', 'recognizing', 'submitting', 'verifying', 'accepted', 'needs_review'}


def configuration():
    python = BASE / '.tools/ddddocr-eval/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    return {'mode': 'manual' if os.environ.get('CLUBOPS_CAPTCHA_MODE') == 'manual' else 'auto',
            'python': str(python), 'max_submissions': 1}


def status():
    config = configuration()
    return {'mode': config['mode'], 'installed': Path(config['python']).is_file(),
            'max_submissions_per_batch': 1, 'experimental': True,
            'browser_adapter': 'slider_and_same_shape_pair', 'http_challenge_adapter': False,
            'live_pass_rate': None}


def clean_event(value):
    if not isinstance(value, dict) or value.get('phase') not in PHASES:
        raise ValueError('验证码阶段无效')
    if value.get('transport') not in ('local_browser', 'http'):
        raise ValueError('验证码通道无效')
    if not value.get('attempt_id'):
        raise ValueError('验证码事件缺少尝试 ID')
    result = {k: value[k] for k in ('phase', 'transport')}
    for key in ('attempt_id', 'reason', 'adapter', 'image_sha256'):
        if key in value:
            if not isinstance(value[key], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value[key]):
                raise ValueError('验证码诊断字段无效')
            result[key] = value[key]
    for key in ('submissions', 'elapsed_ms'):
        if key in value:
            if type(value[key]) is not int or not 0 <= value[key] <= (1 if key == 'submissions' else 900000):
                raise ValueError('验证码计数无效')
            result[key] = value[key]
    if 'platform_verdict' in value:
        if value['platform_verdict'] not in ('passed', 'failed', 'unknown'):
            raise ValueError('验证码平台结果无效')
        result['platform_verdict'] = value['platform_verdict']
    if 'verdict_source' in value:
        if value['verdict_source'] not in ('visible_platform_result', 'platform_response_message', 'none'):
            raise ValueError('验证码平台结果来源无效')
        result['verdict_source'] = value['verdict_source']
    if result['phase'] == 'accepted' and result.get('submissions') != 1:
        raise ValueError('未提交的验证码不能记为通过')
    if result['phase'] == 'accepted' and (result.get('platform_verdict') != 'passed' or result.get('verdict_source') not in ('visible_platform_result', 'platform_response_message')):
        raise ValueError('缺少抖音明确通过判定，不能记为通过')
    return result


def http_unavailable(emit):
    # verify_check alone contains no image, challenge identity or submission contract.
    # Never clear an HTTP session gate on the strength of an unrelated browser login.
    for phase in ('detected', 'needs_review'):
        emit({'type': 'verification', 'event': {'phase': phase, 'transport': 'http',
              'attempt_id': 'http-gate', 'submissions': 0, 'reason': 'http_challenge_not_available'}})
