'''model_manager
隐藏模型细节，向外提供model
'''
from pydantic import HttpUrl, SecretStr

from .models_manager import models_provider

class Model:
    '''Model
    模型接口
    '''

    def __init__(
        self,
        provider: str,
        *,
        model: str,
        base_url: HttpUrl,
        **kwargs,
    ) -> None:

        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.model = self.init_model(
            provider,
            model=model,
            base_url=base_url,
            args=kwargs,
        )

    def init_model(
        self,
        provider: str,
        *,
        model: str,
        base_url: HttpUrl,
        args: dict,
    ): 
        '''get_model
        获取模型，用args隐藏参数，在model侧进行参数检测
        '''

        return models_provider(
            provider,
            model=model,
            base_url=base_url,
            args=args,
        )






