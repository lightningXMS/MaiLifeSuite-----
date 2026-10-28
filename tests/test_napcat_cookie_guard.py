"""napcat 直连取 cookie 的本机回环限制回归。

Bearer Token 走明文 HTTP 请求头，目标必须限定为本机回环地址：
允许 127.0.0.0/8、::1、localhost（RFC 6761）；公网/内网/畸形配置一律拒绝，
且拒绝发生在创建任何 HTTP 客户端之前；Token 不出现在任何日志里。
全部离线，不发起真实网络请求。
"""

from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.maitrace.services import cookie as cookie_mod
from MaiLifeSuite.maitrace.services.cookie import fetch_cookies_by_napcat, normalize_loopback_host


class LoopbackHostTests(TestCase):
    """normalize_loopback_host 的精确边界：不用字符串前缀判断。"""

    def test_accepts_explicit_loopback_forms(self) -> None:
        cases = [
            ("127.0.0.1", "127.0.0.1"),
            (" 127.0.0.1 ", "127.0.0.1"),  # 手滑空格归一化
            ("127.8.8.8", "127.8.8.8"),  # 整个 127/8 都是回环
            ("::1", "[::1]"),  # IPv6 字面量补方括号供 URL 拼接
            ("[::1]", "[::1]"),
            ("LOCALHOST", "localhost"),  # 大小写归一化
            ("localhost.", "localhost"),  # FQDN 结尾点
            ("localhost.localdomain", "localhost.localdomain"),
            ("mystation.localhost", "mystation.localhost"),
        ]
        for host, expected in cases:
            with self.subTest(host=host):
                self.assertEqual(normalize_loopback_host(host), expected)

    def test_rejects_public_private_and_malformed_hosts(self) -> None:
        for host in [
            "8.8.8.8",  # 公网
            "192.168.1.10", "10.0.0.2", "172.16.0.9",  # 内网非回环
            "169.254.169.254",  # 云元数据
            "100.64.0.1",  # CGNAT 段
            "0.0.0.0",  # 未指定地址
            "fe80::1", "::",  # 链路本地 / 未指定 IPv6
            "example.com", "napcat.example.com",  # 域名一律不做解析
            "localhost.evil.com",  # 伪装 localhost 的域名
            "127.0.0.1:9999", "127.0.0.1/evil",  # 带端口/路径的拼接串
            "127.1",  # 缩写形式，ipaddress 严格拒绝
            "999.999.999.999",
            "", "   ", "[]",  # 空配置
        ]:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    normalize_loopback_host(host)


class NapcatFetchGuardTests(IsolatedAsyncioTestCase):
    """fetch_cookies_by_napcat：本机目标照常工作，非本机与畸形配置零请求。"""

    async def test_allowed_local_hosts_reach_loopback_target(self) -> None:
        seen = []
        response = MagicMock()
        response.json = MagicMock(
            return_value={"status": "ok", "retcode": 0, "data": {"cookies": "p_skey=abc;uin=o123"}}
        )

        class FakeClient:
            def __init__(self, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                seen.append((url, kwargs))
                return response

        cases = [
            ("127.0.0.1", "9999", "http://127.0.0.1:9999/get_cookies"),
            ("::1", "9999", "http://[::1]:9999/get_cookies"),
            ("localhost", "9999", "http://localhost:9999/get_cookies"),
            ("127.0.0.1", " 8080 ", "http://127.0.0.1:8080/get_cookies"),  # 端口空格归一化
        ]
        with patch.object(cookie_mod.httpx, "AsyncClient", FakeClient):
            for host, port, expected_url in cases:
                with self.subTest(host=host, port=port):
                    seen.clear()
                    result = await fetch_cookies_by_napcat(host, port, "TOKEN-XYZ")
                    self.assertEqual(result, {"p_skey": "abc", "uin": "o123"})
                    url, kwargs = seen[0]
                    self.assertEqual(url, expected_url)
                    self.assertEqual(kwargs["headers"]["Authorization"], "Bearer TOKEN-XYZ")
                    self.assertEqual(kwargs["json"], {"domain": "qzone.qq.com"})

    async def test_non_local_host_is_rejected_without_any_request(self) -> None:
        factory = MagicMock()
        token = "SECRET-TOKEN-XYZ"
        for host in ("8.8.8.8", "192.168.1.10", "169.254.169.254", "example.com", ""):
            with self.subTest(host=host), \
                    patch.object(cookie_mod.httpx, "AsyncClient", factory), \
                    self.assertLogs(cookie_mod.logger, level="ERROR") as captured:
                result = await fetch_cookies_by_napcat(host, "9999", token)
            self.assertIsNone(result)
            factory.assert_not_called()  # 拒绝发生在创建任何 HTTP 客户端之前
            self.assertNotIn(token, "\n".join(captured.output))
            factory.reset_mock()

    async def test_abnormal_port_is_rejected_without_any_request(self) -> None:
        factory = MagicMock()
        for port in ("abc", "", "70000", "9999/evil", "-1", "0", " 9999x"):
            with self.subTest(port=port), \
                    patch.object(cookie_mod.httpx, "AsyncClient", factory), \
                    self.assertLogs(cookie_mod.logger, level="ERROR") as captured:
                result = await fetch_cookies_by_napcat("127.0.0.1", port, "SECRET-TOKEN-XYZ")
            self.assertIsNone(result)
            factory.assert_not_called()
            self.assertNotIn("SECRET-TOKEN-XYZ", "\n".join(captured.output))
            factory.reset_mock()

    async def test_token_never_appears_in_logs_on_network_error(self) -> None:
        token = "SECRET-TOKEN-XYZ"

        class BrokenClient:
            def __init__(self, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                raise cookie_mod.httpx.ConnectError("connection refused")

        with patch.object(cookie_mod.httpx, "AsyncClient", BrokenClient), \
                self.assertLogs(cookie_mod.logger, level="ERROR") as captured:
            result = await fetch_cookies_by_napcat("127.0.0.1", "9999", token)
        self.assertIsNone(result)
        joined = "\n".join(captured.output)
        self.assertNotIn(token, joined)
        self.assertIn("127.0.0.1", joined)  # 目标 URL 不含敏感信息，可记录


if __name__ == "__main__":
    main()
