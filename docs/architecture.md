# OMT 当前架构

## 1. 总览

CLI 与非流式 FastAPI 是平级的应用入口，共用 SessionManager、依赖组装和 SQLite 检查点。LangChain/LangGraph 负责模型与工具循环，OMT 负责模型连接、会话编排、完整历史校验，以及各入口的展示或响应投影。

```text
omt / python -m omt.cli               omt-api
  → cli.app.run_cli                    → api.app lifespan + routes
     输入、命令、当前选择、终端输出          请求校验、JSON 投影（当前免认证）
               \                       /
       bootstrap.open_session_runtime        bootstrap.open_session_manager
         → SessionRuntime（可切换模型）          → 固定模型
                  → 工具/MCP、Store、AsyncSqliteSaver
                  → 当前 SessionManager
                     ├─ SessionStore：索引与目录归属
                     ├─ Checkpointer：每次读取最新历史
                     └─ Conversation：一轮执行、完整终态校验、结构化事件
                          → Agent → 模型 / 工具
```

core 是与入口无关的共用核心，不限定于编码场景。核心不导入 CLI 或 API，不维护全局“当前会话”，不直接打印终端。CLI 当前选择只是入口局部状态；所有会话登记、查询、恢复和执行均通过 Manager，不再直接编排 Store 或 Conversation。

### 代码地图

| 模块 | 职责 | 不承担的工作 |
| --- | --- | --- |
| [cli/app.py](../src/omt/cli/app.py) | 交互生命周期、延迟创建首个会话、调用 Manager | 检查点加载、并发和失败保护 |
| [cli/login.py](../src/omt/cli/login.py) | /login 选择、隐藏首次 Key、厂商登录状态与模型列表展示 | JSON 读写、厂商协议、运行模型切换 |
| [core/model_configuration.py](../src/omt/core/model_configuration.py) | 类型化 API 登录、凭据复用、完整目录与缓存、默认选择持久化 | 终端提示、会话编排、聊天请求、数据库初始化 |
| [cli/commands.py](../src/omt/cli/commands.py) | /resume、退出、编号映射和当前选择 | 模型执行、直接读写 Store |
| [cli/terminal.py](../src/omt/cli/terminal.py) | 同步/异步输入校验、事件和历史渲染、警告 | 保存历史、HTTP 响应 |
| [api/](../src/omt/api/) | lifespan、请求/响应和稳定错误；当前暂未启用 Bearer 校验 | CLI 命令、会话内部编排 |
| [core/bootstrap.py](../src/omt/core/bootstrap.py) | 提示词/模型配置和依赖组装；SessionRuntime 持有当前 Manager 并协调模型/推理强度重建 | 输入循环、HTTP 传输 |
| [core/configuration.py](../src/omt/core/configuration.py) | 共用会话选项、类型化 MCP 连接配置；入口各自继承并指定前缀 | HTTP 令牌、读取模型认证 |
| [core/session_manager.py](../src/omt/core/session_manager.py) | 按 ID 操作、目录归属、最新历史、互斥/容量、失败和关闭清理 | 跨进程锁、回滚、全局当前会话 |
| [core/conversation.py](../src/omt/core/conversation.py) | 初始提示词/Skills、一轮同步/异步执行、历史加载和终态校验、过程事件 | 索引、模型配置、终端显示 |
| [core/events.py](../src/omt/core/events.py) | RunEvent 数据与 EventHandler 类型 | 渲染、SSE、持久化 |
| [core/store.py](../src/omt/core/store.py) | 已有索引读写；显式初始化缺失数据库 | 消息、隐式建表、已有库迁移、检查点恢复 |
| [core/settings.py](../src/omt/core/settings.py) | 模型配置、认证与目录读取及校验 | 会话编排、OAuth |
| [ai/model.py](../src/omt/ai/model.py)、[models_manager.py](../src/omt/ai/models_manager.py)、[ai/models/](../src/omt/ai/models/) | 厂商注册、模型初始化及厂商目录查询协议 | 配置持久化、历史和展示 |
| [agent/agent.py](../src/omt/agent/agent.py) | create_agent 与同步/异步调用、v3 流包装 | CLI、拼接历史、手动执行工具 |
| [core/prompts/](../src/omt/core/prompts/)、[skills.py](../src/omt/core/skills.py) | 包内基础规则、可信目录的 Skill 元数据发现 | 沙箱、自动执行 Skill 脚本 |
| [tools_manager.py](../src/omt/core/tools_manager.py)、[tools/](../src/omt/core/tools/) | 静态工具注册、文件和 Bash | 权限审批、事务回滚 |

