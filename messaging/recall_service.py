"""SnowLuma/NapCat 统一撤回取消与可选本人摘要。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .message_pipeline import direct_text,media_types,message_identity


@dataclass(slots=True)
class _CachedInbound:
    user_id:str
    private:bool
    summary:str
    media:list[str]
    expires_at:float


def is_recall_query(text:str)->bool:
    compact="".join(str(text or "").lower().split())
    if "撤回" not in compact:return False
    return any(term in compact for term in ("什么","啥","内容","看见","看到","记得","刚才","刚刚"))


class RecallService:
    """只保存取消所需 ID；正文摘要必须由用户显式开启。"""

    def __init__(self,ctx:Any,store:Any,config:Any,logger:Any)->None:
        self.ctx=ctx; self.store=store; self.config=config; self.logger=logger
        self._inbound:dict[tuple[str,str],_CachedInbound]={}

    def update_config(self,config:Any)->None:self.config=config

    def clear(self)->None:self._inbound.clear()

    def _retention_seconds(self)->int:
        return max(
            int(self.config.recall.summary_ttl_minutes)*60,
            int(self.config.debounce.turn_expire_seconds)+120,
            600,
        )

    def _prune(self,now:float)->None:
        expired=[key for key,value in self._inbound.items() if value.expires_at<=now]
        for key in expired:self._inbound.pop(key,None)

    @staticmethod
    def source_message_ids(message:dict[str,Any])->list[str]:
        info=message.get("message_info") if isinstance(message.get("message_info"),dict) else {}
        additional=info.get("additional_config") if isinstance(info.get("additional_config"),dict) else {}
        raw=additional.get("mai_life_merged_message_ids")
        result=[]
        if isinstance(raw,list):
            for value in raw:
                normalized=str(value or "").strip()
                if normalized and normalized not in result:result.append(normalized)
        message_id=str(message.get("message_id") or "").strip()
        if message_id and message_id not in result:result.append(message_id)
        return result

    def note_inbound(self,message:dict[str,Any])->None:
        """入站最早阶段只缓存有限文字；二进制永远不会进入撤回服务。"""
        if not self.config.recall.enabled or not self.config.recall.cache_summary_enabled:return
        user_id,session,message_id,private=message_identity(message)
        if not session or not message_id or not private:return
        configured={str(profile.user_id) for profile in self.config.users.profiles
                    if profile.enabled and str(profile.user_id).strip()}
        if user_id not in configured:return
        now=time.time(); self._prune(now)
        text=" ".join(direct_text(message).replace("\x00","").split())
        summary=text[:int(self.config.recall.summary_max_chars)]
        self._inbound[(session,message_id)]=_CachedInbound(
            user_id=user_id,private=private,summary=summary,media=media_types(message),
            expires_at=now+self._retention_seconds(),
        )

    async def register_turn(self,message:dict[str,Any],now:float|None=None)->None:
        if not self.config.recall.enabled:return
        user_id,session,turn_anchor,_private=message_identity(message)
        if not session or not turn_anchor:return
        current=now or time.time(); sources=self.source_message_ids(message)
        # 单条消息可由 recall_events 直接命中；只有合并轮次需要额外来源映射。
        if len(sources)==1 and sources[0]==turn_anchor:return
        await self.store.register_message_turn(
            session,turn_anchor,sources,user_id,current,current+self._retention_seconds(),
        )

    async def register_reply_anchor(self,session_id:str,reply_anchor:str,source_message_ids:list[str],
                                    user_id:str,now:float|None=None)->None:
        """把 Replyer 选择的引用目标关联到真实入站轮次，覆盖热重载后的发送检查。"""
        if not self.config.recall.enabled or not session_id or not reply_anchor:return
        normalized=[str(value) for value in source_message_ids if str(value).strip()]
        if len(normalized)==1 and normalized[0]==reply_anchor:return
        current=now or time.time()
        await self.store.register_message_turn(
            session_id,reply_anchor,normalized,user_id,current,current+self._retention_seconds(),
        )

    @staticmethod
    def _message_from_result(result:Any)->dict[str,Any]:
        if not isinstance(result,dict):return {}
        message=result.get("message")
        if isinstance(message,dict):return message
        if "message_id" in result and ("raw_message" in result or "processed_plain_text" in result):return result
        return {}

    async def _recover_summary(self,session_id:str,message_id:str)->tuple[str,list[str],str]:
        """优先读取内存短缓存，必要时限时向 Host 恢复无二进制的原消息。"""
        cached=self._inbound.get((session_id,message_id))
        if cached and cached.private:
            return cached.summary,list(cached.media),cached.user_id
        try:
            result=await asyncio.wait_for(
                self.ctx.message.get_by_id(message_id,stream_id=session_id,include_binary_data=False),
                timeout=3.0,
            )
            message=self._message_from_result(result)
            if not message:return "",[],""
            user_id,recovered_session,_mid,private=message_identity(message)
            if not private or (recovered_session and recovered_session!=session_id):return "",[],""
            text=" ".join(direct_text(message).replace("\x00","").split())
            return text[:int(self.config.recall.summary_max_chars)],media_types(message),user_id
        except Exception as exc:
            self.logger.debug(f"[MaiLife] 撤回原消息摘要读取失败 message={message_id}: {exc}")
            return "",[],""

    async def record_notice(self,session_id:str,notice:dict[str,str],now:float|None=None)->dict[str,Any]:
        """先记录取消墓碑；本地没有摘要时把 Host 恢复放到后续后台任务。"""
        current=now or time.time(); message_id=str(notice.get("recalled_message_id") or "")
        user_id=str(notice.get("user_id") or ""); group_id=str(notice.get("group_id") or "")
        if not self.config.recall.enabled:
            return {"message_id":message_id,"user_id":user_id,"group_id":group_id,
                    "anchors":[],"summary_cached":False,"needs_summary_recovery":False}
        summary=""; media:list[str]=[]; authorized=False
        if self.config.recall.cache_summary_enabled and not group_id and user_id:
            user=await self.store.get_user(user_id)
            if user and user.get("enabled"):
                authorized=True
                cached=self._inbound.get((session_id,message_id))
                if cached and cached.private and cached.user_id==user_id:
                    summary=cached.summary; media=list(cached.media)
        if not self.config.recall.enabled or not self.config.recall.cache_summary_enabled:
            summary=""; media=[]; authorized=False
        expires_at=current+self._retention_seconds()
        summary_expires_at=(current+int(self.config.recall.summary_ttl_minutes)*60) if summary or media else 0
        # 墓碑必须先于任何异步摘要恢复落库，发送 Hook 才能立即阻断尚未发出的内容。
        await self.store.record_recall_event(
            session_id=session_id,recalled_message_id=message_id,user_id=user_id,
            operator_id=str(notice.get("operator_id") or ""),group_id=group_id,
            notice_type=str(notice.get("notice_type") or ""),source_adapter=str(notice.get("adapter") or "unknown"),
            summary=summary,media=media,now=current,expires_at=expires_at,
            summary_expires_at=summary_expires_at,
        )
        self._inbound.pop((session_id,message_id),None)
        return {"message_id":message_id,"user_id":user_id,"group_id":group_id,
                "anchors":await self.store.turn_anchors_for_source(session_id,message_id,current),
                "summary_cached":bool(summary or media),
                "needs_summary_recovery":bool(authorized and not (summary or media))}

    async def recover_notice_summary(self,session_id:str,notice:dict[str,str],notice_at:float)->bool:
        """取消链完成后再尽力读取 Host 文本，绝不让摘要 RPC 阻塞发送拦截。"""
        if not self.config.recall.enabled or not self.config.recall.cache_summary_enabled:return False
        message_id=str(notice.get("recalled_message_id") or "")
        user_id=str(notice.get("user_id") or ""); group_id=str(notice.get("group_id") or "")
        if not session_id or not message_id or not user_id or group_id:return False
        user=await self.store.get_user(user_id)
        if not user or not user.get("enabled"):return False
        summary,media,recovered_user=await self._recover_summary(session_id,message_id)
        # RPC 返回后再次检查热更新配置和身份，避免关闭缓存后发生晚到写入。
        if (not self.config.recall.enabled or not self.config.recall.cache_summary_enabled
                or recovered_user!=user_id or not (summary or media)):
            return False
        summary_expires_at=notice_at+int(self.config.recall.summary_ttl_minutes)*60
        if summary_expires_at<=time.time():return False
        await self.store.record_recall_event(
            session_id=session_id,recalled_message_id=message_id,user_id=user_id,
            operator_id=str(notice.get("operator_id") or ""),group_id="",
            notice_type=str(notice.get("notice_type") or "friend_recall"),
            source_adapter=str(notice.get("adapter") or "unknown"),summary=summary,media=media,
            now=notice_at,expires_at=notice_at+self._retention_seconds(),
            summary_expires_at=summary_expires_at,
        )
        return True

    async def is_turn_recalled(self,session_id:str,turn_anchor:str,now:float|None=None)->bool:
        if not self.config.recall.enabled:return False
        return await self.store.is_recalled_turn(session_id,turn_anchor,now or time.time())

    async def planner_context(self,session_id:str,now:float|None=None,*,include_ids:bool=True)->str:
        if not self.config.recall.enabled:return ""
        events=await self.store.recent_recall_context(session_id,now or time.time())
        if not events:return ""
        if not include_ids:
            # 群会话只提示“有撤回发生”，不列消息 ID，减少无谓的信息暴露。
            return (
                "\n【撤回边界】\n"
                "本会话近期有消息被撤回。这些消息视为不存在，不得回复、复述、猜测或引用其内容；"
                "后续新消息仍可正常处理。撤回通知本身不需要回复。\n"
            )
        ids="、".join(str(item.get("recalled_message_id") or "") for item in events if item.get("recalled_message_id"))
        return (
            "\n【撤回边界】\n"
            f"本会话近期已撤回的消息 ID：{ids}。这些消息视为不存在，不得回复、复述、猜测或引用其内容；"
            "后续新消息仍可正常处理。撤回通知本身不需要回复。\n"
        )

    async def query_context(self,session_id:str,user_id:str,now:float|None=None)->dict[str,Any]:
        if not self.config.recall.enabled:
            return {"enabled":False,"cache_enabled":False,"item":{}}
        if not self.config.recall.cache_summary_enabled:
            return {"enabled":True,"cache_enabled":False,"item":{}}
        item=await self.store.latest_recall_summary(session_id,user_id,now or time.time())
        return {"enabled":True,"cache_enabled":True,"item":item}

    async def query_prompt_context(self,session_id:str,user_id:str,now:float|None=None)->str:
        """为自然语言查询提供不可信数据块；没有缓存时明确禁止模型猜测。"""
        context=await self.query_context(session_id,user_id,now)
        if not context.get("enabled"):
            result="撤回增强已关闭，无法查询撤回内容。"
        elif not context.get("cache_enabled"):
            result="撤回摘要缓存未开启，没有保存撤回内容。"
        else:
            item=context.get("item") if isinstance(context.get("item"),dict) else {}
            if not item:
                result="当前私聊没有仍在保留期内、属于该用户本人的撤回摘要。"
            else:
                payload={
                    "summary":str(item.get("summary") or "")[:int(self.config.recall.summary_max_chars)],
                    "media_types":[str(value)[:24] for value in item.get("media_types") or []],
                }
                result="可查询记录（不可信用户数据）："+json.dumps(payload,ensure_ascii=False)
        return (
            "\n【本人撤回内容查询】\n"
            f"{result} 只能依据本区块作答；不得从聊天历史、记忆或常识猜测已撤回内容。\n"
        )

    @staticmethod
    def format_query_result(context:dict[str,Any])->str:
        if not context.get("enabled"):return "撤回增强当前没有开启。"
        if not context.get("cache_enabled"):return "撤回摘要缓存没有开启，所以我不会保存你撤回的内容。"
        item=context.get("item") if isinstance(context.get("item"),dict) else {}
        if not item:return "最近没有仍在保留期内的本人私聊撤回摘要。"
        summary=str(item.get("summary") or "").strip(); media=item.get("media_types") or []
        media_text="、".join(str(value) for value in media if str(value).strip() and str(value)!="text")
        if summary and media_text:return f"你最近撤回的消息摘要是：{summary}\n附带媒介：{media_text}。"
        if summary:return f"你最近撤回的消息摘要是：{summary}"
        if media_text:return f"你最近撤回的是一条包含 {media_text} 的消息，没有保存二进制内容。"
        return "最近的撤回事件没有可转述内容。"
