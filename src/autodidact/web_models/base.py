# 文件职责：声明外部认知来源的提问与健康检查协议，模型回答保持观察身份。
from abc import ABC, abstractmethod

from autodidact.schemas import ModelAnswer


class ExternalCognitiveSource(ABC):
    # 功能：声明向合法授权的外部认知来源提问并返回 ModelAnswer 的协议。
    @abstractmethod
    async def ask(self, prompt: str) -> ModelAnswer:
        raise NotImplementedError

    # 功能：声明适配器可用性检查，不代表模型答案可信。
    @abstractmethod
    async def health_check(self) -> bool:
        raise NotImplementedError
