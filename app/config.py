from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings (secrets live here, never in code)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    reddit_client: str = "real"  # real | public (no credentials, read-only) | fake
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_user_agent: str = "python:reddit-reachout-assistant:v0.1"

    llm_provider: str = "anthropic"  # anthropic | fake
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_base_url: str = "https://api.anthropic.com"

    database_url: str = "sqlite:///data/app.db"
    monitor_config_path: str = "config/monitoring.yaml"
    monitor_enabled: bool = True
    monitor_interval_seconds: int = 3600
    monitor_initial_delay_seconds: int = 10

    reddit_posting_enabled: bool = False
    max_posts_per_day: int = 10
    api_token: str = ""


class ProductConfig(BaseModel):
    name: str = "Our product"
    description: str = ""
    guidelines: str = ""


class FilterConfig(BaseModel):
    min_text_length: int = 25
    require_signal: bool = True


class FetchConfig(BaseModel):
    initial_lookback_hours: int = 24
    max_posts_per_run: int = 100
    max_comments_per_thread: int = 40
    max_comment_chars: int = 800
    max_llm_calls_per_run: int = 30


class ThresholdConfig(BaseModel):
    min_relevance: float = 0.5
    min_confidence: float = 0.5
    monitor_min_relevance: float = 0.3


class ReevaluationConfig(BaseModel):
    min_new_comments: int = 2
    cooldown_hours: float = 6
    max_age_days: int = 7


class MonitoringConfig(BaseModel):
    product: ProductConfig = Field(default_factory=ProductConfig)
    subreddits: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    ignored_authors: list[str] = Field(default_factory=list)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    reevaluation: ReevaluationConfig = Field(default_factory=ReevaluationConfig)


def load_monitoring_config(path: str) -> MonitoringConfig:
    p = Path(path)
    if not p.exists():
        return MonitoringConfig()
    return MonitoringConfig.model_validate(yaml.safe_load(p.read_text()) or {})


@lru_cache
def get_settings() -> Settings:
    return Settings()
