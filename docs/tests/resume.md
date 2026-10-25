# CLI /resume 会话选择与恢复测试说明

## 对象与目的

- 源码：[cli/commands.py](../../src/omt/cli/commands.py)、[cli/app.py](../../src/omt/cli/app.py)，通过 [SessionManager](../../src/omt/core/session_manager.py) 查询和激活会话。
- 测试：[test_resume.py](../../tests/cli/test_resume.py)，随 CLI 拆分迁入该目录。
- 防止命令被当作用户消息、编号与 ID 错配、跨目录恢复、失败切换污染当前选择，以及重复登记旧会话。

## 交互契约与变更

本页保留 `--plain` 的编号和裸词别名协议；测试入口包装显式调用 `main(["--plain"])`，不是放宽或删除原有断言。TUI 改用稳定会话 ID 的选择弹窗，交互证据见 [TUI 测试](tui.md)。

handle_command 返回 run / handled / exit，并只在 Manager.open_session 成功后更新 CliState.current；run_cli 驱动输入、延迟登记和 Manager.run，不直接访问 Store 或 Conversation。

- resume 为 /resume 别名，支持首尾空白与混合大小写；独立词 resume 是保留命令词，无效参数也不会执行模型。resume-help 等仍为普通输入。
- 列表为 number、title、updated_at；0 为当前选择，其他编号排除当前 ID、沿用最近修改优先排序。未登记的 0 显示“新对话”和启动 UTC 时间，不写占位记录。标题折为单行。
- 编号只绑定最近展示的 ID。列表后新增记录不改变旧映射；Manager 在激活前重新查索引与目录归属。无效编号、0、格式错误不执行模型。
- Manager.open_session 校验历史并刷新修改时间，返回 LoadedSession；成功后 CLI 更新当前选择、单独展示历史和警告、清空旧编号。失败保持旧选择，目标删除/移出目录会清空映射。
- 更新时间失败属于部分失败：提示警告但继续使用已加载目标，不能误写旧 ID。SQLite 查询错误和检查点错误单独提示，不展示底层异常原文。
- 与旧 CLI 的差别：无检查点的合法索引现在可作为空会话恢复，与 API 一致；已有但未完成检查点仍拒绝。原缺失状态失败用例改为真实未完成检查点，目标是验证失败时不切换。成功回合现在也由 Manager 更新时间，不仅恢复时更新；列表查询仍不更新时间。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 目录筛选和修改时间排序 | 当前目录两条记录，创建时间顺序与更新时间相反；其他目录有记录，标题含换行 | 展示 updated_at 而非 created_at，0 为未登记的当前会话，1、2 按修改时间降序；不显示其他目录，标题单行化，不生成新索引或执行模型 | test_resume_lists_current_zero_then_update_order_for_current_directory |
| 当前会话不重复 | 先完成一轮，再显示列表 | 当前记录仅出现在编号 0，目录中只多一条普通请求创建的记录，检查点不包含命令 | test_resume_current_record_is_zero_and_not_duplicated |
| 冷启动恢复与缺少斜杠的回归 | 当前实例尚未执行普通请求，列表和选择交叉使用 /resume、resume（含混合大小写与空白），恢复 1 并继续 | 自动展示旧历史；原 ID 下新增且仅新增普通用户问题与回答，恢复命令不进入 messages；不创建冷启动空会话、不覆盖旧标题，新列表 0 指向恢复后的会话，显示已持久化的新修改时间，创建时间不变 | test_resume_switches_before_first_prompt_and_continues_existing_checkpoint |
| 往返切换 | 当前会话已有一轮，恢复旧会话，再刷新列表切回原会话并继续 | 两条索引，不混入另一会话消息，原会话继续写入正确检查点 | test_resume_can_switch_back_to_original_conversation_after_refreshing_list |
| 无效编号 | /resume 和 resume 两种形式下的 0、负数、越界、字母、小数、额外参数、isdigit 为真但不能 int 转换的字符 | 提示当前会话、编号无效或用法，不显示历史、不执行模型、不写新索引，也不刷新旧修改时间 | test_resume_invalid_or_current_numbers_do_not_execute_or_register |
| 未先查看列表 | 目录有历史，但直接 /resume 1 或 resume 1 | 提示先查看列表，不擅自推断编号 | test_resume_requires_a_displayed_list_before_selecting_a_number |
| 记录失效 | 展示后，记录被删除或 workspace_path 改为其他目录 | get_session 复查拒绝，不展示旧状态、不执行模型，不登记当前空会话 | test_resume_revalidates_selected_record_before_switching |
| 检查点未完成 | 索引存在，检查点只有待完成用户输入；恢复失败后输入普通问题 | 提示恢复失败，当前新会话仍可登记并执行，目标索引及修改时间保持原样 | test_resume_failed_switch_keeps_current_id_and_registration_state |
| SQLite 查询错误 | 列表或单条记录查询抛 OperationalError | 显示查询/恢复错误，不执行模型、不登记额外会话 | test_resume_database_errors_are_reported_without_execution |
| 更新时间写入失败 | 已恢复目标后，更新修改时间抛 SQLite 错误，再提交普通输入并查看列表 | 明确提示部分失败，目标仍为当前 0，不重复登记，继续请求写入目标检查点，修改时间保持旧值 | test_resume_timestamp_update_failure_keeps_restored_session_usable |
| 空列表 | 当前目录无索引，使用 /resume 或 resume 查看列表，再输入 /resume 0 | 仅显示当前 0，不创建数据库记录 | test_resume_empty_directory_displays_only_current_without_creating_index |
| 编号稳定及失效 | 展示后插入更晚创建的记录，再选择旧编号；成功后重复使用旧编号 | 恢复原来展示的 ID 而非新记录；成功后提示编号无效需刷新，继续请求仅追加到选中会话 | test_resume_uses_displayed_number_mapping_and_invalidates_it_after_switch |
| 普通输入分发 | 显示列表后提交带空白换行的文本、/resume-help、resume-help、未知命令或 /exit extra | 不误判为已支持的命令；原文本进入检查点，标题保留原输入前 10 字符，仅登记一次 | test_input_dispatch_preserves_ordinary_text_and_unknown_commands |
| 刷新失败后的编号 | 首次显示列表成功，第二次刷新抛 SQLite 错误，再选择旧编号 | 提示查询失败和编号无效，不沿用检查函数外部持有的旧映射，不恢复历史或写新索引 | test_input_dispatch_clears_previous_numbers_when_list_refresh_fails |

