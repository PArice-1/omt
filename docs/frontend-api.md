# 前端接入 API 文档

本文描述当前实现的本机 HTTP 契约，适用于浏览器 UI、其他客户端及负责生成前端的 AI。启动和环境变量见 [HTTP 使用说明](api.md)，模块职责见[架构说明](architecture.md)。

**前后端对齐入口：**第 5 节定义 OMT v1 流式协议、服务端输出链路、事件顺序和界面更新规则；第 6 节给出 `fetch` 增量读取与 SSE 解析代码。前端消费的是本文的 OMT 协议，不是 OpenAI `choices[].delta`、LangGraph 原始事件或普通 JSON `/runs` 响应。

## 1. 接入约束与调用顺序

- 默认地址 `http://127.0.0.1:8000`，所有接口当前免认证；配置 access_token 也不启用校验。只供可信本机使用。
- 推荐浏览器使用同源代理；后端没有 CORS 允许列表，不要把 CORS 当认证，也不要把开发代理向不可信网络开放。
- HTTP 不解释 CLI 斜杠命令；模型、思考等级通过独立接口修改。
- 请求体拒绝额外字段。模型地址、系统提示词、工具、MCP、Skills、工作目录不接受客户端修改。
- 模型可能调用文件/Bash 工具。失败、取消、断线均不保证副作用回滚，执行 POST 不能自动重试。

建议流程：

```text
GET /api/v1/runtime
  ├─ 未就绪：厂商 → 配置预览 → 确认模型 → 检查保存与启用结果
  └─ 已就绪：读取快捷模型列表、会话列表
               → 选择会话并读取历史，或创建空会话
               → POST runs/stream，增量展示
               → 收到成功终态后刷新历史和列表
```

`/health` 返回 `{"status":"ok"}`，仅表示存活；上游不可用、未配置或数据库未就绪时它仍可能正常。

## 2. 基础信息和临时运行设置

### GET /api/v1/runtime

无请求体。200 示例：

```json
{
  "configured": true,
  "ready": true,
  "mutable": true,
  "changing": false,
  "busy": false,
  "running": 0,
  "thinking": {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "level": "high",
    "reasoning_effort": "high",
    "available_levels": ["none", "low", "high", "max"]
  },
  "message": "运行环境已就绪"
}
```

| 字段 | 含义 |
| --- | --- |
| `configured` | 磁盘默认配置是否齐全；不验证远程 Key 有效性或网络可达 |
| `ready` | 是否有可用运行实例；不保证下一次上游调用成功 |
| `mutable` | 是否支持配置/切换；注入固定 Manager 时为 false |
| `changing` | 是否正在确认配置或切换模型/等级 |
| `busy` | 是否有借用 Manager 的请求、互斥操作或配置切换；只是展示快照 |
| `running` | Manager 已占位且尚未结束的轮次数 |
| `thinking` | 当前运行配置；未就绪或固定 Manager 模式为 null |
| `message` | 安全状态提示，不含底层异常原文 |

`thinking.level` 是请求等级；`reasoning_effort` 是当前模型实际映射结果。例如 level=high 而 effort=none，表示保留请求等级但当前模型无法映射。UI 必须提示回退，不能把请求等级当实际能力。

`available_levels` 已按展示顺序返回：none 在首位，已支持的 low → medium → high → max 从低到高排列；未声明或映射为 null 的等级不返回。当前已知 DeepSeek 模型为 `none → low → high → max`。其他自定义名称保留在已知等级后并按名称排序，不推断其强度。运行状态、快捷模型列表和流中运行快照使用同一规则，**前端直接保留服务端顺序，不再按字母排序**。本次只调整数组顺序，不改变等级名称、映射含义或配置文件。

### GET /api/v1/providers

返回注册厂商、认证要求和本地凭据状态：

```json
[
  {"provider":"deepseek","logged_in":true,"requires_api_key":true},
  {"provider":"ollama","logged_in":false,"requires_api_key":false}
]
```

`requires_api_key` 是新增的布尔字段，前端按 `requires_api_key && !logged_in` 决定首次配置时是否显示并必填 Key。不要仅凭 logged_in=false 要求登录：Ollama 无需认证，logged_in 保持 false，不表示不可使用。免 Key 厂商可直接预览模型，不能要求用户填写占位 Key。不返回 Key；logged_in 仅表示本地存在认证厂商的非空 Key，不代表凭据有效；两字段均不保证服务可达或模型已下载。当前实际注册厂商以返回列表为准。

### GET /api/v1/models

