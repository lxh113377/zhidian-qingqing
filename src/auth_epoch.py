"""平台重置后的会话世代标记。

全库重建会复用账号主键，单靠 `admin-3` 这类 ID 无法让旧业务会话失效。
本模块把随机世代写到数据库旁的隐藏文件：登录时绑定，加载用户时校验；
平台管理员可豁免，以便重置请求完成当前会话平滑续用。
"""
import os
import secrets

from flask import current_app, has_app_context, session

EPOCH_KEY = '_auth_epoch'
EPOCH_FILE = '.demo_auth_epoch'
_CURRENT = {'value': None}


def _epoch_path():
    directory = None
    if has_app_context():
        try:
            database = current_app.extensions['sqlalchemy'].engine.url.database
            if database:
                directory = os.path.dirname(os.path.abspath(database))
        except Exception:
            directory = None
    if not directory:
        directory = os.path.join(os.getcwd(), 'data')
    return os.path.join(directory, EPOCH_FILE)


def current_epoch():
    """当前世代；文件不存在时返回空串（兼容既有部署与测试）。"""
    if _CURRENT['value'] is not None:
        return _CURRENT['value']
    try:
        with open(_epoch_path(), encoding='utf-8') as handle:
            _CURRENT['value'] = handle.read().strip()
    except OSError:
        _CURRENT['value'] = ''
    return _CURRENT['value']


def rotate_epoch():
    """生成新世代并原子落盘。"""
    path = _epoch_path()
    token = secrets.token_urlsafe(18)
    _CURRENT['value'] = token
    tmp = f'{path}.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(token)
        os.replace(tmp, path)
    except OSError:
        # 极端只读部署仍保留进程内世代，至少确保本次运行会话失效。
        pass
    return token


def bind_session():
    """把当前世代写入登录会话。"""
    session[EPOCH_KEY] = current_epoch()


def session_epoch_valid():
    """会话世代是否匹配当前库世代。"""
    return session.get(EPOCH_KEY, '') == current_epoch()
