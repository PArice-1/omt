"""键盘/鼠标选择面板；配置和凭据处理只调用 core 的公开入口。"""

from collections.abc import Sequence
from typing import final, override

from pydantic import SecretStr
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from omt.core import model_configuration
from omt.core.model_configuration import LoginResult, ModelLogin, ProviderLogin


@final
class ChoiceScreen(ModalScreen[str | None]):
    """choices 是展示文字/稳定值对；Esc 取消，不按列表下标恢复会话。"""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, title: str, choices: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self.heading = title
        self.choices = choices

    @override
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="dialog-title", markup=False)
            yield OptionList(*(Option(Text(label), id=value) for label, value in self.choices), id="choices")
            if not self.choices:
                yield Static("暂无可选项。", markup=False)
            yield Label("↑↓ 选择 · Enter 确定 · Esc 返回", classes="dialog-hint")
            yield Button("返回", id="choice-cancel")

    def on_mount(self) -> None:
        _ = self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        _ = self.dismiss(event.option_id)

    @on(Button.Pressed, "#choice-cancel")
    def action_cancel(self) -> None:
        _ = self.dismiss(None)


@final
class LoginScreen(ModalScreen[LoginResult | None]):
    """选厂商、首次输入 Key、选模型即保存；后台查询取消后不落盘。"""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self) -> None:
        super().__init__()
        self.prepared: ProviderLogin | None = None
        self.key_input = Input(password=True, placeholder="首次登录才需要 API Key", id="api-key", disabled=True)
        self._loading = False
        self.provider_select = Select[str]([], prompt="选择厂商", id="provider")
        self.model_select = Select[str]([], prompt="选择模型（选择后自动保存）", id="login-model", disabled=True)
        self.load_button = Button("获取模型列表", id="load-models", disabled=True, variant="primary")
        self.key_input.display = self.model_select.display = self.load_button.display = False

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="login-dialog"):
            yield Label("连接模型", classes="dialog-title")
            yield Static("同一厂商只需登录一次。选择模型后自动保存。", classes="dialog-hint")
            yield self.provider_select
            yield self.key_input
            yield self.load_button
            yield Static("选择厂商开始；Esc 随时返回。", id="login-status", markup=False)
            yield self.model_select
            yield Button("取消", id="login-cancel")

    def on_mount(self) -> None:
        try:
            providers = model_configuration.supported_providers()
            choices = [(Text(f"{name} · {'已登录' if model_configuration.is_provider_logged_in(name) else '未登录'}"), name)
                       for name in providers]
        except (ValueError, OSError):
            self.status("配置无法读取，请检查配置文件；不会自动覆盖。")
        else:
            self.provider_select.set_options(choices)
        _ = self.provider_select.focus()

    def status(self, text: str) -> None:
        self.query_one("#login-status", Static).update(text)

    @on(Select.Changed, "#provider")
    def provider_changed(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str):
            return
        self.prepared = None
        self.key_input.value = ""
        model_select = self.model_select
        model_select.set_options([])
        model_select.disabled = True
        model_select.display = False
        try:
            logged_in = model_configuration.is_provider_logged_in(event.value)
        except (ValueError, OSError):
            self.status("无法读取认证，请检查配置文件。")
            return
        self.key_input.disabled = logged_in
        self.key_input.display = not logged_in
        self.load_button.disabled = False
        self.load_button.display = not logged_in
        if logged_in:
            self.start_loading()
        else:
            self.status("输入 API Key，按 Enter 获取完整模型列表。")
            _ = self.key_input.focus()

    @on(Input.Submitted, "#api-key")
    @on(Button.Pressed, "#load-models")
    def start_loading(self) -> None:
        provider = self.provider_select.value
        if self._loading or not isinstance(provider, str):
            return
        key = None if self.key_input.disabled else SecretStr(self.key_input.value.strip())
        self.key_input.value = ""
        if key is not None and not key.get_secret_value():
            self.status("API Key 不能为空。")
            _ = self.key_input.focus()
            return
        self._loading = True
        self.provider_select.disabled = True
        self.load_button.disabled = True
        self.status("正在获取完整模型列表… Esc 可取消。")
        _ = self.run_worker(self.load_models(provider, key), exit_on_error=False)

    async def load_models(self, provider: str, key: SecretStr | None) -> None:
        try:
            self.prepared = await model_configuration.prepare_provider_login(provider, key)
        except Exception:
            self.status("获取失败，尚未保存。请检查网络、Key 或配置文件后重试。")
        else:
            select = self.model_select
            select.set_options([(Text(name), name) for name in self.prepared.model_names])
            select.disabled = False
            select.display = True
            self.key_input.display = self.load_button.display = False
            self.status("刷新失败，使用本地缓存。" if self.prepared.from_cache else "已获取完整列表；选择后自动保存。")
            _ = select.focus()
        finally:
            self._loading = False
            if self.is_mounted:
                self.provider_select.disabled = False
                self.load_button.disabled = False

    @on(Select.Changed, "#login-model")
    def model_changed(self, event: Select.Changed) -> None:
        prepared = self.prepared
        if prepared is None or not isinstance(event.value, str):
            return
        try:
            login = ModelLogin(provider=prepared.connection.provider, api_key=prepared.connection.api_key,
                               model_name=event.value)
            result = model_configuration.save_model_login(login, model_names=prepared.model_names)
        except Exception:
            self.status("保存失败，请检查配置和目录权限后重试。")
        else:
            _ = self.dismiss(result)

    @on(Button.Pressed, "#login-cancel")
    def action_cancel(self) -> None:
        self.key_input.value = ""
        _ = self.dismiss(None)

    def on_unmount(self) -> None:
        self.key_input.value = ""
        self.prepared = None
