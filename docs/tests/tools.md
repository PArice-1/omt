# 工具、注册与文件查找测试说明

## 对象与目的

文件工具、Bash 工具通过 LangChain 的 `tool.invoke()` 验证；不绕过参数 schema 直接调用内部函数。文件和工作目录均来自 tmp_path。

| 源码 | 测试 |
| --- | --- |
| [read.py](../../src/omt/coding_agent/tools/read.py) | [test_read.py](../../tests/coding_agent/tools/test_read.py) |
| [write.py](../../src/omt/coding_agent/tools/write.py) | [test_write.py](../../tests/coding_agent/tools/test_write.py) |
| [edit.py](../../src/omt/coding_agent/tools/edit.py) | [test_edit.py](../../tests/coding_agent/tools/test_edit.py) |
| [bash.py](../../src/omt/coding_agent/tools/bash.py) | [test_bash.py](../../tests/coding_agent/tools/test_bash.py) |
| [tools_manager.py](../../src/omt/coding_agent/tools_manager.py) | [test_tools_manager.py](../../tests/coding_agent/test_tools_manager.py) |
| [find_files.py](../../src/omt/coding_agent/utils/find_files.py) | [test_find_files.py](../../tests/coding_agent/utils/test_find_files.py) |

## 1. 文件工具场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| read 路径与文本 | UTF-8、Path/str、相对路径、临时 HOME 下的 ~ 路径 | 全文原样返回，相对路径解析正确 | test_read_returns_utf8_content、resolves_relative_path、expands_home_path |
| read 偏移与空文件 | 一基 offset、末行无换行、空文件、超出末尾 | 从指定行读取，不补换行；空文件和超出范围返回空串 | offset/empty/past_end 用例 |
| read 行数限制 | 超过/不足 50 行，设置 offset，limit=True/False | True 从 offset 起最多 50 行，False 不截断，不补足短文件 | limit 系列用例 |
| read 拒绝非法输入 | 缺文件参数、非法 offset/limit、缺文件、目录或 FIFO | 相应校验或路径错误，不把非普通文件当文本打开 | missing/invalid/directory/non_regular 用例 |
| write 创建与覆盖 | 新文件、已有文件、空文本 | UTF-8 写入；覆盖而非追加；空文本可建空文件或清空旧文件 | creates/overwrites/empty_content 用例 |
| write 拒绝非法目标 | 父目录缺失或父路径为文件，目标为目录/FIFO | 拒绝写入，不自行创建父目录 | missing_parent/parent_path/directory/non_regular 用例 |
| write 错误参数 | 内容缺失或类型非法，目标原来已有文本 | 拒绝操作，原文件内容保持不变 | invalid_content、missing_content 用例 |
| edit 合法替换 | 唯一匹配，或 replace_all=True 的多匹配，或空替换串 | 精确替换、授权全部替换或删除匹配文本 | replaces_unique、replaces_all、empty_replacement 用例 |
| edit 拒绝与不变性 | old_string 为空、无匹配、多匹配未授权、类型非法、缺 replace_all | 拒绝操作且原文件不变 | rejected_edit_preserves_original_content、invalid_parameters_preserve_file、missing_replace_all 用例 |
| edit 路径错误 | 缺文件、目录、FIFO | 报错，不创建缺失目标，不修改其他文件 | missing/directory/non_regular 用例 |

这些用例验证当前工具行为，不证明跨进程原子写入、权限隔离、符号链接防护或文件系统事务。

