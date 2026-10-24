# CLI 推理强度与运行时重建测试说明

## 对象与目的

- 源码：[bootstrap.py](../../src/omt/core/bootstrap.py) 的 SessionRuntime/open_session_runtime/ThinkingStatus、[session_manager.py](../../src/omt/core/session_manager.py) 的 rebuild，以及 [CLI](../../src/omt/cli/) 的命令与展示。
- 测试：[test_thinking.py](../../tests/cli/test_thinking.py)。
- 保证修改强度不只是更新显示：Model、Agent、Manager 必须重建，下一轮真正使用新模型，同时保留会话历史、工具和内存失败保护。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 显示与成功切换 | 未配置初始等级；先执行一轮，再输入首尾空白/大小写混合的 /THINKING high，目录 high→max，随后继续第二轮 | 初始实际值 none，切换后 max；列出 none/high/low，不列 null 能力；第二轮使用另一假模型，收到完整且不重复的上一轮历史；索引仍只有一条；原 settings.json 和成功缓存不变 | test_cli_switch_rebuilds_and_preserves_history |
| 从有效等级切到 none | 初始 high→max 并执行一轮，随后 /thinking none 再继续 | 再次初始化 SDK，既不发送 reasoning_effort 也不保留旧 extra_body.thinking；新模型实际执行下一轮 | test_switch_to_none_does_not_keep_previous_thinking_options |
| 只执行命令 | /thinking、参数过多、/thinking none 或 media/unsupported，最后退出 | 命令不进入模型、不登记会话；参数错误提示用法；不存在/null 映射按 none 并明确提示；同一请求等级不重复初始化 | test_thinking_commands_do_not_create_sessions_and_use_none |
| 重建失败 | SDK 初始化或 Agent 组装的第二次调用抛含测试敏感标记的异常 | 保留旧 Manager 与显示状态；后续普通消息仍由旧模型回答；提示失败但不显示 SDK 异常原文 | test_switch_failure_keeps_old_manager_and_status |
| 失败会话保护 | 先完成一轮，再模拟流创建失败，使磁盘仍保留完整终态，但 Manager 已记录失败；随后切换 | 新旧 Manager 身份不同、旧实例拒绝操作；新实例对该 ID 的历史读取/恢复/继续执行仍拒绝；不能靠重建绕过内存保护；退出后新实例也拒绝操作 | test_runtime_rebuild_preserves_failed_session_guard_and_closes_managers |
| MCP 工具复用 | 假 MCP 发现返回本地 marker 工具，切换后新模型调用它 | 工具真实执行一次并完成回合；发现只在启动执行，不为每次切换重新连接服务器 | test_switch_reuses_mcp_tools_without_rediscovery |
| 活跃运行保护 | 本地模型等待 Event 时申请切换，之后释放 Event 再切换 | 交接立即报 manager_busy，原运行没有被取消，显示仍为旧值；空闲后能再次成功切换并继续同一会话 | test_switch_does_not_cancel_active_run_and_can_retry_afterwards |
| 取消切换与并行切换 | Agent 组装等待 Event；另一次切换请求到来，再取消第一次切换任务 | 第二次立即拒绝；CancelledError 传播，原 Manager 与显示值保留且仍可执行，再次切换能成功；上下文退出后切换被拒绝，任务 finally 清理 | test_cancelled_switch_keeps_old_manager_and_can_close |
| 固定 Manager 兼容 | 嵌入方注入只提供 Manager 的工厂，没有模型配置文件 | /thinking 被消费并提示不支持，不读取配置、不调用模型、不创建索引；既有 manager_factory seam 保留 | test_fixed_manager_reports_unsupported_without_reading_config |
| 没有思考能力 | 目录 thinkingLevelMap 为 null 或空对象 | 启动和查询均只列出 none，不因缺失能力失败；查询不重新初始化模型 | test_startup_without_thinking_capability_shows_only_none |
| 禁止换检查点 | 给 Manager.rebuild 传入使用另一临时 SQLite Saver 的 Agent | 报错且旧 Manager 仍可用，不切换到不同历史来源 | test_manager_rebuild_rejects_different_checkpointer |

本组通过公开 run_cli、SessionRuntime 与 Manager Interface 验证行为。失败保护用例在 Conversation.arun 入口模拟尚未推进图的失败，特意保留完整磁盘检查点；否则只检查不完整检查点，无法发现丢失 `_tainted` 的回归。

## 隔离方式

沿用 [CLI fixture](../../tests/cli/conftest.py) 隔离 settings.root、cwd、数据库、Skills 和 MCP。每例三份 JSON 只含保留域名和假 Key，清除相关模型设置环境变量；SDK 初始化仅返回不同的 RecordingModel，不访问网络。SQLite、Agent 图和历史装载使用真实实现。

只有 MCP 发现、SDK/Agent 初始化失败及流创建失败用限定替身；MCP marker 是测试内本地工具，不启动服务。等待和取消均用 asyncio.Event、超时及 finally/gather 回收测试任务；所有文件只写 tmp_path。

## 执行

```bash
uv run pytest -q tests/cli/test_thinking.py --tb=no
uv run pytest -q tests/cli tests/core/test_bootstrap.py tests/core/test_settings.py tests/core/test_configuration_files.py --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/core/bootstrap.py src/omt/core/session_manager.py src/omt/cli/app.py src/omt/cli/commands.py src/omt/cli/terminal.py tests/cli/test_thinking.py
```

## 覆盖限制

- 不证明服务端接受某个 effort 或实际输出质量，不请求真实模型/MCP，也不测试真实终端外观。
- 本组旧/测试模型的 none 保留省略参数语义，不证明关闭服务端推理。已知 DeepSeek Flash/V4 Pro 的显式 none/disabled 和登录能力修复由[模型登录测试](model-login.md)及[模型适配测试](ai-models.md)验证；终端不再统一宣称 none 必定省略参数。
- 本组 /thinking 切换仅属于当前 CLI 进程，不持久化、不改变 HTTP 进程，也不是每个 session 的独立配置。
- /thinking 使用当前运行模型的目录快照，不自动重读外部编辑；/login 的持久化、显式切模型及新模型映射回归见[模型登录测试](model-login.md)，不受本组“不持久化”约束。
- 重建的互斥检查保护运行/历史/激活操作，不是跨进程锁；应用入口仍须顺序管理生命周期，不能把旧 Manager 引用继续分发给其他调用者。
