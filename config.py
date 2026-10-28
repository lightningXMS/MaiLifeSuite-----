"""MaiLifeSuite（麦麦生活套件）合并配置模型。

由两个上游插件的配置模型合并而成：
- Mai_life（麦生活）的 20 个配置段原样保留（见 mailife_config.py）；
- MaiTrace（麦时迹）的 14 个配置段经 maitrace.config 引入；
- 三个同名段（[plugin] / [schedule] / [environment]）做字段级并集：
  - [plugin]      = Mai_life PluginSettings（config_version/enabled/llm_model/admin_user_ids）
                    + MaiTrace PluginSection 的空间登录与 /zn 管理员字段（去掉重复的 config_version/enabled）
  - [schedule]    = MaiTrace ScheduleSection（完整日程配置，provider 默认指向麦生活）
                    + Mai_life 的 generate_hour/detail_lead_minutes/template_file（无字段冲突）
  - [environment] = Mai_life EnvironmentSettings（时区/城市/天气）
                    + MaiTrace 的 calendar_enabled（本地历法开关；无字段冲突）

注意：[environment].timezone 与 [schedule].timezone 是两个独立字段
（麦生活与麦时迹各自读取），请保持一致；后续版本计划收敛为单一字段。
"""

from __future__ import annotations

from typing import ClassVar, List, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

from .mailife_config import (
    CONFIG_SCHEMA_VERSION,
    PLUGIN_VERSION,  # noqa: F401  # 麦生活内部模块从根 config 导入该符号，保持兼容
    AnnounceSettings,
    ContextSettings,
    CreationSettings,
    DebounceSettings,
    EnvironmentSettings,
    InformationSettings,
    LinkageSettings,
    MemorySettings,
    ModelRoutingSettings,
    NewsSettings,
    PluginSettings,
    ProactiveSettings,
    RecallSettings,
    RestGateSettings,
    ScheduleSettings,
    SearchAPISettings,
    SearchSettings,
    SocialSettings,
    StateSettings,
    UsageSettings,
    UsersSettings,
    _ui,
)
from .maitrace.config.sections_proactive import RoutineSection
from .maitrace.config.sections_qzone import (
    ImageSection,
    LLMSection,
    MonitorSection,
    PersonaSection,
    ReadSection,
    SendSection,
)
from .maitrace.config.sections_qzone import (
    PluginSection as TracePluginSection,
)
from .maitrace.services.life.config import (
    LifeSection,
    MessagingSection,
)
from .maitrace.services.life.config import (
    EnvironmentSection as TraceEnvironmentSection,
)
from .maitrace.services.schedule.config import (
    GoalCleanupSection,
    InjectSection,
)
from .maitrace.services.schedule.config import (
    ScheduleSection as TraceScheduleSection,
)
from .maitrace.utils.config_validation import qq_list


