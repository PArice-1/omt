# CLI 入口、终端与生命周期测试说明

## 对象与目的

源码：[app.py](../../src/omt/cli/app.py)、[terminal.py](../../src/omt/cli/terminal.py)、[settings.py](../../src/omt/cli/settings.py)，共同使用 [SessionManager](../../src/omt/core/session_manager.py) 和 [bootstrap](../../src/omt/core/bootstrap.py)。

测试：[test_app.py](../../tests/cli/test_app.py)、[test_terminal.py](../../tests/cli/test_terminal.py)、[test_lifecycle.py](../../tests/cli/test_lifecycle.py)。命令选择与恢复另见 [/resume 测试](resume.md)；推理强度展示/切换、重建与失败保护见[推理强度测试](thinking.md)；底层模型解析另见[配置与组装测试](session-and-config.md)。

防止迁出旧 session/runtime 模块后出现空索引、CLI 绕过 Manager、重复打印、历史丢失，以及取消导致工具/SQLite 资源泄漏。旧 test_session.py 被按职责拆分，不再模拟已删除的 Runtime 工厂或 CLI 私有函数；尽量改用真实 Manager、图和检查点验证结果。

## 入口与配置场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 不创建空记录 | /exit、/quit、大写与首尾空白、EOF、输入 KeyboardInterrupt | 输出结束；没有索引，不发送退出文本给模型 | test_cli_exit_does_not_create_empty_session |
| 多轮执行与展示 | 两轮普通输入再 EOF，真实 Manager + SQLite + 假模型 | 索引仅一条、目录正确、标题为首条前 10 字符；每个回答输出一次、系统规则不打印，重新打开 Saver 能读回两轮原文与回答 | test_cli_persists_multiple_turns_and_prints_each_answer_once |
| 登记失败重试 | 首次 insert_session 抛 SQLite 错误，后续恢复真实写入 | 首次提示本轮未执行，失败输入不进入检查点；第二次输入登记一条并执行成功 | test_cli_retries_registration_without_executing_failed_input |
| 前缀独立 | 设置 OMT_API_ 的不同数据库和假令牌，同时设置 OMT_CLI_ 数据库 | CliSettings 只使用 CLI 路径，不含 access_token | test_cli_settings_do_not_inherit_http_token_or_prefix |
| 兼容旧 MCP | 未配置 CLI MCP，再显式设为 {} | 未配置时仍提供旧 knowledge stdio；{} 关闭，配置解析本身不连接服务器 | test_cli_preserves_legacy_mcp_default_but_can_disable_it |
| 临时 Skills 隔离 | 共享 fixture 生效后构造 CliSettings | skill_roots 精确等于 tmp_path/skills，不落回真实用户目录；只解析设置，不扫描文件 | test_cli_fixture_uses_only_temporary_skill_roots |
| Skills 配置选择 | default 删除 CLI 覆盖；custom 设置相对目录；disabled 设置 []；另设置 API 的无关目录 | 分别得到 ~/.agents/skills、当前临时目录下的 custom-skills、空列表；API 前缀不影响 CLI，默认目录只解析不扫描 | test_cli_skill_roots_use_user_default_or_explicit_configuration |
| 完整默认组装 | 临时模型 JSON（显式选择 high 并映射到 high）、预建库、文件与 Skill；参数 skills_enabled 分别开启临时目录和显式关闭；假模型真实调用 read 后继续第二轮 | 经过 bootstrap/Manager/Conversation 与真实同步 read；SDK 替身收到 reasoning_effort=high 及 enabled thinking；启用时系统消息包含临时 Skill 元数据但无正文，关闭时不含 Skills 目录；回答/工具结果可见、假 Key 不显示；重开数据库后系统消息唯一、两轮与工具结果无重复 | test_cli_runs_manager_and_read_tool_without_network |
| 提示词不可用 | 包资源加载替身抛 FileNotFoundError/ValueError | 原异常传播，模型初始化尚未发生，不静默退化 | test_cli_stops_before_model_when_prompt_loading_fails |
| 无库或无索引表 | 缺文件/父目录，或已有库删除 sessions 表 | 启动失败，未初始化模型、不创建目录或索引表 | test_cli_invalid_database_fails_before_model_and_does_not_create_schema |

CLI/API 共用 Manager 的补充集成场景在 [test_api.py](../../tests/api/test_api.py) 的 test_cli_and_http_share_one_manager_without_leaking_terminal_output：HTTP 创建空会话，CLI 恢复并执行，HTTP 再追加；检查同一 ID 的完整历史，并验证 CLI 渲染不泄漏到 HTTP 轮次。此测试在同一事件循环借用同一个 Manager，不声称独立进程共享互斥。

