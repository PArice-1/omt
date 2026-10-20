# OMT 当前架构

## 1. 总览

项目提供单进程、异步执行的交互式 Coding Agent，以及固定工作目录、单用户的非流式 FastAPI 入口，同时保留同步 Agent 与 Runtime 接口。LangChain/LangGraph 负责模型与工具之间的执行循环；OMT 负责模型连接、依赖组装、会话历史和终端显示。

```text
CLI: omt
  └─ session.session() → asyncio.run(asession())
       ├─ load_settings() → 选择 provider/model
       ├─ load_system_prompt() → 读取包内 prompts/system.md
       ├─ _get_model(settings)
       │    ├─ load_auth() → 认证
       │    ├─ load_model_store() → 精确匹配模型与 URL
       │    └─ Model → models_provider → 提供商初始化函数
       ├─ 确认预建数据库存在，生成 thread_id、运行 config 并固定 workspace_path
       ├─ SessionStore(db_path) → 会话索引与检查点使用同一个数据库文件
       ├─ AsyncSqliteSaver.from_conn_string(db_path) → 在会话期间保持连接
       ├─ await _get_agent(model, checkpointer=...) → 组合内置工具、MCP 工具与检查点
       ├─ CreateAgentSession(agent, config=config, system_prompt=...) → 合并提示词与 Skills
       └─ PromptSession 输入循环
            ├─ await _check_user_input(text) → 退出、/resume 列表与切换，或普通请求
            ├─ 首次执行前 await store.insert_session(...)，同一会话只登记一次
            └─ await conversation.arun(text) → 异步流式展示过程，返回最后回答 str
```

配置装配在外部完成，再将 Agent 注入会话。会话不读取认证文件，不自行创建模型，也不重新实现工具执行循环。

### 代码地图

| 模块 | 职责 | 不承担的工作 |
| --- | --- | --- |
| [coding_agent/session.py](../src/omt/coding_agent/session.py) | CLI 组装、创建会话 ID 与运行配置、调用 Store 登记索引、/resume 列表与选择、输入校验与重试 | 保存完整消息历史、手动执行模型请求的工具 |
| [api/](../src/omt/api/) | 应用 lifespan、访问令牌校验、HTTP 请求/响应投影及稳定错误 | CLI 命令解析、任意目录切换、工具执行循环 |
| [coding_agent/bootstrap.py](../src/omt/coding_agent/bootstrap.py) | CLI/HTTP 共用的模型解析、内置工具与可选 MCP 组装 | 输入循环、会话索引、HTTP 传输 |
| [coding_agent/session_manager.py](../src/omt/coding_agent/session_manager.py) | 固定目录会话索引、按 ID 加载最新检查点、会话互斥、全局运行容量及关闭清理 | 多进程锁、持久化任务台账、工具副作用回滚 |
| [coding_agent/events.py](../src/omt/coding_agent/events.py) | 结构化过程事件与默认终端渲染 | SSE 协议、历史持久化、全局事件全序 |
| [coding_agent/prompts/](../src/omt/coding_agent/prompts/) | 保存并加载随包分发的基础系统提示词 | 用户配置覆盖、Skill 正文加载、模板变量或热重载 |
| [coding_agent/settings.py](../src/omt/coding_agent/settings.py) | 配置文件读取、字段校验、设置缓存 | 创建模型、OAuth 登录/刷新 |
| [ai/model.py](../src/omt/ai/model.py) | 通过注册表初始化模型，暴露 `model.model` | 维护对话或渲染终端 |
| [ai/models_manager.py](../src/omt/ai/models_manager.py) 与 [models/deepseek.py](../src/omt/ai/models/deepseek.py) | 提供商路由、连接参数校验、调用 SDK 初始化 | 自动发现提供商或转换任意认证协议 |
| [agent/agent.py](../src/omt/agent/agent.py) | 封装 `create_agent`、同步/异步调用和 v3 流，透传底层能力 | 管理 CLI 或自行拼接会话历史 |
| [coding_agent/runtime.py](../src/omt/coding_agent/runtime.py) | 初始化提示词与 Skills 目录、一轮执行、产生过程事件、提交历史、按 ID 从检查点切换已完成会话 | 管理数据库文件/索引、模型配置解析、外部提示词文件读取 |
| [coding_agent/store.py](../src/omt/coding_agent/store.py) | SessionStore 读写预建的 sessions 表，CLI 首次执行前登记会话元数据 | 自动建库建表、保存图状态、会话恢复或 CLI 命令 |
| [coding_agent/tools_manager.py](../src/omt/coding_agent/tools_manager.py) | 显式加载内置工具，每次返回新列表 | 当前不执行文件内注释掉的 AST 动态扫描 |
| [coding_agent/skills.py](../src/omt/coding_agent/skills.py) | 从显式目录发现 Skill 元数据、返回诊断、生成 Runtime 使用的目录提示词 | 不自动加载正文到会话、不执行脚本 |
| [coding_agent/tools/](../src/omt/coding_agent/tools/) | 读取、编辑、写入文件和执行 Bash | 权限审批、沙箱、工具副作用回滚 |
| [utils/find_files.py](../src/omt/coding_agent/utils/find_files.py) | 按 glob 查找排序后的文件路径 | 不是已注册工具，也不参与当前静态工具注册 |

