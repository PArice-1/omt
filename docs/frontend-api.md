# OMT 前端接入 API 文档

> 面向前端开发者及负责生成前端代码的 AI。本文描述当前可调用的 HTTP 接口、数据格式和可观察行为，不要求了解后端内部实现。可将本文件完整交给 AI，作为前端开发的接口契约。
>
> 适用范围：当前仓库工作区的 `/api/v1` 接口。后端面向**可信本机、单用户、固定工作目录**，采用**非流式 JSON 响应**。本文中的 ID、时间、回答和工具输出均为示例，不是固定返回值。

## 1. 开始接入前必须知道的事

1. 默认服务地址是 `http://127.0.0.1:8000`。下文路径均相对于该地址。
2. 当前工作区已暂时停用访问令牌校验，四个会话业务接口均**无需认证，也不需要发送 `Authorization` 请求头**。前端不需要令牌输入或登录流程；这不影响后端连接模型所需的 API Key。
3. 发送消息后，请求会一直等待本轮结束，再一次性返回最终回答；不是 SSE、WebSocket 或后台任务提交接口。
4. 历史由服务端保存。前端只发送本轮 `message`，**不要上传完整历史、角色数组或系统提示词**。
5. 一轮执行可能读写文件或运行命令。超时、断线、失败不保证操作没发生，**禁止对发送消息请求做无条件自动重试**。
6. 同一个会话不能同时运行或读取历史。默认整个服务同时只允许一轮执行，超额立即报错、不排队。
7. 后端当前没有取消、运行状态查询、删除会话、重命名、流式输出、分页或多用户接口。不要为这些功能编造接口。
8. 后端工作目录在启动时固定，不能通过请求切换。它不是安全沙箱；不要直接向公网或不可信用户开放。

## 2. 连接与通用规则

### 2.1 服务准备

前端接入假定后端已完成模型配置、数据库初始化，并已启动。后端操作者的完整准备步骤见仓库根目录 `README.md`，启动配置见 `docs/api.md`；前端不需要读取这些配置文件。

已准备好环境时，可由后端操作者执行：

```bash
# 在目标工作目录执行；当前无需配置 OMT_API_ACCESS_TOKEN。
uv run omt-api
```

当前后端未启用访问令牌的启动校验及路由鉴权依赖，配置 `OMT_API_ACCESS_TOKEN` 也不会自动恢复接口认证。本文按这一工作区现状描述；其他文档若仍要求 Bearer 令牌，不适用于当前接入流程。不要将模型凭据或用户配置提交到仓库。

当前源码也提供以下辅助文档端点，均无需认证：

| 路径 | 用途 |
| --- | --- |
| `/docs` | Swagger UI，可直接试调业务接口，无需 Authorize |
| `/redoc` | ReDoc 接口文档 |
| `/openapi.json` | OpenAPI 定义，可辅助生成客户端类型 |

当前业务接口同样无需认证。错误响应、并发和失败语义应以本文为补充，不能只根据生成的 OpenAPI 推断。

### 2.2 请求与响应

带 JSON 请求体的接口使用：

```http
Content-Type: application/json
Accept: application/json
```

- GET 不需要请求体；两个 POST 都需要 JSON 对象，创建空会话也必须传 `{}`，不能省略请求体或传 `null`。
- 成功响应直接返回对象或数组，**没有统一的 `data`、`success`、`total` 外层包装**。
- 路径中的 `session_id` 来自创建/列表响应，作为不透明字符串保存，拼接 URL 时使用 `encodeURIComponent`。服务端新建 ID 当前形如 UUID，但前端无需自行生成或解析。
- 请求体拒绝额外字段。不要附加 `stream`、`model`、`messages`、`workspace_path`、`run_id` 等未声明字段。
- 本文列出的接口不提供业务查询参数；不要依赖传入 `page`、`limit`、`search` 等能产生效果。
- 错误统一格式见第 5 节。前端按 HTTP 状态和 `error.code` 分支，不要匹配中文错误文案。

### 2.3 浏览器跨域与本机安全

当前服务**没有配置 CORS 允许列表**。例如前端运行在 `http://localhost:5173`，不能假定浏览器能直接调用 `http://127.0.0.1:8000`。无需认证不等于允许跨域；使用 `Content-Type: application/json` 的跨域 POST 仍会触发预检，跨域 GET 的响应也受浏览器 CORS 限制。

