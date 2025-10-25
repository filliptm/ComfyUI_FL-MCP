"""ComfyUI filesystem utilities for FL_JS agent system.

Provides secure, deterministic access to ComfyUI directory structure
for agent-based analysis and discovery.
"""

import os
import re
import logging
import httpx
from pathlib import Path
from typing import List, Dict, Optional, Union, Any
from dataclasses import dataclass
from enum import Enum

from comfy_models import ComfyFolderType, ComfyFileInfo, ComfySearchResult

logger = logging.getLogger(__name__)


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
    
    def __init__(self, comfyui_root: Optional[str] = None, comfy_url: str = "http://127.0.0.1:8188"):
        """Initialize ComfyUI tools with auto-detection.
        
        Args:
            comfyui_root: Path to ComfyUI installation (auto-detected if None)
            comfy_url: URL of ComfyUI server (default: http://127.0.0.1:8188)
        """
        self.comfyui_root = Path(comfyui_root) if comfyui_root else self._find_comfyui_root()
        self.comfy_url = comfy_url
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
            ComfyFolderType.WORKFLOWS: "user/default/workflows",
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
    
    async def fetch_history(self, prompt_id: Optional[str] = None, max_items: int = 10) -> Dict[str, Any]:
        """Fetch execution history from ComfyUI.
        
        Args:
            prompt_id: Optional specific prompt ID to fetch. If None, fetches recent history.
            max_items: Maximum number of history items to fetch (default: 10)
            
        Returns:
            If prompt_id is provided: Single history entry dict or None if not found
            If prompt_id is None: Dict mapping prompt_id -> history entry
            
        Raises:
            ComfyUIError: If history fetch fails
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.comfy_url}/history",
                    params={"max_items": max_items},
                    timeout=10.0
                )
                response.raise_for_status()
                
                history = response.json()
                
                # Strip out "prompt" key to reduce token usage
                # We only care about status, outputs, and errors
                for prompt_id_key, entry in history.items():
                    if "prompt" in entry:
                        del entry["prompt"]
                
                if prompt_id:
                    return history.get(prompt_id)
                else:
                    return history
                    
        except httpx.TimeoutException:
            raise ComfyUIError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.comfy_url}?"
            )
        except httpx.RequestError as e:
            raise ComfyUIError(
                f"Failed to connect to ComfyUI at {self.comfy_url}: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to fetch history: {e}")
            raise ComfyUIError(f"Failed to fetch history: {e}")    

    async def delete_queue_items(
        self,
        clear_all: bool = False,
        prompt_ids: Optional[List[str]] = None,
        interrupt_running: bool = False
    ) -> Dict[str, Any]:
        """Delete items from the ComfyUI queue.
        
        Args:
            clear_all: If True, clear all pending items from queue
            prompt_ids: List of specific prompt IDs to delete
            interrupt_running: If True, also interrupt currently running workflow
            
        Returns:
            Dict with operation results:
            {
                "success": bool,
                "cleared_all": bool,
                "deleted_ids": List[str],
                "interrupted": bool,
                "message": str
            }
            
        Raises:
            ComfyUIError: If operation fails or parameters are invalid
        """
        # Validation: must provide at least one operation
        if not clear_all and not prompt_ids and not interrupt_running:
            raise ComfyUIError(
                "Must specify at least one operation: clear_all, prompt_ids, or interrupt_running"
            )
        
        # Validation: cannot specify both clear_all and prompt_ids
        if clear_all and prompt_ids:
            raise ComfyUIError(
                "Cannot specify both clear_all=True and prompt_ids. Choose one."
            )
        
        results = {
            "success": True,
            "cleared_all": False,
            "deleted_ids": [],
            "interrupted": False,
            "message": ""
        }
        
        messages = []
        
        try:
            async with httpx.AsyncClient() as client:
                # Operation 1: Clear all pending items
                if clear_all:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/queue",
                            json={"clear": True},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["cleared_all"] = True
                        messages.append("Cleared all pending queue items")
                        logger.info("Queue cleared successfully")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to clear queue: {e}")
                        results["success"] = False
                        messages.append(f"Failed to clear queue: {e.response.status_code}")
                
                # Operation 2: Delete specific prompt IDs
                if prompt_ids:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/queue",
                            json={"delete": prompt_ids},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["deleted_ids"] = prompt_ids
                        messages.append(f"Deleted {len(prompt_ids)} queue item(s): {', '.join(prompt_ids)}")
                        logger.info(f"Deleted queue items: {prompt_ids}")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to delete queue items: {e}")
                        results["success"] = False
                        messages.append(f"Failed to delete items: {e.response.status_code}")
                
                # Operation 3: Interrupt running workflow
                if interrupt_running:
                    try:
                        response = await client.post(
                            f"{self.comfy_url}/interrupt",
                            json={},
                            timeout=10.0
                        )
                        response.raise_for_status()
                        results["interrupted"] = True
                        messages.append("Interrupted currently running workflow")
                        logger.info("Workflow interrupted successfully")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to interrupt workflow: {e}")
                        # Don't fail the entire operation if interrupt fails
                        # (might not have anything running)
                        messages.append(f"Interrupt failed (nothing running?): {e.response.status_code}")
                
                results["message"] = "; ".join(messages)
                return results
                
        except httpx.TimeoutException:
            raise ComfyUIError(
                f"ComfyUI server timeout. Is ComfyUI running at {self.comfy_url}?"
            )
        except httpx.RequestError as e:
            raise ComfyUIError(
                f"Failed to connect to ComfyUI at {self.comfy_url}: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to delete queue items: {e}")
            raise ComfyUIError(f"Failed to delete queue items: {e}")


    def list_folders(
        self,
        folder_type: Union[str, ComfyFolderType],
        pattern: Optional[str] = None,
        sort_by: Optional[str] = None,
        order: str = "asc",
        limit: int = 50
    ) -> List[ComfyFileInfo]:
        """List contents of a ComfyUI directory by type with filtering and sorting.
        
        Args:
            folder_type: Type of folder to list (e.g., 'checkpoints', 'loras')
            pattern: Optional regex pattern to filter paths (case-insensitive)
            sort_by: Optional sort field ('name', 'size', 'modified_time', 'type')
            order: Sort order ('asc' or 'desc')
            limit: Maximum number of items to return
        
        Returns:
            List of ComfyFileInfo objects, filtered, sorted, and limited
        
        Raises:
            ComfyUINotFoundError: If ComfyUI installation not found
            ComfyUISecurityError: If path traversal detected
        """
        # Convert string to enum if needed
        if isinstance(folder_type, str):
            try:
                folder_type = ComfyFolderType(folder_type)
            except ValueError:
                raise ComfyUIError(f"Invalid folder type: {folder_type}")
        
        # Get folder path
        if folder_type not in self.folder_mappings:
            raise ComfyUIError(f"Unknown folder type: {folder_type}")
        
        folder_path = self.comfyui_root / self.folder_mappings[folder_type]
        
        # Security check
        try:
            folder_path = folder_path.resolve()
            if not str(folder_path).startswith(str(self.comfyui_root)):
                raise ComfyUISecurityError(
                    f"Path traversal detected: {folder_path} outside {self.comfyui_root}"
                )
        except (OSError, RuntimeError) as e:
            raise ComfyUIError(f"Invalid path: {e}")
        
        if not folder_path.exists():
            logger.warning(f"Folder does not exist: {folder_path}")
            return []
        
        # Build items list
        items = []
        for entry in folder_path.iterdir():
            try:
                stat = entry.stat()
                relative_path = str(entry.relative_to(self.comfyui_root))
                
                items.append(ComfyFileInfo(
                    name=entry.name,
                    path=relative_path,
                    is_directory=entry.is_dir(),
                    size=stat.st_size if entry.is_file() else None,
                    modified_time=stat.st_mtime,
                    extension=entry.suffix[1:] if entry.suffix else None
                ))
            except (OSError, PermissionError) as e:
                logger.warning(f"Cannot access {entry}: {e}")
                continue
        
        # Apply regex filter if pattern provided
        if pattern:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
                original_count = len(items)
                items = [item for item in items if regex.search(item.path)]
                logger.debug(
                    f"Filtered from {original_count} to {len(items)} items "
                    f"matching pattern: {pattern}"
                )
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")
                # Continue without filtering on invalid pattern
        
        # Apply sorting
        if sort_by is None:
            # Default: directories first, then alphabetical by name
            items.sort(key=lambda x: (not x.is_directory, x.name.lower()))
            logger.debug("Applied default sort: directories first, then by name")
        else:
            # Sort by specified field
            reverse = (order == "desc")
            
            if sort_by == "name":
                items.sort(key=lambda x: x.name.lower(), reverse=reverse)
            elif sort_by == "size":
                items.sort(key=lambda x: x.size or 0, reverse=reverse)
            elif sort_by == "modified_time":
                items.sort(key=lambda x: x.modified_time or 0, reverse=reverse)
            elif sort_by == "type":
                # Sort by: directories vs files, then by extension
                if order == "asc":
                    items.sort(key=lambda x: (not x.is_directory, x.extension or ""))
                else:
                    items.sort(key=lambda x: (x.is_directory, x.extension or ""), reverse=True)
            
            logger.debug(f"Sorted by {sort_by} ({order})")
        
        # Apply limit
        original_count = len(items)
        if limit and len(items) > limit:
            items = items[:limit]
            logger.debug(f"Limited results from {original_count} to {limit} items")
        
        return items
    
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

    def extract_workflow_from_image(self, image_path: str) -> dict:
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
