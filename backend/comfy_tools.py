"""ComfyUI filesystem utilities for FL_JS agent system.

Provides secure, deterministic access to ComfyUI directory structure
for agent-based analysis and discovery.
"""

import os
import re
import logging
from pathlib import Path
from typing import List, Dict, Optional, Union
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ComfyFolderType(str, Enum):
    """Supported ComfyUI folder types."""
    CUSTOM_NODES = "custom_nodes"
    MODELS = "models"
    CHECKPOINTS = "checkpoints"
    LORAS = "loras"
    VAE = "vae"
    CONTROLNET = "controlnet"
    UPSCALE_MODELS = "upscale_models"
    EMBEDDINGS = "embeddings"
    OUTPUT = "output"
    INPUT = "input"
    TEMP = "temp"


@dataclass
class ComfyFileInfo:
    """Information about a ComfyUI file or directory."""
    name: str
    path: str
    is_directory: bool
    size: Optional[int] = None
    modified_time: Optional[float] = None
    extension: Optional[str] = None


@dataclass
class ComfySearchResult:
    """Result from ComfyUI file pattern search."""
    file_path: str
    line_number: int
    line_content: str
    context_before: List[str]
    context_after: List[str]


class ComfyUIError(Exception):
    """Base exception for ComfyUI tool errors."""
    pass


class ComfyUINotFoundError(ComfyUIError):
    """Raised when ComfyUI installation cannot be located."""
    pass


class ComfyUISecurityError(ComfyUIError):
    """Raised when attempting to access files outside ComfyUI directory."""
    pass


