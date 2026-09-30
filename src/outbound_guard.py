"""出站目标守卫 —— 服务端主动向外发请求时，"发给谁"必须是判定过的（第 30 轮 H81）。

为什么必须有它（2026-09-27 当场实测）：Webhook 回调地址此前只过两道校验
（`src/api_open.py:152-155` 的 `http(s)://` 前缀与长度 ≤500），随后被
`src/webhooks.py:202` 原样交给 `requests.post(...)` ⇒ 一个已认证的商家管理员可以把
本服务端当成任意 HTTP 客户端，让服务器去请求 `http://127.0.0.1:5000/…`、
`http://169.254.169.254/latest/meta-data/`、`http://2130706433/`（= 127.0.0.1 的十进制写法）
这类地址。PythonAnywhere 是共享主机，内网面不是"我们的服务"，而是别人的服务。
全仓与 43 项门禁里对**出站目标**的判断：**0 条**。

上游口径（同轮 `gh api` 取回原文逐行核过，缓存在 `D:/tmp/bench31/`）：
- Discourse `lib/final_destination.rb:267-276` —— `SSRFDetector.lookup_and_filter_ips(hostname).first`
  之后**把请求的 hostname 换成解析出的 IP 直连**：把"校验时解析一次、真正连接时又解析一次"的
  DNS-rebinding 时差掐死（只按字符串判永远做不到这件事）；
- Saleor `saleor/webhook/models.py:12-15` —— 校验器挂在 **URLField 本身**
  （`AppURLValidator(schemes=[...])`）⇒ 任何写路径都跑不掉，而不是靠调用点自觉；
- Frappe `integrations/doctype/webhook/webhook.py:100-106` —— 保存时 `validate_request_url()` 早失败。

设计取舍（不偷偷留后门）：
1. **解析结果说了算，不是字符串形状说了算**：`2130706433`、`0x7f000001`、`127.1` 都能被
   `getaddrinfo` 解成环回，按前缀匹配"看着像不像内网"必然漏；因此先解析（可注入 resolver）
   再逐个 IP 判属性。同理**不预设主机名字符集** —— 进制写法本身是合法的解析输入，
   在形状关就按字符拦掉它们，反而会让这些形态走不到"由解析器裁决"这一步。
2. **https 不做 IP 直连**：钉 IP 会破坏证书主机名校验，那是拿一个洞换另一个洞。https 改为
   "临发前重新解析 + 任一非公网即拒"，并把 `pinned=False` 记进留痕，让读的人知道这条腿弱在哪。
3. **不开"允许私网"的环境开关**：为可测性留一个 `WEBHOOK_ALLOW_PRIVATE=1` 就等于把洞留给生产。
   测试用注入 resolver 驱动，真投递一律走本守卫。
"""

import ipaddress
from urllib.parse import urlsplit

__all__ = ['OUTBOUND_PORTS', 'OUTBOUND_SCHEMES', 'check_target', 'describe_host', 'pin_http_url']

OUTBOUND_SCHEMES = ('http', 'https')
# 只放两个标准端口：80/443 是 Webhook 的现实取值。收窄端口不是装饰 ——
# `http://127.0.0.1:5000/` 这种"打到自家应用"的形态，光靠 IP 判也成立，但端口白名单
# 让"内部服务全段"直接不可表达，少一类要记的账。
OUTBOUND_PORTS = (80, 443)
_MAX_URL_LEN = 500
_BLOCKED_HOSTNAMES = frozenset({'localhost', 'localhost.localdomain', 'ip6-localhost',
                                'ip6-loopback', 'metadata', 'metadata.google.internal'})
# 云厂商元数据服务的主机名形态（PA/AWS/GCP 各自的名字，全部按名字先挡一层）
_BLOCKED_HOST_SUFFIXES = ('.localhost', '.internal', '.local', '.localdomain', '.home.arpa')


def _ip_blocked(net):
    """IP 属性判定：非全局可路由的一律拒（含环回/私网/链路本地/组播/保留/未指定/6to4 等）。"""
    return not net.is_global or net.is_private or net.is_loopback or net.is_link_local \
        or net.is_multicast or net.is_reserved or net.is_unspecified or bool(
            getattr(net, 'is_site_local', False))


def describe_host(host):
    """把主机名归一化（小写、去尾点、剥 IPv6 方括号）。形状不合直接返回 ''。"""
    name = str(host or '').strip().rstrip('.').lower()
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1]
    return name


def _default_resolver(host):
    """系统解析：返回该主机名的**全部**地址族结果（多 IP 是常态，逐个判）。"""
    import socket

    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def _scheme_gate(parts, out):
    """协议与 userinfo 两道关（`http://u:p@host/` 的 userinfo 会绕过主机判定）。"""
    scheme = (parts.scheme or '').lower()
    out['scheme'] = scheme
    if scheme not in OUTBOUND_SCHEMES:
        return f'协议 {scheme or "（空）"} 不允许（只支持 {" / ".join(OUTBOUND_SCHEMES)}）'
    if parts.username or parts.password:
        return 'URL 不得带用户名/口令（userinfo 会绕过主机判定）'
    return ''


