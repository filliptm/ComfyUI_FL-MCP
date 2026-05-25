"""Provider and model configuration with intelligent defaults."""

from typing import Dict, Literal
import logging

logger = logging.getLogger(__name__)

# Default model for each provider
DEFAULT_MODELS: Dict[str, str] = {
    "openai": "gpt-4-turbo-preview",
    "anthropic": "claude-3-5-sonnet-20241022",
    "gemini": "gemini-2.0-flash-exp",
    "openrouter": "anthropic/claude-sonnet-4.5",
    "local": "local-model"
}

# Provider-specific tuning for message history and compression
PROVIDER_TUNING: Dict[str, Dict[str, int]] = {
    "openai": {
        "history_limit": 36,
        "max_chars": 2000
    },
    "anthropic": {
        "history_limit": 50,
        "max_chars": 4000
    },
    "gemini": {
        "history_limit": 16,  # Gemini has stricter JSON generation limits
        "max_chars": 1000     # Compress tool results more aggressively
    },
    "openrouter": {
        "history_limit": 36,
        "max_chars": 2000
    },
    "local": {
        "history_limit": 16,
        "max_chars": 1000
    }
}


def get_default_model(provider: str) -> str:
    """Get default model for a provider.
    
    Args:
        provider: Provider name
        
    Returns:
        Default model string
        
    Raises:
        ValueError: If provider unknown
    """
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown provider: {provider}")
    return DEFAULT_MODELS[provider]


def validate_provider_model(provider: str, model: str) -> bool:
    """Validate that model string is compatible with provider.
    
    Args:
        provider: Provider name
        model: Model string to validate
        
    Returns:
        True if valid, False otherwise
    """
    patterns = {
        "openai": ["gpt-", "o1-", "o3-", "text-"],
        "anthropic": ["claude-"],
        "gemini": ["gemini-", "models/gemini-"],
        "openrouter": ["*"],  # OpenRouter accepts any format
        "local": ["*"]
    }
    
    if provider not in patterns:
        return False
    
    # OpenRouter accepts anything
    if "*" in patterns[provider]:
        return True
    
    # Check if model starts with any valid prefix
    return any(model.startswith(prefix) for prefix in patterns[provider])


def get_provider_tuning(provider: str) -> Dict[str, int]:
    """Get tuning parameters for a provider.
    
    Args:
        provider: Provider name
        
    Returns:
        Dict with history_limit and max_chars
    """
    return PROVIDER_TUNING.get(provider, PROVIDER_TUNING["openrouter"])
