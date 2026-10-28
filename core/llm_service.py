"""统一模型路由、结构化生成和插件 Token 统计。"""
from __future__ import annotations

import inspect
import json
import time
from typing import Any


class LLMService:
    """只接受 MaiBot 的任务路由名，不直接保存 Provider 凭据。"""

    def __init__(self, ctx: Any, config: Any, store: Any | None = None) -> None:
        self.ctx = ctx
        self.config = config
        self.store = store
        self.available_tasks: set[str] = set()
        self.health_error = "尚未检查"
        self._supports_task_name: bool | None = None
        # P7 JSON 解析失败计数（内存态，按提供方时区自然日分桶，重启清零）：
        # llm_usage_events 的 success 只记「模型有没有回话」，解析失败在那里不可见，
        # 而它是定位最易碎 schema 的关键信号。不落库避免 SCHEMA 迁移。
        self._json_failures: dict[str, int] = {}
        self._json_failures_day: str = ""

    def update_config(self, config: Any) -> None:
        self.config = config

    def _generate_call(self, task: str, messages: Any, temperature: float, max_tokens: int | None) -> dict[str, Any]:
        """构造 llm.generate 调用参数，兼容新旧 SDK 的任务路由契约。

        SDK 2.8.1 起签名含 task_name 且缺省无条件发送 ``utils``，导致 Host 把
        ``model`` 当具体模型名解析而报"未找到模型"；必须显式传 task_name。
        更旧的 SDK 没有该参数，``model`` 即任务名（Host 兼容分支仍支持）。
        max_tokens 为 None 时不下发该字段，由服务商决定（推理模型的思考会占用
        输出预算，写死小上限会把正文挤成空响应）。
        """
        if self._supports_task_name is None:
            try:
                params = inspect.signature(self.ctx.llm.generate).parameters
                self._supports_task_name = "task_name" in params
            except (TypeError, ValueError):
                self._supports_task_name = False
        call = {"prompt": messages, "temperature": temperature}
        if max_tokens is not None:
            call["max_tokens"] = max_tokens
        if self._supports_task_name:
            call.update({"model": "", "task_name": task})
        else:
            call["model"] = task
        return call

    def _output_limit(self, max_tokens: int) -> int | None:
        """解析输出上限：调用方传正数优先，否则用全局配置；0 或未配 = 不限制。"""
        if max_tokens and max_tokens > 0:
            return int(max_tokens)
        configured = int(getattr(self.config.models, "max_output_tokens", 0) or 0)
        return configured if configured > 0 else None

    def task_for(self, kind: str) -> str:
        """解析具体任务覆盖与基础角色回退，最终返回 MaiBot 的模型任务名。"""
        models = self.config.models
        routes = {
            "fast": models.fast_task,
            "reasoning": models.reasoning_task,
            "creative": models.creative_task,
            # 日程与场景细化是结构化 JSON 任务：默认走快模型。慢推理模型（如
            # step-5-preview）生成一次要 27-30 秒，会撞穿 Host 的 30 秒 cap.call
            # 超时；需要推理质量时用 schedule_task / scene_detail_task 显式覆盖。
            "schedule": models.schedule_task or models.fast_task,
            "scene_detail": models.scene_detail_task or models.fast_task,
            "rest_wakeup": models.rest_wakeup_task or models.fast_task,
            "continuity": models.continuity_task or models.fast_task,
            "dream": models.dream_task or models.creative_task,
            "diary": models.diary_task or models.creative_task,
            "date_analysis": models.date_analysis_task or models.fast_task,
            "news": models.news_task or models.fast_task,
            "search": models.search_task or models.reasoning_task,
            "relevance": models.relevance_task or models.reasoning_task,
            "group_judgment": models.group_judgment_task or models.fast_task,
            "relay_summary": models.relay_summary_task or models.fast_task,
            "creation_outline": models.creation_outline_task or models.reasoning_task,
            "creation_body": models.creation_body_task or models.creative_task,
            "creation_review": models.creation_review_task or models.reasoning_task,
            "reading_annotation": models.reading_annotation_task or models.creative_task,
        }
        return str(routes.get(kind) or models.reasoning_task or "planner").strip()

    def _active_task_kinds(self) -> set[str]:
        """只检查当前启用功能会实际调用的任务，fallback 不因此阻止插件启动。"""
        kinds={"schedule","scene_detail"}
        if self.config.context.enabled and self.config.context.continuity_enabled:kinds.add("continuity")
        if self.config.rest_gate.enabled and self.config.rest_gate.mode=="llm":kinds.add("rest_wakeup")
        if self.config.memory.enabled:
            kinds.add("dream")
            if self.config.memory.diary_enabled:kinds.add("diary")
            if self.config.memory.date_model_analysis_enabled:kinds.add("date_analysis")
        if self.config.information.enabled:
            if self.config.news.enabled:kinds.add("news")
            if self.config.search.enabled:kinds.add("search")
            if self.config.information.association_enabled:kinds.add("relevance")
        # relay_summary 已并入群观察的单次 group_judgment 调用（v1.23.6），不再单列必查。
        if self.config.social.enabled:kinds.add("group_judgment")
        if self.config.creation.enabled:
            kinds.update({"creation_outline","creation_body","creation_review"})
            if self.config.creation.external_reading_enabled and self.config.creation.reading_annotation_enabled:
                kinds.add("reading_annotation")
        return kinds

    async def refresh_health(self) -> set[str]:
        try:
            tasks = await self.ctx.llm.get_available_models()
            self.available_tasks = {str(item).strip() for item in tasks if str(item).strip()}
            if not self.available_tasks:
                self.health_error = "Host 未返回可用任务"
            else:
                missing=sorted({self.task_for(kind) for kind in self._active_task_kinds()}-self.available_tasks)
                self.health_error = "任务不可用: "+", ".join(missing) if missing else ""
        except Exception as exc:
            self.available_tasks = set()
            self.health_error = str(exc)[:200]
        return set(self.available_tasks)

    def task_available(self, kind: str) -> bool:
        task = self.task_for(kind)
        return bool(task and task in self.available_tasks)

    async def _record(self, *, result: dict[str, Any], task: str, request_type: str,
                      started: float, success: bool, error: str = "") -> None:
        if not self.store or not self.config.usage.enabled:
            return
        try:
            await self.store.record_llm_usage(
                created_at=time.time(), source="plugin", task_name=task,
                model_name=str(result.get("model") or result.get("model_name") or ""),
                request_type=request_type,
                prompt_tokens=int(result.get("prompt_tokens") or 0),
                completion_tokens=int(result.get("completion_tokens") or 0),
                total_tokens=int(result.get("total_tokens") or 0),
                latency_ms=(time.perf_counter()-started)*1000,
                success=success, error_summary=error,
            )
        except Exception as exc:
            self.ctx.logger.debug(f"[MaiLife] Token 统计写入失败: {exc}")

    async def generate(self, prompt: str | list[dict[str, Any]], system: str = "", max_tokens: int = 0,
                       temperature: float = 0.5, *, task_kind: str = "reasoning",
                       request_type: str = "generic", task_name: str | None = None) -> str:
        """统一调用 Host 模型并记录插件侧用量；失败时返回空串供业务规则降级。

        max_tokens 传 0 表示用全局配置（models.max_output_tokens，默认 0=不限制）。
        task_name 可显式指定宿主模型任务名（如 replyer/planner/utils），覆盖 task_kind 路由；
        显式任务名必须已在 Host 可用任务列表中，否则按失败降级返回空串。
        """
        if task_name:
            task = str(task_name).strip()
            if task not in self.available_tasks:
                self.ctx.logger.debug(f"[MaiLife] 显式模型任务不可用 task={task}，按失败降级")
                return ""
        else:
            task = self.task_for(task_kind)
        messages: str | list[dict[str, Any]] = prompt
        if system:
            if isinstance(prompt,str):
                messages=[{"role":"system","content":system},{"role":"user","content":prompt}]
            elif isinstance(prompt,list):
                # 列表消息已自带 system 时保留调用方意图；否则在列表前补一条 system，
                # 避免调用方按"消息列表"传参时 system 被静默丢弃。
                has_system=any(isinstance(item,dict) and str(item.get("role") or "").casefold()=="system"
                               for item in prompt)
                messages=prompt if has_system else [{"role":"system","content":system},*prompt]
        started=time.perf_counter(); result:dict[str,Any]={}
        try:
            result=await self.ctx.llm.generate(**self._generate_call(task,messages,temperature,
                                                                     self._output_limit(max_tokens)))
            success=bool(isinstance(result,dict) and result.get("success") and result.get("response"))
            failure_reason=str(result.get("error") or "empty_response") if isinstance(result,dict) else "invalid_result"
            if not success and isinstance(result,dict) and result.get("reasoning"):
                # 推理模型把输出预算烧在思考上时正文为空：只记长度不记正文，便于区分
                # "模型没答"与"答了但解析失败"。
                self.ctx.logger.warning(
                    f"[MaiLife] 模型只返回思考链 task={task} type={request_type} "
                    f"reasoning={len(str(result.get('reasoning')))} chars response=空")
            await self._record(result=result if isinstance(result,dict) else {},task=task,
                               request_type=request_type,started=started,success=success,
                               error="" if success else failure_reason)
            if success:return str(result["response"]).strip()
        except Exception as exc:
            await self._record(result=result,task=task,request_type=request_type,started=started,success=False,error=str(exc))
            self.ctx.logger.warning(f"[MaiLife] LLM 调用失败 task={task} type={request_type}: {exc}")
        return ""

    async def generate_json(self, prompt: str, system: str, fallback: Any, max_tokens: int = 0,
                            *, task_kind: str = "reasoning", request_type: str = "structured") -> Any:
        """从纯响应、Markdown 代码块或首个 JSON 边界中解析结构化结果。"""
        raw=await self.generate(prompt,system,max_tokens=max_tokens,task_kind=task_kind,request_type=request_type)
        if not raw:return fallback
        candidates=[raw]
        if "```" in raw:
            chunks=raw.split("```")
            candidates.extend(chunk.removeprefix("json").strip() for chunk in chunks[1::2])
        for left,right in (("[","]"),("{","}")):
            start,end=raw.find(left),raw.rfind(right)
            if start>=0 and end>start:candidates.append(raw[start:end+1])
        for text in candidates:
            try:return json.loads(text)
            except (json.JSONDecodeError,TypeError):continue
        # 模型可能回显私聊、群聊或外部资料，解析失败日志只记录元数据，不写正文。
        self.ctx.logger.warning(
            f"[MaiLife] 结构化响应解析失败 task={self.task_for(task_kind)} "
            f"type={request_type} chars={len(raw)}"
        )
        self._count_json_failure(task_kind)
        return fallback

    def _count_json_failure(self, task_kind: str) -> None:
        """解析失败按「日|任务名」内存计数；日切用配置时区（坑 12：不用 time.localtime）。"""
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            day = datetime.now(ZoneInfo(str(self.config.environment.timezone or "Asia/Shanghai"))).strftime("%Y-%m-%d")
        except Exception:
            day = time.strftime("%Y-%m-%d")
        if day != self._json_failures_day:
            self._json_failures_day = day; self._json_failures = {}
        task = self.task_for(task_kind)
        self._json_failures[task] = self._json_failures.get(task, 0) + 1

    def json_failure_snapshot(self) -> dict[str, Any]:
        """webui_usage 只读快照：当日解析失败按任务计数（无失败/跨日则空）。"""
        return {"day": self._json_failures_day,
                "failures": dict(sorted(self._json_failures.items(), key=lambda kv: -kv[1]))}

    async def record_observed(self, *, source: str, task_name: str, request_type: str,
                              model_name: str, prompt_tokens: int, completion_tokens: int,
                              total_tokens: int, success: bool = True) -> None:
        if not self.store or not self.config.usage.enabled:return
        try:
            await self.store.record_llm_usage(
                created_at=time.time(),source=source,task_name=task_name,model_name=model_name,
                request_type=request_type,prompt_tokens=prompt_tokens,completion_tokens=completion_tokens,
                total_tokens=total_tokens,latency_ms=0,success=success,error_summary="",
            )
        except Exception as exc:
            # 统计失败不能干扰 Planner/Replyer 主链。
            self.ctx.logger.debug(f"[MaiLife] Host Token 观察写入失败: {exc}")
