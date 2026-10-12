"""Configuration module for vigil.

Loads environment variables from .env and exposes typed Config dataclass.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

# Locate project root and load .env file
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _get_int(key: str, default: int | None = None) -> int | None:
    """Safely parse an integer from environment variables."""
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    """Safely parse a boolean from environment variables."""
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class Config:
    """Application configuration container."""

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_user_id: int | None = _get_int("TELEGRAM_ALLOWED_USER_ID", None)
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model_name: str = os.getenv(
        "LLM_MODEL_NAME", "meta-llama/llama-3.3-70b-instruct"
    )
    database_path: str = os.getenv("DATABASE_PATH", "data/vigil.db")
    heartbeat_interval_minutes: int = _get_int("HEARTBEAT_INTERVAL_MINUTES", 15) or 15
    timezone: str = os.getenv("TIMEZONE", "Asia/Jakarta")
    enable_process_tracing: bool = _get_bool("ENABLE_PROCESS_TRACING", False)
    enable_eval_log: bool = _get_bool("ENABLE_EVAL_LOG", False)
    eval_log_path: str = os.getenv("EVAL_LOG_PATH", "logs/agent_eval.md")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Uppercase aliases matching environment variable names
    @property
    def TELEGRAM_BOT_TOKEN(self) -> str:
        return self.telegram_bot_token

    @property
    def TELEGRAM_ALLOWED_USER_ID(self) -> int | None:
        return self.telegram_allowed_user_id

    @property
    def LLM_API_KEY(self) -> str:
        return self.llm_api_key

    @property
    def LLM_BASE_URL(self) -> str:
        return self.llm_base_url

    @property
    def LLM_MODEL_NAME(self) -> str:
        return self.llm_model_name

    @property
    def DATABASE_PATH(self) -> str:
        return self.database_path

    @property
    def HEARTBEAT_INTERVAL_MINUTES(self) -> int:
        return self.heartbeat_interval_minutes

    @property
    def TIMEZONE(self) -> str:
        return self.timezone

    @property
    def ENABLE_PROCESS_TRACING(self) -> bool:
        return self.enable_process_tracing

    @property
    def ENABLE_EVAL_LOG(self) -> bool:
        return self.enable_eval_log

    @property
    def EVAL_LOG_PATH(self) -> str:
        return self.eval_log_path

    @property
    def LOG_LEVEL(self) -> str:
        """Return a validated logging level string. Falls back to INFO if invalid."""
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        return self.log_level if self.log_level in valid else "INFO"


# Export default ready-to-use instance
config = Config()
