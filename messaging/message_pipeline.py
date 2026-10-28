"""私聊与群聊相互隔离的入站消息收口和媒介识别。"""
from __future__ import annotations

import asyncio
import base64
import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .adapter_compat import component_kind, component_text, walk_components


_URGENT_RE=re.compile(r"急事|紧急|救命|出事了|危险|报警|叫醒|醒醒|快醒|撑不住|自杀|轻生",re.I)
_QUIET_RE=re.compile(r"别回|不要回|不用回|继续睡|别醒|别打扰|不用理我",re.I)
_MEDIA_PLACEHOLDER_RE=re.compile(
    r"\[(?:image|图片|emoji|表情包|表情|voice|语音|video|视频|file|文件|reply|forward|unsupported)\]",
    re.I,
)
_PLACEHOLDER_MEDIA={
    "image":re.compile(r"\[(?:image|图片)\]",re.I),
    "emoji":re.compile(r"\[emoji\]",re.I),
    "voice":re.compile(r"\[(?:voice|语音)\]",re.I),
    "video":re.compile(r"\[(?:video|视频)\]",re.I),
    "file":re.compile(r"\[(?:file|文件)\]",re.I),
    "reply":re.compile(r"\[reply\]",re.I),
    "forward":re.compile(r"\[forward\]",re.I),
}


_walk_components=walk_components


def plain_text(message: dict[str,Any]) -> str:
    """提取用户真正输入的文字，忽略两套 QQ 适配器生成的媒介占位符。"""
    processed=message.get("processed_plain_text")
    if isinstance(processed,str) and processed.strip():
        cleaned=" ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",processed).split())
        if cleaned:return cleaned
    values=[]
    for item in _walk_components(message.get("raw_message") or []):
        if component_kind(item)=="text":values.append(component_text(item))
    return " ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",value).strip() for value in values
                    if _MEDIA_PLACEHOLDER_RE.sub(" ",value).strip()).strip()


def direct_text(message:dict[str,Any])->str:
    """只读取顶层用户文字，控制逻辑不得把合并转发里的原话当成用户指令。"""
    values=[]
    raw=message.get("raw_message")
    if isinstance(raw,list):
        for item in raw:
            if isinstance(item,dict) and component_kind(item)=="text":values.append(component_text(item))
    text=" ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",value).strip() for value in values
                  if _MEDIA_PLACEHOLDER_RE.sub(" ",value).strip()).strip()
    if text or isinstance(raw,list):return text
    processed=message.get("processed_plain_text")
    return " ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",str(processed or "")).split())


def media_types(message: dict[str,Any]) -> list[str]:
    found=[]
    display_parts=[str(message.get("processed_plain_text") or "")]
    for item in _walk_components(message.get("raw_message") or []):
        kind=component_kind(item)
        if kind in {"image","voice","video","reply","forward","file","emoji"} and kind not in found:found.append(kind)
        if kind=="text":display_parts.append(component_text(item))
        if kind in ("image","emoji"):
            fmt=str(item.get("format") or item.get("image_format") or "").lower()
            data=str(item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or "")
            if fmt=="gif" or data.startswith("R0lGOD"):
                if "gif" not in found:found.append("gif")
    display=" ".join(display_parts)
    for kind,pattern in _PLACEHOLDER_MEDIA.items():
        if kind not in found and pattern.search(display):found.append(kind)
    if plain_text(message) and "text" not in found:found.insert(0,"text")
    return found


def classify_intent(text: str, media: list[str]) -> str:
    compact=" ".join(str(text or "").split())
    if _URGENT_RE.search(compact):return "安全或紧急需要"
    recall_compact="".join(compact.lower().split())
    if "撤回" in recall_compact and any(term in recall_compact for term in ("什么","啥","内容","看见","看到","记得","刚才","刚刚")):
        return "询问本人撤回内容"
    if "image" in media or "gif" in media:
        if re.search(r"这(?:张|个)|图里|图片|看得出|是什么|什么意思",compact):return "询问当前图片"
        return "分享图片"
    if re.search(r"怎么办|难受|烦|害怕|焦虑|伤心|撑不住",compact):return "情绪表达或寻求支持"
    if re.search(r"请|帮我|能不能|可以.*吗|麻烦",compact):return "提出请求"
    if "?" in compact or "？" in compact or re.search(r"^(为什么|怎么|什么|谁|哪里|多少|是不是)",compact):return "提出问题"
    return "分享近况或继续话题"


