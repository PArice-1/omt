# 交互式 CLI

CLI 与 [HTTP](api.md) 是平级入口，共用 SessionManager 和依赖组装。CLI 不启动 HTTP 服务，也不要求 HTTP 访问令牌。

## 启动

1. `uv sync` 安装依赖，并按[模型配置说明](architecture.md#3-模型连接与配置)准备模型设置、认证与模型目录。
2. 预先创建 SQLite 数据库及 `sessions` 表，结构见[索引说明](tests/store.md#接口和预建表约定)；入口不自动建表或迁移。
3. 在可信目标目录启动。文件工具和 Bash 基于进程 cwd，工作目录配置不能代替沙箱。

```bash
# 人工运行，不是离线测试；提交问题可能计费并执行工具。
uv run omt
# 等价模块入口：
uv run python -m omt.cli
```

Python 同步入口为 `omt.cli.app.main()`；已有事件循环应 `await omt.cli.app.run_cli()`，不能嵌套 asyncio.run。共用核心包已由 `omt.coding_agent` 更名为 `omt.core`，不提供旧包兼容导入；此前的 session/runtime 模块与 `CreateAgentSession` 也已移除。低层会话对象为 `omt.core.conversation.Conversation`，默认不显示过程。

## 启动配置

CliSettings 使用 `OMT_CLI_` 前缀，与 `OMT_API_` 独立：

| 变量 | 默认值 / 约束 |
| --- | --- |
| `DB_PATH` | `~/.omt/agent/state/omt.sqlite`，文件与 sessions 表须存在 |
| `WORKSPACE_PATH` | 启动 cwd；规范化后必须等于实际 cwd，不执行 chdir |
| `MAX_CONCURRENT_RUNS` | `1`；共用 Manager 的 run 容量，不是工具并发数；CLI 本身顺序输入 |
| `SKILL_ROOTS` | 未配置时使用用户和项目 Skills；JSON 数组 `[]` 关闭发现 |
| `MCP_SERVERS` | 为兼容保留旧 CLI 的 knowledge stdio 配置；JSON 对象可替换，`{}` 关闭 |

现有 knowledge 默认指向本机 `/home/laterain/Projects/Agent/rag-mcp` 的虚拟环境与 `rag_mcp.server`，不是可移植部署配置。其他机器应显式替换或关闭；HTTP 默认不启用它。MCP 字段与类型见 [HTTP 启动配置](api.md#启动配置)，但 CLI 使用自己的环境变量前缀。

```bash
# 例如关闭旧 CLI 的本机 MCP；不影响 HTTP 配置。
export OMT_CLI_MCP_SERVERS='{}'
export OMT_CLI_DB_PATH="$HOME/.omt/agent/state/omt.sqlite"
uv run omt
```

MCP 命令、环境和 headers 属于可信管理员配置，不应包含仓库中的真实凭据。默认启动会读取用户模型认证，不能把启动 CLI 当作无副作用验证命令。

## 命令与会话

- `/exit`、`/quit`、EOF/Ctrl-D：退出，不提交模型。
- `/resume` 或 `resume`：只列出当前工作目录的会话。0 是当前选择，其他编号按最近修改时间排序；未登记时显示“新对话”。
- `/resume N` 或 `resume N`：使用最近展示的编号恢复，成功后显示历史，并清空旧编号。再次切换须重新列出；0 无需恢复。
- 命令名忽略大小写；合法普通输入保留首尾空白和换行。输入须为 1～32000 字符、非全空白且无 NUL。`resume` 为保留命令词，但 `resume-help`、未知命令仍作为普通消息。

首条普通消息执行前才登记索引，以原文前 10 字符为标题；直接退出或只列出历史不会留下空会话。登记失败时本轮尚未执行，可再输入重试。恢复与成功回合都会刷新修改时间，只查看列表或历史不刷新。更新时间失败只展示警告，不把成功回合误报为失败，不应因此重跑工具。

恢复通过 Manager 复查目录归属与完整终态，成功后才切换 CLI 当前 ID。无检查点的已登记会话视为空会话；已有但未完成的检查点不能直接继续。

## 失败、取消与边界

- 运行失败/取消可能已经产生工具或检查点副作用。同一 Manager 会阻止该会话直接再次运行或恢复；可选择其他会话，或退出后人工检查。没有自动修复、重置、幂等重试或持久化失败台账。
- Ctrl-C 在输入或执行阶段均结束整个 CLI，而非取消一轮后继续输入；清理流和 Manager 后关闭 SQLite 连接。嵌入式调用的外部任务取消仍向上传播。
- CLI 只展示过程事件一次，不再次打印最终 answer。历史不展示系统消息、artifact 或模型元数据，但正文和工具参数没有通用脱敏。
- CLI/API 共用实现不代表独立进程共享内存互斥。不要同时用两个入口或多个进程写同一会话；没有跨进程锁。
- 工具有当前系统用户权限，不提供文件沙箱、审批或副作用回滚。同步工具线程或外部进程可能不能立即取消。

离线验证见 [CLI 测试](tests/cli.md)、[/resume 测试](tests/resume.md)和 [Conversation 测试](tests/conversation.md)。
