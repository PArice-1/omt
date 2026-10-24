"""/login 的终端交互；配置校验、目录路由和文件写入全部交给 core。"""

from prompt_toolkit import PromptSession
from prompt_toolkit.history import DummyHistory
from pydantic import ValidationError

from omt.core import model_configuration
from omt.core.model_configuration import LoginResult, ModelLogin, ProviderConnection


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


async def login(terminal: PromptSession[str]) -> LoginResult | None:
    """收集 API 登录参数，确认后交给 core 保存；EOF/拒绝确认返回 None。

    terminal 可注入内存终端。厂商列表来自 core，不写死厂商名；目前仅支持 API Key。
    查询目录需要额外确认，会向用户输入的 base_url 发送凭据；不自动请求聊天模型。
    暂停普通消息的非空校验，让确认提示允许直接回车选择否；退出时恢复校验器。
    Ctrl-C/外部取消向上传播，确认前不保存配置。
    """
    validator = terminal.validator
    terminal.validator = None
    try:
        return await _login(terminal)
    finally:
        terminal.default_buffer.reset()
        terminal.validator = validator


async def _login(terminal: PromptSession[str]) -> LoginResult | None:
    providers = model_configuration.supported_providers()
    print("支持的厂商：" + "、".join(providers))
    print("当前仅支持 API Key 登录。输入 base_url 即指定凭据发送目标，请只使用可信地址。")
    try:
        provider = (await terminal.prompt_async("provider> ")).strip()
        if provider not in providers:
            print("[登录失败] 请从支持的厂商列表中选择。")
            return None
        base_url = (await terminal.prompt_async("base_url> ")).strip()
        key = (await _read_api_key(terminal)).strip()
        connection = ProviderConnection.model_validate({"provider": provider, "base_url": base_url, "api_key": key})
        if (await terminal.prompt_async("使用此地址和 Key 获取厂商模型列表？[y/N]> ")).strip().lower() == "y":
            try:
                models = await model_configuration.discover_models(connection)
            except Exception:
                # 网络及厂商 SDK 异常可能包含请求内容，不在终端回显原文。
                print("[模型列表获取失败] 可继续手动输入 model_name。")
            else:
                print("可选模型：" + "、".join(repr(model) for model in models))
        model_name = (await terminal.prompt_async("model_name> ")).strip()
        request = ModelLogin(provider=connection.provider, api_key=connection.api_key,
                             base_url=connection.base_url, model_name=model_name)
        print("将保存模型及 API 凭据；同厂商共用一个 Key，重新登录会更新该厂商 Key。")
        if (await terminal.prompt_async("确认保存？[y/N]> ")).strip().lower() != "y":
            print("已取消登录，未保存。")
            return None
        result = model_configuration.save_model_login(request)
    except EOFError:
        print("\n已取消登录，未保存。")
        return None
    except ValidationError:
        print("[登录失败] 请检查厂商、非空模型名/API Key 和合法 HTTP(S) 地址。")
        return None
    except ValueError as exc:
        print(f"[登录失败] {exc}")
        return None
    except OSError:
        print("[登录失败] 配置文件无法读写，请检查目录权限后重试。")
        return None
    print(f"模型已保存：{result.provider} / {result.model_name}。已有默认选择不会被覆盖。")
    return result
