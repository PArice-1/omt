# Textual TUI 交互与生命周期测试说明

## 对象与目的

- 源码：[tui.py](../../src/omt/cli/terminal/tui.py)、[screens.py](../../src/omt/cli/terminal/screens.py)、[widgets.py](../../src/omt/cli/terminal/widgets.py)、[completion.py](../../src/omt/cli/terminal/completion.py)、[tui.tcss](../../src/omt/cli/terminal/tui.tcss)、[app.py](../../src/omt/cli/app.py)。
- 测试：[test_tui.py](../../tests/cli/test_tui.py)、[test_tui_input.py](../../tests/cli/test_tui_input.py)。
- 目标：默认入口升级后，真实界面仍保持延迟会话创建、core 配置分层、凭据隐藏、历史兼容和失败保护；防止只验证控件被调用，却没有验证用户能发送、选择、取消或退出。

逐行模式的原有生命周期/编号回归继续独立保留，见 [CLI 测试](cli.md)、[/resume 测试](resume.md)和[模型登录测试](model-login.md)。

## 场景与可观察预期

| 测什么、为什么 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 冷启动与帮助，避免向导、空数据和弹窗串扰 | 配置根不存在；F2，再按其他工具快捷键，Esc；输入 `/help`；Ctrl-Q | 停留登录弹窗，不叠层/清除背景消息；取消返回，帮助正常展示；不创建配置或数据库 | test_missing_configuration_keyboard_help_and_cancel_leave_no_files |
| 入口兼容，避免改变全部旧协议 | 主入口无参数，或显式 `--plain`；替换界面启动点 | 默认只启动 TUI；plain 只 await 原逐行入口 | test_script_entry_selects_tui_by_default_or_explicit_plain |
| 命令边界，避免误发送或误退出 | 已连接本地假模型；输入未知斜杠命令、带参数的退出/登录或多余推理参数 | 保持运行和输入内容，安全提示，无用户消息、索引或模型请求；登录行内参数不提交 | test_invalid_commands_do_not_run_model_or_exit |
| 完整登录、推理与模型切换，避免只测表单 | 本地假目录含 Flash/Pro；真实 core 保存、假 SDK；键盘选厂商/模型、F4，再 F2 | 首次密码输入；自动保存完整目录、创建缺失库但不登记会话；状态变更、SDK 接收正确 effort；再次选厂商不再输入 Key；截图没有假 Key | test_login_thinking_and_model_switch_use_core_without_exposing_key |
| 查询中的取消，避免后台落盘 | 假目录协程阻塞，输入假 Key 后获取，Esc | 目录协程 finally 执行，退出弹窗，配置根不存在，截图无 Key | test_cancel_directory_fetch_cleans_worker_without_saving |
| 真实发送、流和恢复，防止 Enter 被编辑器截获或重复回答 | 真实 Manager/Agent/Saver；FakeMessagesListChatModel 调用本地异步工具后回答；Ctrl-J 多行、Enter 发送；新对话再 F3 恢复 | 用户换行原文保留；Markdown 回答仅一次、工具默认折叠；SystemMessage 不显示；一条真实索引，恢复原 ID、历史及工具参数/结果，不重放工具；退出关闭工厂 | test_stream_tool_markdown_multiline_and_history_restore |
| 工具屏幕缓冲边界，避免大参数绕过上限 | 向真实界面依次投递超过 20000 字符的工具参数、流增量和完成事件 | 参数和输出都仅保留末尾，缓冲不超过 16000 字符；完成状态正确，默认折叠，不创建配置 | test_tool_screen_buffer_limits_parameters_and_stream_output |
| 历史工具参数/结果配对，避免同名并发顺序混淆 | 一条 AI 消息包含两个同名调用、不同参数，ToolMessage 逆序返回且无 name | 恢复两个折叠项，工具名取自调用，按保存的 call ID 各自关联参数和正确结果，不写配置 | test_history_pairs_parallel_tool_results_by_saved_call_id |
| 执行取消/退出，避免只关画面而泄漏任务 | 本地等待工具，运行中输入 `/ne` 并尝试执行，分别 Esc 和 Ctrl-Q | 活动指示可见；忙时指令保留草稿且不打断工具；首个 Esc 只收起提示，再次 Esc 停止；工具 finally 及工厂关闭，同会话不能重跑，失败保护仍在 | test_cancel_or_quit_running_tool_waits_for_cleanup_and_keeps_failure_guard |
| 登录失败的安全提示，防止异常原文泄漏凭据 | 目录替身抛出含假 Key 的异常 | 显示安全失败/未保存提示；密码缓冲清空，模型选择禁用，截图不含 Key，无配置落盘 | test_login_failure_keeps_key_hidden_and_does_not_save |
| 小尺寸可返回，避免控件被裁掉无法操作 | Pilot 从 90×32 缩到 52×20，打开登录并聚焦取消按钮 | 取消按钮在可见范围，Enter 可取消，无配置落盘；不再要求简化后的表单必然发生滚动 | test_small_terminal_login_is_scrollable_and_cancellable |
| 弹窗中全局退出，避免只能先取消才能离开 | 登录弹窗打开，分别 Ctrl-Q / Ctrl-C | App 停止，没有配置文件；不会当作聊天输入 | test_close_from_login_dialog_leaves_no_files |
| 读历史不被打断，避免输出强制抢滚动 | 长消息撑满聊天区，手动回到顶部，再追加输出 | 保持顶部，不自动跳回底部；纯显示无配置写入 | test_new_output_does_not_interrupt_scrollback |

