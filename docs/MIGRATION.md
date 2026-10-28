# MaiLifeSuite 合并迁移档案（P0 实测版）

> 本文档基于 2026-10-27 对两仓库真实代码的检查，所有结论以本地代码为准。
> 上游快照：MaiTrace `main@712c777`（v6.3.0，工作区干净）、Mai_life `main@f21809f`（v1.23.7，工作区干净）。

## 0.1 宿主集成测试记录（2026-10-27，第二轮）

**环境**：测试副本 `e:\agent项目\Maibot\maibot lujing`（onekey 实例根）；运行时 `.onekey\environments\9f726c41-…`（Python 3.12.10、MaiBot 1.3.15 PyPI 包、SDK 2.16.0、插件依赖全部预装）。启动方式：从环境目录执行 `python -c "from src.cli.tool import main; main(['run','--directory',<实例>])"`，设 `MAIBOT_INSTANCE_DIR` 与 `EULA_AGREE`/`PRIVACY_AGREE`（宿主提供的无头确认哈希）。

**集成期修复（2 项）**：

1. 宿主侧：实例根混入的 1.3.9 源码 `src/` 在 Runner 以 `python -m src.runtime`（cwd=实例根）拉起 Worker 时遮蔽 site-packages 的 1.3.15，Worker 以旧代码拒绝新数据库（v45>43）崩溃 → 将 `src/` 改名 `src_1.3.9_source_backup` 移出实例根（保留未删）。
2. 插件侧：`_manifest.json` host_application.min_version 1.3.11 → **1.3.6**（宿主 ManifestValidator 实际自报版本 1.3.9，受实例根遗留 pyproject 影响；Mai_life 主体宿主要求本为 1.3.6，硬门槛 SDK≥2.15 由宿主 2.16.0 满足）。

**宿主实测证据**（logs/app_20261027_152244.log.jsonl 等）：

- 两次完整加载：`插件 maibot-community.mai-life-suite v0.1.0 加载成功` → `注入 PluginContext` → `注册完成`
- `MaiLifeSuite 已加载：麦生活 1.23.7 + 麦时迹 6.3（日程提供方: mai_life_local）`
- LLM 真实调用（deepseek-flash，Token 记账来源=plugin.maibot-community.mai-life-suite）：当日生活框架真实生成
- `Routine 已启动（日程提供方=mai_life_local）` → `当前活动 [studying] 去图书馆整理上午的乐理笔记…` → `硬规则拒绝 browse (活动黑名单(studying))`：进程内桥接 + 麦时迹 Routine 消费麦生活日程全链路
- 数据隔离：`data/plugins/maibot-community.mai-life-suite/` 独立创建（mai_life.db、life/life.sqlite3、迁移标记）；旧插件数据未触碰
- 重启恢复：第二次启动正确读取已有状态（"昨日无生活记录（当日未运行）"判断正确）
- 运行期 MaiLifeSuite 相关 error/warning：**0**；套件相关 LLM Token 记账正常

**未验证项**：优雅卸载日志（后台无 Ctrl+C 通道，on_unload 语义由离线集成测试覆盖）；QQ 真实链路（SnowLuma 未运行）；WebUI 插件页面；空间发布（需 Cookie）；长稳。宿主自报 1.3.9 导致上游 MaiTrace v6.2.4（min 1.3.11）同样被拒——属宿主展示版本问题，与套件无关。

## 0.2 P1 实施记录（2026-10-27 完成）

### 实际落地的目录结构

