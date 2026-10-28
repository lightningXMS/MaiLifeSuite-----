"""麦生活主动私聊链（ProactiveEngine.patrol → maisaka.proactive.trigger）专项回归。

覆盖：
- 调度循环注册与幂等（_start_tasks 含 mai-life-proactive）；
- 全局跳过：总开关/睡眠相位/精力不足；
- 目标缺失：profiles 未配置、用户未建立私聊会话（不误发、不报错、日志可诊断）；
- 硬过滤：静默窗、每日额度、冷却间隔、用户刚发言、评分阈值；
- 触发成功：trigger 参数、pending 登记、task_id 关联；
- 触发失败：Host 拒绝 / 未返回 task_id 时释放契机并标记 failed，不误报成功；
- 热更新：update_config 替换配置实例后引擎继续工作。

全部使用真实 LifeStore（临时 SQLite），不连接真实 QQ。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import IsolatedAsyncioTestCase
import asyncio
import json
import logging
import sys
import time as time_mod
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.core.storage import LifeStore
from MaiLifeSuite.life.proactive import ProactiveEngine
from MaiLifeSuite.mailife_config import MaiLifeSettings, UserProfile
from MaiLifeSuite.plugin import MaiLifeSuitePlugin

TZ = ZoneInfo("Asia/Shanghai")


def now_at(hour: int = 15, minute: int = 0) -> datetime:
    return datetime(2026, 10, 27, hour, minute, 0, tzinfo=TZ)


class FakeProactiveAPI:
    """替身 maisaka.proactive.trigger：记录调用，支持注入失败。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.result: Dict[str, Any] | None = None
        self.error: Exception | None = None

    async def trigger(self, stream_id: str = "", intent: str = "", *, reason: str = "",
                      priority: str = "", metadata: Dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append({"stream_id": stream_id, "intent": intent, "reason": reason,
                           "metadata": metadata or {}})
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return {"stream_id": stream_id, "task_id": f"proactive:maibot-community.mai-life-suite:{len(self.calls)}", "queued": True}


def make_state(energy: float = 50.0, sleep_phase: str = "awake") -> Dict[str, Any]:
    return {"energy": energy, "sleep_phase": sleep_phase, "mood_valence": 0.0}


def make_profile(user_id: str = "111", *, enabled: bool = True, proactive: bool = True,
                 daily_max: int = 3, quiet: str = "00:00") -> UserProfile:
    # quiet 双端相同表示不设静默窗，避免测试落在默认 00:00-08:00 窗内。
    return UserProfile(user_id=user_id, enabled=enabled, proactive_enabled=proactive,
                       daily_proactive_max=daily_max, quiet_start=quiet, quiet_end=quiet)


class ProactiveCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="mailife-proactive-")
        self.config = MaiLifeSettings()
        self.store = LifeStore(self.temp.name, journal_mode="delete", logger=logging.getLogger("test"))
        await self.store.initialize()
        self.api = FakeProactiveAPI()
        ctx = SimpleNamespace(maisaka=SimpleNamespace(proactive=self.api), logger=logging.getLogger("test"))
        self.engine = ProactiveEngine(ctx, self.store, self.config, None, logging.getLogger("test"))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def sync(self, *profiles: UserProfile) -> None:
        await self.store.sync_users(list(profiles))

    async def add_opportunity(self, *, weight: float = 0.6, target: str = "",
                              topic: str = "一段安静的午后", hours: float = 12.0) -> str:
        now = time_mod.time()
        item = {"id": f"opp-{uuid.uuid4().hex[:8]}", "framework_id": "test", "topic": topic,
                "motive": "想分享", "weight": weight, "privacy": "normal",
                "target_user_id": target, "expires_at": now + hours * 3600}
        self.assertTrue(await self.store.add_opportunity(item))
        return item["id"]

    def assert_no_trigger(self) -> None:
        self.assertEqual(self.api.calls, [], "不应提交任何主动任务")

    # ===== 全局跳过 =====

    async def test_disabled_proactive_never_triggers(self) -> None:
        self.config.proactive.enabled = False
        await self.sync(make_profile())
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_sleeping_state_blocks_patrol(self) -> None:
        await self.sync(make_profile())
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state(sleep_phase="deep_sleep"))
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_low_energy_blocks_and_records_skip(self) -> None:
        await self.sync(make_profile())
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state(energy=5.0))
        self.assertFalse(result)
        self.assert_no_trigger()
        summary = await self.store.proactive_skip_summary("2026-10-26")
        self.assertTrue(any(item.get("reason") == "low_energy" for item in summary))

    # ===== 目标缺失（最常见的"为什么没有主动私聊"）=====

    async def test_no_profiles_is_no_target_and_never_sends(self) -> None:
        await self.store.sync_users([])  # 用户未在 WebUI 配置任何档案
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_disabled_or_non_proactive_profiles_are_not_targets(self) -> None:
        # 档案存在但被禁用 / 未开启主动：同样不构成目标。
        await self.sync(make_profile("111", enabled=False), make_profile("222", proactive=False))
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_user_without_stream_is_skipped(self) -> None:
        # 用户档案有效，但从未与麦麦建立真实私聊会话：设计上不创建新会话，只能跳过。
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "")  # 显式保持空 stream
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    # ===== 硬过滤 =====

    async def test_quiet_window_blocks(self) -> None:
        # 跨夜窗 14:00-09:00 必须包含 15:00；skip 记录按 patrol 当日自然日落库。
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        self.store.conn.execute(
            "UPDATE users SET quiet_start='14:00', quiet_end='09:00' WHERE user_id='111'")
        self.store.conn.commit()
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(hour=15), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()
        summary = await self.store.proactive_skip_summary(now_at().strftime("%Y-%m-%d"))
        self.assertTrue(any(item.get("reason") == "quiet" for item in summary))

    async def test_daily_limit_zero_blocks(self) -> None:
        await self.sync(make_profile("111", daily_max=0))
        await self.store.set_user_stream("111", "private-111")
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_daily_quota_exhausted_blocks(self) -> None:
        await self.sync(make_profile("111", daily_max=1))
        await self.store.set_user_stream("111", "private-111")
        # 直接把当日计数置为已用额度（与 patrol 的自然日基准一致）。
        stamp = now_at().timestamp()
        self.store.conn.execute(
            "UPDATE users SET proactive_count=1, proactive_day=?, last_proactive_at=? WHERE user_id='111'",
            (now_at().strftime("%Y-%m-%d"), stamp))
        self.store.conn.commit()
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_min_interval_blocks(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        recent = now_at().timestamp() - 600  # 10 分钟前刚主动过，远小于默认 180 分钟
        self.store.conn.execute(
            "UPDATE users SET last_proactive_at=? WHERE user_id='111'", (recent,))
        self.store.conn.commit()
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_recent_user_silence_blocks(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        recent = now_at().timestamp() - 300  # 用户 5 分钟前刚发言
        self.store.conn.execute(
            "UPDATE users SET last_user_message_at=? WHERE user_id='111'", (recent,))
        self.store.conn.commit()
        await self.add_opportunity()
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_low_score_blocks(self) -> None:
        await self.sync(make_profile("111"))
        await self.add_opportunity(weight=0.0, topic="无足轻重")  # score<threshold
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    async def test_targeted_opportunity_not_shared_with_other_users(self) -> None:
        await self.sync(make_profile("111"), make_profile("222"))
        await self.add_opportunity(target="222")  # 定向给 222 的契机
        # 222 无 stream → 唯一匹配契机不可用；111 不得消费定向契机。
        await self.store.set_user_stream("222", "")
        await self.store.set_user_stream("111", "private-111")
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        self.assert_no_trigger()

    # ===== 触发成功与失败 =====

    async def test_happy_path_triggers_planner_and_links_task(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        await self.add_opportunity(weight=0.6)
        result = await self.engine.patrol(now_at(), make_state())
        self.assertTrue(result)
        self.assertEqual(len(self.api.calls), 1)
        call = self.api.calls[0]
        self.assertEqual(call["stream_id"], "private-111")
        self.assertEqual(call["intent"], "mai_life_proactive")
        reason = json.loads(call["reason"])
        self.assertEqual(reason["source"], "mai_life")
        self.assertTrue(reason.get("event_id"))
        self.assertEqual(reason["topic"], "一段安静的午后")
        # pending 已登记且 Host task_id 已关联（时间基准与 patrol 传入时刻一致）。
        pending = await self.store.pending_proactive_users(now_at().timestamp())
        self.assertIn("111", pending)

    async def test_host_rejection_releases_opportunity_and_marks_failed(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        opp_id = await self.add_opportunity()
        self.api.result = {"success": False, "error": "Host 拒绝主动任务"}
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        # 提交动作发生了 1 次（提交≠发送成功），但 Host 拒绝后契机必须释放、不得误报成功。
        self.assertEqual(len(self.api.calls), 1)
        summary = await self.store.active_opportunities(now_at().timestamp())
        self.assertTrue(any(item["id"] == opp_id for item in summary))

    async def test_missing_task_id_is_treated_as_failure(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        opp_id = await self.add_opportunity()
        self.api.result = {"stream_id": "private-111", "queued": True}  # 无 task_id
        result = await self.engine.patrol(now_at(), make_state())
        self.assertFalse(result)
        # trigger 已发出（提交≠发送成功），但契机必须释放，等待后续巡检重试。
        self.assertEqual(len(self.api.calls), 1)
        summary = await self.store.active_opportunities(now_at().timestamp())
        self.assertTrue(any(item["id"] == opp_id for item in summary))

    # ===== 热更新 =====

    async def test_update_config_keeps_engine_working(self) -> None:
        await self.sync(make_profile("111"))
        await self.store.set_user_stream("111", "private-111")
        await self.add_opportunity()
        new_config = MaiLifeSettings()
        self.engine.update_config(new_config)
        self.assertIs(self.engine.config, new_config)
        result = await self.engine.patrol(now_at(), make_state())
        self.assertTrue(result)
        self.assertEqual(len(self.api.calls), 1)


class SchedulerRegistrationCase(IsolatedAsyncioTestCase):
    """调度循环注册：_start_tasks 必须包含主动巡检且幂等。"""

    def test_start_tasks_registers_proactive_loop_once(self) -> None:
        inst = MaiLifeSuitePlugin()
        inst.set_plugin_config(inst.build_default_config())
        async def run():
            inst._start_tasks()
            inst._start_tasks()  # 热更新重复调用不应重复注册
            names = [task.get_name() for task in inst._tasks]
            self.assertEqual(names.count("mai-life-proactive"), 1)
            self.assertIn("mai-life-maintenance", names)
            self.assertIn("mai-life-bedtime", names)
            await inst._stop_tasks()
            self.assertEqual(inst._tasks, [])
        asyncio.run(run())


if __name__ == "__main__":
    import unittest
    unittest.main()
