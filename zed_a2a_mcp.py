"""zed_a2a_mcp.py — stdio MCP server for Zed IDE → Snapgent bridge (A2A + mesh).

Connects a Zed agent through the local Snapgent bridge WebSocket
(ws://127.0.0.1:17613) using standard library + websockets.
All logging goes to stderr; stdout carries ONLY JSON-RPC lines.
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "zed-a2a-mcp"
SERVER_VERSION = "1.0.0"
DEFAULT_WS_URL = "ws://127.0.0.1:17613"
DEFAULT_HEALTH_URL = "http://127.0.0.1:17614/health"

# ---------------------------------------------------------------------------
# Config loader — never exposes the secret outside this module
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load bridge config from SNAPGENT_BRIDGE_CONFIG or default path."""
    config_path_str = os.environ.get(
        "SNAPGENT_BRIDGE_CONFIG",
        str(Path(__file__).resolve().parent / "config.json"),
    )
    config_path = Path(config_path_str)
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        return json.loads(text)
    except Exception:
        return {}


def _build_bridge_url(secret: str) -> str:
    base = os.environ.get("SNAPGENT_BRIDGE_URL", DEFAULT_WS_URL)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={secret}"


def _get_secret() -> str:
    cfg = _load_config()
    env_secret = os.environ.get("SNAPGENT_BRIDGE_TOKEN")
    if env_secret:
        return env_secret
    auth_section = cfg.get("bridge", {}).get("auth", {})
    secret = auth_section.get("secret", "")
    if not secret:
        raise RuntimeError("no bridge auth secret available")
    return secret


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "bridge_health",
        "description": "Check whether the Snapgent bridge HTTP health endpoint is responding.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "a2a_agents",
        "description": "List registered A2A agents on the bridge.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "a2a_inbox",
        "description": "Read the message inbox for an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "default": "zed"},
            },
        },
    },
    {
        "name": "a2a_send",
        "description": "Send a note message between A2A agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "note": {"type": "string"},
                "sender": {"type": "string", "default": "zed"},
                "message_type": {"type": "string", "default": "note"},
            },
            "required": ["recipient", "note"],
        },
    },
    {
        "name": "coord_prepare",
        "description": "Prepare a coordination request for a model provider.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["chatgpt", "deepseek", "gemini", "qwen"],
                },
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
            },
            "required": ["provider", "messages"],
        },
    },
    {
        "name": "coord_stats",
        "description": "Return current coordination statistics.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mesh_create_project",
        "description": "Create a new mesh project with tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "prompt": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notify_peers": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "title", "prompt"],
                    },
                },
            },
            "required": ["title", "goal", "tasks"],
        },
    },
    {
        "name": "mesh_run_project",
        "description": "Start execution of an existing mesh project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "max_concurrent": {"type": "integer", "default": 3},
                "max_attempts": {"type": "integer", "default": 2},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "mesh_status",
        "description": "Query mesh project status or list all projects when no project_id is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
            },
        },
    },
    {
        "name": "mesh_send_agent",
        "description": "Send a note between mesh agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "note": {"type": "string"},
                "sender": {"type": "string", "default": "zed"},
            },
            "required": ["recipient", "note"],
        },
    },
]


# ---------------------------------------------------------------------------
# Bridge client helpers
# ---------------------------------------------------------------------------