兼容性：核心包由 `omt.coding_agent` 更名为 `omt.core`，不保留旧包的兼容导入；命令 omt、HTTP 路由、JSON 字段、配置环境变量及用户数据目录不变。此前 CLI 拆分已移除旧 session/runtime 模块和 CreateAgentSession；同步入口使用 cli.app.main()，异步入口使用 cli.app.run_cli()，低层会话对象使用 core.conversation.Conversation。load_history() 不自动展示，核心默认静默；需要显示的调用方显式安装 CLI 渲染。

## 2. 入口与共用组装

```bash
# 人工启动：会读取配置，提交消息可能计费并执行工具；不是离线测试。
uv run omt
uv run python -m omt.cli
uv run omt-api
```

omt 和模块入口均调用 omt.cli.app:main，仅在最外层调用一次 asyncio.run(run_cli())；已有事件循环应 await run_cli()。默认 CLI 先调用 core.model_configuration.check_model_configuration：配置缺失、不完整或无效时仍进入命令循环，提示 /login，不打开 Manager、MCP 或数据库。完成 /login 并补齐默认配置后，通过 core.store.initialize_session_database 显式初始化缺失库，再加载运行环境。已有完整配置的正常启动仍按下述预建库契约校验。自定义工厂绕过初始检查；HTTP 不进行交互初始化。omt-api 指向 omt.api.app:main，默认监听 127.0.0.1:8000，单 worker。当前本机分支的 HTTP 业务无需访问令牌，health 公开且不调用模型；Swagger/OpenAPI 文档也已开放。

HTTP 默认通过 `bootstrap.open_session_manager(options)`；CLI 默认通过 `bootstrap.open_session_runtime(options)`，由 SessionRuntime 持有可替换的 Manager。两者共用配置解析、预建索引校验、提示词、模型与 Agent 组装：
1. 确认配置工作目录是实际 cwd，且数据库文件和 sessions 表预先存在；不会 chdir、建索引表或迁移。
2. 读取包内基础提示词，再解析模型设置/认证/目录，组装模型、内置工具和选定的 MCP。
3. 持有一个 AsyncSqliteSaver，创建 Store 与 Manager。
4. 退出先关闭当前 Manager，取消并等待活跃历史/运行操作，再关闭 Saver 连接。

CLI 的 SessionRuntime 提供只读 `manager`/`thinking` 和异步 `set_thinking_level(level)`、`set_model(provider, model_name)`。前者按当前模型目录快照解析等级；后者显式重读所选模型定义和认证，保留请求思考等级，缺映射时实际按 none。两者共用重建链：初始化 Model、创建 Agent，再调用旧 Manager.rebuild(agent) 创建继任 Manager；全部成功后才发布新 Manager、设置和目录快照。构建失败/取消保持旧实例及状态；不请求模型试运行，Runtime 自身不写配置。MCP 工具复用启动发现结果，整个生命周期复用同一个 Saver。HTTP 仍使用固定 Manager，不暴露模型/推理强度修改端点。

`run_cli(manager_factory=...)` 可继续注入固定 Manager，此时不读取额外模型配置，不支持切换；也可注入提供 SessionRuntime 的异步工厂。调用方应每次通过 runtime.manager 取得最新实例，不再使用切换前的引用。

SessionSettings 共享 db_path、workspace_path、max_concurrent_runs、skill_roots、mcp_servers。ApiSettings 使用 OMT_API_，当前访问令牌字段可不配置且不校验长度，路由也不执行鉴权；配置令牌不会自动恢复认证。CliSettings 使用 OMT_CLI_，不要求也不读取 HTTP 令牌。两者默认数据库均为 ~/.omt/agent/state/omt.sqlite。当前 CliSettings 和 ApiSettings 均显式将 skill_roots 默认为 ~/.agents/skills；只有使用共用 SessionSettings 且保留 skill_roots=None 时，bootstrap 才回退到 ~/.omt/agent/skills 与 cwd/skills。CLI/API 默认 MCP 均为空；不再内置开发者本机 knowledge 路径，需要的部署通过各自的 MCP_SERVERS 环境变量显式配置。模型的三份 JSON 配置仍由 settings.py 读取，与这些启动选项不同。

### CLI 交互