只读本地配置，不联网；返回注册厂商下全部已导入模型（需认证厂商要求已存 Key，Ollama 无需 auth.json），不只是曾经选过的模型：

```json
[
  {"provider":"deepseek","model":"deepseek-flash","available_levels":["none","low","high","max"],"current":true},
  {"provider":"deepseek","model":"deepseek-v4-pro","available_levels":["none","low","high","max"],"current":false}
]
```

没有可选项时为 `[]`。UI 可以直接展示和搜索，选择项内部保留 provider + model，不按名称猜厂商。列表按厂商、模型 ID 排序；它不保证远程模型可达或支持工具。

### PUT /api/v1/runtime/model

```json
{"provider":"deepseek","model":"deepseek-v4-pro"}
```

200 返回完整 RuntimeResponse。必须来自本地快捷目录，不接受任意地址或未知模型。切换时重新读取所选模型目录和凭据，保留原请求思考等级；缺映射时实际按 none。

### PUT /api/v1/runtime/thinking

```json
{"level":"high"}
```

200 返回完整 RuntimeResponse。主动提交的等级必须在当前 `available_levels` 中，否则 422，不悄悄回退。

两项切换都：

- 作用于整个 API 进程，影响所有会话的后续轮次，不是当前页面/Session 的私有配置。
- 不改写默认配置、不写入 Session 数据库；重启恢复磁盘默认。
- 忙碌时立即 `409 manager_busy`，不排队、不取消已有 run。
- 构建失败返回安全错误，原运行配置保持不变；成功后应使用返回状态更新 UI。

## 3. 首次配置

未配置也能启动并查询上述状态。会话接口在没有运行实例时返回 `503 runtime_not_ready`。

### POST /api/v1/model-logins：预览

首次输入：

```json
{"provider":"deepseek","api_key":"<在密码输入框收集的 Key>"}
```

已有凭据时省略 api_key，复用保存的 Key：

```json
{"provider":"deepseek"}
```

Ollama 首次使用也直接省略 Key，不要求任何本地认证文件：

```json
{"provider":"ollama"}
```

请求会查询默认 `http://localhost:11434/api/tags` 的已下载模型。兼容客户端传入 null、空字符串或旧占位 Key 时，会归一为无凭据，不保存或发送；推荐直接不传 api_key。DeepSeek 的空字符串仍为 422，省略时必须有可复用的 Key。Ollama 未启动或无模型时返回安全的 422 `login_preview_failed`，提示检查本地服务和已下载模型，不索取 Key。

成功为 201：

```json
{
  "login_id":"一次性预览 ID",
  "provider":"deepseek",
  "models":["deepseek-flash","deepseek-v4-pro"],
  "from_cache":false,
  "expires_in":600
}
```

- 显式查询提供商目录，不保存、不创建数据库；新 Key 查询失败不使用旧缓存假装成功。
- 认证厂商已保存 Key 且未替换时，查询失败可以回退本地目录，from_cache=true；Ollama 无需凭据也可使用已导入的缓存，不保证服务恢复可用。
- 预览包含的凭据仅在服务端内存暂存；有效期从生成后计，默认 600 秒，到期清除，重启即失效。
- 默认最多 8 份预览，查询中的请求也占名额；超限返回 429。
- Key 不出现在响应中。客户端不写 localStorage、聊天历史、URL、日志或异常追踪，提交后尽快清空输入缓冲。

### POST /api/v1/model-logins/{login_id}/confirm：确认保存并启用

```json
{"model":"deepseek-flash"}
```

必须选择本次预览中的精确 ID。不存在或过期预览返回 404；无效模型返回 422，均不保存。

成功保存后的 200：

```json
{
  "configuration_saved":true,
  "runtime_ready":true,
  "runtime":{"configured":true,"ready":true,"mutable":true,"changing":false,"busy":false,"running":0,"thinking":{"provider":"deepseek","model":"deepseek-flash","level":"none","reasoning_effort":"none","available_levels":["none","low","high","max"]},"message":"运行环境已就绪"},
  "error":null
}
```

确认会保存完整厂商目录和默认模型；只有需认证厂商才保存凭据。Ollama 不创建或修改 auth.json，已有其他厂商和旧占位认证保留原字节但不使用；也不需要手动创建空认证文件。尚无运行环境时，显式初始化缺失数据库后启用；已有数据库不修复、不迁移。已有运行实例时尝试切换到所选模型。

