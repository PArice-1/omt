# 内置系统提示词测试说明

## 对象与目的

- 提示词：[system.md](../../src/omt/core/prompts/system.md)。
- 加载源码：[prompts/__init__.py](../../src/omt/core/prompts/__init__.py)。
- 测试：[test_prompts.py](../../tests/core/test_prompts.py)。
- CLI/HTTP 共用接入：[bootstrap.py](../../src/omt/core/bootstrap.py)；集成验证见 [CLI 测试](cli.md)与 [HTTP 测试](api.md)。

基础提示词以 Markdown 包资源保存，`load_system_prompt() -> str` 读取 UTF-8 文本；bootstrap 在初始化模型之前加载，由 Manager 仅向空会话的 `Conversation(system_prompt=...)` 传递。不在执行核心内嵌大段文本，不在 Agent 和 Conversation 中重复配置；恢复已有历史不覆盖原规则。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 正常资源与 cwd 隔离 | 切换到临时目录，并在 cwd 创建同名 system.md | 仍读取包内 OMT 提示词，包含现有工具指引，不采用 cwd 的替代文本 | test_load_system_prompt_uses_bundled_resource_outside_project |
| 格式保留 | 包资源入口替身指向含中文、段落、首尾空白的临时文本 | 返回原始文本，不通过 strip 改写格式 | test_load_system_prompt_preserves_unicode_and_formatting |
| 空内容 | 空串、单空格、换行/制表符 | 明确抛 ValueError，不静默退化成仅 Skills 目录 | test_load_system_prompt_rejects_empty_resource |
| 文件缺失 | 临时资源目录中没有 system.md | FileNotFoundError 向上传播，由启动阶段失败处理 | test_load_system_prompt_propagates_missing_resource |
| 编码无效 | 非 UTF-8 字节 | UnicodeDecodeError 向上传播，不替换乱码或使用隐藏默认值 | test_load_system_prompt_rejects_invalid_utf8 |
| 无隐式缓存 | 两次调用之间更改临时资源文件 | 第二次返回新内容；既有会话本身仍不会热重载 | test_load_system_prompt_rereads_resource_without_cache |

## 隔离方式

正常路径只读仓库自带的包资源，验证核心包更名后仍能从 `omt.core.prompts` 读取原提示词，不受 cwd 影响。异常场景将 `importlib.resources.files` 的模块入口替换为 `tmp_path`，替身同时断言资源锚点为 `omt.core.prompts`，从公开加载函数验证格式保留、缺失、编码及空内容处理；不修改真实资源或用户提示词。

共享 fixture 隔离 settings.root 与 cwd。测试不创建模型、不读取真实认证、不访问网络，也不执行交互入口。

## 执行

```bash
uv run pytest -q tests/core/test_prompts.py tests/cli/test_lifecycle.py tests/core/test_conversation.py --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/core/prompts/__init__.py src/omt/core/bootstrap.py tests/core/test_prompts.py tests/cli/test_lifecycle.py
git diff --check
```

分发验证可另将 wheel 构建到临时目录，检查其中存在 `omt/core/prompts/system.md`，并在独立 Python 进程把该 wheel 加到 sys.path 后调用加载函数。此检查用于确认实际包资源可读，不启动 CLI 或调用模型；构建命令为 `uv build --wheel --out-dir <临时目录>`。

## 覆盖限制

- 验证资源选择、错误处理和消息传递，不证明模型一定遵循提示词；权限约束仍需实际工具机制保障。
- 不支持自动读取用户或项目中的 system.md、提示词覆盖配置、模板变量或热重载。
- 正常资源测试不固定整段提示词快照，允许调整措辞；内容审查需结合实际工具能力，避免写入尚不存在的沙箱或审批能力。
- wheel 验证属于额外分发检查，不替代在所有目标平台上的安装测试。