class SuitePluginSection(PluginSettings):
    """合并 [plugin]：麦生活总开关/管理员 + MaiTrace 空间登录与 /zn 管理员。

    enabled 对合并后的单一插件生效；config_version 沿用麦生活的
    SDK 必需字段，MaiTrace 的 '6.3.0' 版本字段不再保留。
    """

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    admin_qq: List[str] = Field(
        default_factory=list,
        description="所有 /zn 命令的管理员 QQ 列表（空间与日程命令），包括 help；空列表禁用命令。与空间 Tool 权限、admin_user_ids 互相独立。",
        json_schema_extra=_ui(
            "/zn 管理员 QQ", "麦时迹命令系统（/zn）的管理员列表；与上方管理员 QQ 列表（麦生活指令）独立配置。", 3,
            label_en="zn Administrators", hint_en="Administrators for /zn commands (space and schedule). Independent from admin_user_ids.",
            placeholder='["123456"]',
        ),
    )
    adapter_priority: List[str] = Field(
        default_factory=list,
        description="QQ 账号识别、SnowLuma 直发和 adapter Cookie 获取的适配器优先级；空列表自动探测。可填完整 plugin_id 或后缀，如 snowluma-adapter。",
        json_schema_extra=_ui(
            "适配器优先级", "空间功能使用的 QQ 适配器优先级；空列表自动探测。", 4,
            label_en="Adapter Priority", hint_en="Adapter priority for QQ account and cookie detection.",
            placeholder='["snowluma-adapter"]',
        ),
    )
    cookie_methods: List[Literal["adapter", "napcat", "clientkey", "qrcode", "local"]] = Field(
        default_factory=lambda: ["adapter", "napcat", "clientkey", "qrcode", "local"],
        description="QQ 空间 Cookie 获取方式：adapter=适配器 API，napcat=OneBot HTTP，clientkey=本机 QQ，qrcode=扫码，local=缓存。前几项按近期成功率重排；空列表使用默认全部方式，刷新失败仍尝试本地缓存。",
        json_schema_extra=_ui(
            "Cookie 获取顺序", "QQ 空间登录凭据的获取方式与顺序。", 5,
            label_en="Cookie Methods", hint_en="How QQ Zone cookies are obtained.",
        ),
    )
    http_host: str = Field(
        default="127.0.0.1",
        description="napcat Cookie 方式使用的 OneBot HTTP 主机；填写主机名或 IP，不带协议、端口或路径。adapter 获取和 SnowLuma 直发不使用此项。",
        json_schema_extra=_ui(
            "Cookie HTTP 主机", "仅 napcat 方式使用。", 6,
            label_en="Cookie HTTP Host", hint_en="OneBot HTTP host for napcat cookie mode.",
            placeholder="127.0.0.1",
        ),
    )
    http_port: str = Field(
        default="9999",
        description="napcat Cookie 方式使用的 OneBot HTTP 端口，半角数字字符串，范围 1–65535；与适配器 HTTP 服务一致。",
        json_schema_extra=_ui(
            "Cookie HTTP 端口", "仅 napcat 方式使用。", 7,
            label_en="Cookie HTTP Port", hint_en="OneBot HTTP port for napcat cookie mode.",
            placeholder="9999",
        ),
    )
    http_token: str = Field(
        default="",
        description="napcat Cookie 方式使用的 HTTP 认证 Token；适配器未设置认证时留空。",
        json_schema_extra=_ui(
            "Cookie HTTP Token", "适配器启用 HTTP 认证时填写。", 8,
            label_en="Cookie HTTP Token", hint_en="HTTP access token for napcat cookie mode.",
        ),
    )

    @field_validator("admin_qq")
    @classmethod
    def _validate_admin_qq(cls, values: List[str]) -> List[str]:
        return qq_list(values)

    @field_validator("http_host")
    @classmethod
    def _validate_http_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(part in value for part in ("/", "?", "#", " ")):
            raise ValueError("HTTP 主机必须为主机名或 IP，不带协议、路径或空格")
        return value

    @field_validator("http_port")
    @classmethod
    def _validate_http_port(cls, value: str) -> str:
        value = value.strip()
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
            raise ValueError("HTTP 端口必须为 1 至 65535 的数字字符串")
        return value


class SuiteScheduleSection(TraceScheduleSection):
    """合并 [schedule]：MaiTrace 完整日程配置 + 麦生活框架生成参数。

    provider 默认 'maibot-community.mai-life'：合并插件内由进程内桥接
    直接读取麦生活框架（schedule/maitrace 桥），不再经过跨插件 RPC。
    改为 'internal' 时回退 MaiTrace 内置日程引擎（与麦生活框架并存，
    不建议同时启用两者的生成）。
    """

    __ui_label__: ClassVar[str] = "日程与约定"
    __ui_order__: ClassVar[int] = 11

    generate_hour: int = Field(
        default=3, ge=0, le=23,
        description="麦生活每日强制重新生成生活框架的小时。",
        json_schema_extra=_ui(
            "生活框架生成时间（小时）", "0～23 的整数。默认凌晨 3 点；缺少当日日程时仍会立即生成。", 96,
            label_en="Framework Generation Hour", hint_en="Hour 0-23 for regenerating the daily life framework.",
        ),
    )
    detail_lead_minutes: int = Field(
        default=60, ge=10, le=240,
        description="麦生活提前多少分钟细化即将开始的生活场景。",
        json_schema_extra=_ui(
            "场景提前细化时间（分钟）", "默认提前 60 分钟细化当前段和下一段。", 97,
            label_en="Scene Detail Lead Time (min)", hint_en="Minutes before a segment starts to expand its scene.",
        ),
    )
    template_file: str = Field(
        default="mai_template.json",
        description="麦生活日程骨架模板文件名，相对于插件目录。LLM 失败或返回非法日程时使用该模板降级。",
        json_schema_extra=_ui(
            "日程模板文件", "麦生活框架生成的降级模板。", 98,
            label_en="Schedule Template File", hint_en="Fallback template for framework generation.",
            placeholder="mai_template.json",
        ),
    )
    generation_retry_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        description="麦生活每日日程生成失败后的重试间隔（分钟）。",
        json_schema_extra=_ui(
            "生成重试间隔（分钟）", "首次生成失败后每隔该时间自动重试一次；重启后到期的补偿只执行一次。", 99,
            label_en="Generation Retry Interval (min)", hint_en="Wait between failed schedule generation attempts.",
        ),
    )
    generation_max_retries: int = Field(
        default=3, ge=0, le=20,
        description="麦生活每日日程生成失败后的最大额外重试次数（不含首次）。达上限后用本地历史归档随机兜底。",
        json_schema_extra=_ui(
            "生成最大重试次数", "默认 3 表示单轮最多 4 次尝试；0 表示失败后立即进入兜底。", 100,
            label_en="Generation Max Retries", hint_en="Extra attempts after the first try before archived templates are used.",
        ),
    )
    generation_template_retention_days: int = Field(
        default=30, ge=1, le=3650,
        description="麦生活成功生成的日程归档保留天数，按归档时间计算，超期自动清理。",
        json_schema_extra=_ui(
            "日程归档保留天数", "只保留最近 N 天内归档的成功日程模板；随机兜底只使用未过期归档。", 101,
            label_en="Archive Retention Days", hint_en="Keep successfully generated schedule archives for this many days.",
        ),
    )


