"""每日日程生成机制回归：自动重试、归档、过期清理与随机兜底。

全部使用模拟 LLM（忠实复刻 generate_json 的"失败原样返回 fallback"语义）
与隔离临时 SQLite，不访问网络、不发送真实消息。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLifeSuite.config import MaiLifeSuiteSettings
from MaiLifeSuite.core.storage import LifeStore
from MaiLifeSuite.life.schedule_service import ScheduleService

PLUGIN_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周三
DAY = "2026-10-28"

# 一次能通过现有校验（夜眠≥5h、≥2餐、时间不重叠）的完整日程。
GOOD_NODES = [
    {"start": "00:00", "end": "07:30", "kind": "sleep", "summary": "夜间安睡", "location": "卧室", "energy_load": 8, "shareability": 0.05},
    {"start": "07:30", "end": "08:30", "kind": "meal", "summary": "起床吃早餐", "location": "家里", "energy_load": -1, "shareability": 0.3},
    {"start": "08:30", "end": "12:00", "kind": "work", "summary": "上午赶项目进度", "location": "公司", "energy_load": -4, "shareability": 0.3},
    {"start": "12:00", "end": "13:00", "kind": "meal", "summary": "午饭", "location": "食堂", "energy_load": -1, "shareability": 0.4},
    {"start": "13:00", "end": "13:40", "kind": "nap", "summary": "午休", "location": "公司", "energy_load": 3, "shareability": 0.05},
    {"start": "13:40", "end": "18:00", "kind": "work", "summary": "下午开会和写方案", "location": "公司", "energy_load": -4, "shareability": 0.3},
    {"start": "18:00", "end": "19:00", "kind": "meal", "summary": "晚饭", "location": "家里", "energy_load": -1, "shareability": 0.5},
    {"start": "19:00", "end": "23:00", "kind": "leisure", "summary": "玩两把新游戏", "location": "家里", "energy_load": -3, "shareability": 0.6},
    {"start": "23:00", "end": "24:00", "kind": "sleep", "summary": "准备睡觉", "location": "卧室", "energy_load": 1, "shareability": 0.05},
]

GOOD_NODES_V2 = [dict(item, summary=item["summary"] + "（新版）") for item in GOOD_NODES]


class FakeLLM:
    """generate_json 真实语义替身：脚本耗尽后默认成功返回 payload（可用 "sentinel" 显式失败）。"""

    def __init__(self) -> None:
        self.available = True
        self.scripts: list[object] = []   # "ok"→payload / "sentinel"→fallback / list|dict→原样 / Exception→抛出
        self.payload: list = GOOD_NODES
        self.calls = 0
        self.delay = 0.0

    def task_available(self, kind: str) -> bool:
        return self.available

    async def generate_json(self, prompt, system, fallback, max_tokens=0, *, task_kind="", request_type=""):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.scripts:
            return self.payload
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == "ok":
            return self.payload
        if item == "sentinel":
            return fallback
        return item


def _validated(payload: list, day: str) -> list:
    """把原始 LLM 格式（start/end 字符串）转成通过校验的存储格式（分钟制+ID），与真实归档内容一致。"""
    svc = ScheduleService(None, MaiLifeSuiteSettings(), None, str(PLUGIN_DIR), logging.getLogger("test"))
    nodes = svc._validate(day, payload, {})
    assert nodes, "测试夹具日程必须能通过现有校验"
    return nodes


# 归档夹具：与生产一致，归档的是校验后的完整节点快照。
ARCHIVE_NODES = _validated(GOOD_NODES, "2026-10-27")
ARCHIVE_NODES_V2 = _validated(GOOD_NODES_V2, "2026-10-27")


def _template_first_summary(day_type: str) -> str:
    """读取插件自带的 mai_template.json 首节点摘要，作为固定兜底的期望值。"""
    import json as _json
    data = _json.loads((PLUGIN_DIR / "mai_template.json").read_text(encoding="utf-8-sig"))
    return str(data[day_type][0]["summary"])


def make_config(**overrides) -> MaiLifeSuiteSettings:
    config = MaiLifeSuiteSettings()
    for key, value in overrides.items():
        setattr(config.schedule, key, value)
    return config


def make_service(store: LifeStore, llm: FakeLLM, config: MaiLifeSuiteSettings | None = None) -> ScheduleService:
    return ScheduleService(store, config or make_config(), llm, str(PLUGIN_DIR), logging.getLogger("test"))


def expected_node_id(day: str, node: dict) -> str:
    seed = f"{day}:{node['start_minute']}:{node['end_minute']}:{node['kind']}:{node['summary']}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


class GenerationCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mai_gen_test_")
        self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def rebuild(self) -> tuple[LifeStore, ScheduleService]:
        """模拟插件重启：关闭并重开同一数据库。"""
        await self.store.close()
        self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
        await self.store.initialize()
        llm = FakeLLM()
        return self.store, make_service(self.store, llm)

    # ── 基础重试流程 ──

    async def test_1_first_success_no_retry(self) -> None:
        llm = FakeLLM()
        service = make_service(self.store, llm)
        nodes = await service.ensure_day(NOW, "测试人格", "晴")
        self.assertEqual(len(nodes), 9)
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["has_temp"], 0)
        # 已成功：维护循环重复调用不再请求模型。
        await service.ensure_day(NOW, "测试人格", "晴")
        self.assertEqual(llm.calls, 1)

    async def test_2_retry_succeeds_on_second_attempt(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["sentinel", "ok"]
        service = make_service(self.store, llm, make_config(generation_retry_interval_minutes=10))
        first = await service.ensure_day(NOW, "p", "晴")
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "retrying")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["has_temp"], 1)  # 临时骨架已标记，不阻断重试
        # 未到重试时间：不发起新尝试。
        await service.ensure_day(NOW, "p", "晴")
        self.assertEqual(llm.calls, 1)
        # 到期重试成功。
        later = datetime(2026, 10, 28, 10, 11, tzinfo=ZoneInfo("Asia/Shanghai"))
        nodes = await service.ensure_day(later, "p", "晴")
        self.assertEqual(len(nodes), 9)
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["has_temp"], 0)

    async def test_3_success_before_limit(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["sentinel", "sentinel", "ok"]
        service = make_service(self.store, llm, make_config(generation_max_retries=3))
        now = NOW
        for _ in range(2):
            await service.ensure_day(now, "p", "晴")
            now = datetime.fromtimestamp(now.timestamp() + 11 * 60, ZoneInfo("Asia/Shanghai"))
        nodes = await service.ensure_day(now, "p", "晴")
        self.assertEqual(len(nodes), 9)
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["attempts"], 3)

    async def test_4_exhausted_falls_back_to_archive_random(self) -> None:
        await self.store.archive_schedule(ARCHIVE_NODES, source_day="2026-10-27", day_type="workday")
        llm = FakeLLM()
        llm.scripts = ["sentinel"] * 4
        service = make_service(self.store, llm, make_config(generation_retry_interval_minutes=1))
        now = NOW
        for _ in range(3):
            await service.ensure_day(now, "p", "晴")
            now = datetime.fromtimestamp(now.timestamp() + 2 * 60, ZoneInfo("Asia/Shanghai"))
        nodes = await service.ensure_day(now, "p", "晴")  # 第 4 次失败 → 最终兜底
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "exhausted")
        self.assertEqual(state["attempts"], 4)
        self.assertEqual(state["fallback_source"], "archive_random")
        self.assertEqual([n["summary"] for n in nodes], [n["summary"] for n in GOOD_NODES])
        self.assertEqual(llm.calls, 4)  # 兜底不再调用模型

    async def test_5_failure_modes_enter_retry(self) -> None:
        for script in (["sentinel"], [{"oops": 1}], [{"start": "09:00", "end": "10:00", "kind": "leisure"}], [RuntimeError("模型异常")]):
            with self.subTest(script=script):
                store = LifeStore(self.tmp, logger=logging.getLogger("test"))
                await store.initialize()
                llm = FakeLLM()
                llm.scripts = list(script)
                service = make_service(store, llm)
                nodes = await service.ensure_day(NOW, "p", "晴")
                state = await store.get_generation_state(DAY)
                self.assertEqual(state["status"], "retrying")
                self.assertEqual(state["attempts"], 1)
                self.assertTrue(state["last_error"])
                self.assertTrue(state["has_temp"] or nodes)
                await store.close()
                self.store = LifeStore(self.tmp, logger=logging.getLogger("test"))
                await self.store.initialize()
                import sqlite3
                conn = sqlite3.connect(self.store.path)
                conn.execute("DELETE FROM schedule_generation"); conn.execute("DELETE FROM daily_framework")
                conn.commit(); conn.close()

    async def test_6_unavailable_task_never_marked_success(self) -> None:
        llm = FakeLLM()
        llm.available = False
        service = make_service(self.store, llm)
        await service.ensure_day(NOW, "p", "晴")
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "retrying")
        self.assertIn("任务不可用", state["last_error"])
        self.assertEqual(await self.store.list_archives(NOW.timestamp(), 30), [])

    # ── 归档 ──

    async def test_7_success_is_archived(self) -> None:
        llm = FakeLLM()
        service = make_service(self.store, llm)
        await service.ensure_day(NOW, "p", "晴")
        archives = await self.store.list_archives(NOW.timestamp(), 30)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["source_day"], DAY)
        self.assertEqual(archives[0]["day_type"], "workday")
        self.assertEqual(archives[0]["generator"], "llm")
        import json as _json
        self.assertEqual(len(_json.loads(archives[0]["nodes_json"])), 9)

    async def test_8_force_success_creates_new_archive(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["ok"]
        service = make_service(self.store, llm)
        await service.ensure_day(NOW, "p", "晴")
        llm.payload = GOOD_NODES_V2
        llm.scripts = ["ok"]
        await service.ensure_day(NOW, "p", "晴", force=True)
        archives = await self.store.list_archives(NOW.timestamp(), 30)
        self.assertEqual(len(archives), 2)

    async def test_9_archives_survive_restart_and_reused(self) -> None:
        llm = FakeLLM()
        service = make_service(self.store, llm)
        await service.ensure_day(NOW, "p", "晴")
        store, service = await self.rebuild()  # 重启
        self.assertEqual(len(await store.list_archives(NOW.timestamp(), 30)), 1)
        # 次日模型持续失败 → 达上限后随机复用归档。
        tomorrow = datetime(2026, 10, 29, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        llm.scripts = ["sentinel"]
        service = make_service(store, llm, make_config(generation_max_retries=0))
        nodes = await service.ensure_day(tomorrow, "p", "晴")
        state = await store.get_generation_state("2026-10-29")
        self.assertEqual(state["fallback_source"], "archive_random")
        self.assertEqual([n["summary"] for n in nodes], [n["summary"] for n in GOOD_NODES])

    async def test_10_cleanup_by_archived_at(self) -> None:
        await self.store.archive_schedule(ARCHIVE_NODES, source_day="2026-10-01", day_type="workday")
        await self.store.archive_schedule(ARCHIVE_NODES_V2, source_day="2026-10-27", day_type="workday")
        import sqlite3
        conn = sqlite3.connect(self.store.path)
        old_ts = NOW.timestamp() - 40 * 86400
        conn.execute("UPDATE schedule_archives SET archived_at=? WHERE source_day='2026-10-01'", (old_ts,))
        conn.execute(
            "INSERT INTO schedule_generation(day,status,attempts,updated_at) VALUES('2026-10-01','retrying',1,?)",
            (NOW.timestamp(),))
        conn.commit(); conn.close()
        removed = await self.store.cleanup_schedule_records(30, NOW.timestamp())
        self.assertEqual(removed, 1)
        remaining = await self.store.list_archives(NOW.timestamp(), 30)
        self.assertEqual([a["source_day"] for a in remaining], ["2026-10-27"])
        # retrying 状态与未过期归档绝不误删；幂等重复执行安全。
        self.assertIsNotNone(await self.store.get_generation_state("2026-10-01"))
        self.assertEqual(await self.store.cleanup_schedule_records(30, NOW.timestamp()), 0)

    # ── 随机兜底 ──

    async def test_11_archive_conversion_updates_ids(self) -> None:
        await self.store.archive_schedule(ARCHIVE_NODES, source_day="2026-10-20", day_type="workday")
        llm = FakeLLM()
        llm.scripts = ["sentinel"]
        service = make_service(self.store, llm, make_config(generation_max_retries=0))
        nodes = await service.ensure_day(NOW, "p", "晴")
        self.assertTrue(nodes)
        for node in nodes:
            self.assertEqual(node["day"], DAY)
            self.assertEqual(node["id"], expected_node_id(DAY, node))
        self.assertEqual([n["start_minute"] for n in nodes], [0, 450, 510, 720, 780, 820, 1080, 1140, 1380])
        self.assertEqual([n["end_minute"] for n in nodes], [450, 510, 720, 780, 820, 1080, 1140, 1380, 1440])

    async def test_12_no_archive_uses_builtin_template(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["sentinel"]
        service = make_service(self.store, llm, make_config(generation_max_retries=0))
        nodes = await service.ensure_day(NOW, "p", "晴")
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["fallback_source"], "builtin_template")
        # 固定兜底 = 模板文件（无模板文件时才用硬编码骨架）的工作日首节点。
        template_first = _template_first_summary("workday")
        self.assertEqual(nodes[0]["summary"], template_first)
        self.assertFalse(await self.store.list_archives(NOW.timestamp(), 30))  # 兜底不重新归档

    async def test_13_force_failure_preserves_existing(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["ok"]
        service = make_service(self.store, llm)
        first = await service.ensure_day(NOW, "p", "晴")
        first_ids = [n["id"] for n in first]
        llm.scripts = ["sentinel"]
        second = await service.ensure_day(NOW, "p", "晴", force=True)
        self.assertEqual([n["id"] for n in second], first_ids)  # 原日程未被覆盖
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "retrying")
        self.assertEqual(state["attempts"], 1)  # 新一轮的第一次尝试

    async def test_14_retry_count_survives_restart(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["sentinel"]
        service = make_service(self.store, llm)
        await service.ensure_day(NOW, "p", "晴")
        store, service = await self.rebuild()
        llm = FakeLLM()
        service = make_service(store, llm)
        # 未到期：重启后不重置计数也不提前重试。
        await service.ensure_day(NOW, "p", "晴")
        self.assertEqual(llm.calls, 0)
        state = await store.get_generation_state(DAY)
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["status"], "retrying")
        # 到期：补偿重试一次（不逐个补跑错过的间隔）。
        later = datetime(2026, 10, 28, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
        llm.scripts = ["sentinel"]
        await service.ensure_day(later, "p", "晴")
        self.assertEqual(llm.calls, 1)
        self.assertEqual((await store.get_generation_state(DAY))["attempts"], 2)

    async def test_15_concurrent_generation_single_call(self) -> None:
        llm = FakeLLM()
        llm.delay = 0.05
        service = make_service(self.store, llm)
        results = await asyncio.gather(
            service.ensure_day(NOW, "p", "晴"),
            service.ensure_day(NOW, "p", "晴"),
        )
        self.assertEqual(llm.calls, 1)
        self.assertEqual([n["id"] for n in results[0]], [n["id"] for n in results[1]])

    async def test_16_manual_force_opens_new_round(self) -> None:
        llm = FakeLLM()
        llm.scripts = ["sentinel"]
        service = make_service(self.store, llm, make_config(generation_max_retries=0))
        await service.ensure_day(NOW, "p", "晴")  # 立即达上限
        self.assertEqual((await self.store.get_generation_state(DAY))["status"], "exhausted")
        llm.scripts = ["ok"]
        nodes = await service.ensure_day(NOW, "p", "晴", force=True)
        self.assertEqual(len(nodes), 9)
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["attempts"], 1)

    async def test_17_archive_failure_logged_not_silent(self) -> None:
        llm = FakeLLM()
        service = make_service(self.store, llm)
        async def broken_archive(*args, **kwargs):
            raise RuntimeError("模拟归档写入失败")
        self.store.archive_schedule = broken_archive
        with self.assertLogs("test", level="ERROR") as captured:
            nodes = await service.ensure_day(NOW, "p", "晴")
        self.assertTrue(any("日程归档写入失败" in line for line in captured.output))
        self.assertEqual(len(nodes), 9)  # 日程本身仍生效
        self.assertEqual((await self.store.get_generation_state(DAY))["status"], "success")

    async def test_17b_corrupted_archive_skipped(self) -> None:
        await self.store.archive_schedule(ARCHIVE_NODES, source_day="2026-10-20", day_type="workday")
        import sqlite3
        conn = sqlite3.connect(self.store.path)
        conn.execute("UPDATE schedule_archives SET nodes_json='不是JSON' WHERE source_day='2026-10-20'")
        conn.commit(); conn.close()
        llm = FakeLLM()
        llm.scripts = ["sentinel"]
        service = make_service(self.store, llm, make_config(generation_max_retries=0))
        nodes = await service.ensure_day(NOW, "p", "晴")
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["fallback_source"], "builtin_template")
        self.assertEqual(nodes[0]["summary"], _template_first_summary("workday"))

    # ── 随机性与配置 ──

    async def test_18_random_selection_varies(self) -> None:
        await self.store.archive_schedule(ARCHIVE_NODES, source_day="2026-10-20", day_type="workday")
        await self.store.archive_schedule(ARCHIVE_NODES_V2, source_day="2026-10-21", day_type="workday")
        picked = []
        for seed in (1, 2, 3, 4, 5, 6, 7, 8):
            import sqlite3
            conn = sqlite3.connect(self.store.path)
            conn.execute("DELETE FROM schedule_generation"); conn.execute("DELETE FROM daily_framework")
            conn.commit(); conn.close()
            llm = FakeLLM()
            llm.scripts = ["sentinel"]
            service = make_service(self.store, llm, make_config(generation_max_retries=0))
            random.seed(seed)
            nodes = await service.ensure_day(NOW, "p", "晴")
            picked.append(nodes[2]["summary"])  # 第一个 work 节点的摘要区分两份模板
        self.assertEqual(len(set(picked)), 2, f"随机选择应能命中不同归档: {picked}")

    def test_19_config_validation(self) -> None:
        from pydantic import ValidationError
        config = MaiLifeSuiteSettings()
        self.assertEqual(config.schedule.generation_retry_interval_minutes, 10)
        self.assertEqual(config.schedule.generation_max_retries, 3)
        self.assertEqual(config.schedule.generation_template_retention_days, 30)
        with self.assertRaises(ValidationError):
            config.schedule.generation_retry_interval_minutes = 0
        with self.assertRaises(ValidationError):
            config.schedule.generation_max_retries = -1
        with self.assertRaises(ValidationError):
            config.schedule.generation_template_retention_days = 0
        with self.assertRaises(ValidationError):
            config.schedule.generation_retry_interval_minutes = "abc"

    async def test_20_exhausted_with_real_schedule_keeps_it(self) -> None:
        """达上限但当天已有真实日程（has_temp=0）→ 保留且不替换。"""
        llm = FakeLLM()
        llm.scripts = ["ok"]
        service = make_service(self.store, llm)
        first = await service.ensure_day(NOW, "p", "晴")
        await self.store.set_generation_state(DAY, status="retrying", attempts=3,
                                              next_retry_at=NOW.timestamp() + 60,
                                              has_temp=0)
        llm.scripts = ["sentinel"]
        again = await service.ensure_day(
            datetime.fromtimestamp(NOW.timestamp() + 120, ZoneInfo("Asia/Shanghai")), "p", "晴")
        self.assertEqual([n["id"] for n in again], [n["id"] for n in first])
        state = await self.store.get_generation_state(DAY)
        self.assertEqual(state["status"], "exhausted")
        self.assertEqual(state["fallback_source"], "")  # 未被归档兜底替换


if __name__ == "__main__":
    from unittest import main
    main()
