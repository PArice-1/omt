# OMT

一个轻量级的 Python AI Agent 应用，支持工具调用、会话持久化，以及 CLI 和 HTTP API 两种交互方式。

OMT 基于 LangChain / LangGraph 构建：由模型与工具循环完成任务，由应用层管理会话、历史记录和运行生命周期。你可以在终端中与它交互，也可以通过本机 API 将它接入其他程序。

> 项目仍在开发中，目前面向可信的本机、单用户场景。内置工具能够读写文件和执行 Bash，**没有沙箱或执行前审批**；请在可信目录使用，并注意模型请求可能产生费用。

## 功能

- **交互式 CLI**：逐步显示助手文本、工具调用及执行结果。
- **本机 HTTP API**：创建会话、查询历史和执行一轮对话；当前本机分支无需 Bearer 认证，采用非流式响应。
- **持久化会话**：使用 SQLite 保存会话索引和 LangGraph 检查点；CLI 可通过 `/resume` 恢复已完成的历史会话。
- **内置工具**：提供 `read`、`write`、`edit` 和 `bash`，支持文件读取、写入、精确替换及命令执行。
- **MCP 扩展**：通过启动配置接入 `stdio` 或 `streamable_http` MCP 服务。
- **Skills 发现**：发现可信目录中的 `SKILL.md`，将名称、描述和路径提供给模型，由模型按需读取正文。
- **共用核心**：CLI 与 API 共用模型配置、工具组装和会话管理逻辑。

当前模型提供商仅注册了 **DeepSeek**；增加其他提供商需要扩展代码，并非修改配置即可支持任意模型。

## 快速开始

### 1. 安装

需要 **Python 3.12+** 和 **uv**。Bash 工具使用 POSIX 进程能力，建议在 Linux / macOS 环境运行。

克隆仓库并进入仓库根目录后，安装项目及依赖：

```bash
uv sync
```

### 2. 配置模型

CLI 与 API 共用以下三个文件，默认从用户目录读取，而不是从仓库读取：

```text
~/.omt/agent/
├── settings.json
├── auth.json
├── model-store.json
└── state/
    └── omt.sqlite
```

先创建配置目录：

```bash
mkdir -p "$HOME/.omt/agent"
```

按下面示例创建或编辑文件；如已有配置，请合并修改，不要直接覆盖。

**`~/.omt/agent/settings.json`**：选择提供商和模型。

```json
{
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-chat"
}
```

**`~/.omt/agent/auth.json`**：配置对应提供商的 API Key。

```json
{
  "deepseek": {
    "type": "api_key",
    "key": "YOUR_DEEPSEEK_API_KEY"
  }
}
```

将占位值替换为自己的凭据，并限制文件权限：

```bash
chmod 600 "$HOME/.omt/agent/auth.json"
```

**`~/.omt/agent/model-store.json`**：声明模型 ID 和请求地址。

```json
{
  "deepseek": {
    "models": [
      {
        "id": "deepseek-chat",
        "baseUrl": "https://api.deepseek.com",
        "provider": "deepseek"
      }
    ]
  }
}
```

以上为 DeepSeek 配置示例，模型 ID 请根据实际可用模型调整。`defaultModel` 必须与 `models[].id` 完全一致。文件使用严格 JSON，不接受尾随逗号。

当前仅支持 `api_key` 认证，不提供 OAuth，也不会在缺失凭据时自动回退到环境变量中的 Key。`baseUrl` 是凭据的发送目标，请只使用可信地址，**不要将真实凭据提交到仓库**。

### 3. 初始化会话数据库

当前入口**不会自动创建会话索引表或执行迁移**。首次使用时，在终端执行下面的初始化脚本：

```bash
uv run python - <<'PY'
import sqlite3
from pathlib import Path

path = Path("~/.omt/agent/state/omt.sqlite").expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as connection:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            thread_id TEXT PRIMARY KEY NOT NULL,
            workspace_path TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_workspace
            ON sessions(workspace_path, updated_at);
    """)
print(f"会话数据库已就绪：{path}")
PY
```

脚本不会删除已有数据，但也不会修复或迁移已有的不兼容表结构。LangGraph 检查点表由底层库管理，无需在这里手动创建。

### 4. 启动 CLI

```bash
# 当前 CLI 默认保留了开发者本机的 MCP 连接，首次使用请显式关闭。
export OMT_CLI_MCP_SERVERS='{}'
uv run omt
```

也可以使用 `uv run python -m omt.cli`。

进入交互后，直接输入自然语言请求，例如：

```text
请分析当前项目的目录结构，暂时不要修改文件。
```

常用命令：

| 命令 | 作用 |
| --- | --- |
| `/resume` | 列出当前工作目录下的会话；`0` 表示当前选择 |
| `/resume N` | 使用最近一次列表中的编号恢复会话 |
| `/exit` 或 `/quit` | 退出 |
| Ctrl-D / Ctrl-C | 退出；执行中按 Ctrl-C 也会结束整个 CLI |

首条普通消息发送前才登记新会话，仅查看列表或退出不会留下空会话。恢复历史不会自动重新执行工具。

