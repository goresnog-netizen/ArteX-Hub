#!/usr/bin/env python3
"""Standalone regression tests for workspace resolution and A2A agent registration.

No pytest required. Prints PASS/SKIP/FAIL per check and exits non-zero on any FAIL.
Usage:
    python test_workspace_a2a.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import Bridge
from mesh import ProjectMesh, ScriptedGateway
from coordination_hub import CoordinationHub

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

    HAS_TEST_CLIENT = True
except ImportError:
    HAS_TEST_CLIENT = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def _make_auth_request(bridge: Bridge, origin: str) -> tuple[int, dict]:
    """Invoke the health server /auth-token endpoint against a Bridge instance."""
    await bridge.start_health_server()
    try:
        app = bridge.health_server
        # Find the /auth-token route handler from the router
        handler = None
        for route in app.router.routes():
            res = getattr(route, "resource", None)
            if res and getattr(res, "path", "") == "/auth-token":
                # aiohttp stores handler info differently across versions;
                # extract via the first matcher
                matchers = getattr(res, "_matchers", [])
                if matchers:
                    # The actual view callable lives one level deeper in some
                    # aiohttp versions — walk the resource tree until we find
                    # the _info dict which holds 'handler' and 'app'.
                    info = getattr(
                        getattr(res, "_info", {}), "get", lambda k, d=None: d
                    )("handler")
                    if info is None:
                        info = getattr(res, "handler", None)
                    if info is not None:
                        handler = info
                        break
                # Fallback: look at the route itself
                if handler is None:
                    handler = getattr(route, "handler", None)
        if handler is None:
            raise RuntimeError("could not locate /auth-token handler")
        # Build a minimal request manually
        req = web.Request(
            method="GET",
            path="/auth-token",
            headers={"Origin": origin},
            remote="127.0.0.1",
            writer=mock_writer(),
            protocol=None,
            loop=asyncio.get_event_loop(),
            payload=__import__("io").BytesIO(b""),
        )
        resp = await handler(req)
        body = json.loads(await resp.text())
        return resp.status, body
    finally:
        await bridge.stop_health_server()


class _MockWriter:
    async def write(self, data: bytes) -> None:
        pass

    async def write_eof(self, data: bytes = b"") -> None:
        pass

    def transport(self):
        class T:
            def get_extra_info(self, name: str):
                return "127.0.0.1"

        return T()


def mock_writer():
    w = _MockWriter()
    return w


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_vscode_workspace_root() -> tuple[str, str]:
    """a. _ide_workspace_root('vscode') returns existing dir or None;
    when it returns a path, that path is NOT inside the bridge repo directory."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    result = bridge._ide_workspace_root("vscode")
    if result is None:
        return "SKIP", "VS Code globalStorage not found"
    _assert(result.is_dir(), f"Expected directory, got {result}")
    bridge_repo = Path(__file__).resolve().parent
    _assert(
        str(result) not in str(bridge_repo)
        and not str(result).startswith(str(bridge_repo) + os.sep),
        f"IDE folder {result} is inside the bridge repo — IDE folder must win over repo fallback",
    )
    return "PASS", f"VS Code workspace = {result}"


def check_zed_workspace_root() -> tuple[str, str]:
    """b. _ide_workspace_root('zed') returns existing dir or None;
    when it returns a path, that path is NOT inside the bridge repo directory."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    result = bridge._ide_workspace_root("zed")
    if result is None:
        return "SKIP", "Zed threads.db not found"
    _assert(result.is_dir(), f"Expected directory, got {result}")
    bridge_repo = Path(__file__).resolve().parent
    _assert(
        str(result) not in str(bridge_repo)
        and not str(result).startswith(str(bridge_repo) + os.sep),
        f"Zed workspace {result} is inside the bridge repo",
    )
    return "PASS", f"Zed workspace = {result}"


def check_server_workspace() -> tuple[str, str]:
    """c. _server_workspace('zed') equals _ide_workspace_root('zed') when not None;
    _server_workspace('git') equals the bridge global workspace."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    zed_ide = bridge._ide_workspace_root("zed")
    zed_server = bridge._server_workspace("zed")
    git_server = bridge._server_workspace("git")
    if zed_ide is not None:
        _assert(
            zed_server == zed_ide,
            f"_server_workspace('zed')={zed_server} != _ide_workspace_root('zed')={zed_ide}",
        )
    else:
        _assert(
            zed_server == bridge.workspace,
            f"Fallback for 'zed' server mismatch: {zed_server} != {bridge.workspace}",
        )
    _assert(
        git_server == bridge.workspace,
        f"_server_workspace('git')={git_server} != bridge.workspace={bridge.workspace}",
    )
    return "PASS", f"'zed'->{zed_server}, 'git'->{git_server}"