## 终端场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试组 |
| --- | --- | --- | --- |
| 同步/异步读取 | 内存 pipe 输入中文和首尾空白；传入或由函数新建 PromptSession | 返回原文，不调用内置 input；两条路径行为一致 | test_get_user_input_uses_real_prompt_session_without_builtin_input、test_get_user_input_can_create_its_own_prompt_session |
| 无效文档 | 空串、全空白、NUL | 提交时 ValidationError；不改变输入约束以便绕过 Manager | test_input_validator_rejects_blank_or_nul_input |
| 合法文档 | Unicode、含首尾空白的多行文本 | 校验通过，原文保留 | test_input_validator_accepts_unicode_and_multiline |
| 空行重试 | 先空行再有效行 | 空行被拒绝，随后能返回有效输入 | test_prompt_rejects_blank_line_then_accepts_next_input |
| Ctrl-C / Ctrl-D | 内存终端发送控制字符 | 输入函数传播 KeyboardInterrupt / EOFError，入口决定退出 | test_prompt_propagates_cancel_and_eof |

## 失败、取消与信号

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 工具异常后再输入 | 本地异步工具抛错，随后再次提交同一会话 | 工具只执行一次，后续由 Manager 提示上次运行未完成；不沿用旧 CLI 盲目重试策略 | test_cli_failure_blocks_reexecution_and_external_cancel_cleans_up（cancel=False） |
| 外部取消 | 直接 await run_cli，等待中的异步工具被外层取消 | CancelledError 向外传播，不伪装正常退出；工具 finally 执行、不读下一条输入，操作/消费任务被清理 | 同上（cancel=True） |
| 执行时 SIGINT | 测试子进程的工具等待时向自身发送真实 SIGINT | CLI 正常退出、无堆栈、不再读输入；工具与 Manager 清理、索引保留，进程及时结束 | test_cli_sigint_exits_and_closes_manager |

旧“普通失败后直接重试”用例改为阻止再执行，原因是 CLI 与 HTTP 统一使用 Manager 的失败保护。缺表从输入阶段提示改为共用 bootstrap 启动阶段失败；无有效依赖时不会进入循环。原同步输入与真实 SIGINT 回归继续保留。内存历史取消不提交的低层断言仍由 [Conversation 测试](conversation.md) 覆盖，CLI 侧只检查入口可观察行为和资源清理。

## 隔离方式

- [conftest.py](../../tests/cli/conftest.py) 隔离 cwd、settings.root 和 OMT_CLI_/OMT_API_ 环境，前后清空设置缓存。测试默认用 OMT_CLI_MCP_SERVERS={} 禁止旧本机服务器；仅默认配置解析用例删除变量，但不启动连接。
- 默认 Skills 搜索通过 OMT_CLI_SKILL_ROOTS 显式覆盖为 tmp_path/skills。CliSettings 的 ~/.agents/skills 默认值在导入时展开，仅改变 HOME、cwd 或 settings.root 不能隔离，因此不再依赖旧的项目 Skills 自动发现行为。配置默认值专项只解析路径、不启动会话；完整链路分别验证临时目录启用和 [] 禁用，不放宽原有 Skill 元数据、正文隔离和历史断言。
- 数据库、文件、Skill、三份模型配置和假 Key 均在 tmp_path。所有数据库由测试显式建表，图和 AsyncSqliteSaver 真实运行，不读取用户数据。
- 默认 PromptSession 用替身避免占用用户终端；输入专项使用真实 PromptSession + pipe + DummyOutput，不需要 TTY。
- 普通入口测试注入只装本地假模型的固定 Manager 工厂；完整链路仅替换 SDK 初始化，保留默认 bootstrap.open_session_runtime、当前 Manager 和内置 read，不访问模型/MCP 网络。
- 等待测试使用 Event、超时及 finally 清理；SIGINT 仅作用于测试创建的子进程，设定临时 cwd、继承隔离后的假配置变量、关闭 stdin 并有超时回收。子进程注入本地工厂，不读取真实认证。

## 执行

```bash
uv run pytest -q tests/cli --tb=no
uv run pytest -q tests/cli tests/api tests/core/test_bootstrap.py tests/core/test_conversation.py tests/core/test_conversation_history.py --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/cli tests/cli
```

## 覆盖限制

- 内存终端不证明所有真实 TTY、终端宽度或多媒体渲染正确；SIGINT 依赖当前平台的 Python/asyncio 信号行为。
- 不请求真实模型/MCP、不验证用户现有数据库或本机 knowledge 安装。
- 异步工具清理不证明任意同步线程或外部进程可立即停止，也不证明文件与检查点回滚。
- 不覆盖多进程同时写同一会话、自动恢复上次会话、图中断恢复或工具沙箱。
