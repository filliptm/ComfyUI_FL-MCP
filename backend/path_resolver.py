"""Path resolution and merging for ComfyUI model paths.

Handles resolving base_path + relative paths, pipe-separated multiple paths,
and merging with default paths.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional
from comfy_models import ComfyFolderType
from extra_model_paths_loader import ExtraModelPathsConfig

logger = logging.getLogger(__name__)


class PathResolver:
    """Resolves and merges ComfyUI model paths."""
    
    # Mapping from YAML keys to ComfyFolderType enum
    YAML_KEY_TO_FOLDER_TYPE = {
        'checkpoints': ComfyFolderType.CHECKPOINTS,
        'loras': ComfyFolderType.LORAS,
        'vae': ComfyFolderType.VAE,
        'controlnet': ComfyFolderType.CONTROLNET,
        'upscale_models': ComfyFolderType.UPSCALE_MODELS,
        'embeddings': ComfyFolderType.EMBEDDINGS,
        'custom_nodes': ComfyFolderType.CUSTOM_NODES,
        # These exist in YAML but not in our enum yet - skip them
        'clip': None,
        'clip_vision': None,
        'configs': None,
        'diffusion_models': None,
        'hypernetworks': None,
        'gligen': None,
        'unet': None,
    }
    
    def __init__(self, comfyui_root: Path):
        """Initialize resolver.
        
        Args:
            comfyui_root: ComfyUI installation root for validation
        """
        self.comfyui_root = comfyui_root
    
    def get_default_mappings(self) -> Dict[ComfyFolderType, List[Path]]:
        """Get default folder mappings.
        
        Returns:
            Dict mapping folder types to default paths (as single-item lists)
        """
        return {
            ComfyFolderType.CUSTOM_NODES: [self.comfyui_root / "custom_nodes"],
            ComfyFolderType.MODELS: [self.comfyui_root / "models"],
            ComfyFolderType.CHECKPOINTS: [self.comfyui_root / "models" / "checkpoints"],
            ComfyFolderType.LORAS: [self.comfyui_root / "models" / "loras"],
            ComfyFolderType.VAE: [self.comfyui_root / "models" / "vae"],
            ComfyFolderType.CONTROLNET: [self.comfyui_root / "models" / "controlnet"],
            ComfyFolderType.UPSCALE_MODELS: [self.comfyui_root / "models" / "upscale_models"],
            ComfyFolderType.EMBEDDINGS: [self.comfyui_root / "models" / "embeddings"],
            ComfyFolderType.OUTPUT: [self.comfyui_root / "output"],
            ComfyFolderType.INPUT: [self.comfyui_root / "input"],
            ComfyFolderType.TEMP: [self.comfyui_root / "temp"],
            ComfyFolderType.WORKFLOWS: [self.comfyui_root / "user" / "default" / "workflows"],
        }
    
    def merge_with_extra_paths(
        self,
        default_mappings: Dict[ComfyFolderType, List[Path]],
        extra_configs: List[ExtraModelPathsConfig]
    ) -> Dict[ComfyFolderType, List[Path]]:
        """Merge default paths with extra_model_paths.yaml configs.
        
        Args:
            default_mappings: Default folder mappings
            extra_configs: Parsed extra_model_paths configs
            
        Returns:
            Merged mappings with extra paths prepended (higher priority)
        """
        if not extra_configs:
            return default_mappings
        
        # Start with defaults (deep copy to avoid mutation)
        merged = {k: list(v) for k, v in default_mappings.items()}
        
        # Process each config block
        for config in extra_configs:
            base_path = Path(config.base_path)
            
            # Resolve each model type path
            for yaml_key, path_value in config.paths.items():
                # Skip metadata keys
                if yaml_key in ('is_default',):
                    continue
                
                # Skip non-string values (is_default could be bool)
                if not isinstance(path_value, str):
                    continue
                
                # Map YAML key to folder type
                folder_type = self.YAML_KEY_TO_FOLDER_TYPE.get(yaml_key)
                if folder_type is None:
                    logger.debug(f"Skipping unmapped YAML key: {yaml_key}")
                    continue
                
                # Parse path value (handle pipe-separated multiple paths)
                resolved_paths = self._resolve_path_value(base_path, path_value)
                
                # Validate paths exist
                valid_paths = [p for p in resolved_paths if self._validate_path(p)]
                
                if not valid_paths:
                    logger.warning(
                        f"No valid paths found for {yaml_key} in config '{config.config_name}'"
                    )
                    continue
                
                # Add to merged mappings
                if folder_type not in merged:
                    merged[folder_type] = []
                
                # Prepend extra paths (they take priority over defaults)
                for path in valid_paths:
                    if path not in merged[folder_type]:
                        merged[folder_type].insert(0, path)
                
                logger.debug(
                    f"Added {len(valid_paths)} path(s) for {folder_type.value} "
                    f"from config '{config.config_name}'"
                )
        
        return merged
    
    def _resolve_path_value(self, base_path: Path, path_value: str) -> List[Path]:
        """Resolve a path value from YAML (handles pipe-separated multiple paths).
        
        Args:
            base_path: Base path for relative resolution
            path_value: Path value from YAML (may contain pipes)
            
        Returns:
            List of resolved absolute paths
        """
        # Handle pipe-separated multiple paths
        if '|' in path_value:
            raw_paths = [p.strip() for p in path_value.split('|') if p.strip()]
        else:
            raw_paths = [path_value.strip()]
        
        resolved = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            
            # If relative, resolve against base_path
            if not path.is_absolute():
                path = base_path / path
            
            # Resolve symlinks and normalize
            try:
                path = path.resolve()
                resolved.append(path)
            except (OSError, RuntimeError) as e:
                logger.warning(f"Failed to resolve path '{raw_path}': {e}")
                continue
        
        return resolved
    
    def _validate_path(self, path: Path) -> bool:
        """Validate that a path exists and is accessible.
        
        Args:
            path: Path to validate
            
        Returns:
            True if path exists and is accessible, False otherwise
        """
        try:
            return path.exists() and path.is_dir()
        except (OSError, PermissionError) as e:
            logger.debug(f"Path validation failed for {path}: {e}")
            return False
