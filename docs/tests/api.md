# HTTP 与 SessionManager 测试说明

## 对象与目的

- 源码：[api/](../../src/omt/api/)、[session_manager.py](../../src/omt/core/session_manager.py)、[bootstrap.py](../../src/omt/core/bootstrap.py)。
- 测试：[test_api.py](../../tests/api/test_api.py)。
- 验证 HTTP Interface 的可观察响应、真实 SQLite 历史恢复、会话隔离与失败清理；不以 mock 写入或内部方法调用次数替代执行结果。CLI 入口见 [CLI 测试](cli.md)，共用组装见[配置测试](session-and-config.md)，事件与静默执行见 [Conversation 测试](conversation.md)。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 鉴权和输入 | 缺失/错误/非 Bearer 认证；空白、NUL、超长、非字符串消息；额外 workspace 字段 | 业务请求 401 或 422，不产生会话；响应不回显 input；health 无需认证，公开 docs 关闭 | test_http_auth_validation_and_health |
| 完整工具回合与跨应用继续 | 假模型调用本地工具，返回 artifact 和元数据；新建应用、Manager 与 Saver 后继续旧 ID | 200 仅返回最终回答；历史包含工具关联但无系统消息/artifact/元数据；两轮不重复上下文；索引标题不变、修改时间刷新；终端无过程打印 | test_http_multiturn_restart_empty_session_and_history_projection |
| 空会话跨应用首轮 | 只创建索引，不执行；重新打开应用 | 读取历史为空，重启后能开始首轮，不把无检查点误判为未完成历史 | 同上 |
| 不存在、跨目录和未完成 | 临时库包含其他目录记录，以及 HumanMessage 结尾的真实检查点 | 列表过滤目录，跨目录及不存在返回 404；未完成返回 409，不调用模型 | test_http_missing_foreign_and_incomplete_sessions |
| 工具失败和存储错误 | 工具抛带测试私有标记的异常；随后删除临时 sessions 表 | 执行 502 带 run_id，不暴露原文；同会话重试 409；列表存储错误 503，不暴露 SQL/路径 | test_http_errors_are_sanitized_and_failed_run_is_not_retried |
| 完成后更新时间失败 | 工具/模型执行正常，只让 Store 更新时间抛 SQLite 异常 | 200 completed 保留回答与已提交历史，warnings 明确部分失败，不诱导重跑 | test_timestamp_failure_returns_completed_with_warning |
| 同会话互斥、全局容量 | 异步工具等待期间对同/不同 ID 再次运行；全局上限分别为 1、2 | 同 ID session_busy；满载 capacity_exceeded；上限 2 时其他会话独立完成，不混入第一会话消息 | test_manager_concurrency_cancellation_and_shutdown |
| 取消与关闭 | 取消等待工具的任务，或调用 Manager.aclose | 工具 finally 执行，任务终止，容量释放；关闭后拒绝新运行，检查点连接在 Manager 清理后退出 | 同上 |
| 启动失败关闭 | 令牌缺失/空白、数据库缺失、工作目录不匹配、sessions 表缺失 | 配置或启动失败；不创建空库，不提前读取模型配置 | test_settings_and_startup_fail_closed |
| 默认 lifespan 共用组装 | 临时假 settings，SDK 包装对象/MCP 发现为替身，真实默认 bootstrap/Store/Agent/Saver | 应用工厂创建时无模型/MCP 副作用；两轮成功且每个 lifespan 只初始化一次模型/发现；退出后业务请求 503 | test_default_lifespan_builds_dependencies_once_and_cleans_up |
| MCP 配置和敏感值 | 环境变量提供测试 HTTP MCP 配置，短测试令牌触发校验失败 | 默认禁用 MCP；显式配置转换为正确 Connection；repr/格式化异常不回显测试令牌及 header | test_mcp_configuration_is_explicit_and_secrets_are_not_rendered |

同会话/全局并发通过公开 Manager Interface 和 asyncio.Event 验证，避免用线程睡眠猜测请求时序。HTTP 层的 409 在失败重试及不完整检查点场景验证；容量错误的映射为固定 429，尚未通过并发真实网络请求验证。

## CLI/HTTP 共用回归

test_cli_and_http_share_one_manager_without_leaking_terminal_output 使用同一事件循环和 Manager：HTTP 创建空会话，CLI /resume 后执行一轮，再由 HTTP 追加。预期同一 ID 的用户/助手四条消息完整且无重复；CLI 回答只打印一次，后续 HTTP 回答没有终端输出。它验证单轮事件回调隔离与共用历史规则，不验证两个独立进程共享锁。

共用配置类型从 api.settings 移至 core.configuration；ApiSettings 及公开 HTTP 字段不变。默认 lifespan 现在委托 bootstrap.open_session_manager，原组装测试仍验证预建库、MCP 类型、工具、系统提示词、关闭后拒绝操作等可观察行为。

## 隔离方式

- 模块级自动 fixture 将 cwd、settings.root 指向 tmp_path，前后清理模型设置缓存，移除本组使用的宿主配置环境变量。所有数据库均由测试显式建表，不读取本机配置或凭据。
- HTTP 使用 TestClient，不监听端口；LangGraph 图、Conversation、SessionStore、AsyncSqliteSaver 使用真实实现。注入的 LocalModel 只产生本地预设消息，工具只返回测试数据或等待本地 Event，不运行 Bash、修改项目文件或访问网络。
- 重启场景关闭原应用/Saver，再创建新应用与连接，不复用内存 Conversation。正常测试不 mock SQLite。只有更新时间写入失败和默认依赖组装处使用限定替身。
- 取消型用例有等待超时和 finally 清理，退出 Manager 后再关闭 Saver；不保留测试工具任务。MCP 发现一律不启动真实服务，默认 lifespan 用 AsyncMock 返回空工具列表。

## 执行

```bash
uv run pytest -q tests/api/test_api.py --tb=no
uv run pytest -q tests/api tests/cli tests/core/test_conversation.py tests/core/test_conversation_history.py tests/core/test_bootstrap.py --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/api src/omt/cli src/omt/core/bootstrap.py src/omt/core/configuration.py src/omt/core/events.py src/omt/core/session_manager.py src/omt/core/conversation.py tests/api/test_api.py
```

## 覆盖限制

- 不请求真实模型/MCP，不验证用户令牌有效性、网络代理、反向代理、浏览器跨域或实际端口部署。
- “重启”是同一测试进程中重建应用和 SQLite 连接，不是实际操作系统进程重启；不覆盖多 worker/CLI 争用、持久化运行状态和幂等重试。
- 不提供/验证 SSE、事件补发、取消 HTTP Interface 或断线后台运行。普通 JSON 断线不保证立即取消。
- Async 工具的清理不证明任意同步工具线程、远程调用或外部进程能立即停止，也不证明工具副作用回滚。
- 历史字段过滤不等于文本脱敏、目录沙箱或多用户授权；固定工作目录仍有当前系统用户权限。
- 未覆盖真实磁盘损坏、配额和大负载；请求文本长度校验不是整个 HTTP 请求体的内存限制。
