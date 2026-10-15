"""最小交互入口：读取配置，组装模型、Agent 和会话，顺序处理用户输入。"""

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError, Validator

from omt.agent.agent import Agent
from omt.ai.model import Model
from omt.ai.models_manager import models as model_initializers

from .runtime import CreateAgentSession
from .settings import Settings, load_auth, load_model_store, load_settings
from .tools_manager import load_tools


def _get_model(settings: Settings) -> Model:
    """按提供商和模型 ID 精确解析认证与连接信息，不回退到其他模型或环境密钥。"""
    provider = settings.defaultProvider
    model_id = settings.defaultModel
    if provider not in model_initializers:
        raise ValueError(f"提供商 {provider!r} 未注册模型初始化函数")

    credential = load_auth().get(provider)
    if credential is None:
        raise ValueError(f"auth.json 中缺少提供商 {provider!r} 的认证配置")
    if credential.type != "api_key":
        raise ValueError(
            f"提供商 {provider!r} 的认证类型 {credential.type!r} 暂不支持；"
            "目前仅支持 api_key，其他认证需要提供商专用适配，不能直接当作 API Key"
        )
    api_key = credential.key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError(f"auth.json 中提供商 {provider!r} 的 API Key 不能为空")

    provider_models = load_model_store().get(provider)
    if provider_models is None:
        raise ValueError(f"model-store.json 中缺少提供商 {provider!r}")
    definition = next(
        (model for model in provider_models.models if model.id == model_id), None
    )
    if definition is None:
        raise ValueError(
            f"model-store.json 中未找到提供商 {provider!r} 的模型 {model_id!r}；"
            "settings.json 的 defaultModel 必须与 models[].id 完全一致"
        )
    if definition.provider is not None and definition.provider != provider:
        raise ValueError(f"model-store.json 中模型 {model_id!r} 的提供商与配置不一致")

    return Model(
        provider,
        model=definition.id,
        base_url=definition.baseUrl,
        api_key=api_key,
    )


def _get_agent(model: Model) -> Agent:
    """用底层聊天模型和内置工具创建可执行的 Agent 实例。"""
    return Agent(model=model.model, tools=load_tools())


class _UserInputValidator(Validator):
    """在用户提交输入时校验，保留合法输入的原始空格和换行。"""

    def validate(self, document: Document) -> None:
        text = document.text
        if not text.strip():
            raise ValidationError(message="输入不能为空或只有空白", cursor_position=len(text))
        if "\x00" in text:
            raise ValidationError(message="输入不能包含 NUL 字符", cursor_position=text.index("\x00"))


def _get_user_input(terminal: PromptSession[str] | None = None) -> str:
    """同步读取有效输入；Ctrl-C/EOF 交给会话循环处理，不使用内置 input()。"""
    if terminal is None:
        terminal = PromptSession()
    return terminal.prompt(
        "You> ",
        validator=_UserInputValidator(),
        validate_while_typing=False,
    )


def session() -> None:
    """运行单个会话；/exit、/quit 或 Ctrl-D 退出，Ctrl-C 取消当前输入/调用。"""
    settings = load_settings()
    model = _get_model(settings)
    agent = _get_agent(model)
    conversation = CreateAgentSession(agent)
    terminal: PromptSession[str] = PromptSession()
    print("输入 /exit 或 /quit 退出，Ctrl-C 取消当前操作，Ctrl-D 结束会话。")

    while True:
        try:
            user_input = _get_user_input(terminal)
        except KeyboardInterrupt:
            print("\n已取消本次输入。")
            continue
        except EOFError:
            print("\n会话结束。")
            break

        if user_input.strip().lower() in {"/exit", "/quit"}:
            print("会话结束。")
            break

        try:
            # runtime 已负责流式展示，不再把返回的最终回答重复打印一次。
            conversation.run(user_input)
        except KeyboardInterrupt:
            print("\n本轮已中断；已执行的工具操作不会撤销。")
        except Exception:
            # runtime 已显示原始异常，并保留此前完成的历史。
            print("本轮执行未完成，可继续输入，或使用 /exit 退出。")


if __name__ == "__main__":
    session()


# 警告消除
import warnings

warnings.filterwarnings(
    "ignore",
    message=r"The v3 streaming protocol on Pregel is experimental\.",
    module=r"langgraph\.pregel\.main",
)