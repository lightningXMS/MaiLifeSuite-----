"""睡前流程：入睡推迟点维护与被叫醒后的重新入睡。

本模块只维护“何时真正入睡”这一件事，不主动发言——睡前晚安由
Replyer 在自然回复里说出（见 prompt_builder 的 approaching 注入）。
两条规则：
1. 夜窗内有对话时，入睡点推迟到“最后一条私聊 + BEDTIME_SILENCE_MINUTES”，
   每条新消息都重新推（v1.14.6 需求④）；
2. 被叫醒后宽限（awake_grace_minutes）到期且仍在休息窗内，相位重新入睡。

睡前氛围窗口与休息回复闸门的夜间窗严格一致：开始=night_start、结束=night_end，
不额外提前（v1.14.8：用户可改 night_start，流程必须跟着变）。
v1.23.5 闸门生效方式=按日程判断时，本模块全部改跟日程：入睡协商与睡前氛围
跟随当前睡眠段（段开始时刻即协商下限），重新入睡看当前段型是否睡眠/午休。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# 入睡需要“最后一条私聊后静默”的分钟数。
BEDTIME_SILENCE_MINUTES = 10
# 睡前流程检查循环粒度（秒）。
BEDTIME_LOOP_SECONDS = 60

_SLEEP_PHASES = {"falling_asleep", "light_sleep", "deep_sleep"}


def _in_window(start: str, end: str, current: str) -> bool:
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


class BedtimeManager:
    def __init__(self, ctx: Any, store: Any, config: Any, environment: Any,
                 state_engine: Any, logger: Any) -> None:
        self.ctx = ctx; self.store = store; self.config = config
        self.environment = environment; self.state_engine = state_engine; self.logger = logger
        # 按日程判断模式下缓存当前日程段（tick 每 60s 刷新，供同步的 approaching 读取）。
        self._segment_cache: dict[str, Any] = {}

    def update_config(self, config: Any) -> None:
        """WebUI 保存配置后必须换到新实例，否则晚安/推迟一直用旧的闸门时间。"""
        self.config = config

    def _night_start_ts(self, now: datetime, start: str, end: str) -> float:
        """当前/即将到来的这一晚夜窗开始时刻。

        跨午夜窗（如 23:21-08:00）的凌晨按前一晚计算；夜窗开始前取今晚即将开始的时刻。
        """
        hour, minute = (int(part) for part in str(start or "00:00").split(":")[:2])
        today = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo).timestamp()
        if today <= now.timestamp():
            return today
        if _in_window(start, end, now.strftime("%H:%M")):
            return today - 86400
        return today

    def _night_span(self, now: datetime, night_start: str, night_end: str) -> tuple[float, float]:
        """夜窗的起止时刻；与闸门夜窗一致，HH:MM 之外不做任何偏移。"""
        start = self._night_start_ts(now, night_start, night_end)
        hour, minute = (int(part) for part in str(night_end or "08:00").split(":")[:2])
        end = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo).timestamp()
        if end <= start:
            end += 86400
        return start, end

    def _schedule_mode(self) -> bool:
        """闸门生效方式是否为按日程判断（与 RestGate 共用同一开关）。"""
        cfg = self.config.rest_gate
        return str(getattr(cfg, "gate_mode", "time_window") or "time_window") == "schedule"

    async def _current_segment(self, now: datetime) -> dict[str, Any]:
        """当前时刻的日程段（今日框架按分钟定位）；无框架或空档返回空 dict。"""
        nodes = await self.store.get_framework(now.strftime("%Y-%m-%d"))
        minute = now.hour * 60 + now.minute
        for node in nodes:
            if int(node.get("start_minute") or 0) <= minute < int(node.get("end_minute") or 0):
                return node
        return {}

    @staticmethod
    def _segment_start_ts(now: datetime, segment: dict[str, Any]) -> float:
        start = int(segment.get("start_minute") or 0)
        return datetime(now.year, now.month, now.day, start // 60, start % 60, tzinfo=now.tzinfo).timestamp()

    def _sleep_period_floor(self, now: datetime) -> float:
        """当前是否处于入睡协商期及其起点；0 表示不在休息期。

        按日程判断：当前段是睡眠段时，起点=该段开始时刻（跟着日程作息走）。
        按设定时间：夜窗内起点=夜窗开始时刻（与闸门夜窗严格一致）。
        """
        cfg = self.config.rest_gate
        if self._schedule_mode():
            segment = self._segment_cache
            if str(segment.get("kind") or "") == "sleep":
                return self._segment_start_ts(now, segment)
            return 0.0
        if _in_window(cfg.night_start, cfg.night_end, now.strftime("%H:%M")):
            return self._night_start_ts(now, cfg.night_start, cfg.night_end)
        return 0.0

    def _still_resting(self, now: datetime) -> bool:
        """宽限到期时是否仍在休息段：按日程=当前段型睡眠/午休；按设定时间=仍在夜/午窗。"""
        cfg = self.config.rest_gate
        if self._schedule_mode():
            return str(self._segment_cache.get("kind") or "") in {"sleep", "nap"}
        current = now.strftime("%H:%M")
        return (_in_window(cfg.night_start, cfg.night_end, current)
                or _in_window(cfg.nap_start, cfg.nap_end, current))

    async def tick(self, now: datetime) -> None:
        if self._schedule_mode():
            self._segment_cache = await self._current_segment(now)
        runtime = await self.store.get_sleep_runtime()
        phase = str(runtime.get("phase") or "")
        await self._update_sleep_defer(now, runtime, phase)
        await self._check_resleep(now, runtime, phase)

    async def _update_sleep_defer(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        cfg = self.config.rest_gate
        defer = 0.0
        # 只在休息期内、她还没睡着也没被叫醒时协商入睡点；睡眠期/宽限期内一律为 0，
        # 防止被闸门拦下的消息（同样更新 last_user_message_at）把入睡点重新推开。
        # 休息期的判定与下限随闸门生效方式：按日程=当前睡眠段；按设定时间=夜窗。
        if cfg.enabled and phase not in _SLEEP_PHASES and phase != "woken":
            floor = self._sleep_period_floor(now)
            if floor:
                users = await self.store.list_users()
                # 麦麦是单一生命：任一用户还在聊，她就还没睡。
                latest = max((float(user.get("last_user_message_at") or 0) for user in users), default=0.0)
                defer = max(floor, latest + BEDTIME_SILENCE_MINUTES * 60)
        if float(runtime.get("sleep_defer_until", 0) or 0) != defer:
            runtime["sleep_defer_until"] = defer
            await self.store.save_sleep_runtime(runtime)

    async def _check_resleep(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        """宽限到期且仍在休息段内：从 woken 重新入睡（与宽限共用 awake_grace_minutes）。"""
        if phase != "woken":
            return
        if float(runtime.get("awake_grace_until", 0) or 0) > now.timestamp():
            return
        if not self._still_resting(now):
            return  # 休息期外交给日程推进自然转 awake，不强制
        await self.state_engine.mark_resleep(now)

    def approaching(self, now: datetime) -> bool:
        """是否处于睡前氛围期（Replyer 注入收尾/晚安提示）。

        按设定时间=整个夜窗内；按日程判断=当前日程段为睡眠段时
        （段缓存来自最近一次 tick，最多 60 秒滞后，对氛围提示足够）。
        """
        cfg = self.config.rest_gate
        if not cfg.enabled:
            return False
        if self._schedule_mode():
            return str(self._segment_cache.get("kind") or "") == "sleep"
        start, end = self._night_span(now, cfg.night_start, cfg.night_end)
        return start <= now.timestamp() < end