**保存与启用不是事务：**保存成功、启用失败时仍返回 200、configuration_saved=true，但 error 非空，包含稳定 code/message。runtime_ready 可能为 false，也可能表示旧实例仍可用；以 runtime.thinking 判断真正运行的模型。预览在成功保存后即被消耗，重复确认返回 404。保存错误返回 503，可能已有逐文件部分写入，不能假定完全未修改。

日常只切换模型使用 `/runtime/model`，不要每次走配置确认、重写默认选择。

## 4. 会话与非流式兼容

| 方法与路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `POST /api/v1/sessions` | `{}` 或 `{"title":"标题"}` | 201 SessionResponse |
| `GET /api/v1/sessions` | 无 | 200 SessionResponse[] |
| `GET /api/v1/sessions/{id}/messages` | 无 | 200 MessageResponse[] |
| `POST /api/v1/sessions/{id}/runs` | `{"message":"本轮输入"}` | 200 RunResponse，等待本轮结束 |

```ts
interface SessionResponse {
  session_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}
interface MessageResponse {
  role: 'user' | 'assistant' | 'tool';
  text: string;
  message_id?: string;
  reasoning?: string;
  tool_calls: { id?: string; name: string; args: Record<string, unknown> }[];
  tool_call_id?: string;
  name?: string;
  status?: 'success' | 'error';
}
interface RunResponse {
  session_id: string;
  run_id: string;
  status: 'completed';
  answer: string;
  warnings: string[];
}
```

- title 默认 null，最多 200 字符，禁止 NUL；允许空白/换行。同标题可重复创建，无幂等和自动标题生成。
- message 是 1～32000 字符的字符串，禁止全空白和 NUL；保留合法首尾空白和换行。只提交新输入，不提交历史。
- 创建即登记空会话，不调用模型；当前选中的会话由前端维护，没有服务端全局当前会话。
- 列表只含当前工作目录，按 updated_at 降序、created_at 降序、session_id 升序；无分页和消息摘要。能列出不代表会话可继续。
- 时间是 UTC 的 `YYYY-MM-DD HH:mm:ss`；转换为带 Z 的 ISO 字符串后再按时区展示，不能让浏览器猜时区。
- 历史按原顺序完整返回；刷新时替换数组，不整体追加。用户/助手/工具可能多条，不是固定一问一答。
- 历史新增可选 message_id 和 reasoning；旧消息缺失时正常展示。reasoning 优先取标准内容块，旧 DeepSeek 字段只作回退，不重复拼接。
- 空正文但有工具请求的助手消息不能丢。工具按 tool_call_id 关联，status 仅为工具消息状态，不保证 Bash exit_code=0。
- 不返回系统提示词、artifact、模型元数据、检查点、消息时间或历史 run_id；字段过滤不是全面脱敏。
- 同会话运行/读取互斥，运行中不要轮询历史；失败/取消后可能返回 409 session_incomplete，没有在线修复或自动重试流程。
- 普通 JSON 的网络断开仍不保证取消。run_id 只是关联标识，不能据此查询后台任务。
- 成功后刷新历史与列表；刷新失败不能变成重新执行。`session_timestamp_update_failed` 只是更新时间失败，回答仍成功。

## 5. POST /api/v1/sessions/{id}/runs/stream

请求与非流式相同。200 为 `text/event-stream; charset=utf-8`，响应保持打开；使用 fetch 的 ReadableStream，不用只支持 GET 的原生 EventSource，也不能等 response.json()/text() 才展示。

### 流式输出如何实现

```text
浏览器 fetch POST（提交一次 message，保持这次请求的响应打开）
  → 路由校验请求，RunStream.start 等待 Manager 完成占位和历史校验
  → 接纳失败：普通 HTTP JSON 错误；接纳成功：HTTP 200 + StreamingResponse
  → 共用 Conversation 执行模型/工具，产生 RunEvent
  → RunStream 同步投影到有界队列；响应异步读取队列，写出 UTF-8 SSE 帧
  → 浏览器 response.body.getReader() 增量读取
  → TextDecoder 保留跨读取的 UTF-8 字符，缓冲按空行拆帧
  → 解析 data JSON，按 type 和关联 ID 更新对应界面
  → run.completed / run.failed / run.cancelled；响应结束并清理关联执行
```

这是**同一次 HTTP 响应分批发送正文**，不需要服务器反向连接前端，也不等待整轮执行完成才返回整份答案。服务端核心回调只快速入队，不在模型事件回调里等待网络。响应带有 `Cache-Control: no-cache`、`X-Accel-Buffering: no`；代理仍需关闭响应缓冲，前端也必须逐块读取，不能使用 `await response.json()` 或 `await response.text()` 等整包完成。

