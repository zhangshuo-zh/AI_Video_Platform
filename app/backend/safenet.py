"""出站地址的最小信任边界：哪些主机平台**不该**替人去连。

为什么需要这一层：平台里有三处地址是"人填的"或"供应商给的"，服务端会照着它发
请求——`engine_instance.endpoint`（管理员填的引擎地址）、`DEEPSEEK_BASE_URL` /
`ALIYUN_WAN_BASE_URL`（部署时填的供应商地址）、以及云端视频接口应答里的
`video_url`（**外部输入**，见 `cloud_run._download`）。三处的共同形状是：
`urlopen(字符串)` 直接发出去，谁写这个字符串谁就能让服务器替他去连任何地址。

这条边界要防的是什么、不要防什么，必须先说清楚，否则很容易写成"把局域网也封了"：

  * **要防的是云元数据端点与不可信重定向**。`169.254.169.254` 这类链路本地地址
    在云主机上是"不用凭据就能读到实例身份/临时凭据"的接口，一个被诱导的管理员
    或一条被劫持的供应商应答就足以把 worker 变成内网探测器；而 `urllib` 默认
    跟随 3xx，一次 302 就能绕过任何"只看第一跳"的校验。
  * **不要防本机与局域网**。这个平台的实际拓扑就是"模型跑在同一台机器或同一
    网段的另一台机器上"（`http://127.0.0.1:7860`、`http://192.168.x.x:11434`
    在代码注释与使用说明里都是正常配置）。把 loopback 与 private 一律封掉，
    等于让产品的主要用法不可用——那不是加固，是把功能改坏。

所以这里的口径是**按"这类地址不可能是一台模型/供应商服务"来拒**，而不是按
"公网还是内网"来拒：

  * 拒绝：链路本地（169.254.0.0/16、fe80::/10，含 AWS/GCP/Azure 的元数据 IP）、
    未指定地址（0.0.0.0）、广播、以及不是 http/https 的 scheme。
  * 允许：loopback、局域网私网、公网域名/IP。
  * 另外提供 `is_metadata_host()`：云元数据常见的主机名（`metadata.google.internal`、
    `instance-data` 等）一律拒，名称检查放在解析之前，这样即使 DNS 也指不到。

`urlopen` 的重定向单独处理：`NoRedirect` 让调用方拿到 3xx 自己决定，或者
调用方在跟随之前用 `check_url` 复核每一跳。这不改变正常请求的行为
（供应商接口不靠重定向工作），但关掉了"304/302 把请求带去别处"的口子。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# 云元数据接口的常见主机名。名称先拦一次，免得依赖解析结果。
_METADATA_HOSTS = frozenset({
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
    "metadata.azure.com", "169.254.169.254",
})

# 允许的 scheme。只有这两者会真的发到网络上；`file:`／`ftp:`／`gopher:` 一律不放行。
ALLOWED_SCHEMES = ("http", "https")


class UnsafeTarget(ValueError):
    """这个地址平台不替人去连。路由层转 422，worker 侧记成一条明确的失败原因。"""


def _ip_of(host: str) -> ipaddress._BaseAddress | None:
    """把主机名解析成 IP；解析不了返回 None（交给调用的那一步自己报错）。

    这里**不用 `socket.getaddrinfo` 做安全判断**：DNS 可以在解析与连接之间变
    （DNS rebinding），解析结果只能作为"明显是元数据地址"的佐证，不能当作
    "连的一定是这个 IP"的保证。真正的约束留给 `check_url` 的 scheme/host 检查
    与重定向限制——对本项目的威胁模型（被诱导的管理员、被劫持的供应商应答）
    那两条已经覆盖，不需要为此引入"连接时锁 IP"的复杂度。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.lower() in ("localhost",):
        return ipaddress.ip_address("127.0.0.1")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    for info in infos:
        addr = info[4][0]
        try:
            return ipaddress.ip_address(addr)
        except ValueError:
            continue
    return None


def is_metadata_host(host: str) -> bool:
    """这个主机名/地址是不是云元数据接口。名称与地址两侧都判。"""
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return False
    if name in _METADATA_HOSTS:
        return True
    ip = _ip_of(name)
    if ip is None:
        return False
    # IPv4 链路本地 169.254.0.0/16 是 AWS/GCP/Azure/阿里云的元数据段；
    # IPv6 的 fe80::/10 同理。这两段不可能是"一台跑模型的机器"。
    return ip.is_link_local


def check_url(url: str, *, allow_loopback: bool = True,
              allow_private: bool = True) -> str:
    """校验一个**即将发出去**的 URL；不合法抛 `UnsafeTarget`，合法原样返回。

    `allow_loopback`／`allow_private` 默认都是 True：本平台的引擎与供应商服务
    通常就在本机或同一网段。它们存在的意义是让调用方能收紧（例如 `cloud_run`
    的下载链接来自外部应答，可以要求它不得指向本机）。
    """
    text = (url or "").strip()
    if not text:
        raise UnsafeTarget("地址为空")
    parsed = urlparse(text)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeTarget(
            f"只允许 http/https 地址，现在是「{parsed.scheme or '没有 scheme'}」")
    host = parsed.hostname
    if not host:
        raise UnsafeTarget(f"地址里读不出主机名：{text}")
    if is_metadata_host(host):
        raise UnsafeTarget(
            f"{host} 是云元数据接口，平台不会替任何配置或供应商应答去连它")
    ip = _ip_of(host)
    if ip is not None:
        if ip.is_link_local:
            raise UnsafeTarget(f"{host} 是链路本地地址（云元数据段），不予放行")
        if ip.is_unspecified:
            raise UnsafeTarget(f"{host} 是未指定地址，不指向任何服务")
        if ip.is_multicast:
            raise UnsafeTarget(f"{host} 是组播地址，不是可请求的服务")
        if ip.is_loopback and not allow_loopback:
            raise UnsafeTarget(f"{host} 是回环地址，这个场景不允许指向本机")
        if ip.is_private and not ip.is_loopback and not allow_private:
            raise UnsafeTarget(f"{host} 是内网地址，这个场景只允许公网目标")
    return text


class NoRedirect:
    """给 `urllib.request.build_opener` 用的"不跟随重定向"处理器。

    为什么需要：`urlopen` 默认跟着 3xx 走，一次 302 就能把请求带去
    `http://169.254.169.254/...`——只看第一跳的校验形同虚设。
    这里让 3xx 直接抛 `HTTPError`，由调用方当作"这个地址没有直接给我们内容"
    处理（供应商接口不靠重定向工作，行为上不损失什么）。
    """

    def __init__(self):
        from urllib import request as _r
        self._cls = _r.HTTPRedirectHandler

        outer = self

        class _Handler(outer._cls):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise UnsafeTarget(
                    f"上游要求重定向到 {newurl}；为避免绕过地址校验，平台不跟随重定向")

        self.handler = _Handler()


def opener_without_redirects():
    """返回一个不跟随重定向的 opener（`urllib` 的 opener 可复用）。"""
    from urllib import request as _r
    return _r.build_opener(NoRedirect().handler)