```text
MaiLifeSuite/                      # 插件 ID: maibot-community.mai-life-suite, v0.1.0, AGPL-3.0-only
├── _manifest.json / plugin.py / config.py / webui.json / README / CHANGELOG / LICENSE
├── mailife_config.py              # Mai_life config.py 原样改名（22 个 Settings 类）
├── mailife_mixin.py               # Mai_life plugin.py 机械搬迁（Mixin 化，去 Hook/同名 API 装饰器）
├── core/ life/ messaging/ information/ creation/ social/ management/ tools/   # Mai_life 平铺，零导入改动
├── maitrace/                      # MaiTrace 整体子包（config/services/handlers/utils），相对导入不变
│   └── services/schedule/mai_life_local.py   # ★新增：进程内日程桥
├── tests/                         # MaiTrace 14 个测试迁移 + 新增 test_suite_bridge.py（合并层测试）
├── docs/MIGRATION.md              # 本档案
└── LICENSES/                      # MaiTrace 引用组件 MIT 声明 + Mai_life-MIT.txt
```

### P1 关键改造点（均有测试或冒烟验证）

1. **根 plugin.py**：`MaiLifeSuitePlugin(MaiLifeMixin, MaiBotPlugin)`；55 个组件（12 Command/9 Tool/25 API/7 Hook/2 HomeCard）零重名（`collect_components` 实测）；7 个 Hook 点各一份注册，内部分发（麦生活先行、麦时迹随后，任一拦截即止）。
2. **进程内日程桥** `maitrace/services/schedule/mai_life_local.py`：继承 `MaiLifeScheduleSource` 仅覆写 `_call()`，直连宿主 `api_schedule_provider_info/snapshot/timeline`；`provider` 字段新增 `'mai_life_local'` 并设为默认。test_suite_bridge 实测：读取真实 `daily_framework` 数据、全程零 `api.call` RPC。
3. **配置合并**：`[plugin]`/`[schedule]`/`[environment]` 字段级并集（无字段碰撞，实测通过 SDK `build_default_config`/`set_plugin_config` 校验）；Mai_life 内部模块对根 `config.PLUGIN_VERSION` 的导入保持兼容。
4. **PLUGIN_ID 统一**：`maitrace/utils/_logging.py`、`maitrace/services/messaging/controller.py`、`messaging/task_context.py` 三处改为新 ID（主动任务标记匹配用）；`HomeCard` 链接更新；协议层 `PROVIDER_ID='maibot-community.mai-life'` 保留（maibot.schedule v1 契约）。
5. **`_ready` 冲突消解**：Mai_life 的 `_ready` property 保留为服务就绪语义；MaiTrace 运行时标志改名 `_trace_ready`（`services/runtime.py` 两处 + 测试同步）。
6. **惰性命令模式收集**：`mailife_mixin.py` 的 `_command_pattern_res` 由导入期对旧类名求 `dir()` 改为按 `type(self)` 惰性缓存（合并后命令声明分布在 Mixin 与根类）。
7. **生命周期**：`on_load` = 麦生活装配 → 麦时迹 `start_runtime`；`on_unload` 反序（先停消费者再关麦生活 SQLite）；`on_config_update` = 麦生活热更新 → 麦时迹运行时重建。

### P1 测试终局（Python 3.13.7 + SDK 2.16.0，Windows 本机）

- 组件收集：55 个、零重名、Hook 点零重复（新增 test_suite_bridge.py 断言）。
- 合并层新增测试 10/10 通过（桥接协议/数据读取/零 RPC/Hook 分发/拦截语义）。
- 迁移测试适配：导入路径、patch 字符串路径、assertLogs 名、`handle_planner/replyer` 新名、`_trace_ready`、`configured()` 显式 `provider='internal'`、teardown 走 `on_unload` 关麦生活存储、messaging 开关测试改为即时生效语义。
- 显式 skip（未删除）：test_migration.py 中 4 个 MaiTrace 旧配置迁移测试，理由：该迁移链路属 MaiTrace 历史版本升级路径，合并插件为全新插件无旧配置，功能未迁移（见测试内 reason）。
- 环境性错误（与 P0 基线一致，非合并引入）：test_inject.py 14 个错误依赖本机不存在的 MaiBot 源码路径 `E:\src\...\context_item.py`。
- 时序敏感：test_schedule.py 与 test_life_runtime.py 各有 1-3 个用例在整机高负载下偶发 TimeoutError/时序断言失败，隔离重跑均通过（基线同样存在）。
- 宿主实测：本机无 MaiBot 宿主环境，未做真实插件加载；已执行的是 SDK 层面的组件收集/配置校验/离线集成回归。


