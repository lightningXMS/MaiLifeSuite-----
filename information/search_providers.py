"""搜索 Provider 策略接口与注册表：Playwright 优先、API 备援。"""
from __future__ import annotations

from abc import ABC,abstractmethod
from typing import Any

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchBackendError,SearchResponse
from .search_parsing import redact_key_echo


class SearchAttemptError(Exception):
    """编排层统一失败载体：携带错误类别、状态码、响应头和是否惩罚 Key 的标记。"""

    def __init__(self,message:str,*,error_class:str,status_code:int=0,
                 headers:dict[str,str]|None=None,penalize:bool=True)->None:
        super().__init__(message)
        self.error_class=error_class
        self.status_code=status_code
        self.headers=headers or {}
        self.penalize=penalize


class SearchProvider(ABC):
    """联网搜索 Provider 策略：负责配置校验、运行时条目与单次请求执行。"""

    def __init__(self,service:Any)->None:
        self.service=service

    @staticmethod
    @abstractmethod
    def matches(provider_type:str)->bool:
        """判断该策略是否覆盖此 Provider 类型。"""

    @abstractmethod
    def validate(self,provider:Any)->str:
        """返回配置错误类别；空串表示配置通过。"""

    @abstractmethod
    def fingerprints(self,provider:Any)->list[str]:
        """返回需要运行时管理的条目（Key 指纹或 "browser"）。"""

    @abstractmethod
    def available(self,provider:Any,runtimes:dict[str,dict[str,Any]],now:float)->list[tuple[str,str]]:
        """健康检查与冷却去重：返回本次可尝试的 (fingerprint, key) 队列。"""

    @abstractmethod
    async def attempt(self,provider_id:str,provider:Any,fingerprint:str,key:str,
                      query:str,freshness:str)->SearchResponse:
        """执行单次请求；失败时抛 SearchAttemptError 交由编排层统一处理。"""


class PlaywrightProvider(SearchProvider):
    """浏览器搜索策略：无 Key、单条目 "browser"，空结果与内部异常不惩罚。"""

    # 验证码拦截与浏览器侧故障不冷却浏览器条目，仅网络与超时惩罚。
    _NO_PENALTY=frozenset({"blocked","browser_error","playwright_unavailable","internal"})

    @staticmethod
    def matches(provider_type:str)->bool:
        return provider_type=="playwright"

    def validate(self,provider:Any)->str:
        return ""

    def fingerprints(self,provider:Any)->list[str]:
        return ["browser"]

    def available(self,provider:Any,runtimes:dict[str,dict[str,Any]],now:float)->list[tuple[str,str]]:
        runtime=runtimes.get("browser",{})
        status=str(runtime.get("status") or "healthy")
        cooldown=float(runtime.get("cooldown_until") or 0)
        if status=="disabled" or cooldown>now:return []
        return [("browser","")]

    async def attempt(self,provider_id:str,provider:Any,fingerprint:str,key:str,
                      query:str,freshness:str)->SearchResponse:
        client=self.service._browser_client(provider_id,provider)
        try:
            parsed=await client.search(
                query,engine=str(provider.browser_engine),freshness=freshness,
                timeout_seconds=float(self.service.config.search_api.timeout_seconds),
                max_results=int(self.service.config.search_api.max_results),
            )
        except SearchBackendError as exc:
            error_class=str(exc.error_class or "network")
            raise SearchAttemptError(str(exc),error_class=error_class,
                penalize=error_class not in self._NO_PENALTY) from exc
        except Exception:
            raise SearchAttemptError("浏览器搜索内部异常",error_class="internal",penalize=False)
        if not parsed.results:
            raise SearchAttemptError("浏览器搜索未返回结果",error_class="empty_result",penalize=False)
        return SearchResponse(parsed.results,provider_id,"playwright",parsed.generated_text,
                              parsed.cited,parsed.model,parsed.prompt_tokens,
                              parsed.completion_tokens,parsed.total_tokens)


class ApiProvider(SearchProvider):
    """API 搜索策略：管理 Key 级健康与冷却，区分鉴权/限流/配额与服务故障。"""

    @staticmethod
    def matches(provider_type:str)->bool:
        return provider_type!="playwright"

    def validate(self,provider:Any)->str:
        kind=str(provider.provider_type)
        if kind.startswith("openai_"):
            if not str(provider.endpoint).strip() or not str(provider.model).strip():
                return "invalid_config"
            # 自定义 endpoint 只允许公网：字面量内网/环回/云元数据地址直接判配置错误，
            # 不发起请求、不惩罚 Key；域名解析到内网的 rebinding 由请求时 public_only 固定 IP 兜底。
            try:
                if HttpClient.private_host_reason(str(provider.endpoint)):
                    return "unsafe_endpoint"
            except HttpRequestError:
                return "invalid_config"
        return ""

    def fingerprints(self,provider:Any)->list[str]:
        return [self.service.key_fingerprint(key) for key in provider.api_keys]

    def available(self,provider:Any,runtimes:dict[str,dict[str,Any]],now:float)->list[tuple[str,str]]:
        result=[]
        for key in provider.api_keys:
            fingerprint=self.service.key_fingerprint(key)
            runtime=runtimes.get(fingerprint,{})
            status=str(runtime.get("status") or "healthy")
            cooldown=float(runtime.get("cooldown_until") or 0)
            if status=="disabled":continue
            # 服务级故障时整组 Key 冷却，直接切换到下一个 Provider，不再尝试备用 Key。
            if status=="service_error" and cooldown>now:break
            if cooldown>now:continue
            result.append((fingerprint,key))
        return result

    async def attempt(self,provider_id:str,provider:Any,fingerprint:str,key:str,
                      query:str,freshness:str)->SearchResponse:
        kind=str(provider.provider_type)
        try:
            parsed=await self.service._request_provider(provider,key,query,freshness)
        except HttpRequestError as exc:
            # 非公网地址是配置错误而非 Key 故障：记录但不禁用/冷却 Key，避免误伤可用 Key。
            config_error=exc.error_class in {"unsafe_url","invalid_url"}
            quota=self.service._quota_error(exc)
            raise SearchAttemptError(
                "API 请求失败",error_class="quota" if quota else exc.error_class,
                status_code=exc.status_code,headers=exc.headers,penalize=not config_error,
            ) from exc
        except Exception:
            raise SearchAttemptError("API 请求内部异常",error_class="internal",penalize=False)
        parsed=redact_key_echo(parsed,key)
        if not parsed.results:
            raise SearchAttemptError("服务未返回结果",error_class="empty_result",
                                     status_code=200,penalize=False)
        return SearchResponse(parsed.results,provider_id,kind,parsed.generated_text,parsed.cited,
                              parsed.model,parsed.prompt_tokens,parsed.completion_tokens,parsed.total_tokens)


PROVIDER_STRATEGIES:tuple[type[SearchProvider],...]=(PlaywrightProvider,ApiProvider)


def get_provider_strategy(provider_type:str)->type[SearchProvider]:
    """按注册表顺序解析 Provider 类型对应的策略；未匹配时回退到 API 策略。"""
    for strategy_cls in PROVIDER_STRATEGIES:
        if strategy_cls.matches(provider_type):
            return strategy_cls
    return ApiProvider
