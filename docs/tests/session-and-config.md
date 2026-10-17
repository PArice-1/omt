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
| Agent 组装 | Model 包装对象与内置工具 | 传入 client.model，工具集合包含全部静态注册项 | test_get_agent_uses_underlying_chat_model_and_builtin_tools |
| 真实终端读取 | 同步/异步输入路径，内存 pipe 输入中文和首尾空白 | 返回原文本，不调用内置 input()，可使用传入或自行创建的 PromptSession | test_get_user_input_uses_real_prompt_session_without_builtin_input、test_get_user_input_can_create_its_own_prompt_session |
| 输入验证 | 空串、全空白、NUL，或合法 Unicode/多行文档 | 非法提交被拒绝，合法文档通过；空行后可继续输入有效文本 | input_validator 用例、test_prompt_rejects_blank_line_then_accepts_next_input |
| 控制信号 | 内存终端发送 Ctrl-C / Ctrl-D | 输入函数抛 KeyboardInterrupt / EOFError，由循环处理 | test_prompt_propagates_cancel_and_eof |
| 退出命令 | /exit、/quit、带空白的大写形式 | 结束循环，不交给模型 | test_session_exits_without_sending_exit_command_to_model |
| 复用和重试 | 取消输入、有效输入、EOF；执行失败或执行期 KeyboardInterrupt 后再输入 | 复用一个 PromptSession，循环可继续，不再次打印 runtime 返回文本 | handles_input_cancel、allows_retry、continue_after_keyboard_interrupt 用例 |
| 完整离线链路与提示词 | 临时文件、配置、Skill；记录输入的假模型真实调用 read 工具并继续第二轮 | 工具过程和总结正常显示，假 Key 不回显；每次模型输入恰有一条 SystemMessage，基础提示词在 Skills 目录之前，目录含路径但不含 Skill 正文 | test_session_runs_real_runtime_and_read_tool_without_network |
| 提示词启动失败 | 提示词加载替身抛 FileNotFoundError 或 ValueError | 原异常传播，模型初始化和会话创建均未发生，不静默退化为只有 Skills 的会话 | test_session_stops_before_model_initialization_when_prompt_loading_fails |

## 异步 CLI 补充场景

CLI 的 session() 仍为同步脚本入口，内部使用一次 asyncio.run(asession())；原循环测试改为等待异步输入和 arun，原有同步输入函数测试保留并参数化覆盖异步版本。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 同一事件循环 | 两轮有效输入及退出；记录异步输入和回合的运行循环 | 输入与回合复用同一循环，等待 arun，不调用同步 run | test_session_reuses_one_event_loop_for_input_and_turns |
| SIGINT 回合取消 | 循环中第一轮触发已安装的 SIGINT 回调，第二轮正常 | 等待取消完成后继续下一轮，不退出整个会话 | test_session_can_continue_after_keyboard_interrupt_during_run |
| 信号处理器恢复 | 回合成功、抛异常、触发 SIGINT 回调 | 成功/异常语义保留，取消时执行清理；每种路径均恢复之前的处理器 | test_async_turn_restores_sigint_handler |
| 外部任务取消 | 在本地等待中的回合上取消外层任务 | CancelledError 继续向上传播，子任务清理、信号处理器恢复，不误报为用户 Ctrl-C | test_async_turn_propagates_external_cancellation_and_cleans_up |

信号场景仅调用本回合安装的 Python 回调，不向用户进程或终端发送真实系统信号。等待型测试使用本地 asyncio.Event 并设置超时，不执行外部服务。完整离线链路继续通过真实异步 Runtime 调用原有同步 read 工具，验证同步工具可在异步 Agent 中工作。

## 隔离方式

[共享 fixture](../../tests/coding_agent/conftest.py) 自动将 `settings.root` 改为每例的 `tmp_path`，将 cwd 切换到 `tmp_path`，并前后清理 load_settings 缓存。各文件的局部 fixture 在其下创建 `agent/` 假配置。两条路径同时隔离，也避免 Runtime 默认发现真实用户/项目 Skills。

这点是必要条件：生产配置当前位于 `~/.omt/agent/`，只改变 cwd 无法隔离它。更改配置来源或新增缓存时，同步检查共享 fixture，不能让测试失败断言接触真实 Key。

组装测试保留 Model 到 DeepSeek 适配函数的真实调用链，仅替换 SDK 初始化，返回本地假模型；终端使用 create_pipe_input 和 DummyOutput，不占用用户终端。循环控制的单元场景使用输入和 runner 替身，另有真实 Runtime/读文件链路弥补单纯 mock 的不足。集成假模型记录每次收到的消息，验证内置提示词在工具前后和后续轮次均存在且不重复；只读取包内可信提示词和临时 Skill。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_settings.py tests/coding_agent/test_configuration_files.py tests/coding_agent/test_session.py --tb=no
```

## 覆盖限制

- 不读取用户实际配置，不验证真实 Key 有效性或真实服务 URL 可访问。
- 不提供或验证 OAuth 登录/刷新；测试的是明确拒绝不支持的类型。
- 文档验证器接受换行文档，不等于已覆盖所有平台的终端多行编辑和粘贴行为。
- 循环的取消测试不证明任意远程请求、同步工具线程或外部进程能被立即取消，也没有模拟所有操作系统上的真实终端 SIGINT 传递。
- 不是磁盘会话恢复、动态切换模型或热重载配置的产品测试。
