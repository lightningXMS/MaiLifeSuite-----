<div align="center">

<img src="icon.png" alt="MaiLifeSuite 图标" width="128" height="128">

# 麦麦生活套件（MaiLifeSuite）

![Plugin Version](https://img.shields.io/badge/Plugin-1.2.1-informational.svg) ![MaiBot](https://img.shields.io/badge/MaiBot-1.3.6%E2%80%931.3.99-blue.svg) ![Plugin SDK](https://img.shields.io/badge/Plugin%20SDK-2.15%E2%80%932.19-blueviolet.svg) ![License](https://img.shields.io/badge/License-AGPL--3.0-green.svg)

由 [Mai_life（麦生活）](https://github.com/octmicy/Mai_life) v1.23.7 与 [MaiTrace（麦时迹）](https://github.com/Rabbit-Jia-Er/MaiTrace) v6.3.0 合并而成的**单一插件**：持续生活状态、每日作息、睡眠梦境、消息体验、联网见闻、书柜创作、QQ 空间分享与互动，以及**活动播报**（白名单/黑名单 + LLM 拟人渲染）。

> 合并作品整体遵循 **AGPL-3.0-only**（含 MaiTrace AGPL 代码）；麦生活部分保留其 MIT 许可与版权，见 [LICENSE](LICENSE) 与 [LICENSES/](LICENSES/)。

</div>

---

## 目录

- [功能总览](#功能总览)
- [环境要求与兼容性](#环境要求与兼容性)
- [安装与首次配置](#安装与首次配置)
- [活动播报](#活动播报)
- [日程联动机制](#日程联动机制)
- [命令参考](#命令参考)
- [Tool（模型工具）参考](#tool模型工具参考)
- [公开 API 参考](#公开-api-参考)
- [Hook 与组件注册](#hook-与组件注册)
- [WebUI](#webui)
- [配置参考](#配置参考)
- [配置热更新](#配置热更新)
- [数据目录与持久化](#数据目录与持久化)
- [常见问题与排查](#常见问题与排查)
- [升级、备份与卸载](#升级备份与卸载)
- [已知限制与未验证功能](#已知限制与未验证功能)
- [开发与维护脚本](#开发与维护脚本)
- [依赖、许可证与上游项目](#依赖许可证与上游项目)

---

## 功能总览

| 领域 | 功能 | 来源 |
| --- | --- | --- |
| 生活状态 | 精力/饥饿/心情/健康/睡眠相位推进、身体周期、心境基线回归 | 麦生活 |
| 生活框架 | 每日生活框架生成（LLM，失败自动重试 + 历史归档随机兜底）、临近节点场景细化 | 麦生活 |
| **活动播报** | 当前活动模板化推送到群聊/私聊：白名单/黑名单双模式、持久化去重、每日限额、失败重试冷却、可选 LLM 拟人渲染 | 合并 |
| 睡眠体验 | 道晚安、推迟入睡、被叫醒宽限、梦境碎片、抽象日记 | 麦生活 |
| 消息体验 | 私聊/群聊消息防抖收口、同轮防重、撤回取消回复、可选撤回摘要 | 麦生活 |
| 休息闸门 | 睡眠/午休期间私聊判醒（概率/LLM 两种模式，支持按日程段型判定、受控群白名单） | 麦生活 |
| 关系与主动 | 每用户关系温度、活跃时段、免打扰、每日主动额度、契机评分巡检 | 麦生活 |
| 联网见闻 | 有序降级搜索链（Playwright/Bing 免 Key + API 备援）、低频新闻阅读、主动搜索、网页截图 | 麦生活 |
| 书柜创作 | 闲暇创作巡检、作品归档、书柜续写、外部阅读批注 | 麦生活 |
| 社交 | 群聊匿名观察、低频群转私分享、显式转述 | 麦生活 |
| 日程引擎 | LLM 生成每日作息（多轮+质量评分）、临时约定（角色裁判）、约定提醒、活动频率调节、次日策略推断 | 麦时迹 |
| 日程注入 | Planner/Replyer 背景注入、意图分类、闲聊概率优化 | 麦时迹 |
| QQ 空间 | 主题发说说、浏览/点赞/评论/回复、自动发帖例程、聊天素材、AI 配图（绘卷）或表情包 | 麦时迹 |
| 生活层（麦时迹） | 独立生活状态、场景/梦境/私人日记、重要日期、生活主动私聊 | 麦时迹 |
| 合并协同 | 单一日程时间线（进程内桥接）、统一 Hook 入口、合并公开 API、统一数据目录 | 合并 |

**日程与生活的关系（合并核心）**：麦生活的"每日生活框架"是麦麦唯一的时间线真源；麦时迹的日程引擎默认切换为进程内读取该框架（`schedule.provider = "mai_life_local"`），两者不再各跑一份日程。麦时迹原有的内置日程引擎保留为 `internal` 可选提供方，用于麦生活框架不可用时的降级。

---

## 活动播报

把麦麦**当前活动**按模板推送到指定群聊/私聊，默认全部关闭。

| 能力 | 说明 |
| --- | --- |
| 名单模式 | `announce.list_mode`：`whitelist`（列表=允许目标） / `blacklist`（列表=排除对象，名单外全部已注册会话收到） |
| 渠道独立 | `group_enabled` / `private_enabled` 与群聊/私聊 QQ 号列表分别控制 |
| 持久化去重 | 同一活动（日期+类型+时间窗）对同一目标只播报一次，插件重启后仍有效；`deduplicate=false` 时按日重播 |
| 每日限额 | 单群/单用户独立统计成功发送次数，0 表示禁发 |
| 失败重试 | 发送失败进入冷却，按 `retry_interval_minutes` 间隔重试；同一目标存在多条历史会话时逐候选回退 |
| LLM 拟人渲染 | `llm_render_enabled` 开启后模板稿先交给所选模型任务（`replyer`/`planner`/`utils`）改写成口语短句；支持重试次数与冷却秒数，失败自动回退模板文案 |
| 目标解析 | 只使用宿主已注册的真实会话（不创建新会话）；同目标多会话按 scope 择优并逐候选回退，规避旧实例会话路由失败 |

关键配置示例：

```toml
[announce]
enabled = true
list_mode = "whitelist"        # whitelist=列表即允许目标；blacklist=列表即排除对象
group_targets = ["123456789"]
private_targets = ["1001"]
group_enabled = true
private_enabled = true
llm_render_enabled = true      # 可选：LLM 拟人渲染
llm_render_task = "replyer"    # replyer / planner / utils
llm_render_max_retries = 1
llm_render_retry_interval_seconds = 10
```

旧版 `group_ids`/`private_ids`/`*_whitelist`/`*_blacklist` 字段在加载时自动迁移到 `group_targets`/`private_targets`，无需手动修改配置。

---

## 环境要求与兼容性

| 项 | 要求 |
| --- | --- |
| MaiBot 宿主 | `>=1.3.6`（manifest 声明 1.3.6–1.3.99；已在 1.3.15 一键包宿主实测加载，1.3.18 的依赖解析冲突已在本版本解除） |
| 插件 SDK | `maibot-plugin-sdk >=2.15.0, <2.20`（宿主自动校验；MaiBot 1.3.18 要求 SDK ≥2.19.0） |
| Python | `>=3.12`（随宿主环境） |
| Python 依赖 | 由宿主按 `_manifest.json` 自动安装：httpx、beautifulsoup4、json5、tzdata、chinese-calendar、lunar-python、playwright |
| 可选手动安装 | `python -m playwright install chromium`（联网搜索/网页截图需要；不装则自动降级到 API 搜索并记录 `browser_unavailable`） |
| 可选联动 | 麦麦绘卷（空间 AI 配图）、麦麦绘图 maimai-drawpic-plugin（聊天随手拍联动，`linkage.drawpic_enabled`） |

> 兼容性说明：`host_application.min_version` 取上游麦生活的 1.3.6；麦时迹部分在其上游要求 1.3.11+ 的宿主特性时可能受限。已在 MaiBot 1.3.15 一键包环境完成加载、日程生成与 Routine 决策的实测；针对 MaiBot 1.3.18 要求的 maibot-plugin-sdk 2.19.0，已在独立环境通过全量测试套件（451 用例），宿主加载实测以实际部署为准。

---

## 安装与首次配置

1. 将本目录放入 MaiBot 的 `plugins/` 扩展目录（目录名任意，如 `MaiLifeSuite`）。
2. 启动/重启 MaiBot，宿主按 manifest 自动安装 Python 依赖。
3. **不要同时安装上游的 Mai_life 或 MaiTrace 插件**——功能已完全并入本插件，同时安装会导致 Hook 重复处理与双时间线。
4. 在 WebUI 插件配置页（或插件目录下生成的 `config.toml`）完成最小配置：

```toml
[plugin]
admin_user_ids = ["你的QQ"]   # /麦麦 指令管理员（留空时第一个有效私聊用户自动成为管理员）
admin_qq = ["你的QQ"]          # /zn 指令管理员（空间、日程命令）

[[users.profiles]]
user_id = "你的QQ"
enabled = true
role = "owner"                 # 最多一位 owner，其余 friend
daily_proactive_max = 2
```

5. 保存配置并重载插件；私聊发送 `/麦麦状态` 检查服务状态，`/zn help` 查看 /zn 命令。
6. （可选）按需开启：休息闸门 `[rest_gate] enabled`、联网见闻 `[information] enabled`、书柜创作 `[creation] enabled`、空间配图 `[image] enable_image`。
7. （可选）联网见闻使用 Tavily 等 API 服务时，在 `config.toml` 填写服务与 Key（下例为**假 Key 占位**，请到 [Tavily 控制台](https://app.tavily.com) 生成并替换为你自己的真实 Key；Key 只保存在本机 config.toml，不会随插件分发）：

```toml
[[information.providers]]
enabled = true
provider_type = "tavily"
api_keys = ["TAVILY-REPLACE-ME-0000000000000000000000000000"]  # 假 Key 占位，替换为你自己的
```

**默认开关画像（开箱即安全）**：总开关、日程、生活层、防抖、撤回增强为开；联网见闻、新闻、主动搜索、书柜创作、社交转述、绘图联动、生活主动私聊、消息钩子（`messaging.enabled`）为关；自动发帖/浏览默认开但需要 Cookie 登录 + 日程 + 可公开素材才实际执行；关系主动默认开但用户档案为空时无目标。

---

## 日程联动机制

```text
麦生活 ScheduleService（每日框架生成/场景细化/SQLite daily_framework）
        │  进程内直连（mai_life_local，默认，零 RPC）
        ▼
麦时迹 ScheduleEngine 桥（MaiLifeLocalScheduleSource）
        ├── Planner/Replyer 注入（inject 引擎）
        ├── Routine（自动发帖/浏览决策，消费"当前活动+生活公开素材"）
        ├── 约定/提醒/频率调节（provider 为 external 形态时由麦生活管理，local 形态下
        │   generate/update 类 Tool 返回引导信息，重生成走 /麦麦重生日程）
        └── 麦时迹生活层（独立数值状态，与麦生活状态并行推进）
```

- `schedule.provider` 三选一：`mai_life_local`（默认，进程内直连）、`internal`（麦时迹独立日程引擎，与麦生活框架并存——**注意避免双时间线**）、`maibot-community.mai-life`（跨插件 RPC，仅当独立安装 Mai_life 插件时使用）。
- 桥接同时对外保留 `maibot.schedule v1` 协议（`get_schedule_provider_info/get_schedule_snapshot/get_schedule_timeline`），其他插件仍可按协议消费。

---

## 命令参考

### /zn 命令（麦时迹，需 `plugin.admin_qq`；`schedule.admin_users` 非空时还需同时在册）

| 命令 | 说明 |
| --- | --- |
| `/zn help`、`/zn plan help`、`/zn debug help` | 各级帮助 |
| `/zn <主题>` | 生成并发布一条指定主题的说说（可配图） |
| `/zn custom` | 发布 `send.custom_qqaccount` 指定私聊的最新正文（留空禁用） |
| `/zn plan status`、`/zn plan list` | 查看今日作息（文本/图片） |
| `/zn plan generate`、`/zn plan regenerate [要求]` | 补生成/重新生成今日作息（`mai_life_local` 下返回引导，重生成走 `/麦麦重生日程`） |
| `/zn plan update <请求>` | 添加临时约定，如"明天18:00一起吃饭"；角色裁判按人设决定接受与否 |
| `/zn plan delete <ID或序号>`、`/zn plan clear [天数]` | 删除活动/清理旧作息（内置引擎模式） |
| `/zn status`、`/zn life` | 查看生活状态与当前场景 |
| `/zn life diary` | 阅读私人生活日记（主人本人私聊） |
| `/zn dates list`、`/zn dates add YYYY-MM-DD <名称>`、`/zn dates confirm <ID>`、`/zn dates delete <ID>` | 本人重要日期管理（与上游 MaiTrace 行为一致） |
| `/zn ls`、`/zn v [日期]` | 阅读旧版聊天日记（支持 `YYYY-MM-DD`、今天、昨天、前天等写法） |
| `/zn config` | 生活/消息/主动/历法开关摘要 |
| `/zn debug routine`、`/zn debug cookie` | 最近 Routine LLM 决策 / Cookie 状态 |

### /麦麦 命令（麦生活，私聊专用；管理员或已启用档案用户可用）

| 命令 | 英文别名 | 说明 |
| --- | --- | --- |
| `/麦麦`、`/麦麦帮助` | `/mai`、`/mai_help` | 纯文本指令菜单（未识别指令也落入菜单兜底） |
| `/麦麦状态` | `/mai_status` | 生活、消息管线、联网与后台任务状态 |
| `/麦麦配置` | `/mai_config` | 配置摘要（含联动开关三态显示） |
| `/麦麦添加日期 <YYYY-MM-DD> <名称>` | `/mai_date_add` | 添加当前用户重要日期（需启用档案） |
| `/麦麦删除日期 <ID>` | `/mai_date_remove` | 删除当前用户重要日期 |
| `/麦麦确认日期 <ID> [日期]` | `/mai_date_confirm` | 确认日期候选 |
| `/麦麦立即创作` | `/mai_create_now` | 管理员立即执行一次创作判断 |
| `/麦麦转述 <群QQ号> <内容>` | `/mai_relay` | 主人/管理员发起群转述 |
| `/麦麦重生日程` | `/mai_regenerate_schedule` | 管理员重新生成今日日程（推荐的重生成入口） |
| `/麦麦休息测试` | `/mai_rest_test` | 管理员查看休息闸门状态 |

---

## Tool（模型工具）参考

主程序 Planner/Replyer 可按需调用的工具并集（调用权限见对应配置段）：

| Tool | 用途 | 权限/范围 |
| --- | --- | --- |
| `send_feed` | 按主题生成正文、配图并发布说说 | `send.permission`，默认空白名单**禁用** |
| `read_feed` | 读取好友动态并按配置点赞/评论（`enable_comment=false` 可跳过评论） | `read.permission`，默认空黑名单允许 |
| `recall_qzone_activity` | 回忆自己发布/评论/回复过的空间内容 | 共用 `read.permission`，默认返回 10 条 |
| `get_planning_status_v6` | 查询今日作息（缺失时补生成，视 provider） | `schedule.enabled` 与 `allowed_streams` |
| `generate_schedule_v6` | 生成今日作息（`mai_life_local` 下返回引导信息） | 同上 |
| `update_schedule_v6` | 登记临时约定（内置引擎模式；local 下返回引导） | 同上 |
| `mai_life_web_search` | 按需联网搜索（清洗 QQ 号/昵称/邮箱/URL 后查询） | `search_api.tool_enabled` 与 `information.enabled` |
| `mai_life_browse_web` | 打开公网 http/https 网页并截图发到会话（内网拒绝，同页不重复截图） | 同上；需要 Chromium |
| `mai_life_get_schedule` | 获取今日日程文本（群聊作用域，供群轮次补齐日程背景） | 群聊可用 |

---

## 公开 API 参考

命名空间 `maibot-community.mai-life-suite`，其他插件经 `ctx.call_capability("api.call", ...)` 调用（组件收集实测共 25 个 API）。

### 合并实现（原两插件同名 API，麦生活载荷为基底）

| API | 返回约定 |
| --- | --- |
| `get_life_state` | 麦生活 `global_state` 字段（energy/hunger/mood_valence/mood_arousal/health_status/sleep_phase/current_activity 等）+ 麦时迹完整快照在 `trace` 子块（life.enabled 时） |
| `get_current_scene` | 麦生活 `{nodes, current, next, scene}` + 麦时迹 `activity_key/current_activity/activity_kind/current_location` 附加字段与 `trace` 子块 |
| `get_schedule(day)` | 麦生活框架节点字段（start_minute/end_minute/kind/summary/location…）+ ISO 字段（`start_at/end_at/name/description/location/scene`）与麦时迹别名（`start/end/goal_type/goal_id`） |
| `get_environment_snapshot` | 麦生活环境快照（时间/历法/媒介）+ 麦时迹历法快照 `trace` 子块 |
| `create_proactive_opportunity` | 同时路由到关系主动（麦生活）与空间分享契机（麦时迹），返回 `{success, error, results:{mai_life, trace}}` 并保留麦时迹原字段 |

### 麦时迹独有（空间）

`send_feed_api(message, images=[])`（发布自备文图，建议 185s 预算）、`get_feeds_list_api(target_qq, num=5, filter=False)`、`publish_topic_api(topic, current_activity, activity_type, outfit)`（完整生成+配图+发布，建议 1230s 预算）、`get_current_activity`（当前活动快照，供绘卷等消费）。

### 麦生活独有

- **maibot.schedule v1 协议**（public）：`get_schedule_provider_info`、`get_schedule_snapshot(future_limit)`、`get_schedule_timeline(start_at, end_at)`——统一信封 `{success, error, data}`，最大查询跨度 72 小时，只读不生成。
- `get_user_relationship(user_id)`（public）：关系契约（不含 stream/免打扰细节）。
- `admin_snapshot`（private）与 12 个 `webui_*`（private）：WebUI 各页面数据源（状态概览/曲线/见闻/书柜/日程/用户/记忆/日期/用量/配置摘要/管理摘要）。

---

## Hook 与组件注册

合并插件在 **7 个 Hook 点各注册唯一入口**，内部分发（麦生活先行、麦时迹随后；任一方拦截即停止后续）：

| Hook 点 | 麦生活职责 | 麦时迹职责 |
| --- | --- | --- |
| `chat.receive.before_process`（BLOCKING+EARLY） | 撤回通知、命令菜单兜底、私聊/群聊管线（防抖/闸门/观察） | 生活消息观察（`messaging.enabled` 门控） |
| `maisaka.planner.before_request`（BLOCKING） | 主动任务识别、生活上下文注入、撤回/转述边界、绘图联动提示 | Planner 任务追踪、日程注入 |
| `maisaka.planner.after_response`（OBSERVE） | Token 用量记账 | — |
| `maisaka.replyer.before_request`（BLOCKING） | 裁剪生活背景注入 | 回复锚点追踪、日程注入 |
| `maisaka.replyer.after_response`（BLOCKING） | 发送前抑制（撤回/过期轮次/重复回复/朋友边界） | — |
| `send_service.before_send`（BLOCKING） | 最终复核与元数据附加（可 abort） | 休息闸门、撤回取消（`messaging.enabled` 门控） |
| `send_service.after_send`（OBSERVE） | 结算轮次锁/主动事件/休息状态 | 主动额度成功确认 |

组件统计（`get_components()` 实测）：**12 命令、9 Tool、25 API、7 Hook、2 HomeCard**（`mailife_suite_life`、`mai_life_management`），合计 55 个，无重名。

---

## WebUI

`webui.json` 声明 7 个自定义页面（来源于麦生活，麦时迹配置经自动生成的 Schema 呈现于配置页）：

| 页面 | 标题 | 内容 |
| --- | --- | --- |
| overview | 麦麦此刻 | 生活状态、日程定位、休息闸门运行时（workspace 首页） |
| schedule | 日程时间线 | 今日框架时间线、当前/下一节点、场景、明日预览 |
| social | 社交与牵挂 | 用户关系、日期候选、QQ 群配置 |
| inner | 内心世界 | 书柜、日记、梦境、创作状态 |
| information | 见闻与搜索 | 新闻/探索/搜索历史、联网开关与计数 |
| usage | 用量统计 | 按来源/任务的 Token 聚合与近 7 日序列 |
| system | 系统与配置 | 概览计数、主动候选、搜索来源健康、配置摘要 |

配置修改在 WebUI 保存后触发热更新；部分变更（如 `messaging.enabled` 开关、影响 RPC 预算的参数）需重载插件，插件会在日志中明确提示。

---

## 配置参考

配置由 Runner 首次加载时按模型自动生成 `config.toml`（`.gitignore` 已排除）。以下为**全部 31 段 316 项**的逐项清单（默认值取自当前配置模型，与代码一致）。标注 ⚠️ 的为需要特别注意的项。

### [plugin] — 总开关、管理员与空间登录（10 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| config_version | str | 1.11.0 | 配置 Schema 版本，由插件维护，请勿手动修改 |
| enabled | bool | true | 总开关；关闭后停止状态维护、日程生成和主动私聊 |
| llm_model | str | planner | 旧版生活生成任务名；建议改用 `[models]` 编排 |
| admin_user_ids | list | [] | /麦麦 指令管理员；留空时第一个有效私聊用户自动成为管理员 |
| admin_qq | list | [] | /zn 命令管理员（含 help）；空列表禁用全部 /zn 命令 |
| adapter_priority | list | [] | QQ 适配器优先级（完整 plugin_id 或后缀如 `snowluma-adapter`）；空=自动探测 |
| cookie_methods | list | adapter,napcat,clientkey,qrcode,local | 空间 Cookie 获取方式及顺序；刷新失败仍尝试本地缓存 |
| http_host | str | 127.0.0.1 | napcat Cookie 方式的 OneBot HTTP 主机；仅允许本机回环地址（127.0.0.1、::1、localhost），其他地址拒绝连接 |
| http_port | str | 9999 | napcat Cookie 方式的 OneBot HTTP 端口（1–65535 数字字符串） |
| http_token | str | （空） | napcat Cookie 方式的 HTTP 认证 Token；适配器未设认证时留空 |

### [schedule] — 日程与约定（48 项，含麦生活框架参数）

**开关、提供方与会话范围**

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 日程总开关（读取/注入；internal 模式还控制生成、约定、日程主动） |
| provider | enum | mai_life_local | `mai_life_local` 进程内直连麦生活框架（默认）/ `internal` 麦时迹独立引擎 / `maibot-community.mai-life` 跨插件 RPC |
| inject_schedule | bool | true | Planner 决策前追加日程与生活背景 |
| inject_into_replyer | bool | true | Replyer 请求前追加背景（独立开关） |
| auto_generate | bool | true | 仅 internal 模式：查询今天无日程时自动补生成 |
| max_future_activities | int | 3 | 注入背景中列出的未来活动条数（0 不列出） |
| allowed_streams | list | [] | 注入/命令/会话 Tool 的会话白名单；空或 `all` 允许全部。写法 `qq:group:群号`、`qq:private:QQ号`、`session:真实ID` |
| admin_users | list | [] | /zn plan 的额外管理员限制；非空时需同时属于两份名单 |
| timezone | str | Asia/Shanghai | 日程/空间提示词/静默/额度共用的 IANA 时区。⚠️ 需与 `[environment].timezone` 保持一致（两处独立读取） |
| custom_prompt | str | （空） | 日程生成的额外要求（自由文本，非模板） |

**生成质量（internal 模式的 LLM 生成）**

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| use_multi_round | bool | true | 质量不足时多轮生成；修改后重载 |
| max_rounds | int | 2 | 最多轮数（1–5，含首轮） |
| quality_threshold | float | 0.85 | 提前停止分数（0–1）；最终仍需结构与冲突检查 |
| min_activities / max_activities | int | 8 / 15 | 单日活动数量上下限 |
| enable_detailed_description | bool | true | 生成活动详细描述 |
| min_description_length / max_description_length | int | 20 / 50 | 描述字符数上下限 |
| max_tokens | int | 8192 | 单轮生成最大输出 token |
| generation_timeout | float | 180.0 | 生成/裁判/次日推断单次超时（秒）；修改后重载 |
| recent_schedule_days | int | 3 | 生成回看最近日程天数（1–14） |
| cross_day_activity | bool | true | 允许 `23:00-01:30` 跨夜活动 |
| llm_task_name | str | replyer | 生成/裁判/推断/日程主动话题的任务名（与 llm.text_model 独立） |
| history_message_limit | int | 30 | 生成时聊天背景总条数上限（0 禁用） |
| knowledge_search_limit | int | 5 | 可选知识库检索条数（0 禁用） |
| llm_log_enabled | bool | true | 归档生成与裁判日志到 `schedule/llm_logs/` |
| llm_log_retention_days | int | 7 | 归档保留天数 |

**定时生成与次日策略**

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| auto_schedule_enabled | bool | true | 每天到点生成缺失的当日日程 |
| auto_schedule_time | str | 00:30 | 自动生成时间（HH:MM，按 schedule.timezone；启动已过点也会检查） |
| auto_infer_next_day_prompt | bool | false | 前夜推断次日额外要求（只存策略） |
| infer_time | str | 22:30 | 次日推断执行时间 |
| infer_lookback_days | int | 3 | 推断回看天数（1–7） |
| infer_max_prompt_chars | int | 300 | 推断结果最大字符数 |
| infer_use_completion_signal | bool | true | 推断参考活动完成状态 |

**约定（角色裁判）**

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| role_judge_enabled | bool | true | 按人设判断接受/拒绝约定；关闭仍校验日期/时长/冲突 |
| role_judge_temperature | float | 0.3 | 裁判采样温度（0–2） |
| cache_ttl | int | 300 | 快照与注入优化历史有效期（秒） |

**日程主动与频率调节**（需 `proactive_streams`，空列表=三项全部禁用）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| proactive_streams | list | [] | 约定提醒/日程主动/频率调节的会话名单。⚠️ 空列表完全禁用这三项（不支持 all） |
| enable_proactive_trigger | bool | true | 约定开始后前 5 分钟提交提醒（同一约定每天每会话一次） |
| activity_proactive_enabled | bool | true | 合适活动生成自然话题交 Planner（工作/学习/睡眠跳过） |
| activity_proactive_cooldown_minutes | int | 180 | 每会话普通日程主动最短间隔（10–10080） |
| activity_proactive_daily_max | int | 2 | 每会话每天普通日程主动上限（0–20；0 禁用） |
| activity_proactive_quiet_start / _end | str | 23:00 / 08:00 | 普通日程主动静默窗（支持跨夜；相同=不设静默） |
| enable_frequency_modulation | bool | true | 按活动调节名单会话聊天频率（睡眠≈0.05，工作/学习≈0.3） |

**麦生活框架参数（合并新增）**

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| generate_hour | int | 3 | 麦生活每日强制重生成框架的小时 |
| detail_lead_minutes | int | 60 | 提前多少分钟细化临近场景 |
| template_file | str | mai_template.json | 框架生成降级模板（相对插件目录） |

### [environment] — 时区、天气与历法（4 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| timezone | str | Asia/Shanghai | 麦生活使用的 IANA 时区。⚠️ 与 `[schedule].timezone` 需一致 |
| city | str | Shanghai | Open-Meteo 天气城市（自动解析坐标） |
| weather_refresh_minutes | int | 30 | 天气缓存刷新间隔 |
| calendar_enabled | bool | true | 麦时迹本地历法开关（节假日/农历/节气；依赖 chinese-calendar、lunar-python） |

### [users] — 私聊用户（1 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| profiles | list | [] | 用户档案数组，子字段：`user_id`（QQ 号）、`enabled`、`role`（owner/friend，最多一位 owner）、`daily_proactive_max`（0–20 硬上限）、`initial_temperature`（30）、`quiet_start/quiet_end`（00:00/08:00 免打扰）、`proactive_enabled`（true）、`group_to_private_enabled`（friend 默认关，owner 默认开） |

### [state] — 生活状态与身体周期（5 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| tick_interval_minutes | int | 10 | 状态推进间隔 |
| body_cycle_enabled | bool | false | 身体周期模拟开关 |
| body_cycle_start_date | str | （空） | 周期第 1 天日期（YYYY-MM-DD） |
| body_cycle_length_days | int | 28 | 完整周期长度 |
| body_cycle_period_days | int | 5 | 经期持续天数 |

### [memory] — 生活记忆（12 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 离线生活记忆结算总开关 |
| dream_fragments_enabled | bool | true | 夜间睡眠后保存梦境碎片 |
| dream_fragment_count | int | 3 | 每次梦境最多碎片数 |
| dream_probability | float | 0.35 | 醒来后实际生成梦境概率 |
| dream_weekly_max | int | 0 | 近 7 天梦境落库上限（0=不限） |
| diary_enabled | bool | true | 每天生成前一天抽象日记 |
| diary_hour | int | 2 | 结算前一天日记的小时 |
| diary_max_chars | int | 1200 | 单篇日记最大字符数 |
| important_dates_enabled | bool | true | 识别并维护重要日期 |
| date_model_analysis_enabled | bool | false | 规则未识别时调用快速模型分析 |
| date_candidate_retention_days | int | 90 | 未确认日期候选保留天数 |
| date_reminder_lead_days | list | 30,7,1,0 | 提前提醒天数 |

### [rest_gate] — 休息回复闸门（20 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 睡眠/午休判醒闸门总开关（建议日程稳定后开启） |
| mode | enum | probability | `probability` 概率判醒 / `llm` 模型判醒 |
| gate_mode | enum | time_window | `time_window` 按时间窗 / `schedule` 按日程段型判定 |
| wake_probability | float | 0.18 | 概率模式的叫醒概率 |
| llm_threshold | int | 70 | LLM 判醒分数阈值 |
| awake_grace_minutes | int | 30 | 被叫醒后的免判醒宽限 |
| gate_segment_types | list | sleep,nap | schedule 模式下触发闸门的段型 |
| force_wake_terms | list | 救命,急事,报警,醒醒… | 强制唤醒词（无视勿扰词与概率） |
| night_start / night_end | str | 22:30 / 08:00 | 夜间闸门时间窗 |
| nap_start / nap_end | str | 12:00 / 14:30 | 午休闸门时间窗 |
| group_enabled | bool | false | 群聊闸门总开关 |
| group_mode | enum | all | `all` 全部群 / `selected` 仅开启夜间静音的群 |
| group_night_start / group_night_end | str | 22:30 / 08:00 | 群夜间闸门 |
| group_nap_start / group_nap_end | str | 12:00 / 14:30 | 群午休闸门 |
| group_gate_segment_types | list | sleep,nap | 群 schedule 模式段型 |
| group_force_wake_terms | list | 救命,急事,报警,醒醒… | 群强制唤醒词 |

### [context] — 被动回复增强（7 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 向配置私聊注入生活与关系背景 |
| continuity_enabled | bool | true | 未完话题元数据维护 |
| continuity_interval_minutes | int | 10 | 连续话题整理最小间隔 |
| owner_only_terms | list | 主人,老公,老婆,亲爱的主人 | 朋友回复中禁止的主人专属称呼 |
| background_cache_minutes | int | 5 | 慢变背景块会话级缓存（0=实时） |
| replyer_detail | enum | full | `full` 完整 / `compact` 精简 Replyer 背景 |
| prompt_max_chars | int | 4000 | Planner 生活背景最大字符数 |

### [debounce] — 消息防抖（17 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 私聊防抖总开关 |
| text_wait_seconds / image_wait_seconds / forward_wait_seconds | float | 1.2 / 3.0 / 2.0 | 私聊各类消息收口窗口 |
| max_wait_seconds | float | 6.0 | 私聊整轮最长等待 |
| group_enabled | bool | false | 群聊防抖开关（建议观察群回复节奏后再开） |
| group_text_wait_seconds / group_image_wait_seconds / group_forward_wait_seconds | float | 0.8 / 2.0 / 1.2 | 群聊各类收口窗口 |
| group_max_wait_seconds | float | 4.0 | 群聊整轮最长等待 |
| max_messages | int | 12 | 单轮最多合并消息数 |
| max_media_bytes | int | 8388608 | 单轮媒体上限（Base64 解码后字节） |
| merge_separator | str | \n | 多条文本合并分隔符 |
| ignore_empty_message | bool | true | 无主体内容的消息直接放行 |
| log_detail | bool | true | 防抖过程日志 |
| outbound_turn_guard | bool | true | 同一消息只允许一次独立 Replyer 回复 |
| turn_expire_seconds | int | 120 | 轮次锁过期时间 |

### [recall] — 撤回增强（4 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 处理私聊与群聊撤回通知（取消未发送回复） |
| cache_summary_enabled | bool | false | 短期保存本人私聊撤回摘要。⚠️ 隐私敏感，按需开启 |
| summary_ttl_minutes | int | 10 | 撤回摘要保留时长 |
| summary_max_chars | int | 240 | 单条摘要最大长度 |

### [proactive] — 关系主动（8 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 主动私聊巡检开关（实际发送还受档案额度/静默/精力约束） |
| patrol_interval_minutes | int | 10 | 契机检查间隔 |
| min_interval_minutes | int | 180 | 同一用户两次主动最小间隔 |
| recent_user_silence_minutes | int | 30 | 用户发言后的暂停时长 |
| pending_expire_seconds | int | 120 | 主动触发与回复关联窗口 |
| max_retries_per_opportunity | int | 2 | 契机重试次数（0 不重试） |
| minimum_energy | int | 25 | 主动聊天最低精力 |
| score_threshold | float | 0.45 | 候选进入 Planner 终审的最低分 |

### [information] — 联网见闻（5 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 联网见闻与搜索工具总开关（需先开启此项再开新闻/搜索） |
| association_enabled | bool | true | 判断外界信息与麦麦的关联 |
| association_threshold | float | 0.65 | 创建主动契机的最低关联分 |
| proactive_share_enabled | bool | true | 高关联见闻创建主动契机 |
| context_item_limit | int | 3 | 被动上下文最多近期见闻数 |

### [search_api] — 搜索服务链（7 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| providers | list | playwright(Bing) | 有序降级服务列表；每项含 provider_type（playwright/bocha/tavily/you/openai_responses/openai_chat）、key、地址等。⚠️ Key 仅保存在本机 config.toml |
| timeout_seconds | float | 12.0 | 单次外部请求超时 |
| max_attempts | int | 12 | 单次逻辑搜索最多外部请求次数 |
| max_results | int | 5 | 单次搜索最多保留结果数 |
| tool_enabled | bool | true | 允许模型按需调用搜索工具（还需 information.enabled） |
| history_enabled | bool | true | 本地记录搜索历史 |
| history_retention_days | int | 30 | 搜索历史保留天数 |

### [news] — 新闻阅读（7 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 低频新闻阅读开关 |
| daily_max | int | 1 | 每天最多阅读次数 |
| full_text_count | int | 3 | 最多读取正文的结果数 |
| max_concurrency | int | 2 | 正文读取并发 |
| max_article_chars | int | 8000 | 正文最大字符数 |
| retention_days | int | 7 | 正文与摘要保留天数 |
| allowed_schedule_types | list | leisure,rest | 允许阅读的日程类型 |

### [search] — 主动搜索（5 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 主动搜索开关 |
| daily_max | int | 1 | 每天最多次数 |
| include_chat_topics | bool | false | 允许用匿名未完话题规划搜索 |
| allowed_schedule_types | list | leisure,rest | 允许搜索的日程类型 |
| note_retention_days | int | 30 | 探索笔记保留天数 |

### [social] — 社交转述（11 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 社交转述总开关（需配置真实群号） |
| owner_group_to_private_enabled | bool | true | 允许向主人低频分享群聊公开话题 |
| observation_wait_seconds | float | 3.0 | 群话题收口等待 |
| max_buffer_messages | int | 20 | 单话题最多暂存消息数 |
| inactivity_hours | float | 6.0 | 群转私前用户需离群的最短时间 |
| group_share_daily_max | int | 1 | 每用户每日群转私候选上限 |
| group_share_min_interval_minutes | int | 360 | 群转私候选最小间隔 |
| interesting_threshold | float | 0.65 | 进入候选的最低分 |
| summary_retention_hours | int | 72 | 群聊匿名短摘要保留时长 |
| relay_pending_seconds | int | 120 | 显式转述关联窗口 |
| groups | list | [] | QQ 群白名单 |

### [creation] — 书柜与创作（23 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 闲暇创作巡检开关 |
| plaintext_storage_acknowledged | bool | false | ⚠️ 确认作品与私人文本以 SQLite **明文**保存（开启创作的必要确认） |
| patrol_interval_minutes | int | 60 | 创作巡检间隔 |
| daily_max | int | 1 | 每日最多归档作品数 |
| minimum_energy | int | 35 | 允许创作的最低精力 |
| allowed_schedule_types | list | leisure,rest | 允许创作的日程类型 |
| work_types | list | novel_fragment,poem,essay,… | 允许的作品类型 |
| public_works_enabled | bool | true | 无私密来源的作品可公开给朋友 |
| create_share_opportunity | bool | true | 归档后生成低频分享契机 |
| inspiration_lookback_days | int | 7 | 灵感回看天数 |
| max_body_chars | int | 4000 | 作品正文最大字符数 |
| external_reading_enabled | bool | false | 从外部插件 API 读取素材 |
| external_reading_api_name | str | （空） | 外部阅读插件公开 API 名 |
| external_reading_max_items | int | 3 | 每次最多素材数 |
| reading_annotation_enabled | bool | true | 外部阅读生成第一人称批注 |
| continuation_enabled | bool | true | 书柜续写开关 |
| continuation_probability | float | 0.65 | 有资格旧书时选择续写的概率 |
| continuation_min_gap_days | int | 1 | 两章最小间隔天数 |
| continuation_max_chapters | int | 8 | 单书最多章节数 |
| continuation_stale_days | int | 30 | 无新章多少天视为完结 |
| continuation_work_types | list | novel_fragment,screenplay,… | 允许续写的类型（与作品类型取交集） |
| continuation_context_chapters | int | 3 | 续写回看最近章节数 |
| continuation_chapter_tail_chars | int | 400 | 每章回看正文字尾字符数 |

### [models] — 模型与成本编排（20 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| fast_task / reasoning_task / creative_task | str | utils / planner / replyer | 基础任务路由（快速整理/推理/创作） |
| schedule_task、rest_wakeup_task、continuity_task、dream_task、diary_task、date_analysis_task、news_task、search_task、relevance_task、group_judgment_task、relay_summary_task、creation_outline_task、creation_body_task、creation_review_task、reading_annotation_task、scene_detail_task | str | （空） | 各子任务的任务名覆盖；留空回落基础路由 |
| max_output_tokens | int | 0 | 输出 token 上限（0=不限制） |

### [usage] — Token 监控（2 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 记录插件与 Host 模型用量 |
| retention_days | int | 30 | 明细保留天数 |

### [linkage] — 插件联动（1 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| drawpic_enabled | bool | false | 麦麦绘图联动：生活分享时刻注入一次性引导，由麦麦自主决定是否调用 draw 工具（需安装 maimai-drawpic-plugin；群聊不注入，一次最多一张） |

### [persona] — 人设补充（2 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| self_description | str | （空） | 额外身份/外观描述，作用于空间、生活与日程背景；留空时读取已启用绘卷的 selfie.prompt_prefix |
| use_multiple_reply_style | bool | true | 按主程序 multiple_probability 从风格池抽样；关闭或池空时用 reply_style |

### [llm] — 空间与生活模型（3 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| text_model | str | replyer | 空间正文/评论/Routine 决策的任务名（日程生成用 schedule.llm_task_name） |
| llm_timeout_seconds | int | 90 | 空间、生活与素材摘要的单次超时（10–600）；修改后重载 |
| show_prompt | bool | false | INFO 日志打印空间提示词（日程归档由 schedule.llm_log_enabled 控制） |

### [inject] — 注入优化（6 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enable_intent_classification | bool | true | 区分日程问询/闲聊/技术问答/命令；技术/命令跳过注入 |
| enable_state_analysis | bool | true | 补充活动阶段与已进行分钟数 |
| enable_inject_optimization | bool | true | 重复控制与闲聊概率 |
| casual_chat_inject_probability | float | 0.5 | 非连续闲聊的注入概率（0–1） |
| context_max_turns | int | 3 | 每会话注入记录保留轮数 |
| context_ttl | int | 600 | 注入记录有效期（秒） |

### [goal_cleanup] — 日程清理（2 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| cleanup_interval | int | 3600 | 后台清理间隔（秒；启动时也执行一次） |
| cleanup_old_goals_days | int | 30 | 已完成/取消旧作息保留天数 |

### [life] — 麦时迹生活层（18 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | true | 生活层总开关（状态/场景/梦境/私人日记/日期/生活主动；自动发帖的公开素材来源） |
| tick_seconds | int | 60 | 素材/日期/清理巡检间隔（10–3600） |
| offline_hours | int | 72 | 离线后最多回放日程的小时数（1–72） |
| initial_energy / initial_hunger | float | 70 / 20 | 首次建库初值（修改不覆盖已有状态） |
| scene_enabled / dream_enabled / diary_enabled | bool | true | 场景细化 / 梦境 / 私人日记开关 |
| diary_hour | int | 8 | 私人日记整理起始整点 |
| profiles | list | [] | 生活私聊许可档案（子字段：user_id、enabled、role=owner/friend、proactive_enabled、daily_proactive_max、quiet_start/quiet_end） |
| important_dates | list | [] | 重要日期数组（user_id、name、date、yearly、lead_days） |
| private_proactive_enabled | bool | false | 生活素材驱动的主动私聊总开关 |
| private_min_interval_minutes | int | 180 | 同用户生活主动最小间隔 |
| private_silence_minutes | int | 15 | 联系前距用户最近消息的静默要求 |
| private_min_energy | float | 30 | 生活主动最低精力（睡眠相位仍跳过） |
| claim_lease_seconds | int | 1800 | 素材认领租约 |
| private_pending_seconds | int | 300 | 主动任务有效期 |
| opportunity_retention_days | int | 30 | 到期未占用素材的额外保留天数 |

> 说明：`[life]`（麦时迹生活层）与麦生活的 `[state]`/`[memory]`/`[rest_gate]` 是**并存的两套生活实现**（见[已知限制](#已知限制与未验证功能)）。`/zn life diary`、`/zn dates` 使用 `[life]`；睡眠相位判醒（rest_gate）与梦境日记（memory）使用麦生活侧。

### [messaging] — 麦时迹生活消息（6 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | false | 注册生活消息接收/发送前后处理。⚠️ 变更后必须重载插件；生活主动私聊、日期观察、发送结算依赖本项 |
| turn_expire_seconds | int | 300 | 源消息关联与撤回定位保留时长 |
| recall_enabled | bool | true | 按源消息撤回取消未发送回复 |
| rest_gate_enabled | bool | false | 按生活状态拦截已启用档案私聊回复（需 life.enabled）。⚠️ 与麦生活 `[rest_gate]` 功能重叠，**二者只建议开启其一**（默认本项关、麦生活侧闸门为正式入口） |
| wake_probability | float | 0.1 | 该闸门的唤醒概率 |
| awake_grace_minutes | int | 20 | 回复成功后的继续聊天宽限 |

### [send] — 发说说与聊天素材（20 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| permission / permission_type | list/enum | [] / whitelist | send_feed Tool 的 QQ 权限（默认空白名单**禁用**） |
| prompt | str | 内置模板 | 正文模板；占位符 `{current_time} {bot_personality} {bot_expression} {topic} {current_activity}`；字面花括号写 `{{` `}}` |
| history_number | int | 5 | 参考近期说说条数（0 关闭；custom 直发不用） |
| custom_qqaccount | str | （空） | /zn custom 的私聊对象（留空禁用） |
| custom_only_mai | bool | true | custom 只取麦麦发言（false 取对方发言） |
| enable_chat_memory | bool | false | 主题正文使用匿名聊天背景 |
| filter_mode | enum | whitelist | 素材会话过滤：all/whitelist/blacklist |
| target_chats | list | [] | 素材会话名单：`group:群号`、`private:QQ号`、`session:真实ID`。⚠️ 与 schedule 的 `qq:group:` 写法不同 |
| include_private_chat_memory | bool | false | 允许私聊作为匿名背景 |
| time_range / last_hours | enum/int | today / 6 | 素材时间范围（today / 最近 1–72 小时） |
| min_messages / max_messages | int | 5 / 50 | 有效条数门槛与上限 |
| per_message_max_chars | int | 200 | 单条素材字符上限（截断并隐藏号码/邮箱） |
| chat_memory_mode | enum | hybrid | direct/summary/hybrid |
| summary_model | str | utils | 摘要任务名（summary/hybrid 用） |
| summary_prompt | str | 内置模板 | 仅支持 `{chat_logs}` |
| hybrid_recent_count | int | 10 | hybrid 保留的最近片段数 |
| fallback_prompt | str | 内置文案 | 无会话/消息不足时的背景 |

### [image] — 配图（10 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| enable_image | bool | false | 配图总开关（关闭=纯文字） |
| image_mode | enum | random | only_ai / only_emoji / random |
| ai_probability | float | 0.5 | random 模式选 AI 的概率 |
| image_number | int | 1 | 期望图片数（1–4）；修改后重载 |
| pic_plugin_model | str | （空） | 麦麦绘卷 models key（如 model3） |
| clear_image | bool | true | 不新增 AI 图片本地副本 |
| require_image | bool | true | AI 图必须全部成功才发布 |
| image_retry | int | 2 | 每张 AI 图额外重试（0–2，间隔 5 秒）；修改后重载 |
| ai_content_mode | enum | random | random（各 50%）/ selfie / illustration |
| selfie_style | enum | photo | standard/mirror/photo/auto（普通画图忽略） |

### [read] — 读取与评论（8 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| allow_skip_comment | bool | true | 允许模型输出"不回复"跳过评论 |
| permission / permission_type | list/enum | [] / blacklist | read_feed 与 recall Tool 权限（默认空黑名单=允许） |
| read_number | int | 5 | 单次读取条数；修改后重载 |
| like_probability / comment_probability | float | 1.0 / 1.0 | 手动读取的点赞/评论概率 |
| prompt / rt_prompt | str | 内置模板 | 普通/转发说说评论模板（手动与自动共用） |

### [monitor] — 自动浏览（13 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| read_list / read_list_type | list/enum | [] / blacklist | 浏览作者名单及模式（空黑名单=处理全部） |
| like_probability / comment_probability | float | 1.0 / 1.0 | 自动浏览互动概率 |
| silent_hours | str | 22:00-07:00 | 静默时段（多段逗号分隔、支持跨夜；留空全天） |
| like_during_silent / comment_during_silent | bool | false | 静默期互动许可（routine.respect_silent_hours 会先跳过整轮） |
| enable_auto_reply | bool | false | 回复自己说说的新评论 |
| self_read_number | int | 5 | 自评区检查条数 |
| reply_prompt / reply_to_reply_prompt | str | 内置模板 | 评论区回复 / 链式回复模板 |
| processed_feeds_cache_size / processed_comments_cache_size | int | 100 / 100 | 已处理说说/评论去重上限（持久化） |

### [routine] — 日程驱动自动行为（11 项）

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| post_enabled | bool | true | 自动发帖（需 schedule.enabled、life.enabled、当前活动与可公开素材） |
| browse_enabled | bool | true | 自动浏览（需 schedule.enabled 与当前活动） |
| check_interval_minutes | int | 20 | 决策轮间隔 |
| post_cooldown_minutes / browse_cooldown_minutes | int | 120 / 40 | 自动发帖/浏览冷却（不限制手动与 API） |
| respect_silent_hours | bool | true | 静默期跳过整轮决策 |
| post_blocked_activities / browse_blocked_activities | list | sleeping,working,studying,eating,exercising | 禁止发帖/浏览的活动类型硬限制 |
| max_post_chance / max_browse_chance | float | 1.0 / 1.0 | 模型允许后的最终概率 |
| require_reason | bool | true | 模型决策需给理由（供 /zn debug routine 查看） |

---

## 配置热更新

- WebUI 保存配置触发 `on_config_update`：麦生活服务按 scope（self/bot/model）热更新，麦时迹运行时重建（日程、生活、注入、消息、Routine）。
- **必须重载插件**的变更（插件会打日志提示）：`messaging.enabled` 开关（Hook 组件集合）、影响 RPC 预算的参数（`image.image_number/image_retry`、`llm.llm_timeout_seconds`、`read.read_number`、`schedule.generation_timeout/use_multi_round/max_rounds`）。
- 首次加载时 Runner 按配置模型生成 `config.toml` 并自动补齐新增字段；`plugin.config_version` 由插件维护，请勿手改。

---

## 数据目录与持久化

全部数据写入宿主注入的插件专属目录 `data/plugins/maibot-community.mai-life-suite/`（SDK `ctx.paths.data_dir`）：

| 路径 | 内容 |
| --- | --- |
| `mai_life.db` | 麦生活 SQLite：生活状态、关系、日程框架、见闻、书柜、搜索历史、Key 健康指纹（**不保存搜索原始 Key 和图片二进制**） |
| `life/` | 麦时迹生活引擎 SQLite（状态/场景/梦境/日记/日期） |
| `schedule/` | 麦时迹日程数据库（internal 模式的 goals.db）与 `llm_logs/` 模型归档 |
| `images/` | AI 配图本地副本（`image.clear_image=false` 时） |
| `legacy/`、`.maitrace-data-v6.json` | 旧数据迁移标记与归档位 |

- ⚠️ 书柜作品与私人文本为 **SQLite 明文存储**，开启创作需确认 `creation.plaintext_storage_acknowledged=true`；请限制服务器文件权限。
- 搜索 API 原始 Key 仅保存在本机 `config.toml`，数据库/日志/提示词中只出现不可逆指纹。
- 两套数据库（麦生活 + 麦时迹）**有意分库**，不合并表结构，备份时整体拷贝目录即可。

---

## 常见问题与排查

| 现象 | 排查 |
| --- | --- |
| 播报日志出现"未找到目标平台: qq" / `legacy.send.qq 失败` | 目标存在多条历史会话（旧实例 scope）时已自动逐候选回退；请确认适配器已连接且会话 scope 与适配器一致。持续失败时把插件目录下的 `data/plugins/maibot-community.mai-life-suite/mai_life.db` 中该目标的旧会话清理（或直接更新到最新版本，插件会自动择优回退） |
| 活动播报没有发送 | 依次确认：`announce.enabled` 与渠道开关已开 → 名单模式与列表内容正确（白名单=允许，黑名单=排除）→ 去重（同一活动同日只发一次）与每日限额未触达（日志 skipped 中有原因） |
| LLM 拟人渲染没有生效 | `llm_render_enabled` 未开启、`llm_render_task` 不在宿主可用任务列表、或模型连续失败——均自动回退模板文案；渲染调用在宿主日志/插件用量统计可见 |
| 插件未被加载 / "Host 版本不兼容" | 确认宿主版本 ≥1.3.6 且 SDK ≥2.15.0；宿主日志中 `MaiCore 当前版本` 为准（部分一键包实例根残留旧源码/pyproject 会使宿主自报版本偏低，如本测试环境自报 1.3.9——此时需 min_version ≤1.3.9 或清理实例根的遗留源码目录） |
| 启动报"数据库版本高于代码内注册的最新迁移版本" | 实例目录混入了其他 MaiBot 版本的 `src/` 源码，Runner 拉起的 Worker 解析到了旧代码；移出该源码目录（用安装版代码运行） |
| 依赖安装失败 | 检查宿主 Python ≥3.12；Chromium 需手动 `python -m playwright install chromium`（在宿主同一环境执行；Docker 需装进容器） |
| /zn 提示未配置管理员 | 填写 `plugin.admin_qq`；日程命令还需检查 `schedule.admin_users` 与 `allowed_streams` |
| /麦麦 提示仅限私聊 | 所有 /麦麦 指令仅私聊可用；关系/日期等个人功能还需启用 `users.profiles` 档案 |
| 配置保存后不生效 | 见[配置热更新](#配置热更新)——`messaging.enabled` 与预算类参数必须重载插件 |
| 日程一直没有 | 检查 `schedule.enabled` 与 provider：`mai_life_local` 下框架由麦生活生成（看 `/麦麦状态` 与 `schedule.generate_hour`）；`internal` 下看 `/zn plan status` 与 `auto_schedule_time`。`proactive_streams` 为空时无日程主动/提醒/频率调节 |
| 麦麦从不主动私聊 | 按 `[MaiLife] 主动巡检` 日志逐层定位：`proactive_disabled`（开关关）/ `no_eligible_target`（未配置用户档案）/ `no_opportunity`（暂无生活契机）/ `no_stream`（用户未与麦麦建立过私聊，插件不会创建新会话）/ `no_candidate`（静默窗/每日额度/冷却/评分，可在管理摘要 `proactive_skip_summary` 查原因）。启用步骤：WebUI「私聊用户」添加 QQ 并保持启用（默认已开启主动）、`daily_proactive_max>0`，且该用户**先与麦麦私聊过一次**；另需 `proactive.enabled=true` 与精力≥`minimum_energy` |
| 消息没有防抖/闸门效果 | `debounce.group_enabled` 默认关；`rest_gate.enabled` 默认关；`messaging.enabled` 默认关且改后需重载 |
| 自动发帖从不执行 | 需要 Cookie 登录（`/zn debug cookie`）+ 当前活动 + 可公开素材 + 非 `post_blocked_activities` + 冷却/静默/概率；`/zn debug routine` 查看每轮拒绝原因 |
| 模型调用失败 | 插件均有降级路径（日程模板、梦境兜底池、新闻跳过），日志会记录具体原因；确认主程序模型任务名（replyer/utils/planner）已配置 |
| 联网搜索报 browser_unavailable | 未安装 Chromium 或缺少中文字体（截图方块）；按环境要求手动安装 |
| 群聊里麦麦不回应闸门 | 麦生活群闸门需 `rest_gate.group_enabled`；`rest_gate.mode` 与 `gate_mode` 组合是否符合预期 |

---

## 升级、备份与卸载

- **升级**：替换插件目录后重载即可；配置由 SDK 按模型补齐新增字段，已有值保留。
- **备份**：整体拷贝 `data/plugins/maibot-community.mai-life-suite/`（含两套 SQLite 与模型日志）；敏感程度取决于书柜内容与撤回摘要是否开启。
- **卸载**：直接从 `plugins/` 移除目录并重启宿主；数据目录保留，删除即彻底清除。
- **从上游迁移**：本插件**不自动读取/迁移**上游 Mai_life 或 MaiTrace 的数据与配置（两插件的数据目录原样保留、不受影响）。如需保留历史，请先手动备份旧目录，等待后续版本提供迁移工具。

---

## 已知限制与未验证功能

以下事项如实声明（截至 v1.2.1）：

1. **双生活引擎并存**：麦生活 `life_state` 与麦时迹 `life` 层同时运行（消费同一份日程）。活动时间线一致，但精力/心情数值两套独立、场景/梦境/日记双份生成（额外 LLM 开销）。计划在后续版本统一为单引擎。两引擎的私聊用户档案已统一为「私聊用户」单一列表，生活层从其派生联系许可，不再单独配置。
2. **双时区字段**：`[environment].timezone`（麦生活）与 `[schedule].timezone`（麦时迹）独立读取，需用户保持一致；后续收敛为单一字段。
3. `generate_schedule_v6` / `update_schedule_v6` 在默认 `mai_life_local` 下返回引导信息（重生成走 `/麦麦重生日程`）。
4. 已在真实宿主（1.3.15 一键包）**验证**：插件加载/注册、配置生成、LLM 日程框架生成、进程内桥接、Routine 决策、数据落盘、重启恢复、运行期零错误。
5. **未在宿主验证**（仅离线测试或代码审读）：QQ 消息管线与防抖收口的真实消息流、空间发布/评论（需 Cookie 与真实空间）、优雅卸载日志（后台无 Ctrl+C 通道）、WebUI 页面渲染、长时稳定性、约定提醒触发。对应离线测试见 `tests/`（451 用例通过；14 项跳过——1 项需同工作区安装绘卷、13 项为 MaiTrace 旧配置迁移链路的合并改造占位，详见各测试的 skip 说明）。
6. 上游 Mai_life main 分支无测试目录；本仓库测试全部来自 MaiTrace 侧并适配合并结构。
7. Token 消耗：生活/日程/注入/创作会增加模型调用量（上游麦生活实测提示平均 +1M tokens/日 量级），请结合 `[models]` 编排与 `[usage]` 监控控制成本。

---

## 开发与维护脚本

`tools/` 下两个脚本是**开发与维护辅助工具**：不是插件注册组件，插件正常加载时不会自动运行它们，也不参与任何消息处理流程，无需在宿主或配置中开启任何开关。

| 脚本 | 用途与执行效果 | 运行方式与前提 |
| --- | --- | --- |
| `tools/check_secrets.py` | 提交前敏感信息检查：扫描 git 跟踪文件中禁止入库的标识模式（含 Tavily API Key 前缀、曾泄漏的 QQ 号/群号等，具体规则见脚本内 `FORBIDDEN` 清单），并拦截配置备份与 `.env` 类文件名入库。发现命中时以退出码 1 失败并列出命中文件与规则；通过则退出码 0。适用于本仓库开发者提交代码或接入 CI 前 | 在克隆的本仓库目录内执行 `python tools/check_secrets.py`（依赖 git 命令与 Python 3 标准库；脚本按自身位置定位仓库根，不依赖当前工作目录）。规则清单在脚本内 `FORBIDDEN` 维护，新增敏感标识时同步追加 |
| `tools/state_stats.py` | Mai_life 状态动力学统计（**只读**）：读取插件数据库的 `state_snapshots`（每小时一条）与 `mood_events` 表，在控制台输出每日精力/饥饿/心情轨迹、心情起止、睡眠阶段分布、整体均值与心情事件汇总，供长期调参分析；`--csv` 可把逐小时快照导出为 CSV 文件 | `python tools/state_stats.py /path/to/mai_life.db`，可加 `--days N`（默认 30，0=全部）与 `--csv 文件路径`。纯 Python 标准库，无第三方依赖；以只读模式打开数据库（`mode=ro`），不影响运行中的插件。默认相对路径 `data/mai_life.db` 相对当前工作目录解析，建议直接传完整路径（部署实例通常在 `data/plugins/maibot-community.mai-life-suite/mai_life.db`）。快照与事件从部署本版本后开始积累，旧库无历史数据属正常（脚本会提示并正常退出） |

---

## v1.2.1 变更：私聊用户列表统一

此前配置页存在两份重复的私聊用户设置——「私聊用户」（麦生活）与「生活状态与私聊」内的私聊联系许可（麦时迹），QQ/启用/角色/主动私聊许可/每日上限/免打扰时段各配一份且默认值不一致。自 v1.2.1 起**统一为「私聊用户」单一列表**：

- 麦时迹生活层（生活观察、生活主动私聊、私人日记访问角色）运行时从「私聊用户」派生，不再单独配置；该配置项已从配置页移除。
- 免打扰时段与主动私聊许可以「私聊用户」中配置的值为准。
- 迁移提示：此前只在生活层配置过、未加入「私聊用户」的 QQ，请补录到「私聊用户」；重要日期所属用户同样匹配该列表，未匹配的日期在启动日志警告且不产生提醒。
- 两个生活引擎的分工、被动聊天与其他功能不受影响。

---

## 依赖、许可证与上游项目

- 运行依赖（宿主按 manifest 自动安装）：httpx、beautifulsoup4、json5、tzdata、chinese-calendar、lunar-python、playwright。
- 许可证：合并作品 **AGPL-3.0-only**；麦生活部分 MIT（版权声明保留于 LICENSES/Mai_life-MIT.txt）；MaiTrace 引用组件的 MIT 声明见 LICENSES/ 下其余文件。
- 上游项目与致谢：
  - [Mai_life（麦生活）](https://github.com/octmicy/Mai_life) — octmicy，MIT
  - [MaiTrace（麦时迹）](https://github.com/Rabbit-Jia-Er/MaiTrace) — Rabbit-Jia-Er，AGPL-3.0-only
  - [MaiBot](https://github.com/Mai-with-u/MaiBot) 与[插件开发文档](https://docs.mai-mai.org/plugin/)
- 合并版的修复请在本仓库进行；涉及上游功能的根本问题建议同时反馈至上游 Issues。
- 安全提示：Cookie、Token、API Key 只保存在本机 `config.toml` 与空间登录缓存，请限制文件权限；插件日志不会输出明文凭据（适配器 token 在日志中已脱敏为 `***`）。
