# OMT 文档导航

OMT 当前是一个基于 LangChain/LangGraph 的 Agent 应用，CLI 与支持 JSON/SSE 的本机 FastAPI 共用 core 模块，并保留同步调用能力。先理解“组装入口、会话状态、执行图”分别由谁负责，再修改实现。

## 按任务阅读

| 要做的事 | 先读 | 再看源码 |
| --- | --- | --- |
| 开发 Web 前端、交给 AI 对接接口 | [前端接入 API 文档](frontend-api.md) | [路由](../src/omt/api/routes.py)、[数据结构](../src/omt/api/schemas.py) |
| 启动或调用 HTTP、调整会话管理 | [HTTP 使用说明](api.md)、[HTTP 测试](tests/api.md)、[架构说明](architecture.md) | [api/](../src/omt/api/)、[session_manager.py](../src/omt/core/session_manager.py) |
| 启动 CLI，调整交互或命令 | [CLI 使用说明](cli.md)、[CLI 测试](tests/cli.md)、[架构说明](architecture.md) | [cli/](../src/omt/cli/)、[session_manager.py](../src/omt/core/session_manager.py) |
| 调整认证、配置路径或默认模型 | [架构说明](architecture.md)、[配置与组装测试](tests/session-and-config.md) | [settings.py](../src/omt/core/settings.py)、[bootstrap.py](../src/omt/core/bootstrap.py) |
| 调整模型初始化或增加提供商 | [模型测试](tests/ai-models.md)、架构说明 | [model.py](../src/omt/ai/model.py)、[models_manager.py](../src/omt/ai/models_manager.py) |
| 调整 Agent 参数、流协议或中断 | [Agent 测试](tests/agent.md)、架构说明 | [agent.py](../src/omt/agent/agent.py) |
| 调整返回值、历史保存或输出展示 | [Conversation 测试](tests/conversation.md)、[CLI 测试](tests/cli.md)、架构说明 | [conversation.py](../src/omt/core/conversation.py)、[terminal/](../src/omt/cli/terminal/) |
| 新增或修改文件/Bash 工具 | [工具测试](tests/tools.md)、架构说明 | [工具目录](../src/omt/core/tools/)、[tools_manager.py](../src/omt/core/tools_manager.py) |
| 调整基础系统提示词或加载方式 | [提示词测试说明](tests/prompts.md)、[架构说明](architecture.md) | [system.md](../src/omt/core/prompts/system.md)、[加载函数](../src/omt/core/prompts/__init__.py)、bootstrap.py |
| Skills 发现、元数据或目录提示词 | [Skills 测试说明](tests/skills.md)、[架构说明](architecture.md) | [skills.py](../src/omt/core/skills.py)、[conversation.py](../src/omt/core/conversation.py) |
| 编写测试与人类 Review 说明 | [测试规范与索引](tests/README.md) | 对应测试模块 |

协作规则的唯一入口是根目录的 [AGENTS.md](../AGENTS.md)。架构文档描述当前契约；测试说明描述验证证据与覆盖限制，不把两者当作未来功能清单。

## 方案调研

- [TUI 展示层方案调研](research/tui-frameworks.md)：比较 Textual、prompt-toolkit 与 Urwid，记录实验分支的选型、落地与验证边界。
- [Skills 模块设计调研](research/skills-design.md)：比较其他 Agent 的实现，并记录 OMT 的初步建议；不是已实现功能说明。

## 维护方式

- 改接口或数据流时，在同一次改动中更新架构说明的相关段落。
- 新增或变更测试时，在 `docs/tests/` 更新对应说明；不要只把测试代码粘贴到文档里。
- 代码入口、依赖和命令最终以 [pyproject.toml](../pyproject.toml) 为准。文档不固定记录测试总数或依赖的精确安装版本。