## 终端式输入与布局

第一版大按钮/消息边框改为内容优先布局，测试核对可用阅读空间、消息对齐和真实键盘交互，不用检查样式字符串代替可观察结果。

| 测什么、为什么 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 命令发现、筛选、补全和取消，避免强迫记忆 | 输入 `/`，↓ 选 `/model`，Tab，Esc，再 Enter | 显示说明列表且编辑器保留焦点；Tab 只填文字，Esc 不丢草稿/同步收起状态提示；Enter 打开模型面板，无配置落盘 | test_slash_hints_navigate_complete_without_executing_and_escape_keeps_draft |
| 前缀执行和别名 | `/lo`、`/mo`、大写 `/LO` 后 Enter | 执行所选完整命令而非把前缀当用户消息；`/model` 与 `/login` 共用流程；面板初始不堆叠密码/模型空字段 | test_enter_executes_filtered_command_not_partial_prompt |
| 不擅自改写内容 | 普通文字、未知斜杠命令、行内 Key、额外推理参数、多行粘贴 | 不提供错误补全或截取第一行，输入原文不变、不落盘 | test_no_suggestions_for_unknown_arguments_or_multiline_paste |
| 快速输入的候选过期保护 | 菜单仍为 `/exit` 或 `/login`，同步替换为带非法参数的新文本，在 Changed 派发前立即提交 | 不执行上一帧指令、不退出/打开面板，保留新文本，无索引/配置写入 | test_typeahead_does_not_execute_stale_command |
| 真正按运行能力补全 | 假能力仅 none/turbo；输入 `/thi`、`/thinking `、`/thinking T` 及不支持等级/多余参数；另在真实 Runtime 输入 `/thinking h` + Tab/Enter | 只提示传入能力，不硬编码 high；Tab/Enter 后真实 Runtime/SDK 的 effort=high，命令不登记会话 | test_thinking_hints_only_use_runtime_capabilities、test_login_thinking_and_model_switch_use_core_without_exposing_key |
| 鼠标选择 | `/he` 后点击候选行 | 展示帮助，清空指令输入，不新增聊天/索引，不要求先把焦点移进菜单 | test_clicking_suggestion_executes_help_without_creating_conversation |
| 伸缩、软换行、缩放及小屏菜单 | 80×24 输入长单行/20 行，缩到 44×24 再到 44×16；打开全部命令并导航末项 | 初始输入一行、聊天区至少 18 行；软换行后增高、最多六行；缩放后重新测量，菜单/输入/状态不越界；可滚到最后命令 | test_input_autogrows_and_command_menu_fits_small_terminal |
| 消息正文对齐回归 | 真实用户与 Markdown 助手消息，80 列 | 标记只占两列，正文紧随其后、至少 70 列；防止 Label 同属 Static 导致 CSS 把标记也扩成半屏 | test_chat_markers_leave_space_for_message_body |
| 普通 Tab 行为 | 普通文字且无菜单，已有可聚焦的折叠工具 | Tab 正常切焦点，不吞按键或改写输入 | test_tab_keeps_normal_focus_navigation_without_completion_menu |
| 真正发送后的回看和草稿 | 假模型完成两轮，输入草稿再 ↑↑↓↓；多行中移动及 Shift-↑ | 精确回看两条已发送输入并恢复草稿；多行内部方向键和选择照常，不产生额外消息/会话 | test_sent_input_history_preserves_draft_and_multiline_editing |
| 历史容量与敏感命令排除 | 公开 remember 依次接纳 60 条普通输入，再传入登录行内参数，反复上下回看 | 只保留末尾 50 条，越界稳定、恢复草稿，不记录登录指令，截图无假 Key，无配置写入 | test_input_history_is_bounded_ignores_commands_and_preserves_draft |

