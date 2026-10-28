"""命令回复的实时 stream 解析、图片发送与文本降级。"""
from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import Any


class CommandReplyService:
    def __init__(self,ctx:Any,logger:Any)->None:self.ctx=ctx; self.logger=logger

    def _debug(self,message:str)->None:
        callback=getattr(self.logger,"debug",None)
        if callable(callback):
            try:callback(message)
            except Exception:pass

    @classmethod
    def _stream_dicts(cls,value:Any,depth:int=0)->Iterable[dict[str,Any]]:
        """遍历 SDK 字典、列表和模型对象中的会话记录，并限制递归深度。"""
        if depth>5:return
        if hasattr(value,"model_dump"):
            try:value=value.model_dump()
            except Exception:return
        elif not isinstance(value,(dict,list,tuple,str,bytes)) and hasattr(value,"__dict__"):
            try:value=dict(vars(value))
            except Exception:return
        if isinstance(value,dict):
            if value.get("stream_id") or value.get("session_id"):yield value
            for nested in value.values():
                if isinstance(nested,(dict,list,tuple)) or hasattr(nested,"model_dump"):
                    yield from cls._stream_dicts(nested,depth+1)
        elif isinstance(value,(list,tuple)):
            for nested in value:yield from cls._stream_dicts(nested,depth+1)

    @classmethod
    def _result_stream_id(cls,value:Any)->str:
        # SDK 标准返回嵌套字典；保留字符串兼容以适应旧 Host 和轻量测试上下文。
        if isinstance(value,str):return value.strip()
        return next((cls._stream_id(item) for item in cls._stream_dicts(value) if cls._stream_id(item)),"")

    @staticmethod
    def _stream_id(value:dict[str,Any])->str:
        return str(value.get("stream_id") or value.get("session_id") or "").strip()

    @staticmethod
    def _target_id(value:dict[str,Any],kind:str)->str:
        direct=str(value.get(f"{kind}_id") or "").strip()
        info=value.get(f"{kind}_info") if isinstance(value.get(f"{kind}_info"),dict) else {}
        return direct or str(info.get(f"{kind}_id") or "").strip()

    async def _exact_stream(self,user_id:str,group_id:str,platform:str)->str:
        try:
            result=(await self.ctx.chat.get_stream_by_group_id(group_id=group_id,platform=platform)
                    if group_id else await self.ctx.chat.get_stream_by_user_id(user_id=user_id,platform=platform))
            return self._result_stream_id(result)
        except Exception:return ""

    async def _scan_streams(self,user_id:str,group_id:str,platform:str)->str:
        """精确查询不可用时枚举对应会话类型，并按真实 QQ 目标过滤。"""
        chat=getattr(self.ctx,"chat",None)
        if chat is None:return ""
        methods=[]
        if group_id:methods.append(getattr(chat,"get_group_streams",None))
        elif user_id:methods.append(getattr(chat,"get_private_streams",None))
        methods.append(getattr(chat,"get_all_streams",None))
        target=group_id or user_id; kind="group" if group_id else "user"
        for method in methods:
            if not callable(method):continue
            try:result=await method(platform=platform)
            except Exception:continue
            for item in self._stream_dicts(result):
                if self._target_id(item,kind)==target and self._stream_id(item):return self._stream_id(item)
        return ""

    async def _open_stream(self,user_id:str,group_id:str,platform:str)->str:
        method=getattr(getattr(self.ctx,"chat",None),"open_session",None)
        if not callable(method):return ""
        try:
            result=await method(platform=platform,chat_type="group" if group_id else "private",
                                group_id=group_id,user_id=user_id)
            return self._result_stream_id(result)
        except Exception:return ""

    async def resolve_live_stream_id(self,stream_id:str,user_id:str="",group_id:str="",platform:str="qq")->str:
        """优先按真实 QQ 目标重新解析；传入 stream 只作为最终失败开放值。"""
        user=str(user_id or "").strip(); group=str(group_id or "").strip(); platform=str(platform or "qq").strip() or "qq"
        if user or group:
            exact=await self._exact_stream(user,group,platform)
            if exact:return exact
            scanned=await self._scan_streams(user,group,platform)
            if scanned:return scanned
            opened=await self._open_stream(user,group,platform)
            if opened:return opened
        return str(stream_id or "").strip()

    async def send_image_bytes_with_fallback(self,image_bytes:bytes,stream_id:str,user_id:str="",group_id:str="",
                                             platform:str="qq")->bool:
        """Base64 编码本地 PNG，优先发送到实时解析的会话，失败后尝试原始会话。"""
        if not image_bytes:return False
        resolved=await self.resolve_live_stream_id(stream_id,user_id,group_id,platform)
        if not resolved:return False
        encoded=base64.b64encode(image_bytes).decode("ascii")
        try:
            if await self.ctx.send.image(encoded,resolved):return True
        except Exception as exc:
            self._debug(f"[MaiLife] 指令图片发送失败 type={type(exc).__name__}")
        fallback=str(stream_id or "").strip()
        if not fallback or fallback==resolved:return False
        try:return bool(await self.ctx.send.image(encoded,fallback))
        except Exception:return False

    async def send_text_with_fallback(self,text:str,stream_id:str,user_id:str="",group_id:str="",
                                      platform:str="qq")->bool:
        """按与图片相同的实时会话策略发送纯文本降级内容。"""
        resolved=await self.resolve_live_stream_id(stream_id,user_id,group_id,platform)
        if not resolved:return False
        try:
            if await self.ctx.send.text(text=str(text),stream_id=resolved):return True
        except Exception as exc:
            self._debug(f"[MaiLife] 命令文本发送失败 type={type(exc).__name__}")
        fallback=str(stream_id or "").strip()
        if not fallback or fallback==resolved:return False
        try:return bool(await self.ctx.send.text(text=str(text),stream_id=fallback))
        except Exception:return False


__all__=["CommandReplyService"]
