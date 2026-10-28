"""联网搜索的共享结果模型与后端异常。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    title:str
    url:str
    snippet:str
    provider_generated:bool=False
    # 弱相关标记：由 Playwright 解析侧填充，语义=该条目标题/摘要与查询词没有实质词元重叠。
    # API 路线暂不填充，保持默认 False。消费方（news 入库过滤、工具层"搜不到"信号）据此判断。
    weak:bool=False


@dataclass(frozen=True)
class SearchResponse:
    results:list[SearchResult]
    provider_id:str=""
    provider_type:str=""
    generated_text:str=""
    cited:bool=False
    model:str=""
    prompt_tokens:int=0
    completion_tokens:int=0
    total_tokens:int=0


class SearchBackendError(RuntimeError):
    """搜索后端失败的结构化异常；消息不得包含查询词、Key 或页面正文。"""

    def __init__(self,message:str,*,error_class:str="network")->None:
        super().__init__(message)
        self.error_class=error_class
