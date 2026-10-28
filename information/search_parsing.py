"""联网搜索 API 协议解析与结果清洗。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchResponse,SearchResult

_URL_RE=re.compile(r"https?://[^\s<>\]\[()\"']+",re.I)

# 疑问/语气冗余词：弱相关判定与 Bing 整形共用。长词优先，避免"怎么"被"么"抢先。
# 只收多字词：单字"的/了"等不做子串剔除（会切碎"目的/了解"等正常词），仅在独立成词时忽略。
_WEAK_FILLERS=("怎么样","什么的","一下子","的话","怎么","如何","什么","请问","一下")
_WEAK_FILLER_CHARS=frozenset("的吗呢吧啊呀嘛么地得和与及或了")
# 词元片段：CJK 连续段与 ASCII 字母数字连续段；标点/空白天然分词。
_SEGMENT_RE=re.compile(r"[^\W_]+",re.U)
_RUN_RE=re.compile(r"[\u4e00-\u9fff]+|[0-9a-z]+")
# 弱判定阈值：命中的内容字符占比不足三分之一视为"重叠极弱"。
_WEAK_COVERAGE=3


def _nested(value:Any,*paths:str)->Any:
    for path in paths:
        current=value
        for part in path.split("."):
            if not isinstance(current,dict):current=None; break
            current=current.get(part)
        if current is not None:return current
    return None


def error_from_payload(payload:Any)->str:
    if not isinstance(payload,dict):return ""
    code=str(payload.get("code") or payload.get("status") or "").casefold()
    error=payload.get("error")
    success=payload.get("success")
    # 显式成功（success=True 或 code 为明确成功值且无 error 字段）时直接返回，
    # 避免 message 中误含 "quota"/"balance"/"credit" 等词被当成错误。
    # 注意 code="" 属于歧义，不在此早返回，需继续走关键字分类以兼容只靠 message 报错的 Provider。
    if error is None and success is not False and code in {"0","200","ok","success"}:
        return ""
    if isinstance(error,dict):message=str(error.get("message") or error.get("type") or "")
    else:message=str(error or payload.get("message") or payload.get("msg") or "")
    text=(code+" "+message).casefold()
    if not text.strip():return ""
    if any(term in text for term in ("insufficient_quota","quota","credit","balance","exhausted","额度","余额")):return "quota"
    if any(term in text for term in ("unauthorized","authentication","invalid api key","invalid_api_key","鉴权","密钥无效")):return "auth"
    if any(term in text for term in ("rate limit","rate_limit","too many","限流")):return "rate_limit"
    if success is False or code not in {"","0","200","ok","success"}:return "provider_error"
    return ""


def clean_result(title:Any,url:Any,snippet:Any,generated:bool=False)->SearchResult|None:
    title_text=" ".join(str(title or "").split())[:500]
    snippet_text=" ".join(str(snippet or "").split())[:3000]
    url_text=str(url or "").strip()[:2000]
    if url_text:
        try:HttpClient.validate_url(url_text)
        except HttpRequestError:url_text=""
    if not title_text and not snippet_text:return None
    return SearchResult(title_text or "未命名结果",url_text,snippet_text,generated)


def _filler_mask(segment:str)->list[bool]:
    """标出片段中冗余词占据的字符位置（长词优先，避免短词切碎长词）。"""
    mask=[False]*len(segment); index=0
    while index<len(segment):
        for filler in _WEAK_FILLERS:
            if segment.startswith(filler,index):
                for position in range(index,index+len(filler)):mask[position]=True
                index+=len(filler); break
        else:index+=1
    return mask


def is_weak_result(query:str,title:str,snippet:str)->bool:
    """纯函数：结果标题/摘要与查询词没有实质词元重叠时判弱，供 Playwright 解析阶段填充 weak。

    语义（按 2026-10-17 cn.bing 实证标定）：
    - 查询按标点/空白切分，CJK 段生成 2-4 字滑窗、ASCII 段按整词，与标题+摘要比对；
    - 完全不重叠，或命中内容字符不足查询内容字符的三分之一（"重叠极弱"）→ True；
    - 1 字 CJK 查询恒判弱：滑窗最小 2 字，单字无法命中，字面单字不足以判定相关
      （实证：'猫'只命中百科词条）；
    - 查询没有可匹配的内容字符（单字 ASCII、纯语气词）时无法证明相关 → True；
    - 2 字查询完整字面命中即过阈值，不因查询短而一律判弱（实证：'天气'命中北京天气预报、
      '新闻'命中腾讯网新闻中心、'电影'命中豆瓣电影、'go'命中 The Go Programming Language）。
    不得误判的实证：'明天天气怎么样'命中 5 个天气站点（命中"天气"即过阈值）、
    '开源软件'命中 OSCHINA/GitHub/Gitee（完整四字命中）都必须 weak=False。
    """
    text=unicodedata.normalize("NFKC",f"{title or ''} {snippet or ''}").casefold()
    query_text=unicodedata.normalize("NFKC",str(query or "")).casefold()
    if not query_text.strip():return False  # 查询为空时无法判定，不标记弱
    content=0; matched_content=0
    for segment in _SEGMENT_RE.findall(query_text):
        if all(character in _WEAK_FILLER_CHARS for character in segment):continue
        mask=_filler_mask(segment); hit:set[int]=set()
        for run in _RUN_RE.finditer(segment):
            token=run.group(); start=run.start()
            if token.isascii():
                # ASCII 词元按整词边界匹配，避免 "ai" 命中 "said" 之类的子串噪声。
                if len(token)<2:continue
                content+=len(token)
                if re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])",text):
                    hit.update(range(start,start+len(token)))
            else:
                content+=sum(1 for position in range(start,start+len(token)) if not mask[position])
                for offset in range(max(0,len(token)-1)):
                    for size in (4,3,2):
                        if offset+size>len(token):continue
                        if token[offset:offset+size] in text:
                            hit.update(range(start+offset,start+offset+size))
        matched_content+=sum(1 for position in hit if not mask[position])
    # 没有可匹配的内容字符（单字 ASCII 被跳过、整段都是语气词）时无法证明相关，判弱；
    # 短查询不再一律判弱——2 字查询完整字面命中（天气/新闻/电影）是强相关信号。
    if content==0:return True
    return matched_content*_WEAK_COVERAGE<content


def parse_standard(provider_type:str,payload:Any,max_results:int)->SearchResponse:
    """把博查、Tavily 和 You.com 的不同字段归一化为统一搜索结果。"""
    if not isinstance(payload,dict):return SearchResponse([])
    if provider_type=="bocha":
        raw=_nested(payload,"data.webPages.value","data.web_pages.value","webPages.value","results")
        fields=("name","url","summary")
    elif provider_type=="tavily":
        raw=payload.get("results"); fields=("title","url","content")
    else:
        raw=_nested(payload,"hits","results","data.hits"); fields=("title","url","description")
    results=[]
    for item in raw if isinstance(raw,list) else []:
        if not isinstance(item,dict):continue
        snippet=(item.get(fields[2]) or item.get("snippet") or item.get("snippets")
                 or item.get("summary") or item.get("content") or "")
        if isinstance(snippet,list):snippet=" ".join(str(value) for value in snippet)
        result=clean_result(item.get(fields[0]) or item.get("name"),item.get(fields[1]),snippet)
        if result:results.append(result)
    limit=int(max_results)
    return SearchResponse(results[:limit],cited=any(item.url for item in results[:limit]))


def content_text(value:Any)->str:
    if isinstance(value,str):return value
    if not isinstance(value,list):return ""
    parts=[]
    for item in value:
        if isinstance(item,str):parts.append(item)
        elif isinstance(item,dict):
            text=item.get("text") or item.get("content")
            if isinstance(text,str):parts.append(text)
    return "\n".join(parts)


def parse_openai_citations(payload:Any,provider_type:str)->list[Any]:
    """纯函数：从 Responses/Chat 载荷中提取引用标注，兼容顶层与消息内嵌两种位置。"""
    if not isinstance(payload,dict):return []
    citations=[]
    if provider_type=="openai_responses":
        for output in payload.get("output") if isinstance(payload.get("output"),list) else []:
            if not isinstance(output,dict):continue
            for content in output.get("content") if isinstance(output.get("content"),list) else []:
                if not isinstance(content,dict):continue
                for annotation in content.get("annotations") if isinstance(content.get("annotations"),list) else []:
                    if isinstance(annotation,dict):citations.append(annotation)
    else:
        choices=payload.get("choices") if isinstance(payload.get("choices"),list) else []
        message=(choices[0].get("message") if choices and isinstance(choices[0],dict) else {})
        if isinstance(message,dict):
            for key in ("citations","sources","web_search_results"):
                value=message.get(key)
                if isinstance(value,list):citations.extend(value)
    for key in ("citations","sources","web_search_results"):
        value=payload.get(key)
        if isinstance(value,list):citations.extend(value)
    return citations


def parse_openai_usage(payload:Any)->tuple[int,int,int]:
    """纯函数：解析 Responses/Chat 的 Token 用量，缺省时用输入加输出推算总数。"""
    if not isinstance(payload,dict):return 0,0,0
    usage=payload.get("usage") if isinstance(payload.get("usage"),dict) else {}
    prompt=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total=int(usage.get("total_tokens") or prompt+completion)
    return prompt,completion,total


def clean_generated_text(texts:list[str])->str:
    """纯函数：合并生成文本片段并做空白清洗，用于展示与引用去重。"""
    return "\n".join(part.strip() for part in texts if part.strip())[:12000]


def parse_openai(provider_type:str,payload:Any,model:str,max_results:int)->SearchResponse:
    """解析 Responses/Chat 中转文本、引用和 Token；无 URL 时保留 Provider 生成标记。"""
    if not isinstance(payload,dict):return SearchResponse([])
    texts=[]
    if provider_type=="openai_responses":
        if isinstance(payload.get("output_text"),str):texts.append(payload["output_text"])
        for output in payload.get("output") if isinstance(payload.get("output"),list) else []:
            if not isinstance(output,dict):continue
            for content in output.get("content") if isinstance(output.get("content"),list) else []:
                if not isinstance(content,dict):continue
                text=content.get("text")
                if isinstance(text,str):texts.append(text)
    else:
        choices=payload.get("choices") if isinstance(payload.get("choices"),list) else []
        message=(choices[0].get("message") if choices and isinstance(choices[0],dict) else {})
        if isinstance(message,dict):
            text=content_text(message.get("content"))
            if text:texts.append(text)
    citations=parse_openai_citations(payload,provider_type)
    generated=clean_generated_text(texts)
    results=[]; seen=set()
    for citation in citations:
        if isinstance(citation,str):url=citation; title="外部引用"
        elif isinstance(citation,dict):
            nested=citation.get("url_citation") if isinstance(citation.get("url_citation"),dict) else citation
            url=str(nested.get("url") or nested.get("link") or ""); title=str(nested.get("title") or nested.get("name") or "外部引用")
        else:continue
        if url in seen:continue
        result=clean_result(title,url,generated[:1200],True)
        if result and result.url:results.append(result); seen.add(result.url)
    for url in _URL_RE.findall(generated):
        clean=url.rstrip(".,;:!?，。；：！？")
        if clean in seen:continue
        result=clean_result("模型返回的外部引用",clean,generated[:1200],True)
        if result and result.url:results.append(result); seen.add(result.url)
    if not results and generated:
        results=[SearchResult(f"{model} 联网结果", "", generated[:3000], True)]
    prompt,completion,total=parse_openai_usage(payload)
    limit=int(max_results)
    return SearchResponse(results[:limit],generated_text=generated,cited=any(item.url for item in results),
                          model=str(payload.get("model") or model),prompt_tokens=prompt,
                          completion_tokens=completion,total_tokens=total)


def redact_key_echo(response:SearchResponse,key:str)->SearchResponse:
    """不信任服务返回内容；即使中转回显请求 Key，也不能让它进入缓存或 Prompt。"""
    secret=str(key or "")
    if not secret:return response
    def clean(value:str)->str:return str(value or "").replace(secret,"[REDACTED]")
    results=[SearchResult(
        clean(item.title),"" if secret in item.url else item.url,clean(item.snippet),item.provider_generated,
        item.weak,
    ) for item in response.results]
    return SearchResponse(
        results,response.provider_id,response.provider_type,clean(response.generated_text),
        bool(response.cited and any(item.url for item in results)),
        clean(response.model),response.prompt_tokens,response.completion_tokens,response.total_tokens,
    )