服务端不是等客户端读取到 run.started 后才开始模型；该事件表示已经接纳，不是需要前端回传的握手。停止时用同一请求的 AbortController，后端在检测到断线后尝试取消，不增加取消路由。

### 事件信封与分帧

```text
event: assistant.reasoning.delta
data: {"version":1,"seq":3,"type":"assistant.reasoning.delta","session_id":"...","run_id":"...","message_id":"...","role":"assistant","data":{"delta":"思考片段"}}

```

每个事件以空行结束；JSON 内换行已转义。一次网络读取可能包含半个事件、多个事件或半个 UTF-8 字符，必须增量解码并保留未完成帧。

| 字段 | 规则 |
| --- | --- |
| version | 当前为 1；未知版本不得按旧协议猜测 |
| seq | 当前流发送序号，递增；错误/溢出时不保证连续，不是补发游标 |
| type | 与 SSE event 名相同；客户端应允许未来未知类型但不能把它当成功 |
| session_id / run_id | 本轮关联，所有事件一致 |
| message_id | 助手消息事件携带；正文和 reasoning 共用，优先使用底层消息 ID，缺失时生成本流消息 ID；历史 ID 可缺失，不能要求所有旧消息都能按 ID 合并 |
| tool_call_id | 工具事件携带，关联同一次工具调用，不靠工具名或完成顺序猜配对 |
| role | 助手为 assistant、工具为 tool；运行事件省略，reasoning 不是独立角色 |
| data | 显式投影的事件内容，不是原始 LangChain 对象 |

### 事件与 data

| type | data | 含义 |
| --- | --- | --- |
| `run.started` | `{thinking: ThinkingResponse 或 null}` | 正式占位且历史通过校验；实际厂商/模型、请求等级和实际 effort 在 data.thinking 内，不另设 data.model；固定 Manager 模式为 null，不保证上游可达 |
| `assistant.started` | `{}` | 一条有可展示内容的助手消息开始 |
| `assistant.text.delta` | `{delta: string}` | 正文追加片段 |
| `assistant.reasoning.delta` | `{delta: string}` | 提供商实际返回的 reasoning 片段，不保证所有模型都有 |
| `assistant.completed` | `{}` | 该助手消息结束，不是整轮结束；只含工具请求的消息可能没有这些内容事件 |
| `tool.started` | `{name: string, input: JSON值}` | 工具实际执行输入，不是尚未执行的参数 token |
| `tool.delta` | `{name: string, delta: JSON值}` | 工具主动报告的 JSON 进度，可能是对象而非文本；不保证每个工具都提供 |
| `tool.completed` | `{name: string, text: string, status: "success"}` 或 `{name: string, output: JSON值}` | 常规 ToolMessage 结果剔除 artifact；其他 JSON 返回使用 output，不能假定总有 text |
| `tool.failed` | `{name: string, text: string, status: "error"}` 或 `{name: string, code: "tool_failed", message: string}` | 已处理错误的工具正文，或安全的执行失败说明，不发送未处理异常原文；仅结束该工具，不代表整轮失败 |
| `run.completed` | `{status:"completed", answer, warnings}` | 成功终态，answer 是权威最终回答，不是所有正文增量的拼接 |
| `run.failed` | `{code, message}` | 失败终态，可能已有副作用 |
| `run.cancelled` | `{code:"run_cancelled", message}` | 运行被取消；断线后不保证能收到 |

正文、reasoning 和工具可能交替出现，多次调用同名工具也可能并行；seq 只保证 Adapter 的发送顺序，不承诺底层所有并行事件的绝对发生顺序。客户端分别累积每条消息的正文/reasoning。最终 answer 用于确认或替换最终回答，不能重复追加，更不能把中间计划发言拼成最终答案。

空闲心跳是 `: heartbeat\n\n`，不是 JSON 业务事件，不增加 seq，也不是运行成功。本协议不发送 OpenAI 风格的 `data: [DONE]`；必须根据运行终态事件结束本轮，不能只看连接关闭。

### 一轮带工具调用的顺序示例

以下为一种合法顺序，工具名和 ID 为示意；每一行实际编码成前述 `event:` + `data:` + 空行的 SSE 帧。thinking 对象字段见第 2 节；真实传输没有省略号。

