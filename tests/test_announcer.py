"""活动播报（AnnouncerService + LifeStore.announce_log）回归测试。

全部使用隔离临时数据库与 Fake 发送通道，不向真实 QQ 目标发送任何消息。
覆盖任务书要求的场景（第一版范围）：
- 配置门控：默认关闭、总开关、渠道开关、目标列表为空；
- 活动门控：无活动、睡眠跳过、数据不完整安全跳过、活动切换重播、
  框架重生成/场景细化不重播；
- 目标解析：群号与用户号分别解析、未配置目标不发送、不创建新会话；
- 限流与去重：按目标独立、跨重启持久化、失败不计成功、重试冷却、
  并发 claim 原子性、去重关闭时按日重播；
- 模板与异常：占位符渲染、缺场景降级、无效模板跳过、发送异常不中断、
  存储异常安全降级。
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.config import MaiLifeSuiteSettings
from MaiLifeSuite.core.storage import LifeStore
from MaiLifeSuite.life.announcer import AnnouncerService, build_activity_key

NOW = datetime(2026, 10, 28, 14, 30, 0)


# ── 测试替身 ──────────────────────────────────────────────


class FakeChat:
    """宿主 chat 能力：精确查询 + 枚举；记录 open_session 调用次数（必须为 0）。"""

    def __init__(self, groups: dict | None = None, privates: dict | None = None) -> None:
        self.groups = dict(groups or {})
        self.privates = dict(privates or {})
        self.open_calls = 0
        self.all_streams_calls = 0

    async def get_stream_by_group_id(self, group_id, platform="qq"):
        item = self.groups.get(str(group_id))
        return {"success": True, "stream": item} if item else {"success": False, "stream": None}

    async def get_stream_by_user_id(self, user_id, platform="qq"):
        item = self.privates.get(str(user_id))
        return {"success": True, "stream": item} if item else {"success": False, "stream": None}

    async def get_group_streams(self, platform="qq"):
        return {"success": True, "streams": list(self.groups.values())}

    async def get_private_streams(self, platform="qq"):
        return {"success": True, "streams": list(self.privates.values())}

    async def get_all_streams(self, platform="qq"):
        self.all_streams_calls += 1
        return {"success": True, "streams": [*self.groups.values(), *self.privates.values()]}

    async def open_session(self, **kwargs):
        self.open_calls += 1
        return {"success": False, "error": "播报测试不允许打开新会话"}


class FakeSend:
    """send.text 替身：可指定失败流（返回 False）与异常流；accepted 只记受理成功。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []       # 全部尝试（含失败）
        self.accepted: list[tuple[str, str]] = []    # 接口明确受理成功
        self.fail_streams: set[str] = set()
        self.raise_streams: set[str] = set()

    async def text(self, content, stream_id, **kwargs):
        if stream_id in self.raise_streams:
            raise RuntimeError("模拟发送异常")
        self.calls.append((stream_id, content))
        if stream_id in self.fail_streams:
            return False
        self.accepted.append((stream_id, content))
        return True


class FakeEngine:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def current_activity_payload(self) -> dict:
        return self.payload


async def fake_schedule_context(now):
    return {"nodes": [], "current": {"summary": "在家"}, "next": None,
            "scene": {"framework_id": "f1", "scene": "书桌前", "state_deltas": {}}}


def make_config(**overrides) -> MaiLifeSuiteSettings:
    """默认：总开关与双渠道开启，群 701 + 私聊 1001；冷却置 0 避免测试真实等待。"""
    config = MaiLifeSuiteSettings()
    config.announce.enabled = True
    config.announce.group_targets = ["701"]
    config.announce.private_targets = ["1001"]
    config.announce.group_enabled = True
    config.announce.private_enabled = True
    config.announce.llm_render_retry_interval_seconds = 0
    for key, value in overrides.items():
        setattr(config.announce, key, value)
    return config


def make_payload(name="整理书桌", goal_type="study", window="14:00-15:00",
                 has=True, error=None, as_of="2026-10-28T14:30:00+08:00", **extra) -> dict:
    active = has and not error
    activity = {"name": name, "description": "把桌面收拾整齐", "goal_type": goal_type,
                "time_window": window, **extra} if active else None
    return {"has_activity": active, "activity": activity, "next_activities": [],
            "as_of": as_of if active else "", "timezone": "Asia/Shanghai", "error": error}


def default_chat() -> FakeChat:
    return FakeChat(
        groups={"701": {"stream_id": "stream-g701", "group_id": "701", "group_name": "测试群",
                        "account_id": "555555555", "scope": "MaiBot Main"},
                "702": {"stream_id": "stream-g702", "group_id": "702", "group_name": "未配置群",
                        "account_id": "555555555", "scope": "MaiBot Main"}},
        privates={"1001": {"stream_id": "stream-p1001", "user_id": "1001",
                           "account_id": "555555555", "scope": "MaiBot Main"},
                  "1002": {"stream_id": "stream-p1002", "user_id": "1002",
                           "account_id": "555555555", "scope": "MaiBot Main"}},
    )


