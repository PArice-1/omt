# OMT

一个轻量级的 Python AI Agent 应用，支持工具调用、会话持久化，以及 CLI 和 HTTP API 两种交互方式。

OMT 基于 LangChain / LangGraph 构建：由模型与工具循环完成任务，由应用层管理会话、历史记录和运行生命周期。你可以在终端中与它交互，也可以通过本机 API 将它接入其他程序。

> 项目仍在开发中，目前面向可信的本机、单用户场景。内置工具能够读写文件和执行 Bash，**没有沙箱或执行前审批**；请在可信目录使用，并注意模型请求可能产生费用。

## 功能

- **交互式 TUI**：Textual 全屏聊天、Markdown、折叠工具输出和模型/会话/推理选择；`--plain` 保留逐行界面。
- **本机 HTTP API**：首次配置模型、临时切换模型/思考等级、创建会话及查询历史；保留 JSON 响应并支持正文/reasoning/工具 SSE 事件。当前免 Bearer 认证。
- **持久化会话**：使用 SQLite 保存会话索引和 LangGraph 检查点；CLI 可通过 `/resume` 恢复已完成的历史会话。
- **内置工具**：提供 `read`、`write`、`edit` 和 `bash`，支持文件读取、写入、精确替换及命令执行。
- **MCP 扩展**：通过启动配置接入 `stdio` 或 `streamable_http` MCP 服务。
- **Skills 发现**：发现可信目录中的 `SKILL.md`，将名称、描述和路径提供给模型，由模型按需读取正文。
- **共用核心**：CLI 与 API 共用模型配置、工具组装和会话管理逻辑。

当前模型提供商已注册 **DeepSeek** 和 **Ollama**。Ollama 底层适配使用 `langchain-ollama` 的原生本地推理协议，默认地址为 `http://localhost:11434`，支持无 Key 的 Python 初始化及已下载模型查询；CLI/TUI/HTTP 首次配置均支持免 Key 选择 Ollama，本地模型不需要账号、凭据或占位 Key。增加其他提供商仍需要扩展代码，并非修改配置即可支持任意模型。

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

直接运行 `uv run omt` 进入 TUI。默认模型、认证或模型目录不完整时，提示按 **F2** 或输入 `/login`，不会自动启动配置向导或发送模型请求。用方向键/鼠标选厂商：DeepSeek 首次输入隐藏 Key，Ollama 跳过 Key 直接查询本地已下载模型，选择模型即保存。请先自行启动 Ollama 并下载需要的模型。

下面是可选的 `uv run omt --plain` 逐行模式示例：

```text
You> /login
支持的厂商：deepseek、ollama
1. deepseek（未登录）
2. ollama（无需 API Key）
provider（编号或名称）> deepseek
api_key> （隐藏输入，仅首次需要）
1. <厂商返回的模型 ID-A>
2. <厂商返回的模型 ID-B>
model（编号或名称）> 1
```

`/login` 可以随时使用：选厂商 → 需认证且未登录时输入 Key（Ollama 跳过）→ 选择模型，随后自动保存，不再询问地址、是否拉取或是否保存。已登录厂商显示“已登录”并复用凭据，例如在同厂商的 Pro/Flash 间选择无需重新输入 Key；TUI 用选择列表，逐行模式支持编号和名称，模型 ID 以厂商实际返回列表为准。

地址由对应厂商模块维护，DeepSeek 默认使用 [deepseek.py](src/omt/ai/models/deepseek.py) 的官方地址。每次 `/login` 自动拉取该厂商完整目录，全部导入 `model-store.json`，并非只保存选中的模型；已登录时刷新失败可使用本地缓存，首次获取失败则提示重试。保留其他厂商、旧目录元数据和自定义思考映射；厂商模块为已确认的 deepseek-flash/deepseek-v4-pro 自动补齐 low/high/max，未知 ID 不猜测能力。旧配置缺映射时，重新启动后再次 /login 选择模型即可补齐，无需重输 Key。普通启动仅本地检查，不自动拉取目录。

CLI 只收集输入，core 校验、合并相关 JSON 并更新默认模型。Ollama 仅保存 settings/model-store，不创建或修改 auth.json，已有其他厂商认证和旧占位 Key 保持原样，但不使用旧 Ollama Key。首次完成登录后创建缺失数据库，即可继续发消息；已有运行环境会立即切换模型，保留会话和历史。切换失败时当前运行保留原模型，已保存的默认选择仍生效于后续启动。需认证厂商共用一个 Key，已保存的凭据不会因选模型而替换；选择完成前取消/EOF 不落盘，损坏配置不自动覆盖。

以下是**手动配置**的格式参考；使用 `/login` 时无需自行创建。手动编辑前先创建 `~/.omt/agent`，已有文件请合并修改，不要直接覆盖。

**`~/.omt/agent/settings.json`**：选择提供商和模型。

```json
{
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-chat"
}
```

**`~/.omt/agent/auth.json`**：配置需要认证的提供商（如 DeepSeek）的 API Key；只用 Ollama 时无需创建。

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

