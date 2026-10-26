# 本机 FastAPI 接口

HTTP 与 CLI 共用 SessionRuntime、SessionManager 和 SQLite 检查点。HTTP 支持首次模型配置、运行中切换、普通 JSON 运行及 SSE 流式运行。完整字段、OMT v1 事件协议、事件到界面的更新规则与浏览器示例见[前端接入 API 文档](frontend-api.md)；前后端如何实现同一次 HTTP 响应的增量发送与读取，见[流式输出如何实现](frontend-api.md#流式输出如何实现)。

> 仅面向可信本机、单用户、单进程。当前没有启用访问令牌认证；工具拥有当前用户的文件和进程权限，没有沙箱或执行审批。不要向不可信网络开放。

## 启动

```bash
uv sync
# 在可信的目标工作目录运行；启动不是离线验证，发送消息可能计费和执行工具。
uv run omt-api
```

默认监听 `127.0.0.1:8000`，一个 worker，无自动重载。工作目录必须与进程实际 cwd 一致；不能通过请求切换目录。不要增加 worker，或同时用 CLI 写同一会话。

- **没有模型配置**：服务仍可启动，`GET /api/v1/runtime` 返回 `configured=false, ready=false`；不初始化模型、MCP 或数据库。配置与公开文档接口可用，会话接口返回 `503 runtime_not_ready`。
- **已有完整配置**：启动时尝试打开 Runtime，正常启动仍要求数据库及 sessions 表已存在。初始化失败不关闭配置入口，状态为未就绪；不回显底层异常。
- **通过 HTTP 确认首次配置**：保存模型配置后显式初始化缺失数据库，再启用 Runtime。已有文件不修复、不迁移；保存和启用分别报告结果。
- 模型文件仍为 `~/.omt/agent/settings.json`、`auth.json`、`model-store.json`。读取状态、模型列表和预览目录不会修改它们。

手动配置及预建数据库说明见[快速开始](../README.md)。如果首次启用失败，可检查问题后重新预览、确认；预览支持复用已保存 Key。

### 启动配置

环境变量前缀为 `OMT_API_`，也可通过 `create_app(ApiSettings(...))` 注入。资源在 lifespan 中创建；导入模块和创建 app 不连接数据库或初始化模型。

| 后缀 | 默认值 / 约束 |
| --- | --- |
| `ACCESS_TOKEN` | 空字符串；仅兼容字段，配置后也不启用鉴权 |
| `DB_PATH` | `~/.omt/agent/state/omt.sqlite` |
| `WORKSPACE_PATH` | 启动 cwd，必须与实际目录一致，不是沙箱 |
| `MAX_CONCURRENT_RUNS` | `1`，正整数；满载立即 429，不排队，不限制图内部工具并发 |
| `SKILL_ROOTS` | `["~/.agents/skills"]`；JSON 数组替换，`[]` 关闭发现 |
| `MCP_SERVERS` | `{}`；可信管理员配置，不接受请求修改 |
| `STREAM_BUFFER_EVENTS` | `256`，正整数；积压业务事件数上限 |
| `STREAM_BUFFER_BYTES` | `1048576`，至少 1024；积压编码后字节数及单条事件上限 |
| `STREAM_SEND_TIMEOUT` | `30.0` 秒，正数；单次 ASGI 发送等待上限，不是整轮运行超时 |
| `STREAM_HEARTBEAT` | `15.0` 秒，正数；无事件时发送 SSE 注释心跳 |
| `LOGIN_PREVIEW_TTL` | `600` 秒，正整数；预览生成后的有效期，到期清除内存凭据 |
| `LOGIN_PREVIEW_LIMIT` | `8`，正整数；已生成及查询中的预览共用名额 |

SSE 缓冲不包含底层 SDK/图流自己的内部缓冲，不是整个进程的内存限制。网络中正在发送的一条事件和单独终态也有有限的额外占用。超限时终止本轮，不截断或静默丢弃正文。

MCP 支持 stdio 和 streamable_http，连接参数见[架构说明](architecture.md)。例如：

```json
{
  "knowledge": {
    "transport": "stdio",
    "command": "/trusted/server/.venv/bin/python",
    "args": ["-m", "example.server"],
    "cwd": "/trusted/server"
  }
}
```

stdio 还支持 env；HTTP 支持 url 和 headers。只使用可信地址和进程，不把真实凭据写入仓库。

## 接口总览

所有接口当前免认证，`/docs`、`/redoc` 和 `/openapi.json` 可直接访问。未配置 CORS 允许列表；浏览器建议同源代理，CORS 不等于认证。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 200 存活检查，不探测模型/数据库 |
| `GET /api/v1/runtime` | 配置就绪状态、当前模型和思考等级、忙碌快照 |
| `GET /api/v1/providers` | 注册厂商及本地凭据状态，不返回 Key |
| `GET /api/v1/models` | 已保存凭据的厂商下全部本地已导入模型，不联网 |
| `POST /api/v1/model-logins` | 201；查询厂商目录并创建内存预览，未确认不落盘 |
| `POST /api/v1/model-logins/{id}/confirm` | 保存所选模型及完整目录，尝试启用；分别返回保存/运行结果 |
| `PUT /api/v1/runtime/model` | 从本地快捷列表切换当前进程模型，不保存 |
| `PUT /api/v1/runtime/thinking` | 修改当前进程思考等级，不保存 |
| `POST /api/v1/sessions` | 201；显式登记空会话 |
| `GET /api/v1/sessions` | 当前工作目录的会话列表 |
| `GET /api/v1/sessions/{id}/messages` | 完整可展示历史，正文与 reasoning 分开 |
| `POST /api/v1/sessions/{id}/runs` | 原有非流式 JSON 响应，等待最终答案 |
| `POST /api/v1/sessions/{id}/runs/stream` | POST + SSE，持续返回一轮事件 |

首次配置的预览会显式查询提供商目录；快速列表、切换和模型构建不发送聊天试运行请求。HTTP 不解析 `/login`、`/models`、`/thinking` 等 CLI 命令，消息中的这些文本仍是普通 prompt。

## 最小调用

先读状态；没有配置时，前端通过密码输入框收集 Key，调用模型预览接口，再确认模型。不要把真实 Key 写进命令行历史或日志。详细请求见[首次配置](frontend-api.md#3-首次配置)。

已有运行环境时：

```bash
curl -sS http://127.0.0.1:8000/api/v1/runtime
curl -sS http://127.0.0.1:8000/api/v1/models

curl -sS http://127.0.0.1:8000/api/v1/sessions \
  -H 'Content-Type: application/json' -d '{"title":"分析项目"}'

# 将创建结果保存为 SESSION_ID；提交 prompt 可能执行工具。
curl -N "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/runs/stream" \
  -H 'Content-Type: application/json' \
  -d '{"message":"请分析当前项目结构，暂不修改文件"}'
```

`curl -N` 关闭客户端缓冲。代理也需关闭响应缓冲、允许长连接，不得自动重试执行 POST。一次请求只有一个 HTTP 响应，事件用 SSE 空行分帧；不是服务端回调客户端地址。

原 `/runs` 保留以下成功结构：

```json
{"session_id":"...","run_id":"...","status":"completed","answer":"最终回答","warnings":[]}
```

模型和思考等级是整个 API 进程的设置，影响所有会话的后续轮次；当前不按 Session 保存。切换失败保留原运行实例；运行或互斥请求期间切换返回 409，不排队。换模型保留请求等级，缺映射时实际回退为 none；主动设置不支持的等级返回 422。重启后回到磁盘默认。模型等级、请求等级和实际 effort 的解释见[架构说明](architecture.md#3-模型连接与配置)。

## 错误与执行边界

普通错误结构：`{"error":{"code":"...","message":"...","run_id":"可选"}}`。不回传底层异常原文、校验输入、密钥或数据库位置。

| HTTP | code |
| --- | --- |
| 404 | `session_not_found`、`login_preview_not_found`（含过期） |
| 409 | `session_busy`、`session_incomplete`、`manager_busy`、`configuration_invalid`、`runtime_immutable` |
| 422 | `invalid_request`、`invalid_model`、`invalid_thinking_level`、`login_preview_failed` |
| 429 | `capacity_exceeded`、`login_capacity_exceeded` |
| 502 | `run_failed`、`model_change_failed` |
| 503 | `storage_unavailable`、`manager_closed`、`runtime_not_ready`、`configuration_save_failed` |
| 500 | `internal_error` |

确认接口若配置保存成功但启用失败，仍返回 200，`configuration_saved=true`，`error` 非空，`runtime_ready` 表示是否仍有可用实例；它可能是旧模型。保存失败可能已经逐文件部分写入，不承诺跨文件事务。

SSE 接纳前返回普通 HTTP 错误；`run.started` 表示完成占位及历史校验，尚不保证上游可达。接纳后失败使用 `run.failed`，HTTP 状态不会从已发送的 200 改成 502。只有 `run.completed` 表示成功；EOF 或心跳不是成功终态。

- 主动中止流式请求或检测到断线时，尝试取消并等待本轮清理；没有独立取消接口。
- ASGI 发送失败/发送超时以及旧协议的断线通知都进入清理。无输出时心跳有助于发现断线，但不保证即时发现。
- 缓冲溢出以 `stream_buffer_exceeded` 终止；不可投影的数据为 `invalid_stream_data`；终态过大为 `stream_event_too_large`。连接已不可写时不能保证送达终态。
- 失败/取消会触发本进程的会话保护；后续不能直接重跑，工具副作用不回滚。成功后更新时间失败只返回 warning，不诱导重跑。
- 同步工具线程或外部进程不保证立即停止。普通 JSON 断线仍不保证立即取消。
- 无后台运行台账、按 run_id 查询、幂等、重连补发或自动恢复执行。不得自动重试运行请求。

`create_app(manager_factory=...)` 保留嵌入兼容：固定 Manager 模式不读取模型配置，`mutable=false`、thinking 为 null，配置与切换接口返回 `runtime_immutable`；会话 JSON/SSE 仍可用。默认入口及 `runtime_factory=...` 使用可切换 Runtime，两种工厂不能同时注入。

离线证据见 [HTTP 测试](tests/api.md)及[运行配置与流协议测试](tests/api-runtime-streaming.md)。
