# CLI Session 与配置测试说明

## 对象与目的

源码：

- [session.py](../../src/omt/coding_agent/session.py)：选择模型、组装 Agent、校验输入和运行循环。
- [settings.py](../../src/omt/coding_agent/settings.py)：读取与校验三类配置。
- [prompts/__init__.py](../../src/omt/coding_agent/prompts/__init__.py)：加载包内基础系统提示词；资源异常场景见 [提示词测试说明](prompts.md)。

测试：

- [test_session.py](../../tests/coding_agent/test_session.py)
- [test_settings.py](../../tests/coding_agent/test_settings.py)
- [test_configuration_files.py](../../tests/coding_agent/test_configuration_files.py)

要防止的错误包括：选错模型或 URL、把 OAuth 当 Key、配置错误泄露凭据、测试误读用户文件、无效输入进入模型、回答被重复显示。

## 配置读取场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| Settings 字段 | 合法 provider/model，或缺字段、错误类型、空白值 | 合法值可加载，缺失/非法选择被拒绝 | test_settings.py 的 accepts/reject 用例；test_configuration_files.py：test_settings_reject_blank_selection |
| 文件与缓存 | 临时 settings.json、文件缺失、错误 JSON、修复后重试、成功后修改文件 | 错误传播；成功结果缓存，cache_clear 后读取新值；失败不固化为缓存结果 | test_settings.py 的 load/missing/invalid/cached/retried 用例 |
| API Key 类型 | auth 中提供假 Key | 加载为 SecretStr，可明确取测试值，但 repr 不暴露原值 | test_auth_loads_api_key_as_masked_secret |
| 非 API Key 认证 | OAuth 类型附带 access/refresh | 加载类型，不误用为 key；额外 token 不显示在对象 repr | test_auth_retains_type_without_treating_oauth_as_api_key |
| 非法认证结构 | 尾随逗号、缺 type、错误结构，携带测试敏感标记 | 带文件提示的错误；完整格式化异常也不含该标记 | test_invalid_auth_errors_do_not_expose_credentials |
| 认证重读 | 假 Key 改变，或 auth 文件不存在 | 下次读取新值；缺文件抛异常 | test_auth_is_reread_after_credentials_change、test_missing_auth_file_raises |
| 目录字段 | 模型带 URL、provider 及能力/价格额外字段 | 连接字段可用，额外元数据不妨碍加载 | test_model_store_parses_model_url_and_ignores_catalog_metadata |
| 非法目录 | 错误 JSON、错误顶层、缺 URL、非 HTTP(S) URL、空 ID、重复 ID、缺文件 | 拒绝目录或报文件缺失，不选择一个凑合使用 | test_invalid_model_store_is_rejected、test_missing_model_store_file_raises |