推荐开发时由前端开发服务器代理 `/api` 到后端，生产环境使用同源反向代理。若需要健康检查，同样代理 `/health`。以下为 Vite 配置片段，**属于前端建议，不是后端已有配置**：

```ts
server: {
  proxy: {
    '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
  },
}
```

也可由后端另行配置明确的可信来源，但不要直接放开全部来源；CORS 不是认证或权限控制。代理需允许长时间等待执行响应，不要对 POST 自动重试。

当前接口没有访问令牌保护，任何能够访问后端的调用方都可能读取会话或触发文件、命令工具。后端及前端开发代理都应只面向可信本机使用，不要通过监听所有网卡、反向代理或端口转发向不可信网络开放。未来若要做面向他人的部署，需要先设计认证和权限隔离。

## 3. 接口总览

| 方法 | 路径 | 需要认证 | 成功状态 | 功能 |
| --- | --- | --- | --- | --- |
| GET | `/health` | 否 | 200 | 服务存活检查 |
| POST | `/api/v1/sessions` | 否 | 201 | 创建并持久化一个空会话 |
| GET | `/api/v1/sessions` | 否 | 200 | 获取当前工作目录的会话列表 |
| GET | `/api/v1/sessions/{session_id}/messages` | 否 | 200 | 获取会话的完整可展示历史 |
| POST | `/api/v1/sessions/{session_id}/runs` | 否 | 200 | 在指定会话发送一条消息并等待本轮完成 |

### 3.1 GET /health：存活检查

无参数、无请求体、无需鉴权。

```bash
curl -sS http://127.0.0.1:8000/health
```

成功响应：

```json
{"status":"ok"}
```

这是存活检查，不会执行模型或工具；不代表持续探测数据库可用性或保证上游模型可用。

### 3.2 POST /api/v1/sessions：创建会话

#### 请求

| 字段 | 类型 | 必填 | 默认值 | 约束 |
| --- | --- | --- | --- | --- |
| `title` | `string \| null` | 否 | `null` | 最多 200 字符，不含 NUL（`\u0000`）；允许空字符串、纯空白和换行，不自动去除首尾空白 |

```bash
curl -sS http://127.0.0.1:8000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"分析项目"}'
```

以下三个请求体均合法：

```json
{}
```

```json
{"title":null}
```

```json
{"title":"分析项目"}
```

#### 成功响应：201

```json
{
  "session_id": "8f194738-7c09-4f50-a97d-0aedb97e7c67",
  "title": "分析项目",
  "created_at": "2026-06-01 08:00:00",
  "updated_at": "2026-06-01 08:00:00"
}
```

字段说明：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `session_id` | `string` | 后续读取历史、发送消息时使用的会话 ID |
| `title` | `string \| null` | 创建时传入的标题；未传时为 null |
| `created_at` | `string` | 创建时间，正常初始化的数据库返回 UTC，格式 `YYYY-MM-DD HH:mm:ss` |
| `updated_at` | `string` | 最近更新时间，格式同上；成功执行一轮后刷新 |

行为说明：

- 创建接口本身不调用模型或工具，但会立即保存会话。因此创建后即出现在列表中，历史为 `[]`。
- 相同标题可以创建多个会话。重复 POST 会创建不同 ID；该接口也没有幂等去重。
- 没有自动生成/更新标题的承诺。省略标题后即使发送消息，也不会自动从消息生成标题。
- 前端可用“新对话”作为空标题展示兜底，但不要把展示兜底误认为后端存储值。
- 不会切换任何服务端“当前会话”；当前选中的会话由前端自己维护。

时间字符串没有 `Z` 或时区偏移，不应直接用 `new Date(raw)` 并假定所有浏览器都会按 UTC 解析。可先将标准格式转换为 `YYYY-MM-DDTHH:mm:ssZ`，再转换到用户时区。列表展示通常应保留服务端返回顺序。

### 3.3 GET /api/v1/sessions：会话列表

无请求体，无分页、筛选或搜索参数。

```bash
curl -sS http://127.0.0.1:8000/api/v1/sessions
```

#### 成功响应：200

返回 `SessionResponse[]`，每一项字段与创建响应一致：

```json
[
  {
    "session_id": "8f194738-7c09-4f50-a97d-0aedb97e7c67",
    "title": "分析项目",
    "created_at": "2026-06-01 08:00:00",
    "updated_at": "2026-06-01 08:05:00"
  },
  {
    "session_id": "29c8023b-1573-4bc7-ab89-5521db6738f8",
    "title": null,
    "created_at": "2026-06-01 07:00:00",
    "updated_at": "2026-06-01 07:00:00"
  }
]
```