class SuiteEnvironmentSection(EnvironmentSettings):
    """合并 [environment]：麦生活时区/城市/天气 + MaiTrace 本地历法开关。

    时区读取方：麦生活各服务读 environment.timezone；麦时迹日程读
    schedule.timezone。两个字段需保持一致。
    """

    __ui_label__: ClassVar[str] = "环境与天气"
    __ui_order__: ClassVar[int] = 2

    calendar_enabled: bool = Field(
        default=True,
        description="生活环境快照提供中国节假日、农历和节气；需 chinese-calendar、lunar-python 依赖。关闭后仍有配置时区的日期时间。",
        json_schema_extra=_ui(
            "中国节假日与农历", "麦时迹本地历法开关；麦生活的环境服务始终提供历法。", 9,
            label_en="Chinese Calendar", hint_en="Chinese holidays, lunar dates and solar terms in snapshots.",
        ),
    )


class MaiLifeSuiteSettings(PluginConfigBase):
    """MaiLifeSuite 完整配置：麦生活 20 段 + 麦时迹 14 段（3 段合并为字段并集）。"""

    # ── 合并段 ──
    plugin: SuitePluginSection = Field(
        default_factory=SuitePluginSection,
        json_schema_extra=_ui("插件设置", "总开关、模型、管理员与空间登录。", 0,
                              label_en="Plugin Settings", hint_en="Switch, models, administrators and QQ Zone login."),
    )
    schedule: SuiteScheduleSection = Field(
        default_factory=SuiteScheduleSection,
        json_schema_extra=_ui("日程与约定", "生活框架生成、场景细化、日程生成与临时约定。", 11,
                              label_en="Schedule", hint_en="Life framework, scenes, schedules and commitments."),
    )
    environment: SuiteEnvironmentSection = Field(
        default_factory=SuiteEnvironmentSection,
        json_schema_extra=_ui("环境与天气", "时区、城市、天气刷新与本地历法。", 2,
                              label_en="Environment", hint_en="Timezone, city, weather and local calendar."),
    )

    # ── 麦生活独有段（原样保留） ──
    users: UsersSettings = Field(
        default_factory=UsersSettings,
        json_schema_extra=_ui("私聊用户", "配置允许互动和主动私聊的用户。", 1,
                              label_en="Private Users", hint_en="Users allowed to interact."),
    )
    state: StateSettings = Field(
        default_factory=StateSettings,
        json_schema_extra=_ui("生活状态", "精力、饥饿、健康和周期。", 3,
                              label_en="Life State", hint_en="Energy, hunger, health and body cycle."),
    )
    memory: MemorySettings = Field(
        default_factory=MemorySettings,
        json_schema_extra=_ui("生活记忆", "梦境碎片、抽象日记和重要日期。", 4,
                              label_en="Life Memory", hint_en="Dream fragments, diary and important dates."),
    )
    rest_gate: RestGateSettings = Field(
        default_factory=RestGateSettings,
        json_schema_extra=_ui("休息回复闸门", "睡眠和午休期间的判醒逻辑。", 5,
                              label_en="Rest Reply Gate", hint_en="Wake decisions during sleep and naps."),
    )
    context: ContextSettings = Field(
        default_factory=ContextSettings,
        json_schema_extra=_ui("被动回复增强", "关系、状态、意图和连续话题。", 6,
                              label_en="Passive Reply Context", hint_en="Relationship, state, intent and continuity."),
    )
    debounce: DebounceSettings = Field(
        default_factory=DebounceSettings,
        json_schema_extra=_ui("消息收口防抖", "私聊与群聊独立收口及同轮回复防重。", 7,
                              label_en="Message Debounce", hint_en="Private/group debounce and duplicate-reply protection."),
    )
    recall: RecallSettings = Field(
        default_factory=RecallSettings,
        json_schema_extra=_ui("撤回增强", "撤回通知取消回复与可选本人摘要。", 8,
                              label_en="Recall Handling", hint_en="Cancel replies for recalled messages."),
    )
    proactive: ProactiveSettings = Field(
        default_factory=ProactiveSettings,
        json_schema_extra=_ui("主动私聊", "公共冷却和评分；每日额度在每个私聊用户中设置。", 12,
                              label_en="Proactive Private Chat", hint_en="Shared cooldown and scoring."),
    )
    information: InformationSettings = Field(
        default_factory=InformationSettings,
        json_schema_extra=_ui("联网见闻", "公共开关、上下文和自我关联。", 13,
                              label_en="Connected Discovery", hint_en="Shared switches, context and self-association."),
    )
    search_api: SearchAPISettings = Field(
        default_factory=SearchAPISettings,
        json_schema_extra=_ui("联网搜索服务", "博查、Tavily、You.com 与自定义联网中转的有序降级链。", 14,
                              label_en="Search API Providers", hint_en="Ordered failover for web-search providers."),
    )
    news: NewsSettings = Field(
        default_factory=NewsSettings,
        json_schema_extra=_ui("新闻阅读", "按兴趣低频调用统一搜索 API。", 15,
                              label_en="News Reading", hint_en="Low-frequency interest-based reading."),
    )
    search: SearchSettings = Field(
        default_factory=SearchSettings,
        json_schema_extra=_ui("主动搜索", "在空闲日程中使用统一搜索 API 探索。", 16,
                              label_en="Proactive Search", hint_en="Explore via search API during free time."),
    )
    social: SocialSettings = Field(
        default_factory=SocialSettings,
        json_schema_extra=_ui("社交转述", "QQ群号白名单、显式转述和群转私边界。", 17,
                              label_en="Social Relay", hint_en="QQ group allowlists and relay boundaries."),
    )
    creation: CreationSettings = Field(
        default_factory=CreationSettings,
        json_schema_extra=_ui("书柜与创作", "作品、日记、阅读批注和分阶段创作。", 18,
                              label_en="Bookshelf and Creation", hint_en="Works, diaries and staged creation."),
    )
    models: ModelRoutingSettings = Field(
        default_factory=ModelRoutingSettings,
        json_schema_extra=_ui("模型与成本编排", "基础任务路由和高级覆盖。", 19,
                              label_en="Model Routing", hint_en="Base task routes and per-task overrides."),
    )
    usage: UsageSettings = Field(
        default_factory=UsageSettings,
        json_schema_extra=_ui("Token 监控", "调用次数、Token、耗时和失败统计。", 20,
                              label_en="Token Monitoring", hint_en="Calls, tokens, latency and failure statistics."),
    )
    linkage: LinkageSettings = Field(
        default_factory=LinkageSettings,
        json_schema_extra=_ui("插件联动", "与其它插件的协作开关；目标插件未安装时不生效。", 21,
                              label_en="Plugin Linkage", hint_en="Collaboration switches."),
    )
    announce: AnnounceSettings = Field(
        default_factory=AnnounceSettings,
        json_schema_extra=_ui("活动播报", "把当前活动模板化播报到指定群聊/私聊；默认关闭。", 22,
                              label_en="Activity Announce", hint_en="Template-based announce to groups/private chats; off by default."),
    )

    # ── 麦时迹独有段（原样保留） ──
    persona: PersonaSection = Field(default_factory=PersonaSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    inject: InjectSection = Field(default_factory=InjectSection)
    goal_cleanup: GoalCleanupSection = Field(default_factory=GoalCleanupSection)
    life: LifeSection = Field(default_factory=LifeSection)
    messaging: MessagingSection = Field(default_factory=MessagingSection)
    send: SendSection = Field(default_factory=SendSection)
    image: ImageSection = Field(default_factory=ImageSection)
    read: ReadSection = Field(default_factory=ReadSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    routine: RoutineSection = Field(default_factory=RoutineSection)


__all__ = [
    "MaiLifeSuiteSettings",
    "SuitePluginSection",
    "SuiteScheduleSection",
    "SuiteEnvironmentSection",
    "CONFIG_SCHEMA_VERSION",
]
