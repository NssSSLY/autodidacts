from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://autodidact:autodidact@localhost:5432/autodidact"
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = Field(default=2048, ge=256, le=32768)
    llm_input_usd_per_million: float = Field(default=0.0, ge=0)
    llm_output_usd_per_million: float = Field(default=0.0, ge=0)
    judge_llm_model: str = ""
    judge_llm_base_url: str = ""
    judge_llm_api_key: str = ""
    web_models_config: str = "config/web_models.yaml"
    enabled_web_models: str = ""
    benchmark_snapshot_id: str = ""
    migration_new_api_key: str = ""
    embedding_provider: str = "disabled"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = ""
    search_provider: str = "auto"
    brave_search_api_key: str = ""
    brave_search_base_url: str = "https://api.search.brave.com/res/v1/web/search"
    search_timeout_seconds: float = 30.0
    agent_config: str = "config/agent.yaml"
    http_user_agent: str = "Autodidact/0.1 (+local research agent)"
    log_level: str = "INFO"


class AgentIdentityConfig(BaseModel):
    name: str
    mission: str
    initial_focus: str


class LearningConfig(BaseModel):
    daily_goal_limit: int = 8
    max_sources_per_goal: int = 6
    max_chars_per_source: int = 18000
    min_independent_sources_for_verified_belief: int = 2
    min_evidence_level_for_verified_belief: int = 2
    min_independent_sources_for_dispute_resolution: int = 2
    max_retry: int = 3
    pass_score: float = 0.82
    exploration_rate: float = 0.30
    contradiction_threshold: float = 0.72
    max_follow_up_goals_per_cycle: int = 4
    max_cycles_per_process: int = 20
    cycle_sleep_seconds: int = 30
    evaluation_questions: int = Field(default=3, ge=1, le=8)
    require_independent_verification: bool = True
    max_daily_model_calls: int = Field(default=200, ge=1)
    max_daily_tokens: int = Field(default=1000000, ge=1000)
    max_daily_searches: int = Field(default=100, ge=1)
    max_daily_web_reads: int = Field(default=200, ge=1)
    max_daily_web_model_calls: int = Field(default=20, ge=1)
    max_daily_embedding_calls: int = Field(default=200, ge=1)
    max_daily_estimated_cost_usd: float = Field(default=0.0, ge=0)
    max_response_bytes: int = Field(default=2000000, ge=1024)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_dispute_investigations: int = Field(default=3, ge=1)
    memory_review_days: int = Field(default=30, ge=1)
    auto_consolidate_memory: bool = True


class SourcePolicyConfig(BaseModel):
    model_output_evidence_level: int = 0
    ordinary_web_evidence_level: int = 1
    high_quality_secondary_level: int = 2
    textbook_level: int = 3
    primary_or_official_level: int = 4
    proof_or_reproducible_experiment_level: int = 5


class SafetyConfig(BaseModel):
    external_content_is_untrusted: bool = True
    allow_shell: bool = False
    allow_file_delete: bool = False
    allow_login_automation: bool = False
    allow_captcha_bypass: bool = False


class AgentConfig(BaseModel):
    agent: AgentIdentityConfig
    learning: LearningConfig = Field(default_factory=LearningConfig)
    source_policy: SourcePolicyConfig = Field(default_factory=SourcePolicyConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


@lru_cache
def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()


@lru_cache
def agent_config() -> AgentConfig:
    path = Path(runtime_settings().agent_config)
    if not path.is_file() and runtime_settings().agent_config == "config/agent.yaml":
        from autodidact.resources import resource_root

        path = resource_root() / "config" / "agent.yaml"
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AgentConfig.model_validate(raw)
