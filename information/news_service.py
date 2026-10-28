"""通过统一搜索 API 读取低频新闻并按需抓取正文。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any

from .feed_parser import readable_text
from .http_client import HttpClient
from .search_models import SearchResult
from .search_service import SearchService


class NewsService:
    def __init__(self,store:Any,config:Any,search:SearchService,http:HttpClient,logger:Any,llm:Any,bot_name:str="麦麦")->None:
        self.store=store; self.config=config; self.search=search; self.http=http; self.logger=logger
        self.llm=llm; self.bot_name=bot_name

    def update_config(self,config:Any)->None:self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    @staticmethod
    def _day_bounds(now:Any)->tuple[float,float]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        return start.timestamp(),(start+timedelta(days=1)).timestamp()

    async def refresh_due(self,now:Any,schedule:dict[str,Any],personality:str)->int:
        """在允许的空闲日程由人格规划新闻词，执行一次逻辑新闻搜索并保存去重结果。"""
        cfg=self.config.news
        if not self.config.information.enabled or not cfg.enabled or int(cfg.daily_max)<=0:return 0
        current=schedule.get("current") or {}
        if str(current.get("kind") or "") not in set(cfg.allowed_schedule_types):return 0
        start,end=self._day_bounds(now)
        if await self.store.search_attempt_count("news",start,end)>=int(cfg.daily_max):return 0
        query=await self.search.sanitize_query(await self._plan_query(now,personality,schedule))
        if not query:
            return 0
        response=await self.search.search(
            query,operation="news",freshness="day",event_at=now.timestamp(),
        )
        if not response.results:return 0
        # 入库前相关性过滤：弱相关条目（标题/摘要与查询词无实质词元重叠，由 Playwright 侧标记 weak）不入库；
        # API 路线不填充 weak（保持 False），因此只影响浏览器搜索结果。
        results=[item for item in response.results if not item.weak][:min(5,int(self.config.search_api.max_results))]
        if not results:
            self.logger.info("[MaiLife] 新闻搜索结果整体弱相关，未写入新闻库")
            return 0
        contents=await self._read_articles(results)
        changed=0; now_ts=now.timestamp(); retention=int(cfg.retention_days)*86400
        for index,result in enumerate(results):
            uncited=bool(result.provider_generated and not result.url)
            summary=result.snippet
            if uncited:summary="Provider 生成、无外部引用："+summary
            # 去重键只由 url 或 title+snippet 决定，不再拼入 provider_id：
            # 配置变化导致 provider_id 改变时，同一条新闻不会被重复入库。
            stable=result.url or hashlib.sha256(f"{result.title}:{result.snippet}".encode()).hexdigest()
            item_id=hashlib.sha256(stable.encode()).hexdigest()
            content=contents.get(index,"")[:int(cfg.max_article_chars)]
            digest=hashlib.sha256(json.dumps([result.title,summary,content],ensure_ascii=False).encode()).hexdigest()
            source_id=f"api:{response.provider_type}"+(":uncited" if uncited else "")
            if await self.store.upsert_news_item({
                "id":item_id,"source_id":source_id,"title":result.title,"url":result.url,
                "summary":summary,"content":content,"published_at":now_ts,"fetched_at":now_ts,
                "content_hash":digest,"expires_at":now_ts+retention,
            }):changed+=1
        return changed

    async def _plan_query(self,now,personality:str,schedule:dict)->str:
        """让麦麦根据人格自主决定此刻想了解什么新闻；模型不可用时跳过。"""
        if not self.llm.task_available("news"):
            return ""
        current=schedule.get("current") or {}
        result=await self.llm.generate_json(
            f"你是{self.bot_name}。根据你的人设与当前状态，自主决定此刻最想了解的一类新闻，给出一个简短、"
            "适合网页搜索的查询词（不超过 40 字）。不要搜索用户身份、账号、群名、私密关系或聊天原句。\n"
            + json.dumps({
                "personality": personality[:1200],
                "current_activity": current.get("summary") or "",
            }, ensure_ascii=False)
            + "\n只返回JSON：{\"query\":\"\"}。",
            f"你自主决定{self.bot_name}此刻读什么新闻。", {"query": ""}, max_tokens=0,
            task_kind="news", request_type="news_query_planning",
        )
        if not isinstance(result,dict):
            return ""
        query=" ".join(str(result.get("query") or "").split())[:100]
        return query

    async def _read_articles(self,results:list[SearchResult])->dict[int,str]:
        """并发读取最多三篇公网正文；任一页面失败只降级为搜索摘要。"""
        limit=min(len(results),int(self.config.news.full_text_count),3)
        if limit<=0:return {}
        semaphore=asyncio.Semaphore(int(self.config.news.max_concurrency))
        async def read(index:int,result:SearchResult)->tuple[int,str]:
            if not result.url:return index,""
            async with semaphore:
                try:
                    response=await self.http.get(
                        result.url,timeout=float(self.config.search_api.timeout_seconds),max_bytes=2_000_000,
                        headers={"Accept":"text/html,text/plain;q=0.9"},public_only=True,
                    )
                    content_type=response.headers.get("content-type","").casefold()
                    if content_type and "text/html" not in content_type and "text/plain" not in content_type:return index,""
                    return index,readable_text(response.text(),int(self.config.news.max_article_chars))
                except Exception:return index,""
        values=await asyncio.gather(*(read(index,result) for index,result in enumerate(results[:limit])))
        return {index:content for index,content in values if content}