async def _send_recv(frame: dict, timeout: float = 10.0) -> dict:
    """Send one JSON frame to the bridge and return the matching reply."""
    secret = _get_secret()
    url = _build_bridge_url(secret)

    try:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            from websockets.client import connect  # type: ignore[no-redef]

        async with connect(url, open_timeout=5, close_timeout=2) as ws:
            await ws.send(json.dumps(frame, ensure_ascii=False))
            expected_id = frame.get("id")
            deadline = time.monotonic() + timeout
            reply = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "ok": False,
                        "error": "bridge websocket timeout waiting for reply",
                    }
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                try:
                    candidate = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(candidate, dict):
                    continue
                if expected_id is None or candidate.get("id") == expected_id:
                    reply = candidate
                    break
                # Ignore the "connected" handshake and any broadcast frame.
            kind = reply.get("type", "")
            if kind.endswith("_error") or kind == "error":
                err_msg = reply.get("error", str(reply))
                return {"ok": False, "error": err_msg}
            return reply
    except asyncio.TimeoutError:
        return {"ok": False, "error": "bridge websocket timeout"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "bridge unreachable (connection refused)"}
    except OSError as exc:
        return {"ok": False, "error": f"bridge connection error: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_REQUEST_ID_COUNTER = 0


def _next_id() -> int:
    global _REQUEST_ID_COUNTER
    _REQUEST_ID_COUNTER += 1
    return _REQUEST_ID_COUNTER


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_bridge_health(**_kwargs: Any) -> dict:
    url = os.environ.get("SNAPGENT_BRIDGE_HEALTH_URL", DEFAULT_HEALTH_URL)
    try:
        req = urllib.request.Request(url, method="GET")
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(3):
            resp = await loop.run_in_executor(None, urllib.request.urlopen, req)
            body = resp.read().decode("utf-8")
            return {"ok": True, **json.loads(body)}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "health check timeout"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "health endpoint unreachable"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _tool_a2a_agents(**_kwargs: Any) -> dict:
    frame = {"type": "a2a_agents", "id": _next_id()}
    reply = await _send_recv(frame)
    if not reply.get("ok", True) and "error" in reply and "ok" not in reply:
        # _send_recv already wraps errors; pass through
        return reply
    return {"ok": True, **reply}


async def _tool_a2a_inbox(agent: str = "zed", **_kwargs: Any) -> dict:
    frame = {"type": "a2a_inbox", "id": _next_id(), "agent": agent}
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_a2a_send(
    recipient: str,
    note: str,
    sender: str = "zed",
    message_type: str = "note",
    **_kwargs: Any,
) -> dict:
    frame = {
        "type": "a2a_send",
        "id": _next_id(),
        "sender": sender,
        "recipient": recipient,
        "messageType": message_type,
        "payload": {"text": note},
    }
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_coord_prepare(provider: str, messages: list, **_kwargs: Any) -> dict:
    frame = {
        "type": "coord_prepare",
        "id": _next_id(),
        "provider": provider,
        "messages": messages,
    }
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_coord_stats(**_kwargs: Any) -> dict:
    frame = {"type": "coord_stats", "id": _next_id()}
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_mesh_create_project(
    title: str,
    goal: str,
    tasks: list,
    **_kwargs: Any,
) -> dict:
    frame = {
        "type": "mesh_create_project",
        "id": _next_id(),
        "title": title,
        "goal": goal,
        "tasks": tasks,
    }
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_mesh_run_project(
    project_id: str,
    max_concurrent: int = 3,
    max_attempts: int = 2,
    **_kwargs: Any,
) -> dict:
    frame = {
        "type": "mesh_run_project",
        "id": _next_id(),
        "projectId": project_id,
        "maxConcurrent": max_concurrent,
        "maxAttempts": max_attempts,
    }
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_mesh_status(project_id: str | None = None, **_kwargs: Any) -> dict:
    frame: dict[str, Any] = {"type": "mesh_status", "id": _next_id()}
    if project_id is not None:
        frame["projectId"] = project_id
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


async def _tool_mesh_send_agent(
    recipient: str,
    note: str,
    sender: str = "zed",
    **_kwargs: Any,
) -> dict:
    frame = {
        "type": "mesh_send_agent",
        "id": _next_id(),
        "sender": sender,
        "recipient": recipient,
        "note": note,
    }
    reply = await _send_recv(frame)
    if "error" in reply and not reply.get("ok", True):
        return reply
    return {"ok": True, **reply}


# Map tool name → coroutine
_TOOL_REGISTRY: dict[str, object] = {
    "bridge_health": _tool_bridge_health,
    "a2a_agents": _tool_a2a_agents,
    "a2a_inbox": _tool_a2a_inbox,
    "a2a_send": _tool_a2a_send,
    "coord_prepare": _tool_coord_prepare,
    "coord_stats": _tool_coord_stats,
    "mesh_create_project": _tool_mesh_create_project,
    "mesh_run_project": _tool_mesh_run_project,
    "mesh_status": _tool_mesh_status,
    "mesh_send_agent": _tool_mesh_send_agent,
}


# ---------------------------------------------------------------------------
# JSON-RPC request handler (mirrors git_mcp.py handle_request pattern)
# ---------------------------------------------------------------------------


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    method = request.get("method")
    params = request.get("params", {})
    request_id = request.get("id")

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
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
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
            fn = _TOOL_REGISTRY.get(tool_name)
            if not fn:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            result = await fn(**arguments)
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
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(exc)},
        }


# ---------------------------------------------------------------------------
# Stdio main loop
# ---------------------------------------------------------------------------


async def _read_stdin_lines():
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
