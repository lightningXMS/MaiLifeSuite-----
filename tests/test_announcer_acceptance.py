"""活动播报名单整合 · 验收测试：插件工具调用 + 配置迁移 + 播报链路冒烟。

模拟环境（Fake Chat/Send + 隔离 SQLite），不向真实 QQ 发送任何消息。
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.config import MaiLifeSuiteSettings
from MaiLifeSuite.core.storage import LifeStore
from MaiLifeSuite.life.announcer import AnnouncerService
from MaiLifeSuite.plugin import create_plugin
from MaiLifeSuite.tests.test_announcer import (
    FakeChat, FakeEngine, FakeSend, default_chat, make_payload, NOW,
)


class AcceptanceCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mai_accept_")
        self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 插件加载与组件注册 ──

    def test_plugin_components_registered(self) -> None:
        """插件可实例化，组件声明完整且无重名（配置改动不影响注册）。"""
        plugin = create_plugin()
        plugin.set_plugin_config({})
        components = plugin.get_components()
        self.assertTrue(components, "组件清单不能为空")
        names = [str(item.get("name") or "") for item in components]
        self.assertEqual(len(names), len(set(names)), f"组件重名: {names}")

    # ── 工具调用：未初始化兜底（原功能不受配置改动影响） ──

    async def test_tools_fallback_without_initialization(self) -> None:
        """未执行 on_load 时调用公开工具：返回兜底结构，不得抛异常。"""
        plugin = create_plugin()
        plugin.set_plugin_config({})
        activity = await plugin.handle_get_current_activity()
        self.assertIn("has_activity", activity)
        self.assertEqual(activity["activity"], None)
        schedule = await plugin.handle_get_schedule()
        self.assertEqual(schedule, [])
        state = await plugin.handle_get_life_state()
        self.assertEqual(state, {})

    async def test_tool_get_current_activity_with_engine(self) -> None:
        """注入日程引擎后 get_current_activity 返回真实 payload。"""
        plugin = create_plugin()
        plugin.set_plugin_config({})
        payload = make_payload()
        plugin._schedule_engine = FakeEngine(payload)
        result = await plugin.handle_get_current_activity()
        self.assertTrue(result["has_activity"])
        self.assertEqual(result["activity"]["name"], "整理书桌")

    # ── 配置迁移（生产旧配置兼容） ──

    def test_production_legacy_config_migration(self) -> None:
        """生产配置形态（基础列表与白名单同填）迁移后语义不变。"""
        raw = {"announce": {
            "enabled": True, "group_enabled": True, "private_enabled": True,
            "group_ids": ["111111111", "444444444"], "private_ids": ["333333333", "222222222"],
            "group_whitelist": ["111111111", "444444444"], "private_whitelist": ["333333333", "222222222"],
        }}
        cfg = MaiLifeSuiteSettings(**raw).announce
        self.assertEqual(cfg.group_targets, ["111111111", "444444444"])
        self.assertEqual(cfg.private_targets, ["333333333", "222222222"])
        self.assertEqual(cfg.list_mode, "whitelist")
        # WebUI schema 含新字段且不含旧字段。
        schema = MaiLifeSuiteSettings.model_json_schema()
        announce_props = schema["$defs"]["AnnounceSettings"]["properties"]
        self.assertIn("list_mode", announce_props)
        self.assertIn("group_targets", announce_props)
        self.assertNotIn("group_ids", announce_props)
        self.assertNotIn("group_whitelist", announce_props)

    # ── 播报链路冒烟：两种模式各一轮 ──

    async def test_announce_whitelist_mode_end_to_end(self) -> None:
        """白名单模式端到端：名单内会话收到一条模板播报，且落库去重。"""
        config = MaiLifeSuiteSettings()
        config.announce.enabled = True
        config.announce.group_enabled = True
        config.announce.private_enabled = True
        config.announce.group_targets = ["701"]
        config.announce.private_targets = ["1001"]
        send = FakeSend()
        service = AnnouncerService(None, self.store, config, None, FakeEngine(make_payload()),
                                   None, logging.getLogger("test"))
        service.ctx = type("Ctx", (), {"chat": default_chat(), "send": send})()
        stats = await service.tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(len(send.accepted), 2)
        for _, text in send.accepted:
            self.assertIn("整理书桌", text)
        # 去重已落库：第二次巡查不再发送。
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 0)

    async def test_announce_blacklist_mode_end_to_end(self) -> None:
        """黑名单模式端到端：名单内被排除，名单外会话收到播报。

        名单值未命中任何已注册会话时不排除任何目标（黑名单按会话匹配）。
        """
        config = MaiLifeSuiteSettings()
        config.announce.enabled = True
        config.announce.group_enabled = True
        config.announce.private_enabled = True
        config.announce.list_mode = "blacklist"
        config.announce.group_targets = ["701"]
        config.announce.private_targets = ["333333333"]  # 不在会话池中 → 不排除任何私聊
        send = FakeSend()
        service = AnnouncerService(None, self.store, config, None, FakeEngine(make_payload()),
                                   None, logging.getLogger("test"))
        service.ctx = type("Ctx", (), {"chat": default_chat(), "send": send})()
        stats = await service.tick(NOW)
        self.assertEqual(stats["sent"], 3)  # 702（名单外的群）+ 私聊 1001/1002
        accepted = {c[0] for c in send.accepted}
        self.assertNotIn("stream-g701", accepted)
        self.assertIn("stream-g702", accepted)
        self.assertIn("skipped_in_blacklist:group:701", stats["skipped"])


if __name__ == "__main__":
    from unittest import main
    main()
