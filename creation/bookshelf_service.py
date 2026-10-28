"""按主人/朋友边界读取书柜。"""
from __future__ import annotations

from typing import Any


_TYPE_LABELS={
    "novel_fragment":"小说片段","poem":"诗","essay":"随笔","screenplay":"短剧",
    "storyboard":"分镜脚本","character":"角色设定","worldbuilding":"世界观片段",
    "reading_note":"阅读笔记","diary":"日记",
}


class BookshelfService:
    def __init__(self,store:Any,config:Any)->None:self.store=store; self.config=config
    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def allow_private(user:dict[str,Any],is_admin:bool=False)->bool:
        return bool(is_admin or str(user.get("role") or "friend")=="owner")

    async def list_for_user(self,user:dict[str,Any],limit:int=20,*,is_admin:bool=False,
                            doc_type:str="")->list[dict[str,Any]]:
        return await self.store.list_bookshelf_documents(
            allow_private=self.allow_private(user,is_admin),limit=limit,doc_type=doc_type,
        )

    async def read_for_user(self,document_id:str,user:dict[str,Any],*,is_admin:bool=False)->dict[str,Any]:
        return await self.store.get_bookshelf_document(
            document_id,allow_private=self.allow_private(user,is_admin),
        )

    async def resolve_reference(self,reference:str,user:dict[str,Any],*,is_admin:bool=False,
                                limit:int=20)->str:
        """把 /麦麦阅读 的输入解析为文档 ID。

        纯数字按与 /麦麦书柜 相同的权限、排序和上限取列表序号；超出范围或列表为空
        返回空串。其余输入视为完整文档 ID 原样返回——现有 ID 形如
        work-…/diary:…/reading:…，不会是纯数字，因此两种输入没有歧义。
        """
        ref=str(reference or "").strip()
        if not ref:
            return ""
        if not ref.isdigit():
            return ref
        rows=await self.list_for_user(user,limit,is_admin=is_admin)
        index=int(ref)
        if 1<=index<=len(rows):
            return str(rows[index-1]["id"])
        return ""

    async def context_for_user(self,user:dict[str,Any],limit:int=3)->dict[str,Any]:
        rows=await self.list_for_user(user,limit)
        return {"items":[{"id":item["id"],"type":_TYPE_LABELS.get(item.get("work_type"),item.get("doc_type","文本")),
                          "title":item["title"],"summary":item.get("summary") or "",
                          "privacy":item["privacy"]} for item in rows]}

    @staticmethod
    def type_label(value:str)->str:return _TYPE_LABELS.get(value,value or "文本")
