"""无第三方依赖的异步 HTTP 客户端，兼容 Windows 与 Linux。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin,urlparse

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl

from ..config import PLUGIN_VERSION


_USER_AGENT=f"Mai_life/{PLUGIN_VERSION} (+https://github.com/octmicy/Mai_life)"
_MAX_REDIRECTS=10


class HttpRequestError(RuntimeError):
    """HTTP 失败的结构化结果；异常文本不包含请求头、Key 或响应正文。"""

    def __init__(self,message:str,*,error_class:str="network",status_code:int=0,
                 headers:dict[str,str]|None=None,response_body:bytes=b"")->None:
        super().__init__(message)
        self.error_class=error_class; self.status_code=int(status_code)
        self.headers=headers or {}; self.response_body=response_body[:64_000]


@dataclass(frozen=True)
class HttpResponse:
    status:int
    url:str
    headers:dict[str,str]
    body:bytes

    def text(self)->str:
        content_type=self.headers.get("content-type",""); charset="utf-8"
        if "charset=" in content_type:
            charset=content_type.split("charset=",1)[1].split(";",1)[0].strip() or "utf-8"
        for encoding in (charset,"utf-8","gb18030"):
            try:return self.body.decode(encoding)
            except (LookupError,UnicodeDecodeError):continue
        return self.body.decode("utf-8",errors="replace")

    def json(self)->Any:
        try:return json.loads(self.text())
        except (json.JSONDecodeError,TypeError) as exc:
            raise HttpRequestError("接口未返回合法 JSON",error_class="invalid_response",
                                   status_code=self.status,headers=self.headers) from exc


def _validated_url(url:str)->str:
    value=str(url or "").strip(); parsed=urlparse(value)
    if parsed.scheme not in {"http","https"} or not parsed.netloc or not parsed.hostname:
        raise HttpRequestError("只允许完整的 HTTP(S) 地址",error_class="invalid_url")
    if parsed.username or parsed.password:
        raise HttpRequestError("地址不能包含用户凭据",error_class="invalid_url")
    return value


def _validate_public_url_sync(url:str)->tuple[str,str]:
    """解析域名并拒绝任何非公网地址，返回 (url, 选定的公网 IP) 供固定 IP 连接使用。"""
    value=_validated_url(url); host=str(urlparse(value).hostname or "").strip("[]").casefold()
    if host in {"localhost","localhost.localdomain"} or host.endswith(".localhost"):
        raise HttpRequestError("拒绝访问本机地址",error_class="unsafe_url")
    try:addresses=[ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses=list({ipaddress.ip_address(item[4][0].split("%",1)[0])
                            for item in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)})
        except (socket.gaierror,OSError) as exc:
            raise HttpRequestError("域名解析失败",error_class="dns") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise HttpRequestError("拒绝访问内网、回环或保留地址",error_class="unsafe_url")
    pinned=next((str(address) for address in addresses if address.is_global),"")
    if not pinned:
        raise HttpRequestError("拒绝访问内网、回环或保留地址",error_class="unsafe_url")
    return value,pinned


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接到已校验的公网 IP；Host 头仍使用原域名，阻断 DNS rebinding。"""
    def __init__(self,host:str,port:int,*,pinned_ip:str,timeout:float)->None:
        super().__init__(host,port,timeout=timeout)
        self._pinned_ip=pinned_ip
    def connect(self)->None:
        self.sock=socket.create_connection((self._pinned_ip,self.port),self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接到已校验的公网 IP；TLS SNI 与 Host 头仍使用原域名，阻断 DNS rebinding。"""
    def __init__(self,host:str,port:int,*,pinned_ip:str,timeout:float,context:ssl.SSLContext)->None:
        super().__init__(host,port,timeout=timeout,context=context)
        self._pinned_ip=pinned_ip
    def connect(self)->None:
        sock=socket.create_connection((self._pinned_ip,self.port),self.timeout)
        if self._tunnel_host:
            self.sock=sock; self._tunnel()
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


def _request_path(url:str)->str:
    """从 URL 提取 http.client 使用的 origin-form 请求路径。"""
    parsed=urlparse(url); path=parsed.path or "/"
    if parsed.query:path+="?"+parsed.query
    return path


class _ConnectionFactory:
    """按 public_only 标志生产连接：公网模式固定 IP 阻断 DNS rebinding，普通模式直连。"""

    def __init__(self,*,timeout:float,public_only:bool,context:ssl.SSLContext)->None:
        self.timeout=max(1,timeout); self.public_only=public_only; self.context=context

    def build(self,url:str)->tuple[http.client.HTTPConnection,str]:
        """解析并校验 URL，返回 (连接, 最终目标 URL)。"""
        if self.public_only:
            target,pinned=_validate_public_url_sync(url)
        else:
            target=_validated_url(url); pinned=""
        parsed=urlparse(target); host=str(parsed.hostname or ""); scheme=parsed.scheme
        port=parsed.port or (443 if scheme=="https" else 80)
        if scheme=="https":
            if self.public_only:
                return (_PinnedHTTPSConnection(host,port,pinned_ip=pinned,timeout=self.timeout,
                                               context=self.context),target)
            return (http.client.HTTPSConnection(host,port,timeout=self.timeout,context=self.context),target)
        if self.public_only:
            return (_PinnedHTTPConnection(host,port,pinned_ip=pinned,timeout=self.timeout),target)
        return (http.client.HTTPConnection(host,port,timeout=self.timeout),target)


class HttpClient:
    def __init__(self,logger:Any)->None:self.logger=logger

    @staticmethod
    def validate_url(url:str)->str:return _validated_url(url)

    @staticmethod
    def validate_public_url(url:str)->str:return _validate_public_url_sync(url)[0]

    @staticmethod
    def private_host_reason(url:str)->str:
        """不做 DNS 的静态主机判断：返回非空即明显非公网（字面量环回/内网/保留地址或保留后缀）。

        供配置校验等同步路径使用；能把域名解析到内网的 rebinding 仍由请求时的
        public_only 固定 IP 校验兜底（那条路在线程里解析，不阻塞事件循环）。
        """
        value=_validated_url(url); host=str(urlparse(value).hostname or "").strip("[]").casefold()
        if host in {"localhost","localhost.localdomain"} or host.endswith((".localhost",".local",".internal",".home.arpa",".onion")):
            return "主机名指向本机或保留内网域名"
        try:address=ipaddress.ip_address(host)
        except ValueError:return ""
        if address.is_loopback:return "环回地址"
        if address.is_link_local:return "链路本地地址（含云元数据 169.254.169.254）"
        if address.is_private:return "内网地址"
        if not address.is_global:return "保留地址"
        return ""

    async def get(self,url:str,*,timeout:float=8,max_bytes:int=2_000_000,
                  headers:dict[str,str]|None=None,public_only:bool=False)->HttpResponse:
        return await self.request("GET",url,timeout=timeout,max_bytes=max_bytes,
                                  headers=headers,public_only=public_only)

    async def post_json(self,url:str,payload:Any,*,timeout:float=12,max_bytes:int=2_000_000,
                        headers:dict[str,str]|None=None,public_only:bool=False)->HttpResponse:
        body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        merged={"Content-Type":"application/json",**(headers or {})}
        return await self.request("POST",url,body=body,timeout=timeout,max_bytes=max_bytes,
                                  headers=merged,public_only=public_only)

    async def request(self,method:str,url:str,*,body:bytes|None=None,timeout:float=8,
                      max_bytes:int=2_000_000,headers:dict[str,str]|None=None,
                      public_only:bool=False)->HttpResponse:
        """在线程中执行阻塞 urllib 请求，避免占用 MaiBot 的异步消息循环。"""
        target=self.validate_url(url)
        return await asyncio.to_thread(
            self._request_sync,str(method or "GET").upper(),target,body,float(timeout),
            int(max_bytes),headers or {},bool(public_only),
        )

    @staticmethod
    def _request_sync(method:str,url:str,body:bytes|None,timeout:float,max_bytes:int,
                      headers:dict[str,str],public_only:bool)->HttpResponse:
        """统一执行一次带大小上限与逐跳重定向的请求，并把网络/HTTP 失败归一化为不含 Key 的异常。"""
        request_headers={"User-Agent":_USER_AGENT,"Accept-Encoding":"identity",**headers}
        context=ssl.create_default_context()
        factory=_ConnectionFactory(timeout=timeout,public_only=public_only,context=context)
        target=_validated_url(url); visited=0
        while True:
            connection,target=factory.build(target)
            try:
                connection.request(method,_request_path(target),body=body,headers=request_headers)
                response=connection.getresponse()
                status=response.status
                if status in (301,302,303,307,308):
                    location=response.getheader("Location") or ""
                    response.read()  # 释放响应体，保证连接可安全关闭。
                    if not location:
                        raise HttpRequestError("重定向缺少 Location",error_class="network")
                    visited+=1
                    if visited>_MAX_REDIRECTS:
                        raise HttpRequestError("重定向次数过多",error_class="network")
                    target=urljoin(target,location)
                    if status in (301,302,303) and method!="HEAD":
                        # 与标准客户端一致：301/302/303 把 POST 等请求转为 GET 并丢弃请求体。
                        method="GET"; body=None
                        request_headers={key:value for key,value in request_headers.items()
                                         if key.casefold() not in {"content-type","content-length"}}
                    continue
                if status>=400:
                    response_headers={str(key).lower():str(value) for key,value in response.getheaders()}
                    try:response_body=response.read(64_001)[:64_000]
                    except Exception:response_body=b""
                    error_class=("auth" if status in {401,403} else
                                 "rate_limit" if status==429 else "server" if status>=500 else "http")
                    raise HttpRequestError(f"HTTP {status}",error_class=error_class,status_code=status,
                                           headers=response_headers,response_body=response_body)
                response_body=response.read(max(1,max_bytes)+1)
                # 多读一个字节用于可靠判断是否越过上限，避免把超大页面完整载入内存。
                if len(response_body)>max_bytes:
                    raise HttpRequestError("响应超过大小限制",error_class="too_large",
                                           status_code=int(status))
                return HttpResponse(int(status),target,
                                    {str(key).lower():str(value) for key,value in response.getheaders()},
                                    response_body)
            except HttpRequestError:raise
            except (TimeoutError,socket.timeout) as exc:
                raise HttpRequestError("请求超时",error_class="timeout") from exc
            except (ssl.SSLError,OSError,http.client.HTTPException) as exc:
                raise HttpRequestError("网络连接失败",error_class="network") from exc
            finally:
                try:connection.close()
                except Exception:pass
