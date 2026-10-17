# Runtime、历史与会话 ID 测试说明

## 对象与目的

- 源码：[runtime.py](../../src/omt/coding_agent/runtime.py)
- 测试：[test_runtime.py](../../tests/test_runtime.py)

核心契约是：`run()` / `await arun()` 返回本轮最后回答的字符串；完整消息链留在会话的 `messages`。展示过程、保存历史、返回值三者不能相互替代。

## 初始消息与 Skills 场景

构造函数保留原有 `agent, config=None` 的位置参数，新增仅关键字参数 `system_prompt: str = ""`、`skill_roots: Sequence[Path] | None = None`。本次沿用 Runtime 首条 SystemMessage 的方式；相同基础提示词不能同时传给 Agent 和 Runtime。外部文件应由调用方读取后传入文本，Runtime 不自行猜测提示词文件位置。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 无初始内容 | 外部提示词为空或纯空白，显式关闭 Skills | 初始 messages 为空，不插入空 SystemMessage | test_initial_history_omits_empty_system_messages |
| 外部提示词 | 提示词带中文、换行及首尾空白 | 首条且唯一的 SystemMessage 保留原始文本格式 | test_external_system_prompt_is_preserved_in_initial_history |
| 合并目录 | 非空基础提示词和临时 Skill | 只初始化一条 SystemMessage，基础提示词在前、目录在后，包含绝对路径，不包含正文 | test_initial_history_combines_external_prompt_and_skill_catalog |
| 默认目录和覆盖 | 临时 settings.root/agent/skills 与 cwd/skills 中存在同名 Skill | 项目版本覆盖用户版本，覆盖诊断只写 stderr | test_default_skill_roots_prefer_project_over_user |
| 关闭或替换扫描 | 默认目录有内容；传入空列表或自定义目录 | 空列表不扫描默认目录，自定义目录替换而不是追加默认目录 | test_empty_skill_roots_disable_default_discovery、test_explicit_skill_roots_replace_default_search_paths |
| 无效 Skill | 缺少 description 的文件，正文含测试私有标记 | 显示诊断但不回显正文，无其他内容时保持空历史 | test_invalid_skill_is_diagnosed_without_creating_an_empty_system_message |
| 完整读取和多轮历史 | 本地假模型请求 read 读取超过 50 行的 Skill，再执行第二轮；分别启用/禁用 InMemorySaver | 每次模型输入恰含一条初始系统消息，正文作为 ToolMessage 保留，两轮不重复历史，检查点与 Session 一致；删除源文件后继续轮次不重新扫描 | test_system_message_and_skill_read_survive_multiple_turns_without_duplicates |
| 初始化后失败 | 已有初始 SystemMessage，提交空白输入或模型抛异常 | 旧历史保持原样，不提交失败回合 | test_failed_turn_preserves_initialized_system_history |

## 原有回合、展示与会话 ID 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 空白输入 | 空串、空格、换行/制表符 | 调用前拒绝，不改变历史 | test_run_rejects_blank_input_without_changing_history |
| 完整工具回合 | 模型请求一个本地工具，然后给出回答 | 保存 human → ai(tool_calls) → tool → ai；调用 ID 对应，返回值仅为最终总结 | test_run_saves_user_tool_request_tool_result_and_final_ai_message |
| 第二轮上下文 | 同一实例连续运行两轮 | 第二轮模型收到完整上一轮上下文及新输入，历史不重复；两轮各返回自己的回答 | test_next_turn_receives_complete_previous_history_without_duplicates |
| 输出组织 | 中间模型文本、工具参数、进度片段、最终结果、总结 | 各内容按该场景执行次序显示一次，不打印消息对象的元数据 repr | test_display_includes_intermediate_text_tool_input_progress_result_and_answer |
| 只有工具请求的模型输出 | 第一条 AIMessage 没有普通文本但有 tool_calls | 不丢弃该消息，工具仍执行和显示，最终回答正常返回 | test_tool_only_model_message_is_saved_and_tool_execution_is_displayed |
| 文本形态 | 合法用户输入有首尾空白；最后 AI content 是文本或内容块列表 | 输入原样保存，文本只显示一次，返回始终是最后回答的纯文本 | test_run_streams_plain_text_once_and_preserves_original_user_text、test_run_returns_plain_text_when_final_ai_content_is_a_block_list |
| 生成 ID | config 缺失、空字典、空 configurable | 生成合法 UUID v4 字符串 | test_session_generates_uuid_thread_id_when_missing |
| 保留外部配置 | 指定 thread_id、workspace、tags、metadata 等 | 现有值保留，配置外层和 configurable 被复制，原始字典不被修改 | test_session_preserves_external_thread_id_and_other_config、test_generating_thread_id_does_not_modify_external_config |
| ID 生命周期 | 新建两个会话；同一会话执行两轮 | 新会话 ID 不同，同一会话复用 ID 并传入每次 stream_event | test_new_sessions_have_different_thread_ids、test_run_reuses_and_forwards_session_thread_id |
| 检查点兼容 | Agent 配 InMemorySaver，Session 自动生成 ID | 两轮正常执行，检查点历史与 Session 历史一致，不重复消息 | test_generated_thread_id_works_with_checkpointer_without_duplicate_history |
| 不配置 config | 只注入 Agent | 仍能完成一轮并保存消息 | test_config_is_optional |
| 模型调用失败 | 上轮成功后，下轮模型抛异常 | 原异常传播并显示，旧历史保持不变，不提交失败回合 | test_failed_turn_preserves_previous_history_and_propagates_error |
| 未处理的工具异常 | 本地工具直接抛 RuntimeError | 显示工具失败，不误报工具完成，不保存为成功回合 | test_tool_exception_is_displayed_and_not_saved_as_a_successful_turn |
| 已处理的工具错误 | 工具返回 status=error 的 ToolMessage，模型继续解释 | 显示工具失败，错误消息和最终解释仍保存为完整回合 | test_error_tool_message_is_displayed_as_failure_and_kept_in_history |
| 多工具请求 | 同一 AIMessage 请求多个工具 | 每个工具结果保留，按调用 ID 与请求关联，不依靠完成先后猜配对 | test_multiple_tool_calls_are_saved_with_their_matching_results |
| 附加数据 | 工具返回 content/artifact，AI 带附加字段、结束原因和用量 | 数据保留在历史中；显示文本不包含对象元数据 | test_tool_artifact_and_ai_metadata_are_preserved_in_history |
| 实例隔离 | 两个独立 Session | 各自保存自己的用户和模型消息 | test_sessions_do_not_share_message_history |
| 不完整终态 | 最终状态缺失、空消息、最后为 ToolMessage 或待执行工具请求 | 明确报错，不返回工具内容或中间计划，不覆盖旧历史 | test_missing_final_state_does_not_commit_user_message、test_run_does_not_return_tool_output_or_pending_request_as_final_answer |

