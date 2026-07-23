"""Codex subscription discovery and official CLI login helpers."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from collections import Counter
from typing import Any

AUTH_TIMEOUT_SECONDS = 8
STATUS_CACHE_SECONDS = 5
MODEL_CATALOG_TIMEOUT_SECONDS = 12
MODEL_CATALOG_CACHE_SECONDS = 300


class CodexSubscriptionService:
    """Use Codex's own ChatGPT login without reading or copying its tokens."""

    def __init__(self) -> None:
        self._cached_status: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._cached_models: list[dict[str, str]] | None = None
        self._models_cached_at = 0.0

    @staticmethod
    def cli_path() -> str | None:
        return shutil.which("codex")

    async def status(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not refresh
            and self._cached_status is not None
            and now - self._cached_at < STATUS_CACHE_SECONDS
        ):
            return dict(self._cached_status)

        cli = self.cli_path()
        if not cli:
            value = {
                "configured": False,
                "source": "codex_cli",
                "installed": False,
                "authenticated": False,
                "authMethod": None,
                "subscriptionType": None,
                "version": None,
                "message": "Codex CLI is not installed or is not on PATH.",
            }
            self._remember(value)
            return dict(value)

        authenticated = False
        auth_method: str | None = None
        auth_error: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                cli,
                "login",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=AUTH_TIMEOUT_SECONDS,
            )
            status_text = "\n".join((
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )).strip()
            authenticated = process.returncode == 0
            if authenticated and "chatgpt" in status_text.lower():
                auth_method = "chatgpt"
            elif authenticated:
                auth_method = "api_key"
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            auth_error = "Codex authentication check timed out."
        except OSError:
            auth_error = "Codex authentication status could not be read."

        version = await self._version(cli)
        subscription_auth = authenticated and auth_method == "chatgpt"
        if subscription_auth:
            message = "Codex is signed in with a ChatGPT subscription."
        elif authenticated:
            message = (
                "Codex is signed in with an API key, not a ChatGPT subscription."
            )
        else:
            message = auth_error or "Codex is not signed in."

        value = {
            "configured": subscription_auth,
            "source": "codex_cli",
            "installed": True,
            "authenticated": authenticated,
            "authMethod": auth_method,
            "subscriptionType": "chatgpt" if subscription_auth else None,
            "version": version,
            "message": message,
        }
        self._remember(value)
        return dict(value)

    async def models(self, *, refresh: bool = False) -> list[dict[str, str]]:
        """Return the visible model catalog exposed by the installed Codex CLI."""
        now = time.monotonic()
        if (
            not refresh
            and self._cached_models is not None
            and now - self._models_cached_at < MODEL_CATALOG_CACHE_SECONDS
        ):
            return [dict(item) for item in self._cached_models]

        cli = self.cli_path()
        if not cli:
            return []

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                cli,
                "debug",
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
            )
            if process.returncode != 0:
                return []
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except (TimeoutError, OSError, json.JSONDecodeError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return []

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in payload.get("models", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            model_id = str(item.get("slug") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            label = str(item.get("display_name") or model_id).strip()
            candidate = {"id": model_id, "label": label}
            description = str(item.get("description") or "").strip()
            if description:
                candidate["description"] = description
            candidates.append(candidate)

        label_counts = Counter(item["label"].casefold() for item in candidates)
        for item in candidates:
            if label_counts[item["label"].casefold()] <= 1:
                continue
            if item["id"] == "codex-auto-review":
                suffix = "Auto Review"
            elif item["id"] == "gpt-5.6-terra":
                suffix = "Standard"
            else:
                suffix = item["id"]
            item["label"] = f"{item['label']} · {suffix}"

        self._cached_models = [dict(item) for item in candidates]
        self._models_cached_at = time.monotonic()
        return [dict(item) for item in candidates]

    async def _version(self, cli: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                cli,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=AUTH_TIMEOUT_SECONDS,
            )
            if process.returncode == 0:
                value = stdout.decode("utf-8", errors="replace").strip()
                return value or None
        except (TimeoutError, OSError):
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
        return None

    def launch_login(self) -> dict[str, Any]:
        """Open the official Codex ChatGPT login command in a visible terminal."""
        cli = self.cli_path()
        if not cli:
            raise RuntimeError("Codex CLI is not installed or is not on PATH.")

        system = platform.system()
        command = f"{shlex.quote(cli)} login"
        if system == "Darwin":
            script = (
                'tell application "Terminal"\n'
                "activate\n"
                f"do script {json.dumps(command)}\n"
                "end tell"
            )
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif system == "Windows":
            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                ["cmd", "/c", "start", "", "cmd", "/k", f'"{cli}" login'],
                creationflags=creation_flags,
            )
        elif system == "Linux":
            terminal = next(
                (
                    candidate
                    for candidate in (
                        "gnome-terminal",
                        "konsole",
                        "xfce4-terminal",
                        "xterm",
                    )
                    if shutil.which(candidate)
                ),
                None,
            )
            if not terminal or not os.getenv("DISPLAY"):
                raise RuntimeError(
                    "Open a terminal and run `codex login`, then refresh the status."
                )
            shell_command = f"{command}; exec {shlex.quote(os.getenv('SHELL', '/bin/sh'))}"
            commands = {
                "gnome-terminal": [terminal, "--", "sh", "-lc", shell_command],
                "konsole": [terminal, "-e", "sh", "-lc", shell_command],
                "xfce4-terminal": [terminal, "-e", f"sh -lc {shlex.quote(shell_command)}"],
                "xterm": [terminal, "-hold", "-e", "sh", "-lc", shell_command],
            }
            subprocess.Popen(commands[terminal])
        else:
            raise RuntimeError(
                "Open a terminal and run `codex login`, then refresh the status."
            )

        self._cached_status = None
        self._cached_models = None
        return {
            "launched": True,
            "message": "Codex login opened in a terminal.",
        }

    def _remember(self, value: dict[str, Any]) -> None:
        self._cached_status = dict(value)
        self._cached_at = time.monotonic()


codex_subscription = CodexSubscriptionService()