## 2. CLI 与组装入口

`pyproject.toml` 将命令 `omt` 指向 `omt.coding_agent.session:session`。`session()` 保持普通函数，仅在最外层调用一次 `asyncio.run(asession())`；已有事件循环的调用方应直接 `await asession()`。也可通过模块运行：

```bash
uv run omt
# 或：
uv run python -m omt.coding_agent.session
```

这些是人工运行命令，不是离线测试：会读取用户配置，提交问题后可能请求真实提供商并执行工具。

`asession()` 先读取配置和内置系统提示词，确认 session.py 的 db_path 指向已有数据库文件，再初始化一次模型、Agent、会话和 `PromptSession`。会话索引表仍须预先创建，不在入口执行建表或迁移。入口生成 UUID 和 RunnableConfig 并注入 Runtime，记录启动时 Path.cwd().resolve() 作为工作目录；数据库路径不是工作目录。随后在同一事件循环中顺序处理输入与回合：

- `_aget_user_input()` 使用 prompt_toolkit 的 `prompt_async()`，提交时拒绝全空白和 NUL 字符，保留合法文本原样。原有同步 `_get_user_input()` 保留供同步调用方使用。
- `_check_user_input()` 独立识别并处理退出与 /resume 命令，返回动作 run、handled、exit 以及成功恢复的 SessionRecord（没有则为 None）；编号字典原地更新。asession() 只根据结果退出、跳过命令或继续普通回合，并根据恢复记录同步入口 ID 和登记状态。普通请求文本不被检查函数改写。
- `/exit`、`/quit` 经过去空白、忽略大小写的匹配后退出，不发给模型；EOF/Ctrl-D 退出。
- `resume` 是 `/resume` 的无斜杠别名，共用同一命令分支，避免误输入 `resume 1` 被发给模型、登记为新会话或触发工具。独立词 resume 现在是保留命令词，错误参数也只提示；resume-help 等不同词仍作为普通输入。已有检查点中的旧消息不因此删除或改写。
- `/resume` 通过 list_sessions(workspace_path) 读取当前启动目录的索引，显示 number、title、updated_at（上次修改时间）。0 固定为当前会话，其余沿用 Store 按修改时间从新到旧的顺序，不重复当前 ID；标题折叠为单行。尚未登记的当前会话显示“新对话”和启动 UTC 时间，不写占位记录。
- `/resume N` 只选择最近一次已展示列表中的正数编号，通过 get_session(thread_id) 复查记录存在且仍属于当前目录，再 await conversation.switch_session(thread_id)。0 提示无需恢复；未展示列表、无效编号或参数格式错误只提示，不执行 Agent 或登记索引。新增记录不改变已经展示的编号映射。
- 切换成功后调用 Store.update_session_updated_at 刷新目标修改时间，再同步入口的 thread_id、当前修改时间和已登记标记，不重复插入原索引、不改变创建时间；清空旧编号映射，下次切换须重新 /resume。SQLite 查询与恢复失败单独提示，失败切换不改变原历史和登记状态，也不刷新修改时间。若切换已成功但修改时间写入失败，明确提示部分失败，仍同步入口到目标会话，避免错写旧 ID；两步之间没有跨模块原子事务。列表、编号解析和恢复均在普通输入登记之前处理；命令名忽略大小写。
- 输入阶段和执行阶段 Ctrl-C 均退出整个 CLI 会话，不再取消当前操作后重新等待输入。输入的 KeyboardInterrupt 向外传播；执行期间使用 asyncio.run 的默认 SIGINT 取消机制，由 Runtime 清理消费任务并关闭图流。最外层 session() 捕获 KeyboardInterrupt，仅显示会话结束，不打印堆栈。
- 不再为单个回合创建取消包装任务或安装自定义 SIGINT 处理器。外部程序发起的任务取消仍向上传播，不作为用户退出或普通失败吞掉；已有事件循环中直接调用 asession() 时，由调用方处理取消或 KeyboardInterrupt。关闭流不保证同步工具的线程或外部副作用立即停止，事件循环关闭时仍可能等待正在执行的同步工具。
- 普通轮次异常由 runtime 展示并抛出，入口提示可继续输入。启动阶段的配置/初始化错误不在此重试循环内。
- runtime 已展示回答，入口不再打印 `arun()` 的返回值，避免重复输出。
- 第一条普通请求（不含退出或 /resume 命令）执行前，await SessionStore.insert_session() 登记索引，以输入前 10 个字符作为标题。插入提交后才设置登记标记；后续回合（包括首次执行失败后的重试）不重复插入。直接退出或输入时 Ctrl-C 不登记空会话。
- SQLite 索引写入失败单独提示“本轮尚未执行”，不调用 arun，下一次输入可重新尝试登记；不把数据库错误误报为模型执行失败。首轮开始后即使失败或取消，索引也保留，以便定位可能已经写入的中间检查点，索引存在不代表回合执行成功。

