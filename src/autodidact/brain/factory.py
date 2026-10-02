# 文件职责：构造可选独立裁判模型，并添加与学习模型一致的调用审计。
from autodidact.brain.llm import MockLLM, OpenAICompatibleLLM
from autodidact.brain.observed import ObservedLLM
from autodidact.config import runtime_settings


# 功能：应用可选裁判模型/服务/密钥配置，留空时复用学习配置，返回带 judge 角色的审计模型。
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