## 组装与交互场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 三份配置拼接 | 假默认选择、认证与目录匹配 | provider/model/精确 URL/SecretStr 传至 SDK 替身，返回可用模型 | test_get_model_joins_settings_auth_and_matching_catalog_entry |
| 认证不可用 | provider 无认证、Key 缺失/空白、OAuth/bearer/未知类型 | 初始化前报错，SDK 替身未调用；错误不含测试 token | test_get_model_rejects_missing_or_blank_auth、test_get_model_explicitly_rejects_unsupported_auth_types |
| 提供商与目录不匹配 | provider 未注册、目录缺 provider/model、模型 provider 不符 | 明确拒绝，不回退到其他模型 | test_get_model_rejects_unregistered_provider_before_initializing_model、test_get_model_rejects_missing_or_mismatched_catalog |
| 精确 URL 选择 | 所选模型不是 models 列表第一项 | 只使用所选 ID 的 URL | test_get_model_uses_url_of_exact_selected_model_not_first_entry |
| Agent 组装 | Model 包装对象与内置工具，MCP 发现返回空列表的异步替身 | 等待异步 _get_agent，传入 client.model，工具集合包含全部静态注册项 | test_get_agent_uses_underlying_chat_model_and_builtin_tools |
| 真实终端读取 | 同步/异步输入路径，内存 pipe 输入中文和首尾空白 | 返回原文本，不调用内置 input()，可使用传入或自行创建的 PromptSession | test_get_user_input_uses_real_prompt_session_without_builtin_input、test_get_user_input_can_create_its_own_prompt_session |
| 输入验证 | 空串、全空白、NUL，或合法 Unicode/多行文档 | 非法提交被拒绝，合法文档通过；空行后可继续输入有效文本 | input_validator 用例、test_prompt_rejects_blank_line_then_accepts_next_input |
| 控制信号 | 内存终端发送 Ctrl-C / Ctrl-D | 输入函数抛 KeyboardInterrupt / EOFError，由循环处理 | test_prompt_propagates_cancel_and_eof |
| 退出命令 | /exit、/quit、带空白的大写形式 | 结束循环，不交给模型，也不写空会话索引 | test_session_exits_without_sending_exit_command_to_model |
| 复用和重试 | 有效输入后 EOF；普通执行失败后再次输入 | 复用一个 PromptSession，普通失败可重试，不再次打印 runtime 返回文本；Ctrl-C 不属于重试路径 | test_session_handles_eof_and_does_not_print_answer_twice、test_session_allows_retry_after_failed_turn |
| 完整离线链路与提示词 | 临时预建 SQLite、文件、配置、Skill；假模型真实调用 read 并继续第二轮，结束后重新打开 Saver 查询 | 工具过程和总结正常显示，假 Key 不回显；SystemMessage 唯一、目录不含正文；按索引 ID 能读到完整且不重复的两轮检查点消息 | test_session_runs_real_runtime_and_read_tool_without_network |
| 提示词启动失败 | 提示词加载替身抛 FileNotFoundError 或 ValueError | 原异常传播，模型初始化和会话创建均未发生，不静默退化为只有 Skills 的会话 | test_session_stops_before_model_initialization_when_prompt_loading_fails |

## 会话配置与索引写入

CLI 负责创建 UUID、RunnableConfig 并注入 Runtime，同时将同一个 ID 交给 SessionStore；不通过 Runtime 的 getter 反向组装存储配置。工作目录在启动时捕获，与数据库文件位置分开。索引在首条普通请求执行之前插入（不含 /resume 命令），写入成功才更新登记标记；记录存在不等于该回合完成或检查点已回滚。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 配置、路径和一次登记 | 两条请求，分别让首轮成功或抛异常 | UUID v4 与注入 Runtime 的 ID 相同；记录的是启动 cwd，不是数据库路径；标题为第一条请求前 10 字符；每次 arun 开始前已可查索引，且全程仅一行 | test_session_registers_once_before_running_with_the_same_thread_and_workspace |
| 写入失败后重试 | 首次插入抛 SQLite OperationalError，第二次调用真实插入 | 第一次不执行 arun；提示存储失败且本轮未执行；保留同一 ID，第二次可写入并执行，不因提前修改标记而漏存 | test_session_retries_registration_after_storage_failure_without_running_agent |
| 数据库缺失 | 数据库父目录和文件均不存在 | 启动抛明确 FileNotFoundError，不创建目录或空库，不初始化模型 | test_session_missing_database_fails_without_creating_it_or_initializing_model |
| sessions 表缺失 | 已有临时数据库，但移除 sessions 表 | 单独提示索引存储失败，不执行 Agent，不自动建表 | test_session_missing_table_is_reported_without_running_agent_or_creating_table |

## 异步 CLI 补充场景