文件工具和 Bash 以**进程当前工作目录**为基准。若要在其他项目中使用，先切换到目标目录，再运行：

```bash
uv run --project /absolute/path/to/OMT omt
```

上面的 MCP 环境变量需要在该终端中保持生效。更多交互与失败处理约定见 [CLI 使用说明](docs/cli.md)。

## HTTP API

完成模型配置和数据库初始化后，在目标工作目录启动：

```bash
# 当前本机分支无需配置访问令牌。
uv run omt-api
```

默认监听 `127.0.0.1:8000`，使用单 worker。API 默认不启用 MCP；当前已暂时停用访问令牌校验，即使配置令牌也不会自动恢复认证。模型 API Key 仍需配置。后端及前端代理均只应面向可信本机使用，不要向不可信网络开放。

可以先检查服务并创建会话：

```bash
curl -sS http://127.0.0.1:8000/health

curl -sS http://127.0.0.1:8000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"分析项目"}'
```

将返回的 `session_id` 保存为 `SESSION_ID`，再发送请求；这一步会调用模型，并可能执行工具：

```bash
SESSION_ID='<上一步返回的 session_id>'
curl -sS "http://127.0.0.1:8000/api/v1/sessions/$SESSION_ID/runs" \
  -H 'Content-Type: application/json' \
  -d '{"message":"请分析当前项目结构，暂不修改文件"}'
```

执行接口等待本轮结束后返回最终回答。会话列表、历史查询、错误码和配置项见 [HTTP 使用说明](docs/api.md)；前端开发或交给 AI 对接时，使用 [前端接入 API 文档](docs/frontend-api.md)。当前源码提供 `/docs`、`/redoc` 和 `/openapi.json`，但尚未实现 SSE、后台任务或取消接口。

## 配置与扩展

模型配置由上述三个 JSON 文件管理；入口启动选项使用环境变量，CLI 前缀为 `OMT_CLI_`，API 前缀为 `OMT_API_`。

| 后缀 | 用途 |
| --- | --- |
| `DB_PATH` | SQLite 文件路径；默认 `~/.omt/agent/state/omt.sqlite`，需预建 |
| `WORKSPACE_PATH` | 工作目录声明，必须与启动时实际 cwd 一致；不会切换目录 |
| `MAX_CONCURRENT_RUNS` | 同一 Manager 的运行上限，默认 `1`，不是工具并发数 |
| `SKILL_ROOTS` | Skills 搜索路径的 JSON 数组；`[]` 关闭发现 |
| `MCP_SERVERS` | MCP 连接配置的 JSON 对象；`{}` 关闭 MCP |
| `ACCESS_TOKEN` | 仅 API 保留兼容字段；当前可不配置，不校验长度，也不启用接口认证 |

当前 CLI / API 的默认 Skills 目录均为 `~/.agents/skills`。可显式指定项目自己的目录，例如在启动 CLI 前设置：

```bash
export OMT_CLI_SKILL_ROOTS='["./skills"]'
```

Skills 仅提供元数据发现与按需读取提示，不会自动执行其中的脚本。MCP 连接参数与 Skills 加载规则分别见 [HTTP 配置](docs/api.md#启动配置)和 [架构说明](docs/architecture.md)。

## 项目结构

```text
.
├── src/omt/
│   ├── agent/   # LangChain Agent 调用及流接口包装
│   ├── ai/      # 模型提供商注册与初始化
│   ├── api/     # FastAPI 入口与路由，当前暂未启用认证
│   ├── cli/     # 终端交互、命令与输出展示
│   └── core/    # 会话管理、存储、配置、工具与 Skills
├── tests/       # 离线测试
└── docs/        # 使用说明、架构与测试说明
```

## 开发与验证

默认测试使用本地假模型和临时文件，不需要真实模型 Key：

```bash
uv run pytest -q --tb=no
git diff --check
```

修改 Python 代码后还需进行静态类型检查；若环境已安装 `basedpyright`，可针对修改路径运行：

```bash
# 将下面的示例路径替换为本次修改的 Python 文件。
basedpyright --pythonpath .venv/bin/python src/omt/core/settings.py
```

协作约定见 [AGENTS.md](AGENTS.md)，测试隔离和用例说明见 [测试文档](docs/tests/README.md)。启动 CLI / API 不是离线测试，不要用真实模型请求代替默认验证。

## 当前边界

- **没有工具沙箱、执行审批或副作用回滚**；工具拥有当前系统用户的文件和进程权限。
- **HTTP 仅面向可信本机调用方**，不要直接暴露公网；当前不支持多用户隔离。
- 会话互斥仅在同一进程内生效，**不要通过多个进程或 CLI / API 同时写同一会话**。
- 已完成会话可以继续；失败或未完成的会话没有自动修复、重置或中断恢复流程。失败、取消或丢失响应后不要盲目重试，以免重复产生工具副作用。
- HTTP 暂无流式事件、持久化后台任务、取消及断线重连能力。

## 文档

- [文档导航](docs/README.md)
- [CLI 使用说明](docs/cli.md)
- [HTTP API 使用说明](docs/api.md)
- [架构与模块职责](docs/architecture.md)
- [测试说明与索引](docs/tests/README.md)
