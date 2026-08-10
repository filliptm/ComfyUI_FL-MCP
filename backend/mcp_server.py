"""MCP Server implementation using FastMCP.

This module defines MCP tools for controlling and inspecting ComfyUI.
"""

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Union, Literal
from urllib.parse import quote

# Ensure this file's own directory is importable for flat sibling imports below.
# The embedded ComfyUI Python uses a ._pth file and does NOT auto-prepend the
# script directory to sys.path, so an MCP client launching this with the
# embedded interpreter would otherwise fail with ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_pywin32_importable() -> None:
    """Make pywin32 (pywintypes/pythoncom) importable under embedded Python.

    fastmcp's server support imports ``pywintypes`` (via keyring). On a normal
    install ``pywin32.pth`` puts win32/win32/lib/pywin32_system32 on sys.path,
    but the embedded ComfyUI Python disables ``site`` (its ``._pth`` has
    ``# import site``), so those .pth files never run and the import fails with
    ``ModuleNotFoundError: No module named 'pywintypes'``. Note the embedded
    interpreter also ignores ``PYTHONPATH`` when a ``._pth`` is present, so this
    has to be done in-process rather than via the environment.
    """
    if os.name != "nt":
        return
    try:
        import pywintypes  # noqa: F401
        return
    except ImportError:
        pass
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    for sub in ("win32", "win32/lib", "pywin32_system32", "Pythonwin"):
        candidate = site_packages.joinpath(*sub.split("/"))
        if candidate.is_dir():
            p = str(candidate)
            if p not in sys.path:
                sys.path.insert(0, p)


_ensure_pywin32_importable()

import websockets
import httpx
from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image as MCPImage
from PIL import Image as PILImage, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator

from config import DATA_DIR, MAX_GENERATION_COMPLETION_TIMEOUT_SECONDS, settings
from models import WorkflowQuery
from comfy_models import (
    ComfyListFoldersRequest, ComfyListFoldersResponse,
    ComfyReadFileRequest, ComfyReadFileResponse,
    ComfySearchFilesRequest, ComfySearchFilesResponse, ComfyFolderType
)
from comfy_tools import get_comfy_tools, ComfyUIError, ComfyUINotFoundError
from chat_images import ChatImageReference
from node_library import (
    get_node_library_client,
    normalize_node_schema_contract,
    NodeLibraryError,
    NodeLibraryConnectionError,
    NodeTypeNotFoundError
)
from node_catalog_store import NodeCatalogStore
from workflow_planner import (
    ApplyWorkflowPlanRequest,
    PlanWorkflowRequest,
    compile_workflow_plan,
)
from workflow_resolver import (
    ResolveWorkflowSpecRequest,
    resolve_workflow_spec as resolve_workflow_capabilities,
)
from workflow_compiler import (
    CompileWorkflowSpecRequest,
    compile_workflow_spec as compile_semantic_workflow,
)
from workflow_refinement import (
    ApplyWorkflowRefinementRequest,
    CanonicalAppendReplacement,
    GRAPH_PRECONDITION_HASH_SCHEMA,
    PlanWorkflowRefinementRequest,
    WORKFLOW_IDENTITY_SCHEMA,
    WorkflowRefinementExistingOutput,
    WorkflowRefinementNode,
    WorkflowRefinementSideInputMapping,
    compile_workflow_refinement,
    normalize_workflow_graph,
)
from workflow_graph_patch import (
    ApplyScopedGraphPatchRequest,
    GRAPH_PATCH_SCHEMA,
    MAX_GRAPH_PATCH_ATTACHMENT_BYTES,
    SCOPED_GRAPH_PATCH_SCHEMA,
    WorkflowGraphPatchApplyRequest,
    compile_graph_patch,
    compile_scoped_graph_patch,
    graph_patch_request_from_apply,
    scoped_graph_patch_request_from_apply,
    verify_completed_graph_patch_state,
)
from workflow_capability_graph import VerifiedCapabilityLesson
from workflow_branch_operations import (
    BranchOperationRequest,
    ResolveBranchSuccessorsRequest,
    WORKFLOW_BRANCH_SUCCESSOR_SCHEMA,
    compile_workflow_branch_operation as compile_branch_operation,
    resolve_workflow_branch_successors as resolve_branch_successors,
)
from workflow_branch_queries import (
    CompareWorkflowBranchesRequest,
    compare_workflow_branches,
)
from workflow_branch_tools import (
    DiscoverWorkflowBranchesRequest,
    NavigateWorkflowBranchRequest,
    discover_workflow_branch_selection,
)
from workflow_refinement_compiler import (
    CompileWorkflowRefinementSpecRequest,
    compile_workflow_refinement_spec as compile_semantic_refinement,
)
from comfy_registry import ComfyRegistryClient, normalize_github_repository_url

from comfy_manager import (
    get_comfy_manager_client,
    ManagerError,
    ManagerNotInstalledError,
    ManagerConnectionError,
    ManagerAPIError
)
from sysinfo import get_system_info as _get_system_info

from manager import manager # This is the Connection Manager, not comfy manager :D
from calc import acalc_batch, CalcBatchParams
from coding_tools import (
    CodingToolError,
    apply_unified_patch,
    create_pack as coding_create_pack,
    git_commit as coding_git_commit,
    git_diff as coding_git_diff,
    git_push as coding_git_push,
    git_status as coding_git_status,
    list_packs as coding_list_packs,
    read_file as coding_read_file,
    search as coding_search,
    validate_pack as coding_validate_pack,
    write_file as coding_write_file,
)
from comfy_supervisor import comfy_supervisor
from web_cache import WebCache
from web_fetcher import AsyncWebFetcher
from web_search import WebSearchService
from web_service import WebPageService

# LOGGING

log_level_name = settings.log_level
log_level = getattr(logging, log_level_name, logging.INFO)

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
log_file = _LOG_DIR / f"fl_mcp_client-{os.getpid()}.log"

# Configure logging to both console and file
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),              # Console output
        logging.FileHandler(log_file, mode="a", encoding="utf-8")  # File output
    ],
)

logger = logging.getLogger("fl_mcp_server")
logger.info(f"Logger initialized with level: {log_level_name}")


# ============================================================================
# WebSocket Client for MCP Subprocess (persist across tool calls)
# ============================================================================

class FrontendToolExecutionError(RuntimeError):
    """Structured failure returned by the ComfyUI browser tool executor."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_execution_failed",
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class MCPWebSocketClient:
    """WebSocket client for MCP subprocess to communicate with backend."""
    
    def __init__(self, session_id: str, ws_url: str, client_id: Optional[str] = None):
        self.session_id = session_id
        self.ws_url = ws_url
        self.client_id = client_id or os.getenv("FL_MCP_CLIENT_ID") or f"mcp-{os.getpid()}"
        workflow_id = os.getenv("FL_MCP_WORKFLOW_ID", "").strip()
        self.workflow = None
        if workflow_id:
            self.workflow = {
                "id": workflow_id,
                "name": os.getenv("FL_MCP_WORKFLOW_NAME", "").strip() or "Workflow",
                "path": os.getenv("FL_MCP_WORKFLOW_PATH", "").strip() or None,
            }
        self.ws = None
        self.pending_requests = {}  # request_id -> (Future, connection generation)
        self.connected = False
        self._receive_task = None
        self._connect_lock = asyncio.Lock()
        self._generation = 0
        
    async def connect(self):
        """Connect to the backend once, serializing concurrent reconnects."""
        async with self._connect_lock:
            if self.connected and self.ws is not None:
                return

            previous = self.ws
            self.ws = None
            self.connected = False
            if previous is not None:
                await previous.close()

            logger.info(f"[MCP-WS] Connecting to {self.ws_url} with session {self.session_id}")
            websocket = await websockets.connect(self.ws_url)
            try:
                await websocket.send(json.dumps({
                    'type': 'handshake',
                    'session_id': self.session_id,
                    'client_version': '1.0.0-mcp',
                    'connection_type': 'mcp',
                    'client_id': self.client_id,
                }))
                response = await websocket.recv()
                data = json.loads(response)
                if data.get('type') != 'handshake_ack':
                    raise RuntimeError(f"Unexpected handshake response: {data}")
            except Exception:
                await websocket.close()
                raise

            self._generation += 1
            generation = self._generation
            self.ws = websocket
            self.connected = True
            logger.info("[MCP-WS] Connected and handshake complete")
            self._receive_task = asyncio.create_task(
                self._receive_loop(websocket, generation)
            )
    
    async def _receive_loop(self, websocket, generation: int):
        """Receive and process messages from backend."""
        try:
            async for message in websocket:
                data = json.loads(message)
                await self._handle_message(data, generation)
        except websockets.exceptions.ConnectionClosed as exc:
            logger.warning("[MCP-WS] Connection closed: %s", exc)
        except Exception as e:
            logger.error(f"[MCP-WS] Receive loop error: {e}")
        finally:
            if self.ws is websocket:
                self.connected = False
            await self._fail_pending_for_generation(
                generation,
                RuntimeError("WebSocket closed"),
            )
    
    async def _handle_message(self, data: dict, generation: int):
        """Handle incoming message from backend."""
        msg_type = data.get('type')
        
        if msg_type == 'tool_result':
            request_id = data.get('request_id')
            pending = self.pending_requests.get(request_id)
            if pending and pending[1] == generation:
                future = pending[0]
            else:
                future = None
            if future is not None and not future.done():
                if data.get('success'):
                    future.set_result(data.get('data'))
                else:
                    future.set_exception(FrontendToolExecutionError(
                        data.get('error', 'Tool execution failed'),
                        code=data.get('error_code') or 'tool_execution_failed',
                        details=data.get('error_details'),
                    ))
                self.pending_requests.pop(request_id, None)
        else:
            logger.warning(f"[MCP-WS] Unexpected message type: {msg_type}")

    async def _fail_pending_for_generation(self, generation: int, exc: Exception):
        for rid, (fut, pending_generation) in list(self.pending_requests.items()):
            if pending_generation != generation:
                continue
            if not fut.done():
                fut.set_exception(exc)
            self.pending_requests.pop(rid, None)

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any], timeout_ms: int = 30000) -> dict:
        """Execute a tool via WebSocket callback."""
        if not self.connected or self.ws is None:
            logger.warning("[MCP-WS] Not connected; reconnecting before %s", tool_name)
            await self.connect()

        websocket = self.ws
        generation = self._generation
        if websocket is None:
            raise RuntimeError("WebSocket reconnect did not produce a connection")
        
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[request_id] = (future, generation)
        
        logger.info(f"[MCP-WS] Executing tool: {tool_name} (request_id: {request_id})")
        
        try:
            message = {
                'type': 'tool_request',
                'session_id': self.session_id,
                'request_id': request_id,
                'tool_name': tool_name,
                'parameters': parameters,
                'timeout_ms': timeout_ms,
            }
            if self.workflow:
                message['workflow'] = self.workflow
            await websocket.send(json.dumps(message))
            
            timeout_seconds = timeout_ms / 1000.0
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            logger.info(f"[MCP-WS] Tool execution complete: {request_id}")
            return result

        except websockets.exceptions.ConnectionClosed as exc:
            if self.ws is websocket:
                self.connected = False
            self.pending_requests.pop(request_id, None)
            raise RuntimeError(
                "WebSocket disconnected while dispatching the tool; "
                "the result is unknown and the operation was not retried"
            ) from exc
        except Exception as e:
            logger.error(f"[MCP-WS] Tool execution error: {e}")
            self.pending_requests.pop(request_id, None)
            raise
    
    async def disconnect(self):
        """Optional explicit shutdown (not used by lifespan)."""
        websocket = self.ws
        self.ws = None
        self.connected = False
        if websocket is not None:
            await websocket.close()
        if self._receive_task:
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        for generation in {
            pending_generation
            for _, pending_generation in self.pending_requests.values()
        }:
            await self._fail_pending_for_generation(
                generation,
                RuntimeError("WebSocket disconnected"),
            )
        logger.info("[MCP-WS] Disconnected")


# ============================================================================
# FastMCP lifespan: reuse a single persistent client (no teardown)
# ============================================================================

_WS_CLIENT = None  # module-level singleton

BROWSER_BRIDGE_TOOLS = {
    "query_workflow",
    "workflow_overview",
    "workflow_diagram",
    "frontend_list_commands",
    "frontend_execute_command",
    "frontend_list_keybindings",
    "workflow_get_current_json",
    "workflow_load_json",
    "workflow_get_tabs",
    "workflow_list_files",
    "workflow_read_file",
    "workflow_save_current",
    "workflow_rename_file",
    "workflow_delete_file",
    "workflow_close_current",
    "workflow_duplicate_current",
    "find_node",
    "create_nodes",
    "remove_nodes",
    "bypass_nodes",
    "unbypass_nodes",
    "pin_nodes",
    "unpin_nodes",
    "select_nodes",
    "focus_on_nodes",
    "take_screenshot",
    "get_current_node_selection",
    "get_node_values",
    "view_node_mask",
    "edit_node_mask",
    "confirm_mask_review",
    "set_node_values",
    "connect_nodes",
    "get_node_slots",
    "connect_nodes_batch",
    "auto_connect_workflow",
    "get_layout",
    "modify_layout",
    "queue_workflow",
    "cancel_workflow",
    "enable_auto_queue",
    "disable_auto_queue",
    "set_batch_count",
    "get_queue_status",
    "generate_seed",
    "generate_float",
    "generate_int",
    "random_choice",
    "plan_workflow_refinement",
    "apply_workflow_refinement",
    "workflow_branches_discover",
    "workflow_branch_compare",
    "workflow_branch_navigate",
    "compile_workflow_branch_operation",
    "resolve_workflow_branch_successor",
    "compile_workflow_refinement_spec",
    "apply_workflow_graph_patch",
    "apply_workflow_plan",
    "place_chat_image_in_node",
}

NODE_CATALOG_TOOLS = {
    "node_library_status",
    "node_knowledge_search",
    "node_library_search",
    "node_library_get_details",
    "node_library_find_compatible",
    "plan_workflow_refinement",
    "apply_workflow_refinement",
    "workflow_branch_compare",
    "compile_workflow_branch_operation",
    "resolve_workflow_branch_successor",
    "compile_workflow_refinement_spec",
    "apply_workflow_graph_patch",
    "plan_workflow",
    "compile_workflow_spec",
    "resolve_workflow_spec",
    "apply_workflow_plan",
}


def _embedded_allowed_tools() -> set[str] | None:
    if os.getenv("FL_MCP_MODE") != "subprocess":
        return None
    raw = os.getenv("FL_MCP_ALLOWED_TOOLS")
    if raw is None:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}

@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[Any]:
    """Manage MCP server lifespan and persistent WebSocket connection."""
    global _WS_CLIENT

    allowed_tools = _embedded_allowed_tools()
    all_tools = allowed_tools is None
    needs_manager = all_tools or any(
        name.startswith(("manager_", "registry_"))
        or name == "mcp_capability_audit"
        for name in allowed_tools
    )
    needs_node_catalog = all_tools or bool(allowed_tools & NODE_CATALOG_TOOLS)
    needs_web = all_tools or bool(allowed_tools & {"web_search", "web_fetch_page"})
    needs_registry = all_tools or any(name.startswith("registry_") for name in allowed_tools)
    needs_browser_bridge = all_tools or bool(allowed_tools & BROWSER_BRIDGE_TOOLS)

    manager_client = None
    manager_available = False
    
    logger.info(f"FL_MCP_MODE: {os.getenv('FL_MCP_MODE')}")
    
    if needs_manager:
        try:
            manager_client = get_comfy_manager_client(
                server_url=settings.comfyui_server_url,
                timeout=settings.comfyui_api_timeout
            )
            version_info = await manager_client.check_installed()

            if version_info.installed:
                logger.info(f"[MCP] ComfyUI Manager detected (v{version_info.version})")
                manager_available = True
            else:
                logger.warning("[MCP] ComfyUI Manager not installed - manager tools will return errors")
        except Exception as e:
            logger.warning(f"[MCP] Could not check Manager status: {e}")

    node_catalog_store: NodeCatalogStore | None = None
    node_library_client = None
    if needs_node_catalog:
        node_library_client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        try:
            node_catalog_store = NodeCatalogStore(DATA_DIR / "node_catalog.sqlite3")
            node_library_client.bind_persistence(node_catalog_store)
        except Exception as exc:
            logger.warning("[MCP] Persistent node knowledge is unavailable: %s", exc)
            if node_catalog_store is not None:
                node_catalog_store.close()
            node_catalog_store = None

    web_cache = WebCache(DATA_DIR / "web_cache.sqlite3") if needs_web else None
    web_fetcher = AsyncWebFetcher() if needs_web else None
    web_pages = (
        WebPageService(fetcher=web_fetcher, cache=web_cache)
        if web_fetcher is not None and web_cache is not None
        else None
    )
    web_search = (
        WebSearchService(
            mode=os.getenv("FL_MCP_WEB_SEARCH_MODE", "free"),
            tavily_api_key=(
                os.getenv("FL_MCP_TAVILY_API_KEY")
                or os.getenv("TAVILY_API_KEY")
            ),
        )
        if needs_web
        else None
    )
    web_images_allowed = (
        os.getenv("FL_MCP_MODE") != "subprocess"
        or os.getenv("FL_MCP_WEB_IMAGES_ALLOWED", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    registry_client = ComfyRegistryClient() if needs_registry else None

    async def close_web_resources() -> None:
        if web_search is not None:
            await web_search.aclose()
        if web_fetcher is not None:
            await web_fetcher.aclose()
        if web_cache is not None:
            web_cache.close()

    def close_node_knowledge() -> None:
        if node_catalog_store is None:
            return
        if node_library_client is not None:
            node_library_client.unbind_persistence(node_catalog_store)
        node_catalog_store.close()

    if os.getenv('FL_MCP_MODE') == 'subprocess':
        session_id = os.getenv('FL_MCP_SESSION_ID')
        ws_url = os.getenv('FL_MCP_WS_URL')
        if not session_id or not ws_url:
            logger.error("Missing FL_MCP_SESSION_ID or FL_MCP_WS_URL environment variables")
            await close_web_resources()
            close_node_knowledge()
            raise RuntimeError("MCP subprocess not properly configured")
        
        logger.info(f"[MCP] Starting in subprocess mode for session: {session_id}")

        if needs_browser_bridge:
            try:
                if _WS_CLIENT is None:
                    _WS_CLIENT = MCPWebSocketClient(session_id, ws_url)
                await _WS_CLIENT.connect()
                logger.info("[MCP] WebSocket client connected (persistent)")
            except Exception as e:
                logger.error(f"MCP Initialization Failed: {str(e)}")
                await close_web_resources()
                close_node_knowledge()
                raise

        try:
            yield {
                "client": _WS_CLIENT if needs_browser_bridge else None,
                "manager_client": manager_client,
                "manager_available": manager_available,
                "web_search": web_search,
                "web_pages": web_pages,
                "web_images_allowed": web_images_allowed,
                "registry_client": registry_client,
                "node_catalog_store": node_catalog_store,
            }
        finally:
            await close_web_resources()
            close_node_knowledge()

        # NOTE: no disconnect/teardown here; keep WS open for the process lifetime.
        return

    # Standalone (no WebSocket bridge)
    logger.info("[MCP] Running in standalone mode (no WebSocket)")
    try:
        yield {
            "client": None,
            "manager_client": manager_client,
            "manager_available": manager_available,
            "web_search": web_search,
            "web_pages": web_pages,
            "web_images_allowed": web_images_allowed,
            "registry_client": registry_client,
            "node_catalog_store": node_catalog_store,
        }
    finally:
        await close_web_resources()
        close_node_knowledge()

# Initialize FastMCP server with lifespan
mcp = FastMCP("ComfyUI FL-MCP", lifespan=mcp_lifespan)


async def _execute_tool(ctx: Context, tool_name: str, parameters: Dict[str, Any], timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """Execute a tool via WebSocket callback.
    
    Args:
        ctx: FastMCP Context
        tool_name: Name of the tool to execute
        parameters: Tool parameters
        timeout_ms: Optional timeout in milliseconds
        
    Returns:
        Tool execution result
        
    Raises:
        RuntimeError: If WebSocket client not initialized
    """
    _ws_client = ctx.request_context.lifespan_context['client']
    if _ws_client is None:
        return {
            "success": False,
            "error": (
                "requires_browser_bridge: this tool needs the ComfyUI browser bridge. "
                "Run the MCP server with FL_MCP_MODE=subprocess, FL_MCP_SESSION_ID, "
                "and FL_MCP_WS_URL, and keep ComfyUI open in a browser."
            ),
            "requires_browser_bridge": True,
        }
    
    return await _ws_client.execute_tool(
        tool_name=tool_name,
        parameters=parameters,
        timeout_ms=timeout_ms or 30000
    )

import time
async def _report_tool_activity(ctx: Context, tool_name: str) -> None:
    """Report tool activity to frontend for Python-only tools."""
    _ws_client = ctx.request_context.lifespan_context.get('client')
    if _ws_client and _ws_client.connected:
        try:
            await _ws_client.ws.send(json.dumps({
                'type': 'tool_report',
                'session_id': _ws_client.session_id,
                'tool_name': tool_name,
                'timestamp': time.time()
            }))
        except Exception as e:
            logger.debug(f"Could not report tool activity: {e}")


def _node_knowledge_store(ctx: Context) -> NodeCatalogStore | None:
    """Return the optional per-process catalog store without opening ad-hoc DB handles."""

    request_context = getattr(ctx, "request_context", None)
    lifespan_context = getattr(request_context, "lifespan_context", None)
    if not isinstance(lifespan_context, dict):
        return None
    store = lifespan_context.get("node_catalog_store")
    return store if isinstance(store, NodeCatalogStore) else None


def _active_verified_capability_lessons(
    ctx: Context,
) -> tuple[VerifiedCapabilityLesson, ...]:
    """Load current-schema lessons as optional compiler ranking priors.

    Lesson retrieval is best-effort and never weakens live catalog validation.
    Invalid or stale rows are ignored by the schema-scoped capability graph.
    """

    store = _node_knowledge_store(ctx)
    if store is None:
        return ()
    try:
        rows = store.get_all_active_verified_lessons()
    except Exception as exc:
        logger.warning("Could not load verified capability lessons: %s", exc)
        return ()
    lessons: list[VerifiedCapabilityLesson] = []
    for row in rows:
        node_type = row.get("node_type") if isinstance(row, dict) else None
        schema_hash = row.get("schema_hash") if isinstance(row, dict) else None
        payload = row.get("payload") if isinstance(row, dict) else None
        if (
            isinstance(node_type, str)
            and node_type
            and isinstance(schema_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", schema_hash)
            and isinstance(payload, dict)
        ):
            lessons.append(
                VerifiedCapabilityLesson(
                    node_type=node_type,
                    schema_hash=schema_hash,
                    payload=payload,
                )
            )
    return tuple(lessons)


def _comfy_base_url() -> str:
    return settings.comfyui_server_url.rstrip("/")


def _disabled_by_config(flag_name: str) -> Dict[str, Any]:
    setting_name = {
        "FL_MCP_ENABLE_WORKFLOW_WRITES": "enable_workflow_writes",
        "FL_MCP_ENABLE_CUSTOM_NODE_WRITES": "enable_custom_node_writes",
        "FL_MCP_ENABLE_GIT_WRITES": "enable_git_writes",
        "FL_MCP_ENABLE_MANAGER_MUTATIONS": "enable_manager_mutations",
        "FL_MCP_ENABLE_COMFY_PROCESS_CONTROL": "enable_comfy_process_control",
    }[flag_name]
    return {
        "success": False,
        "error": (
            "disabled_by_config: enable the matching capability under "
            "Ren > Settings > Bridge & safety, then restart ComfyUI."
        ),
        "disabled_by_config": True,
        "required_setting": setting_name,
    }


async def _comfy_request(
    method: str,
    path: str,
    *,
    json_data: Optional[Any] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Small async wrapper around ComfyUI HTTP routes for backend-only tools."""
    url = f"{_comfy_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, json=json_data, params=params)

    try:
        data: Any = response.json()
    except Exception:
        data = response.text

    return {
        "success": 200 <= response.status_code < 300,
        "status": response.status_code,
        "data": data,
    }


