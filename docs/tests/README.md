# 测试说明规范与索引

这里是供人类 Review 的测试行为说明，不替代可执行测试，也不收录真实运行日志或凭据。

## 1. 按模块查找

| 说明 | 对应测试 |
| --- | --- |
| [模型初始化](ai-models.md) | [test_ai.py](../../tests/ai/test_ai.py)、[test_models_manager.py](../../tests/ai/test_models_manager.py)、[test_deepseek.py](../../tests/ai/test_deepseek.py)、[test_model_catalog.py](../../tests/ai/test_model_catalog.py) |
| [Agent 与同步/异步 v3 流](agent.md) | [test_agent.py](../../tests/test_agent.py)、[test_agent_stream.py](../../tests/test_agent_stream.py)、[test_agent_async.py](../../tests/test_agent_async.py) |
| [Conversation、历史与会话 ID](conversation.md) | [test_conversation.py](../../tests/core/test_conversation.py)、[test_conversation_history.py](../../tests/core/test_conversation_history.py) |
| [推理强度展示、切换与重建](thinking.md) | [test_thinking.py](../../tests/cli/test_thinking.py) |
| [模型登录、完整目录与运行中选择](model-login.md) | [test_model_configuration.py](../../tests/core/test_model_configuration.py)、[test_login.py](../../tests/cli/test_login.py) |
| [Textual TUI 交互与生命周期](tui.md) | [test_tui.py](../../tests/cli/test_tui.py)、[test_tui_input.py](../../tests/cli/test_tui_input.py) |
| [CLI 入口、终端与生命周期](cli.md) | [test_app.py](../../tests/cli/test_app.py)、[test_terminal.py](../../tests/cli/test_terminal.py)、[test_lifecycle.py](../../tests/cli/test_lifecycle.py) |
| [HTTP 与会话管理](api.md) | [test_api.py](../../tests/api/test_api.py) |
| [SQLite 会话索引](store.md) | [test_store.py](../../tests/core/test_store.py) |
| [CLI /resume 会话选择与恢复](resume.md) | [test_resume.py](../../tests/cli/test_resume.py) |
| [模型配置与共用组装](session-and-config.md) | [test_bootstrap.py](../../tests/core/test_bootstrap.py)、[test_settings.py](../../tests/core/test_settings.py)、[test_configuration_files.py](../../tests/core/test_configuration_files.py) |
| [工具、注册与文件查找](tools.md) | [test_tools_manager.py](../../tests/core/test_tools_manager.py)、[test_read.py](../../tests/core/tools/test_read.py)、[test_edit.py](../../tests/core/tools/test_edit.py)、[test_write.py](../../tests/core/tools/test_write.py)、[test_bash.py](../../tests/core/tools/test_bash.py)、[test_find_files.py](../../tests/core/utils/test_find_files.py) |
| [内置系统提示词](prompts.md) | [test_prompts.py](../../tests/core/test_prompts.py) |
| [Skills 发现与提示词](skills.md) | [test_skills.py](../../tests/core/test_skills.py) |

核心源码与测试目录统一为 `src/omt/core/`、`tests/core/`；原有配置隔离、工具与会话断言保持不变。CLI/API 测试也统一从 `omt.core` 导入，提示词用例核对新的包资源路径，防止只移动目录却遗漏动态导入或资源定位。

测试文件里的参数化用例可能产生多个执行项，实际数量以 pytest 为准，不在这里固定维护通过数量。

## 2. 每次改代码时怎么交付测试

1. 新增或更新相关测试，说明新增行为、修复回归或契约变化。问题修复尽可能先看到用例失败，再验证修复后通过。
2. 同步更新本目录的模块说明；新增独立模块时新建说明并补入上方索引。测试被删除、合并或预期改变，也必须同步说明原因。
3. 文字说明必须能让未读实现的人回答“为什么测、怎么触发、正确结果是什么、如何独立运行”。
4. 测试说明写可观察行为，不只写“测试正常/异常”；不要复制整段测试代码、把 mock 调用次数当成全部业务预期。
5. 运行相关测试和全量测试。交付记录写实际结果与限制；本文档只保存稳定的行为说明，不堆叠每次运行截图和历史计数。

每份说明至少包含：

- 测试对象和对应源码、测试文件。
- 场景、输入/前置条件、预期行为，以及可定位的测试函数或函数组。
- 正常、边界、异常情况；不适用时说明理由。
- 测试数据和外部依赖如何隔离。
- 运行命令。
- 覆盖限制：测试没有证明什么，哪些是人工或受平台限制的验证。

仅修改文档时核对链接与源码事实，无需为了凑用例增加不验证行为的测试。

## 3. 可复用的说明模板

```markdown
# <模块> 测试说明

## 对象与目的
- 源码：<路径>
- 测试：<路径>
- 本次要防止的错误或保证的契约：<具体描述>

## 场景
| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 正常路径 | ... | ... | test_... |
| 边界路径 | ... | ... | test_... |
| 异常路径 | ... | ... | test_... |

## 隔离方式
<临时文件、假模型、SDK 替身、环境变量和清理策略>

## 执行
<可复制的 pytest 命令>

## 覆盖限制
<未覆盖的平台、真实网络、交互或兼容性>
```

一次变更影响多个模块时，可以更新多份现有说明，不要求每个测试函数单独新建文件。

## 4. 安全与执行约定

全量离线验证：

```bash
uv run pytest -q --tb=no
```

确认失败输出不含敏感数据后，再对具体用例使用 `--tb=short` 排查。不要依赖隐藏堆栈来替代测试隔离。

- [core/conftest.py](../../tests/core/conftest.py) 的自动 fixture 将 `settings.root` 和 cwd 指向每例的 `tmp_path`，并在前后清空设置缓存。配置测试不能只 `chdir`：实际配置目录可能固定在用户 HOME。Conversation 测试也同时隔离默认用户和项目 Skills。CLI 测试的 [cli/conftest.py](../../tests/cli/conftest.py) 额外清理入口环境变量，并显式关闭 MCP，将 CLI 的 Skills 搜索覆盖为 tmp_path/skills，避免读取在设置类导入时已展开的真实 ~/.agents/skills。
- [ai/conftest.py](../../tests/ai/conftest.py) 在 SDK 初始化处返回本地假模型，保留 Model 到提供商初始化函数的内部调用链。
- 顶层 Agent 和 core 下的 Conversation 测试直接注入假模型，不读取配置或真实认证；Conversation 测试另用局部自动 fixture 隔离 settings.root 与 cwd，避免扫描真实 Skills。
- 文件操作只使用临时目录。Bash 测试只执行本地假数据命令，并清理测试启动的进程；POSIX/Bash 或 FIFO 不可用时允许有明确原因的跳过。
- 真实模型调用、真实账户 OAuth、远程服务和用户终端的人工验证不属于默认测试通过的含义；需要额外授权并与离线结果分开报告。

Python 代码变更还应对修改文件运行静态检查。检查器安装情况及诊断以实际环境为准，不通过忽略错误来凑“零错误”。

## 5. Review 验收清单

- [ ] 修改的行为都有对应测试；用例不是只验证“没报错”。
- [ ] 返回值、状态变化和副作用都按相关性检查。
- [ ] 失败与边界输入不会被正常路径掩盖。
- [ ] 配置、文件、模型和子进程已隔离，不访问真实凭据或网络。
- [ ] 说明中的测试路径与名称存在，输入和预期与断言一致。
- [ ] 架构契约变化已同步到 [架构说明](../architecture.md)。
- [ ] 执行结果、类型检查、平台跳过和未覆盖内容已如实交代。