- PromptSession 异步读取；校验与 Manager 共用 validate_message：1～32000 字符，非全空白、无 NUL，保留合法输入原文。
- /login 随时列出厂商及登录状态，仅未登录时收集 Key，由 core 复用认证、拉取完整目录并保存所选默认模型。无模型时其他业务输入仅提示登录；命令本身不登记会话、不提交聊天请求。已有 Runtime 时立即切换模型，保留会话和历史；固定 Manager 只保存并提示不能切换。
- /exit、/quit、EOF 退出；命令支持首尾空白及大小写混合。resume 是 /resume 的别名；resume-help 等不同词仍是普通消息。
- 首条普通请求才调用 Manager.create_session，以原文前 10 字符为标题。仅看列表、退出或输入 Ctrl-C 不登记空会话。登记失败不执行模型，下一次输入可重试；首轮开始后即使失败/取消也保留索引。
- /resume 调用 Manager.list_sessions，只显示当前工作目录的记录。0 固定为当前选择；尚未登记时显示“新对话”和启动 UTC 时间。其他编号沿用 Store 修改时间排序，标题折为单行。
- /resume N 只使用最近展示的编号映射，调用 Manager.open_session 复查归属、加载历史并刷新修改时间。成功后才替换 CLI 当前选择、展示历史与警告，并清空旧编号；失败保持旧选择，删除/移出目录的记录使旧映射失效。
- 启动和 /thinking 展示请求等级、实际 effort 与目录支持的可选等级（始终包含 none）；/thinking <level> 修改当前进程后续轮次使用的强度。缺失/null 映射按 none 并提示，不把请求等级误当成实际 effort。命令不登记会话；重建不丢失当前选择、编号和历史，不绕过失败保护。
- 普通请求调用 Manager.run(..., event_handler=terminal.print_event)。最终回答已通过事件输出，入口不再次打印 answer；成功轮次由 Manager 刷新 updated_at。
- 模型/工具失败后允许输入其他命令，但同一会话会被 Manager 标为需恢复，不能盲目重跑；可选择其他会话或退出检查。不再沿用旧 CLI 的失败后直接追加策略。
- Ctrl-C 在输入与执行阶段均退出整个 CLI。执行时沿用 asyncio.run 默认 SIGINT 取消，Conversation 清理消费任务和流，Manager 先于 Saver 关闭。main 只捕获 KeyboardInterrupt；外部任务取消向上传播，不伪装成正常退出。同步工具线程、外部进程和已经发生的副作用不保证立即停止或撤销。

CLI 配置和示例见[使用说明](cli.md)，交互证据见[CLI 测试](tests/cli.md)及[/resume 测试](tests/resume.md)。

### 基础规则与 HTTP

load_system_prompt() 使用 importlib.resources 读取包内 prompts/system.md，与 cwd 无关；资源缺失、非 UTF-8 或纯空白在模型初始化前失败。bootstrap 每次生命周期读取一次，Manager 只在空会话首轮传入 Conversation，与 Skills 目录合并成单条 SystemMessage；不要同时传给 Agent.system_prompt。恢复已有消息不替换原系统规则，也不重新扫描 Skills。

HTTP 显式创建即登记，允许空会话；历史响应只投影用户/助手/工具文本和工具关联，不暴露系统提示词、artifact、模型元数据或检查点。其 JSON 调用默认不绑定过程回调。当前 HTTP 免认证，仅适合可信本机使用；后端及前端代理均不应向不可信网络开放。完整路由、认证现状和错误见[HTTP 使用说明](api.md)。

## 3. 模型连接与配置

### 文件位置与缓存

当前 `settings.root` 在模块导入时解析为 `~/.omt`，实际读取：

```text
~/.omt/agent/settings.json
~/.omt/agent/auth.json
~/.omt/agent/model-store.json
```

模型登录分三层：`cli/login.py` 仅展示和选择 provider、模型 ID，未登录时读取隐藏 Key，不收集地址/查询确认/保存确认，也不读写 JSON；`core/model_configuration.py` 用 `is_provider_logged_in` 检查本地认证，`prepare_provider_login` 复用凭据并取得完整目录，`save_model_login` 自动合并配置。厂商地址和查询协议在 `ai/models/<厂商>.py`，注册表项为 `ModelProvider(initialize, list_models, base_url)`；DeepSeek 的默认地址是模块常量 `DEEPSEEK_BASE_URL`。新增厂商定义地址和两种能力后注册，不修改 CLI/core 分支。ModelProvider 另有可选 thinking_level_maps，按精确模型 ID 提供厂商模块维护的官方能力，默认空表，不改变原有调用方式。