输入验证不是工具权限控制。Ctrl-C 的处理也不等于 LangGraph 检查点的暂停/恢复，不能保证正在进行的外部操作被撤销。

### 基础系统提示词

基础规则保存在 [coding_agent/prompts/system.md](../src/omt/coding_agent/prompts/system.md)，作为版本化的 Markdown 包资源分发；内容涵盖工作流程、现有工具使用、授权和敏感信息处理。提示词是模型指引，不是实际沙箱或权限实现。

[prompts/__init__.py](../src/omt/coding_agent/prompts/__init__.py) 的 `load_system_prompt() -> str` 使用 `importlib.resources.files` 加载 UTF-8 文本，不依赖 cwd，不读取用户或项目中的同名文件。资源缺失、编码错误或纯空白内容会在启动阶段报错，且发生在模型初始化之前；不静默回退为无基础规则。

CLI 将文本传给 `CreateAgentSession(system_prompt=...)`，由 Runtime 与 Skills 目录合并到首条 SystemMessage；不同时写入 Agent.system_prompt，避免重复。基础规则在 system.md 中维护，不写成长字符串散落在 session.py 或 runtime.py。每个新 CLI 会话加载一次；直接创建 Runtime 时仍需自行传入文本，默认参数保持空串。源码改动对新会话生效，已安装的 wheel 需要重新构建/安装；既有会话不热重载。

验证与分发检查见 [提示词测试说明](tests/prompts.md)。

### HTTP 入口与资源生命周期

`omt-api` 指向 `omt.api.app:main`，默认仅监听 127.0.0.1:8000，单 worker。create_app() 创建应用时不初始化模型，lifespan 确认工作目录等于进程 cwd、数据库与 sessions 表已存在后，加载提示词、模型和显式配置的 MCP，保持一个 AsyncSqliteSaver，再注入 SessionManager。退出时先关闭 Manager、取消并等待其活跃读取/运行，再关闭 Saver。默认 HTTP MCP 为空，不继承 CLI 本机硬编码服务器。共享组装位于 bootstrap.py；CLI 的 _get_model 是 build_model 兼容别名，_get_agent 保留 CLI 的 MCP 配置后调用共用 build_agent。

```text
HTTP + Bearer 认证 → 请求校验 / 响应投影
  → SessionManager（固定目录、按 session_id 操作、同会话互斥、全局容量）
    → SessionStore（元数据）+ 最新 Checkpointer 状态
    → 独立 Runtime（event_handler=None，不打印执行正文）
      → Agent / Tools
```