class FakeAnnounceLLM:
    """LLMService.generate 替身：验证渲染调用、显式任务名与失败回退。"""

    def __init__(self, available=("replyer", "planner", "utils"),
                 response="我去收拾下书桌，一会儿聊") -> None:
        self.available_tasks = set(available)
        self.response = response
        self.calls = 0
        self.task_names: list[str | None] = []
        self.raise_error = False
        self.fail_times = 0  # 前 N 次调用抛异常（模拟瞬时故障后恢复）

    async def generate(self, prompt, system="", max_tokens=0, temperature=0.5, *,
                       task_kind="reasoning", request_type="generic", task_name=None):
        self.calls += 1
        self.task_names.append(task_name)
        if self.raise_error or self.fail_times > 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise RuntimeError("模拟模型异常")
        return self.response


def make_service(store: LifeStore, config: MaiLifeSuiteSettings, engine: FakeEngine,
                 chat: FakeChat | None = None, send: FakeSend | None = None,
                 context=fake_schedule_context, llm: FakeAnnounceLLM | None = None) -> AnnouncerService:
    ctx = SimpleNamespace(chat=chat or default_chat(), send=send or FakeSend())
    return AnnouncerService(ctx, store, config, None, engine,
                            SimpleNamespace(context=context), logging.getLogger("test"), llm=llm)


GROUP_STREAM = "stream-g701"
PRIVATE_STREAM = "stream-p1001"


# ── 用例 ──────────────────────────────────────────────────


class AnnouncerCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mai_announce_test_")
        self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 配置门控 ──

    def test_default_config_all_off(self) -> None:
        """新配置默认关闭：总开关、双渠道关闭，名单列表为空，模式为白名单。"""
        cfg = MaiLifeSuiteSettings().announce
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.group_enabled)
        self.assertFalse(cfg.private_enabled)
        self.assertEqual(cfg.group_targets, [])
        self.assertEqual(cfg.private_targets, [])
        self.assertEqual(cfg.list_mode, "whitelist")
        self.assertEqual(cfg.template, "📢 麦麦现在去「{activity}」啦～{scene}")

    async def test_total_switch_off_blocks_all(self) -> None:
        """总开关关闭时，即使渠道开启、目标非空也不发送。"""
        config = make_config(enabled=False)
        engine = FakeEngine(make_payload())
        stats = await make_service(self.store, config, engine).tick(NOW)
        self.assertEqual(stats["sent"], 0)
        self.assertIn("disabled", stats["skipped"])

    async def test_empty_targets_no_send(self) -> None:
        config = make_config(group_targets=[], private_targets=[])
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("skipped_target_not_configured:group", stats["skipped"])
        self.assertIn("skipped_target_not_configured:private", stats["skipped"])

    async def test_group_channel_off_only_private_sent(self) -> None:
        config = make_config(group_enabled=False)
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual([c[0] for c in send.calls], [PRIVATE_STREAM])
        self.assertEqual(stats["sent"], 1)

    async def test_private_channel_off_only_group_sent(self) -> None:
        config = make_config(private_enabled=False)
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual([c[0] for c in send.calls], [GROUP_STREAM])
        self.assertEqual(stats["sent"], 1)

    async def test_zero_daily_cap_blocks_channel(self) -> None:
        """每日上限 0 表示该渠道当日禁发。"""
        config = make_config(daily_max_per_group=0, daily_max_per_private=0)
        send = FakeSend()
        await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual(send.calls, [])

    # ── 活动门控 ──

    async def test_announce_current_activity_to_both_targets(self) -> None:
        send = FakeSend()
        stats = await make_service(self.store, make_config(), FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual({c[0] for c in send.calls}, {GROUP_STREAM, PRIVATE_STREAM})
        for _, text in send.calls:
            self.assertIn("整理书桌", text)
            self.assertIn("书桌前", text)

    async def test_no_activity_safely_skipped(self) -> None:
        send = FakeSend()
        stats = await make_service(self.store, make_config(),
                                   FakeEngine(make_payload(has=False)), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("no_current_activity", stats["skipped"])

    async def test_schedule_error_skipped(self) -> None:
        send = FakeSend()
        stats = await make_service(self.store, make_config(),
                                   FakeEngine(make_payload(error="snapshot_not_ready")), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertTrue(any(item.startswith("schedule_error:") for item in stats["skipped"]))

    async def test_sleep_skipped_by_default(self) -> None:
        """sleep/nap 映射后的 goal_type='sleep' 默认跳过。"""
        send = FakeSend()
        stats = await make_service(self.store, make_config(),
                                   FakeEngine(make_payload(goal_type="sleep", window="23:00-07:00")),
                                   send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("sleep_activity", stats["skipped"])

    async def test_sleep_announced_when_skip_disabled(self) -> None:
        config = make_config(skip_sleep=False)
        send = FakeSend()
        stats = await make_service(self.store, config,
                                   FakeEngine(make_payload(goal_type="sleep", window="23:00-07:00")),
                                   send=send).tick(NOW)
        self.assertEqual(stats["sent"], 2)

    async def test_incomplete_activity_data_safe_skip(self) -> None:
        """活动缺 time_window 时无法可靠区分 → 安全跳过而不是连发。"""
        send = FakeSend()
        stats = await make_service(self.store, make_config(),
                                   FakeEngine(make_payload(window="")), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("incomplete_activity_data", stats["skipped"])

    async def test_same_activity_not_repeated(self) -> None:
        service = make_service(self.store, make_config(), FakeEngine(make_payload()))
        first = await service.tick(NOW)
        self.assertEqual(first["sent"], 2)
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 0)
        self.assertIn(f"already_announced:group:701", second["skipped"])
        self.assertIn(f"already_announced:private:1001", second["skipped"])

    async def test_activity_switch_reannounces(self) -> None:
        engine = FakeEngine(make_payload())
        service = make_service(self.store, make_config(), engine)
        self.assertEqual((await service.tick(NOW))["sent"], 2)
        engine.payload = make_payload(name="午休", goal_type="rest", window="13:00-14:00")
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 2)

    async def test_framework_regen_same_window_no_repeat(self) -> None:
        """框架重生成（活动 ID/描述变化但同日起止+类型）不视为新活动。"""
        engine = FakeEngine(make_payload(goal_id="old-goal", description="旧描述"))
        service = make_service(self.store, make_config(), engine)
        self.assertEqual((await service.tick(NOW))["sent"], 2)
        # 重生成后 goal_id 与描述变化、名称换写法，但类型与时间窗不变。
        engine.payload = make_payload(name="收拾房间", goal_id="new-goal", description="新描述")
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 0)

    async def test_scene_text_change_no_repeat(self) -> None:
        """仅场景细化文本变化不产生重复播报（场景不参与活动键）。"""
        engine = FakeEngine(make_payload())
        service = make_service(self.store, make_config(), engine)
        self.assertEqual((await service.tick(NOW))["sent"], 2)
        async def refined_context(now):
            base = await fake_schedule_context(now)
            base["scene"] = {"framework_id": "f1", "scene": "书桌前·整理第三层抽屉", "state_deltas": {}}
            return base
        service.schedule_service = SimpleNamespace(context=refined_context)
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 0)

    # ── 目标解析 ──

    async def test_group_and_private_resolved_separately(self) -> None:
        send = FakeSend()
        await make_service(self.store, make_config(), FakeEngine(make_payload()), send=send).tick(NOW)
        texts = dict(send.calls)
        self.assertIn(GROUP_STREAM, texts)
        self.assertIn(PRIVATE_STREAM, texts)
        self.assertNotEqual(texts[GROUP_STREAM], "")

    async def test_unconfigured_target_never_sent(self) -> None:
        """会话存在但未配置的目标（群 702/用户 1002）永远不发送。"""
        send = FakeSend()
        await make_service(self.store, make_config(), FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual({c[0] for c in send.calls}, {GROUP_STREAM, PRIVATE_STREAM})

    async def test_never_opens_new_session(self) -> None:
        chat = default_chat()
        await make_service(self.store, make_config(), FakeEngine(make_payload()), chat=chat).tick(NOW)
        self.assertEqual(chat.open_calls, 0)

    async def test_group_id_not_treated_as_user_id(self) -> None:
        """把群号填进私聊名单不会把群会话当私聊发送；私聊解析失败仅跳过。"""
        config = make_config(private_targets=["701"])  # 701 是群号，不是用户号
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual([c[0] for c in send.calls], [GROUP_STREAM])
        self.assertIn("no_stream:private:701", stats["skipped"])

    async def test_unresolvable_stream_skipped(self) -> None:
        chat = FakeChat()  # 宿主没有任何会话
        send = FakeSend()
        stats = await make_service(self.store, make_config(), FakeEngine(make_payload()),
                                   chat=chat, send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("no_stream:group:701", stats["skipped"])
        self.assertIn("no_stream:private:1001", stats["skipped"])

    # ── 限流与去重 ──

    async def test_daily_limit_independent_per_target(self) -> None:
        """群限额 1、私聊限额 2：第二个活动时群被限额跳过，私聊仍可发送。"""
        config = make_config(daily_max_per_group=1, daily_max_per_private=2)
        engine = FakeEngine(make_payload())
        send = FakeSend()
        service = make_service(self.store, config, engine, send=send)
        self.assertEqual((await service.tick(NOW))["sent"], 2)
        engine.payload = make_payload(name="午休", goal_type="rest", window="13:00-14:00")
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 1)  # 仅私聊
        self.assertIn("daily_limit:group:701", second["skipped"])
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "group", "701"), 1)
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "private", "1001"), 2)

    async def test_dedup_persists_across_rebuild(self) -> None:
        """插件重启（重建 Store 与服务）后去重与每日限额仍然有效。"""
        engine = FakeEngine(make_payload())
        send = FakeSend()
        config = make_config()
        await make_service(self.store, config, engine, send=send).tick(NOW)
        self.assertEqual(len(send.calls), 2)
        await self.store.close()
        self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
        await self.store.initialize()
        rebuilt = make_service(self.store, config, engine, send=send)
        stats = await rebuilt.tick(NOW)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(len(send.calls), 2)  # 没有新消息

    async def test_failed_send_not_counted_as_success(self) -> None:
        """发送失败不计入成功去重与每日限额，只进入重试冷却。"""
        send = FakeSend()
        send.fail_streams.add(GROUP_STREAM)
        config = make_config(retry_interval_minutes=1)
        service = make_service(self.store, make_config(), FakeEngine(make_payload()), send=send)
        stats = await service.tick(NOW)
        self.assertEqual(stats["sent"], 1)  # 私聊成功
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "group", "701"), 0)
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "private", "1001"), 1)
        # 立即重查：群在冷却中，私聊已去重。
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 0)
        self.assertIn("retry_cooldown:group:701", second["skipped"])
        self.assertIn("already_announced:private:1001", second["skipped"])

    async def test_retry_after_cooldown_succeeds(self) -> None:
        """冷却期满后重试同一活动可以成功并正常去重。"""
        send = FakeSend()
        send.fail_streams.add(GROUP_STREAM)
        config = make_config(retry_interval_minutes=1)
        service = make_service(self.store, config, FakeEngine(make_payload()), send=send)
        await service.tick(NOW)
        self.assertEqual([c[0] for c in send.accepted], [PRIVATE_STREAM])  # 仅私聊成功
        later = datetime(2026, 10, 28, 14, 32, 0)  # 超过 1 分钟冷却
        send.fail_streams.clear()
        stats = await service.tick(later)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual([c[0] for c in send.accepted], [PRIVATE_STREAM, GROUP_STREAM])
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "group", "701"), 1)

    async def test_concurrent_claim_is_atomic(self) -> None:
        """两个巡查并发占位同一目标+活动键时只有一个成功。"""
        store = self.store
        first = await store.announce_try_claim("group", "701", "k1", day="2026-10-28",
                                               now=NOW.timestamp(), deduplicate=True,
                                               retry_interval_minutes=10)
        second = await store.announce_try_claim("group", "701", "k1", day="2026-10-28",
                                                now=NOW.timestamp(), deduplicate=True,
                                                retry_interval_minutes=10)
        self.assertEqual(first, (True, "claimed"))
        self.assertEqual(second, (False, "in_progress"))

    async def test_deduplicate_off_resends_within_daily_cap(self) -> None:
        """去重关闭时同日活动按日重播，仍受每日限额约束。"""
        config = make_config(deduplicate=False, daily_max_per_group=2, daily_max_per_private=2)
        engine = FakeEngine(make_payload())
        send = FakeSend()
        service = make_service(self.store, config, engine, send=send)
        self.assertEqual((await service.tick(NOW))["sent"], 2)
        self.assertEqual((await service.tick(NOW))["sent"], 2)  # 重播
        third = await service.tick(NOW)  # 限额到达
        self.assertEqual(third["sent"], 0)
        self.assertIn("daily_limit:group:701", third["skipped"])
        self.assertIn("daily_limit:private:1001", third["skipped"])

    # ── 模板与异常 ──

    async def test_template_placeholders_rendered(self) -> None:
        config = make_config(template="[{activity}]({scene})@{time}")
        send = FakeSend()
        await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        texts = dict(send.calls)
        self.assertEqual(texts[GROUP_STREAM], "[整理书桌](书桌前)@14:30")

    async def test_missing_scene_degrades_safely(self) -> None:
        """场景缺失时渲染不含 None/null/空括号，回退当前节点摘要。"""
        async def empty_scene(now):
            return {"nodes": [], "current": {"summary": "在家休息"}, "next": None, "scene": {}}
        config = make_config(template="[{activity}]({scene})@{time}")
        send = FakeSend()
        service = make_service(self.store, config, FakeEngine(make_payload()), send=send,
                               context=empty_scene)
        await service.tick(NOW)
        texts = dict(send.calls)
        self.assertEqual(texts[GROUP_STREAM], "[整理书桌](在家休息)@14:30")

    async def test_invalid_template_skips_without_crash(self) -> None:
        """模板格式错误：记录日志并跳过本次发送，巡查循环正常返回。"""
        config = make_config(template="坏模板 {oops")
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("template_error:group:701", stats["skipped"])
        self.assertEqual(stats["sent"], 0)

    async def test_send_exception_does_not_block_other_targets(self) -> None:
        """发送接口抛异常：该目标记失败，其他目标继续完成。"""
        send = FakeSend()
        send.raise_streams.add(GROUP_STREAM)
        config = make_config()
        service = make_service(self.store, config, FakeEngine(make_payload()), send=send)
        stats = await service.tick(NOW)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual([c[0] for c in send.calls], [PRIVATE_STREAM])

    async def test_send_return_false_is_failure(self) -> None:
        """send.text 返回 False 只能视为发送失败，不得计入成功。"""
        send = FakeSend()
        send.fail_streams.add(PRIVATE_STREAM)
        stats = await make_service(self.store, make_config(), FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "private", "1001"), 0)

    async def test_storage_error_safely_skips(self) -> None:
        """存储异常时无法确认去重状态 → 安全降级为不发送。"""
        async def broken_claim(*args, **kwargs):
            raise RuntimeError("模拟数据库故障")
        self.store.announce_try_claim = broken_claim
        send = FakeSend()
        stats = await make_service(self.store, make_config(), FakeEngine(make_payload()), send=send).tick(NOW)
        self.assertEqual(send.calls, [])
        self.assertIn("storage_error:group:701", stats["skipped"])
        self.assertIn("storage_error:private:1001", stats["skipped"])

    async def test_config_validation(self) -> None:
        """非法名单 ID / 空模板 / 越界间隔在配置层被拒绝。"""
        from pydantic import ValidationError
        config = MaiLifeSuiteSettings()
        with self.assertRaises(ValidationError):
            config.announce.group_targets = ["abc群"]
        with self.assertRaises(ValidationError):
            config.announce.template = "   "
        with self.assertRaises(ValidationError):
            config.announce.patrol_interval_minutes = 0
        # 清洗：逗号/空格分隔、去重保序。
        config.announce.private_targets = "1001, 1002，1001"
        self.assertEqual(config.announce.private_targets, ["1001", "1002"])

    async def test_legacy_config_fields_migrated(self) -> None:
        """旧版 6 列表字段加载时自动迁移为「名单模式 + 2 个目标列表」。"""
        legacy = {
            "enabled": True, "group_enabled": True, "private_enabled": True,
            "group_ids": ["701"], "private_ids": ["1001"],
            "group_whitelist": ["702"], "group_blacklist": [],
            "private_whitelist": [], "private_blacklist": ["222222222"],
        }
        cfg = MaiLifeSuiteSettings(**{"announce": legacy}).announce
        self.assertEqual(cfg.group_targets, ["701"])       # 旧基础列表优先
        self.assertEqual(cfg.private_targets, ["1001"])
        self.assertEqual(cfg.list_mode, "whitelist")       # 默认白名单模式，行为不变
        # 旧字段不再出现在模型中（未知键已被迁移器移除）。
        self.assertFalse(hasattr(cfg, "group_ids"))

    def test_activity_key_shape(self) -> None:
        self.assertEqual(build_activity_key("2026-10-28", "study", "14:00-15:00"),
                         "2026-10-28:study:14:00-15:00")

    # ── 择优选流（修复生产 scope 路由故障） ──

    async def test_prefers_scope_bound_stream(self) -> None:
        """同一群存在新旧两条会话时，优先选择 scope 可路由的活跃会话（生产故障修复）。"""
        chat = FakeChat(
            groups={
                # 旧会话在前（模拟宿主按插入序枚举），scope 为空 → 发送会回退 legacy 驱动失败。
                "701": {"stream_id": "stream-g-old", "group_id": "701", "group_name": "测试群旧会话",
                        "account_id": "555555555", "scope": None},
            },
            privates={
                "1001": {"stream_id": "stream-p-old", "user_id": "1001",
                         "account_id": "555555555", "scope": None},
            },
        )
        # 追加新会话：枚举顺序上在旧会话之后。
        chat.groups["701-new"] = {"stream_id": "stream-g701", "group_id": "701",
                                  "group_name": "测试群", "account_id": "555555555", "scope": "MaiBot Main"}
        chat.privates["1001-new"] = {"stream_id": "stream-p1001", "user_id": "1001",
                                     "account_id": "555555555", "scope": "MaiBot Main"}
        send = FakeSend()
        await make_service(self.store, make_config(), FakeEngine(make_payload()), chat=chat, send=send).tick(NOW)
        self.assertEqual({c[0] for c in send.accepted}, {"stream-g701", "stream-p1001"})

    async def test_send_failure_logs_stream_diag(self) -> None:
        """发送失败日志附带首选候选会话的 scope/account 诊断，不再只留"返回 False"。"""
        chat = default_chat()
        chat.groups["701"]["scope"] = None  # 模拟旧会话被选中
        send = FakeSend()
        send.fail_streams.add(GROUP_STREAM)
        with self.assertLogs("test", level="WARNING") as captured:
            await make_service(self.store, make_config(), FakeEngine(make_payload()),
                               chat=chat, send=send).tick(NOW)
        self.assertTrue(any("scope=空" in line and "尝试 1/1" in line for line in captured.output))

    # ── 候选回退链与 scope 记忆（修复私聊旧实例会话路由失败） ──

    async def test_private_send_falls_back_to_next_candidate(self) -> None:
        """私聊存在多条会话时：首选候选发送失败自动尝试下一候选，成功即结算。"""
        chat = FakeChat(privates={
            # 两条候选：旧实例会话（洛天依 scope）在前，当前适配器会话（MaiBot Main）在后。
            "1001": {"stream_id": "stream-p-luotianyi", "user_id": "1001",
                     "account_id": "555555555", "scope": "洛天依（maibot v0.4.21）"},
            "1001-new": {"stream_id": "stream-p-main", "user_id": "1001",
                         "account_id": "555555555", "scope": "MaiBot Main"},
        })
        send = FakeSend()
        send.fail_streams.add("stream-p-luotianyi")  # 旧 scope 会话路由失败
        config = make_config(group_targets=[], private_targets=["1001"])
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=chat, send=send).tick(NOW)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual([c[0] for c in send.accepted], ["stream-p-main"])
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "private", "1001"), 1)

    async def test_preferred_scope_learned_from_group_success(self) -> None:
        """群聊发送成功后记忆 scope；私聊多候选排序优先同 scope 会话。"""
        chat = FakeChat(
            groups={"701": {"stream_id": "stream-g-main", "group_id": "701",
                            "account_id": "555555555", "scope": "MaiBot Main"}},
            privates={
                "1001": {"stream_id": "stream-p-luotianyi", "user_id": "1001",
                         "account_id": "555555555", "scope": "洛天依（maibot v0.4.21）"},
                "1001-new": {"stream_id": "stream-p-main", "user_id": "1001",
                             "account_id": "555555555", "scope": "MaiBot Main"},
            },
        )
        send = FakeSend()
        config = make_config()
        service = make_service(self.store, config, FakeEngine(make_payload()), chat=chat, send=send)
        await service.tick(NOW)  # 群成功 → _preferred_scope = "MaiBot Main"
        self.assertEqual(service._preferred_scope, "MaiBot Main")
        # 第二轮：新活动。私聊应直接先试 MaiBot Main 候选（scope 记忆生效）。
        service.schedule_engine = FakeEngine(make_payload(name="午休", goal_type="rest", window="13:00-14:00"))
        send2 = FakeSend()
        service.ctx.send = send2
        await service.tick(NOW)
        self.assertEqual([c[0] for c in send2.calls], ["stream-g-main", "stream-p-main"])  # 私聊首选命中记忆 scope

    # ── LLM 拟人渲染 ──

    async def test_llm_render_disabled_by_default(self) -> None:
        """渲染默认关闭：不调用模型，直接发模板文案。"""
        llm = FakeAnnounceLLM()
        send = FakeSend()
        await make_service(self.store, make_config(), FakeEngine(make_payload()),
                           send=send, llm=llm).tick(NOW)
        self.assertEqual(llm.calls, 0)
        self.assertTrue(all("整理书桌" in text for _, text in send.accepted))

    async def test_llm_render_rewrites_text(self) -> None:
        """开启渲染：播报文本使用 LLM 输出，且显式传宿主任务名。"""
        llm = FakeAnnounceLLM(response="先去把书桌理一理，回聊")
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_task="replyer")
        await make_service(self.store, config, FakeEngine(make_payload()), send=send, llm=llm).tick(NOW)
        self.assertEqual(llm.calls, 2)  # 群 + 私聊各渲染一次
        self.assertEqual(set(llm.task_names), {"replyer"})
        self.assertTrue(all(text == "先去把书桌理一理，回聊" for _, text in send.accepted))

    async def test_llm_render_task_unavailable_falls_back(self) -> None:
        """渲染任务不可用：不调用模型，回退模板文案。"""
        llm = FakeAnnounceLLM(available=("planner",))
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_task="replyer")
        await make_service(self.store, config, FakeEngine(make_payload()), send=send, llm=llm).tick(NOW)
        self.assertEqual(llm.calls, 0)
        self.assertTrue(all("整理书桌" in text for _, text in send.accepted))

    async def test_llm_render_failure_falls_back(self) -> None:
        """模型异常/空输出：回退模板文案，播报仍然发出。"""
        llm = FakeAnnounceLLM()
        llm.raise_error = True
        send = FakeSend()
        config = make_config(llm_render_enabled=True)
        stats = await make_service(self.store, config, FakeEngine(make_payload()), send=send, llm=llm).tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertTrue(all("整理书桌" in text for _, text in send.accepted))
        llm2 = FakeAnnounceLLM(response="   \n  ")
        send2 = FakeSend()
        service2 = make_service(self.store, config,
                                FakeEngine(make_payload(name="午休", goal_type="rest", window="13:00-14:00")),
                                send=send2, llm=llm2)
        stats2 = await service2.tick(NOW)
        self.assertEqual(stats2["sent"], 2)
        self.assertTrue(all("整理书桌" not in text for _, text in send2.accepted))  # 空渲染回退的是新活动的模板稿

    async def test_llm_render_retry_then_success(self) -> None:
        """首次渲染失败后按重试次数自动重试，第二次成功则使用 LLM 文案。"""
        llm = FakeAnnounceLLM()
        llm.fail_times = 1  # 第一次失败，第二次恢复
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_max_retries=2)
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   send=send, llm=llm).tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(llm.calls, 3)  # 群：失败+重试成功（2 次）；私聊：首次成功（1 次）
        self.assertTrue(all(text == "我去收拾下书桌，一会儿聊" for _, text in send.accepted))

    async def test_llm_render_retry_exhausted_falls_back(self) -> None:
        """全部重试耗尽后回退模板文案，播报仍然发出，且不再无限重试。"""
        llm = FakeAnnounceLLM()
        llm.raise_error = True
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_max_retries=1)
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   send=send, llm=llm).tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(llm.calls, 4)  # 2 个目标 × (1 首次 + 1 重试)，不无限重试
        self.assertTrue(all("整理书桌" in text for _, text in send.accepted))

    async def test_llm_render_zero_retries_single_attempt(self) -> None:
        """重试次数为 0：失败后立即回退模板，只调用一次。"""
        llm = FakeAnnounceLLM()
        llm.fail_times = 5
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_max_retries=0)
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   send=send, llm=llm).tick(NOW)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(llm.calls, 2)  # 每个目标各 1 次，不重试
        self.assertTrue(all("整理书桌" in text for _, text in send.accepted))

    async def test_llm_render_retry_cooldown(self) -> None:
        """重试冷却：按配置的秒数在两次渲染尝试之间等待，0 表示立即重试。"""
        from unittest.mock import patch
        from MaiLifeSuite.life import announcer as announcer_module
        llm = FakeAnnounceLLM()
        llm.fail_times = 5
        send = FakeSend()
        config = make_config(llm_render_enabled=True, llm_render_max_retries=2,
                             llm_render_retry_interval_seconds=7)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with patch.object(announcer_module.asyncio, "sleep", side_effect=fake_sleep):
            await make_service(self.store, config, FakeEngine(make_payload()),
                               send=send, llm=llm).tick(NOW)
        self.assertEqual(sleeps, [7, 7, 7, 7])  # 2 个目标 × 3 次尝试，各自 2 个间隔
        # 冷却为 0：不等待。
        llm2 = FakeAnnounceLLM()
        llm2.fail_times = 5
        send2 = FakeSend()
        config2 = make_config(llm_render_enabled=True, llm_render_max_retries=2,
                              llm_render_retry_interval_seconds=0)
        sleeps2: list[float] = []
        with patch.object(announcer_module.asyncio, "sleep", side_effect=fake_sleep):
            await make_service(self.store, config2, FakeEngine(make_payload()),
                               send=send2, llm=llm2).tick(NOW)
        self.assertEqual(sleeps2, [])

    # ── 黑白名单（名单模式） ──

    async def test_whitelist_mode_sends_only_listed_targets(self) -> None:
        """白名单模式：列表即允许目标；列表外的会话不发送。"""
        config = make_config(group_targets=["701"])  # 702 在会话里但不在名单
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        self.assertEqual({c[0] for c in send.accepted}, {GROUP_STREAM, PRIVATE_STREAM})
        self.assertNotIn("stream-g702", {c[0] for c in send.calls})

    async def test_blacklist_mode_excludes_listed_and_sends_rest(self) -> None:
        """黑名单模式：名单内的群被排除，名单外的已注册群收到播报。"""
        config = make_config(list_mode="blacklist", group_targets=["701"],
                             daily_max_per_group=5)
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        accepted = {c[0] for c in send.accepted}
        self.assertNotIn(GROUP_STREAM, accepted)      # 701 被黑名单排除
        self.assertIn("stream-g702", accepted)        # 名单外的群正常收到
        self.assertIn("skipped_in_blacklist:group:701", stats["skipped"])

    async def test_blacklist_mode_empty_list_sends_all(self) -> None:
        """黑名单模式 + 空名单：发给全部已注册会话（不禁任何目标）。"""
        config = make_config(list_mode="blacklist", group_targets=[], private_targets=[])
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        self.assertEqual({c[0] for c in send.accepted},
                         {GROUP_STREAM, "stream-g702", PRIVATE_STREAM, "stream-p1002"})

    async def test_blacklist_mode_private_independent(self) -> None:
        """黑名单模式群/私聊名单独立：群被排除不影响私聊，反之亦然。"""
        config = make_config(list_mode="blacklist",
                             group_targets=["701"], private_targets=["1001"])
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        accepted = {c[0] for c in send.accepted}
        self.assertNotIn(GROUP_STREAM, accepted)
        self.assertNotIn(PRIVATE_STREAM, accepted)
        self.assertIn("stream-g702", accepted)
        self.assertIn("stream-p1002", accepted)
        self.assertIn("skipped_in_blacklist:group:701", stats["skipped"])
        self.assertIn("skipped_in_blacklist:private:1001", stats["skipped"])

    async def test_blacklist_mode_daily_limit_still_caps(self) -> None:
        """黑名单模式不绕过每日限额：全部会话共享各自的上限。"""
        config = make_config(list_mode="blacklist", group_targets=[], private_targets=[],
                             daily_max_per_group=1)
        engine = FakeEngine(make_payload())
        send = FakeSend()
        service = make_service(self.store, config, engine, chat=default_chat(), send=send)
        first = await service.tick(NOW)
        self.assertEqual(first["sent"], 4)  # 两个群 + 两个私聊
        engine.payload = make_payload(name="午休", goal_type="rest", window="13:00-14:00")
        second = await service.tick(NOW)
        self.assertEqual(second["sent"], 2)  # 群全部触限，仅私聊尚有额度
        self.assertIn("daily_limit:group:701", second["skipped"])
        self.assertIn("daily_limit:group:702", second["skipped"])

    async def test_whitelist_mode_unresolved_target_noted(self) -> None:
        """白名单模式：名单目标没有已注册会话时记录 no_stream，不发送。"""
        config = make_config(group_targets=["701", "999999999"], private_targets=[])
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        self.assertIn("no_stream:group:999999999", stats["skipped"])
        self.assertEqual({c[0] for c in send.accepted}, {GROUP_STREAM})

    async def test_mode_switch_hot_update(self) -> None:
        """配置热更新切换名单模式后立即按新模式筛选。"""
        chat = default_chat()
        service = make_service(self.store, make_config(), FakeEngine(make_payload()),
                               chat=chat, send=FakeSend())
        await service.tick(NOW)  # 白名单模式正常发送
        updated = make_config(list_mode="blacklist", group_targets=["701"])
        service.update_config(updated)
        send2 = FakeSend()
        chat.all_streams_calls = 0
        service.send = send2
        service2 = make_service(self.store, updated, FakeEngine(make_payload()), chat=chat, send=send2)
        stats = await service2.tick(NOW)
        self.assertNotIn(GROUP_STREAM, {c[0] for c in send2.accepted})
        self.assertIn("stream-g702", {c[0] for c in send2.accepted})
        self.assertIn("skipped_in_blacklist:group:701", stats["skipped"])

    async def test_blacklist_rejects_before_claim(self) -> None:
        """名单拦截发生在去重/限额/发送之前，不占用 claim 也不计失败。"""
        config = make_config(list_mode="blacklist", group_targets=["701"])
        await make_service(self.store, config, FakeEngine(make_payload())).tick(NOW)
        # 702 正常成功；701 没有产生任何 announce_log 记录。
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "group", "701"), 0)
        self.assertEqual(await self.store.announce_daily_count("2026-10-28", "group", "702"), 1)

    async def test_invalid_target_id_rejected(self) -> None:
        """非法名单 ID（绕过配置校验的场景）在过滤层默认拒绝，不跨类型猜测。"""
        from MaiLifeSuite.mailife_config import AnnounceSettings
        broken_announce = AnnounceSettings.model_construct(
            enabled=True, list_mode="whitelist", group_enabled=True, private_enabled=True,
            group_targets=["701", "not-a-qq"], private_targets=["1001"],
            daily_max_per_group=5, daily_max_per_private=3, skip_sleep=True,
            deduplicate=True, retry_interval_minutes=10,
            template="📢 {activity}{scene}",
        )
        config = MaiLifeSuiteSettings.model_construct(announce=broken_announce)
        send = FakeSend()
        stats = await make_service(self.store, config, FakeEngine(make_payload()),
                                   chat=default_chat(), send=send).tick(NOW)
        self.assertEqual({c[0] for c in send.accepted}, {GROUP_STREAM, PRIVATE_STREAM})
        self.assertIn("skipped_invalid_target:group:not-a-qq", stats["skipped"])

    async def test_resolve_backoff_after_persistent_enumeration_failure(self) -> None:
        """会话枚举持续为空达阈值后退避若干巡查周期，期满自动恢复重试。"""
        chat = FakeChat()  # 永远返回空会话列表
        service = make_service(self.store, make_config(), FakeEngine(make_payload()), chat=chat)
        for _ in range(3):
            stats = await service.tick(NOW)
            self.assertNotIn("skipped_resolve_backoff:1/6", stats["skipped"])
        self.assertEqual(chat.all_streams_calls, 3)
        stats = await service.tick(NOW)  # 第 4 轮进入退避
        self.assertIn("skipped_resolve_backoff:1/6", stats["skipped"])
        self.assertEqual(chat.all_streams_calls, 3)  # 退避期不再枚举
        # 退避 6 轮后恢复重试。
        for _ in range(6):
            await service.tick(NOW)
        self.assertEqual(chat.all_streams_calls, 4)

    async def test_config_validation_for_lists(self) -> None:
        from pydantic import ValidationError
        config = MaiLifeSuiteSettings()
        with self.assertRaises(ValidationError):
            config.announce.group_targets = ["abc群"]
        with self.assertRaises(ValidationError):
            config.announce.private_targets = ["12 34"]
        config.announce.group_targets = "701, 702，701"
        self.assertEqual(config.announce.group_targets, ["701", "702"])
        with self.assertRaises(ValidationError):
            config.announce.list_mode = "灰色名单"


if __name__ == "__main__":
    from unittest import main
    main()