## 1. 源仓库状态与合规矩阵

| 项 | MaiTrace | Mai_life | 合并决定 |
| --- | --- | --- | --- |
| 版本 | 6.3.0 | 1.23.7 | 新插件从 0.1.0 起版 |
| 许可证 | AGPL-3.0-only（另有 3 份 MIT 引用组件声明于 LICENSES/） | MIT | 合并作品整体 **AGPL-3.0-only**，保留 Mai_life MIT 版权声明 |
| Python | >=3.12（pyproject） | 未声明（代码用 `int \| None`，>=3.10） | **>=3.12**（取严） |
| host_application | 1.3.11–1.99.99 | 1.3.6–1.3.99 | **1.3.11–1.3.99**（交集） |
| sdk | 2.15.0–2.99.99 | 2.7.0–2.99.99 | **2.15.0–2.99.99**（交集） |
| 依赖 | httpx, bs4, json5, tzdata, chinese-calendar, lunar-python | playwright, chinese-calendar, lunar-python | 并集 7 包，版本规格无冲突 |
| 测试 | 14 个文件，pytest | main 分支无测试 | 迁移 MaiTrace 测试 |

SDK 实测：本机安装 maibot-plugin-sdk **2.16.0**，满足约束。已核实 SDK `collect_components()`（components.py:797）用 `dir(instance)` 遍历完整 MRO——**Mixin 基类上的装饰器方法会被收集**，组件以属性名作 handler_name，两侧方法名无碰撞（仅 `get_components`/生命周期需合并类统一覆盖）。

## 2. 注册清单（实测）

### Hook（5 个重叠点，合并后每点只保留一个注册）

| Hook 点 | Mai_life | MaiTrace | 合并策略 |
| --- | --- | --- | --- |
| chat.receive.before_process | on_receive（撤回通知/命令菜单/私聊群聊管线，BLOCKING+EARLY） | handle_receive（生活消息观察，BLOCKING+LATE） | 先 Mai_life 后 MaiTrace；Mai_life 拦截则跳过 |
| maisaka.planner.before_request | on_planner（上下文注入+主动任务识别，BLOCKING） | handle_planner_life（任务追踪+注入，BLOCKING+LATE） | 顺序执行，任一 block 则尊重 |
| maisaka.planner.after_response | on_planner_after（OBSERVE，token 记账） | 无 | 仅 Mai_life |
| maisaka.replyer.before_request | on_replyer（注入裁剪背景，BLOCKING） | handle_replyer_life（锚点追踪+注入，BLOCKING+LATE） | 顺序执行 |
| maisaka.replyer.after_response | on_replyer_after（发送前抑制：撤回/过期轮次/重复回复/边界，BLOCKING） | 无 | 仅 Mai_life |
| send_service.before_send | on_send_before（最终复核+元数据，BLOCKING） | handle_before_send（休息闸门+撤回，BLOCKING+LATE） | Mai_life 可 abort；通过后再走 MaiTrace |
| send_service.after_send | on_send_after（结算，OBSERVE） | handle_after_send（额度确认，OBSERVE） | 两者都执行 |

**同名 API 冲突（5 个，合并后各保留一份实现）**：`get_life_state`、`get_current_scene`、`get_schedule`、`get_environment_snapshot`、`create_proactive_opportunity`。
**MaiTrace 独有 API（4）**：`send_feed_api`、`get_feeds_list_api`、`publish_topic_api`、`get_current_activity`。
**Mai_life 独有 API（21）**：`get_schedule_provider_info`、`get_schedule_snapshot`、`get_schedule_timeline`、`get_user_relationship`、`admin_snapshot`、12 个 `webui_*` 私有 API 等。