HTTP 创建会话立即登记索引，允许空会话；不是 CLI 首条输入前登记。session_id 直接映射 thread_id，每次读取或运行重新从最新检查点加载，不缓存全局当前会话；无检查点允许开始首轮，已有但未完成的状态返回冲突，不直接重试工具。成功后刷新 updated_at；若仅更新时间写入失败，仍返回 completed 和警告。失败/取消在当前 Manager 内阻止再次执行该会话，重启后根据最新磁盘检查点判断；没有持久化失败台账或原子事务。run_id 是本次请求标识，不是可查询后台任务。

业务路由均需独立的 HTTP Bearer 令牌，未配置或太短时启动失败；health 为公开存活检查，不请求模型。历史仅投影用户/助手/工具文本和工具关联，不返回系统提示词、artifact、模型元数据或检查点；这不是正文脱敏。输入拒绝额外字段，客户端不能更换工作目录、模型 URL 或 MCP 命令。请求期间不调用 chdir。当前进程内互斥不保护外部 CLI 或其他 worker，不能同时写同一会话。

非流式执行等待完成后返回 JSON；同会话忙返回 409，全局容量满返回 429。断线不保证立即取消，尚无 SSE、后台任务查询、取消、事件重放、多用户或沙箱。启动配置、完整路由、错误及限制见 [HTTP 使用说明](api.md)，离线验证见 [HTTP 测试说明](tests/api.md)。

## 3. 模型连接与配置

### 文件位置与缓存

当前 `settings.root` 在模块导入时解析为 `~/.omt`，实际读取：

```text
~/.omt/agent/settings.json
~/.omt/agent/auth.json
~/.omt/agent/model-store.json
```

仓库内同名的 `agent/` 文件不是当前默认配置来源。切换工作目录或在导入后修改 HOME，不会自动重定位已计算的 `settings.root`。

- `load_settings()` 读取 `defaultProvider`、`defaultModel`，拒绝空白选择，并缓存成功结果；主动重载用 `cache_clear()`。
- `load_auth()` 不缓存；按 provider 保存认证类型及可选的 `SecretStr` Key。加载错误不回显文件原文。
- `load_model_store()` 不缓存；读取 provider 下的 `models`，使用 `id`、`baseUrl`、可选 `provider`，校验 HTTP(S) URL 和 ID 唯一性。
- 文件采用严格 JSON，不接受尾随逗号。能力、价格等额外目录字段目前不驱动初始化逻辑。

### 解析与初始化

`_get_model(settings)` 要求：

1. provider 已存在于 `models_manager.models` 注册表；目前只有 DeepSeek。
2. `auth.json` 有对应认证，类型是 `api_key`，Key 非空白。
3. 默认模型与该 provider 的 `models[].id` 精确匹配，不猜别名、不取列表第一项。
4. 模型定义若声明 provider，必须与所选 provider 一致。

满足条件后，将 provider、模型 ID、HttpUrl 和 SecretStr 传给 `Model`。当前入口没有缺失凭据时的环境变量回退，也没有 OAuth 或令牌刷新适配。

`Model` 构造时立即调用 `_init_model()`，经注册表进入 `deepseek_init()`，最终使用 LangChain SDK 创建聊天模型。传给 `Agent` 的是 **`client.model`**，不是 Model 包装对象本身。

`baseUrl` 是凭据将被发送到的目标地址，应只使用可信地址。测试不通过读取真实配置验证连接。

## 4. 会话契约与历史

### 三种容易混淆的对象

| 名称 | 含义 |
| --- | --- |
| CLI 函数 `session()` | 创建依赖并接收用户输入的应用入口 |
| `CreateAgentSession` | 负责多轮历史和展示的会话对象；当前保留此公开名称 |
| 一轮 `session.run(text)` / `await session.arun(text)` | 完成一个用户请求，内部可以调用模型和工具多次；同一实例仅支持顺序执行 |

`CreateAgentSession(agent, config=None, *, system_prompt="", skill_roots=None, event_handler=print_event)` 接收现成 Agent，原有两个位置参数兼容。CLI 主动创建 ID 和 config，再将同一个 thread_id 交给 Runtime 与 Store；Runtime 不管理数据库文件或 sessions 索引，切换时通过现有图的 Checkpointer 读取状态。初始化时复制配置外层和 `configurable` 字典；没有 `thread_id` 键才生成 UUID v4 字符串，作为非 CLI 直接调用方的兼容默认行为。已有值保持不变，后续每轮复用同一 ID，get_config() 返回当前 RunnableConfig。其他嵌套配置不是深拷贝。

