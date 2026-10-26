# 模型初始化测试说明

## 对象与目的

验证“Model 包装对象 → 提供商注册表 → DeepSeek 初始化函数 → SDK 初始化”的调用链，确保参数校验在调用模型前完成，构造失败不会被当成一个有效模型保存。

源码：

- [model.py](../../src/omt/ai/model.py)
- [models_manager.py](../../src/omt/ai/models_manager.py)
- [models/deepseek.py](../../src/omt/ai/models/deepseek.py)

Ollama 原生本地适配的无 Key 初始化、目录、思考选项与真实 SDK 离线协议验证另见 [Ollama 测试](ollama.md)；共用注册表现包含 DeepSeek/Ollama，DeepSeek 旧调用链不变。

测试文件沿用 [test_ai.py](../../tests/ai/test_ai.py) 名称，但实际导入对象是 `omt.ai.model.Model`；不要根据旧名称恢复已移走的 ai.py 模块。

## 场景

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 创建可用模型 | 已注册 provider、HTTP URL、假 Key 和 temperature | 构造立即初始化；保存 provider/base_url，client.model 为可调用的本地模型 | test_ai.py：test_constructor_initializes_and_stores_usable_model |
| 显式初始化参数 | 在构造后调用私有初始化方法并传入另一个模型名及 max_tokens | 指定值沿真实内部调用链传到 SDK 初始化位置，不被旧值覆盖 | test_ai.py：test_private_init_model_returns_model_using_explicit_parameters |
| 注册表路由 | DeepSeek 配置与额外 args | 返回 SDK 替身；参数正确传递，调用者的 args 字典保持原值 | [test_models_manager.py](../../tests/ai/test_models_manager.py)：test_registered_provider_returns_usable_model |
| DeepSeek 启用思考 | effort 为 high/max，额外请求体含自定义字段和 disabled thinking，另传 max_tokens | 返回可调用假模型；SDK 收到原 effort 和 enabled thinking；保留其他选项，不修改原请求体 | test_deepseek.py：test_reasoning_effort_enables_thinking_and_preserves_options |
| 旧/未知模型不主动配置思考 | 旧模型 deepseek-chat 的 effort 为 Python None 或字符串 none，extra_body 为空或含自定义字段；原有效参数用例不传 effort | 不向 SDK 传 reasoning_effort，不新增 thinking；已有请求体保持原值，不伪造 disabled | test_deepseek.py：test_no_reasoning_effort_does_not_configure_thinking、test_valid_parameters_return_usable_model |
| 已知模型三档与关闭 | deepseek-flash/deepseek-v4-pro，effort 分别为 None/none/low/high/max，请求体已有自定义字段及 enabled thinking | None 不设置参数；low/high/max 原样下发且 enabled；字符串 none 显式下发并改为 disabled，保留其他字段且不修改调用者对象；返回可调用假模型 | test_deepseek.py：test_known_models_forward_thinking_levels_and_explicitly_disable_none |
| 类型转换 | URL/Key 分别使用字符串或 HttpUrl/SecretStr | 接受合法输入；传给 SDK 的 URL 是字符串，Key 保持 SecretStr | [test_deepseek.py](../../tests/ai/test_deepseek.py)：test_valid_parameters_return_usable_model |
| 未注册 provider | 空或未知 provider | 拒绝创建，不调用 SDK 初始化 | test_ai.py、test_models_manager.py 的 unknown_provider 用例 |
| 缺失或空白 Key | 无 Key、None、空串、空格或换行 | 缺失/非法参数校验失败；空白 Key 被明确拒绝，不调用 SDK | 三个测试文件的 missing/blank/validation 用例 |
| 错误 URL 或参数类型 | 非 HTTP(S) URL、非法 provider/model/Key 类型 | 对应参数位置校验失败 | test_deepseek.py：test_invalid_parameters_are_rejected |
| 参数校验异常包装 | 提供商初始化抛 Pydantic ValidationError | 注册表层转为带参数错误提示的 ValueError，并保留原始 cause | test_models_manager.py：test_validation_error_is_wrapped_with_original_cause |
| 外部初始化失败 | SDK 替身抛 ValueError 或 RuntimeError | 原始异常继续传播，不返回错误字符串或伪模型 | 三个文件的 initialization_error / error_propagates 用例 |

