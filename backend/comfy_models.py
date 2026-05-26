"""Pydantic models for ComfyUI extended tools."""

from typing import List, Dict, Literal, Optional, Any, Union
from pydantic import BaseModel, Field
from enum import Enum


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
    CLIP = "clip"
    CLIP_VISION = "clip_vision"
    CONFIGS = "configs"
    DIFFUSION_MODELS = "diffusion_models"
    HYPERNETWORKS = "hypernetworks"
    GLIGEN = "gligen"
    UNET = "unet"
    OUTPUT = "output"
    INPUT = "input"
    TEMP = "temp"
    WORKFLOWS = "workflows"


class ComfyFileInfo(BaseModel):
    """Information about a ComfyUI file or directory."""
    name: str = Field(..., description="File or directory name")
    path: str = Field(..., description="Relative path from ComfyUI root")
    is_directory: bool = Field(..., description="True if this is a directory")
    size: Optional[int] = Field(None, description="File size in bytes")
    modified_time: Optional[float] = Field(None, description="Last modified timestamp")
    extension: Optional[str] = Field(None, description="File extension")


class ComfySearchResult(BaseModel):
    """Result from pattern search in ComfyUI files."""
    file_path: str = Field(..., description="Relative path to file containing match")
    line_number: int = Field(..., description="Line number of match")
    line_content: str = Field(..., description="Content of matching line")
    context_before: List[str] = Field(default_factory=list, description="Lines before match")
    context_after: List[str] = Field(default_factory=list, description="Lines after match")


# REQUEST MODELS
class ComfyListFoldersRequest(BaseModel):
    """Request to list ComfyUI directory contents with filtering and sorting."""
    folder_type: ComfyFolderType = Field(
        ..., 
        description="Type of ComfyUI directory to list"
    )
    pattern: Optional[str] = Field(
        None,
        description="Regex pattern to filter by full relative path (case-insensitive). "
                    "Examples: '.*\\.safetensors$' for safetensors files, 'sdxl' for SDXL models"
    )
    sort_by: Optional[Literal["name", "size", "modified_time", "type"]] = Field(
        None,
        description="Field to sort by: 'name' (filename), 'size' (file size), "
                    "'modified_time' (last modified), 'type' (extension/directory). "
                    "If None, sorts directories first then alphabetically by name."
    )
    order: Literal["asc", "desc"] = Field(
        "asc",
        description="Sort order: 'asc' (ascending) or 'desc' (descending)"
    )
    limit: int = Field(
        50,
        ge=1,
        le=1000,
        description="Maximum number of items to return (default: 50, max: 1000)"
    )


class ComfyReadFileRequest(BaseModel):
    """Request to read a file within ComfyUI directory."""
    path: str = Field(
        ..., 
        description="Relative path to file from ComfyUI root"
    )
    max_size: int = Field(
        24000,
        ge=1,
        le=24000,
        description="Maximum characters to return"
    )
    start_line: int = Field(
        1,
        ge=1,
        description="First 1-based line number to return"
    )
    line_count: int = Field(
        240,
        ge=1,
        le=800,
        description="Maximum number of lines to return"
    )


class ComfySearchFilesRequest(BaseModel):
    """Request to search for patterns in ComfyUI files."""
    pattern: str = Field(
        ..., 
        description="Regular expression pattern to search for"
    )
    folder_type: ComfyFolderType = Field(
        ComfyFolderType.CUSTOM_NODES,
        description="Directory to search in"
    )
    file_pattern: Optional[str] = Field(
        None,
        description="Optional glob pattern to filter files (e.g., '*.py')"
    )
    max_results: int = Field(
        20,
        ge=1,
        le=80,
        description="Maximum number of search results to return"
    )
    context_lines: int = Field(
        2,
        ge=0,
        le=5,
        description="Number of context lines around each match"
    )


# RESPONSE MODELS
class ComfyListFoldersResponse(BaseModel):
    """Response from listing ComfyUI directory contents."""
    folder_type: str = Field(..., description="Type of folder that was listed")
    folder_path: str = Field(..., description="Directory path that was listed")
    items: List[ComfyFileInfo] = Field(..., description="List of files and directories")
    total_items: int = Field(..., description="Total number of items found")
    comfyui_root: str = Field(..., description="ComfyUI installation root path")


class ComfyReadFileResponse(BaseModel):
    """Response from reading a ComfyUI file."""
    path: str = Field(..., description="Path to the file that was read")
    content: str = Field(..., description="File content as text")
    size: int = Field(..., description="File size in bytes")
    encoding: str = Field(..., description="Text encoding used")
    extension: str = Field(..., description="File extension")
    comfyui_root: str = Field(..., description="ComfyUI installation root path")


class ComfySearchFilesResponse(BaseModel):
    """Response from searching ComfyUI files."""
    pattern: str = Field(..., description="Search pattern that was used")
    folder_type: str = Field(..., description="Directory that was searched")
    results: List[ComfySearchResult] = Field(..., description="List of search matches")
    total_matches: int = Field(..., description="Total number of matches found")
    files_searched: int = Field(..., description="Number of files searched")
    truncated: bool = Field(..., description="True if results were truncated")
    comfyui_root: str = Field(..., description="ComfyUI installation root path")