初始化消息只准备一次：

- `system_prompt` 是调用方提供的文本，不是文件路径；读取外部提示词文件由调用方负责。
- 默认按 `settings.root/agent/skills`（通常为 `~/.omt/agent/skills`）、`cwd/skills` 顺序发现 Skills，项目版本覆盖同名用户版本。显式 `skill_roots` 替换默认目录；空列表关闭发现。
- 基础提示词与 Skills 目录以空行连接，非空内容合并成首条 `SystemMessage`；两者都为空或仅空白时，初始 `messages=[]`，不插入空系统消息。基础提示词原始格式保留，目录不含正文。
- 扫描诊断以 `[Skills]` 前缀写入 stderr，不加入模型历史。只在创建会话时扫描；后续轮次不热重载。
- 此入口沿用 Runtime 管理初始系统消息的方式；同一基础提示词不要再传给 `Agent(system_prompt=...)`，否则底层会在请求时再次添加。Agent 本身的该能力仍保留供其他调用方使用。

### 一轮数据流

```text
上一轮 self.messages + 新 HumanMessage
           ↓
Agent.stream_event(...) / await Agent.astream_event(...)
           ↓
LangGraph 执行模型 → 工具 → 模型，runtime 显示过程
           ↓
stream.output / await stream.output()：本次执行的最终状态
           ↓
校验最后消息是无待执行 tool_calls 的 AIMessage
           ↓
self.messages = 完整 messages 列表
return 最后 AIMessage.text
```

必须保持的区别：

- `run(text) -> str` 与 `await arun(text) -> str` 共享终态校验与历史提交逻辑，只返回最后一次模型回答的纯文本，不返回整份状态，也不拼入中间日志。
- `self.messages` 保存完整消息对象：可选的初始 SystemMessage、HumanMessage、携带工具请求的 AIMessage、ToolMessage、最终 AIMessage，可包含多次工具调用。初始系统消息随完整历史传递，不在每轮重复插入。
- 以最终列表**替换**已有历史，不将它再次追加到旧历史。下一轮提交的是“完整历史 + 新输入”。
- 不从文本片段重建消息；原始对象保留工具调用 ID、ToolMessage artifact 和 AIMessage 元数据。
- 最终状态缺失、消息为空、最后是工具结果或仍有工具请求时，当前 Runtime 报错，不把中间内容假装成总结。

成功提交之前不改变 `self.messages`。普通异常会显示后继续向上抛出，异步任务取消也向上传播但不显示为普通执行失败，二者都保留此前已完成的会话历史；已执行的文件修改、进程副作用以及外部 checkpointer 已写入的状态不因此回滚。

### 内存与检查点

CLI 当前在 asession() 中打开 AsyncSqliteSaver，将检查点传给 Agent，数据库文件由 session.py 的 db_path 指定。Runtime 仍在内存中保存完整历史并逐轮提交；每次启动 CLI 生成新的 UUID 并注入 Runtime，不自动恢复上次会话；用户可通过 /resume 和 /resume N 显式选择当前目录下已完成的历史，加载旧 thread_id 的状态后继续交互。仅有索引不保证可恢复，缺失或未完成的检查点会被拒绝。

底层 Agent 支持 checkpointer 和 thread_id。Runtime 的 switch_session() 已支持按 ID 加载最新已完成状态，但运行策略仍是提交完整历史，不是改为“只提交本轮增量”。内存和 SQLite 回归测试验证加载后继续执行不会重复消息。未来变更输入策略时，应先明确历史主源并同步调整回归测试。

### Runtime 会话切换

await conversation.switch_session(thread_id) 使用当前 Agent 的现有 Checkpointer 查询目标根图的最新状态，成功后在同一实例中替换 messages 和 config，不创建另一个 Runtime。保留当前运行选项，复制配置外层和 configurable 后替换 thread_id，并移除 checkpoint_id、checkpoint_ns、checkpoint_map；不把返回快照的具体 checkpoint_id 固定到后续调用，避免追加输入时从旧检查点分支。