正常初始化用例同时核对 `configurable_fields=None`、`config_prefix=None`：OMT 的 Model 需要直接可调用的聊天模型，SDK 工厂不返回运行时再选模型的包装器。此项覆盖 test_ai.py、test_models_manager.py 和 test_deepseek.py 的正常初始化断言。

## 厂商模型目录查询

新增 [test_model_catalog.py](../../tests/ai/test_model_catalog.py)，通过公开 list_provider_models → models[provider].list_models → ai/models/deepseek.py 验证目录查询。注册表每项为 ModelProvider(initialize, list_models, base_url)，地址来自厂商模块（DeepSeek 为 DEEPSEEK_BASE_URL）；目录入口只需 provider/api_key，不接收用户地址或当前模型名，因此查询不按当前模型过滤。注册项另可提供 thinking_level_maps（默认空表），由厂商模块维护精确模型 ID 的官方能力，core 导入时合并；/models 响应仍只提供 ID，不从名称推测未知能力。模型初始化入口 models_provider 的调用参数不变，原有初始化回归继续保留。

| 场景 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 厂商地址与底层显式覆盖 | 注册表入口不传 URL；底层适配另测带 /proxy/v1/ 的覆盖地址，内存响应含重复/乱序的多个 ID | 默认请求模块定义的官网；底层仍能保留覆盖地址前缀以便测试/代理适配（不在 /login 暴露）。追加 /models，带 Bearer 和十秒超时，完整返回全部去重排序 ID并关闭响应 | test_catalog_uses_provider_default_or_explicit_low_level_prefix |
| 非法响应 | 空列表、错误 ID 类型、非法 JSON、超过 1 MiB | 安全 ValueError，不显示响应或测试敏感标记，响应关闭 | test_invalid_catalog_response_is_sanitized_and_closed |
| 网络及读取异常 | HTTPError、URLError 或 IncompleteRead，错误含敏感标记 | 安全错误，不回显详情；存在的 HTTP 响应关闭；安装的 handler 不允许重定向 | test_catalog_errors_hide_details_and_close_http_resources |
| 云端 Key 要求保留 | 公共目录入口省略 Key 或传入空串/纯空白；DeepSeek HTTP 构建器替身 | 初始化 HTTP 前拒绝，不能因新增本地免 Key 能力而放宽 DeepSeek；注册项默认要求 Key，目录回调只在免认证时收到 None | test_cloud_catalog_still_requires_nonblank_key |
| 未注册厂商 | unknown provider | 明确拒绝，不访问网络 | test_unregistered_provider_catalog_is_rejected |

注册第二个测试厂商即可完成查询和持久化、不改 core/CLI 的扩展性证据在[模型登录测试](model-login.md)。

## 隔离方式

[ai/conftest.py](../../tests/ai/conftest.py) 仅替换 DeepSeek 模块使用的 `init_chat_model`，返回 FakeListChatModel。测试仍经过真实的 Model、models_provider、deepseek_init，且使用无效的测试 Key，不读取认证文件、不调用网络。

目录测试替换 DeepSeek 模块的 build_opener，返回内存字节流或受控异常，不请求用户 URL 或官方网络。新增提供商时为该提供商建立同类的初始化和 HTTP 替身，不以真实账户证明单元测试通过。

## 执行

```bash
uv run pytest -q tests/ai --tb=no
```

## 覆盖限制

- 不证明真实 DeepSeek 服务接受某个模型名或 effort，也不验证账单、速率限制、网络重试或输出质量。适配参考 [DeepSeek 思考模式协议](https://api-docs.deepseek.com/guides/thinking_mode/)；只有已知支持关闭的模型显式传 none 才构造关闭参数，旧/未知模型仍省略该参数，None 仍沿用服务端配置。
- 本页主要覆盖 DeepSeek；厂商列表回归已更新为 DeepSeek/Ollama 两项，Ollama 专项见独立说明。其他提供商和 OAuth 未覆盖。
- 会话如何从多个配置文件选择参数，由 [Session 与配置测试](session-and-config.md) 验证。