## 2. Bash 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 标准流与退出码 | printf 输出 stdout/stderr 后 exit 7 | 分离捕获文本，非零退出码作为结果返回，不当成 Python 调用失败 | test_bash_captures_stdout_stderr_and_nonzero_exit_code |
| 工作目录 | 默认 cwd、含中文/空格的目录 | pwd 返回预期目录，不因空格拆分路径 | defaults_to_current_directory、handles_working_directory 用例 |
| Shell 能力 | 管道与重定向写入临时文件 | 执行成功，输出及临时文件内容正确 | test_bash_supports_pipes_and_redirection |
| 参数与环境错误 | 空白/NUL 命令，timeout 非整数、布尔、越界，cwd 缺失/为文件，Bash 不存在 | 执行前拒绝或明确报不可用 | invalid_command、invalid_timeout、working_directory、missing_bash 用例 |
| 超时清理 | 输出一行后启动 sleep 子进程并等待，timeout=1 | timed_out=True，保留先前输出，非零退出；在测试时间上限内返回而非等 sleep 结束 | test_timeout_terminates_process_group_and_keeps_partial_output |
| 关闭 stdin | 命令尝试从终端读取 | 立即读到 EOF 而不是占用用户输入，未触发超时 | test_bash_does_not_read_from_interactive_stdin |
| 输出截断 | stdout/stderr 分别超过 20000 个 Unicode 字符；另有恰好等于限额的输出 | 每路最多 20000 字符，截断标志准确，不按 UTF-8 字节误切文本 | large_stdout_and_stderr、output_at_limit 用例 |
| 编码与语法错误 | 非 UTF-8 输出；不完整 Shell 语法 | 非法字节替换而不抛解码异常；语法错误以退出码与 stderr 返回 | non_utf8_output、shell_syntax_error 用例 |
| 异常时回收 | 输出读取被模拟为 KeyboardInterrupt 或 RuntimeError | 原异常传播，直接子进程已回收 | test_interrupted_output_capture_terminates_the_process |
| 工具协议 | 用带 tool_call ID 的调用封套执行本地 printf | 形成 ToolMessage，调用 ID 与结构化结果正确 | test_tool_call_returns_tool_message_with_structured_result |
| Agent 集成 | 假模型请求已注册 Bash，再返回总结 | 真实执行本地命令；Session 保存完整工具回合，终端显示工具开始/结果/完成 | test_registered_bash_runs_in_agent_and_preserves_tool_message |

只在 POSIX 且 Bash 可用时执行该测试文件。命令仅操作测试数据，不访问网络或真实凭据。

超时用例用继承输出管道的 sleep 验证常规进程组清理；不证明主动脱离进程组的 daemon 可被回收。输出是完整捕获后截断，测试不证明内存受限，也不覆盖实时日志、审批或沙箱。

## 3. 注册与查找场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 工具集合 | 调用 load_tools | 返回 edit/read/write/bash 的 BaseTool 列表，无重复 | test_load_tools_returns_builtin_langchain_tools |
| 列表隔离 | 连续加载并清空第一份列表 | 第二份及后续加载不受影响 | test_load_tools_returns_independent_lists |
| 路径独立 | 切换到临时目录后加载工具 | 工具仍可加载，并完成 write → read → edit → read 闭环 | test_loaded_tools_work_outside_project_directory |
| glob 查找 | 嵌套测试文件、递归/非递归模式 | 返回排序后的绝对文件路径；非递归只取直属匹配 | test_recursive_search_returns_sorted_absolute_files、test_non_recursive_pattern_only_returns_direct_children |
| 忽略目录 | 任意层级的 .git、.venv、__pycache__ | 忽略这些目录内的文件，但保留其他匹配 | test_ignored_directories_are_filtered_at_every_depth |
| 空结果与非法根目录 | 空目录、无匹配、相对/~ 路径、根目录缺失或为文件 | 正确返回空列表或解析路径；无效根目录拒绝使用 | test_find_files.py 的其余路径/空结果用例 |

find_files 是辅助函数；这些测试不表示注释中的 AST 动态工具扫描已启用。

## 隔离方式与执行

[共享 fixture](../../tests/coding_agent/conftest.py) 隔离配置根目录，FIFO 在不支持 mkfifo 的平台跳过。Bash 使用本地命令，集成测试注入假模型，不请求真实提供商。

```bash
uv run pytest -q tests/coding_agent/tools tests/coding_agent/test_tools_manager.py tests/coding_agent/utils/test_find_files.py --tb=no
```

新增工具时同步更新：工具调用测试、注册集合断言、[Session 组装测试](session-and-config.md) 和本说明的场景表。

## 覆盖限制

工具当前有宿主用户权限。测试通过不表示命令或文件操作对不可信输入安全，不表示失败可回滚文件修改，也不表示任意平台或远程文件系统行为已经验证。
