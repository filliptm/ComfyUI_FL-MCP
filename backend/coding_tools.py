"""Scoped coding helpers for Ren custom node development."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


COMFY_ROOT = Path(__file__).resolve().parents[3]
CUSTOM_NODES_ROOT = COMFY_ROOT / "custom_nodes"
REN_REPO_ROOT = Path(__file__).resolve().parents[1]

READ_DEFAULT_CHARS = 12000
READ_MAX_CHARS = 24000
READ_DEFAULT_LINES = 240
READ_MAX_LINES = 800
SEARCH_MAX_RESULTS = 80
SEARCH_MAX_LINE_CHARS = 600
COMMAND_OUTPUT_CHARS = 12000
LONG_LINE_CHARS = 1000


class CodingToolError(RuntimeError):
    pass


def _resolve_custom_path(path: str = ".") -> Path:
    raw = Path(path).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (CUSTOM_NODES_ROOT / raw).resolve()
    try:
        candidate.relative_to(CUSTOM_NODES_ROOT.resolve())
    except ValueError as exc:
        raise CodingToolError(f"Path is outside custom_nodes: {path}") from exc
    return candidate


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(COMFY_ROOT.resolve()))
    except ValueError:
        return str(path)


def _chunk_long_lines(text: str, limit: int = LONG_LINE_CHARS) -> str:
    chunks: List[str] = []
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        if len(body) <= limit:
            chunks.append(line)
            continue
        for index in range(0, len(body), limit):
            suffix = "\n" if index + limit < len(body) else newline
            chunks.append(body[index:index + limit] + suffix)
    return "".join(chunks)


def _bounded_text(text: str, max_chars: int) -> Dict[str, Any]:
    max_chars = max(1, min(int(max_chars), READ_MAX_CHARS))
    separated = _chunk_long_lines(text)
    truncated = len(separated) > max_chars
    returned = separated[:max_chars]
    if truncated:
        returned = returned.rstrip() + (
            f"\n\n[Ren truncated output at {max_chars} characters; "
            "request a narrower line range or search if more context is needed.]"
        )
    return {
        "text": returned,
        "truncated": truncated,
        "charsReturned": len(returned),
        "originalChars": len(text),
    }


def _bounded_command_text(text: str) -> str:
    bounded = _bounded_text(text, COMMAND_OUTPUT_CHARS)
    return str(bounded["text"])


def _run(cmd: List[str], cwd: Path, timeout: int = 120) -> Dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "command": cmd,
        "cwd": str(cwd),
        "returnCode": proc.returncode,
        "stdout": _bounded_command_text(proc.stdout),
        "stderr": _bounded_command_text(proc.stderr),
        "success": proc.returncode == 0,
    }


def list_packs() -> Dict[str, Any]:
    packs = []
    if CUSTOM_NODES_ROOT.exists():
        for child in sorted(CUSTOM_NODES_ROOT.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            packs.append({
                "name": child.name,
                "path": _rel(child),
                "hasInit": (child / "__init__.py").exists(),
                "hasRequirements": (child / "requirements.txt").exists(),
                "isGitRepo": (child / ".git").exists(),
            })
    return {"customNodesRoot": str(CUSTOM_NODES_ROOT), "packs": packs}


def read_file(
    path: str,
    max_chars: int = READ_DEFAULT_CHARS,
    start_line: int = 1,
    line_count: Optional[int] = READ_DEFAULT_LINES,
) -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    if not target.exists() or not target.is_file():
        raise CodingToolError(f"File not found: {path}")
    max_chars = max(1, min(int(max_chars), READ_MAX_CHARS))
    start_line = max(1, int(start_line))
    if line_count is None:
        line_count = READ_DEFAULT_LINES
    line_count = max(1, min(int(line_count), READ_MAX_LINES))

    content = target.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines(keepends=True)
    start_index = min(start_line - 1, len(lines))
    end_index = min(start_index + line_count, len(lines))
    selected = "".join(lines[start_index:end_index])
    bounded = _bounded_text(selected, max_chars)
    truncated = bool(bounded["truncated"]) or end_index < len(lines)
    return {
        "path": _rel(target),
        "content": bounded["text"],
        "truncated": truncated,
        "size": target.stat().st_size,
        "startLine": start_index + 1 if lines else 1,
        "endLine": end_index,
        "lineCount": end_index - start_index,
        "totalLines": len(lines),
        "maxChars": max_chars,
    }


def write_file(path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    if target.exists() and not overwrite:
        raise CodingToolError(f"File already exists. Set overwrite=true to replace: {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": _rel(target), "bytes": target.stat().st_size}


def search(query: str, path: str = ".", glob: Optional[str] = None, max_results: int = 100) -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    if not target.exists():
        raise CodingToolError(f"Search path not found: {path}")
    max_results = max(1, min(int(max_results), SEARCH_MAX_RESULTS))
    rg = shutil.which("rg")
    if not rg:
        raise CodingToolError("ripgrep is not installed")
    cmd = [rg, "--line-number", "--no-heading", "--color", "never"]
    if glob:
        cmd.extend(["--glob", glob])
    cmd.extend([query, str(target)])
    result = _run(cmd, COMFY_ROOT, timeout=60)
    raw_lines = [line for line in result["stdout"].splitlines() if line]
    lines = []
    for line in raw_lines[:max_results]:
        if len(line) > SEARCH_MAX_LINE_CHARS:
            line = line[:SEARCH_MAX_LINE_CHARS].rstrip() + " [line truncated]"
        lines.append(line)
    return {
        "success": result["returnCode"] in (0, 1),
        "query": query,
        "path": _rel(target),
        "matches": lines,
        "truncated": len(raw_lines) > max_results,
        "maxResults": max_results,
        "maxLineChars": SEARCH_MAX_LINE_CHARS,
        "stderr": result["stderr"],
    }


def _patch_paths(patch: str) -> List[Path]:
    paths: List[Path] = []
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ")):
            raw = line[4:].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            paths.append((COMFY_ROOT / raw).resolve())
    return paths


def apply_unified_patch(patch: str) -> Dict[str, Any]:
    touched = _patch_paths(patch)
    if not touched:
        raise CodingToolError("Patch does not include any file paths")
    root = CUSTOM_NODES_ROOT.resolve()
    for path in touched:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise CodingToolError(f"Patch touches path outside custom_nodes: {_rel(path)}") from exc

    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=str(COMFY_ROOT),
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check.returncode != 0:
        return {
            "success": False,
            "stage": "check",
            "stdout": _bounded_command_text(check.stdout),
            "stderr": _bounded_command_text(check.stderr),
            "touched": [_rel(path) for path in touched],
        }

    apply = subprocess.run(
        ["git", "apply", "-"],
        cwd=str(COMFY_ROOT),
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return {
        "success": apply.returncode == 0,
        "stage": "apply",
        "stdout": _bounded_command_text(apply.stdout),
        "stderr": _bounded_command_text(apply.stderr),
        "touched": [_rel(path) for path in touched],
    }


def _safe_pack_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    if not safe:
        raise CodingToolError("Pack name is required")
    if not safe.startswith("ComfyUI_") and not safe.startswith("ComfyUI-"):
        safe = f"ComfyUI_{safe}"
    return safe


def create_pack(
    name: str,
    *,
    node_class: str = "RenExampleNode",
    display_name: str = "Ren Example Node",
    category: str = "Ren",
    overwrite: bool = False,
) -> Dict[str, Any]:
    pack_name = _safe_pack_name(name)
    pack_dir = _resolve_custom_path(pack_name)
    if pack_dir.exists() and not overwrite:
        raise CodingToolError(f"Pack already exists: {pack_name}")
    pack_dir.mkdir(parents=True, exist_ok=True)

    node_class = re.sub(r"[^A-Za-z0-9_]", "", node_class) or "RenExampleNode"
    files = {
        "__init__.py": f'''from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
''',
        "nodes.py": f'''class {node_class}:
    @classmethod
    def INPUT_TYPES(cls):
        return {{
            "required": {{
                "text": ("STRING", {{"default": "hello from Ren", "multiline": True}}),
            }}
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "run"
    CATEGORY = "{category}"

    def run(self, text):
        return (text,)


NODE_CLASS_MAPPINGS = {{
    "{node_class}": {node_class},
}}

NODE_DISPLAY_NAME_MAPPINGS = {{
    "{node_class}": "{display_name}",
}}
''',
        "README.md": f'''# {pack_name}

Generated by Ren.

## Nodes

- {display_name}
''',
        "requirements.txt": "",
    }
    written = []
    for relative, content in files.items():
        target = pack_dir / relative
        if target.exists() and not overwrite:
            continue
        target.write_text(content, encoding="utf-8")
        written.append(_rel(target))
    return {"pack": pack_name, "path": _rel(pack_dir), "written": written}


def validate_pack(path: str) -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    if not target.exists() or not target.is_dir():
        raise CodingToolError(f"Pack folder not found: {path}")
    return _run([os.sys.executable, "-m", "compileall", "-q", str(target)], COMFY_ROOT, timeout=120)


def git_status(path: str = ".") -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    cwd = target if target.is_dir() else target.parent
    return _run(["git", "status", "--short"], cwd, timeout=60)


def git_diff(path: str = ".") -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    cwd = target if target.is_dir() else target.parent
    return _run(["git", "diff", "--", str(target)], COMFY_ROOT, timeout=60)


def git_commit(path: str, message: str) -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    add = _run(["git", "add", str(target)], COMFY_ROOT, timeout=60)
    if not add["success"]:
        return add
    return _run(["git", "commit", "-m", message], COMFY_ROOT, timeout=120)


def git_push(path: str = ".") -> Dict[str, Any]:
    target = _resolve_custom_path(path)
    cwd = target if target.is_dir() else target.parent
    return _run(["git", "push"], cwd, timeout=180)