def _port_gate(parts, want_port, out):
    """端口白名单：显式传入的 `port` 优先于 URL 里的端口（调用侧可能按默认端口判）。"""
    try:
        chosen = want_port if want_port is not None else parts.port
    except ValueError:
        return '端口号畸形'
    chosen = chosen if chosen is not None else (443 if out['scheme'] == 'https' else 80)
    out['port'] = chosen
    if chosen not in OUTBOUND_PORTS:
        return f'端口 {chosen} 不在出站白名单 {list(OUTBOUND_PORTS)}'
    return ''


def _shape_gate(parts, want_port, out):
    """不用解析就能判的关：协议 / userinfo / 主机名 / 内网名 / 端口。返回拒因（'' = 过关）。"""
    reason = _scheme_gate(parts, out)
    if reason:
        return reason
    host = describe_host(parts.hostname)
    out['host'] = host
    if not host:
        return 'URL 没有主机名'
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return f'主机 {host} 属内网/元数据命名面，不允许作为出站目标'
    return _port_gate(parts, want_port, out)


def _literal_gate(host, out):
    """字面 IP 直接按属性判。返回 (拒因, 是否已定论)；定论时不再需要解析。"""
    try:
        net = ipaddress.ip_address(host)
    except ValueError:
        return '', False
    out['literal'] = True
    out['ip'] = str(net)
    if _ip_blocked(net):
        return f'目标地址 {net} 是内网/环回/链路本地/保留面', True
    return '', True


def _resolved_gate(host, addrs, out):
    """解析结果逐个判：任一地址非公网 ⇒ 整条拒（不做"挑一个能用的"那种妥协）。"""
    parsed = []
    for item in addrs:
        try:
            net = ipaddress.ip_address(str(item).split('%')[0])
        except ValueError:
            return f'解析结果 {item!r} 不是合法 IP 地址 ⇒ 拒绝出站'
        if _ip_blocked(net):
            return f'主机 {host} 解析到内网/非公网地址 {net} ⇒ 拒绝出站'
        parsed.append(str(net))
    out['ip'] = parsed[0]
    return ''


def _resolve_gate(host, res, out):
    """解析关：解析失败、解析为空、任一结果非公网 —— 三种"看不清"一律拒，不猜。"""
    try:
        addrs = list(res(host))
    except Exception as exc:
        return False, f'主机 {host} 解析失败（{type(exc).__name__}）⇒ 拒绝出站（宁可不发）'
    if not addrs:
        return False, f'主机 {host} 解析结果为空 ⇒ 拒绝出站'
    reason = _resolved_gate(host, addrs, out)
    return (False, reason) if reason else (True, '')


def _judge(parts, want_port, res, out):
    """三段判定的编排：形状关 → 字面 IP 关 → 解析关。返回 `(ok, reason)`。"""
    reason = _shape_gate(parts, want_port, out)
    if reason:
        return False, reason
    reason, decided = _literal_gate(out['host'], out)
    if reason:
        return False, reason
    if decided:
        return True, ''
    return _resolve_gate(out['host'], res, out)


def check_target(url, resolver=None, port=None):
    """判定一个出站目标是否允许。返回 dict(ok, reason, ip, host, scheme, port, literal)。

    拒的情形：非 http(s)、无主机名、带 userinfo、端口不在白名单、元数据/内网名、
    解析失败或解析为空、任一解析结果不是公网单播地址。
    **不猜**：任何"看不清"的输入都判拒 —— 把"没解析出来"读成"应该没问题"是 SSRF 的常规死法。
    """
    text = str(url or '').strip()
    out = {'ok': False, 'reason': '', 'ip': None, 'host': '', 'scheme': '',
           'port': port, 'literal': False}
    if not text or len(text) > _MAX_URL_LEN:
        out['reason'] = f'URL 为空或超过 {_MAX_URL_LEN} 字符'
        return out
    try:
        parts = urlsplit(text)
    except ValueError as exc:                 # 畸形 IPv6 括号等会让 urlsplit 直接抛
        out['reason'] = f'URL 无法解析：{type(exc).__name__}'
        return out
    out['ok'], out['reason'] = _judge(parts, port, resolver or _default_resolver, out)
    return out


def pin_http_url(url, ip):
    """http（无 TLS）才做 IP 直连：把主机名换成已判定的 IP，并保留原 Host 头。

    https 返回 None ⇒ 调用侧走"临发前重解析"，因为钉 IP 会让证书主机名校验失效
    （用一个洞换另一个洞不是修洞）。
    """
    parts = urlsplit(str(url or '').strip())
    if (parts.scheme or '').lower() != 'http' or not ip:
        return None
    host = describe_host(parts.hostname)
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = f'[{ip}]' if ':' in ip else ip
    if port:
        netloc = f'{netloc}:{port}'
    return parts._replace(netloc=netloc).geturl(), f'{host}{":" + str(port) if port else ""}'