def _history_completion_result(
    prompt_id: str,
    history_entry: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    status = history_entry.get("status") or {}
    status_str = status.get("status_str")
    messages = status.get("messages") or []
    interrupted = any(
        isinstance(message, (list, tuple))
        and len(message) >= 1
        and message[0] == "execution_interrupted"
        for message in messages
    )

    if interrupted:
        return {
            "success": False,
            "status": "cancelled",
            "completed": False,
            "terminal": True,
            "error": "Workflow execution was cancelled.",
            "prompt_id": prompt_id,
        }

    if status_str == "success":
        return {
            "success": True,
            "status": "completed",
            "completed": True,
            "terminal": True,
            "outputs": history_entry.get("outputs", {}),
            "prompt_id": prompt_id,
        }

    if status_str == "error":
        errors = []
        for message in messages:
            if (
                not isinstance(message, (list, tuple))
                or len(message) < 2
                or message[0] != "execution_error"
                or not isinstance(message[1], dict)
            ):
                continue
            data = message[1]
            errors.append({
                "node_id": data.get("node_id"),
                "node_type": data.get("node_type"),
                "exception_type": data.get("exception_type"),
                "exception_message": data.get("exception_message"),
            })
        return {
            "success": False,
            "status": "execution_error",
            "completed": False,
            "terminal": True,
            "error": "Workflow execution failed.",
            "errors": errors,
            "prompt_id": prompt_id,
        }

    return None


def _queue_contains_prompt(queue_data: Any, prompt_id: str) -> bool:
    if not isinstance(queue_data, dict):
        return False
    for key in ("queue_running", "queue_pending"):
        for item in queue_data.get(key, []):
            if isinstance(item, dict):
                item_prompt_id = item.get("prompt_id")
            elif isinstance(item, (list, tuple)) and len(item) > 1:
                item_prompt_id = item[1]
            else:
                continue
            if str(item_prompt_id) == prompt_id:
                return True
    return False


async def _read_history_completion(
    prompt_id: str,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    response = await _comfy_request(
        "GET",
        f"/history/{quote(prompt_id, safe='')}",
        timeout=timeout,
    )
    if not response["success"] or not isinstance(response["data"], dict):
        return None
    history_entry = response["data"].get(prompt_id)
    if not isinstance(history_entry, dict):
        return None
    return _history_completion_result(prompt_id, history_entry)


async def _wait_for_generation_completion(
    prompt_id: str,
    timeout_seconds: int,
    poll_interval: float = 1.0,
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    observed_in_queue = False

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {
                "success": False,
                "status": "timeout",
                "completed": False,
                "terminal": False,
                "error": (
                    f"Timed out after {timeout_seconds} seconds while waiting for "
                    "workflow completion. The workflow may still be running."
                ),
                "prompt_id": prompt_id,
            }
        request_timeout = max(
            0.1,
            min(float(settings.comfyui_api_timeout), 5.0, remaining),
        )
        try:
            outcome = await _read_history_completion(prompt_id, request_timeout)
            if outcome is not None:
                return outcome

            queue_response = await _comfy_request(
                "GET",
                "/queue",
                timeout=request_timeout,
            )
            if queue_response["success"]:
                in_queue = _queue_contains_prompt(queue_response["data"], prompt_id)
                if in_queue:
                    observed_in_queue = True
                elif observed_in_queue:
                    outcome = await _read_history_completion(
                        prompt_id,
                        request_timeout,
                    )
                    if outcome is not None:
                        return outcome
                    return {
                        "success": False,
                        "status": "cancelled",
                        "completed": False,
                        "terminal": True,
                        "error": "Workflow left the queue before execution completed.",
                        "prompt_id": prompt_id,
                    }
        except httpx.HTTPError as exc:
            logger.debug(f"Could not poll ComfyUI generation {prompt_id}: {exc}")

        remaining = deadline - loop.time()
        if remaining <= 0:
            return {
                "success": False,
                "status": "timeout",
                "completed": False,
                "terminal": False,
                "error": (
                    f"Timed out after {timeout_seconds} seconds while waiting for "
                    "workflow completion. The workflow may still be running."
                ),
                "prompt_id": prompt_id,
            }
        await asyncio.sleep(min(poll_interval, remaining))


def _resolve_comfy_file(path: str) -> str:
    tools = get_comfy_tools()
    return str(tools._validate_path(path))


async def _comfy_upload_file(
    path: str,
    endpoint: str,
    *,
    file_field: str = "image",
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    full_path = _resolve_comfy_file(path)
    filename = os.path.basename(full_path)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    url = f"{_comfy_base_url()}{endpoint}"

    form_data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in (data or {}).items()}
    async with httpx.AsyncClient(timeout=timeout) as client:
        with open(full_path, "rb") as file_obj:
            files = {file_field: (filename, file_obj, mime_type)}
            response = await client.post(url, data=form_data, files=files)

    try:
        payload: Any = response.json()
    except Exception:
        payload = response.text
    return {
        "success": 200 <= response.status_code < 300,
        "status": response.status_code,
        "data": payload,
        "source_path": path,
    }


# ============================================================================
# REQUEST MODELS
# ============================================================================

# Query & Analysis
class WorkflowOverviewRequest(BaseModel):
    """Request for workflow overview."""
    pass

class WorkflowDiagramRequest(BaseModel):
    """Request to generate workflow diagram."""
    node_ids: Optional[List[int]] = Field(None, description="Optional list of node IDs to include (null for all nodes)")

# Node Management
class FindNodeRequest(BaseModel):
    """Request to find a node."""
    node_id: Optional[int] = Field(None, description="Node ID to find")
    node_type: Optional[str] = Field(None, description="Node type/class to find (e.g., 'KSampler')")
    title: Optional[str] = Field(None, description="Node title to find")
    find_last: bool = Field(False, description="If true, search from end of array")

class CreateNodeRequest(BaseModel):
    """Request to create a new node.

    Simplified schema for better MCP JSON generation reliability.
    Position flattened to x/y fields. Parameters removed - set them separately with set_node_values.
    The frontend measures the real node rectangle and moves colliding nodes to
    the nearest free position. Omit x/y to place the node beside the graph.
    """
    node_type: str = Field(..., description="ComfyUI node class name (e.g., 'CheckpointLoaderSimple')")
    x: Optional[float] = Field(None, description="Preferred X position; omit for automatic collision-free placement")
    y: Optional[float] = Field(None, description="Preferred Y position; omit for automatic collision-free placement")
    
class CreateNodesRequest(BaseModel):
    nodes: List[CreateNodeRequest] = Field(..., description="List of nodes to create each their own parameters")

class RemoveNodesRequest(BaseModel):
    """Request to remove nodes from workflow."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to remove")

class BypassNodesRequest(BaseModel):
    """Request to bypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to bypass")

class UnbypassNodesRequest(BaseModel):
    """Request to unbypass nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unbypass")

class PinNodesRequest(BaseModel):
    """Request to pin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to pin")

class UnpinNodesRequest(BaseModel):
    """Request to unpin nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to unpin")

class SelectNodesRequest(BaseModel):
    """Request to select nodes."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs or titles to select")

class GetSelectedNodesRequest(BaseModel):
    """Request to get currently selected nodes."""
    pass

class FocusOnNodesRequest(BaseModel):
    """Request to fit canvas view to specific nodes."""
    node_ids: Optional[List[int]] = Field(
        None,
        description="Node IDs to focus on (null=selected nodes, empty=all nodes)"
    )

class TakeScreenshotRequest(BaseModel):
    """Request to take a screenshot of the canvas."""
    format: Literal["jpeg", "png"] = Field(
        "jpeg",
        description="Image format (jpeg recommended for smaller size)"
    )
    quality: float = Field(
        0.9,
        ge=0.0,
        le=1.0,
        description="JPEG quality (0.0-1.0, only applies to jpeg format)"
    )
    fit_view: bool = Field(
        True,
        description="Fit the canvas before capture using ComfyUI's native Fit View command"
    )
    node_ids: Optional[List[int]] = Field(
        default_factory=list,
        description="Node IDs to fit before capture (null=selected nodes, empty=all nodes, ignored when fit_view=false)"
    )


# Node Manipulation
class GetNodeValuesRequest(BaseModel):
    """Request to get node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")


class ViewNodeMaskRequest(BaseModel):
    """Request to inspect a node image with its mask highlighted."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    max_dimension: int = Field(
        default=2048,
        ge=256,
        le=4096,
        description="Maximum preview width or height sent to the vision model.",
    )


class MaskRegionRequest(BaseModel):
    """One rectangle or ellipse to paint into or erase from a mask."""
    x: float = Field(..., ge=0, description="Left edge in pixels or normalized coordinates")
    y: float = Field(..., ge=0, description="Top edge in pixels or normalized coordinates")
    width: float = Field(..., gt=0, description="Region width in pixels or normalized coordinates")
    height: float = Field(..., gt=0, description="Region height in pixels or normalized coordinates")
    shape: Literal["rectangle", "ellipse"] = Field("rectangle", description="Region shape")
    operation: Literal["paint", "erase"] = Field("paint", description="Add to or remove from the mask")
    feather: float = Field(0, ge=0, le=512, description="Soft edge radius in image pixels")


class EditNodeMaskRequest(BaseModel):
    """Paint or erase regions in the image mask attached to a canvas node."""
    node_id: Union[int, str] = Field(..., description="Load Image node ID or title")
    regions: List[MaskRegionRequest] = Field(..., min_length=1, max_length=100)
    coordinate_space: Literal["pixels", "normalized"] = Field(
        "pixels",
        description="Whether x/y/width/height use image pixels or values from 0 to 1",
    )
    clear_existing: bool = Field(
        False,
        description="Clear the current mask before applying regions; use when these should be the only masked areas",
    )

    @model_validator(mode="after")
    def validate_normalized_regions(self) -> "EditNodeMaskRequest":
        if self.coordinate_space == "normalized":
            for region in self.regions:
                if region.x + region.width > 1 or region.y + region.height > 1:
                    raise ValueError("Normalized mask regions must remain within 0..1")
        return self


class ConfirmMaskReviewRequest(BaseModel):
    """Confirm that the user accepts the currently visible edited mask."""
    node_id: Union[int, str] = Field(
        ...,
        description="Edited image node ID or title",
    )
    review_token: str = Field(
        ...,
        min_length=1,
        description="Token returned by edit_node_mask",
    )


class SetNodeValuesRequest(BaseModel):
    """Request to set node parameter values."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")
    values: Dict[str, Any] = Field(..., description="Parameter values to set as key-value pairs")

class ConnectNodesRequest(BaseModel):
    """Request to connect two nodes."""
    source_node_id: Union[int, str] = Field(..., description="Source node ID or title, must be a number: 1-9999")
    target_node_id: Union[int, str] = Field(..., description="Target node ID or title, must be a number: 1-9999")
    source_slot: Optional[Union[str, int]] = Field(None, description="Source output slot name or index (auto-match if not provided)")
    target_slot: Optional[Union[str, int]] = Field(None, description="Target input slot name or index (auto-match if not provided)")
    auto_match: bool = Field(True, description="Enable auto-matching by type if slot names not found")
    match_strategy: Literal["first", "type", "name"] = Field(
        "type",
        description="Auto-match strategy: 'first'=use first available, 'type'=match by data type, 'name'=match by similar names"
    )

class GetNodeSlotsRequest(BaseModel):
    """Request to get node slot information."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class ConnectionSpec(BaseModel):
    """Single connection specification for batch operations.

    Simplified schema for better MCP JSON generation - removed Union types.
    Use node IDs (integers) for reliability. Slot names as strings only.
    """
    source_node_id: int = Field(..., description="Source node ID")
    target_node_id: int = Field(..., description="Target node ID")
    source_slot_name: Optional[str] = Field(None, description="Source output slot name (optional for auto-match)")
    target_slot_name: Optional[str] = Field(None, description="Target input slot name (optional for auto-match)")

class ConnectNodesBatchRequest(BaseModel):
    """Request to connect multiple node pairs in batch."""
    connections: List[ConnectionSpec] = Field(..., description="List of connection specifications")
    auto_match: bool = Field(True, description="Enable auto-matching by type if slot names not found")
    stop_on_error: bool = Field(False, description="Stop on first error (false = continue and report all)")

class AutoConnectWorkflowRequest(BaseModel):
    """Request to auto-connect nodes in sequence."""
    node_ids: List[Union[int, str]] = Field(..., description="List of node IDs to connect in order")
    strategy: Literal["sequential", "type_match"] = Field(
        "sequential",
        description="Connection strategy: 'sequential' connects in order, 'type_match' finds all compatible pairs"
    )

# Layout Management
class GetNodeRectRequest(BaseModel):
    """Request to get node position and size."""
    node_id: Union[int, str] = Field(..., description="Node ID or title")

class GetLayoutRequest(BaseModel):
    """Request to get layout for all nodes or specific nodes."""
    node_ids: Optional[List[Union[int, str]]] = Field(
        None, 
        description="Optional list of node IDs or titles to get rects for (omit for all nodes)"
    )

class SetNodeRectRequest(BaseModel):
    """Request to set node position and/or size."""
    node_id: int = Field(..., description="Node id of the node who's layout rectangle to set.")
    x: Optional[float] = Field(None, description="X position (null to keep current)")
    y: Optional[float] = Field(None, description="Y position (null to keep current)")
    width: Optional[float] = Field(None, description="Width (null to keep current)")
    height: Optional[float] = Field(None, description="Height (null to keep current)")

class NodeRect(BaseModel):
    """Single node layout specification.

    Flattened schema for better MCP JSON generation - node_id included directly.
    """
    node_id: int = Field(..., description="Node ID to modify")
    x: Optional[float] = Field(None, description="X position (omit to keep current)")
    y: Optional[float] = Field(None, description="Y position (omit to keep current)")
    width: Optional[float] = Field(None, description="Width (omit to keep current)")
    height: Optional[float] = Field(None, description="Height (omit to keep current)")

class BatchLayoutRequest(BaseModel):
    """Modify layout of multiple nodes with optional auto-layout.
    
    Can be used in two modes:
    1. Manual layout: Provide node_rects with explicit positions
    2. Auto-layout: Provide auto_layout params, optionally with node_ids filter
    
    Auto-layout and manual layout are mutually exclusive.
    """
    # Manual layout fields
    node_rects: Optional[List[NodeRect]] = Field(
        None,
        description="List of node rectangles to update (for manual layout)"
    )
    
    # Auto-layout fields
    auto_layout: Optional[bool] = Field(
        None,
        description="Enable automatic layout calculation"
    )
    node_ids: Optional[List[Union[int, str]]] = Field(
        None,
        description="Node IDs to auto-arrange (None = all nodes). Only used with auto_layout=True"
    )
    strategy: Optional[Literal["flow_horizontal", "flow_vertical", "grid"]] = Field(
        None,
        description="Auto-layout strategy. Only used with auto_layout=True"
    )
    spacing_multiplier: Optional[float] = Field(
        None,
        description="Spacing multiplier for auto-layout (1.0 = default, 1.5 = 50% more space). Only used with auto_layout=True"
    )

    @model_validator(mode='after')
    def validate_layout_mode(self):
        """Ensure either manual or auto-layout is specified, not both."""
        has_manual = self.node_rects is not None
        has_auto = self.auto_layout is True
        
        if not has_manual and not has_auto:
            raise ValueError("Must specify either node_rects (manual) or auto_layout=True (auto)")
        
        if has_manual and has_auto:
            raise ValueError("Cannot use both node_rects and auto_layout in the same request")
        
        return self

class PositionNodeLeftRequest(BaseModel):
    """Request to position node to the left of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeRightRequest(BaseModel):
    """Request to position node to the right of another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(32, description="Margin between nodes in pixels")

class PositionNodeTopRequest(BaseModel):
    """Request to position node above another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class PositionNodeBottomRequest(BaseModel):
    """Request to position node below another."""
    target_node_id: Union[int, str] = Field(..., description="Node to position")
    anchor_node_id: Union[int, str] = Field(..., description="Reference node")
    margin: int = Field(64, description="Margin between nodes in pixels")

class MoveNodeRightRequest(BaseModel):
    """Request to move node to the right, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(32, description="Margin to maintain when avoiding collisions")

class MoveNodeBottomRequest(BaseModel):
    """Request to move node downward, avoiding collisions."""
    node_id: Union[int, str] = Field(..., description="Node to move")
    margin: int = Field(64, description="Margin to maintain when avoiding collisions")

# Workflow Control
class QueueWorkflowRequest(BaseModel):
    """Request to queue workflow for execution."""
    wait_for_completion: Optional[bool] = Field(
        None,
        description="Override the saved generation waiting behavior for this call",
    )
    completion_timeout: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_GENERATION_COMPLETION_TIMEOUT_SECONDS,
        description="Maximum seconds to wait when completion waiting is enabled",
    )

class CancelWorkflowRequest(BaseModel):
    """Request to cancel workflow execution."""
    pass

class EnableAutoQueueRequest(BaseModel):
    """Request to enable auto-queue mode."""
    pass

class DisableAutoQueueRequest(BaseModel):
    """Request to disable auto-queue mode."""
    pass

class SetBatchCountRequest(BaseModel):
    """Request to set workflow batch count."""
    count: int = Field(..., description="Batch count (number of times to execute workflow)")

class GetQueueStatusRequest(BaseModel):
    """Request to get queue status."""
    pass

class FrontendExecuteCommandRequest(BaseModel):
    """Execute a registered ComfyUI frontend command by ID."""
    command_id: str = Field(..., description="ComfyUI command ID, e.g. 'Comfy.SaveWorkflow' or 'Comfy.Canvas.FitView'")

class WorkflowCurrentJsonRequest(BaseModel):
    """Get the current workflow JSON."""
    api_format: bool = Field(False, description="Return API prompt format instead of editable workflow JSON")


class PlanCurrentWorkflowRefinementRequest(BaseModel):
    """Describe one exact splice or retained-source append in the active workflow."""

    model_config = {"extra": "forbid"}

    application_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
        description=(
            "Stable idempotency ID for this intended refinement. Reuse it only "
            "when retrying the identical change."
        ),
    )
    path_node_ids: List[StrictInt | StrictStr] = Field(
        default_factory=list,
        max_length=201,
        description=(
            "Ordered node IDs spanning two retained boundary nodes. For insertion "
            "pass [upstream, downstream]. For replacement or deletion include every "
            "internal node to remove: [upstream, old_node..., downstream]. Leave empty "
            "only for a terminal_source append."
        ),
    )
    terminal_source: Optional[WorkflowRefinementExistingOutput] = Field(
        None,
        description=(
            "Existing retained node output that begins a terminal append. Supply its "
            "node ID and output name and/or index; it feeds the first replacement "
            "node's chain_input without disconnecting existing fan-out."
        ),
    )
    side_input_mappings: List[WorkflowRefinementSideInputMapping] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "Additional retained-node outputs mapped into exact inputs on replacement "
            "aliases. Valid for terminal append and ordinary insert/replace splices."
        ),
    )
    replacement_nodes: List[WorkflowRefinementNode] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "Ordered exact locally loaded nodes for the new sequential spine. The "
            "last append node may omit chain_output when no downstream connection is "
            "requested. Leave the list empty only when deleting internal path nodes."
        ),
    )
    expected_graph_hash: Optional[str] = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Optional raw editable-graph hash from a prior inspection. If supplied, "
            "planning fails when the active canvas changed."
        ),
    )
    expected_catalog_hash: Optional[str] = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Optional local node-catalog hash. If supplied, planning fails when loaded "
            "native, partner, or custom-node schemas changed."
        ),
    )

    @model_validator(mode="after")
    def validate_path_identity(self) -> "PlanCurrentWorkflowRefinementRequest":
        typed_ids = {(type(value).__name__, value) for value in self.path_node_ids}
        if len(typed_ids) != len(self.path_node_ids):
            raise ValueError("path_node_ids must not repeat a node")
        if self.terminal_source is not None:
            if self.path_node_ids:
                raise ValueError(
                    "terminal_source append refinements must leave path_node_ids empty"
                )
            if not self.replacement_nodes:
                raise ValueError(
                    "terminal_source append refinements require replacement_nodes"
                )
        elif len(self.path_node_ids) < 2:
            raise ValueError(
                "linear refinements require at least two path_node_ids"
            )
        if self.side_input_mappings and not self.replacement_nodes:
            raise ValueError(
                "side_input_mappings require replacement_nodes and cannot accompany deletion"
            )
        return self

class WorkflowLoadJsonRequest(BaseModel):
    """Load workflow JSON into the active ComfyUI canvas."""
    workflow: Dict[str, Any] = Field(..., description="Editable ComfyUI workflow JSON object")
    name: Optional[str] = Field(None, description="Optional workflow tab/name")
    clean: bool = Field(True, description="Mark the workflow clean after load")
    restore_view: bool = Field(True, description="Restore saved canvas view if present")

class WorkflowPathRequest(BaseModel):
    """Workflow file path under userdata/workflows."""
    path: str = Field(..., description="Workflow path, with or without the workflows/ prefix and .json suffix")

class WorkflowSaveCurrentRequest(BaseModel):
    """Save the current workflow to userdata/workflows."""
    path: str = Field(..., description="Destination workflow path")
    overwrite: bool = Field(True, description="Overwrite an existing file")

class WorkflowRenameRequest(BaseModel):
    """Rename or move a workflow file under userdata/workflows."""
    path: str = Field(..., description="Source workflow path")
    dest: str = Field(..., description="Destination workflow path")
    overwrite: bool = Field(True, description="Overwrite an existing destination")

class ComfyJobsListRequest(BaseModel):
    """List ComfyUI job queue/history items."""
    status: Optional[str] = Field(None, description="Optional status filter if supported by this ComfyUI version")
    workflow_id: Optional[str] = Field(None, description="Optional workflow ID filter if supported")
    limit: Optional[int] = Field(None, ge=1, le=200, description="Maximum number of jobs if supported")
    offset: int = Field(0, ge=0, description="Offset if supported")

class ComfyJobRequest(BaseModel):
    """Get one ComfyUI job by ID."""
    job_id: str = Field(..., description="ComfyUI job/prompt ID")

class ComfyFreeMemoryRequest(BaseModel):
    """Request ComfyUI to unload models and/or free memory."""
    unload_models: bool = Field(False, description="Unload loaded models")
    free_memory: bool = Field(False, description="Free model memory")

class ComfyHistoryDeleteRequest(BaseModel):
    """Delete ComfyUI history entries."""
    clear_all: bool = Field(False, description="Clear all history")
    prompt_ids: Optional[List[str]] = Field(None, description="Specific prompt IDs to remove from history")

class ComfySettingsGetRequest(BaseModel):
    """Read ComfyUI settings."""
    id: Optional[str] = Field(None, description="Specific setting ID, or omit for all settings")

class ComfySettingsSetRequest(BaseModel):
    """Set one or more ComfyUI settings."""
    id: Optional[str] = Field(None, description="Specific setting ID for a single setting update")
    value: Optional[Any] = Field(None, description="Value for single setting update")
    settings: Optional[Dict[str, Any]] = Field(None, description="Multiple setting values keyed by setting ID")

class ManagerQueueActionRequest(BaseModel):
    """Queue a ComfyUI Manager action. Requires explicit confirmation."""
    endpoint: Literal[
        "install", "update", "uninstall", "disable", "enable", "fix",
        "install_model", "install-model", "update_comfyui", "update-comfyui",
        "update_all", "update-all"
    ] = Field(..., description="Manager queue endpoint/action")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Raw Manager payload for that action")
    confirmed: bool = Field(False, description="Must be true to perform install/update/uninstall/disable actions")
    start_queue: bool = Field(True, description="Start the Manager worker queue after enqueueing")
    client_id: str = Field("fl-mcp", description="Manager client identifier")
    ui_id: Optional[str] = Field(None, description="Optional Manager task UI ID")

class ManagerInstalledPacksRequest(BaseModel):
    """List installed ComfyUI Manager node packs."""
    mode: Literal["default", "imported"] = Field("default", description="Installed pack source")

class ManagerQueueStatusRequest(BaseModel):
    """Read ComfyUI Manager queue status."""
    client_id: Optional[str] = Field(None, description="Optional Manager client ID filter")

class ManagerSnapshotsRequest(BaseModel):
    """List Manager v4 snapshots."""
    pass

class DeleteQueueItemsRequest(BaseModel):
    """Request to delete items from the queue."""
    clear_all: Optional[bool] = Field(
        None,
        description="If True, clear all pending items from queue (cannot be used with prompt_ids)"
    )
    prompt_ids: Optional[List[str]] = Field(
        None,
        description="List of specific prompt IDs to delete from queue (cannot be used with clear_all)"
    )
    interrupt_running: Optional[bool] = Field(
        False,
        description="If True, also interrupt the currently running workflow"
    )


# System Control
class DisableSleepRequest(BaseModel):
    """Request to disable system sleep."""
    pass

class EnableSleepRequest(BaseModel):
    """Request to enable system sleep."""
    pass

class DisableScreensaverRequest(BaseModel):
    """Request to disable screensaver."""
    pass

class EnableScreensaverRequest(BaseModel):
    """Request to enable screensaver."""
    pass

class SendImagesRequest(BaseModel):
    """Request to send images to external URL."""
    url: str = Field(..., description="Target URL to send images to")
    field: str = Field(..., description="Form field name for images")
    file_paths: List[Union[str, Dict[str, Any]]] = Field(..., description="List of file paths or PreviewImage node objects")

# Utility
class GenerateSeedRequest(BaseModel):
    """Request to generate random seed."""
    pass

class GenerateFloatRequest(BaseModel):
    """Request to generate random float."""
    min: float = Field(..., description="Minimum value")
    max: float = Field(..., description="Maximum value")

class GenerateIntRequest(BaseModel):
    """Request to generate random integer."""
    min: int = Field(..., description="Minimum value")
    max: int = Field(..., description="Maximum value")

class RandomChoiceRequest(BaseModel):
    """Request to pick random item from list."""
    items: List[Any] = Field(..., description="List of items to choose from")

class GetSystemInfoRequest(BaseModel):
    """Request for system information."""
    pass  # No parameters needed

# # Error Feedback
# class GetRecentErrorsRequest(BaseModel):
#     """Request to get recent execution errors."""
#     limit: int = Field(10, description="Number of recent errors to retrieve (default: 10, max: 100)")

# class GetErrorsForRunRequest(BaseModel):
#     """Request to get errors for a specific workflow run."""
#     prompt_id: str = Field(..., description="The prompt/run ID to get errors for")

class GetWorkflowHistoryRequest(BaseModel):
    """Request for workflow history."""
    prompt_id: Optional[str] = Field(
        default=None,
        description="Specific prompt ID to get history for. If None, returns recent history."
    )
    max_items: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of history items to return (1-100)"
    )

class GetQueueStatusDetailsRequest(BaseModel):
    """Request to get detailed queue status and active executions."""
    pass

class GetExecutionDetailsRequest(BaseModel):
    """Request to get execution details for a specific run."""
    prompt_id: str = Field(..., description="The prompt/run ID to get details for")


class ViewOutputImageRequest(BaseModel):
    """Request a generated image as visual MCP content."""
    prompt_id: Optional[str] = Field(
        default=None,
        description="Execution prompt ID. If omitted, use the latest successful execution with images.",
    )
    node_id: Optional[str] = Field(
        default=None,
        description="Optional output node ID. If omitted, consider images from all output nodes.",
    )
    output_index: int = Field(
        default=-1,
        ge=-1,
        description="Image index across matching outputs. -1 selects the final image.",
    )
    max_dimension: int = Field(
        default=2048,
        ge=256,
        le=4096,
        description="Maximum preview width or height sent to the vision model.",
    )


class ViewChatImageRequest(BaseModel):
    """Request a user-attached chat image as visual MCP content."""
    image: ChatImageReference
    max_dimension: int = Field(default=2048, ge=256, le=4096)


class PlaceChatImageInNodeRequest(BaseModel):
    """Assign a user-attached image to a Load Image-style canvas node."""
    image: ChatImageReference
    node_id: Optional[Union[int, str]] = Field(
        default=None,
        description="Target node ID/title. Omit to use exactly one selected image node.",
    )

class ClearErrorBufferRequest(BaseModel):
    """Request to clear the error buffer."""
    pass

class WaitRequest(BaseModel):
    delay: float = Field(..., description="Brief period of time to wait (keep between 5 and 20 seconds). Great for waiting a bit after the workflow is queued to show some result")


class WebSearchRequest(BaseModel):
    """Search the public web using the mode chosen in Ren's composer."""

    query: str = Field(..., min_length=1, max_length=500, description="Focused search query")
    max_results: int = Field(5, ge=1, le=10, description="Maximum ranked results")
    time_range: Optional[Literal["day", "week", "month", "year"]] = Field(
        None,
        description="Optional freshness window",
    )


class WebFetchPageRequest(BaseModel):
    """Fetch and locally extract one public result page."""

    url: str = Field(..., min_length=1, max_length=2048, description="Public HTTP(S) URL")
    max_chars: int = Field(12000, ge=1000, le=30000, description="Maximum extracted characters")
    force_refresh: bool = Field(False, description="Ignore a cached extraction")
    include_images: bool = Field(
        False,
        description=(
            "Return image candidates only when the current user explicitly asked for images "
            "or visual references"
        ),
    )


class CustomNodesPathRequest(BaseModel):
    path: str = Field(".", description="Path inside ComfyUI/custom_nodes")


class CustomNodesReadFileRequest(BaseModel):
    path: str = Field(..., description="File path inside ComfyUI/custom_nodes")
    max_chars: int = Field(12000, ge=1, le=24000, description="Maximum characters to return")
    start_line: int = Field(1, ge=1, description="First 1-based line number to return")
    line_count: Optional[int] = Field(240, ge=1, le=800, description="Maximum number of lines to return")


class CustomNodesSearchRequest(BaseModel):
    query: str = Field(..., description="Search text or regex for ripgrep")
    path: str = Field(".", description="Folder inside ComfyUI/custom_nodes")
    glob: Optional[str] = Field(None, description="Optional glob such as '*.py'")
    max_results: int = Field(80, ge=1, le=80, description="Maximum matches")


class CustomNodesWriteFileRequest(BaseModel):
    path: str = Field(..., description="File path inside ComfyUI/custom_nodes")
    content: str = Field(..., description="Full file content to write")
    overwrite: bool = Field(False, description="Allow replacing an existing file")


class CustomNodesApplyPatchRequest(BaseModel):
    patch: str = Field(..., description="Unified diff patch. All touched files must be under custom_nodes.")


class CustomNodesCreatePackRequest(BaseModel):
    name: str = Field(..., description="Folder/package name for the new custom node pack")
    node_class: str = Field("FLMCPExampleNode", description="Python class name for the initial node")
    display_name: str = Field("FL-MCP Example Node", description="Display name in ComfyUI")
    category: str = Field("FL-MCP", description="ComfyUI node category")
    overwrite: bool = Field(False, description="Allow writing into an existing pack")


class CustomNodesGitCommitRequest(BaseModel):
    path: str = Field(".", description="Path inside custom_nodes to add/commit")
    message: str = Field(..., description="Commit message")


class ComfyLogsRequest(BaseModel):
    limit: int = Field(300, ge=1, le=2000, description="Number of log lines")

# ============================================================================
# NODE LIBRARY REQUEST MODELS
# ============================================================================

class NodeLibraryStatusRequest(BaseModel):
    """Inspect or refresh the local /object_info catalog snapshot."""
    refresh: bool = Field(
        False,
        description="Fetch a fresh /object_info snapshot before returning status"
    )


class NodeLibrarySearchRequest(BaseModel):
    """Search for ComfyUI node types by various criteria."""
    query: Optional[str] = Field(
        None,
        description="Text search in node type names and descriptions (case-insensitive)"
    )
    category: Optional[str] = Field(
        None,
        description="Filter by node category (e.g., 'sampling', 'loaders', 'image')"
    )
    input_type: Optional[str] = Field(
        None,
        description="Find node types accepting this input type (e.g., 'LATENT', 'IMAGE')"
    )
    output_type: Optional[str] = Field(
        None,
        description="Find node types producing this output type (e.g., 'IMAGE', 'LATENT')"
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50)"
    )