行为说明：

- 没有会话时返回 `[]`，不是 404。
- 只返回后端当前工作目录所属的会话，不返回其他工作目录的记录。
- 排序为 `updated_at` 降序，再按 `created_at` 降序，最后按 `session_id` 升序。
- 列表没有消息摘要、未读数、运行状态、失败状态或消息数量字段；不要从时间戳推断会话是否正在运行或能否继续。
- 读取列表不会刷新时间，也不会执行模型或工具。
- 即使某会话正在运行或处于未完成状态，也可能出现在列表中。能列出不等于能成功读取它的历史。

### 3.4 GET /api/v1/sessions/{session_id}/messages：完整历史

路径参数 `session_id` 为会话 ID，无请求体。

```bash
SESSION_ID='<创建或列表接口返回的 session_id>'
curl -sS "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/messages"
```

#### 成功响应：200

返回按历史顺序排列的 `MessageResponse[]`。以下示例包含一次工具调用：

```json
[
  {
    "role": "user",
    "text": "请查看 README.md",
    "tool_calls": []
  },
  {
    "role": "assistant",
    "text": "我先读取文件。",
    "tool_calls": [
      {
        "id": "call_001",
        "name": "read",
        "args": {"file":"README.md","offset":1,"limit":true}
      }
    ]
  },
  {
    "role": "tool",
    "text": "# OMT\n项目说明……",
    "tool_calls": [],
    "tool_call_id": "call_001",
    "name": "read",
    "status": "success"
  },
  {
    "role": "assistant",
    "text": "这是项目说明的摘要……",
    "tool_calls": []
  }
]
```

字段说明：

| 字段 | 类型 | 返回规则与含义 |
| --- | --- | --- |
| `role` | `"user" \| "assistant" \| "tool"` | 必有；用户、助手或工具消息 |
| `text` | `string` | 必有；可能为空字符串，不保证是 Markdown |
| `tool_calls` | `ToolCallResponse[]` | 必有；没有工具调用时为 `[]` |
| `tool_call_id` | `string` | 可省略；工具结果对应的调用 ID |
| `name` | `string` | 可省略；工具名称 |
| `status` | `"success" \| "error"` | 可省略；工具消息状态，不是整轮运行状态 |

`ToolCallResponse` 字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | `string`，值为空时会省略 | 工具调用 ID；有值时可与结果的 `tool_call_id` 关联 |
| `name` | `string` | 工具名称，不要在前端写死可用工具枚举 |
| `args` | `object` | 工具输入参数，键和值依工具不同而变化，可含嵌套 JSON 数据 |

可空字段在历史响应中会被省略，不要要求每条消息都有 `name`、`status` 或调用 ID。`session.title` 则不同，未设置时仍明确返回 `null`。

行为与展示要求：

- 新建但尚未发送消息的会话返回 `[]`。
- 返回的是完整可展示历史，不是增量。刷新后应**替换**对应会话的消息数组，而不是整体追加，否则会重复展示。
- 一轮可能包含多条助手消息、多次工具调用和多个工具结果，不保证严格“一条 user + 一条 assistant”。
- 助手可能只有工具调用而没有文本。不要丢弃 `text === ""` 但 `tool_calls` 非空的消息。
- 按原数组顺序展示；有调用 ID 时关联工具请求和结果。缺 ID 时保留原消息，不要依赖文本猜配对。
- 本接口不返回消息 ID、消息时间、run_id 或分页游标；渲染键可在当前完整快照内使用索引，但不要将其当永久消息标识。
- 不返回系统提示词、内部模型元数据或二进制附件；消息正文、工具参数及结果仍可能含敏感信息，不代表已脱敏。
- `status: "success"` 表示工具消息状态，不能单独证明 Bash 退出码为 0 或业务目标已完成，仍需看工具结果正文。
- 读取历史不会执行新一轮，也不会更新会话的 `updated_at`。不存在“激活/恢复会话”的 HTTP 调用；选中后读历史、继续向原 ID 发送即可。
- 运行中读取同会话历史会返回 `409 session_busy`，不能把此接口当实时进度流。
- 不存在或属于其他目录：`404 session_not_found`；会话未完成或此前运行失败：可能返回 `409 session_incomplete`，不是部分历史。

### 3.5 POST /api/v1/sessions/{session_id}/runs：发送消息

