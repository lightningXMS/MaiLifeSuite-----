"""进程内直连的麦生活日程来源（合并插件专用）。

MaiLifeScheduleSource 通过跨插件 RPC（ctx.call_capability('api.call')）读取
独立安装的 Mai_life 插件；在 MaiLifeSuite 合并插件中，麦生活的日程 API 方法
就在同一个插件实例上，因此本类仅覆写 ``_call``：直接调用宿主 Mixin 的
``api_schedule_provider_info`` / ``api_schedule_snapshot`` /
``api_schedule_timeline`` 三个方法并解包业务信封。

协议、字段校验、缓存与格式映射全部复用父类 MaiLifeScheduleSource，
与 maibot.schedule v1 的契约保持一致（provider_id 仍为
maibot-community.mai-life，外部消费方无感知）。
"""

from __future__ import annotations

from typing import Any, Dict

from .mai_life import MaiLifeScheduleSource

_HANDLERS = {
    'get_schedule_provider_info': 'api_schedule_provider_info',
    'get_schedule_snapshot': 'api_schedule_snapshot',
    'get_schedule_timeline': 'api_schedule_timeline',
}


class MaiLifeLocalScheduleSource(MaiLifeScheduleSource):
    """把 maibot.schedule v1 的三个只读查询路由到同插件麦生活服务。"""

    async def _call(self, name: str, **args: Any) -> Dict[str, Any]:
        handler = getattr(self.plugin, _HANDLERS[name], None)
        if handler is None:
            raise RuntimeError(f'宿主缺少麦生活日程接口: {name}')
        envelope = await handler(**args)
        # 信封契约与跨插件形态一致：{success, error, data}；业务失败不抛异常而是 success=False。
        if not isinstance(envelope, dict) or envelope.get('success') is not True:
            error = envelope.get('error', '无效的日程信封') if isinstance(envelope, dict) else '无效的日程信封'
            raise RuntimeError(f'Mai_life(本地) {name} 查询失败: {error}')
        data = envelope.get('data')
        if not isinstance(data, dict):
            raise ValueError(f'Mai_life(本地) {name} 缺少 data 对象')
        return data
