"""稳定的 HTTP 数据结构，不暴露 LangChain 状态或内部消息元数据。"""

from typing import ClassVar, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator

from omt.core.session_manager import validate_message
from omt.core.settings import NonEmptyString
from omt.core.store import SessionRecord


class CreateSessionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def check_title(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("标题不能包含 NUL")
        return value


class RunRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32000)

    @field_validator("message")
    @classmethod
    def check_message(cls, value: str) -> str:
        return validate_message(value)


class SessionResponse(BaseModel):
    session_id: str
    title: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: SessionRecord) -> "SessionResponse":
        return cls(
            session_id=record.thread_id, title=record.title,
            created_at=record.created_at, updated_at=record.updated_at,
        )


class ToolCallResponse(BaseModel):
    id: str | None
    name: str
    args: dict[str, object]


class MessageResponse(BaseModel):
    role: Literal["user", "assistant", "tool"]
    text: str
    message_id: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCallResponse] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    status: Literal["success", "error"] | None = None


def reasoning_text(message: AIMessage) -> str | None:
    """优先读取标准 reasoning 内容块；旧 DeepSeek 消息回退到附加文本，不重复拼接。"""
    parts: list[object] = [block.get("reasoning", "") for block in message.content_blocks
             if block["type"] == "reasoning"]
    # 原始历史可来自旧 SDK，类型化内容块仍需防御缺失/非字符串 reasoning。
    text = "".join(part for part in parts if isinstance(part, str))
    if not text:
        legacy = message.additional_kwargs.get("reasoning_content")
        text = legacy if isinstance(legacy, str) else ""
    return text or None


def project_history(messages: list[AnyMessage]) -> list[MessageResponse]:
    result: list[MessageResponse] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            result.append(MessageResponse(role="user", text=message.text, message_id=message.id))
        elif isinstance(message, AIMessage):
            result.append(MessageResponse(
                role="assistant", text=message.text, message_id=message.id,
                reasoning=reasoning_text(message),
                tool_calls=[ToolCallResponse(id=call["id"], name=call["name"], args=call["args"])
                            for call in message.tool_calls],
            ))
        elif isinstance(message, ToolMessage):
            result.append(MessageResponse(
                role="tool", text=message.text, message_id=message.id,
                tool_call_id=message.tool_call_id,
                name=message.name, status=message.status,
            ))
    return result


class RunResponse(BaseModel):
    session_id: str
    run_id: str
    status: Literal["completed"] = "completed"
    answer: str
    warnings: list[str] = Field(default_factory=list)


class StrictRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ModelRequest(StrictRequest):
    provider: NonEmptyString
    model: NonEmptyString


class ThinkingRequest(StrictRequest):
    level: NonEmptyString


class ThinkingResponse(BaseModel):
    provider: str
    model: str
    level: str
    reasoning_effort: str
    available_levels: list[str]


class RuntimeResponse(BaseModel):
    configured: bool
    ready: bool
    mutable: bool
    changing: bool
    busy: bool
    running: int
    thinking: ThinkingResponse | None
    message: str


class ProviderResponse(BaseModel):
    provider: str
    logged_in: bool


class ModelResponse(BaseModel):
    provider: str
    model: str
    available_levels: list[str]
    current: bool


class LoginPreviewRequest(StrictRequest):
    provider: NonEmptyString
    api_key: SecretStr | None = Field(default=None, repr=False)

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value


class LoginPreviewResponse(BaseModel):
    login_id: str
    provider: str
    models: list[str]
    from_cache: bool
    expires_in: int


class LoginConfirmRequest(StrictRequest):
    model: NonEmptyString


class OperationError(BaseModel):
    code: str
    message: str


class HttpErrorDetail(OperationError):
    run_id: str | None = None


class HttpErrorResponse(BaseModel):
    error: HttpErrorDetail


class LoginConfirmResponse(BaseModel):
    configuration_saved: bool
    runtime_ready: bool
    runtime: RuntimeResponse
    error: OperationError | None = None


class StreamEvent(BaseModel):
    """版本 1 的网络信封；data 只包含显式投影的 JSON，不序列化原始框架对象。"""

    version: Literal[1] = 1
    seq: int
    type: str
    session_id: str
    run_id: str
    message_id: str | None = None
    tool_call_id: str | None = None
    role: Literal["assistant", "tool"] | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