需认证厂商当前仅支持 `api_key`，Ollama 则免认证；不提供 OAuth，也不会在缺失凭据时自动回退到环境变量中的 Key。`baseUrl` 是凭据的发送目标，请只使用可信地址，**不要将真实凭据提交到仓库**。

### 3. 初始化会话数据库

未加载模型的 CLI 在 `/login` 保存并补齐默认配置后，会创建**缺失的**数据库和索引表，再初始化运行环境；已存在的数据库不修复、不迁移。已有完整模型配置的正常启动仍要求预建数据库。若使用手动配置方式，或已有空库缺少索引表，可按需执行下面的初始化脚本：

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

### 4. 启动 TUI

```bash
# 默认全屏 TUI，不启动 MCP；缺少模型配置时提示 F2 / /login。
uv run omt
# 可选：保留原逐行交互
uv run omt --plain
```

也可以使用 `uv run python -m omt.cli`。

进入交互后，直接输入自然语言请求，例如：

```text
请分析当前项目的目录结构，暂时不要修改文件。
```

界面以对话为主，不再使用大按钮工具栏。输入 `/` 会显示可筛选的指令提示；↑↓ 选择、Tab 补全、Enter 执行，Esc 收起。[查看界面预览](docs/cli.md#默认-tui)。

常用操作：

| 快捷键 / 命令 | 作用 |
| --- | --- |
| Enter / Ctrl-J | 发送 / 换行 |
| F2 / `/login` / `/model` | 选择厂商及模型（仅需认证厂商首次隐藏输入 Key）；复用认证、获取完整目录、自动保存并切换 |
| F3 / `/resume` | 选择并恢复当前工作目录的历史会话 |
| F4 / `/thinking` | 查看并选择当前进程的推理等级 |
| Ctrl-N / `/new` | 新对话，不立即创建空记录 |
| ↑↓ | 在首/末行回看已发送输入，保留未发送草稿 |
| Esc | 收起指令提示，或停止操作/取消弹窗；不回滚已有副作用 |
| Ctrl-Q / Ctrl-C / `/exit` | 清理资源并退出 |
| `/help` | 查看操作指南 |

逐行模式保留 `/resume N` 编号选择、裸词 `resume` 和 EOF/Ctrl-D 退出；TUI 使用选择弹窗而非编号，并拒绝未知斜杠命令。方案比较见 [GitHub TUI 调研](docs/research/tui-frameworks.md)。

首条普通消息发送前才登记新会话，仅查看列表或退出不会留下空会话。恢复历史不会自动重新执行工具。

文件工具和 Bash 以**进程当前工作目录**为基准。若要在其他项目中使用，先切换到目标目录，再运行：

```bash
uv run --project /absolute/path/to/OMT omt
```

如需 MCP，请显式设置 `OMT_CLI_MCP_SERVERS`，不再内置开发者本机路径。更多交互与失败处理约定见 [CLI 使用说明](docs/cli.md)。

## HTTP API

在可信目标工作目录启动；未配置时也可通过 API 预览并确认模型，首次确认后初始化缺失数据库：

```bash
# 当前本机分支无需配置访问令牌。
uv run omt-api
```

默认监听 `127.0.0.1:8000`，使用单 worker。API 默认不启用 MCP；当前已暂时停用访问令牌校验，即使配置令牌也不会自动恢复认证。DeepSeek 等认证厂商仍需配置模型 Key，Ollama 无需。后端及前端代理均只应面向可信本机使用，不要向不可信网络开放。

先通过 `GET /api/v1/runtime` 确认 ready=true；未就绪时按 API 文档完成配置。就绪后可以检查服务并创建会话：

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

原 `/runs` 等待本轮结束后返回最终回答；使用 `curl -N` 请求 `/runs/stream` 可持续接收正文、reasoning 和工具事件。`GET /api/v1/runtime` 查询当前配置，`GET /api/v1/models` 获取本地快捷模型列表；快速切换不保存配置。断开流式连接会尝试取消，不回滚工具副作用。完整请求、事件和错误见 [HTTP 使用说明](docs/api.md)与[前端接入 API 文档](docs/frontend-api.md)。`/docs`、`/redoc` 和 `/openapi.json` 已开放；没有后台任务或独立取消接口。

## 配置与扩展

模型配置由上述三个 JSON 文件管理；入口启动选项使用环境变量，CLI 前缀为 `OMT_CLI_`，API 前缀为 `OMT_API_`。

| 后缀 | 用途 |
| --- | --- |
| `DB_PATH` | SQLite 文件路径；默认 `~/.omt/agent/state/omt.sqlite`，CLI 首次 `/login` 或 API 确认首次配置后可初始化缺失库；正常启动不修复已有库 |
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
- HTTP 无持久化后台任务、独立取消接口或断线重连补发；流式断线仅尝试取消，JSON 断线不保证取消。

## 文档

- [文档导航](docs/README.md)
- [CLI 使用说明](docs/cli.md)
- [HTTP API 使用说明](docs/api.md)
- [架构与模块职责](docs/architecture.md)
- [测试说明与索引](docs/tests/README.md)
