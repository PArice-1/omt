# 模型配置与共用组装测试说明

## 对象与目的

- 源码：[settings.py](../../src/omt/coding_agent/settings.py)、[bootstrap.py](../../src/omt/coding_agent/bootstrap.py)。
- 测试：[test_settings.py](../../tests/coding_agent/test_settings.py)、[test_configuration_files.py](../../tests/coding_agent/test_configuration_files.py)、[test_bootstrap.py](../../tests/coding_agent/test_bootstrap.py)。
- 防止选错模型或 URL、将 OAuth 当 Key、配置报错泄露凭据，以及测试误读用户配置。

原 test_session.py 的模型解析和 Agent 组装用例迁入 test_bootstrap.py，直接测试共用 build_model/build_agent，不再经已删除的 CLI 私有别名。交互、输入、信号与完整组装链路迁入 [CLI 测试](cli.md)，恢复迁入 [/resume 测试](resume.md)。这不是删除旧行为覆盖：低层模型断言保留；旧失败后直接重试契约按 Manager 的保守策略替换。

## 配置读取场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| Settings 字段 | 合法 provider/model，或缺字段、错误类型、空白值 | 合法值可加载，缺失/非法选择被拒绝 | test_settings.py 的 accepts/reject 用例；test_configuration_files.py：test_settings_reject_blank_selection |
| 文件与缓存 | 临时 settings.json、文件缺失、错误 JSON、修复后重试、成功后修改文件 | 错误传播；成功结果缓存，cache_clear 后读取新值；失败不固化为缓存结果 | test_settings.py 的 load/missing/invalid/cached/retried 用例 |
| API Key 类型 | auth 中提供假 Key | 加载为 SecretStr，可明确取测试值，但 repr 不暴露原值 | test_auth_loads_api_key_as_masked_secret |
| 非 API Key 认证 | OAuth 类型附带 access/refresh | 加载类型，不误用为 key；额外 token 不显示在对象 repr | test_auth_retains_type_without_treating_oauth_as_api_key |
| 非法认证结构 | 尾随逗号、缺 type、错误结构，携带测试敏感标记 | 带文件提示的错误；完整格式化异常也不含该标记 | test_invalid_auth_errors_do_not_expose_credentials |
| 认证重读 | 假 Key 改变，或 auth 文件不存在 | 下次读取新值；缺文件抛异常 | test_auth_is_reread_after_credentials_change、test_missing_auth_file_raises |
| 目录字段 | 模型带 URL、provider 及能力/价格额外字段 | 连接字段可用，额外元数据不妨碍加载 | test_model_store_parses_model_url_and_ignores_catalog_metadata |
| 非法目录 | 错误 JSON、错误顶层、缺 URL、非 HTTP(S) URL、空 ID、重复 ID、缺文件 | 拒绝目录或报文件缺失，不选择一个凑合使用 | test_invalid_model_store_is_rejected、test_missing_model_store_file_raises |

## 共用模型与 Agent 组装

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 三份配置拼接 | 假默认选择、认证与目录匹配 | provider/model/精确 URL/SecretStr 传至 SDK 替身，返回可用模型 | test_get_model_joins_settings_auth_and_matching_catalog_entry |
| 认证不可用 | provider 无认证、Key 缺失/空白、OAuth/bearer/未知类型 | 初始化前报错，SDK 替身未调用；错误不含测试 token | test_get_model_rejects_missing_or_blank_auth、test_get_model_explicitly_rejects_unsupported_auth_types |
| 提供商与目录不匹配 | provider 未注册、目录缺 provider/model、模型 provider 不符 | 明确拒绝，不回退到其他模型 | test_get_model_rejects_unregistered_provider_before_initializing_model、test_get_model_rejects_missing_or_mismatched_catalog |
| 精确 URL 选择 | 所选模型不是 models 列表第一项 | 只使用所选 ID 的 URL | test_get_model_uses_url_of_exact_selected_model_not_first_entry |
| Agent 组装 | Model 包装对象与内置工具，MCP 发现返回空列表的异步替身 | 等待异步 build_agent，传入 client.model，工具集合包含全部静态注册项 | test_get_agent_uses_underlying_chat_model_and_builtin_tools |

上述 test_bootstrap.py 用例沿用原 test_get_model/test_get_agent 名称，仅被测函数改为共用 bootstrap 实现。资源管理器 open_session_manager 的实际创建/关闭、预建库校验和提示词传递由 [CLI](cli.md) 与 [HTTP](api.md) 集成测试验证；共用启动选项 SessionSettings 的路径/MCP 校验主要在 HTTP 配置与 CLI 配置用例中覆盖。

## 隔离方式

[coding_agent/conftest.py](../../tests/coding_agent/conftest.py) 同时隔离 settings.root 与 cwd，前后清空 load_settings 缓存。所有 JSON 均写在 tmp_path，使用保留域名与假 Key，不输出真实认证文件。

模型组装只替换 SDK 初始化端点，保留从 build_model → Model → 提供商函数的调用链；返回 FakeListChatModel，不发网络请求。Agent 用真实内置工具定义，默认不传 MCP 配置，不启动 MCP。配置解析错误仅包含测试假标记，用于检验完整格式化异常也不泄密。

不能只切换 cwd：settings.root 在模块导入时就已固定；新增配置测试必须使用共享 fixture 或显式隔离该值并清缓存。

## 执行

```bash
uv run pytest -q tests/coding_agent/test_settings.py tests/coding_agent/test_configuration_files.py tests/coding_agent/test_bootstrap.py --tb=no
uv run pytest -q tests/cli tests/api --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/coding_agent/bootstrap.py src/omt/coding_agent/configuration.py tests/coding_agent/test_bootstrap.py
```

## 覆盖限制

- 不读取或验证用户现有配置，不请求真实模型、OAuth、MCP 或代理。
- SDK 替身验证连接参数，不证明真实服务可达或凭据有效。
- 不验证多进程共享设置缓存或热重载；成功配置缓存需调用方主动清理。
- 不把输入校验、目录 resolve 或提示词当成权限和沙箱实现。
