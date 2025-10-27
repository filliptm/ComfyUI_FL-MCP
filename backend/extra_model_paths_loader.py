"""Loader for ComfyUI extra_model_paths.yaml configuration.

Provides parsing and validation of ComfyUI's extra model paths
configuration file without path resolution.
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
import yaml

logger = logging.getLogger(__name__)


class ExtraModelPathsConfig:
    """Structured representation of extra_model_paths.yaml config block."""
    
    def __init__(self, config_name: str, base_path: str, paths: Dict[str, Any]):
        """Initialize config block.
        
        Args:
            config_name: Name of the config block (e.g., 'a111', 'comfyui')
            base_path: Base path for relative path resolution
            paths: Dict of model_type -> path_value (may contain pipes)
        """
        self.config_name = config_name
        self.base_path = base_path
        self.paths = paths
        self.is_default = paths.get('is_default', False)
    
    def __repr__(self):
        return f"ExtraModelPathsConfig(name={self.config_name}, base={self.base_path}, types={len(self.paths)})"


class ExtraModelPathsLoader:
    """Loads and parses extra_model_paths.yaml configuration."""
    
    YAML_FILENAME = "extra_model_paths.yaml"
    YAML_EXAMPLE_FILENAME = "extra_model_paths.yaml.example"
    
    def __init__(self, comfyui_root: Path):
        """Initialize loader.
        
        Args:
            comfyui_root: Path to ComfyUI installation root
        """
        self.comfyui_root = comfyui_root
    
    def find_yaml_file(self) -> Optional[Path]:
        """Find extra_model_paths.yaml in expected locations.
        
        Searches in priority order:
        1. ComfyUI root directory
        2. Platform-specific config directories
        3. Environment variable override
        
        Returns:
            Path to YAML file if found, None otherwise
        """
        locations = [
            # 1. ComfyUI root directory (highest priority)
            self.comfyui_root / self.YAML_FILENAME,
            
            # 2. Environment variable override
            self._get_env_config_path(),
            
            # 3. Platform-specific config directories
            self._get_platform_config_path(),
        ]
        
        for location in locations:
            if location and location.exists() and location.is_file():
                logger.info(f"Found extra_model_paths.yaml at: {location}")
                return location
        
        logger.debug("No extra_model_paths.yaml found, using default paths only")
        return None
    
    def _get_platform_config_path(self) -> Optional[Path]:
        """Get platform-specific config directory path.
        
        Returns:
            Platform-specific config path, or None if not applicable
        """
        if os.name == 'nt':  # Windows
            appdata = os.getenv('APPDATA')
            if appdata:
                return Path(appdata) / "ComfyUI" / self.YAML_FILENAME
        else:  # Linux/Mac
            home = Path.home()
            return home / ".config" / "ComfyUI" / self.YAML_FILENAME
        return None
    
    def _get_env_config_path(self) -> Optional[Path]:
        """Get path from environment variable.
        
        Returns:
            Path from COMFYUI_EXTRA_MODEL_PATHS env var, or None
        """
        env_path = os.getenv('COMFYUI_EXTRA_MODEL_PATHS')
        if env_path:
            return Path(env_path)
        return None
    
    def load(self) -> List[ExtraModelPathsConfig]:
        """Load and parse extra_model_paths.yaml.
        
        Returns:
            List of ExtraModelPathsConfig objects (one per config block)
            Empty list if file not found or parsing fails
        """
        yaml_path = self.find_yaml_file()
        if not yaml_path:
            return []
        
        try:
            return self._parse_yaml(yaml_path)
        except Exception as e:
            logger.error(f"Failed to parse {yaml_path}: {e}")
            return []
    
    def _parse_yaml(self, yaml_path: Path) -> List[ExtraModelPathsConfig]:
        """Parse YAML file into structured configs.
        
        Args:
            yaml_path: Path to YAML file
            
        Returns:
            List of ExtraModelPathsConfig objects
            
        Raises:
            yaml.YAMLError: If YAML is malformed
            ValueError: If required fields missing
        """
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        if not data or not isinstance(data, dict):
            logger.warning(f"Empty or invalid YAML structure in {yaml_path}")
            return []
        
        configs = []
        for config_name, config_data in data.items():
            if not isinstance(config_data, dict):
                logger.warning(f"Skipping invalid config block: {config_name}")
                continue
            
            # Extract base_path (required)
            base_path = config_data.get('base_path')
            if not base_path:
                logger.warning(f"Config '{config_name}' missing base_path, skipping")
                continue
            
            # Extract all other keys as model type paths
            paths = {k: v for k, v in config_data.items() if k != 'base_path'}
            
            configs.append(ExtraModelPathsConfig(
                config_name=config_name,
                base_path=str(base_path),
                paths=paths
            ))
        
        logger.info(f"Loaded {len(configs)} config block(s) from {yaml_path}")
        return configs