#### 请求

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `message` | `string` | 是 | 1～32000 字符；不能全为空白，不能含 NUL（`\u0000`）；合法输入的首尾空白与换行原样保留 |

```bash
curl -sS "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/runs" \
  -H 'Content-Type: application/json' \
  -d '{"message":"请分析当前项目结构，暂时不要修改文件"}'
```

只发送当前用户输入，不要传历史。`message: null`、数字、空字符串、纯空白、超过上限或额外字段都会被拒绝。长度由服务端按字符校验；JavaScript 的 `.length` 对部分 emoji 等字符计数不同，前端校验不能替代服务端结果。

HTTP 不解释 CLI 命令；`/resume`、`/exit` 等会作为普通消息发送，不会切换会话或关闭服务。

#### 成功响应：200

```json
{
  "session_id": "8f194738-7c09-4f50-a97d-0aedb97e7c67",
  "run_id": "04aa0222-e299-4c4a-bdea-cb9d6e3cf707",
  "status": "completed",
  "answer": "项目包含 API、CLI 和核心模块……",
  "warnings": []
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `session_id` | `string` | 本轮所属会话 |
| `run_id` | `string` | 本轮请求关联 ID，可用于错误提示和诊断；不能据此查询任务 |
| `status` | `"completed"` | 成功响应固定为 completed；没有 running/pending 的成功响应 |
| `answer` | `string` | 本轮最终助手回答，可为空；不是工具全过程或完整历史 |
| `warnings` | `string[]` | 非致命警告；正常为 `[]` |

目前已知警告：

| 警告 | 含义 | 前端处理 |
| --- | --- | --- |
| `session_timestamp_update_failed` | 本轮已完成，但会话最近更新时间更新失败 | 仍显示成功回答，可提示列表时间可能未更新；不要重新执行本轮 |

行为说明：

- 不是 `202 Accepted`。HTTP 响应到达前，本轮可能仍在处理模型或工具，期间没有进度响应。
- 不承诺固定耗时；客户端和代理不宜设置过短超时。任何客户端超时都不等于后端取消。
- 服务端接续已有历史；已完成会话在后端重启后仍可用原 ID 继续。
- 成功后刷新历史以获取本轮全部消息；刷新列表以获取新的更新时间及排序。
- `answer` 通常已在刷新后的历史末尾。若先临时显示 answer，刷新后要替换/移除临时消息，不能再重复追加。
- 某个工具返回错误内容不一定使整轮失败；若后续完成并返回 200，仍按本轮完成处理。真正运行失败时见 `502 run_failed`。
- 没有幂等键、去重或自动重试。重复 POST 即使内容相同，也可能执行新一轮并重复产生文件/进程副作用。

## 4. 推荐的前端调用流程

### 4.1 页面初始化和会话切换

```text
配置服务地址/同源代理（无需访问令牌）
  → GET /api/v1/sessions
  → 选中一个会话，GET /api/v1/sessions/{id}/messages
  → 用返回数组替换该会话的消息列表
```

- 空列表展示新建入口；空历史展示尚无消息，而不是报错。
- 当前会话 ID 由前端维护。切换会话不调用额外“打开”接口。
- 切换较快时，较早的历史响应可能晚到。按 session_id 或请求序号校验，只更新相应会话缓存，不要覆盖新选中会话。
- 对同会话的历史读取去重，避免组件重复请求互相触发 `session_busy`。

### 4.2 创建并发送

```text
POST /api/v1/sessions，保存返回的 session_id
  → POST /api/v1/sessions/{id}/runs，只传本轮 message
  → 等待最终响应
  → 成功后 GET /api/v1/sessions/{id}/messages 替换历史
  → GET /api/v1/sessions 刷新侧栏