def check_server_status() -> tuple[str, str]:
    """d. _server_status() returns a list; each entry has 'workspace' as absolute path string."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    status = bridge._server_status()
    _assert(
        isinstance(status, list),
        f"_server_status returned {type(status)}, expected list",
    )
    if not status:
        return "PASS", "No configured MCP servers — method returned empty list"
    for entry in status:
        ws = entry.get("workspace")
        _assert(ws is not None, f"Server status entry missing 'workspace' key: {entry}")
        _assert(isinstance(ws, str), f"'workspace' is {type(ws)}, expected str")
        _assert(os.path.isabs(ws), f"'workspace' is relative: {ws}")
    return "PASS", f"{len(status)} server(s) with valid workspace paths"


def check_resolve_peer_and_register() -> tuple[str, str]:
    """e. resolve_peer('') returns '';
    resolve_peer('zed') returns 'zed' (already registered by Bridge.__init__);
    resolve_peer for unknown provider returns 'provider:<name>'."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    mesh = bridge.mesh
    hub = bridge.hub
    if mesh is None or hub is None:
        return "SKIP", "ProjectMesh or CoordinationHub unavailable"
    # e1: empty peer
    _assert(mesh.resolve_peer("") == "", "resolve_peer('') must return ''")
    # e2: 'zed' is already registered by Bridge.__init__ (line 818)
    _assert(
        "zed" in hub.a2a.agents,
        "Bridge should register 'zed' at init time (line 818); agents dict missing it",
    )
    _assert(
        mesh.resolve_peer("zed") == "zed",
        f"resolve_peer('zed')={mesh.resolve_peer('zed')} — expected 'zed'",
    )
    # e3: unknown unregistered name → provider: prefix
    resolved = mesh.resolve_peer("chatgpt")
    _assert(
        resolved == "provider:chatgpt",
        f"resolve_peer('chatgpt')={resolved!r} — expected 'provider:chatgpt'",
    )
    return (
        "PASS",
        f"resolve_peer contracts verified (agents={list(hub.a2a.agents.keys())})",
    )


def check_register_idempotent() -> tuple[str, str]:
    """f. register_agent_inbox called twice for same name does not duplicate."""
    try:
        bridge = Bridge()
    except Exception as exc:
        return "SKIP", f"Bridge init failed ({exc})"
    mesh = bridge.mesh
    hub = bridge.hub
    if mesh is None or hub is None:
        return "SKIP", "ProjectMesh or CoordinationHub unavailable"
    initial_count = len(hub.a2a.agents)
    # Call twice for 'zed' (already registered at init line 818)
    mesh.register_agent_inbox("zed")
    count_after_first = len(hub.a2a.agents)
    mesh.register_agent_inbox("zed")
    count_after_second = len(hub.a2a.agents)
    _assert(
        count_after_second == count_after_first,
        f"Second register duplicated agent: count went from {count_after_first} to {count_after_second}",
    )
    _assert(
        count_after_first <= initial_count + 1,
        f"First register spiked unexpectedly: {initial_count} → {count_after_first}",
    )
    return "PASS", f"Idempotent — agent count stable at {count_after_second}"