切换只读检查点、不执行模型或工具、不重新扫描 Skills 或插入系统提示词，不操作 sessions 表。目标历史中的原始消息、ID、工具结果 artifact 和模型元数据均保留。更新 messages、config 后默认调用同步 display_history()；HTTP 使用新增的仅关键字参数 display=False 关闭自动展示，不改变校验和加载行为。按历史顺序打印用户消息、助手文本、工具调用参数和结果；错误工具结果标为失败，工具名缺失时显示 tool_call_id。该方法也可独立调用，空历史或仅有系统消息时提示暂无历史。展示不输出系统提示词、artifact 或模型元数据，不重放工具开始/完成通知；消息正文与工具参数仍按原内容展示，不提供通用脱敏。目标不存在或没有消息、ID 为空、图仍有待执行节点、最后消息不是无待执行工具的 AIMessage 时拒绝切换。查询失败、取消或校验失败时，当前 messages、config 保持不变。此方法要求与 run、arun 及其他切换顺序调用，不提供并发切换或中断执行恢复。

CLI 的 /resume N 在选择当前目录的合法索引后调用该方法，成功后由 CLI 调用 Store 更新时间并同步入口局部 ID 和登记状态，不再次插入已存在的索引；直接调用 Runtime 不读写会话索引。它不替换模型、工具或其他初始化依赖。交互与失败路径见 [CLI /resume 测试说明](tests/resume.md)。

### 会话索引

SessionStore(db_path) 独立管理预先创建的 sessions 表，用 thread_id 关联检查点恢复，用 workspace_path 区分会话创建目录。构造函数只保存规范化路径，不读默认配置、不自动建库或建表。四个异步方法 insert_session、get_session、list_sessions、update_session_updated_at 各自打开并关闭连接；写入提交，缺文件、缺表和重复 ID 的数据库异常原样传播。连接使用 URI 的 mode=rw，禁止文件缺失时静默创建空库。

查询返回 SessionRecord，包含 thread_id、workspace_path、title、created_at、updated_at。工作目录统一 expanduser 和 resolve 后精确匹配，列表按更新时间降序、创建时间降序、ID 升序排列。目录不自动归并到 Git 根目录，不扫描所有检查点推导会话列表。表结构、隔离及边界见 [SQLite 会话索引测试说明](tests/store.md)。

当前 CLI 实例化 Store 并在首次回合前登记索引，与 Checkpointer 显式使用同一个文件路径，在一个数据库中维护各自的表。通过 /resume 列表与选择接入 Runtime 恢复，但不提供 /sessions 别名、标题更新、会话删除，仅在切换成功后主动刷新目标 updated_at，普通回合暂不自动刷新。更新时间方法只改目标修改时间，使用 UTC 秒格式，提交后返回新值，不存在的 ID 抛 ValueError。CLI 的 /resume 页面沿用 Store 最近修改优先的顺序，编号 0 始终为当前会话。索引与检查点之间没有跨模块的原子事务，不能用元数据记录推断执行成功或状态已回滚。

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

Runtime 通过 event_handler 接收函数输出 RunEvent（助手文本开始/增量/结束、工具开始/增量/完成/失败、运行失败），默认交给 events.print_event 终端渲染，保留 CLI 行为；传入 None 关闭过程正文输出，但仍消费全部通道并校验终态。接收函数为同步快速回调，不支持等待网络或异步背压；这些事件不是持久化消息，也不是已经实现的 SSE 协议。switch_session(display=False) 可独立关闭恢复后的历史展示，默认仍展示以兼容 CLI。Skills 诊断仍写 stderr。

runtime 同步路径用 `interleave("messages", "tool_calls")` 同时订阅模型和工具通道。当前依赖的异步流没有 `interleave()`，异步路径先订阅两个通道，再通过两个消费任务并行读取；完成后检查原始异常，退出前取消并等待未完成的消费任务，再关闭流。两种路径都显示助手文本、工具参数、工具增量、最终结果和错误，工具终态展示规则共用。不要先耗尽一个通道，再期望未订阅的另一个通道回放全过程。

处理单个模型或工具句柄时的内部循环会继续推进执行，所以显示逻辑不是所有原始事件严格全序的回放。需要全局原始顺序时应另行设计事件级渲染，而不是把快照全部打印出来。

