from autodidact.brain.llm import MockLLM, OpenAICompatibleLLM
from autodidact.brain.observed import ObservedLLM
from autodidact.config import runtime_settings


def build_judge(engine):
    settings = runtime_settings()
    if settings.llm_provider == "mock" and not settings.judge_llm_model:
        return ObservedLLM(MockLLM(), engine, role="judge")
    judge = settings.model_copy(
        update={
            "llm_model": settings.judge_llm_model or settings.llm_model,
            "llm_base_url": settings.judge_llm_base_url or settings.llm_base_url,
            "llm_api_key": settings.judge_llm_api_key or settings.llm_api_key,
        }
    )
    return ObservedLLM(OpenAICompatibleLLM(judge), engine, role="judge")