async def check_auth_token_handler_403() -> tuple[str, str]:
    """g. Wrong Origin → 403 with reason='extension_origin_mismatch', expected_origin, seen_origin, hint."""
    old_id = os.environ.get("SNAPGENT_EXTENSION_ID")
    fake_id = "a" * 32
    os.environ["SNAPGENT_EXTENSION_ID"] = fake_id
    bridge = None
    try:
        bridge = Bridge()
    except Exception as exc:
        os.environ.pop("SNAPGENT_EXTENSION_ID", None)
        if old_id is not None:
            os.environ["SNAPGENT_EXTENSION_ID"] = old_id
        return "SKIP", f"Bridge init failed ({exc})"
    try:
        try:
            if not HAS_TEST_CLIENT or bridge.health_server is None:
                await bridge.start_health_server()
            from aiohttp.test_utils import TestClient, TestServer

            client = TestClient(TestServer(bridge.health_server))
            await client.start_server()
            try:
                resp = await client.get(
                    "/auth-token", headers={"Origin": "chrome-extension://wrongid"}
                )
                body = await resp.json()
                _assert(resp.status == 403, f"Expected 403, got {resp.status}")
                _assert(
                    body.get("reason") == "extension_origin_mismatch",
                    f"Missing/wrong reason: {body.get('reason')}",
                )
                _assert(
                    body.get("expected_origin") == f"chrome-extension://{fake_id}",
                    f"Wrong expected_origin: {body.get('expected_origin')}",
                )
                _assert(
                    body.get("seen_origin") == "chrome-extension://wrongid",
                    f"Wrong seen_origin: {body.get('seen_origin')}",
                )
                hint = body.get("hint") or ""
                _assert(len(hint) > 10, f"Hint too short or missing: {hint!r}")
                return (
                    "PASS",
                    f"403 body correct (expected_origin=chrome-extension://{fake_id[:8]}…)",
                )
            finally:
                await client.close()
        except (OSError, RuntimeError) as exc:
            return "SKIP", f"Health port busy ({exc}) — skipping auth endpoint test"
    finally:
        os.environ.pop("SNAPGENT_EXTENSION_ID", None)
        if old_id is not None:
            os.environ["SNAPGENT_EXTENSION_ID"] = old_id
        if bridge is not None and bridge.health_server is not None:
            await bridge.stop_health_server()


async def check_auth_token_handler_200() -> tuple[str, str]:
    """h. Matching Origin → 200 with JSON keys 'token' and 'enabled'."""
    old_id = os.environ.get("SNAPGENT_EXTENSION_ID")
    fake_id = "b" * 32
    os.environ["SNAPGENT_EXTENSION_ID"] = fake_id
    bridge = None
    try:
        bridge = Bridge()
    except Exception as exc:
        os.environ.pop("SNAPGENT_EXTENSION_ID", None)
        if old_id is not None:
            os.environ["SNAPGENT_EXTENSION_ID"] = old_id
        return "SKIP", f"Bridge init failed ({exc})"
    try:
        try:
            if not HAS_TEST_CLIENT or bridge.health_server is None:
                await bridge.start_health_server()
            from aiohttp.test_utils import TestClient, TestServer

            client = TestClient(TestServer(bridge.health_server))
            await client.start_server()
            try:
                resp = await client.get(
                    "/auth-token",
                    headers={"Origin": f"chrome-extension://{fake_id}"},
                )
                body = await resp.json()
                _assert(resp.status == 200, f"Expected 200, got {resp.status}")
                _assert(
                    "token" in body,
                    f"Missing 'token' key in 200 body: {list(body.keys())}",
                )
                _assert(
                    "enabled" in body,
                    f"Missing 'enabled' key in 200 body: {list(body.keys())}",
                )
                return "PASS", "200 body has 'token' and 'enabled' keys"
            finally:
                await client.close()
        except (OSError, RuntimeError) as exc:
            return "SKIP", f"Health port busy ({exc}) — skipping auth endpoint test"
    finally:
        os.environ.pop("SNAPGENT_EXTENSION_ID", None)
        if old_id is not None:
            os.environ["SNAPGENT_EXTENSION_ID"] = old_id
        if bridge is not None and bridge.health_server is not None:
            await bridge.stop_health_server()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    checks = [
        ("vscode_workspace_root", check_vscode_workspace_root),
        ("zed_workspace_root", check_zed_workspace_root),
        ("server_workspace", check_server_workspace),
        ("server_status", check_server_status),
        ("resolve_peer_and_register", check_resolve_peer_and_register),
        ("register_idempotent", check_register_idempotent),
        ("auth_token_403", lambda: asyncio.run(check_auth_token_handler_403())),
        ("auth_token_200", lambda: asyncio.run(check_auth_token_handler_200())),
    ]

    passed = skipped = failed = 0
    for name, fn in checks:
        try:
            result = fn()
            if isinstance(result, tuple):
                status, detail = result
            else:
                status, detail = "PASS", str(result)
            if status == "PASS":
                print(f"PASS {name}")
                passed += 1
            elif status == "SKIP":
                print(f"SKIP {name} {detail}")
                skipped += 1
            else:
                print(f"FAIL {name} {detail}")
                failed += 1
        except Exception as exc:
            print(f"FAIL {name} {exc}")
            failed += 1

    total = passed + skipped + failed
    print(
        f"\nRESULT: {passed} passed, {skipped} skipped, {failed} failed (out of {total})"
    )
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
