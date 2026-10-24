"""/login 的终端交互；认证复用、完整目录和自动保存全部交给 core。"""

from prompt_toolkit import PromptSession
from prompt_toolkit.history import DummyHistory
from pydantic import SecretStr, ValidationError

from omt.core import model_configuration
from omt.core.model_configuration import LoginResult, ModelLogin


async def _read_api_key(terminal: PromptSession[str]) -> str:
    """临时禁用输入历史，退出（含取消）时清空密钥缓冲并恢复显示状态。"""
    buffer = terminal.default_buffer
    history, password = buffer.history, terminal.is_password
    buffer.history = DummyHistory()
    try:
        return await terminal.prompt_async("api_key> ", is_password=True)
    finally:
        buffer.reset()
        buffer.history = history
        terminal.is_password = password


async def _choose(terminal: PromptSession[str], prompt: str, choices: tuple[str, ...]) -> str:
    """接受从 1 开始的编号或完整名称，不把短名称猜测成厂商别名。"""
    if not choices:
        raise ValueError("没有可选条目")
    while True:
        value = (await terminal.prompt_async(prompt)).strip()
        if value in choices:
            return value
        if value.isascii() and value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]
        print("请选择列表中的编号或完整名称。")


async def login(terminal: PromptSession[str]) -> LoginResult | None:
    """选择厂商和模型后自动保存；已登录厂商不重复索取 Key，也不询问地址或确认。

    terminal 可注入内存终端。使用厂商内置地址自动拉取完整模型列表，网络失败且
    已有认证时可使用本地目录；首次拉取失败不能凭空选择。选定模型前 EOF 不落盘，
    Ctrl-C/外部取消向上传播；退出后恢复普通输入校验、历史及显示状态。
    """
    validator = terminal.validator
    terminal.validator = None
    try:
        return await _login(terminal)
    finally:
        terminal.default_buffer.reset()
        terminal.validator = validator


async def _login(terminal: PromptSession[str]) -> LoginResult | None:
    try:
        providers = model_configuration.supported_providers()
        print("支持的厂商：" + "、".join(providers))
        logged_in = {provider: model_configuration.is_provider_logged_in(provider) for provider in providers}
        for number, provider in enumerate(providers, 1):
            print(f"{number}. {provider}（{'已登录' if logged_in[provider] else '未登录'}）")
        print("使用厂商内置地址获取完整模型列表，选择后自动保存。")
        provider = await _choose(terminal, "provider（编号或名称）> ", providers)
        key = None if logged_in[provider] else SecretStr((await _read_api_key(terminal)).strip())
        prepared = await model_configuration.prepare_provider_login(provider, key)
        if prepared.from_cache:
            print("[模型列表刷新失败] 使用已缓存的本地模型列表，无需重新登录。")
        for number, model_name in enumerate(prepared.model_names, 1):
            print(f"{number}. {model_name}")
        selected = await _choose(terminal, "model（编号或名称）> ", prepared.model_names)
        request = ModelLogin(provider=provider, api_key=prepared.connection.api_key, model_name=selected)
        result = model_configuration.save_model_login(request, model_names=prepared.model_names)
    except EOFError:
        print("\n已取消选择，未保存。")
        return None
    except ValidationError:
        print("[登录失败] 请检查厂商、模型名和非空 API Key。")
        return None
    except ValueError as exc:
        print(f"[登录失败] {exc}")
        return None
    except OSError:
        print("[登录失败] 配置文件无法读写，请检查目录权限后重试。")
        return None
    print(f"已保存完整厂商目录，默认模型：{result.provider} / {result.model_name}。")
    return result