`/login` 每次自动查询厂商完整列表，未选模型前只读、不落盘。已保存 Key 且未显式替换时，查询失败可使用本地目录；新 Key 或无缓存时获取失败需重试，不再允许随意手填未知 ID。选择支持编号或完整名称。auth 仍按厂商共用一个 Key；已登录选择模型直接复用，不重复索取。model-store 导入全部返回 ID，而非仅所选项；同 ID 使用厂商内置地址，保留思考映射/额外元数据、其他厂商及未返回的旧条目；已知能力由厂商模块补齐缺失/null 的 thinkingLevelMap，已有映射（包括显式空表）不覆盖。当前为 deepseek-flash/deepseek-v4-pro 提供 low/high/max，未知 ID 不推测能力。settings 更新默认厂商/模型，保留思考等级；损坏 JSON、非法字段和已有非 API 认证拒绝覆盖。

Python 接口兼容性：ProviderConnection/ModelLogin 不再接受 base_url；list_provider_models 只接收 provider/api_key；ModelProvider 必须提供 base_url；save_model_login 新增必填的完整 model_names 参数，LoginResult 移除旧 is_default（所选模型始终设为默认）。仓库调用方和测试同步调整；models_provider 初始化接口、三份 JSON 字段及用户数据路径不变，已有配置无需迁移。

CLI 的 Key 输入临时使用 DummyHistory，结束/取消后清空缓冲并恢复显示；登录期间暂停普通消息校验，退出后恢复。每份变更 JSON 用 0600 临时文件原子替换，没有跨文件事务或跨进程锁；模型选择完成前取消不落盘，I/O 失败后可能需重新 /login。core 读取器仍严格只读，数据库初始化由 store 的显式函数承担，不放进配置保存或查询。首次登录启动运行环境，已有 Runtime 时由 cli/app.py 调用 set_model；选择已持久化但切换失败时保留旧运行模型并提示，二者不构成事务。会话选择、历史、检查点和失败保护不因切换而重置。

仓库内同名的 `agent/` 文件不是当前默认配置来源。切换工作目录或在导入后修改 HOME，不会自动重定位已计算的 `settings.root`。

- `load_settings()` 读取 `defaultProvider`、`defaultModel` 和 `defaultThinkingLevel`，拒绝空白选择，并缓存成功结果；主动重载用 `cache_clear()`。`defaultThinkingLevel` 未配置或为 `null` 时按 `"none"` 处理，不再预设 `media` 等思考等级；显式非空等级名由模型目录映射。
- `load_auth()` 不缓存；按 provider 保存认证类型及可选的 `SecretStr` Key。加载错误不回显文件原文。
- `load_model_store()` 不缓存；读取 provider 下的 `models`，使用 `id`、`baseUrl`、可选 `provider` 和 `thinkingLevelMap`，校验 HTTP(S) URL 和 ID 唯一性。`thinkingLevelMap` 是等级名到提供商 effort 字符串或 `null` 的映射，非 null 值必须非空；未选模型的缺失映射或 null 等级不影响目录加载。
- 文件采用严格 JSON，不接受尾随逗号。除思考映射外，能力、价格等额外目录字段目前不驱动初始化逻辑。配置文件名沿用 `model-store.json`，不是 `model-stores.json`。

### 解析与初始化

`bootstrap.build_model(settings)` 要求：

1. provider 已存在于 `models_manager.models` 注册表；目前只有 DeepSeek。
2. `auth.json` 有对应认证，类型是 `api_key`，Key 非空白。
3. 默认模型与该 provider 的 `models[].id` 精确匹配，不猜别名、不取列表第一项。
4. 模型定义若声明 provider，必须与所选 provider 一致。

思考能力不作为初始化的必备条件：未配置等级、等级为 `null`/`"none"`、模型的 `thinkingLevelMap` 缺失或为 `null`、等级不存在或对应值为 `null` 时，统一使用 `reasoning_effort="none"`，不报缺失映射错误。只有显式选择且存在有效映射的等级才使用映射值；显式 `"none"` 不会被目录重新映射为开启思考。

