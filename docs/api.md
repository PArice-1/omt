# 本机 FastAPI 接口

当前提供固定工作目录、单用户、单进程的非流式 HTTP 入口。CLI `omt` 继续可用；HTTP 不解析 `/resume` 等 CLI 命令，消息中的这些文本会作为普通输入提交。

## 启动前提

1. 用 `uv sync` 安装项目及锁定依赖。
2. 按[架构说明](architecture.md#3-模型连接与配置)配置模型。HTTP 与 CLI 共用模型配置，访问令牌不是模型 API Key。
3. 预先创建 SQLite 数据库及 `sessions` 表，表结构见[会话索引说明](tests/store.md#接口和预建表约定)。HTTP 不自动创建目录、索引表或迁移；LangGraph 自行管理检查点表。
4. 在可信的目标工作目录启动。文件工具和 Bash 使用进程 cwd，配置的工作目录必须与启动 cwd 一致，不能通过请求切换目录。

```bash
# 以下是人工启动，不属于离线测试；提交消息可能计费并修改文件。
# 使用临时环境变量，不把令牌写入源码或命令历史中的字面值。
export OMT_API_ACCESS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OMT_API_DB_PATH="$HOME/.omt/agent/state/omt.sqlite"
uv run omt-api
```

默认监听 `127.0.0.1:8000`，一个 worker，不启用自动重载。不要把 `--workers` 调大或同时用 CLI 写相同会话。需要从其他目录启动时，应先安装项目并在该目录运行 `omt-api`，或使用 `uv run --project <OMT仓库路径> omt-api`；cwd 仍须核对。

### 启动配置

环境变量使用 `OMT_API_` 前缀，也可向 `omt.api.app.create_app(ApiSettings(...))` 显式注入配置。导入模块不会加载模型或连接数据库，资源在 FastAPI lifespan 内建立和释放。

| 变量 | 默认值 / 约束 |
| --- | --- |
| `ACCESS_TOKEN` | 必填；去除首尾空白后长度至少 32，实际比较保留原值；建议随机生成 |
| `DB_PATH` | `~/.omt/agent/state/omt.sqlite`；必须已存在且有 sessions 表 |
| `WORKSPACE_PATH` | 启动 cwd；规范化后必须与进程 cwd 相同 |
| `MAX_CONCURRENT_RUNS` | `1`；正整数，满载时立即返回 429，不排队 |
| `SKILL_ROOTS` | 未配置时依次为用户 Skills 和工作目录 Skills；JSON 数组 `[]` 关闭发现 |
| `MCP_SERVERS` | 默认 `{}`，不启用 CLI 中的本机硬编码 MCP；JSON 对象，支持 stdio 和 streamable_http |

例如 MCP 可以由服务端配置为：

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

stdio 还支持 env，streamable_http 支持 url 和 headers。它们都是可信管理员的启动配置，不接受 HTTP 请求修改；不要把真实凭据写入仓库。模型和工具在每次应用 lifespan 中组装一次。MCP 工具的连接生命周期由适配库管理，不承诺每个远程 MCP 会话都常驻或调用结果可撤销。

## Interface

除 `/health` 外，所有业务请求都需 `Authorization: Bearer <访问令牌>`。默认关闭 `/docs`、`/redoc` 和公开 OpenAPI 端点，不配置跨域允许列表；需要前端跨域时应另行设计明确来源，不能把 CORS 当认证。

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /health` | 无需认证 | `200 {"status":"ok"}`，不请求模型；是存活检查，不持续探测数据库或上游 |
| `POST /api/v1/sessions` | JSON `{}` 或 `{"title":"标题"}` | 201，会话元数据 |
| `GET /api/v1/sessions` | 无 | 200，当前工作目录的元数据数组，沿用 Store 修改时间排序 |
| `GET /api/v1/sessions/{id}/messages` | 无 | 200，历史数组；空会话为 `[]` |
| `POST /api/v1/sessions/{id}/runs` | `{"message":"请求文本"}` | 200，等待本轮结束后返回最终回答 |

标题最多 200 字符，可为 null；消息为 1～32000 字符，拒绝纯空白和 NUL，合法文本保留首尾空白和换行。请求体拒绝额外字段，不能提交 workspace、模型地址、工具配置或客户端生成的内部状态。

### 最小调用

```bash
curl -sS http://127.0.0.1:8000/api/v1/sessions \
  -H "Authorization: Bearer $OMT_API_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"分析项目"}'

# 将上一步返回的 session_id 保存为 SESSION_ID。
curl -sS "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/runs" \
  -H "Authorization: Bearer $OMT_API_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"请分析当前项目结构，暂不修改文件"}'
```

执行成功的返回结构：

```json
{
  "session_id": "...",
  "run_id": "...",
  "status": "completed",
  "answer": "最终回答",
  "warnings": []
}
```

session_id 直接对应检查点 thread_id；run_id 只用于本次请求关联，不是持久化任务记录。当前没有按 run_id 查询、幂等去重或自动重试能力。若执行已成功、但索引 updated_at 更新失败，仍返回 completed，warnings 包含 `session_timestamp_update_failed`，避免客户端误重试工具操作。

历史响应只投影 user、assistant、tool 的文本和工具关联字段。助手可含 tool_calls（id、name、args）；工具含 tool_call_id、name、status。不返回系统提示词、artifact、模型元数据或原始检查点。工具参数和消息正文可能含敏感内容，这种字段过滤不是通用脱敏或沙箱。

### 错误

统一结构为 `{"error":{"code":"...","message":"...","run_id":"可选"}}`，不回传底层异常原文、Pydantic input/context 或数据库位置。

| HTTP 状态 | code | 含义 |
| --- | --- | --- |
| 401 | unauthorized | 缺失或错误 Bearer 令牌 |
| 404 | session_not_found | 不存在，或属于另一个工作目录 |
| 409 | session_busy | 同会话正在运行或读取历史 |
| 409 | session_incomplete | 已有未完成检查点，或本进程观察到失败/取消，不能直接追加输入 |
| 422 | invalid_request | 字段、格式、类型或文本校验失败 |
| 429 | capacity_exceeded | 全局运行容量已满 |
| 502 | run_failed | 模型、工具或终态校验失败；可能已有副作用 |
| 503 | storage_unavailable / manager_closed | 存储异常或应用未就绪/关闭中 |
| 500 | internal_error | 未预期错误，不暴露实现细节 |

## 会话与失败语义

- 创建时立即写入索引，允许空会话；不同于 CLI 首次输入前登记。空会话在重启后仍能开始首轮。
- 每次历史读取/运行都从最新检查点装载 Runtime，不维护全局“当前会话”或无限 Runtime 缓存；运行成功后才提交 Runtime 内存历史，再刷新索引修改时间。
- 已完成会话在应用重新启动后可用原 ID 继续；已有但未完成的检查点不会自动恢复执行或伪装成空会话。此版没有修复/重置检查点的 HTTP 入口，需人工检查，必要时创建新会话。
- 失败/取消在本进程中使会话不可直接重试；重启后根据磁盘最新检查点重新判断，不提供持久化失败台账。不保证取消、失败或丢失响应时没有发生工具副作用，也不保证索引与检查点原子提交。
- 同会话操作互斥；不同会话可在配置的全局上限内并发。进程内占位不是跨进程锁，外部 CLI/其他 worker 的同会话写入不受保护。
- 关闭 Manager 时拒绝新操作，取消并等待活跃历史读取/运行，再由 lifespan 关闭检查点连接。同步工具线程或外部进程可能延迟退出，文件和远程副作用不会回滚。
- 普通 JSON 请求期间客户端断线不保证立即取消执行；本版没有断线重连、事件补发、后台任务查询、超时策略或取消 Interface。等待超时/断线后不要盲目重试。

## 安全范围与后续工作

这是具有当前用户文件和进程权限的执行入口，只适合可信本机调用方。固定目录不是文件沙箱，路径解析也不会限制模型读取目录外文件。不要直接暴露公网；多用户、工具审批、进程/文件隔离、速率和请求体资源限制需单独实现。

SSE、持久化运行状态、取消、断线重连与多用户均未实现。相关离线验证及限制见 [HTTP 测试说明](tests/api.md)。
