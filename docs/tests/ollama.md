# Ollama 本地模型适配测试说明

## 对象与目的

- 源码：[ollama.py](../../src/omt/ai/models/ollama.py)、[models_manager.py](../../src/omt/ai/models_manager.py)。
- 测试：[test_ollama.py](../../tests/ai/test_ollama.py)。
- 目标：新增厂商使用 Ollama 原生协议和 ChatOllama，而非把本地服务误当 DeepSeek/OpenAI；底层无需 Key，模型构造默认不联网；保留完整模型标签及 SDK 原生选项，不把 DeepSeek 的思考等级自动套用到本地模型。
- 当前已打通 core/CLI/TUI/HTTP 的免 Key 配置，修复先前仅底层免 Key、上层仍强制登录的问题。相关预览/保存/重启/切换/取消用例见 [模型登录测试](model-login.md)、[TUI 测试](tui.md)、[HTTP 运行配置测试](api-runtime-streaming.md) 和 [组装测试](session-and-config.md)；本页侧重适配层。

## 场景

| 测什么、为什么 | 输入 / 前置条件 | 预期结果 | 对应测试 |
| --- | --- | --- | --- |
| 无 Key 的注册调用链 | Model("ollama")、本地完整名称及 num_ctx/num_predict/temperature；SDK 工厂返回假聊天模型 | 经真实注册表初始化，返回可调用模型；默认地址 localhost:11434、无 Key、无启动校验；注册项不猜测模型能力 | test_registered_model_is_usable_without_api_key |
| 共用 Key 参数兼容 | 不传 Key、None、空 SecretStr、非空假 Key | 底层均可初始化，该参数不传给 SDK，不自动添加 reasoning 或 DeepSeek 请求体 | test_compatibility_key_is_not_forwarded |
| 原生思考选项 | 模型注册接口传 None/none/false/true/low/medium/high，同时原生 reasoning=True | None 保留原生选项；none/false→False、true→True、三档字符串原样转换到 reasoning；其他选项及原输入字典不变，不发送 reasoning_effort/extra_body | test_reasoning_effort_maps_to_native_option |
| 不支持的思考值 | max、未知值、空串 | 初始化 SDK 前明确拒绝，不将 max 伪装为 high | test_unsupported_effort_is_rejected_before_sdk |
| 无效模型/地址 | 空白名称、非 HTTP(S) 地址、非字符串模型 | 失败位置正确，不初始化 SDK；通过注册项的公开初始化回调注入非法类型，保留运行时校验且无需类型忽略 | test_blank_model_is_rejected、test_invalid_parameters_are_rejected |
| 显式校验和 SDK 异常 | 调用方显式设置 validate_model_on_init=True，替身抛 RuntimeError | 不吞掉显式选项，原异常继续传播，不返回伪模型 | test_explicit_validation_and_sdk_failure_are_preserved |
| 真实 SDK 同步/异步原生协议 | 真实 ChatOllama、带 /proxy/ 前缀的测试地址；MockTransport 返回独立 thinking/content | 构造不发请求；invoke/ainvoke 请求 POST /proxy/api/chat；发送 think=high，不发送兼容 Key/reasoning_effort；返回正文与 reasoning_content 分离 | test_real_sdk_uses_native_chat_and_separates_reasoning |
| 完整本地目录 | 注册入口使用默认地址且省略 Key，底层另测自定义代理前缀；内存 models 含重复、乱序、首尾空白名称及标签 | 请求 /api/tags，无 Authorization，十秒超时；保留 :tag、去空白去重排序，关闭响应 | test_catalog_uses_native_tags_and_preserves_full_names |
| 空目录与非法响应 | 空目录、全空白名称、错误名称类型、OpenAI 格式、非法 JSON、超过 1 MiB | 安全 ValueError，提示启动服务和下载模型，不回显响应或测试敏感标记；响应关闭 | test_catalog_invalid_responses_are_sanitized_and_closed |
| HTTP、网络、读取失败和重定向 | 受控 HTTPError、URLError、IncompleteRead；错误含假敏感标记 | 安全异常，不回显内部详情；有响应时关闭，不允许重定向改变目标 | test_catalog_errors_do_not_leak_and_http_resources_are_closed |
| 不安全的目录地址 | 地址带用户名密码、查询参数或片段 | 打开 HTTP 前拒绝，错误不含地址中的假秘密 | test_catalog_rejects_embedded_auth_query_and_fragment |

厂商列表回归同步更新 [test_model_catalog.py](../../tests/ai/test_model_catalog.py) 的 `test_catalog_uses_provider_default_or_explicit_low_level_prefix`，预期精确注册 DeepSeek/Ollama 两项；DeepSeek 原协议和凭据发送规则不变。HTTP 的无配置厂商枚举同步见 [HTTP 运行配置测试](api-runtime-streaming.md)。

## 隔离方式

- 初始化单测仅替换本模块的 init_chat_model，返回共享 FakeListChatModel；保留 Model、注册表和 ollama_init 的真实调用链。不读取用户配置，不启动 Ollama，不下载模型，不消耗推理资源。
- 目录测试替换本模块 build_opener，所有响应均为内存字节流，失败均由测试主动构造；不请求 localhost 或远程地址。
- 真实 SDK 协议测试给同步、异步客户端同时注入 httpx.MockTransport；清理 OLLAMA_API_KEY，避免 SDK 读取用户的环境认证。所有请求由内存处理器响应，无真实连接、模型调用或文件写入。
- 本文件不使用配置保存与数据库，无 settings.root 访问；不启动工具、MCP 或外部进程。

## 执行

```bash
uv run pytest -q tests/ai/test_ollama.py --tb=short
uv run pytest -q tests/ai tests/api/test_runtime_streaming.py --tb=no
uv run pytest -q --tb=no
basedpyright --pythonpath .venv/bin/python src/omt/ai/models/ollama.py src/omt/ai/models_manager.py tests/ai/test_ollama.py tests/ai/test_model_catalog.py tests/api/test_runtime_streaming.py
```

## 覆盖限制

- 不证明本机安装、服务可达、模型已下载，或某个本地模型支持工具/思考/某个强度；也不验证 GPU、性能、内存占用或输出质量。
- 目录仅返回已下载模型名，不调用 /api/show、不下载/卸载模型、不推断 thinkingLevelMap。
- 原生思考能力由具体模型决定；传入 False 不保证所有模型都能关闭思考，low/medium/high 也不代表任意模型均接受，服务错误仍需交给正常执行错误处理。
- 不覆盖定制服务地址交互或 Ollama 云端认证；CLI/HTTP 免 Key 配置由上方链接的集成测试覆盖。兼容 api_key 参数只是不向 SDK 传递该参数，调用方自行提供的 client_kwargs 或 Ollama SDK 环境配置不在这里重写。
- 使用原生协议，不提供 OpenAI /v1 地址自动识别或转换，不自动访问外部服务修复配置。
