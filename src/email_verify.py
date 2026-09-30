"""邮箱验证码服务（PH2-3）：仅存哈希 / TTL / 限尝试 / 限重发 + Brevo HTTPS 发送。

设计要点：
  1. **只存哈希**：`EmailVerification.code_hash` 存 `salt$sha256(salt:code)`，
     数据库泄露也无法还原 6 位码（纯 sha256 对 6 位码可彩虹表爆破，故加盐）。
  2. **限时有效**：TTL 默认 10 分钟，过期视为无效且不区分「过期」与「错误」。
  3. **限尝试次数**：同一验证码最多校验 5 次，超限即作废，防在线爆破。
  4. **限重发频率**：同邮箱同用途 60 秒内不可重发（`retry_after` 给出剩余秒数）。
  5. **可测试**：发送器可通过 `set_sender()` 注入测试桩，L4 不依赖线上额度。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import UTC, datetime, timedelta

import requests

from src.models import EmailVerification, db

CODE_LENGTH = 6
CODE_TTL_MINUTES = 10
MAX_VERIFY_ATTEMPTS = 5
RESEND_INTERVAL_SECONDS = 60

PURPOSE_SIGNUP = 'signup'

BREVO_ENDPOINT = 'https://api.brevo.com/v3/smtp/email'
BREVO_TIMEOUT_SECONDS = 10

_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')

# 发送器可注入（测试桩）：签名 (email, code, purpose) -> (ok: bool, detail: str)
_SENDER = {'fn': None}


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _cfg(key, default=''):
    """优先读 Flask 配置，脱离应用上下文时回退环境变量。"""
    from flask import current_app, has_app_context
    if has_app_context():
        return current_app.config.get(key, default)
    return os.environ.get(key, default)


def normalize_email(email):
    return (email or '').strip().lower()


def valid_email(email):
    text = normalize_email(email)
    return bool(text) and len(text) <= 120 and bool(_EMAIL_RE.match(text))


def brevo_configured():
    """Brevo 是否已配置（缺 API Key 或发件人都算未配置）。"""
    return bool(str(_cfg('BREVO_API_KEY') or '').strip()
                and str(_cfg('BREVO_SENDER_EMAIL') or '').strip())


def signup_enabled():
    """自助注册开关：显式 SIGNUP_ENABLED 优先，否则以 Brevo 配置为准。"""
    raw = _cfg('SIGNUP_ENABLED')
    if raw is None or str(raw).strip() == '':
        return brevo_configured()
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def set_sender(fn):
    """注入测试桩发送器（None 恢复真实 Brevo 发送）。"""
    _SENDER['fn'] = fn


def _hash_code(code, salt):
    return hashlib.sha256(f'{salt}:{code}'.encode()).hexdigest()


def _store_hash(code):
    salt = secrets.token_hex(16)
    return f'{salt}${_hash_code(code, salt)}'


def _check_hash(code, stored):
    try:
        salt, digest = str(stored).split('$', 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_code(str(code), salt), digest)


def send_code_email(email, code, purpose=PURPOSE_SIGNUP):
    """通过 Brevo HTTPS API 发送验证码。返回 (ok, detail)。"""
    api_key = str(_cfg('BREVO_API_KEY') or '').strip()
    sender = str(_cfg('BREVO_SENDER_EMAIL') or '').strip()
    if not (api_key and sender):
        return False, 'not_configured'
    payload = {
        'sender': {'email': sender, 'name': str(_cfg('BREVO_SENDER_NAME') or '智电轻擎')},
        'to': [{'email': email}],
        'subject': '【智电轻擎】邮箱验证码',
        'htmlContent': (f'<p>您的验证码是 <strong>{code}</strong>，'
                        f'{CODE_TTL_MINUTES} 分钟内有效。</p>'
                        f'<p>若非本人操作请忽略本邮件。</p>'),
    }
    try:
        resp = requests.post(
            BREVO_ENDPOINT, json=payload, timeout=BREVO_TIMEOUT_SECONDS,
            headers={'accept': 'application/json',
                     'content-type': 'application/json',
                     'api-key': api_key},
        )
    except requests.RequestException:
        return False, 'network_error'
    if resp.status_code in (200, 201, 202):
        return True, 'sent'
    return False, f'http_{resp.status_code}'


def _deliver(email, code, purpose):
    fn = _SENDER['fn'] or send_code_email
    return fn(email, code, purpose)


def request_code(email, purpose=PURPOSE_SIGNUP, now=None):
    """生成并发送验证码。

    Returns:
        dict: {'ok': bool, 'reason': str, 'retry_after': int|None,
               'record_id': int|None, 'expires_at': datetime|None}
        reason 取值：invalid_email / not_configured / too_soon / send_failed
    """
    email = normalize_email(email)
    if not valid_email(email):
        return {'ok': False, 'reason': 'invalid_email'}
    if not brevo_configured() and _SENDER['fn'] is None:
        return {'ok': False, 'reason': 'not_configured'}
    now = now or _now()

    latest = (EmailVerification.query
              .filter_by(email=email, purpose=purpose)
              .order_by(EmailVerification.id.desc()).first())
    if latest and latest.last_sent_at:
        elapsed = (now - latest.last_sent_at).total_seconds()
        if elapsed < RESEND_INTERVAL_SECONDS:
            return {'ok': False, 'reason': 'too_soon',
                    'retry_after': max(1, int(RESEND_INTERVAL_SECONDS - elapsed))}

    code = f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'
    row = EmailVerification(
        email=email, purpose=purpose, code_hash=_store_hash(code),
        expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        attempts=0, sent_count=((latest.sent_count if latest else 0) + 1),
        last_sent_at=now,
    )
    db.session.add(row)
    db.session.flush()
    ok, detail = _deliver(email, code, purpose)
    if not ok:
        # 发送失败不留下「已发送」状态，用户可立即重试
        db.session.delete(row)
        db.session.commit()
        return {'ok': False, 'reason': 'send_failed', 'detail': detail}
    db.session.commit()
    return {'ok': True, 'reason': 'sent', 'record_id': row.id,
            'expires_at': row.expires_at, 'retry_after': RESEND_INTERVAL_SECONDS}


def verify_code(email, code, purpose=PURPOSE_SIGNUP, now=None):
    """校验验证码。成功后标记消费（不可重放）。

    Returns:
        dict: {'ok': bool, 'reason': str}
        reason 取值：invalid_input / no_pending / expired / too_many_attempts /
        mismatch / consumed
    """
    email = normalize_email(email)
    code = str(code or '').strip()
    if not valid_email(email) or not code.isdigit() or len(code) != CODE_LENGTH:
        return {'ok': False, 'reason': 'invalid_input'}
    now = now or _now()

    row = (EmailVerification.query
           .filter_by(email=email, purpose=purpose, consumed_at=None)
           .order_by(EmailVerification.id.desc()).first())
    if row is None:
        return {'ok': False, 'reason': 'no_pending'}
    if row.expires_at and now > row.expires_at:
        return {'ok': False, 'reason': 'expired'}
    if row.attempts >= MAX_VERIFY_ATTEMPTS:
        return {'ok': False, 'reason': 'too_many_attempts'}

    if not _check_hash(code, row.code_hash):
        row.attempts = (row.attempts or 0) + 1
        db.session.commit()
        return {'ok': False, 'reason': 'mismatch',
                'attempts_left': max(0, MAX_VERIFY_ATTEMPTS - row.attempts)}

    row.consumed_at = now
    row.attempts = (row.attempts or 0) + 1
    db.session.commit()
    return {'ok': True, 'reason': 'consumed', 'record_id': row.id}


__all__ = [
    'CODE_LENGTH',
    'CODE_TTL_MINUTES',
    'MAX_VERIFY_ATTEMPTS',
    'PURPOSE_SIGNUP',
    'RESEND_INTERVAL_SECONDS',
    'brevo_configured',
    'normalize_email',
    'request_code',
    'send_code_email',
    'set_sender',
    'signup_enabled',
    'valid_email',
    'verify_code',
]
