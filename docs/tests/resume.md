# CLI /resume 会话选择与恢复测试说明

## 对象与目的

- 源码：[session.py](../../src/omt/coding_agent/session.py)，调用 [store.py](../../src/omt/coding_agent/store.py) 与 [runtime.py](../../src/omt/coding_agent/runtime.py) 的公开接口。
- 测试：[test_resume.py](../../tests/coding_agent/test_resume.py)。
- 保证 /resume 只操作当前启动目录的会话，不把命令发送给模型或写成新会话标题；展示编号与恢复的 thread_id 一致，切换成功后不重复登记或污染其他会话历史。
- Store 的 get_session(thread_id) 仍是单条元数据查询，列表通过 list_sessions(workspace_path) 获取；完整消息由 switch_session() 从 Checkpointer 加载并展示，不在索引表中重建。

## 交互契约

命令识别与处理从 asession() 提取为异步 _check_user_input()：返回 (action, restored_session)，action 为 run（普通请求）、handled（命令已处理）或 exit（退出）；只有恢复成功才返回 SessionRecord。编号字典通过 clear/update 原地更新，asession 仍负责读取输入、根据恢复记录同步 ID 与登记状态、登记普通请求并执行 arun。命令检查先于索引登记和模型执行，测试通过公开 session() 驱动，不依赖内部函数调用次数。

- resume 是 /resume 的无斜杠别名，两者共用列表、编号校验和恢复分支，支持首尾空白与大小写混合。兼容性变化：以独立词 resume 开头的输入现在保留为 CLI 命令，即使参数无效也不再作为普通用户消息交给模型；resume-help 等不同词仍按普通输入处理。
- /resume 展示 number、title、updated_at 三列。0 始终是当前会话，标题追加“当前”；其余记录排除当前 ID，沿用 Store 最近修改优先的顺序：updated_at 降序，同时间按 created_at 降序和 ID 升序。根据需求由创建时间改为上次修改时间，页面不再另按创建时间排序。
- 当前会话尚未执行普通请求时也有编号 0，显示“新对话”和启动时的 UTC 时间，但不写占位索引。空目录仍可显示当前会话。标题中的换行和空白合并为单行，空标题显示“未命名对话”。
- /resume N 只接受最近一次已显示列表中的正数编号；0 提示无需恢复，其他无效编号、非整数或多余参数均提示用法，不执行 Agent。命令支持首尾空白、命令名忽略大小写。
- 编号绑定该次展示的 thread_id。列表显示后新增记录不会静默改变编号含义；选择时通过 get_session 再查记录存在性与目录归属。
- 先 await switch_session，成功后才调用 Store.update_session_updated_at 刷新目标记录，返回的 SessionRecord 带新 updated_at，再同步 CLI 局部 thread_id、修改时间与已登记标记，不再 insert_session，也不改 created_at。旧编号映射清空，下一次切换需重新 /resume，以保证 0 仍表示当前会话。
- 查询或恢复失败单独提示，不执行新回合、不更改登记状态，也不刷新修改时间；Runtime 对失败切换保持 messages/config 不变。修改时间写入发生在切换之后，若写入失败，则提示“修改时间更新失败，会话已切换”，仍返回恢复记录让入口同步目标 ID 和已登记状态；不能误报恢复失败或继续写到旧会话。记录删除或移出目录时清空旧列表。未完成检查点或只有索引没有状态的会话不能直接追加输入。

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
| 检查点缺失 | 历史有索引但没有状态；恢复失败后输入普通问题 | 提示恢复失败，当前新会话仍可登记并执行，目标索引及修改时间保持原样 | test_resume_failed_switch_keeps_current_id_and_registration_state |
| SQLite 查询错误 | 列表或单条记录查询抛 OperationalError | 显示查询/恢复错误，不执行模型、不登记额外会话 | test_resume_database_errors_are_reported_without_execution |
| 更新时间写入失败 | 已恢复目标后，更新修改时间抛 SQLite 错误，再提交普通输入并查看列表 | 明确提示部分失败，目标仍为当前 0，不重复登记，继续请求写入目标检查点，修改时间保持旧值 | test_resume_timestamp_update_failure_keeps_restored_session_usable |
| 空列表 | 当前目录无索引，使用 /resume 或 resume 查看列表，再输入 /resume 0 | 仅显示当前 0，不创建数据库记录 | test_resume_empty_directory_displays_only_current_without_creating_index |
| 编号稳定及失效 | 展示后插入更晚创建的记录，再选择旧编号；成功后重复使用旧编号 | 恢复原来展示的 ID 而非新记录；成功后提示编号无效需刷新，继续请求仅追加到选中会话 | test_resume_uses_displayed_number_mapping_and_invalidates_it_after_switch |
| 普通输入分发 | 显示列表后提交带空白换行的文本、/resume-help、resume-help、未知命令或 /exit extra | 不误判为已支持的命令；原文本进入检查点，标题保留原输入前 10 字符，仅登记一次 | test_input_dispatch_preserves_ordinary_text_and_unknown_commands |
| 刷新失败后的编号 | 首次显示列表成功，第二次刷新抛 SQLite 错误，再选择旧编号 | 提示查询失败和编号无效，不沿用检查函数外部持有的旧映射，不恢复历史或写新索引 | test_input_dispatch_clears_previous_numbers_when_list_refresh_fails |

## 隔离方式

共享 [conftest.py](../../tests/coding_agent/conftest.py) 将 settings.root、cwd 指向 tmp_path 并清理设置缓存。resume_db fixture 显式创建临时 SQLite 与 sessions 表，替换导入时固定的 session.db_path。配置、模型初始化与 PromptSession 由本地替身提供；异步 Agent 工厂使用 FakeListChatModel 和真实 AsyncSqliteSaver，不发现或启动 MCP 服务，不读取真实凭据。

历史索引由真实 SessionStore 写入，固定创建/更新时间避免依赖 sleep；历史检查点通过真实图 aupdate_state 写入。交互结束后重新打开 Saver 读取 messages，验证正确 ID、上下文和命令隔离。只有数据库错误注入时替换对应 Store 查询方法；正常恢复使用真实 Runtime.switch_session 和 display_history。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_resume.py --tb=no
uv run pytest -q tests/coding_agent/test_resume.py tests/coding_agent/test_session.py tests/test_runtime_switch.py --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/coding_agent/session.py tests/coding_agent/test_resume.py
```

## 覆盖限制

- 交互通过异步输入替身驱动，不验证真实终端的表格宽度或键盘编辑；真实 PromptSession 和 SIGINT 另见 [Session 测试](session-and-config.md)。
- 不验证进程重启后自动恢复：启动仍创建新会话，必须显式选择历史。不支持工具执行中的暂停恢复、会话删除或重命名；本次只在切换成功后刷新 updated_at，不在普通回合结束后自动刷新，也不在查询列表时刷新。
- 目录筛选是工作目录归属规则，不是多用户权限控制；外部程序可修改数据库，不保证查询与切换之间的跨连接原子事务。
- 列表时间按现有文本字段排序，外部写入必须采用一致的 UTC 格式；标题不是任意终端控制字符的安全过滤器。