满足条件后，将 provider、模型 ID、HttpUrl、SecretStr 和映射后的 `reasoning_effort` 通过 `Model` 的 `**kwargs` 传给提供商。`build_model(settings, definition=...)` 可显式复用已解析目录快照，并校验其 ID/provider，保证 SessionRuntime 的展示与初始化使用同一映射。参数统一拼写为 `reasoning_effort`。当前入口没有缺失凭据时的环境变量回退，也没有 OAuth 或令牌刷新适配。

配置示例（只展示相关字段，目录中仍需设置可信的 `baseUrl`）：

```json
{
  "defaultProvider": "deepseek",
  "defaultModel": "your-model-id",
  "defaultThinkingLevel": "high"
}
```

所选 `models[]` 项的映射示例；实际 effort 应按所用模型的提供商协议配置，不将等级名直接当成 SDK 参数：

```json
{
  "high": "high",
  "max": "max",
  "none": "none",
  "unsupported": null
}
```

该对象放在模型项的 `thinkingLevelMap` 字段中。不要求旧目录补齐映射；省略 `defaultThinkingLevel` 不主动开启思考。缺失能力按 none 处理，但错误 JSON、错误字段类型、空白等级/映射值及连接或认证错误仍报错。

`Model` 构造时立即调用 `_init_model()`，经注册表进入 `deepseek_init()`，最终使用 LangChain SDK 创建可直接调用的聊天模型，而非可配置模型包装器。传给 `Agent` 的是 **`client.model`**，不是 Model 包装对象本身。

DeepSeek 适配将有效 `reasoning_effort` 原样传给 SDK，并合并 `extra_body={"thinking": {"type": "enabled"}}`。已知支持关闭推理的 deepseek-flash/deepseek-v4-pro 收到字符串 `"none"` 时，显式下发 reasoning_effort="none" 和 disabled thinking；未传/Python None 则沿用服务端配置。旧/未知 ID 的字符串 none 保留省略参数的兼容行为，不据此保证关闭服务端推理。已有 `extra_body` 的其他字段及 temperature/max_tokens 等选项保留，覆盖 thinking 时不修改调用者字典。规则参考 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)；离线测试不证明真实服务接受某个 effort。

`baseUrl` 是凭据将被发送到的目标地址，应只使用可信地址。测试不通过读取真实配置验证连接。

## 4. 会话契约与历史

### 对象与职责

| 对象 | 含义 |
| --- | --- |
| CLI / HTTP | 输入、当前选择、展示或协议适配 |
| SessionManager | 应用层会话服务：按 ID 协调索引、检查点、互斥、运行和关闭；不是 FastAPI middleware |
| Conversation | 顺序执行的一轮/多轮低层会话对象，不感知入口 |
| SessionStore / AsyncSqliteSaver | 元数据 / 完整图状态，默认同一 SQLite 文件，不是同一事务 |

### Manager 的参数与状态

SessionManager(agent, store, *, workspace_path, system_prompt, skill_roots, max_concurrent_runs=1) 要求 Agent 配置 Checkpointer。store 仅保存索引，历史由 Checkpointer 负责；workspace_path 规范化后用于归属过滤，不 chdir，也不是沙箱。system_prompt 是正文，skill_roots 是搜索目录，[] 关闭发现，仅在无检查点时生效。max_concurrent_runs 限制同一实例的 run 操作，不限制图内部工具并发，满载立即拒绝、不排队。

- _running：已接纳且未结束的 run 数；_busy：运行、读取或激活的会话 ID，同会话不可重入。
- _active：关闭时要取消并等待的操作任务；_tainted：本进程观察到运行失败/取消、需人工检查的 ID；_closed：拒绝新操作的关闭标记。
- 上述状态只属于当前进程与 Manager，不是持久化任务台账、跨进程锁或租约。CLI/API 复用同一实现，不代表分别启动的两个进程受同一把锁保护；不得同时写同一会话。

每次 get_history、open_session 或 run 都从最新检查点装载独立 Conversation，不缓存 Runtime 或全局当前会话。无检查点视为空会话；已有状态要求图无待执行节点且最终消息是无工具请求的 AIMessage，未完成或失败会话不伪装成新会话。

get_history 只读取、不刷新修改时间。open_session 返回 LoadedSession(record, messages, warnings)，校验成功后刷新时间；原始消息由 CLI 决定如何显示，Manager 不改变当前选择。run 返回 RunResult(session_id, run_id, answer, warnings)，run_id 不是可查询后台任务。event_handler 只影响本轮，默认 None。