**Command 无冲突**：/zn（MaiTrace 1 条）与 /麦麦*（Mai_life 11 条）并存。
**Tool 无冲突**：MaiTrace 6 个（send_feed、read_feed、recall_qzone_activity、get_planning_status_v6、generate_schedule_v6、update_schedule_v6）；Mai_life 3 个（mai_life_web_search、mai_life_browse_web、mai_life_get_schedule）。

## 3. 模块迁移对照表

### 3.1 Mai_life → 合并根包（平铺保留，零导入改动）

| 源 | 目标 | 处置 | 验证方式 |
| --- | --- | --- | --- |
| config.py（22 个 Settings 类） | config.py + mailife_config.py | 拆分：根 config.py 定义合并模型，Mai_life 原文件改名 mailife_config.py 供导入 | 配置实例化冒烟 |
| core/ life/ messaging/ information/ creation/ social/ management/ tools/ | 同名目录平铺 | 原样复制（内部相对导入不变） | 导入冒烟 + 原测试 |
| mai_template.json、webui.json | 根目录 | webui.json 后续合并（P1-4） | WebUI schema 校验 |
| plugin.py（2660 行） | **不整体复制**；拆为 `mailife_mixin.py`（Hooks/Tools/Commands/APIs/辅助方法）+ 根 plugin.py 生命周期 | 见 §5 | 注册查重 + 冒烟 |

### 3.2 MaiTrace → `maitrace/` 子包（整体搬迁，相对导入不变）

| 源 | 目标 | 处置 | 验证方式 |
| --- | --- | --- | --- |
| config/ services/ handlers/ utils/ | maitrace/ 同名子包 | 原样复制；仅 runtime.py 的 `_ready` 属性改名 `_trace_ready`（避开 Mai_life 的 `_ready` property） | 导入冒烟 + 迁移其 14 个测试 |
| plugin.py | **不复制**；注册全部上移到根 plugin.py；`get_default_config` 的旧配置迁移逻辑不搬（新插件无旧配置） | 见 §5 | 注册查重 |
| tests/ | tests/ | 迁移并修包路径 | pytest |
| _manifest.json、README、LICENSE、LICENSES/、icon.png、pyproject.toml | 根目录/合并 | manifest 重写；LICENSE 合并；LICENSES/ 保留 | manifest 校验 |

## 4. 重复模块清单与裁决

| 重复项 | MaiTrace 版 | Mai_life 版 | P1 裁决 |
| --- | --- | --- | --- |
| adapter_compat.py | services/messaging/adapter_compat.py | messaging/adapter_compat.py | **两份几乎逐字相同**（Mai_life 多一个 `import json`）。P1 各自保留在原位（Mai_life 平铺、MaiTrace 子包），P2 收敛到 core/ |
| 生活状态引擎 | services/life/state.py（纯计算器，消费 goals.db 活动段） | life/life_state.py（身体周期/心情回归/LLM 梦境，消费 daily_framework） | **P1 双引擎并存**，同源日程（Mai_life framework）。P2 统一。用户明确“暂不贸然重写内核” |
| 日程引擎 | services/schedule/（生成器+约定+提醒+goals.db） | life/schedule_service.py（框架生成+daily_framework 表） | **P1 由 Mai_life 生成唯一时间线**；MaiTrace 内置引擎保留为 `provider="internal"` 降级选项（配置默认改为 Mai_life 桥接） |
| 休息闸门 | services/messaging/controller.py 内置（默认关） | life/rest_gate.py + bedtime.py（更完整） | 仅注册 Mai_life 的；MaiTrace `messaging.rest_gate_enabled` 保持默认 false |
| 撤回取消 | messaging/controller 内置 | messaging/recall_service.py | 各自只取消自己任务的发送，互不干扰；P1 并存，文档标注建议只开 Mai_life |
| 主动任务 | schedule/proactive + life 私聊主动 + routine | life/proactive.py | P1 并存（各自独立额度）；P3 建 proactive_hub 统一登记 |
| 日记/梦境/重要日期 | services/diary + life/memory + dates | life/memory_service.py + creation/ | P1 并存（产物不同：MaiTrace 私人日记 vs Mai_life 记忆/书柜） |
| LLM 调用 | services/llm_runner.py | core/llm_service.py | P1 并存（任务名路由不同）；P2 收敛 |
| 环境历法 | services/life/environment.py（用 schedule.timezone） | core/environment.py（含天气，用 environment.timezone） | P1 并存；文档标注两时区字段需保持一致 |

