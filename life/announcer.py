"""活动播报服务：把当前活动按模板推送到指定群聊/私聊目标。

设计要点（第二阶段第一版）：
- 文案为模板拼接（{activity}/{scene}/{time}），不调用模型；
- 数据源复用麦时迹日程桥（current_activity_payload），不新建活动状态系统；
- 去重键 = f"{day}:{goal_type}:{time_window}"（同日起止+类型，对框架重生成与
  场景细化免疫；场景文本变化不视为新活动），按 目标+活动键 持久化去重；
- 每群/每私聊用户独立每日限额（仅发送接口明确受理才计数）；
- 发送失败记 failed + 重试冷却，不标记去重成功，不中断其他目标；
- 目标解析只使用宿主已存在的真实会话，不创建新会话。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

# 睡眠类活动：_KINDS 映射后 sleep 与 nap 都得到 goal_type='sleep'（见 mai_life.py）。
_SLEEP_GOAL_TYPES = {"sleep"}

# 目标解析连续失败达到该次数后进入退避；退避持续的巡查周期数（内存态，重启即清零）。
_RESOLVE_FAIL_THRESHOLD = 3
_RESOLVE_BACKOFF_ROUNDS = 6


def build_activity_key(day: str, goal_type: str, time_window: str) -> str:
    """活动去重键：同日起止时段+类型，对框架重生成/场景细化/ID 变化免疫。"""
    return f"{day}:{goal_type}:{time_window}"


class AnnouncerService:
    def __init__(self, ctx: Any, store: Any, config: Any, environment: Any,
                 schedule_engine: Any, schedule_service: Any, logger: Any,
                 llm: Any = None, bot_name: str = "麦麦") -> None:
        self.ctx = ctx
        self.store = store
        self.config = config
        self.environment = environment
        self.schedule_engine = schedule_engine
        self.schedule_service = schedule_service
        self.logger = logger
        self.llm = llm
        self.bot_name = str(bot_name or "麦麦")
        # 解析诊断：目标 → 首选候选会话（scope/account），发送失败时留痕。
        self._stream_diag: dict[str, dict[str, Any]] = {}
        # 目标解析连续失败计数（内存态）：达到阈值后按巡查周期数退避，避免永久性故障反复调用。
        self._resolve_failures: dict[str, int] = {}
        self._resolve_skip_rounds: dict[str, int] = {}
        self._last_resolve_ok: bool = True
        # 发送成功时记忆当前适配器可路由的 scope：同一目标多候选时优先同 scope 会话
        # （生产实锤：私聊存在旧实例会话 scope="洛天依（maibot v0.4.21）"，与当前
        # 适配器绑定 scope="MaiBot Main" 不一致会导致路由回退 legacy 失败）。
        self._preferred_scope: str = ""

    def update_config(self, config: Any) -> None:
        self.config = config

    # ── 目标解析（只查宿主已有会话，不创建新会话） ──

    @classmethod
    def _iter_streams(cls, value: Any, depth: int = 0):
        """遍历 SDK 字典/列表/模型对象中的会话记录（对齐 command_reply 的兼容处理）。"""
        if depth > 5:
            return
        if hasattr(value, "model_dump"):
            try:
                value = value.model_dump()
            except Exception:
                return
        elif not isinstance(value, (dict, list, tuple, str, bytes)) and hasattr(value, "__dict__"):
            try:
                value = dict(vars(value))
            except Exception:
                return
        if isinstance(value, dict):
            if value.get("stream_id") or value.get("session_id"):
                yield value
            for nested in value.values():
                if isinstance(nested, (dict, list, tuple)) or hasattr(nested, "model_dump"):
                    yield from cls._iter_streams(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from cls._iter_streams(nested, depth + 1)

    @classmethod
    def _stream_id(cls, item: dict[str, Any]) -> str:
        return str(item.get("stream_id") or item.get("session_id") or "").strip()

    @classmethod
    def _target_of(cls, item: dict[str, Any], kind: str) -> str:
        direct = str(item.get(f"{kind}_id") or "").strip()
        info = item.get(f"{kind}_info") if isinstance(item.get(f"{kind}_info"), dict) else {}
        return direct or str(info.get(f"{kind}_id") or "").strip()

    async def _fetch_all_streams(self) -> list[dict[str, Any]]:
        """拉取宿主全部真实会话；get_all_streams 失败时回退分类枚举。"""
        chat = getattr(self.ctx, "chat", None)
        if chat is None:
            return []
        methods = [getattr(chat, "get_all_streams", None),
                   getattr(chat, "get_group_streams", None), getattr(chat, "get_private_streams", None)]
        collected: list[dict[str, Any]] = []
        for method in methods:
            if not callable(method):
                continue
            try:
                result = await method(platform="qq")
            except TypeError:
                try:
                    result = await method()
                except Exception as exc:
                    self.logger.debug(f"[MaiLife] 活动播报: 枚举会话失败: {exc}")
                    continue
            except Exception as exc:
                self.logger.debug(f"[MaiLife] 活动播报: 枚举会话失败: {exc}")
                continue
            for item in self._iter_streams(result):
                collected.append(item)
            if collected and method is methods[0]:
                break  # get_all_streams 已覆盖全部会话
        return collected

    def _candidate_rank(self, item: dict[str, Any], offset: int) -> tuple[int, int, int, int]:
        """候选会话排序键：优先当前适配器可路由的 scope（记忆值），其次 scope/账号非空，顺序稳定。"""
        scope = str(item.get("scope") or "").strip()
        preferred = self._preferred_scope
        return (0 if (preferred and scope == preferred) else 1,
                0 if scope else 1,
                0 if str(item.get("account_id") or "").strip() else 1,
                offset)

    async def resolve_targets(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        """枚举宿主全部真实会话，按目标归组为**候选列表**（排序后）。

        黑名单模式需要完整的同类型会话池（名单外的全部会话都要发送），
        因此这里解析全部群聊/私聊会话，而不是只解析名单内的目标；
        白名单模式由 _select_targets 再按名单过滤。
        同一目标存在多条历史会话（不同 scope）时全部保留，由发送侧逐个回退。
        """
        streams = await self._fetch_all_streams()
        self._last_resolve_ok = bool(streams)
        if not streams:
            self.logger.warning("[MaiLife] 活动播报: 未能枚举任何宿主会话，目标解析跳过")
        group_cands: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        private_cands: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for offset, item in enumerate(streams):
            gid = self._target_of(item, "group")
            if gid and self._stream_id(item):
                group_cands.setdefault(gid, []).append((offset, item))
            uid = self._target_of(item, "user")
            if uid and self._stream_id(item):
                private_cands.setdefault(uid, []).append((offset, item))
        group_map: dict[str, list[dict[str, Any]]] = {}
        private_map: dict[str, list[dict[str, Any]]] = {}
        self._stream_diag.clear()
        for tid, cands in group_cands.items():
            group_map[tid] = [item for _, item in cands]
            self._stream_diag[f"group:{tid}"] = cands[0][1]
        for tid, cands in private_cands.items():
            private_map[tid] = [item for _, item in cands]
            self._stream_diag[f"private:{tid}"] = cands[0][1]
        return group_map, private_map

    async def _resolve_targets_with_backoff(self, stats: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        """会话枚举连续失败时按巡查周期退避，避免平台未注册类持久故障每轮重试。

        退避只针对"枚举不出任何会话"这类链路级故障；个别目标无会话（no_stream）
        不计入退避，仍每轮正常尝试解析。
        """
        if self._resolve_failures.get("count", 0) >= _RESOLVE_FAIL_THRESHOLD:
            rounds = self._resolve_skip_rounds.get("count", 0)
            if rounds < _RESOLVE_BACKOFF_ROUNDS:
                self._resolve_skip_rounds["count"] = rounds + 1
                stats["skipped"].append(f"skipped_resolve_backoff:{rounds + 1}/{_RESOLVE_BACKOFF_ROUNDS}")
                return {}, {}
        group_map, private_map = await self.resolve_targets()
        if self._last_resolve_ok:
            self._resolve_failures.pop("count", None)
            self._resolve_skip_rounds.pop("count", None)
        else:
            failures = self._resolve_failures.get("count", 0) + 1
            self._resolve_failures["count"] = failures
            if failures >= _RESOLVE_FAIL_THRESHOLD:
                self.logger.error(f"[MaiLife] 活动播报: 会话枚举连续失败 {failures} 次，"
                                  f"进入 {_RESOLVE_BACKOFF_ROUNDS} 个巡查周期退避")
        return group_map, private_map

    # ── 模板渲染 ──

    def _render(self, activity_name: str, scene: str, now: datetime) -> str:
        cfg = self.config.announce
        scene_text = str(scene or "").strip()
        try:
            return cfg.template.format(
                activity=activity_name, scene=scene_text, time=now.strftime("%H:%M"),
            )
        except Exception as exc:
            raise ValueError(f"播报模板渲染失败: {exc}") from exc

    # ── 主巡查 ──

    async def tick(self, now: datetime) -> dict[str, Any]:
        """一轮活动播报巡查；返回统计供日志与测试断言。"""
        cfg = self.config.announce
        stats: dict[str, Any] = {"sent": 0, "skipped": [], "failed": 0}
        if not cfg.enabled:
            stats["skipped"].append("disabled")
            return stats
        # 黑名单模式下空名单也有效（发给全部已注册会话），不能在此早退；
        # 白名单模式空名单的提示由 _select_targets 统一给出。
        if self.schedule_engine is None:
            stats["skipped"].append("schedule_not_ready")
            return stats

        payload = self.schedule_engine.current_activity_payload()
        if payload.get("error") and not payload.get("has_activity"):
            stats["skipped"].append(f"schedule_error:{payload.get('error')}")
            return stats
        activity = payload.get("activity") if payload.get("has_activity") else None
        if not isinstance(activity, dict) or not str(activity.get("name") or "").strip():
            stats["skipped"].append("no_current_activity")
            return stats

        goal_type = str(activity.get("goal_type") or "")
        if cfg.skip_sleep and goal_type in _SLEEP_GOAL_TYPES:
            stats["skipped"].append("sleep_activity")
            return stats

        as_of = str(payload.get("as_of") or "")
        day = as_of[:10] if len(as_of) >= 10 else now.strftime("%Y-%m-%d")
        time_window = str(activity.get("time_window") or "")
        if not time_window:
            # 活动数据不完整，无法可靠区分活动 → 安全跳过，避免连发。
            stats["skipped"].append("incomplete_activity_data")
            return stats
        activity_key = build_activity_key(day, goal_type, time_window)

        scene = ""
        if self.schedule_service is not None:
            try:
                context = await self.schedule_service.context(now)
                # context["scene"] 是细化场景 dict（含 scene 文本键）；缺失时回退当前节点摘要。
                scene_data = context.get("scene")
                scene = str(scene_data.get("scene") or "") if isinstance(scene_data, dict) else str(scene_data or "")
                if not scene:
                    current = context.get("current")
                    scene = str(current.get("summary") or "") if isinstance(current, dict) else ""
            except Exception as exc:
                self.logger.debug(f"[MaiLife] 活动播报: 场景读取失败（降级为空）: {exc}")

        group_map, private_map = await self._resolve_targets_with_backoff(stats)
        now_ts = now.timestamp()
        announced = 0

        for gid in self._select_targets("group", group_map, stats):
            candidates = group_map.get(gid) or []
            if not candidates:
                stats["skipped"].append(f"no_stream:group:{gid}")
                continue
            ok = await self._announce_one("group", gid, candidates, activity_key, day,
                                          now, now_ts, activity, scene, stats)
            announced += 1 if ok else 0

        for uid in self._select_targets("private", private_map, stats):
            candidates = private_map.get(uid) or []
            if not candidates:
                stats["skipped"].append(f"no_stream:private:{uid}")
                continue
            ok = await self._announce_one("private", uid, candidates, activity_key, day,
                                          now, now_ts, activity, scene, stats)
            announced += 1 if ok else 0

        stats["sent"] = announced
        return stats

    def _select_targets(self, kind: str, resolved: dict[str, list[dict[str, Any]]], stats: dict[str, Any]) -> list[str]:
        """按名单模式计算实际发送目标。

        名单校验在去重/限额/发送之前执行，自动巡查、重试与手动触发共用本方法。
        - whitelist：列表即允许目标；非法 ID 默认拒绝，不猜测类型或跨类型尝试。
        - blacklist：解析到的全部同类型真实会话 − 名单，命中名单的逐个留痕。
        """
        cfg = self.config.announce
        if kind == "group":
            channel_enabled, targets = cfg.group_enabled, list(cfg.group_targets or [])
        else:
            channel_enabled, targets = cfg.private_enabled, list(cfg.private_targets or [])
        if not channel_enabled:
            if targets:
                stats["skipped"].append(f"skipped_type_disabled:{kind}")
            return []
        if str(cfg.list_mode or "whitelist") == "blacklist":
            blocked = {str(item or "").strip() for item in targets}
            for tid in sorted(set(resolved) & blocked):
                stats["skipped"].append(f"skipped_in_blacklist:{kind}:{tid}")
            return [tid for tid in resolved if tid not in blocked]
        if not targets:
            stats["skipped"].append(f"skipped_target_not_configured:{kind}")
            return []
        result: list[str] = []
        for raw in targets:
            target_id = str(raw or "").strip()
            if not target_id.isdigit():
                stats["skipped"].append(f"skipped_invalid_target:{kind}:{target_id[:24]}")
                continue
            result.append(target_id)
        return result

    async def _announce_one(self, target_type: str, target_id: str, candidates: list[dict[str, Any]],
                            activity_key: str, day: str, now: datetime, now_ts: float,
                            activity: dict[str, Any], scene: str, stats: dict[str, Any]) -> bool:
        """对单一目标执行去重→限额→渲染→逐候选发送→结算；任何失败不影响其他目标。

        同一目标可能存在多条历史会话（不同 scope）：按排序逐个候选尝试发送，
        第一个被受理的会话即结算成功并记忆其 scope；全部失败才记失败。
        """
        cfg = self.config.announce
        daily_cap = cfg.daily_max_per_group if target_type == "group" else cfg.daily_max_per_private
        try:
            claimed, reason = await self.store.announce_try_claim(
                target_type, target_id, activity_key, day=day, now=now_ts,
                deduplicate=cfg.deduplicate, retry_interval_minutes=cfg.retry_interval_minutes,
            )
        except Exception as exc:
            # 存储异常时无法确认去重状态 → 安全降级为不发送，绝不无限发送。
            self.logger.error(f"[MaiLife] 活动播报: 去重存储异常 target={target_type}:{target_id}: {exc}")
            stats["skipped"].append(f"storage_error:{target_type}:{target_id}")
            return False
        if not claimed:
            stats["skipped"].append(f"{reason}:{target_type}:{target_id}")
            return False

        count = await self.store.announce_daily_count(day, target_type, target_id)
        if count >= daily_cap:
            await self.store.announce_finish(target_type, target_id, activity_key, day=day,
                                             now=now_ts, ok=False, error="daily_limit",
                                             retry_interval_minutes=cfg.retry_interval_minutes)
            stats["skipped"].append(f"daily_limit:{target_type}:{target_id}")
            return False

        activity_name = str(activity.get("name") or "日常活动").strip()
        try:
            text = self._render(activity_name, scene, now)
        except ValueError as exc:
            self.logger.warning(f"[MaiLife] 活动播报: 模板异常跳过 target={target_type}:{target_id}: {exc}")
            await self.store.announce_finish(target_type, target_id, activity_key, day=day,
                                             now=now_ts, ok=False, error=str(exc)[:200],
                                             retry_interval_minutes=cfg.retry_interval_minutes)
            stats["skipped"].append(f"template_error:{target_type}:{target_id}")
            return False
        # 可选 LLM 拟人渲染：失败/不可用自动回退模板文案，不阻断播报。
        text = await self._render_llm(text, activity_name, str(activity.get("goal_type") or ""), scene, now)

        last_error = ""
        tried = 0
        # 发送时排序：scope 记忆由本轮更早的成功发送实时更新，必须在发送前重排。
        ordered = [item for _, item in sorted(enumerate(candidates),
                                              key=lambda pair: self._candidate_rank(pair[1], pair[0]))]
        if ordered:
            self._stream_diag[f"{target_type}:{target_id}"] = ordered[0]
        for item in ordered:
            stream_id = self._stream_id(item)
            if not stream_id:
                continue
            tried += 1
            try:
                result = await self.ctx.send.text(text, stream_id)
            except Exception as exc:
                last_error = f"exception:{exc}"
                self.logger.debug(f"[MaiLife] 活动播报: 候选流发送异常 target={target_type}:{target_id} "
                                  f"stream={stream_id[:16]}: {exc}")
                continue
            if self._send_ok(result):
                await self.store.announce_finish(target_type, target_id, activity_key, day=day,
                                                 now=now_ts, ok=True,
                                                 retry_interval_minutes=cfg.retry_interval_minutes)
                if not cfg.deduplicate:
                    # 去重关闭时归档同键记录：重播边界由 activity_key 内含的日期提供。
                    try:
                        await self.store.announce_forget(target_type, target_id, activity_key)
                    except Exception as exc:
                        self.logger.warning(f"[MaiLife] 活动播报: 去重记录清理失败 target={target_type}:{target_id}: {exc}")
                scope = str(item.get("scope") or "").strip()
                if scope:
                    self._preferred_scope = scope  # 记忆当前适配器可路由的 scope，供后续候选排序
                self.logger.info(f"[MaiLife] 活动播报已发送: target={target_type}:{target_id} "
                                 f"activity={activity_name} key={activity_key} "
                                 f"stream={stream_id[:16]} scope={scope or '空'} "
                                 f"(候选 {tried}/{len(candidates)})")
                return True
            last_error = self._send_error(result)
            self.logger.debug(f"[MaiLife] 活动播报: 候选流发送失败 target={target_type}:{target_id} "
                              f"stream={stream_id[:16]} scope={item.get('scope') or '空'}: {last_error}，尝试下一候选")
        # 全部候选失败：记录最后一个错误与首选候选诊断，避免丢失底层原因。
        error = last_error or "无可用候选会话"
        await self.store.announce_finish(target_type, target_id, activity_key, day=day,
                                         now=now_ts, ok=False, error=error,
                                         retry_interval_minutes=cfg.retry_interval_minutes)
        stats["failed"] += 1
        self.logger.warning(f"[MaiLife] 活动播报发送失败 target={target_type}:{target_id}: {error} "
                            f"（尝试 {tried}/{len(candidates)} 个候选会话）{self._stream_note(target_type, target_id)}")
        return False

    def _stream_note(self, target_type: str, target_id: str) -> str:
        """发送失败时附带首选候选会话的诊断信息（scope/account），便于区分路由故障。"""
        item = self._stream_diag.get(f"{target_type}:{target_id}")
        if not item:
            return "[stream_unknown]"
        scope = str(item.get("scope") or "").strip() or "空"
        account = str(item.get("account_id") or "").strip() or "空"
        return f"[首选会话 scope={scope} account={account}]"

    @staticmethod
    def _send_ok(result: Any) -> bool:
        """send.text 返回 bool | SendResult：True 视为受理成功，False/失败键视为失败。"""
        if result is True:
            return True
        if isinstance(result, dict):
            return result.get("success") is True or result.get("success") is None and result.get("queued") is True
        return bool(result) if isinstance(result, bool) else False

    @staticmethod
    def _send_error(result: Any) -> str:
        if result is False:
            return "send.text 返回 False"
        if isinstance(result, dict):
            return str(result.get("error") or result.get("message") or "send.text 返回失败")
        return f"send.text 返回异常值: {result!r}"

    # ── 可选 LLM 拟人渲染 ──

    _GOAL_TYPE_ZH = {"sleep": "睡觉", "rest": "休息", "study": "学习",
                     "work": "工作", "relaxing": "休闲", "meal": "吃饭", "commuting": "出行"}

    async def _render_llm(self, fallback_text: str, activity_name: str,
                          goal_type: str, scene: str, now: datetime) -> str:
        """把模板稿交给 LLM 改写成更拟人的口语；不可用/失败/空结果一律回退模板稿。

        模型任务由 announce.llm_render_task 指定（replyer/planner/utils），必须
        是 Host 已注册的可用任务；失败后按 announce.llm_render_max_retries 额外
        重试，全部尝试失败才回退模板稿。每次渲染计入插件用量统计。
        """
        cfg = self.config.announce
        if not cfg.llm_render_enabled or self.llm is None:
            return fallback_text
        task = str(cfg.llm_render_task or "replyer")
        if task not in getattr(self.llm, "available_tasks", set()):
            # 任务不可用是持久性条件，不进入重试循环。
            self.logger.debug(f"[MaiLife] 活动播报: 渲染任务 {task} 不在 Host 可用任务中，使用模板文案")
            return fallback_text
        prompt = (f"当前活动：{activity_name}（{self._GOAL_TYPE_ZH.get(goal_type, goal_type or '日常')}）；"
                  f"时间：{now.strftime('%H:%M')}；所在场景：{scene or '未提供'}。参考稿：「{fallback_text}」\n"
                  "请把参考稿改写成一句更自然、更像真人随口说的话，直接告诉朋友们你要去做什么。"
                  "要求：不使用引号和表情符号；不编造参考稿里没有的细节；不超过 30 个字；只输出改写后的一句话。")
        system = f"你是{self.bot_name}，一个按真实日程生活的虚拟网友，说话简短自然、口语化。"
        attempts = 1 + max(0, int(cfg.llm_render_max_retries))
        cooldown = max(0, int(getattr(cfg, "llm_render_retry_interval_seconds", 0)))
        for attempt in range(1, attempts + 1):
            if attempt > 1 and cooldown > 0:
                # 重试冷却：避免瞬时故障时连续打满模型；冷却时长由配置提供（秒）。
                await asyncio.sleep(cooldown)
            try:
                rendered = await self.llm.generate(prompt, system, max_tokens=0, temperature=0.7,
                                                   task_kind="creative", request_type="announce_render",
                                                   task_name=task)
            except Exception as exc:
                rendered = ""
                self.logger.debug(f"[MaiLife] 活动播报: LLM 渲染异常（第 {attempt}/{attempts} 次）: {exc}")
            rendered = str(rendered or "").strip()
            rendered = rendered.splitlines()[0].strip() if rendered else ""
            rendered = rendered.strip("「」\"\"\"‘’'\"").strip()
            if rendered:
                self.logger.debug(f"[MaiLife] 活动播报: LLM 渲染成功 task={task}（第 {attempt}/{attempts} 次）-> {rendered[:40]}")
                return rendered[:120]
            if attempt < attempts:
                self.logger.debug(f"[MaiLife] 活动播报: LLM 渲染第 {attempt}/{attempts} 次失败或为空，准备重试")
        self.logger.warning(f"[MaiLife] 活动播报: LLM 渲染连续 {attempts} 次未成功，回退模板文案")
        return fallback_text
