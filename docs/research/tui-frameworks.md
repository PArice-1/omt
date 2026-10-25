# TUI 展示层方案调研

## 问题与结论

目标是把逐行 CLI 升级为容易上手的终端界面：流式聊天、Markdown、可折叠工具输出、模型/会话/推理选择、隐藏 Key，以及异步取消和可重复的离线测试。业务继续使用现有 core，不引入 HTTP 或另一套会话存储。

本次在 `feature/tui` 实验分支采用 **Textual**，保留 `omt --plain`。这是 Python 终端展示层的选择，不是替换 LangChain/LangGraph 或 SessionManager。合并前仍需人工体验真实终端。

## GitHub 候选比较

| 方案与一手资料 | 与本项目的匹配 | 本次取舍 |
| --- | --- | --- |
| [Textualize/textual](https://github.com/Textualize/textual) | Python 异步框架，提供布局、输入区、选择、Markdown 等组件和测试框架；可以直接接现有 asyncio Runtime | 采用。复用成熟组件而非自己维护布局、键鼠路由、弹窗和流式富文本 |
| [prompt-toolkit/python-prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | 现有逐行输入已经使用；也支持全屏应用、布局、键鼠和异步，升级不会新增另一套输入框架 | 保留给 `--plain`。并非不支持 TUI，但本次聊天气泡、Markdown、选择弹窗等还需更多自行组装 |
| [urwid/urwid](https://github.com/urwid/urwid) | 成熟 Python 控件/布局库，支持事件循环；适合传统终端应用 | 未选。可以实现目标，但对本次富文本聊天和无终端交互验证，Textual 的现成能力更直接 |

以上是需求匹配判断，不是跨框架性能跑分或流行度排名；不以不断变化的 star 数作为依据，也未在 OMT 中分别实现三套原型。

## 核实的关键能力

- [Textual README](https://github.com/Textualize/textual/blob/main/README.md)：异步基础、组件、布局与测试入口。
- [Workers 官方文档源码](https://github.com/Textualize/textual/blob/main/docs/guide/workers.md)：后台异步工作避免阻塞界面；取消是协作式的，不能据此声称工具副作用回滚。
- [Testing 官方文档源码](https://github.com/Textualize/textual/blob/main/docs/guide/testing.md)：`App.run_test()` 和 Pilot 驱动键盘、鼠标与终端尺寸，不需要真实 TTY 或收费模型。
- 本地安装版本的 `App`、`Screen`、`Select`、`TextArea`、`Markdown` API 经源码核对并通过交互测试验证。依赖使用 `textual>=8.2.8,<9`，精确解析结果由 `uv.lock` 固定；不用未安装的主分支 API 当作运行事实。

官方站点 `textual.textualize.io` 在本环境的内容抓取受到地址安全限制；改读其官方 GitHub 文档源码，未放宽网络安全配置。GitHub 页面/README 是选型证据；项目内的行为证据见 [TUI 测试说明](../tests/tui.md)。

## 落地边界

- `terminal.py` 拆为 `terminal/` 包：`plain.py` 保留旧函数，`__init__.py` 重导出以维持已有导入；`tui.py` 管界面生命周期，`screens.py` 管弹窗，`widgets.py` 管可复用展示组件，`completion.py` 管指令提示元数据和匹配，`tui.tcss` 管样式。
- 默认 `omt` / `omt --tui` 使用 Textual；`omt --plain` 和公开 `run_cli()` 保留原逐行协议。两者的数据目录、配置格式和 SQLite 不变，不迁移旧历史。
- 界面只调用 core 的配置和 SessionRuntime/Manager 接口；新增厂商仍只改 ai 注册，不在界面增加厂商分支。
- UI 后台操作串行；退出等待取消与资源清理。屏幕缓冲的裁剪不改数据库历史，不宣称取消撤销外部副作用。
- 首次 Key 不进入聊天记录，密码控件使用隐藏显示；这不改变 `auth.json` 明文加文件权限的现有存储性质。

## 交互风格调整

第一版控件式布局改为 coding-agent 常见的内容优先展示：移除大按钮工具栏，使用紧凑的用户/助手标记、细分隔输入区与单行状态；`/` 提示、Tab 补全和真实模型推理参数提示代替记忆命令。输入区支持软换行伸缩及有界内存历史。这里只借鉴交互原则，并非复制 Claude/Pi/Codex 的实现，也未把 Textual 切换成原生终端 scrollback。

[预览](../cli.md#默认-tui)由真实组件离线生成，不含用户凭据或真实模型响应。

## 验收与剩余限制

离线测试覆盖真实组件、真实临时 SQLite 与假模型/工具。真实终端、不同字体、系统剪贴板、屏幕阅读器、超长会话性能以及 Windows/macOS 的兼容性仍需人工验证；未请求真实厂商模型，也未做跨平台兼容承诺。
