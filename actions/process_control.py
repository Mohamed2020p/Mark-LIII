"""Explicit process inspection and termination for the current desktop user.

Process termination is deliberately separate from ordinary computer control:
listing and inspection are read-only, while terminating a process requires the
HUD confirmation gate.  The action never elevates privileges and refuses to
stop JARVIS, core operating-system processes, or processes owned by another
user.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import time
from typing import Any

from core import confirm as confirm_gate

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - exercised on minimal installations
    psutil = None

_CURRENT_USER = getpass.getuser().casefold()
_CURRENT_PID = os.getpid()

# These names are deliberately conservative.  The user can still terminate an
# ordinary desktop shell after the warning and confirmation; these are the
# processes whose loss can crash the OS, sever the session, or kill JARVIS.
_PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "startmenuexperiencehost.exe",
    "idle", "init", "systemd", "kthreadd", "launchd", "kernel_task",
    "windowserver", "windowserver.exe", "loginwindow", "dbus-daemon",
    "systemd-journald", "systemd-logind", "systemd-udevd", "udevd", "journald",
    "networkmanager", "sshd", "cron", "crond", "rsyslogd", "dockerd", "containerd",
    "jarvis", "mark-liii",
}
_PROTECTED_SUBSTRINGS = (
    "systemd-", "kernel thread", "jbd2/", "kworker/",
)


def _available() -> bool:
    return psutil is not None


def _clean_user(value: Any) -> str:
    return str(value or "").strip()


def _same_user(username: str) -> bool:
    if not username:
        return True
    normalized = username.casefold()
    return normalized == _CURRENT_USER or normalized.rsplit("\\", 1)[-1] == _CURRENT_USER


def _protected_reason(pid: int, name: str, username: str) -> str:
    normalized = name.casefold().strip()
    if pid == _CURRENT_PID:
        return "JARVIS itself"
    if pid in (0, 1):
        return "the operating-system init/idle process"
    if normalized in _PROTECTED_NAMES or any(part in normalized for part in _PROTECTED_SUBSTRINGS):
        return "a core operating-system/service process"
    if username and not _same_user(username):
        return "a process owned by another user"
    if not username:
        return "the process owner could not be verified"
    return ""


def _importance(pid: int, name: str, username: str, status: str) -> tuple[str, str, bool]:
    reason = _protected_reason(pid, name, username)
    if reason:
        return "protected", reason, False

    normalized = name.casefold()
    if normalized in {"explorer.exe", "explorer", "gnome-shell", "plasmashell", "dock"}:
        return "important desktop shell", "stopping it may hide or reset the desktop", True
    if normalized in {"xorg", "xorg.bin", "wayland", "com.apple.windowserver"}:
        return "important display server", "stopping it may end the graphical session", True
    if status in {"zombie", "dead"}:
        return "stale process", "already exiting", True
    return "user process", "no core-system protection flag detected", True


_SENSITIVE_ARGUMENT = re.compile(
    r"(?i)(--?(?:api[-_]?key|access[-_]?token|auth(?:orization)?|password|passwd|secret|token)(?:=|\s+))([^\s]+)"
)

def _redact_command(command: str) -> str:
    # Command lines are useful for identifying a process, but they can contain
    # API keys/passwords. Never echo those values to the model or HUD.
    command = _SENSITIVE_ARGUMENT.sub(r"\1<redacted>", command)
    return re.sub(r"(?i)(https?://[^:/\s]+:)[^@\s]+(@)", r"\1<redacted>\2", command)


def _call(proc: Any, method: str, default: Any = None) -> Any:
    try:
        value = getattr(proc, method)
        return value() if callable(value) else value
    except Exception:
        return default


def _info(proc: Any, cpu: float | None = None) -> dict[str, Any] | None:
    try:
        pid = int(proc.pid)
    except Exception:
        return None

    name = _clean_user(_call(proc, "name", "<unknown>")) or "<unknown>"
    username = _clean_user(_call(proc, "username", ""))
    status = _clean_user(_call(proc, "status", "")) or "unknown"
    cmdline = _call(proc, "cmdline", []) or []
    if isinstance(cmdline, str):
        command = cmdline
    else:
        command = " ".join(str(part) for part in cmdline)
    command = _redact_command(command)[:360]
    importance, reason, killable = _importance(pid, name, username, status)
    if cpu is None:
        raw_cpu = _call(proc, "cpu_percent", 0.0)
        try:
            cpu = float(raw_cpu or 0.0)
        except (TypeError, ValueError):
            cpu = 0.0
    raw_memory = _call(proc, "memory_percent", 0.0)
    try:
        memory = float(raw_memory or 0.0)
    except (TypeError, ValueError):
        memory = 0.0

    return {
        "pid": pid,
        "name": name,
        "username": username or "unknown",
        "status": status,
        "cpu_percent": round(max(0.0, cpu), 1),
        "memory_percent": round(max(0.0, memory), 1),
        "command": command,
        "create_time": _call(proc, "create_time", None),
        "importance": importance,
        "reason": reason,
        "killable": killable,
        "process": proc,
    }


def _snapshot(sample: bool = True) -> list[dict[str, Any]]:
    if not _available():
        return []

    try:
        processes = list(psutil.process_iter())
    except Exception:
        return []

    if sample:
        for proc in processes:
            try:
                proc.cpu_percent(None)
            except Exception:
                pass
        # A short sample gives useful per-process CPU values without creating a
        # long blocking action.  No process is touched during this read phase.
        time.sleep(0.20)

    rows: list[dict[str, Any]] = []
    for proc in processes:
        cpu = None
        if sample:
            try:
                cpu = float(proc.cpu_percent(None) or 0.0)
            except Exception:
                cpu = 0.0
        row = _info(proc, cpu)
        if row is not None:
            rows.append(row)
    return rows


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in {"process", "create_time"}}


def _line(row: dict[str, Any]) -> str:
    command = row.get("command") or "-"
    return (
        f"PID {row['pid']} | CPU {row['cpu_percent']:.1f}% | RAM {row['memory_percent']:.1f}% | "
        f"{row['name']} | {row['importance']} | {row['username']} | {command[:180]}"
    )


def _not_available() -> str:
    return "Process control requires psutil. Install the project dependencies with the user's approval first."


def _find_by_pid(pid: int) -> tuple[dict[str, Any] | None, str]:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None, f"No process with PID {pid} exists."
    except psutil.AccessDenied:
        return None, f"Access denied while inspecting PID {pid}; no privilege escalation was attempted."
    row = _info(proc)
    return (row, "") if row else (None, f"Could not inspect PID {pid}.")


def _resolve_target(parameters: dict, *, sample: bool = True) -> tuple[dict[str, Any] | None, str]:
    pid_value = parameters.get("pid")
    target = str(
        parameters.get("target")
        or parameters.get("selector")
        or parameters.get("name")
        or ""
    ).strip()

    if pid_value not in (None, ""):
        try:
            return _find_by_pid(int(pid_value))
        except (TypeError, ValueError):
            return None, f"Invalid PID: {pid_value}"

    if target.isdigit():
        return _find_by_pid(int(target))

    if not target:
        return None, "Specify an exact PID or process name; for the busiest process use target=highest_cpu."

    target_key = re.sub(r"[\s_-]+", " ", target.casefold()).strip()
    if target_key in {
        "highest cpu", "top cpu", "most cpu", "cpu",
        "using most cpu", "process using most cpu",
        "the process using the most cpu", "highest cpu process",
        "most cpu process", "process with the highest cpu",
    }:
        rows = _snapshot(sample=sample)
        if not rows:
            return None, "No accessible processes were found."
        rows.sort(key=lambda row: row["cpu_percent"], reverse=True)
        top = rows[0]
        if not top["killable"]:
            candidate = next((row for row in rows if row["killable"]), None)
            extra = f" Highest non-protected process: {_line(candidate)}" if candidate else ""
            return None, (
                f"The highest-CPU process is protected ({_line(top)}). I will not stop it."
                + extra
            )
        return top, ""

    rows = _snapshot(sample=sample)
    needle = target.casefold()
    matches = [
        row for row in rows
        if needle == row["name"].casefold()
        or needle in row["name"].casefold()
        or needle in row["command"].casefold()
    ]
    if not matches:
        return None, f"No accessible process matched: {target}"
    matches.sort(key=lambda row: row["cpu_percent"], reverse=True)
    if len(matches) > 1:
        return None, (
            f"More than one process matched '{target}'. Specify a PID; I will not guess:\n"
            + "\n".join(_line(row) for row in matches[:12])
        )
    return matches[0], ""


def _overview() -> str:
    if not _available():
        return _not_available()
    try:
        memory = psutil.virtual_memory()
        return (
            f"OS: {platform.platform()}\n"
            f"Machine: {platform.machine()}\n"
            f"User: {_CURRENT_USER}\n"
            f"CPU logical/physical: {psutil.cpu_count()} / {psutil.cpu_count(logical=False)}\n"
            f"Memory: {memory.used / 1024**3:.1f} / {memory.total / 1024**3:.1f} GB ({memory.percent:.1f}%)\n"
            f"Jarvis PID: {_CURRENT_PID}\n"
            f"Boot time: {time.ctime(psutil.boot_time())}"
        )
    except Exception as exc:
        return f"System overview failed: {exc}"


def _settings() -> str:
    """Report non-secret runtime settings, never credential values."""
    try:
        from memory.config_manager import load_api_keys
        config = load_api_keys()
        safe_keys = sorted(
            key for key in config
            if not any(secret in key.casefold() for secret in ("key", "token", "password", "secret"))
        )
        configured = ", ".join(safe_keys) if safe_keys else "none"
    except Exception:
        configured = "unavailable"

    lines = [
        f"Platform: {platform.system()} {platform.release()}",
        f"Configured non-secret settings: {configured}",
        "Credential values: hidden",
        f"Current user: {_CURRENT_USER}",
        f"JARVIS PID: {_CURRENT_PID}",
    ]
    try:
        import pyautogui
        width, height = pyautogui.size()
        lines.append(f"Primary display: {width}x{height}")
    except Exception:
        lines.append("Primary display: unavailable")
    return "\n".join(lines)


def _stop_after_confirmation(row: dict[str, Any], operation: str) -> str:
    proc = row["process"]
    pid = row["pid"]
    expected_start = row.get("create_time")
    try:
        current = psutil.Process(pid)
        current_start = _call(current, "create_time", None)
        if expected_start is None or current_start is None:
            return f"Could not verify PID {pid} identity; nothing was stopped."
        if abs(float(expected_start) - float(current_start)) > 0.01:
            return f"PID {pid} now belongs to a different process; nothing was stopped."
        latest = _info(current)
        if not latest:
            return f"Could not re-check PID {pid}; nothing was stopped."
        if not latest["killable"]:
            return f"Refused to stop PID {pid}: it is now protected ({latest['reason']})."

        current.terminate()
        try:
            current.wait(timeout=3)
            return f"Stopped PID {pid} ({latest['name']}) gracefully."
        except psutil.TimeoutExpired:
            if operation != "kill":
                return f"PID {pid} did not exit within 3 seconds; it was not force-killed."
            current.kill()
            current.wait(timeout=3)
            return f"Force-stopped PID {pid} ({latest['name']}) after graceful termination timed out."
    except psutil.NoSuchProcess:
        return f"PID {pid} already exited; no action was needed."
    except psutil.AccessDenied:
        return f"Access denied while stopping PID {pid}; no privilege escalation was attempted."
    except Exception as exc:
        return f"Could not stop PID {pid}: {exc}"


def process_control(parameters: dict, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "top_cpu")).strip().lower()
    if not _available():
        return _not_available()

    if operation in {"overview", "system_info", "system"}:
        return _overview()
    if operation in {"settings", "system_settings"}:
        return _settings()

    if operation in {"list", "top_cpu", "top", "processes", "top_memory", "memory", "ram"}:
        rows = _snapshot(sample=True)
        if not rows:
            return "No accessible processes were found."
        if operation in {"top_memory", "memory", "ram"}:
            metric = "memory_percent"
        elif operation == "list" and str(params.get("sort_by", "cpu")).casefold() in {"memory", "ram", "mem"}:
            metric = "memory_percent"
        else:
            metric = "cpu_percent"
        rows.sort(key=lambda row: row[metric], reverse=True)
        try:
            limit = max(1, min(int(params.get("limit", 10)), 25))
        except (TypeError, ValueError):
            limit = 10
        heading = "Top processes by RAM:" if metric == "memory_percent" else (
            "Top processes by CPU:" if operation != "list" else "Processes by current CPU usage:"
        )
        return heading + "\n" + "\n".join(_line(row) for row in rows[:limit])

    if operation in {"inspect", "info", "details"}:
        row, error = _resolve_target(params, sample=False)
        if row is None:
            return error
        return "Process details:\n" + "\n".join(f"{key}: {value}" for key, value in _public(row).items())

    if operation in {"kill", "terminate", "stop", "end"}:
        row, error = _resolve_target(params, sample=True)
        if row is None:
            return error
        if not row["killable"]:
            return f"I will not stop this protected process: {_line(row)} ({row['reason']})."
        if confirm_gate.pending_title():
            return "There is already a confirmation waiting on screen. Ask the user to answer it first."

        mode = "kill" if operation == "kill" else "terminate"
        title = f"Stop process {row['name']} (PID {row['pid']})"
        detail = (
            f"{_line(row)}\n\n"
            f"Importance assessment: {row['importance']} — {row['reason']}.\n"
            + ("JARVIS will try graceful termination, then force-stop only if you explicitly requested kill."
               if mode == "kill" else
               "JARVIS will request graceful termination and will not force-kill it.")
        )
        return confirm_gate.request(
            key=f"process-{row['pid']}",
            title=title,
            detail=detail,
            run=lambda r=row, m=mode: _stop_after_confirmation(r, m),
        )

    return (
        "Unknown process operation. Use overview, settings, list, top_cpu, top_memory, inspect, "
        "terminate, or kill."
    )


TOOL = {
    "name": "process_control",
    "description": (
        "Inspects current processes and system information, reporting CPU/RAM usage, owner, "
        "status, command, and a JARVIS/core-service/desktop-shell/user-process assessment. "
        "It can compare list/top CPU/RAM processes, inspect a PID, and stop an explicitly "
        "requested process. Termination is always confirmation-protected, refuses "
        "JARVIS/core OS/other-user processes, never elevates "
        "privileges, and re-checks the PID before acting. Use target=highest_cpu for the "
        "highest CPU process; if that process is protected, explain why instead of guessing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "overview | settings | list | top_cpu | top_memory | inspect | terminate | kill",
            },
            "target": {
                "type": "STRING",
                "description": "Process name, PID, or highest_cpu; required for targeted stop",
            },
            "selector": {
                "type": "STRING",
                "description": "Alias for target, such as highest_cpu",
            },
            "pid": {
                "type": "INTEGER",
                "description": "Exact process ID for inspect or stop",
            },
            "name": {
                "type": "STRING",
                "description": "Exact or partial process name; ambiguous names are never guessed",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Number of rows for list/top_cpu/top_memory, maximum 25",
            },
            "sort_by": {
                "type": "STRING",
                "description": "cpu or memory for list comparisons",
            },
        },
        "required": ["operation"],
    },
    "handler": process_control,
}
