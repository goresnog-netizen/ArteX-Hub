#!/usr/bin/env python3
"""Shell/Terminal MCP Server for Snapgent - provides terminal operations via MCP."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

MAX_OUTPUT_CHARS = 500_000
DEFAULT_TIMEOUT = 30
MAX_SESSIONS = 50
SESSION_CLEANUP_INTERVAL = 300  # 5 minutes

# Safe commands that are always allowed
SAFE_COMMANDS = {
    "ls",
    "dir",
    "pwd",
    "cd",
    "echo",
    "cat",
    "type",
    "head",
    "tail",
    "less",
    "more",
    "grep",
    "find",
    "which",
    "where",
    "whereis",
    "locate",
    "file",
    "stat",
    "git",
    "python",
    "python3",
    "node",
    "npm",
    "npx",
    "pip",
    "pip3",
    "cargo",
    "go",
    "rustc",
    "javac",
    "java",
    "dotnet",
    "msbuild",
    "make",
    "cmake",
    "docker",
    "docker-compose",
    "kubectl",
    "helm",
    "terraform",
    "ansible",
    "curl",
    "wget",
    "ping",
    "nslookup",
    "dig",
    "netstat",
    "ss",
    "lsof",
    "ps",
    "top",
    "htop",
    "df",
    "du",
    "free",
    "uptime",
    "whoami",
    "id",
    "env",
    "printenv",
    "set",
    "export",
    "alias",
    "unalias",
    "history",
    "mkdir",
    "rmdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "ln",
    "chmod",
    "chown",
    "tar",
    "gzip",
    "gunzip",
    "zip",
    "unzip",
    "rsync",
    "scp",
    "ssh",
    "vim",
    "nano",
    "vi",
    "emacs",
    "code",
    "notepad",
    "edit",
    "date",
    "cal",
    "bc",
    "calc",
    "expr",
    "awk",
    "sed",
    "cut",
    "sort",
    "uniq",
    "wc",
    "diff",
    "patch",
    "md5sum",
    "sha256sum",
    "sha512sum",
    "base64",
    "base64 -d",
    "openssl",
    "gpg",
    "age",
    "rage",
    "jq",
    "yq",
    "xmllint",
    "csvtool",
    "sqlite3",
    "psql",
    "mysql",
    "redis-cli",
    "mongo",
    "mongosh",
}

# Dangerous commands that require explicit confirmation
DANGEROUS_COMMANDS = {
    "rm -rf /",
    "rm -rf /*",
    "dd if=",
    "mkfs",
    "fdisk",
    "parted",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
    "kill -9 1",
    "killall",
    "pkill -9",
    ":(){ :|:& };:",
    "fork bomb",
    "chmod 777 /",
    "chown -R root:root /",
    "passwd",
    "userdel",
    "groupdel",
    "iptables -F",
    "ufw disable",
    "systemctl stop",
    "systemctl disable",
    "crontab -r",
    "atrm",
    "rm -rf ~",
    "rm -rf $HOME",
}

# Commands that should run in background (servers, watchers)
BACKGROUND_PATTERNS = [
    "serve",
    "server",
    "daemon",
    "watch",
    "webpack --watch",
    "vite",
    "nodemon",
    "pm2 start",
    "supervisord",
    "gunicorn",
    "uvicorn",
    "docker run -d",
    "docker-compose up -d",
]


@dataclass
class Session:
    id: str
    name: str
    shell: str
    cwd: Path
    process: Optional[asyncio.subprocess.Process] = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    output_buffer: List[str] = field(default_factory=list)
    is_background: bool = False
    tmux_session: Optional[str] = None
    screen_session: Optional[str] = None


class ShellManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions: Dict[str, Session] = {}
        self.default_shell = self._detect_shell()
        self._cleanup_task: Optional[asyncio.Task] = None

    def _detect_shell(self) -> str:
        if sys.platform == "win32":
            # Prefer PowerShell on Windows
            if shutil.which("pwsh"):
                return "pwsh"
            if shutil.which("powershell"):
                return "powershell"
            return "cmd"
        # Unix-like: prefer bash, then zsh, then sh
        for shell in ["bash", "zsh", "fish", "sh"]:
            if shutil.which(shell):
                return shell
        return "sh"

    def _split_command(self, command: str) -> List[str]:
        """Tokenize a command line without mangling Windows paths.

        POSIX mode shlex eats backslashes, so `python "C:\\dir with space\\x.py"`
        turns into several bogus tokens on Windows.
        """
        if sys.platform == "win32":
            parts = shlex.split(command, posix=False)
            cleaned: List[str] = []
            for part in parts:
                part = part.strip()
                if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
                    part = part[1:-1]
                if part:
                    cleaned.append(part)
            return cleaned
        return shlex.split(command)

    def _shell_invocation(self, command: str) -> Tuple[Optional[List[str]], bool]:
        """Build the argv used to run `command`.

        Returns (argv, use_shell). `use_shell=True` hands the command line to
        the OS shell verbatim (Windows cmd fallback) because cmd.exe does not
        understand Python's argv quoting: list2cmdline escapes quotes with
        backslashes, which splits paths that contain spaces.
        """
        shell = self.default_shell
        if sys.platform == "win32":
            if shell in ("pwsh", "powershell"):
                return (
                    [shell, "-NoProfile", "-NonInteractive", "-Command", command],
                    False,
                )
            if shell == "cmd":
                return None, True
            return [shell, "-c", command], False
        return [shell, "-c", command], False

    def _is_safe_command(self, command: str) -> Tuple[bool, str]:
        """Check if command is safe to execute."""
        cmd_lower = command.lower().strip()

        # Check for dangerous patterns
        for dangerous in DANGEROUS_COMMANDS:
            if dangerous in cmd_lower:
                return False, f"Dangerous command pattern detected: {dangerous}"

        # Parse command to get base command
        try:
            parts = self._split_command(command)
            if not parts:
                return False, "Empty command"
            base_cmd = parts[0].lower()
            # Remove path if present
            base_cmd = os.path.basename(base_cmd)
        except ValueError:
            return False, "Invalid command syntax"

        # Allow if in safe list
        if base_cmd in SAFE_COMMANDS:
            return True, ""

        # Allow common development tools
        dev_tools = {
            "pytest",
            "jest",
            "mocha",
            "vitest",
            "cargo test",
            "go test",
            "npm test",
            "npm run",
            "yarn",
            "pnpm",
            "bun",
            "mypy",
            "ruff",
            "black",
            "isort",
            "flake8",
            "pylint",
            "eslint",
            "prettier",
            "tsc",
            "ts-node",
            "gradle",
            "maven",
            "mvn",
            "ant",
            "sbt",
            "terraform plan",
            "terraform apply",
            "terraform destroy",
            "ansible-playbook",
            "packer",
            "vagrant",
        }
        for tool in dev_tools:
            if cmd_lower.startswith(tool):
                return True, ""

        return (
            False,
            f"Command '{base_cmd}' not in allowed list. Use 'shell_allow' to add it.",
        )

    def _is_background_command(self, command: str) -> bool:
        cmd_lower = command.lower().strip()
        for pattern in BACKGROUND_PATTERNS:
            if pattern in cmd_lower:
                return True
        return False

    async def create_session(
        self,
        name: str = "",
        shell: str = "",
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Session:
        session_id = f"sess_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
        session_name = name or f"session-{len(self.sessions) + 1}"
        session_shell = shell or self.default_shell
        session_cwd = cwd or self.workspace
        session_env = os.environ.copy()
        if env:
            session_env.update(env)

        # Check for tmux/screen
        tmux_session = None
        screen_session = None

        if shutil.which("tmux"):
            tmux_session = f"snapgent-{session_id}"
            cmd = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                tmux_session,
                "-c",
                str(session_cwd),
            ]
            if session_shell != "sh":
                cmd.extend([session_shell])
            proc = await asyncio.create_subprocess_exec(*cmd, env=session_env)
            await proc.wait()
        elif shutil.which("screen"):
            screen_session = f"snapgent-{session_id}"
            cmd = ["screen", "-dmS", screen_session, "-c", str(session_cwd)]
            if session_shell != "sh":
                cmd.extend([session_shell])
            proc = await asyncio.create_subprocess_exec(*cmd, env=session_env)
            await proc.wait()

        session = Session(
            id=session_id,
            name=session_name,
            shell=session_shell,
            cwd=session_cwd,
            tmux_session=tmux_session,
            screen_session=screen_session,
        )

        self.sessions[session_id] = session
        return session

    async def run_command(
        self,
        command: str,
        session_id: Optional[str] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = DEFAULT_TIMEOUT,
        background: bool = False,
    ) -> Dict[str, Any]:
        # Validate command
        safe, reason = self._is_safe_command(command)
        if not safe:
            return {
                "success": False,
                "error": reason,
                "command": command,
                "exit_code": -1,
            }

        # Determine working directory
        work_dir = cwd or self.workspace
        if session_id and session_id in self.sessions:
            work_dir = self.sessions[session_id].cwd

        # Prepare environment
        cmd_env = os.environ.copy()
        if env:
            cmd_env.update(env)

        # Parse command for subprocess
        if sys.platform == "win32":
            # On Windows, use shell=True for built-in commands
            if (
                session_id
                and session_id in self.sessions
                and self.sessions[session_id].tmux_session
            ):
                # Send to tmux session
                return await self._send_to_tmux(session_id, command)
            elif (
                session_id
                and session_id in self.sessions
                and self.sessions[session_id].screen_session
            ):
                return await self._send_to_screen(session_id, command)
        shell_cmd, use_shell = self._shell_invocation(command)

        start_time = time.time()

        try:
            if background or self._is_background_command(command):
                # Run in background
                if use_shell:
                    process = await asyncio.create_subprocess_shell(
                        command,
                        cwd=str(work_dir),
                        env=cmd_env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=(
                            subprocess.CREATE_NEW_PROCESS_GROUP
                            if sys.platform == "win32"
                            else 0
                        ),
                    )
                else:
                    process = await asyncio.create_subprocess_exec(
                        *shell_cmd,
                        cwd=str(work_dir),
                        env=cmd_env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                # Don't wait for completion
                return {
                    "success": True,
                    "background": True,
                    "pid": process.pid,
                    "command": command,
                    "cwd": str(work_dir),
                    "message": "Command started in background",
                }
            else:
                # Run with timeout
                if use_shell:
                    process = await asyncio.create_subprocess_shell(
                        command,
                        cwd=str(work_dir),
                        env=cmd_env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    process = await asyncio.create_subprocess_exec(
                        *shell_cmd,
                        cwd=str(work_dir),
                        env=cmd_env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return {
                        "success": False,
                        "error": f"Command timed out after {timeout} seconds",
                        "command": command,
                        "exit_code": -1,
                        "timeout": True,
                    }

                duration = time.time() - start_time
                output = stdout.decode("utf-8", errors="replace") if stdout else ""
                error_output = (
                    stderr.decode("utf-8", errors="replace") if stderr else ""
                )

                # Truncate if too large
                if len(output) > MAX_OUTPUT_CHARS:
                    output = output[:MAX_OUTPUT_CHARS] + "\n[OUTPUT TRUNCATED]"
                if len(error_output) > MAX_OUTPUT_CHARS:
                    error_output = (
                        error_output[:MAX_OUTPUT_CHARS] + "\n[ERROR OUTPUT TRUNCATED]"
                    )

                # Update session activity
                if session_id and session_id in self.sessions:
                    self.sessions[session_id].last_activity = time.time()
                    self.sessions[session_id].output_buffer.append(
                        f"$ {command}\n{output}\n{error_output}"
                    )
                    # Keep buffer manageable
                    if len(self.sessions[session_id].output_buffer) > 100:
                        self.sessions[session_id].output_buffer = self.sessions[
                            session_id
                        ].output_buffer[-100:]

                return {
                    "success": process.returncode == 0,
                    "command": command,
                    "exit_code": process.returncode,
                    "stdout": output,
                    "stderr": error_output,
                    "duration_ms": int(duration * 1000),
                    "cwd": str(work_dir),
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "command": command,
                "exit_code": -1,
            }

    async def _send_to_tmux(self, session_id: str, command: str) -> Dict[str, Any]:
        session = self.sessions[session_id]
        if not session.tmux_session:
            return {"success": False, "error": "No tmux session", "command": command}

        # Send command to tmux
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            session.tmux_session,
            command,
            "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        # Capture pane content
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-t",
            session.tmux_session,
            "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        error_output = stderr.decode("utf-8", errors="replace") if stderr else ""

        session.last_activity = time.time()
        session.output_buffer.append(f"$ {command}\n{output}")

        return {
            "success": True,
            "command": command,
            "stdout": output,
            "stderr": error_output,
            "exit_code": 0,
            "tmux_session": session.tmux_session,
        }

    async def _send_to_screen(self, session_id: str, command: str) -> Dict[str, Any]:
        session = self.sessions[session_id]
        if not session.screen_session:
            return {"success": False, "error": "No screen session", "command": command}

        # Send command to screen
        proc = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            session.screen_session,
            "-X",
            "stuff",
            f"{command}\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        # Capture screen content (harder with screen, use hardcopy)
        proc = await asyncio.create_subprocess_exec(
            "screen",
            "-S",
            session.screen_session,
            "-X",
            "hardcopy",
            "-h",
            "/tmp/screen_capture.txt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        try:
            with open("/tmp/screen_capture.txt", "r") as f:
                output = f.read()
        except Exception:
            output = ""

        session.last_activity = time.time()
        session.output_buffer.append(f"$ {command}\n{output}")

        return {
            "success": True,
            "command": command,
            "stdout": output,
            "stderr": "",
            "exit_code": 0,
            "screen_session": session.screen_session,
        }

    async def get_session_output(
        self, session_id: str, lines: int = 100
    ) -> Dict[str, Any]:
        if session_id not in self.sessions:
            return {"success": False, "error": "Session not found"}

        session = self.sessions[session_id]
        buffer_text = "\n".join(session.output_buffer)
        buffer_lines = buffer_text.splitlines()
        return {
            "success": True,
            "session_id": session_id,
            "name": session.name,
            "shell": session.shell,
            "cwd": str(session.cwd),
            "created_at": session.created_at,
            "last_activity": session.last_activity,
            "output": "\n".join(buffer_lines[-lines:]),
            "tmux_session": session.tmux_session,
            "screen_session": session.screen_session,
        }

    async def list_sessions(self) -> List[Dict[str, Any]]:
        result = []
        for session in self.sessions.values():
            result.append(
                {
                    "id": session.id,
                    "name": session.name,
                    "shell": session.shell,
                    "cwd": str(session.cwd),
                    "created_at": session.created_at,
                    "last_activity": session.last_activity,
                    "is_background": session.is_background,
                    "tmux_session": session.tmux_session,
                    "screen_session": session.screen_session,
                }
            )
        return result

    async def kill_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self.sessions:
            return {"success": False, "error": "Session not found"}

        session = self.sessions[session_id]

        # Kill tmux session
        if session.tmux_session:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "kill-session",
                "-t",
                session.tmux_session,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

        # Kill screen session
        if session.screen_session:
            proc = await asyncio.create_subprocess_exec(
                "screen",
                "-S",
                session.screen_session,
                "-X",
                "quit",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

        # Kill process if exists
        if session.process and session.process.returncode is None:
            session.process.kill()
            await session.process.wait()

        del self.sessions[session_id]
        return {"success": True, "message": f"Session {session_id} terminated"}

    async def send_keys(self, session_id: str, keys: str) -> Dict[str, Any]:
        """Send raw keys to a tmux/screen session."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Session not found"}

        session = self.sessions[session_id]

        if session.tmux_session:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "send-keys",
                "-t",
                session.tmux_session,
                keys,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            return {"success": True, "tmux_session": session.tmux_session}

        if session.screen_session:
            proc = await asyncio.create_subprocess_exec(
                "screen",
                "-S",
                session.screen_session,
                "-X",
                "stuff",
                keys,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            return {"success": True, "screen_session": session.screen_session}

        return {"success": False, "error": "No tmux or screen session"}

    async def create_tmux_session(
        self, name: str, cwd: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Create a new tmux session."""
        if not shutil.which("tmux"):
            return {"success": False, "error": "tmux not installed"}

        session_name = name or f"snapgent-{int(time.time())}"
        work_dir = cwd or self.workspace

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = (
                stderr.decode("utf-8", errors="replace") if stderr else "Unknown error"
            )
            return {"success": False, "error": error}

        return {"success": True, "session_name": session_name, "cwd": str(work_dir)}

    async def attach_tmux(self, session_name: str) -> Dict[str, Any]:
        """Get instructions to attach to tmux session."""
        return {
            "success": True,
            "message": f"Run 'tmux attach -t {session_name}' in your terminal to attach",
            "command": f"tmux attach -t {session_name}",
        }

    async def cleanup_old_sessions(self, max_age: int = 3600) -> int:
        """Remove sessions older than max_age seconds."""
        now = time.time()
        to_remove = []
        for session_id, session in self.sessions.items():
            if now - session.last_activity > max_age:
                to_remove.append(session_id)

        for session_id in to_remove:
            await self.kill_session(session_id)

        return len(to_remove)


import shutil


# MCP Server Implementation
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "shell_run",
        "description": "Run a shell command",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "session_id": {"type": "string"},
                "cwd": {"type": "string"},
                "env": {"type": "object"},
                "timeout": {"type": "integer", "default": 30},
                "background": {"type": "boolean", "default": False},
            },
            "required": ["command"],
        },
    },
    {
        "name": "shell_create_session",
        "description": "Create a new shell session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "shell": {"type": "string"},
                "cwd": {"type": "string"},
                "env": {"type": "object"},
            },
        },
    },
    {
        "name": "shell_get_output",
        "description": "Get output from a session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "lines": {"type": "integer", "default": 100},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "shell_list_sessions",
        "description": "List all active sessions",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shell_kill_session",
        "description": "Kill a session",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "shell_send_keys",
        "description": "Send keys to a tmux/screen session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "keys": {"type": "string"},
            },
            "required": ["session_id", "keys"],
        },
    },
    {
        "name": "shell_tmux_new",
        "description": "Create a new tmux session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
    },
    {
        "name": "shell_tmux_attach",
        "description": "Get attach command for tmux session",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": {"type": "string"}},
            "required": ["session_name"],
        },
    },
    {
        "name": "shell_tmux_list",
        "description": "List tmux sessions",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shell_tmux_kill",
        "description": "Kill a tmux session",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": {"type": "string"}},
            "required": ["session_name"],
        },
    },
]


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# Global shell manager instance
_shell_manager: Optional[ShellManager] = None


def get_shell_manager(workspace: Path) -> ShellManager:
    global _shell_manager
    if _shell_manager is None:
        _shell_manager = ShellManager(workspace)
    return _shell_manager


async def handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")

    # JSON-RPC notifications carry no id and must never get a response
    if request_id is None and str(method).startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "shell-mcp", "version": SERVER_VERSION},
                },
            }
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": TOOLS},
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            # Get workspace from environment or use current directory
            workspace = Path(os.environ.get("SNAPGENT_WORKSPACE", str(Path.cwd())))
            manager = get_shell_manager(workspace)

            # Route to tool function
            tool_map = {
                "shell_run": manager.run_command,
                "shell_create_session": manager.create_session,
                "shell_get_output": manager.get_session_output,
                "shell_list_sessions": manager.list_sessions,
                "shell_kill_session": manager.kill_session,
                "shell_send_keys": manager.send_keys,
                "shell_tmux_new": manager.create_tmux_session,
                "shell_tmux_attach": manager.attach_tmux,
                "shell_tmux_list": lambda: _list_tmux_sessions(),
                "shell_tmux_kill": lambda name: _kill_tmux_session(name),
            }

            tool_func = tool_map.get(tool_name)
            if not tool_func:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }

            # Convert string paths to Path objects
            if "cwd" in arguments and arguments["cwd"]:
                arguments["cwd"] = Path(arguments["cwd"])

            result = await tool_func(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ]
                },
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(e)},
        }


async def _list_tmux_sessions() -> Dict[str, Any]:
    if not shutil.which("tmux"):
        return {"success": False, "error": "tmux not installed", "sessions": []}

    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return {"success": True, "sessions": []}  # No sessions

    output = stdout.decode("utf-8", errors="replace")
    sessions = []
    for line in output.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            sessions.append(
                {
                    "name": parts[0],
                    "created": parts[1],
                    "windows": int(parts[2]),
                    "attached": parts[3] == "1",
                }
            )

    return {"success": True, "sessions": sessions}


async def _kill_tmux_session(name: str) -> Dict[str, Any]:
    if not shutil.which("tmux"):
        return {"success": False, "error": "tmux not installed"}

    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-session",
        "-t",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()

    return {
        "success": proc.returncode == 0,
        "message": f"Killed tmux session {name}"
        if proc.returncode == 0
        else "Failed to kill session",
    }


async def _read_stdin_lines():
    """Read lines from stdin asynchronously (cross-platform)."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        yield line


async def main() -> None:
    try:
        async for line in _read_stdin_lines():
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = await handle_request(request)
            if response is None:
                continue
            sys.stdout.write(_json_line(response) + "\n")
            sys.stdout.flush()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
