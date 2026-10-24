# 模型登录、配置持久化与无模型 CLI 测试说明

## 对象与目的

- core：[model_configuration.py](../../src/omt/core/model_configuration.py)、[store.py](../../src/omt/core/store.py)。
- CLI：[login.py](../../src/omt/cli/login.py)、[app.py](../../src/omt/cli/app.py)。
- 测试：[test_model_configuration.py](../../tests/core/test_model_configuration.py)、[test_login.py](../../tests/cli/test_login.py)。
- 厂商协议由 ai/models/deepseek.py 承担，独立的网络限制测试见[模型测试](ai-models.md#厂商模型目录查询)。

防止“终端负责配置持久化且写死 DeepSeek”的耦合；默认模型未导入时仍允许进入 CLI 并提示 /login，输入的 provider、model_name、api_key、base_url 全部交给 core。新增厂商只扩展 ai 模块与单一注册表，不要求修改 CLI/core 分支。

需求调整：此前 cli/setup.py 和 omt configure 的实验实现已替换为 /login。旧 test_setup.py 中的文件补齐、元数据保留、取消、秘密输入、网络错误及已有库保护，分别移到 core、CLI、ai 的新测试；“自动向导”“强制官方 URL”“重选已有默认模型”的预期不再保留，因为现在明确要求输入 /login、自定义 base_url 和只补齐缺失默认字段。

## core 场景

下表均对应 test_model_configuration.py，通过公开类型及函数验证，不依赖 CLI 输入。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 全新登录 | 配置目录不存在，提交合法 ModelLogin | 生成三份 JSON，公开读取器可读回厂商、ID、Key、用户 URL；不写数据库、不发聊天/目录请求；POSIX auth 权限 0600 | test_first_login_creates_three_files_without_network_or_database |
| 不完整默认选择 | settings 分别为空、仅 provider、仅 model、null/空白 | 只补齐缺失字段，最终默认模型就绪 | test_login_fills_missing_default_fields |
| 添加其他模型 | 默认已配置且缓存；目录有自定义 URL/思考映射/价格/其他厂商，auth 有 label/其他认证 | 新模型追加，默认配置字节不变、思考等级保留；旧目录和其他厂商不丢失；同厂商 Key 更新，认证额外元数据保留 | test_additional_model_preserves_default_and_existing_metadata_and_updates_provider_key |
| 重复登录同 ID | 已有模型和思考映射 | 更新 URL，不重复追加、不丢映射 | test_repeated_login_updates_url_without_duplicate_or_losing_thinking_mapping |
| 缺失单份文件 | 成功登录并读取缓存后，分别删除三份文件之一 | 检查返回未就绪，再次登录补齐且公开设置读取正确 | test_login_repairs_missing_file_and_invalidates_cached_settings |
| 现存 auth 损坏 | 非法 JSON 或错误字段类型，带测试敏感标记 | 安全错误，完整格式化异常不含标记，原文件不覆盖、其他文件不生成 | test_invalid_existing_auth_is_rejected_without_overwrite_or_secret_in_traceback |
| 输入非法 | 未注册厂商、空模型名/Key、非 HTTP URL、URL 含认证/查询参数 | 类型校验拒绝，不回显敏感值、不写任何文件 | test_login_input_validation_hides_sensitive_values_and_has_no_side_effects |
| 不兼容的已有选择 | 当前厂商 OAuth、同 ID 声明其他厂商，或其他默认厂商尚缺模型 | 保存前拒绝，所有现存文件字节和文件集合保持不变 | test_login_rejects_incompatible_existing_configuration_before_writing |
| 已缓存默认值更新 | 缓存 first 后外部把默认选择改为 second，再登录导入 second | 清除旧缓存，公开 load_settings 返回 second | test_successful_login_invalidates_previously_cached_default_selection |
| 扩展厂商 | 临时注册 second，提供假初始化和目录函数 | 厂商列表自动包含 second；查询收到原始连接参数，配置可持久化并就绪；初始化函数未调用，无需修改 core/CLI | test_new_provider_routes_catalog_and_persistence_without_core_changes |
| 显式库初始化 | 库不存在，或已有只含自定义表的库 | 缺失时建索引后可由真实 SessionStore 查询；已有时字节不变，不自动迁移补表 | test_explicit_database_initialization_only_creates_missing_files |

## CLI 场景

下表均对应 test_login.py，除终端专项外使用脚本输入，默认入口链路使用真实 bootstrap、Manager、图和 SQLite。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 无模型进入 CLI | 无配置无库，依次普通消息、/resume、/thinking、/exit | 提示 /login，不初始化模型、不生成配置/数据库、不执行普通消息 | test_missing_configuration_enters_cli_and_only_prompts_login_without_writes |
| 首次登录后对话 | /LoGiN，输入厂商/自定义 URL/Key/模型并确认，再普通消息 | 保存的选择和 SDK 收到的参数一致；同一 CLI 能回答；只登记普通消息，Key 不打印 | test_first_login_passes_all_parameters_to_core_and_can_chat_immediately |
| 已有会话中添加模型 | 已有默认选择和真实库，一轮消息、/login 添加另一 ID、第二轮消息 | 默认文件字节不变，模型只初始化一次，会话仅一条；第二轮历史保留第一轮且不含登录参数 | test_login_during_chat_adds_model_without_changing_default_runtime_or_history |
| 只登录后退出 | /login 成功后 /exit | 配置和缺失库可生成，但无空会话、无模型请求 | test_login_command_itself_never_registers_empty_session |
| 可选目录查询 | 同意查询，core 替身返回两个 ID，提交其中之一 | 经 core 查询，不在 CLI 实现 HTTP；目录只保存提交的 ID | test_catalog_is_optional_and_routes_through_core |
| 目录查询失败 | core 替身抛含敏感标记的异常 | 不显示异常原文，允许手动登录成功 | test_failed_catalog_allows_manual_login_without_disclosing_exception |
| 保存前取消 | 确认时输入 n、EOF、KeyboardInterrupt、CancelledError | n/EOF 返回未登录，其他取消传播；无配置或数据库副作用 | test_cancelled_login_has_no_configuration_or_database_side_effects |
| 未支持厂商 | 首个 provider 不在列表 | 索取 Key 前拒绝，不写文件 | test_unknown_provider_is_rejected_before_asking_for_secret |
| CLI 接受新增厂商 | 仅注册 second，输入该厂商参数 | CLI 自动列出并接受 second，core 保存后默认厂商正确，无需修改 CLI | test_login_lists_and_accepts_new_registered_provider_without_cli_changes |
| 命令行内参数 | /login bad-argument，再退出 | 给用法提示，不交给模型、不保存 | test_login_with_arguments_is_not_sent_to_model_or_saved |
| 真实终端恢复 | PromptSession + InputValidator + pipe；输入 Key 后 EOF，或查询处直接回车并确认保存 | Key 不在历史/缓冲，恢复历史对象和普通显示、原校验器；直接回车选择否不被普通消息非空校验阻挡 | test_real_terminal_key_is_not_in_history_and_password_mode_is_restored |
| 登录后初始化失败 | SDK 替身抛含敏感标记的异常 | 配置已保存但不泄露错误原文；CLI 仍可输入 /login/退出，普通消息继续被阻止 | test_initialization_failure_after_login_keeps_cli_available_and_hides_error |

## 隔离方式

- core 使用共享 fixture 隔离 settings.root/cwd，CLI 使用对应共享 fixture 隔离启动环境、MCP 和 Skills；设置缓存前后清除。新用例另移除模型选择环境变量，避免环境覆盖测试默认值。
- 所有 JSON、数据库和工作目录都在 tmp_path，使用假 Key 和保留测试域名。登录保存不调用网络；目录路由专项提供本地函数，ai HTTP 测试仅使用内存响应。
- CLI 集成只替换 DeepSeek SDK 初始化，返回记录历史的本地假模型，保留真实内置工具定义、Manager 和 SQLite 检查点，不执行外部工具/MCP。
- 秘密输入回归使用真实 PromptSession、内存 pipe、InMemoryHistory 和 DummyOutput，具有五秒超时，不依赖用户 TTY。其他输入用 AsyncMock，不占用终端。

## 执行

```bash
uv run pytest -q tests/core/test_model_configuration.py tests/cli/test_login.py tests/ai --tb=no
uv run pytest -q tests/cli tests/core/test_store.py tests/core/test_bootstrap.py --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/core/model_configuration.py src/omt/core/store.py src/omt/ai/models_manager.py src/omt/ai/models/deepseek.py src/omt/cli/login.py src/omt/cli/app.py tests/core/test_model_configuration.py tests/cli/test_login.py tests/ai/test_model_catalog.py
```

## 覆盖限制

- 不证明真实服务可达、Key 有效、远程 ID 兼容工具调用；当前只有真实 DeepSeek 适配，second 仅用于扩展性隔离测试。
- 只提供 API Key 登录；auth 的现有 JSON 结构为每厂商一个 Key，不支持同厂商每模型独立凭据。
- 新增模型不会自动切换已有默认选择或运行模型，不测试未实现的 /model/OAuth/HTTP 登录功能。
- 配置逐文件原子替换，没有跨文件/数据库事务或跨进程锁；不模拟断电、磁盘满和并发登录。
- 显式初始化不修改已有库，不验证用户已有库的实际结构；0600 只在 POSIX 下断言，内存终端不覆盖所有真实 TTY。
