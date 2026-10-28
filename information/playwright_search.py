"""Playwright 搜索结果页解析与浏览器搜索客户端。"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any,Awaitable,Callable
from urllib.parse import urlencode

import asyncio
import base64
import binascii
import re

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchBackendError,SearchResponse,SearchResult
from .search_parsing import is_weak_result


_BING_REDIRECT=re.compile(r"(?:[?&])u=a1([^&]+)",re.I)
# Bing 查询整形剔除的疑问/语气冗余词（长词优先，避免短词抢先匹配段尾）。
# 只按词边界剔除（整段或段尾），绝不做子串剔除：'为什么'里的'什么'按子串删掉会变成'为'，
# '什么意思'会变成'意思'，正常疑问词被打碎，失配反而加重。
_BING_QUERY_FILLERS=("怎么样","什么的","一下子","的话","怎么","如何","什么","请问","一下")
# 独立成词（空白/标点分隔）时才丢弃的单字语气词。
_BING_FILLER_CHARS=frozenset("的吗呢吧啊呀嘛么地得和与及或了")
# 整形后查询少于 3 个字符时保留原查询：1-2 字查询发给搜索引擎没有意义，
# 也避免把"怎么 办"这类带空白的疑问词打成单字"办"。
_BING_MIN_SHAPED=3


def _clean_text(value:str,limit:int)->str:
    return " ".join(value.split())[:limit]


def _validated_url(value:str)->str:
    url=value.strip()[:2000]
    try:HttpClient.validate_url(url)
    except HttpRequestError:return ""
    return url


def _bing_real_url(value:str)->str:
    """解码 Bing 结果的 u=a1<Base64URL> 重定向，失败时按普通链接校验。"""
    match=_BING_REDIRECT.search(value)
    if match:
        encoded=match.group(1)
        padding="="*(-len(encoded)%4)
        try:
            decoded=base64.urlsafe_b64decode(encoded+padding).decode("utf-8")
        except (UnicodeDecodeError,binascii.Error):decoded=value
        value=decoded
    return _validated_url(value)


class _SearchHTMLParser(HTMLParser):
    def __init__(self)->None:
        super().__init__(convert_charrefs=True)
        self._stack:list[str]=[]
        self._classes:list[str]=[]
        self.results:list[dict[str,str]]=[]
        self._current:dict[str,str]|None=None
        self._field=""

    @staticmethod
    def _class_value(attrs:list[tuple[str,str|None]])->str:
        return " ".join(str(value or "") for key,value in attrs if key=="class").casefold()

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        self._stack.append(tag); self._classes.append(self._class_value(attrs))
        if self._current is not None:self._on_child(tag,self._classes[-1],attrs)

    def handle_endtag(self,tag:str)->None:
        if self._current is not None:self._close_child(tag)
        if self._stack:del self._stack[-1]
        if self._classes:del self._classes[-1]
        if tag==self._container_tag and self._current is not None:
            self._finish_result()

    def handle_data(self,data:str)->None:
        if self._current is None or not self._field:return
        self._current[self._field]=(self._current.get(self._field) or "")+data

    def parse(self,html:str,limit:int,query:str="")->list[SearchResult]:
        """解析结果页 HTML，并按配置数量返回自然搜索结果。

        query 非空时按词元重叠逐条填充 weak 弱相关标记（与另一组的 SearchResult.weak 契约）。
        """
        self.feed(html); self.close()
        cleaned=" ".join(str(query or "").split())
        results=[]
        for item in self.results[:max(0,int(limit))]:
            weak=bool(cleaned) and is_weak_result(cleaned,item["title"],item["snippet"])
            results.append(SearchResult(title=item["title"],url=item["url"],snippet=item["snippet"],weak=weak))
        return results

    def _finish_result(self)->None:
        result=self._current or {}
        title=_clean_text(result.get("title") or "",500)
        url=self._clean_url(result.get("url") or "")
        snippet=_clean_text(result.get("snippet") or "",3000)
        if title and url:self.results.append({"title":title,"url":url,"snippet":snippet})
        self._current=None; self._field=""

    def _close_child(self,tag:str)->None:
        if self._field and tag in {"a","p","span","div"}:self._field=""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:raise NotImplementedError
    def _clean_url(self,value:str)->str:return _validated_url(value)
    @property
    def _container_tag(self)->str:raise NotImplementedError


class BingSearchParser(_SearchHTMLParser):
    """解析 Bing 自然结果卡片，并还原重定向后的目标地址。"""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:
        if tag!="a" or "h2" not in self._stack:return
        href=next((str(value or "") for key,value in attrs if key=="href"),"")
        self._current["url"]=href; self._field="title"

    def _clean_url(self,value:str)->str:return _bing_real_url(value)
    @property
    def _container_tag(self)->str:return "li"

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        classes=self._class_value(attrs)
        if tag=="li" and "b_algo" in classes.split():
            self._current={"title":"","url":"","snippet":""}
        super().handle_starttag(tag,attrs)

    def handle_data(self,data:str)->None:
        if self._current is not None and not self._field and any("b_caption" in item.split() for item in self._classes):
            self._field="snippet"
        super().handle_data(data)

    def _close_child(self,tag:str)->None:
        if self._field=="snippet" and tag=="p":self._field=""
        super()._close_child(tag)


class DuckDuckGoSearchParser(_SearchHTMLParser):
    """解析 DuckDuckGo HTML 结果页。"""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:
        words=classes.split()
        if tag=="a" and "result__a" in words:
            href=next((str(value or "") for key,value in attrs if key=="href"),"")
            self._current["url"]=href; self._field="title"
        elif tag in {"a","div","span"} and "result__snippet" in words:self._field="snippet"

    @property
    def _container_tag(self)->str:return "div"

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        classes=self._class_value(attrs); words=classes.split()
        if tag=="div" and "result" in words and any(item.startswith("result__") or item=="results_links_normal" for item in words):
            self._current={"title":"","url":"","snippet":""}
        super().handle_starttag(tag,attrs)




class PlaywrightSearchClient:
    """用 Chromium 搜索并解析自然结果；浏览器实例在多次搜索间复用。"""

    _BING_FRESHNESS={"day":'ex1:"ez5_19700_19701"',"week":'ex1:"ez5_19700_19702"',
                     "month":'ex1:"ez5_19700_19704"',"year":'ex1:"ez5_19700_19705"'}
    _DUCK_FRESHNESS={"day":"d","week":"w","month":"m","year":"y"}

    @staticmethod
    def _normalized_engine(engine:str)->str:
        """搜索引擎名统一归一化：URL 生成与解析器选择必须走同一条路径。

        否则传入 "Bing" 时 search_url 按 Bing 建 URL、解析器却精确匹配 engine=="bing" 失败
        而回落到 DuckDuckGo 解析器，把 Bing 页面解析成空结果。
        """
        return str(engine or "").strip().casefold()

    @staticmethod
    def _shape_bing_query(query:str)->str:
        """Bing 查询整形：剔除疑问/语气冗余词，降低多词中文查询只命中部分词元的失配。

        只按词边界剔除，绝不做子串剔除：'为什么/怎么办/什么意思' 里的 '什么/怎么'
        一旦按子串删掉就变成 '为/办/意思'，与降低失配的意图相反。
        - 空白/标点分隔的整段全是冗余词（多字词或单字语气词）时丢弃；
        - CJK 连续段只剔除段尾的冗余词，且剔除后该段仍不少于 _BING_MIN_SHAPED 个字符
          （'明天天气怎么样'→'明天天气'；'什么意思'→'意思' 这类不足 3 字的剔除直接放弃）。
        全部为冗余词、或整形后不足 _BING_MIN_SHAPED 个字符时保留原查询，避免把 q 整空或打得太碎。
        """
        parts=[]
        for part in query.split():
            if part in _BING_QUERY_FILLERS:continue
            if all(character in _BING_FILLER_CHARS for character in part):continue
            for filler in _BING_QUERY_FILLERS:
                if part.endswith(filler) and len(part)-len(filler)>=_BING_MIN_SHAPED:
                    part=part[:len(part)-len(filler)]; break
            parts.append(part)
        shaped=" ".join(parts)
        return shaped if len(shaped)>=_BING_MIN_SHAPED else query

    @staticmethod
    def search_url(engine:str,query:str,freshness:str)->str:
        """生成固定搜索引擎地址；不接受任意自定义 URL。"""
        kind=PlaywrightSearchClient._normalized_engine(engine)
        value=str(freshness or "any").strip().casefold()
        raw=str(query or "").strip()
        params={"q":PlaywrightSearchClient._shape_bing_query(raw) if kind=="bing" else raw}
        if kind=="bing":
            if value in PlaywrightSearchClient._BING_FRESHNESS:params["filters"]=PlaywrightSearchClient._BING_FRESHNESS[value]
            return "https://www.bing.com/search?"+urlencode(params)
        if kind=="duckduckgo":
            if value in PlaywrightSearchClient._DUCK_FRESHNESS:params["df"]=PlaywrightSearchClient._DUCK_FRESHNESS[value]
            return "https://html.duckduckgo.com/html/?"+urlencode(params)
        raise SearchBackendError("不支持的浏览器搜索引擎",error_class="invalid_response")

    def __init__(self,logger:Any,*,headless:bool=True,
                 playwright_factory:Callable[[],Awaitable[Any]]|None=None)->None:
        self.logger=logger; self._headless=bool(headless)
        self._playwright_factory=playwright_factory or self._start_playwright
        self._init_lock=asyncio.Lock(); self._search_lock=asyncio.Lock()
        self._playwright:Any|None=None; self._browser:Any|None=None; self._context:Any|None=None

    @staticmethod
    async def _start_playwright()->Any:
        from playwright.async_api import async_playwright
        return await async_playwright().start()

    async def _ensure_browser(self)->Any:
        if self._context is not None:return self._context
        async with self._init_lock:
            if self._context is not None:return self._context
            try:playwright=await self._playwright_factory()
            except ImportError as exc:
                raise SearchBackendError("Playwright 依赖未安装",error_class="playwright_unavailable") from exc
            browser=None
            try:browser=await playwright.chromium.launch(headless=self._headless)
            except Exception as exc:
                if playwright:await playwright.stop()
                raise SearchBackendError("Chromium 浏览器不可用",error_class="browser_unavailable") from exc
            try:
                context=await browser.new_context(locale="zh-CN",viewport={"width":1366,"height":768})
            except Exception as exc:
                await browser.close(); await playwright.stop()
                raise SearchBackendError("浏览器上下文创建失败",error_class="browser_unavailable") from exc
            self._playwright=playwright; self._browser=browser; self._context=context
            return context

    async def search(self,query:str,*,engine:str,freshness:str,
                     timeout_seconds:float,max_results:int)->SearchResponse:
        """执行一次固定引擎搜索，并返回自然结果。"""
        cleaned=" ".join(str(query or "").split())
        if not cleaned:raise SearchBackendError("搜索词为空",error_class="invalid_response")
        kind=self._normalized_engine(engine)
        url=self.search_url(engine,cleaned,freshness)
        parser=BingSearchParser() if kind=="bing" else DuckDuckGoSearchParser()
        timeout_ms=max(1000,int(timeout_seconds*1000))
        async with self._search_lock:
            context=await self._ensure_browser(); page=await context.new_page()
            try:
                # 路由注册在 page 上，page.close() 后自动失效；Chromium 跟随重定向发起的每一跳都过公网校验（与 screenshot 对齐，阻断重定向 SSRF）。
                await page.route("**/*",self._guard_browser_request)
                await page.goto(url,timeout=timeout_ms,wait_until="domcontentloaded")
                # page.content() 与 goto 同源超时：页面卡住时按网络超时分类，绝不无限期挂起。
                html=await asyncio.wait_for(page.content(),timeout=timeout_ms/1000)
            except Exception as exc:
                message=str(exc).casefold()
                if "timeout" in message or isinstance(exc,TimeoutError):
                    raise SearchBackendError("浏览器搜索超时",error_class="network") from exc
                if any(marker in message for marker in ("err_name_not_resolved","err_connection","err_timed_out","failed to navigate")):
                    raise SearchBackendError("浏览器搜索网络失败",error_class="network") from exc
                raise SearchBackendError("浏览器搜索失败",error_class="browser_error") from exc
            finally:await page.close()
            results=parser.parse(html,max_results,cleaned)
            if not results:
                lowered=html.casefold()
                if any(marker in lowered for marker in ('class="b_captcha"',"challenge-form","are you a human","unusual traffic")):
                    raise SearchBackendError("搜索页面被验证码或反爬拦截",error_class="blocked")
                raise SearchBackendError("浏览器搜索未返回自然结果",error_class="empty_result")
            return SearchResponse(results=results,provider_type="playwright",cited=True,model=str(engine))

    async def _guard_browser_request(self,route:Any,request:Any)->None:
        """校验浏览器将发起的每一个请求（含重定向目标），非公网一律中断，阻断重定向 SSRF。"""
        url=str(getattr(request,"url","") or "")
        try:
            # validate_public_url 是同步阻塞（DNS 解析），放线程里执行避免卡住事件循环。
            await asyncio.to_thread(HttpClient.validate_public_url,url)
        except Exception as exc:
            debug=getattr(self.logger,"debug",None)
            if callable(debug):debug(f"Playwright 拦截非公网或未校验请求:{url}({exc})")
            await route.abort()
            return
        await route.continue_()

    async def screenshot(self,url:str,timeout_seconds:float)->bytes:
        """打开网页并截图，返回 PNG 字节；失败抛 SearchBackendError。"""
        try:
            validated=HttpClient.validate_public_url(url)
        except HttpRequestError:
            raise SearchBackendError("网页地址无效或不允许访问内网",error_class="invalid_response")
        if not validated:
            raise SearchBackendError("网页地址为空或不可访问",error_class="invalid_response")
        async with self._search_lock:
            context=await self._ensure_browser()
            page=await context.new_page()
            try:
                # 路由注册在 page 上，page.close() 后自动失效；Chromium 跟随重定向发起的每一跳都过公网校验。
                await page.route("**/*",self._guard_browser_request)
                await page.goto(validated,timeout=max(1000,int(timeout_seconds*1000)),wait_until="domcontentloaded")
                return await page.screenshot(type="png")
            except Exception as exc:
                message=str(exc).casefold()
                if "timeout" in message or isinstance(exc,TimeoutError):
                    raise SearchBackendError("网页打开超时",error_class="network") from exc
                raise SearchBackendError("网页打开失败",error_class="browser_error") from exc
            finally:
                await page.close()

    async def close(self)->None:
        """释放页面上下文、浏览器与 Playwright 运行时。"""
        async with self._search_lock:
            context,browser,playwright=self._context,self._browser,self._playwright
            self._context=None; self._browser=None; self._playwright=None
            if context is not None:await context.close()
            if browser is not None:await browser.close()
            if playwright is not None:await playwright.stop()
