"""Safe, explicit VS Code workspace operations.

The model remains responsible for deciding *what* the user asked for and for
writing code content.  This action supplies the reliable local workflow around
that request: resolve one workspace, inspect files, write reversible edits,
open VS Code, and run a requested test/build command.  It never uses a shell
string for commands and never trusts a model-supplied confirmation flag.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from core import confirm as confirm_gate
from core.undo import push_undo

_MAX_READ = 60_000
_MAX_TREE_ITEMS = 180
_MAX_SEARCH_MATCHES = 80
_MAX_UNDO_BYTES = 1_000_000
_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules", "__pycache__",
    ".venv", "venv", "env", "dist", "build", ".mypy_cache", ".pytest_cache",
}
_PROTECTED_OPS = {"run", "test", "build", "debug", "install", "delete"}


def _workspace(raw: str) -> Path:
    value = str(raw or "").strip()
    path = Path(value).expanduser() if value else Path.cwd()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _workspace_for(params: dict, operation: str) -> Path:
    raw_workspace = str(params.get("workspace_path", "")).strip()
    if raw_workspace:
        return _workspace(raw_workspace)

    # An explicitly supplied absolute file path is already a clear workspace
    # choice.  Deriving its parent keeps single-file commands convenient while
    # still confining relative paths to that parent.
    raw_path = str(params.get("path", "")).strip()
    if raw_path and Path(raw_path).expanduser().is_absolute() and operation not in {"open", "git_status"}:
        candidate = Path(raw_path).expanduser()
        return (candidate if candidate.is_dir() else candidate.parent).resolve()
    return Path.cwd().resolve()


def _inside(workspace: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def _target(workspace: Path, raw: str, *, required: bool = True) -> Path:
    value = str(raw or "").strip()
    if not value:
        if required:
            raise ValueError("A workspace-relative path is required.")
        return workspace
    target = Path(value).expanduser()
    if not target.is_absolute():
        target = workspace / target
    target = target.resolve()
    if not _inside(workspace, target):
        raise ValueError("Path escapes the requested workspace; operation refused.")
    return target


def _display(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace)) or "."
    except ValueError:
        return str(path)


def _tree(workspace: Path) -> str:
    if not workspace.exists():
        return f"Workspace not found: {workspace}"
    if not workspace.is_dir():
        return f"Workspace is not a folder: {workspace}"

    rows: list[str] = []
    for root, dirs, files in os.walk(workspace, topdown=True, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS and not d.startswith("."))
        files = sorted(f for f in files if not f.startswith("."))
        root_path = Path(root)
        for name in files:
            rows.append(_display(root_path / name, workspace))
            if len(rows) >= _MAX_TREE_ITEMS:
                rows.append("… (tree truncated)")
                return "\n".join(rows)
    return "\n".join(rows) if rows else "(workspace contains no visible source files)"


def _read(path: Path) -> str:
    if not path.exists():
        return f"File not found: {path}"
    if not path.is_file():
        return f"Not a file: {path}"
    if path.stat().st_size > _MAX_READ * 4:
        return f"File is too large to inspect safely: {path}"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:_MAX_READ]
    except Exception as exc:
        return f"Could not read {path}: {exc}"


def _search(workspace: Path, query: str, pattern: str = "") -> str:
    query = query.strip()
    if not query:
        return "Please provide search text."
    matches: list[str] = []
    for root, dirs, files in os.walk(workspace, topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
        for name in files:
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            path = Path(root) / name
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append(f"{_display(path, workspace)}:{number}: {line.strip()[:240]}")
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return "\n".join(matches) + "\n… (matches truncated)"
    return "\n".join(matches) if matches else f"No matches for: {query}"


def _open_vscode(workspace: Path) -> str:
    candidates = []
    code = shutil.which("code")
    if code:
        candidates.append([code, str(workspace)])
    if os.name == "nt":
        candidates.extend([
            [r"code.cmd", str(workspace)],
            [str(Path.home() / "AppData/Local/Programs/Microsoft VS Code/bin/code.cmd"), str(workspace)],
            [r"C:\Program Files\Microsoft VS Code\bin\code.cmd", str(workspace)],
        ])
    elif sys.platform == "darwin":
        candidates.append(["open", "-a", "Visual Studio Code", str(workspace)])

    for command in candidates:
        try:
            use_shell = os.name == "nt" and str(command[0]).lower().endswith((".cmd", ".bat"))
            subprocess.Popen(command, shell=use_shell, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened VS Code workspace: {workspace}"
        except (FileNotFoundError, OSError):
            continue

    # Reuse the existing app launcher as a last visible fallback. It cannot pass
    # the folder on some desktops, so report that honestly.
    try:
        from actions.open_app import open_app
        result = open_app(parameters={"app_name": "VS Code"})
        return f"{result} Workspace path: {workspace}"
    except Exception as exc:
        return f"Could not open VS Code: {exc}"


def _default_command(workspace: Path, operation: str) -> list[str] | None:
    if (workspace / "package.json").exists():
        return ["npm", "test"] if operation == "test" else ["npm", "run", "build"]
    if (workspace / "go.mod").exists():
        return ["go", "test", "./..."] if operation == "test" else ["go", "build", "./..."]
    if operation == "test" and ((workspace / "pytest.ini").exists() or (workspace / "tests").exists()):
        return [sys.executable, "-m", "pytest"]
    if operation == "build" and (workspace / "pyproject.toml").exists():
        return [sys.executable, "-m", "build"]
    if any(workspace.rglob("*.py")):
        return [sys.executable, "-m", "compileall", "-q", "."]
    return None


def _command_parts(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    value = str(command or "").strip()
    if not value:
        return []
    parts = shlex.split(value, posix=(os.name != "nt"))
    if parts and parts[0].lower() in {"python", "python3"}:
        parts[0] = sys.executable
    elif parts and os.name == "nt" and parts[0].lower() in {"npm", "npx"}:
        parts[0] += ".cmd"
    return parts


def _run(workspace: Path, command: str | list[str], timeout: int) -> str:
    parts = _command_parts(command)
    if not parts:
        return "No command was provided and no safe project test/build command was detected."
    try:
        completed = subprocess.run(
            parts, cwd=str(workspace), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=max(1, min(int(timeout), 300)),
            shell=False,
        )
        output = []
        if completed.stdout.strip():
            output.append("STDOUT:\n" + completed.stdout.strip())
        if completed.stderr.strip():
            output.append("STDERR:\n" + completed.stderr.strip())
        output.append(f"Exit code: {completed.returncode}")
        return "\n\n".join(output)
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return f"Command not found: {parts[0]}"
    except Exception as exc:
        return f"Command failed to start: {exc}"


def _write(workspace: Path, path: Path, content: str) -> str:
    previous: str | None = None
    existed = path.exists()
    if existed:
        if not path.is_file():
            return f"Cannot write over a directory: {_display(path, workspace)}"
        if path.stat().st_size > _MAX_UNDO_BYTES:
            return "Refusing to overwrite a file larger than 1 MB because it cannot be safely undone."
        try:
            previous = path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Could not read the previous file contents: {exc}"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return f"Could not write {_display(path, workspace)}: {exc}"

    def undo() -> str:
        try:
            if existed and previous is not None:
                path.write_text(previous, encoding="utf-8")
                return f"Restored {_display(path, workspace)}."
            if path.exists():
                path.unlink()
            return f"Removed {_display(path, workspace)}."
        except Exception as exc:
            return f"Could not undo {_display(path, workspace)}: {exc}"

    push_undo(f"VS Code write: {_display(path, workspace)}", undo)
    return f"Wrote {_display(path, workspace)} ({len(content)} characters). This edit can be undone."


def _mkdir(workspace: Path, path: Path) -> str:
    if path.exists():
        return f"Folder already exists: {_display(path, workspace)}"
    try:
        path.mkdir(parents=True)
    except Exception as exc:
        return f"Could not create folder: {exc}"

    def undo() -> str:
        try:
            if path.exists() and not any(path.iterdir()):
                path.rmdir()
                return f"Removed {_display(path, workspace)}."
            return f"Left {_display(path, workspace)} in place because it is no longer empty."
        except Exception as exc:
            return f"Could not undo folder creation: {exc}"

    push_undo(f"VS Code folder: {_display(path, workspace)}", undo)
    return f"Created folder: {_display(path, workspace)}"


def _delete(workspace: Path, path: Path) -> str:
    if not path.exists():
        return f"Path not found: {_display(path, workspace)}"
    if path.is_dir():
        if any(path.iterdir()):
            return "Refusing to delete a non-empty folder. Move or remove its contents explicitly first."
        path.rmdir()
        return f"Deleted empty folder: {_display(path, workspace)}"
    if path.stat().st_size > _MAX_UNDO_BYTES:
        return "Refusing to delete a file larger than 1 MB through this action."
    previous = path.read_text(encoding="utf-8", errors="replace")
    path.unlink()

    def undo() -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(previous, encoding="utf-8")
        return f"Restored {_display(path, workspace)}."

    push_undo(f"VS Code delete: {_display(path, workspace)}", undo)
    return f"Deleted {_display(path, workspace)}; it can be restored with undo."


def _execute(params: dict, operation: str, workspace: Path) -> str:
    if operation == "open":
        return _open_vscode(workspace)
    if operation in {"list", "organize"}:
        tree = _tree(workspace)
        if operation == "organize":
            return f"Workspace layout (no files changed):\n{tree}"
        return f"Workspace: {workspace}\n{tree}"
    if operation == "git_status":
        return _run(workspace, ["git", "status", "--short", "--branch"], 30)
    if operation == "read":
        return _read(_target(workspace, params.get("path")))
    if operation == "search":
        return _search(workspace, str(params.get("query", "")), str(params.get("pattern", "")))
    if operation in {"write", "edit"}:
        content = params.get("content")
        if content is None:
            return "Write/edit requires the complete file content in `content`."
        return _write(workspace, _target(workspace, params.get("path")), str(content))
    if operation == "mkdir":
        return _mkdir(workspace, _target(workspace, params.get("path")))
    if operation == "delete":
        return _delete(workspace, _target(workspace, params.get("path")))
    if operation in {"run", "test", "build", "debug", "install"}:
        command = params.get("command")
        if not command and operation in {"test", "build"}:
            command = _default_command(workspace, operation)
        return _run(workspace, command or "", int(params.get("timeout", 30)))
    if operation == "explain":
        raw_path = str(params.get("path", "")).strip()
        if raw_path:
            return _read(_target(workspace, raw_path))
        return f"Workspace: {workspace}\n{_tree(workspace)}"
    return f"Unknown workspace operation: {operation}"


def _safe_execute(params: dict, operation: str, workspace: Path) -> str:
    try:
        return _execute(params, operation, workspace)
    except Exception as exc:
        return f"Workspace operation failed: {exc}"


def vscode_workspace(parameters: dict, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "list")).strip().lower()
    workspace = _workspace_for(params, operation)

    if operation not in {
        "open", "list", "read", "search", "write", "edit", "mkdir", "run",
        "test", "build", "debug", "explain", "organize", "git_status", "install", "delete",
    }:
        return f"Unknown workspace operation: {operation}"

    if operation not in {"open", "list", "organize", "git_status"} and not workspace.exists():
        if operation in {"write", "mkdir"}:
            try:
                workspace.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return f"Could not create workspace: {exc}"
        else:
            return f"Workspace not found: {workspace}"

    if player:
        player.write_log(f"[VS Code] {operation} — {workspace}")

    if operation in _PROTECTED_OPS:
        if confirm_gate.pending_title():
            return "There is already a confirmation waiting on screen. Ask the user to answer it first."
        command = params.get("command") or operation
        detail = (
            f"Workspace: {workspace}\nOperation: {operation}\n"
            f"Target: {params.get('path', '') or command}\n\n"
            "This will run a command or make a destructive change in the requested workspace."
        )
        return confirm_gate.request(
            key=f"vscode-{operation}",
            title=f"VS Code: {operation}",
            detail=detail,
            run=lambda: _safe_execute(params, operation, workspace),
        )

    return _safe_execute(params, operation, workspace)


TOOL = {
    "name": "vscode_workspace",
    "description": (
        "Works inside an explicitly requested VS Code workspace: open it, list/read/search files, "
        "write reversible edits, create folders, explain or organize the project, inspect git status, "
        "and run tests/builds/debug commands. Paths stay inside the requested workspace. Run, test, "
        "build, install, and delete are protected by the human confirmation gate."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "open | list | read | search | write | edit | mkdir | run | test | build | debug | explain | organize | git_status | install | delete",
            },
            "workspace_path": {
                "type": "STRING",
                "description": "Existing project/workspace folder; use the user's exact path",
            },
            "path": {
                "type": "STRING",
                "description": "Workspace-relative file or folder path",
            },
            "content": {
                "type": "STRING",
                "description": "Complete file content for write/edit",
            },
            "query": {
                "type": "STRING",
                "description": "Text to search for",
            },
            "pattern": {
                "type": "STRING",
                "description": "Optional filename pattern such as *.py",
            },
            "command": {
                "type": "STRING",
                "description": "Explicit test/build/debug/install command; executed without a shell",
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Command timeout in seconds, default 30",
            },
        },
        "required": ["operation"],
    },
    "handler": vscode_workspace,
}
