# Agent 与同步/异步 v3 流测试说明

## 对象与目的

对象是 [Agent 包装](../../src/omt/agent/agent.py)。测试验证包装是否保留底层调用语义，以及同步/异步 v3 是否仍是懒执行、可投影和可清理的原始流，而非包装层自行拼装的事件列表。

测试：

- [test_agent.py](../../tests/test_agent.py)
- [test_agent_stream.py](../../tests/test_agent_stream.py)
- [test_agent_async.py](../../tests/test_agent_async.py)

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 同步调用 | 本地模型、有/无工具列表 | 真实执行 Agent 图，返回用户消息和 AIMessage；默认 tools 为 None | test_invoke_runs_real_graph_with_local_model、test_constructor_defaults_to_no_tools |
| 调用参数透传 | 状态、Command、None，以及 config/context/输出/中断等参数 | 参数和值的身份按底层约定传递，返回底层对象 | test_invoke_forwards_input_config_context_and_extra_options |
| 构造与默认值 | 多种结构化响应配置、工具序列、中间件、检查点等 | 构造参数透传；包装默认值和参数种类与底层一致 | test_constructor_forwards_explicit_options_and_extra_options、test_explicit_defaults_match_underlying_functions |
| 多轮检查点 | 同一内存检查点和 thread_id，连续提交新消息 | 第二轮状态包含两轮完整对话 | test_checkpointer_keeps_conversation_history |
| 运行上下文 | context_schema 与真实 middleware | middleware 收到原始 context 对象 | 两个测试文件的 context/middleware 用例 |
| 非默认输出 | updates 模式、output_keys | invoke 返回收集后的更新或所选结果，不伪装成实时迭代 | test_invoke_updates_mode_returns_collected_updates、test_invoke_output_keys_selects_messages |
| 静态中断恢复 | 模型节点前中断，随后使用 None 恢复 | 暂停时仅有输入消息，恢复后得到模型回答 | test_interrupt_before_model_can_resume_with_none |
| v3 透传与懒执行 | 创建流但不消费，随后迭代 | 返回原始 GraphRunStream；消费前模型未执行；不调用异步入口 | test_stream_event_forwards_options_and_returns_original_stream、test_stream_event_defaults_to_v3_and_is_lazy |
| 原始事件与投影 | 消费原始事件、values 或 messages.text | 原始事件使用 method/params；状态与最终输出一致；文本按片段出现 | test_stream_event_defaults_to_v3_and_is_lazy、test_stream_event_values_projection_and_output、test_stream_event_messages_projection_streams_text |
| 提前退出 | 读取首个状态后退出 with | 后续迭代停止，模型尚未被执行，再次 abort 安全 | test_stream_event_context_manager_aborts_early |
| 同步流中断恢复 | 内存检查点，模型前中断后清除中断条件 | 同步恢复并产生最后回答，不被当作永久完成 | test_stream_event_can_interrupt_and_resume_synchronously |
| 异常与错误版本 | 底层抛出异常，或请求 v1/v2/未知协议 | 异常对象保留；非 v3 在执行前被拒绝 | 两个文件的 preserves_*error、test_stream_event_rejects_other_protocols |

## 异步接口场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试（test_agent_async.py） |
| --- | --- | --- | --- |
| 异步透传 | 状态、Command 或 None；config/context、输出/中断/持久化及额外参数 | ainvoke/astream_event 等待底层异步接口，返回同一对象，不调用同步入口 | test_async_interfaces_forward_options_and_preserve_result |
| 异步错误与默认值 | 底层抛原始异常；比对 ainvoke 签名 | 保留异常身份；默认值及参数种类与底层一致 | test_async_interfaces_preserve_backend_error、test_ainvoke_defaults_match_underlying_function |
| 混合工具 | 本地模型同时请求只有异步实现的工具和同步工具；分别走 ainvoke 和 v3 流 | 两个工具均执行，结果按调用 ID 对应，最终回答正常返回 | test_async_graph_executes_async_only_and_sync_tools |
| 懒执行与文本投影 | 获取异步流后消费 messages.text | 获取流不执行模型，逐字输出，await output() 返回完整终态 | test_astream_event_is_lazy_and_streams_text_and_final_state |
| 原始事件与提前退出 | 读取首条 values 后离开 async with | 事件含 method/params；不执行后续模型，迭代结束，再次 abort 安全 | test_astream_event_raw_events_and_early_exit_close_stream |
| 异步检查点 | 内存检查点、模型前中断；None 恢复后继续第二轮 | 异步流恢复正常；ainvoke 保留两轮完整历史 | test_async_checkpointer_interrupt_resume_and_second_turn |
| 非 v3 请求 | v1/v2/未知协议 | 获取流前拒绝，不触发底层执行 | test_astream_event_rejects_other_protocols |

异步用例使用标准库 asyncio.run 管理每例的事件循环，不新增 pytest 异步插件；回合取消和正在执行的工具清理另见 [Runtime 测试](runtime.md)。

## 隔离方式

真实 LangChain/LangGraph 图搭配本地假模型；仅参数转发场景使用调用替身。检查点使用 InMemorySaver，存储使用内存对象；不读取配置或凭据，不请求模型提供商。

这些测试是 Agent 这层的 Interface 验证，不要求 Runtime 或终端参与。

## 执行

```bash
uv run pytest -q tests/test_agent.py tests/test_agent_stream.py tests/test_agent_async.py --tb=no
```

## 覆盖限制

- 结构化输出配置透传不代表真实提供商的结构化输出已验证。
- 流测试围绕同步/异步 v3，不覆盖真实提供商网络断开、所有投影组合或全部异步中间件行为。
- 中断用例验证底层静态中断与恢复，不代表上层 CLI 已有暂停/恢复、审批或持久化产品流程。
- 不对 subgraphs、reasoning、extensions 的全部行为作覆盖承诺。
- LangGraph v3 的实验性警告和升级兼容性需单独看待；隐藏警告不意味着兼容性得到验证。
