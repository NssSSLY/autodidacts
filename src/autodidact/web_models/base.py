from abc import ABC, abstractmethod

from autodidact.schemas import ModelAnswer


class ExternalCognitiveSource(ABC):
    @abstractmethod
    async def ask(self, prompt: str) -> ModelAnswer:
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> bool:
        raise NotImplementedError
