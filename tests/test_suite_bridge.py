"""MaiLifeSuite 合并层回归：进程内日程桥接与统一 Hook 分发。

覆盖 P1 新增的两个关键链路（不访问外部服务）：
1. MaiLifeLocalScheduleSource 不经过 RPC，直接调用宿主 Mixin 的
   maibot.schedule v1 API 方法，并正确映射为麦时迹内部快照；
2. 统一 Hook 入口按"麦生活先行、麦时迹随后"的顺序分发，且任一方
   拦截时不执行后续方。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo
import asyncio
import sys

from maibot_sdk.context import PluginContext, PluginPaths

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.maitrace.services.schedule.mai_life_local import MaiLifeLocalScheduleSource
from MaiLifeSuite.plugin import MaiLifeSuitePlugin

PLUGIN_ID = "maibot-community.mai-life-suite"


class BridgeRPC:
    """记录全部 RPC；桥接模式不允许出现任何 cap.call（日程查询必须走进程内）。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, method: str, plugin_id: str, payload: Dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append({"method": method, "plugin_id": plugin_id, **payload})
        if payload.get("capability") == "config.get":
            values = {"bot.nickname": "测试麦", "bot.qq_account": "999", "bot.alias_names": []}
            args = payload.get("args", {})
            return {"success": True, "value": values.get(args.get("key"), args.get("default"))}
        if payload.get("capability") == "chat.get_all_streams":
            return {"success": True, "streams": []}
        return {"success": True}


class LocalPlugin(MaiLifeSuitePlugin):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory

    @property
    def plugin_dir(self) -> Path:
        return self.directory


class BridgeCase(IsolatedAsyncioTestCase):
    """进程内日程桥：宿主 Mixin 的 api_schedule_* 直接供麦时迹消费。"""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="suite-bridge-")
        self.directory = Path(self.temp.name)
        self.plugin = LocalPlugin(self.directory)
        self.plugin.set_plugin_config(self.plugin.build_default_config())
        self.plugin.config.schedule.provider = "mai_life_local"
        self.plugin.config.schedule.auto_schedule_enabled = False
        self.plugin.config.schedule.auto_infer_next_day_prompt = False
        self.plugin.config.environment.calendar_enabled = False
        self.plugin.config.life.enabled = False
        self.plugin.config.routine.post_enabled = False
        self.plugin.config.routine.browse_enabled = False
        self.rpc = BridgeRPC()
        self.plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=self.rpc, paths=PluginPaths(
            data_dir=self.directory / "sdk-data", runtime_dir=self.directory / "sdk-runtime",
        )))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_framework(self, now: datetime) -> None:
        """按真实当前时刻写入框架节点（测试时间无关）：清空当日框架后写入自习段。

        on_load 的维护循环会即时生成真实框架；先删除当日节点，保证快照唯一命中本节点。
        """
        store = self.plugin._store
        day = now.date().isoformat()
        minute = now.hour * 60 + now.minute
        start = max(0, minute - 30)
        end = min(1440, minute + 30)
        if end <= start:
            end = min(1440, start + 1)
        store.conn.execute("DELETE FROM daily_framework WHERE day=?", (day,))
        store.conn.execute(
            "INSERT OR REPLACE INTO daily_framework(day,id,start_minute,end_minute,kind,summary,location,energy_load,shareability) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (day, "b2", start, end, "study", "图书馆自习", "图书馆", 0.5, 0.5),
        )
        store.conn.commit()

    async def test_bridge_honest_error_before_ready(self) -> None:
        """麦生活服务未初始化时，协议信封诚实报告而不是伪造空日程。"""
        envelope = await self.plugin.api_schedule_snapshot()
        self.assertFalse(envelope["success"])
        self.assertIn("初始化", envelope["error"])

    async def test_bridge_reads_host_framework_without_rpc(self) -> None:
        """运行时装配后，麦时迹引擎经进程内桥读取麦生活框架，且零 api.call RPC。"""
        from MaiLifeSuite.maitrace.services.schedule.mai_life_local import MaiLifeLocalScheduleSource

        await self.plugin.on_load()
        try:
            self.assertIsInstance(self.plugin._schedule_engine, MaiLifeLocalScheduleSource)
            now = self.plugin._env.now()
            self._seed_framework(now)
            await self.plugin._schedule_engine.refresh(force=True)
            payload = self.plugin._schedule_engine.current_activity_payload()
            self.assertTrue(payload.get("has_activity"), payload)
            # 框架节点 id 由存储层按内容重算（内容哈希），与 store.get_framework 保持一致即可。
            stored_ids = {str(node.get("id")) for node in await self.plugin._store.get_framework(now.date().isoformat())}
            self.assertIn(payload["activity"]["goal_id"], stored_ids)
            self.assertEqual(payload["activity"]["name"], "图书馆自习")
            self.assertEqual(payload["timezone"], self.plugin.config.schedule.timezone)
            # 桥接全程不得产生 api.call RPC（全部能力调用由 FakeRPC 记录供断言）。
            self.assertEqual(
                [call for call in self.rpc.calls if call.get("capability") == "api.call"],
                [],
            )
        finally:
            await self.plugin.on_unload()

    async def test_provider_info_matches_protocol(self) -> None:
        envelope = await self.plugin.api_schedule_provider_info()
        self.assertTrue(envelope["success"])
        data = envelope["data"]
        self.assertEqual(data["protocol"], "maibot.schedule")
        self.assertEqual(data["protocol_version"], 1)
        self.assertIn("snapshot", data["capabilities"])
        self.assertIn("timeline", data["capabilities"])