## 隔离方式

[CLI conftest](../../tests/cli/conftest.py) 同时隔离 settings.root、cwd 和配置环境变量，前后清理缓存。resume_db 显式建立 tmp_path 下 SQLite 与 sessions 表，注入真实 Manager/Agent/AsyncSqliteSaver，只使用本地 FakeListChatModel 与关闭的 Skills/MCP。输入和 PromptSession 用替身，不触碰用户终端或凭据。

正常路径使用真实 Store、检查点和 Manager；历史通过图 aupdate_state 写入。固定元数据时间避免 sleep。交互后重新打开 Saver，核对原 ID、上下文、命令未入历史。只有 SQLite 错误注入才替换 Store 查询/更新时间方法；编号失效测试真实更新或删除临时记录。

## 执行

```bash
uv run pytest -q tests/cli/test_resume.py --tb=no
uv run pytest -q tests/cli tests/core/test_conversation_history.py tests/api --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/cli src/omt/core/session_manager.py tests/cli/test_resume.py
```

## 覆盖限制

- 交互由异步输入替身驱动，不证明真实终端宽度/键盘编辑；真实内存 PromptSession 和 SIGINT 见 [CLI 测试](cli.md)。
- 启动不自动恢复上次会话；不支持图中断恢复、删除/重命名命令或检查点修复。
- 目录归属不是多用户权限或沙箱；不保证外部进程修改索引与检查点时的跨连接原子性。
- 时间按文本字段排序，外部写入须统一 UTC 格式；标题折行不等于过滤任意终端控制字符。