CLI 的 session() 仍为同步脚本入口，内部使用一次 asyncio.run(asession())；原循环测试改为等待异步输入和 arun，原有同步输入函数测试保留并参数化覆盖异步版本。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 同一事件循环 | 两轮有效输入及退出；记录异步输入和回合的运行循环 | 输入与回合复用同一循环，等待 arun，不调用同步 run | test_session_reuses_one_event_loop_for_input_and_turns |
| 输入时 Ctrl-C | 异步输入抛 KeyboardInterrupt，其后仍预置其他输入 | CLI 正常结束，不读取下一条输入、不调用模型，不再显示取消后继续的提示 | test_session_ctrl_c_during_input_exits_without_starting_a_turn |
| 执行时 SIGINT | 子进程使用临时预建库与真实 SQLite Saver，本地异步工具等待时向自身发送真实 SIGINT | CLI 结束，不读取下一轮；工具 finally 执行，取消回合不提交 Runtime 内存历史，无堆栈；同一 ID 和 cwd 的索引保留，不承诺数据库检查点回滚 | test_session_sigint_during_execution_exits_and_cleans_up |
| 外部任务取消 | 直接调用 asession，等待中的回合被外层取消 | CancelledError 继续向上传播，回合 finally 执行，不误报为用户退出，也不开始下一轮 | test_asession_propagates_external_cancellation_and_cleans_up |

交互契约按用户要求调整为 Ctrl-C 直接退出。原“取消本轮后继续”用例改为退出回归测试；单回合自定义 SIGINT 包装已删除，对应的处理器恢复测试不再适用，由真实子进程 SIGINT 场景验证默认 asyncio.run 取消与清理链路。

真实信号仅发送给测试自己创建的子进程，不影响 pytest 或用户终端。子进程以临时目录为 cwd，显式指向父测试预建的临时数据库，注入本地假模型、异步 Agent 工厂和接收外部 config 的 Runtime 工厂，显式关闭 Skills；实际使用 SQLite Saver，不读取真实配置或请求模型/MCP 服务。subprocess.run 设置超时并负责超时后的进程回收。其他等待型测试使用本地 asyncio.Event 并设置超时。完整离线链路继续通过真实异步 Runtime 调用原有同步 read 工具，验证同步工具可在异步 Agent 中工作。

## 隔离方式

[共享 fixture](../../tests/coding_agent/conftest.py) 自动将 `settings.root` 改为每例的 `tmp_path`，将 cwd 切换到 `tmp_path`，并前后清理 load_settings 缓存。各文件的局部 fixture 在其下创建 `agent/` 假配置。两条路径同时隔离，也避免 Runtime 默认发现真实用户/项目 Skills。

这点是必要条件：生产配置当前位于 `~/.omt/agent/`，只改变 cwd 无法隔离它。更改配置来源或新增缓存时，同步检查共享 fixture，不能让测试失败断言接触真实 Key。

本文件的自动 session_db fixture 另行预建 tmp_path/cli.sqlite 的 sessions 表，替换 session.py 在导入时固定的 db_path，并将 MCP get_tools 替换为返回空列表的 AsyncMock。不能仅修改 settings.root 后假定导入时已计算的数据库路径也会变化。元数据使用真实 SessionStore 和 SQLite，不把成功 mock 写入当成持久化证据。

组装测试保留 Model 到 DeepSeek 适配函数的真实调用链，仅替换 SDK 初始化，返回本地假模型；终端使用 create_pipe_input 和 DummyOutput，不占用用户终端。循环控制的单元场景使用输入和 runner 替身，另有真实 Runtime/读文件链路弥补单纯 mock 的不足。集成假模型记录每次收到的消息，验证内置提示词在工具前后和后续轮次均存在且不重复；只读取包内可信提示词和临时 Skill。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_settings.py tests/coding_agent/test_configuration_files.py tests/coding_agent/test_session.py --tb=no
```

## 覆盖限制

- 不读取用户实际配置，不验证真实 Key 有效性或真实服务 URL 可访问。
- 不提供或验证 OAuth 登录/刷新；测试的是明确拒绝不支持的类型。
- 文档验证器接受换行文档，不等于已覆盖所有平台的终端多行编辑和粘贴行为。
- 真实 SIGINT 验证覆盖当前环境中的异步工具清理，不证明任意远程请求、同步工具线程或外部进程能被立即取消，也没有模拟所有操作系统上的真实终端 SIGINT 传递；同步工具可能延长事件循环关闭时间。
- 本页不验证 /resume 选择与磁盘历史恢复；相关离线交互链路见 [CLI /resume 测试说明](resume.md)。不验证动态切换模型或热重载配置。