| seq | type | message_id | tool_call_id | data |
| --- | --- | --- | --- | --- |
| 1 | run.started | 不发送 | 不发送 | `{thinking: ThinkingResponse}` |
| 2 | assistant.started | m1 | 不发送 | `{}` |
| 3 | assistant.reasoning.delta | m1 | 不发送 | `{"delta":"先分析"}` |
| 4 | assistant.text.delta | m1 | 不发送 | `{"delta":"正在查询"}` |
| 5 | assistant.completed | m1 | 不发送 | `{}` |
| 6 | tool.started | 不发送 | t1 | `{"name":"note","input":{"value":"一"}}` |
| 7 | tool.delta | 不发送 | t1 | `{"name":"note","delta":{"progress":"一"}}` |
| 8 | tool.completed | 不发送 | t1 | `{"name":"note","text":"结果:一","status":"success"}` |
| 9 | assistant.started | m2 | 不发送 | `{}` |
| 10 | assistant.text.delta | m2 | 不发送 | `{"delta":"最终答案"}` |
| 11 | assistant.completed | m2 | 不发送 | `{}` |
| 12 | run.completed | 不发送 | 不发送 | `{"status":"completed","answer":"最终答案","warnings":[]}` |

每帧都还包含相同的 version=1、session_id、run_id；助手/工具帧分别带 role=assistant/tool，运行帧不带 role。不强制每轮都有 reasoning、工具或工具增量；同名工具可以有多个不同调用 ID 并行交错。工具也可以失败后由模型解释并最终 run.completed，因此不能把 tool.failed 或 assistant.completed 当作运行终态。

### 前端如何更新界面

| 收到事件 | 更新方式 |
| --- | --- |
| run.started | 将本次请求标为运行中，绑定 session_id/run_id，展示本轮 thinking 快照，而非以后切换的新配置 |
| assistant.started | 按 session_id + run_id + message_id 建立临时助手消息；不是每个 delta 新建一条消息 |
| assistant.text.delta | 仅向该消息正文追加 data.delta |
| assistant.reasoning.delta | 仅向同一消息的独立 reasoning 区域追加 data.delta，不拼入正文或下一轮请求 |
| assistant.completed | 关闭该消息的生成状态，但保留整轮运行状态 |
| tool.started / tool.delta | 按 session_id + run_id + tool_call_id 建立/更新工具卡片；工具事件不依赖 message_id，不按工具名合并 |
| tool.completed / tool.failed | 标记对应工具完成/失败；兼容 text/status、output、code/message 的不同结果形态，仍等待运行终态 |
| run.completed | 标记本轮成功，用 answer 核对最终结果，不再追加一份相同回答；随后刷新完整历史并整体替换临时列表，以历史确认最终消息边界 |
| run.failed / run.cancelled | 标记本轮失败/取消，保留已收到的过程用于提示，不自动重发 prompt |
| 未知事件或注释心跳 | 不当作成功；未知类型可忽略展示，注释不进入 JSON 解析 |

流式临时状态不要直接作为完整历史提交；下一轮仍只发送新 message。连接结束却未收到运行终态时，标记“结果未确认”，不是成功，也不是已经回滚。用户切换会话后，晚到事件只能更新原 session_id/run_id，不能写入当前页面刚选中的另一会话。

### 接纳、终态和取消

- 接纳前：404/409/422/429/503 等普通 JSON 错误，不发送 run.started，不启动模型。
- 接纳后：响应已是 200，执行失败通过 run.failed，不能期待 HTTP 改成 502。
- 只有 run.completed 表示成功。EOF 前没收到终态、网络异常、解析失败都按结果未确认处理，不自动重试。
- UI 停止按钮使用 AbortController.abort()；后端检测断线后尝试取消和清理。显示“已停止接收/正在终止”，不要承诺已经回滚或立即停止所有工具。
- 缓冲溢出、无法投影的数据会终止本轮；发送阻塞也有上限。终态过大时可能执行已经成功，但不能传回完整答案，应检查历史，不重跑。
- 没有 Last-Event-ID、重连补发、后台运行/查询、幂等键或单独取消接口。失败/取消保护不会因切换模型而清除。

## 6. 浏览器最小流式客户端

以下为接入示例，不包含自动重试。建议正式 UI 再对各事件 data 做具体类型校验，限制浏览器展示缓冲，并按 session_id/run_id 归属更新对应界面，不要覆盖后来选中的会话。