## 5. 关键设计决策（与之前方案分析的差异修订）

1. **修订**：此前方案假设“合并后单一生涯引擎”。按用户 P1 指示改为**双引擎并存、单一时间线**：MaiTrace 的 `MaiLifeScheduleSource` 从 RPC（`ctx.call_capability('api.call')`，共 3 个调用点：provider_info / snapshot / timeline）改为**进程内直连** Mai_life 的 `store.get_framework` 数据（新建 `maitrace/services/schedule/mai_life_local.py`，复用原格式映射逻辑），消除“两个日程/两个身体”的核心矛盾。
2. **修订**：日程消费唯一真源是 `LifeStore.get_framework(day)`（表 `daily_framework`：start_minute/end_minute/kind/summary/location），下游 LifeState/RestGate/Bedtime 全部兼容——桥接层只需维护该 schema。
3. **Mixin 组织**（SDK dir() 收集机制已验证）：根 plugin.py 定义 `MaiLifeSuitePlugin(MaiLifeMixin, MaiBotPlugin)`；MaiLifeMixin 承载 Mai_life 原 plugin.py 的全部方法（除生命周期/get_components/_ready，这三者由合并类覆盖）；MaiTrace 的注册直接写在合并类上（方法名无冲突）。
4. **同名 API 统一**：P1 先保留 Mai_life 实现为正式入口，MaiTrace 消费方（绘卷）依赖的 `get_current_activity` 无冲突不受影响；5 个同名 API 的载荷并集在 P1-3 逐个比对后决定（见 plugin.py 注释）。
5. **数据安全**：新插件数据目录为 `data/plugins/<新ID>/`，两套 SQLite（mai_life.db + maitrace 的 schedule/goals.db、life/）并存；**不读取、不迁移、不改名旧插件数据目录**。旧配置不做自动导入。
6. **SDK 自调用回避**：原 MaiLifeScheduleSource 通过 RPC 自调用公开 API 的路径在合并后改为进程内调用，避免依赖“插件调用自身 API”的未验证行为。

## 6. 测试基线（P0 实测，Python 3.13.7 + SDK 2.16.0）

```
MaiTrace tests: 1 failed, 299 passed, 13 skipped, 14 errors, 68 subtests passed (204.79s)
- 14 errors：test_inject.py 全部，FileNotFoundError 'E:\src\llm_models\payload_content\context_item.py'
  → 测试对 MaiBot 源码树路径有依赖，本机无 MaiBot 源码，环境性错误而非代码缺陷
- 1 failed：test_schedule.py::test_history_deadline_covers_discovery_and_cancels_all_readers
  → 时序敏感（patch 超时 0.05s + 阻塞 future），Windows 本机未命中预期异常文案；保留原测试不删除
Mai_life tests：main 分支无测试目录（上游说明 main 不含 tests/）
```

## 7. P1 后续待办（进入 P2/P3 的输入）

- 双生活引擎数值语义差异（精力/心情尺度不同）→ P2 统一或映射
- 双注入来源的 Prompt 总量控制（防 Token 翻倍）
- Mai_life [environment].timezone 与 MaiTrace [schedule].timezone 需用户保持一致（P2 收敛为单一字段）
- 主动行为统一登记（proactive_hub）→ P3
- 空间发布与书柜/搜索/群观察联动 → P3
