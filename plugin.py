"""MaiLifeSuite（麦麦生活套件）插件入口。

合并 Mai_life（麦生活）与 MaiTrace（麦时迹）为单一插件：
- 生命周期：先装配麦生活服务（MaiLifeMixin.on_load，含唯一 SQLite 存储与
  消息/生活/搜索/创作服务），再启动麦时迹运行时（日程桥接、生活引擎、
  注入、消息控制与空间例程），卸载时按相反顺序收口。
- 组件注册：来自两个上游的全部 Command/Tool/API/HomeCard，且每个 Hook
  点只注册一个入口，内部分发给麦生活 Mixin 与麦时迹子系统。
- 同名公开 API（get_life_state / get_current_scene / get_schedule /
  get_environment_snapshot / create_proactive_opportunity）使用合并实现：
  麦生活载荷为基底，麦时迹数据以附加字段或 ``trace`` 子块提供。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Tuple
import asyncio

from maibot_sdk import (
    API,
    Command,
    HomeCard,
    HookHandler,
    MaiBotPlugin,
    Tool,
)
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import MaiLifeSuiteSettings
from .mailife_mixin import MaiLifeMixin
from .maitrace.handlers.actions import (
    execute_read_feed,
    execute_recall_qzone_activity,
    execute_send_feed,
)
from .maitrace.handlers.apis import (
    get_feeds_list_api,
    publish_topic_api,
    send_feed_api,
)
from .maitrace.handlers.commands import dispatch_zn
from .maitrace.services.persistence import PluginPersistence
from .maitrace.services.schedule.mai_life import _KINDS
from .maitrace.utils import get_logger

if TYPE_CHECKING:
    from .maitrace.services.inject import InjectionEngine
    from .maitrace.services.life import LifeEngine
    from .maitrace.services.messaging import MessagingController
    from .maitrace.services.routine import RoutineRunner
    from .maitrace.services.schedule import ScheduleEngine

logger = get_logger(__name__)


def _tool_error(message: str) -> Dict[str, Any]:
    """Host 依据 success 判断工具结果，不能只填写旧格式的 type=error。"""
    return {"success": False, "type": "error", "content": message, "error": message}


class MaiLifeSuitePlugin(MaiLifeMixin, MaiBotPlugin):
    """麦麦生活套件：生活模拟、消息体验、联网见闻、书柜创作与 QQ 空间。"""

    config_model: ClassVar[type] = MaiLifeSuiteSettings

    # 麦时迹运行时类型注解（实际属性在 __init__ 中初始化）
    _routine: Optional["RoutineRunner"]
    _schedule_engine: Optional["ScheduleEngine"]
    _life_engine: Optional["LifeEngine"]
    _injection: Optional["InjectionEngine"]
    _messaging: Optional["MessagingController"]

    def __init__(self) -> None:
        super().__init__()
        # 麦时迹运行时槽位（属性名沿用上游 services/runtime.py 的约定，
        # 其中 _ready 改名 _trace_ready 以避开麦生活 Mixin 的 _ready property）。
        self._trace_ready = False
        self._trace_persistence: Optional[PluginPersistence] = None
        self._schedule_engine = None
        self._life_engine = None
        self._injection: Optional["InjectionEngine"] = None
        self._messaging = None
        self._routine = None
        self._runtime_task: Optional[asyncio.Task] = None
        # 合并 Hook 始终注册；麦时迹消息部分由 config.messaging.enabled 自门控。
        self._messaging_registered = True
        self._runtime_lock = asyncio.Lock()
        self._routine_state: Dict[str, Any] = {}
        self._registered_deadline_signature: Optional[Tuple[Any, ...]] = None

    # ===== 麦时迹基础属性（供 maitrace/handlers 与 services/runtime 使用） =====

    @property
    def plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent

    @property
    def persistence(self) -> PluginPersistence:
        if self._trace_persistence is None:
            self._trace_persistence = PluginPersistence(self.ctx.paths.data_dir)
        return self._trace_persistence

    @property
    def schedule_engine(self) -> Optional["ScheduleEngine"]:
        return self._schedule_engine

    @property
    def life_engine(self) -> Optional["LifeEngine"]:
        return self._life_engine

    async def _archive_pending_config(self) -> None:
        """合并插件没有麦时迹旧配置迁移需求，保留 no-op 以兼容 services/runtime 调用点。"""
        return None

    def get_components(self) -> List[Dict[str, Any]]:
        """先由 Mixin 应用麦生活的 chat_scope，再应用麦时迹的动态超时预算。"""
        components = super().get_components()
        from .maitrace.services.limits import feed_read_budget_seconds, publication_budget_seconds
        publish_seconds = publication_budget_seconds(self.config)
        read_seconds = feed_read_budget_seconds(self.config)
        schedule = self.config.schedule
        rounds = schedule.max_rounds if schedule.use_multi_round else 1
        # 角色裁判与生成前状态推断也会请求模型，每一轮都有自己的 RPC 预算。
        schedule_seconds = (rounds + 2) * (schedule.generation_timeout + 10) + 120
        for component in components:
            if component["name"] in ("zn", "send_feed", "publish_topic_api"):
                component["metadata"]["timeout_ms"] = int(max(publish_seconds, schedule_seconds) * 1000)
            elif component["name"] == "read_feed":
                component["metadata"]["timeout_ms"] = int(read_seconds * 1000)
            elif component["name"].removesuffix("_v6") in (
                "get_planning_status", "generate_schedule", "update_schedule",
            ):
                component["metadata"]["timeout_ms"] = int(schedule_seconds * 1000)
        self._registered_deadline_signature = self._deadline_signature()
        return components

    def _deadline_signature(self) -> Tuple[Any, ...]:
        return (
            self.config.image.image_number, self.config.image.image_retry,
            self.config.llm.llm_timeout_seconds,
            self.config.read.read_number,
            self.config.schedule.generation_timeout, self.config.schedule.use_multi_round,
            self.config.schedule.max_rounds,
        )

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """先装配麦生活（存储与服务），再启动麦时迹运行时（日程桥读取麦生活框架）。"""
        await super().on_load()
        if not self.config.plugin.enabled:
            self.ctx.logger.info("MaiLifeSuite 已禁用（plugin.enabled=false），麦时迹运行时不启动")
            return
        from .maitrace.services.runtime import start_runtime
        async with self._runtime_lock:
            await start_runtime(self)
        self.ctx.logger.info(
            "MaiLifeSuite 已加载：麦生活 %s + 麦时迹 6.3（日程提供方: %s）",
            self._mailife_version(), self.config.schedule.provider,
        )

    async def on_unload(self) -> None:
        """先停麦时迹（消费者，含日程桥），再关闭麦生活存储。"""
        from .maitrace.services.runtime import stop_runtime
        async with self._runtime_lock:
            await stop_runtime(self)
        await super().on_unload()
        self.ctx.logger.info("MaiLifeSuite 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """麦生活热更新（scope 分发）之后，重建麦时迹运行时。"""
        await super().on_config_update(scope, config_data, version)
        from .maitrace.services.runtime import start_runtime, stop_runtime
        if scope == "self" and self._registered_deadline_signature != self._deadline_signature():
            self.ctx.logger.warning("请求预算配置已更新，请重载插件以同步 Host 的组件超时")
        async with self._runtime_lock:
            await stop_runtime(self)
            if self.config.plugin.enabled:
                await start_runtime(self)
        self.ctx.logger.info("MaiLifeSuite 配置更新: scope=%s version=%s", scope, version)

    def _mailife_version(self) -> str:
        from .mailife_config import PLUGIN_VERSION
        return PLUGIN_VERSION

    # ===== Command / Tool / API：麦时迹组件（逻辑分发到 maitrace/handlers） =====

    @Command(
        "zn",
        description="麦时迹统一命令：/zn help 查看用法",
        pattern=r"^\s*/zn(?:\s+(?P<sub>.+))?\s*$",
    )
    async def handle_zn(self, **kwargs: Any) -> tuple:
        return await dispatch_zn(self, **kwargs)

    @Tool(
        "send_feed",
        description="发一条相应主题的说说到 QQ 空间，自动配图并返回已发布的正文。",
        detailed_description=(
            "适用场景：用户要求发说说、有人希望你更新 QQ 空间、或你自行判断适合发说说时。"
            "典型触发词：说说 / 空间 / 动态。"
        ),
        parameters=[
            ToolParameterInfo(
                name="topic", param_type=ToolParamType.STRING,
                description="要发送的说说主题或完整内容", required=True,
            ),
            ToolParameterInfo(
                name="user_name", param_type=ToolParamType.STRING,
                description="要求你发说说的好友的 QQ 名称", required=False,
            ),
        ],
    )
    async def handle_send_feed(self, **kwargs: Any) -> Dict[str, Any]:
        return await execute_send_feed(self, **kwargs)

    @Tool(
        "read_feed",
        description="读取指定好友最近的 QQ 空间动态，返回动态内容并按配置评论点赞；enable_comment=false 时不发表评论。",
        detailed_description=(
            "适用场景：需要查看某人动态/说说/QQ 空间，或有人希望你评价某人的空间内容时。"
            "典型触发词：说说 / 空间 / 动态。"
            "若用户只要看不要评，传入 enable_comment=false；点赞仍按配置执行。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target_name", param_type=ToolParamType.STRING,
                description="需要阅读动态的好友的 QQ 名称", required=True,
            ),
            ToolParameterInfo(
                name="user_name", param_type=ToolParamType.STRING,
                description="要求你阅读动态的好友的 QQ 名称", required=False,
            ),
            ToolParameterInfo(
                name="enable_comment", param_type=ToolParamType.STRING,
                description="是否评论：true/false；false、否、不回复=不发表评论，点赞仍按配置执行。默认 true",
                required=False,
            ),
        ],
    )
    async def handle_read_feed(self, **kwargs: Any) -> Dict[str, Any]:
        return await execute_read_feed(self, **kwargs)

    @Tool(
        "recall_qzone_activity",
        description="查询自己在 QQ 空间发布、评论和回复过的内容。被问及空间活动时先查记录再回答。",
        parameters=[
            ToolParameterInfo(name="keyword", param_type=ToolParamType.STRING, description="可选的人名、QQ、动态ID或话题关键词", required=False),
            ToolParameterInfo(name="limit", param_type=ToolParamType.STRING, description="返回条数，默认10，范围1到50", required=False),
        ],
        visibility="visible",
    )
    async def handle_recall_qzone_activity(self, **kwargs: Any) -> Dict[str, Any]:
        return await execute_recall_qzone_activity(self, **kwargs)

    @Tool(
        "get_planning_status_v6",
        description="查看今日日程安排，按时间顺序显示正在进行和即将到来的活动",
        parameters=[
            ToolParameterInfo(
                name="detailed", param_type=ToolParamType.BOOLEAN,
                description="是否显示详细信息", required=False,
            ),
        ],
    )
    async def handle_get_planning_status(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run_schedule_tool('get_planning_status', kwargs)

    @Tool(
        "generate_schedule_v6",
        description="生成今日生活作息（默认由麦生活框架负责，已有日程时直接返回）。",
        parameters=[],
    )
    async def handle_generate_schedule(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run_schedule_tool("generate_schedule", kwargs)

    @Tool(
        "update_schedule_v6",
        description="按人设判断是否接受约定，插入今天空档或预约未来日期；内置日程模式下可用，麦生活提供日程时请使用麦生活的修改功能。",
        parameters=[
            ToolParameterInfo(
                name="description", param_type=ToolParamType.STRING,
                description="约定的日期、时间、事情及可选时长（如\"今天14:00聊半小时\"、\"下周一18:00一起吃饭\"），未来日期不限制天数",
                required=True,
            ),
        ],
    )
    async def handle_update_schedule(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._run_schedule_tool('update_schedule', kwargs)

    async def _run_schedule_tool(self, name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if self._schedule_engine is None or not self.config.schedule.enabled:
            return _tool_error("日程尚未就绪或已关闭")
        result = await self._schedule_engine.handle_tool(name, **kwargs)
        if result.get("type") == "error" or result.get("success") is False:
            return {**result, **_tool_error(str(result.get("error") or result.get("content", "日程工具执行失败")))}
        return result

    # ===== 公开 API：麦时迹独有（空间） =====

    @API(
        "send_feed_api",
        description="发送一条说说到 QQ 空间。参数：message(str, 必填)、images(list[str] base64, 可选)。",
        version="1",
        public=True,
        timeout_ms=180000,
    )
    async def handle_send_feed_api(
        self, message: str = "", images: Optional[list] = None, **kwargs: Any,
    ) -> dict:
        del kwargs
        return await send_feed_api(self, message=message, images=images)

    @API(
        "get_feeds_list_api",
        description="获取指定 QQ 的最近说说列表。参数：target_qq(str, 必填)、num(int, 默认5)。",
        version="1",
        public=True,
    )
    async def handle_get_feeds_list_api(
        self, target_qq: str = "", num: int = 5, filter: bool = False, **kwargs: Any,
    ) -> dict:
        del kwargs
        return await get_feeds_list_api(self, target_qq=target_qq, num=num, filter=filter)

    @API(
        "publish_topic_api",
        description=(
            "完整发说说流程：LLM 按主题生成正文 + 自动配图 + 发布。"
            "参数：topic(str, 必填，传 'custom' 走私聊取词模式)、current_activity(str, 可选)。"
            "返回 {result, story, message}。"
        ),
        version="1",
        public=True,
    )
    async def handle_publish_topic_api(
        self, topic: str = "", current_activity: str = "", activity_type: str = "", outfit: str = "", **kwargs: Any,
    ) -> dict:
        del kwargs
        return await publish_topic_api(
            self, topic=topic, current_activity=current_activity,
            activity_type=activity_type, outfit=outfit,
        )

    @API("get_current_activity", description="当前活动快照（麦生活框架），供绘卷等插件查询", public=True, timeout_ms=10000)
    async def handle_get_current_activity(self, chat_id: str = "global", **kwargs: Any) -> Dict[str, Any]:
        del chat_id, kwargs  # 日程始终是麦麦的一条全局时间线。
        if not self.config.schedule.enabled or self._schedule_engine is None:
            return {"has_activity": False, "activity": None, "next_activities": [],
                    "as_of": "", "timezone": self.config.schedule.timezone,
                    "error": "schedule_disabled" if not self.config.schedule.enabled else "plugin_not_initialized"}
        return self._schedule_engine.current_activity_payload()

    # ===== 公开 API：同名合并实现（麦生活基底 + 麦时迹附加数据） =====

    @API(
        "get_life_state",
        description="获取麦麦全局生活状态：麦生活字段为基底（energy/hunger/mood/sleep_phase 等），麦时迹完整快照在 trace 子块。",
        version="1", public=True, timeout_ms=10000,
    )
    async def handle_get_life_state(self, **kwargs: Any) -> Dict[str, Any]:
        payload = await self.api_get_state(**kwargs)
        payload = dict(payload) if isinstance(payload, dict) else {}
        if self._life_engine is not None:
            trace = self._life_engine.peek_state()
            payload.setdefault("enabled", trace.get("enabled"))
            payload["trace"] = trace
        return payload

    @API(
        "get_current_scene",
        description="获取当前细化生活场景：麦生活 context（nodes/current/next/scene）为基底，麦时迹场景字段与 trace 子块附加。",
        version="1", public=True, timeout_ms=10000,
    )
    async def handle_get_current_scene(self, **kwargs: Any) -> Dict[str, Any]:
        payload = await self.api_get_scene(**kwargs)
        payload = dict(payload) if isinstance(payload, dict) else {}
        if self._life_engine is not None:
            trace = self._life_engine.current_scene()
            for key in ("activity_key", "current_activity", "activity_kind", "current_location"):
                payload.setdefault(key, trace.get(key, ""))
            payload["trace"] = trace
        return payload

    @API(
        "get_schedule",
        description="获取指定日期日程：麦生活框架节点字段（start_minute/end_minute/kind/summary）+ ISO 时刻与麦时迹别名字段（start/end/name/goal_type/goal_id）。",
        version="1", public=True, timeout_ms=25000,
    )
    async def handle_get_schedule(self, day: str = "", **kwargs: Any) -> List[Dict[str, Any]]:
        del kwargs
        if not self._ready or not self._store:
            return []
        nodes = await self.api_get_schedule(day=day)
        if not isinstance(nodes, list):
            return []
        tz = self._schedule_tz()
        resolved_day = str(day or "").strip() or self._env.now().date().isoformat() if self._env else str(day or "")
        merged: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            item = dict(node)
            try:
                iso = self._schedule_activity(node, resolved_day, tz)
            except Exception:
                merged.append(item)
                continue
            item.update(iso)
            item.setdefault("name", iso.get("name"))
            item["start"] = iso.get("start_at")
            item["end"] = iso.get("end_at")
            item["goal_type"] = _KINDS.get(str(node.get("kind") or ""), str(node.get("kind") or ""))
            item["goal_id"] = iso.get("id")
            merged.append(item)
        return merged

    @API(
        "get_environment_snapshot",
        description="获取时间、历法与媒介环境快照：麦生活环境为基底，麦时迹本地历法快照在 trace 子块。",
        version="1", public=True, timeout_ms=10000,
    )
    async def handle_get_environment(
        self, platform: str = "qq", adapter: str = "unknown", chat_type: str = "private",
        media: Optional[list] = None, **kwargs: Any,
    ) -> Dict[str, Any]:
        payload = await self.api_get_environment(
            platform=platform, adapter=adapter, chat_type=chat_type, media=media, **kwargs,
        )
        payload = dict(payload) if isinstance(payload, dict) else {}
        if self._life_engine is not None:
            trace = self._life_engine.environment_snapshot()
            for key, value in trace.items():
                payload.setdefault(key, value)
            payload["trace"] = trace
        return payload

    @API(
        "create_proactive_opportunity",
        description="创建外部主动分享契机：同时路由到麦生活关系主动与麦时迹空间分享（各自受开关与额度约束）。返回 {success, error, results} 并保留麦时迹原字段。",
        version="1", public=True,
    )
    async def handle_create_opportunity(self, topic: str = "", motive: str = "", **kwargs: Any) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        try:
            results["mai_life"] = await self.api_create_opportunity(topic=topic, motive=motive, **kwargs)
        except Exception as exc:
            results["mai_life"] = {"success": False, "error": str(exc)}
        if self._life_engine is not None:
            try:
                results["trace"] = await self._life_engine.add_external_opportunity(
                    topic=topic, motive=motive,
                    privacy=str(kwargs.get("privacy", "private")),
                    expires_minutes=int(kwargs.get("expires_minutes", 120)),
                    source_key=str(kwargs.get("source_key", "")),
                )
            except (ValueError, TypeError) as exc:
                results["trace"] = {"success": False, "error": str(exc)}
        success = any(isinstance(item, dict) and item.get("success") is True for item in results.values())
        payload: Dict[str, Any] = {"success": success, "error": None if success else "所有主动引擎不可用或参数无效", "results": results}
        trace_result = results.get("trace")
        if isinstance(trace_result, dict):
            for key, value in trace_result.items():
                payload.setdefault(key, value)
        return payload

    # ===== Hook：统一注册，内部分发（麦生活 Mixin + 麦时迹消息子系统） =====

    @HookHandler("chat.receive.before_process", name="suite_receive",
                 mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=30000)
    async def handle_receive(self, **kwargs: Any) -> Dict[str, Any]:
        """麦生活管线（撤回/防抖/闸门）先行，麦时迹生活观察随后。"""
        result = await self.on_receive(**kwargs)
        if isinstance(result, dict) and result.get("action") == "block":
            return result
        if self._messaging is None or not self.config.messaging.enabled:
            return result or {"action": "continue"}
        trace_result = await self._messaging.on_receive(**kwargs)
        trace_result = trace_result or {"action": "continue"}
        if trace_result.get("action") == "block":
            return trace_result
        if "modified_kwargs" in trace_result:
            return trace_result
        return result or {"action": "continue"}

    @HookHandler("maisaka.planner.before_request", name="suite_planner",
                 mode=HookMode.BLOCKING, timeout_ms=30000)
    async def handle_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """麦生活注入与主动任务识别先行，麦时迹任务追踪与日程注入随后。"""
        result = await self.on_planner(**kwargs)
        if isinstance(result, dict) and result.get("action") == "block":
            return result
        if self._messaging is not None:
            self._messaging.track_planner_task(str(kwargs.get("session_id", "")), kwargs.get("items", []))
        trace_result: Dict[str, Any] = {"action": "continue"}
        if self._injection is not None:
            trace_result = self._injection.planner(kwargs) or {"action": "continue"}
        if trace_result.get("action") == "block":
            return trace_result
        if "modified_kwargs" in trace_result:
            return trace_result
        return result or {"action": "continue"}

    @HookHandler("maisaka.planner.after_response", name="suite_planner_after",
                 mode=HookMode.OBSERVE, timeout_ms=10000)
    async def handle_planner_after(self, **kwargs: Any) -> Dict[str, Any]:
        await self.on_planner_after(**kwargs)
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_request", name="suite_replyer",
                 mode=HookMode.BLOCKING, timeout_ms=30000)
    async def handle_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """麦生活背景注入先行，麦时迹锚点追踪与日程注入随后。"""
        result = await self.on_replyer(**kwargs)
        if isinstance(result, dict) and result.get("action") == "block":
            return result
        if self._messaging is not None:
            self._messaging.track_reply_anchor(**kwargs)
        trace_result: Dict[str, Any] = {"action": "continue"}
        if self._injection is not None:
            trace_result = self._injection.replyer(kwargs) or {"action": "continue"}
        if trace_result.get("action") == "block":
            return trace_result
        if "modified_kwargs" in trace_result:
            return trace_result
        return result or {"action": "continue"}

    @HookHandler("maisaka.replyer.after_response", name="suite_replyer_after",
                 mode=HookMode.BLOCKING, timeout_ms=30000)
    async def handle_replyer_after(self, **kwargs: Any) -> Dict[str, Any]:
        return await self.on_replyer_after(**kwargs)

    @HookHandler("send_service.before_send", name="suite_before_send",
                 mode=HookMode.BLOCKING, timeout_ms=30000)
    async def handle_before_send(self, **kwargs: Any) -> Dict[str, Any]:
        """麦生活发送前复核（可 abort）先行，麦时迹休息闸门与撤回随后。"""
        result = await self.on_send_before(**kwargs)
        if isinstance(result, dict) and result.get("action") in ("abort", "block"):
            return result
        if self._messaging is None or not self.config.messaging.enabled:
            return result or {"action": "continue"}
        trace_result = await self._messaging.on_send_before(**kwargs)
        trace_result = trace_result or {"action": "continue"}
        if trace_result.get("action") in ("abort", "block"):
            return trace_result
        if "modified_kwargs" in trace_result:
            return trace_result
        return result or {"action": "continue"}

    @HookHandler("send_service.after_send", name="suite_after_send",
                 mode=HookMode.OBSERVE, timeout_ms=10000)
    async def handle_after_send(self, **kwargs: Any) -> Dict[str, Any]:
        # 麦生活结算 + 麦时迹额度确认；热关闭期间仍收口已发出消息。
        await self.on_send_after(**kwargs)
        if self._messaging is not None:
            await self._messaging.on_send_after(**kwargs)
        return {"action": "continue"}

    # ===== WebUI 首页卡片 =====

    @HomeCard("mailife_suite_life", "麦麦生活套件", content=(
        "麦麦的生活、消息体验与 QQ 空间。\n\n"
        "`/麦麦` 查看麦生活命令，`/zn help` 查看麦时迹命令，`/麦麦状态` 查看运行状态。"
    ), icon="calendar", order=40)
    async def handle_home_card(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return {"title": "麦麦生活套件"}


def create_plugin() -> MaiBotPlugin:
    """工厂函数：Runner 通过此函数实例化插件。"""
    return MaiLifeSuitePlugin()
