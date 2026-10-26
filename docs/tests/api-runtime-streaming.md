# HTTP 运行配置与 SSE 测试说明

## 对象与目的

- 源码：[api/runtime.py](../../src/omt/api/runtime.py)、[configuration_routes.py](../../src/omt/api/configuration_routes.py)、[streaming.py](../../src/omt/api/streaming.py)、[schemas.py](../../src/omt/api/schemas.py)、[app.py](../../src/omt/api/app.py)，以及 [SessionManager](../../src/omt/core/session_manager.py) 的接纳通知。
- 测试：[test_runtime_streaming.py](../../tests/api/test_runtime_streaming.py)。
- 目的：保证 HTTP 使用真实共用 Runtime/Manager，不在路由重新执行模型或工具；首次配置不提前落盘，快速切换不保存，SSE 真正增量输出且断线能够回收运行。
- 核心正文/reasoning 的同步/异步投影与只读快捷目录另见[核心 reasoning 与模型选择测试](reasoning-and-model-choices.md)。原 JSON 兼容、会话恢复、存储错误及 CLI 共用回归见 [HTTP/Manager 测试](api.md)。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 首次完整配置 | 无配置无库；查状态/厂商/快捷列表，预览后先选非法模型再确认合法项 | 未确认不创建配置/数据库、不初始化 SDK；无配置时精确枚举已注册的 deepseek/ollama 且均未登录（Ollama 新注册不改变现有凭据契约）；非法选择 422；确认保存完整目录、初始化缺失库并立即可运行；确认响应、GET runtime 和快捷目录都按 none/low/high/max 返回可选等级；修复旧字母排序，与 CLI 一致；预览不可重复使用；响应不含 Key | test_first_configuration_is_preview_then_confirm_and_runs |
| 预览过期、限额及错误 | 限额 1、TTL 2 的临时配置；测试时钟推进；目录查询失败、空白 Key、额外地址字段 | 达上限 429；过期及重建应用后的旧 ID 404；网络/输入错误安全，不落盘、不泄露假秘密 | test_preview_limits_expiry_and_errors_do_not_persist |
| 查询中的预览也限额 | 第一请求在本地 Event 等待，再申请第二份；取消第一请求 | 第二请求立即 429；取消释放名额，之后预览成功，始终未保存 | test_inflight_previews_are_limited_and_cancellation_releases_slot |
| 保存与启用分别报告 | 首次确认保存后 SDK 构建抛含假秘密的异常 | 200，configuration_saved=true、runtime_ready=false、error 非空；磁盘默认可读，服务保留配置入口，错误不含秘密 | test_confirmation_reports_saved_but_not_enabled |
| 快速切换和思考控制 | 完成一轮后设 high、换到不支持 high 的模型，继续原会话；再让构建失败 | 请求 high 保留、实际 none；主动 unknown 等级 422；下一轮新模型收到完整历史，仅一条索引；所有 JSON 字节不变；构建失败 502，原运行模型保持 | test_quick_switch_and_thinking_preserve_history_and_configuration |
| 重启不记住临时选择 | 临时选择 high 和另一模型，关闭后重建应用 | 读取原磁盘默认模型、等级 none，不把进程选择伪装成持久化 | test_restart_uses_saved_defaults_not_temporary_selection |
| 切换取消与并发保护 | 新 Agent 构建等待 Event，同时请求新会话和另一次切换，再取消构建请求 | 状态 changing=true；两个操作 409；取消后原模型、Manager 和状态保持可用，能完成正常一轮 | test_switch_cancellation_blocks_new_work_and_keeps_old_runtime |
| reasoning、并行同名工具及历史 | 两次助手消息含标准 reasoning 内容块，调用同名工具两次；重建应用查询 | SSE seq 递增、run/session 一致；OpenAPI 正确声明 SSE 及安全 422 错误结构；正文/reasoning 分开；工具按不同调用 ID 关联；流中助手 ID 与历史一致；成功终态只含最终答案；SQLite 重开后完整历史及 reasoning 保留 | test_stream_protocol_reasoning_tool_links_and_restart_history |
| 接纳前拒绝与执行失败 | 不存在会话；另一个真实图的工具抛假秘密异常，再次运行失败会话 | 不存在为 HTTP 404 且未调用模型；已接纳执行 HTTP 200 后以 run.failed 结束，不泄露异常、不冒成功终态；重试 409，不发送 started | test_stream_rejections_and_sanitized_execution_failure |
| 真增量、互斥、容量与断线 | 原始 ASGI send 收到 tool.started 后工具仍在 Event 等待；同时切设置、重入、另一会话运行 | 执行未结束已收到 run.started 和正文；切换/重入 409，满载 429，均不启动第二次执行 | test_live_stream_is_incremental_and_disconnect_cleans_up |
| 网络失败、通知断线及发送超时 | 同上，参数化 ASGI 2.4 send 抛 OSError、send 永久等待、ASGI 2.0 receive 返回 disconnect，以及正常完成 | 失败/超时/断线进入清理，工具 finally 执行，busy 清零，失败会话不可直接追加；正常流完成后历史可读 | 同上 |
| 缓冲溢出 | 参数化字节限额 1024 的大正文，以及事件数限额 1 的突发输出 | 不截断伪装成功，不发送超大正文；终态 stream_buffer_exceeded，运行释放容量，另一会话仍可执行 | test_buffer_overflow_terminates_and_releases_capacity |