过程通知不等于消息：每段 token、工具开始提示、进度片段不一定独立保存在历史中；最终工具返回值才由底层形成工具消息。工具执行由 LangGraph 完成，runtime 不根据调用请求再次手动执行。

v3 仍是实验性协议。session.py 在模块入口启动之前过滤对应的实验性提示；过滤 LangChainBetaWarning 只影响显示，不保证升级兼容。

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

### Skills 发现能力与 Runtime 接入

`skills.py` 提供独立能力，由 Runtime 在初始化时调用。`session.py` 加载基础提示词并组装模型、内置工具和会话；创建会话时默认 Skills 目录与基础提示词合并为初始 SystemMessage，随后随历史传入模型：

- `discover_skills(roots: Sequence[Path]) -> SkillDiscoveryResult` 只扫描调用方传入的目录，递归寻找 `SKILL.md`，不默认搜索 HOME/cwd。返回 `skills` 和字符串 `diagnostics`。
- `SkillDefinition` 保存名称、描述和文件绝对路径，不保存正文。使用安全 YAML 加载，校验非空字符串、名称格式及名称/描述长度，忽略额外字段的运行时语义；接受 UTF-8 BOM 和多行描述。
- 按根目录传入顺序、目录内路径排序处理，同名后发现者覆盖前者并诊断；同一真实文件路径只处理一次，重复根目录不会重新恢复旧版本。结果按名称排序。
- 缺失目录正常跳过；无效根目录、非普通文件、读取失败或头部无效均诊断并继续。诊断不包含 YAML 原文；每次发现重新读取，不缓存。
- `render_skills_prompt(skills) -> str` 只渲染 Markdown 元数据目录与加载规则，无 Skills 时返回空串。说明要求模型调用现有 `read(file=绝对路径, offset=1, limit=False)` 读取完整正文，并按 Skill 所在目录解析相对资源路径；模块不保证模型一定遵循说明。

目录发现不是信任或沙箱机制。普通文件符号链接可以指向目录外；没有文件大小或提示词预算限制，只应扫描可信根目录。当前不提供 Skill 脚本自动执行、权限审批、显式命令或独立的 Skill 加载状态机。验证范围见 [Skills 测试说明](tests/skills.md)。

## 7. 修改定位与当前限制

| 变更目标 | 优先修改 | 回归依据 |
| --- | --- | --- |
| 输入、退出、CLI 组装 | session.py | [Session 与配置测试](tests/session-and-config.md) |
| 历史、返回文本、工具展示、会话 ID | runtime.py | [Runtime 测试](tests/runtime.md) |
| SQLite 会话元数据、按目录列出会话 | store.py | [SQLite 会话索引测试](tests/store.md) |
| 配置路径、认证字段、目录校验 | settings.py、session.py | Session 与配置测试 |
| 增加提供商 | ai/models/、models_manager.py；必要时调整认证解析 | [模型测试](tests/ai-models.md) |
| 底层调用参数、协议、中断/恢复 | agent/agent.py | [Agent 测试](tests/agent.md) |
| 新工具或工具契约 | tools/、tools_manager.py | [工具测试](tests/tools.md) |
| 基础系统提示词 | prompts/system.md、prompts/__init__.py、session.py | [提示词测试](tests/prompts.md)、[Session 测试](tests/session-and-config.md) |
| Skill 发现与目录提示词 | skills.py；Runtime 初始化消费目录 | [Skills 测试](tests/skills.md)、[Runtime 测试](tests/runtime.md) |

当前不提供：OAuth 登录/刷新、启动时自动恢复上次会话及 /sessions 别名、Runtime 的图中断恢复流程、工具沙箱/审批、Bash 实时增量输出、自动启用的 AST 工具发现。

底层 Agent 的中断测试通过，不代表 CLI Session 已实现暂停恢复。开发时把这些层次的支持范围分别核对，不从底层参数透传推断上层功能已经完成。

另有源码跟踪风险：当前 `.gitignore` 的 `agent/` 是目录名规则，会匹配 `src/omt/agent/`。若源码在磁盘可读却未出现在 Git 状态中，运行 `git check-ignore -v <path>` 核对；未经明确变更不要顺手修改忽略规则，更不要提交用户认证文件。

测试执行方式与配置隔离要求见 [测试规范与索引](tests/README.md)。