def message_identity(message: dict[str,Any]) -> tuple[str,str,str,bool]:
    info=message.get("message_info") if isinstance(message.get("message_info"),dict) else {}
    user_info=info.get("user_info") if isinstance(info.get("user_info"),dict) else {}
    group_info=info.get("group_info") if isinstance(info.get("group_info"),dict) else {}
    return (
        str(user_info.get("user_id") or ""),str(message.get("session_id") or ""),
        str(message.get("message_id") or ""),not bool(group_info.get("group_id")),
    )


def is_command(message: dict[str,Any]) -> bool:
    return bool(message.get("is_command")) or direct_text(message).lstrip().startswith("/")


def media_bytes(message: dict[str,Any]) -> int:
    total=0
    for item in _walk_components(message.get("raw_message") or []):
        raw=item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or item.get("image_base64")
        if isinstance(raw,str):
            if "," in raw and raw.startswith("data:"):raw=raw.split(",",1)[1]
            total+=len(raw)*3//4
    return total


@dataclass
class _Burst:
    """单个防抖窗口的收集状态。"""

    started: float
    generation: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    media_hints: list[list[str] | None] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    timer_task: asyncio.Task | None = field(default=None)


class MessageDebouncer:
    """按会话窗口收集连续消息，静默到期后合并为一条再放行。

    参考消息防抖合并插件（Message_Debouncing_Refactored）的“定时器 + 结算事件”模型：
    每条新消息都会重置防抖定时器，窗口到期后由最新调用合并整窗消息并继续主链；
    同时保留 Mai_life 原有能力：私聊/群聊独立开关、媒体感知静默窗、紧急/安静
    立即结算、数量与媒体上限、撤回移除。
    """

    def __init__(self, config: Any, logger: Any) -> None:
        self.config = config; self.logger = logger
        self._lock = asyncio.Lock(); self._bursts: dict[str, _Burst] = {}; self._closed = False

    def update_config(self, config: Any) -> None:
        self.config = config

    async def close(self) -> None:
        """关闭防抖器：取消所有定时器并唤醒等待方立即结算。"""
        async with self._lock:
            self._closed = True
            bursts = list(self._bursts.values())
        for burst in bursts:
            if burst.timer_task:
                burst.timer_task.cancel()
                burst.timer_task = None
            burst.event.set()

    @staticmethod
    def _burst_key(message: dict[str, Any]) -> tuple[str, bool]:
        uid, session, _mid, private = message_identity(message)
        if private:
            return f"private:{session}", True
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        group_id = str(group.get("group_id") or "")
        platform = str(message.get("platform") or "qq")
        # Focus 可能让多个聊天流共享 session；物理群号和发送者 QQ 才是隔离边界。
        return f"group:{platform}:{group_id or session}:{uid}", False

    def _quiet_wait(self, messages: list[dict[str, Any]], private: bool = True, *,
                    media_hints: list[list[str] | None] | None = None) -> float:
        # 入口已计算过媒体类型时直接复用提示，避免对同一条消息重复遍历组件。
        hints = media_hints or []
        types: set[str] = set()
        for index, msg in enumerate(messages):
            hint = hints[index] if index < len(hints) else None
            types.update(hint if hint is not None else media_types(msg))
        cfg = self.config.debounce
        if private:
            text_wait = float(cfg.text_wait_seconds); image_wait = float(cfg.image_wait_seconds)
            forward_wait = float(cfg.forward_wait_seconds)
        else:
            text_wait = float(cfg.group_text_wait_seconds); image_wait = float(cfg.group_image_wait_seconds)
            forward_wait = float(cfg.group_forward_wait_seconds)
        if "forward" in types:
            return forward_wait
        image_count = sum(1 for msg in messages for item in _walk_components(msg.get("raw_message") or []) if component_kind(item) == "image")
        if image_count == 1 and not any(plain_text(msg) for msg in messages):
            return image_wait
        return text_wait

    @staticmethod
    def _has_body(message: dict[str, Any]) -> bool:
        """检查消息是否包含文本或主体组件（图片/表情/语音/视频/文件/转发）。"""
        if plain_text(message):
            return True
        body_types = {"text", "image", "emoji", "voice", "video", "file", "forward"}
        return any(isinstance(part, dict) and component_kind(part) in body_types
                   for part in _walk_components(message.get("raw_message") or []))

    def _schedule_timer_locked(self, burst_key: str, burst: _Burst, private: bool) -> None:
        """重置防抖定时器（需持有锁时调用）：静默窗与最长等待取较小值。"""
        timer = burst.timer_task
        if timer:
            timer.cancel()
        quiet = self._quiet_wait(burst.messages, private, media_hints=burst.media_hints)
        cfg = self.config.debounce
        max_wait = float(cfg.max_wait_seconds if private else cfg.group_max_wait_seconds)
        delay = min(quiet, max_wait - (time.monotonic() - burst.started))
        burst.timer_task = asyncio.create_task(self._flush_later(burst_key, max(0.0, delay)))

    async def _flush_later(self, burst_key: str, delay: float) -> None:
        """防抖窗口到期后触发结算事件（参考插件定时器模型）。"""
        try:
            await asyncio.sleep(max(0.0, delay))
            async with self._lock:
                burst = self._bursts.get(burst_key)
                if burst is not None:
                    burst.event.set()
        except asyncio.CancelledError:
            return

    def _is_owner_locked(self, burst_key: str, burst: _Burst, generation: int) -> bool:
        """当前调用是否仍拥有该窗口（需持有锁时调用）。"""
        current = self._bursts.get(burst_key)
        return current is burst and burst.generation == generation

    def _finish_locked(self, burst_key: str, burst: _Burst) -> None:
        """结算窗口：从状态表移除并取消定时器（需持有锁时调用）。"""
        self._bursts.pop(burst_key, None)
        if burst.timer_task:
            burst.timer_task.cancel()
            burst.timer_task = None

    def _merge(self, messages: list[dict[str, Any]], separator: str = "\n") -> dict[str, Any]:
        """参考插件合并逻辑：以最新消息为基底，按分隔符拼接组件与文本。"""
        latest = copy.deepcopy(messages[-1])
        combined: list[dict[str, Any]] = []; texts: list[str] = []; ids: list[str] = []
        for index, message in enumerate(messages):
            components = copy.deepcopy(message.get("raw_message") or [])
            if not isinstance(components, list):
                # 个别适配器可能给出非列表 raw_message；丢弃组件但保留文本，避免合并炸断。
                self.logger.debug("[MaiLife] 合并跳过非列表 raw_message message=%s type=%s",
                                  str(message.get("message_id") or ""), type(components).__name__)
                components = []
            if index and combined and separator:
                combined.append({"type": "text", "data": separator})
            combined.extend(components)
            text = plain_text(message)
            if text:
                texts.append(text)
            mid = str(message.get("message_id") or "")
            if mid:
                ids.append(mid)
        latest["raw_message"] = combined
        latest["processed_plain_text"] = separator.join(texts)
        latest["is_emoji"] = all(self._is_emoji_only(message.get("raw_message") or []) for message in messages)
        latest["is_picture"] = any(self._has_component(message.get("raw_message") or [], "image") for message in messages)
        # 任一段是命令（含 / 开头）则合并轮按命令处理：连发拆分命令的第二段不会被当聊天。
        latest["is_command"] = any(
            bool(message.get("is_command")) or direct_text(message).lstrip().startswith("/")
            for message in messages
        )
        info = latest.setdefault("message_info", {})
        additional = info.setdefault("additional_config", {}) if isinstance(info, dict) else {}
        if isinstance(additional, dict):
            additional["mai_life_merged_message_ids"] = ids
        return latest

    @staticmethod
    def _is_emoji_only(raw_message: list[dict[str, Any]]) -> bool:
        """检查消息是否仅由表情组成（参考插件合并标记）。"""
        return bool(raw_message) and all(isinstance(part, dict) and component_kind(part) == "emoji" for part in raw_message)

    @staticmethod
    def _has_component(raw_message: list[dict[str, Any]], part_type: str) -> bool:
        """检查消息是否包含指定类型的组件。"""
        return any(isinstance(part, dict) and component_kind(part) == part_type for part in raw_message)

    @staticmethod
    def _preview(text: object, limit: int = 80) -> str:
        """截断文本用于日志预览。"""
        value = " ".join(str(text or "").split())
        return value if len(value) <= limit else value[:limit] + "..."

    @staticmethod
    def _short_key(key: str) -> str:
        """截断会话键用于日志显示。"""
        return key if len(key) <= 24 else key[:10] + "..." + key[-10:]

    async def collect(self, message: dict[str, Any], *, media_hint: list[str] | None = None) -> tuple[bool, dict[str, Any], str]:
        """按会话收集补话并返回最终一轮；每条新消息都会重置防抖定时器。

        media_hint 是入口已计算好的媒体类型，供静默窗复用，避免重复遍历消息组件。
        """
        cfg = self.config.debounce
        _uid, session, _mid, private = message_identity(message)
        if private and not cfg.enabled:
            return True, message, "disabled"
        if not private and not cfg.group_enabled:
            return True, message, "group_disabled"
        if not session:
            return True, message, "missing_session"
        # 参考插件：无文本且无主体内容的消息不参与防抖，直接放行。
        if bool(cfg.ignore_empty_message) and not self._has_body(message):
            return True, message, "empty_ignored"
        burst_key, _private_check = self._burst_key(message)
        separator = str(cfg.merge_separator)
        now = time.monotonic()
        async with self._lock:
            if self._closed:
                return True, message, "closing"
            burst = self._bursts.get(burst_key)
            if burst is None:
                burst = _Burst(started=now); self._bursts[burst_key] = burst
            else:
                # 新消息加入窗口：唤醒旧等待方，防抖窗口重新计时。
                burst.event.set(); burst.event = asyncio.Event()
            # 深拷贝避免后续 Hook 修改 Host 入参；generation 是并发调用唯一的所有权凭据。
            burst.messages.append(copy.deepcopy(message))
            burst.media_hints.append(list(media_hint) if media_hint else None)
            burst.generation += 1
            generation = burst.generation
            over_limit = len(burst.messages) >= int(cfg.max_messages) or sum(media_bytes(item) for item in burst.messages) > int(cfg.max_media_bytes)
            text = direct_text(message)
            # 强制唤醒词与紧急/安静词同级别立即结算：单独立即放行，不与后续消息合并。
            force_terms = [str(term).strip() for term in
                           (getattr(self.config.rest_gate, "force_wake_terms", None) or []) if str(term).strip()]
            immediate = bool(_URGENT_RE.search(text) or _QUIET_RE.search(text) or over_limit
                             or any(term in text for term in force_terms))
            if not immediate:
                # 参考插件：每条新消息重置防抖定时器，总等待不超过首次消息起的 max_wait。
                self._schedule_timer_locked(burst_key, burst, private)
            if bool(cfg.log_detail):
                action = "消息防抖开始" if len(burst.messages) == 1 else "消息防抖追加"
                self.logger.info("[MaiLife] %s key=%s count=%d text=%s", action,
                                 self._short_key(burst_key), len(burst.messages), self._preview(text))
        if immediate:
            # 紧急/安静/超限消息立即结算，不等防抖窗口。
            async with self._lock:
                if not self._is_owner_locked(burst_key, burst, generation):
                    return False, message, "superseded"
                self._finish_locked(burst_key, burst)
            merged = self._merge(burst.messages, separator)
            return True, merged, f"merged:{len(burst.messages)}"
        # 等待防抖窗口结算；新消息/撤回会替换事件，需按事件身份重新等待。
        while True:
            async with self._lock:
                if not self._is_owner_locked(burst_key, burst, generation):
                    return False, message, "superseded"
                if self._closed:
                    self._finish_locked(burst_key, burst)
                    break
                event = burst.event
            await event.wait()
            async with self._lock:
                if not self._is_owner_locked(burst_key, burst, generation):
                    return False, message, "superseded"
                if self._closed:
                    self._finish_locked(burst_key, burst)
                    break
                if burst.event is not event:
                    # 事件已被替换（新消息或撤回）：回到等待，使用新的防抖窗口。
                    continue
                self._finish_locked(burst_key, burst)
                break
        merged = self._merge(burst.messages, separator)
        if bool(cfg.log_detail):
            self.logger.info("[MaiLife] 消息防抖结算 key=%s count=%d merged=%s",
                             self._short_key(burst_key), len(burst.messages),
                             self._preview(merged.get("processed_plain_text", "")))
        return True, merged, f"merged:{len(burst.messages)}"

    async def recall(self, session_id: str, message_id: str) -> dict[str, Any]:
        """从尚未收口的突发消息中移除撤回项，并唤醒当前最终调用重新计时。"""
        if not session_id or not message_id:
            return {}
        async with self._lock:
            for key, burst in list(self._bursts.items()):
                removed: dict[str, Any] = {}; retained: list[dict[str, Any]] = []
                retained_hints: list[list[str] | None] = []
                for index, message in enumerate(burst.messages):
                    same_session = str(message.get("session_id") or "") == session_id
                    if not removed and same_session and str(message.get("message_id") or "") == message_id:
                        removed = message
                    else:
                        retained.append(message)
                        if index < len(burst.media_hints):
                            retained_hints.append(burst.media_hints[index])
                if not removed:
                    continue
                old_event = burst.event; burst.event = asyncio.Event(); old_event.set()
                if retained:
                    # 不增加 generation：当前最新 Hook 可以携带剩余消息继续主链，并重新计时。
                    burst.messages = retained
                    burst.media_hints = retained_hints
                    self._schedule_timer_locked(key, burst, key.startswith("private:"))
                else:
                    self._finish_locked(key, burst)
                return removed
            return {}

    @property
    def active_bursts(self) -> int:
        return len(self._bursts)