补充回归：`test_handled_tool_error_and_timestamp_warning_still_complete` 让工具返回已处理错误并让索引更新时间失败，预期工具失败事件后仍有成功终态、最终解释及 timestamp warning，不泄露存储异常；`test_stream_storage_failure_before_admission_is_http_error` 删除临时 sessions 表，预期接纳前 503 storage_unavailable、不启动模型、不打开事件流。工具关联用例同时发出 JSON 进度，核对 tool.delta 的调用 ID。

`test_history_projects_legacy_reasoning_without_duplicating_standard_blocks` 通过原 JSON 执行两轮，分别保存旧 reasoning_content 和同时含标准块/旧字段的消息；历史预期各返回一份 reasoning，正文不混入思考。它锁定新增历史字段对旧检查点的兼容与去重。

## 隔离方式

- 模块级 autouse fixture 把 settings.root 和 cwd 指向 tmp_path；清理 OMT_API_* 与模型选择环境变量，前后清空设置缓存。
- 厂商目录查询和 MCP 发现一律用本地异步替身；SDK 构建返回 RecordingModel，Key 为固定假标记。无真实模型、凭据、网络请求、MCP 进程或 Bash。
- 实际 SessionRuntime、Agent 图、工具循环、SessionManager、SessionStore 和 AsyncSqliteSaver 不替换。工具只返回本地文本或等待测试 Event。
- 大部分 HTTP 测试用 TestClient；真正增量/断线用原始 ASGI send/receive 捕获帧，并用同事件循环的 AsyncClient 并发访问真实路由。避免 TestClient/ASGITransport 收集完整响应后才返回导致“伪流式”测试通过。
- 预览有效期的逻辑边界通过替换 api.runtime 导入的 monotonic 测试，不修改整个 asyncio 的时钟。生产清理还使用事件循环定时器，到期/确认/关闭清除预览并取消计时句柄。
- 等待均有超时，finally 取消并 gather 测试任务；取消运行在 Saver 关闭前完成。发送超时用本地永不完成的 Event 触发，不靠外部网络卡顿。

## 执行

```bash
uv run pytest -q tests/api/test_runtime_streaming.py --tb=no
uv run pytest -q tests/api tests/core/test_reasoning.py tests/core/test_model_choices.py tests/cli --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/api src/omt/core/bootstrap.py src/omt/core/session_manager.py tests/api/test_api.py tests/api/test_runtime_streaming.py
```

## 覆盖限制

- 不证明真实 DeepSeek 的 effort、reasoning 可用性和输出质量；只验证当前依赖已暴露的内容块通过共用执行层正确传递。
- 原始 ASGI 验证应用级即时发送、断线通知/发送错误及清理，不代替真实 TCP、浏览器、反向代理、CORS 或局域网测试。发送错误可由 Starlette 转换为 ClientDisconnect；测试按框架实际行为验收。
- 不证明远端断线能瞬时发现；无输出时靠心跳触发下一次发送。不保证已不可写的连接收到 cancelled/failed 终态。
- 同步工具线程、外部进程和已发生的副作用不保证停止或回滚。取消/失败保护不等于数据库事务。
- 未模拟断电、磁盘满、跨文件保存事务、真实大负载和底层 SDK 内部无限缓冲；API 队列上限不是进程整体内存限额。
- 不实现/验证后台任务查询、幂等、事件补发、独立取消接口、按会话保存模型、多 worker、鉴权或多用户隔离。
