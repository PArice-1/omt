'''models_manager
管理可以提供服务的模型
'''
from typing import Callable

from pydantic import HttpUrl, ValidationError
from langchain.chat_models import init_chat_model

from .models.deepseek import deepseek_init


models = {
    "deepseek": deepseek_init, 
}

def models_provider(
        provider: str,
        *,
        model: str,
        base_url: HttpUrl,
        args: dict
):
    # 查找模型
    if not provider in models:
        raise ValueError("改模型未找到")
    else:
        model_init_fun = models[provider]

    try:
        model = model_init_fun(
            provider,
            model=model,
            base_url=base_url,
            **args
        )
    except ValidationError as exc:
        raise ValueError(f"[参数错误]: {exc}") from exc

    return model

        
    