"""Configuration management for FL_JS backend."""

from typing import Dict, Literal, Optional
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
import logging

from model_defaults import (
    get_default_model,
    validate_provider_model,
    get_provider_tuning
)

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Backend Launch Configuration ===
    backend_launch_mode: Literal["auto", "terminal", "subprocess", "manual"] = "subprocess"
    auto_start_backend: bool = False
    auto_restart_backend: bool = True  # Only applies to subprocess mode
    log_backend_to_file: bool = True   # Only applies to subprocess mode

    # LLM Provider Configuration
    llm_provider: Literal["openai", "anthropic", "gemini", "openrouter", "local"] = "gemini"
    local_llm_url: Optional[str] = None # For use with local hosting like ollama or vllm
    llm_model: Optional[str] = None  # If None, uses provider default
    llm_temperature: float = 0.7
    llm_max_tokens: int = 32000

    # API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    openrouter_api_key: str = ""
    local_api_key: str = ""

    # WebSocket Settings
    ws_host: str = "127.0.0.1"
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

    # ComfyUI Server Configuration
    comfyui_server_url: str = "http://127.0.0.1:8188"
    comfyui_api_timeout: int = 10  # seconds

    # Conversation
    conversation_max_history: int = 50

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # Public URL Configuration (for ngrok/production)
    public_url: str = "http://127.0.0.1:8000"  # Default to 127.0.0.1

    @property
    def resolved_model(self) -> str:
        """Get the model to use, falling back to provider default if not specified.
        
        Returns:
            Model string to use
        """
        if self.llm_model:
            # Validate explicit model against provider
            if not validate_provider_model(self.llm_provider, self.llm_model):
                logger.warning(
                    f"Model '{self.llm_model}' may not be compatible with "
                    f"provider '{self.llm_provider}'. Proceeding anyway."
                )
            return self.llm_model
        
        # Use default for provider
        default_model = get_default_model(self.llm_provider)
        logger.info(f"Using default model '{default_model}' for provider '{self.llm_provider}'")
        return default_model
    
    @property
    def provider_tuning(self) -> Dict[str, int]:
        """Get tuning parameters for current provider.
        
        Returns:
            Dict with history_limit and max_chars
        """
        return get_provider_tuning(self.llm_provider)

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
