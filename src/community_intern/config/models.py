from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from community_intern.ai_response.config import AIConfig
from community_intern.llm.settings import LLMSettings


class AppSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool


class FileRotationSettings(BaseModel):
    """
    Date-based rotation settings (daily).

    This maps cleanly to Python's standard library TimedRotatingFileHandler behavior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_count: int


class FileLoggingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    rotation: FileRotationSettings


class LoggingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str
    file: FileLoggingSettings


class DiscordSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    message_batch_wait_seconds: float
    message_grouping_window_seconds: float = 300.0
    team_member_ids: Sequence[str] = ()


class KnowledgeBaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sources_dir: str
    index_path: str
    index_cache_path: str
    links_file_path: str

    llm: Optional[LLMSettings] = None

    web_fetch_timeout_seconds: float
    web_fetch_cache_dir: str

    url_download_concurrency: int
    summarization_concurrency: int

    url_refresh_min_interval_hours: float
    runtime_refresh_tick_seconds: float
    file_watch_debounce_seconds: float

    max_source_bytes: int

    # KB source summarization prompt
    summarization_prompt: str

    # Team knowledge paths
    team_raw_dir: str = "data/team-knowledge/raw"
    team_topics_dir: str = "data/team-knowledge/topics"
    team_index_path: str = "data/team-knowledge/index-team.txt"
    team_index_cache_path: str = "data/team-knowledge/index-team-cache.json"

    # Team knowledge prompts
    team_classification_prompt: str
    team_integration_prompt: str
    team_summarization_prompt: str
    team_image_summary_prompt: str

    # Team knowledge state
    team_state_path: str = "data/team-knowledge/state.json"
    qa_raw_last_processed_id: str = ""


class AppConfig(BaseModel):
    """
    Effective runtime configuration after applying all precedence rules.

    This is a schema contract only. Loading, validation, and override resolution are not
    implemented at this stage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    app: AppSettings
    logging: LoggingSettings
    discord: DiscordSettings
    ai_response: AIConfig
    kb: KnowledgeBaseSettings


@dataclass(frozen=True, slots=True)
class ConfigLoadRequest:
    """
    Optional inputs for a configuration loader.

    Implementations may use these to control where configuration is read from.
    """

    yaml_path: str = "data/config/config.yaml"
    env_prefix: str = "APP__"
    dotenv_path: Optional[str] = "data/.env"
