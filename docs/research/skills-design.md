# Skills 模块调研与 OMT 初步建议

本文记录方案调研与建议，不作为当前功能的唯一依据。后续已在 `core/skills.py` 实现元数据发现与目录提示词生成，Conversation 也已将目录与可选外部提示词文本合并为初始 SystemMessage；当前契约见[架构说明](../architecture.md)，验证见[Skills 测试说明](../tests/skills.md)。

## 1. 核心模型

Agent Skills 将专业工作流打包成一个包含 `SKILL.md` 的目录，使用 YAML frontmatter 描述名称和适用场景，正文提供操作指引，旁边可以放脚本、参考资料和模板。[S1][S2]

```text
review/
├── SKILL.md
├── references/
├── scripts/
└── assets/
```

核心机制是渐进披露，而不是为每个 Skill 创建独立 Agent：[S1][S2]

1. 启动时只向模型展示各 Skill 的名称、描述和加载位置。
2. 任务匹配后才加载完整 `SKILL.md`。
3. 按正文指引继续读取参考文件，或通过已有工具运行脚本。

Skill 是流程与知识，Tool 是可执行能力。加载 Skill 不等于执行脚本，也不自动获得新的权限。[S2][S5]

## 2. 不同 Agent 的实现取向

| 实现 | 发现和加载 | 触发与扩展 | 参考价值 |
| --- | --- | --- | --- |
| Pi | 扫描配置位置，将名称、描述和路径写入系统提示词；模型通过读取文件加载正文 | 支持 `/skill:name`；`disable-model-invocation` 可阻止自动选择；支持资源重载、同名诊断 | 可以复用现成的 read/bash，不必新增执行器 [S2] |
| Claude Code | 以 `SKILL.md` 定义工作流，按需加载 | 支持 `/skill-name`；`disable-model-invocation` 控制自动调用；`context: fork` 可在子代理上下文运行 | 将基础 Skill 与可选的上下文隔离扩展区分开 [S3] |
| Codex | 根据 `SKILL.md` 描述匹配任务 | 支持显式选择和隐式选择；可通过 `agents/openai.yaml` 的 `allow_implicit_invocation` 关闭隐式调用 | 共享内容格式，产品专属行为可放在单独配置中 [S4] |
| OpenCode | 在 `skill` 工具说明中列出可用 Skills，通过工具按名称加载 | Skill 权限支持 allow/ask/deny | 统一加载入口更容易增加权限检查和可观察性 [S5] |

各产品的命令、搜索路径、冲突策略和扩展字段并不完全一致。共享 `SKILL.md` 不代表这些行为也通用。

尤其要区分：**提示词要求模型加载 Skill，并不保证它一定加载**；Pi 文档明确指出模型可能遗漏，显式调用用于强制选择。[S2]

## 3. OMT 现状与接入位置

已核对以下本地文件：

- `src/omt/core/bootstrap.py`：共用组装模型、工具和 Manager；由 Manager 在空会话创建 Conversation 时发现 Skills 并初始化系统消息，而不是注入 Agent.system_prompt。旧 CLI session.py 已迁至平级 cli 模块。
- `src/omt/agent/agent.py`：构造函数已经支持 `system_prompt` 和 middleware；基础能力足够承载 Skill 目录提示词，无需改造执行循环。
- `src/omt/core/tools_manager.py`：静态注册 edit/read/write/bash，可承接 Skill 文件读取和脚本运行。
- `docs/architecture.md`：Conversation 管理历史和结构化事件，CLI 负责终端展示，工具循环由 LangGraph 执行。

建议把文件发现、解析、冲突处理和目录渲染放在 `core` 下的 Skills 模块。原建议由入口传给 Agent.system_prompt；当前按后续需求选择在 Conversation 调用独立发现函数，初始化首条 SystemMessage，恢复已有历史不重扫。文件解析和目录渲染仍由 Skills 模块负责，不在执行核心重复实现。

## 4. 建议的第一版范围（发现、渲染与 Conversation 接入已实现，其余仍是建议）

