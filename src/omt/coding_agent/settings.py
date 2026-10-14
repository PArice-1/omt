# 读取setting
from typing import Annotated
from pathlib import Path
from functools import cache

from pydantic import Field
from pydantic_settings import BaseSettings

root = Path(".")

class Settings(BaseSettings):
    """Setting
    agent基础配置
    file: settings.json
    """

    defaultProvider: Annotated[str, Field(min_length=1, description="默认模型提供商")] 
    defaultModel: str

    '''optimizer
    1. defaultProvider 可以用Literal来限制, 可以先读取model-store.json里面的模型
        然后判断这个default是不是在里面，防止用户瞎改
    '''

@cache
def load_settings() -> Settings:
    # settings.json position
    settings_path = root / "agent" / "settings.json"

    return  Settings.model_validate_json(
    settings_path.read_text(encoding="utf-8")
    )