class ComfyUITools:
    """Core ComfyUI filesystem utilities."""
    
    def __init__(self, comfyui_root: Optional[str] = None):
        """Initialize ComfyUI tools with auto-detection."""
        self.comfyui_root = Path(comfyui_root) if comfyui_root else self._find_comfyui_root()
        self._validate_comfyui_installation()
        
        # Define folder mappings
        self.folder_mappings = {
            ComfyFolderType.CUSTOM_NODES: "custom_nodes",
            ComfyFolderType.MODELS: "models",
            ComfyFolderType.CHECKPOINTS: "models/checkpoints",
            ComfyFolderType.LORAS: "models/loras",
            ComfyFolderType.VAE: "models/vae",
            ComfyFolderType.CONTROLNET: "models/controlnet",
            ComfyFolderType.UPSCALE_MODELS: "models/upscale_models",
            ComfyFolderType.EMBEDDINGS: "models/embeddings",
            ComfyFolderType.OUTPUT: "output",
            ComfyFolderType.INPUT: "input",
            ComfyFolderType.TEMP: "temp",
        }
        
        # Safe file extensions for reading
        self.safe_read_extensions = {
            '.py', '.json', '.yaml', '.yml', '.toml', '.txt', '.md', '.rst',
            '.cfg', '.ini', '.conf', '.log', '.csv', '.xml', '.html', '.js', 
            '.css', '.sh', '.bat'
        }
        
        logger.info(f"ComfyUI tools initialized for: {self.comfyui_root}")
    
    def _find_comfyui_root(self) -> Path:
        """Auto-detect ComfyUI installation directory."""
        # Get current FL_JS project root
        current_dir = Path(__file__).parent.parent  # backend -> fl_js root
        
        # Check if we have a symlinked ComfyUI in the project
        project_comfyui = current_dir / "ComfyUI"
        if project_comfyui.exists() and (project_comfyui / "nodes.py").exists():
            logger.info(f"Found ComfyUI via project symlink: {project_comfyui}")
            return project_comfyui.resolve()
        
        # Check if we're running as a ComfyUI custom node
        custom_node_root = current_dir.parent  # fl_js -> custom_nodes
        if (custom_node_root.name == "custom_nodes" and 
            (custom_node_root.parent / "nodes.py").exists()):
            comfyui_root = custom_node_root.parent
            logger.info(f"Found ComfyUI via custom node installation: {comfyui_root}")
            return comfyui_root
        
        # Check common locations
        common_paths = [
            Path("/ComfyUI"),
            Path("~/ComfyUI").expanduser(),
            Path("../ComfyUI"),
            Path("../../ComfyUI"),
        ]
        
        # Add environment variable if set
        env_path = os.environ.get("COMFYUI_PATH")
        if env_path:
            common_paths.insert(0, Path(env_path))
        
        for path in common_paths:
            if path.exists() and (path / "nodes.py").exists():
                logger.info(f"Found ComfyUI at: {path}")
                return path.resolve()
        
        raise ComfyUINotFoundError(
            "ComfyUI installation not found. Set COMFYUI_PATH environment variable."
        )
    
    def _validate_comfyui_installation(self) -> None:
        """Validate that directory is a valid ComfyUI installation."""
        required_files = ["nodes.py", "folder_paths.py"]
        required_dirs = ["custom_nodes", "models", "output"]
        
        for file in required_files:
            if not (self.comfyui_root / file).exists():
                raise ComfyUINotFoundError(
                    f"Invalid ComfyUI installation: missing {file}"
                )
        
        for dir in required_dirs:
            if not (self.comfyui_root / dir).exists():
                raise ComfyUINotFoundError(
                    f"Invalid ComfyUI installation: missing {dir}/ directory"
                )
    
    def _validate_path(self, path: str) -> Path:
        """Validate path is within ComfyUI directory."""
        try:
            full_path = (self.comfyui_root / path).resolve()
            
            # Ensure path is within ComfyUI directory
            if not str(full_path).startswith(str(self.comfyui_root)):
                raise ComfyUISecurityError(
                    f"Path outside ComfyUI directory: {path}"
                )
            
            return full_path
            
        except Exception as e:
            raise ComfyUISecurityError(f"Invalid path: {path} - {e}")
    
    def list_folders(self, folder_type: Union[str, ComfyFolderType]) -> List[ComfyFileInfo]:
        """List contents of a ComfyUI directory by type."""
        try:
            # Convert string to enum if needed
            if isinstance(folder_type, str):
                folder_type = ComfyFolderType(folder_type)
            
            # Get folder path
            folder_path = self.folder_mappings.get(folder_type)
            if not folder_path:
                raise ComfyUIError(f"Unknown folder type: {folder_type}")
            
            # Validate and resolve path
            full_path = self._validate_path(folder_path)
            
            if not full_path.exists():
                logger.warning(f"Directory does not exist: {full_path}")
                return []
            
            if not full_path.is_dir():
                raise ComfyUIError(f"Path is not a directory: {folder_path}")
            
            # List directory contents
            items = []
            for item in full_path.iterdir():
                try:
                    stat = item.stat()
                    items.append(ComfyFileInfo(
                        name=item.name,
                        path=str(item.relative_to(self.comfyui_root)),
                        is_directory=item.is_dir(),
                        size=stat.st_size if item.is_file() else None,
                        modified_time=stat.st_mtime,
                        extension=item.suffix.lower() if item.is_file() else None
                    ))
                except (OSError, PermissionError) as e:
                    logger.warning(f"Cannot access {item}: {e}")
                    continue
            
            # Sort: directories first, then by name
            items.sort(key=lambda x: (not x.is_directory, x.name.lower()))
            
            logger.info(f"Listed {len(items)} items in {folder_path}")
            return items
            
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"Error listing folder {folder_type}: {e}")
    
    def read_file(self, path: str, max_size: int = 1024 * 1024) -> str:
        """Read a text file within the ComfyUI directory."""
        try:
            # Validate path
            full_path = self._validate_path(path)
            
            if not full_path.exists():
                raise ComfyUIError(f"File does not exist: {path}")
            
            if not full_path.is_file():
                raise ComfyUIError(f"Path is not a file: {path}")
            
            # Check file size
            file_size = full_path.stat().st_size
            if file_size > max_size:
                raise ComfyUIError(
                    f"File too large: {file_size} bytes (max: {max_size})"
                )
            
            # Check file extension for safety
            if full_path.suffix.lower() not in self.safe_read_extensions:
                logger.warning(
                    f"Reading potentially unsafe file type: {full_path.suffix}"
                )
            
            # Read file
            try:
                content = full_path.read_text(encoding='utf-8')
                logger.info(f"Read file: {path} ({len(content)} chars)")
                return content
            except UnicodeDecodeError:
                # Try with fallback encoding
                content = full_path.read_text(encoding='latin-1')
                logger.warning(f"File read with fallback encoding: {path}")
                return content
                
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"Error reading file {path}: {e}")
    
    def search_files(
        self,
        pattern: str,
        folder_type: Union[str, ComfyFolderType] = ComfyFolderType.CUSTOM_NODES,
        file_pattern: Optional[str] = None,
        max_results: int = 20,
        context_lines: int = 2
    ) -> List[ComfySearchResult]:
        """Search for pattern in files within a ComfyUI directory."""
        try:
            # Convert string to enum if needed
            if isinstance(folder_type, str):
                folder_type = ComfyFolderType(folder_type)
            
            # Get folder path
            folder_path = self.folder_mappings.get(folder_type)
            if not folder_path:
                raise ComfyUIError(f"Unknown folder type: {folder_type}")
            
            # Validate path
            full_path = self._validate_path(folder_path)
            
            if not full_path.exists():
                logger.warning(f"Search directory does not exist: {full_path}")
                return []
            
            # Compile regex pattern
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            
            results = []
            files_searched = 0
            
            # Search files recursively
            for file_path in full_path.rglob(file_pattern or "*"):
                if not file_path.is_file():
                    continue
                
                # Skip binary files and very large files
                if file_path.suffix.lower() not in self.safe_read_extensions:
                    continue
                
                try:
                    file_size = file_path.stat().st_size
                    if file_size > 1024 * 1024:  # Skip files > 1MB
                        continue
                    
                    # Read and search file
                    content = file_path.read_text(encoding='utf-8', errors='ignore')
                    lines = content.split('\n')
                    
                    for line_num, line in enumerate(lines, 1):
                        if regex.search(line):
                            # Extract context
                            start_line = max(0, line_num - context_lines - 1)
                            end_line = min(len(lines), line_num + context_lines)
                            
                            context_before = lines[start_line:line_num-1]
                            context_after = lines[line_num:end_line]
                            
                            results.append(ComfySearchResult(
                                file_path=str(file_path.relative_to(self.comfyui_root)),
                                line_number=line_num,
                                line_content=line.strip(),
                                context_before=context_before,
                                context_after=context_after
                            ))
                            
                            if len(results) >= max_results:
                                logger.info(f"Search truncated at {max_results} results")
                                return results
                    
                    files_searched += 1
                    
                except (OSError, UnicodeDecodeError) as e:
                    logger.debug(f"Skipped file {file_path}: {e}")
                    continue
            
            logger.info(f"Search complete: {len(results)} matches in {files_searched} files")
            return results
            
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"Error searching files: {e}")

    def extract_workflow_from_image(self, image_path: str) -> Optional[Dict[str, Any]]:
        """Extract ComfyUI workflow from PNG metadata.
        
        Args:
            image_path: Path to PNG file relative to ComfyUI root
            
        Returns:
            Workflow dictionary if found, None if no metadata
            
        Raises:
            ComfyUIError: If file access fails or is invalid
        """
        try:
            from PIL import Image
            import json
            
            # Validate and resolve path
            full_path = self._validate_path(image_path)
            
            if not full_path.exists():
                raise ComfyUIError(f"Image file does not exist: {image_path}")
            
            if not full_path.is_file():
                raise ComfyUIError(f"Path is not a file: {image_path}")
            
            # Check file extension
            if full_path.suffix.lower() not in ['.png', '.webp']:
                raise ComfyUIError(
                    f"Unsupported file format: {full_path.suffix}. "
                    "Only PNG and WebP files contain workflow metadata."
                )
            
            # Open image and extract metadata
            img = Image.open(full_path)
            
            # Try to get workflow from metadata
            workflow_json = None
            
            # Method 1: Check img.text attribute (PNG tEXt chunks)
            if hasattr(img, 'text') and 'workflow' in img.text:
                workflow_json = img.text['workflow']
            
            # Method 2: Check img.info dictionary (fallback)
            elif 'workflow' in img.info:
                workflow_json = img.info['workflow']
            
            # No workflow found
            if not workflow_json:
                logger.info(f"No workflow metadata found in: {image_path}")
                return None
            
            # Parse JSON
            try:
                workflow = json.loads(workflow_json)
                logger.info(
                    f"Extracted workflow from {image_path}: "
                    f"{len(workflow.get('nodes', []))} nodes, "
                    f"version {workflow.get('version', 'unknown')}"
                )
                return workflow
            except json.JSONDecodeError as e:
                raise ComfyUIError(f"Invalid workflow JSON in image metadata: {e}")
            
        except ComfyUIError:
            raise
        except ImportError:
            raise ComfyUIError(
                "PIL (Pillow) not available. Install with: pip install pillow"
            )
        except Exception as e:
            raise ComfyUIError(f"Error extracting workflow from image: {e}")

# Global instance
_comfy_tools: Optional[ComfyUITools] = None


def get_comfy_tools() -> ComfyUITools:
    """Get or create the global ComfyUITools instance."""
    global _comfy_tools
    if _comfy_tools is None:
        _comfy_tools = ComfyUITools()
    return _comfy_tools