`await manager.rebuild(agent)` 只在没有运行/历史/激活操作时允许交接，要求新 Agent 复用同一个 Checkpointer；创建新的 Manager，保留 Store、目录、提示词、Skills、容量及 `_tainted`，成功后关闭旧 Manager。历史不手动复制，下一轮仍从检查点读取；不登记或激活会话，不刷新索引时间。忙碌时立即拒绝，不取消现有请求。失败集合的交接封装在 Manager 内部，入口和 runtime 不访问其私有字段。

运行失败/取消将 ID 加入 _tainted，后续读取/激活/运行均拒绝；模型重建仍保留此保护，进程重启后根据磁盘检查点判断，不等于失败回滚。模型、工具或终态错误转换为稳定 SessionError；仅最后更新时间失败仍返回成功，附 session_timestamp_update_failed，避免用户因元数据写入失败重跑有副作用的工具。aclose 拒绝新操作并取消/等待活跃历史与运行任务；调用方再关闭 Saver。

### Conversation 初始化与一轮执行

Conversation(agent, config=None, *, system_prompt="", skill_roots=None, event_handler=None) 接收现成 Agent，不读认证或模型配置。配置复制外层及 configurable 字典；仅当 thread_id 键不存在时生成 UUID。既有值保持不变，其他嵌套配置不深拷贝。

新会话默认按 settings.root/agent/skills、cwd/skills 发现 Skill，项目覆盖用户同名项；显式目录替换默认，[] 关闭。基础提示词与 Skill 元数据目录合并为首条 SystemMessage，均为空时 messages=[]。正文不预加载，诊断经 skills.diagnostic 事件发送，不加入历史。低层直接创建的实例只在初始化时扫描，Manager 恢复已有历史时使用空搜索目录避免重扫。

```text
上一轮完整 messages + HumanMessage
  → Agent 同步/异步 v3 流 → 模型 / 工具
  → 读取最终 output["messages"]
  → 校验最后消息为无 tool_calls 的 AIMessage
  → 替换 self.messages，返回最终 AIMessage.text
```

run()/arun() 只返回最终文本，不把 token、日志或工具进度拼入答案。完整原始消息保留系统规则、工具请求 ID、ToolMessage artifact 和模型元数据；下一轮提交完整历史，不能再次追加同一终态列表。终态缺失、消息为空、最终仍是工具结果/工具请求时抛错。

成功前不改变内存 messages；异常或取消向上传播，取消不显示为普通执行失败。内存历史未提交不代表 Checkpointer、文件、进程或远程副作用回滚。Manager 不缓存失败实例，而是拒绝同 ID 继续执行。

### 低层历史加载

await conversation.load_history(thread_id) 只从当前 Agent 的 Checkpointer 加载根图最新状态，不运行模型/工具、不重新扫描 Skills、不读写索引，也不打印。它复制当前配置、替换 thread_id，并清除 checkpoint_id、checkpoint_ns、checkpoint_map，避免之后从旧具体快照分支。成功才同时替换 messages/config，保留原始消息对象；空白 ID、无状态/消息、待执行节点、非已完成 AI 终态或查询失败均不改变当前状态。取消也传播。调用方须与 run/arun 顺序使用；需要互斥应经 Manager。

CLI 的 terminal.display_history(messages) 独立按历史顺序显示用户、助手、工具请求和结果；过滤系统提示词、artifact 和模型元数据，不重放工具开始/完成通知。工具名缺失用调用 ID，空历史提示暂无内容。字段过滤不等于正文或参数脱敏。

### 索引与检查点

SessionStore 四个异步方法分别连接并关闭预建 sessions 表：insert_session、get_session、list_sessions、update_session_updated_at。路径 expanduser/resolve，数据库使用 mode=rw 防止误建空库；错误向上传播。列表精确匹配目录，按 updated_at 降序、created_at 降序、thread_id 升序排序，不归并 Git 根目录。

SessionRecord 包含 thread_id、workspace_path、title、created_at、updated_at；更新时间仅改目标 UTC 秒时间，不修改创建时间。Manager 在显式激活及成功轮次后刷新；只列出或读取历史不刷新。HTTP 显式创建和 CLI 延迟创建不同，但均经 Manager 登记。没有索引/检查点跨表事务，索引存在不证明运行成功；仅有索引、无检查点是合法空会话。表结构与边界见[索引测试](tests/store.md)。

