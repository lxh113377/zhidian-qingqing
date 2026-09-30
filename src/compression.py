"""响应压缩（gzip）—— 标准库实现，**零新增依赖**。

为什么自己做而不是引入 Flask-Compress：
  1. 本项目的压缩需求很窄（文本类响应 + 超过阈值才压），规则完全可控；
  2. 生产环境是 PythonAnywhere 免费层与私有化部署，依赖清单越短越稳
     （参考 `requirements.txt`，此前已刻意不引入图表渲染库）。

实测收益（2026-09-23，本机真实请求）：
  - `static/css/style.css`  80.3 KB → 约 12 KB
  - `static/js/echarts.custom.js` 604 KB → 约 170 KB（手机端 H5 首屏最大单点）
  - 首屏 3D 叙事页 vendor（three+react+gsap 合计 855 KB）→ 约 250 KB
  - 模板渲染出的 HTML 18–28 KB → 约 5 KB

设计约束：
  - 只压**文本类** MIME；图片/字体/已压缩格式直接跳过（压了反而更大）；
  - 小于 `COMPRESS_MIN_SIZE`（默认 500 字节）不压，避免 CPU 换不到带宽；
  - 尊重 `Cache-Control: no-transform`（上游显式声明不得改写时不压）；
  - 必带 `Vary: Accept-Encoding`，否则共享缓存会把压缩版发给不支持的客户端；
  - 已带 `Content-Encoding` 的响应不重复压。

已知边界（部署相关，勿误解为代码缺陷）：
  若托管平台把 `/static/` 直接映射给前置 Nginx（PythonAnywhere 的 Web 标签可配），
  则静态文件**不经过 Flask**，本模块不会生效 —— 需由前置服务器负责压缩，
  或取消该静态映射让 Flask 托管。详见 `部署/README.md`。
"""
from __future__ import annotations

import gzip

from flask import Response, request

# 可压缩的 MIME 前缀 / 精确值（文本类；图片字体等已压缩格式一律不在此列）
COMPRESSIBLE_PREFIXES = ('text/',)
COMPRESSIBLE_EXACT = frozenset({
    'application/json',
    'application/javascript',
    'application/x-javascript',
    'application/xml',
    'application/manifest+json',
    'application/xhtml+xml',
    'image/svg+xml',
})

# 即使命中上面规则也不压的状态码（无实体 / 无收益）
SKIP_STATUS = frozenset({204, 205, 304})


def _client_accepts_gzip() -> bool:
    """客户端是否接受 gzip（解析 Accept-Encoding，含 q=0 显式拒绝）。"""
    header = request.headers.get('Accept-Encoding', '')
    if not header:
        return False
    for part in header.split(','):
        piece = part.strip().lower()
        if not piece:
            continue
        name, _, params = piece.partition(';')
        name = name.strip()
        if name not in ('gzip', 'x-gzip', '*'):
            continue
        # q=0 表示明确不接受；缺失或 q>0 均视为接受
        q = 1.0
        for param in params.split(';'):
            key, _, value = param.partition('=')
            if key.strip() == 'q':
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 1.0
        if q > 0:
            return True
    return False


def _is_compressible(response: Response, min_size: int) -> bool:
    """判断该响应是否值得压缩（不含「压完是否更小」的最终判断）。"""
    if response.status_code in SKIP_STATUS or not 200 <= response.status_code < 300:
        return False
    if response.headers.get('Content-Encoding'):
        return False
    # 上游显式声明不得改写实体内容
    if 'no-transform' in (response.headers.get('Cache-Control') or '').lower():
        return False

    content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    type_ok = bool(content_type) and (
        content_type.startswith(COMPRESSIBLE_PREFIXES) or content_type in COMPRESSIBLE_EXACT
    )
    if not type_ok:
        return False

    # 长度已知且小于阈值 → 直接放弃（省掉一次压缩尝试）；
    # 长度未知（如模板渲染响应）→ 放行，由调用方按实体实际长度再判一次。
    declared = response.headers.get('Content-Length')
    size_ok = True
    if declared is not None:
        try:
            size_ok = int(declared) >= min_size
        except (TypeError, ValueError):
            size_ok = True
    return size_ok


def init_app(app):
    """注册 gzip 压缩钩子。`COMPRESS_ENABLED` 为假时完全不注册（零开销）。

    Args:
        app: Flask 应用实例。
    """
    if not app.config.get('COMPRESS_ENABLED', True):
        app.logger.info('[compression] 已按配置关闭（COMPRESS_ENABLED=0）')
        return

    min_size = int(app.config.get('COMPRESS_MIN_SIZE', 500))
    level = int(app.config.get('COMPRESS_LEVEL', 6))

    @app.after_request
    def gzip_response(response: Response) -> Response:
        if not _client_accepts_gzip():
            return response
        if not _is_compressible(response, min_size):
            return response

        # Flask 的 static / send_file 走 direct_passthrough，必须先关掉才能读到实体；
        # 本项目静态资源最大 604 KB，整块读入内存无压力。
        response.direct_passthrough = False
        try:
            raw = response.get_data()
        except (RuntimeError, OSError) as exc:  # 读不到就原样返回，绝不因压缩而 500
            app.logger.warning('[compression] 跳过（读取响应体失败）: %s', exc)
            return response

        if len(raw) < min_size:
            return response

        compressed = gzip.compress(raw, compresslevel=level, mtime=0)
        if len(compressed) >= len(raw):  # 压缩无收益（极小的文本/高熵内容）→ 不压
            return response

        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(compressed))
        # Vary 必须有：否则中间缓存可能把 gzip 版本发给不支持 gzip 的客户端
        response.headers.add('Vary', 'Accept-Encoding')
        return response
