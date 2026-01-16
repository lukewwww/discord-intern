from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from community_intern.ai.interfaces import AIConfig


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


class KnowledgeBaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sources_dir: str
    index_path: str
    index_cache_path: str
    links_file_path: str

    web_fetch_timeout_seconds: float
    web_fetch_cache_dir: str

    url_refresh_min_interval_seconds: float
    runtime_refresh_tick_seconds: float
    file_watch_debounce_seconds: float

    max_source_bytes: int
    max_snippet_chars: int
    max_snippets_per_query: int
    max_sources_per_query: int


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
    ai: AIConfig
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