class HookDispatchCase(IsolatedAsyncioTestCase):
    """统一 Hook 分发顺序与拦截语义（实例属性遮蔽方法，直接断言调用序列）。"""

    def setUp(self) -> None:
        self.plugin = MaiLifeSuitePlugin()
        self.plugin.set_plugin_config(self.plugin.build_default_config())
        self.plugin.config.plugin.enabled = False   # 让麦生活 Mixin 钩子直接短路
        self.plugin.config.messaging.enabled = True

    def _trace(self, receive: Any = None, before: Any = None, after: Any = None) -> SimpleNamespace:
        return SimpleNamespace(
            on_receive=receive or AsyncMock(return_value={"action": "continue"}),
            on_send_before=before or AsyncMock(return_value={"action": "continue"}),
            on_send_after=after or AsyncMock(),
        )

    async def test_receive_passes_trace_result_when_mailife_continues(self) -> None:
        self.plugin.on_receive = AsyncMock(return_value={"action": "continue"})
        trace = self._trace(receive=AsyncMock(return_value={"action": "block"}))
        self.plugin._messaging = trace
        result = await self.plugin.handle_receive(session_id="s", message={})
        self.assertEqual(result["action"], "block")
        self.plugin.on_receive.assert_awaited_once()
        trace.on_receive.assert_awaited_once()

    async def test_receive_stops_at_mailife_block(self) -> None:
        self.plugin.on_receive = AsyncMock(return_value={"action": "block"})
        trace = self._trace(receive=AsyncMock(return_value={"action": "continue"}))
        self.plugin._messaging = trace
        result = await self.plugin.handle_receive(session_id="s", message={})
        self.assertEqual(result["action"], "block")
        trace.on_receive.assert_not_awaited()

    async def test_receive_skips_trace_when_messaging_disabled(self) -> None:
        self.plugin.config.messaging.enabled = False
        self.plugin.on_receive = AsyncMock(return_value={"action": "continue"})
        trace = self._trace(receive=AsyncMock(return_value={"action": "block"}))
        self.plugin._messaging = trace
        result = await self.plugin.handle_receive(session_id="s", message={})
        self.assertEqual(result["action"], "continue")
        trace.on_receive.assert_not_awaited()

    async def test_send_before_respects_abort(self) -> None:
        self.plugin.on_send_before = AsyncMock(return_value={"action": "abort"})
        trace = self._trace(before=AsyncMock(return_value={"action": "continue"}))
        self.plugin._messaging = trace
        result = await self.plugin.handle_before_send(message={})
        self.assertEqual(result["action"], "abort")
        trace.on_send_before.assert_not_awaited()

    async def test_after_send_calls_both_settlements(self) -> None:
        self.plugin.on_send_after = AsyncMock()
        trace = self._trace(after=AsyncMock())
        self.plugin._messaging = trace
        result = await self.plugin.handle_after_send(message={}, sent=True)
        self.assertEqual(result["action"], "continue")
        self.plugin.on_send_after.assert_awaited_once()
        trace.on_send_after.assert_awaited_once()

    async def test_planner_dispatch_keeps_order(self) -> None:
        self.plugin.on_planner = AsyncMock(return_value={"action": "continue"})
        tracker = SimpleNamespace(track_planner_task=Mock(), track_reply_anchor=Mock())
        self.plugin._messaging = tracker
        injection = SimpleNamespace(planner=Mock(return_value={"action": "continue"}))
        self.plugin._injection = injection
        items = [{"item_type": "UserMessageItem", "parts": []}]
        result = await self.plugin.handle_planner(session_id="s", items=items)
        self.assertEqual(result["action"], "continue")
        self.plugin.on_planner.assert_awaited_once()
        tracker.track_planner_task.assert_called_once_with("s", items)
        injection.planner.assert_called_once()

    async def test_hook_components_registered_exactly_once(self) -> None:
        comps = self.plugin.get_components()
        hooks = [c for c in comps if c["type"] == "HOOK_HANDLER"]
        points = [c["metadata"]["hook"] for c in hooks]
        self.assertEqual(len(points), len(set(points)), "同一 Hook 点不允许重复注册")
        self.assertEqual(
            sorted(points),
            sorted([
                "chat.receive.before_process",
                "maisaka.planner.before_request",
                "maisaka.planner.after_response",
                "maisaka.replyer.before_request",
                "maisaka.replyer.after_response",
                "send_service.before_send",
                "send_service.after_send",
            ]),
        )
