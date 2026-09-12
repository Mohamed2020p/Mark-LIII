"""Explicit command execution for the paired desktop.

This tool is deliberately separate from mouse/browser actions: the model must
have an exact command from the user's current request. Arbitrary commands stay
behind the interface confirmation gate, while ordinary computer controls such
as volume, opening apps, and browser navigation remain direct.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from core import confirm as confirm_gate

_OS = platform.system()
_MAX_OUTPUT = 12_000
_MAX_TIMEOUT = 300


def _windows_command(command: str) -> list[str]:
    return ["cmd.exe", "/d", "/s", "/c", command]


def _unix_command(command: str) -> list[str]:
    return ["bash", "-lc", command]


def _command_argv(command: str) -> list[str]:
    return _windows_command(command) if _OS == "Windows" else _unix_command(command)


def _resolve_cwd(raw: str) -> Path:
    if not raw.strip():
        return Path.home()
    cwd = Path(raw).expanduser().resolve()
    if not cwd.exists() or not cwd.is_dir():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")
    return cwd


def _run_command(command: str, cwd: Path, timeout: int, background: bool) -> str:
    argv = _command_argv(command)
    hide = {"creationflags": subprocess.CREATE_NO_WINDOW} if _OS == "Windows" else {}

    if background:
        flags = dict(hide)
        if _OS == "Windows":
            flags["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            flags["start_new_session"] = True
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **flags,
        )
        return f"Command started in background (PID {process.pid}): {command}"

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            **hide,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"
    except OSError as exc:
        return f"Could not start command: {exc}"

    output = (completed.stdout or "") + (completed.stderr or "")
    output = output.strip()
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n... [output clipped]"
    status = (
        "completed successfully"
        if completed.returncode == 0
        else f"exited with code {completed.returncode}"
    )
    return (
        f"Command {status} in {cwd}: {command}\n"
        f"{output or '(no output)'}"
    )


def command_control(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    command = str(params.get("command", "")).strip()
    if not command:
        return "No command provided."
    if "\x00" in command:
        return "Command rejected: it contains a null byte."

    try:
        cwd = _resolve_cwd(str(params.get("cwd", "")))
        timeout = max(1, min(int(params.get("timeout", 30)), _MAX_TIMEOUT))
        raw_background = params.get("background", False)
        background = (
            raw_background is True
            or str(raw_background).strip().lower() in {"1", "true", "yes", "on"}
        )
    except (TypeError, ValueError, OSError) as exc:
        return f"Command parameters are invalid: {exc}"

    detail = (
        f"Working directory: {cwd}\n"
        f"Command: {command}\n"
        f"Mode: {'background' if background else 'wait for completion'}"
    )

    def run() -> str:
        result = _run_command(command, cwd, timeout, background)
        if player:
            try:
                player.write_log(f"[command] {result[:240]}")
            except Exception:
                pass
        return result

    # A shell command can delete files, alter services, install software, or
    # change networking. The exact user request gets executed, but only after
    # the trusted desktop/dashboard button resolves this pending operation.
    if confirm_gate.pending_title():
        return (
            "There is already a confirmation waiting on screen. "
            "Ask the user to answer that one first."
        )
    return confirm_gate.request(
        key="command",
        title="Execute a desktop command",
        detail=detail,
        run=run,
    )


TOOL = {
    "name": "command_control",
    "description": (
        "Executes an exact Windows command on the paired desktop when the user "
        "explicitly asks to run it. Supports a working directory, timeout, "
        "captured output, and optional background mode. Use open_app for apps, "
        "browser_control for websites/localhost, and computer_settings for "
        "volume or ordinary OS controls. Arbitrary shell commands remain "
        "confirmation-protected."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "The exact command the user explicitly requested",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory for the command (default: user home)",
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Wait timeout in seconds, maximum 300",
            },
            "background": {
                "type": "BOOLEAN",
                "description": "Start and detach instead of waiting for output",
            },
        },
        "required": ["command"],
    },
    "handler": command_control,
}