```

可以点击“新建会话”就创建，也可在首条发送时延迟创建；后者是前端策略，不是后端自动行为。若创建失败，不要继续向猜测的 ID 发送。创建请求丢失响应时，可先刷新列表确认，避免连续重复创建。

执行期间禁用同会话重复发送，不对该会话轮询历史。默认全局运行上限为 1；即使切到另一个会话发送，也可能得到 `429 capacity_exceeded`。

### 4.3 建议的页面状态（前端自行维护）

| 前端状态 | 进入条件 | 展示/操作建议 |
| --- | --- | --- |
| `idle` | 初始或已完成 | 可发送 |
| `sending` | 已发出 runs 请求，尚未收到响应 | 展示“正在执行”；禁止重复发送，不假装显示真实工具进度 |
| `completed` | 收到 200 completed | 展示 answer、warnings，刷新历史与列表 |
| `rejected` | 例如 422、session_busy、capacity_exceeded | 修正输入或等待；由用户确认再发，不循环重试 |
| `needs_attention` | session_incomplete、run_failed | 提示需要检查，阻止盲目继续；可新建会话但不代表旧操作已回滚 |
| `unknown` | 发出执行请求后网络断开、客户端超时或响应无法解析 | 提示“无法确认本轮结果”，不要显示为“已取消”或自动重发 |

以上不是服务端返回的会话状态；列表没有这些字段。刷新页面后也没有可查询的持久化运行台账。

结果不明时，可由用户稍后尝试读取历史：若返回成功则检查最新内容，若 busy 则仍有操作占用，若 incomplete 则需人工检查。即使历史能读取，也没有 run_id 对照字段，**不能保证自动判断某次丢失响应的请求是否执行过**。

## 5. 错误格式与处理

应用返回的错误为：

```json
{
  "error": {
    "code": "run_failed",
    "message": "本轮执行失败，可能已有工具副作用，请检查会话",
    "run_id": "04aa0222-e299-4c4a-bdea-cb9d6e3cf707"
  }
}
```

- `code`、`message` 为字符串；`run_id` 可省略，不是每个错误都有。
- 业务错误不采用 FastAPI 默认的 `detail` 数组，不返回字段级校验详情或原始请求内容。
- 错误不暴露底层异常堆栈。前端可以展示 message，但逻辑判断应使用 code。
- 代理、浏览器网络错误或其他基础设施响应不保证采用该 JSON 格式，客户端应保留兜底错误处理。

| HTTP | `error.code` | 含义及建议 |
| --- | --- | --- |
| 404 | `session_not_found` | ID 不存在，或不属于当前工作目录；刷新列表，清理失效选择，不当作空历史 |
| 409 | `session_busy` | 同会话正在运行或读取历史；本次操作被拒绝，稍后由用户重试，不循环发送 |
| 409 | `session_incomplete` | 会话处于未完成/失败状态，不能直接读取或追加；没有在线修复接口，提示人工检查或另建会话 |
| 422 | `invalid_request` | 请求体、类型、长度、格式或额外字段不合法；修正请求，不自动重复发送 |
| 429 | `capacity_exceeded` | 全局运行容量已满，本次运行未接纳，不会排队；等待其他执行结束后由用户重试 |
| 502 | `run_failed` | 本轮运行失败，可能已有文件/命令副作用；同会话后续操作通常转为 incomplete，不自动重发 |
| 503 | `storage_unavailable` | 存储异常；若发生在执行期间，可能已产生副作用。GET 可稍后再查，POST 不盲目重试 |
| 503 | `manager_closed` | 服务未就绪或正在关闭；等待后端恢复；不要将所有 503 都视作已安全回滚 |
| 500 | `internal_error` | 未预期的服务异常；执行结果可能不明，保留诊断信息并提示检查，不自动重发 |
| 404 / 405 等 | `http_error` | 未知路径、方法不允许等 HTTP 错误；检查 URL 和方法，不视为业务数据 |

并发拒绝、容量拒绝或输入校验错误没有可靠的“错误优先级”承诺。例如满载时不要依赖一个无效会话 ID 必然先得到 404。

## 6. TypeScript 类型与最小客户端

以下示例可作为前端 API 层起点，不绑定 UI 框架。假定现代浏览器支持 fetch，并且已配置同源代理；类型与本文响应一致，代码不包含自动重试、超时或取消承诺。

```ts
export interface SessionResponse {
  session_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCallResponse {
  id?: string;
  name: string;
  args: Record<string, unknown>;
}

export interface MessageResponse {
  role: 'user' | 'assistant' | 'tool';
  text: string;
  tool_calls: ToolCallResponse[];
  tool_call_id?: string;
  name?: string;
  status?: 'success' | 'error';
}

export interface RunResponse {
  session_id: string;
  run_id: string;
  status: 'completed';
  answer: string;
  warnings: string[];
}

export interface ErrorResponse {
  error: { code: string; message: string; run_id?: string };
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly runId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false;
  }
  const error = value.error;
  return typeof error === 'object' && error !== null
    && 'code' in error && typeof error.code === 'string'
    && 'message' in error && typeof error.message === 'string'
    && (!('run_id' in error) || typeof error.run_id === 'string');
}

