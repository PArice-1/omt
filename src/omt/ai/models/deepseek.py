

from langchain.chat_models import init_chat_model
from pydantic import validate_call, HttpUrl, SecretStr



@validate_call
def deepseek_init(
    provider: str,
    *,
    model: str,
    base_url: HttpUrl,
    api_key: SecretStr,
    **args,
):
    '''deepseek_init
    初始化deepseek的model
    ''' 

    key = api_key.get_secret_value()
    if not key.strip():
        raise ValueError(f"{model} 的 API_KEY 不能为空") 


    return init_chat_model(
        model_provider=provider,
        model=model,
        api_key=api_key,
        base_url=str(base_url),
        **args,
    )