class NodeKnowledgeSearchRequest(BaseModel):
    """Search the persistent last-valid locally loaded node knowledge index."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Concise capability or exact node class to find",
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of active persisted matches to return",
    )
    refresh: bool = Field(
        False,
        description=(
            "Refresh the authoritative live /object_info catalog before searching. "
            "A failed refresh preserves but marks the last-valid knowledge stale."
        ),
    )


class NodeLibraryGetDetailsRequest(BaseModel):
    """Get detailed information about a specific node type."""
    node_type: str = Field(
        ...,
        description="Exact node type name (e.g., 'KSampler', 'CheckpointLoaderSimple')"
    )


class NodeLibraryFindCompatibleRequest(BaseModel):
    """Find node types compatible with a given node type."""
    node_type: str = Field(
        ...,
        description="Source node type name (e.g., 'KSampler')"
    )
    direction: Literal["downstream", "upstream", "both"] = Field(
        "downstream",
        description="downstream=connects AFTER, upstream=connects BEFORE, both=both directions"
    )
    output_slot: Optional[str] = Field(
        None,
        description="Specific output slot name to match (downstream only)"
    )
    input_slot: Optional[str] = Field(
        None,
        description="Specific input slot name to match (upstream only)"
    )
    max_results: int = Field(
        30,
        ge=1,
        le=100,
        description="Maximum results per direction (1-100)"
    )


class RegistrySearchPackagesRequest(BaseModel):
    """Search the official Comfy Registry for published custom-node packs."""
    query: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Concise capability terms, for example 'background removal', not the "
            "user's whole request. Use a short generic phrase such as 'new nodes' "
            "when the user wants to browse Registry-ranked packages."
        ),
    )
    comfy_node_search: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description="Optional Comfy node class/name to match inside published packs",
    )
    supported_os: Optional[str] = Field(
        None,
        min_length=1,
        max_length=40,
        description="Optional Registry operating-system filter, for example macos, windows, or linux",
    )
    supported_accelerator: Optional[str] = Field(
        None,
        min_length=1,
        max_length=40,
        description="Optional Registry accelerator filter, for example metal, cuda, or cpu",
    )
    include_installed: bool = Field(
        False,
        description=(
            "Include packages Manager identifies as already installed. Defaults to false "
            "for new-node discovery."
        ),
    )
    max_results: int = Field(
        10,
        ge=1,
        le=20,
        description="Maximum number of ranked packages to return (1-20)",
    )
    refresh: bool = Field(
        False,
        description="Bypass the lightweight Registry response cache",
    )


class RegistryGetPackageRequest(BaseModel):
    """Read one official Comfy Registry package and its published nodes."""
    package_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Exact Registry package identifier returned by registry_search_packages",
    )
    refresh: bool = Field(
        False,
        description="Bypass the lightweight Registry response cache",
    )
    max_classes: int = Field(
        200,
        ge=1,
        le=200,
        description="Maximum published node classes to include (1-200)",
    )

# ============================================================================
# MANAGER REQUEST MODELS
# ============================================================================

class ManagerSearchNodesRequest(BaseModel):
    """Search for custom node packs in ComfyUI Manager."""
    query: Optional[str] = Field(None, description="Search query for node pack name/description/author")
    node_filter: Optional[str] = Field(None, description="Regex pattern to filter by node class names (e.g., 'KSampler', 'FL_.*', 'Image.*Saver')")
    category: Optional[str] = Field(None, description="Filter by category")
    installed_only: bool = Field(False, description="Only show installed packs")
    updates_available: bool = Field(False, description="Only show packs with updates available")
    mode: Literal["local", "remote", "cache"] = Field("cache", description="Data source mode")
    max_results: int = Field(16, ge=1, le=100, description="Maximum results to return")


class ManagerGetNodeMappingsRequest(BaseModel):
    """Get node type to pack mappings from ComfyUI Manager."""
    node_type: Optional[str] = Field(None, description="Specific node type to look up (empty for all)")
    mode: Literal["local", "remote", "cache", "nickname"] = Field("local", description="Mapping source")


class ManagerCheckUpdatesRequest(BaseModel):
    """Check for available updates to installed node packs."""
    mode: Literal["local", "remote"] = Field("remote", description="Check mode")


class ManagerSearchExternalModelsRequest(BaseModel):
    """Search for uninstalled models in ComfyUI Manager registry."""
    query: Optional[str] = Field(
        None, 
        description="Regex search across name, description, filename"
    )
    base_filter: Optional[str] = Field(
        None, 
        description="Regex filter for base (e.g., 'FLUX', 'SDXL', 'SD1')"
    )
    type_filter: Optional[str] = Field(
        None, 
        description="Regex filter for type (e.g., 'checkpoint', 'lora', 'upscale', 'TAESD')"
    )
    name_filter: Optional[str] = Field(
        None, 
        description="Regex filter for model name"
    )
    description_filter: Optional[str] = Field(
        None, 
        description="Regex filter for description text"
    )
    reference_filter: Optional[str] = Field(
        None, 
        description="Regex filter for reference URL"
    )
    uninstalled_only: bool = Field(
        True, 
        description="Only show uninstalled models (default: True)"
    )
    installed_only: bool = Field(
        False, 
        description="Only show installed models (default: False)"
    )
    max_results: int = Field(
        10, 
        ge=1, 
        le=100, 
        description="Maximum results to return (1-100)"
    )
    mode: Literal["cache", "remote"] = Field(
        "cache", 
        description="Data source mode"
    )
    
# PNG Workflow Extraction
class ExtractWorkflowFromImageRequest(BaseModel):
    """Request to extract workflow from PNG metadata."""
    image_path: str = Field(
        ...,
        description="Path to PNG file relative to ComfyUI root (e.g., 'output/ComfyUI_00042_.png')"
    )

class ComfyUploadImageRequest(BaseModel):
    """Upload an image already present under the ComfyUI root."""
    image_path: str = Field(..., description="Relative path under ComfyUI root to upload")
    image_type: Literal["input", "output", "temp"] = Field("input", description="ComfyUI target image type")
    subfolder: str = Field("", description="Target subfolder")
    overwrite: bool = Field(False, description="Overwrite existing destination file")

class ComfyUploadMaskRequest(BaseModel):
    """Upload a mask already present under the ComfyUI root."""
    image_path: str = Field(..., description="Relative path under ComfyUI root to mask image")
    original_ref: Dict[str, Any] = Field(..., description="Original image reference expected by /upload/mask")
    image_type: Literal["input", "output", "temp"] = Field("input", description="ComfyUI target image type")
    subfolder: str = Field("", description="Target subfolder")

class ComfyModelsListRequest(BaseModel):
    """List ComfyUI model folders or files."""
    folder: Optional[str] = Field(None, description="Optional model folder name such as checkpoints or loras")

class ComfyWorkflowTemplatesRequest(BaseModel):
    """List workflow templates."""
    pack: Optional[str] = Field(None, description="Optional custom node pack name")
    filename: Optional[str] = Field(None, description="Optional template JSON filename")

class ComfyGlobalSubgraphsRequest(BaseModel):
    """List or read global subgraphs."""
    id: Optional[str] = Field(None, description="Optional subgraph ID")

class ComfyAssetsListRequest(BaseModel):
    """List ComfyUI assets."""
    limit: int = Field(50, ge=1, le=200, description="Maximum assets to return")
    offset: int = Field(0, ge=0, description="Offset for pagination")
    include_tags: Optional[List[str]] = Field(None, description="Only include assets with these tags")
    exclude_tags: Optional[List[str]] = Field(None, description="Exclude assets with these tags")
    name_contains: Optional[str] = Field(None, description="Filter by name substring")

class ComfyAssetRequest(BaseModel):
    """Read or delete one ComfyUI asset."""
    asset_id: str = Field(..., description="Asset UUID")

class ComfyAssetUploadRequest(BaseModel):
    """Upload a local ComfyUI-root file to the assets API."""
    file_path: str = Field(..., description="Relative path under ComfyUI root")
    name: Optional[str] = Field(None, description="Optional asset display name")
    tags: Optional[List[str]] = Field(None, description="Optional asset tags")
    mime_type: Optional[str] = Field(None, description="Optional MIME type override")

class ComfyTagsListRequest(BaseModel):
    """List ComfyUI asset tags."""
    prefix: Optional[str] = Field(None, description="Optional tag prefix")
    limit: int = Field(100, ge=1, le=500, description="Maximum tags to return")
    offset: int = Field(0, ge=0, description="Offset for pagination")


# ===========================================================================
# GENERAL UTILITIES
# ===========================================================================

@mcp.tool()
async def calculate_expressions(request: CalcBatchParams, ctx: Context) -> Dict[str, Any]:
    """
    Evaluate a *batch* of math AST expressions return their results. Great for calculating simple math expressions for calculating bounding boxes for layout modification. Don't include comments.

    Features:
      • Supports + - * / // % **, parentheses, unary +/-
      • Variables & **simple assignments** (`x = 2+3`) that persist across lines
      • Math funcs: sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
        exp, log, log10, log2, sqrt, floor, ceil, hypot, radians, degrees
      • Builtins: abs, round, min, max, pow
      • Constants: pi, e, tau
      • Random (seeded via `params.seed`): `rand()` / `random()`, `uniform(a,b)`, `randint(a,b)`
      • No `eval` or attributes; AST is strictly whitelisted
      • If `params.variables` is given, it is **updated** with numeric names

    Returns
    -------
    list[float] : one numeric result per input expression (assignment returns assigned value)
    """
    await _report_tool_activity(ctx, "calculate_expressions")
    
    try:
        response = await acalc_batch(request)
        return {"results": response}
    except Exception as e:
        ctx.error(str(e))
        raise

@mcp.tool()
async def wait(request: WaitRequest, ctx: Context) -> Dict[str, Any]:
    """Use this to wait for some short period of time, perhaps after generating an image"""
    await _report_tool_activity(ctx, "wait")
    
    await asyncio.sleep(float(request.delay))
    return {"waited_for": request.delay}


@mcp.tool()
async def web_search(request: WebSearchRequest, ctx: Context) -> Dict[str, Any]:
    """Search the web with the user-selected Free, Tavily Basic, or Tavily Advanced mode.

    The provider and Tavily depth are fixed by the current Ren message's composer action.
    Results include titles, URLs, snippets, and actual Tavily credit usage when applicable.
    """
    await _report_tool_activity(ctx, "web_search")
    service: WebSearchService = ctx.request_context.lifespan_context["web_search"]
    response = await service.search(
        request.query,
        max_results=request.max_results,
        time_range=request.time_range,
    )
    return {"success": True, **response.model_dump(mode="json")}


@mcp.tool()
async def web_fetch_page(request: WebFetchPageRequest, ctx: Context) -> Dict[str, Any]:
    """Safely fetch and locally extract a public web page returned by search.

    Private, loopback, metadata, unsafe-port, oversized, binary, and unsafe redirect targets
    are rejected. Ordinary HTML is parsed locally and cached without Tavily credits.
    """
    await _report_tool_activity(ctx, "web_fetch_page")
    service: WebPageService = ctx.request_context.lifespan_context["web_pages"]
    page = await service.fetch_page(request.url, force_refresh=request.force_refresh)
    content = page.markdown or page.text
    truncated = len(content) > request.max_chars
    images_allowed = bool(
        ctx.request_context.lifespan_context.get("web_images_allowed", True)
    )
    include_images = request.include_images and images_allowed
    warnings = list(page.warnings)
    if request.include_images and not images_allowed:
        warnings.append(
            "Web images were omitted because the current user message did not explicitly "
            "request them."
        )
    return {
        "success": True,
        "requestedUrl": page.requested_url,
        "finalUrl": page.final_url,
        "canonicalUrl": page.canonical_url,
        "title": page.title,
        "description": page.description,
        "language": page.language,
        "content": content[:request.max_chars],
        "contentLength": len(content),
        "truncated": truncated,
        "contentHash": page.content_hash,
        "qualityScore": page.quality_score,
        "requiresHostedFallback": page.requires_hosted_fallback,
        "fromCache": page.from_cache,
        "links": [item.model_dump(mode="json") for item in page.links[:25]],
        "images": (
            [item.model_dump(mode="json") for item in page.images[:10]]
            if include_images
            else []
        ),
        "imagesIncluded": include_images,
        "warnings": warnings,
    }

# ============================================================================
# QUERY & ANALYSIS TOOLS
# ============================================================================

@mcp.tool()
async def query_workflow(request: WorkflowQuery, ctx: Context) -> Dict[str, Any]:
    """Query the workflow graph using structured filters, traversal, and aggregation."""
    return await _execute_tool(ctx, "query_workflow", request.model_dump())


@mcp.tool()
async def workflow_overview(request: WorkflowOverviewRequest, ctx: Context) -> Dict[str, Any]:
    """Get a comprehensive overview of the current workflow."""
    return await _execute_tool(ctx, "workflow_overview", {})


@mcp.tool()
async def workflow_diagram(request: WorkflowDiagramRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a Mermaid diagram of the workflow or subset of nodes."""
    return await _execute_tool(ctx, "workflow_diagram", request.model_dump())


@mcp.tool()
async def frontend_list_commands(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """List registered ComfyUI frontend commands the browser bridge can execute."""
    return await _execute_tool(ctx, "frontend_list_commands", {})


@mcp.tool()
async def frontend_execute_command(request: FrontendExecuteCommandRequest, ctx: Context) -> Dict[str, Any]:
    """Execute a ComfyUI frontend command by command ID."""
    return await _execute_tool(ctx, "frontend_execute_command", request.model_dump())


@mcp.tool()
async def frontend_list_keybindings(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """List ComfyUI commands and their current keybindings."""
    return await _execute_tool(ctx, "frontend_list_keybindings", {})


@mcp.tool()
async def workflow_get_current_json(request: WorkflowCurrentJsonRequest, ctx: Context) -> Dict[str, Any]:
    """Get the current workflow as editable JSON or API prompt JSON."""
    return await _execute_tool(ctx, "workflow_get_current_json", request.model_dump())


@mcp.tool()
async def workflow_load_json(request: WorkflowLoadJsonRequest, ctx: Context) -> Dict[str, Any]:
    """Load editable workflow JSON into the active ComfyUI canvas."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_load_json", request.model_dump(), timeout_ms=60000)


@mcp.tool()
async def workflow_get_tabs(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """List open ComfyUI workflow tabs and the active tab."""
    return await _execute_tool(ctx, "workflow_get_tabs", {})


@mcp.tool()
async def workflow_list_files(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """List saved workflow files in ComfyUI user data."""
    return await _execute_tool(ctx, "workflow_list_files", {})


@mcp.tool()
async def workflow_read_file(request: WorkflowPathRequest, ctx: Context) -> Dict[str, Any]:
    """Read a saved workflow JSON file from ComfyUI user data."""
    return await _execute_tool(ctx, "workflow_read_file", request.model_dump())


@mcp.tool()
async def workflow_save_current(request: WorkflowSaveCurrentRequest, ctx: Context) -> Dict[str, Any]:
    """Save the current workflow to ComfyUI user data."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_save_current", request.model_dump())


@mcp.tool()
async def workflow_rename_file(request: WorkflowRenameRequest, ctx: Context) -> Dict[str, Any]:
    """Rename or move a saved workflow file in ComfyUI user data."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_rename_file", request.model_dump())


@mcp.tool()
async def workflow_delete_file(request: WorkflowPathRequest, ctx: Context) -> Dict[str, Any]:
    """Delete a saved workflow file from ComfyUI user data."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_delete_file", request.model_dump())


@mcp.tool()
async def workflow_close_current(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Close the active ComfyUI workflow tab via the native frontend command."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_close_current", {})


