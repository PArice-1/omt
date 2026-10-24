# 模型登录、完整目录与运行中选择测试说明

## 对象与目的

- core：[model_configuration.py](../../src/omt/core/model_configuration.py)、[bootstrap.py](../../src/omt/core/bootstrap.py)、[store.py](../../src/omt/core/store.py)。
- CLI：[login.py](../../src/omt/cli/login.py)、[app.py](../../src/omt/cli/app.py)。
- 测试：[test_model_configuration.py](../../tests/core/test_model_configuration.py)、[test_login.py](../../tests/cli/test_login.py)。
- 厂商地址、协议和 HTTP 限制见[模型测试](ai-models.md#厂商模型目录查询)。

保证配置处理在 core，厂商地址/协议在 ai，CLI 仅交互。无配置也能进入输入循环等待 `/login`，新增厂商不需要修改 CLI/core 分支。

本次需求替换旧预期：删除输入 URL、是否查询与保存确认；已登录厂商复用 Key，自动拉取完整列表并全部导入。选模型同时更新默认选择并切换当前 Runtime，不再保持旧默认/旧运行模型。取消只发生在选定模型之前；首次获取失败不接受凭空手填 ID，已有认证和目录时可离线选择缓存。这些变化是明确的交互调整，不以放松断言掩盖旧行为。

本次回归补充：此前完整目录仅导入 ID，导致 deepseek-flash 缺少 thinkingLevelMap，只显示 none。现在将厂商模块已确认的能力随目录合并；旧条目缺失/null 时通过再次 /login 补齐，已有自定义映射和显式空表不覆盖。未知 ID 不按名称猜测能力。

## core 场景

下表对应 test_model_configuration.py，经公开类型/函数和配置读取器验证。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 首次完整导入 | 无配置，提交 flash，完整列表含 flash/pro | 三份 JSON 可读回；两个模型均使用厂商模块地址；auth 权限 0600（POSIX）；无目录/聊天请求或数据库副作用；就绪检查和登录状态正确 | test_first_login_saves_entire_catalog_and_provider_url_without_database |
| 已知模型能力及旧配置修复 | 首次导入或已存在但缺映射的 deepseek-flash；完整列表另含 deepseek-v4-pro、相似未知 ID | 两个已知 ID 导入 low/high/max；未知 ID 不填能力，旧价格等字段保留；重复导入字节不变 | test_login_imports_known_thinking_capabilities_and_repairs_old_entries |
| 自定义能力保留 | 已知模型已有 null、空表、自定义 high 或 high→null | 仅 null 补齐官方映射；空表和自定义映射保持原值 | test_known_capabilities_only_fill_missing_or_null_mapping |
| 不完整/不同默认 | settings 为空、只有厂商、只有旧模型或 null/空白 | 显式选择 pro 后默认厂商/模型一致，并就绪，不保留错配的旧字段 | test_selection_fills_or_replaces_default_fields |
| 合并与认证复用 | 已缓存 flash，目录含自定义 URL、思考映射、价格、旧条目和其他厂商；认证有额外字段 | 默认切 pro、缓存失效，思考等级保留；导入项地址来自厂商模块，旧项和元数据保留；认证字节不变 | test_selection_updates_default_but_preserves_metadata_other_providers_and_auth |
| 重复导入 | 完整列表有乱序、重复及首尾空白 ID，再选 pro | 归一、去重，不重复追加；默认选择更新 | test_repeated_import_deduplicates_full_catalog_and_selected_model |
| 缺失单份文件 | 登录并缓存后分别删除三份文件之一 | 检查未就绪；重新保存可补齐、清除设置缓存，公开读回新选择 | test_login_repairs_missing_file_and_invalidates_cached_settings |
| 复用认证与完整列表 | 已登录 flash，查询返回 pro/flash/new-model 和重复项 | 不传新 Key 即可准备，查询收到原 Key，返回全部去重排序 ID；准备过程不修改任何文件 | test_prepare_reuses_key_fetches_all_models_and_does_not_write |
| 未登录缺 Key | 无配置直接请求准备厂商 | 明确要求输入 Key，不创建配置目录 | test_prepare_requires_key_for_first_login_without_writing |
| 获取失败 | 查询异常带测试敏感标记，分别有/无缓存 | 已保存 Key 时可回退完整本地列表并标记缓存；显式新 Key 始终拒绝与旧缓存拼接；无缓存不落盘；不泄露标记 | test_prepare_network_failure_uses_cache_only_for_saved_key |
| 非法或不匹配目录 | 空列表、空白 ID 或列表中无所选模型 | 拒绝保存，不生成文件 | test_empty_invalid_or_unselectable_catalog_is_not_saved |
| 损坏 auth | 非 JSON 或非法认证字段，含敏感标记 | 安全异常及完整 traceback 不含原文，现有文件不覆盖、其他文件不生成 | test_invalid_existing_auth_is_rejected_without_overwrite_or_secret_in_traceback |
| 输入校验 | 未注册厂商、空 ID/Key，或旧 base_url 参数 | 类型校验拒绝，无文件副作用，错误不回显 Key；登录 DTO 不再接受地址 | test_login_input_validation_hides_secrets_and_no_longer_accepts_url |
| 厂商扩展 | 临时注册 second，模块地址与本地目录函数 | 按注册的地址查询并保存完整列表/默认选择及注册的能力映射；不初始化聊天，不改 CLI/core | test_new_provider_defines_its_own_address_without_cli_or_core_changes |
| 不兼容配置 | OAuth 或同 ID 声明其他厂商 | 保存前拒绝，文件集合和字节不变 | test_incompatible_existing_configuration_is_not_overwritten |
| 显式初始化数据库 | 缺失库，或已有仅含自定义表的库 | 缺失时能经真实 SessionStore 查询；已有时字节不变，不迁移 | test_explicit_database_initialization_only_creates_missing_files |

## CLI 与 Runtime 场景

下表对应 test_login.py；默认链路使用真实 bootstrap、Manager、Agent 图和 SQLite。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 无模型启动 | 无配置无库，输入普通消息、/resume、/thinking、/exit | 只提示登录，不初始化 SDK、不生成文件、不执行消息 | test_missing_configuration_enters_cli_and_only_prompts_login_without_writes |
| 首次简化登录后对话 | /LoGiN、厂商、Key、flash、普通消息 | 不问 URL/查询/确认；导入 flash/pro，SDK 收到选择和厂商地址，能立即回答；仅普通消息登记会话，不打印 Key | test_first_login_only_asks_provider_key_and_model_and_saves_full_catalog |
| Flash 能力导入与 SDK 参数 | 无配置，或启动时已有无映射的 Flash；/login 后依次切换 low/high/max/none | 新旧两种路径均补全完整目录映射，可选等级包含三档；SDK 实际收到 low/high/max/none，none 携带 disabled thinking；不再显示“无有效映射”或旧的省略参数提示 | test_flash_login_imports_levels_and_applies_each_effort |
| 再次选择模型 | 已登录厂商，输入厂商编号和 pro 编号 | 显示已登录；仅两次输入，不索取 Key；认证字节不变、默认切 pro | test_logged_in_provider_only_asks_model_and_keeps_credentials_unchanged |
| 对话中切模型 | 先完成一轮 Flash，再 /login 选 Pro，继续第二轮 | SDK 分别初始化 Flash/Pro，新模型收到完整、无重复的历史；仅一条会话记录，默认选择更新 | test_login_switches_running_model_and_preserves_one_session_history |
| 只登录退出 | 首次成功登录后 /exit | 可生成配置和缺失库，不登记空会话、不发送聊天请求 | test_login_command_itself_never_registers_empty_session |
| 已登录获取失败 | 缓存完整列表，刷新抛含敏感标记的异常 | 提示使用缓存，只需厂商/模型两次输入，可选 pro；不显示原异常 | test_failed_refresh_reuses_cached_models_without_new_key |
| 首次获取失败 | 无缓存，获取抛异常 | 不保存、不展示不存在的模型；提示重试，不泄露异常原文 | test_first_fetch_failure_does_not_save_or_offer_nonexistent_models |
| 选模型前取消 | 输入 Key 后 EOF、KeyboardInterrupt 或 CancelledError | EOF 返回 None，其他取消传播；没有配置/数据库副作用 | test_cancel_before_model_choice_has_no_file_side_effects |
| 无效编号/名称 | 先输入不存在厂商、0、不存在模型及超范围编号，随后合法选择 | 在对应列表重新提示；不增加 URL/确认步骤，只保存合法选择 | test_invalid_provider_and_model_choices_retry_without_extra_fields |
| 新厂商相同交互 | 临时注册 second 后选择该厂商 | 使用同一交互，完整目录地址来自注册项，默认厂商正确 | test_new_registered_provider_uses_same_simple_selection |
| 禁止行内参数 | /login bad-argument，再退出 | 不发送给模型、不保存 | test_login_with_arguments_is_not_sent_to_model_or_saved |
| 真实终端秘密清理 | PromptSession/pipe 输入 Key 后 EOF，或直接选择模型编号 | Key 不在历史/缓冲，历史对象、普通显示和原校验器恢复；无保存确认；EOF 无文件 | test_real_terminal_key_history_and_validator_are_restored |
| 新模型思考映射与失败保护 | 原会话完成后模拟失败；切换带 high→max 的 pro，再修改强度 | 新 Runtime 使用 pro 的映射，SDK 收到 max；旧 Manager 关闭，新 Manager 仍拒绝失败会话历史，不能靠换模型绕过保护 | test_model_switch_uses_new_thinking_map_and_keeps_failed_session_guard |
| 组装失败/取消与重试 | 第二次 Agent 组装失败，或在 Event 等待时并发切换并取消 | 失败/取消保留原 Manager 和模型；并发切换报 busy，不强制取消运行；之后可重试成功，测试任务 finally 回收 | test_failed_or_cancelled_model_switch_keeps_runtime_and_allows_retry |
| 保存后切换失败 | 已运行 flash，选 pro 后 SDK 构建失败，再普通消息 | 默认 pro 已保存，当前仍由 flash 回答，明确提示两种状态且不泄露错误原文 | test_switch_failure_keeps_running_model_but_records_saved_default |
| 首次保存后初始化失败 | 初次登录完成后 SDK 失败 | 配置已保存，CLI 仍允许登录/退出，普通消息被阻止，安全提示 | test_initialization_failure_after_login_keeps_cli_available |

## 隔离方式

- core/CLI 共享 fixture 隔离 settings.root、cwd、数据库、Skills 和 MCP，前后清空设置缓存；本组清理模型选择环境变量。所有写入在 tmp_path，Key 为假值。
- core 保存无网络。准备流程用 AsyncMock 返回完整目录；扩展厂商专项用本地函数保留真实路由。core 测试还阻止真实 DeepSeek HTTP；ai 专项仅用内存 HTTP 响应。
- CLI 自动替换 discover_models，不触达目录 API；SDK 返回记录输入的本地 RecordingModel，仍使用真实 Manager、SQLite 和检查点。不执行外部工具/MCP。
- 终端专项用 PromptSession、内存 pipe、InMemoryHistory、DummyOutput 和五秒超时；其余用 AsyncMock 脚本输入。
- Runtime 异常用 SDK/Agent/Conversation 的限定替身；Event 控制并发时机，finally/cancel/gather 回收测试创建的任务。思考切换已有的 MCP 复用和活跃运行约束继续由[推理强度测试](thinking.md)覆盖。

## 执行

```bash
uv run pytest -q tests/core/test_model_configuration.py tests/cli/test_login.py tests/ai tests/cli/test_thinking.py --tb=no
uv run pytest -q tests/cli tests/core/test_store.py tests/core/test_bootstrap.py --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/core/model_configuration.py src/omt/core/bootstrap.py src/omt/ai/models_manager.py src/omt/ai/models/deepseek.py src/omt/cli/login.py src/omt/cli/app.py tests/core/test_model_configuration.py tests/cli/test_login.py tests/ai/test_model_catalog.py
```

## 覆盖限制

- 不证明真实服务可达、Key 有效、远程 ID 支持工具调用或某种 effort；second 是测试适配，不代表真实支持该厂商。
- 仅 API Key、每厂商一份，不实现 OAuth、自动凭据刷新、独立 /model 或 HTTP 登录端点。“已登录”不验证 Key 有效期，刷新失败使用缓存不保证后续聊天成功。
- 配置逐文件原子替换，没有跨文件/数据库/Runtime 事务和跨进程锁；不模拟断电、磁盘满或并行登录。选择保存后 Runtime 失败的区别由专门回归覆盖。
- 显式数据库初始化不修复已有库；0600 仅 POSIX 检查，内存终端不覆盖所有真实 TTY。
