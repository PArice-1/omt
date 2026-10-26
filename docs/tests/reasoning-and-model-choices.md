# 核心 reasoning 与快捷模型目录测试说明

## 对象与目的

- 源码：[conversation.py](../../src/omt/core/conversation.py)、[events.py](../../src/omt/core/events.py)、[model_configuration.py](../../src/omt/core/model_configuration.py)、[settings.py](../../src/omt/core/settings.py) 的 ModelDefinition。
- 测试：[test_reasoning.py](../../tests/core/test_reasoning.py)、[test_model_choices.py](../../tests/core/test_model_choices.py)。
- 保证 reasoning 属于入口无关的共用执行能力，不混入 answer，不为了前端显示重建模型历史；快捷模型目录可供 API 和后续 CLI 复用，不依赖 HTTP。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 真实分片推理与正文 | 本地模型依次输出同块两段 reasoning、另一块两段正文；分别公开调用 run/arun | reasoning 增量先于正文，顺序不乱；finish 不重复整块；answer 仅正文；事件 message_id 等于最终消息 ID；历史仍是原始 human/ai 链，标准 reasoning 块完整保留 | test_reasoning_deltas_are_ordered_separate_and_not_duplicated_at_finish |
| 并行工具先后反转 | 第一个异步工具等待第二个输出并完成，两个调用同名且 ID 不同；通过公开 arun 运行真实图 | 两个工具的进度和完成事件都保留，调用 ID 正确，不因逐个订阅惰性子通道丢掉第二个进度 | test_parallel_tool_progress_is_preserved_when_second_finishes_before_first |
| 仅完整内容块 | FakeMessagesListChatModel 一次性返回 reasoning/text 完整块；分别 run/arun | 没有 token delta 时 finish 补发全部内容各一次，reasoning 不混入最终回答 | test_finish_only_reasoning_is_emitted_without_becoming_answer |
| 空目录与完整导入 | 无配置调用快捷列表，再登录一个模型但导入多个完整 ID | 首次返回空列表且不创建目录；导入后按厂商/模型排序返回所有已配置项；已知映射按 none/low/high/max 排列，未知仅 none；读取前后配置字节不变 | test_configured_choices_are_read_only_and_include_the_full_catalog |
| 思考等级语义排序与兼容 | 本地目录参数化为 null/空映射、乱序 low/high/max、包含 medium 与自定义名称、null 能力及与等级强度相反的 effort 映射 | none 恒在首位；已支持的 low/medium/high/max 按等级名语义排列，不按 effort 值或字母；自定义名称在后按名称稳定排列；不补未声明能力，不写配置 | test_thinking_levels_use_strength_order_and_preserve_custom_levels |
| 认证厂商无凭据 | 已有 DeepSeek 目录但没有 auth，或 Key 为空/纯空白 | 不把这些模型列成可切换项，不创建默认设置；Ollama 免 Key 目录另由模型配置专项验证 | test_choices_exclude_models_without_saved_credentials |
| 未注册或厂商错配 | 临时本地目录含未注册厂商和与所属厂商不一致的模型项 | 只返回注册且身份匹配的已认证模型，不猜测其他厂商的能力 | test_choices_skip_unregistered_and_mismatched_provider_entries |

## 隔离方式

沿用 [core/conftest.py](../../tests/core/conftest.py) 的自动 fixture：settings.root 与 cwd 均位于 tmp_path，设置缓存前后清空。模型是本地 FakeList/FakeMessages 的确定性实现，不读取真实配置、不联网；并行回归仅使用 asyncio.Event 控制先后，不依赖随机 sleep；目录保存使用假 Key 和保留域名，仅写入 tmp_path 内的配置，无真实用户文件、数据库、MCP 或进程副作用。通过公开 Conversation 和 list_configured_models 验证行为，不直接调用私有投影器。

## 执行

```bash
uv run pytest -q tests/core/test_reasoning.py tests/core/test_model_choices.py --tb=no
uv run pytest -q tests/core/test_conversation.py tests/core/test_conversation_history.py tests/api tests/cli --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/core/conversation.py src/omt/core/events.py src/omt/core/model_configuration.py src/omt/core/settings.py tests/core/test_reasoning.py tests/core/test_model_choices.py
```

## 覆盖限制

- 不验证真实提供商一定返回 reasoning；只处理提供商可见文本，不提取加密/隐藏内容。
- 顺序断言针对同一个模型消息的内容块；不保证并行工具/模型的全局底层发生顺序。
- 测试不增加 CLI reasoning 展示或 `/models` 命令；CLI 继续使用既有正文渲染，接入另行推进。
- 快捷目录不联网验证 Key 或模型有效性；配置损坏仍由共用读取器拒绝，不自动覆盖修复。
- 历史跨 SQLite 重建应用、消息关联和 HTTP 安全投影见 [SSE 测试](api-runtime-streaming.md)。
