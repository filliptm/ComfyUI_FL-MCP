"""Configuration management for FL_JS backend."""

from typing import Literal
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Backend Auto-Start ===
    auto_start_backend: bool = True
    auto_restart_backend: bool = True
    log_backend_to_file: bool = True

    # LLM Provider Configuration
    llm_provider: Literal["openai", "anthropic", "gemini", "openrouter"] = "openrouter"
    llm_model: str = "deepseek/deepseek-chat"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 32000

    # API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    openrouter_api_key: str = ""

    # WebSocket Settings
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000
    ws_heartbeat_interval: int = 30
    ws_session_timeout: int = 300
    ws_max_reconnect_attempts: int = 5

    # Connection Limits
    max_connections_per_ip: int = 10
    max_message_size: int = 1000000

    # Tool Execution
    tool_timeout: int = 30000  # milliseconds
    max_tool_retries: int = 3

    # Conversation
    conversation_max_history: int = 50

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    def get_api_key(self) -> str:
        """Get the API key for the configured LLM provider.

        Returns:
            API key string

        Raises:
            ValueError: If API key is not configured
        """
        if self.llm_provider == "openai":
            if not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            return self.openai_api_key
        elif self.llm_provider == "anthropic":
            if not self.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            return self.anthropic_api_key
        elif self.llm_provider == "gemini":
            if not self.google_api_key:
                raise ValueError("GOOGLE_API_KEY not configured")
            return self.google_api_key
        elif self.llm_provider == "openrouter":
            if not self.openrouter_api_key:
                raise ValueError("OPENROUTER_API_KEY not configured")
            return self.openrouter_api_key
        else:
            raise ValueError(f"Unknown LLM provider: {self.llm_provider}")


# Global settings instance
settings = Settings()