## 隔离与同步方式

- 沿用 [CLI conftest](../../tests/cli/conftest.py)，每例隔离 cwd、`settings.root`、环境配置、Skills 和 MCP，并前后清理设置缓存。不能仅改变 cwd 或依赖 headless 就认为不会读用户配置。
- `App.run_test()` / Pilot 使用真实 Textual 布局、控件、键盘路由与鼠标事件，不打开用户 TTY。默认入口路由测试例外，替换启动点避免真正进入交互。
- 真实 core 配置保存和 SQLite/检查点仅落在 `tmp_path`。目录查询、聊天 SDK 均为本地替身，假 Key 不具有认证能力；不访问模型/MCP 网络，不读写真实 `~/.omt`。
- `factory_for` 构建真实 SessionManager、Agent 与 AsyncSqliteSaver；仅模型/测试工具是假的。事件确认工具开始、取消及资源关闭；条件等待有超时，不用任意 sleep 推测业务已完成。
- 密码安全同时检查控件 password 状态、清空后的 value、界面截图与磁盘结果；不是只断言某个隐藏函数被调用。截图是在内存中导出的 SVG，不含用户数据，不保存认证文件原文。

## 执行

```bash
uv run pytest -q tests/cli/test_tui.py tests/cli/test_tui_input.py --tb=short
uv run pytest -q tests/cli --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/cli/app.py src/omt/cli/terminal tests/cli/test_tui.py tests/cli/test_tui_input.py
```

## 覆盖限制

- 文档中的界面图是脱敏的离线演示，来自实际 Textual 渲染，但不作为固定像素快照断言，也不代表模型实际完成了示例任务。
- Pilot 验证组件交互与布局约束，不代替全部真实终端、中文字体、剪贴板、屏幕阅读器与跨平台验证；没有固定像素快照断言。
- 不连接真实厂商、不证明远程 Key 有效或目录模型支持全部工具/推理协议，不验证实际计费请求。
- 取消测试使用可协作的异步工具，不证明任意同步线程/外部进程立即终止或副作用回滚；不改变 Manager 已有失败保护契约。
- 实时 RunEvent 尚无工具调用 ID，实时同名并发工具按名称/开始顺序归组，不能作为严格的逐调用审计；持久化历史的参数/结果有 ID，恢复时精确配对。本次不扩大 core 事件接口。
- 屏幕工具输出最多保留末尾 16000 字符，不等于裁剪 SQLite；本文未做超大历史或持续高频流的性能压测。
