"""Mai_life WebUI 配置模型。

所有分组和字段都提供中文标签、中文说明及英文 i18n，避免 WebUI 直接显示
``user_id``、``wake_probability`` 等内部字段名。
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

import re

from maibot_sdk import Field, PluginConfigBase
from pydantic import ValidationInfo, field_validator, model_validator

PLUGIN_VERSION = "1.23.7"
CONFIG_SCHEMA_VERSION = "1.12.0"
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _qq_id(value:Any,field_name:str)->str:
    text=str(value or "").strip()
    if text and not re.fullmatch(r"[0-9]+",text):raise ValueError(f"{field_name} 必须填写纯数字 QQ 号")
    return text


def _ui(
    label: str,
    hint: str,
    order: int,
    *,
    label_en: str = "",
    hint_en: str = "",
    placeholder: str | None = None,
    hidden: bool = False,
    disabled: bool = False,
    enum_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造 MaiBot WebUI 使用的字段元数据。"""
    zh: dict[str, str] = {"label": label, "hint": hint}
    en: dict[str, str] = {"label": label_en or label, "hint": hint_en or hint}
    result: dict[str, Any] = {
        "label": label,
        "hint": hint,
        "order": order,
        "i18n": {"zh_CN": zh, "en_US": en},
    }
    if placeholder is not None:
        result["placeholder"] = placeholder
        zh["placeholder"] = placeholder
        en["placeholder"] = placeholder
    if hidden:
        result["hidden"] = True
    if disabled:
        result["disabled"] = True
    if enum_labels:
        # 同时提供两种常见枚举翻译扩展，兼容不同 WebUI 渲染版本。
        result["enum_labels"] = enum_labels
        result["x-enumNames"] = [enum_labels[key] for key in enum_labels]
    return result


def _time(value: Any, default: str) -> str:
    """把非法时间恢复为安全默认值。"""
    text = str(value or "").strip()
    return text if _TIME_RE.fullmatch(text) else default