// baseUrl：同源代理时传 ''；只有跨域已允许时才传完整后端地址。
export function createOmtClient(baseUrl: string) {
  const base = baseUrl.replace(/\/+$/, '');

  async function request<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Accept', 'application/json');
    if (init.body !== undefined) {
      headers.set('Content-Type', 'application/json');
    }

    // 网络失败直接抛出；runs 的调用方应按“结果不明”处理，不能自动重试。
    const response = await fetch(`${base}${path}`, { ...init, headers });
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiError(
        response.status, 'invalid_response', '服务或代理返回了无法解析的响应',
      );
    }
    if (!response.ok) {
      if (isErrorResponse(body)) {
        throw new ApiError(
          response.status, body.error.code, body.error.message, body.error.run_id,
        );
      }
      throw new ApiError(response.status, 'unexpected_response', '请求失败');
    }
    // 此断言只提供编译期类型，不等于运行时校验；正式项目可补充响应 schema 校验。
    return body as T;
  }

  const sessionPath = (id: string) =>
    `/api/v1/sessions/${encodeURIComponent(id)}`;

  return {
    health: () => request<{ status: 'ok' }>('/health'),
    listSessions: () => request<SessionResponse[]>('/api/v1/sessions'),
    createSession: (title: string | null = null) =>
      request<SessionResponse>('/api/v1/sessions', {
        method: 'POST', body: JSON.stringify({ title }),
      }),
    getMessages: (id: string) =>
      request<MessageResponse[]>(`${sessionPath(id)}/messages`),
    run: (id: string, message: string) =>
      request<RunResponse>(`${sessionPath(id)}/runs`, {
        method: 'POST', body: JSON.stringify({ message }),
      }),
  };
}
```

最小调用顺序（在按钮事件等显式用户操作中执行，不要在渲染/自动重试逻辑里触发）：

```ts
// 已配置同源代理，无需访问令牌。
const api = createOmtClient('');
const session = await api.createSession('分析项目');
const result = await api.run(session.session_id, '请分析项目结构，暂时不要修改文件');
// 到这里运行已成功。后面的刷新失败只能提示“刷新失败”，不能重新调用 run。
console.log(result.answer, result.warnings);
const messages = await api.getMessages(session.session_id);
const sessions = await api.listSessions();
// 用 messages 替换该会话历史，用 sessions 替换侧栏列表。
```

生产 UI 应分别处理“运行失败”和“成功后刷新失败”，并按 session_id 保存异步结果；不要因为切换了会话就把返回答案放进当前新会话。也不要把用户/助手/工具正文作为可信 HTML 直接插入页面；Markdown 渲染需禁用或清理不可信 HTML，工具参数与输出建议以转义文本或 JSON 展示。

## 7. 能力边界与交付给前端 AI 的约束清单

### 已支持

- 创建空会话、获取列表、按 ID 加载完整历史。
- 在已有会话连续发送多轮消息，历史由后端保存。
- 获取最终回答及非致命警告。
- 从历史中展示助手的工具请求及工具结果。
- 已完成的持久化会话在服务重启后继续使用。

### 当前没有对应接口或保证

- SSE、WebSocket、逐 token 文本、实时工具进度。
- `GET /runs/{run_id}`、后台任务列表、持久化运行状态。
- 停止、取消、暂停、断线事件补发或恢复执行；浏览器 AbortController 只停止本地等待，不保证后端停止。
- 会话删除、重命名、搜索、服务端分页、单独查询会话详情。
- 编辑/删除历史、重新生成、分支、上传图片或附件。
- 前端指定模型、系统提示词、工具、MCP、Skills 或工作目录。
- 登录、注册、令牌刷新、多用户会话隔离、工具执行审批或沙箱。
- 请求幂等、失败回滚、断线后可靠判断某次请求是否执行过。

前端第一版应围绕“会话侧栏 + 历史消息 + 输入框 + 等待状态 + 错误/警告提示”实现。不要把未实现能力做成看似有效的按钮，也不要用模拟打字动画冒充服务端实时输出。若使用演示数据，应与真实接口模式明确区分。

## 8. 文档维护依据

本文契约核对范围为 `src/omt/api/app.py`、`routes.py`、`schemas.py`、`settings.py`，以及与外部会话行为相关的 `src/omt/core/session_manager.py`、`store.py`。接口变化时应同步更新本文。

仓库内进一步阅读：

- [后端启动配置与 HTTP 使用说明](api.md)
- [HTTP 与会话行为测试说明](tests/api.md)
- [项目快速开始](../README.md)
