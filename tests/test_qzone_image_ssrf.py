"""qzone_api.get_image_base64_by_url 的 SSRF 防护回归。

全部离线：不发起真实网络请求。用假 getaddrinfo / 假 socket 模拟
DNS 解析与 HTTP 响应，验证外部可控图片地址无法触达内网目标。
"""

from pathlib import Path
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import patch
import base64
import io
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.maitrace.services.qzone_api import QzoneAPI


PUBLIC_IP = "93.184.216.34"


class _FakeSocket:
    """最小 socket 替身：丢弃请求字节，按脚本返回预置的原始 HTTP 响应。"""

    def __init__(self, raw_response: bytes) -> None:
        self._file = io.BytesIO(raw_response)
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str, *args, **kwargs):
        assert mode == "rb", mode
        return self._file

    def close(self) -> None:
        pass


def _redirect_302(location: str) -> bytes:
    return (f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n").encode("ascii")


PNG_BYTES = b"\x89PNG"
_OK_200 = (
    f"HTTP/1.1 200 OK\r\nContent-Length: {len(PNG_BYTES)}\r\n\r\n".encode("ascii") + PNG_BYTES
)


def _public_dns(*_args, **_kwargs):
    """把所有域名解析到同一个公网 IP，保证校验逻辑而非 DNS 决定结果。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]


class QzoneImageSsrfTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.api = QzoneAPI(cookies={}, uin="12345")

    async def _fetch(self, url: str):
        return await self.api.get_image_base64_by_url(url)

    async def test_rejects_non_http_schemes_and_embedded_credentials(self) -> None:
        for url in ("file:///C:/Windows/win.ini",
                    "ftp://example.com/a.png",
                    "http://user:pass@example.com/a.png"):
            with self.subTest(url=url):
                self.assertIsNone(await self._fetch(url))

    async def test_rejects_private_loopback_and_metadata_addresses(self) -> None:
        for url in ("http://127.0.0.1:8000/api/secret",
                    "http://localhost/a.png",
                    "http://[::1]/a.png",
                    "http://10.0.0.5/a.png",
                    "http://172.16.0.9/a.png",
                    "http://192.168.1.10/a.png",
                    "http://169.254.169.254/latest/meta-data/"):
            with self.subTest(url=url):
                self.assertIsNone(await self._fetch(url))

    async def test_rejects_hostname_resolving_to_private_ip(self) -> None:
        # DNS 把公网域名解析到内网地址（rebinding 的第一步）也必须被拒绝。
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.8.7", 0))]
        with patch.object(socket, "getaddrinfo", return_value=private_dns):
            self.assertIsNone(await self._fetch("https://internal.example/a.png"))

    async def test_redirect_to_private_address_is_blocked_before_connect(self) -> None:
        connections = []

        def fake_create_connection(address, timeout=None):
            connections.append(address)
            return _FakeSocket(_redirect_302("http://169.254.169.254/latest/meta-data/"))

        with patch.object(socket, "getaddrinfo", _public_dns), \
                patch.object(socket, "create_connection", side_effect=fake_create_connection):
            self.assertIsNone(await self._fetch("http://redirector.example/a.png"))
        # 第二跳（链路本地元数据地址）在建立连接前就被拒绝：只发生过第一跳。
        self.assertEqual(len(connections), 1, connections)
        self.assertEqual(connections[0][0], PUBLIC_IP)

    async def test_redirect_chain_length_is_capped(self) -> None:
        connections = []

        def fake_create_connection(address, timeout=None):
            connections.append(address)
            return _FakeSocket(_redirect_302("http://redirector.example/loop"))

        with patch.object(socket, "getaddrinfo", _public_dns), \
                patch.object(socket, "create_connection", side_effect=fake_create_connection):
            self.assertIsNone(await self._fetch("http://redirector.example/a.png"))
        # 无限重定向在跳数上限被切断：初始请求 + _MAX_REDIRECTS 次重定向 = 11 跳，
        # 第 12 次响应到来时直接报"重定向次数过多"，不会无限消耗连接。
        self.assertEqual(len(connections), 11, connections)

    async def test_public_image_download_still_works(self) -> None:
        with patch.object(socket, "getaddrinfo", _public_dns), \
                patch.object(socket, "create_connection", return_value=_FakeSocket(_OK_200)), \
                patch("ssl.SSLContext.wrap_socket", lambda self, sock, **kwargs: sock):
            result = await self._fetch("https://multimedia.example/a.png")
        self.assertEqual(base64.b64decode(result), PNG_BYTES)


if __name__ == "__main__":
    main()