## 5. Agent、流和输出

`Agent.invoke()` / `await Agent.ainvoke()` 分别透传底层同步/异步调用；异步执行路径支持同步工具及只有异步实现的工具，同步路径不会自动执行异步工具。包装层不使用 `asyncio.run()` 或自行调度工具。

`Agent.stream_event()` 返回原始 `GraphRunStream`；`await Agent.astream_event()` 返回原始 `AsyncGraphRunStream`。两者均只接受 `version="v3"`，获取流本身不会运行模型，消费事件或获取 output 才推进执行。

同步使用 `with agent.stream_event(...) as stream`；异步使用 `async with await agent.astream_event(...) as stream`、`async for` 消费投影、`await stream.output()` 读取终态。提前退出、异常或任务取消均经过上下文清理。v3 自己管理通道，不能沿用旧接口的 `stream_mode`、`subgraphs` 或 include/exclude 过滤参数。

| 读取入口 | 语义 |
| --- | --- |
| 原始事件 `event["method"] / event["params"]` | v3 协议通知，不是 v2 的 event/data |
| `stream.messages` | 每次模型调用的输出流，并非只有最后一次调用 |
| `message.text` | 当前模型调用的文本片段；模型只生成工具请求时可能为空 |
| `message.tool_calls` / `message.output.tool_calls` | 模型提出的工具调用请求 |
| `stream.tool_calls` | 工具实际执行的输入、增量、完成结果或错误 |
| `stream.values` | Agent 状态快照，通常不是消息增量 |
| `stream.output` / `await stream.output()` | 同步属性 / 异步方法；最终状态字典，`["messages"]` 含此次输入历史与执行产生的完整消息 |
| `context=` | 运行上下文；不是自动发送给模型的对话历史 |

Conversation 通过可选 event_handler 发送 RunEvent（助手文本开始/增量/结束、工具开始/增量/完成/失败、运行失败及 Skills 诊断），默认 None 完全静默。events.py 只定义数据与回调类型；CLI 的 terminal.print_event 渲染终端，历史由 terminal.display_history 单独展示。回调仅限快速同步操作，不支持网络等待或异步背压；事件不是持久化消息，也不是 SSE。Manager 每轮单独绑定回调，不会把 CLI 渲染泄漏到同实例后续 HTTP 操作。

Conversation 同步路径用 `interleave("messages", "tool_calls")` 同时订阅模型和工具通道。当前依赖的异步流没有 `interleave()`，异步路径先订阅两个通道，再通过两个消费任务并行读取；完成后检查原始异常，退出前取消并等待未完成的消费任务，再关闭流。两种路径都显示助手文本、工具参数、工具增量、最终结果和错误，工具终态展示规则共用。不要先耗尽一个通道，再期望未订阅的另一个通道回放全过程。

处理单个模型或工具句柄时的内部循环会继续推进执行，所以显示逻辑不是所有原始事件严格全序的回放。需要全局原始顺序时应另行设计事件级渲染，而不是把快照全部打印出来。

过程通知不等于消息：每段 token、工具开始提示、进度片段不一定独立保存在历史中；最终工具返回值才由底层形成工具消息。工具执行由 LangGraph 完成，Conversation 不根据调用请求再次手动执行。

v3 仍是实验性协议。cli.app.main() 在交互启动前过滤对应的实验性提示；过滤 LangChainBetaWarning 只影响显示，不保证升级兼容。

## 6. 工具与文件范围

当前静态注册顺序为 `[edit, read, write, bash]`。新增工具在 `tools/` 定义 `@tool` 函数，并显式注册；同步更新注册集合测试、Agent 组装测试与 [工具测试说明](tests/tools.md)。

| 工具 | 当前约定 |
| --- | --- |
| read | UTF-8 读取；offset 从 1 开始；limit 是布尔开关，True 最多读取 50 行，并非整数行数 |
| write | 创建或覆盖文件；父目录必须存在，空字符串可清空文件，不自动创建父目录 |
| edit | 精确文本替换；old_string 非空；replace_all 必须显式提供，多处匹配未授权时拒绝修改 |
| bash | POSIX/Bash，同步执行；stdin 关闭；超时 1～120 秒、默认 30 秒；超时/调用内异常时终止其进程组 |

文件工具相对路径和 Bash 默认工作目录均基于进程当前工作目录，不是配置目录 `~/.omt`。当前没有独立的 workspace 沙箱。