@mcp.tool()
async def workflow_duplicate_current(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Duplicate the active ComfyUI workflow tab via the native frontend command."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "workflow_duplicate_current", {})


# ============================================================================
# NODE MANAGEMENT TOOLS
# ============================================================================

# TODO: Add tools to see what nodes are installed in the comfy sort of python environment (checking custom_nodes folder?) (needs web research)
#       kinda also needs a way to see what's like in each node pack somehow (is there an easy comfy lib way for this?)

@mcp.tool()
async def find_node(request: FindNodeRequest, ctx: Context) -> Dict[str, Any]:
    """Find a node by ID, type, or title."""
    return await _execute_tool(ctx, "find_node", request.model_dump(exclude_none=True))


@mcp.tool()
async def create_nodes(request: CreateNodesRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Create one or more new nodes in the workflow.

    Before calling this tool, prove every node type is locally loaded with
    node_library_search. For a new workflow or topology change, validate the
    intended graph with plan_workflow and proceed only from a valid plan hash.

    This is a TRUE BATCH operation - all nodes are created in a single frontend execution without round-trips per node.
    Placement is collision-aware: each node is measured after creation, moved
    clear of existing rectangles when needed, and returned with its final
    position and size. Omit x/y when exact placement is unimportant.
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")  # type: ignore[return-value]
    node_count = len(request.nodes)
    logger.info(f"[BATCH] Creating {node_count} nodes in single batch operation")

    # Send all nodes at once to frontend for batch creation
    result = await _execute_tool(ctx, "create_nodes_batch", request.model_dump())

    logger.info(f"[BATCH] Batch create complete: {node_count} nodes")
    return result


@mcp.tool()
async def remove_nodes(request: RemoveNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Remove one or more nodes from the workflow."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "remove_nodes", request.model_dump())


@mcp.tool()
async def bypass_nodes(request: BypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Bypass (mute) one or more nodes."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "bypass_nodes", request.model_dump())


@mcp.tool()
async def unbypass_nodes(request: UnbypassNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unbypass (unmute) one or more nodes."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "unbypass_nodes", request.model_dump())


@mcp.tool()
async def pin_nodes(request: PinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Pin one or more nodes to prevent movement."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "pin_nodes", request.model_dump())


@mcp.tool()
async def unpin_nodes(request: UnpinNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Unpin one or more nodes to allow movement."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "unpin_nodes", request.model_dump())


@mcp.tool()
async def select_nodes(request: SelectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Select one or more nodes in the UI."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "select_nodes", request.model_dump())

@mcp.tool()
async def focus_on_nodes(request: FocusOnNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Fit canvas view to show specific nodes, the current selection, or the whole workflow.

    This tool uses ComfyUI's native Fit View command, equivalent to pressing the `.` hotkey.
    Use it before every screenshot and after layout edits so screenshots are centered and not cropped.

    If you don't know what nodes to focus on, use `workflow_overview` to get node ids based on your task.
    
    This tool adjusts the canvas viewport to center and fit the specified nodes,
    making them clearly visible. Useful for:
    - Focusing on a workflow section before taking a screenshot
    - Navigating to specific nodes in large workflows
    - Preparing visual context for user
    
    PARAMETERS:
    - node_ids: Optional list of node IDs
      - null (default): Fit to currently selected nodes
      - [] (empty list): Fit to all nodes in the workflow
      - [1, 2, 3]: Fit to specific nodes
    
    WORKFLOW:
    1. select_nodes([1, 2, 3]) - Select nodes
    2. focus_on_nodes() - Zoom to selected nodes (no params needed)
    3. take_screenshot() - Capture the focused view
    
    RETURNS:
    - fitted_count: Number of nodes fitted in view
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "focus_on_nodes", request.model_dump())

@mcp.tool()
async def take_screenshot(request: TakeScreenshotRequest, ctx: Context) -> Dict[str, Any]:
    """Capture the current ComfyUI canvas as an image.

    By default, this tool automatically triggers ComfyUI's native Fit View behavior
    before capture, equivalent to pressing the `.` hotkey.
    - whole workflow: omit node_ids or pass an empty list
    - specific section: pass those node IDs
    - current selection: pass null for node_ids
    - exact current viewport: set fit_view=false

    This avoids cropped or off-center screenshots without requiring a separate
    `focus_on_nodes` call.
    
    This tool takes a screenshot of the workflow canvas and saves it to
    output/screenshots/. The screenshot can then be displayed to the user
    or analyzed by an MCP client.
    
    USE CASES:
    - Visual documentation: "Show me the workflow"
    - Section capture: Focus on nodes, then screenshot
    - Debugging: Capture problematic workflow sections for the user to see
    - Sharing: Create shareable workflow images
    
    The URL can be embedded directly in MCP client responses:
    ![Screenshot](api/view?filename={filename}&type=output&subfolder=screenshots&rand=0.123)
    """
    result = await _execute_tool(ctx, "take_screenshot", request.model_dump())
    
    # Add URL for easy markdown embedding with public_url support
    if result.get('success') and result.get('filename'):
        import random
        from config import settings
        
        # Use public_url from settings (supports both localhost and ngrok)
        base_url = settings.public_url.rstrip('/')
        result['url'] = (
            f"{base_url}/api/view?filename={result['filename']}"
            f"&type=output&subfolder=screenshots&rand={random.random()}"
        )
    
    return result


@mcp.tool()
async def get_current_node_selection(request: GetSelectedNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Get currently selected nodes in ComfyUI to understand user's current focus.
    
    This tool provides context-aware assistance by returning detailed information
    about the nodes the user currently has selected in the workflow canvas.
    
    USE CASES:
    - User asks "what does this node do?" - Check selected nodes for context
    - User says "change the seed" - Find seed parameter in selected nodes
    - User requests modifications - Know which nodes they're referring to
    - Debugging assistance - Analyze parameters of nodes user is examining
    
    RETURNS:
    Dictionary with 'nodes' key containing array of selected node objects.
    Each node includes:
    - id: Node ID (integer)
    - title: Node title (string)
    - type: Node type/class (string, e.g., "KSampler")
    - position: {x: float, y: float}
    - size: {width: float, height: float}
    - mode: Node mode (0=normal, 2=muted, 4=bypassed)
    - parameters: Dictionary of parameter name -> value
    - inputs: Array of {name, type, link} objects
    - outputs: Array of {name, type, links} objects
    
    If no nodes are selected, returns empty array: {"nodes": []}    
    """
    return await _execute_tool(ctx, "get_selected_nodes", {})


# ============================================================================
# NODE MANIPULATION TOOLS
# ============================================================================

@mcp.tool()
async def get_node_values(request: GetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Get all parameter values from a node."""
    return await _execute_tool(ctx, "get_node_values", request.model_dump())


@mcp.tool()
async def view_node_mask(request: ViewNodeMaskRequest, ctx: Context) -> ToolResult:
    """View a node's current image with masked pixels highlighted in magenta.

    Inspect this overlay before editing so coordinates avoid subjects that must
    remain unchanged. Coordinates reported by visual inspection use the image's
    top-left as (0, 0).
    """
    node_result = await _execute_tool(
        ctx,
        "get_node_image_ref",
        {"node_id": request.node_id},
    )
    image_ref = node_result["image"]
    path = _resolve_comfy_image_path(get_comfy_tools(), image_ref)
    preview, preview_format, original_size, preview_size, mask_info = _mask_overlay_preview(
        path,
        request.max_dimension,
    )
    result = {
        "success": True,
        **node_result,
        "image": image_ref,
        "originalSize": {"width": original_size[0], "height": original_size[1]},
        "previewSize": {"width": preview_size[0], "height": preview_size[1]},
        "mask": mask_info,
        "message": "Masked pixels are highlighted in magenta. Inspect the overlay before choosing edit coordinates.",
    }
    return ToolResult(
        content=[result, MCPImage(data=preview, format=preview_format)],
        structured_content=result,
    )


@mcp.tool()
async def edit_node_mask(request: EditNodeMaskRequest, ctx: Context) -> ToolResult:
    """Paint or erase rectangular/elliptical regions in a node's image mask.

    This uses ComfyUI's authenticated browser upload path and updates the node's
    image widget to the newly saved masked image. Use normalized coordinates for
    resolution-independent edits. Set clear_existing=true when the supplied
    regions should be the only masked areas.
    """
    if not settings.enable_workflow_writes:
        result = _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
        return ToolResult(content=result, structured_content=result)
    edit_result = await _execute_tool(
        ctx,
        "edit_node_mask",
        request.model_dump(),
    )
    image_ref = edit_result["image"]
    path = _resolve_comfy_image_path(get_comfy_tools(), image_ref)
    preview, preview_format, original_size, preview_size, mask_info = _mask_overlay_preview(
        path,
        2048,
    )
    result = {
        **edit_result,
        "originalSize": {"width": original_size[0], "height": original_size[1]},
        "previewSize": {"width": preview_size[0], "height": preview_size[1]},
        "mask": mask_info,
        "message": (
            "Mask saved and shown in magenta on the canvas. Call "
            "confirm_mask_review with the returned review token so the user "
            "can approve it before queueing."
        ),
    }
    return ToolResult(
        content=[result, MCPImage(data=preview, format=preview_format)],
        structured_content=result,
    )


@mcp.tool()
async def confirm_mask_review(
    request: ConfirmMaskReviewRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Ask the user to approve the visible mask before workflow execution.

    Call this immediately after every successful edit_node_mask. The user must
    explicitly accept the magenta canvas preview; this review cannot be bypassed
    or remembered for future masks. Queueing remains blocked until it succeeds.
    """
    return await _execute_tool(ctx, "confirm_mask_review", request.model_dump())


@mcp.tool()
async def set_node_values(request: SetNodeValuesRequest, ctx: Context) -> Dict[str, Any]:
    """Set parameter values on a node."""
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "set_node_values", request.model_dump())


@mcp.tool()
async def connect_nodes(request: ConnectNodesRequest, ctx: Context) -> Dict[str, Any]:
    """Connect two nodes with optional auto-matching.
    
    BASIC USAGE (with slot names):
    Provide exact slot names for reliable connections.
    
    SMART USAGE (auto-match by type):
    Omit slot names to automatically find compatible connections by type.
    
    PARAMETERS:
    - source_node_id: Source node ID or title (required)
    - target_node_id: Target node ID or title (required)
    - source_slot: Output slot name/index (optional, auto-matches if not provided)
    - target_slot: Input slot name/index (optional, auto-matches if not provided)
    - auto_match: Enable auto-matching (default: true)
    - match_strategy: How to auto-match (default: "type")
      - "first": Use first available output/input
      - "type": Match by compatible types
      - "name": Match by similar slot names
    
    RETURNS:
    Dictionary with connection details including source/target nodes, slots, and data type.
    
    ERROR HANDLING:
    If connection fails, error message includes available slots on both nodes
    and suggestion to use get_node_slots() for discovery.
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "connect_nodes", request.model_dump())


@mcp.tool()
async def get_node_slots(request: GetNodeSlotsRequest, ctx: Context) -> Dict[str, Any]:
    """Get detailed input and output slot information for a node.
    
    This tool enables MCP clients to discover exact slot names, types, and connection status
    before attempting to connect nodes, eliminating guesswork and connection failures.
    
    USE CASES:
    - Pre-connection discovery: Determine available slots before connecting
    - Type matching: Find compatible slots by data type
    - Connection debugging: Understand why connections fail
    - Workflow planning: Verify connection compatibility
    
    RETURNS:
    Dictionary containing:
    - node_id: Node ID (integer)
    - type: Node type/class (string)
    - title: Node title (string)
    - inputs: Array of input slot objects with name, type, index, connection status
    - outputs: Array of output slot objects with name, type, index, connection status
    
    Each slot object includes:
    - name: Exact slot name (case-sensitive string)
    - type: Data type (e.g., "LATENT", "IMAGE", "MODEL")
    - index: Slot index for direct connection (integer)
    - connected: Whether slot is currently connected (boolean)
    - connected_from/connected_to: Connection details if connected
    """
    return await _execute_tool(ctx, "get_node_slots", request.model_dump())


@mcp.tool()
async def connect_nodes_batch(request: ConnectNodesBatchRequest, ctx: Context) -> Dict[str, Any]:
    """Connect multiple node pairs in a single batch operation.
    
    This tool enables efficient batch connection of nodes, reducing the number of
    tool calls needed to build complex workflows from N calls to 1 call.
    
    PARAMETERS:
    - connections: List of connection specifications (source, target, optional slots)
    - auto_match: Enable auto-matching by type (default: true)
    - stop_on_error: Stop on first error vs continue (default: false = continue)
    
    RETURNS:
    Dictionary with:
    - total: Total number of connection attempts
    - successful: Number of successful connections
    - failed: Number of failed connections
    - results: Array of result objects for each connection
    
    Each result object contains:
    - success: Whether connection succeeded (boolean)
    - connection: Connection details if successful
    - error: Error message if failed
    - attempted: Original connection spec if failed
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "connect_nodes_batch", request.model_dump())


@mcp.tool()
async def auto_connect_workflow(request: AutoConnectWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Automatically connect nodes based on type compatibility.
    
    This tool simplifies workflow creation by automatically connecting nodes in sequence
    or by finding all compatible type matches.
    
    STRATEGIES:
    - "sequential": Connect nodes in order A→B→C→D (left to right workflow)
    - "type_match": Find and connect all compatible type pairs in the workflow
    
    PARAMETERS:
    - node_ids: List of node IDs to connect
    - strategy: Connection strategy (default: "sequential")
    
    RETURNS:
    Dictionary with:
    - connections_made: Number of successful connections
    - connections: Array of connection details
    - failed: Array of failed connection attempts with reasons
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(ctx, "auto_connect_workflow", request.model_dump())


# ============================================================================
# LAYOUT MANAGEMENT TOOLS
# ============================================================================

# @mcp.tool()
# async def get_node_rect(request: GetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
#     """Get node position and size. Only use this to """
#     return await _execute_tool(ctx, "get_node_rect", request.model_dump())


@mcp.tool()
async def get_layout(request: GetLayoutRequest, ctx: Context) -> Dict[str, Any]:
    """Get position and size for all nodes (or specified nodes) in the workflow. Use it to understand node spatial organization or understanding visual workflow structure.
    
    This tool retrieves layout information for the entire workflow at once. Use it before calling `modify_layout`.
    
    Returns:
        {
            "nodes": [
                {
                    "node_id": int,
                    "title": str,
                    "type": str,
                    "rect": {"x": float, "y": float, "width": float, "height": float}
                },
                ...
            ],
            "count": int
        }
        
    """
    return await _execute_tool(ctx, "get_layout", request.model_dump())


# @mcp.tool()
# async def set_node_rect(request: SetNodeRectRequest, ctx: Context) -> Dict[str, Any]:
#     """Set node position and/or size."""
#     return await _execute_tool(ctx, "set_node_rect", request.model_dump())

@mcp.tool()
async def modify_layout(request: BatchLayoutRequest, ctx: Context) -> List[Dict[str, Any]]:
    """Modify node layout using manual positioning or intelligent auto-layout.
    
    TWO MODES:
    
    1. MANUAL LAYOUT:
       Provide node_rects with explicit x/y/width/height for each node.
       Use get_layout first to see current positions.
    
    2. AUTO-LAYOUT:
       Set auto_layout=True and optionally specify strategy, node_ids, spacing.
       The layout engine analyzes connections and calculates optimal positions.
       
       Examples:
       - Arrange all nodes: {"auto_layout": true}
       - Horizontal flow: {"auto_layout": true, "strategy": "flow_horizontal"}
       - Specific nodes: {"auto_layout": true, "node_ids": [1, 2, 3]}
       - More spacing: {"auto_layout": true, "spacing_multiplier": 2.0}
    
    AUTO-LAYOUT STRATEGIES:
    - "flow_horizontal" (default): Left-to-right dataflow, ideal for standard pipelines
    - "flow_vertical": Top-to-bottom dataflow, good for ControlNet stacks
    - "grid": Simple grid layout for unconnected nodes
    
    RETURNS:
    Array of layout results for each modified node with success status.
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")  # type: ignore[return-value]
    return await _execute_tool(ctx, "modify_layout", request.model_dump())

# @mcp.tool()
# async def position_node_left(request: PositionNodeLeftRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node to the left of another node."""
#     return await _execute_tool(ctx, "position_node_left", request.model_dump())


# @mcp.tool()
# async def position_node_right(request: PositionNodeRightRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node to the right of another node."""
#     return await _execute_tool(ctx, "position_node_right", request.model_dump())


# @mcp.tool()
# async def position_node_top(request: PositionNodeTopRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node above another node."""
#     return await _execute_tool(ctx, "position_node_top", request.model_dump())


# @mcp.tool()
# async def position_node_bottom(request: PositionNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
#     """Position a node below another node."""
#     return await _execute_tool(ctx, "position_node_bottom", request.model_dump())


# @mcp.tool()
# async def move_node_right(request: MoveNodeRightRequest, ctx: Context) -> Dict[str, Any]:
#     """Move a node to the right, avoiding collisions."""
#     return await _execute_tool(ctx, "move_node_right", request.model_dump())


# @mcp.tool()
# async def move_node_bottom(request: MoveNodeBottomRequest, ctx: Context) -> Dict[str, Any]:
#     """Move a node downward, avoiding collisions."""
#     return await _execute_tool(ctx, "move_node_bottom", request.model_dump())


# ============================================================================
# WORKFLOW CONTROL TOOLS
# ============================================================================

@mcp.tool()
async def queue_workflow(request: QueueWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Queue the workflow for execution.

    Before calling this tool, call `workflow_overview` to check for disconnected
    nodes and missing slot connections. When completion waiting is enabled, this
    one tool call remains open and returns completed, execution_error, cancelled,
    or timeout without requiring model-driven status calls.
    """
    frontend_parameters = request.model_dump(
        exclude={"wait_for_completion", "completion_timeout"},
        exclude_none=True,
    )
    r = await _execute_tool(ctx, "queue_workflow", frontend_parameters)
    logger.debug(f"Queue result: {r}")

    prompt_id = r.get("prompt_id")
    node_errors = r.get("node_errors", {})
    queue_number = r.get("queue_number")
    batch_count = r.get("batch_count")

    if node_errors:
        logger.warning(f"Workflow validation failed: {node_errors}")
        return {
            "success": False,
            "error": "Workflow validation failed",
            "node_errors": node_errors,
            "suggestion": (
                "The workflow has node configuration errors. "
                "Use workflow_overview to identify disconnected nodes or missing inputs. "
                "Fix the errors and try queueing again."
            )
        }

    if not prompt_id:
        logger.error(f"No prompt_id in queue result: {r}")
        return {
            "success": False,
            "error": r.get("error", "No prompt_id returned from queue operation"),
            "raw_result": r,
            "suggestion": "Check the open workflow for validation errors and try again.",
        }

    base_result = {
        "queued": True,
        "prompt_id": prompt_id,
        "queue_number": queue_number,
        "batch_count": batch_count,
    }
    wait_for_completion = (
        request.wait_for_completion
        if request.wait_for_completion is not None
        else settings.wait_for_generation_completion
    )
    if not wait_for_completion:
        logger.info(f"Workflow queued successfully: {prompt_id} (position {queue_number})")
        return base_result | {
            "success": True,
            "status": "queued",
            "completed": False,
            "terminal": False,
            "waited": False,
            "message": f"Workflow queued successfully at position {queue_number}.",
        }

    timeout_seconds = (
        request.completion_timeout
        if request.completion_timeout is not None
        else settings.generation_completion_timeout
    )
    logger.info(f"Waiting up to {timeout_seconds}s for workflow {prompt_id}")
    outcome = await _wait_for_generation_completion(prompt_id, timeout_seconds)
    return base_result | outcome | {
        "waited": True,
        "completion_timeout": timeout_seconds,
    }


@mcp.tool()
async def cancel_workflow(request: CancelWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Cancel the currently executing workflow."""
    return await _execute_tool(ctx, "cancel_workflow", {})


@mcp.tool()
async def enable_auto_queue(request: EnableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Enable auto-queue mode."""
    return await _execute_tool(ctx, "enable_auto_queue", {})


@mcp.tool()
async def disable_auto_queue(request: DisableAutoQueueRequest, ctx: Context) -> Dict[str, Any]:
    """Disable auto-queue mode."""
    return await _execute_tool(ctx, "disable_auto_queue", {})


@mcp.tool()
async def set_batch_count(request: SetBatchCountRequest, ctx: Context) -> Dict[str, Any]:
    """Set the workflow batch count."""
    return await _execute_tool(ctx, "set_batch_count", request.model_dump())


@mcp.tool()
async def get_queue_status(request: GetQueueStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Get current queue status and settings."""
    return await _execute_tool(ctx, "get_queue_status", {})

@mcp.tool()
async def comfy_jobs_list(request: ComfyJobsListRequest, ctx: Context) -> Dict[str, Any]:
    """List ComfyUI jobs using the native /api/jobs endpoint."""
    await _report_tool_activity(ctx, "comfy_jobs_list")
    params = {k: v for k, v in request.model_dump().items() if v is not None}
    return await _comfy_request("GET", "/api/jobs", params=params)

@mcp.tool()
async def comfy_job_get(request: ComfyJobRequest, ctx: Context) -> Dict[str, Any]:
    """Get one ComfyUI job by prompt/job ID using the native /api/jobs/{id} endpoint."""
    await _report_tool_activity(ctx, "comfy_job_get")
    return await _comfy_request("GET", f"/api/jobs/{request.job_id}")

@mcp.tool()
async def comfy_free_memory(request: ComfyFreeMemoryRequest, ctx: Context) -> Dict[str, Any]:
    """Ask ComfyUI to unload models and/or free memory."""
    await _report_tool_activity(ctx, "comfy_free_memory")
    return await _comfy_request("POST", "/free", json_data=request.model_dump())

@mcp.tool()
async def comfy_history_delete(request: ComfyHistoryDeleteRequest, ctx: Context) -> Dict[str, Any]:
    """Delete ComfyUI history entries or clear all history."""
    await _report_tool_activity(ctx, "comfy_history_delete")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    if request.clear_all and request.prompt_ids:
        return {"success": False, "error": "clear_all and prompt_ids are mutually exclusive"}
    payload: Dict[str, Any] = {}
    if request.clear_all:
        payload["clear"] = True
    elif request.prompt_ids:
        payload["delete"] = request.prompt_ids
    else:
        return {"success": False, "error": "Provide clear_all=True or prompt_ids"}
    return await _comfy_request("POST", "/history", json_data=payload)

@mcp.tool()
async def comfy_settings_get(request: ComfySettingsGetRequest, ctx: Context) -> Dict[str, Any]:
    """Read all ComfyUI settings or one setting by ID."""
    await _report_tool_activity(ctx, "comfy_settings_get")
    if request.id:
        return await _comfy_request("GET", f"/settings/{request.id}")
    return await _comfy_request("GET", "/settings")

@mcp.tool()
async def comfy_settings_set(request: ComfySettingsSetRequest, ctx: Context) -> Dict[str, Any]:
    """Set one or more ComfyUI settings."""
    await _report_tool_activity(ctx, "comfy_settings_set")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    if request.id:
        return await _comfy_request("POST", f"/settings/{request.id}", json_data=request.value)
    if request.settings:
        return await _comfy_request("POST", "/settings", json_data=request.settings)
    return {"success": False, "error": "Provide either id/value or settings"}

@mcp.tool()
async def manager_queue_action(request: ManagerQueueActionRequest, ctx: Context) -> Dict[str, Any]:
    """Queue a ComfyUI Manager action such as install/update/uninstall/disable.

    This is intentionally confirmation-gated because Manager actions mutate the
    local ComfyUI installation and often require a restart. For Registry node
    installs, pass the canonical package ID and exact published version from
    registry_get_package. Manager installs package requirements in the Python
    environment running ComfyUI.
    """
    await _report_tool_activity(ctx, "manager_queue_action")
    if not settings.enable_manager_mutations:
        return _disabled_by_config("FL_MCP_ENABLE_MANAGER_MUTATIONS")
    endpoint_map = {
        "install_model": "install-model",
        "update_comfyui": "update-comfyui",
        "update_all": "update-all",
    }
    kind = endpoint_map.get(request.endpoint, request.endpoint)
    if not request.confirmed:
        return {
            "success": False,
            "confirmation_required": True,
            "message": (
                "Set confirmed=True to perform this Manager action. "
                "Install/update/uninstall/disable actions can change files and may require a ComfyUI restart."
            ),
            "endpoint": request.endpoint,
            "payload": request.payload,
        }

    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return await manager_client.queue_action(
            kind=kind,
            payload=request.payload,
            client_id=request.client_id,
            ui_id=request.ui_id,
            start_queue=request.start_queue,
        )
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_queue_status(request: ManagerQueueStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Get ComfyUI Manager queue status."""
    await _report_tool_activity(ctx, "manager_queue_status")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return {"success": True, "data": await manager_client.queue_status(client_id=request.client_id)}
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_queue_start(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Start the ComfyUI Manager worker queue."""
    await _report_tool_activity(ctx, "manager_queue_start")
    if not settings.enable_manager_mutations:
        return _disabled_by_config("FL_MCP_ENABLE_MANAGER_MUTATIONS")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return {"success": True, "data": await manager_client.queue_start()}
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_queue_reset(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Reset the ComfyUI Manager queue."""
    await _report_tool_activity(ctx, "manager_queue_reset")
    if not settings.enable_manager_mutations:
        return _disabled_by_config("FL_MCP_ENABLE_MANAGER_MUTATIONS")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return {"success": True, "data": await manager_client.queue_reset()}
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_v4_status(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Report ComfyUI Manager v4 availability and queue status."""
    await _report_tool_activity(ctx, "manager_v4_status")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return {"success": True, "data": await manager_client.status()}
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_v4_queue_status(request: ManagerQueueStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Get ComfyUI Manager v4 queue status."""
    return await manager_queue_status.fn(request, ctx)

@mcp.tool()
async def manager_v4_queue_action(request: ManagerQueueActionRequest, ctx: Context) -> Dict[str, Any]:
    """Queue a confirmation-gated ComfyUI Manager v4 action."""
    return await manager_queue_action.fn(request, ctx)

@mcp.tool()
async def manager_v4_installed_packs(request: ManagerInstalledPacksRequest, ctx: Context) -> Dict[str, Any]:
    """List installed custom node packs through Manager v4."""
    await _report_tool_activity(ctx, "manager_v4_installed_packs")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        data = await manager_client.list_installed_packs(mode=request.mode)
        return {"success": True, "data": data, "count": len(data or {})}
    except ManagerError as e:
        return {"success": False, "error": str(e), "data": {}, "count": 0}

@mcp.tool()
async def manager_v4_snapshots(request: ManagerSnapshotsRequest, ctx: Context) -> Dict[str, Any]:
    """List Manager v4 snapshots."""
    await _report_tool_activity(ctx, "manager_v4_snapshots")
    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if not manager_client:
        return {"success": False, "error": "ComfyUI Manager client not initialized"}
    try:
        return {"success": True, "data": await manager_client.list_snapshots()}
    except ManagerError as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
async def manager_v4_node_mappings(request: ManagerGetNodeMappingsRequest, ctx: Context) -> Dict[str, Any]:
    """Find node-to-pack mappings through Manager v4."""
    return await manager_get_node_mappings.fn(request, ctx)

@mcp.tool()
async def manager_v4_external_models(request: ManagerSearchExternalModelsRequest, ctx: Context) -> Dict[str, Any]:
    """Search Manager v4 external model definitions."""
    return await manager_search_external_models.fn(request, ctx)

@mcp.tool()
async def delete_queue_items(request: DeleteQueueItemsRequest, ctx: Context) -> Dict[str, Any]:
    """Delete items from the ComfyUI execution queue.
    
    Can clear all pending items, delete specific items by prompt_id, or interrupt
    the currently running workflow. Operations can be combined except clear_all
    and prompt_ids which are mutually exclusive.
    
    USE CASES:
    - Clear all pending: clear_all=True
    - Delete specific items: prompt_ids=["id1", "id2"] (get IDs from get_queue_status)
    - Stop everything: clear_all=True, interrupt_running=True
    - Just stop current: interrupt_running=True
    
    RETURNS:
    Dict with:
    - success: bool - overall operation success
    - cleared_all: bool - whether queue was cleared
    - deleted_ids: List[str] - IDs that were deleted
    - interrupted: bool - whether running workflow was interrupted
    - message: str - human-readable summary
    """
    await _report_tool_activity(ctx, "delete_queue_items")
    
    try:
        comfy_tools = get_comfy_tools()
        
        # Convert None to False for clear_all to match method signature
        clear_all_value = request.clear_all if request.clear_all is not None else False
        interrupt_value = request.interrupt_running if request.interrupt_running is not None else False
        
        result = await comfy_tools.delete_queue_items(
            clear_all=clear_all_value,
            prompt_ids=request.prompt_ids,
            interrupt_running=interrupt_value
        )
        
        return result
        
    except ComfyUIError as e:
        error_result = {
            "success": False,
            "error": str(e),
            "error_type": "ComfyUIError"
        }
        return error_result
    except Exception as e:
        logger.error(f"delete_queue_items failed: {e}")
        error_result = {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }
        return error_result


# ============================================================================
# SYSTEM CONTROL TOOLS
# ============================================================================

# THIS IS COMMENTED OUT BECAUSE: Vanilla claude code is a lying piece of shit and it didn't implement shit, plus this is fucking backend functionality... vibe coded slop... removed.

# @mcp.tool()
# async def disable_sleep(request: DisableSleepRequest, ctx: Context) -> Dict[str, Any]:
#     """Disable system sleep/suspend."""
#     return await _execute_tool(ctx, "disable_sleep", {})


# @mcp.tool()
# async def enable_sleep(request: EnableSleepRequest, ctx: Context) -> Dict[str, Any]:
#     """Enable system sleep/suspend."""
#     return await _execute_tool(ctx, "enable_sleep", {})


# @mcp.tool()
# async def disable_screensaver(request: DisableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
#     """Disable screensaver."""
#     return await _execute_tool(ctx, "disable_screensaver", {})


# @mcp.tool()
# async def enable_screensaver(request: EnableScreensaverRequest, ctx: Context) -> Dict[str, Any]:
#     """Enable screensaver."""
#     return await _execute_tool(ctx, "enable_screensaver", {})


# @mcp.tool()
# async def send_images(request: SendImagesRequest, ctx: Context) -> Dict[str, Any]:
#     """Send images to an external URL."""
#     return await _execute_tool(ctx, "send_images", request.model_dump())


# ============================================================================
# UTILITY TOOLS
# ============================================================================
# TODO: These all can use python instead of the frontend bridge, lol!

@mcp.tool()
async def generate_seed(request: GenerateSeedRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random seed value."""
    return await _execute_tool(ctx, "generate_seed", {})


@mcp.tool()
async def generate_float(request: GenerateFloatRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random float value."""
    return await _execute_tool(ctx, "generate_float", request.model_dump())


@mcp.tool()
async def generate_int(request: GenerateIntRequest, ctx: Context) -> Dict[str, Any]:
    """Generate a random integer value."""
    return await _execute_tool(ctx, "generate_int", request.model_dump())


@mcp.tool()
async def random_choice(request: RandomChoiceRequest, ctx: Context) -> Dict[str, Any]:
    """Pick a random item from a list."""
    return await _execute_tool(ctx, "random_choice", request.model_dump())

@mcp.tool()
async def get_system_info(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Get system and environment information for installation guidance.
    
    This tool provides OS, Python, and virtual environment details to help
    provide platform-specific installation instructions for ComfyUI components.
    
    USE CASES:
    - Installation Guidance: Determine correct pip/python commands for user's platform
    - Environment Detection: Check if running in venv/conda for dependency installation
    - Platform-Specific Help: Provide Windows vs Linux vs macOS specific instructions
    - ControlNet Setup: Guide users through manual model installation with correct paths
    - Dependency Installation: Show correct command syntax for user's environment
    
    RETURNED INFORMATION:
    - OS platform (Windows/Linux/Darwin) and architecture
    - Python version and executable path
    - Virtual environment status and type (venv/conda/virtualenv)
    - ComfyUI installation paths
    - Platform-specific installation command templates
        
    SECURITY: Read-only system information, no modifications.
    """
    await _report_tool_activity(ctx, "get_system_info")
    
    try:
        # Get ComfyUI tools to include installation paths
        tools = get_comfy_tools()
        
        # Get comprehensive system info
        info = _get_system_info(comfy_tools=tools)
        
        return info
        
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        # Still return basic info even if ComfyUI tools fail
        try:
            info = _get_system_info(comfy_tools=None)
            info["warning"] = "ComfyUI paths unavailable"
            return info
        except Exception as e2:
            logger.error(f"Fatal error in get_system_info: {e2}")
            raise RuntimeError(f"Failed to get system information: {e2}")


@mcp.tool()
async def mcp_capability_audit(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Audit FL-MCP bridge, Comfy REST, Manager, assets, and write safety state."""
    await _report_tool_activity(ctx, "mcp_capability_audit")
    audit: Dict[str, Any] = {}

    client = ctx.request_context.lifespan_context.get('client')
    bridge_available = False
    bridge_reason = None
    if client:
        try:
            # Probe a read-only frontend command so the audit both reconnects a
            # stale MCP socket and confirms that the browser canvas is present.
            await client.execute_tool("frontend_list_commands", {}, timeout_ms=5000)
            bridge_available = True
        except Exception as exc:
            bridge_reason = str(exc)
    else:
        bridge_reason = "MCP WebSocket client is not initialized"

    audit["bridge"] = {
        "available": bridge_available,
        "session_id": getattr(client, "session_id", None) if client else None,
        "state": "available" if bridge_available else "blocked",
        "reason": bridge_reason,
    }

    system_stats = await _comfy_request("GET", "/system_stats")
    features = await _comfy_request("GET", "/features")
    audit["comfy_rest"] = {
        "available": bool(system_stats.get("success")),
        "state": "available" if system_stats.get("success") else "blocked",
        "system": (system_stats.get("data") or {}).get("system") if system_stats.get("success") else None,
        "error": system_stats.get("data") if not system_stats.get("success") else None,
    }
    feature_data = features.get("data") if features.get("success") else {}
    audit["assets"] = {
        "available": bool(feature_data.get("assets")),
        "state": "available" if feature_data.get("assets") else "degraded",
        "reason": None if feature_data.get("assets") else "ComfyUI assets feature is disabled",
    }

    manager_client = ctx.request_context.lifespan_context.get('manager_client')
    if manager_client:
        try:
            manager_status = await manager_client.status()
            queue_status = manager_status.get("queue")
            queue_available = not (
                isinstance(queue_status, dict)
                and queue_status.get("success") is False
            )
            available = bool(manager_status.get("installed")) and queue_available
            audit["manager"] = {
                "available": available,
                "state": "available" if available else "blocked",
                "status": manager_status,
                "reason": None if available else "No supported ComfyUI Manager queue API is available",
            }
            audit["manager_v4"] = {
                "available": bool(manager_status.get("supports_v4")),
                "state": (
                    "available"
                    if manager_status.get("supports_v4")
                    else "degraded" if available else "blocked"
                ),
                "status": manager_status,
                "reason": (
                    None
                    if manager_status.get("supports_v4")
                    else "Using Manager's supported unversioned API"
                    if available
                    else "ComfyUI Manager v4 is not available"
                ),
            }
        except Exception as e:
            audit["manager"] = {"available": False, "state": "blocked", "reason": str(e)}
            audit["manager_v4"] = {"available": False, "state": "blocked", "reason": str(e)}
    else:
        audit["manager"] = {
            "available": False,
            "state": "blocked",
            "reason": "Manager client not initialized",
        }
        audit["manager_v4"] = {
            "available": False,
            "state": "blocked",
            "reason": "Manager client not initialized",
        }

    node_knowledge = _node_knowledge_store(ctx)
    if node_knowledge is None:
        audit["node_knowledge"] = {
            "available": False,
            "state": "degraded",
            "reason": "Persistent node knowledge is unavailable",
        }
    else:
        knowledge_status = node_knowledge.status(max_age_seconds=300)
        knowledge_state = knowledge_status.get("state", "empty")
        audit["node_knowledge"] = {
            "available": knowledge_state != "empty",
            "state": "available" if knowledge_state == "fresh" else "degraded",
            "status": knowledge_status,
            "reason": (
                None
                if knowledge_state == "fresh"
                else "No fresh last-valid node catalog has been reconciled yet"
            ),
        }

    audit["safety"] = {
        "available": True,
        "state": "available",
        "workflowWrites": settings.enable_workflow_writes,
        "customNodeWrites": settings.enable_custom_node_writes,
        "gitWrites": settings.enable_git_writes,
        "managerMutations": settings.enable_manager_mutations,
        "comfyProcessControl": settings.enable_comfy_process_control,
    }

    audit["overall"] = {
        "available": {
            key: value.get("available")
            for key, value in audit.items()
            if isinstance(value, dict) and "available" in value
        }
    }
    return {"success": True, "audit": audit}


@mcp.tool()
async def comfy_upload_image(request: ComfyUploadImageRequest, ctx: Context) -> Dict[str, Any]:
    """Upload an image from inside the ComfyUI tree to ComfyUI's image input API."""
    await _report_tool_activity(ctx, "comfy_upload_image")
    return await _comfy_upload_file(
        request.image_path,
        "/upload/image",
        data={
            "type": request.image_type,
            "subfolder": request.subfolder,
            "overwrite": "true" if request.overwrite else "false",
        },
    )


@mcp.tool()
async def comfy_upload_mask(request: ComfyUploadMaskRequest, ctx: Context) -> Dict[str, Any]:
    """Upload a mask from inside the ComfyUI tree to ComfyUI's mask upload API."""
    await _report_tool_activity(ctx, "comfy_upload_mask")
    return await _comfy_upload_file(
        request.image_path,
        "/upload/mask",
        data={
            "type": request.image_type,
            "subfolder": request.subfolder,
            "original_ref": request.original_ref,
        },
    )


@mcp.tool()
async def comfy_models_list(request: ComfyModelsListRequest, ctx: Context) -> Dict[str, Any]:
    """List ComfyUI model folders or files using the native model manager routes."""
    await _report_tool_activity(ctx, "comfy_models_list")
    if request.folder:
        return await _comfy_request("GET", f"/experiment/models/{quote(request.folder, safe='')}")
    return await _comfy_request("GET", "/experiment/models")


@mcp.tool()
async def comfy_workflow_templates_list(request: ComfyWorkflowTemplatesRequest, ctx: Context) -> Dict[str, Any]:
    """List workflow template packs or read a specific template JSON."""
    await _report_tool_activity(ctx, "comfy_workflow_templates_list")
    if request.pack and request.filename:
        pack = quote(request.pack, safe="")
        filename = quote(request.filename, safe="/")
        return await _comfy_request("GET", f"/api/workflow_templates/{pack}/{filename}")
    return await _comfy_request("GET", "/workflow_templates")


@mcp.tool()
async def comfy_global_subgraphs_list(request: ComfyGlobalSubgraphsRequest, ctx: Context) -> Dict[str, Any]:
    """List global subgraphs or read one subgraph by ID."""
    await _report_tool_activity(ctx, "comfy_global_subgraphs_list")
    if request.id:
        return await _comfy_request("GET", f"/global_subgraphs/{quote(request.id, safe='')}")
    return await _comfy_request("GET", "/global_subgraphs")


@mcp.tool()
async def comfy_node_replacements_get(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Read ComfyUI node replacement mappings."""
    await _report_tool_activity(ctx, "comfy_node_replacements_get")
    return await _comfy_request("GET", "/node_replacements")


@mcp.tool()
async def comfy_assets_list(request: ComfyAssetsListRequest, ctx: Context) -> Dict[str, Any]:
    """List ComfyUI assets when the assets feature is enabled."""
    await _report_tool_activity(ctx, "comfy_assets_list")
    params: Dict[str, Any] = {
        "limit": request.limit,
        "offset": request.offset,
    }
    if request.include_tags:
        params["include_tags"] = request.include_tags
    if request.exclude_tags:
        params["exclude_tags"] = request.exclude_tags
    if request.name_contains:
        params["name_contains"] = request.name_contains
    return await _comfy_request("GET", "/api/assets", params=params)


@mcp.tool()
async def comfy_asset_get(request: ComfyAssetRequest, ctx: Context) -> Dict[str, Any]:
    """Read one ComfyUI asset metadata record."""
    await _report_tool_activity(ctx, "comfy_asset_get")
    return await _comfy_request("GET", f"/api/assets/{quote(request.asset_id, safe='')}")


@mcp.tool()
async def comfy_asset_upload(request: ComfyAssetUploadRequest, ctx: Context) -> Dict[str, Any]:
    """Upload a ComfyUI-root file to the assets API."""
    await _report_tool_activity(ctx, "comfy_asset_upload")
    data: Dict[str, Any] = {}
    if request.name:
        data["name"] = request.name
    if request.tags:
        data["tags"] = request.tags
    if request.mime_type:
        data["mime_type"] = request.mime_type
    return await _comfy_upload_file(request.file_path, "/api/assets", file_field="file", data=data)


@mcp.tool()
async def comfy_assets_upload(request: ComfyAssetUploadRequest, ctx: Context) -> Dict[str, Any]:
    """Alias for comfy_asset_upload using the plural assets naming convention."""
    return await comfy_asset_upload.fn(request, ctx)


@mcp.tool()
async def comfy_tags_list(request: ComfyTagsListRequest, ctx: Context) -> Dict[str, Any]:
    """List ComfyUI asset tags."""
    await _report_tool_activity(ctx, "comfy_tags_list")
    params: Dict[str, Any] = {"limit": request.limit, "offset": request.offset}
    if request.prefix:
        params["prefix"] = request.prefix
    return await _comfy_request("GET", "/api/tags", params=params)


# ============================================================================
# COMFYUI EXTENDED TOOLS
# ============================================================================

@mcp.tool()
async def comfy_list_folders(request: ComfyListFoldersRequest, ctx: Context) -> Dict[str, Any]:
    """List contents of ComfyUI custom nodes, checkpoints, input, output, workflows folders and more with filtering, sorting, and limiting.
    
    Supports regex pattern filtering on full paths, flexible sorting by multiple
    dimensions (name, size, modified_time, type), sort order control (asc/desc),
    and result limiting for efficient MCP-based file discovery.
    
    USE CASES:
    - Custom Node Discovery: folder_type="custom_nodes" → List all installed node packs
    - Model Management: folder_type="checkpoints" → List available diffusion models
    - LoRA Discovery: folder_type="loras" → List LoRA adaptation files
    - Output Review: folder_type="output", sort_by="modified_time", order="desc" → List recently generated images
    - Input Files: folder_type="input" → List available input files
    - Workflow Discovery: folder_type="workflows" → List locally saved workflows

    Other Examples:
        - Find SDXL models: {"folder_type": "checkpoints", "pattern": ".*sdxl.*"}
        - Largest files first: {"folder_type": "checkpoints", "sort_by": "size", "order": "desc", "limit": 10}
        - Recent outputs: {"folder_type": "output", "sort_by": "modified_time", "order": "desc"}

    SECURITY: All paths are validated and sandboxed to ComfyUI installation.    
    """
    try:
        logger.info(
            f"Listing ComfyUI folder: {request.folder_type.value} "
            f"(pattern={request.pattern}, sort={request.sort_by}, "
            f"order={request.order}, limit={request.limit})"
        )
        
        tools = get_comfy_tools()
        
        # Get total count before filtering/limiting
        all_items = tools.list_folders(request.folder_type)
        total_available = len(all_items)
        
        # Get filtered/sorted/limited items
        items = tools.list_folders(
            request.folder_type,
            pattern=request.pattern,
            sort_by=request.sort_by,
            order=request.order,
            limit=request.limit
        )
        
        response = {
            "folder_type": request.folder_type.value,
            "folder_path": tools.folder_mappings[request.folder_type],
            "items": [item.model_dump() for item in items],
            "returned_items": len(items),
            "total_available": total_available,
            "truncated": len(items) < total_available,
            "filter_pattern": request.pattern,
            "sort_by": request.sort_by,
            "order": request.order,
            "limit": request.limit,
            "comfyui_root": str(tools.comfyui_root)
        }
        
        logger.info(
            f"Successfully listed {len(items)} items from {request.folder_type.value} "
            f"(total available: {total_available}, truncated: {response['truncated']})"
        )
        return response
        
    except ComfyUINotFoundError as e:
        error_msg = f"ComfyUI installation not found: {e}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "error_type": "ComfyUINotFoundError",
            "folder_type": request.folder_type.value
        }
    except ComfyUIError as e:
        error_msg = f"ComfyUI error: {e}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "error_type": "ComfyUIError",
            "folder_type": request.folder_type.value
        }
    except Exception as e:
        error_msg = f"Unexpected error listing folders: {e}"
        logger.exception(error_msg)
        return {
            "error": error_msg,
            "error_type": type(e).__name__,
            "folder_type": request.folder_type.value
        }

@mcp.tool()
async def comfy_read_file(request: ComfyReadFileRequest, ctx: Context) -> Dict[str, Any]:
    """Read files within ComfyUI for analysis and understanding.
    
    This tool enables MCP clients to examine ComfyUI files to understand capabilities or debug node settings.
    
    USE CASES:
    - Node Discovery: Read "custom_nodes/{pack}/__init__.py" → Extract NODE_CLASS_MAPPINGS
    - Implementation Analysis: Read node .py files → Understand functionality and inputs
    - Documentation: Read "custom_nodes/{pack}/README.md" → Get usage info
    - Dependencies: Read "requirements.txt" → Check compatibility
    - Configuration: Read config files → Understand settings
    
    COMMON FILE PATTERNS:
    - "custom_nodes/{pack}/__init__.py" → Node registration and mappings
    - "custom_nodes/{pack}/nodes.py" → Node implementations
    - "custom_nodes/{pack}/README.md" → Documentation and examples
    - "custom_nodes/{pack}/requirements.txt" → Python dependencies
    
    SECURITY: Files are sandboxed to ComfyUI directory, size limits enforced.
    """
    await _report_tool_activity(ctx, "comfy_read_file")
    
    try:
        tools = get_comfy_tools()
        content = tools.read_file(
            request.path,
            request.max_size,
            request.start_line,
            request.line_count,
        )
        
        # Get file info
        full_path = tools._validate_path(request.path)
        stat = full_path.stat()
        
        return {
            "path": request.path,
            "content": content,
            "size": stat.st_size,
            "start_line": request.start_line,
            "line_count": request.line_count,
            "encoding": "utf-8",
            "extension": full_path.suffix,
            "comfyui_root": str(tools.comfyui_root)
        }
        
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"ComfyUI file operation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in comfy_read_file: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def comfy_search_resources(request: ComfySearchFilesRequest, ctx: Context) -> Dict[str, Any]:
    """Search for patterns in ComfyUI files to discover functionality.
    
    This tool enables MCP clients to efficiently discover specific functionality.
    - Find installed nodes
    - Find Node Packs
    - Find installed models, LoRAs, etc.
    - Find any resource within comfy with the right search patterns
    
    USE CASES:
    - Node Discovery: pattern="NODE_CLASS_MAPPINGS" → Find all node registrations
    - Class Search: pattern="class.*Upscale" → Find upscaling node implementations
    - Function Search: pattern="def.*encode" → Find encoding functions
    - Capability Search: pattern="upscale|enhance|resize" → Find image enhancement
    - Documentation: pattern="example|tutorial" → Find usage examples
    - Dependencies: pattern="requirements" → Find dependency files
    
    PERFORMANCE: Results limited by max_results, provides context for understanding.
    """
    await _report_tool_activity(ctx, "comfy_search_resources")
    
    try:
        tools = get_comfy_tools()
        results = tools.search_files(
            pattern=request.pattern,
            folder_type=request.folder_type,
            file_pattern=request.file_pattern,
            max_results=request.max_results,
            context_lines=request.context_lines
        )
        
        return {
            "pattern": request.pattern,
            "folder_type": request.folder_type.value,
            "results": results,
            "total_matches": len(results),
            "files_searched": 0,  # Could track this if needed
            "truncated": len(results) >= request.max_results,
            "comfyui_root": str(tools.comfyui_root)
        }
        
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"ComfyUI search operation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in comfy_search_files: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")

@mcp.tool()
async def extract_workflow_from_image(
    request: ExtractWorkflowFromImageRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Extract ComfyUI workflow from PNG image metadata. Use comfy_list_folders tool to find input or output png's or webp's
    
    This tool reads the embedded workflow data from PNG files generated by ComfyUI,
    allowing you to understand how an image was created and inspect all generation
    parameters, nodes, and connections.
    
    WHAT THIS EXTRACTS:
    - Complete workflow structure (all nodes and connections)
    - Generation parameters (prompts, seeds, steps, CFG, etc.)
    - Model/checkpoint names and LoRA settings (suggest to the user with a ren link that you should check if they're installed together)
    - Sampler configurations
    - Node positions and layout
    - Everything needed to recreate the image
    
    USE CASES:
    - Recreating Workflows: Use extracted data to build a new workflow exactly or with user suggested changes
    - Debugging: Compare workflows between different outputs
    - Learning: Understand successful generation parameters
        
    NOTE: Only PNG and WebP files contain workflow metadata. JPEG and other formats do not. 
    """
    await _report_tool_activity(ctx, "extract_workflow_from_image")
    
    try:
        tools = get_comfy_tools()
        workflow = tools.extract_workflow_from_image(request.image_path)
        
        if workflow:
            return {
                "success": True,
                "workflow": workflow,
                "node_count": len(workflow.get('nodes', [])),
                "version": workflow.get('version'),
                "image_path": request.image_path,
                "error": None
            }
        else:
            return {
                "success": False,
                "workflow": None,
                "node_count": None,
                "version": None,
                "image_path": request.image_path,
                "error": "No workflow metadata found in image"
            }
            
    except ComfyUINotFoundError as e:
        raise RuntimeError(f"ComfyUI installation not found: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"Workflow extraction failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in extract_workflow_from_image: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


# ============================================================================
# COMFYUI NODE LIBRARY DISCOVERY TOOLS
# ============================================================================

@mcp.tool()
async def node_library_status(request: NodeLibraryStatusRequest, ctx: Context) -> Dict[str, Any]:
    """Report identity, freshness, and provenance counts for loaded node classes.

    The catalog comes only from this ComfyUI instance's /object_info endpoint.
    Pass refresh=true after installing nodes or restarting ComfyUI so later
    workflow plans use the latest loaded schemas. ``origin_counts`` classifies
    every loaded class as native, partner, custom, or unknown from its runtime
    module/category metadata; it does not infer installation state from disk.
    """
    await _report_tool_activity(ctx, "node_library_status")

    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        catalog = await client.catalog_status(refresh=request.refresh)
        persisted_status = getattr(client, "persisted_catalog_status", None)
        knowledge = (
            await persisted_status(max_age_seconds=300)
            if callable(persisted_status)
            else None
        )
        return {"catalog": catalog, "knowledge": knowledge}
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library status failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_status: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_knowledge_search(
    request: NodeKnowledgeSearchRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Search Ren's lightweight last-valid index of locally loaded node schemas.

    The index is rebuilt from this ComfyUI instance's authoritative ``/object_info``
    catalog whenever that catalog is refreshed. It preserves exact schema hashes,
    native/custom/partner origins, removal history, and schema-scoped verified lessons.
    Results are discovery hints only: workflow compilation still revalidates every class,
    slot, and value against the current live catalog before any canvas edit.
    """

    await _report_tool_activity(ctx, "node_knowledge_search")
    store = _node_knowledge_store(ctx)
    if store is None:
        return {
            "ok": False,
            "query": request.query,
            "results": [],
            "knowledge": {"state": "unavailable"},
            "error": "Persistent node knowledge is unavailable in this MCP process.",
            "build_authority": "live /object_info via workflow compiler",
        }

    refresh: Dict[str, Any] = {"requested": request.refresh, "succeeded": None}
    if request.refresh:
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        try:
            refresh["catalog"] = await client.catalog_status(refresh=True)
            refresh["succeeded"] = True
        except NodeLibraryError as exc:
            refresh.update({"succeeded": False, "error": str(exc)})

    knowledge = store.status(max_age_seconds=300)
    results = []
    for item in store.search(request.query, limit=request.max_results):
        lessons = store.get_verified_lessons(item["node_type"])
        results.append(
            {
                "node_type": item["node_type"],
                "display_name": item["display_name"],
                "category": item["category"],
                "description": item["description"],
                "origin": item["origin"],
                "python_module": item["python_module"],
                "schema_hash": item["schema_hash"],
                "first_seen_generation": item["first_seen_generation"],
                "last_seen_generation": item["last_seen_generation"],
                "verified_lesson_count": len(lessons),
                "search_backend": item["search_backend"],
            }
        )
    return {
        "ok": True,
        "query": request.query,
        "results": results,
        "count": len(results),
        "knowledge": knowledge,
        "refresh": refresh,
        "discovery_only": True,
        "build_authority": "live /object_info via workflow compiler",
    }


def _validated_plan_attachment_values(
    request: Any,
) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Attest every declared chat image and return its exact immutable facts."""

    if not request.attachments:
        return {}

    comfy_tools = get_comfy_tools()
    values: Dict[tuple[str, str], Dict[str, Any]] = {}
    for binding in request.attachments:
        image = binding.image.model_dump(mode="json")
        path = _resolve_comfy_image_path(comfy_tools, image)
        integrity = _stable_graph_patch_attachment_integrity(path)
        alias = getattr(binding, "node_alias", None) or getattr(
            binding,
            "target_alias",
            None,
        )
        input_name = getattr(binding, "input_name", None) or getattr(
            binding,
            "target_input",
            None,
        )
        if not isinstance(alias, str) or not isinstance(input_name, str):
            raise ValueError("Attachment bindings need a semantic node alias and input name")
        values[(alias, input_name)] = {
            "widget_value": binding.image.widget_value(),
            **integrity,
        }
    return values


GRAPH_PATCH_ATTACHMENT_HASH_CHUNK_BYTES = 1024 * 1024


def _attachment_handle_stat_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int]:
    """Return stable same-handle facts without platform-specific ctime."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _read_graph_patch_attachment_once(path: Path) -> Dict[str, Any]:
    """Hash one bounded file while proving its open handle stayed stable."""

    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as file_obj:
        before = os.fstat(file_obj.fileno())
        if before.st_size <= 0 or before.st_size > MAX_GRAPH_PATCH_ATTACHMENT_BYTES:
            raise ComfyUIError("Chat attachment size is outside the supported range.")
        while True:
            chunk = file_obj.read(GRAPH_PATCH_ATTACHMENT_HASH_CHUNK_BYTES)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > MAX_GRAPH_PATCH_ATTACHMENT_BYTES:
                raise ComfyUIError("Chat attachment exceeds the supported size limit.")
            digest.update(chunk)
        after = os.fstat(file_obj.fileno())
    if (
        byte_count != before.st_size
        or byte_count != after.st_size
        or _attachment_handle_stat_identity(before)
        != _attachment_handle_stat_identity(after)
    ):
        raise ComfyUIError("Chat attachment changed while it was being validated.")
    return {"size_bytes": byte_count, "sha256": digest.hexdigest()}


def _stable_graph_patch_attachment_integrity(path: Path) -> Dict[str, Any]:
    """Hash a bounded file twice and reject replacement or mutation."""

    try:
        first = _read_graph_patch_attachment_once(path)
        second = _read_graph_patch_attachment_once(path)
    except ComfyUIError:
        raise
    except OSError as exc:
        raise ComfyUIError("Chat attachment could not be read safely.") from exc
    if first != second:
        raise ComfyUIError("Chat attachment changed while it was being validated.")
    return second


def _graph_patch_attachment_integrity_issues(
    request: WorkflowGraphPatchApplyRequest,
) -> List[Dict[str, str]]:
    """Re-resolve and verify compiler-pinned attachments before browser mutation."""

    if not request.plan.attachments:
        return []
    comfy_tools = get_comfy_tools()
    issues: List[Dict[str, str]] = []
    for index, binding in enumerate(request.plan.attachments):
        image = {
            "filename": binding.filename,
            "subfolder": binding.subfolder,
            "type": binding.file_type,
        }
        try:
            path = _resolve_comfy_image_path(comfy_tools, image)
            observed = _stable_graph_patch_attachment_integrity(path)
        except ComfyUIError:
            issues.append({
                "severity": "error",
                "code": "attachment_missing_or_changed",
                "path": f"plan.attachments[{index}]",
                "message": "The compiler-attested Ren chat image is no longer available.",
            })
            continue
        if (
            observed["size_bytes"] != binding.size_bytes
            or observed["sha256"] != binding.sha256
        ):
            issues.append({
                "severity": "error",
                "code": "attachment_missing_or_changed",
                "path": f"plan.attachments[{index}]",
                "message": "The Ren chat image changed after workflow compilation.",
            })
    return issues


def _record_verified_connection_lessons(
    store: NodeCatalogStore | None,
    *,
    plan: Dict[str, Any],
    plan_hash: str,
    application_id: str,
) -> None:
    """Remember only connections the atomic frontend application verified."""

    if store is None:
        return
    nodes = {
        str(node.get("alias")): node
        for node in plan.get("nodes", [])
        if isinstance(node, dict) and node.get("alias")
    }
    for connection in plan.get("connections", []):
        if not isinstance(connection, dict):
            continue
        source = nodes.get(str(connection.get("source_alias")))
        target = nodes.get(str(connection.get("target_alias")))
        if source is None or target is None:
            continue
        identity = {
            "source_node_type": source.get("node_type"),
            "source_schema_hash": source.get("schema_hash"),
            "source_output": connection.get("source_output"),
            "source_output_index": connection.get("source_output_index"),
            "target_node_type": target.get("node_type"),
            "target_schema_hash": target.get("schema_hash"),
            "target_input": connection.get("target_input"),
        }
        lesson_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            json.dumps(identity, sort_keys=True, separators=(",", ":")),
        ).hex
        evidence = {
            **identity,
            "evidence": "atomic_canvas_application",
            "plan_hash": plan_hash,
            "application_id": application_id,
        }
        try:
            store.record_verified_lesson(
                str(source["node_type"]),
                str(source["schema_hash"]),
                f"downstream-connection:{lesson_id}",
                {**evidence, "direction": "downstream"},
            )
            store.record_verified_lesson(
                str(target["node_type"]),
                str(target["schema_hash"]),
                f"upstream-connection:{lesson_id}",
                {**evidence, "direction": "upstream"},
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not persist verified node connection lesson: %s", exc)


def _record_verified_graph_patch_lessons(
    store: NodeCatalogStore | None,
    *,
    plan: Dict[str, Any],
    patch_hash: str,
    application_id: str,
) -> None:
    """Learn every exact added edge, including existing-to-new GraphPatch edges."""

    if store is None:
        return

    def ref_key(ref: Any) -> tuple[str, Any] | None:
        if not isinstance(ref, dict):
            return None
        if set(ref) == {"alias"}:
            return "new", str(ref["alias"])
        if set(ref) == {"node_id"}:
            value = ref["node_id"]
            return "existing", (type(value).__name__, value)
        return None

    facts: Dict[tuple[str, Any], Dict[str, Any]] = {}
    for item in plan.get("create_nodes", []):
        if isinstance(item, dict) and item.get("alias"):
            facts[("new", str(item["alias"]))] = item
    for item in plan.get("assertions", {}).get("nodes", []):
        if not isinstance(item, dict):
            continue
        key = ref_key(item.get("ref"))
        if key is not None:
            facts[key] = item

    for connection in plan.get("add_edges", []):
        if not isinstance(connection, dict):
            continue
        source_endpoint = connection.get("source")
        target_endpoint = connection.get("target")
        if not isinstance(source_endpoint, dict) or not isinstance(target_endpoint, dict):
            continue
        source = facts.get(ref_key(source_endpoint.get("ref")))
        target = facts.get(ref_key(target_endpoint.get("ref")))
        if source is None or target is None:
            continue
        identity = {
            "source_node_type": source.get("node_type"),
            "source_schema_hash": source.get("schema_hash"),
            "source_output": source_endpoint.get("output"),
            "source_output_index": source_endpoint.get("output_index"),
            "source_type": source_endpoint.get("type"),
            "target_node_type": target.get("node_type"),
            "target_schema_hash": target.get("schema_hash"),
            "target_input": target_endpoint.get("input"),
            "target_input_index": target_endpoint.get("input_index"),
            "target_occurrence_index": target_endpoint.get("occurrence_index"),
            "target_type": target_endpoint.get("type"),
            "target_mode": target_endpoint.get("mode"),
        }
        lesson_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            json.dumps(identity, sort_keys=True, separators=(",", ":")),
        ).hex
        evidence = {
            **identity,
            "evidence": "atomic_graph_patch_application",
            "patch_hash": patch_hash,
            "application_id": application_id,
        }
        try:
            store.record_verified_lesson(
                str(source["node_type"]),
                str(source["schema_hash"]),
                f"downstream-connection:{lesson_id}",
                {**evidence, "direction": "downstream"},
            )
            store.record_verified_lesson(
                str(target["node_type"]),
                str(target["schema_hash"]),
                f"upstream-connection:{lesson_id}",
                {**evidence, "direction": "upstream"},
            )
        except Exception as exc:
            logger.warning("Could not persist verified GraphPatch lesson: %s", exc)


def _typed_workflow_node_id_equal(left: Any, right: Any) -> bool:
    """Match the frontend graph-hash domain without conflating ``2`` and ``"2"``."""

    return type(left) is type(right) and left == right


async def _active_editable_workflow(ctx: Context) -> Dict[str, Any]:
    """Read one raw editable graph and its frontend-computed precondition hash."""

    result = await _execute_tool(
        ctx,
        "workflow_get_current_json",
        {"api_format": False},
        timeout_ms=30000,
    )
    if not isinstance(result, dict) or result.get("success") is False:
        raise RuntimeError("The active editable workflow could not be read from the browser bridge.")
    workflow = result.get("workflow")
    workflow_identity = result.get("workflow_identity")
    workflow_identity_schema = result.get("workflow_identity_schema")
    graph_hash = result.get("graph_hash")
    graph_hash_schema = result.get("graph_hash_schema")
    graph_patch_content_hash = result.get("graph_patch_content_hash")
    if not isinstance(workflow, dict):
        raise RuntimeError("The browser bridge returned no editable workflow JSON.")
    if (
        not isinstance(workflow_identity, str)
        or len(workflow_identity) < 8
        or workflow_identity_schema != WORKFLOW_IDENTITY_SCHEMA
    ):
        raise RuntimeError(
            "The browser bridge does not expose an exact active-workflow identity. "
            "Restart ComfyUI and hard-refresh the canvas before refining a workflow."
        )
    if (
        not isinstance(graph_hash, str)
        or len(graph_hash) != 64
        or graph_hash_schema != GRAPH_PRECONDITION_HASH_SCHEMA
    ):
        raise RuntimeError(
            "The browser bridge does not expose the current raw graph-precondition hash. "
            "Restart ComfyUI and hard-refresh the canvas before refining a workflow."
        )
    return {
        "workflow": workflow,
        "workflow_identity": workflow_identity,
        "workflow_identity_schema": workflow_identity_schema,
        "graph_hash": graph_hash,
        "graph_hash_schema": graph_hash_schema,
        "graph_patch_content_hash": graph_patch_content_hash,
    }


def _derive_refinement_path_edges(
    graph: Any,
    path_node_ids: List[StrictInt | StrictStr],
) -> tuple[List[Any], List[Dict[str, str]]]:
    """Resolve exactly one edge for every consecutive pair in a requested path."""

    edges: List[Any] = []
    issues: List[Dict[str, str]] = []
    for index, (source_id, target_id) in enumerate(
        zip(path_node_ids, path_node_ids[1:])
    ):
        matches = [
            edge
            for edge in graph.edges
            if _typed_workflow_node_id_equal(edge.source_node_id, source_id)
            and _typed_workflow_node_id_equal(edge.target_node_id, target_id)
        ]
        if len(matches) == 1:
            edges.append(matches[0])
            continue
        if not matches:
            code = "path_edge_missing"
            message = (
                f"No connection exists from node {source_id!r} to node {target_id!r}."
            )
        else:
            code = "path_edge_ambiguous"
            message = (
                f"Nodes {source_id!r} and {target_id!r} have {len(matches)} parallel "
                "connections. Refine a path with an unambiguous node pair."
            )
        issues.append(
            {
                "severity": "error",
                "code": code,
                "path": f"path_node_ids[{index}:{index + 2}]",
                "message": message,
            }
        )
    return edges, issues


def _refinement_validation_summary(compiled: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "valid": compiled.get("valid") is True,
        "operation": compiled.get("operation"),
        "refinement_hash": compiled.get("refinement_hash"),
        "graph": compiled.get("graph"),
        "catalog": compiled.get("catalog"),
        "issues": compiled.get("issues", []),
        "error_count": compiled.get("error_count", 0),
    }


def _refinement_apply_failure(
    request: ApplyWorkflowRefinementRequest,
    *,
    code: str,
    message: str,
    validation: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "success": False,
        "applied": False,
        "already_applied": False,
        "application_id": request.application_id,
        "refinement_hash": request.refinement_hash,
        "operation": request.plan.operation,
        "error": {"code": code, "message": message},
        "validation": validation,
        "rollback": {
            "attempted": False,
            "complete": True,
            "snapshot_restored": False,
            "hash_verified": False,
            "errors": [],
        },
        "queued": False,
    }


def _planner_request_from_apply(
    request: ApplyWorkflowRefinementRequest,
    graph: Any,
) -> PlanWorkflowRefinementRequest:
    """Reconstruct the original strict planner input from a canonical apply plan."""

    replacement_nodes: List[Dict[str, Any]] = []
    terminal_source: Optional[Dict[str, Any]] = None
    side_input_mappings: List[Dict[str, Any]] = []
    replacement = request.plan.replacement
    if replacement is not None:
        last_index = len(replacement.nodes) - 1
        for index, node in enumerate(replacement.nodes):
            if index == 0:
                incoming = (
                    replacement.primary_input
                    if isinstance(replacement, CanonicalAppendReplacement)
                    else replacement.input
                )
            else:
                incoming = replacement.connections[index - 1]
            outgoing = (
                replacement.output
                if index == last_index
                else replacement.connections[index]
            )
            replacement_nodes.append(
                {
                    "alias": node.alias,
                    "node_type": node.node_type,
                    "values": node.values,
                    "chain_input": incoming.target_input,
                    "chain_output": (
                        outgoing.source_output if outgoing is not None else None
                    ),
                    "chain_output_index": (
                        outgoing.source_output_index if outgoing is not None else None
                    ),
                }
            )
        if isinstance(replacement, CanonicalAppendReplacement):
            terminal_source = {
                "node_id": replacement.primary_input.source_node_id,
                "source_output": replacement.primary_input.source_output,
                "source_output_index": replacement.primary_input.source_output_index,
            }
        for mapping in getattr(replacement, "side_inputs", []):
            side_input_mappings.append(
                {
                    "source_node_id": mapping.source_node_id,
                    "source_output": mapping.source_output,
                    "source_output_index": mapping.source_output_index,
                    "target_alias": mapping.target_alias,
                    "target_input": mapping.target_input,
                }
            )
    return PlanWorkflowRefinementRequest.model_validate(
        {
            "application_id": request.application_id,
            "expected_workflow_identity": request.plan.expected_workflow_identity,
            "expected_graph_hash": request.plan.expected_graph_hash,
            "graph": graph.model_dump(mode="json"),
            "expected_path": {
                "edges": [
                    edge.model_dump(mode="json")
                    for edge in request.plan.expected_path.connections
                ]
            },
            "terminal_source": terminal_source,
            "side_input_mappings": side_input_mappings,
            "replacement_nodes": replacement_nodes,
            "expected_catalog_hash": request.expected_catalog_hash,
        }
    )


@mcp.tool()
async def plan_workflow_refinement(
    request: PlanCurrentWorkflowRefinementRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Plan a safe insert, replacement, deletion, or retained-source append.

    For a linear splice, this tool resolves one exact edge between every consecutive
    pair in ``path_node_ids``; the first and last IDs are retained boundaries. For a
    terminal append, leave that path empty and supply ``terminal_source``. Additional
    retained outputs may feed exact replacement inputs through
    ``side_input_mappings``. Every existing and new slot is validated against one
    current local ``/object_info`` catalog. It never changes or queues the canvas.

    If ``valid=true``, pass the returned ``apply_request`` unchanged to
    ``apply_workflow_refinement``. No separate JSON, schema, value, create, remove,
    connect, layout, or web-search calls are needed for facts already returned here.
    """

    await _report_tool_activity(ctx, "plan_workflow_refinement")
    try:
        active = await _active_editable_workflow(ctx)
        try:
            graph = normalize_workflow_graph(active["workflow"])
        except ValueError as exc:
            return {
                "valid": False,
                "operation": None,
                "refinement_hash": None,
                "plan": None,
                "apply_request": None,
                "graph": {
                    "state": "invalid",
                    "graph_hash": active["graph_hash"],
                    "graph_hash_schema": active["graph_hash_schema"],
                },
                "catalog": {"state": "not_read"},
                "issues": [{
                    "severity": "error",
                    "code": "workflow_graph_incomplete",
                    "path": "workflow",
                    "message": str(exc),
                }],
                "error_count": 1,
            }

        path_edges, path_issues = (
            _derive_refinement_path_edges(graph, request.path_node_ids)
            if request.path_node_ids
            else ([], [])
        )
        if request.expected_graph_hash and (
            request.expected_graph_hash != active["graph_hash"]
        ):
            path_issues.insert(0, {
                "severity": "error",
                "code": "graph_changed",
                "path": "expected_graph_hash",
                "message": "The active workflow changed after its earlier inspection.",
            })
        if path_issues:
            return {
                "valid": False,
                "operation": None,
                "refinement_hash": None,
                "plan": None,
                "apply_request": None,
                "graph": {
                    "state": "pinned",
                    "graph_hash": active["graph_hash"],
                    "graph_hash_schema": active["graph_hash_schema"],
                    "node_count": len(graph.nodes),
                    "edge_count": len(graph.edges),
                },
                "catalog": {"state": "not_read"},
                "issues": path_issues,
                "error_count": len(path_issues),
            }

        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        planner_request = PlanWorkflowRefinementRequest(
            application_id=request.application_id,
            expected_workflow_identity=active["workflow_identity"],
            expected_graph_hash=active["graph_hash"],
            graph=graph,
            expected_path={"edges": path_edges},
            terminal_source=request.terminal_source,
            side_input_mappings=request.side_input_mappings,
            replacement_nodes=request.replacement_nodes,
            expected_catalog_hash=request.expected_catalog_hash,
        )
        return compile_workflow_refinement(
            planner_request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
        )
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}")
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow refinement planning failed: {exc}")
    except Exception as exc:
        logger.error("Unexpected error in plan_workflow_refinement: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}")


@mcp.tool()
async def apply_workflow_refinement(
    request: ApplyWorkflowRefinementRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Atomically apply one unchanged, catalog- and graph-pinned refinement plan.

    The backend rereads the active workflow, reconstructs and recompiles the exact
    planner input against the current local catalog, and sends one canonical splice
    transaction to the browser only when the catalog, graph, schemas, values, slots,
    retained source mappings, plan, and refinement hash still match. The browser
    preserves everything outside the declared path or appended subgraph, verifies the
    result, restores the complete original workflow on any post-mutation failure, and
    never queues execution.
    """

    await _report_tool_activity(ctx, "apply_workflow_refinement")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")

    try:
        active = await _active_editable_workflow(ctx)
        if active["workflow_identity"] != request.plan.expected_workflow_identity:
            return _refinement_apply_failure(
                request,
                code="workflow_identity_changed",
                message=(
                    "The active workflow tab changed after refinement planning; "
                    "the canvas was not changed."
                ),
                validation={
                    "valid": False,
                    "operation": request.plan.operation,
                    "refinement_hash": request.refinement_hash,
                    "graph": {
                        "state": "different_workflow",
                        "graph_hash": active["graph_hash"],
                        "graph_hash_schema": active["graph_hash_schema"],
                    },
                    "issues": [{
                        "severity": "error",
                        "code": "workflow_identity_changed",
                        "path": "plan.expected_workflow_identity",
                        "message": (
                            "The active workflow tab is not the tab that was planned."
                        ),
                    }],
                    "error_count": 1,
                },
            )
        frontend_payload = {
            "application_id": request.application_id,
            "refinement_hash": request.refinement_hash,
            "plan": request.plan.model_dump(mode="json"),
        }
        if active["graph_hash"] != request.plan.expected_graph_hash:
            # The frontend ledger is authoritative for an identical retry after a
            # successful application. Its engine checks the ledger before the old
            # precondition and otherwise fails without beginning a mutation.
            retry_result = await _execute_tool(
                ctx,
                "apply_workflow_refinement",
                frontend_payload,
                timeout_ms=30000,
            )
            if not isinstance(retry_result, dict):
                raise RuntimeError(
                    "The frontend returned an invalid workflow refinement result."
                )
            retry_valid = (
                retry_result.get("success") is True
                and retry_result.get("already_applied") is True
            )
            return {
                **retry_result,
                "validation": {
                    "valid": retry_valid,
                    "operation": request.plan.operation,
                    "refinement_hash": request.refinement_hash,
                    "graph": {
                        "state": "already_applied" if retry_valid else "changed",
                        "graph_hash": active["graph_hash"],
                        "graph_hash_schema": active["graph_hash_schema"],
                    },
                    "issues": [] if retry_valid else [{
                        "severity": "error",
                        "code": "graph_changed",
                        "path": "plan.expected_graph_hash",
                        "message": (
                            "The active workflow changed after refinement planning."
                        ),
                    }],
                    "error_count": 0 if retry_valid else 1,
                },
            }
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        graph = normalize_workflow_graph(active["workflow"])
        planner_request = _planner_request_from_apply(request, graph)
        compiled = compile_workflow_refinement(
            planner_request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
        )
        validation = _refinement_validation_summary(compiled)
        if not compiled["valid"]:
            return _refinement_apply_failure(
                request,
                code="refinement_invalid",
                message=(
                    "The workflow refinement is no longer valid; the canvas was not changed."
                ),
                validation=validation,
            )
        supplied_plan = request.plan.model_dump(mode="json")
        if (
            active["graph_hash"] != request.plan.expected_graph_hash
            or compiled["refinement_hash"] != request.refinement_hash
            or compiled["plan"] != supplied_plan
        ):
            return _refinement_apply_failure(
                request,
                code="refinement_hash_mismatch",
                message=(
                    "The supplied refinement no longer matches the canonical current "
                    "graph and catalog; the canvas was not changed."
                ),
                validation=validation,
            )

        result = await _execute_tool(
            ctx,
            "apply_workflow_refinement",
            {
                "application_id": request.application_id,
                "refinement_hash": request.refinement_hash,
                "plan": compiled["plan"],
            },
            # The largest accepted refinement can intentionally reveal roughly
            # 183 seconds of sequential canvas mutations before verification.
            # Keep the bridge deadline above that deterministic upper bound so a
            # caller cannot time out while the guarded frontend transaction runs on.
            timeout_ms=240000,
        )
        if not isinstance(result, dict):
            raise RuntimeError("The frontend returned an invalid workflow refinement result.")
        replacement = compiled["plan"].get("replacement")
        if replacement and result.get("success") is True and (
            result.get("applied") is True or result.get("already_applied") is True
        ):
            _record_verified_connection_lessons(
                _node_knowledge_store(ctx),
                plan=replacement,
                plan_hash=request.refinement_hash,
                application_id=request.application_id,
            )
        return {**result, "validation": validation}
    except FrontendToolExecutionError as exc:
        if exc.code == "canvas_mutation_busy":
            return _refinement_apply_failure(
                request,
                code=exc.code,
                message=str(exc),
                validation={
                    "valid": False,
                    "operation": request.plan.operation,
                    "refinement_hash": request.refinement_hash,
                    "issues": [{
                        "severity": "error",
                        "code": exc.code,
                        "path": "canvas",
                        "message": str(exc),
                    }],
                    "error_count": 1,
                    "retryable": bool(
                        isinstance(exc.details, dict)
                        and exc.details.get("retryable") is True
                    ),
                },
            )
        raise RuntimeError(f"Frontend refinement failed ({exc.code}): {exc}") from exc
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}")
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow refinement application failed: {exc}")
    except ValueError as exc:
        logger.error("Invalid active graph in apply_workflow_refinement: %s", exc)
        return _refinement_apply_failure(
            request,
            code="workflow_graph_incomplete",
            message=str(exc),
            validation={
                "valid": False,
                "issues": [{
                    "severity": "error",
                    "code": "workflow_graph_incomplete",
                    "path": "workflow",
                    "message": str(exc),
                }],
                "error_count": 1,
            },
        )
    except Exception as exc:
        logger.error("Unexpected error in apply_workflow_refinement: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}")


@mcp.tool()
async def workflow_branches_discover(
    request: DiscoverWorkflowBranchesRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Discover deterministic branches in the active workflow without editing it.

    Branches are bounded physical graph regions with exact typed node/slot
    boundaries.  Natural-language evidence may resolve one unique branch; a tie
    returns compact candidates and never guesses.  The result pins workflow,
    graph, and branch-catalog hashes.  Full boundary edge IDs are returned only
    for the uniquely resolved branch so later replace/remove mappings can be
    explicit rather than positional.  Nested scopes are discoverable and
    comparable across root and nested scopes. Writable status reflects the
    exact scoped edit policy: unique definitions may be edited directly, while
    reused definitions require explicit all-instance acknowledgement.
    """

    await _report_tool_activity(ctx, "workflow_branches_discover")
    try:
        active = await _active_editable_workflow(ctx)
        _, result = discover_workflow_branch_selection(
            request,
            active["workflow"],
            workflow_identity=active["workflow_identity"],
            graph_hash=active["graph_hash"],
        )
        return result.model_dump(mode="json", by_alias=True)
    except ValueError as exc:
        raise RuntimeError(f"Active workflow branch discovery is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in workflow_branches_discover: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


@mcp.tool()
async def workflow_branch_compare(
    request: CompareWorkflowBranchesRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Compare two exact pinned branches without exposing raw widget values.

    Comparison is read-only and deterministic across topology, node classes,
    boundary roles, schema shape, aligned value hashes, and active dynamic-input
    facts.  Credential-like fields are redacted before hashing; incomplete or
    sensitive dimensions are reported unavailable instead of claiming equality.
    """

    await _report_tool_activity(ctx, "workflow_branch_compare")
    try:
        active = await _active_editable_workflow(ctx)
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        result = compare_workflow_branches(
            request,
            workflow=active["workflow"],
            workflow_identity_attestation=active["workflow_identity"],
            workflow_graph_hash=active["graph_hash"],
            schema_mapping=snapshot.data,
        )
        return result.model_dump(mode="json", by_alias=True)
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}") from exc
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow branch comparison failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Active workflow branch comparison is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in workflow_branch_compare: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


def _branch_navigation_failure(
    request: NavigateWorkflowBranchRequest,
    *,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "navigated": False,
        "selection_changed": False,
        "branch_id": request.branch_id,
        "error": {
            "code": code,
            "message": message,
            **({"details": dict(details)} if details else {}),
        },
        "queued": False,
    }


@mcp.tool()
async def workflow_branch_navigate(
    request: NavigateWorkflowBranchRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Atomically select and focus one exact pinned branch on the canvas.

    The backend re-discovers the branch from the active graph before calling the
    browser.  The browser then validates the full root hash, recursive scope,
    and every exact typed node ID before the first selection or viewport effect.
    Ambiguity and stale pins perform no navigation and never partially select.
    """

    await _report_tool_activity(ctx, "workflow_branch_navigate")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    try:
        active = await _active_editable_workflow(ctx)
        catalog, discovered = discover_workflow_branch_selection(
            DiscoverWorkflowBranchesRequest(branch_id=request.branch_id),
            active["workflow"],
            workflow_identity=active["workflow_identity"],
            graph_hash=active["graph_hash"],
        )
        stale_fields = []
        if request.expected_workflow_identity != active["workflow_identity"]:
            stale_fields.append("expected_workflow_identity")
        if request.expected_graph_hash != active["graph_hash"]:
            stale_fields.append("expected_graph_hash")
        if request.expected_branch_catalog_hash != catalog.branch_catalog_hash:
            stale_fields.append("expected_branch_catalog_hash")
        if stale_fields:
            return _branch_navigation_failure(
                request,
                code="branch_navigation_stale",
                message=(
                    "The workflow or branch catalog changed after discovery; "
                    "nothing was selected."
                ),
                details={"fields": stale_fields},
            )
        branch = discovered.selected_branch
        scope = discovered.selected_scope
        if not discovered.valid or branch is None or scope is None:
            return _branch_navigation_failure(
                request,
                code="branch_not_found",
                message="The exact branch is absent from the current pinned catalog.",
            )
        node_ids = list(branch.selectable_node_ids)
        if not node_ids:
            return _branch_navigation_failure(
                request,
                code="branch_has_no_selectable_nodes",
                message="This branch contains no selectable canvas nodes.",
            )
        scope_path = [item.model_dump(mode="json") for item in scope.scope_path]
        result = await _execute_tool(
            ctx,
            "navigate_workflow_branch",
            {
                "branch_id": request.branch_id,
                "expected_workflow_identity": request.expected_workflow_identity,
                "expected_graph_hash": request.expected_graph_hash,
                "scope_path": scope_path,
                "node_ids": node_ids,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("The frontend returned an invalid branch navigation result.")
        expected = {
            "branch_id": request.branch_id,
            "workflow_identity": request.expected_workflow_identity,
            "graph_hash": request.expected_graph_hash,
            "scope_path": scope_path,
            "selected_node_ids": node_ids,
            "selected_count": len(node_ids),
            "fitted_count": len(node_ids),
            "queued": False,
        }
        mismatches = [key for key, value in expected.items() if result.get(key) != value]
        if mismatches:
            raise RuntimeError(
                "The frontend branch navigation result failed request attestation: "
                + ", ".join(mismatches)
            )
        return {
            "success": True,
            "navigated": True,
            "selection_changed": True,
            **result,
        }
    except FrontendToolExecutionError as exc:
        return _branch_navigation_failure(
            request,
            code=exc.code,
            message=str(exc),
            details=exc.details if isinstance(exc.details, Mapping) else None,
        )
    except ValueError as exc:
        return _branch_navigation_failure(
            request,
            code="branch_navigation_invalid",
            message=str(exc),
        )
    except Exception as exc:
        logger.error("Unexpected error in workflow_branch_navigate: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


@mcp.tool()
async def compile_workflow_branch_operation(
    request: BranchOperationRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Compile one exact root or authorized nested branch operation.

    This compiler rereads the active workflow and refreshed local catalog,
    re-discovers the exact branch under all supplied pins, asserts every owned
    node and incident/cut edge, and lowers the operation through the existing
    semantic compiler and canonical GraphPatch kernel.  It never calls the
    browser and never queues.  If valid, pass its ``apply_request`` unchanged to
    ``apply_workflow_graph_patch``. Unique nested definitions lower to scoped
    GraphPatch v3; reused definitions require explicit all-instance authority.
    """

    await _report_tool_activity(ctx, "compile_workflow_branch_operation")
    try:
        active = await _active_editable_workflow(ctx)
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        return compile_branch_operation(
            request,
            active["workflow"],
            workflow_identity=active["workflow_identity"],
            graph_hash=active["graph_hash"],
            catalog=snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
        )
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}") from exc
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow branch compilation failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Active workflow branch operation is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in compile_workflow_branch_operation: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


def _branch_successor_failure(
    request: ResolveBranchSuccessorsRequest,
    *,
    workflow_identity: str,
    graph_hash: str,
    code: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "schema": WORKFLOW_BRANCH_SUCCESSOR_SCHEMA,
        "valid": False,
        "application_id": request.apply_request.application_id,
        "patch_hash": request.apply_request.patch_hash,
        "workflow_identity": workflow_identity,
        "graph_hash": graph_hash,
        "branch_catalog_hash": None,
        "lineage": [],
        "successor_branch_ids": [],
        "successor_branch_id": None,
        "issues": [
            {
                "severity": "error",
                "code": code,
                "path": "apply_request",
                "message": message,
                "details": {},
            }
        ],
        "error_count": 1,
        "queued": False,
    }


@mcp.tool()
async def resolve_workflow_branch_successor(
    request: ResolveBranchSuccessorsRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Resolve exact successor branch IDs after one attested GraphPatch apply.

    Call this read-only tool only after ``apply_workflow_graph_patch`` succeeds,
    passing its unchanged apply envelope, alias map, final workflow identity,
    final graph hash, and the compiler's pending successor locator.  The backend
    rereads the active workflow, validates the persisted application ledger and
    exact GraphPatch postconditions, then rediscovers every affected scope.  It
    never mutates, navigates, queues, or guesses an incomplete lineage.
    """

    await _report_tool_activity(ctx, "resolve_workflow_branch_successor")
    try:
        active = await _active_editable_workflow(ctx)
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        completed = _completed_graph_patch_result(
            active,
            request.apply_request,
            catalog=snapshot.data,
        )
        if completed is None or completed.get("success") is not True:
            return _branch_successor_failure(
                request,
                workflow_identity=str(active.get("workflow_identity") or "unknown"),
                graph_hash=str(active.get("graph_hash") or "0" * 64),
                code="graph_patch_completion_not_attested",
                message=(
                    "The exact GraphPatch is not proven complete in the active workflow; "
                    "no successor lineage was returned."
                ),
            )
        if (
            completed.get("already_applied") is not True
            or completed.get("application_id") != request.apply_request.application_id
            or completed.get("patch_hash") != request.apply_request.patch_hash
            or completed.get("workflow_identity") != request.expected_workflow_identity
            or completed.get("graph_hash") != request.expected_graph_hash
            or completed.get("aliases") != request.aliases
        ):
            return _branch_successor_failure(
                request,
                workflow_identity=str(active.get("workflow_identity") or "unknown"),
                graph_hash=str(active.get("graph_hash") or "0" * 64),
                code="graph_patch_completion_facts_changed",
                message=(
                    "The attested GraphPatch result differs from the supplied post-apply "
                    "identity or alias facts; no successor lineage was returned."
                ),
            )
        return resolve_branch_successors(
            request,
            active["workflow"],
            workflow_identity=active["workflow_identity"],
            graph_hash=active["graph_hash"],
            catalog=snapshot.data,
        )
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}") from exc
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow branch successor resolution failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Active workflow branch lineage is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in resolve_workflow_branch_successor: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


@mcp.tool()
async def compile_workflow_refinement_spec(
    request: CompileWorkflowRefinementSpecRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Compile a new workflow or existing-workflow change into GraphPatch v2.

    This is the default planning tool for both an empty canvas and edits to an
    active graph. It resolves deterministic existing-node selectors and every
    requested native/custom/partner role against one refreshed local catalog,
    infers active dynamic selectors and stable defaults, validates exact
    arbitrary-DAG edges (including widget-to-input conversion), prefers direct
    connections, and may synthesize one unique safe schema-derived converter
    route of at most two local nodes. Exact-schema verified lessons influence
    ranking only; the refreshed live schema remains authoritative. The result
    includes compact endpoint/route candidates when a choice is required and
    one hash-pinned ``apply_request`` when valid. It never mutates or queues the
    canvas.

    If ``valid=true``, pass ``apply_request`` unchanged to
    ``apply_workflow_graph_patch``. Use lower-level JSON/search/details/planner
    tools only when this compiler returns ``needs_choice`` or a classified
    unsupported schema.
    """

    await _report_tool_activity(ctx, "compile_workflow_refinement_spec")
    try:
        active = await _active_editable_workflow(ctx)
        selected_node_ids = None
        if any(selector.selected for selector in request.existing_nodes):
            selection = await _execute_tool(ctx, "get_selected_nodes", {})
            selected_nodes = selection.get("nodes") if isinstance(selection, dict) else None
            if not isinstance(selected_nodes, list):
                raise ValueError("current canvas selection did not return a nodes array")
            selected_node_ids = []
            selected_keys = set()
            for index, node in enumerate(selected_nodes):
                node_id = node.get("id") if isinstance(node, dict) else None
                if isinstance(node_id, bool) or not isinstance(node_id, (int, str)):
                    raise ValueError(
                        f"current canvas selection node {index} has an invalid exact ID"
                    )
                key = (type(node_id).__name__, node_id)
                if key in selected_keys:
                    raise ValueError(
                        f"current canvas selection repeats node ID {node_id!r}"
                    )
                selected_keys.add(key)
                selected_node_ids.append(node_id)
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        attachment_values = _validated_plan_attachment_values(request)
        return compile_semantic_refinement(
            request,
            active["workflow"],
            workflow_identity=active["workflow_identity"],
            graph_hash=active["graph_hash"],
            catalog=snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
            validated_attachment_values=attachment_values,
            selected_node_ids=selected_node_ids,
            verified_lessons=_active_verified_capability_lessons(ctx),
        )
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}") from exc
    except NodeLibraryError as exc:
        raise RuntimeError(f"Workflow refinement compilation failed: {exc}") from exc
    except ComfyUIError as exc:
        raise RuntimeError(f"Workflow attachment validation failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Active workflow refinement input is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in compile_workflow_refinement_spec: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


def _graph_patch_validation_summary(compiled: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "valid": compiled.get("valid") is True,
        "schema": compiled.get("schema"),
        "patch_hash": compiled.get("patch_hash"),
        "catalog": compiled.get("catalog"),
        "expected_final": compiled.get("expected_final"),
        "issues": compiled.get("issues", []),
        "error_count": compiled.get("error_count", 0),
    }


def _graph_patch_schema_contracts(
    plan: Dict[str, Any],
    catalog: Dict[str, Any],
) -> Dict[str, Any]:
    """Provide browser-verifiable normalized schemas for only touched node classes."""

    facts: Dict[str, str] = {}
    for item in [
        *plan.get("assertions", {}).get("nodes", []),
        *plan.get("create_nodes", []),
        *plan.get("update_nodes", []),
        *plan.get("remove_nodes", []),
    ]:
        if not isinstance(item, dict):
            continue
        node_type = item.get("node_type")
        schema_hash = item.get("schema_hash")
        if not isinstance(node_type, str) or not isinstance(schema_hash, str):
            continue
        previous = facts.setdefault(node_type, schema_hash)
        if previous != schema_hash:
            raise ValueError(f"GraphPatch has conflicting schema hashes for {node_type}")
    contracts: Dict[str, Any] = {}
    for node_type, schema_hash in sorted(facts.items()):
        node_info = catalog.get(node_type)
        if not isinstance(node_info, dict):
            raise ValueError(f"GraphPatch node type {node_type} is absent from the catalog")
        contracts[node_type] = {
            "schema_hash": schema_hash,
            "schema": normalize_node_schema_contract(node_info),
        }
    return contracts


def _graph_patch_failure(
    request: WorkflowGraphPatchApplyRequest,
    *,
    code: str,
    message: str,
    validation: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "success": False,
        "applied": False,
        "already_applied": False,
        "application_id": request.application_id,
        "patch_hash": request.patch_hash,
        "error": {"code": code, "message": message},
        "validation": validation,
        "rollback": {
            "attempted": False,
            "complete": True,
            "snapshot_restored": False,
            "hash_verified": False,
            "errors": [],
        },
        "queued": False,
    }


_GRAPH_PATCH_LEDGER_SCHEMA = "fl-mcp.workflow-graph-patch.v2"
_GRAPH_PATCH_LEDGER_LIMIT = 64
_GRAPH_PATCH_LEDGER_FACT_LIMIT = 100
_GRAPH_PATCH_LEDGER_NODE_ID_STRING_LIMIT = 4_096
_GRAPH_PATCH_LEDGER_APPLICATION_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
)
_GRAPH_PATCH_LEDGER_ALIAS = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_GRAPH_PATCH_LEDGER_HASH = re.compile(r"^[0-9a-f]{64}$")
_GRAPH_PATCH_LEDGER_ENTRY_FIELDS = frozenset(
    {
        "aliases",
        "created_node_ids",
        "patch_hash",
        "removed_node_ids",
        "result_content_hash",
    }
)


def _valid_graph_patch_ledger_node_id(value: Any) -> bool:
    return bool(
        type(value) is int
        or (
            type(value) is str
            and 0 < len(value) <= _GRAPH_PATCH_LEDGER_NODE_ID_STRING_LIMIT
        )
    )


def _valid_graph_patch_ledger_hash(value: Any) -> bool:
    return bool(
        type(value) is str
        and _GRAPH_PATCH_LEDGER_HASH.fullmatch(value) is not None
    )


def _graph_patch_ledger_is_valid(ledger: Any) -> bool:
    """Validate the complete untrusted persisted ledger without copying it."""

    if not isinstance(ledger, dict) or len(ledger) != 3:
        return False
    if set(ledger) != {"schema", "order", "entries"}:
        return False
    order = ledger.get("order")
    entries = ledger.get("entries")
    if (
        ledger.get("schema") != _GRAPH_PATCH_LEDGER_SCHEMA
        or not isinstance(order, list)
        or len(order) > _GRAPH_PATCH_LEDGER_LIMIT
        or not isinstance(entries, dict)
        or len(entries) > _GRAPH_PATCH_LEDGER_LIMIT
    ):
        return False
    if any(
        type(application_id) is not str
        or _GRAPH_PATCH_LEDGER_APPLICATION_ID.fullmatch(application_id) is None
        for application_id in order
    ):
        return False
    if len(order) != len(set(order)):
        return False
    if any(
        type(application_id) is not str
        or _GRAPH_PATCH_LEDGER_APPLICATION_ID.fullmatch(application_id) is None
        for application_id in entries
    ):
        return False
    if set(order) != set(entries):
        return False

    for application_id in order:
        entry = entries.get(application_id)
        if not isinstance(entry, dict):
            return False
        fields = set(entry)
        if fields not in (
            _GRAPH_PATCH_LEDGER_ENTRY_FIELDS,
            _GRAPH_PATCH_LEDGER_ENTRY_FIELDS | {"result_definition_hash"},
        ):
            return False
        if any(
            not _valid_graph_patch_ledger_hash(entry.get(field))
            for field in ("patch_hash", "result_content_hash")
        ):
            return False
        if (
            "result_definition_hash" in entry
            and not _valid_graph_patch_ledger_hash(entry.get("result_definition_hash"))
        ):
            return False
        aliases = entry.get("aliases")
        created = entry.get("created_node_ids")
        removed = entry.get("removed_node_ids")
        if (
            not isinstance(aliases, dict)
            or len(aliases) > _GRAPH_PATCH_LEDGER_FACT_LIMIT
            or not isinstance(created, list)
            or len(created) > _GRAPH_PATCH_LEDGER_FACT_LIMIT
            or not isinstance(removed, list)
            or len(removed) > _GRAPH_PATCH_LEDGER_FACT_LIMIT
        ):
            return False
        if any(
            type(alias) is not str
            or _GRAPH_PATCH_LEDGER_ALIAS.fullmatch(alias) is None
            or not _valid_graph_patch_ledger_node_id(node_id)
            for alias, node_id in aliases.items()
        ):
            return False
        if any(not _valid_graph_patch_ledger_node_id(item) for item in created):
            return False
        if any(not _valid_graph_patch_ledger_node_id(item) for item in removed):
            return False
        typed_alias_ids = {(type(item).__name__, item) for item in aliases.values()}
        typed_created = {(type(item).__name__, item) for item in created}
        typed_removed = {(type(item).__name__, item) for item in removed}
        if (
            len(typed_alias_ids) != len(aliases)
            or len(typed_created) != len(created)
            or len(typed_removed) != len(removed)
            or typed_alias_ids != typed_created
            or typed_created & typed_removed
        ):
            return False
    return True


def _completed_graph_patch_result(
    active: Dict[str, Any],
    request: WorkflowGraphPatchApplyRequest,
    *,
    catalog: Mapping[str, Any],
) -> Dict[str, Any] | None:
    """Return a verified idempotent result from the persisted GraphPatch ledger."""

    if active.get("workflow_identity") != request.plan.expected_workflow_identity:
        return None

    workflow = active.get("workflow")
    extra = workflow.get("extra") if isinstance(workflow, dict) else None
    ledger = extra.get("fl_mcp_graph_patch_ledger") if isinstance(extra, dict) else None
    if ledger is None:
        return None
    if not _graph_patch_ledger_is_valid(ledger):
        return _graph_patch_failure(
            request,
            code="invalid_graph_patch_ledger",
            message="The persisted GraphPatch ledger is malformed; the canvas was not edited.",
            validation={"valid": False, "issues": [], "error_count": 1},
        )
    entry = ledger["entries"].get(request.application_id)
    if entry is None:
        return None
    target_has_definition_hash = "result_definition_hash" in entry
    if target_has_definition_hash != isinstance(request, ApplyScopedGraphPatchRequest):
        return _graph_patch_failure(
            request,
            code="invalid_graph_patch_ledger",
            message=(
                "The persisted GraphPatch ledger entry has the wrong root/scoped "
                "result shape; the canvas was not edited."
            ),
            validation={"valid": False, "issues": [], "error_count": 1},
        )
    if not isinstance(entry, dict) or entry.get("patch_hash") != request.patch_hash:
        return _graph_patch_failure(
            request,
            code="graph_patch_idempotency_conflict",
            message="This application ID is already bound to a different GraphPatch.",
            validation={"valid": False, "issues": [], "error_count": 1},
        )
    current_content_hash = active.get("graph_patch_content_hash")
    if (
        not isinstance(current_content_hash, str)
        or current_content_hash != entry.get("result_content_hash")
    ):
        return _graph_patch_failure(
            request,
            code="graph_patch_idempotency_conflict",
            message="The workflow changed after this GraphPatch was applied.",
            validation={"valid": False, "issues": [], "error_count": 1},
        )
    completed = {
        "success": True,
        "applied": False,
        "already_applied": True,
        "patch_schema": (
            SCOPED_GRAPH_PATCH_SCHEMA
            if isinstance(request, ApplyScopedGraphPatchRequest)
            else GRAPH_PATCH_SCHEMA
        ),
        "application_id": request.application_id,
        "patch_hash": request.patch_hash,
        "operation": request.plan.operation,
        "expected_workflow_identity": request.plan.expected_workflow_identity,
        "workflow_identity": active.get("workflow_identity"),
        "graph_hash": active.get("graph_hash"),
        "aliases": entry.get("aliases", {}),
        "created_node_ids": entry.get("created_node_ids", []),
        "removed_node_ids": entry.get("removed_node_ids", []),
        "verification": {"valid": True, "issues": [], "idempotency_verified": True},
        "rollback": {
            "attempted": False,
            "complete": True,
            "snapshot_restored": False,
            "hash_verified": False,
            "errors": [],
        },
        "validation": {
            "valid": True,
            "patch_hash": request.patch_hash,
            "issues": [],
            "error_count": 0,
            "idempotency_verified": True,
        },
        "queued": False,
    }
    try:
        attested = _attest_graph_patch_frontend_result(completed, request)
    except RuntimeError:
        return _graph_patch_failure(
            request,
            code="invalid_graph_patch_ledger",
            message=(
                "The persisted GraphPatch ledger does not match the exact plan-shaped "
                "application result; the canvas was not edited."
            ),
            validation={"valid": False, "issues": [], "error_count": 1},
        )
    state_issues = verify_completed_graph_patch_state(
        request,
        workflow,
        catalog,
        attested["aliases"],
        result_definition_hash=entry.get("result_definition_hash"),
    )
    if state_issues:
        return _graph_patch_failure(
            request,
            code="invalid_graph_patch_ledger",
            message=(
                "The persisted GraphPatch ledger is not proven by the current exact "
                "plan postconditions; the canvas was not edited."
            ),
            validation={
                "valid": False,
                "issues": state_issues,
                "error_count": len(state_issues),
            },
        )
    return attested


def _attest_graph_patch_frontend_result(
    result: Any,
    request: WorkflowGraphPatchApplyRequest,
) -> Dict[str, Any]:
    """Bind a bridge response to this exact request before trusting success."""

    if not isinstance(result, dict):
        raise RuntimeError("The frontend returned a non-object GraphPatch result.")
    expected_facts = {
        "patch_schema": (
            SCOPED_GRAPH_PATCH_SCHEMA
            if isinstance(request, ApplyScopedGraphPatchRequest)
            else GRAPH_PATCH_SCHEMA
        ),
        "application_id": request.application_id,
        "patch_hash": request.patch_hash,
        "operation": request.plan.operation,
        "expected_workflow_identity": request.plan.expected_workflow_identity,
    }
    mismatches = [
        field
        for field, expected in expected_facts.items()
        if result.get(field) != expected
    ]
    if mismatches:
        raise RuntimeError(
            "The frontend GraphPatch result failed request attestation: "
            + ", ".join(mismatches)
        )
    for field in ("success", "applied", "already_applied"):
        if type(result.get(field)) is not bool:
            raise RuntimeError(f"The frontend GraphPatch result has invalid {field} state.")
    if result.get("queued") is not False:
        raise RuntimeError("The frontend GraphPatch result did not attest queued=false.")
    if result["success"] is not True:
        if result["applied"] or result["already_applied"]:
            raise RuntimeError("A failed GraphPatch result claimed a committed application.")
        return result
    if result["applied"] == result["already_applied"]:
        raise RuntimeError(
            "A successful GraphPatch result must be applied or already_applied, exclusively."
        )
    verification = result.get("verification")
    if not (
        isinstance(verification, dict)
        and verification.get("valid") is True
        and verification.get("issues") == []
    ):
        raise RuntimeError("The frontend GraphPatch success lacks exact clean verification.")
    rollback = result.get("rollback")
    if not (
        isinstance(rollback, dict)
        and rollback.get("attempted") is False
        and rollback.get("complete") is True
        and rollback.get("errors") == []
    ):
        raise RuntimeError("The frontend GraphPatch success has an invalid rollback state.")
    aliases = result.get("aliases")
    created_node_ids = result.get("created_node_ids")
    removed_node_ids = result.get("removed_node_ids")
    if not isinstance(aliases, dict) or not isinstance(created_node_ids, list):
        raise RuntimeError("The frontend GraphPatch success lacks created-node identity facts.")
    if not isinstance(removed_node_ids, list):
        raise RuntimeError("The frontend GraphPatch success lacks removed-node identity facts.")
    expected_aliases = {item.alias for item in request.plan.create_nodes}
    if set(aliases) != expected_aliases or len(created_node_ids) != len(expected_aliases):
        raise RuntimeError("The frontend GraphPatch created-node facts do not match the plan.")
    if any(type(value) not in (int, str) for value in created_node_ids):
        raise RuntimeError("The frontend GraphPatch returned an invalid created node ID.")
    typed_created = {(type(value).__name__, value) for value in created_node_ids}
    if len(typed_created) != len(created_node_ids):
        raise RuntimeError("The frontend GraphPatch returned duplicate created node IDs.")
    if {(type(value).__name__, value) for value in aliases.values()} != typed_created:
        raise RuntimeError("The frontend alias mapping disagrees with created node IDs.")
    if any(type(value) not in (int, str) for value in removed_node_ids):
        raise RuntimeError("The frontend GraphPatch returned an invalid removed node ID.")
    typed_removed = [(type(value).__name__, value) for value in removed_node_ids]
    if len(set(typed_removed)) != len(typed_removed):
        raise RuntimeError("The frontend GraphPatch returned duplicate removed node IDs.")
    expected_removed = [
        (type(item.ref.node_id).__name__, item.ref.node_id)
        for item in request.plan.remove_nodes
    ]
    if typed_removed != expected_removed:
        raise RuntimeError("The frontend removed-node facts do not match the plan.")
    graph_hash = result.get("graph_hash")
    if not isinstance(graph_hash, str) or re.fullmatch(r"[0-9a-f]{64}", graph_hash) is None:
        raise RuntimeError("The frontend GraphPatch success lacks a valid final graph hash.")
    if result["already_applied"] and verification.get("idempotency_verified") is not True:
        raise RuntimeError("The frontend idempotent GraphPatch result was not verified.")
    return result


@mcp.tool()
async def apply_workflow_graph_patch(
    request: WorkflowGraphPatchApplyRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Atomically apply one unchanged semantic GraphPatch v2 or scoped v3 envelope.

    The backend rereads the active graph, refreshes the local node catalog, and
    recompiles the supplied canonical plan. Scoped v3 plans additionally resolve
    the exact definition hash, edit mode, immutable boundary ports, and complete
    shared-definition instance set from the fresh root workflow. Any workflow,
    graph, catalog, schema, scope, slot, value, or hash drift stops before browser
    mutation. The frontend applies the delta under one canvas lock with exact
    verification, idempotency, rollback, bounded pacing, and no queue call.
    """

    await _report_tool_activity(ctx, "apply_workflow_graph_patch")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    try:
        active = await _active_editable_workflow(ctx)
        empty_validation = {
            "valid": False,
            "schema": None,
            "patch_hash": None,
            "catalog": None,
            "expected_final": None,
            "issues": [],
            "error_count": 0,
        }
        attachment_issues = _graph_patch_attachment_integrity_issues(request)
        if attachment_issues:
            return _graph_patch_failure(
                request,
                code="attachment_missing_or_changed",
                message=(
                    "A compiler-attested Ren chat image is missing or changed; "
                    "compile the workflow refinement again."
                ),
                validation={
                    **empty_validation,
                    "issues": attachment_issues,
                    "error_count": len(attachment_issues),
                },
            )
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        completed = _completed_graph_patch_result(
            active,
            request,
            catalog=snapshot.data,
        )
        if completed is not None:
            return completed
        if active["workflow_identity"] != request.plan.expected_workflow_identity:
            return _graph_patch_failure(
                request,
                code="workflow_identity_changed",
                message="The active workflow tab changed; the canvas was not edited.",
                validation=empty_validation,
            )
        if active["graph_hash"] != request.plan.expected_graph_hash:
            return _graph_patch_failure(
                request,
                code="graph_changed",
                message="The active workflow graph changed; compile the refinement again.",
                validation=empty_validation,
            )
        if isinstance(request, ApplyScopedGraphPatchRequest):
            canonical_request = scoped_graph_patch_request_from_apply(request)
            compiled = compile_scoped_graph_patch(
                canonical_request,
                active["workflow"],
                workflow_identity=active["workflow_identity"],
                graph_hash=active["graph_hash"],
                catalog=snapshot.data,
                catalog_hash=snapshot.catalog_hash,
                source=snapshot.source,
            )
        else:
            graph = normalize_workflow_graph(active["workflow"])
            canonical_request = graph_patch_request_from_apply(request, graph)
            compiled = compile_graph_patch(
                canonical_request,
                snapshot.data,
                catalog_hash=snapshot.catalog_hash,
                source=snapshot.source,
            )
        validation = _graph_patch_validation_summary(compiled)
        if not compiled["valid"]:
            return _graph_patch_failure(
                request,
                code="patch_invalid",
                message="The graph patch is no longer valid; the canvas was not edited.",
                validation=validation,
            )
        if (
            compiled["patch_hash"] != request.patch_hash
            or compiled["plan"] != request.plan.model_dump(mode="json")
        ):
            return _graph_patch_failure(
                request,
                code="patch_hash_mismatch",
                message="The current canonical graph patch differs from the supplied plan.",
                validation=validation,
            )
        # Catalog refresh and recompilation are awaited operations. Recheck the
        # compiler-pinned bytes at the last backend boundary before the browser
        # is allowed to mutate the canvas, closing that apply-time TOCTOU gap.
        attachment_issues = _graph_patch_attachment_integrity_issues(request)
        if attachment_issues:
            return _graph_patch_failure(
                request,
                code="attachment_missing_or_changed",
                message=(
                    "A compiler-attested Ren chat image is missing or changed; "
                    "compile the workflow refinement again."
                ),
                validation={
                    **empty_validation,
                    "issues": attachment_issues,
                    "error_count": len(attachment_issues),
                },
            )
        result = await _execute_tool(
            ctx,
            "apply_workflow_graph_patch",
            {
                **compiled["apply_request"],
                "schema_contracts": _graph_patch_schema_contracts(
                    compiled["plan"],
                    snapshot.data,
                ),
            },
            timeout_ms=240000,
        )
        result = _attest_graph_patch_frontend_result(result, request)
        if result.get("success") is True and (
            result.get("applied") is True or result.get("already_applied") is True
        ):
            _record_verified_graph_patch_lessons(
                _node_knowledge_store(ctx),
                plan=compiled["plan"],
                patch_hash=request.patch_hash,
                application_id=request.application_id,
            )
        return {**result, "validation": validation}
    except NodeLibraryConnectionError as exc:
        raise RuntimeError(f"ComfyUI server connection failed: {exc}") from exc
    except NodeLibraryError as exc:
        raise RuntimeError(f"Graph-patch application failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Active workflow graph is invalid: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error in apply_workflow_graph_patch: %s", exc)
        raise RuntimeError(f"Tool execution failed: {exc}") from exc


@mcp.tool()
async def plan_workflow(request: PlanWorkflowRequest, ctx: Context) -> Dict[str, Any]:
    """Validate a deterministic workflow plan without changing the canvas.

    Call this after discovering loaded nodes with node_library_search and
    inspecting their exact schemas with node_library_get_details. It resolves
    semantic aliases against one pinned /object_info catalog, validates widget
    values (without relying on mutable defaults) and exact connection slots
    (including supported Comfy v3 dynamic inputs), rejects cycles, and returns
    a stable plan hash only when valid.

    This is a dry run. It never creates, edits, connects, queues, or installs
    anything. If valid=false, correct every reported error and plan again before
    using canvas-edit tools.
    """
    await _report_tool_activity(ctx, "plan_workflow")

    try:
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        attachment_values = _validated_plan_attachment_values(request)
        return compile_workflow_plan(
            request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
            validated_attachment_values=attachment_values,
        )
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Workflow planning failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in plan_workflow: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def compile_workflow_spec(
    request: CompileWorkflowSpecRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Compile a complete semantic workflow request into one ready-to-apply plan.

    Prefer this tool for new workflows described in user language. In one bounded
    dry run it resolves roles to exact locally loaded classes, canonicalizes unique
    short widget and slot names to their exact runtime names (including dotted Comfy
    v3 partner inputs), fills stable schema defaults, validates trusted Ren chat image
    bindings, and produces the exact ``apply_workflow_plan`` request.

    Partner/API selections include structured authentication, cost, and privacy facts.
    Those facts are sufficient for a build-only request and do not require web search.
    This tool never changes the canvas, transmits images to a partner, queues, installs,
    or executes anything. If valid=true, pass ``apply_request`` unchanged to
    ``apply_workflow_plan``. Use lower-level resolver, detail, and planner tools only
    when this compiler reports a genuine ambiguity or unsupported schema.
    """
    await _report_tool_activity(ctx, "compile_workflow_spec")

    try:
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        attachment_values = _validated_plan_attachment_values(request)
        return compile_semantic_workflow(
            request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
            validated_attachment_values=attachment_values,
        )
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Workflow compilation failed: {e}")
    except ComfyUIError as e:
        raise RuntimeError(f"Workflow attachment validation failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in compile_workflow_spec: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def resolve_workflow_spec(
    request: ResolveWorkflowSpecRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Resolve semantic workflow roles to exact locally loaded node classes.

    Use this before ``plan_workflow`` when the user describes capabilities rather
    than exact class names. Resolution is read-only and pinned to one /object_info
    catalog generation. It applies hard input/output/origin constraints, honors an
    explicitly requested exact class without silent substitution, prefers classes
    already used by the workflow or a verified local pattern, and otherwise uses
    stable semantic scoring, local-first origin policy, and lexical tie-breaking.

    Only loaded native, custom, partner, or unknown classes are eligible. Public
    Registry results never enter a workflow through this tool. Partner selections
    carry authentication, cost, and privacy review warnings. Inspect every selected
    class with ``node_library_get_details``, then submit exact nodes and edges to
    ``plan_workflow``. This tool never changes the canvas, installs, or queues.
    """
    await _report_tool_activity(ctx, "resolve_workflow_spec")

    try:
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        return resolve_workflow_capabilities(
            request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
        )
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Workflow capability resolution failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in resolve_workflow_spec: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def apply_workflow_plan(
    request: ApplyWorkflowPlanRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Atomically apply one valid, catalog-pinned workflow plan to the canvas.

    Re-submit the exact nodes, connections, catalog hash, and plan hash returned
    by ``plan_workflow``. The server recompiles them against the current loaded
    catalog before any browser mutation. The browser then creates widget values
    and exact connections in one transaction, verifies the applied subgraph, and
    removes every created node if creation, connection, or verification fails.

    ``application_id`` is the idempotency boundary. Reusing it for the identical
    plan returns the existing semantic alias-to-node-ID mapping without creating
    duplicates. Reusing it for a different or manually changed graph fails closed.
    This tool never queues the workflow.
    """
    await _report_tool_activity(ctx, "apply_workflow_plan")
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")

    try:
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout,
        )
        snapshot = await client.catalog_snapshot(force_refresh=True)
        plan_request = PlanWorkflowRequest(
            nodes=request.nodes,
            connections=request.connections,
            attachments=request.attachments,
            expected_catalog_hash=request.expected_catalog_hash,
        )
        attachment_values = _validated_plan_attachment_values(request)
        compiled = compile_workflow_plan(
            plan_request,
            snapshot.data,
            catalog_hash=snapshot.catalog_hash,
            source=snapshot.source,
            validated_attachment_values=attachment_values,
        )
        validation = {
            "valid": compiled["valid"],
            "plan_hash": compiled["plan_hash"],
            "catalog": compiled["catalog"],
            "issues": compiled["issues"],
            "error_count": compiled["error_count"],
            "warning_count": compiled["warning_count"],
        }
        if not compiled["valid"]:
            return {
                "success": False,
                "applied": False,
                "already_applied": False,
                "application_id": request.application_id,
                "plan_hash": request.plan_hash,
                "error": {
                    "code": "plan_invalid",
                    "message": "The workflow plan is no longer valid; the canvas was not changed.",
                },
                "validation": validation,
                "rollback": {
                    "attempted": False,
                    "complete": True,
                    "attempted_node_ids": [],
                    "remaining_node_ids": [],
                    "errors": [],
                },
                "queued": False,
            }
        if compiled["plan_hash"] != request.plan_hash:
            return {
                "success": False,
                "applied": False,
                "already_applied": False,
                "application_id": request.application_id,
                "plan_hash": request.plan_hash,
                "error": {
                    "code": "plan_hash_mismatch",
                    "message": (
                        "The supplied plan hash does not match the canonical current plan; "
                        "the canvas was not changed."
                    ),
                },
                "validation": validation,
                "rollback": {
                    "attempted": False,
                    "complete": True,
                    "attempted_node_ids": [],
                    "remaining_node_ids": [],
                    "errors": [],
                },
                "queued": False,
            }

        result = await _execute_tool(
            ctx,
            "apply_workflow_plan",
            {
                "application_id": request.application_id,
                "plan_hash": request.plan_hash,
                "plan": compiled["plan"],
            },
            timeout_ms=60000,
        )
        if not isinstance(result, dict):
            raise RuntimeError("The frontend returned an invalid workflow application result.")
        if result.get("success") is True and (
            result.get("applied") is True or result.get("already_applied") is True
        ):
            _record_verified_connection_lessons(
                _node_knowledge_store(ctx),
                plan=compiled["plan"],
                plan_hash=request.plan_hash,
                application_id=request.application_id,
            )
        return {**result, "validation": validation}
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Workflow application failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in apply_workflow_plan: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_search(request: NodeLibrarySearchRequest, ctx: Context) -> Dict[str, Any]:
    """Search for available ComfyUI node types (not workflow nodes).
    
    This tool searches the library of installed node types that can be created
    in workflows. Use this to discover what node types are available before
    creating them with create_nodes().
    
    DISTINCTION FROM find_node():
    - find_node() searches nodes already IN your workflow
    - node_library_search() searches node TYPES available to create
    
    USE CASES:
    - "What node types handle upscaling?" → output_type="IMAGE", query="upscale"
    - "Show samplers" → category="sampling"
    - "What accepts LATENT?" → input_type="LATENT"
    - "Find LoRA loaders" → query="lora"
    
    RETURNS:
    A compact ranked candidate list. Full input schemas are intentionally omitted
    so a broad search cannot inject hundreds of kilobytes into the chat context.
    Use node_library_get_details() only for a chosen exact type; normal workflow
    builds/refinements should prefer their one-pass semantic compiler instead.
    """
    await _report_tool_activity(ctx, "node_library_search")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        results = await client.search_nodes(
            query=request.query,
            category=request.category,
            input_type=request.input_type,
            output_type=request.output_type,
            max_results=request.max_results + 1
        )
        truncated = len(results) > request.max_results
        results = results[:request.max_results]

        formatted_results = [
            {
                "node_type": r.node_type,
                "display_name": r.display_name,
                "category": r.category,
                "description": r.description,
                "output_types": sorted(
                    {str(item) for item in r.outputs if isinstance(item, str)}
                ),
                "match_reason": r.match_reason,
                "origin": r.origin,
                "python_module": r.python_module,
                "schema_hash": r.schema_hash,
                "score": r.score,
            }
            for r in results
        ]

        return {
            "query": request.model_dump(exclude_none=True),
            "results": formatted_results,
            "total_results": len(formatted_results),
            "truncated": truncated,
            "compact": True,
            "schema_details_tool": "node_library_get_details",
            "catalog": await client.catalog_status(),
        }
        
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_search: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_get_details(request: NodeLibraryGetDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get comprehensive details about a specific node type.
    
    This tool provides everything needed to understand and use a node type
    before creating it in the workflow with create_nodes().
    
    DISTINCTION FROM get_node_values():
    - get_node_values() gets parameter VALUES from a workflow node instance
    - node_library_get_details() gets parameter DEFINITIONS for a node type
    
    USE CASES:
    - Before creating a node: understand what parameters it needs
    - When planning workflow: verify input/output compatibility
    - When debugging: check valid parameter ranges and types
    - Learning: understand what a node type does
    
    RETURNS:
    Complete node type specification including:
    - All input parameters with types, defaults, constraints (min/max/options)
    - All output types and names
    - Category and display information
    - Parameter order (for UI layout)
    """
    await _report_tool_activity(ctx, "node_library_get_details")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        node_info = await client.get_node_details(request.node_type)
        store = _node_knowledge_store(ctx)
        verified_lessons = (
            store.get_verified_lessons(request.node_type) if store is not None else []
        )
        
        return {
            "node_type": request.node_type,
            "display_name": node_info.get('display_name', request.node_type),
            "category": node_info.get('category', ''),
            "description": node_info.get('description', ''),
            "inputs": node_info.get('input', {}),
            "outputs": node_info.get('output', []),
            "output_names": node_info.get('output_name', []),
            "input_order": node_info.get('input_order', []),
            "origin": node_info.get('origin', 'unknown'),
            "python_module": node_info.get('python_module', ''),
            "schema_hash": node_info.get('schema_hash'),
            "catalog_hash": node_info.get('catalog_hash'),
            "source": node_info.get('source'),
            "api_node": bool(node_info.get('api_node')),
            "deprecated": bool(node_info.get('deprecated')),
            "experimental": bool(node_info.get('experimental')),
            "verified_lessons": verified_lessons,
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library lookup failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_get_details: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


@mcp.tool()
async def node_library_find_compatible(request: NodeLibraryFindCompatibleRequest, ctx: Context) -> Dict[str, Any]:
    """Find node types that can connect to/from a given node type.

    This tool helps discover what node types are compatible based on input/output
    type matching. Use this when building workflows to find what comes next.

    HEURISTIC ONLY: This currently uses exact runtime type labels. Confirm the
    chosen nodes with node_library_get_details before creating connections.
    
    DISTINCTION FROM connect_nodes():
    - connect_nodes() connects EXISTING workflow nodes together
    - node_library_find_compatible() finds compatible node TYPES to create
    
    USE CASES:
    - Building workflow: "What can I connect after KSampler?" → downstream
    - Understanding flow: "What feeds into VAEDecode?" → upstream
    - Planning chain: "Build checkpoint → sampler → decode → save" → iterate downstream
    - Type checking: "Can I connect this type to that?" → verify compatibility
    
    RETURNS:
    Array of compatible node types with connection details:
    - Which output/input slots are compatible
    - What data types match
    - Suggested connection patterns
    """
    await _report_tool_activity(ctx, "node_library_find_compatible")
    
    try:
        from config import settings
        client = get_node_library_client(
            server_url=settings.comfyui_server_url,
            timeout=settings.comfyui_api_timeout
        )
        
        compatible = await client.find_compatible_nodes(
            node_type=request.node_type,
            direction=request.direction,
            output_slot=request.output_slot,
            input_slot=request.input_slot,
            max_results=request.max_results + 1
        )
        directions = (
            ["downstream", "upstream"]
            if request.direction == "both"
            else [request.direction]
        )
        truncated = False
        visible_compatible = []
        for direction in directions:
            directional = [item for item in compatible if item.direction == direction]
            truncated = truncated or len(directional) > request.max_results
            visible_compatible.extend(directional[:request.max_results])

        formatted_compatible = [
            {
                "node_type": c.node_type,
                "display_name": c.display_name,
                "category": c.category,
                "direction": c.direction,
                "connection": c.connection,
                "description": c.description
            }
            for c in visible_compatible
        ]
        
        return {
            "source_node_type": request.node_type,
            "direction": request.direction,
            "compatible_nodes": formatted_compatible,
            "total_compatible": len(formatted_compatible),
            "truncated": truncated,
            "catalog": await client.catalog_status(),
        }
        
    except NodeTypeNotFoundError as e:
        raise RuntimeError(str(e))
    except NodeLibraryConnectionError as e:
        raise RuntimeError(f"ComfyUI server connection failed: {e}")
    except NodeLibraryError as e:
        raise RuntimeError(f"Node library compatibility search failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in node_library_find_compatible: {e}")
        raise RuntimeError(f"Tool execution failed: {e}")


# ============================================================================
# OFFICIAL COMFY REGISTRY DISCOVERY TOOLS
# ============================================================================

async def _registry_installed_pack_ids(
    ctx: Context,
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Best-effort Manager annotation; Registry discovery must work without it."""
    manager_client = ctx.request_context.lifespan_context.get("manager_client")
    if manager_client is None:
        return None, {
            "state": "unknown",
            "source": "comfyui_manager",
            "reason": "ComfyUI Manager client is unavailable",
        }
    try:
        installed = await manager_client.list_installed_packs()
    except Exception as exc:
        return None, {
            "state": "unknown",
            "source": "comfyui_manager",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(installed, dict):
        return None, {
            "state": "unknown",
            "source": "comfyui_manager",
            "reason": "Manager returned an unexpected installed-pack response",
        }

    package_ids: set[str] = set()
    repository_urls: set[str] = set()
    identity_complete = True
    for fallback_id, metadata in installed.items():
        fallback_package_id = str(fallback_id or "").strip()
        if fallback_package_id:
            package_ids.add(fallback_package_id)

        registry_id = metadata.get("cnr_id") if isinstance(metadata, dict) else None
        registry_id = str(registry_id or "").strip()
        if registry_id:
            package_ids.add(registry_id)

        aux_id = metadata.get("aux_id") if isinstance(metadata, dict) else None
        aux_id = str(aux_id or "").strip()
        repository_url = normalize_github_repository_url(aux_id)
        if repository_url is None and aux_id.count("/") == 1:
            repository_url = normalize_github_repository_url(
                f"https://github.com/{aux_id}"
            )
        if repository_url:
            repository_urls.add(repository_url)

        # A Manager folder key can resemble a Registry ID, but does not prove
        # package identity without either CNR metadata or a verified repository.
        if not registry_id and not repository_url:
            identity_complete = False

    envelope = {
        "package_ids": sorted(package_ids, key=str.casefold),
        "repository_urls": sorted(repository_urls, key=str.casefold),
        "identity_complete": identity_complete,
    }
    diagnostics: Dict[str, Any] = {
        "state": "known",
        "source": "comfyui_manager",
        "installed_pack_count": len(installed),
        "package_identity_count": len(package_ids),
        "repository_identity_count": len(repository_urls),
        "identity_complete": identity_complete,
    }
    if not identity_complete:
        diagnostics["reason"] = (
            "Some Manager packs lack both a Registry ID and a verifiable GitHub aux_id"
        )
    return envelope, diagnostics


@mcp.tool()
async def registry_search_packages(
    request: RegistrySearchPackagesRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Search all published packages in the official Comfy Registry.

    Use this for new or uninstalled node-pack discovery. It is intentionally
    separate from node_library_search, which only searches node types already
    loaded by the current ComfyUI /object_info endpoint, and from
    manager_search_nodes, which searches Manager's local/cache view.

    Every result contains both `registry_url` and `github_url` so the user can
    inspect the official package record and source repository. Registry
    metadata is discovery evidence, not proof that a package is installed,
    compatible with this machine, or usable by the current workflow.

    Search functional requests with concise capability terms such as
    `background removal`, not the user's whole sentence. A generic request to
    browse new Registry nodes returns a bounded Registry-ranked package page;
    do not describe that result as recent unless its metadata proves recency.
    By default Manager-known installed packages are excluded; set
    include_installed=true only when the user wants Registry records for both
    installed and uninstalled packs.
    """
    await _report_tool_activity(ctx, "registry_search_packages")
    registry_client: ComfyRegistryClient = (
        ctx.request_context.lifespan_context["registry_client"]
    )
    installed_pack_ids, install_state = await _registry_installed_pack_ids(ctx)
    try:
        result = await registry_client.search_packages(
            request.query,
            comfy_node_search=request.comfy_node_search,
            supported_os=request.supported_os,
            supported_accelerator=request.supported_accelerator,
            include_installed=request.include_installed,
            max_results=request.max_results,
            refresh=request.refresh,
            installed_pack_ids=installed_pack_ids,
        )
        return {**result, "local_install_state": install_state}
    except Exception as exc:
        logger.error("Official Registry package search failed: %s", exc)
        raise RuntimeError(f"Official Registry search failed: {exc}") from exc


@mcp.tool()
async def registry_get_package(
    request: RegistryGetPackageRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Inspect one official Registry package before recommending installation.

    The structured result always includes the Registry page and deterministic
    GitHub repository hyperlinks supplied by the Registry client, plus bounded
    published node-class metadata. This does not prove local compatibility or
    availability; use node_library_search after installation and restart.
    """
    await _report_tool_activity(ctx, "registry_get_package")
    registry_client: ComfyRegistryClient = (
        ctx.request_context.lifespan_context["registry_client"]
    )
    installed_pack_ids, install_state = await _registry_installed_pack_ids(ctx)
    try:
        result = await registry_client.get_package(
            request.package_id,
            refresh=request.refresh,
            installed_pack_ids=installed_pack_ids,
            max_classes=request.max_classes,
        )
        return {**result, "local_install_state": install_state}
    except Exception as exc:
        logger.error("Official Registry package lookup failed: %s", exc)
        raise RuntimeError(f"Official Registry package lookup failed: {exc}") from exc

# ============================================================================
# COMFYUI MANAGER TOOLS
# ============================================================================

@mcp.tool()
async def manager_search_nodes(
    request: ManagerSearchNodesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search ComfyUI Manager's installed-pack and local/cache data.

    This is not an authoritative search of every package published in the
    official Comfy Registry. Use registry_search_packages for new or
    uninstalled package discovery and registry_get_package before recommending
    a package.
    
    Use this tool to inspect Manager-known installed or cached packs by name,
    category, functionality, or specific node class. Results depend on the
    current Manager installation and selected local/remote/cache mode.
    
    WHEN TO USE:
    - "What node packs handle image upscaling?" → query="upscale"
    - "Show me animation node packs" → category="animation"
    - "Which pack has KSampler?" → node_filter="KSampler"
    - "Find FL nodes" → node_filter="FL_.*"
    - "What's installed?" → installed_only=True
    - "What can I update?" → updates_available=True
    - "Find packs by author" → query="author_name"
    
    NODE FILTER EXAMPLES:
    - "KSampler" → exact match
    - "FL_.*" → all FL nodes
    - "Image.*Saver" → ImageSaver, ImageBatchSaver, etc.
    - "(Load|Save)Image" → LoadImage or SaveImage
    
    RETURNS:
    Array of node pack objects with:
    - name, description, author, repository
    - installation status ("True", "False", "Update")
    - stars, last_update, category
    - files (download URLs)
    - matched_nodes (if node_filter used) - list of node class names that matched
    
    NOTE: Mutating installs and updates are available through the confirmation-gated
    manager_queue_action / manager_v4_queue_action tools.
    """
    await _report_tool_activity(ctx, "manager_search_nodes")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "results": [],
                "count": 0
            }
        
        results = await manager_client.search_node_packs(
            query=request.query,
            category=request.category,
            node_filter=request.node_filter,
            installed_only=request.installed_only,
            updates_available=request.updates_available,
            mode=request.mode,
            max_results=request.max_results
        )
        
        # Convert dataclass to dict
        results_dict = [
            {
                "id": pack.id,
                "name": pack.name,
                "description": pack.description,
                "author": pack.author,
                "repository": pack.repository,
                "installed": pack.installed,
                "updatable": pack.updatable,
                "stars": pack.stars,
                "last_update": pack.last_update,
                "category": pack.category,
                "files": pack.files,
                "matched_nodes": pack.matched_nodes  # Will be None if no node_filter
            }
            for pack in results
        ]
        
        return {
            "results": results_dict,
            "count": len(results_dict),
            "truncated": len(results_dict) >= request.max_results
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {"error": str(e), "results": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "results": [], "count": 0}


@mcp.tool()
async def manager_get_node_mappings(
    request: ManagerGetNodeMappingsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Find which node pack provides a specific node type.
    
    Use this tool to discover the source node pack for any node type in ComfyUI.
    Helps understand dependencies and find where to get missing nodes.
    
    WHEN TO USE:
    - "What pack has the KSampler node?" → node_type="KSampler"
    - "Where does FL_ImageCaptionSaver come from?" → node_type="FL_ImageCaptionSaver"
    - "Show all node-to-pack mappings" → node_type=None (returns all)
    - Debugging missing nodes → lookup node type to find pack
    
    RETURNS:
    If node_type specified:
    - Single mapping: {node_type, pack_id, pack_name, found: true/false}
    
    If node_type empty:
    - All mappings: {mappings: {node_type: {pack_id, pack_name}, ...}, count}
    
    NOTE: This is different from node_library tools which search node TYPE definitions.
    This tool maps node types to their SOURCE PACK.
    """
    await _report_tool_activity(ctx, "manager_get_node_mappings")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "mappings": {},
                "count": 0
            }
        
        mappings = await manager_client.get_node_mappings(mode=request.mode)
        
        if request.node_type:
            # Return specific mapping
            if request.node_type in mappings:
                mapping = mappings[request.node_type]
                return {
                    "node_type": mapping.node_type,
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name,
                    "found": True
                }
            else:
                return {
                    "node_type": request.node_type,
                    "found": False,
                    "error": f"Node type '{request.node_type}' not found in mappings"
                }
        else:
            # Return all mappings
            mappings_dict = {
                node_type: {
                    "pack_id": mapping.node_pack_id,
                    "pack_name": mapping.node_pack_name
                }
                for node_type, mapping in mappings.items()
            }
            return {
                "mappings": mappings_dict,
                "count": len(mappings_dict)
            }
            
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "mappings": {}, "count": 0}


@mcp.tool()
async def manager_check_updates(
    request: ManagerCheckUpdatesRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Check if any installed node packs have available updates.
    
    Use this tool to discover if the ComfyUI installation has outdated node packs
    that could benefit from updates.
    
    WHEN TO USE:
    - Maintenance: "Are there any updates available?"
    - Before troubleshooting: Check if updating might fix issues
    - After installing ComfyUI: See what's outdated
    - Regular checks: Keep environment up to date
    
    MODES:
    - "remote": Check against remote repositories (fresh, slower)
    - "local": Check against local cache (fast, may be stale)
    
    RETURNS:
    {
        "updates_available": bool,
        "details": {...} or "message": "No updates available"
    }
    
    NOTE: This is read-only discovery. To update, use the confirmation-gated
    manager_queue_action / manager_v4_queue_action tools.
    """
    await _report_tool_activity(ctx, "manager_check_updates")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "updates_available": False
            }
        
        result = await manager_client.check_updates(mode=request.mode)
        return result
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"error": str(e), "updates_available": False}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"error": str(e), "updates_available": False}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"error": str(e), "updates_available": False}


@mcp.tool()
async def manager_search_external_models(
    request: ManagerSearchExternalModelsRequest,
    ctx: Context
) -> Dict[str, Any]:
    """Search for uninstalled models available through ComfyUI Manager.
    
    Use this tool to discover models that can be downloaded and installed.
    Different from manager_search_models which searches INSTALLED local files.
    
    WHEN TO USE:
    - "What FLUX models are available?" → base_filter="FLUX"
    - "Find upscalers" → type_filter="upscale"
    - "Search for anime models" → query="anime"
    - "What models can I download?" → uninstalled_only=True
    - "Find TAESD decoders" → type_filter="TAESD"
    
    FILTER EXAMPLES:
    - base_filter="FLUX|SDXL" → FLUX or SDXL models
    - type_filter="checkpoint|lora" → Checkpoints or LoRAs
    - query="4x" → Models with "4x" in name/description/filename
    - description_filter="anime" → Models mentioning anime
    
    RETURNS:
    Array of external model objects with:
    - name, filename, type, base
    - description, reference (source URL)
    - save_path (where it installs)
    - size (human-readable)
    - url (direct download link)
    - installed (boolean status)
    
    NOTE: This tool is READ-ONLY discovery. To install a discovered model, use
    the confirmation-gated manager_queue_action / manager_v4_queue_action tools
    with the install-model action and the selected model metadata.
    """
    await _report_tool_activity(ctx, "manager_search_external_models")
    
    try:
        manager_client = ctx.request_context.lifespan_context.get('manager_client')
        if not manager_client:
            return {
                "error": "ComfyUI Manager client not initialized",
                "results": [],
                "count": 0
            }
        
        results = await manager_client.search_external_models(
            query=request.query,
            base_filter=request.base_filter,
            type_filter=request.type_filter,
            name_filter=request.name_filter,
            description_filter=request.description_filter,
            reference_filter=request.reference_filter,
            uninstalled_only=request.uninstalled_only,
            installed_only=request.installed_only,
            max_results=request.max_results,
            mode=request.mode
        )
        
        # Convert dataclass to dict
        results_dict = [
            {
                "name": model.name,
                "filename": model.filename,
                "type": model.type,
                "base": model.base,
                "description": model.description,
                "reference": model.reference,
                "save_path": model.save_path,
                "size": model.size,
                "url": model.url,
                "installed": model.installed
            }
            for model in results
        ]
        
        return {
            "success": True,
            "supported": True,
            "results": results_dict,
            "count": len(results_dict),
            "truncated": len(results_dict) >= request.max_results
        }
        
    except ManagerNotInstalledError as e:
        logger.warning(f"[Manager] Not installed: {e}")
        return {"success": False, "supported": False, "error": str(e), "results": [], "count": 0}
    except ManagerAPIError as e:
        logger.error(f"[Manager] API error: {e}")
        return {"success": False, "supported": False, "error": str(e), "results": [], "count": 0}
    except ManagerConnectionError as e:
        logger.error(f"[Manager] Connection error: {e}")
        return {"success": False, "supported": False, "error": str(e), "results": [], "count": 0}
    except Exception as e:
        logger.error(f"[Manager] Unexpected error: {e}")
        return {"success": False, "supported": False, "error": str(e), "results": [], "count": 0}

# ============================================================================
# ERROR FEEDBACK & QUEUE STATUS TOOLS
# ============================================================================

def _output_image_candidates(
    outputs: Dict[str, Any],
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Flatten ComfyUI history outputs into stable, selectable image records."""
    candidates: List[Dict[str, Any]] = []
    for output_node_id, output in outputs.items():
        if node_id is not None and str(output_node_id) != str(node_id):
            continue
        if not isinstance(output, dict):
            continue
        images = output.get("images")
        if not isinstance(images, list):
            continue
        for node_image_index, image in enumerate(images):
            if not isinstance(image, dict) or not image.get("filename"):
                continue
            candidates.append({
                "nodeId": str(output_node_id),
                "nodeImageIndex": node_image_index,
                "filename": str(image["filename"]),
                "subfolder": str(image.get("subfolder") or ""),
                "type": str(image.get("type") or "output").lower(),
            })
    return candidates


def _resolve_comfy_image_path(comfy_tools: Any, image: Dict[str, Any]) -> Path:
    """Resolve one image inside trusted ComfyUI output/input/temp roots."""
    folder_types = {
        "output": ComfyFolderType.OUTPUT,
        "input": ComfyFolderType.INPUT,
        "temp": ComfyFolderType.TEMP,
    }
    folder_type = folder_types.get(image["type"])
    if folder_type is None:
        raise ComfyUIError(f"Unsupported ComfyUI image type: {image['type']}")

    relative = Path(image["subfolder"]) / image["filename"]
    if relative.is_absolute():
        raise ComfyUIError("Image path must be relative to ComfyUI.")
    for root in comfy_tools._iter_all_paths(folder_type):
        trusted_root = root.resolve()
        candidate = (trusted_root / relative).resolve()
        if candidate.is_relative_to(trusted_root) and candidate.is_file():
            return candidate
    raise ComfyUINotFoundError(
        f"ComfyUI image was not found: {image['type']}/{relative.as_posix()}"
    )


MCP_VISION_PREVIEW_MAX_BYTES = 600_000
MCP_VISION_PREVIEW_MIN_DIMENSION = 256


def _has_visible_transparency(image: PILImage.Image) -> bool:
    """Return true only when alpha changes visible pixels."""

    if "A" in image.getbands():
        return image.getchannel("A").getextrema()[0] < 255
    if image.mode == "P" and "transparency" in image.info:
        return image.convert("RGBA").getchannel("A").getextrema()[0] < 255
    return False


def _bounded_vision_preview(
    image: PILImage.Image,
    max_dimension: int,
) -> tuple[bytes, str, tuple[int, int]]:
    """Encode visual MCP content below Claude's JSON transport threshold."""

    preserve_alpha = _has_visible_transparency(image)
    target_dimension = max_dimension
    while True:
        preview = image.copy()
        preview.thumbnail(
            (target_dimension, target_dimension),
            PILImage.Resampling.LANCZOS,
        )
        buffer = io.BytesIO()
        if preserve_alpha:
            preview.save(buffer, format="PNG", optimize=True)
            preview_format = "png"
        else:
            preview.convert("RGB").save(
                buffer,
                format="JPEG",
                quality=88,
                optimize=True,
            )
            preview_format = "jpeg"
        content = buffer.getvalue()
        current_dimension = max(preview.size)
        if (
            len(content) <= MCP_VISION_PREVIEW_MAX_BYTES
            or current_dimension <= MCP_VISION_PREVIEW_MIN_DIMENSION
        ):
            return content, preview_format, preview.size

        scale = min(
            0.9,
            max(0.5, (MCP_VISION_PREVIEW_MAX_BYTES / len(content)) ** 0.5 * 0.92),
        )
        target_dimension = max(
            MCP_VISION_PREVIEW_MIN_DIMENSION,
            int(current_dimension * scale),
        )


def _output_image_preview(path: Path, max_dimension: int) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    """Create transport-bounded visual content without changing the source image."""
    try:
        with PILImage.open(path) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).copy()
    except (OSError, UnidentifiedImageError) as exc:
        raise ComfyUIError(f"Output is not a readable image: {path.name}") from exc

    original_size = image.size
    preview, preview_format, preview_size = _bounded_vision_preview(
        image,
        max_dimension,
    )
    return preview, preview_format, original_size, preview_size


def _mask_overlay_preview(
    path: Path,
    max_dimension: int,
) -> tuple[bytes, str, tuple[int, int], tuple[int, int], Dict[str, Any]]:
    """Render masked pixels as a magenta overlay for visual verification."""
    try:
        with PILImage.open(path) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        raise ComfyUIError(f"Mask source is not a readable image: {path.name}") from exc

    original_size = image.size
    mask = ImageOps.invert(image.getchannel("A"))
    histogram = mask.histogram()
    weighted_pixels = sum(value * count for value, count in enumerate(histogram)) / 255
    total_pixels = max(1, image.width * image.height)
    bbox = mask.getbbox()
    mask_info = {
        "coveragePercent": round(weighted_pixels / total_pixels * 100, 3),
        "bounds": None if bbox is None else {
            "x": bbox[0],
            "y": bbox[1],
            "width": bbox[2] - bbox[0],
            "height": bbox[3] - bbox[1],
        },
    }
    base = image.convert("RGB")
    tint = PILImage.new("RGB", base.size, (255, 0, 180))
    highlighted = PILImage.blend(base, tint, 0.62)
    preview = PILImage.composite(highlighted, base, mask)
    content, preview_format, preview_size = _bounded_vision_preview(
        preview,
        max_dimension,
    )
    return content, preview_format, original_size, preview_size, mask_info


@mcp.tool()
async def view_output_image(request: ViewOutputImageRequest, ctx: Context) -> ToolResult:
    """View a generated ComfyUI output as real image content for visual review.

    Use after queueing and waiting for completion, or whenever the user asks to
    inspect the latest result. The default selects the last image from the latest
    successful execution. Pass prompt_id, node_id, or output_index to choose a
    specific result. The returned image is visible to vision-capable MCP clients,
    so inspect its pixels rather than relying only on filenames or execution status.
    """
    await _report_tool_activity(ctx, "view_output_image")
    comfy_tools = get_comfy_tools()
    if request.prompt_id:
        prompt_id = request.prompt_id
        history_entry = await comfy_tools.fetch_history(prompt_id=prompt_id)
    else:
        prompt_id = None
        history_entry = None
        history = await comfy_tools.fetch_history(max_items=20)
        for candidate_prompt_id, candidate_entry in history.items():
            status = candidate_entry.get("status", {})
            candidate_images = _output_image_candidates(
                candidate_entry.get("outputs", {}),
                request.node_id,
            )
            if status.get("status_str") == "success" and candidate_images:
                prompt_id = candidate_prompt_id
                history_entry = candidate_entry
                break

    if not history_entry or not prompt_id:
        result = {
            "success": False,
            "error": "No matching completed execution with image outputs was found.",
        }
        return ToolResult(content=result, structured_content=result)

    status = history_entry.get("status", {})
    if status.get("status_str") != "success":
        result = {
            "success": False,
            "promptId": prompt_id,
            "status": status.get("status_str", "unknown"),
            "error": "The execution has not completed successfully.",
        }
        return ToolResult(content=result, structured_content=result)

    images = _output_image_candidates(
        history_entry.get("outputs", {}),
        request.node_id,
    )
    if not images:
        result = {
            "success": False,
            "promptId": prompt_id,
            "error": "The execution completed but has no matching image outputs.",
        }
        return ToolResult(content=result, structured_content=result)
    if request.output_index >= len(images):
        result = {
            "success": False,
            "promptId": prompt_id,
            "error": f"output_index {request.output_index} is out of range.",
            "availableOutputCount": len(images),
        }
        return ToolResult(content=result, structured_content=result)

    selected_index = request.output_index if request.output_index >= 0 else len(images) - 1
    selected = images[selected_index]
    path = _resolve_comfy_image_path(comfy_tools, selected)
    preview, preview_format, original_size, preview_size = _output_image_preview(
        path,
        request.max_dimension,
    )
    relative_path = "/".join(filter(None, (
        selected["type"],
        selected["subfolder"],
        selected["filename"],
    )))
    result = {
        "success": True,
        "promptId": prompt_id,
        "status": "success",
        "selectedOutputIndex": selected_index,
        "availableOutputCount": len(images),
        "nodeId": selected["nodeId"],
        "nodeImageIndex": selected["nodeImageIndex"],
        "relativePath": relative_path,
        "image": {
            "filename": selected["filename"],
            "subfolder": selected["subfolder"],
            "type": selected["type"],
        },
        "originalSize": {"width": original_size[0], "height": original_size[1]},
        "previewSize": {"width": preview_size[0], "height": preview_size[1]},
        "message": "The generated image follows as visual MCP content. Inspect the pixels before judging the result.",
    }
    return ToolResult(
        content=[result, MCPImage(data=preview, format=preview_format)],
        structured_content=result,
    )


@mcp.tool()
async def view_chat_image(request: ViewChatImageRequest, ctx: Context) -> ToolResult:
    """View an image the user attached to Ren as real visual MCP content.

    Call this before describing, comparing, or editing an attached image. The
    image reference is supplied in the user's attachment context and always
    resolves inside ComfyUI's trusted Ren chat input folder.
    """
    await _report_tool_activity(ctx, "view_chat_image")
    image_ref = request.image.model_dump()
    path = _resolve_comfy_image_path(get_comfy_tools(), image_ref)
    preview, preview_format, original_size, preview_size = _output_image_preview(
        path,
        request.max_dimension,
    )
    result = {
        "success": True,
        "image": image_ref,
        "originalSize": {"width": original_size[0], "height": original_size[1]},
        "previewSize": {"width": preview_size[0], "height": preview_size[1]},
        "message": "The user's attached image follows as visual MCP content.",
    }
    return ToolResult(
        content=[result, MCPImage(data=preview, format=preview_format)],
        structured_content=result,
    )


@mcp.tool()
async def place_chat_image_in_node(
    request: PlaceChatImageInNodeRequest,
    ctx: Context,
) -> Dict[str, Any]:
    """Put a user-attached image into a Load Image-style canvas node.

    Omit node_id when the user selected exactly one compatible node. This only
    updates the node's image widget and visible preview; it never queues a run.
    """
    if not settings.enable_workflow_writes:
        return _disabled_by_config("FL_MCP_ENABLE_WORKFLOW_WRITES")
    return await _execute_tool(
        ctx,
        "place_chat_image_in_node",
        request.model_dump(),
    )

@mcp.tool()
async def get_execution_history(request: GetWorkflowHistoryRequest, ctx: Context) -> Dict[str, Any]:
    """Get workflow currently processing queue and history from ComfyUI.
    
    Retrieves execution history including status, errors, and outputs for workflows.
    Can fetch a specific workflow by prompt_id or recent history.
    
    For each workflow in history, you'll get:
    - status: "success", "error", or "running"
    - outputs: Generated images/files (if successful)
    - errors: Full error details with traceback (if failed)
    - prompt: The workflow that was executed
    
    Use this to:
    - Check if a workflow succeeded or failed
    - Get detailed error information for debugging
    - Retrieve outputs from successful workflows
    - Monitor recent workflow executions
    
    Returns:
        If prompt_id provided:
        {
            "prompt_id": str,
            "status": "success" | "error" | "unknown",
            "completed": bool,
            "outputs": {...},  # Only if successful
            "errors": [...],   # Only if failed, with full traceback
            "executed_nodes": [...],  # Nodes that ran successfully
            "prompt": {...}    # The workflow definition
        }
        
        If prompt_id not provided:
        {
            "history": {
                "prompt_id_1": {...},
                "prompt_id_2": {...},
                ...
            },
            "count": int,
            "total_items": int
        }
    """
    await _report_tool_activity(ctx, "get_workflow_history")
    
    try:
        comfy_tools = get_comfy_tools()
        
        if request.prompt_id:
            # Get specific workflow history
            history_entry = await comfy_tools.fetch_history(
                prompt_id=request.prompt_id
            )
            
            if not history_entry:
                return {
                    "prompt_id": request.prompt_id,
                    "status": "unknown",
                    "completed": False,
                    "message": "History not found - workflow may still be running or prompt_id is invalid"
                }
            
            # Parse the history entry
            status = history_entry.get("status", {})
            status_str = status.get("status_str", "unknown")
            completed = status.get("completed", False)
            
            result = {
                "prompt_id": request.prompt_id,
                "status": status_str,
                "completed": completed,
                "outputs": history_entry.get("outputs", {}),
                "prompt": history_entry.get("prompt", [])
            }
            
            # Add error details if failed
            if status_str == "error":
                errors = []
                messages = status.get("messages", [])
                
                for msg_type, msg_data in messages:
                    if msg_type == "execution_error":
                        error = {
                            "node_id": msg_data.get("node_id"),
                            "node_type": msg_data.get("node_type"),
                            "exception_type": msg_data.get("exception_type"),
                            "exception_message": msg_data.get("exception_message"),
                            "traceback": msg_data.get("traceback", []),
                            "current_inputs": msg_data.get("current_inputs", {}),
                            "timestamp": msg_data.get("timestamp")
                        }
                        errors.append(error)
                
                result["errors"] = errors
                result["error_count"] = len(errors)
                
                # Add executed nodes (nodes that ran before failure)
                if errors:
                    result["executed_nodes"] = errors[0].get("executed", [])
            
            # Add execution messages for all statuses
            result["messages"] = status.get("messages", [])
            
            return result
            
        else:
            # Get recent history
            history = await comfy_tools.fetch_history(max_items=request.max_items)
            
            # Parse each entry to add simplified status
            parsed_history = {}
            for prompt_id, entry in history.items():
                status = entry.get("status", {})
                status_str = status.get("status_str", "unknown")
                
                parsed_entry = {
                    "status": status_str,
                    "completed": status.get("completed", False),
                    "has_outputs": bool(entry.get("outputs")),
                    "has_errors": status_str == "error"
                }
                
                # Add error summary if failed
                if status_str == "error":
                    messages = status.get("messages", [])
                    for msg_type, msg_data in messages:
                        if msg_type == "execution_error":
                            parsed_entry["error_summary"] = {
                                "node_id": msg_data.get("node_id"),
                                "node_type": msg_data.get("node_type"),
                                "exception_message": msg_data.get("exception_message")
                            }
                            break
                
                parsed_history[prompt_id] = parsed_entry
            
            return {
                "history": parsed_history,
                "count": len(parsed_history),
                "total_items": len(history),
                "message": f"Retrieved {len(parsed_history)} recent workflow executions"
            }
            
    except ComfyUIError as e:
        logger.error(f"ComfyUI error in get_workflow_history: {e}")
        return {
            "success": False,
            "error": str(e),
            "prompt_id": request.prompt_id if request.prompt_id else None
        }
    except Exception as e:
        logger.error(f"Unexpected error in get_workflow_history: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "prompt_id": request.prompt_id if request.prompt_id else None
        }
        
@mcp.tool()
async def get_queue_status_details(request: GetQueueStatusDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get current ComfyUI queue status and active executions.
    
    Returns information about currently running and queued workflows,
    including execution progress and node tracking.
    """
    await _report_tool_activity(ctx, "get_queue_status_details")
    
    queue_status = manager.execution_tracker.get_queue_status()
    active_executions = manager.execution_tracker.get_all_executions()
    
    return {
        "queue": queue_status,
        "active_executions": active_executions,
        "execution_count": len(active_executions)
    }

@mcp.tool()
async def get_execution_details(request: GetExecutionDetailsRequest, ctx: Context) -> Dict[str, Any]:
    """Get detailed execution state for a specific workflow run.
    
    Provides comprehensive information about a workflow execution including
    current node, executed nodes, cached nodes, and status.
    """
    await _report_tool_activity(ctx, "get_execution_details")
    
    execution = manager.execution_tracker.get_execution_state(request.prompt_id)
    return {
        "prompt_id": request.prompt_id,
        "found": execution is not None,
        "execution": execution
    }

@mcp.tool()
async def clear_error_buffer(request: ClearErrorBufferRequest, ctx: Context) -> Dict[str, Any]:
    """Clear the error buffer.
    
    Removes all stored errors from the buffer. Use this to start fresh
    after fixing issues or when the buffer gets too cluttered.
    """
    await _report_tool_activity(ctx, "clear_error_buffer")
    
    previous_count = manager.error_buffer.get_count()
    manager.error_buffer.clear()
    return {
        "cleared": True,
        "previous_count": previous_count
    }


# ============================================================================
# REN CODING / CUSTOM NODE DEVELOPMENT TOOLS
# ============================================================================

def _coding_result(fn, *args, **kwargs) -> Dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            result.setdefault("success", True)
            return result
        return {"success": True, "result": result}
    except CodingToolError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Coding tool failed: {e}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e}"}


@mcp.tool()
async def custom_nodes_list_packs(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """List installed ComfyUI custom node packs and basic metadata."""
    await _report_tool_activity(ctx, "custom_nodes_list_packs")
    return _coding_result(coding_list_packs)


@mcp.tool()
async def custom_nodes_read_file(request: CustomNodesReadFileRequest, ctx: Context) -> Dict[str, Any]:
    """Read a bounded line range from a file under ComfyUI/custom_nodes. Paths outside custom_nodes are blocked."""
    await _report_tool_activity(ctx, "custom_nodes_read_file")
    return _coding_result(
        coding_read_file,
        request.path,
        request.max_chars,
        request.start_line,
        request.line_count,
    )


@mcp.tool()
async def custom_nodes_read_file_excerpt(request: CustomNodesReadFileRequest, ctx: Context) -> Dict[str, Any]:
    """Read a bounded excerpt from a custom node file. Prefer this for large files and follow-up ranges."""
    await _report_tool_activity(ctx, "custom_nodes_read_file_excerpt")
    return _coding_result(
        coding_read_file,
        request.path,
        request.max_chars,
        request.start_line,
        request.line_count,
    )


@mcp.tool()
async def custom_nodes_search(request: CustomNodesSearchRequest, ctx: Context) -> Dict[str, Any]:
    """Search text under ComfyUI/custom_nodes using ripgrep."""
    await _report_tool_activity(ctx, "custom_nodes_search")
    return _coding_result(coding_search, request.query, request.path, request.glob, request.max_results)


@mcp.tool()
async def custom_nodes_write_file(request: CustomNodesWriteFileRequest, ctx: Context) -> Dict[str, Any]:
    """Write a full file under ComfyUI/custom_nodes. Use carefully; existing files require overwrite=true."""
    await _report_tool_activity(ctx, "custom_nodes_write_file")
    if not settings.enable_custom_node_writes:
        return _disabled_by_config("FL_MCP_ENABLE_CUSTOM_NODE_WRITES")
    return _coding_result(coding_write_file, request.path, request.content, request.overwrite)


@mcp.tool()
async def custom_nodes_apply_patch(request: CustomNodesApplyPatchRequest, ctx: Context) -> Dict[str, Any]:
    """Apply a unified diff. Every touched path must remain inside ComfyUI/custom_nodes."""
    await _report_tool_activity(ctx, "custom_nodes_apply_patch")
    if not settings.enable_custom_node_writes:
        return _disabled_by_config("FL_MCP_ENABLE_CUSTOM_NODE_WRITES")
    return _coding_result(apply_unified_patch, request.patch)


@mcp.tool()
async def custom_nodes_create_pack(request: CustomNodesCreatePackRequest, ctx: Context) -> Dict[str, Any]:
    """Create a new ComfyUI custom node pack with a working starter node."""
    await _report_tool_activity(ctx, "custom_nodes_create_pack")
    if not settings.enable_custom_node_writes:
        return _disabled_by_config("FL_MCP_ENABLE_CUSTOM_NODE_WRITES")
    return _coding_result(
        coding_create_pack,
        request.name,
        node_class=request.node_class,
        display_name=request.display_name,
        category=request.category,
        overwrite=request.overwrite,
    )


@mcp.tool()
async def custom_nodes_validate_pack(request: CustomNodesPathRequest, ctx: Context) -> Dict[str, Any]:
    """Run Python compile validation for a custom node pack."""
    await _report_tool_activity(ctx, "custom_nodes_validate_pack")
    return _coding_result(coding_validate_pack, request.path)


@mcp.tool()
async def custom_nodes_git_status(request: CustomNodesPathRequest, ctx: Context) -> Dict[str, Any]:
    """Show git status for a path under custom_nodes."""
    await _report_tool_activity(ctx, "custom_nodes_git_status")
    return _coding_result(coding_git_status, request.path)


@mcp.tool()
async def custom_nodes_git_diff(request: CustomNodesPathRequest, ctx: Context) -> Dict[str, Any]:
    """Show git diff for a path under custom_nodes."""
    await _report_tool_activity(ctx, "custom_nodes_git_diff")
    return _coding_result(coding_git_diff, request.path)


@mcp.tool()
async def custom_nodes_git_commit(request: CustomNodesGitCommitRequest, ctx: Context) -> Dict[str, Any]:
    """Commit changes for a path under custom_nodes."""
    await _report_tool_activity(ctx, "custom_nodes_git_commit")
    if not settings.enable_git_writes:
        return _disabled_by_config("FL_MCP_ENABLE_GIT_WRITES")
    return _coding_result(coding_git_commit, request.path, request.message)


@mcp.tool()
async def custom_nodes_git_push(request: CustomNodesPathRequest, ctx: Context) -> Dict[str, Any]:
    """Push the git repo containing a path under custom_nodes."""
    await _report_tool_activity(ctx, "custom_nodes_git_push")
    if not settings.enable_git_writes:
        return _disabled_by_config("FL_MCP_ENABLE_GIT_WRITES")
    return _coding_result(coding_git_push, request.path)


@mcp.tool()
async def comfy_restart(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Restart a ComfyUI process managed by FL-MCP daemon mode."""
    await _report_tool_activity(ctx, "comfy_restart")
    if not settings.enable_comfy_process_control:
        return _disabled_by_config("FL_MCP_ENABLE_COMFY_PROCESS_CONTROL")
    return comfy_supervisor.restart()


@mcp.tool()
async def comfy_get_logs(request: ComfyLogsRequest, ctx: Context) -> Dict[str, Any]:
    """Read recent ComfyUI logs captured by FL-MCP daemon mode."""
    await _report_tool_activity(ctx, "comfy_get_logs")
    return comfy_supervisor.logs(limit=request.limit)


@mcp.tool()
async def comfy_status(request: GetSystemInfoRequest, ctx: Context) -> Dict[str, Any]:
    """Get ComfyUI process and HTTP reachability status."""
    await _report_tool_activity(ctx, "comfy_status")
    return comfy_supervisor.status()

    
# Other Ideas
#   (**DONE**) Meta-Awareness: Awareness of the full environment including installed plugins (this is through python I'm assuming!)
#   Workspace awareness: what tabs do you have? can we switch workflow tabs? etc. (from frontend then executed tools through here?)
#   Workflow awareness: list workflows, find workflows? or like rather, be pointed at a folder or workflow to load? loading, etc. stuff that's in the file menu?
#   (**DONE**) Node Search and Node Finding: What is already possible through comfy lib? It'd be nice to have tools for find_installed_node that lets us search over all nodes names, descriptions, etc.

async def _restrict_tools_from_environment() -> None:
    """Expose only the tools selected for an embedded chat run."""
    raw = os.getenv("FL_MCP_ALLOWED_TOOLS", "").strip()
    if not raw:
        return
    allowed = {name.strip() for name in raw.split(",") if name.strip()}
    if hasattr(mcp, "list_tools"):
        registered = await mcp.list_tools(run_middleware=False)
    else:
        registered = (await mcp.get_tools()).values()
    removed = 0
    for tool in registered:
        if tool.name not in allowed:
            mcp.remove_tool(tool.name)
            removed += 1
    logger.info(
        "[MCP] Restricted embedded tool surface to %s tools (%s removed)",
        len(allowed),
        removed,
    )


def main():
    asyncio.run(_restrict_tools_from_environment())
    mcp.run()
    
if __name__ == "__main__":
    main()