```ts
interface RunEvent {
  version: 1;
  seq: number;
  type: string;
  session_id: string;
  run_id: string;
  message_id?: string;
  tool_call_id?: string;
  role?: 'assistant' | 'tool';
  data: Record<string, unknown>;
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function envelope(value: unknown): RunEvent {
  if (!record(value) || value.version !== 1 || !Number.isInteger(value.seq)
      || typeof value.seq !== 'number' || typeof value.type !== 'string'
      || typeof value.session_id !== 'string' || typeof value.run_id !== 'string'
      || !record(value.data)
      || (value.message_id !== undefined && typeof value.message_id !== 'string')
      || (value.tool_call_id !== undefined && typeof value.tool_call_id !== 'string')
      || (value.role !== undefined && value.role !== 'assistant' && value.role !== 'tool')) {
    throw new Error('事件格式或版本不受支持，运行结果未确认');
  }
  return {
    version: 1, seq: value.seq, type: value.type,
    session_id: value.session_id, run_id: value.run_id, data: value.data,
    message_id: value.message_id, tool_call_id: value.tool_call_id, role: value.role,
  };
}
export async function streamRun(
  baseUrl: string, sessionId: string, message: string,
  onEvent: (event: RunEvent) => void, signal: AbortSignal,
): Promise<RunEvent> {
  const response = await fetch(
    `${baseUrl.replace(/\/+$/, '')}/api/v1/sessions/${encodeURIComponent(sessionId)}/runs/stream`,
    { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ message }), signal },
  );
  if (!response.ok) {
    // 接纳前 HTTP 错误；实际 UI 应解析稳定 error.code 并保留基础设施错误兜底。
    const body: unknown = await response.json().catch(() => null);
    const text = record(body) && record(body.error) && typeof body.error.message === 'string'
      ? body.error.message : `HTTP ${response.status}`;
    throw new Error(text);
  }
  if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
    throw new Error('不是可读取的事件流，运行结果未确认');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  let terminal: RunEvent | undefined;
  let runId: string | undefined;
  let previousSeq = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split('\n');
        const payload = lines.filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
        if (!payload) continue; // 包括注释心跳
        const parsed: unknown = JSON.parse(payload);
        const event = envelope(parsed);
        const name = lines.find(line => line.startsWith('event: '))?.slice(7);
        if (name !== event.type || event.session_id !== sessionId || terminal
            || event.seq <= previousSeq || (runId !== undefined && event.run_id !== runId)) {
          throw new Error('事件关联或顺序错误，运行结果未确认');
        }
        runId = event.run_id;
        previousSeq = event.seq;
        onEvent(event);
        if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)) terminal = event;
      }
      if (done) break;
    }
    if (buffer.trim() || !terminal) throw new Error('连接结束但没有完整终态，运行结果未确认');
    return terminal; // 调用方按 type 区分成功、失败与取消
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
```

调用时为每轮创建 AbortController，停止按钮调用 abort；不要在组件自动重试或渲染逻辑里提交 prompt。该解析器针对本服务固定使用 LF 的 SSE 输出；如果扩展接收其他服务，还需支持 CRLF 等标准行结束形式。

Markdown/工具正文都是不可信输入，必须禁用或清理 HTML，不能直接 innerHTML。切换会话后的晚到结果按 ID 保存；刷新历史用完整数组替换临时流式视图。

## 7. 错误、限制和后续工作

普通错误统一为 `{"error":{"code":"...","message":"...","run_id":"可选"}}`。不采用默认 Pydantic detail 数组，不回显原始 input。HTTP 状态及错误码见 [HTTP 错误表](api.md#错误与执行边界)。

- `session_incomplete` / `run_failed`：提示人工检查，不自动重跑；必要时可新建会话，但不是撤销旧操作。
- `manager_busy` / `session_busy` / `capacity_exceeded`：本次被拒绝，不排队，等用户重试。
- `configuration_save_failed`：可能有部分文件写入；重新检查配置，不能当作完全未保存。
- `model_change_failed`：原运行配置保留，UI 不应提前显示新选择已生效。
- `stream_buffer_exceeded` / `invalid_stream_data` / `stream_event_too_large`：本次传输不能完整完成，检查会话，不自动重试。
- 普通 GET 可由用户稍后重查；写操作、运行 POST 不自动重试。

当前没有：多用户认证隔离、工具沙箱/审批、会话删除/重命名/分页、历史编辑/重新生成/分支、文件上传、按 Session 保存模型、CLI `/models` 或 CLI reasoning 展示。CLI 后续接入与会话模型持久化另行推进。

离线证据见 [运行配置与流式测试](tests/api-runtime-streaming.md)；真实浏览器、代理和局域网验证尚不属于测试通过的含义。