Bash 返回 exit_code、stdout、stderr、timed_out、truncated。非零退出码作为结果交给模型分析，不自动抛 Python 异常；`[工具完成]` 表示调用已返回，不代表命令退出码是 0。输出在完整捕获后分别截断至 20000 字符，不是内存限额，也不是实时日志流。

工具有当前用户的文件和进程权限，可产生不可逆副作用。目录解析、输入校验和超时都不等于沙箱；不向不可信调用方承诺隔离、审批或全面的后台进程管理。

### Skills 发现能力与 Conversation 接入

`skills.py` 提供独立能力，由 Conversation 在空会话初始化时调用。bootstrap 加载基础提示词，Manager 将其与 Skills 搜索目录传入 Conversation，合并为初始 SystemMessage；恢复已有历史不重新发现 Skills：

- `discover_skills(roots: Sequence[Path]) -> SkillDiscoveryResult` 只扫描调用方传入的目录，递归寻找 `SKILL.md`，不默认搜索 HOME/cwd。返回 `skills` 和字符串 `diagnostics`。
- `SkillDefinition` 保存名称、描述和文件绝对路径，不保存正文。使用安全 YAML 加载，校验非空字符串、名称格式及名称/描述长度，忽略额外字段的运行时语义；接受 UTF-8 BOM 和多行描述。
- 按根目录传入顺序、目录内路径排序处理，同名后发现者覆盖前者并诊断；同一真实文件路径只处理一次，重复根目录不会重新恢复旧版本。结果按名称排序。
- 缺失目录正常跳过；无效根目录、非普通文件、读取失败或头部无效均诊断并继续。诊断不包含 YAML 原文；每次发现重新读取，不缓存。
- `render_skills_prompt(skills) -> str` 只渲染 Markdown 元数据目录与加载规则，无 Skills 时返回空串。说明要求模型调用现有 `read(file=绝对路径, offset=1, limit=False)` 读取完整正文，并按 Skill 所在目录解析相对资源路径；模块不保证模型一定遵循说明。

目录发现不是信任或沙箱机制。普通文件符号链接可以指向目录外；没有文件大小或提示词预算限制，只应扫描可信根目录。当前不提供 Skill 脚本自动执行、权限审批、显式命令或独立的 Skill 加载状态机。验证范围见 [Skills 测试说明](tests/skills.md)。

## 7. 修改定位与当前限制

| 变更目标 | 优先修改 | 回归依据 |
| --- | --- | --- |
| 输入、退出、命令和终端展示 | cli/app.py、commands.py、terminal.py | [CLI 测试](tests/cli.md)、[/resume 测试](tests/resume.md) |
| 共用资源组装和配置 | bootstrap.py、configuration.py；入口各自 settings.py | [配置与组装测试](tests/session-and-config.md)、CLI/HTTP 测试 |
| 会话加载、互斥、失败与关闭 | session_manager.py | [HTTP/Manager 测试](tests/api.md)、CLI 测试 |
| 历史、返回文本和过程事件 | conversation.py、events.py | [Conversation 测试](tests/conversation.md) |
| SQLite 会话元数据 | store.py | [会话索引测试](tests/store.md) |
| 增加提供商 | ai/models/、models_manager.py、bootstrap.py | [模型测试](tests/ai-models.md) |
| 底层调用参数、协议、中断/恢复 | agent/agent.py | [Agent 测试](tests/agent.md) |
| 工具或 Skill | tools/、tools_manager.py、skills.py | [工具测试](tests/tools.md)、[Skills 测试](tests/skills.md) |
| 基础系统提示词 | prompts/system.md、prompts/__init__.py、bootstrap.py | [提示词测试](tests/prompts.md)、CLI 集成测试 |

当前不提供 OAuth 登录/刷新、自动恢复上次会话、/sessions 别名、图中断恢复流程、工具沙箱/审批、Bash 实时增量输出或自动 AST 工具发现。HTTP 尚无 SSE、后台任务、取消接口和多用户。底层 Agent 的中断测试通过，不代表应用入口已实现暂停恢复。

源码跟踪注意：当前 .gitignore 的 agent/ 是目录名规则，会匹配 src/omt/agent/。若磁盘可读却未出现在 Git 状态中，运行 git check-ignore -v <path>；不要为提交源码而取消敏感配置忽略保护，更不要提交认证文件。

测试执行方式和配置隔离要求见[测试规范与索引](tests/README.md)。