优先采用 Pi 风格的“目录提示词 + read 加载”，保留以后改成专用 `skill` 工具的余地。

### 文件格式

先兼容标准基础字段 `name`、`description`；其他标准字段可以保留，但不能把未实现的字段解释成已生效的能力。不要执行 YAML 对象构造；解析时使用安全加载方式。

### 对外接口方向

- `discover_skills(roots)`：返回可用 Skill 元数据和结构化诊断；隐藏扫描、解析、排序和重名处理。
- `render_skills_prompt(skills)`：生成目录及“先加载再使用”的说明，不拼接全部正文。

Skill 元数据至少保存名称、描述和 `SKILL.md` 的绝对路径。仅做目录加载时，不必提前增加加载状态机、Skill 继承或插件生命周期。

### 必须先确定的契约

1. **搜索目录**：建议支持项目 `.agents/skills/` 与用户 `~/.agents/skills/`；是否补充 OMT 专属目录另行决定，不默认实现祖先目录遍历。
2. **同名处理**：建议项目覆盖用户目录，并返回可展示的冲突诊断；保持扫描顺序确定。此为 OMT 建议，不是 Pi 行为的照搬。
3. **无效文件**：单个文件格式错误不阻断整个 Agent；跳过并报告路径和原因，不回显全部文件内容。
4. **相对路径**：正文里的资源路径按 Skill 所在目录解析，不能直接按项目 cwd 猜测。
5. **信任**：只启用已信任的项目目录；Skills 能诱导调用当前用户权限下的工具，格式校验不等于安全审查。
6. **命令**：显式调用的命令属于 CLI 入口；若实现，应直接解析已发现的名称并读取文件，而不是只把命令字符串交给模型。
7. **限制**：控制扫描范围、单文件大小和目录提示词预算；若用现有 read 工具，确保完整读取 Skill 正文，而不是只取前 50 行。

### 暂不纳入

子代理隔离、自动安装市场、热重载、依赖安装、动态注册新工具、独立 Skill 执行器。

尤其不应把“读取 Skill”与“自动运行其中脚本”绑定。执行脚本继续走既有工具机制；目前 OMT 没有工具沙箱或审批，不能宣称实现 Skill 权限隔离。

### 后续测试方向

基础发现与渲染测试现已补充，见 [Skills 测试说明](../tests/skills.md)。Conversation 接入也已补充测试，见 [Conversation 测试说明](../tests/conversation.md)。后续扩展仍需先阅读 `docs/tests/README.md`，再补测试和对应文字说明：发现与解析、排序与重名、坏文件隔离、无 Skills、路径解析、系统提示词仅含摘要，以及通过假模型验证正文按需进入消息历史。所有测试使用临时目录和假模型，不读取用户 Skills 或调用真实模型。

## 5. 来源与核对限制

- [S1] [Agent Skills 规范](https://agentskills.io/specification)，以及[规范源码](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)。规范源码相关内容通过搜索服务返回的第一方来源摘要核对。
- [S2] 当前本机 Pi 1.3.0 安装包的 `docs/skills.md`，已完整读取；安装路径为 `/home/laterain/.pi/agent/install/releases/1.3.0/node_modules/@earendil-works/pi-coding-agent/docs/skills.md`。此项描述该本地版本，不保证与远端最新版一致。
- [S3] [Claude Code Skills 官方文档](https://code.claude.com/docs/en/skills)，通过搜索服务返回的第一方来源摘要核对。
- [S4] [Codex Skills 官方文档](https://developers.openai.com/codex/skills)，通过搜索服务返回的第一方来源摘要核对。
- [S5] [OpenCode Skills 官方文档](https://docs.opencode.ai/docs/skills/)，通过搜索服务返回的第一方来源摘要核对。

外部页面直接抓取受本环境网络地址检查限制，未获取完整正文，因此上述远端实现比较是文档层面的调研，不是完整源码审计。后续实现产品专属字段前需进一步核对。当前没有可用子代理模板，调研由主代理完成。
