#!/usr/bin/env python3
"""Cross-platform local WebSocket bridge for Snapgent MCP servers - Enterprise Security Edition."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
import weakref


def _quiet_unraisable_hook(unraisable) -> None:
    """Filter known asyncio shutdown noise from transports outliving the loop."""
    exc = unraisable.exc_value
    if exc is not None:
        msg = str(exc)
        if (
            "Event loop is closed" in msg
            or "I/O operation on closed pipe" in msg
            or "handle is invalid" in msg
        ):
            return
    sys.__unraisablehook__(unraisable)


sys.unraisablehook = _quiet_unraisable_hook

try:
    import websockets
    from websockets.asyncio.server import ServerConnection, serve as websockets_serve
except ImportError:
    try:
        import websockets
        from websockets.legacy.server import WebSocketServerProtocol as ServerConnection

        websockets_serve = websockets.serve
    except ImportError:
        websockets = None
        ServerConnection = None
        websockets_serve = None

try:
    from aiohttp import web
except ImportError:
    web = None

try:
    from coordination_hub import (
        AllProvidersExhausted,
        A2AMessage,
        CoordinationHub,
        ProviderConfig,
    )
except ImportError:  # coordination is optional - bridge still serves MCP tools
    CoordinationHub = None
    ProviderConfig = None
    AllProvidersExhausted = None
    A2AMessage = None

try:
    from mesh import ProjectMesh, ScriptedGateway
except ImportError:  # the mesh is optional - coordination still works without it
    ProjectMesh = None
    ScriptedGateway = None

try:
    import watchfiles
except ImportError:
    watchfiles = None

try:
    from oep import OEPSession
except ImportError:  # OEP is optional - bridge still serves plaintext tool calls
    OEPSession = None


if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent

CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "logs" / "bridge_debug.log"
SECURITY_LOG_PATH = ROOT / "logs" / "bridge_security.log"
HOST = "127.0.0.1"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = int(os.environ.get("ZS_HEALTH_PORT", "17614"))
MCP_PROTOCOL_VERSION = "2024-11-05"
BRIDGE_VERSION = "3.11.0"
MAX_LOG_CHARS = 2000
MAX_MESSAGE_SIZE = 50 * 1024 * 1024

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
SECURITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


class TraceIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "trace_id"):
            record.trace_id = TraceContext.get()
        return True


class TraceIdFormatter(logging.Formatter):
    """Formatter that never crashes when a record lacks trace_id (e.g. aiohttp access logs)."""

    def format(self, record):
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return super().format(record)


logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(trace_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Add filter to root logger to ensure trace_id is always present
root_logger = logging.getLogger()
root_logger.addFilter(TraceIdFilter())
# Handler-level filter AND formatter: logger filters do NOT apply to records
# propagated from child loggers (aiohttp.access, websockets), which previously
# crashed emit() with KeyError 'trace_id' and hung the health endpoint.
for _h in root_logger.handlers:
    _h.addFilter(TraceIdFilter())
    _h.setFormatter(
        TraceIdFormatter(
            "%(asctime)s %(levelname)s [%(trace_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

security_logger = logging.getLogger("security")
security_handler = logging.FileHandler(str(SECURITY_LOG_PATH))
security_handler.setFormatter(
    TraceIdFormatter(
        "%(asctime)s %(levelname)s [%(trace_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
security_handler.addFilter(TraceIdFilter())
security_logger.addHandler(security_handler)
security_logger.setLevel(logging.INFO)
security_logger.propagate = False

# Silence websockets library logging
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)


class TraceContext:
    _current: Dict[int, str] = {}

    @classmethod
    def _task_id(cls) -> Optional[int]:
        try:
            task = asyncio.current_task()
            return id(task) if task else None
        except RuntimeError:
            return None

    @classmethod
    def set(cls, trace_id: str) -> None:
        task_id = cls._task_id()
        if task_id is not None:
            cls._current[task_id] = trace_id

    @classmethod
    def get(cls) -> str:
        task_id = cls._task_id()
        if task_id is not None:
            return cls._current.get(task_id, "no-trace")
        return "no-trace"

    @classmethod
    def clear(cls) -> None:
        task_id = cls._task_id()
        if task_id is not None and task_id in cls._current:
            del cls._current[task_id]


class TraceLogger:
    @staticmethod
    def _log(level: int, message: str, **kwargs) -> None:
        extra = {"trace_id": TraceContext.get()}
        extra.update(kwargs)
        logging.log(level, message, extra=extra)

    @staticmethod
    def info(message: str, **kwargs) -> None:
        TraceLogger._log(logging.INFO, message, **kwargs)

    @staticmethod
    def warning(message: str, **kwargs) -> None:
        TraceLogger._log(logging.WARNING, message, **kwargs)

    @staticmethod
    def error(message: str, **kwargs) -> None:
        TraceLogger._log(logging.ERROR, message, **kwargs)

    @staticmethod
    def debug(message: str, **kwargs) -> None:
        TraceLogger._log(logging.DEBUG, message, **kwargs)

    @staticmethod
    def security(event: str, **kwargs) -> None:
        extra = {"trace_id": TraceContext.get()}
        extra.update(kwargs)
        security_logger.info(event, extra=extra)


def log(message: str) -> None:
    TraceLogger.info(message)


def security_log(event: str, **kwargs) -> None:
    TraceLogger.security(event, **kwargs)


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def generate_secret() -> str:
    return secrets.token_hex(32)


def verify_hmac(secret: str, message: str, signature: str) -> bool:
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


@dataclass
class RateLimiter:
    requests_per_minute: int = 300
    burst: int = 50
    _requests: Dict[str, List[float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self, client_id: str) -> Tuple[bool, Dict[str, int]]:
        async with self._lock:
            now = time.time()
            window_start = now - 60

            if client_id not in self._requests:
                self._requests[client_id] = []

            self._requests[client_id] = [
                ts for ts in self._requests[client_id] if ts > window_start
            ]

            current_count = len(self._requests[client_id])
            remaining = max(0, self.requests_per_minute - current_count)
            retry_after = 0

            if current_count >= self.requests_per_minute:
                oldest = (
                    min(self._requests[client_id]) if self._requests[client_id] else now
                )
                retry_after = int(oldest + 60 - now) + 1
                return False, {
                    "remaining": 0,
                    "retry_after": retry_after,
                    "limit": self.requests_per_minute,
                }

            self._requests[client_id].append(now)
            return True, {
                "remaining": remaining,
                "retry_after": 0,
                "limit": self.requests_per_minute,
            }

    def cleanup(self) -> None:
        now = time.time()
        window_start = now - 60
        for client_id in list(self._requests.keys()):
            self._requests[client_id] = [
                ts for ts in self._requests[client_id] if ts > window_start
            ]
            if not self._requests[client_id]:
                del self._requests[client_id]


@dataclass
class AuthManager:
    enabled: bool = True
    secret: str = ""
    algorithm: str = "HS256"
    _initialized: bool = False

    def __post_init__(self):
        if self.secret == "CHANGE_ME_ON_FIRST_RUN_GENERATE_RANDOM_32_BYTES_HEX":
            self.secret = generate_secret()
            self._save_secret()

    def _save_secret(self) -> None:
        try:
            if CONFIG_PATH.exists():
                config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                config.setdefault("bridge", {}).setdefault("auth", {})["secret"] = (
                    self.secret
                )
                CONFIG_PATH.write_text(
                    json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            pass

    def verify_token(self, token: str) -> bool:
        if not self.enabled:
            return True
        if not token or not self.secret:
            return False
        return constant_time_compare(token, self.secret)

    def get_auth_header_value(self) -> str:
        return self.secret


@dataclass
class SandboxConfig:
    default: str = "none"
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def get_launcher(
        self,
        sandbox_type: str,
        workspace: Path,
        temp_dir: Path,
        command: str = "",
        args: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        if sandbox_type == "none" or sandbox_type not in self.profiles:
            return None

        profile = self.profiles[sandbox_type]
        if not profile.get("enabled", False):
            return None

        if sandbox_type == "firejail":
            profile_path = ROOT / profile.get("profile", "mcp-restrictive.profile")
            if not profile_path.exists():
                return None
            whitelist_paths = []
            for wp in profile.get("whitelistPaths", []):
                wp = wp.replace("${workspace}", str(workspace))
                wp = wp.replace("${temp}", str(temp_dir))
                whitelist_paths.append(f"--whitelist={wp}")
            # firejail takes the real command AFTER the "--" separator.
            return [
                "firejail",
                f"--profile={profile_path}",
                *whitelist_paths,
                "--",
                command,
                *(args or []),
            ]

        elif sandbox_type == "docker":
            docker_cmd = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--memory",
                profile.get("memoryLimit", "512m"),
                "--cpus",
                profile.get("cpuLimit", "0.5"),
                "--network",
                profile.get("networkMode", "none"),
            ]
            if profile.get("readOnlyRootfs", True):
                docker_cmd.append("--read-only")
            docker_cmd += [
                "-v",
                f"{workspace}:/workspace:ro",
                "-v",
                f"{temp_dir}:/tmp:rw",
                "--workdir",
                "/workspace",
                profile.get("image", "python:3.11-slim"),
                command,
                *(args or []),
            ]
            return docker_cmd

        elif sandbox_type == "windows-job":
            # Windows native sandbox via pure-Python Job Object wrapper.
            # NOTE: never route through `powershell -File` — PowerShell's
            # ValueFromRemainingArguments binding rejects the `--` separator
            # from subprocess ("parameter name '' is ambiguous").
            job_script = ROOT / "sandbox_job.py"
            if not job_script.exists():
                logging.warning(
                    f"sandbox_job.py not found: {job_script}, falling back to none"
                )
                return None

            # Build allowed paths
            allowed_paths = []
            for ap in profile.get("allowedPaths", []):
                ap = ap.replace("${workspace}", str(workspace))
                ap = ap.replace("${temp}", str(temp_dir))
                allowed_paths.append(ap)

            denied_paths = []
            for dp in profile.get("deniedPaths", []):
                # Expand wildcards for user profile paths
                import glob

                expanded = glob.glob(dp)
                denied_paths.extend(expanded if expanded else [dp])

            # Write config JSON to temp file (avoids PowerShell arg-passing bugs)
            import json as _json

            cfg_file = temp_dir / f"sandbox_cfg_{uuid.uuid4().hex[:8]}.json"
            cfg = {
                "memoryLimitMB": profile.get("memoryLimitMB", 512),
                "maxProcesses": profile.get("maxProcesses", 50),
                "exe": command,
                "args": list(args),
                "cwd": str(workspace),
            }
            cfg_file.write_text(_json.dumps(cfg))
            job_script = ROOT / "sandbox_job.py"
            if not job_script.exists():
                logging.warning(
                    f"sandbox_job.py not found: {job_script}, falling back to none"
                )
                return None
            return ["python", str(job_script), str(cfg_file)]

        return None


class MCPServer:
    def __init__(
        self,
        server_id: str,
        config: Dict[str, Any],
        sandbox_config: Optional[SandboxConfig] = None,
        workspace: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
    ) -> None:
        self.server_id = server_id
        self.config = config
        self.sandbox_config = sandbox_config or SandboxConfig()
        self.workspace = Path(workspace) if workspace else ROOT
        self.temp_dir = (
            Path(temp_dir) if temp_dir else Path(tempfile.gettempdir()) / "snapgent"
        )
        self.process: Optional[asyncio.subprocess.Process] = None
        self.tools: List[Dict[str, Any]] = []
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        self.error: Optional[str] = None
        self.ready = False
        self.sandbox_type = config.get("sandbox", self.sandbox_config.default)

    @property
    def alive(self) -> bool:
        reader_dead = bool(self._reader_task and self._reader_task.done())
        return bool(
            self.process and self.process.returncode is None and not reader_dead
        )

    def _expand(self, value: str) -> str:
        """Resolve the ${workspace} / ${temp} placeholders in config values."""
        return (
            str(value)
            .replace("${workspace}", str(self.workspace))
            .replace("${temp}", str(self.temp_dir))
        )

    def _resolve_arg(self, arg: str) -> str:
        """Make a script argument absolute against the bridge root.

        The child process runs with cwd = the user's workspace (which may be a
        different project entirely), so a relative "git_mcp.py" would resolve
        against the wrong directory. MCP server scripts live next to
        bridge.py, so that is the base for relative script paths.
        """
        if not arg or arg.startswith("-"):
            return arg
        candidate = Path(arg)
        if candidate.is_absolute():
            return arg
        if candidate.suffix.lower() == ".py" or candidate.exists():
            rooted = ROOT / candidate
            if rooted.exists():
                return str(rooted)
        return arg

    def _build_command(self) -> Tuple[str, List[str], Dict[str, str]]:
        command = self.config.get("command", "")
        raw_args = self.config.get("args", [])
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        env = os.environ.copy()
        server_env = self.config.get("env", {})
        if isinstance(server_env, dict):
            env.update(
                {
                    str(key): self._expand(str(value))
                    for key, value in server_env.items()
                }
            )
        # The child must resolve the same root as the bridge. IDE servers get
        # the folder their IDE actually has open, so a Zed agent never lands in
        # the bridge repo by accident.
        env["SNAPGENT_WORKSPACE"] = str(self.workspace)
        env["SNAPGENT_WORKSPACE_ROOT"] = str(self.workspace)

        command = self._expand(str(command))
        args = [self._resolve_arg(self._expand(arg)) for arg in args]

        launcher = self.sandbox_config.get_launcher(
            self.sandbox_type,
            self.workspace,
            self.temp_dir,
            command=command,
            args=args,
        )
        if launcher:
            return launcher[0], launcher[1:], env

        return command, args, env

    async def start(self) -> bool:
        async with self._start_lock:
            if self.alive and self.ready:
                return True
            await self.stop()

            command, args, env = self._build_command()

            if not isinstance(command, str) or not command.strip():
                self.error = "MCP server command is missing."
                log(f"[{self.server_id}] {self.error}")
                return False
            if not isinstance(args, list) or not all(
                isinstance(arg, str) for arg in args
            ):
                self.error = "MCP server args must be an array of strings."
                log(f"[{self.server_id}] {self.error}")
                return False

            try:
                self.process = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    cwd=str(self.workspace),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=64 * 1024 * 1024,
                )
            except (OSError, ValueError) as error:
                self.error = str(error)
                log(f"[{self.server_id}] failed to launch: {self.error}")
                security_log(
                    "mcp_launch_failed", server=self.server_id, error=str(error)
                )
                return False

            self.tools = []
            self.error = None
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())

            try:
                await self._request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "snapgent-bridge",
                            "version": BRIDGE_VERSION,
                        },
                    },
                    timeout=12,
                )
                await self._notify("notifications/initialized")
                result = await self._request("tools/list", {}, timeout=20)
                raw_tools = result.get("tools", []) if isinstance(result, dict) else []
                if not isinstance(raw_tools, list):
                    raise RuntimeError("MCP tools/list returned an invalid tool list.")
                self.tools = [
                    tool
                    for tool in raw_tools
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                ]
                self.ready = True
                log(
                    f"[{self.server_id}] MCP server up ({len(self.tools)} tools advertised)"
                )
                security_log(
                    "mcp_started",
                    server=self.server_id,
                    tools=len(self.tools),
                    sandbox=self.sandbox_type,
                )
                return True
            except (asyncio.TimeoutError, RuntimeError, OSError, ValueError) as error:
                self.error = str(error)
                log(f"[{self.server_id}] MCP startup failed: {self.error}")
                security_log(
                    "mcp_startup_failed", server=self.server_id, error=str(error)
                )
                await self.stop()
                return False

    async def stop(self) -> None:
        process = self.process
        self.process = None
        self.tools = []
        self.ready = False
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        self._reader_task = None
        self._stderr_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    RuntimeError(f"{self.server_id} MCP server stopped.")
                )
        self._pending.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not process or process.returncode is not None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _send(self, payload: Dict[str, Any]) -> None:
        process = self.process
        if not process or process.returncode is not None or not process.stdin:
            raise RuntimeError(f"{self.server_id} MCP server is offline.")
        data = (_json_line(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _request(
        self, method: str, params: Dict[str, Any], timeout: float = 30
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            detail = response["error"]
            if isinstance(detail, dict):
                raise RuntimeError(str(detail.get("message") or detail))
            raise RuntimeError(str(detail))
        return response.get("result")

    async def _notify(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        if not self.alive:
            raise RuntimeError(self.error or f"{self.server_id} MCP server is offline.")
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
        )
        return (
            result if isinstance(result, dict) else {"content": [{"text": str(result)}]}
        )

    async def _read_stdout(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log(f"[{self.server_id}] ignored non-JSON MCP stdout.")
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future and not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError) as error:
            log(f"[{self.server_id}] MCP stdout read failed: {error}")
        finally:
            if self.process is process and process.returncode is None:
                log(f"[{self.server_id}] MCP stdout closed.")
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            RuntimeError(f"{self.server_id} MCP server disconnected.")
                        )

    async def _read_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    log(f"[{self.server_id}] stderr: {text[:MAX_LOG_CHARS]}")
        except asyncio.CancelledError:
            raise


import tempfile


class Bridge:
    def __init__(self) -> None:
        self.servers: Dict[str, MCPServer] = {}
        self.clients: Set[ServerConnection] = set()
        self.client_info: Dict[ServerConnection, Dict[str, Any]] = {}
        self._client_lock = asyncio.Lock()
        self._catalog: List[Dict[str, Any]] = []
        self._last_studio: Optional[bool] = None
        self.auth = AuthManager()
        self.rate_limiter = RateLimiter()
        self.workspace = self._detect_workspace()
        self.temp_dir = Path(tempfile.gettempdir()) / "snapgent"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_config = SandboxConfig()
        self.hub: Optional[Any] = None
        self.mesh: Optional[Any] = None
        self.mesh_gateway: Optional[Any] = None
        if CoordinationHub is not None:
            self.hub = CoordinationHub()
            self._register_bridge_a2a()
        if ProjectMesh is not None and ScriptedGateway is not None:
            if self.hub is None:
                self.hub = CoordinationHub()
                self._register_bridge_a2a()
            self.mesh = ProjectMesh(self.hub, ScriptedGateway(), None)
            self.mesh_gateway = self.mesh.gateway
            self.mesh.refresh_agents()
            # The Zed IDE is an external agent: it receives routed findings in
            # its own inbox and reads them through the bridge protocol.
            self.mesh.register_agent_inbox("zed")
        self._mesh_watchers: Dict[str, asyncio.Task] = {}
        self.config_watcher_task: Optional[asyncio.Task] = None
        self.health_server: Optional[web.Application] = None
        self.health_runner: Optional[web.AppRunner] = None
        self.metrics = {
            "requests_total": 0,
            "requests_errors": 0,
            "tools_called": 0,
            "tools_errors": 0,
            "clients_connected": 0,
            "servers_started": 0,
            "servers_failed": 0,
            "start_time": time.time(),
        }
        self._config_mtime: float = 0

    def _detect_workspace(self) -> Path:
        """Resolve the folder used for ${workspace} and MCP server roots.

        Order: explicit env override -> folder of the IDE that is actually
        running right now -> the bridge project root. Stale folders left behind
        by an IDE that is not running are ignored, so the agents never target
        the wrong project.
        """
        for key in ("SNAPGENT_WORKSPACE_ROOT", "SNAPGENT_WORKSPACE"):
            raw = (os.environ.get(key) or "").strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                return candidate.resolve()
        for ide in ("vscode", "cursor", "zed"):
            folder = self._ide_workspace_root(ide)
            if folder is not None:
                return folder
        return ROOT

    def _ide_workspace_root(self, ide: str) -> Optional[Path]:
        """Resolve the folder one IDE actually has open.

        The "vscode" and "zed" MCP servers must run against the project the
        operator opened in that IDE, not against the bridge's own repo. Each
        IDE exposes that folder differently: VS Code keeps it in its global
        storage file, Zed records it in its thread database.
        """
        key = (ide or "").strip().lower()
        products = {
            "vscode": "Code",
            "code": "Code",
            "code - insiders": "Code - Insiders",
            "vscodium": "VSCodium",
            "cursor": "Cursor",
        }
        if key in products:
            appdata = os.environ.get("APPDATA")
            if not appdata:
                return None
            storage = (
                Path(appdata)
                / products[key]
                / "User"
                / "globalStorage"
                / "storage.json"
            )
            return self._folder_from_ide_storage(storage)
        if key == "zed":
            return self._zed_workspace()
        return None

    def _zed_workspace(self) -> Optional[Path]:
        """Read the folder Zed last worked in from its thread database.

        Zed keeps no folder state file in %APPDATA%, but every agent thread
        records the folders it ran in. The most recently updated thread holds
        the workspace the operator is working in right now.
        """
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        database = Path(local) / "Zed" / "threads" / "threads.db"
        if not database.is_file():
            return None
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT folder_paths FROM threads "
                    "WHERE folder_paths IS NOT NULL AND folder_paths <> '' "
                    "ORDER BY updated_at DESC LIMIT 20"
                ).fetchall()
            finally:
                connection.close()
        except Exception:
            return None
        for (raw,) in rows:
            for entry in str(raw or "").splitlines():
                entry = entry.strip()
                if not entry:
                    continue
                candidate = Path(entry)
                if candidate.is_dir():
                    return candidate.resolve()
        return None

    def _server_workspace(self, server_id: str) -> Path:
        """IDE servers run against their own IDE folder; others share the global one."""
        folder = self._ide_workspace_root(server_id)
        if folder is not None:
            return folder
        return self.workspace

    def _normalize_workspace_uri(self, uri: str) -> Optional[Path]:
        """Normalize a stored workspace URI to a local Path.

        - Plain Windows drive path (^[A-Za-z]:[\\/]) used as-is (no urlparse)
        - file:// URI: extract path, convert backslashes to forward slashes,
          strip leading "/" when drive letter follows
        - Non-file schemes (vscode-remote://, ssh-remote, etc.): REJECTED
        - Returns None if not an existing directory
        """
        # Plain Windows drive path
        if re.match(r"^[A-Za-z]:[\\/]", uri):
            return Path(uri).resolve() if Path(uri).resolve().is_dir() else None

        # file:// URI
        if uri.startswith("file://"):
            parsed = urlparse(uri)
            path = unquote(parsed.path)
            path = path.replace("\\", "/")
            if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            candidate = Path(path).resolve()
            return candidate if candidate.is_dir() else None

        # Non-file schemes — reject
        return None

    def _folder_from_ide_storage(self, storage_path: Path) -> Optional[Path]:
        """Read the last active folder out of a VS Code compatible state file."""
        if not storage_path.is_file():
            return None
        try:
            data = json.loads(
                storage_path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            return None
        windows_state = data.get("windowsState") or {}
        uris: List[str] = []
        last_folder = (windows_state.get("lastActiveWindow") or {}).get("folder")
        if last_folder:
            uris.append(last_folder)
        for window in windows_state.get("openedWindows") or []:
            folder = (window or {}).get("folder")
            if folder:
                uris.append(folder)
        for uri in uris:
            candidate = self._normalize_workspace_uri(uri)
            if candidate is not None:
                return candidate
        return None

    def _running_process_names(self) -> Set[str]:
        """Lowercase executable names of the processes running right now."""
        if sys.platform != "win32":
            return set()
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except Exception:
            return set()
        names: Set[str] = set()
        for line in (result.stdout or "").splitlines():
            match = re.match(r'^"([^"]+)"', line.strip())
            if match:
                names.add(match.group(1).lower())
        return names

    def _ide_storage_paths(self) -> List[Path]:
        """State files of the IDEs that are running right now (Cursor, VS Code)."""
        running = self._running_process_names()
        products: List[str] = []
        if "cursor.exe" in running:
            products.append("Cursor")
        if "code.exe" in running:
            products.append("Code")
        if "code - insiders.exe" in running:
            products.append("Code - Insiders")
        if "vscodium.exe" in running:
            products.append("VSCodium")
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return []
        return [
            Path(appdata) / product / "User" / "globalStorage" / "storage.json"
            for product in products
        ]

    def load_config(self) -> Dict[str, Dict[str, Any]]:
        if not CONFIG_PATH.exists():
            return {}
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {error}") from error

        bridge_config = config.get("bridge", {})
        if bridge_config:
            auth_config = bridge_config.get("auth", {})
            self.auth.enabled = auth_config.get("enabled", True)
            self.auth.secret = auth_config.get("secret", self.auth.secret)
            # Regenerate the placeholder secret on first run so the bridge
            # never ships with the well-known default string as its token.
            if (
                self.auth.secret
                == "CHANGE_ME_ON_FIRST_RUN_GENERATE_RANDOM_32_BYTES_HEX"
            ):
                self.auth.secret = generate_secret()
                self.auth._save_secret()
            self.auth.algorithm = auth_config.get("algorithm", "HS256")

            health_config = bridge_config.get("health", {})
            self.health_enabled = health_config.get("enabled", True)

            hot_reload_config = bridge_config.get("hotReload", {})
            self.hot_reload_enabled = hot_reload_config.get("enabled", True)

            tracing_config = bridge_config.get("tracing", {})
            self.tracing_enabled = tracing_config.get("enabled", True)

            sandbox_config = bridge_config.get("sandbox", {})
            self.sandbox_config.default = sandbox_config.get("default", "none")
            self.sandbox_config.profiles = sandbox_config.get("profiles", {})

            security_config = bridge_config.get("security", {})
            self.rate_limiter.requests_per_minute = security_config.get(
                "rateLimit", {}
            ).get("requestsPerMinute", 300)
            self.rate_limiter.burst = security_config.get("rateLimit", {}).get(
                "burst", 50
            )
            self.allowed_origins = set(security_config.get("allowedOrigins", []))
            # Pin the extension ID from config (set by scripts/gen_ext_key.py).
            # This value is the exact chrome-extension://<id> the bridge will
            # accept on WS handshakes and /auth-token. Without it, the bridge
            # refuses every extension origin - which is the safe default.
            ext_id = (security_config.get("extensionId") or "").strip()
            if ext_id:
                os.environ["SNAPGENT_EXTENSION_ID"] = ext_id

        # Providers registered from config; safe on hot reload (register overwrites)
        if self.hub is not None:
            for name, spec in config.get("providers", {}).items():
                if not isinstance(spec, dict) or ProviderConfig is None:
                    continue
                self.hub.providers.register(
                    name,
                    ProviderConfig(
                        endpoint=spec.get("endpoint", ""),
                        api_key=spec.get("apiKey", ""),
                        models=spec.get("models", ["default"]),
                        auth=spec.get("auth", "bearer"),
                        headers=spec.get("headers", {}),
                        requests_per_minute=spec.get("requestsPerMinute", 60),
                        tokens_per_minute=spec.get("tokensPerMinute", 100000),
                        max_concurrent=spec.get("maxConcurrent", 5),
                        timeout=spec.get("timeout", 30.0),
                        max_retries=spec.get("maxRetries", 3),
                        priority=spec.get("priority", 1),
                    ),
                )
                self.hub.memory.register_profile(
                    name,
                    spec.get("memoryStrategy", "sliding"),
                    spec.get("memoryWindowTokens", 30000),
                )
            # New providers get an A2A agent address in the mesh.
            if self.mesh is not None:
                self.mesh.refresh_agents()

        servers = config.get("mcpServers") if isinstance(config, dict) else None
        if not isinstance(servers, dict):
            raise RuntimeError(
                f"{CONFIG_PATH.name} must contain an object named mcpServers."
            )
        return {
            server_id: value
            for server_id, value in servers.items()
            if isinstance(server_id, str)
            and isinstance(value, dict)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", server_id)
        }

    async def start_servers(self) -> None:
        config = self.load_config()
        new_servers = {}
        for server_id, server_config in config.items():
            if server_id in self.servers and self.servers[server_id].alive:
                new_servers[server_id] = self.servers[server_id]
            else:
                new_servers[server_id] = MCPServer(
                    server_id,
                    server_config,
                    self.sandbox_config,
                    self._server_workspace(server_id),
                    self.temp_dir,
                )

        for server_id in list(self.servers.keys()):
            if server_id not in new_servers:
                await self.servers[server_id].stop()

        self.servers = new_servers

        async def _start_one(server: MCPServer) -> None:
            try:
                success = await server.start()
                if success:
                    self.metrics["servers_started"] += 1
                else:
                    self.metrics["servers_failed"] += 1
            except Exception as error:
                server.error = str(error)
                self.metrics["servers_failed"] += 1
                log(f"[{server.server_id}] unexpected startup error: {error}")
                security_log(
                    "mcp_unexpected_error", server=server.server_id, error=str(error)
                )
            self._rebuild_catalog()
            await self.broadcast(self._connected_message())

        await asyncio.gather(*(_start_one(server) for server in self.servers.values()))
        self._rebuild_catalog()
        self._log_health()

        # Start health server if enabled
        if getattr(self, "health_enabled", True):
            await self.start_health_server()

    async def stop_servers(self) -> None:
        for task in list(self._mesh_watchers.values()):
            task.cancel()
        self._mesh_watchers.clear()
        for run in list(getattr(self.mesh, "_runs", {}).values()):
            if run is not None and not run.done():
                run.cancel()
        await asyncio.gather(*(server.stop() for server in self.servers.values()))
        self.servers.clear()
        self._catalog = []

    def _rebuild_catalog(self) -> None:
        entries = []
        for server in self.servers.values():
            if server.alive:
                entries.extend((server.server_id, tool) for tool in server.tools)
        name_counts: Dict[str, int] = {}
        for _, tool in entries:
            name_counts[tool["name"]] = name_counts.get(tool["name"], 0) + 1
        self._catalog = []
        for server_id, tool in entries:
            bare_name = tool["name"]
            name = (
                f"{server_id}/{bare_name}" if name_counts[bare_name] > 1 else bare_name
            )
            self._catalog.append(
                {
                    "name": name,
                    "server": server_id,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get(
                        "inputSchema", {"type": "object", "properties": {}}
                    ),
                }
            )

    def _server_status(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": server.server_id,
                "alive": server.alive,
                "tools": len(server.tools) if server.alive else 0,
                "sandbox": server.sandbox_type,
                # A server object without a workspace must not break the
                # handshake: report the bridge workspace instead.
                "workspace": str(getattr(server, "workspace", self.workspace)),
                "error": server.error,
            }
            for server in self.servers.values()
        ]

    def _log_health(self) -> None:
        ids = ", ".join(self.servers) or "none"
        active = (
            ", ".join(
                server.server_id for server in self.servers.values() if server.alive
            )
            or "none"
        )
        log(f"configured {len(self.servers)} MCP server(s): {ids}  (active: {active})")
        total = len(self._catalog)
        log(f"ready {total} tools available ({len(self.servers)} MCP server(s))")

    def _connected_message(self) -> Dict[str, Any]:
        return {
            "type": "connected",
            "bridge_version": BRIDGE_VERSION,
            "mcp_alive": any(server.alive for server in self.servers.values()),
            "tools": self._catalog,
            "servers": self._server_status(),
            "providers": self._coord_provider_snapshot(),
        }

    def _register_bridge_a2a(self) -> None:
        """Register the bridge itself as an A2A agent so peers can address it."""

        def handle(message: Any) -> Dict[str, Any]:
            log(f"[a2a] {message.sender} -> {message.type} ({message.recipient})")
            security_log("a2a_message", sender=message.sender, type=message.type)
            return {"ok": True}

        self.hub.a2a.register_agent("bridge", handle)

    def _coord_provider_snapshot(self) -> List[Dict[str, Any]]:
        if self.hub is None:
            return []
        out: List[Dict[str, Any]] = []
        for name in self.hub.providers.list_providers():
            try:
                cfg = self.hub.providers.providers[name]
                out.append(
                    {
                        "id": name,
                        "priority": cfg.priority,
                        "healthy": self.hub.providers.is_healthy(name),
                        "quota": self.hub.providers.remaining_quota(name),
                        "health": self.hub.providers.health[name].status,
                    }
                )
            except (KeyError, AttributeError):
                continue
        return out

    async def _coord_prepare(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        request_id = request.get("id")
        provider = request.get("provider")
        messages = request.get("messages") or []
        if self.hub is None:
            await self._send_client(
                websocket,
                {
                    "type": "coord_error",
                    "id": request_id,
                    "error": "coordination hub unavailable",
                },
            )
            return
        if not isinstance(provider, str) or not isinstance(messages, list):
            await self._send_client(
                websocket,
                {
                    "type": "coord_error",
                    "id": request_id,
                    "error": "provider (string) and messages (list) are required",
                },
            )
            return
        try:
            result = self.hub.prepare_request(
                provider,
                messages,
                allow_over_quota=bool(request.get("allowOverQuota")),
                task=request.get("task") or None,
            )
        except AllProvidersExhausted as error:
            self.metrics["requests_errors"] += 1
            await self._send_client(
                websocket,
                {
                    "type": "coord_error",
                    "id": request_id,
                    "error": str(error),
                    "providers": self.hub.providers.stats(),
                },
            )
            return
        self.metrics["requests_total"] += 1
        await self._send_client(
            websocket, {"type": "coord_prepared", "id": request_id, **result}
        )

    async def _a2a_send(
        self, websocket: ServerConnection, request: Dict[str, Any], trace_id: str
    ) -> None:
        request_id = request.get("id")
        if self.hub is None:
            await self._send_client(
                websocket,
                {
                    "type": "a2a_error",
                    "id": request_id,
                    "error": "coordination hub unavailable",
                },
            )
            return
        message_type = request.get("messageType", "note")
        sender = request.get("sender", "bridge")
        payload = request.get("payload") or {}
        if not isinstance(message_type, str) or not isinstance(payload, dict):
            await self._send_client(
                websocket,
                {
                    "type": "a2a_error",
                    "id": request_id,
                    "error": "messageType (string) and payload (object) are required",
                },
            )
            return
        if str(message_type).startswith("notifications/"):
            await self._send_client(
                websocket,
                {
                    "type": "a2a_ack",
                    "id": request_id,
                    "delivered": "broadcast",
                    "message": {
                        "type": message_type,
                        "sender": sender,
                        "payload": payload,
                        "recipient": None,
                    },
                },
            )
            return
        message = A2AMessage(
            type=message_type,
            sender=sender,
            payload=payload,
            recipient=request.get("recipient"),
        )
        delivered = self.hub.a2a.route(message)
        await self._send_client(
            websocket,
            {
                "type": "a2a_ack",
                "id": request_id,
                "delivered": message.recipient or "broadcast",
                "agents": delivered,
                "message": message.to_dict(),
            },
        )

    async def _a2a_agents(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        """List the A2A agents that can receive messages, with inbox depth."""
        agents: List[Dict[str, Any]] = []
        if self.hub is not None:
            for name in sorted(self.hub.a2a.agents):
                depth = self.mesh.inbox_depth(name) if self.mesh is not None else 0
                agents.append({"name": name, "inbox": depth})
        await self._send_client(
            websocket,
            {
                "type": "a2a_agents",
                "id": request.get("id"),
                "agents": agents,
            },
        )

    async def _a2a_inbox(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        """Read (and optionally drain) one agent's inbox.

        External agents such as the Zed IDE poll their own inbox to collect
        findings that other agents routed to them.
        """
        agent = str(request.get("agent") or "zed").strip() or "zed"
        drain = bool(request.get("drain", False))
        messages: List[Dict[str, Any]] = []
        if self.mesh is not None:
            if drain:
                messages = self.mesh.drain_inbox(agent)
            else:
                messages = list(self.mesh.inboxes.get(agent, []))
        await self._send_client(
            websocket,
            {
                "type": "a2a_inbox",
                "id": request.get("id"),
                "agent": agent,
                "messages": messages,
            },
        )

    # -- project mesh (multi-agent, cross-provider build) -------------------

    @staticmethod
    def _mesh_task_prompt(call: Dict[str, Any]) -> str:
        """Last user message of a parked call - what the worker must answer."""
        messages = call.get("messages") or []
        for item in reversed(messages):
            if item.get("role") == "user":
                return str(item.get("content") or "")
        return str(messages[-1].get("content") or "") if messages else ""

    async def _mesh_create_project(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        request_id = request.get("id")
        if self.mesh is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable (mesh.py missing)",
                },
            )
            return
        title = request.get("title") or "untitled project"
        goal = request.get("goal") or ""
        raw_tasks = request.get("tasks") or []
        if not isinstance(raw_tasks, list):
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "tasks must be a list",
                },
            )
            return
        project = self.mesh.create_project(title, goal, raw_tasks)
        security_log(
            "mesh_project_created", project_id=project.id, tasks=len(project.tasks)
        )
        await self._send_client(
            websocket,
            {
                "type": "mesh_project_created",
                "id": request_id,
                "project": project.to_dict(),
            },
        )

    async def _mesh_run_project(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        request_id = request.get("id")
        if self.mesh is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable",
                },
            )
            return
        project_id = request.get("projectId")
        if project_id not in self.mesh.projects:
            await self._send_client(
                websocket,
                {"type": "mesh_error", "id": request_id, "error": "unknown project"},
            )
            return
        existing = self.mesh._runs.get(project_id)
        if existing is not None and not existing.done():
            await self._send_client(
                websocket,
                {
                    "type": "mesh_run_started",
                    "id": request_id,
                    "projectId": project_id,
                    "alreadyRunning": True,
                },
            )
            return

        async def _run() -> Dict[str, Any]:
            try:
                summary = await self.mesh.run_project(
                    project_id,
                    max_concurrent=int(request.get("maxConcurrent") or 3),
                    max_attempts=int(request.get("maxAttempts") or 2),
                    allow_over_quota=bool(request.get("allowOverQuota")),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                security_log(
                    "mesh_project_error",
                    project_id=project_id,
                    error=str(exc),
                )
            try:
                await self._send_client(
                    websocket,
                    {
                        "type": "mesh_run_finished",
                        "id": request_id,
                        "projectId": project_id,
                        "summary": summary,
                        "providers": self._coord_provider_snapshot(),
                    },
                )
            except Exception:
                pass
            security_log(
                "mesh_project_finished",
                project_id=project_id,
                status=(summary or {}).get("status"),
            )
            return summary

        self.mesh._runs[project_id] = asyncio.create_task(_run())
        self._mesh_watch_parked(project_id)
        await self._send_client(
            websocket,
            {"type": "mesh_run_started", "id": request_id, "projectId": project_id},
        )

    def _mesh_watch_parked(self, project_id: str) -> None:
        """Push parked tasks to every client as `mesh_task`, ONE AT A TIME.

        Polling from a service worker is how a mesh task dies: the worker parks,
        the parked set grows, nothing answers. The bridge owns the parked set, so
        the bridge pushes. This is the extension's ONLY reason to exist in the
        data path - it is a worker, not a participant.

        Only one task is in flight at any moment. A browser worker types into
        the active tab of one window, so handing the front end two parked tasks
        at once parks the second in waitVisible until its budget is gone. The
        turn is released when the worker answers (the call leaves the parked
        set) or when it outlives the broker timeout, which is exactly when the
        mesh would have given up on it anyway - so a dead worker can never wedge
        the rest of the run.
        """
        if project_id in self._mesh_watchers:
            return

        timeout = float(getattr(self.mesh_gateway, "timeout", 900.0) or 900.0)
        turn_slack = 30.0

        async def _watch() -> None:
            seen: Set[str] = set()
            outstanding: Optional[str] = None
            try:
                while True:
                    run = self.mesh._runs.get(project_id)
                    if project_id not in self.mesh.projects:
                        return
                    parked = self.mesh.gateway.pending_tasks(project_id)
                    if run is not None and run.done() and not parked:
                        return

                    by_id = {call.get("thread_id"): call for call in parked}
                    if (
                        outstanding is not None
                        and outstanding not in by_id
                        and outstanding in seen
                    ):
                        # answered (or the broker gave up on it): next turn
                        outstanding = None

                    if outstanding is None:
                        for call in parked:
                            thread_id = call.get("thread_id")
                            if not thread_id or thread_id in seen:
                                continue
                            seen.add(thread_id)
                            outstanding = thread_id
                            log(
                                f"[mesh:{project_id}] handing out {thread_id} "
                                f"({call.get('provider')})"
                            )
                            await self.broadcast(
                                {
                                    "type": "mesh_task",
                                    "task": {
                                        "threadId": thread_id,
                                        "projectId": project_id,
                                        "provider": call.get("provider"),
                                        "system": call.get("system", ""),
                                        "prompt": self._mesh_task_prompt(call),
                                    },
                                }
                            )
                            break
                    elif outstanding in by_id:
                        age = time.time() - float(
                            by_id[outstanding].get("created_at") or time.time()
                        )
                        if age > timeout + turn_slack:
                            log(
                                f"[mesh:{project_id}] releasing stalled "
                                f"{outstanding} after {age:.0f}s"
                            )
                            outstanding = None
                    await asyncio.sleep(0.7)
            finally:
                self._mesh_watchers.pop(project_id, None)

        self._mesh_watchers[project_id] = asyncio.create_task(_watch())

    async def _mesh_park_open_tasks(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        """Deliver any parked provider call to the client so it can answer it."""
        request_id = request.get("id")
        if self.mesh is None or self.mesh_gateway is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable",
                },
            )
            return
        project_id = request.get("projectId")
        parked = list(
            self.mesh_gateway.pending_tasks(
                project_id if isinstance(project_id, str) else ""
            )
        )
        await self._send_client(
            websocket,
            {"type": "mesh_parked", "id": request_id, "parked": parked},
        )

    async def _mesh_task_result(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        """The extension returns the answer a provider produced in the chat page."""
        request_id = request.get("id")
        if self.mesh is None or self.mesh_gateway is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable",
                },
            )
            return
        thread_id = request.get("threadId")
        text = request.get("text")
        if not isinstance(thread_id, str) or not isinstance(text, str):
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "threadId (string) and text (string) are required",
                },
            )
            return
        ok = self.mesh_gateway.resolve(thread_id, text)
        security_log("mesh_task_result", thread_id=thread_id, resolved=ok)
        await self._send_client(
            websocket, {"type": "mesh_task_result", "id": request_id, "resolved": ok}
        )

    async def _mesh_status(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        request_id = request.get("id")
        if self.mesh is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable",
                },
            )
            return
        project_id = request.get("projectId")
        if project_id:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_project_status",
                    "id": request_id,
                    "status": self.mesh.project_status(project_id),
                },
            )
            return
        await self._send_client(
            websocket,
            {
                "type": "mesh_list",
                "id": request_id,
                "projects": self.mesh.list_projects(),
            },
        )

    async def _mesh_send_agent(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        request_id = request.get("id")
        if self.mesh is None:
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "project mesh unavailable",
                },
            )
            return
        sender = request.get("sender") or "operator"
        recipient = request.get("recipient") or ""
        note = request.get("note") or ""
        if not isinstance(recipient, str) or not isinstance(note, str):
            await self._send_client(
                websocket,
                {
                    "type": "mesh_error",
                    "id": request_id,
                    "error": "recipient and note are required",
                },
            )
            return
        out = self.mesh.send_between_agents(sender, recipient, note)
        security_log(
            "mesh_agent_message",
            sender=sender,
            recipient=recipient,
            delivered=out.get("delivered"),
        )
        await self._send_client(
            websocket, {"type": "mesh_agent_message", "id": request_id, "sent": out}
        )

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._client_lock:
            clients = list(self.clients)
        if not clients:
            return
        await asyncio.gather(
            *(self._send_client(client, message) for client in clients),
            return_exceptions=True,
        )

    async def _send_client(
        self, client: ServerConnection, message: Dict[str, Any]
    ) -> None:
        try:
            await client.send(_json_line(message))
        except Exception:
            pass

    async def handle_client(
        self, websocket: ServerConnection, _path: Optional[str] = None
    ) -> None:
        trace_id = str(uuid.uuid4())[:8]
        TraceContext.set(trace_id)

        client_ip = (
            websocket.remote_address[0] if websocket.remote_address else "unknown"
        )

        allowed, rate_info = await self.rate_limiter.check(client_ip)
        if not allowed:
            security_log(
                "rate_limit_exceeded",
                client_ip=client_ip,
                retry_after=rate_info["retry_after"],
            )
            await websocket.close(code=4029, reason="Rate limit exceeded")
            return

        ws_headers = getattr(websocket, "request_headers", None)
        if ws_headers is None:
            ws_request = getattr(websocket, "request", None)
            ws_headers = getattr(ws_request, "headers", None) if ws_request else None

        # websockets>=12 exposes the handshake headers on websocket.request; the
        # legacy .origin attribute no longer exists, so the Origin header is
        # read from there. The browser extension connects from a
        # chrome-extension:// origin, the provider pages from their own.
        origin = None
        for header_name, header_value in (ws_headers or {}).items():
            if header_name.lower() == "origin":
                origin = header_value
                break
        if origin:
            expected_ext_id = (os.environ.get("SNAPGENT_EXTENSION_ID") or "").strip()
            expected_origins = set()
            if expected_ext_id:
                expected_origins.add(f"chrome-extension://{expected_ext_id}")
                expected_origins.add(f"moz-extension://{expected_ext_id}")
            is_extension = origin in expected_origins
            is_allowed = origin in self.allowed_origins
            if not (is_extension or is_allowed):
                security_log("origin_rejected", client_ip=client_ip, origin=origin)
                await websocket.close(code=4003, reason="Origin not allowed")
                return
        else:
            # No Origin header at all: only local CLI clients (tests, scripts)
            # may connect this way, and only when auth is enabled they still
            # need the bearer token. Extension sockets always carry Origin.
            pass

        auth_header = None
        for header_name, header_value in (ws_headers or {}).items():
            if header_name.lower() == "authorization":
                auth_header = header_value
                break

        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # Browsers cannot set an Authorization header on a WebSocket handshake,
        # so the extension passes the token as a query parameter instead.
        if not token:
            ws_request = getattr(websocket, "request", None)
            request_path = getattr(ws_request, "path", "") or ""
            query_token = parse_qs(urlparse(request_path).query).get("token", [""])[0]
            if query_token:
                token = query_token

        if self.auth.enabled and not self.auth.verify_token(token or ""):
            security_log("auth_failed", client_ip=client_ip, has_token=bool(token))
            await websocket.close(code=4001, reason="Authentication required")
            return

        async with self._client_lock:
            self.clients.add(websocket)
            self.client_info[websocket] = {
                "connected_at": time.time(),
                "client_ip": client_ip,
                "trace_id": trace_id,
            }
        self.metrics["clients_connected"] += 1

        security_log("client_connected", client_ip=client_ip, trace_id=trace_id)
        log("extension connected")

        await self._send_client(websocket, self._connected_message())

        try:
            async for raw in websocket:
                if len(raw) > MAX_MESSAGE_SIZE:
                    security_log(
                        "message_too_large", client_ip=client_ip, size=len(raw)
                    )
                    await self._send_client(
                        websocket, {"type": "error", "error": "Message too large"}
                    )
                    continue

                try:
                    request = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    await self._send_client(
                        websocket, {"type": "error", "error": "Invalid JSON request."}
                    )
                    continue

                if not isinstance(request, dict):
                    await self._send_client(
                        websocket,
                        {"type": "error", "error": "Request must be a JSON object."},
                    )
                    continue

                await self._handle_request(websocket, request, trace_id)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as error:
            log(f"Client handler error: {error}")
            security_log("client_handler_error", client_ip=client_ip, error=str(error))
        finally:
            async with self._client_lock:
                self.clients.discard(websocket)
                self.client_info.pop(websocket, None)
            log("extension disconnected")
            security_log("client_disconnected", client_ip=client_ip, trace_id=trace_id)
            TraceContext.clear()

    async def _handle_request(
        self, websocket: ServerConnection, request: Dict[str, Any], trace_id: str
    ) -> None:
        request_id = request.get("id")
        kind = request.get("type")

        self.metrics["requests_total"] += 1

        if kind == "ping":
            await self._send_client(websocket, {"type": "pong", "id": request_id})
        elif kind == "coord_stats":
            await self._send_client(
                websocket,
                {
                    "type": "coord_stats",
                    "id": request_id,
                    "providers": self.hub.stats() if self.hub else None,
                },
            )
        elif kind == "coord_prepare":
            await self._coord_prepare(websocket, request)
        elif kind == "a2a_send":
            await self._a2a_send(websocket, request, trace_id)
        elif kind == "a2a_agents":
            await self._a2a_agents(websocket, request)
        elif kind == "a2a_inbox":
            await self._a2a_inbox(websocket, request)
        elif kind == "mesh_create_project":
            await self._mesh_create_project(websocket, request)
        elif kind == "mesh_run_project":
            await self._mesh_run_project(websocket, request)
        elif kind == "mesh_parked":
            await self._mesh_park_open_tasks(websocket, request)
        elif kind == "mesh_task_result":
            await self._mesh_task_result(websocket, request)
        elif kind == "mesh_status":
            await self._mesh_status(websocket, request)
        elif kind == "mesh_send_agent":
            await self._mesh_send_agent(websocket, request)
        elif kind == "oep_hello":
            await self._oep_hello(websocket, request)
        elif kind == "oep_call_tool":
            await self._oep_call_tool(websocket, request, trace_id)
        elif kind == "studio_status":
            studio, studio_app, studio_proc = await self._studio_status()
            await self._send_client(
                websocket,
                {
                    "type": "studio_status",
                    "id": request_id,
                    "studio": studio,
                    "studio_app": studio_app,
                    "studio_proc": studio_proc,
                },
            )
        elif kind == "list_tools":
            await self._send_client(
                websocket,
                {
                    **self._connected_message(),
                    "type": "tools",
                    "id": request_id,
                },
            )
        elif kind == "call_tool":
            await self._call_tool(websocket, request, trace_id)
        elif kind == "restart_mcp":
            try:
                await self.stop_servers()
                await self.start_servers()
                response = {
                    "type": "mcp_status",
                    "id": request_id,
                    "ok": True,
                    "alive": any(server.alive for server in self.servers.values()),
                    "tools": self._catalog,
                    "servers": self._server_status(),
                }
            except (OSError, RuntimeError) as error:
                response = {
                    "type": "mcp_status",
                    "id": request_id,
                    "ok": False,
                    "alive": False,
                    "error": str(error),
                }
            await self._send_client(websocket, response)
            await self.broadcast(self._connected_message())
        else:
            await self._send_client(
                websocket,
                {
                    "type": "error",
                    "id": request_id,
                    "error": f"Unknown bridge request: {kind}",
                },
            )

    async def _oep_hello(
        self, websocket: ServerConnection, request: Dict[str, Any]
    ) -> None:
        """Establish an OEP session for this socket.

        Client sends its X25519 public key (base64). Bridge replies with its
        own public key. Both sides derive the same AES-256-GCM session key
        from the X25519 exchange + HKDF. The provider never sees either key.
        """
        request_id = request.get("id")
        if OEPSession is None:
            await self._send_client(
                websocket,
                {
                    "type": "oep_error",
                    "id": request_id,
                    "error": "OEP unavailable (cryptography not installed)",
                },
            )
            return
        pub_b64 = request.get("pub") or ""
        if not isinstance(pub_b64, str) or not pub_b64:
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": "pub required"},
            )
            return
        try:
            session = OEPSession()
            bridge_pub_b64 = session.on_hello(pub_b64)
        except Exception as exc:
            security_log("oep_hello_failed", error=str(exc))
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": str(exc)},
            )
            return
        info = self.client_info.get(websocket) or {}
        info["oep"] = session
        self.client_info[websocket] = info
        security_log("oep_established", trace_id=info.get("trace_id"))
        await self._send_client(
            websocket,
            {
                "type": "oep_ready",
                "id": request_id,
                "pub": bridge_pub_b64,
                "session_id": base64.b64encode(session.session_id).decode("ascii"),
            },
        )

    async def _oep_call_tool(
        self, websocket: ServerConnection, request: Dict[str, Any], trace_id: str
    ) -> None:
        """Call a tool with OEP-encrypted arguments and result.

        Request body:
            {"type":"oep_call_tool","id":N,"env":"[ENV:v1:...]"}
        Response body:
            {"type":"oep_result","id":N,"env":"[RES:v1:...]"}

        Plaintext envelope:
            {"name":str,"arguments":obj,"timeout_ms":int,"server":str?}
        """
        request_id = request.get("id")
        info = self.client_info.get(websocket) or {}
        session = info.get("oep")
        if not session or not session.established:
            await self._send_client(
                websocket,
                {
                    "type": "oep_error",
                    "id": request_id,
                    "error": "OEP session not established - send oep_hello first",
                },
            )
            return
        env = request.get("env") or ""
        if not isinstance(env, str) or not env:
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": "env required"},
            )
            return
        try:
            payload = session.oep.unwrap_envelope(env)
        except Exception as exc:
            security_log("oep_decrypt_failed", error=str(exc))
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": "decrypt failed"},
            )
            return
        if not isinstance(payload, dict):
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": "bad envelope"},
            )
            return

        name = payload.get("name")
        arguments = payload.get("arguments") or {}
        timeout_ms = payload.get("timeout_ms", 120000)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self._send_client(
                websocket,
                {
                    "type": "oep_error",
                    "id": request_id,
                    "error": "name and arguments required",
                },
            )
            return

        server, tool_name = self._resolve_tool(name)
        allowed = payload.get("server")
        if isinstance(allowed, str) and allowed:
            if server is None and "/" not in name:
                allowed_server = self.servers.get(allowed)
                if (
                    allowed_server
                    and allowed_server.alive
                    and any(t["name"] == name for t in allowed_server.tools)
                ):
                    server, tool_name = allowed_server, name
            if server is not None and server.server_id != allowed:
                await self._send_oep_result(
                    websocket,
                    session,
                    request_id,
                    {
                        "ok": False,
                        "kind": "unknown_tool",
                        "error": f"MCP tool not available for server: {name}",
                    },
                )
                return
        if not server:
            await self._send_oep_result(
                websocket,
                session,
                request_id,
                {"ok": False, "kind": "unknown_tool", "error": f"Unknown: {name}"},
            )
            return

        try:
            timeout_seconds = max(1.0, min(float(timeout_ms) / 1000, 300.0))
        except (TypeError, ValueError):
            timeout_seconds = 120.0

        start_time = time.time()
        self.metrics["tools_called"] += 1
        try:
            result = await server.call_tool(tool_name, arguments, timeout_seconds)
            duration_ms = int((time.time() - start_time) * 1000)
            text_parts = []
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    if isinstance(item.get("text"), str):
                        text_parts.append(item["text"])
            text = "\n".join(text_parts)
            structured = result.get("structuredContent")
            if not text and structured is not None:
                text = json.dumps(structured, ensure_ascii=False)
            await self._send_oep_result(
                websocket,
                session,
                request_id,
                {
                    "ok": not result.get("isError"),
                    "text": text,
                    "duration_ms": duration_ms,
                },
            )
            security_log(
                "oep_tool_called",
                tool=name,
                server=server.server_id,
                duration_ms=duration_ms,
                success=not result.get("isError"),
            )
        except asyncio.TimeoutError:
            self.metrics["tools_errors"] += 1
            await self._send_oep_result(
                websocket,
                session,
                request_id,
                {"ok": False, "kind": "timeout", "error": "tool call timed out"},
            )
        except (RuntimeError, OSError, ValueError) as exc:
            self.metrics["tools_errors"] += 1
            await self._send_oep_result(
                websocket,
                session,
                request_id,
                {"ok": False, "kind": "tool_error", "error": str(exc)},
            )

    async def _send_oep_result(
        self,
        websocket: ServerConnection,
        session: Any,
        request_id: Any,
        payload: Dict[str, Any],
    ) -> None:
        try:
            env = session.oep.wrap_result(payload)
        except Exception as exc:
            security_log("oep_encrypt_failed", error=str(exc))
            await self._send_client(
                websocket,
                {"type": "oep_error", "id": request_id, "error": "encrypt failed"},
            )
            return
        await self._send_client(
            websocket,
            {"type": "oep_result", "id": request_id, "env": env},
        )

    async def _call_tool(
        self, websocket: ServerConnection, request: Dict[str, Any], trace_id: str
    ) -> None:
        request_id = request.get("id")
        name = request.get("name")
        arguments = request.get("arguments") or {}
        timeout = request.get("timeout", 120000)

        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "error": "Invalid tool call.",
                },
            )
            return

        try:
            timeout_seconds = max(1.0, min(float(timeout) / 1000, 300.0))
        except (TypeError, ValueError):
            timeout_seconds = 120.0

        server, tool_name = self._resolve_tool(name)
        allowed = request.get("server")
        if isinstance(allowed, str) and allowed:
            if server is None and "/" not in name:
                allowed_server = self.servers.get(allowed)
                if (
                    allowed_server
                    and allowed_server.alive
                    and any(t["name"] == name for t in allowed_server.tools)
                ):
                    server, tool_name = allowed_server, name
            if server is not None and server.server_id != allowed:
                await self._send_client(
                    websocket,
                    {
                        "type": "tool_result",
                        "id": request_id,
                        "ok": False,
                        "kind": "unknown_tool",
                        "error": f"MCP tool not available for the selected server: {name}",
                    },
                )
                return

        if not server:
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "kind": "unknown_tool",
                    "error": f"Unknown or unavailable MCP tool: {name}",
                },
            )
            return

        start_time = time.time()
        self.metrics["tools_called"] += 1

        try:
            result = await server.call_tool(tool_name, arguments, timeout_seconds)
            duration_ms = int((time.time() - start_time) * 1000)

            text_parts = []
            images = []
            for item in result.get("content", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
                elif item.get("type") == "image" and isinstance(item.get("data"), str):
                    images.append(
                        {
                            "data": item["data"],
                            "mimeType": item.get("mimeType", "image/png"),
                        }
                    )
            structured = result.get("structuredContent")
            if not text_parts and structured is not None:
                text_parts.append(json.dumps(structured, ensure_ascii=False))
            text = "\n".join(text_parts)

            if result.get("isError"):
                self.metrics["tools_errors"] += 1
                await self._send_client(
                    websocket,
                    {
                        "type": "tool_result",
                        "id": request_id,
                        "ok": False,
                        "error": text or "MCP tool failed.",
                    },
                )
            else:
                await self._send_client(
                    websocket,
                    {
                        "type": "tool_result",
                        "id": request_id,
                        "ok": True,
                        "text": text,
                        "images": images,
                    },
                )
            log(f"<- {name} ({duration_ms}ms) ({text[:120].replace(chr(10), ' ')})")
            security_log(
                "tool_called",
                tool=name,
                server=server.server_id,
                duration_ms=duration_ms,
                success=not result.get("isError"),
            )
        except asyncio.TimeoutError:
            self.metrics["tools_errors"] += 1
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "kind": "timeout",
                    "error": "Tool call timed out",
                },
            )
            log(f"<- {name} TIMEOUT")
            security_log("tool_timeout", tool=name, server=server.server_id)
        except (RuntimeError, OSError, ValueError) as error:
            self.metrics["tools_errors"] += 1
            message = str(error)
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "kind": "tool_error",
                    "error": message,
                },
            )
            log(f"<- {name} ERROR: {message[:MAX_LOG_CHARS]}")
            security_log(
                "tool_error", tool=name, server=server.server_id, error=message
            )

    def _resolve_tool(self, name: str) -> Tuple[Optional[MCPServer], str]:
        if "/" in name:
            server_id, bare_name = name.split("/", 1)
            server = self.servers.get(server_id)
            if (
                server
                and server.alive
                and any(tool["name"] == bare_name for tool in server.tools)
            ):
                return server, bare_name
        matches = [
            (server, tool)
            for server in self.servers.values()
            if server.alive
            for tool in server.tools
            if tool["name"] == name
        ]
        if len(matches) == 1:
            return matches[0][0], name
        return None, name

    async def _studio_status(
        self,
    ) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
        studio_server = self.servers.get("roblox")
        studio_proc = _process_running(
            "RobloxStudioBeta.exe" if os.name == "nt" else "RobloxStudio"
        )
        if not studio_server or not studio_server.alive:
            return None, None, studio_proc
        if not any(tool["name"] == "get_studio_state" for tool in studio_server.tools):
            return None, None, studio_proc
        try:
            result = await studio_server.call_tool("get_studio_state", {}, 8)
        except (asyncio.TimeoutError, RuntimeError, OSError, ValueError):
            return False, studio_proc, studio_proc
        text = " ".join(
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if result.get("isError") or re.search(
            r"Unable to find an active Studio instance|previously active Studio has disconnected",
            text,
            re.IGNORECASE,
        ):
            return False, studio_proc, studio_proc
        return True, True, studio_proc

    async def _change_server(
        self, websocket: ServerConnection, request: Dict[str, Any], remove: bool
    ) -> None:
        request_id = request.get("id")
        server_id = request.get("server_id")
        if (
            not isinstance(server_id, str)
            or server_id in ("roblox", "vscode")
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", server_id)
        ):
            await self._send_client(
                websocket,
                {
                    "type": "server_changed",
                    "id": request_id,
                    "ok": False,
                    "error": "Invalid or protected MCP server id.",
                },
            )
            return
        try:
            config = (
                json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if CONFIG_PATH.exists()
                else {}
            )
            servers = config.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise RuntimeError("mcpServers must be a JSON object.")
            if remove:
                servers.pop(server_id, None)
            else:
                command = request.get("command")
                args = request.get("args", [])
                env = request.get("env", {})
                sandbox = request.get("sandbox", "none")
                if not isinstance(command, str) or not command.strip():
                    raise RuntimeError("MCP server command is required.")
                if not isinstance(args, list) or not all(
                    isinstance(arg, str) for arg in args
                ):
                    raise RuntimeError("MCP server args must be an array of strings.")
                if not isinstance(env, dict) or not all(
                    isinstance(k, str) for k in env
                ):
                    raise RuntimeError("MCP server env must be an object.")
                servers[server_id] = {
                    "command": command,
                    "args": args,
                    "env": env,
                    "sandbox": sandbox,
                }
            temp_path = CONFIG_PATH.with_suffix(".json.tmp")
            temp_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(CONFIG_PATH)
            await self.stop_servers()
            await self.start_servers()
            await self._send_client(
                websocket,
                {
                    "type": "server_changed",
                    "id": request_id,
                    "ok": True,
                    "restarting": False,
                },
            )
            await self.broadcast(self._connected_message())
            security_log(
                "server_config_changed",
                server_id=server_id,
                action="remove" if remove else "add",
            )
        except (OSError, json.JSONDecodeError, RuntimeError) as error:
            await self._send_client(
                websocket,
                {
                    "type": "server_changed",
                    "id": request_id,
                    "ok": False,
                    "error": str(error),
                },
            )

    async def start_health_server(self) -> None:
        if self.health_runner is not None:
            return
        if web is None:
            log("aiohttp not available, health server disabled")
            return

        async def health_handler(request: web.Request) -> web.Response:
            origin = request.headers.get("Origin", "")
            expected_ext_id = (os.environ.get("SNAPGENT_EXTENSION_ID") or "").strip()
            allowed_origins = set()
            if expected_ext_id:
                allowed_origins.add(f"chrome-extension://{expected_ext_id}")
                allowed_origins.add(f"moz-extension://{expected_ext_id}")
            cors_headers = {"Vary": "Origin"}
            if not origin:
                cors_headers.update(
                    {
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization",
                        "Access-Control-Allow-Private-Network": "true",
                        "Access-Control-Max-Age": "600",
                    }
                )
            elif origin in allowed_origins:
                cors_headers.update(
                    {
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization",
                        "Access-Control-Allow-Private-Network": "true",
                        "Access-Control-Max-Age": "600",
                    }
                )
            if request.method == "OPTIONS":
                if origin and origin not in allowed_origins:
                    return web.Response(status=403, headers=cors_headers)
                return web.Response(status=204, headers=cors_headers)
            return web.json_response(
                {
                    "status": "healthy",
                    "uptime_seconds": int(time.time() - self.metrics["start_time"]),
                    "version": BRIDGE_VERSION,
                    "servers": len(self.servers),
                    "active_servers": sum(1 for s in self.servers.values() if s.alive),
                    "clients": len(self.clients),
                    "metrics": self.metrics,
                },
                headers=cors_headers,
            )

        async def metrics_handler(request: web.Request) -> web.Response:
            lines = [
                f"snapgent_bridge_requests_total {self.metrics['requests_total']}",
                f"snapgent_bridge_requests_errors_total {self.metrics['requests_errors']}",
                f"snapgent_bridge_tools_called_total {self.metrics['tools_called']}",
                f"snapgent_bridge_tools_errors_total {self.metrics['tools_errors']}",
                f"snapgent_bridge_clients_connected {self.metrics['clients_connected']}",
                f"snapgent_bridge_servers_started_total {self.metrics['servers_started']}",
                f"snapgent_bridge_servers_failed_total {self.metrics['servers_failed']}",
                f"snapgent_bridge_uptime_seconds {int(time.time() - self.metrics['start_time'])}",
            ]
            for server in self.servers.values():
                lines.append(
                    f'snapgent_bridge_server_alive{{server="{server.server_id}"}} {1 if server.alive else 0}'
                )
                lines.append(
                    f'snapgent_bridge_server_tools{{server="{server.server_id}"}} {len(server.tools) if server.alive else 0}'
                )
            return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

        async def servers_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "servers": self._server_status(),
                    "catalog": self._catalog,
                }
            )

        async def clients_handler(request: web.Request) -> web.Response:
            clients_info = []
            for ws, info in self.client_info.items():
                clients_info.append(
                    {
                        "connected_at": info["connected_at"],
                        "duration_seconds": int(time.time() - info["connected_at"]),
                        "client_ip": info["client_ip"],
                        "trace_id": info.get("trace_id", ""),
                    }
                )
            return web.json_response({"clients": clients_info})

        async def auth_token_handler(request: web.Request) -> web.Response:
            """Hand the local handshake secret to the extension.

            A browser cannot set an Authorization header on a WebSocket
            handshake, so the extension reads the secret here and passes it as
            a query parameter on the socket. This endpoint is deliberately
            narrow:

            * the health server binds to 127.0.0.1 only, so only local
              processes can reach it;
            * no CORS header is sent, so a normal web page cannot read the
              response cross-origin (the extension reads it because MV3 host
              permissions bypass CORS for its own fetches);
            * a request carrying an unexpected Origin is refused outright, so
              a page that manages to issue the request still cannot take the
              secret out of the browser.
            """
            origin = request.headers.get("Origin", "")
            expected_ext_id = (os.environ.get("SNAPGENT_EXTENSION_ID") or "").strip()
            allowed_origins = set()
            if expected_ext_id:
                allowed_origins.add(f"chrome-extension://{expected_ext_id}")
                allowed_origins.add(f"moz-extension://{expected_ext_id}")

            def _cors_headers() -> Dict[str, str]:
                # The extension's service worker fetches with no Origin header
                # (Sec-Fetch-Site: none). Echoing "*" in that case is safe:
                # a normal web page cannot produce an Origin-less request, so
                # this does not expose the secret to web origins.
                headers = {"Vary": "Origin"}
                if not origin:
                    headers.update(
                        {
                            "Access-Control-Allow-Origin": "*",
                            "Access-Control-Allow-Methods": "GET, OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type, Authorization",
                            "Access-Control-Allow-Private-Network": "true",
                            "Access-Control-Max-Age": "600",
                        }
                    )
                elif origin in allowed_origins:
                    headers.update(
                        {
                            "Access-Control-Allow-Origin": origin,
                            "Access-Control-Allow-Methods": "GET, OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type, Authorization",
                            "Access-Control-Allow-Private-Network": "true",
                            "Access-Control-Max-Age": "600",
                        }
                    )
                return headers

            # Preflight (Private Network Access / CORS).
            if request.method == "OPTIONS":
                if origin and origin not in allowed_origins:
                    return web.Response(status=403, headers=_cors_headers())
                return web.Response(status=204, headers=_cors_headers())

            # A non-empty, unexpected Origin (e.g. a web page) is refused so
            # the secret never leaves to a browser origin. An empty Origin is
            # the extension's own service worker and is allowed.
            if origin and origin not in allowed_origins:
                security_log(
                    f"auth_token_refused origin={origin!r} expected={expected_ext_id!r}",
                    path=request.path,
                    client_ip=request.remote,
                    origin=origin[:120] or "<none>",
                )
                return web.json_response(
                    {
                        "error": "Forbidden",
                        "reason": "extension_origin_mismatch",
                        "expected_origin": (
                            f"chrome-extension://{expected_ext_id}"
                            if expected_ext_id
                            else ""
                        ),
                        "seen_origin": origin[:120],
                        "hint": (
                            "Reload the unpacked extension (chrome://extensions "
                            "-> Reload) so it picks up the key in manifest.json."
                        ),
                    },
                    status=403,
                    headers=_cors_headers(),
                )
            return web.json_response(
                {
                    "token": self.auth.get_auth_header_value(),
                    "enabled": self.auth.enabled,
                },
                headers=_cors_headers(),
            )

        @web.middleware
        async def auth_middleware(request: web.Request, handler):
            # Rate limiting for all endpoints
            client_ip = request.remote or "unknown"
            allowed, rate_info = await self.rate_limiter.check(client_ip)
            if not allowed:
                security_log(
                    "rate_limit_exceeded",
                    client_ip=client_ip,
                    retry_after=rate_info["retry_after"],
                    path=request.path,
                )
                return web.json_response(
                    {
                        "error": "Rate limit exceeded",
                        "retry_after": rate_info["retry_after"],
                        "limit": rate_info["limit"],
                    },
                    status=429,
                    headers={"Retry-After": str(rate_info["retry_after"])},
                )

            # Public endpoints - no auth required
            if request.path in ("/health", "/metrics", "/auth-token"):
                return await handler(request)

            # Protected endpoints - require auth
            if self.auth.enabled:
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    return web.json_response(
                        {"error": "Authentication required"}, status=401
                    )
                token = auth_header[7:]
                if not self.auth.verify_token(token):
                    security_log(
                        "auth_failed", path=request.path, client_ip=request.remote
                    )
                    return web.json_response({"error": "Invalid token"}, status=403)
            return await handler(request)

        app = web.Application(middlewares=[auth_middleware])
        app.router.add_get("/health", health_handler)
        app.router.add_route("OPTIONS", "/health", health_handler)
        app.router.add_get("/metrics", metrics_handler)
        app.router.add_get("/servers", servers_handler)
        app.router.add_get("/clients", clients_handler)
        app.router.add_get("/auth-token", auth_token_handler)
        app.router.add_route("OPTIONS", "/auth-token", auth_token_handler)

        self.health_server = app
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        site = web.TCPSite(self.health_runner, HEALTH_HOST, HEALTH_PORT)
        await site.start()
        log(f"Health server listening on http://{HEALTH_HOST}:{HEALTH_PORT}")

    async def stop_health_server(self) -> None:
        if self.health_runner:
            await self.health_runner.cleanup()
            self.health_runner = None
            self.health_server = None

    async def watch_config(self) -> None:
        if watchfiles is None:
            log("watchfiles not available, config hot reload disabled")
            return

        if not self.hot_reload_enabled:
            return

        self._config_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0

        async def _watch():
            try:
                async for changes in watchfiles.awatch(CONFIG_PATH, debounce=500):
                    if not self.hot_reload_enabled:
                        break
                    current_mtime = (
                        CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
                    )
                    if current_mtime != self._config_mtime:
                        self._config_mtime = current_mtime
                        log("Config file changed, reloading...")
                        security_log("config_reload_triggered")
                        await self.stop_servers()
                        await self.start_servers()
                        await self.broadcast(self._connected_message())
                        log("Config reloaded and servers restarted")
                        security_log("config_reload_completed")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log(f"Config watcher error: {error}")
                security_log("config_watcher_error", error=str(error))

        self.config_watcher_task = asyncio.create_task(_watch())

    async def stop_config_watcher(self) -> None:
        if self.config_watcher_task:
            self.config_watcher_task.cancel()
            try:
                await self.config_watcher_task
            except asyncio.CancelledError:
                pass
            self.config_watcher_task = None


def _process_running(process_name: str) -> Optional[bool]:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            return process_name.lower() in result.stdout.lower()
        result = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


from urllib.parse import urlparse, unquote, parse_qs


async def run() -> None:
    if websockets_serve is None:
        raise RuntimeError(
            "The 'websockets' package is required. Install it with "
            f"'{sys.executable} -m pip install websockets'."
        )

    bridge = Bridge()

    await bridge.start_servers()
    await bridge.start_health_server()
    await bridge.watch_config()

    server = await websockets_serve(
        bridge.handle_client,
        HOST,
        PORT,
        max_size=MAX_MESSAGE_SIZE,
        ping_interval=20,
        ping_timeout=20,
    )

    log(f"snapgent bridge v{BRIDGE_VERSION}")
    log(
        f"listening on ws://{HOST}:{PORT} - load the extension and open a supported AI chat"
    )
    log(f"health server on http://{HEALTH_HOST}:{HEALTH_PORT}")
    if bridge.auth.enabled:
        log(f"auth enabled - token: {bridge.auth.get_auth_header_value()[:8]}...")
    else:
        log("auth disabled")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def _monitor_servers(bridge: Bridge) -> None:
        while True:
            await asyncio.sleep(5)
            changed = False
            for server in bridge.servers.values():
                if not server.alive:
                    log(f"[{server.server_id}] MCP server disconnected; restarting.")
                    security_log("mcp_auto_restart", server=server.server_id)
                    await server.start()
                    changed = True
            if changed:
                bridge._rebuild_catalog()
                bridge._log_health()
                await bridge.broadcast(bridge._connected_message())
            bridge.rate_limiter.cleanup()

    monitor_task = asyncio.create_task(_monitor_servers(bridge))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop_event.wait()
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await bridge.stop_config_watcher()
        await bridge.stop_health_server()
        server.close()
        await server.wait_closed()
        await bridge.stop_servers()
        log("bridge stopped")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (OSError, RuntimeError, ValueError) as error:
        log(f"FATAL: {error}")
        security_log("fatal_error", error=str(error))
        raise SystemExit(1)
