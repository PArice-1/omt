"""稳定的 HTTP 数据结构，不暴露 LangChain 状态或内部消息元数据。"""

from typing import ClassVar, Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omt.core.session_manager import validate_message
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
    tool_calls: list[ToolCallResponse] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    status: Literal["success", "error"] | None = None


def project_history(messages: list[AnyMessage]) -> list[MessageResponse]:
    result: list[MessageResponse] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            result.append(MessageResponse(role="user", text=message.text))
        elif isinstance(message, AIMessage):
            result.append(MessageResponse(
                role="assistant", text=message.text,
                tool_calls=[ToolCallResponse(id=call["id"], name=call["name"], args=call["args"])
                            for call in message.tool_calls],
            ))
        elif isinstance(message, ToolMessage):
            result.append(MessageResponse(
                role="tool", text=message.text, tool_call_id=message.tool_call_id,
                name=message.name, status=message.status,
            ))
    return result


class RunResponse(BaseModel):
    session_id: str
    run_id: str
    status: Literal["completed"] = "completed"
    answer: str
    warnings: list[str] = Field(default_factory=list)