class PluginSettings(PluginConfigBase):
    """插件开关、模型与管理员配置。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Plugin Settings", "description": "Basic switch, model and administrator settings."}
    }

    # SDK 2.7+ 强制要求 plugin.config_version，用于配置补齐与迁移判定。
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 Schema 版本，请勿手动修改。",
        json_schema_extra=_ui(
            "配置版本", "由插件维护，请勿手动修改。", 99,
            label_en="Config Version", hint_en="Managed by the plugin. Do not edit manually.",
            hidden=True, disabled=True,
        ),
    )
    enabled: bool = Field(
        default=True,
        description="麦生活总开关。关闭后停止状态维护、日程生成和主动私聊。",
        json_schema_extra=_ui(
            "启用麦生活", "关闭后保留数据库，但停止所有后台模拟与主动行为。", 0,
            label_en="Enable Mai Life", hint_en="Keep data but stop simulation and proactive behavior when disabled.",
        ),
    )
    llm_model: str = Field(
        default="planner",
        description="旧版生活生成任务名；新配置优先使用“模型与成本编排”。",
        json_schema_extra=_ui(
            "兼容生活生成任务", "保留用于兼容旧配置；建议在“模型与成本编排”中设置。", 1,
            label_en="Life Generation Model", hint_en="Model used for schedules, scenes and dreams.",
        ),
    )
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="允许在私聊使用菜单、状态和管理诊断指令的 QQ 号列表。",
        json_schema_extra=_ui(
            "管理员 QQ 列表", "管理员无需同时创建用户档案即可使用管理类指令；关系、撤回摘要和重要日期等个人功能仍需启用用户档案。留空时，第一个有效私聊用户自动成为管理员。", 2,
            label_en="Administrator QQ IDs", hint_en="Administrators can use management commands without a user profile; relationship and personal-record commands still require an enabled profile. If empty, the first valid private user becomes administrator.",
            placeholder="123456789",
        ),
    )

    @field_validator("config_version", mode="before")
    @classmethod
    def normalize_config_version(cls, value: Any) -> str:
        # 只校正框架必需的版本号，其他旧配置值仍由 SDK 默认补齐，不重写用户资料。
        del value
        return CONFIG_SCHEMA_VERSION

    @field_validator("admin_user_ids",mode="before")
    @classmethod
    def normalize_admin_ids(cls,value:Any)->list[str]:
        values=value if isinstance(value,list) else []
        result=[]
        for item in values:
            user_id=_qq_id(item,"管理员 QQ")
            if user_id and user_id not in result:result.append(user_id)
        return result


class UserProfile(PluginConfigBase):
    """单个私聊网友的关系和免打扰设置。"""

    __ui_label__: ClassVar[str] = "私聊用户资料"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Private User Profile", "description": "Per-user relationship and quiet-hour settings."}
    }

    user_id: str = Field(
        default="",
        description="QQ 用户 ID。",
        json_schema_extra=_ui(
            "QQ 号", "填写允许进入麦生活系统的 QQ 号。", 0,
            label_en="QQ User ID", hint_en="QQ account allowed to use Mai Life.", placeholder="123456789",
        ),
    )
    enabled: bool = Field(
        default=True,
        description="是否记录该用户的互动和关系状态。",
        json_schema_extra=_ui(
            "启用用户档案", "关闭后不记录该用户互动，也不会注入生活上下文。", 1,
            label_en="Enable User Profile", hint_en="Disable to stop relationship tracking and context injection for this user.",
        ),
    )
    proactive_enabled: bool = Field(
        default=True,
        description="是否允许麦麦向该用户主动发起私聊。",
        json_schema_extra=_ui(
            "允许主动私聊", "关闭后仍可正常被动聊天，但麦麦不会主动找这个用户。", 2,
            label_en="Allow Proactive Chat", hint_en="Passive chat remains available, but Mai will not proactively message this user.",
        ),
    )

    role: Literal["owner", "friend"] = Field(
        default="friend",
        description="该私聊用户在麦麦关系中的权限角色。",
        json_schema_extra=_ui(
            "关系角色", "最多只能有一个主人；朋友不会获得主人专属上下文和敏感能力。", 3,
            label_en="Relationship Role", hint_en="At most one owner is allowed. Friends never receive owner-only context.",
            enum_labels={"owner": "主人（owner）", "friend": "朋友（friend）"},
        ),
    )
    daily_proactive_max: int = Field(
        default=1, ge=0, le=20,
        description="该用户每天最多实际收到的主动消息次数（硬上限，不是目标次数）。",
        json_schema_extra=_ui(
            "用户每日主动上限", "每日最多主动发送 N 次（硬上限，非目标次数）。实际次数常少于该值：受免打扰、冷却、精力、候选评分和 Planner 自主决策约束。0 表示禁止主动消息；主人通常填 2，朋友通常填 1。", 4,
            label_en="Per-user Daily Proactive Limit", hint_en="Hard cap of proactive messages per day (not a target). Actual count is often lower due to quiet hours, cooldown, energy, scoring and Planner decisions. Zero disables proactive messages.",
        ),
    )
    initial_temperature: int = Field(
        default=30, ge=0, le=100,
        description="首次创建用户档案时使用的关系温度。",
        json_schema_extra=_ui(
            "初始关系温度", "仅首次创建档案时使用。30 表示“认识”，不会自动进入恋爱语气。", 6,
            label_en="Initial Relationship Temperature", hint_en="Used only when the profile is first created. Default 30 means acquaintance.",
        ),
    )
    quiet_start: str = Field(
        default="00:00",
        description="该用户免打扰时段开始时间。",
        json_schema_extra=_ui(
            "免打扰开始", "此时间后不向该用户主动发消息，支持跨午夜时段。格式 HH:MM。", 7,
            label_en="Quiet Hours Start", hint_en="No proactive messages after this time. HH:MM format.", placeholder="00:00",
        ),
    )
    quiet_end: str = Field(
        default="08:00",
        description="该用户免打扰时段结束时间。",
        json_schema_extra=_ui(
            "免打扰结束", "到达此时间后恢复主动消息。格式 HH:MM。", 8,
            label_en="Quiet Hours End", hint_en="Proactive messaging resumes after this time. HH:MM format.", placeholder="08:00",
        ),
    )
    group_to_private_enabled: bool = Field(
        default=False,
        description="是否允许把白名单群聊中的公开话题低频转述给该用户。",
        json_schema_extra=_ui(
            "允许群聊转私聊", "朋友默认关闭；主人还需启用“社交转述”中的主人群转私开关。", 9,
            label_en="Allow Group-to-private Sharing",
            hint_en="Disabled for friends by default. Owner sharing also requires the social relay owner switch.",
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_daily_limit(cls, value: Any) -> Any:
        """把 v1.6 的 -1 角色默认值迁移为明确数字。"""
        if not isinstance(value,dict):return value
        data=dict(value)
        try:configured=int(data.get("daily_proactive_max",1))
        except (TypeError,ValueError):configured=1
        if configured<0:data["daily_proactive_max"]=2 if str(data.get("role") or "friend")=="owner" else 1
        return data

    @model_validator(mode="before")
    @classmethod
    def default_owner_group_to_private(cls, value: Any) -> Any:
        """主人档案未单独配置群转私开关时默认允许，保持旧版“全局开关即可”的行为。

        群转私对主人是“全局开关 AND 档案开关”；只有显式关闭档案开关的主人才被排除，
        避免既有部署在升级后静默失去主人群转私。朋友仍默认关闭。
        """
        if not isinstance(value,dict):return value
        if str(value.get("role") or "friend")!="owner":return value
        if "group_to_private_enabled" in value:return value
        data=dict(value); data["group_to_private_enabled"]=True
        return data

    @field_validator("user_id", mode="before")
    @classmethod
    def normalize_user_id(cls, value: Any) -> str:
        return _qq_id(value,"私聊用户")

    @field_validator("quiet_start", mode="before")
    @classmethod
    def validate_quiet_start(cls, value: Any) -> str:
        return _time(value, "00:00")

    @field_validator("quiet_end", mode="before")
    @classmethod
    def validate_quiet_end(cls, value: Any) -> str:
        return _time(value, "08:00")


class UsersSettings(PluginConfigBase):
    """允许进入陪伴系统的私聊用户列表。"""

    __ui_label__: ClassVar[str] = "私聊用户"
    __ui_order__: ClassVar[int] = 1
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Private Users", "description": "Users allowed to share Mai's global life timeline."}
    }

    profiles: list[UserProfile] = Field(
        default_factory=list,
        description="允许进入麦生活系统的私聊用户。",
        json_schema_extra=_ui(
            "私聊用户列表", "麦生活状态全局共享，但每位用户的关系、额度和免打扰相互独立。", 0,
            label_en="Private User Profiles", hint_en="Life state is global while relationship, quota and quiet hours are per user.",
        ),
    )
    @model_validator(mode="after")
    def only_one_owner(self) -> "UsersSettings":
        user_ids=[profile.user_id for profile in self.profiles if profile.user_id]
        if len(user_ids)!=len(set(user_ids)):
            raise ValueError("私聊用户 QQ 号不能重复")
        owners = [profile.user_id for profile in self.profiles if profile.enabled and profile.role == "owner"]
        if len(owners) > 1:
            raise ValueError("私聊用户中最多只能配置一个主人")
        return self


class SocialGroupProfile(PluginConfigBase):
    """社交观察和转述使用的 QQ 群白名单。"""

    __ui_label__: ClassVar[str] = "群聊白名单"

    group_id: str = Field(
        default="", description="QQ 群号。",
        json_schema_extra=_ui("QQ群号", "SnowLuma 与 NapCat 均填写真实 QQ 群号字符串。", 0,
                              label_en="QQ Group ID", hint_en="Real QQ group ID used by both SnowLuma and NapCat.",
                              placeholder="123456789"),
    )
    enabled: bool = Field(
        default=True, description="是否启用该群配置。",
        json_schema_extra=_ui("启用群配置", "关闭后该群既不观察也不作为转述目标。", 1,
                              label_en="Enable Group", hint_en="Disable all observation and relay behavior for this group."),
    )
    observe_enabled: bool = Field(
        default=False, description="是否观察该群的公开话题。",
        json_schema_extra=_ui("允许观察公开话题", "只保存短摘要，不保存群聊原文。", 2,
                              label_en="Observe Public Topics", hint_en="Store short summaries only, never raw group messages."),
    )
    relay_target_enabled: bool = Field(
        default=False, description="是否允许管理员向该群发起显式转述。",
        json_schema_extra=_ui("允许作为转述目标", "启用后主人或管理员可用 /麦麦转述 群QQ号 内容触发该群 Planner。", 3,
                              label_en="Allow Relay Target", hint_en="Allow owner/admin to trigger this group's Planner with the Chinese relay command."),
    )
    rest_gate_enabled: bool = Field(
        default=False, description="是否在该群启用夜间静音（休息闸门）。",
        json_schema_extra=_ui("启用夜间静音", "开启后，在「休息回复闸门→群聊闸门」配置的时间窗内，该群只有含强制唤醒词的消息会被回复，其余静默。需先在总开关启用群聊休息闸门。", 4,
                              label_en="Enable Group Night Silence", hint_en="During group quiet windows only force-wake terms get replies in this group."),
    )

    @field_validator("group_id", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _qq_id(value,"QQ群")

class SocialSettings(PluginConfigBase):
    """群聊观察、显式转述和群转私的共同边界。"""

    __ui_label__: ClassVar[str] = "社交转述"
    __ui_order__: ClassVar[int] = 16

    enabled: bool = Field(default=False, description="社交转述总开关。",
        json_schema_extra=_ui("启用社交转述", "默认关闭；只处理下方显式配置的 QQ 群。", 0,
                              label_en="Enable Social Relay", hint_en="Disabled by default and limited to configured QQ groups."))
    owner_group_to_private_enabled: bool = Field(default=True, description="允许向主人低频分享群聊公开话题。",
        json_schema_extra=_ui("允许向主人群转私", "仍需群观察开启、已知主人离群至少指定时长，并遵守主动额度。", 1,
                              label_en="Share Group Topics with Owner", hint_en="Requires observation, inactivity proof and proactive quota."))
    observation_wait_seconds: float = Field(default=3.0, ge=0.5, le=20, description="群聊话题收口等待。",
        json_schema_extra=_ui("群话题等待（秒）", "使用独立群缓冲，不复用私聊防抖参数。", 2,
                              label_en="Group Topic Wait (sec)", hint_en="Uses a separate group buffer from private debounce."))
    max_buffer_messages: int = Field(default=20, ge=2, le=100, description="单个群话题最多暂存消息数。",
        json_schema_extra=_ui("群缓冲消息上限", "只在内存中短暂保留，入库前会整理成摘要。", 3,
                              label_en="Group Buffer Limit", hint_en="Kept briefly in memory and summarized before storage."))
    inactivity_hours: float = Field(default=6.0, ge=1, le=168, description="群转私前用户需离群的最短时间。",
        json_schema_extra=_ui("离群时长（小时）", "无已知群活跃记录时不推断用户离群，避免误打扰。", 4,
                              label_en="Group Inactivity (hours)", hint_en="Unknown activity is not treated as proven absence."))
    group_share_daily_max: int = Field(default=1, ge=0, le=5, description="每用户每日群转私候选上限。",
        json_schema_extra=_ui("每日群转私上限", "该上限同时受用户主动消息总额度约束。", 5,
                              label_en="Daily Group-share Limit", hint_en="Also constrained by the user's total proactive quota."))
    group_share_min_interval_minutes: int = Field(default=360, ge=30, le=10080, description="群转私候选最小间隔。",
        json_schema_extra=_ui("群转私最小间隔（分钟）", "默认 6 小时，避免连续搬运群话题。", 6,
                              label_en="Group-share Interval (min)", hint_en="Default six hours to avoid repeated topic relays."))
    interesting_threshold: float = Field(default=0.65, ge=0, le=1, description="公开话题进入群转私候选的最低分。",
        json_schema_extra=_ui("公开话题阈值", "越高越克制；模型不可用时使用保守本地规则。", 7,
                              label_en="Public Topic Threshold", hint_en="Higher is more conservative; local fallback is used without a model."))
    summary_retention_hours: int = Field(default=72, ge=1, le=720, description="群聊短摘要保留时长。",
        json_schema_extra=_ui("群摘要保留（小时）", "到期自动删除，不保存群聊原文。", 8,
                              label_en="Group Summary Retention", hint_en="Expired summaries are deleted; raw messages are never stored."))
    relay_pending_seconds: int = Field(default=120, ge=30, le=600, description="显式转述与发送关联窗口。",
        json_schema_extra=_ui("转述确认窗口（秒）", "Planner 沉默或超时不会记为已发送。", 9,
                              label_en="Relay Confirmation Window", hint_en="Planner silence or expiry is not counted as sent."))
    groups: list[SocialGroupProfile] = Field(default_factory=list, description="QQ群白名单。",
        json_schema_extra=_ui("群聊白名单", "只填写真实 QQ 群号；群名称由 MaiBot 会话自动读取。", 10,
                              label_en="Group Allowlist", hint_en="Controls observation sources and explicit relay targets."))

    @model_validator(mode="after")
    def unique_group_ids(self) -> "SocialSettings":
        ids=[item.group_id for item in self.groups if item.group_id]
        if len(ids)!=len(set(ids)):raise ValueError("社交转述中的 QQ 群号不能重复")
        return self


class CreationSettings(PluginConfigBase):
    """书柜、阅读批注与分阶段创作设置。"""

    __ui_label__: ClassVar[str] = "书柜与创作"
    __ui_order__: ClassVar[int] = 17

    enabled: bool = Field(default=False, description="启用闲暇创作巡检。",
        json_schema_extra=_ui("启用书柜创作", "默认关闭；启用后仍需确认 SQLite 明文存储风险。", 0,
                              label_en="Enable Bookshelf Creation", hint_en="Disabled by default and requires plaintext-storage acknowledgement."))
    plaintext_storage_acknowledged: bool = Field(default=False, description="确认作品和私人文本以 SQLite 明文保存。",
        json_schema_extra=_ui("确认明文存储风险", "数据库文件权限由服务器管理员负责；未确认时不会启动创作和外部阅读。", 1,
                              label_en="Acknowledge Plaintext Storage", hint_en="Creation stays blocked until the server file-permission risk is acknowledged."))
    patrol_interval_minutes: int = Field(default=60, ge=10, le=1440, description="创作巡检间隔。",
        json_schema_extra=_ui("创作巡检间隔（分钟）", "默认每小时检查一次空档，不代表每次都会创作。", 2,
                              label_en="Creation Patrol Interval", hint_en="How often idle-time creation eligibility is checked."))
    daily_max: int = Field(default=1, ge=0, le=5, description="每日最多归档作品数。",
        json_schema_extra=_ui("每日作品上限", "默认 1；失败运行不计入已归档作品。", 3,
                              label_en="Daily Work Limit", hint_en="Maximum archived works per day; failed runs do not count."))
    minimum_energy: int = Field(default=35, ge=0, le=100, description="允许创作的最低精力。",
        json_schema_extra=_ui("创作最低精力", "精力不足时优先休息，不启动创作模型链。", 4,
                              label_en="Minimum Creation Energy", hint_en="Creation is skipped when Mai needs rest."))
    allowed_schedule_types: list[str] = Field(default_factory=lambda:["leisure","rest"], description="允许创作的日程类型。",
        json_schema_extra=_ui("允许创作的日程", "建议只保留 leisure 和 rest。", 5,
                              label_en="Allowed Schedule Types", hint_en="Usually leisure and rest only."))
    work_types: list[str] = Field(
        default_factory=lambda:["novel_fragment","poem","essay","screenplay","storyboard","character","worldbuilding"],
        description="允许生成的作品类型。",
        json_schema_extra=_ui("作品类型", "可选小说片段、诗、随笔、短剧、分镜、角色和世界观。", 6,
                              label_en="Work Types", hint_en="Allowed narrative and design formats."))
    public_works_enabled: bool = Field(default=True, description="允许无私密来源的作品公开给朋友。",
        json_schema_extra=_ui("允许公开作品", "日记、梦境和私密阅读来源仍会强制保持 private。", 7,
                              label_en="Allow Public Works", hint_en="Diary, dream and private-reading sources remain private."))
    create_share_opportunity: bool = Field(default=True, description="归档后生成低频分享契机。",
        json_schema_extra=_ui("允许分享完成作品", "私人作品只会定向给主人，公开作品仍由 Planner 和主动额度终审。", 8,
                              label_en="Share Archived Works", hint_en="Private works target the owner; public works still require Planner review."))
    inspiration_lookback_days: int = Field(default=7, ge=1, le=90, description="灵感来源回看天数。",
        json_schema_extra=_ui("灵感回看天数", "只读取麦麦自己的生活、梦境和日记摘要。", 9,
                              label_en="Inspiration Lookback", hint_en="Reads Mai's own life, dreams and diary summaries only."))
    max_body_chars: int = Field(default=4000, ge=500, le=16000, description="作品正文最大字符数。",
        json_schema_extra=_ui("作品正文上限", "用于限制模型成本和 SQLite 文本体积。", 10,
                              label_en="Maximum Work Length", hint_en="Limits model cost and SQLite text size."))
    external_reading_enabled: bool = Field(default=False, description="从可选插件 API 读取素材摘要。",
        json_schema_extra=_ui("启用外部阅读联动", "默认关闭；只调用下方插件 API，不直接联网。", 11,
                              label_en="Enable External Reading", hint_en="Calls an optional plugin API only and performs no direct networking."))
    external_reading_api_name: str = Field(default="", description="外部阅读插件公开 API 名称。",
        json_schema_extra=_ui("外部阅读 API", "例如 other-plugin.list_reading_items；留空不会调用。", 12,
                              label_en="External Reading API", hint_en="Public plugin API name; left empty to disable calls."))
    external_reading_max_items: int = Field(default=3, ge=1, le=20, description="每次最多读取素材数。",
        json_schema_extra=_ui("每次阅读素材上限", "忽略二进制，只处理标题和有限长度文字。", 13,
                              label_en="External Reading Item Limit", hint_en="Binary fields are ignored; only bounded text is processed."))
    reading_annotation_enabled: bool = Field(default=True, description="为外部阅读摘要生成第一人称批注。",
        json_schema_extra=_ui("生成阅读批注", "批注保持 private，朋友无权读取。", 14,
                              label_en="Generate Reading Annotations", hint_en="Annotations remain private and unavailable to friends."))
    continuation_enabled: bool = Field(default=True, description="启用书柜续写：有资格的旧书可按概率续写下一章。",
        json_schema_extra=_ui("启用续写旧书", "关闭后与仅创作全新作品的行为完全一致，可一键回退。", 15,
                              label_en="Enable Bookshelf Continuation", hint_en="Disable to always create brand-new works instead of continuing old ones."))
    continuation_probability: float = Field(default=0.65, ge=0, le=1, description="存在有资格旧书时选择续写的概率。",
        json_schema_extra=_ui("续写概率", "0.65 表示约 2/3 的日子增厚旧书、1/3 开新书；1.0 永远先续旧书，0.0 永久只开新书。", 16,
                              label_en="Continuation Probability", hint_en="Chance of continuing an eligible old book; 1.0 always continues, 0.0 never does."))
    continuation_min_gap_days: int = Field(default=1, ge=0, le=30, description="同一本书两章之间的最小间隔天数。",
        json_schema_extra=_ui("续写最小间隔（天）", "按上一章归档时间判断；必须小于续写停滞天数，否则候选窗口为空、续写静默失效；0 允许同一天续写。", 17,
                              label_en="Continuation Min Gap (days)", hint_en="Minimum days between two chapters of the same book, measured from the last archived chapter."))
    continuation_max_chapters: int = Field(default=8, ge=2, le=100, description="单本书最多续写的章节数。",
        json_schema_extra=_ui("单本书章节上限", "达到上限视为完结、不再续写，把机会让给新书。", 18,
                              label_en="Maximum Chapters per Book", hint_en="A book stops being continued once it reaches this many chapters."))
    continuation_stale_days: int = Field(default=30, ge=1, le=365, description="多少天没有新章节的书视为完结。",
        json_schema_extra=_ui("续写停滞天数", "超过该天数没有新章节的书不再续写；必须大于续写最小间隔，否则候选窗口为空、续写静默失效。", 19,
                              label_en="Continuation Stale Days", hint_en="Books without a new chapter for this many days are treated as finished."))
    continuation_work_types: list[str] = Field(
        default_factory=lambda:["novel_fragment","screenplay","storyboard","worldbuilding","character"],
        description="允许续写的作品类型；运行时与作品类型取交集。",
        json_schema_extra=_ui("可续写的体裁", "默认只保留连载型体裁；诗与随笔以单篇完整性为美，不参与续写。与上方作品类型的交集为空等于禁用续写。", 20,
                              label_en="Continuable Work Types", hint_en="Intersected with Work Types; poems and essays are excluded by default."))
    continuation_context_chapters: int = Field(default=3, ge=1, le=10, description="续写时向模型回看的最近章节数。",
        json_schema_extra=_ui("回看章节数", "控制 Prompt 体积与 Token 成本的线性上限。", 21,
                              label_en="Context Chapters", hint_en="How many recent chapters are replayed into the continuation prompt."))
    continuation_chapter_tail_chars: int = Field(default=400, ge=0, le=2000, description="每章回看携带的正文字尾字符数。",
        json_schema_extra=_ui("每章正文字尾字符", "0 表示只回看章节提纲、不带上文，进一步压低成本。", 22,
                              label_en="Chapter Tail Characters", hint_en="Trailing body characters replayed per chapter; zero replays outlines only."))


class EnvironmentSettings(PluginConfigBase):
    """时间与现实天气背景配置。"""

    __ui_label__: ClassVar[str] = "环境与天气"
    __ui_order__: ClassVar[int] = 2
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Environment and Weather", "description": "Timezone and Open-Meteo weather location."}
    }

    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区名称。",
        json_schema_extra=_ui(
            "时区", "影响日程日期、免打扰和每日结算。中国大陆通常使用 Asia/Shanghai。", 0,
            label_en="Timezone", hint_en="IANA timezone used for schedules, quiet hours and daily settlement.", placeholder="Asia/Shanghai",
        ),
    )
    city: str = Field(
        default="Shanghai",
        description="Open-Meteo 查询天气时使用的城市。",
        json_schema_extra=_ui(
            "天气城市", "填写麦生活所在地的城市名，插件会自动查询天气坐标。", 1,
            label_en="Weather City", hint_en="City name used to automatically resolve weather coordinates.", placeholder="Shanghai",
        ),
    )
    weather_refresh_minutes: int = Field(
        default=30, ge=10, le=360,
        description="天气缓存刷新间隔。",
        json_schema_extra=_ui(
            "天气刷新间隔（分钟）", "后台刷新，不会在被动回复时等待网络请求。建议 30～60 分钟。", 4,
            label_en="Weather Refresh Interval (min)", hint_en="Background refresh interval. Passive replies never wait for weather requests.",
        ),
    )


class StateSettings(PluginConfigBase):
    """生活数值推进与身体周期配置。"""

    __ui_label__: ClassVar[str] = "生活状态"
    __ui_order__: ClassVar[int] = 3
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Life State", "description": "Deterministic energy, hunger, health and body-cycle simulation."}
    }

    tick_interval_minutes: int = Field(
        default=10, ge=1, le=60,
        description="生活状态推进间隔。",
        json_schema_extra=_ui(
            "状态推进间隔（分钟）", "越短状态更新越及时，但数据库写入更频繁。建议保持 10 分钟。", 0,
            label_en="State Tick Interval (min)", hint_en="Shorter intervals update faster but write to the database more often.",
        ),
    )
    body_cycle_enabled: bool = Field(
        default=False,
        description="是否启用身体周期模拟。",
        json_schema_extra=_ui(
            "启用身体周期", "默认关闭。启用前请确认符合麦麦的人格和身体设定。", 1,
            label_en="Enable Body Cycle", hint_en="Disabled by default. Enable only when it fits the character setting.",
        ),
    )
    body_cycle_start_date: str = Field(
        default="",
        description="身体周期第 1 天对应的日期。",
        json_schema_extra=_ui(
            "周期起始日期", "格式 YYYY-MM-DD；仅在启用身体周期后使用。", 2,
            label_en="Cycle Start Date", hint_en="Day one of the cycle in YYYY-MM-DD format.", placeholder="2026-07-01",
        ),
    )
    body_cycle_length_days: int = Field(
        default=28, ge=20, le=45,
        description="完整身体周期长度。",
        json_schema_extra=_ui(
            "周期长度（天）", "允许范围 20～45 天，默认 28 天。", 3,
            label_en="Cycle Length (days)", hint_en="Full cycle length, from 20 to 45 days.",
        ),
    )
    body_cycle_period_days: int = Field(
        default=5, ge=1, le=10,
        description="周期中经期持续天数。",
        json_schema_extra=_ui(
            "经期持续天数", "允许范围 1～10 天，只用于轻度状态修正。", 4,
            label_en="Period Duration (days)", hint_en="From 1 to 10 days; only used for mild state adjustments.",
        ),
    )


class MemorySettings(PluginConfigBase):
    """梦境、日记与重要日期。"""

    __ui_label__: ClassVar[str] = "生活记忆"
    __ui_order__: ClassVar[int] = 4

    enabled: bool = Field(
        default=True, description="启用离线生活记忆结算。",
        json_schema_extra=_ui("启用生活记忆", "总开关；关闭后保留已有日记和日期数据。", 0,
                              label_en="Enable Life Memory", hint_en="Keep existing data while pausing memory settlement."),
    )
    dream_fragments_enabled: bool = Field(
        default=True, description="在有效夜间睡眠后保存梦境碎片。",
        json_schema_extra=_ui("启用梦境碎片", "梦境摘要之外保存少量醒后片段，不强制在聊天中提起。", 1,
                              label_en="Enable Dream Fragments", hint_en="Store a few wake-up fragments alongside the dream summary."),
    )
    dream_fragment_count: int = Field(
        default=3, ge=1, le=5, description="每次梦境最多保存的碎片数。",
        json_schema_extra=_ui("梦境碎片数量", "范围 1～5，默认 3。", 2,
                              label_en="Dream Fragment Count", hint_en="Maximum fragments stored per dream."),
    )
    dream_probability: float = Field(
        default=0.35, ge=0, le=1, description="合格夜晚醒来后实际生成梦境的概率。",
        json_schema_extra=_ui("做梦概率", "每个合格夜晚醒来独立掷一次，0.35 表示约每周 2～3 次；0 永不做梦，1 每晚必做。午睡和短睡路径不掷。", 3,
                              label_en="Dream Probability", hint_en="Rolled once per eligible night wake-up; 0 never dreams, 1 always does. Naps are not rolled."),
    )
    dream_weekly_max: int = Field(
        default=0, ge=0, le=7, description="近 7 天已落库梦境数上限。",
        json_schema_extra=_ui("每周梦境上限", "默认 0 不限制，只靠上方概率单旋钮；设为正数后，近 7 天梦境数达到上限的当晚直接跳过做梦，是对冲连彩的可选保险。", 4,
                              label_en="Weekly Dream Limit", hint_en="Default 0 disables the cap. When set, nights already at the recent 7-day limit skip dreaming."),
    )
    diary_enabled: bool = Field(
        default=True, description="每天生成前一天的抽象日记。",
        json_schema_extra=_ui("启用抽象日记", "只使用生活节点、梦境和互动数量，不复制聊天原文。", 5,
                              label_en="Enable Abstract Diary", hint_en="Uses life events and interaction counts, never chat transcripts."),
    )
    diary_hour: int = Field(
        default=2, ge=0, le=23, description="每天结算前一天日记的小时。",
        json_schema_extra=_ui("日记结算时间（小时）", "默认凌晨 2 点，在 3 点生成新日程之前完成。", 6,
                              label_en="Diary Settlement Hour", hint_en="Hour used to settle the previous day."),
    )
    diary_max_chars: int = Field(
        default=1200, ge=300, le=4000, description="单篇日记正文最大字符数。",
        json_schema_extra=_ui("日记长度上限", "限制日记和后续 Prompt 占用。", 7,
                              label_en="Diary Length Limit", hint_en="Maximum characters in one diary entry."),
    )
    important_dates_enabled: bool = Field(
        default=True, description="识别并维护生日、考试、约定等日期。",
        json_schema_extra=_ui("启用重要日期", "明确日期自动记录，模糊日期只进入待确认列表。", 8,
                              label_en="Enable Important Dates", hint_en="Save explicit dates and keep ambiguous dates as candidates."),
    )
    date_model_analysis_enabled: bool = Field(
        default=False, description="本地规则未识别时调用快速模型分析日期。",
        json_schema_extra=_ui("启用日期模型分析", "默认关闭；开启会增加私聊后的异步模型调用。", 9,
                              label_en="Enable Date Model Analysis", hint_en="Disabled by default because it adds an asynchronous model call."),
    )
    date_candidate_retention_days: int = Field(
        default=90, ge=7, le=365, description="未确认日期候选的保留天数。",
        json_schema_extra=_ui("日期候选保留天数", "过期候选只清理待确认项，不删除正式日期。", 10,
                              label_en="Date Candidate Retention", hint_en="Retention for unconfirmed date candidates."),
    )
    date_reminder_lead_days: list[int] = Field(
        default_factory=lambda: [30,7,1,0], description="重要日期提前准备和提醒的天数。",
        json_schema_extra=_ui("重要日期提前天数", "默认提前 30、7、1 天以及当天生成一次专属契机。", 11,
                              label_en="Important Date Lead Days", hint_en="Days before an event when a private opportunity may be created."),
    )
    @field_validator("date_reminder_lead_days", mode="before")
    @classmethod
    def normalize_lead_days(cls, value: Any) -> list[int]:
        values=value if isinstance(value,list) else [30,7,1,0]
        cleaned=[]
        for item in values:
            try:number=int(item)
            except (TypeError,ValueError):continue
            if 0<=number<=365 and number not in cleaned:cleaned.append(number)
        return sorted(cleaned or [30,7,1,0],reverse=True)


class InformationSettings(PluginConfigBase):
    """联网任务的公共开关、关联和上下文参数。"""

    __ui_label__: ClassVar[str] = "联网见闻"
    __ui_order__: ClassVar[int] = 13

    enabled: bool = Field(default=False,description="联网见闻与搜索工具总开关。",json_schema_extra=_ui(
        "启用联网见闻","默认关闭；新闻、主动搜索和联网工具还受各自开关控制。",0,label_en="Enable Connected Discovery",hint_en="Disabled by default; news, proactive search and the web-search tool have separate switches."))
    association_enabled: bool = Field(default=True,description="判断外界信息与麦麦自身的关系。",json_schema_extra=_ui(
        "启用自我关联","先判断与人格、状态、兴趣、创作或关系是否有关，再考虑分享。",1,label_en="Enable Self-association",hint_en="Relate external information to Mai before considering sharing."))
    association_threshold: float = Field(default=0.65,ge=0,le=1,description="创建主动契机的最低关联分。",json_schema_extra=_ui(
        "关联分数阈值","越高越克制；低于阈值只保留为近期见闻。",2,label_en="Association Threshold",hint_en="Items below this score remain notes and do not create opportunities."))
    proactive_share_enabled: bool = Field(default=True,description="允许高关联见闻创建主动契机。",json_schema_extra=_ui(
        "允许见闻主动分享","仍受用户额度、休息、免打扰和 Planner 终审限制。",3,label_en="Allow Discovery Sharing",hint_en="Still subject to quota, rest, quiet hours and Planner review."))
    context_item_limit: int = Field(default=3,ge=0,le=10,description="被动上下文最多包含的近期见闻数。",json_schema_extra=_ui(
        "近期见闻上下文数量","设为 0 可只积累记录、不注入被动回复。",4,label_en="Discovery Context Items",hint_en="Set to zero to store discoveries without passive context injection."))


class SearchProviderProfile(PluginConfigBase):
    """一个有序搜索服务及其主备 Key。"""

    enabled: bool = Field(default=False,description="是否启用该搜索服务。",json_schema_extra=_ui(
        "启用服务","列表顺序就是跨服务降级顺序。",0,label_en="Enable Provider",hint_en="List order defines provider failover priority."))
    provider_type: Literal["playwright","bocha","tavily","you","openai_responses","openai_chat"] = Field(
        default="playwright",description="搜索服务协议。",json_schema_extra=_ui(
            "服务类型","默认 Playwright 免 Key 浏览器搜索；API 服务可作为备援。",1,
            label_en="Provider Type",hint_en="Playwright is the default keyless provider; API providers can serve as fallbacks.",
            enum_labels={"playwright":"Playwright 浏览器搜索","bocha":"博查","tavily":"Tavily","you":"You.com",
                         "openai_responses":"自定义 Responses 联网","openai_chat":"自定义 Chat 联网"}))
    browser_engine: Literal["bing","duckduckgo"] = Field(default="bing",description="Playwright 搜索引擎。",json_schema_extra=_ui(
        "浏览器搜索引擎","默认 Bing；DuckDuckGo 可作为搜索页结构变化时的浏览器备援。",2,
        label_en="Browser Engine",hint_en="Bing is default; DuckDuckGo is an alternate browser engine.",
        enum_labels={"bing":"Bing","duckduckgo":"DuckDuckGo"}))
    headless: bool = Field(default=True,description="Playwright 是否使用无头浏览器。",json_schema_extra=_ui(
        "无头浏览器","默认开启；排查验证码或页面结构时可临时关闭。",3,
        label_en="Headless Browser",hint_en="Enabled by default; disable temporarily to diagnose CAPTCHA or page changes."))
    api_keys: list[str] = Field(default_factory=list,description="按优先级排列的 API Key。",json_schema_extra=_ui(
        "API Key 列表","仅 API 服务使用；Playwright 必须留空。",4,
        label_en="API Keys",hint_en="Used by API providers only; must remain empty for Playwright."))
    endpoint: str = Field(default="",description="自定义 OpenAI 兼容服务地址。",json_schema_extra=_ui(
        "自定义服务地址","内置服务留空；可填写基础地址或完整 responses/chat/completions 地址。",5,
        label_en="Custom Endpoint",hint_en="Leave empty for built-ins; accepts a base URL or full endpoint."))
    model: str = Field(default="",description="自定义联网模型名称。",json_schema_extra=_ui(
        "自定义模型名","仅自定义 Responses/Chat 使用，例如中转提供的 Grok 模型名。",6,
        label_en="Custom Model",hint_en="Required for custom Responses or Chat providers."))

    @field_validator("api_keys",mode="before")
    @classmethod
    def normalize_keys(cls,value:Any)->list[str]:
        values=value if isinstance(value,list) else []
        cleaned=[]
        for item in values:
            key=str(item or "").strip()
            if key and key not in cleaned:cleaned.append(key)
        return cleaned[:20]

    @field_validator("endpoint","model",mode="before")
    @classmethod
    def normalize_provider_text(cls,value:Any)->str:return str(value or "").strip()

    @model_validator(mode="after")
    def validate_provider(self)->"SearchProviderProfile":
        if self.provider_type=="playwright" and self.api_keys:
            raise ValueError("Playwright 搜索不使用 API Key，请清空 API Key 列表")
        if self.enabled and self.provider_type in {"openai_responses","openai_chat"}:
            if not self.endpoint:raise ValueError("启用自定义联网服务时必须填写服务地址")
            if not self.model:raise ValueError("启用自定义联网服务时必须填写模型名")
        return self



class SearchAPISettings(PluginConfigBase):
    """新闻和主动搜索共用的 API 服务链。"""

    __ui_label__: ClassVar[str] = "联网搜索服务"
    __ui_order__: ClassVar[int] = 14

    providers: list[SearchProviderProfile] = Field(
        default_factory=lambda:[SearchProviderProfile(enabled=True,provider_type="playwright")],
        description="有序搜索服务列表。",json_schema_extra=_ui(
            "搜索服务列表","默认免 Key Playwright/Bing；可继续添加 API 服务作为备援。",0,
            label_en="Search Provider Chain",hint_en="Defaults to keyless Playwright/Bing; API providers can be appended as fallbacks."))
    timeout_seconds: float = Field(default=12.0,ge=2,le=60,description="单次外部请求超时。",json_schema_extra=_ui(
        "请求超时（秒）","超时视为服务故障并切换下一服务，不逐个消耗该服务的 Key。",1,
        label_en="Request Timeout",hint_en="Timeouts fail over to the next provider without burning all keys."))
    max_attempts: int = Field(default=12,ge=1,le=12,description="单次逻辑搜索最多外部请求次数。",json_schema_extra=_ui(
        "单次最大尝试次数","限制错误配置或大量失效 Key 造成的请求风暴。",2,
        label_en="Maximum Attempts",hint_en="Caps external attempts for one logical search."))
    max_results: int = Field(default=5,ge=1,le=10,description="单次搜索最多保留结果数。",json_schema_extra=_ui(
        "单次结果数","默认 5 条，新闻和主动搜索共同使用。",3,
        label_en="Maximum Results",hint_en="Shared result limit for news and exploration."))
    tool_enabled: bool = Field(default=True,description="允许麦麦按需调用联网搜索工具。",json_schema_extra=_ui(
        "允许麦麦使用联网搜索工具","仍需开启“联网见闻”总开关并配置至少一个可用服务；工具只返回搜索摘要，不会直接发送消息。",4,
        label_en="Enable Web Search Tool",hint_en="Also requires connected discovery and an available provider; the tool returns search summaries only."))
    history_enabled: bool = Field(default=True,description="本地记录每次逻辑搜索的查询词与结果摘要。",json_schema_extra=_ui(
        "记录搜索历史","记录联网工具、主动搜索和新闻阅读的查询词与结果摘要；查询词已做隐私清洗，原始 Key 不入库。",5,
        label_en="Record Search History",hint_en="Store sanitized query and result summaries for local audit."))
    history_retention_days: int = Field(default=30,ge=1,le=365,description="本地搜索历史保留天数。",json_schema_extra=_ui(
        "搜索历史保留天数","到期自动清理；新闻与探索笔记仍按各自保留期管理。",6,
        label_en="Search History Retention (days)",hint_en="Expired history rows are cleaned automatically."))


class NewsSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "新闻阅读"
    __ui_order__: ClassVar[int] = 14

    enabled: bool = Field(default=False,description="启用低频 API 新闻阅读。",json_schema_extra=_ui(
        "启用新闻阅读","默认关闭；还需要在“联网搜索服务”中启用至少一个服务。",0,label_en="Enable News Reading",hint_en="Disabled by default and requires an enabled search provider."))
    daily_max: int = Field(default=1,ge=0,le=12,description="每天最多发起的新闻阅读次数。",json_schema_extra=_ui(
        "每日新闻阅读次数","默认 1 次；一次主备 Key 和跨服务降级链合计为一次。",1,label_en="Daily News Reads",hint_en="One provider failover chain counts as one daily attempt."))
    full_text_count: int = Field(default=3,ge=0,le=3,description="最多继续读取正文的结果数。",json_schema_extra=_ui(
        "正文读取数量","最多读取最相关的前 3 篇；0 表示只使用 API 返回内容。",3,label_en="Full-text Articles",hint_en="Read up to three source pages; zero uses API summaries only."))
    max_concurrency: int = Field(default=2,ge=1,le=4,description="正文读取最大并发数。",json_schema_extra=_ui(
        "正文读取并发","中国网络环境建议保持 1～2。",4,label_en="Article Concurrency",hint_en="Keep low for constrained networks."))
    max_article_chars: int = Field(default=8000,ge=1000,le=30000,description="保存和整理的正文最大字符数。",json_schema_extra=_ui(
        "正文长度上限","超长正文会在本地清洗后截断。",5,label_en="Article Length Limit",hint_en="Clean and truncate long articles locally."))
    retention_days: int = Field(default=7,ge=1,le=90,description="新闻正文和摘要保留天数。",json_schema_extra=_ui(
        "新闻保留天数","过期缓存自动清理。",6,label_en="News Retention",hint_en="Expired cached articles are removed automatically."))
    allowed_schedule_types: list[Literal["leisure","rest"]] = Field(default_factory=lambda:["leisure","rest"],description="允许阅读新闻的日程类型。",json_schema_extra=_ui(
        "允许阅读的日程","默认只在闲暇或休息时阅读。",7,label_en="Allowed Schedules",hint_en="Read news only during leisure or rest.",enum_labels={"leisure":"闲暇","rest":"休息"}))


class SearchSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "主动搜索"
    __ui_order__: ClassVar[int] = 15

    enabled: bool = Field(default=False,description="启用低频主动搜索。",json_schema_extra=_ui(
        "启用主动搜索","默认关闭；还需要在“联网搜索服务”中启用至少一个服务。",0,
        label_en="Enable Proactive Search",hint_en="Disabled by default and requires an enabled search provider."))
    daily_max: int = Field(default=1,ge=0,le=10,description="每天最多发起的主动搜索次数。",json_schema_extra=_ui(
        "每日主动搜索上限","默认 1 次；失败或空结果也会计入，避免持续请求。",1,label_en="Daily Search Limit",hint_en="Failures and empty results count to prevent repeated external requests."))
    include_chat_topics: bool = Field(default=False,description="允许使用匿名未完话题规划搜索。",json_schema_extra=_ui(
        "允许参考聊天话题","默认关闭，避免把私聊内容变成外部搜索词。",2,label_en="Use Chat Topics",hint_en="Disabled by default to keep private chat out of external queries."))
    allowed_schedule_types: list[Literal["leisure","rest","travel"]] = Field(default_factory=lambda:["leisure","rest"],description="允许主动搜索的日程类型。",json_schema_extra=_ui(
        "允许搜索的日程类型","只在空档、休息或按配置允许的出行段探索。",4,label_en="Allowed Schedule Types",hint_en="Search only during configured free schedule segments.",enum_labels={"leisure":"闲暇","rest":"休息","travel":"出行"}))
    note_retention_days: int = Field(default=30,ge=1,le=365,description="探索笔记保留天数。",json_schema_extra=_ui(
        "探索笔记保留天数","过期笔记自动清理。",5,label_en="Exploration Note Retention",hint_en="Expired exploration notes are removed automatically."))


class RestGateSettings(PluginConfigBase):
    """睡眠和午休期间的被动回复判醒设置。"""

    __ui_label__: ClassVar[str] = "休息回复闸门"
    __ui_order__: ClassVar[int] = 4
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Rest Reply Gate", "description": "Decides whether Mai wakes up for passive messages during rest."}
    }

    enabled: bool = Field(
        default=False,
        description="是否启用睡眠和午休期间的判醒闸门。",
        json_schema_extra=_ui(
            "启用休息回复闸门", "默认关闭。开启后，麦麦睡眠或午休时可能暂不回复普通消息。", 0,
            label_en="Enable Rest Reply Gate", hint_en="When enabled, Mai may remain silent for ordinary messages during sleep or naps.",
        ),
    )
    mode: Literal["probability", "llm"] = Field(
        default="probability",
        description="普通消息使用概率还是 LLM 进行判醒。",
        json_schema_extra=_ui(
            "判醒模式", "规则会先处理明确勿扰和紧急叫醒；这里只控制剩余普通消息。", 1,
            label_en="Wake Decision Mode", hint_en="Explicit quiet and urgent wake signals are handled before this mode.",
            enum_labels={"probability": "规则边界 + 概率", "llm": "LLM 模型判断"},
        ),
    )
    gate_mode: Literal["time_window", "schedule"] = Field(
        default="time_window", description="闸门生效方式：按设定时间窗，或按日程段型自行判断。",
        json_schema_extra=_ui(
            "闸门生效方式", "按设定时间：只在下方夜间/午休时间窗内闸门生效；按日程判断：日程进入「闸门生效的日程类型」所列段型时即生效（跟着日程作息走，如日程 22:30 就寝则 22:30 开始休息，忽略时间窗），晚安与入睡推迟同样跟随日程睡眠段。", 1,
            label_en="Gate Mode",
            hint_en="time_window: gate only inside configured windows; schedule: gate whenever the schedule enters listed segment kinds.",
            enum_labels={"time_window": "按设定时间", "schedule": "按日程判断"},
        ),
    )
    wake_probability: float = Field(
        default=0.18, ge=0, le=1,
        description="概率模式下普通消息叫醒麦麦的概率。",
        json_schema_extra=_ui(
            "普通消息醒来概率", "0.18 表示 18%。只在 probability 模式下生效。", 2,
            label_en="Wake Probability", hint_en="0.18 means 18%. Only used in probability mode.",
        ),
    )
    llm_threshold: int = Field(
        default=70, ge=0, le=100,
        description="LLM 判醒分数阈值。",
        json_schema_extra=_ui(
            "LLM 判醒阈值", "分数达到该值且模型认为应回复时才会醒来。只在 LLM 模式生效。", 3,
            label_en="LLM Wake Threshold", hint_en="Wake only when the model score reaches this threshold.",
        ),
    )
    awake_grace_minutes: int = Field(
        default=30, ge=0, le=240,
        description="被叫醒后免于重复判醒的时间。",
        json_schema_extra=_ui(
            "醒来缓冲（分钟）", "缓冲期间普通消息直接放行，避免睡眠段内每条消息都重新判醒。", 4,
            label_en="Awake Grace Period (min)", hint_en="Messages pass directly during this period without another wake decision.",
        ),
    )
    gate_segment_types: list[Literal["sleep", "nap", "rest"]] = Field(
        default_factory=lambda: ["sleep", "nap"],
        description="按日程判断模式下启用闸门的日程类型；按设定时间模式下不参与生效。",
        json_schema_extra=_ui(
            "闸门生效的日程类型", "按日程判断模式下：日程进入所列段型时闸门即生效（主力开关，任意时间）；按设定时间模式下：本列表不参与闸门生效，仅用于判醒提示词里的活动描述。加入 rest 后，按日程判断时普通休息段也可能不回复。", 5,
            label_en="Gated Schedule Types", hint_en="In schedule mode the gate activates on listed segment kinds; in time-window mode this list only labels the wake-judgment prompt.",
            enum_labels={"sleep": "夜间睡眠（sleep）", "nap": "午休小睡（nap）", "rest": "普通休息（rest）"},
        ),
    )
    force_wake_terms: list[str] = Field(
        default_factory=lambda: ["救命","急事","报警","醒醒","快醒","叫醒","出事了","很难受","撑不住","危险","轻生","自杀"],
        description="强制唤醒词：消息含其中任意一词时无视勿扰词与概率，立即放行并建立待醒候选。",
        json_schema_extra=_ui("强制唤醒词","含任意一词立即放行；可自由增删",6,
                              label_en="Force Wake Terms",
                              hint_en="Any term in the message bypasses quiet terms and probability, waking Mai immediately."),
    )
    night_start: str = Field(
        default="22:30", description="夜间闸门开始时间。",
        json_schema_extra=_ui("夜间闸门开始", "默认 22:30，支持跨午夜；仅在「按设定时间」模式下生效，与结束时间相同则该窗口永不生效。", 6, label_en="Night Gate Start", hint_en="Night gate start in HH:MM format."),
    )
    night_end: str = Field(
        default="08:00", description="夜间闸门结束时间。",
        json_schema_extra=_ui("夜间闸门结束", "默认 08:00。", 7, label_en="Night Gate End", hint_en="Night gate end in HH:MM format."),
    )
    nap_start: str = Field(
        default="12:00", description="午休闸门开始时间。",
        json_schema_extra=_ui("午休闸门开始", "默认 12:00。仅在「按设定时间」模式下生效。", 8, label_en="Nap Gate Start", hint_en="Nap gate start in HH:MM format."),
    )
    nap_end: str = Field(
        default="14:30", description="午休闸门结束时间。",
        json_schema_extra=_ui("午休闸门结束", "默认 14:30。", 9, label_en="Nap Gate End", hint_en="Nap gate end in HH:MM format."),
    )

    # ── 群聊闸门（与私聊完全独立；轻量版：强制词放行 + 勿扰词阻断 + 其余静默）──
    group_enabled: bool = Field(
        default=False, description="群聊休息闸门总开关。",
        json_schema_extra=_ui("启用群聊休息闸门", "默认关闭。开启后，在下方群时间窗内，群里只有含强制唤醒词的消息会被回复，其余静默（不回复、不积压、不进主程序）。", 10,
                              label_en="Enable Group Rest Gate", hint_en="During group quiet windows only force-wake terms get replies."),
    )
    group_mode: Literal["all", "selected"] = Field(
        default="all", description="群闸门作用范围：all=所有群；selected=只对下方「受控群白名单」以及社交白名单中开启「夜间静音」的群生效。",
        json_schema_extra=_ui("群闸门作用范围", "「所有群」开箱即用；「指定群」在下方受控群白名单填写群号即可，社交白名单中开启「夜间静音」的群同样生效（兼容旧配置方式）。", 10,
                              label_en="Group Gate Scope", hint_en="All groups, or only groups listed in the whitelist below (plus social-allowlist groups with night silence).",
                              enum_labels={"all": "所有群", "selected": "指定群（白名单）"}),
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="selected 模式的受控群号列表（纯数字 QQ 群号）。仅这些群受群闸门管辖；社交白名单中开启「夜间静音」的群也一并生效。",
        json_schema_extra=_ui("受控群白名单", "「指定群」模式下的群号列表，纯数字 QQ 群号，一行一个或逗号分隔；留空且未配置社交白名单时，selected 模式下没有任何群受控（等同不拦截）。", 10,
                              label_en="Controlled Group Whitelist", hint_en="Group IDs controlled by the gate in selected mode; deduplicated and digits-only."),
    )

    @field_validator("group_whitelist", mode="before")
    @classmethod
    def normalize_group_whitelist(cls, value: Any) -> list[str]:
        """清洗受控群白名单：只保留纯数字群号，去重保序；支持逗号/空格分隔的字符串输入。"""
        if value is None:
            return []
        items: list[Any] = value if isinstance(value, list) else re.split(r"[,，\s]+", str(value))
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            if not text.isdigit():
                raise ValueError(f"受控群白名单的群号必须是纯数字: {text[:24]}")
            if text not in result:
                result.append(text)
        return result
    group_night_start: str = Field(
        default="22:30", description="群夜间闸门开始时间。",
        json_schema_extra=_ui("群夜间闸门开始", "默认 22:30，支持跨午夜；仅在「按设定时间」模式下生效，与结束时间相同则该窗口永不生效。", 11, label_en="Group Night Start", hint_en="Group night window start in HH:MM format."),
    )
    group_night_end: str = Field(
        default="08:00", description="群夜间闸门结束时间。",
        json_schema_extra=_ui("群夜间闸门结束", "默认 08:00。", 12, label_en="Group Night End", hint_en="Group night window end in HH:MM format."),
    )
    group_nap_start: str = Field(
        default="12:00", description="群午休闸门开始时间。",
        json_schema_extra=_ui("群午休闸门开始", "默认 12:00。仅在「按设定时间」模式下生效。", 13, label_en="Group Nap Start", hint_en="Group nap window start in HH:MM format."),
    )
    group_nap_end: str = Field(
        default="14:30", description="群午休闸门结束时间。",
        json_schema_extra=_ui("群午休闸门结束", "默认 14:30。", 14, label_en="Group Nap End", hint_en="Group nap window end in HH:MM format."),
    )
    group_gate_segment_types: list[Literal["sleep", "nap", "rest", "study", "work", "leisure", "meal", "travel"]] = Field(
        default_factory=lambda: ["sleep", "nap"],
        description="按日程判断模式下触发群闸门的日程类型；按设定时间模式下不参与生效。",
        json_schema_extra=_ui("群闸门生效的日程类型",
            "按日程判断模式下：日程进入所列段型时群闸门即生效（任意时间）；按设定时间模式下不参与生效。默认与私聊一致：夜间睡眠+午休。", 16,
            label_en="Group Gated Schedule Types",
            hint_en="In schedule mode the group gate activates on listed kinds; in time-window mode this list is inert."),
        enum_labels={"sleep": "夜间睡眠（sleep）", "nap": "午休小睡（nap）", "rest": "普通休息（rest）",
                     "study": "学习（study）", "work": "工作（work）", "leisure": "休闲（leisure）",
                     "meal": "餐饮（meal）", "travel": "出行（travel）"})
    group_force_wake_terms: list[str] = Field(
        default_factory=lambda: ["救命","急事","报警","醒醒","快醒","叫醒","出事了","很难受","撑不住","危险","轻生","自杀"],
        description="群强制唤醒词：群消息含其中任意一词时放行；独立于私聊词表。",
        json_schema_extra=_ui("群强制唤醒词","群消息含任意一词立即放行；可自由增删",15,
                              label_en="Group Force Wake Terms",
                              hint_en="Group messages containing any term bypass the group silence."),
    )

    @field_validator("night_start", "night_end", "nap_start", "nap_end",
                     "group_night_start", "group_night_end", "group_nap_start", "group_nap_end", mode="before")
    @classmethod
    def validate_gate_time(cls, value: Any, info: ValidationInfo) -> str:
        defaults={"night_start":"22:30","night_end":"08:00","nap_start":"12:00","nap_end":"14:30",
                  "group_night_start":"22:30","group_night_end":"08:00",
                  "group_nap_start":"12:00","group_nap_end":"14:30"}
        return _time(value,defaults.get(info.field_name,"00:00"))


class ContextSettings(PluginConfigBase):
    """被动回复增强与朋友边界。"""

    __ui_label__: ClassVar[str] = "被动回复增强"
    __ui_order__: ClassVar[int] = 5

    enabled: bool = Field(
        default=True, description="向配置私聊注入生活与关系背景。",
        json_schema_extra=_ui("启用被动回复增强", "只处理配置的私聊用户，不影响群聊。", 0, label_en="Enable Passive Context", hint_en="Inject context only for configured private users."),
    )
    continuity_enabled: bool = Field(
        default=True, description="维护轻量未完话题元数据。",
        json_schema_extra=_ui("启用连续话题", "异步整理意图与未完话题，不复制完整聊天记忆。", 1, label_en="Enable Conversation Continuity", hint_en="Maintain lightweight unfinished-topic metadata asynchronously."),
    )
    continuity_interval_minutes: int = Field(
        default=10, ge=1, le=180, description="同一会话整理连续话题的最小间隔。",
        json_schema_extra=_ui("话题整理间隔（分钟）", "默认 10 分钟，不阻塞当前回复。", 2, label_en="Continuity Update Interval", hint_en="Minimum interval between continuity summaries."),
    )
    owner_only_terms: list[str] = Field(
        default_factory=lambda: ["主人", "老公", "老婆", "亲爱的主人"],
        description="朋友回复中禁止出现的主人专属称呼。",
        json_schema_extra=_ui("主人专属称呼", "朋友回复命中后会重生成一次，仍命中则静默。请只填写明确专属词。", 3, label_en="Owner-only Terms", hint_en="Terms forbidden in friend replies."),
    )
    background_cache_minutes: int = Field(
        default=5, ge=0, le=60,
        description="慢变背景块（生活记忆/外界见闻/书柜/梦境）的会话级缓存分钟数；0 表示每轮实时读取。",
        json_schema_extra=_ui(
            "背景缓存（分钟）", "记忆/见闻/书柜/梦境按用户缓存指定分钟数，减少每轮重复读取与注入；0 关闭。",
            5, label_en="Background Cache (min)",
            hint_en="Cache slow-changing background blocks per user; 0 disables.",
        ),
    )
    replyer_detail: Literal["full", "compact"] = Field(
        default="full", description="Replyer 背景详略：full 完整背景；compact 精简（日记行省略、书柜/见闻预算减半）。",
        json_schema_extra=_ui(
            "回复背景详略", "full=与 Planner 同源完整背景；compact=精简版（省 token，日记余韵不进回复层、"
            "书柜与见闻摘要减半）。默认 full 不改行为。",
            6, label_en="Replyer Detail",
            hint_en="full keeps parity with planner; compact slims replyer-only background to save tokens.",
            enum_labels={"full": "full（完整）", "compact": "compact（精简）"},
        ),
    )
    prompt_max_chars: int = Field(
        default=4000, ge=600, le=8000, description="Planner 生活背景的最大字符数。",
        json_schema_extra=_ui("上下文长度上限", "限制额外 Prompt 大小，避免状态信息挤占正常聊天。", 4, label_en="Context Character Limit", hint_en="Maximum characters appended by Mai Life."),
    )


class DebounceSettings(PluginConfigBase):
    """私聊与群聊相互独立的消息收口参数。"""

    __ui_label__: ClassVar[str] = "消息收口防抖"
    __ui_order__: ClassVar[int] = 6

    enabled: bool = Field(default=True, description="合并短时间连续补话。", json_schema_extra=_ui("启用私聊收口", "只对配置私聊用户生效。", 0, label_en="Enable Private Debounce", hint_en="Merge rapid follow-up messages for configured private users."))
    text_wait_seconds: float = Field(default=1.2, ge=0, le=10, description="私聊文本静默窗口。", json_schema_extra=_ui("私聊文本等待（秒）", "默认 1.2 秒。", 1, label_en="Private Text Wait (sec)", hint_en="Quiet window for private text messages."))
    image_wait_seconds: float = Field(default=3.0, ge=0, le=15, description="私聊单图等待补充说明时间。", json_schema_extra=_ui("私聊单图等待（秒）", "默认 3 秒，便于用户补充图片说明。", 2, label_en="Private Image Wait (sec)", hint_en="Wait for a private image caption."))
    forward_wait_seconds: float = Field(default=2.0, ge=0, le=15, description="私聊合并转发等待时间。", json_schema_extra=_ui("私聊转发等待（秒）", "默认 2 秒。", 3, label_en="Private Forward Wait (sec)", hint_en="Quiet window for private forwarded messages."))
    max_wait_seconds: float = Field(default=6.0, ge=0.5, le=30, description="私聊整轮最长等待。", json_schema_extra=_ui("私聊最长等待（秒）", "无论是否继续补话，达到上限都会结束收口。", 4, label_en="Private Maximum Wait (sec)", hint_en="Hard limit for one private burst."))
    group_enabled: bool = Field(default=False,description="是否合并群聊中的连续补话。",json_schema_extra=_ui(
        "启用群聊收口","默认关闭；开启后覆盖所有 QQ 群，并按群号和发送者 QQ 号隔离。",5,
        label_en="Enable Group Debounce",hint_en="Disabled by default; isolates bursts by group and sender QQ IDs."))
    group_text_wait_seconds: float = Field(default=0.8,ge=0,le=10,description="群聊文本静默窗口。",json_schema_extra=_ui(
        "群聊文本等待（秒）","默认 0.8 秒。",6,label_en="Group Text Wait (sec)",hint_en="Quiet window for group text messages."))
    group_image_wait_seconds: float = Field(default=2.0,ge=0,le=15,description="群聊单图等待补充说明时间。",json_schema_extra=_ui(
        "群聊单图等待（秒）","默认 2 秒。",7,label_en="Group Image Wait (sec)",hint_en="Wait for a group image caption."))
    group_forward_wait_seconds: float = Field(default=1.2,ge=0,le=15,description="群聊合并转发等待时间。",json_schema_extra=_ui(
        "群聊转发等待（秒）","默认 1.2 秒。",8,label_en="Group Forward Wait (sec)",hint_en="Quiet window for group forwarded messages."))
    group_max_wait_seconds: float = Field(default=4.0,ge=0.5,le=30,description="群聊整轮最长等待。",json_schema_extra=_ui(
        "群聊最长等待（秒）","默认 4 秒。",9,label_en="Group Maximum Wait (sec)",hint_en="Hard limit for one group burst."))
    max_messages: int = Field(default=12, ge=2, le=50, description="单轮最多合并消息数。", json_schema_extra=_ui("单轮最大消息数", "私聊和群聊共同使用；超过后立即结束当前轮次。", 10, label_en="Maximum Messages", hint_en="Shared maximum messages per burst."))
    max_media_bytes: int = Field(default=8_388_608, ge=262_144, le=33_554_432, description="单轮媒体 Base64 解码后大小上限。", json_schema_extra=_ui("媒体大小上限（字节）", "默认 8 MiB，超出后失败开放。", 11, label_en="Media Size Limit", hint_en="Decoded media size limit for one burst."))
    merge_separator: str = Field(
        default="\n",
        description="多条文本合并时使用的分隔符。",
        json_schema_extra=_ui(
            "合并分隔符", "多条连续消息合并时，文本之间插入的分隔符，默认换行。", 14,
            label_en="Merge Separator", hint_en="Separator inserted between merged text segments.",
        ),
    )
    ignore_empty_message: bool = Field(
        default=True,
        description="没有文本或图片等主体内容的消息不参与收口，直接放行。",
        json_schema_extra=_ui(
            "忽略空消息", "无文本且无图片/表情/语音等主体内容的消息直接放行，不进入防抖窗口。", 15,
            label_en="Ignore Empty Messages", hint_en="Messages without text or media body bypass the debounce window.",
        ),
    )
    log_detail: bool = Field(
        default=True,
        description="记录开始收集、追加、结算等防抖过程日志。",
        json_schema_extra=_ui(
            "记录详细日志", "开启后按消息记录防抖开始/追加/结算日志，便于排查合并行为。", 16,
            label_en="Detailed Debounce Logs", hint_en="Log debounce start, append and settle events per message.",
        ),
    )
    outbound_turn_guard: bool = Field(default=True, description="阻止同一消息触发多次独立 Replyer 回复。", json_schema_extra=_ui("启用同轮回复防重", "不影响一次回复内部的正常分段。", 12, label_en="Enable Reply Turn Guard", hint_en="Prevent repeated Replyer responses for the same user message."))
    turn_expire_seconds: int = Field(default=120, ge=20, le=600, description="轮次锁过期时间。", json_schema_extra=_ui("轮次锁过期（秒）", "发送失败时会提前释放。", 13, label_en="Turn Lock Expiry", hint_en="How long a reply turn lock remains valid."))


class RecallSettings(PluginConfigBase):
    """SnowLuma 与 NapCat 共用的撤回通知处理。"""

    __ui_label__: ClassVar[str] = "撤回增强"
    __ui_order__: ClassVar[int] = 7

    enabled: bool = Field(
        default=True,
        description="同时处理私聊和群聊撤回通知。",
        json_schema_extra=_ui(
            "启用撤回增强", "只有这一个范围开关；开启后私聊和群聊使用相同取消规则。", 0,
            label_en="Enable Recall Handling", hint_en="Use the same cancellation rules for private and group recalls.",
        ),
    )
    cache_summary_enabled: bool = Field(
        default=False,
        description="短期保存本人私聊撤回消息的有限摘要。",
        json_schema_extra=_ui(
            "缓存本人撤回摘要", "默认关闭。开启后只保存已配置用户本人私聊的短文本和媒介类型，不保存任何二进制。", 1,
            label_en="Cache Own Recall Summary", hint_en="Disabled by default; only configured private senders can query their own short summaries.",
        ),
    )
    summary_ttl_minutes: int = Field(
        default=10, ge=1, le=60,
        description="可查询撤回摘要的保留时间。",
        json_schema_extra=_ui(
            "撤回摘要保留（分钟）", "只影响可选摘要；回复取消墓碑至少覆盖消息处理周期。", 2,
            label_en="Recall Summary TTL (min)", hint_en="Retention period for optional private recall summaries.",
        ),
    )
    summary_max_chars: int = Field(
        default=240, ge=40, le=1000,
        description="单条撤回文字摘要的最大长度。",
        json_schema_extra=_ui(
            "撤回摘要长度", "超出部分会截断，媒介只记录类型。", 3,
            label_en="Recall Summary Length", hint_en="Maximum characters stored for one recalled private message.",
        ),
    )


class ModelRoutingSettings(PluginConfigBase):
    """MaiBot 任务路由名，而不是 Provider API Key。"""

    __ui_label__: ClassVar[str] = "模型与成本编排"
    __ui_order__: ClassVar[int] = 19

    fast_task: str = Field(default="utils", description="快速整理任务名。", json_schema_extra=_ui("快速任务", "默认 utils，用于连续话题等轻量任务。", 0, label_en="Fast Task", hint_en="MaiBot task name for lightweight analysis."))
    reasoning_task: str = Field(default="planner", description="推理任务名。", json_schema_extra=_ui("推理任务", "默认 planner，用于搜索规划、自我关联等复杂判断（日程与场景已改走快速任务）。", 1, label_en="Reasoning Task", hint_en="MaiBot task name for reasoning."))
    creative_task: str = Field(default="replyer", description="创作任务名。", json_schema_extra=_ui("创作任务", "默认 replyer，用于梦境和叙事文本。", 2, label_en="Creative Task", hint_en="MaiBot task name for narrative generation."))
    schedule_task: str = Field(default="", description="日程任务覆盖。", json_schema_extra=_ui("日程任务覆盖", "留空继承快速任务；日程是结构化 JSON 任务，慢推理模型容易撞 30 秒超时。", 4, label_en="Schedule Override", hint_en="Leave empty to inherit fast task; slow reasoning models hit the 30s timeout."))
    rest_wakeup_task: str = Field(default="", description="判醒任务覆盖。", json_schema_extra=_ui("判醒任务覆盖", "留空继承快速任务。", 5, label_en="Wake Decision Override", hint_en="Leave empty to inherit fast task."))
    continuity_task: str = Field(default="", description="连续话题任务覆盖。", json_schema_extra=_ui("话题整理任务覆盖", "留空继承快速任务。", 6, label_en="Continuity Override", hint_en="Leave empty to inherit fast task."))
    dream_task: str = Field(default="", description="梦境任务覆盖。", json_schema_extra=_ui("梦境任务覆盖", "留空继承创作任务。", 7, label_en="Dream Override", hint_en="Leave empty to inherit creative task."))
    diary_task: str = Field(default="", description="日记任务覆盖。", json_schema_extra=_ui("日记任务覆盖", "留空继承创作任务。", 9, label_en="Diary Override", hint_en="Leave empty to inherit creative task."))
    date_analysis_task: str = Field(default="", description="日期分析任务覆盖。", json_schema_extra=_ui("日期分析任务覆盖", "留空继承快速任务。", 10, label_en="Date Analysis Override", hint_en="Leave empty to inherit fast task."))
    news_task: str = Field(default="", description="新闻整理任务覆盖。", json_schema_extra=_ui("新闻整理任务覆盖", "留空继承快速任务。", 11, label_en="News Digest Override", hint_en="Leave empty to inherit fast task."))
    search_task: str = Field(default="", description="主动搜索任务覆盖。", json_schema_extra=_ui("主动搜索任务覆盖", "留空继承推理任务。", 12, label_en="Search Planning Override", hint_en="Leave empty to inherit reasoning task."))
    relevance_task: str = Field(default="", description="自我关联任务覆盖。", json_schema_extra=_ui("自我关联任务覆盖", "留空继承推理任务。", 13, label_en="Self-association Override", hint_en="Leave empty to inherit reasoning task."))
    group_judgment_task: str = Field(default="", description="群聊公开话题判断任务覆盖。", json_schema_extra=_ui("群聊判断任务覆盖", "留空继承快速任务。", 15, label_en="Group Judgment Override", hint_en="Leave empty to inherit fast task."))
    relay_summary_task: str = Field(default="", description="群聊短摘要任务覆盖。", json_schema_extra=_ui("群转述摘要任务覆盖", "v1.23.6 起摘要已并入群观察判定调用，此字段暂无效果，保留兼容旧配置。", 16, label_en="Relay Summary Override", hint_en="Merged into the group judgment call since v1.23.6; kept for compatibility."))
    creation_outline_task: str = Field(default="", description="创作提纲任务覆盖。", json_schema_extra=_ui("创作提纲任务覆盖", "留空继承推理任务。", 17, label_en="Creation Outline Override", hint_en="Leave empty to inherit reasoning task."))
    creation_body_task: str = Field(default="", description="创作正文任务覆盖。", json_schema_extra=_ui("创作正文任务覆盖", "留空继承创作任务。", 18, label_en="Creation Body Override", hint_en="Leave empty to inherit creative task."))
    creation_review_task: str = Field(default="", description="创作审校任务覆盖。", json_schema_extra=_ui("创作审校任务覆盖", "留空继承推理任务。", 19, label_en="Creation Review Override", hint_en="Leave empty to inherit reasoning task."))
    reading_annotation_task: str = Field(default="", description="阅读批注任务覆盖。", json_schema_extra=_ui("阅读批注任务覆盖", "留空继承创作任务。", 20, label_en="Reading Annotation Override", hint_en="Leave empty to inherit creative task."))
    scene_detail_task: str = Field(default="", description="临近场景细化任务覆盖。", json_schema_extra=_ui(
        "场景细化任务覆盖", "留空继承快速任务；同日程，慢推理模型容易撞 30 秒超时。", 21,
        label_en="Scene Detail Override", hint_en="Leave empty to inherit fast task; slow reasoning models hit the 30s timeout."))


    max_output_tokens: int = Field(default=0, ge=0, description="模型输出 token 上限；0=不限制，由服务商决定。", json_schema_extra=_ui("输出 token 上限", "0=不限制（推荐）。推理模型的思考链占用输出预算，写死小上限会把正文挤成空响应；仅在对成本敏感时设正数。", 22, label_en="Max Output Tokens", hint_en="0 = unlimited (provider decides). Tight caps starve content on reasoning models."))


class UsageSettings(PluginConfigBase):
    """插件模型调用统计。"""

    __ui_label__: ClassVar[str] = "Token 监控"
    __ui_order__: ClassVar[int] = 20

    enabled: bool = Field(default=True, description="记录插件与观察到的 Host 模型用量。", json_schema_extra=_ui("启用 Token 统计", "不限制每日额度，只记录调用、Token、耗时与失败。", 0, label_en="Enable Token Monitoring", hint_en="Record usage without enforcing a daily budget."))
    retention_days: int = Field(default=30, ge=1, le=365, description="明细保留天数。", json_schema_extra=_ui("明细保留天数", "每日聚合可长期保留，调用明细按此清理。", 1, label_en="Detail Retention Days", hint_en="Retention period for individual call records."))


class ScheduleSettings(PluginConfigBase):
    """每日生活框架与临近场景细化设置。"""

    __ui_label__: ClassVar[str] = "日程与场景"
    __ui_order__: ClassVar[int] = 5
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Schedule and Scenes", "description": "Daily framework generation and near-term scene expansion."}
    }

    generate_hour: int = Field(
        default=3, ge=0, le=23,
        description="每天强制重新生成生活框架的小时。",
        json_schema_extra=_ui(
            "日程生成时间（小时）", "0～23 的整数。默认凌晨 3 点；缺少当日日程时仍会立即生成。", 0,
            label_en="Schedule Generation Hour", hint_en="Hour from 0 to 23. Missing schedules are still generated immediately.",
        ),
    )
    detail_lead_minutes: int = Field(
        default=60, ge=10, le=240,
        description="提前多少分钟细化即将开始的生活场景。",
        json_schema_extra=_ui(
            "场景提前细化时间（分钟）", "默认提前 60 分钟细化当前段和下一段，减少模型调用并保持环境新鲜。", 1,
            label_en="Scene Detail Lead Time (min)", hint_en="Expand current and next scenes this many minutes before they start.",
        ),
    )
    generation_retry_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        description="每日日程生成失败后的重试间隔（分钟）。",
        json_schema_extra=_ui(
            "生成重试间隔（分钟）", "首次生成失败后每隔该时间自动重试一次；重启后到期的补偿只执行一次，不逐个补跑间隔。", 2,
            label_en="Generation Retry Interval (min)", hint_en="Wait between failed schedule generation attempts.",
        ),
    )
    generation_max_retries: int = Field(
        default=3, ge=0, le=20,
        description="每日日程生成失败后的最大额外重试次数（不含首次尝试）。达上限后改用本地历史归档随机兜底。",
        json_schema_extra=_ui(
            "生成最大重试次数", "首次尝试之外最多再重试 N 次；默认 3 表示单轮最多 4 次尝试。0 表示失败后立即进入兜底。", 3,
            label_en="Generation Max Retries", hint_en="Extra attempts after the first try before falling back to archived templates.",
        ),
    )
    generation_template_retention_days: int = Field(
        default=30, ge=1, le=3650,
        description="成功生成的日程归档保留天数，按归档时间计算，超期自动清理。",
        json_schema_extra=_ui(
            "日程归档保留天数", "只保留最近 N 天内归档的成功日程模板；随机兜底只会使用未过期的归档。", 4,
            label_en="Archive Retention Days", hint_en="Keep successfully generated schedule archives for this many days.",
        ),
    )
    template_file: str = Field(
        default="mai_template.json",
        description="日程骨架模板文件名。",
        json_schema_extra=_ui(
            "日程模板文件", "相对于插件目录。LLM 失败或返回非法日程时使用该模板降级。", 2,
            label_en="Schedule Template File", hint_en="Path relative to the plugin directory, used as generation fallback.", placeholder="mai_template.json",
        ),
    )


class ProactiveSettings(PluginConfigBase):
    """主动私聊的公共冷却和评分阈值；每日额度只在用户档案中配置。"""

    __ui_label__: ClassVar[str] = "主动私聊"
    __ui_order__: ClassVar[int] = 6
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Proactive Private Chat", "description": "Shared cooldown and candidate scoring settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用主动私聊巡检。",
        json_schema_extra=_ui(
            "启用主动私聊", "关闭后麦麦仍维护生活状态，但不会主动找任何用户。", 0,
            label_en="Enable Proactive Chat", hint_en="Life simulation continues, but Mai will not proactively message users.",
        ),
    )
    patrol_interval_minutes: int = Field(
        default=10, ge=1, le=60,
        description="检查主动聊天契机的间隔。",
        json_schema_extra=_ui(
            "主动巡检间隔（分钟）", "建议 10 分钟。更短只会增加检查频率，不代表一定发送更多消息。", 1,
            label_en="Proactive Patrol Interval (min)", hint_en="How often proactive opportunities are evaluated.",
        ),
    )
    min_interval_minutes: int = Field(
        default=180, ge=1, le=1440,
        description="同一用户两次主动消息之间的最小间隔。",
        json_schema_extra=_ui(
            "主动消息最小间隔（分钟）", "默认 180 分钟，防止短时间连续打扰同一用户。", 2,
            label_en="Minimum Proactive Interval (min)", hint_en="Minimum time between proactive messages to the same user.",
        ),
    )
    recent_user_silence_minutes: int = Field(
        default=30, ge=0, le=240,
        description="用户刚发言后暂停主动消息的时间。",
        json_schema_extra=_ui(
            "用户发言后冷却（分钟）", "用户刚刚聊过时无需再主动开场。默认等待 30 分钟。", 3,
            label_en="Cooldown after User Message (min)", hint_en="Do not start another proactive topic immediately after the user speaks.",
        ),
    )
    pending_expire_seconds: int = Field(
        default=120, ge=30, le=600,
        description="主动触发与 Replyer 回复关联的有效时间。",
        json_schema_extra=_ui(
            "主动回复确认窗口（秒）", "用于判断 Planner 是否真的生成了主动回复。通常无需修改。", 4,
            label_en="Proactive Confirmation Window (sec)", hint_en="Window used to correlate a Planner trigger with an actual Replyer response.",
        ),
    )
    max_retries_per_opportunity: int = Field(
        default=2, ge=0, le=5,
        description="主动候选过期或 Planner 沉默后，同一生活契机最多重新进入候选池的次数。",
        json_schema_extra=_ui(
            "契机重试次数", "Planner 沉默或超时后，同一契机最多重试这么多次；超过后不再重复触发，避免反复打扰。0 表示不重试。", 5,
            label_en="Opportunity Retry Limit", hint_en="How many times one opportunity re-enters the candidate pool after Planner silence or timeout.",
        ),
    )
    minimum_energy: int = Field(
        default=25, ge=0, le=100,
        description="允许普通主动聊天所需的最低精力。",
        json_schema_extra=_ui(
            "主动聊天最低精力", "低于该值时麦麦优先休息，不产生普通主动开场。", 5,
            label_en="Minimum Energy for Proactive Chat", hint_en="Mai prioritizes rest below this energy level.",
        ),
    )
    score_threshold: float = Field(
        default=0.45, ge=0, le=2,
        description="主动候选进入 Planner 终审所需的最低综合分。",
        json_schema_extra=_ui(
            "主动候选分数阈值", "越高越克制。综合考虑契机、精力、心情、关系和用户活跃时间。", 6,
            label_en="Proactive Candidate Score Threshold", hint_en="Higher values make proactive behavior more conservative.",
        ),
    )


class AnnounceSettings(PluginConfigBase):
    """活动播报：把当前活动模板化推送到群聊/私聊。

    名单模式（list_mode）决定群聊/私聊 QQ 号列表的语义：
    - whitelist：列表是允许接收播报的目标，只有名单内的真实会话会收到；
    - blacklist：列表是禁止接收播报的目标，名单外的全部已注册会话都会收到。
    渠道开关（群聊/私聊）与总开关任一关闭即不发送；第一版文案为模板拼接，不调用模型。
    """

    __ui_label__: ClassVar[str] = "活动播报"
    __ui_order__: ClassVar[int] = 22

    enabled: bool = Field(
        default=False,
        description="活动播报总开关。关闭后任何渠道都不会发送播报。",
        json_schema_extra=_ui(
            "启用活动播报", "总开关；关闭后即使渠道开关开启、名单非空也不会发送。", 0,
            label_en="Enable Activity Announce", hint_en="Master switch; nothing is sent while off.",
        ),
    )
    list_mode: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="名单模式：whitelist=列表是允许接收播报的目标；blacklist=列表是禁止接收播报的目标（发给名单外全部已注册会话）。",
        json_schema_extra=_ui(
            "名单模式", "白名单：只有列表内的群聊/私聊会收到播报。黑名单：列表内的群聊/私聊被排除，其余已注册会话都会收到。", 1,
            label_en="List Mode", hint_en="Whitelist: only listed targets receive announce. Blacklist: listed targets are excluded.",
            enum_labels={"whitelist": "白名单（列表=允许）", "blacklist": "黑名单（列表=禁止）"},
        ),
    )
    group_targets: list[str] = Field(
        default_factory=list,
        description="群聊 QQ 号列表（纯数字）。语义随名单模式切换：白名单=允许接收播报的群；黑名单=禁止接收播报的群。",
        json_schema_extra=_ui(
            "群聊 QQ 号列表", "纯数字 QQ 群号，一行一个或逗号分隔。含义由上方名单模式决定；仅在真实已注册的群会话中生效，不创建新会话。", 2,
            label_en="Group QQ List", hint_en="Digit-only QQ group IDs; meaning depends on the list mode.",
        ),
    )
    private_targets: list[str] = Field(
        default_factory=list,
        description="私聊 QQ 号列表（纯数字）。语义随名单模式切换：白名单=允许接收播报的用户；黑名单=禁止接收播报的用户。",
        json_schema_extra=_ui(
            "私聊 QQ 号列表", "纯数字 QQ 号，一行一个或逗号分隔。含义由上方名单模式决定；仅在真实已注册的私聊会话中生效，不创建新会话。", 3,
            label_en="Private QQ List", hint_en="Digit-only QQ user IDs; meaning depends on the list mode.",
        ),
    )
    group_enabled: bool = Field(
        default=False,
        description="群聊播报独立开关。",
        json_schema_extra=_ui("启用群聊播报", "与总开关、群聊 QQ 号列表共同控制群聊渠道。", 4,
                              label_en="Enable Group Channel", hint_en="Per-channel switch for group announce."),
    )
    private_enabled: bool = Field(
        default=False,
        description="私聊播报独立开关。",
        json_schema_extra=_ui("启用私聊播报", "与总开关、私聊 QQ 号列表共同控制私聊渠道。", 5,
                              label_en="Enable Private Channel", hint_en="Per-channel switch for private announce."),
    )
    patrol_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        description="活动播报巡查间隔（分钟）。",
        json_schema_extra=_ui("播报巡查间隔（分钟）", "1～1440。间隔越短活动切换的播报越及时，但检查更频繁。", 6,
                              label_en="Announce Patrol Interval (min)", hint_en="How often the current activity is checked."),
    )
    daily_max_per_group: int = Field(
        default=5, ge=0, le=50,
        description="每个群每日最多成功播报次数；0 表示该渠道当日不发送。黑名单模式下这是控制覆盖面的关键项。",
        json_schema_extra=_ui("单群每日播报上限", "按自然日（配置时区）统计成功发送；0 表示群聊渠道当日禁发。", 7,
                              label_en="Daily Max Per Group", hint_en="Successful announce cap per group per day; 0 disables."),
    )
    daily_max_per_private: int = Field(
        default=3, ge=0, le=50,
        description="每个私聊用户每日最多成功播报次数；0 表示该渠道当日不发送。",
        json_schema_extra=_ui("单用户每日播报上限", "按自然日（配置时区）统计成功发送；0 表示私聊渠道当日禁发。", 8,
                              label_en="Daily Max Per Private", hint_en="Successful announce cap per user per day; 0 disables."),
    )
    skip_sleep: bool = Field(
        default=True,
        description="当前活动为睡眠/午休时不播报。",
        json_schema_extra=_ui("跳过睡眠活动", "睡眠与午休段通常无需打扰群友；关闭后睡眠活动也会播报。", 9,
                              label_en="Skip Sleeping Activities", hint_en="Skip sleep/nap activities when enabled."),
    )
    deduplicate: bool = Field(
        default=True,
        description="同一活动（同日、同类型、同时间段）只向同一目标播报一次；关闭后每次巡查都可能重复发送，慎用。",
        json_schema_extra=_ui("活动去重", "按目标+活动键持久化去重，重载/重启后仍有效。", 10,
                              label_en="Deduplicate", hint_en="Persisted per-target dedup; keep enabled to avoid spam."),
    )
    retry_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        description="发送失败后的最短重试间隔（分钟）。",
        json_schema_extra=_ui("失败重试间隔（分钟）", "发送失败的目标在该间隔内不会被再次尝试。", 11,
                              label_en="Retry Interval (min)", hint_en="Minimum wait before retrying a failed target."),
    )
    template: str = Field(
        default="📢 麦麦现在去「{activity}」啦～{scene}",
        description="播报文案模板，支持 {activity}（活动名）、{scene}（场景，可为空）、{time}（HH:MM）；字面花括号写成 {{ 与 }}。",
        json_schema_extra=_ui(
            "播报文案模板", "占位符：{activity} 活动名、{scene} 场景描述（可为空，自动省略）、{time} 当前 HH:MM。LLM 渲染关闭或失败时直接使用模板文案。", 12,
            label_en="Announce Template", hint_en="Placeholders: {activity}, {scene}, {time}. Used directly when LLM rendering is off or fails.",
        ),
    )
    llm_render_enabled: bool = Field(
        default=False,
        description="启用 LLM 渲染播报文案：模板稿先交给所选模型改写成更拟人的口语再发送；失败自动回退模板文案。",
        json_schema_extra=_ui(
            "LLM 拟人渲染", "开启后每次播报多一次模型调用；模型失败或超时不会阻断播报（回退模板）。", 13,
            label_en="LLM Humanized Rendering", hint_en="Rewrite the template text via LLM for a more natural tone; falls back to the template on failure.",
        ),
    )
    llm_render_task: Literal["replyer", "planner", "utils"] = Field(
        default="replyer",
        description="LLM 渲染使用的宿主模型任务：replyer（回复，最口语）、planner（规划）、utils（工具，最快）。",
        json_schema_extra=_ui(
            "渲染模型任务", "必须是宿主已配置的模型任务名；任务不可用时自动回退模板文案。", 14,
            label_en="Render Model Task", hint_en="Host model task used for rendering; falls back to the template if unavailable.",
            enum_labels={"replyer": "回复模型（replyer）", "planner": "规划模型（planner）", "utils": "工具模型（utils）"},
        ),
    )
    llm_render_max_retries: int = Field(
        default=1, ge=0, le=5,
        description="LLM 渲染失败后的额外重试次数（不含首次尝试）。全部尝试失败后回退模板文案。",
        json_schema_extra=_ui(
            "渲染重试次数", "首次调用之外最多再重试 N 次；0 表示只调用一次。全部失败不影响播报（回退模板）。", 15,
            label_en="Render Max Retries", hint_en="Extra render attempts after the first failure; falls back to the template when exhausted.",
        ),
    )
    llm_render_retry_interval_seconds: int = Field(
        default=10, ge=0, le=300,
        description="LLM 渲染重试之间的冷却间隔（秒）。0 表示立即重试。",
        json_schema_extra=_ui(
            "渲染重试冷却（秒）", "两次渲染尝试之间的等待时间，避免瞬时故障时连续打满模型。0～300。", 16,
            label_en="Render Retry Interval (s)", hint_en="Cooldown between render retries, in seconds.",
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_target_lists(cls, value: Any) -> Any:
        """旧版 6 个列表字段迁移为「名单模式 + 2 个目标列表」。

        group_ids/group_whitelist 中非空者迁移为 group_targets（private 同理）；
        旧黑名单字段的排除语义由 list_mode=blacklist 承担，旧键一律移除，
        避免旧配置文件中的未知字段报错。新字段显式配置时优先于旧值。
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_group = data.pop("group_ids", None) or data.pop("group_whitelist", None)
        legacy_private = data.pop("private_ids", None) or data.pop("private_whitelist", None)
        data.pop("group_blacklist", None)
        data.pop("private_blacklist", None)
        if data.get("group_targets") in (None, [], "") and legacy_group is not None:
            data["group_targets"] = legacy_group
        if data.get("private_targets") in (None, [], "") and legacy_private is not None:
            data["private_targets"] = legacy_private
        return data

    @field_validator("group_targets", "private_targets", mode="before")
    @classmethod
    def normalize_target_ids(cls, value: Any) -> list[str]:
        """清洗名单：只保留纯数字 QQ 号，去重保序；支持逗号/空格分隔字符串。"""
        if value is None:
            return []
        items: list[Any] = value if isinstance(value, list) else re.split(r"[,，\s]+", str(value))
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            if not text.isdigit():
                raise ValueError(f"活动播报名单必须是纯数字 QQ 号: {text[:24]}")
            if text not in result:
                result.append(text)
        return result

    @field_validator("template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("播报文案模板不能为空")
        return text


class LinkageSettings(PluginConfigBase):
    """与其它插件的联动开关；目标插件未安装时开关不生效。"""

    __ui_label__: ClassVar[str] = "插件联动"
    __ui_order__: ClassVar[int] = 21

    drawpic_enabled: bool = Field(
        default=False,
        description="启用麦麦绘图联动：聊天中麦麦想分享日常生活画面时，可调用麦麦绘图插件（maimai-drawpic-plugin）的工具生成并发送。",
        json_schema_extra=_ui(
            "启用麦麦绘图联动",
            "默认关闭；需已安装麦麦绘图插件。检测仅确认插件已加载（不校验其版本与绘图平台 Key），注入时另需 draw 工具对当前对话可见，否则不生效。",
            0, label_en="Drawpic Linkage",
            hint_en="Off by default; requires the maimai-drawpic-plugin installed. Detection confirms the plugin is loaded only (no version or platform-key check); injection also requires the draw tool to be visible in the current chat.",
        ),
    )


class MaiLifeSettings(PluginConfigBase):
    """麦生活完整配置。"""

    plugin: PluginSettings = Field(
        default_factory=PluginSettings,
        json_schema_extra=_ui("插件设置", "总开关、模型与管理员。", 0, label_en="Plugin Settings", hint_en="Switch, model and administrators."),
    )
    users: UsersSettings = Field(
        default_factory=UsersSettings,
        json_schema_extra=_ui("私聊用户", "配置允许互动和主动私聊的用户。", 1, label_en="Private Users", hint_en="Users allowed to interact with Mai Life."),
    )
    environment: EnvironmentSettings = Field(
        default_factory=EnvironmentSettings,
        json_schema_extra=_ui("环境与天气", "时区、城市和天气刷新。", 2, label_en="Environment and Weather", hint_en="Timezone, location and weather refresh."),
    )
    state: StateSettings = Field(
        default_factory=StateSettings,
        json_schema_extra=_ui("生活状态", "精力、饥饿、健康和周期。", 3, label_en="Life State", hint_en="Energy, hunger, health and body cycle."),
    )
    memory: MemorySettings = Field(
        default_factory=MemorySettings,
        json_schema_extra=_ui("生活记忆", "梦境碎片、抽象日记和重要日期。", 4,
                              label_en="Life Memory", hint_en="Dream fragments, diary and important dates."),
    )
    rest_gate: RestGateSettings = Field(
        default_factory=RestGateSettings,
        json_schema_extra=_ui("休息回复闸门", "睡眠和午休期间的判醒逻辑。", 5, label_en="Rest Reply Gate", hint_en="Wake decisions during sleep and naps."),
    )
    context: ContextSettings = Field(
        default_factory=ContextSettings,
        json_schema_extra=_ui("被动回复增强", "关系、状态、意图和连续话题。", 6, label_en="Passive Reply Context", hint_en="Relationship, state, intent and conversation continuity."),
    )
    debounce: DebounceSettings = Field(
        default_factory=DebounceSettings,
        json_schema_extra=_ui("消息收口防抖", "私聊与群聊独立收口及同轮回复防重。", 7, label_en="Message Debounce", hint_en="Separate private/group debounce and duplicate-reply protection."),
    )
    recall: RecallSettings = Field(
        default_factory=RecallSettings,
        json_schema_extra=_ui("撤回增强", "撤回通知取消回复与可选本人摘要。", 8,
                              label_en="Recall Handling", hint_en="Cancel replies for recalled messages and optionally retain own summaries."),
    )
    schedule: ScheduleSettings = Field(
        default_factory=ScheduleSettings,
        json_schema_extra=_ui("日程与场景", "每日框架和临近场景细化。", 11, label_en="Schedule and Scenes", hint_en="Daily framework and scene expansion."),
    )
    proactive: ProactiveSettings = Field(
        default_factory=ProactiveSettings,
        json_schema_extra=_ui("主动私聊", "公共冷却和评分；每日额度在每个私聊用户中设置。", 12, label_en="Proactive Private Chat", hint_en="Shared cooldown and scoring; daily limits are configured per user."),
    )
    information: InformationSettings = Field(
        default_factory=InformationSettings,
        json_schema_extra=_ui("联网见闻", "公共开关、上下文和自我关联。", 13,label_en="Connected Discovery",hint_en="Shared switches, context and self-association."),
    )
    search_api: SearchAPISettings = Field(
        default_factory=SearchAPISettings,
        json_schema_extra=_ui("联网搜索服务", "博查、Tavily、You.com 与自定义联网中转的有序降级链。", 14,
                              label_en="Search API Providers",hint_en="Ordered failover for built-in and custom web-search providers."),
    )
    news: NewsSettings = Field(
        default_factory=NewsSettings,
        json_schema_extra=_ui("新闻阅读", "按兴趣低频调用统一搜索 API。", 15,label_en="News Reading",hint_en="Low-frequency interest-based reading through the shared search API."),
    )
    search: SearchSettings = Field(
        default_factory=SearchSettings,
        json_schema_extra=_ui("主动搜索", "在空闲日程中使用统一搜索 API 探索。", 16,label_en="Proactive Search",hint_en="Explore through the shared search API during free time."),
    )
    social: SocialSettings = Field(
        default_factory=SocialSettings,
        json_schema_extra=_ui("社交转述", "QQ群号白名单、显式转述和群转私边界。", 17,
                              label_en="Social Relay", hint_en="QQ group allowlists, relay and group-to-private boundaries."),
    )
    creation: CreationSettings = Field(
        default_factory=CreationSettings,
        json_schema_extra=_ui("书柜与创作", "作品、日记、阅读批注和分阶段创作。", 18,
                              label_en="Bookshelf and Creation", hint_en="Works, diaries, reading notes and staged creation."),
    )
    models: ModelRoutingSettings = Field(
        default_factory=ModelRoutingSettings,
        json_schema_extra=_ui("模型与成本编排", "基础任务路由和高级覆盖。", 19, label_en="Model Routing", hint_en="Base task routes and per-task overrides."),
    )
    usage: UsageSettings = Field(
        default_factory=UsageSettings,
        json_schema_extra=_ui("Token 监控", "调用次数、Token、耗时和失败统计。", 20, label_en="Token Monitoring", hint_en="Calls, tokens, latency and failure statistics."),
    )
    linkage: LinkageSettings = Field(
        default_factory=LinkageSettings,
        json_schema_extra=_ui("插件联动", "与其它插件的协作开关；目标插件未安装时不生效。", 21, label_en="Plugin Linkage", hint_en="Collaboration switches; ineffective when the target plugin is absent."),
    )