## 异步回合场景

新增用例直接通过公开 arun 调用真实异步 Agent 图；终态缺失场景才替换异步展示方法。测试既验证异步工具支持，也保证已有同步工具和同步会话契约不回归。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 混合工具与多轮 | 假模型请求同步/异步工具，异步工具报告进度并返回 artifact；可选内存检查点 | 输出各工具过程，保存调用 ID、artifact、模型元数据及唯一系统消息；第二轮上下文完整，thread_id 不变，检查点无重复 | test_arun_executes_mixed_tools_and_preserves_multiturn_history |
| 文本流与输入 | 逐字流式模型；输入带首尾空白 | 回答只显示一次，返回纯文本，输入原样保存 | test_arun_streams_text_once_and_preserves_original_input |
| 空白输入 | 空串、空格、换行/制表符，已有系统消息 | 执行前拒绝，历史不变 | test_arun_rejects_blank_input_without_changing_history |
| 模型中途失败 | 先成功一轮，后续模型输出部分文本再抛异常 | 原异常传播并展示，历史保留上一轮，不提交部分文本 | test_arun_failure_preserves_previous_history_and_original_error |
| 工具失败 | 异步工具抛异常，或返回 status=error 的 ToolMessage | 均显示工具失败、不显示完成；未处理异常保留原对象且不提交回合，已处理错误保留工具消息和最终解释 | test_arun_displays_tool_failures_without_reporting_success |
| 不完整终态 | None、空 messages、工具结果或待执行工具请求作为最后消息 | 不冒充最终回答，不覆盖旧历史 | test_arun_incomplete_final_state_does_not_commit_history |
| 取消与清理 | 本地异步工具进入等待后取消 arun 任务 | CancelledError 向外传播，工具 finally 执行，无遗留消费任务，初始历史不变，不打印普通执行失败 | test_arun_cancellation_cleans_up_tool_and_consumers_without_committing_history |
| 同步/异步交替 | 同一会话按 run → arun → run 顺序执行 | 共享完整历史，三轮不丢失、不重复 | test_sync_and_async_turns_share_history |

## 隔离方式

真实 Agent 图搭配本地假模型和临时定义的工具；工具进度通过 ToolRuntime.emit_output_delta 产生。模型输入被记录以核对第二轮确实获得历史，而非仅检查本地列表长度。只有缺失终态等难以从正常图得到的场景使用流/方法替身。

本组不读取 Settings/auth 文件或真实模型。模块级自动 fixture 同时将 settings.root 指向 tmp_path/.omt、将 cwd 切换到 tmp_path，因此默认用户/项目 Skills 都被隔离，不读取本机 Skills。测试完成由 monkeypatch 恢复路径。

## 执行

```bash
uv run pytest -q tests/test_runtime.py --tb=no
```

## 覆盖限制

- 不证明磁盘持久化、进程重启恢复或图中断恢复已完成。
- UUID 测试只验证格式、实例差异和复用，不构成持久化方案。
- “失败保留旧历史”只指 Session 已提交的内存列表，不表示工具副作用或外部检查点自动回滚。
- 输出顺序验证针对确定的本地执行链，不验证所有并发工具或原始事件的全局时间顺序；异步两个消费任务的打印顺序不作全局全序保证。
- 未验证同一 Session 的并发 run/arun；当前设计用于顺序调用。异步展示的两个消费任务不等于允许并发提交回合。
- 假模型按测试预设调用 read，验证的是加载和历史链路，不证明真实模型一定会自主选择正确 Skill。
- 不提供提示词或 Skills 热重载，也不负责外部提示词文件读取或工具沙箱；启用默认项目 Skills 前应确认目录可信。
