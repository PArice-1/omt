# Skills 发现与提示词测试说明

## 对象与目的

- 源码：[skills.py](../../src/omt/coding_agent/skills.py)。
- 测试：[test_skills.py](../../tests/coding_agent/test_skills.py)。
- 公开调用方式：`discover_skills(roots)` 返回 `SkillDiscoveryResult`，`render_skills_prompt(skills)` 返回 Markdown 字符串。
- 保证发现结果保留 Skill 文件的绝对路径，模型目录只包含元数据与加载规则，全文仍可通过已有 read 工具按需取得。
- 本说明覆盖独立发现与渲染模块；Conversation 现已消费目录并初始化系统消息，接入测试与契约另见 [Conversation 测试说明](conversation.md)。工具集合未改变。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 正常发现与定位 | 临时目录中的嵌套 SKILL.md、相对根目录、普通 Markdown 文件 | 只发现 SKILL.md，结果按名称排序，保存绝对路径 | test_discover_skills_recurses_and_sorts_with_absolute_paths |
| YAML 与编码 | UTF-8 BOM、CRLF、块状多行描述、额外 metadata、正文含分隔符 | 正确提取头部，保留描述语义，不将正文当头部解析 | test_discover_skills_accepts_bom_crlf_multiline_yaml_and_body_delimiters |
| 无效文件隔离 | 缺少分隔符、非法 YAML、非映射头部、字段缺失/类型错误/空白、非法名称、超长字段、Python 对象标签；另有合法文件 | 无效文件被跳过并诊断，合法文件仍被发现；不接受 YAML 对象构造 | test_invalid_skills_are_diagnosed_without_blocking_valid_files |
| 长度与空白边界 | 名称恰好 64 字符、描述恰好 1024 字符；字段两端带空白 | 长度上限可接受，元数据两端空白去除 | test_discover_skills_accepts_field_length_boundaries、test_discover_skills_trims_metadata_whitespace |
| 跨目录重名 | 用户根目录在前、项目根目录在后，同名但内容不同 | 后发现的版本覆盖先发现的版本，诊断包含两个路径 | test_later_roots_override_duplicate_names_with_diagnostic |
| 同目录重名 | 不同子目录声明同一名称 | 按路径排序，后发现的版本获胜，结果可重复 | test_duplicate_names_within_root_use_sorted_path_order |
| 重复与重叠扫描 | 重复根目录、父子根目录；覆盖发生后再次传入旧目录 | 每个真实文件路径只处理一次，不产生自冲突或恢复旧版本 | test_overlapping_and_repeated_roots_do_not_report_self_conflicts、test_repeated_root_does_not_restore_an_overridden_version |
| 非文件条目 | 名为 SKILL.md 的目录，或临时 FIFO | 不打开 FIFO，不把目录作为文件读取；返回诊断，其他文件继续发现 | test_directory_named_skill_is_rejected_without_blocking_nested_skills、test_non_regular_skill_is_rejected_without_opening_fifo |
| 空目录与缺失目录 | 无根目录、空目录、未创建目录 | 返回空结果，无诊断 | test_empty_and_missing_roots_are_normal |
| 根路径错误 | 把普通文件当作根目录，或扫描替身抛 PermissionError | 报告根路径，继续扫描后续有效目录 | test_file_root_is_reported_and_other_roots_are_scanned、test_scan_error_does_not_block_later_roots |
| 读取错误 | 非 UTF-8 文件，或读取替身抛 PermissionError | 跳过问题文件并报告路径，保留其他结果 | test_invalid_encoding_is_reported_without_file_contents、test_read_error_does_not_block_other_skills |
| 诊断不回显原文 | 非法 YAML 中包含测试私有标记 | 报告路径和原因，不包含标记或 YAML 原文 | test_yaml_error_diagnostic_does_not_expose_content |
| 显式搜索位置 | 临时 HOME 中有 Skill，但传空根列表；或显式传入含 `~` 的目录 | 不默认扫描 HOME/cwd；显式路径正确展开 | test_discover_skills_does_not_scan_implicit_home_or_cwd、test_discover_skills_expands_explicit_home_path |
| 无隐式缓存 | 首次发现后修改文件，再次调用 | 返回更新的描述 | test_discover_skills_rereads_changes_without_cache |
| 目录提示词 | 空列表；或发现后删除原文件再渲染 | 空列表返回空串；非空结果包含名称、描述、路径与加载规则，不重新读取正文 | test_render_skills_prompt_is_empty_without_skills、test_render_skills_prompt_contains_metadata_and_loading_rules_only |
| 多行描述渲染 | 描述包含换行、缩进和制表符 | 目录中的用途压成单行，不改变 SkillDefinition | test_render_skills_prompt_flattens_multiline_descriptions |
| 完整正文读取 | 超过 50 行的 Skill；发现后切换 cwd | 使用发现的绝对路径，以 offset=1、limit=False 调用真实 read 工具，取得包括末尾指引的完整正文 | test_discovered_location_can_load_full_skill_with_existing_read_tool |

## 隔离方式

所有根目录、Skills、HOME 替身与 FIFO 均来自 `tmp_path`，不扫描真实用户 Skills、不读认证、不请求模型或网络。共享配置 fixture 同样生效，保证 `settings.root` 定位到临时目录并清理缓存。

权限异常使用局部 `monkeypatch` 模拟，避免依赖测试用户是否具有管理员权限；用例结束自动恢复。FIFO 使用共享 fixture，只创建和检查，不打开；不支持 FIFO 的平台明确跳过相关用例。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_skills.py --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/coding_agent/skills.py tests/coding_agent/test_skills.py
git diff --check
```

## 覆盖限制

- 本组测试不验证 Conversation 接入或模型是否自主选择正确 Skill；独立读取链路直接调用公开 read 工具，Conversation 的假模型加载链路见 [Conversation 测试说明](conversation.md)。
- 仅校验基础 name/description 字段，不实现其他 frontmatter 字段的运行时语义。
- 不提供信任审批、路径沙箱、文件大小上限或提示词预算；调用方应只传入可信根目录。文件符号链接可指向根目录外，目录扫描也不是安全隔离。
- 不保证并发文件修改、极深目录或恶意超大 YAML 下的资源上限。底层 rglob 对部分系统扫描错误可能自行跳过；测试验证的是传播到模块的异常处理。
- 不运行 Skill 中的脚本，不验证显式命令、热重载或安装机制。
