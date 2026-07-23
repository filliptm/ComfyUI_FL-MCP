"""Claude Code subscription discovery and official CLI login helpers."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from typing import Any

AUTH_TIMEOUT_SECONDS = 8
STATUS_CACHE_SECONDS = 5


class ClaudeSubscriptionService:
    """Use Claude Code's own credential store without reading or copying tokens."""

    def __init__(self) -> None:
        self._cached_status: dict[str, Any] | None = None
        self._cached_at = 0.0

    @staticmethod
    def cli_path() -> str | None:
        return shutil.which("claude")

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
                "source": "claude_cli",
                "installed": False,
                "authenticated": False,
                "authMethod": None,
                "subscriptionType": None,
                "version": None,
                "message": "Claude Code is not installed or is not on PATH.",
            }
            self._remember(value)
            return dict(value)

        auth_payload: dict[str, Any] = {}
        auth_error: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                cli,
                "auth",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=AUTH_TIMEOUT_SECONDS,
            )
            if process.returncode == 0:
                parsed = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    auth_payload = parsed
            else:
                auth_error = "Claude Code is not signed in."
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            auth_error = "Claude Code authentication check timed out."
        except (OSError, json.JSONDecodeError):
            auth_error = "Claude Code authentication status could not be read."

        version = await self._version(cli)
        authenticated = bool(auth_payload.get("loggedIn"))
        auth_method = (
            str(auth_payload.get("authMethod"))
            if auth_payload.get("authMethod")
            else None
        )
        subscription_type = (
            str(auth_payload.get("subscriptionType"))
            if auth_payload.get("subscriptionType")
            else None
        )
        subscription_auth = authenticated and auth_method == "claude.ai"
        if subscription_auth:
            label = subscription_type.title() if subscription_type else "Claude"
            message = f"Claude Code is signed in with a {label} subscription."
        elif authenticated:
            message = (
                "Claude Code is signed in, but not with Claude.ai subscription credentials."
            )
        else:
            message = auth_error or "Claude Code is not signed in."

        value = {
            "configured": subscription_auth,
            "source": "claude_cli",
            "installed": True,
            "authenticated": authenticated,
            "authMethod": auth_method,
            "subscriptionType": subscription_type,
            "version": version,
            "message": message,
        }
        self._remember(value)
        return dict(value)

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
        """Open the official Claude Code login command in a visible terminal."""
        cli = self.cli_path()
        if not cli:
            raise RuntimeError("Claude Code is not installed or is not on PATH.")

        system = platform.system()
        command = f"{shlex.quote(cli)} auth login"
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
                ["cmd", "/c", "start", "", "cmd", "/k", f'"{cli}" auth login'],
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
                    "Open a terminal and run `claude auth login`, then refresh the status."
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
                "Open a terminal and run `claude auth login`, then refresh the status."
            )

        self._cached_status = None
        return {
            "launched": True,
            "message": "Claude Code login opened in a terminal.",
        }

    def _remember(self, value: dict[str, Any]) -> None:
        self._cached_status = dict(value)
        self._cached_at = time.monotonic()


claude_subscription = ClaudeSubscriptionService()
